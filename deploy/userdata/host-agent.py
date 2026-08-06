#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""OpenClaw Host Agent — probes local VMs and writes health status to DynamoDB.
Replaces per-tenant SSM health checks. Runs as systemd service on each host.
"""

import json
import os
import random
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import boto3
from botocore.config import Config as BotoConfig

# P2b (2026-07-08): route_ops lives in the same userdata directory. Add it to
# sys.path so the tenant-route helpers (port bitmap / DNAT / Redis writer /
# drift reconcile) resolve when host-agent runs as a systemd ExecStart from
# /opt/openclaw/. See engineering/00-knowledge-base/SPEC/11-ENGINE-TRANSFORM/
# INTERFACE-CONTRACT.md §1/§3/§4/§6/§8.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route_ops  # noqa: E402

POLL_INTERVAL = int(os.environ.get("OC_AGENT_POLL_INTERVAL", "15"))
PORT = int(os.environ.get("OC_AGENT_PORT", "8899"))
# Health/control AND Prometheus /metrics are served by the SAME HTTPServer
# on PORT (8899). The earlier OC_AGENT_PROM_PORT=9090 split-port design was
# never wired into main(), causing the ADOT collector to fail every scrape
# (issue #4 regression, fixed in 1.2.5).
VM_DIR = "/data/firecracker-vms"
GATEWAY_PORT = 18789
TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "")
# Since 1.3.0: host-agent also writes a heartbeat to the hosts table so the
# health_check Lambda can do AZ-level failover (it needs to know which hosts
# are still alive at the host level, not just whether their tenants reported).
HOSTS_TABLE = os.environ.get("HOSTS_TABLE", "")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "")
# #394 — Host-local slot pointer is authoritative. Heartbeat mirrors it continuously so
# global snapshot deletion can fail closed on a freshness timestamp instead of trusting a
# one-shot best-effort write from the mutation that may have failed.
IMAGE_SLOTS_FILE = os.environ.get("IMAGE_SLOTS_FILE", "/data/firecracker-assets/slots.json")
# P2b: ElastiCache primary endpoint DNS name (contract §8 — never a node IP).
# Empty string disables Redis writes; host-agent still writes DDB as normal
# so an ungated deploy stays backwards-compatible with pre-P2 environments.
ENGINE_REDIS_ENDPOINT = os.environ.get("ENGINE_REDIS_ENDPOINT", "")
ENGINE_REDIS_PORT = int(os.environ.get("ENGINE_REDIS_PORT", "6379"))


# ═══════════════════════════════════════════
# Prometheus exporter (issue #4)
# ═══════════════════════════════════════════
#
# Exposes a /metrics endpoint in Prom text-exposition format on the same
# HTTPServer (and therefore the same PORT, 8899) as /health, to avoid a
# second listener. An ADOT collector running as a sibling systemd service
# scrapes 127.0.0.1:8899/metrics and remote-writes to AMP.
#
# Why text-format and not the prometheus_client library?
# - We already have BaseHTTPRequestHandler. Adding prometheus_client just for
#   text rendering pulls in a dependency tree we don't otherwise need.
# - The exposition format is stable and trivially correct to emit by hand.
# - Pure function (input dict → string) is testable without a live server.

_PROM_GAUGES = (
    (
        "openclaw_vm_memory_used_mb",
        "Per-VM memory in active use (MB)",
        "memory_used_mb",
    ),
    (
        "openclaw_vm_memory_balloon_mib",
        "Balloon size held by the host (MiB)",
        "memory_balloon_mib",
    ),
    ("openclaw_vm_disk_used_mb", "Per-VM data disk used (MB)", "disk_used_mb"),
    ("openclaw_vm_disk_total_mb", "Per-VM data disk capacity (MB)", "disk_total_mb"),
    ("openclaw_vm_disk_used_pct", "Per-VM data disk used (percent)", "disk_used_pct"),
    ("openclaw_vm_cpu_pct", "Per-VM CPU usage (percent of allocated vcpus)", "cpu_pct"),
)


def _render_metrics_text(snapshots, port_stats=None, agent_stats=None):
    """Render the in-memory snapshots dict as Prometheus exposition text.

    Pure function — no I/O — so it is easy to assert against in unit tests.
    Always emits HELP/TYPE headers (even with zero samples) so that scrapers
    that validate metadata don't choke on a quiet host.

    #387: port_stats/agent_stats are OPTIONAL dict snapshots taken by the
    caller (do_GET) from already-initialized singletons/state. This function
    MUST NOT touch _get_port_bitmap() or any lazy-init singleton — rendering
    a metrics page must never mutate global routing state. When port_stats
    is None (bitmap not yet initialized, i.e. no route was ever allocated on
    this host) the three openclaw_host_dnat_ports_* series are ABSENT —
    deliberately no fake zeros, so dashboards can tell "no data yet" from
    "0 ports used".
    """
    out = []
    for metric_name, help_text, key in _PROM_GAUGES:
        out.append(f"# HELP {metric_name} {help_text}")
        out.append(f"# TYPE {metric_name} gauge")
        for tid, info in snapshots.items():
            metrics = info.get("metrics") if isinstance(info, dict) else None
            if not metrics:
                continue
            value = metrics.get(key, 0)
            out.append(f'{metric_name}{{tenant="{tid}"}} {int(value)}')
    # vm_health as 0/1 — useful for alerting even when metrics are missing.
    out.append("# HELP openclaw_vm_health 1 if the VM responded to ping, else 0")
    out.append("# TYPE openclaw_vm_health gauge")
    for tid, info in snapshots.items():
        if not isinstance(info, dict):
            continue
        v = 1 if info.get("vm_health") == "up" else 0
        out.append(f'openclaw_vm_health{{tenant="{tid}"}} {v}')
    # #387 (#197 病史): app_health as 0/1 — gateway HTTP liveness per tenant.
    # vm_health=1 + app_health=0 is exactly the "ping ok, gateway crashloop"
    # blind spot (#197: gateway crashed 2715x while vm_health stayed green).
    out.append(
        "# HELP openclaw_app_health 1 if the tenant gateway answered HTTP, else 0"
    )
    out.append("# TYPE openclaw_app_health gauge")
    for tid, info in snapshots.items():
        if not isinstance(info, dict):
            continue
        v = 1 if info.get("app_health") == "up" else 0
        out.append(f'openclaw_app_health{{tenant="{tid}"}} {v}')
    # #387 容量红线: host DNAT port pool watermark. Port exhaustion means new
    # VMs can never be promoted (PortAllocationError → skip-promote forever).
    # HELP semantics: "used" INCLUDES stopped tenants — stop/start keeps the
    # route (and its port); only delete releases it. quarantined = ports in
    # cooldown, not yet re-allocatable, and NOT counted in used.
    if isinstance(port_stats, dict) and port_stats:
        out.append(
            "# HELP openclaw_host_dnat_ports_used DNAT ports currently allocated"
            " (includes stopped tenants: stop keeps the route+port, only delete"
            " frees it)"
        )
        out.append("# TYPE openclaw_host_dnat_ports_used gauge")
        out.append(f"openclaw_host_dnat_ports_used {int(port_stats['used'])}")
        out.append(
            "# HELP openclaw_host_dnat_ports_total total slots in the DNAT port"
            " range (high-low+1, from runtime env)"
        )
        out.append("# TYPE openclaw_host_dnat_ports_total gauge")
        out.append(f"openclaw_host_dnat_ports_total {int(port_stats['total'])}")
        out.append(
            "# HELP openclaw_host_dnat_ports_quarantined ports inside the range"
            " in cooldown (not in used, not allocatable yet)"
        )
        out.append("# TYPE openclaw_host_dnat_ports_quarantined gauge")
        out.append(
            f"openclaw_host_dnat_ports_quarantined {int(port_stats['quarantined'])}"
        )
    # #387 v4: agent self-health/operational metrics (kata_agent_*/kubelet_
    # runtime_operations_* layer). All host-level; kept out of the tenant map.
    if isinstance(agent_stats, dict):
        ticks = agent_stats.get("loop_last_tick") or {}
        if ticks:
            out.append(
                "# HELP openclaw_agent_loop_last_tick_epoch unix epoch of each"
                " background loop's last completed iteration (absent label ="
                " loop not enabled; stale value = loop hung while HTTP still"
                " serves old snapshots)"
            )
            out.append("# TYPE openclaw_agent_loop_last_tick_epoch gauge")
            for loop_name, epoch in sorted(ticks.items()):
                out.append(
                    f'openclaw_agent_loop_last_tick_epoch{{loop="{loop_name}"}}'
                    f" {int(epoch)}"
                )
        sha = agent_stats.get("build_sha")
        if sha:
            out.append(
                "# HELP openclaw_agent_build_info full sha256 of this agent's"
                " own file, computed once at process start (NOT git sha) —"
                " exposes disk-new/process-old drift (#373)"
            )
            out.append("# TYPE openclaw_agent_build_info gauge")
            out.append(f'openclaw_agent_build_info{{sha="{sha}"}} 1')
        if "route_ensure_failures" in agent_stats:
            out.append(
                "# HELP openclaw_route_ensure_failures_total promote-blocking"
                " route failures: _ensure_route raised (port exhausted /"
                " iptables broke) OR returned degraded (host_ip/port missing)"
                " — both leave the tenant stuck at creating"
            )
            out.append("# TYPE openclaw_route_ensure_failures_total counter")
            out.append(
                "openclaw_route_ensure_failures_total"
                f" {int(agent_stats['route_ensure_failures'])}"
            )
        ssm = agent_stats.get("ssm_agent_up")
        if ssm is not None:
            out.append(
                "# HELP openclaw_host_ssm_agent_up 1 if the local SSM agent"
                " systemd unit is active, else 0. Local-active does NOT imply"
                " control-plane PingStatus=Online (network/IAM breakage is"
                " invisible here); the authoritative signal stays with the"
                " control plane. This scrape path is independent of SSM, so"
                " a dead SSM agent is still reportable."
            )
            out.append("# TYPE openclaw_host_ssm_agent_up gauge")
            out.append(f"openclaw_host_ssm_agent_up {1 if ssm else 0}")
    return "\n".join(out) + "\n"


# Balloon config (from /etc/platform.env)
BALLOON_ENABLED = os.environ.get("BALLOON_ENABLED", "false") == "true"
BALLOON_MAX_INFLATE_RATIO = float(os.environ.get("BALLOON_MAX_INFLATE_RATIO", "0.4"))
BALLOON_MIN_GUEST_AVAILABLE_MB = int(
    os.environ.get("BALLOON_MIN_GUEST_AVAILABLE_MB", "512")
)

# DynamoDB client (region auto-detected from instance metadata)
_ddb = None
_status = {}
_lock = threading.Lock()

# #387 v4: agent self-metrics live in their OWN dict — NOT in _status.
# _status is a tenant map; a host-level key mixed in would be rendered as a
# phantom tenant by the per-tenant gauge loops above. Guarded by _lock (same
# lock as _status: all writers already hold it or write scalar values).
_agent_metrics = {
    "loop_last_tick": {},  # loop_name -> unix epoch of last completed pass
    "route_ensure_failures": 0,  # counter: skip-promote route failures
    # "build_sha" set once in main(); "ssm_agent_up" set by poll loop probe.
}


def _agent_loop_tick(loop_name: str) -> None:
    """#387: each background loop stamps ITS OWN heartbeat at the end of every
    pass (never stamped centrally — a central stamper would defeat the whole
    point of detecting an individual hung loop)."""
    with _lock:
        _agent_metrics["loop_last_tick"][loop_name] = int(time.time())


def _probe_ssm_agent() -> None:
    """#387: cache the local SSM agent unit state (probed from the poll loop,
    NEVER at scrape time). Unit name verified on real hosts 2026-07-26:
    snap.amazon-ssm-agent.amazon-ssm-agent.service. Any failure mode
    (inactive / timeout / systemctl missing) records 0 — fail-loud."""
    try:
        r = subprocess.run(
            [
                "systemctl",
                "is-active",
                "snap.amazon-ssm-agent.amazon-ssm-agent.service",
            ],
            capture_output=True,
            timeout=2,
            check=False,
        )
        up = (r.stdout or b"").decode().strip() == "active"
    except Exception:
        up = False
    with _lock:
        _agent_metrics["ssm_agent_up"] = up

# P2b: cached host private IP (contract §4 descriptor.host_private_ip). IMDS
# is 100% reliable but avoids repeat calls per poll cycle.
_host_private_ip: str | None = None
_host_private_ip_lock = threading.Lock()

# P2b: port bitmap + Redis writer singletons. Both are optional — a pre-P2
# deployment without ENGINE_REDIS_ENDPOINT can run this agent unchanged.
_port_bitmap: route_ops.PortBitmap | None = None
_redis_writer: route_ops.RedisRouteWriter | None = None
_route_singleton_lock = threading.Lock()


def _imdsv2(path: str, ttl_sec: int = 60) -> str:
    """Fetch a single IMDSv2 metadata field. Returns "" on any failure.
    Same PUT-then-GET dance as _get_ddb — extracted so region and
    local-ipv4 lookups share one code path."""
    import urllib.request  # noqa: PLC0415

    try:
        tok = (
            urllib.request.urlopen(
                urllib.request.Request(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
                    "http://169.254.169.254/latest/api/token",  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-urllib-request
                    headers={"X-aws-ec2-metadata-token-ttl-seconds": str(ttl_sec)},
                    method="PUT",
                ),
                timeout=2,
            )
            .read()
            .decode()
        )
        return (
            urllib.request.urlopen(
                urllib.request.Request(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
                    f"http://169.254.169.254/latest/meta-data/{path}",  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-urllib-request
                    headers={"X-aws-ec2-metadata-token": tok},
                ),
                timeout=2,
            )
            .read()
            .decode()
        )
    except Exception:
        return ""


def _get_host_private_ip() -> str:
    """Contract §1/§4: `host_private_ip` = VPC private IPv4 of this host,
    read from IMDS. Cached — the value is fixed for the lifetime of an
    EC2 instance. Empty string on failure so promotion can degrade
    without crashing."""
    global _host_private_ip
    if _host_private_ip is not None:
        return _host_private_ip
    with _host_private_ip_lock:
        if _host_private_ip is not None:
            return _host_private_ip
        _host_private_ip = _imdsv2("local-ipv4").strip()
        return _host_private_ip


def _get_port_bitmap() -> route_ops.PortBitmap:
    """Contract §3: 401-slot port bitmap. On first call, rebuild from the
    live iptables PREROUTING rules so host-agent restarts don't reallocate
    ports still owned by running microVMs (idempotency: rules survive the
    process; the in-memory bookkeeping does not)."""
    global _port_bitmap
    if _port_bitmap is not None:
        return _port_bitmap
    with _route_singleton_lock:
        if _port_bitmap is not None:
            return _port_bitmap
        b = route_ops.PortBitmap()
        try:
            n = route_ops.rebuild_bitmap_from_iptables(b)
            print(f"port_bitmap: recovered {n} in-use ports from iptables")
        except Exception as e:
            # Fail-open on bootstrap: an empty bitmap is safer than crashing
            # the agent. New allocs will alloc lowest-free; a colliding rule
            # (rare — only if bootstrap really failed) surfaces at -C check.
            print(f"port_bitmap: rebuild failed (starting empty): {e}")
        _port_bitmap = b
        return _port_bitmap


def _get_redis_writer() -> route_ops.RedisRouteWriter | None:
    """Contract §1/§6/§8: Redis route writer, only wired when the endpoint
    is configured. Returns None otherwise so callers know to skip cleanly."""
    global _redis_writer
    if _redis_writer is not None:
        return _redis_writer
    if not ENGINE_REDIS_ENDPOINT:
        return None
    with _route_singleton_lock:
        if _redis_writer is not None:
            return _redis_writer
        _redis_writer = route_ops.RedisRouteWriter(
            primary_endpoint=ENGINE_REDIS_ENDPOINT,
            port=ENGINE_REDIS_PORT,
        )
        return _redis_writer


def _ensure_route(tenant_id: str, guest_ip: str) -> tuple[str, int | None]:
    """Contract §3: allocate a host port + write PREROUTING DNAT for this
    tenant, then §1: write `route:{tenant_id}` to Redis. Idempotent per
    tenant: if the DDB descriptor already carries a host_port and the
    iptables rule exists, we reuse the port (no reallocation).

    Returns (host_private_ip, host_port). host_port is None only if
    the alloc + DNAT itself failed (fail-loud upstream — this shouldn't
    normally happen once the bitmap is initialised).

    Redis write failure does NOT propagate: contract §6 HA — DDB is
    authoritative and route.lua fail-static handles the transient gap.
    """
    host_ip = _get_host_private_ip()
    bitmap = _get_port_bitmap()
    port = route_ops.ensure_port_and_dnat(bitmap, guest_ip)
    writer = _get_redis_writer()
    if writer is not None and host_ip:
        writer.set_route(tenant_id, host_ip, port, guest_ip)
    return host_ip, port


def _release_route(tenant_id: str, host_port: int | None, guest_ip: str) -> None:
    """Symmetric release. Best-effort: any single component failing must
    not block the others (crash recovery calls this)."""
    if host_port is not None and guest_ip:
        try:
            route_ops.release_port_and_dnat(_get_port_bitmap(), host_port, guest_ip)
        except Exception as e:
            print(f"release_port_and_dnat({host_port},{guest_ip}) failed: {e}")
    writer = _get_redis_writer()
    if writer is not None:
        writer.del_route(tenant_id)


def _resolve_region():
    """Region: env override first, else IMDSv2, else ap-northeast-1 fallback.
    Shared by _get_ddb and the #340 disk-report thread's independent resource so
    neither reimplements IMDS."""
    env = os.environ.get("OC_REGION") or os.environ.get("AWS_REGION")
    if env:
        return env
    # Get region from IMDS (IMDSv2 with session token)
    # EC2 IMDS only supports http:// on link-local 169.254.169.254
    try:
        import urllib.request

        tok = (
            urllib.request.urlopen(
                urllib.request.Request(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
                    "http://169.254.169.254/latest/api/token",  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-urllib-request
                    headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                    method="PUT",
                ),
                timeout=2,
            )
            .read()
            .decode()
        )
        return (
            urllib.request.urlopen(
                urllib.request.Request(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
                    "http://169.254.169.254/latest/meta-data/placement/region",  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-urllib-request
                    headers={"X-aws-ec2-metadata-token": tok},
                ),
                timeout=2,
            )
            .read()
            .decode()
        )
    except Exception:
        return "ap-northeast-1"  # fallback default (env+IMDSv2 unavailable)


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource(
            "dynamodb",
            region_name=_resolve_region(),
            config=BotoConfig(retries={"max_attempts": 2}),
        )
    return _ddb


_recovering = set()  # Track VMs being recovered to avoid duplicate launches
# #315 — recover 失败退避时刻表 tenant_id → earliest-retry-epoch。修 _recovering 泄漏后,
# 失败的 recover 不再每 5s 紧密重试(thundering herd:同 tid 反复 fire launch-vm 抢 flock 空转),
# 按 RECOVER_BACKOFF_SEC + jitter 退避;成功即清除。
_recover_backoff = {}
RECOVER_BACKOFF_SEC = float(os.environ.get("OC_RECOVER_BACKOFF", "15"))

# #208 — 已回填过 phys_vm_num 的 tenant_id(每进程只写一次,避免每 tick 重复 update)。
# if_not_exists 保证幂等且绝不覆盖已有值(迁移后 phys_vm_num 必须恒等原始 launch 号),
# 这个 set 只是省掉重复的 no-op 写。
_phys_backfilled = set()

# Dead-zone guard: FC alive but guest unreachable (e.g. TAP DOWN after a partial
# launch) is invisible to _recover_vm, which only fires when FC is absent. After
# this many consecutive unreachable polls, force a stop+relaunch.
_net_dead_polls = {}  # tenant_id -> consecutive polls: fc alive, guest unreachable
_NET_DEAD_THRESHOLD = 3


def _register_net_poll(tenant_id, guest_reachable):
    """Track consecutive 'FC alive but guest unreachable' polls. Returns True
    when the count crosses _NET_DEAD_THRESHOLD (caller should force-relaunch).
    A reachable poll resets the counter. Pure/testable — no I/O."""
    if guest_reachable:
        _net_dead_polls.pop(tenant_id, None)
        return False
    n = _net_dead_polls.get(tenant_id, 0) + 1
    _net_dead_polls[tenant_id] = n
    if n >= _NET_DEAD_THRESHOLD:
        _net_dead_polls.pop(tenant_id, None)
        return True
    return False


def _launch_argv(*args):
    """Wrap a shell command so its stdout+stderr land in journald under the
    `claw-launch` syslog tag. R12 (#218): launch-vm.sh emits FATAL fail-loud
    diagnostics (#199 root cause) that were being dropped by DEVNULL on all
    three launch paths, so bad rootfs / DDB timeouts / bind-mount failures
    left no trace. Capturing to journald keeps triage traceable without
    opening a control-plane back-channel.
    Query with: `journalctl -t claw-launch [-u host-agent -f]`.
    """
    return ["systemd-cat", "-t", "claw-launch", *args]


def _recover_vm(tenant_id, cfg):
    """Launch VM that has vm.json but no running Firecracker process.

    #315 修 _recovering 永久泄漏(真机 28/300 卡 creating 根因):旧版只在 Popen 抛异常时
    discard,起 VM 子进程失败(flock-skip rc75 / START 后被杀 / 半成品)【不抛异常】→
    _recovering 永留 → 下个 probe `if tid in _recovering: return` 永久跳过 → recover 只试
    一次、失败即永久卡(host-agent.py 的 discard 只在 fc_running=True 才走到)。
    修:Popen fire-and-forget(不等子进程,不设 timeout——`subprocess.call(timeout=)` 超时会
    p.kill() 腰斩满载几十秒的 launch,codex review6 P1),**finally 释放 _recovering** 让下个
    probe 能重试,并记 backoff 防每 5s 紧密重试的 thundering herd。真正清 backoff/_recovering
    交给 _probe_all 的 fc_running=True 分支(确认 FC 真活才算 recover 成功);flock 防同租户重复起。
    """
    if tenant_id in _recovering:
        return
    # backoff:上次 recover 未到重试时刻 → 本 tick 跳过(不占 flock、不踩踏)。
    now = time.time()
    due = _recover_backoff.get(tenant_id, 0)
    if due and now < due:
        return
    _recovering.add(tenant_id)
    vm_num = cfg.get("vm_num", 1)
    vcpu = cfg.get("vcpu", 2)
    mem_mb = cfg.get("mem_mb", 4096)
    print(f"recovering {tenant_id} (vm{vm_num} {vcpu}vCPU/{mem_mb}MB)")
    try:
        subprocess.Popen(
            _launch_argv(
                "bash",
                "/home/ubuntu/launch-vm.sh",
                str(tenant_id),
                str(vm_num),
                str(vcpu),
                str(mem_mb),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"recover {tenant_id} failed: {e}")
    finally:
        # fire-and-forget:Popen 返回≠FC 真活,记 backoff 防下 probe 立即再 fire;
        # 真正清 backoff + _recovering 交给 _probe_all 确认 fc_running=True 的分支。
        _recovering.discard(tenant_id)
        _recover_backoff[tenant_id] = time.time() + _jitter(RECOVER_BACKOFF_SEC)


def _force_relaunch_vm(tenant_id, cfg):
    """stop-vm + launch-vm to rebuild a VM whose FC is alive but guest network
    is dead. Unlike _recover_vm, handles the FC-alive case."""
    if tenant_id in _recovering:
        return
    _recovering.add(tenant_id)
    vm_num = cfg.get("vm_num", 1)
    vcpu = cfg.get("vcpu", 2)
    mem_mb = cfg.get("mem_mb", 4096)
    print(
        f"force-relaunch {tenant_id} (vm{vm_num}): FC alive but guest unreachable "
        f"for {_NET_DEAD_THRESHOLD} polls — rebuilding network"
    )
    try:
        subprocess.run(
            _launch_argv(
                "bash", "/home/ubuntu/stop-vm.sh", str(tenant_id), str(vm_num)
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.Popen(
            _launch_argv(
                "bash",
                "/home/ubuntu/launch-vm.sh",
                str(tenant_id),
                str(vm_num),
                str(vcpu),
                str(mem_mb),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"force-relaunch {tenant_id} failed: {e}")
        _recovering.discard(tenant_id)


def _probe_all():
    """Probe all local VMs."""
    results = {}
    try:
        entries = os.listdir(VM_DIR)
    except FileNotFoundError:
        return results

    for tenant_id in entries:
        vm_path = os.path.join(VM_DIR, tenant_id)
        cfg_file = os.path.join(vm_path, "vm.json")
        if not os.path.isfile(cfg_file):
            continue

        try:
            with open(cfg_file, encoding="utf-8") as f:
                cfg = json.load(f)
            # #237 — the LOCAL vm.json's guest_ip is the data-plane truth of
            # what actually booted (launch-vm.sh writes it from the /30 scheme).
            # _write_ddb reconciles the DDB record's guest_ip to this so a
            # control-plane↔data-plane drift can't leave a ghost IP that edge
            # routes to a black hole. (vm_num is deliberately NOT reconciled —
            # see _refresh_health: the logical↔physical split is #208's job.)
            guest_ip = cfg.get("guest_ip", "")
        except Exception:
            continue
        if not guest_ip:
            continue

        # #208 — vm.json 的 vm_num 是这台 VM 物理挂的 tap-vm{N} 号(launch-vm.sh:172 写,
        # 迁移 restore 也不改写)。带回 results 供 _report 回填 DDB 的 phys_vm_num——历史
        # /迁移前建的租户 DDB 里没有 phys_vm_num,靠这里从物理真值补齐,补齐后撞号检查
        # (create+migrate)才对这些老租户也生效。
        phys_vm_num = cfg.get("vm_num")

        # Skip intentionally stopped VMs
        stopped_marker = os.path.join(vm_path, ".stopped")
        if os.path.exists(stopped_marker):
            continue

        # Auto-recover: vm.json exists but Firecracker not running.
        # Capture pid here too so the metrics composer can read /proc/<pid>
        # without re-running pgrep on every gauge.
        sock_file = os.path.join(vm_path, "fc.sock")
        pgrep = subprocess.run(
            ["pgrep", "-f", f"api-sock {sock_file}"], capture_output=True, text=True
        )
        fc_pid = None
        if pgrep.returncode == 0:
            pids = pgrep.stdout.strip().split()
            if pids:
                try:
                    fc_pid = int(pids[0])
                except (ValueError, IndexError):
                    pass
        fc_running = fc_pid is not None

        if not fc_running:
            _recover_vm(tenant_id, cfg)
            results[tenant_id] = {
                "vm_health": "recovering",
                "app_health": "down",
                "guest_ip": guest_ip,
                "phys_vm_num": phys_vm_num,
            }
            continue

        _recovering.discard(tenant_id)
        # #315(codex review4 #4)—— FC 进程真活了才算 recover 成功 → 清 backoff。
        # recover 里对任何尝试(含 rc==0)都记了 backoff,只有这里(确认 fc_running=True)才清,
        # 避免"launch-vm 退 0 但 FC 随即死"被误判成功后每 probe 重启。
        _recover_backoff.pop(tenant_id, None)

        vm_health = "down"
        app_health = "down"

        try:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", "2", guest_ip], capture_output=True, timeout=5
            )
            if r.returncode == 0:
                vm_health = "up"
        except Exception:
            pass

        if vm_health == "up":
            _register_net_poll(tenant_id, guest_reachable=True)
            try:
                # app_health = gateway 的 HTTP server 是否在应答(不是某个具体路由存不存在)。
                # 病史:老版探 `/` 用 `curl -f`,404 被当失败误报 down;改探 `/healthz` 期望
                # 200——但 openclaw 2026.2.26 的 gateway **没有 /healthz 端点,任何路径都 404**
                # (实测 18789 上 / /healthz /health /ping 全 404,gateway 进程却正常在跑),
                # 于是全 fleet app_health=down、promotion gate 卡在 creating。根因是"探一个
                # 版本相关的具体路由"这个思路本身脆——不同 openclaw 版本 health 路由不一样。
                # 改判据:**端口有 HTTP 应答即 gateway 活**(连得上并返回任意 HTTP 状态码,
                # 含 404/401/200),而 curl 退出码 7(拒连)/28(超时)/000 才是真 down。
                # 这样既不误报 2.26 的 404,又保留"gateway 没起/端口不通=down"的判活能力。
                # 不再用 -f;用 -w %{http_code} 拿状态码,非 000 = HTTP server 在应答。
                r = subprocess.run(
                    [
                        "curl",
                        "-s",
                        "-o",
                        "/dev/null",
                        "-w",
                        "%{http_code}",
                        "--connect-timeout",
                        "3",
                        f"http://{guest_ip}:{GATEWAY_PORT}/",
                    ],
                    capture_output=True,
                    timeout=8,
                )
                code = (r.stdout or b"").decode(errors="replace").strip()
                # 000 = 没连上/无 HTTP 响应(curl 连接失败时 http_code 为 000);
                # 任何真实 HTTP 码(2xx/4xx/401/404…)= gateway HTTP server 活着在应答。
                if r.returncode == 0 and code.isdigit() and code != "000":
                    app_health = "up"
            except Exception:
                pass
        elif _register_net_poll(tenant_id, guest_reachable=False):
            # FC alive but guest unreachable past the threshold — rebuild network.
            _force_relaunch_vm(tenant_id, cfg)
            results[tenant_id] = {
                "vm_health": "recovering",
                "app_health": "down",
                "guest_ip": guest_ip,
                "phys_vm_num": phys_vm_num,
            }
            continue

        results[tenant_id] = {
            "vm_health": vm_health,
            "app_health": app_health,
            "guest_ip": guest_ip,
            "fc_pid": fc_pid,
            "phys_vm_num": phys_vm_num,
        }

    return results


# P2b (contract §4, 2026-07-08): removed _read_gateway_token and
# _read_channel_secret. gateway_token is P1's job — control-plane pre-mints
# the KMS-envelope ciphertext and writes it to `openclaw-tenant-secrets`
# at create time; host-agent no longer SSH-reads a plaintext token from the
# guest. channel_secret is being retired with the claw-channel wss data path
# (P3/P4). Host→VM SSH management channel remains via /etc/openclaw/host_vm_key
# for other uses (guest health probes, launch-vm cleanup) — no callers remain
# here.


VM_DATA_MOUNT = "/data"
# #340 磁盘上报独立线程节奏。默认 15s(= poll 默认),但走自己的线程不被 VM probe 拖累。
# 消费侧 TTL(DISPATCH_DISK_REPORT_TTL_SEC,默认 90s)是它的数倍,容几次漏报不误判陈旧。
_DISK_REPORT_INTERVAL_SEC = int(os.environ.get("OC_DISK_REPORT_INTERVAL_SEC", "15"))

# #340 — 磁盘上报线程【专属】DDB resource。boto3 Session/Resource 非线程安全(AWS 官方),
# 全局 _get_ddb() 的 _ddb 已被 poll/GC/dispatch 共享,故磁盘线程用【独立 Session】完全绕开它
# (codex review:之前从 _get_ddb() 取 region 又碰了共享对象)。超时收紧到远小于消费侧 TTL:
# 默认 connect/read 各 60s + 重试,最坏可远超 90s TTL → 写卡顿时报告陈旧、消费侧 fail-open,
# 满盘 host 又接单。connect 3s + read 5s + 总尝试 2 次,最坏 ~16s << 90s,写慢也不会拖成陈旧。
_disk_ddb = None
_DISK_DDB_CFG = BotoConfig(
    connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2}
)


def _get_disk_ddb():
    """磁盘上报线程专属 DDB resource(独立 Session,线程隔离 + 短超时;codex review)。"""
    global _disk_ddb
    if _disk_ddb is None:
        _disk_ddb = boto3.Session().resource(
            "dynamodb", region_name=_resolve_region(), config=_DISK_DDB_CFG
        )
    return _disk_ddb


def _data_disk_free_mb():
    """#340 — host /data 物理剩余可用空间(MB),给 dispatch 的磁盘软门用。

    返回值三态(codex review:区分"确诊坏"和"测不了"):
    - int ≥ 0:正常读数(statvfs f_bavail×f_frsize,非特权可写块,已扣 root 保留);
    - 0:**/data 未挂载**——确诊的坏状态(init-host `mount ${DATA_DEV} /data` 失败,目录落
      根盘;派租户过去 launch-vm `mkdir /data/firecracker-vms` 会写进根盘/起在错盘)。上报 0
      让消费侧【阻断】这台 host,不能 fail-open 放行(那才会把租户写进根盘);
    - None:**探测本身失败**(statvfs 抛异常,权限/瞬时 IO)——测不了,unknown,调用方不写字段,
      消费侧 fail-open(与"没上报"同待,交存活兜底,不拿一次瞬时抖动误摘)。
    绝不抛(上报线程不能因取磁盘失败崩)。"""
    try:
        if not os.path.ismount(VM_DATA_MOUNT):
            # 确诊坏:未挂载。上报 0 让消费侧阻断(不是 None——未挂载是明确的"不可用",
            # 不能 fail-open,否则租户被派来写进根盘)。
            print(f"{VM_DATA_MOUNT} not a mountpoint — report 0 (block this host)")
            return 0
        st = os.statvfs(VM_DATA_MOUNT)
        return int(st.f_bavail * st.f_frsize // (1024 * 1024))
    except Exception as e:  # noqa: BLE001
        # 测不了(unknown):statvfs 抛。返 None → 不上报 → 消费侧 fail-open(不拿一次瞬时
        # 探测失败误摘健康 host;交存活兜底)。
        print(f"data disk statvfs failed (non-fatal): {e}")
        return None


def _write_disk_report():
    """#340 — 上报 host /data 物理剩余(avail_disk_mb + disk_check_ts_epoch)给 dispatch
    磁盘软门。【独立线程】跑(_disk_report_loop),不塞进 poll 心跳。

    为什么独立线程(codex score:关键):poll 心跳同一轮还要串行 probe 所有 VM(几十个
    curl,慢 host 累计可超 TTL),若磁盘时间戳搭 poll 便车,一台【健康但 VM 多/probe 慢】
    的 host 会周期性上报陈旧 → 被消费侧误摘。剥离到独立快循环后,磁盘时间戳只反映"磁盘
    探测这件小事跑没跑",陈旧才真正 ⟺ agent 没在跑(与 _disk_gc_loop 同款隔离理由,#321)。
    取不到磁盘(非挂载点/statvfs 失败)→ 不写字段(消费侧缺字段 fail-open)。best-effort。"""
    if not HOSTS_TABLE or not INSTANCE_ID:
        return
    free_mb = _data_disk_free_mb()
    if free_mb is None:
        return  # 取不到 → 不写(消费侧缺 avail_disk_mb → fail-open)
    try:
        table = _get_disk_ddb().Table(HOSTS_TABLE)  # 线程专属 resource(非线程安全隔离)
        table.update_item(
            Key={"instance_id": INSTANCE_ID},
            UpdateExpression="SET avail_disk_mb = :d, disk_check_ts_epoch = :de",
            ExpressionAttributeValues={":d": free_mb, ":de": int(time.time())},
        )
    except Exception as e:  # noqa: BLE001
        print(f"disk report failed (non-fatal): {e}")


def _disk_report_loop():
    """#340 — 独立单例线程上报 /data 剩余(与 _disk_gc_loop/#321 同款隔离:一次 statvfs/DDB
    写卡住也不阻塞 poll 心跳,反之 poll 的慢 probe 也不拖累磁盘时间戳新鲜度)。"""
    while True:
        try:
            _write_disk_report()
        except Exception as e:  # noqa: BLE001
            print(f"disk-report loop error (non-fatal): {e}")
        _agent_loop_tick("disk_report")  # #387 self-stamped
        time.sleep(_DISK_REPORT_INTERVAL_SEC)


def _read_image_slots():
    """Read and validate the Host-authoritative slots pointer for heartbeat mirroring.

    Missing/unparseable state returns None and deliberately does not refresh the mirror
    timestamp. Snapshot deletion then treats the control-plane mirror as stale and refuses
    to delete rather than guessing that an unobserved Host reference does not exist.
    """
    try:
        with open(IMAGE_SLOTS_FILE, encoding="utf-8") as fh:
            slots = json.load(fh)
        if not isinstance(slots, dict):
            return None
        generation = int(slots.get("generation") or 0)
        return {
            "generation": generation,
            "live": slots.get("live"),
            "canary": slots.get("canary"),
            "previous_live": slots.get("previous_live"),
        }
    except (OSError, ValueError, TypeError):
        return None


def _write_host_heartbeat():
    """Update host liveness and continuously reconcile the slots.json DDB mirror.

    The slots freshness marker is written in the same UpdateItem as the mirror. If reading
    local slots fails, only liveness is updated; the old marker ages out and destructive
    snapshot deletion fails closed.
    """
    if not HOSTS_TABLE or not INSTANCE_ID:
        return
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        table = _get_ddb().Table(HOSTS_TABLE)
        slots = _read_image_slots()
        expression = "SET last_seen = :t, last_health_check = :t"
        values = {":t": ts}
        if slots is not None:
            expression += ", image_slots = :slots, image_slots_synced_at_epoch = :se"
            values.update({":slots": slots, ":se": int(time.time())})
        table.update_item(
            Key={"instance_id": INSTANCE_ID},
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
        )
    except Exception as e:
        # Heartbeat failures must never crash the poll loop.
        print(f"host heartbeat failed (non-fatal): {e}")


def _refresh_health(table, tid, info, now, metrics):
    """Health-refresh write for a tenant NOT promoted this tick (already
    running, still creating with the gateway not up yet, or down).

    #237 — beyond the health fields, reconcile `guest_ip` to the data-plane
    truth carried up from the local vm.json. A control-plane↔data-plane drift
    (DDB records a guest_ip the host never actually booted — e.g. DDB .30 while
    the host launched .26) otherwise festers as a ghost forever: the promote
    path only reconciles at creating→running, but a tenant whose gateway never
    comes up (app_health=down) stays in `creating` and only ever hits this
    refresh path, so edge keeps routing to the ghost IP (unreachable) while
    vm_health=up (the probe hits the REAL local IP) — the exact #237 gwdiag
    false-health signal. Writing the real guest_ip here makes DDB's advertised
    IP == the probed IP, self-healing it on the next poll.

    Why guest_ip ONLY, never vm_num: guest_ip is a pure function of the vm slot
    and, crucially, after a live migration DDB.guest_ip already equals the
    target-local vm.json.guest_ip (both carry the SOURCE value — migrate-vm.sh
    restore ships source's vm.json and route_ops ready-route reads it), so
    reconciling guest_ip is a safe no-op post-migration. `vm_num` is different:
    the migration commit deliberately sets DDB.vm_num = target_vm_num (the
    logical/capacity slot) while local vm.json.vm_num stays source (the physical
    tap the snapshot reattaches to). Writing vm_num from local vm.json would
    REVERT that repoint, and host_id=:self can't block it (the target host
    genuinely owns the VM). The logical↔physical vm_num split is #208's
    (phys_vm_num, if_not_exists) — host-agent must not fight it here.

    Two guards, BOTH mandatory:
      * attribute_exists(id) — never resurrect a deleted tenant as a
        health-only orphan (loop 2026-07-02 found 1254 such orphans).
      * host_id = :self — never let a VM still lingering on the OLD host during
        a migration drain window report health / write guest_ip over the record
        the migration just repointed at the target. A VM whose DDB record says
        it lives on another host is not ours to touch; the condition fails
        cleanly (caught by the caller).
    """
    expr = (
        "SET vm_health = :vh, app_health = :ah, last_health_check = :t, guest_ip = :gi"
    )
    vals = {
        ":vh": info["vm_health"],
        ":ah": info["app_health"],
        ":t": now,
        ":gi": info.get("guest_ip", ""),
        ":self": INSTANCE_ID,
    }
    names = None
    if metrics is not None:
        expr += ", #m = :m"  # `metrics` is a DDB reserved keyword — alias via #m.
        vals[":m"] = metrics
        names = {"#m": "metrics"}
    kwargs = {
        "Key": {"id": tid},
        "UpdateExpression": expr,
        "ConditionExpression": "attribute_exists(id) AND host_id = :self",
        "ExpressionAttributeValues": vals,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    table.update_item(**kwargs)


def _write_ddb(results):
    """Update tenant health in DynamoDB. Promote creating → running when VM is up."""
    if not TENANTS_TABLE or not results:
        return
    table = _get_ddb().Table(TENANTS_TABLE)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for tid, info in results.items():
        # #208 — phys_vm_num 回填(物理 tap 权威值)。历史/迁移前建的租户 DDB 无此字段,
        # 撞号检查(create + migrate)对它们会退回 vm_num,迁移过的会有短暂盲区。这里用 vm.json
        # 里的物理 vm_num 补齐,if_not_exists 保证只写一次、绝不覆盖(create 已写的、或先前
        # 回填的都不动)——迁移把 vm_num 翻成 target 槽时 phys_vm_num 恒定,靠的正是"绝不覆盖"。
        _pvn = info.get("phys_vm_num")
        if tid not in _phys_backfilled and isinstance(_pvn, int):
            try:
                table.update_item(
                    Key={"id": tid},
                    UpdateExpression="SET phys_vm_num = if_not_exists(phys_vm_num, :pvn)",
                    ConditionExpression="attribute_exists(id)",
                    ExpressionAttributeValues={":pvn": _pvn},
                )
                _phys_backfilled.add(tid)
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                _phys_backfilled.add(tid)  # 租户已删,别再试
            except Exception as e:
                print(f"phys_vm_num backfill {tid} (non-fatal): {e}")

        # Compute per-VM metrics for healthy VMs (issue #3).
        # Skipped for down/recovering VMs to keep their last-known metrics
        # rather than overwriting with zeros (which would mask the failure).
        metrics = None
        if info["vm_health"] == "up":
            sock_file = os.path.join(VM_DIR, tid, "fc.sock")
            data_file = os.path.join(VM_DIR, tid, "data.ext4")
            cfg_file = os.path.join(VM_DIR, tid, "vm.json")
            vm_mem_mb = 4096
            vm_vcpu = 1
            try:
                with open(cfg_file, encoding="utf-8") as f:
                    cfg = json.load(f)
                    vm_mem_mb = cfg.get("mem_mb", 4096)
                    vm_vcpu = cfg.get("vcpu", 1) or 1
            except Exception:
                pass
            fc_pid = info.get("fc_pid")
            try:
                metrics = _compose_metrics(
                    tid, vm_mem_mb, sock_file, data_file, fc_pid=fc_pid, vcpu=vm_vcpu
                )
            except Exception as e:
                print(f"compose_metrics {tid}: {e}")
            # Mirror computed metrics back into the in-memory snapshot so
            # the Prometheus exporter (/metrics endpoint scraped by ADOT
            # → AMP) sees the actual per-VM gauges. Without this only
            # vm_health was being exposed, leaving openclaw_vm_memory_used_mb
            # / disk_used_mb / disk_used_pct etc. empty in AMP. (Companion
            # fix to the 8899/9090 port-mismatch — both shipped 1.2.5.)
            if metrics is not None:
                info["metrics"] = metrics

        try:
            # #197 — promotion gate 加 app_health:只 gate vm_health(ICMP ping)会让
            # gateway 崩溃重启(schema fail-closed 拒未知 key 等)的 VM 冒充 running——
            # ping 通但 gateway HTTP server 挂,租户对外全 502 却报 running(实测 gateway
            # 崩 2715 次仍 running)。promote 要 VM 活(ping)且 gateway 活(18789 端口有
            # HTTP 应答,见上面 app_health 探测:不探版本相关的具体路由,只判 HTTP server
            # 是否应答,兼容 openclaw 2.26 无 /healthz 端点全 404 的情况)。
            # 只 ping 通、gateway 未起的 VM 停在 creating,由 else 分支刷 health 字段,
            # 下 tick 再 promote(不 promote ≠ 报错,只是等 gateway 就绪)。
            if info["vm_health"] == "up" and info["app_health"] == "up":
                # P2b (contract §3/§4): allocate host_port + write DNAT +
                # publish Redis route BEFORE promoting. If port alloc fails
                # (bitmap exhausted or iptables broke), skip promotion this
                # tick — the next probe will retry. gateway_token is P1's
                # concern (control-plane pre-mints ciphertext into DDB at
                # create); host-agent no longer SSH-reads it (§4).
                try:
                    host_private_ip, host_port = _ensure_route(tid, info["guest_ip"])
                except Exception as e:
                    print(f"ensure_route {tid} failed (skip promote this tick): {e}")
                    # #387: count BOTH skip-promote branches — each leaves the
                    # tenant stuck at creating; counting only one under-reports.
                    with _lock:
                        _agent_metrics["route_ensure_failures"] += 1
                    continue
                if not host_private_ip or host_port is None:
                    print(f"ensure_route {tid} degraded (host_ip or port missing)")
                    with _lock:
                        _agent_metrics["route_ensure_failures"] += 1
                    continue
                # NOTE: `metrics` is a DynamoDB reserved keyword, so it must be
                # referenced via an ExpressionAttributeNames placeholder (#m).
                # Same for `status` (#s, already aliased). Without #m the
                # update_item call returns ValidationException and the tenant
                # never gets promoted to running.
                # #237 — stamp host_id = :self on promote. The promoting host
                # definitionally runs this VM (it read the local vm.json), so
                # this self-heals a record that reached us with host_id
                # unset/stale (e.g. a dispatch backfill that failed under
                # throttle) — otherwise a host_id-less `running` tenant makes
                # delete skip stop-vm/DNAT/counter-release. Gated on `#s = :c`
                # (creating), so a migrating tenant — never `creating` — is
                # untouched (no cross-host clobber). We do NOT write vm_num here:
                # this is the fresh creating→running promote, whose guest_ip is
                # already correct; the logical(DDB)↔physical(vm.json) vm_num split
                # after migration is #208's job (phys_vm_num), not host-agent's.
                # #412(codex review2 #1)—— promote creating→running 时 REMOVE
                # capacity_reservation_id:VM 已真起,容量归 running 租户合法持有,后续正常
                # delete(按 item.vcpu 扣)回收。清掉令牌后,poller/rollback 的失败释放
                # (条件 capacity_reservation_id=:rid)对已 running 租户落空(no-op)→ 绝不误删
                # 活租户放置/容量(data-loss 红线)。是控制面 _mark_running 的 host-agent 对偶。
                update_expr = (
                    "SET #s = :r, vm_health = :vh, app_health = :ah, "
                    "health_failures = :z, last_health_check = :t, "
                    "updated_at = :t, host_private_ip = :hpi, host_id = :self, "
                    "host_port = :hp, guest_ip = :gi, #m = :m "
                    "REMOVE capacity_reservation_id"
                )
                # NOTE (loop 2026-07-01): we tried widening this to
                # `#s IN (creating, stopped)` to self-heal a "stopped-but-alive"
                # contradiction, but it RACES fleet-power stop: fleet_power
                # reconciles DDB→stopped immediately (async SSM not yet run), then
                # this poll sees the VM still up (SSM hasn't stopped it) + DDB
                # stopped and pulls it back to running — so after stop-vm finally
                # writes .stopped, the VM is stopped but DDB stays running forever.
                # That regression hits EVERY normal fleet-power stop, far worse
                # than the rare stopped-but-alive edge (only when stop's SSM fails
                # on a host). So promotion stays creating→running ONLY. The
                # stopped-but-alive edge is a known limitation to fix later with a
                # mechanism that doesn't collide with the stop path (e.g. a
                # grace-timed sweep keyed on the missing .stopped marker).
                update_vals = {
                    ":r": "running",
                    ":c": "creating",
                    ":vh": info["vm_health"],
                    ":ah": info["app_health"],
                    ":z": 0,
                    ":t": now,
                    ":hpi": host_private_ip,
                    ":hp": int(host_port),
                    ":gi": info["guest_ip"],
                    ":self": INSTANCE_ID,
                    ":m": metrics or {},
                }
                # #412(codex review5 #2)—— host_id 不是 ABA-safe 的 promote 闸:租户被释放后
                # 重投【落回同一 host】拿【新】预留(新 vm_num N2,vm_num 单调不复用),此时本机
                # 若残留【旧】vm.json(旧 vm_num N1)会把 N2 的租户按 N1 promote → DDB 放置与实跑
                # VM 的 vm_num 分叉。加 vm_num=:phys 闸:只 promote 【DDB vm_num == 本机 vm.json
                # vm_num】的租户,旧 vm.json 的 N1≠N2 → 条件失败跳过(等旧 VM 被 orphan-reap 清)。
                # 只【条件】用 vm_num,不【写】它(#208:promote 不写 vm_num,迁移安全不变)。
                # phys_vm_num 缺失(legacy vm.json 无 vm_num)→ 回落仅 host_id 闸(不比现状差)。
                _phys = info.get("phys_vm_num")
                if _phys is not None:
                    promote_cond = (
                        "#s = :c AND host_id = :self AND vm_num = :phys "
                        "AND attribute_not_exists(dispatch_settle)"
                    )
                    update_vals[":phys"] = int(_phys)
                else:
                    promote_cond = (
                        "#s = :c AND host_id = :self "
                        "AND attribute_not_exists(dispatch_settle)"
                    )
                # #412(codex review3 #1)—— promote 必须与令牌释放【互斥】,否则:poller/rollback
                # 释放清了 host_id/token/容量但留 status=creating,本 promote 若只判 #s=:c 会把
                # 已释放的租户"复活"成 running,而容量已扣 → 未记账的 running VM(超卖)。fence 加
                # host_id=:self:promote 的租户来自本机 vm.json(reserve 时 host_id 已原子写成本机),
                # 释放会 REMOVE host_id → host_id=:self 条件失败 → 不复活。#412 后 dispatch 租户在
                # reserve 就落 host_id(不再有 host_id-less 的 creating 需要 #237 自愈),故 fence 安全;
                # 队列等待的无 host_id 租户没有本机 vm.json、根本到不了 promote。
                try:
                    table.update_item(
                        Key={"id": tid},
                        UpdateExpression=update_expr,
                        ConditionExpression=promote_cond,
                        ExpressionAttributeNames={"#s": "status", "#m": "metrics"},
                        ExpressionAttributeValues=update_vals,
                    )
                    print(
                        f"promoted {tid} creating → running "
                        f"(host={host_private_ip}:{host_port} guest={info['guest_ip']})"
                    )
                except table.meta.client.exceptions.ConditionalCheckFailedException:
                    # promote's `#s = :c AND host_id = :self` lost: tenant is already
                    # running (normal — just refresh), deleted / migrated away, OR
                    # #412 —— 其 dispatch 预留已被释放(host_id 被 REMOVE)→ 正在拆除,
                    # 【绝不复活】。回落 guarded refresh(attribute_exists(id) + host_id);
                    # 已释放租户 host_id 没了 → refresh 的 host_id 守卫也 CCF → 干净 no-op。
                    _refresh_health(table, tid, info, now, metrics)
            else:
                # Not promoted this tick (still creating w/ gateway not up, or a
                # health-only refresh for a down VM). Reconcile + guard in the
                # shared helper (attribute_exists(id) + host_id ownership).
                _refresh_health(table, tid, info, now, metrics)
        except Exception as e:
            # Expected here: _refresh_health's CCF for a deleted / migrated-away
            # tenant (its guard failed cleanly), plus any transient DDB error.
            # Logged, never crashes the poll loop.
            print(f"ddb update {tid}: {e}")


# ═══════════════════════════════════════════
# Per-VM resource metrics (issue #3)
# ═══════════════════════════════════════════
#
# Sources:
#   memory_used_mb / memory_balloon_mib  : Firecracker /balloon/statistics
#   disk_used_mb / disk_total_mb / pct   : dumpe2fs -h on data.ext4 (host-side)
#   cpu_pct                               : reserved (0 in this PR)
#
# Tenants without a probe failure get a `metrics` field on their DDB record.


def _parse_dumpe2fs_blocks(output):
    """Extract (used_mb, total_mb) from dumpe2fs -h output.

    dumpe2fs prints many irrelevant lines (features, UUIDs, etc.); we only
    need three keys. Returns (0, 0) on malformed input rather than raising
    so the polling loop never crashes from a transient FS-tool failure.
    """
    import re

    block_count = block_size = free_blocks = None
    for line in output.splitlines():
        m = re.match(r"^Block count:\s+(\d+)", line)
        if m:
            block_count = int(m.group(1))
            continue
        m = re.match(r"^Block size:\s+(\d+)", line)
        if m:
            block_size = int(m.group(1))
            continue
        m = re.match(r"^Free blocks:\s+(\d+)", line)
        if m:
            free_blocks = int(m.group(1))
    if block_count is None or block_size is None or free_blocks is None:
        return 0, 0
    total_bytes = block_count * block_size
    used_bytes = (block_count - free_blocks) * block_size
    return used_bytes // (1024 * 1024), total_bytes // (1024 * 1024)


def _get_disk_usage(data_file):
    """Run `dumpe2fs -h` on data.ext4 and return (used_mb, total_mb, pct).

    Host-side: avoids SSH into the guest. Safe even when the file does not
    exist (newly-creating VM) — returns zeros instead of raising.
    """
    if not data_file or not os.path.exists(data_file):
        return 0, 0, 0
    try:
        r = subprocess.run(
            ["dumpe2fs", "-h", data_file], capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return 0, 0, 0
        used_mb, total_mb = _parse_dumpe2fs_blocks(r.stdout)
        pct = int(used_mb * 100 / total_mb) if total_mb else 0
        return used_mb, total_mb, pct
    except Exception:
        return 0, 0, 0


def _get_memory_usage(stats, vm_mem_mb):
    """Compute (used_mb, balloon_mib) from balloon /statistics response.

    `available_memory` reflects what the guest kernel could hand out, so
    `vm_mem_mb - available_mb` is a good proxy for "memory in active use".
    Pure function — no I/O — so it can be tested without a running VM.
    """
    if not stats:
        return 0, 0
    available_bytes = stats.get("stats", {}).get("available_memory", 0)
    available_mb = available_bytes // (1024 * 1024)
    used_mb = max(0, vm_mem_mb - available_mb)
    balloon_mib = stats.get("actual_mib", 0)
    return used_mb, balloon_mib


# ───────────────────────────────────────────────
# Real CPU / memory sampling from /proc/<pid>/* (issue: meeting note
# 2026-05-22 — "可观测性里你那个内存是读不到的，CPU 也读不到").
#
# Background: balloon /statistics often returns available_memory=0 on
# kernels without VIRTIO_BALLOON_F_STATS_VQ enabled, so the previous
# proxy `vm_mem_mb - available_mb` evaluated to vm_mem_mb (= 100% RSS,
# obviously wrong). And cpu_pct was a hard-coded 0 stub. Both surfaced
# as "—" placeholders in the console.
#
# Fix: read straight from /proc/<fc_pid>/* on the host. Firecracker is a
# normal Linux process, so its CPU time and RSS are first-class kernel
# stats — no balloon protocol negotiation, no in-guest agent.
#
# Memory: VmRSS reflects host-resident memory of the Firecracker process,
# which is approximately guest_used + Firecracker overhead (~30–60 MB).
# That's good enough to render a usage bar; we don't subtract overhead so
# the number is the number you'd see in `top` for the firecracker pid.
#
# CPU: utime+stime are cumulative jiffies. We diff against the previous
# sample for the same tenant and divide by the sampling interval to get
# the percentage of one CPU. Then we divide by vcpu_count to express it
# as percent-of-allocated-vcpus (which is what operators want to see —
# 100% means "all configured vCPUs are pegged").
# ───────────────────────────────────────────────


# Per-tenant rolling window for CPU sampling. {tid → (jiffies, monotonic_ts)}
# Module-global is fine: the polling thread is the only writer and a
# missing entry just means "first sample, return 0".
_CPU_SAMPLES = {}

try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError):
    _CLK_TCK = 100  # Linux default; kept for portability + test isolation.


def _read_proc_stat_cpu_jiffies(pid):
    """Sum of utime + stime from /proc/<pid>/stat, in jiffies. None on failure.

    Pure function (input pid → integer jiffies) so the unit test can drive
    it through a tmpfs fixture without monkey-patching subprocess. The
    /proc/PID/stat format is documented in proc(5); fields 14 + 15 (1-indexed)
    are utime and stime. Note that the comm field (#2) can contain spaces +
    parentheses, so we split on the *trailing* `)` rather than naïve split.
    """
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            line = f.read()
    except (FileNotFoundError, PermissionError):
        return None
    rparen = line.rfind(")")
    if rparen < 0:
        return None
    fields = line[rparen + 1 :].split()
    # After the trailing `)` we lose fields 1 + 2; field 14 (utime) becomes
    # index 11, field 15 (stime) becomes index 12.
    try:
        return int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def _read_proc_status_rss_kb(pid):
    """VmRSS in KB from /proc/<pid>/status. None if unavailable.

    Pure function, easy to fixture in tests.
    """
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    return None


def _compute_cpu_pct(
    prev_jiffies, prev_ts, cur_jiffies, cur_ts, vcpu, clk_tck=_CLK_TCK
):
    """Compute CPU% (of allocated vcpus) from two /proc/stat samples.

    Pure function. Returns 0 on the first sample (no prior baseline) and
    on bogus inputs (negative delta means pid was reused, vcpu==0 means
    the caller forgot to populate it). Caps at 100 — overshoot can happen
    if the guest briefly outruns its quota and the host accounting catches
    up unevenly.
    """
    if prev_jiffies is None or cur_jiffies is None:
        return 0
    if prev_ts is None or cur_ts is None or cur_ts <= prev_ts:
        return 0
    if cur_jiffies < prev_jiffies:
        return 0  # pid reused or counter wrapped — discard
    if not vcpu or vcpu <= 0 or clk_tck <= 0:
        return 0
    elapsed_s = cur_ts - prev_ts
    cpu_seconds = (cur_jiffies - prev_jiffies) / clk_tck
    pct = int((cpu_seconds / elapsed_s / vcpu) * 100)
    return max(0, min(100, pct))


def _sample_cpu_pct(tenant_id, fc_pid, vcpu):
    """Read current jiffies, compare to last sample, return CPU%.

    Side effect: updates _CPU_SAMPLES so the next call has a baseline.
    Returns 0 on the first sample for a tenant.
    """
    now = time.monotonic()
    cur_jiffies = _read_proc_stat_cpu_jiffies(fc_pid)
    prev = _CPU_SAMPLES.get(tenant_id)
    pct = 0
    if prev is not None and cur_jiffies is not None:
        prev_jiffies, prev_ts = prev
        pct = _compute_cpu_pct(prev_jiffies, prev_ts, cur_jiffies, now, vcpu)
    if cur_jiffies is not None:
        _CPU_SAMPLES[tenant_id] = (cur_jiffies, now)
    return pct


def _compose_metrics(tenant_id, vm_mem_mb, sock_file, data_file, fc_pid=None, vcpu=1):
    """Build the per-VM metrics dict written to DDB.

    1.2.9 fix: cpu_pct now comes from /proc/<fc_pid>/stat sampling rather
    than the previous hard-coded 0; memory_used_mb now prefers VmRSS over
    the unreliable balloon stats path. The balloon-stats path is kept as a
    fallback so hosts on older kernels still report something sensible.
    """
    # CPU — real %, sampled across two polls.
    cpu_pct = _sample_cpu_pct(tenant_id, fc_pid, vcpu)

    # Memory — VmRSS first (always readable on Linux), balloon as fallback.
    mem_used = 0
    rss_kb = _read_proc_status_rss_kb(fc_pid) if fc_pid else None
    if rss_kb:
        mem_used = rss_kb // 1024  # KB → MB

    # Balloon stats are still useful for `memory_balloon_mib` (how much the
    # host has reclaimed) regardless of whether available_memory is reliable.
    stats = _get_balloon_stats(sock_file) if sock_file else None
    balloon_used_mb, balloon_mib = _get_memory_usage(stats, vm_mem_mb)
    if not mem_used:
        mem_used = balloon_used_mb  # last-ditch fallback

    disk_used, disk_total, disk_pct = _get_disk_usage(data_file)
    return {
        "memory_used_mb": int(mem_used),
        "memory_balloon_mib": int(balloon_mib),
        "disk_used_mb": int(disk_used),
        "disk_total_mb": int(disk_total),
        "disk_used_pct": int(disk_pct),
        "cpu_pct": int(cpu_pct),
    }


def _get_balloon_stats(sock_file):
    """Get balloon statistics from a VM via Firecracker API."""
    try:
        r = subprocess.run(
            [
                "curl",
                "-sf",
                "--unix-socket",
                sock_file,
                "http://localhost/balloon/statistics",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def _set_balloon_target(sock_file, amount_mib):
    """Set balloon target size (inflate/deflate)."""
    try:
        subprocess.run(
            [
                "curl",
                "-sf",
                "--unix-socket",
                sock_file,
                "-X",
                "PATCH",
                "http://localhost/balloon",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps({"amount_mib": amount_mib}),
            ],
            capture_output=True,
            timeout=5,
        )
    except Exception as e:
        print(f"balloon set failed: {e}")


def _get_host_mem_info():
    """Read host /proc/meminfo, return (total_mb, available_mb)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            info = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])  # kB
            total = info.get("MemTotal", 0) // 1024
            available = info.get("MemAvailable", 0) // 1024
            return total, available
    except Exception:
        return 0, 0


def _adjust_balloons(probe_results):
    """Dynamically adjust balloon sizes based on host memory pressure.

    Strategy:
    - If host available memory < 20% of total → inflate balloons on VMs with spare memory
    - If host available memory > 40% of total → deflate balloons to give memory back
    - Never inflate beyond max_inflate_ratio of VM's declared memory
    - Never reduce guest available below min_guest_available_mb
    """
    if not BALLOON_ENABLED:
        return

    host_total, host_available = _get_host_mem_info()
    if host_total == 0:
        return

    host_pressure = host_available / host_total  # 0.0 = no memory, 1.0 = all free

    for tid, info in probe_results.items():
        if info.get("vm_health") != "up":
            continue
        sock_file = os.path.join(VM_DIR, tid, "fc.sock")
        if not os.path.exists(sock_file):
            continue

        # Read VM config for declared memory
        cfg_file = os.path.join(VM_DIR, tid, "vm.json")
        try:
            with open(cfg_file, encoding="utf-8") as f:
                cfg = json.load(f)
            vm_mem_mb = cfg.get("mem_mb", 4096)
        except Exception:
            continue

        stats = _get_balloon_stats(sock_file)
        if not stats:
            continue

        current_balloon_mib = stats.get("actual_mib", 0)
        max_balloon = int(vm_mem_mb * BALLOON_MAX_INFLATE_RATIO)

        # Guest available memory (from balloon stats)
        guest_available_mb = stats.get("stats", {}).get("available_memory", 0) // (
            1024 * 1024
        )

        if host_pressure < 0.20:
            # Host under pressure — try to reclaim from this VM
            reclaimable = guest_available_mb - BALLOON_MIN_GUEST_AVAILABLE_MB
            if reclaimable > 0:
                target = min(current_balloon_mib + reclaimable, max_balloon)
                if target > current_balloon_mib:
                    _set_balloon_target(sock_file, target)
                    print(
                        f"balloon inflate {tid}: {current_balloon_mib}→{target}MB "
                        f"(host_avail={host_available}MB guest_avail={guest_available_mb}MB)"
                    )

        elif host_pressure > 0.40:
            # Host has plenty of memory — give back to VMs
            if current_balloon_mib > 0:
                _set_balloon_target(sock_file, 0)
                print(
                    f"balloon deflate {tid}: {current_balloon_mib}→0MB (host_avail={host_available}MB)"
                )


# Orphan-firecracker overwatcher (Firecracker prod-host-setup.md:69-83 recommends
# a host process that reaps unresponsive/leaked firecrackers). Our normal recovery
# only iterates vm.json dirs, so a firecracker whose vm.json was already removed
# (tenant deleted, but DELETE raced the kill / SIGKILL chaser missed) becomes an
# ORPHAN that no probe ever revisits — it silently holds ~600MB-1GB RSS forever.
# This sweep finds firecrackers whose socket dir has no vm.json and SIGKILLs them.
_ORPHAN_GRACE_SEC = int(os.environ.get("OC_ORPHAN_GRACE_SEC", "120"))

# #321 — 已删租户的 VM 目录(data.ext4+overlay.ext4,~85M/个)磁盘回收兜底。delete 侧
# tenant_service.py rm -rf 在高负载下 SSM 丢/超时会漏删,孤儿累积撑满 /data(真机实测:
# host 82% 目录是漏删孤儿,/data 64~70% → 撑满后新 VM mkdir 失败转 intervention)。
# 独立单例线程(不阻塞 poll heartbeat:50×rm 超时最坏会累计撑过 stale 门槛误判重启,
# codex 复审),每 _DISK_GC_INTERVAL_SEC 跑一次,每轮回收上限防单轮抖动。
_DISK_GC_MAX_PER_SWEEP = int(os.environ.get("OC_DISK_GC_MAX_PER_SWEEP", "50"))
_DISK_GC_INTERVAL_SEC = int(os.environ.get("OC_DISK_GC_INTERVAL_SEC", "60"))
_PURGE_PREFIX = ".purge-"  # 平级 tombstone: /data/firecracker-vms/.purge-<tid>
_disk_gc_cursor = (
    ""  # 轮转游标:上轮处理到的最后一个 tombstone 名,下轮从它之后起(防饥饿)
)


def _gc_orphan_vm_dirs():
    """回收 delete 侧 rm -rf 漏删的 VM 目录(兜底 GC)。双门确认,绝不误删有效数据:

    判据(两条都满足才删):
      ① 存在平级 tombstone `.purge-<tid>` —— 控制面 delete(keep_data=false)在 rm -rf 前
         写的"该数据盘应销毁"持久信号,放【VM 目录外】故 rm -rf <tid> 中断也不丢(codex:
         标记在被删目录内会随半删消失)。keep_data=true 软删不写 tombstone → 保盘。
      ② DDB 强一致读该租户 status=deleted —— 确认删除【已完成】。deleting(备份/停机前、
         可回滚 running)、记录不存在(同 id 重建的最终一致空窗口,误删新盘)一律【不删】
         (codex 复审:只认明确 deleted,强一致读避开重建竞态)。
    删成功后清 tombstone。任一查询/删除异常 → 跳过(no-data-loss:绝不因不确定而删)。
    每轮回收上限,余量下一轮继续。
    """
    if not TENANTS_TABLE:
        return 0
    try:
        entries = os.listdir(VM_DIR)
    except FileNotFoundError:
        return 0
    table = _get_ddb().Table(TENANTS_TABLE)
    global _disk_gc_cursor
    # 门①:先筛出所有【平级 tombstone、普通文件、非符号链接】(codex:伪造成 .purge-<victim>
    # 符号链接/目录可诱导删他人目录)。排序 + 从上轮游标之后环绕起(codex:防前 50 个长期删不掉
    # 时第 51 个永远轮不到的饥饿)。
    tombs = sorted(
        n
        for n in entries
        if n.startswith(_PURGE_PREFIX)
        and not os.path.islink(os.path.join(VM_DIR, n))
        and os.path.isfile(os.path.join(VM_DIR, n))
    )
    if tombs:
        start = next((i for i, n in enumerate(tombs) if n > _disk_gc_cursor), 0)
        tombs = tombs[start:] + tombs[:start]  # 环绕:游标后 → 回头补前面
    reclaimed = 0
    # checked = 处理过的 tombstone 数(含 DDB 查询失败/rm 超时),上限防单轮扫完整个目录 +
    # 打满 DDB/rm 时间上界;余量下一轮从游标续。
    for name in tombs[:_DISK_GC_MAX_PER_SWEEP]:
        _disk_gc_cursor = name
        tid = name[len(_PURGE_PREFIX) :]
        # tid 路径穿越防护(codex 复审):`.purge-..` 解出 tid=".." → join 出 VM_DIR 父目录 →
        # rm -rf 可能删到 /data。拒空/`.`/`..`/含分隔符,并 commonpath 确认目标仍在 VM_DIR 下。
        vm_path = os.path.join(VM_DIR, tid)
        if (
            not tid
            or tid in (".", "..")
            or "/" in tid
            or os.path.commonpath([VM_DIR, os.path.realpath(vm_path)]) != VM_DIR
        ):
            print(f"disk-gc: unsafe tid from tombstone {name!r} — skip")
            continue
        tomb = os.path.join(VM_DIR, name)
        # 门②:强一致读,只认明确 deleted。异常/deleting/记录不存在 → 跳过(不删)。
        try:
            item = table.get_item(
                Key={"id": tid},
                ConsistentRead=True,
                ProjectionExpression="#s",
                ExpressionAttributeNames={"#s": "status"},
            ).get("Item")
        except Exception as e:
            print(f"disk-gc: DDB get {tid} failed (skip): {e}")
            continue
        if not item or item.get("status") != "deleted":
            continue
        try:
            subprocess.run(["rm", "-rf", vm_path], check=True, timeout=30)
            os.remove(tomb)  # 删净后清 tombstone(下轮不再扫);删失败留着下轮重试
            reclaimed += 1
        except Exception as e:
            print(f"disk-gc: rm -rf {vm_path} failed: {e}")
    if reclaimed:
        print(f"disk-gc: reclaimed {reclaimed} purged-tenant VM dir(s) from {VM_DIR}")
    return reclaimed


def _disk_gc_loop():
    """#321 — 独立单例线程跑磁盘 GC。与 poll/dispatch/housekeeping 分开,一次 sweep 卡住
    (rm -rf 在 IO hang 下超时)也不阻塞 host heartbeat → 不会误判 host stale 触发重启。"""
    while True:
        try:
            _gc_orphan_vm_dirs()
        except Exception as e:
            print(f"disk-gc loop error (non-fatal): {e}")
        _agent_loop_tick("disk_gc")  # #387 self-stamped
        time.sleep(_DISK_GC_INTERVAL_SEC)


def _reap_orphan_firecrackers():
    """SIGKILL firecracker processes whose VM dir has no vm.json (leaked/orphaned).

    Guards against false positives: only reaps a firecracker that has been running
    longer than _ORPHAN_GRACE_SEC, so a VM mid-launch (firecracker started before
    launch-vm.sh writes vm.json) is never killed."""
    try:
        pg = subprocess.run(
            ["pgrep", "-af", "api-sock"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return
    if pg.returncode != 0:
        return
    reaped = 0
    for line in pg.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2 or "firecracker" not in parts[1]:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        # Extract the --api-sock path → its dir is the VM dir.
        sock = None
        for tok in parts[1].split():
            if tok.endswith("fc.sock") or "/fc.sock" in tok:
                sock = tok
                break
        if not sock:
            # fall back: --api-sock <path>
            toks = parts[1].split()
            if "--api-sock" in toks:
                i = toks.index("--api-sock")
                if i + 1 < len(toks):
                    sock = toks[i + 1]
        if not sock:
            continue
        vmdir = os.path.dirname(sock)
        # has a live vm.json → legitimate, skip
        if os.path.isfile(os.path.join(vmdir, "vm.json")):
            continue
        # grace: skip young processes (mid-launch, vm.json not written yet)
        try:
            et = subprocess.run(
                ["ps", "-o", "etimes=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            age = int(et.stdout.strip() or "0")
        except Exception:
            age = 0
        if age < _ORPHAN_GRACE_SEC:
            continue
        print(
            f"overwatcher: reaping orphan firecracker pid={pid} dir={vmdir} age={age}s"
        )
        try:
            # Kill by EXACT socket path (zero false-positive: matches only this
            # firecracker), NOT by a guessed vm_num. We deliberately do NOT call
            # stop-vm.sh here because it deletes tap-vm<NUM> from a vm_num we can't
            # reliably recover for an orphan (vm.json is gone) — a wrong guess would
            # touch the wrong tap. Process reclaim (the 600MB-1GB RSS) is the goal;
            # a leaked tap is harmless and swept separately below best-effort.
            subprocess.run(
                ["pkill", "-KILL", "-f", f"api-sock {sock}"],
                capture_output=True,
                timeout=10,
            )
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=5)
            # best-effort tap cleanup: derive tap from the dead fc's own netns/cmdline
            # is unreliable for an orphan, so leave the tap for the periodic tap GC.
            try:
                os.remove(os.path.join(vmdir, "fc.sock"))
            except OSError:
                pass
            # PRD 2.1 根治"僵尸 per-tenant nginx 路由累积":孤儿的 nginx conf 没走
            # stop-vm 清理通道,这里按 tenant_id 精确删它那一条 + reload。tenant_id =
            # vmdir basename(确定值,非猜测),只删自己这条,不碰别人。
            orphan_tid = os.path.basename(vmdir)
            nginx_conf = f"/etc/nginx/conf.d/tenants/{orphan_tid}.conf"
            if os.path.isfile(nginx_conf):
                try:
                    os.remove(nginx_conf)
                    subprocess.run(
                        ["nginx", "-s", "reload"], capture_output=True, timeout=10
                    )
                    print(f"overwatcher: removed orphan nginx route {orphan_tid}")
                except Exception:
                    pass
            # #134 修:孤儿(含 delete 后残留)也要清 Redis route: 键——否则 edge 仍
            # 缓存指向已删 VM 的 host:port(DNAT 已被控制面摘,连接 refused/502)。
            # _release_route 无条件 del_route(tenant),port=None 时跳过 DNAT(delete 已摘)。
            try:
                _release_route(orphan_tid, None, "")
                print(f"overwatcher: released redis route {orphan_tid}")
            except Exception as e:
                print(f"overwatcher: release route {orphan_tid} failed: {e}")
            reaped += 1
        except Exception as e:
            print(f"overwatcher: reap pid={pid} failed: {e}")
    if reaped:
        print(f"overwatcher: reaped {reaped} orphan firecracker(s)")


# ═══════════════════════════════════════════
# SQS dispatch — pull-mode reconciler (二期)
# ═══════════════════════════════════════════
#
# When ASSIGNMENTS_TABLE is set, we run a second thread that pulls "desired
# state" rows from the openclaw-assignments DDB table and locally fans out
# launch-vm.sh. The consumer Lambda only writes rows (no SSM), so:
#   - dispatch scales with DDB Query throughput (host count × 5s poll), not
#     SSM SendCommand rate (which cratered at ~40 concurrent per-instance);
#   - a host that missed one poll auto-catches up on the next tick — the
#     table IS the desired state (no fire-and-forget SendCommand to replay).
#
# The planning step (_plan_dispatch) is a pure function that takes the DDB
# rows and the on-disk VM inventory and returns a list of (assignment,
# action) tuples: launch | skip_done | over_budget. Tests exercise this
# without any AWS.

ASSIGNMENTS_TABLE = os.environ.get("ASSIGNMENTS_TABLE", "")
DISPATCH_POLL = int(os.environ.get("OC_AGENT_DISPATCH_POLL", "5"))
DISPATCH_PARALLEL = int(os.environ.get("OC_AGENT_DISPATCH_PARALLEL", "96"))
DISPATCH_RETRY_BUDGET = int(os.environ.get("DISPATCH_RETRY_BUDGET", "3"))

# ── P0.1 backoff + jitter (kubelet pod_workers.go:completeWork:1524-1548) ──
# Every failure used to burn one of DISPATCH_RETRY_BUDGET immediately; three
# strikes and the tenant went to requires_intervention. That's cruel to
# transient SSM/DDB throttles and it makes 380 hosts hammer the same second
# after a failure (惊群). Kubelet's answer: classify the error, back off
# for a category-specific delay, and jitter every enqueue by up to 50%.
# We keep the budget as a hard cap for terminal (non-transient) failures,
# but transient errors no longer count against it. Values mirror kubelet:
#   pod_workers.go:325  workerBackOffPeriodDefault  = 10s
#   pod_workers.go:327  workerResyncIntervalJitterFactor = 0.5
#   pod_workers.go:333  backOffOnTransientErrorPeriod = 1s
DISPATCH_BACKOFF_BASE_SEC = float(os.environ.get("OC_AGENT_DISPATCH_BACKOFF", "10"))
DISPATCH_BACKOFF_MAX_SEC = float(os.environ.get("OC_AGENT_DISPATCH_BACKOFF_MAX", "60"))
DISPATCH_BACKOFF_TRANSIENT_SEC = float(
    os.environ.get("OC_AGENT_DISPATCH_TRANSIENT", "1")
)
DISPATCH_JITTER_FACTOR = 0.5
# Housekeeping runs on its own slower cadence (kubelet housekeepingPeriod=2s;
# we go slower because DDB Query is more expensive — 60s is one 5s-poll batch
# worth of tenants scanned per minute per host).
DISPATCH_HOUSEKEEPING_SEC = int(os.environ.get("OC_AGENT_HOUSEKEEPING", "60"))

_dispatch_inflight = set()  # tenant_ids currently in a launch subprocess
_dispatch_inflight_lock = threading.Lock()

# ── P0.3 workQueue: map[tenant_id]→due_time_epoch_seconds ──
# Modelled on util/queue/work_queue.go:36-68 — a plain dict is fine at our
# scale (≤380 rows/host). Enqueue overwrites due_time (dedup on tenant_id);
# _dispatch_get_due() pops all entries whose due_time ≤ now. On each tick we
# still do a bounded DDB Query as the desired-state truth, but a tenant that
# just failed with backoff won't run again until its due_time — the queue is
# the "not yet due" filter over the DDB row set.
_dispatch_queue = {}  # tenant_id -> due_time (epoch seconds)
_dispatch_queue_lock = threading.Lock()


def _jitter(base_sec, factor=DISPATCH_JITTER_FACTOR):
    """Add up to `factor * base_sec` uniform random skew to a delay. Kubelet
    calls wait.Jitter(t, 0.5); the point is to spread N hosts that hit the
    same failure at t0 across [t0+t, t0+1.5t] so their retries don't stampede.
    Pure function — testable by seeding random.
    """
    if base_sec <= 0:
        return 0.0
    return base_sec + random.uniform(0.0, factor * base_sec)


def _backoff_for_reason(reason):
    """Kubelet-style error classification. Return the delay (seconds) before
    the next attempt. Transient categories get a tight retry (1s + jitter);
    generic failures get the 10s base capped at 60s (like kubelet's clamp to
    resyncInterval in pod_workers.go:1544). Callers add jitter.
    """
    r = (reason or "").lower()
    # DDB / SSM throttles, SQS TooManyRequests, transport hiccups — kubelet
    # groups these under backOffOnTransientErrorPeriod. Match real AWS error
    # names (ProvisionedThroughputExceededException, RequestLimitExceeded,
    # ThrottlingException) as well as the generic strings.
    transient_markers = (
        "throttl",
        "timeout",
        "unavailab",
        "networknotready",
        "connection reset",
        "too many requests",
        "provisionedthroughput",
        "requestlimitexceeded",
        "servicetooheavy",
    )
    if any(m in r for m in transient_markers):
        return DISPATCH_BACKOFF_TRANSIENT_SEC
    # launch-vm.sh non-zero (rc=1, disk full, image missing, guest kernel
    # panic) is a real failure; use the longer backoff and let the budget cap
    # take terminal decisions on repeated real failures.
    return min(DISPATCH_BACKOFF_BASE_SEC, DISPATCH_BACKOFF_MAX_SEC)


def _dispatch_enqueue(tenant_id, delay_sec):
    """Set due_time = now + delay + jitter, replacing any prior entry for the
    same tenant. Kubelet work_queue.go:64-68 does the exact same map assign
    (dedup semantics — a second Enqueue for the same key wins).
    """
    due = time.time() + _jitter(delay_sec)
    with _dispatch_queue_lock:
        _dispatch_queue[tenant_id] = due


def _dispatch_pop_due(now=None):
    """Pop all tenants whose due_time ≤ now (work_queue.go:GetWork:50).
    Returns a set of tenant_ids the caller may include in this tick.
    Non-due entries stay in the queue and are skipped by _dispatch_tick.
    """
    if now is None:
        now = time.time()
    due = set()
    with _dispatch_queue_lock:
        for tid, when in list(_dispatch_queue.items()):
            if when <= now:
                due.add(tid)
                del _dispatch_queue[tid]
    return due


def _dispatch_peek_pending(now=None):
    """Return the set of tenant_ids that are currently queued but NOT yet due.
    Used by _dispatch_tick to short-circuit tenants that failed recently and
    are still cooling off — DDB row still says pending, but our local queue
    says 'don't touch until due_time'.
    """
    if now is None:
        now = time.time()
    with _dispatch_queue_lock:
        return {tid for tid, when in _dispatch_queue.items() if when > now}


def _dispatch_queue_size():
    with _dispatch_queue_lock:
        return len(_dispatch_queue)


def _plan_dispatch(assignments, local_vms, retry_budget=DISPATCH_RETRY_BUDGET):
    """Given a list of assignment DDB rows and the local vm inventory, decide
    the action for each row. Pure function — no I/O, no side effects — so it
    is trivial to test with plain dicts.

    Args:
      assignments: [{"tenant_id","vm_num","vcpu","mem_mb","chat_ep","status",
                     "dispatch_retries" (optional)}] — status is expected to
                     be "pending" (caller filters), but we defend against a
                     stale row that already flipped to "done".
      local_vms:   set of tenant_ids that already have /data/firecracker-vms/
                   <tenant>/vm.json on this host.
      retry_budget: hard cap; retries ≥ budget → over_budget (agent flips
                   tenant to requires_intervention per the contract).

    Returns: [{"tenant_id","action","assignment"}]
      action ∈ {"launch","skip_done","over_budget","skip_inflight"}
    """
    plan = []
    for a in assignments:
        tid = a.get("tenant_id")
        if not tid:
            continue
        if a.get("status") != "pending":
            continue
        # Idempotent guard: another agent thread (or a prior poll cycle) may
        # have already launched. `vm.json exists` = we own that tenant; flip
        # to done without relaunching.
        if tid in local_vms:
            plan.append({"tenant_id": tid, "action": "skip_done", "assignment": a})
            continue
        with _dispatch_inflight_lock:
            if tid in _dispatch_inflight:
                # Prior tick's subprocess still running — do not double-launch.
                plan.append(
                    {"tenant_id": tid, "action": "skip_inflight", "assignment": a}
                )
                continue
        retries = int(a.get("dispatch_retries", 0) or 0)
        if retries >= retry_budget:
            plan.append({"tenant_id": tid, "action": "over_budget", "assignment": a})
            continue
        plan.append({"tenant_id": tid, "action": "launch", "assignment": a})
    return plan


def _local_vm_inventory():
    """Snapshot tenant_ids that already have vm.json on disk."""
    try:
        return {
            tid
            for tid in os.listdir(VM_DIR)
            if os.path.isfile(os.path.join(VM_DIR, tid, "vm.json"))
        }
    except FileNotFoundError:
        return set()


def _mark_assignment_done(table, instance_id, tenant_id):
    """Condition write: assignment status=pending → done. Loser (already done
    /failed) is fine; we ack silently."""
    try:
        table.update_item(
            Key={"instance_id": instance_id, "tenant_id": tenant_id},
            UpdateExpression="SET #s = :d, done_ts = :t",
            ConditionExpression="attribute_not_exists(#s) OR #s = :p",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":d": "done",
                ":p": "pending",
                ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
    except Exception as e:
        # Conditional-check failures are expected (another agent won the race).
        if "ConditionalCheckFailedException" not in str(e):
            print(f"assignment done write {tenant_id} failed: {e}")


def _mark_assignment_failed(table, instance_id, tenant_id, reason=""):
    try:
        table.update_item(
            Key={"instance_id": instance_id, "tenant_id": tenant_id},
            UpdateExpression="SET #s = :f, failed_ts = :t, fail_reason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":f": "failed",
                ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ":r": (reason or "")[:200],
            },
        )
    except Exception as e:
        print(f"assignment fail write {tenant_id} failed: {e}")


def _promote_tenant_running(tenant_id):
    """tenants status=creating → running (condition write). Loser (already
    running / stopped / deleting) is silently accepted — we never overwrite
    a downstream state (D-#0-C fail-loud on assumption)."""
    if not TENANTS_TABLE:
        return
    try:
        table = _get_ddb().Table(TENANTS_TABLE)
        # #412(codex review2 #1)—— promote 时 REMOVE capacity_reservation_id(同 _write_ddb
        # 的对偶):running 租户容量归其合法持有,清令牌后失败释放对它落空、绝不误删活租户。
        table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :r, running_ts = :t REMOVE capacity_reservation_id",
            ConditionExpression="#s = :c AND attribute_not_exists(dispatch_settle)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":r": "running",
                ":c": "creating",
                ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
    except Exception as e:
        if "ConditionalCheckFailedException" not in str(e):
            print(f"promote {tenant_id} to running failed: {e}")


def _bump_dispatch_retries(tenant_id):
    """Atomic tenants.dispatch_retries += 1。返回新值(#315:调用方据此判是否超预算标终态);
    表缺失/异常返回 None(调用方按"未知,不标终态"处理,留给下轮重试,fail-safe 不误终态)。"""
    if not TENANTS_TABLE:
        return None
    try:
        table = _get_ddb().Table(TENANTS_TABLE)
        resp = table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="ADD dispatch_retries :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int((resp.get("Attributes") or {}).get("dispatch_retries", 0))
    except Exception as e:
        print(f"bump retries {tenant_id} failed: {e}")
        return None


def _flag_requires_intervention(tenant_id):
    """Budget exhausted: tenants.status=requires_intervention (no reset). Only
    from creating/failed to avoid clobbering a subsequent recovery.

    #315(codex review7 P2)返回 assignment 应写的终态字符串,让 assignment 与 tenant 状态一致:
    - 写成功 → "failed":tenant 刚翻 requires_intervention,assignment 标 failed 匹配。
    - CCF → 回读 tenant 真实状态定夺(不再一律当 failed,否则健康线程已 promote running 时会留下
      tenant=running / assignment=failed 矛盾):
        · running → "done":VM 已活,assignment 应标 done(与 _mark_assignment_done 一致)。
        · 其它终态(deleted/stopped/已 intervention)→ "failed":标 failed 无害。
    - 真异常(DDB 5xx/throttle,非 CCF)/回读失败 → None:tenant 终态未确认,调用方【保持 assignment
      pending】下轮重试,不能标终态(否则 assignment 脱离 pending + tenant 仍 creating = 断链)。"""
    if not TENANTS_TABLE:
        return None
    try:
        table = _get_ddb().Table(TENANTS_TABLE)
        table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :r, requires_intervention_ts = :t",
            ConditionExpression="#s IN (:c, :f)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":r": "requires_intervention",
                ":c": "creating",
                ":f": "failed",
                ":t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        return "failed"
    except Exception as e:
        if "ConditionalCheckFailedException" not in str(e):
            print(f"requires_intervention {tenant_id} failed: {e}")
            return None  # 真异常:终态未确认,保持 assignment pending
        # CCF:tenant 已非 creating/failed → 回读真实状态,让 assignment 与之一致。
        # #315(codex review8 P2)ConsistentRead=True:健康线程刚 promote running 触发本 CCF,
        # 默认最终一致的 get_item 可能仍读到旧 creating → 误返 "failed" 再成矛盾态。强一致读拿
        # 到刚写入的 running。
        try:
            cur = table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
                "Item", {}
            )
            return "done" if cur.get("status") == "running" else "failed"
        except Exception as e2:
            print(f"requires_intervention {tenant_id} status reread failed: {e2}")
            return None  # 回读失败:不确定,保持 pending 下轮重试


def _execute_launch(assignment):
    """Fire launch-vm.sh in a subprocess and wait — the DISPATCH_PARALLEL
    semaphore is the caller's job (a ThreadPoolExecutor)."""
    tid = assignment["tenant_id"]
    vm_num = str(assignment.get("vm_num", 1))
    vcpu = str(assignment.get("vcpu", 2))
    mem_mb = str(assignment.get("mem_mb", 2048))
    chat_ep = str(assignment.get("chat_ep", 0))
    with _dispatch_inflight_lock:
        _dispatch_inflight.add(tid)
    try:
        # Same 10-position invocation as launch-all-vms.sh — positions 5-9
        # left blank so launch-vm.sh reads secrets from DDB itself (single
        # code path with _recover_vm and the SSM push driver).
        rc = subprocess.call(
            _launch_argv(
                "bash",
                "/home/ubuntu/launch-vm.sh",
                tid,
                vm_num,
                vcpu,
                mem_mb,
                "",
                "",
                "",
                "",
                "",
                chat_ep,
                "",
                "",
                "",
                str(assignment.get("capacity_reservation_id", "")),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # #267 — rc 75 = launch-vm.sh per-tenant flock 抢锁失败(#256):另一进程
        # (push fan-out / ssm wake / _recover_vm)正在起同租户,是良性 skip,不是失败。
        # 返 "skip" 让调用点保持 assignment pending(不标 failed、不消耗 retry 预算),
        # 下一 tick 自然收敛或 winner 已起好后 vm.json 存在被跳过。systemd-cat 透传
        # 退出码(systemd 255 真机实测:exit 75→rc 75),故这里能拿到真实 75。
        if rc == 75:
            return "skip"
        # #411/6.3 codex(round5) — launch-vm status 闸的两个新退出码,pull 路径也要按语义分开,
        # 不能落进 `rc == 0` 的 False 当普通失败(那会烧 retry 预算 + 把在途/回滚态误判终态):
        #   44 = 租户已 deleted(终态)→ "abort":调用点标 assignment done、不重投、不计失败。
        #   45 = deleting/状态未知/读失败(非终态,delete 可能回滚)→ "skip":保持 pending、
        #        不消耗 retry 预算,下一 tick 重判(同 rc75 的 pending 语义)。
        if rc == 44:
            return "abort"
        if rc == 45:
            return "skip"
        return rc == 0
    except Exception as e:
        print(f"dispatch launch {tid} failed: {e}")
        return False
    finally:
        with _dispatch_inflight_lock:
            _dispatch_inflight.discard(tid)


def _query_pending_assignments(table, instance_id):
    """DDB Query keyed on this host. Uses status filter (pending) — bounded by
    the per-host row count (≤380 concurrent creations), so no pagination
    logic needed at this scale. If we ever grow >1000 rows, add an LSI on
    status and switch to KeyConditionExpression."""
    rows = []
    try:
        resp = table.query(
            KeyConditionExpression="instance_id = :i",
            FilterExpression="#s = :p",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":i": instance_id, ":p": "pending"},
        )
        rows = resp.get("Items", [])
    except Exception as e:
        print(f"query assignments failed (non-fatal): {e}")
    return rows


def _dispatch_tick(table):
    """One reconciliation pass. Isolated so tests can drive it without a
    running thread.

    P0.3 workQueue integration: after planning, drop any launch step whose
    tenant is queued but not yet due (still cooling off after a prior
    failure). The DDB row is still 'pending' — we honor the local backoff
    clock instead of re-hitting launch-vm.sh at every 5s poll.
    """
    if not INSTANCE_ID:
        return
    assignments = _query_pending_assignments(table, INSTANCE_ID)
    if not assignments:
        return
    local = _local_vm_inventory()
    plan = _plan_dispatch(assignments, local)
    # P0.3: cooling-off filter — tenants in queue with due_time>now stay quiet
    # this tick. Their DDB row is still 'pending' so the next tick after
    # due_time will re-plan them naturally (dict pop drains automatically via
    # _dispatch_pop_due below).
    not_yet_due = _dispatch_peek_pending()
    # First, handle no-op rows without holding a thread slot.
    launches = []
    for step in plan:
        tid = step["tenant_id"]
        if step["action"] == "skip_done":
            _mark_assignment_done(table, INSTANCE_ID, tid)
            # #315 — 不在此处直接 promote:_promote_tenant_running 无健康/路由检查,会把
            # 半成品 VM(vm.json 在但 FC 没起/gateway 没活)误标 running(真机 28/300 卡的
            # 反面:误 promote 更糟)。统一由健康探测路径 _write_ddb 在 vm_health+app_health
            # (gateway 18789 应答)+ _ensure_route 都成立后才 creating→running(codex 判:唯一 promote 写手)。
        elif step["action"] == "over_budget":
            # #315(codex review7 P2)先写 tenant 终态,按返回的终态给 assignment 打一致标签:
            # "failed"→标 failed;"done"(tenant 已被健康线程 promote running)→标 done 避免
            # tenant=running/assignment=failed 矛盾;None(DDB 异常)→保持 pending 下轮重试不断链。
            _final = _flag_requires_intervention(tid)
            if _final == "done":
                _mark_assignment_done(table, INSTANCE_ID, tid)
            elif _final == "failed":
                _mark_assignment_failed(
                    table, INSTANCE_ID, tid, reason="retry budget exhausted"
                )
        elif step["action"] == "skip_inflight":
            # Prior tick is still running; next tick will re-plan.
            continue
        else:
            if tid in not_yet_due:
                # Cooling off from a prior failure — skip this tick.
                continue
            launches.append(step["assignment"])
    # Drain due entries: they've served their purpose (the DDB row will
    # decide whether to launch this tick), so pop them out of the queue.
    _dispatch_pop_due()
    if not launches:
        return
    # Bounded parallelism — the semaphore keeps the host at DISPATCH_PARALLEL
    # in-flight launch-vm.sh subprocesses (matches launch-all-vms.sh's
    # `wait -n` gate). ThreadPoolExecutor here waits for the whole batch, so
    # a single tick never runs past DISPATCH_POLL if launches are fast.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=DISPATCH_PARALLEL) as pool:
        futures = {pool.submit(_execute_launch, a): a for a in launches}
        for fut, a in futures.items():
            tid = a["tenant_id"]
            ok = False
            err_reason = "launch-vm.sh non-zero"
            try:
                ok = fut.result()
            except Exception as e:
                # Preserve the raised reason for _backoff_for_reason to
                # classify (throttled / timeout ⇒ transient).
                err_reason = str(e) or err_reason
                print(f"dispatch future {tid} raised: {err_reason}")
                ok = False
            # #267 — flock skip 必须在 `if ok:` 之前判:"skip" 是 truthy 字符串,
            # 落 `if ok:` 会被当成功 _mark_assignment_done → 该租户其实没起(winner
            # 可能也失败),孤儿。skip = 良性(另一进程在起同租户),保持 pending 不动,
            # 不标 done/failed、不消耗 retry 预算、不重入队,下一 tick 由 DDB 行决定。
            if ok == "skip":
                continue
            # #411/6.3 codex(round5) — "abort" = launch-vm status 闸读到租户已 deleted(rc44,
            # 终态)。与 "skip"(rc45/75,保持 pending)不同:deleted 是终态,标 assignment done
            # 停止重投(否则每 tick 反复叫醒 host 起一个已删租户的 VM)。必须在 `if ok:` 之前判
            # ——"abort" 也是 truthy 字符串,落 `if ok:` 会被 _mark_assignment_done 但走的是
            # "成功起了"的语义分支(误标 running)。这里显式终结:标 done、不重投、不算失败。
            if ok == "abort":
                _mark_assignment_done(table, INSTANCE_ID, tid)
                continue
            if ok:
                _mark_assignment_done(table, INSTANCE_ID, tid)
                # #315 — 不在此处直接 promote(见 skip_done 分支同款理由):launch-vm 返回 0
                # 只代表脚本退出 0,不代表 gateway 已活、路由已通。半成品 VM(START 后被杀/
                # gateway 没起)会被误标 running。统一由 _write_ddb 健康门 promote(唯一写手)。
            else:
                # P0.1: classify the failure. Transient (throttle/timeout) →
                # short jittered retry, do NOT burn retry budget. Real
                # failure (launch-vm rc≠0) → 10s backoff + bump retries; the
                # budget cap still catches genuinely stuck tenants.
                delay = _backoff_for_reason(err_reason)
                is_transient = delay <= DISPATCH_BACKOFF_TRANSIENT_SEC
                _dispatch_enqueue(tid, delay)
                if not is_transient:
                    new_retries = _bump_dispatch_retries(tid)
                # #315(codex 点 5)真机 15/300 卡 creating 根因:旧版无条件
                # _mark_assignment_failed(pending→failed),但 _query_pending_assignments
                # 只查 pending → assignment 一旦 failed,dispatch_tick 下轮再也捞不到它,
                # _dispatch_enqueue 的 backoff 成摆设(队列说"可起"但 DDB 捞不到 pending 就不起)
                # → tenant 永久卡 creating、assignment=failed、无 vm.json(真机实证:flock 争用下
                # 起 VM 真失败一次即永久卡)。修:普通失败【保持 assignment pending】(下轮 backoff
                # 到期 dispatch_tick 重新捞到重试),【只有 retries 超预算】才标终态 failed(不再重试)。
                if (
                    not is_transient
                    and new_retries is not None
                    and (new_retries >= DISPATCH_RETRY_BUDGET)
                ):
                    # #315(codex review7 P2)先写 tenant 终态,按返回的终态给 assignment 一致标签:
                    # "failed"→failed;"done"(健康线程已 promote running)→done 避免矛盾态;
                    # None(DDB 异常)→保持 pending 下轮重捞重试,不断链(两写不原子的安全顺序)。
                    _final = _flag_requires_intervention(tid)
                    if _final == "done":
                        _mark_assignment_done(table, INSTANCE_ID, tid)
                    elif _final == "failed":
                        _mark_assignment_failed(
                            table, INSTANCE_ID, tid, reason=err_reason
                        )


def _dispatch_loop():
    """Poll thread. Only started when ASSIGNMENTS_TABLE is set (systemd env)."""
    while True:
        try:
            table = _get_ddb().Table(ASSIGNMENTS_TABLE)
            _dispatch_tick(table)
        except Exception as e:
            # Never crash — main _poll_loop keeps the host alive.
            print(f"dispatch loop error (non-fatal): {e}")
        _agent_loop_tick("dispatch")  # #387 self-stamped
        time.sleep(DISPATCH_POLL)


# ═══════════════════════════════════════════
# P0.2 Housekeeping — bidirectional reconciliation (kubelet §5)
# ═══════════════════════════════════════════
#
# The 5s dispatch tick only handles the forward direction (DDB says pending →
# maybe launch). It cannot detect two silent-drift classes:
#
#   Class A (desired without local):  DDB row exists as pending, we somehow
#       have no vm.json on disk AND no queue entry AND no inflight subprocess.
#       Cause: agent process crashed mid-launch, or launch-vm.sh succeeded but
#       vm.json write got wiped by a reboot. Fix: enqueue for immediate re-pull.
#
#   Class B (local without desired):  vm.json exists on disk but DDB has no
#       matching assignment (or the row is 'done'/'failed', not 'pending').
#       Cause: control-plane deleted the tenant while VM still runs; or a
#       manual test-launched tenant. Fix: report/log; deletion is _reap_
#       orphan_firecrackers's job (it uses its own missing-vm.json signal),
#       so housekeeping stays report-only for Class B — no destructive
#       action from the housekeeper (safety > completeness).
#
# Ordering rule (kubelet_pods.go:1219-1233): snapshot ACTUAL state BEFORE
# reading desired state. If we read desired first, a tenant that just landed
# on disk between reads would appear as Class A and get double-launched.
#
# Cadence: DISPATCH_HOUSEKEEPING_SEC (60s) — much slower than the 5s dispatch
# tick because DDB Query cost + orphan-detection is cheap-per-tick but pointless
# to run every poll interval.


def _housekeeping_reconcile(table):
    """One housekeeping pass. Pure orchestration — I/O is delegated to
    _local_vm_inventory / _query_pending_assignments / _dispatch_enqueue so
    tests can drive this without a table.

    Returns a dict summary {re_enqueued, orphaned, desired, local} for logging
    and for tests to assert on.
    """
    if not INSTANCE_ID:
        return {"re_enqueued": 0, "orphaned": 0, "desired": 0, "local": 0}
    # Snapshot ACTUAL first — kubelet_pods.go:1219-1233 ordering rule.
    local = _local_vm_inventory()
    # Snapshot inflight second (subset of the truth about "we own this tid").
    with _dispatch_inflight_lock:
        inflight = set(_dispatch_inflight)
    # Now read desired.
    rows = _query_pending_assignments(table, INSTANCE_ID)
    desired = {r.get("tenant_id"): r for r in rows if r.get("tenant_id")}
    # Also filter out tenants already backing off (queued, not-yet-due) —
    # they are being handled, not silently drifting.
    with _dispatch_queue_lock:
        queued = set(_dispatch_queue.keys())
    # Class A: DDB says pending, we have no local trace at all.
    re_enqueued = 0
    for tid in desired.keys():
        if tid in local:
            continue
        if tid in inflight:
            continue
        if tid in queued:
            continue
        # We should be launching this tenant but haven't — enqueue with a
        # small jittered delay so 380 hosts that all detected the same
        # crash-recovery drift don't fire at once. Use transient backoff
        # (1s + up to 0.5s jitter) — this is not a failure retry, it's a
        # forward re-drive of desired state.
        _dispatch_enqueue(tid, DISPATCH_BACKOFF_TRANSIENT_SEC)
        re_enqueued += 1
    # Class B: vm.json on disk with no matching pending assignment. We only
    # report; deletion belongs to _reap_orphan_firecrackers (which checks a
    # different signal — missing vm.json, not missing DDB row).
    orphaned = sum(1 for tid in local if tid not in desired)
    if re_enqueued or orphaned:
        print(
            f"housekeeping: desired={len(desired)} local={len(local)} "
            f"re_enqueued={re_enqueued} orphaned_report={orphaned}"
        )
    return {
        "re_enqueued": re_enqueued,
        "orphaned": orphaned,
        "desired": len(desired),
        "local": len(local),
    }


def _report_route_drift() -> dict[str, int]:
    """P2b (contract §3): three-set diff between local VM inventory, the
    tenants-table descriptors that name this host, and live iptables DNAT
    rules. Report only — no auto-mutation (blast radius: writing DNAT for
    a stale row could shadow a live tenant of the same port). A follow-up
    audited PR will wire remediation on top of this signal.

    Returns a summary {orphan_dnat, missing_dnat, ghost_descriptor} for
    logging + tests. Missing table names / IMDS quietly return zeros.
    """
    empty = {"orphan_dnat": 0, "missing_dnat": 0, "ghost_descriptor": 0}
    if not TENANTS_TABLE or not INSTANCE_ID:
        return empty
    host_ip = _get_host_private_ip()
    if not host_ip:
        return empty
    try:
        vm_dir_tids: set[str] = set()
        try:
            vm_dir_tids = set(os.listdir(VM_DIR))
        except FileNotFoundError:
            pass
        try:
            dnat_rules = route_ops.list_dnat_rules()
        except Exception as e:
            print(f"route_drift: iptables list failed (skip): {e}")
            return empty
        # Only descriptors that name THIS host contribute to the diff. Full
        # table scan is fine — this runs every 60s and each host owns ≤400
        # rows, so the "host_private_ip = :ip" filter kicks in on the DDB
        # side after Query. But there's no GSI; a scan with a filter is the
        # honest cost. Skip if the table is small / new.
        table = _get_ddb().Table(TENANTS_TABLE)
        ddb_desc: dict[str, dict] = {}
        try:
            resp = table.scan(
                FilterExpression="host_private_ip = :ip",
                ExpressionAttributeValues={":ip": host_ip},
                ProjectionExpression="id, host_port, guest_ip",
            )
            for row in resp.get("Items", []):
                tid = row.get("id")
                if tid:
                    ddb_desc[tid] = {
                        "host_port": row.get("host_port"),
                        "guest_ip": row.get("guest_ip"),
                    }
        except Exception as e:
            print(f"route_drift: DDB scan failed (skip): {e}")
            return empty
        diff = route_ops.reconcile_drift(vm_dir_tids, ddb_desc, dnat_rules)
        summary = {k: len(v) for k, v in diff.items()}
        if any(summary.values()):
            print(
                f"route_drift: orphan_dnat={summary['orphan_dnat']} "
                f"missing_dnat={summary['missing_dnat']} "
                f"ghost_descriptor={summary['ghost_descriptor']}"
            )
        return summary
    except Exception as e:
        # Never let drift-reporting kill the housekeeping loop.
        print(f"route_drift error (non-fatal): {e}")
        return empty


def _housekeeping_loop():
    """Independent tick from the 5s dispatch loop. Only started when
    ASSIGNMENTS_TABLE is set."""
    while True:
        try:
            table = _get_ddb().Table(ASSIGNMENTS_TABLE)
            _housekeeping_reconcile(table)
        except Exception as e:
            print(f"housekeeping loop error (non-fatal): {e}")
        # P2b (contract §3): report-only drift check. Independent of the
        # assignments-table reconcile above so a scan failure doesn't
        # suppress dispatch housekeeping.
        try:
            _report_route_drift()
        except Exception as e:
            print(f"route drift check error (non-fatal): {e}")
        _agent_loop_tick("housekeeping")  # #387 self-stamped
        time.sleep(DISPATCH_HOUSEKEEPING_SEC)


def _poll_loop():
    while True:
        try:
            # 1.3.0: heartbeat at the start so failures in tenant probing
            # don't suppress the host-level liveness signal.
            _write_host_heartbeat()
            _reap_orphan_firecrackers()
            results = _probe_all()
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with _lock:
                _status.clear()
                for tid, info in results.items():
                    info["updated_at"] = ts
                    _status[tid] = info
            _write_ddb(results)
            _adjust_balloons(results)
            _probe_ssm_agent()  # #387: cached here, never at scrape time
        except Exception as e:
            print(f"poll error: {e}")
        _agent_loop_tick("poll")  # #387: self-stamped (period=POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            # Prometheus text exposition (issue #4). Scraped by sibling
            # ADOT collector that remote-writes to AMP.
            with _lock:
                data = dict(_status)
                agent_stats = {
                    "loop_last_tick": dict(_agent_metrics["loop_last_tick"]),
                    "route_ensure_failures": _agent_metrics[
                        "route_ensure_failures"
                    ],
                    "build_sha": _agent_metrics.get("build_sha"),
                    "ssm_agent_up": _agent_metrics.get("ssm_agent_up"),
                }
            # #387: read the port bitmap ONLY if already initialized — going
            # through _get_port_bitmap() here would lazily rebuild from
            # iptables (mutating global state) on a host that never allocated
            # a route. Uninitialized → port_stats=None → series absent.
            port_stats = None
            bitmap = _port_bitmap
            if bitmap is not None:
                try:
                    route_ops.rebuild_bitmap_from_iptables(bitmap)
                    used = bitmap.used_count()
                    quarantined = len(
                        route_ops.get_quarantined_ports() - bitmap.snapshot()
                    )
                    port_stats = {
                        "used": used,
                        "total": route_ops.PORT_RANGE_HIGH
                        - route_ops.PORT_RANGE_LOW
                        + 1,
                        "quarantined": quarantined,
                    }
                except Exception as e:  # noqa: BLE001
                    # Collection failure → omit the whole group (no fake 0s,
                    # never crash /metrics).
                    print(f"port stats collection failed (omitted): {e}")
                    port_stats = None
            body = _render_metrics_text(data, port_stats, agent_stats).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/health", "/"):
            with _lock:
                data = dict(_status)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    print(
        f"openclaw-agent starting on :{PORT}, poll every {POLL_INTERVAL}s, table={TENANTS_TABLE}"
    )
    # #387 (#373 病史): freeze THIS process's own file sha256 at startup.
    # Hot-copying host-agent.py without restarting leaves disk-new/process-old
    # drift — a startup-frozen hash makes that drift observable in /metrics.
    try:
        import hashlib
        from pathlib import Path

        _sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        with _lock:
            _agent_metrics["build_sha"] = _sha
        print(f"agent build sha256: {_sha}")
    except Exception as e:  # noqa: BLE001
        print(f"build sha capture failed (metric absent): {e}")
    # #387 (codex 评审): startup port-bitmap recovery. Without this, an agent
    # restart on a host whose VMs are ALL stopped/down leaves _port_bitmap None
    # forever (no promote → no lazy init) and the port gauges stay absent while
    # real DNAT allocations exist — exhaustion alerts go blind. Rebuild once at
    # startup; only publish the singleton when recovery found in-use ports, so
    # a genuinely fresh host still reports absent (DoD ⓪: no fake zeros).
    try:
        _probe_bm = route_ops.PortBitmap()
        _recovered = route_ops.rebuild_bitmap_from_iptables(_probe_bm)
        if _recovered > 0:
            global _port_bitmap
            with _route_singleton_lock:
                if _port_bitmap is None:
                    _port_bitmap = _probe_bm
            print(f"port_bitmap: startup recovery found {_recovered} in-use ports")
    except Exception as e:  # noqa: BLE001
        print(f"port_bitmap startup recovery failed (stays lazy): {e}")
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    # #321: disk GC on its own always-on thread (independent of dispatch/poll so a
    # stuck rm -rf never blocks heartbeat → no false stale-restart).
    g = threading.Thread(target=_disk_gc_loop, daemon=True)
    g.start()
    # #340: /data free-space report on its own thread (independent of poll's slow VM
    # probes so a busy host's disk timestamp stays fresh → stale ⟺ agent actually down,
    # letting dispatch fail-open safely on stale reads instead of mis-blocking).
    dr = threading.Thread(target=_disk_report_loop, daemon=True)
    dr.start()
    # Pull-mode dispatch reconciler (二期 SPEC/specs/sqs-dispatch/interfaces.md).
    # Only starts when ASSIGNMENTS_TABLE is injected via systemd (dispatch.enabled
    # && dispatch.mode=pull in config.yml). One extra daemon thread; no impact
    # on the health/heartbeat loop above.
    if ASSIGNMENTS_TABLE:
        print(
            f"openclaw-agent dispatch pull mode: assignments={ASSIGNMENTS_TABLE} "
            f"poll={DISPATCH_POLL}s parallel={DISPATCH_PARALLEL} "
            f"housekeeping={DISPATCH_HOUSEKEEPING_SEC}s"
        )
        d = threading.Thread(target=_dispatch_loop, daemon=True)
        d.start()
        # P0.2: housekeeping runs independently — slower cadence, catches
        # silent-drift classes the 5s dispatch tick can't see (crash-recovery
        # after mid-launch abort, vm.json wipe by reboot, etc.).
        hk = threading.Thread(target=_housekeeping_loop, daemon=True)
        hk.start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
