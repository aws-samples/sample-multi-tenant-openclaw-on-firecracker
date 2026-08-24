#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""OpenClaw Host Agent — probes local VMs and writes health status to DynamoDB.
Replaces per-tenant SSM health checks. Runs as systemd service on each host.
"""

import fcntl
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
)

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
VM_DIR = "/data/firecracker-vms"
GATEWAY_PORT = 18789
TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "")
# Since 1.3.0: host-agent also writes a heartbeat to the hosts table so the
# health_check Lambda can do AZ-level failover (it needs to know which hosts
# are still alive at the host level, not just whether their tenants reported).
HOSTS_TABLE = os.environ.get("HOSTS_TABLE", "")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "")
# global snapshot deletion can fail closed on a freshness timestamp instead of trusting a
# one-shot best-effort write from the mutation that may have failed.
IMAGE_SLOTS_FILE = os.environ.get("IMAGE_SLOTS_FILE", "/data/firecracker-assets/slots.json")
# P2b: ElastiCache primary endpoint DNS name (contract §8 — never a node IP).
# Empty string disables Redis writes; host-agent still writes DDB as normal
# so an ungated deploy stays backwards-compatible with pre-P2 environments.
ENGINE_REDIS_ENDPOINT = os.environ.get("ENGINE_REDIS_ENDPOINT", "")
ENGINE_REDIS_PORT = int(os.environ.get("ENGINE_REDIS_PORT", "6379"))


# ═══════════════════════════════════════════
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


def _render_metrics_text(
    snapshots, port_stats=None, agent_stats=None, stranding=None
):
    """Render the in-memory snapshots dict as Prometheus exposition text.

    Pure function — no I/O — so it is easy to assert against in unit tests.
    Always emits HELP/TYPE headers (even with zero samples) so that scrapers
    that validate metadata don't choke on a quiet host.

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
    # vm_health=1 + app_health=0 is exactly the "ping ok, gateway crashloop"
    out.append(
        "# HELP openclaw_app_health 1 if the tenant gateway answered HTTP, else 0"
    )
    out.append("# TYPE openclaw_app_health gauge")
    for tid, info in snapshots.items():
        if not isinstance(info, dict):
            continue
        v = 1 if info.get("app_health") == "up" else 0
        out.append(f'openclaw_app_health{{tenant="{tid}"}} {v}')
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
    # Grafana 侧算比率,避免 agent 侧固化一个除法口径。stranding=None → 整组省略。
    if isinstance(stranding, tuple) and len(stranding) == 4:
        s_vcpu, s_mem, alloc_v, alloc_m = stranding
        out.append(
            "# HELP openclaw_host_stranded_vcpu allocatable vCPU that can never"
            " be placed because the memory dimension can no longer fit the"
            " smallest tenant spec (2D stranding; M-series strands CPU by design"
            " under a uniform 1:4 ratio)"
        )
        out.append("# TYPE openclaw_host_stranded_vcpu gauge")
        out.append(f"openclaw_host_stranded_vcpu {int(s_vcpu)}")
        out.append(
            "# HELP openclaw_host_stranded_mem_mb allocatable memory that can"
            " never be placed because the vCPU dimension is exhausted"
        )
        out.append("# TYPE openclaw_host_stranded_mem_mb gauge")
        out.append(f"openclaw_host_stranded_mem_mb {int(s_mem)}")
        out.append(
            "# HELP openclaw_host_allocatable_vcpu total allocatable vCPU"
            " (total_vcpu x per-family cpu overcommit) — denominator for the"
            " stranding ratio"
        )
        out.append("# TYPE openclaw_host_allocatable_vcpu gauge")
        out.append(f"openclaw_host_allocatable_vcpu {int(alloc_v)}")
        out.append(
            "# HELP openclaw_host_allocatable_mem_mb total allocatable memory"
            " (total_mem_mb x per-family mem overcommit)"
        )
        out.append("# TYPE openclaw_host_allocatable_mem_mb gauge")
        out.append(f"openclaw_host_allocatable_mem_mb {int(alloc_m)}")
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
        _bss = agent_stats.get("backup_script_stale")
        if _bss is not None:
            out.append(
                "# HELP openclaw_backup_script_stale 1 if the local backup-data.sh"
                " is missing or stale, so the host-local backup sweep is REFUSING"
                " to run. The refusal is fail-closed (a stale script produces"
                " corrupt/unfindable restore points) but it means this host is"
                " taking NO local backups — and if the central schedule is also"
                " disabled, no backups at all. The backup loop's own heartbeat"
                " stays healthy, so this is the only signal (#469 R7)"
            )
            out.append("# TYPE openclaw_backup_script_stale gauge")
            out.append(f"openclaw_backup_script_stale {int(_bss)}")
        _vlp = agent_stats.get("backup_vm_left_paused")
        if _vlp is not None:
            out.append(
                "# HELP openclaw_backup_vm_left_paused 1 if a host-local backup left a"
                " tenant VM PAUSED (bounded resume retries exhausted, or the script was"
                " SIGKILLed before its EXIT trap finished). The tenant row still says"
                " running and the health check CANNOT tell the difference — a paused"
                " Firecracker process is still alive — so this metric is the only signal"
                " that a customer guest is frozen. The backup loop's own heartbeat stays"
                " healthy too. Needs a manual resume or a lifecycle op (#469)"
            )
            out.append("# TYPE openclaw_backup_vm_left_paused gauge")
            out.append(f"openclaw_backup_vm_left_paused {int(_vlp)}")
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
        # 这条答"它在但收不动命令"。判据锚点见 _probe_ssm_buffer_full 的注释。
        buf = agent_stats.get("ssm_buffer_full")
        if buf is not None:
            out.append(
                "# HELP openclaw_host_ssm_buffer_full 1 if the local SSM agent log"
                " shows the MDS interactor waiting on a full worker buffer"
                " (mdsinteractor.go: 'till the buffer is free'), else 0. Early-warning"
                " only: after the aggregated-dispatch change each host sees <=1-2"
                " concurrent commands, far from the 5-worker+5-buffer ceiling."
            )
            out.append("# TYPE openclaw_host_ssm_buffer_full gauge")
            out.append(f"openclaw_host_ssm_buffer_full {1 if buf else 0}")
        drift = agent_stats.get("route_drift")
        if drift:
            out.append(
                "# HELP openclaw_host_route_drift three-set reconciliation drift"
                " counts. foreign_vm is the cross-host split signal: this host holds"
                " the VM while the ledger attributes the tenant elsewhere."
            )
            out.append("# TYPE openclaw_host_route_drift gauge")
            for _k in ("orphan_dnat", "missing_dnat", "ghost_descriptor", "foreign_vm"):
                out.append(
                    f'openclaw_host_route_drift{{kind="{_k}"}} '
                    f"{int(drift.get(_k) or 0)}"
                )
    return "\n".join(out) + "\n"


# Balloon config (from /etc/platform.env)
BALLOON_ENABLED = os.environ.get("BALLOON_ENABLED", "false") == "true"
BALLOON_MAX_INFLATE_RATIO = float(os.environ.get("BALLOON_MAX_INFLATE_RATIO", "0.4"))
BALLOON_MIN_GUEST_AVAILABLE_MB = int(
    os.environ.get("BALLOON_MIN_GUEST_AVAILABLE_MB", "512")
)

# DynamoDB client (region auto-detected from instance metadata)
_ddb = None
_cred_lock = threading.Lock()
_consecutive_cred_failures = 0
# #549 — 连续凭据失败达到该数就 sys.exit(1),让 systemd Restart=always 重建整个进程
_CRED_FAILURE_EXIT_THRESHOLD = int(os.environ.get("AGENT_CRED_FAIL_EXIT_THRESHOLD", "12"))
_status = {}
_lock = threading.Lock()

# _status is a tenant map; a host-level key mixed in would be rendered as a
# phantom tenant by the per-tenant gauge loops above. Guarded by _lock (same
# lock as _status: all writers already hold it or write scalar values).
_agent_metrics = {
    "loop_last_tick": {},  # loop_name -> unix epoch of last completed pass
    "route_ensure_failures": 0,  # counter: skip-promote route failures
    # "build_sha" set once in main(); "ssm_agent_up" set by poll loop probe.
    #      "route_drift" 由 _report_route_drift 设。两者都不预置键 —— 未探测过时
    #      /metrics 里对应序列缺席,而不是谎报 0(与 port_stats=None 同一范式)。
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
        # #549 — 用全新 Session(而非 boto3.resource 走的默认 session)重跑凭据链:
        # botocore 在 create_client 时一次性把凭据绑到 signer,启动瞬间 IMDS 抖动会把
        # 该 client 永久绑成"无凭据"。_reset_ddb_clients() 置 None 后,这里以新 session 重建
        # 才能真正逃出坏凭据,与 _get_disk_ddb/_get_backup_ddb 同款。
        _ddb = boto3.Session().resource(
            "dynamodb",
            region_name=_resolve_region(),
            config=BotoConfig(retries={"max_attempts": 2}),
        )
    return _ddb


def _is_credential_error(exc) -> bool:
    """凭据类异常:client 在创建瞬间把 credentials 绑到 signer,若那一刻 IMDS 抖动解析为
    None,则该 client 之后每次请求都 NoCredentialsError,IMDS 事后恢复也没用。这类异常必须
    让缓存的 resource 失效重建(用全新 Session 重跑凭据链),否则进程永久失效。"""
    if isinstance(exc, (NoCredentialsError, CredentialRetrievalError, PartialCredentialsError)):
        return True
    if isinstance(exc, ClientError):
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
        return code in (
            "ExpiredToken",
            "ExpiredTokenException",
            "RequestExpired",
            "RequestExpiredException",
        )
    return False


def _reset_ddb_clients() -> None:
    """凭据失败后废弃所有缓存的 DDB resource,下次取用时以全新 Session 重建。"""
    global _ddb, _disk_ddb, _backup_ddb
    with _cred_lock:
        _ddb = None
        _disk_ddb = None
        _backup_ddb = None


def _note_ddb_ok() -> None:
    """一次 DDB 调用成功即清零连续凭据失败计数(自愈成功)。"""
    global _consecutive_cred_failures
    with _cred_lock:
        _consecutive_cred_failures = 0


def _handle_ddb_exc(exc, label: str) -> None:
    """统一处理 DDB 调用异常:打印(non-fatal)保留原日志文案;若是凭据类异常则废弃缓存
    client 触发重建并累计连续失败;连续超阈值则 sys.exit(1) 让 systemd Restart=always 接管。"""
    global _consecutive_cred_failures
    print(f"{label} (non-fatal): {exc}")
    if not _is_credential_error(exc):
        return
    _reset_ddb_clients()
    with _cred_lock:
        _consecutive_cred_failures += 1
        n = _consecutive_cred_failures
    if n == 1:
        print(
            f"{label}: credential error — invalidated cached DDB clients; "
            "will rebuild with a fresh boto3 session on the next tick (#549)"
        )
    if n >= _CRED_FAILURE_EXIT_THRESHOLD:
        print(
            f"host-agent: {n} consecutive credential failures >= "
            f"{_CRED_FAILURE_EXIT_THRESHOLD}; exiting so systemd Restart=always rebuilds "
            "the process (tenant VMs survive: KillMode=process, #323/#435) (#549)"
        )
        sys.stdout.flush()
        sys.exit(1)


_recovering = set()  # Track VMs being recovered to avoid duplicate launches
# 失败的 recover 不再每 5s 紧密重试(thundering herd:同 tid 反复 fire launch-vm 抢 flock 空转),
# 按 RECOVER_BACKOFF_SEC + jitter 退避;成功即清除。
_recover_backoff = {}
RECOVER_BACKOFF_SEC = float(os.environ.get("OC_RECOVER_BACKOFF", "15"))

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


def _host_btime():
    """host 的开机 UNIX 时刻(/proc/stat 的 btime 行)。None 表示读不到。

    /proc/<pid>/stat 的 starttime 是"自开机起的 clock ticks",要换成挂钟时间必须加上开机
    时刻。缓存在模块级:host 的开机时刻在本进程生命周期内是常量。"""
    if _btime_cache["v"] is not None:
        return _btime_cache["v"]
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    _btime_cache["v"] = int(line.split()[1])
                    return _btime_cache["v"]
    except Exception:  # noqa: BLE001 — 拿不到就退化成"无启动证据",不影响健康上报
        pass
    return None


_btime_cache = {"v": None}


def _fc_boot_iso(fc_pid):
    """这个 Firecracker 进程的启动时刻,ISO-8601 UTC 串;拿不到返 ""。

    ADR §5.4a 路 2 的证据强度补强(见 _read_proc_start_ticks 的说明):控制面用它判断
    「这个 FC 进程是不是本次 rebuild 之后新起的」,以排掉"旧 FC 未被杀掉、vm.json 已改新
    版本、VM 却还跑着旧 rootfs"这种版本相符的假成功。

    返回空串(而不是抛)是刻意的:读不到 /proc 只意味着少一份证据,控制面据此拒绝单凭版本
    判 done —— 保守方向,不会造成假成功。"""
    ticks = _read_proc_start_ticks(fc_pid)
    btime = _host_btime()
    if ticks is None or btime is None or not _CLK_TCK:
        return ""
    try:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(btime + (ticks / float(_CLK_TCK)))
        )
    except Exception:  # noqa: BLE001
        return ""


def _probe_app_health(guest_ip, chat_ep):
    """探 guest gateway 的 app_health,返回 "up"/"down"。

    #526 — 对开了 chatCompletions 的租户(chat_ep 为真)收紧判据:探真实数据面入口
    /v1/chat/completions,返回 404 = 端点缺失(本 bug:restore/wake 传第10位 0 →
    harden-config del(chatCompletions),客户拿 404 而 app_health 却 up 静默)= down;
    401/200 等真实码 = 端点在 = up。
    chat_ep 假的租户维持既有宽松判据:探 `/` 任意非 000 码即 up —— openclaw 2026.2.26 的
    gateway 任何路径都 404,不能拿"具体路由 404"当 down(病史:全 fleet 误报 down、卡 creating)。
    curl 退出码 7(拒连)/28(超时)或 http_code 000 = 端口不通 = down。
    """
    try:
        base = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "--connect-timeout", "3"]
        if chat_ep:
            args = base + ["-X", "POST", "-H", "content-type: application/json",
                           "-d", "{}",
                           f"http://{guest_ip}:{GATEWAY_PORT}/v1/chat/completions"]
        else:
            args = base + [f"http://{guest_ip}:{GATEWAY_PORT}/"]
        r = subprocess.run(args, capture_output=True, timeout=8)
        code = (r.stdout or b"").decode(errors="replace").strip()
        if r.returncode != 0 or not code.isdigit() or code == "000":
            return "down"  # 端口不通/无 HTTP 应答
        if chat_ep and code == "404":
            return "down"  # chatCompletions 端点缺失(#526)
        return "up"
    except Exception:
        return "down"


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
            # what actually booted (launch-vm.sh writes it from the /30 scheme).
            # _write_ddb reconciles the DDB record's guest_ip to this so a
            # control-plane↔data-plane drift can't leave a ghost IP that edge
            # routes to a black hole. (vm_num is deliberately NOT reconciled —
            guest_ip = cfg.get("guest_ip", "")
        except Exception:
            continue
        if not guest_ip:
            continue

        # 迁移 restore 也不改写)。带回 results 供 _report 回填 DDB 的 phys_vm_num——历史
        # /迁移前建的租户 DDB 里没有 phys_vm_num,靠这里从物理真值补齐,补齐后撞号检查
        # (create+migrate)才对这些老租户也生效。
        phys_vm_num = cfg.get("vm_num")

        # ADR-rebuild-idempotency-sync-contract §5.4a 路 2 —— 本次启动【实际使用】的
        # 镜像版本。launch-vm.sh 每次起 VM 都把它写进这个 vm.json(见该脚本 §"记录本次
        # 启动实际使用的版本"),而本函数本来就在读这个文件、下游 _refresh_health 本来就
        # 在往同一张 tenants 表写字段 —— 真机上「我现在跑的是哪个版本」一直都有,只是
        # 没被捎上去。带上它,控制面就能拿【期望版本】与【上报的实际版本】异步对账,
        # 不再完全依赖那一次 SSM 回音的成败(回执丢失时最坏等一个心跳周期 15s)。
        # 注:这是 best-effort 审计值,只用于【确认成功】,不可用于判定失败 —— 见
        # _refresh_health 里的说明。
        observed_image = cfg.get("image_snapshot_time") or ""

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
                # 不带 observed_image_snapshot_time:此刻 Firecracker 并未在跑(或 guest
                # 不可达正在重建网络),vm.json 里的版本只是"上次启动打算用哪个版本",
                # 不构成"这台 VM 现在真的跑着该版本"的证据。上报它会让控制面把一次
                # 启动到一半就失败的 rebuild 判成成功(假成功)。
            }
            continue

        _recovering.discard(tenant_id)
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
            # #526 — chat_ep=1 的租户探真实数据面入口(chatCompletions),404=端点缺失=down;
            # chat_ep 假的维持"端口任意码即 up"的 2.26 兼容判据。详见 _probe_app_health。
            app_health = _probe_app_health(guest_ip, cfg.get("chat_ep", 0))
        elif _register_net_poll(tenant_id, guest_reachable=False):
            # FC alive but guest unreachable past the threshold — rebuild network.
            _force_relaunch_vm(tenant_id, cfg)
            results[tenant_id] = {
                "vm_health": "recovering",
                "app_health": "down",
                "guest_ip": guest_ip,
                "phys_vm_num": phys_vm_num,
                # 不带 observed_image_snapshot_time:此刻 Firecracker 并未在跑(或 guest
                # 不可达正在重建网络),vm.json 里的版本只是"上次启动打算用哪个版本",
                # 不构成"这台 VM 现在真的跑着该版本"的证据。上报它会让控制面把一次
                # 启动到一半就失败的 rebuild 判成成功(假成功)。
            }
            continue

        results[tenant_id] = {
            "vm_health": vm_health,
            "app_health": app_health,
            "guest_ip": guest_ip,
            "fc_pid": fc_pid,
            "phys_vm_num": phys_vm_num,
            # 只在【VM 真的起来了】时才上报版本(vm_health=="up" 即 guest ping 通),并连
            # FC 进程的启动时刻一起上报。
            #
            # 为什么两者都必须有:launch-vm.sh 在起 firecracker 之前 800+ 行就把版本写进了
            # vm.json(建盘/mkfs/解压/拉备份都在那之后),所以「vm.json 里有目标版本」只
            # 证明启动流程走到了那一行。两种假成功由此而来:
            #   ① 中途失败 → 版本==目标但 VM 根本没起(ping 不通挡掉);
            #   ② 旧 FC 没被 stop-vm 杀掉 → VM ping 得通、vm.json 已改成新版本,但跑的还是
            #      【旧】rootfs(ping 挡不住,只能靠进程启动时刻:它早于本次 rebuild 发起
            #      时刻,说明这不是本次起来的进程)。
            # 控制面据此把判据从「版本相符」升格为「版本相符 且 进程是本次 rebuild 之后
            # 新起的」。缺失该时刻(读不到 /proc)时控制面不得单凭版本判 done。
            **(
                {
                    "observed_image_snapshot_time": observed_image,
                    "observed_boot_at": _fc_boot_iso(fc_pid),
                }
                if vm_health == "up"
                else {}
            ),
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
# 消费侧 TTL(DISPATCH_DISK_REPORT_TTL_SEC,默认 90s)是它的数倍,容几次漏报不误判陈旧。
_DISK_REPORT_INTERVAL_SEC = int(os.environ.get("OC_DISK_REPORT_INTERVAL_SEC", "15"))

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


# 为什么必须下沉(算数,不是偏好):中心方案是 EventBridge rate(30 minutes) → 一个
# Lambda 每次备 BACKUP_BATCH_LIMIT(20)个,即 40 个/小时、24 小时上限 960 个。而目标
# 是 10 万租户每 24h 全量一次 —— 差约 104 倍;连 1000 个租户都要 25h > 24h 间隔。
# 下沉后每台机器只管自己的租户、并发天然按机器打散:按 ADR 真机实测(单备份最坏 22s、
# 本地并发 2),一台 380 租户最坏 1.2h/轮、1000 租户 3.1h/轮,都远小于 24h。
#
# 范围(只做 R7 字面要求,不扩):本线程只接【定时全量】这一路。手动/删前/迁移前的
# 单租户即时备份仍走 openclaw-backup Lambda 的 tenant_id 入口(tenant_service 的
# 删前 fail-closed 依赖它同步返回成败),本改动不碰那条路。
# 备份任务状态机、间隔在线改、单租户跨机搬迁属 ADR-tenant-backup-host-local-scheduling
# 的 F1 剩余部分与 F2/F3,该 ADR 仍是 Proposed(未经客户 sign-off),不在本次范围。
_BACKUP_LOOP_ENABLED = os.environ.get("OC_BACKUP_LOOP", "1") not in ("0", "false", "")
# tick 60s:间隔是小时级,tick 只决定"到期后多久被发现",60s 足够且不空转打 DDB。
_BACKUP_TICK_SEC = int(os.environ.get("OC_BACKUP_TICK_SEC", "60"))
_BACKUP_INTERVAL_HOURS = float(os.environ.get("OC_BACKUP_INTERVAL_HOURS", "24"))
# 本轮【串行】备,不开本地并发。算数够用:单备份最坏 22s(ADR §2.4 真机),一台 1000
# 租户串行也只要 6.1h/轮 ≪ 24h 间隔;而 ADR 同一处实测"一个 pigz 就能吃满 16 核",
# 并发只是互相抢 CPU,与 R7"不抢客户 CPU"的要求相反。需要并发时再加,不预留空参数。
# R7 明确要求"备份前识别当前 CPU 负载,有大量用户占用 CPU 时不抢 CPU 做备份"。
# 判据用 1 分钟 loadavg / 核数:>0.7 即认为用户负载已高,本轮整体让路(不是逐个跳过 ——
# 逐个跳过会在高负载下反复起 pigz 又退,依然抢 CPU)。备份晚几十分钟无所谓(间隔 24h),
# 抢了客户的 CPU 是真问题。
_BACKUP_LOAD_CEILING = float(os.environ.get("OC_BACKUP_LOAD_CEILING", "0.7"))
# 让路必须有【上界】(codex 独立复审第三轮)。原实现只要负载持续高于阈值就无限期跳过,
# 一台长期繁忙的 host 会永远不备份 —— 而中心调度关掉后没有第二个执行者兜底,于是
# 24 小时的备份保证被静默违反。我此前在证据里写"备份晚几十分钟无所谓(间隔 24h)"
# 是对的,但没上界的让路不是"晚几十分钟",是"永远不备"。
#
# 判据用【最久没备的那个租户已经过期多久】,而不是"让路了几轮":前者直接对应要守的
# SLA(24h),后者与 SLA 无关(tick 变了含义就变)。超过硬上限就不再让路,改用
# nice/ionice 降优先级跑 —— 抢一点 CPU 也比丢掉备份保证强,而降优先级让这个代价可控。
_BACKUP_MAX_DEFER_HOURS = float(os.environ.get("OC_BACKUP_MAX_DEFER_HOURS", "6"))
# 软到期阈值(codex 独立复审第六轮)。让路预算必须【从 interval 里切出来】而不是叠在
# 它之上,否则最坏年龄 = interval + MAX_DEFER = 30h,而配置明写「至少每 24h」
# (config.yml.example:189)。软到期开始尝试、硬期限(= interval)必须跑,最坏年龄 =
# interval。clamp 到 (0, interval]:MAX_DEFER 被配成 ≥ interval 时不能把阈值压到 0 或负
# (那会变成每 tick 都备),取一个下界 = interval 的一半,并保持"软 ≤ 硬"。
# MAX_DEFER=0 → 软硬重合,退回单一阈值、完全不让路。
def _backup_soft_interval_hours(tenant_count=0):
    """软到期阈值(小时)。随【本机租户数】自适应 —— codex 独立复审第十二轮。

    让路预算必须从 interval 里切出来(第六轮),但切多少不能是定值:排空是【串行】的,
    队列越长需要的提前量越大。ADR 的规模基准是「一台 host 最多 1000 个租户(用户确认)」,
    按单租户最坏 22s 算,满载排空 6.1h —— 若软到期固定在 interval-6h=18h,而插队点又被
    压在 interval-3h=21h(我第九轮夹了半个预算),队尾会到 27h,而配置写的是「至少每 24h」。
    **那是在受支持密度下承诺就不成立**,不是"容量规划"能解释掉的。

    所以提前量取 max(让路预算, 满载排空估算):
      · 租户少 → 由让路预算主导(默认 6h),行为与第六轮一致;
      · 租户多 → 由排空估算主导,自动提早开始尝试,保证整条队列在 interval 内备完。

    下界仍夹在 interval/2:排空估算超过它意味着这台机器的密度已经超出串行方案的能力,
    再往前挪只会把节拍压到不可接受(备份次数翻倍)。那种情形的解法是开本地并发或降
    interval —— 属容量规划,已记入 UNRESOLVED_GAPS。用 22s(最坏值)而不是典型秒级:
    低估提前量会漏备,高估只是多备几次(ADR 的 ~1h/轮 用的是典型值,这里刻意保守)。

    tenant_count=0(调用方还不知道租户数)时退化成"只由让路预算决定",即第六轮的行为。
    """
    _lead = _BACKUP_MAX_DEFER_HOURS
    if tenant_count > 0:
        _lead = max(_lead, tenant_count * _BACKUP_PER_TENANT_ESTIMATE_SEC / 3600.0)
    return max(_BACKUP_INTERVAL_HOURS / 2.0, _BACKUP_INTERVAL_HOURS - _lead)


# 兼容既有引用与测试:默认(不看租户数)的软阈值。
_BACKUP_SOFT_INTERVAL_HOURS = _backup_soft_interval_hours()
# 单租户备份耗时估值,只用来算"整条队列还要多久排空"(codex 第九轮)。
# 22s = ADR §2.4 的真机实测【最坏值】(8G 盘含加密+上传),与 :927 那段算数同源 ——
# 那里用它推出"380 租户 2.3h/轮、1000 租户 6.1h/轮"。取最坏值而不是均值:低估排空时间
# 会让队尾租户超出 interval,而那正是这个估值要防的事;高估只是早一点开始插队。
_BACKUP_PER_TENANT_ESTIMATE_SEC = float(
    os.environ.get("OC_BACKUP_PER_TENANT_ESTIMATE_SEC", "22")
)
# 降优先级跑的 nice 值。19 = 最低优先级:只吃别人不要的 CPU 时间片。
_BACKUP_NICE = os.environ.get("OC_BACKUP_NICE", "19")
# 单个租户备份的墙钟上限。实测最坏 ~22s(8G 盘含加密+上传),300s 留足余量;超时即放弃
# 该租户本轮(不写 last_backup_at → 下轮自然重试),不让一个卡住的备份占死信号量。
_BACKUP_PER_TENANT_TIMEOUT = int(os.environ.get("OC_BACKUP_TIMEOUT_SEC", "300"))
# 超时后先 SIGTERM 给 backup-data.sh 的 EXIT trap 留出清理时间(它要 rm 临时文件 +
# 把 Paused 的 VM Resume 回来),这段时间过完仍不退才 SIGKILL。
#
# ㉕ 这个值必须【大于 cleanup 的有界最坏耗时】(codex 独立复审第二十一轮)。
#
# 原值 15s,注释写着"cleanup 只有一次本地 rm 与一次 unix-socket curl,都是秒级"。
# 那句话在 backup-data.sh 的 ㉑(resume 改成有界重试)之前是真的,㉑ 把它变成了假话:
# 现在 cleanup 会重试 5 次、间隔 1+2+3+4 秒,每次 curl 上限 5s ——
#     最坏 = 5 × 5s(curl) + (1+2+3+4)s(退避) = 35s
# 15s 的宽限期会在 Resume 还没做完时就 SIGKILL,**客户 VM 永久留在 Paused**,
# 正是 ㉑ 本身要消灭的后果。抬到 60s,留 25s 余量;仍远小于 _BACKUP_PER_TENANT_TIMEOUT=300。
#
# 这两个数字分处 bash 与 Python、无法共享常量,所以加了一道 parity 测试把它们锁在一起
# (tests/test_469_r7_host_backup_loop_adversarial.py::TestResumeBudgetFitsInTheTermGrace):
# 谁改一边忘了另一边,那条就红。这与仓里 host-pin-parity 挡内核 pin 漂移是同一个手法。
_BACKUP_TERM_GRACE_SEC = int(os.environ.get("OC_BACKUP_TERM_GRACE_SEC", "60"))
_backup_ddb = None
_BACKUP_DDB_CFG = BotoConfig(
    connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 3}
)


def _get_backup_ddb():
    """备份线程专属 DDB resource(独立 Session;boto3 resource 非线程安全,同 #340)。"""
    global _backup_ddb
    if _backup_ddb is None:
        _backup_ddb = boto3.Session().resource(
            "dynamodb", region_name=_resolve_region(), config=_BACKUP_DDB_CFG
        )
    return _backup_ddb


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


# 所以它天然是"这个进程是不是换了一个"的判据:随心跳上报,控制面比对上轮值,变了就是
# agent 重启过。为什么需要它:ping 通 + gateway 200 在"崩了又被拉起、内存态全丢"时照样绿,
# 那种假活只有靠"进程换代"才看得出来。
_AGENT_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# 答不了"它在但收不动命令"。判据锚点来自 ADR-controlplane-sqs-batch-dispatch.md:74:
# buffer 满每 10s 重试,源码 mdsinteractor.go:522-536,日志行是
# "Will wake up every 10 seconds till the buffer is free"。
# 按 ADR Q3 的结论,聚合下发落地后每 host 同时 ≤1-2 条命令、已远离该区间,所以这是
# 【早期预警】信号,不是当前主故障源 —— 别把它当主解用。
_SSM_AGENT_LOG = "/var/log/amazon/ssm/amazon-ssm-agent.log"
_SSM_BUFFER_FULL_MARK = "till the buffer is free"


def _probe_ssm_buffer_full() -> None:
    """扫 SSM agent 日志尾部找 buffer-full 标记,结果缓存进 _agent_metrics。

    只读日志尾部(最后 64KB):日志会长到几十 MB,整读会在每个 poll 周期烧掉 IO。
    读不到文件 / 读失败一律记 False 而不是 None —— 这是预警信号,不是门,不该因为
    日志轮转就把 host 判病。
    """
    hit = False
    try:
        with open(_SSM_AGENT_LOG, "rb") as fh:
            try:
                fh.seek(-65536, os.SEEK_END)
            except OSError:
                fh.seek(0)  # 文件比 64KB 小
            hit = _SSM_BUFFER_FULL_MARK in fh.read().decode("utf-8", "replace")
    except Exception:
        hit = False
    with _lock:
        _agent_metrics["ssm_buffer_full"] = hit


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
        # 为什么需要它:账本 used_mem_mb 只证明"声明内存没超卖";租户真实占用可能超出
        # 声明(balloon 回收是 best-effort)。调度器据此在物理内存跌破水位时停止向该
        # host 派新租户。带 ts 让调度侧能判新鲜度、陈旧即 fail-open —— 与既有的
        # avail_disk_mb / disk_check_ts_epoch 同一范式。
        # total==0 说明 /proc/meminfo 读失败,此时【不写】任何字段:让调度侧走"无信号
        # → fail-open",而不是写个 0 进去把整台 host 永久判成内存耗尽。
        _mem_total, _mem_avail = _get_host_mem_info()
        if _mem_total > 0:
            expression += (
                ", mem_total_mb = :mt, mem_avail_mb = :ma, mem_check_ts_epoch = :mts"
            )
            values.update(
                {":mt": _mem_total, ":ma": _mem_avail, ":mts": int(time.time())}
            )
        # 重启过。恒写是刻意的 —— 条件写会让"从没上报过"和"上报过但没变"混在一起,而前者
        # 恰恰是刚重启完那一轮。
        expression += ", agent_started_at = :ast"
        values[":ast"] = _AGENT_STARTED_AT
        # ssm_buffer_full 只进 /metrics,控制面两个都看不见,也就没法做跨 host 汇总。
        with _lock:
            _buf_full = _agent_metrics.get("ssm_buffer_full")
            _drift = dict(_agent_metrics.get("route_drift") or {})
        if _buf_full is not None:
            expression += ", ssm_buffer_full = :sbf"
            values[":sbf"] = bool(_buf_full)
        if _drift:
            # 在别台。单机视角只看得见这一半,控制面汇总各 host 的这一半才拼出全貌。
            expression += (
                ", drift_ghost_descriptor = :dgd, drift_foreign_vm = :dfv, "
                "drift_check_ts_epoch = :dts"
            )
            values.update(
                {
                    ":dgd": int(_drift.get("ghost_descriptor") or 0),
                    ":dfv": int(_drift.get("foreign_vm") or 0),
                    ":dts": int(time.time()),
                }
            )
        table.update_item(
            Key={"instance_id": INSTANCE_ID},
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
        )
        _note_ddb_ok()
    except Exception as e:
        # Heartbeat failures must never crash the poll loop.
        _handle_ddb_exc(e, "host heartbeat failed")


def _refresh_health(table, tid, info, now, metrics, host_port=None):
    """Health-refresh write for a tenant NOT promoted this tick (already
    running, still creating with the gateway not up yet, or down).

    `host_port` (#526): pass the value `_ensure_route` just returned so the
    record's advertised port is reconciled to the live bitmap/DNAT truth — same
    reasoning as guest_ip below. Pass None from the call site that never ran
    `_ensure_route` (health-only refresh for a down VM): writing an empty value
    would wipe the previous truth.

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
    # ADR-rebuild-idempotency-sync-contract §5.4a 路 2 —— 上报本机 vm.json 里记的
    # 【实际运行版本】,供控制面与 rebuild_target_snapshot_time(期望值)异步对账。
    # 这样一次回执丢失的 rebuild 不再只能停在 unconfirmed:期望 == 实际即可确认成功,
    # 完全不依赖那次 SSM 调用的成败。
    #
    # 只在有值时写:空值不覆盖已有字段。vm.json 缺该字段的情况真实存在(legacy 扁平布局
    # 下 launch-vm 不写、老租户的 vm.json 是加该字段之前生成的),写空串会把上一次的真值
    # 抹成空 → 对账反而失去依据。
    #
    # **只能用于确认成功,不能用于判定失败**(ADR §5.4a 约束 2):launch-vm 写它是
    # best-effort(jq 失败即跳过、注释明写"纯审计信息"),且它记的是"启动时打算用哪个
    # 版本"而不是"当前进程真的挂着哪个 rootfs"。宽松方向(有证据就认成功)安全;严格
    # 方向(没证据就判失败)会误杀。故控制面侧的对账逻辑必须只做 == 目标 → done,
    # 绝不因该字段缺失或不等而标 failed。
    _observed = (info.get("observed_image_snapshot_time") or "").strip()
    if _observed:
        expr += ", observed_image_snapshot_time = :ois"
        vals[":ois"] = _observed
        # 连 FC 进程启动时刻一起上报(见 _probe_all 里的理由):控制面要靠它区分"这是本次
        # rebuild 新起的进程"与"旧 FC 没死、只是 vm.json 被改了"。空串照样写:让控制面能
        # 分清"没有启动证据"(不得单凭版本判 done)与"上一轮的旧证据"(会误判)。两者必须
        # 同进同退,否则会出现新版本配旧启动时刻的错配组合。
        expr += ", observed_boot_at = :oba"
        vals[":oba"] = info.get("observed_boot_at") or ""
    # 病史:restore 由控制面用 legacy 公式 VM_PORT_BASE + vm_num - 1 算 host_port 并直接
    # 翻 running,而那个端口族【从未落到 iptables】(ssm_dispatch 的 "never consumed by
    # Edge" 注释,同段还明确 "Keep host_port in the record for the live bitmap route")。
    # 真值只在本机 bitmap 分配器手里(route_ops.ensure_port_and_dnat)。create 路径靠
    # promote 覆盖,restore 收尾直接翻 running → promote 的 `#s = :c` 闸门再也命中不了,
    # DDB 永久停在 legacy 值:edge 拿它转发到一个本机不存在的端口 → 客户不可达,而
    # vm_health/app_health 全绿(真机实测 DDB 21643 / Redis+iptables 10785,25 个在役租户)。
    #
    # 为什么修在这里而不是 restore 里:restore 是同步 API,受 API GW 集成超时 29s 硬限
    # (实测该路径 Lambda Duration p90 18.7s / p99 44.4s 已贴墙),再插两次 SSM 往返
    # (各 ~3.7s)会把更多调用推过线 —— 修在控制面反而制造 504。而本函数每 tick 都跑、
    # host_port 已在上游 _ensure_route 算好,顺带写回零额外成本,约一个 tick 内自动收敛,
    # 且存量错值一并自愈。
    #
    # 只在有值时写(同下方 observed_image_snapshot_time 的取舍):调用方在"未跑
    # _ensure_route"的分支传 None,写空会把上一次的真值抹掉。
    #
    # 两道守卫已够,不需新增(见本函数 docstring):
    #   · attribute_exists(id) — 不复活已删租户
    #   · host_id = :self      — 迁移 drain 窗口里源 host 上残留的 VM 不能覆写
    #     migration 刚 repoint 到 target 的记录(migration commit 在同一条 SET 里把
    #     host_id 切到 target,故源 host 的 :self 不匹配 → 干净 CCF)
    if host_port is not None:
        expr += ", host_port = :hp"
        vals[":hp"] = int(host_port)
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
                # definitionally runs this VM (it read the local vm.json), so
                # this self-heals a record that reached us with host_id
                # unset/stale (e.g. a dispatch backfill that failed under
                # throttle) — otherwise a host_id-less `running` tenant makes
                # delete skip stop-vm/DNAT/counter-release. Gated on `#s = :c`
                # (creating), so a migrating tenant — never `creating` — is
                # untouched (no cross-host clobber). We do NOT write vm_num here:
                # this is the fresh creating→running promote, whose guest_ip is
                # already correct; the logical(DDB)↔physical(vm.json) vm_num split
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
                # 重投【落回同一 host】拿【新】预留(新 vm_num N2,vm_num 单调不复用),此时本机
                # 若残留【旧】vm.json(旧 vm_num N1)会把 N2 的租户按 N1 promote → DDB 放置与实跑
                # VM 的 vm_num 分叉。加 vm_num=:phys 闸:只 promote 【DDB vm_num == 本机 vm.json
                # vm_num】的租户,旧 vm.json 的 N1≠N2 → 条件失败跳过(等旧 VM 被 orphan-reap 清)。
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
                # 释放清了 host_id/token/容量但留 status=creating,本 promote 若只判 #s=:c 会把
                # 已释放的租户"复活"成 running,而容量已扣 → 未记账的 running VM(超卖)。fence 加
                # host_id=:self:promote 的租户来自本机 vm.json(reserve 时 host_id 已原子写成本机),
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
                    # 【绝不复活】。回落 guarded refresh(attribute_exists(id) + host_id);
                    # 已释放租户 host_id 没了 → refresh 的 host_id 守卫也 CCF → 干净 no-op。
                    #
                    # #526 —— 这条分支正是【已 running 的租户每 tick 走的路】,而 host_port
                    # 已由上方 _ensure_route 算出真值。restore 过的租户 DDB 里存的是控制面
                    # legacy 公式的产物(从未落到 iptables),在此顺带对账回真值:漂移一个 tick
                    # 内自动收敛,存量错值也一并自愈。promote 那条 `#s = :c` 闸门永远命中不到
                    # 它们,所以必须在这条回落路径上修。
                    _refresh_health(table, tid, info, now, metrics, host_port=host_port)
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


def _read_proc_start_ticks(pid):
    """/proc/<pid>/stat 的 field 22(starttime,单位 clock ticks since boot)。None 表示读不到。

    用途(ADR-rebuild-idempotency-sync-contract §5.4a 路 2 的证据强度):单看 vm.json 里的
    版本不足以证明"这台 VM 现在真的跑着该版本"—— launch-vm.sh 在起 firecracker 之前 800+ 行
    就写了那个字段,中途失败会留下「版本==目标但 VM 没起」;更隐蔽的是旧 FC 没被 stop-vm 杀掉
    的情况:VM ping 得通、vm.json 已被改成新版本,但跑的还是【旧】rootfs。两种都会让只看
    版本的对账判出假成功。

    FC 进程的启动时刻能把这两种情形排掉:rebuild 必然 kill 旧 FC 再起新 FC,所以只有
    「FC 的启动时刻晚于本次 rebuild 的发起时刻」才说明这是本次 rebuild 起来的那个进程。
    控制面据此把「版本相符」升格为「版本相符 且 进程是本次新起的」。

    与 _read_proc_stat_cpu_jiffies 同款解析:comm 字段(#2)可能含空格和括号,故从最后一个
    `)` 之后再切分。切分后 field 22 落在 index 19。纯函数,测试用 tmpfs fixture 即可驱动。
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
    try:
        return int(fields[19])
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



# 搁浅 = 本维还有余量,但【另一维】已装不下最小规格租户,导致本维余量永远用不出去。
# 为什么要这个指标:统一 1:4 下 M 系(4 GB/vCPU,只有 1c2G 需求 2 GB/vCPU 的一半)
# 必然 CPU 搁浅,这是已接受的代价 —— 但必须【可观测】,否则扩容决策会误判"还有 CPU"。
# 口径按【资源量】不按个数:VM 个数随规格组合浮动,资源占用才是确定的。
_STRAND_CPU_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", "1.0") or "1.0")
_STRAND_MEM_RATIO = float(os.environ.get("MEM_OVERCOMMIT_RATIO", "1.0") or "1.0")
_STRAND_MIN_VCPU = int(os.environ.get("VM_DEFAULT_VCPU", "1") or "1")
_STRAND_MIN_MEM_MB = int(os.environ.get("VM_DEFAULT_MEM", "2048") or "2048")


def _overcommit_for_family(instance_type):
    """per-family 超卖比(与控制面 core.host_profile.ratios 同口径)。

    OVERCOMMIT_BY_FAMILY 是 JSON:{"m8g":{"cpu":4.0,"mem":1.025},...}。缺项/非法
    回落全局 CPU/MEM_OVERCOMMIT_RATIO —— 与控制面 fail-safe 一致,不因一个畸形
    环境变量让指标算错。"""
    raw = os.environ.get("OVERCOMMIT_BY_FAMILY", "")
    cpu_r, mem_r = _STRAND_CPU_RATIO, _STRAND_MEM_RATIO
    if not raw or not instance_type:
        return cpu_r, mem_r
    try:
        table = json.loads(raw)
        entry = table.get(str(instance_type).split(".")[0]) or {}
        if isinstance(entry, dict):
            cpu_r = float(entry.get("cpu", cpu_r))
            mem_r = float(entry.get("mem", mem_r))
    except (ValueError, TypeError):
        pass
    return cpu_r, mem_r


def _collect_stranding_stats():
    """(stranded_vcpu, stranded_mem_mb, alloc_vcpu, alloc_mem_mb) 或 None。

    读本机 host 行(强一致不必要:指标容忍一个 poll 周期的滞后)。任何缺字段/读失败
    → 返 None,让 /metrics 整组省略该指标 —— 与 port_stats 同款"绝不吐假 0"。
    """
    if not HOSTS_TABLE or not INSTANCE_ID:
        return None
    try:
        item = (
            _get_ddb().Table(HOSTS_TABLE).get_item(Key={"instance_id": INSTANCE_ID})
        ).get("Item")
        if not item:
            return None
        tv, tm = int(item["total_vcpu"]), int(item["total_mem_mb"])
        uv, um = int(item.get("used_vcpu", 0)), int(item.get("used_mem_mb", 0))
        cpu_r, mem_r = _overcommit_for_family(item.get("instance_type"))
        alloc_v, alloc_m = int(tv * cpu_r), int(tm * mem_r)
        free_v, free_m = max(0, alloc_v - uv), max(0, alloc_m - um)
        # 与控制面 core.capacity.stranded() 逐字同款判据(两处必须同口径,否则
        # 指标说的搁浅与调度器判定的搁浅不是一回事)。
        s_vcpu = free_v if free_m < _STRAND_MIN_MEM_MB else 0
        s_mem = free_m if free_v < _STRAND_MIN_VCPU else 0
        return s_vcpu, s_mem, alloc_v, alloc_m
    except Exception as e:  # noqa: BLE001 — 指标采集失败绝不影响 /metrics 可用性
        print(f"stranding stats collection failed (omitted): {e}")
        return None

# Orphan-firecracker overwatcher (Firecracker prod-host-setup.md:69-83 recommends
# a host process that reaps unresponsive/leaked firecrackers). Our normal recovery
# only iterates vm.json dirs, so a firecracker whose vm.json was already removed
# (tenant deleted, but DELETE raced the kill / SIGKILL chaser missed) becomes an
# ORPHAN that no probe ever revisits — it silently holds ~600MB-1GB RSS forever.
# This sweep finds firecrackers whose socket dir has no vm.json and SIGKILLs them.
_ORPHAN_GRACE_SEC = int(os.environ.get("OC_ORPHAN_GRACE_SEC", "120"))

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
#
# 撤回理由(Codex 独立复审 CHANGES_NEEDED,判定成立):tombstone 上**没有记它是为哪一次
# delete 铸的**,所以一张过期的旧票会被当成对【当前】这次删除的授权。失败链:
#   ① 一次 keep_data=false 的 delete 落了票,随后 rm 被中断【或整条 delete 回滚】——
#      而回滚路径**不清票**(delete-vm.sh 只在第 ④ 步 rm 成功后才 `rm -f` 票);
#   ② 租户继续存活(或被 rebuild),盘上那张票一直留着;
#   ③ 后来客户用 keep_data=true 软删它 —— 软删【同样经过 deleting】
#      (tenant_service.py:3144 的 deleting CAS 没有 keep_data 条件);
#   ④ 老化判据于是全部满足 → 删掉一份客户明确要求保留的数据盘。no-data-loss 违规。
# `delete-vm.sh:158` 的注释早就点出过这张残留票的危险(「租户后续若被软删重建…GC 会凭这张
# 陈旧票删掉本该保留的盘」);当时读到了却把它当成"接管有价值(销毁过期票)"的论据,没看出
# 接管本身也在**使用**那张过期票。
#
# 四种零 schema 的补救都试过,没有一个严密:
#   · 比对 `active_lifecycle_op_id` —— delete 回滚会释放围栏,基准随之消失;
#   · 比对 `created_at` 世代 —— 上述链里 created_at 没变,挡不住;
#   · 要求 `vm.json` 不存在 —— delete-vm.sh 第 ② 步**无条件**删 vm.json,只覆盖一半;
#   · 比对 tombstone mtime 与 VM_DIR mtime —— `rm -rf` 中断会改目录 mtime,判据反向。
# 结论:tombstone 不是 purge intent token 的等价物,它缺了 token 的核心性质 ——
# (双侧持久化 + 精确匹配),那要碰 delete 主路径与一个持久字段,属 issue 自己写明的
# 人工红线大改。故 DoD-b 留作未兑现,不在此处用一个不严密的判据充数。
#
# preserved`(断言「status=deleting must not be reclaimed」)。当时那个老化接管是加在
# `elif` 分支上,而那条既有测试用的是**新鲜票**,所以它没红 —— 于是被误读成"不冲突"。
# **既有护栏没红 ≠ 没违反它守的契约**;它只覆盖了新鲜票那一档。
#
# 双门此刻同样会误删软删盘 —— 软删走完也是 deleted。撤回只是不再扩大那个窗口。


def _gc_orphan_vm_dirs():
    """回收 delete 侧 rm -rf 漏删的 VM 目录(兜底 GC)。多门确认,绝不误删有效数据:

    判据:
      ① 存在平级 tombstone `.purge-<tid>` —— 控制面 delete(keep_data=false)在 rm -rf 前
         写的"该数据盘应销毁"持久信号,放【VM 目录外】故 rm -rf <tid> 中断也不丢(codex:
         标记在被删目录内会随半删消失)。keep_data=true 软删不写 tombstone → 保盘。
      ②' #339 目标② —— 抢到 per-tenant flock(`oc-launch-<tid>.lock`,与 launch-vm.sh /
         delete-vm.sh / backup-data.sh 同一把)。抢不到 = 有人正在动这个租户,跳过。
         必须在门② 之【前】:否则读与 rm 之间仍是裸窗口。
      ② 持锁后 DDB 强一致读该租户 status=deleted —— 确认删除【已完成】。deleting(备份/
         停机前、可回滚 running)、记录不存在(同 id 重建的最终一致空窗口,误删新盘)一律
         【不删】(codex 复审:只认明确 deleted,强一致读避开重建竞态)。
         `deleting` 的老化接管试过又撤回了,理由见上方 #339 那段长注释。
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
        #
        # 顺序是这条护栏的全部:此前门② 的读与 `rm -rf` 之间是裸窗口,同 id 重建的租户在
        # 当时只在控制面侧堵了入口 —— tenant_service.py:5157 拒绝 deleted/deleting 的
        # mutating action。那道门管不到 host 侧的 launch 重投)。加锁后:
        #   · launch-vm.sh:403 / delete-vm.sh:69 / backup-data.sh 与这里争同一把 inode
        #     advisory 锁 → 抢到即证明此刻没有任何一方在动这个租户的盘;
        #   · 抢到锁之后才读的 status,到 rm 为止不可能再被 host 侧改。
        # `wait_sec=0` 纯非阻塞:GC 是 60s 一轮的巡检线程,一点也不需要等 —— 抢不到就是
        # 就是为了不阻塞 host heartbeat)。
        _gc_lock_fd = _acquire_tenant_lock(tid, wait_sec=0, who="disk-gc")
        if _gc_lock_fd is None:
            continue
        try:
            # 门②:强一致读。异常/记录不存在 → 跳过(不删)。
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
            # 只认明确 deleted。`deleting`(可回滚)、记录不存在(同 id 重建的最终一致空
            if not item or item.get("status") != "deleted":
                continue
            try:
                subprocess.run(["rm", "-rf", vm_path], check=True, timeout=30)
                os.remove(tomb)  # 删净后清 tombstone(下轮不再扫);删失败留着下轮重试
                reclaimed += 1
            except Exception as e:
                print(f"disk-gc: rm -rf {vm_path} failed: {e}")
        finally:
            # 关 fd 即释放 flock。必须在 finally:上面任一 continue/异常若漏掉这一步,
            # 这把锁会被 GC 线程一直持着,把该租户的 launch/delete/backup 全部锁死。
            os.close(_gc_lock_fd)
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


def _backup_now_iso():
    """UTC ISO8601,与中心 Lambda 写 last_backup_at 的格式一致(backup/handler.py:176
    `datetime.now(timezone.utc).isoformat()`)。两侧必须同格式 —— 到期判断要能解析对方
    写的值,否则切换期间会被当成"解析不了"而每轮重备。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


#
# backup-data.sh 的 EXIT trap 在有界 resume 全部用尽时打这个串。它是【机器判据】,
# 与 backup Lambda(deploy/lambda/backup/handler.py)认的是同一个串 —— 三跳都靠它对齐,
# 没有共享常量,所以有一道 parity 测试盯着(tests/test_469_r7...::TestVmLeftPausedSentinel)。
_BACKUP_VM_LEFT_PAUSED_SENTINEL = "OC_BACKUP_VM_LEFT_PAUSED"


def _backup_left_vm_paused(out, err):
    """脚本输出里是否出现「VM 被留在 Paused」哨兵。两个流都看 —— log() 写 stdout,
    但 SIGTERM 打断时缓冲的去向不确定,只看一个流会漏。"""
    return _BACKUP_VM_LEFT_PAUSED_SENTINEL in ((out or "") + (err or ""))


def _note_vm_left_paused(tid, why):
    """fail-loud + 置一个【持久指标】。

    为什么必须有指标而不只是日志:租户行仍然是 running,而健康检查分不出 Paused 与
    Running(一个 Paused 的 Firecracker 进程照样活着)。**没有这个指标,一个冻住的客户 VM
    在系统里就是完全不可见的** —— 这正是 #469 零节 S1/S2 说的那类失效。
    备份循环自己的心跳照常健康,所以它也救不了。

    与 backup_script_stale 同款:置在 _agent_metrics 里,由 _render_metrics_text 渲染成
    openclaw_backup_vm_left_paused 供抓取与告警。不在这里尝试自己 resume —— 那需要
    per-tenant 锁(此刻脚本刚被杀,锁的归属不确定),盲发 Resume 会和下一轮备份或一次
    迁移交错。信号交给运维/下一轮扫描,动作留给持锁的一方。
    """
    print(
        f"backup: {tid} FATAL — the VM was NOT confirmed resumed ({why}). "
        "The tenant row still says running while the guest is frozen; health checks "
        "cannot tell the difference. Resume it on the host or force a lifecycle op. "
        f"(metric: openclaw_backup_vm_left_paused)"
    )
    _agent_metrics["backup_vm_left_paused"] = 1


def _cpu_load_ratio():
    """1 分钟 loadavg / 核数。读不到返 None(判定侧当"不确定",按放行处理)。

    为什么用 1 分钟而不是 5/15 分钟:要的是"此刻用户在不在用 CPU",15 分钟均值会让
    刚结束的高负载继续压着备份、也会让刚开始的高负载被稀释掉。
    为什么除以核数:loadavg 是绝对可运行队列长度,96 核机器 load=8 其实很闲。
    """
    try:
        with open("/proc/loadavg") as fh:
            one_min = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    cores = os.cpu_count() or 1
    return one_min / cores


def _backup_due(last_backup_at, now_epoch, interval_hours):
    """纯函数:该租户是否到期该备份。

    从未备份过 → 立即到期。解析不了时间戳 → 也当到期(宁可多备一次,不漏备:
    漏备会让 RPO 静默失效,多备只是多花一次秒级操作)。
    """
    if not last_backup_at:
        return True
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(str(last_backup_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    return (now_epoch - dt.timestamp()) >= interval_hours * 3600


# backup-data.sh 的路径与新鲜度哨兵。
#
# 哨兵与 **backup Lambda 的自愈判据同源**(deploy/lambda/backup/handler.py:112-118 的
# 双 grep:oc_flush_guest + OC_BACKUP_SOURCE_ABSENT),再加 R7 自己的 _RUN_ID:
#   · oc_flush_guest          —— 缺它 = 旧版无 guest flush = 备份丢未落盘的客户数据
#   · OC_BACKUP_SOURCE_ABSENT —— 机器可判的「源盘不在」哨兵
#   · _RUN_ID                 —— R7 为「同秒两次备份不撞 key」加的,同时蕴含 per-tenant
#                                flock 与读 BACKUP_PREFIX 那批改动
# 两处判据必须同源:各写一套会漂移 —— 那个 Lambda 的注释里记着同类教训(用
# OC_BACKUP_SOURCE_ABSENT 单独当判据会让自愈分支永远跳过,新版永不被拉取,
# 「线上修复静默不生效」反复踩)。
# 判据形态承控制面 ssm_dispatch.host_script_self_heal 的 freshness:「存在」不够,
# 还要「认得新语义」。
_BACKUP_SCRIPT = "/home/ubuntu/backup-data.sh"
_BACKUP_SCRIPT_SENTINELS = ("oc_flush_guest", "OC_BACKUP_SOURCE_ABSENT", "_RUN_ID")


def _backup_script_is_current():
    """backup-data.sh 在位【且】认得 R7 新语义吗?拿不准一律返 False(fail-closed)。

    为什么必须查(codex 独立复审第二轮):`init-host.sh` 只在【开机时】从
    `s3://$ASSETS_BUCKET/deployment/scripts/` 装这些脚本 —— 既有机器不会重跑它。而 R7
    把 host 变成定时备份的唯一执行者、中心调度一关,就【没有任何路径】能自愈这个脚本。
    旧脚本缺 per-tenant flock、缺 guest flush、不读 BACKUP_PREFIX、key 不带 run id,
    却照样 `exit 0` → 上游把 last_backup_at 往前推,于是持续产出「损坏或找不到」的恢复点。
    本分支的证据里就实测到过:真机只换了 host-agent.py,那台 host 上的 backup-data.sh
    仍是旧版(lock=0)。

    只读不改:发现过期就拒绝备份并 fail-loud,【不】在这里自动从 S3 装载 —— 装载会写
    /home/ubuntu 下的可执行文件,那是 init-host.sh 与部署流程的职责边界;agent 自己去
    改它等于两个写手抢同一个文件(本轮验证第一次就因误动该路径而回滚过)。
    """
    if not os.path.isfile(_BACKUP_SCRIPT) or not os.access(_BACKUP_SCRIPT, os.X_OK):
        print(f"backup: SKIP whole round — {_BACKUP_SCRIPT} 缺失或不可执行")
        return False
    try:
        with open(_BACKUP_SCRIPT, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    except OSError as e:
        print(f"backup: SKIP whole round — 读不出 {_BACKUP_SCRIPT}: {e}")
        return False
    missing = [s for s in _BACKUP_SCRIPT_SENTINELS if s not in body]
    if missing:
        print(
            f"backup: SKIP whole round — {_BACKUP_SCRIPT} 是旧版(缺 {', '.join(missing)});"
            "它没有 per-tenant flock / guest flush / BACKUP_PREFIX / 唯一 key,跑它会产出"
            "损坏或找不到的恢复点而 last_backup_at 照样推进。需随 setup.sh 把 "
            "deployment/scripts/ 推到本机后才恢复备份。"
        )
        return False
    return True


# per-tenant 生命周期锁的路径,与 backup-data.sh:89 / launch/stop/delete/migrate 同一把
# inode advisory 锁。不新开一把:备份要与那些动作互斥,不只与另一次备份互斥。
_LIFECYCLE_LOCK_DIR = "/run/lock"
# -w 30 的同款预算:备份可重投(下轮再来),短暂让位给正在跑的 launch/restore 是对的。
_BACKUP_LOCK_WAIT_SEC = int(os.environ.get("OC_BACKUP_LOCK_WAIT_SEC", "30"))


def _acquire_tenant_lock(tid, wait_sec=None, who="backup"):
    """取 per-tenant 生命周期锁,返回已持锁的 fd;取不到返 None。

    per-tenant flock」),而它打出 `backup: …锁被占用` 会误导排障。默认值保持 "backup",
    既有调用点行为逐字节不变。

    为什么锁要在 agent 侧取(codex 独立复审第二/三轮,连点两轮):
    此前所有权与 status 的检查在 sweep 里做,而 flock 在 backup-data.sh 内部才取 ——
    两者之间存在窗口。迁移若恰在此间完成,源 host 会用【旧盘】产出一个更新的 S3 对象:
    最终那次 `host_id = :self` 的 CAS 只挡住 last_backup_at 更新,**S3 对象已经落地
    且是最新的**,将来恢复选到它就是拿陈旧数据覆盖在役租户。
    修法只能是「先持锁、再持锁复读校验、然后把 fd 传给脚本」——检查与动作之间不留窗口。

    非阻塞轮询而不是 flock(LOCK_EX) 死等:agent 是单线程 tick 循环,死等会让整个
    host-agent(健康探测/路由维护/GC)停摆。轮询到超时就放手,下轮再来。
    """
    if wait_sec is None:
        wait_sec = _BACKUP_LOCK_WAIT_SEC
    path = os.path.join(_LIFECYCLE_LOCK_DIR, f"oc-launch-{tid}.lock")
    try:
        os.makedirs(_LIFECYCLE_LOCK_DIR, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        print(f"{who}: {tid} 打不开生命周期锁 {path}: {e}")
        return None
    deadline = time.time() + wait_sec
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() >= deadline:
                os.close(fd)
                print(
                    f"{who}: {tid} 生命周期锁被占用 >{wait_sec}s"
                    "(并发 launch/stop/delete/migrate)— 本轮跳过,下轮再来"
                )
                return None
            time.sleep(1)


def _tenant_still_ours(table, tid):
    """持锁期间强一致复读:仍是 running 且仍归本机?

    与 sweep 里那次读的区别是【ConsistentRead + 在锁内】—— sweep 那次是最终一致读、
    且读完就放手了。迁移 commit 会在同一条 SET 里切 host_id,持锁复读能看到真相。
    """
    try:
        item = table.get_item(Key={"id": tid}, ConsistentRead=True).get("Item")
    except Exception as e:  # noqa: BLE001 — 读不到就不备,宁可漏一轮
        print(f"backup: {tid} 持锁复读失败: {e}")
        return False
    if not item or item.get("status") != "running":
        print(f"backup: {tid} 持锁复读发现 status={item.get('status') if item else None} — 跳过")
        return False
    if not INSTANCE_ID or item.get("host_id") != INSTANCE_ID:
        print(
            f"backup: {tid} 持锁复读发现 host_id={item.get('host_id')} != {INSTANCE_ID}"
            " —— 迁移已在检查与取锁之间完成,拒绝上传陈旧盘"
        )
        return False
    return True


def _backup_one_tenant(tid, nice=False, table=None):
    """备份单个租户:直接调 host 本地的 backup-data.sh(单租户原子操作,契约不变)。

    成功才写 last_backup_at —— 它同时是"到期判断"的依据,失败不写则下轮自然重试,
    这就是重试机制本身,不需要另建状态。写失败(DDB 抖)也不算成功:宁可下轮重备,
    也不能让 last_backup_at 缺失却以为备过了。

    脚本自己 source /etc/platform.env 拿桶名与 CMK(见 backup-data.sh:16-18),故这里
    不传参 —— 不从 Python 侧拼桶名,避免两处配置漂移。
    """
    # ── 先持锁,再持锁复读校验,然后把 fd 传下去(codex 连点两轮的迁移窗口竞态)──────
    # 顺序不可换:检查与动作之间不留窗口。取不到锁就本轮跳过(可重投,下轮再来)。
    _lock_fd = _acquire_tenant_lock(tid)
    if _lock_fd is None:
        return False
    try:
        if table is not None and not _tenant_still_ours(table, tid):
            return False
        return _run_backup_script(tid, nice=nice, lock_fd=_lock_fd)
    finally:
        # 释放:close 会一并释放 flock(锁绑在打开文件描述上)。
        try:
            os.close(_lock_fd)
        except OSError:
            pass


def _run_backup_script(tid, nice=False, lock_fd=None):
    """真正起 backup-data.sh。lock_fd 非 None 时以 OC_LIFECYCLE_LOCK_FD 继承给它 ——
    脚本据此复用同一把锁而不是重新 open(backup-data.sh:90;flock 绑在打开文件描述上,
    新 open 出来的描述不受继承锁保护,会自己阻塞到超时)。
    """
    # 超时【不能】用 subprocess.run(timeout=) —— codex 独立复审抓出的真问题:
    # 它超时后发 SIGKILL,而 backup-data.sh 靠 `trap cleanup EXIT`(:39)在退出时把
    # 被 Pause 的客户 VM 重新 Resumed(:36)。SIGKILL 不触发 trap → 一次慢备份就把
    # 客户 VM 永久留在 Paused 状态,那是客户直接可见的故障,比备份失败严重得多。
    # 正确做法:先 SIGTERM 让 trap 跑完清理,等一小段,仍不退才 SIGKILL 兜底。
    # 用 start_new_session 起独立进程组,信号发给整组 —— 否则 pigz/openssl/aws 这些
    # 子进程收不到,shell 退出了它们还在写盘。
    try:
        # nice=True 时用 nice/ionice 降优先级(只在"已超硬上限、不能再让路"那轮)。
        # ionice -c3 = idle 级 IO:只在磁盘空闲时读写,不与客户的 IO 竞争。
        # 两个命令都可能不存在(最小化镜像),故做存在性回落 —— 降不了优先级也要备,
        # 丢备份比抢一点 CPU 严重。
        _cmd = ["/home/ubuntu/backup-data.sh", tid]
        if nice:
            if shutil.which("ionice"):
                _cmd = ["ionice", "-c3"] + _cmd
            if shutil.which("nice"):
                _cmd = ["nice", "-n", _BACKUP_NICE] + _cmd
        # 把持有的锁 fd 继承给脚本:pass_fds 关掉该 fd 的 close-on-exec,
        # OC_LIFECYCLE_LOCK_FD 告诉脚本复用哪个号(backup-data.sh:90 的既有契约,
        # delete-vm.sh 也是这么把锁传给 stop-vm.sh 的)。
        _env = dict(os.environ)
        _pass = ()
        if lock_fd is not None:
            _env["OC_LIFECYCLE_LOCK_FD"] = str(lock_fd)
            _pass = (lock_fd,)
        proc = subprocess.Popen(
            _cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=_env,
            pass_fds=_pass,
        )
    except Exception as e:  # noqa: BLE001 — 一个租户失败绝不能让整个循环崩
        print(f"backup: {tid} failed to spawn: {e}")
        return False
    try:
        out, err = proc.communicate(timeout=_BACKUP_PER_TENANT_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(
            f"backup: {tid} timed out after {_BACKUP_PER_TENANT_TIMEOUT}s; "
            "sending SIGTERM so the script's EXIT trap can resume the VM"
        )
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as e:
            print(f"backup: {tid} SIGTERM failed: {e}")
        try:
            # 给 trap 留时间。**注意这里不能再写"cleanup 只做 rm + 一次 curl,秒级够了"**
            # —— 那句话在 backup-data.sh 的 resume 改成有界重试之后就不成立了,
            # _BACKUP_TERM_GRACE_SEC 的定义处(㉕)已按最坏 35s 抬到 60s 并加了 parity 测试。
            out, err = proc.communicate(timeout=_BACKUP_TERM_GRACE_SEC)
            # ㉙ 【不能无条件宣布"VM resumed"】(codex 独立复审第二十四轮)。
            #
            # 这里原来无条件打 "(cleanup ran, VM resumed)"。但 cleanup 的 resume 是【有界
            # 重试】,用尽仍可能失败 —— 那时它打哨兵 OC_BACKUP_VM_LEFT_PAUSED,而这行日志
            # 却在宣布成功。租户行仍是 running,健康检查也分不出来(一个 Paused 的
            # Firecracker 进程照样活着),于是这是一次【静默的客户中断】。
            #
            # 这是同一条判断的第五个面:前四面在 Lambda 那条路(rc==89 / rm 失败 /
            # stop 失败 / 备份失败留 Paused),这一面在 R7 自驱路 —— **我上一轮加了哨兵,
            # 却只把它接进了 backup Lambda,没接进 host-agent。只建了桥墩没铺桥面。**
            _left_paused = _backup_left_vm_paused(out, err)
            if _left_paused:
                _note_vm_left_paused(tid, "SIGTERM cleanup exhausted its resume retries")
            else:
                print(f"backup: {tid} exited after SIGTERM (cleanup ran, VM resumed)")
        except subprocess.TimeoutExpired:
            print(f"backup: {tid} ignored SIGTERM, escalating to SIGKILL")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            out, err = proc.communicate()
            # SIGKILL 之后 EXIT trap 【没能跑完】,所以 VM 大概率还停着。这里不装作知道
            # 确切状态,而是按"未确认已恢复"处理 —— 与上面那支同一个信号,让它可被告警。
            # 原来这里只在日志里说一句 "VM may stay paused" 就 return 了:那等于承认有个
            # 冻住的客户 VM,然后什么都不做。
            _note_vm_left_paused(tid, "SIGKILL after SIGTERM was ignored; the EXIT trap "
                                      "never finished, so the VM was not confirmed resumed")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"backup: {tid} wait failed: {e}")
        return False
    if proc.returncode != 0:
        tail = (err or out or "").strip()[-200:]
        print(f"backup: {tid} rc={proc.returncode} {tail!r}")
        # ㉙ 正常退出但非零 —— 同样要看哨兵。脚本在 resume 用尽重试时【就是】非零退出
        #    (backup-data.sh 的主路径 `if ! oc_resume_vm; then ... exit 1`),所以这条路
        #    恰恰是"VM 被留在 Paused"最常见的到达方式,不能只打一行 rc= 就算完。
        if _backup_left_vm_paused(out, err):
            _note_vm_left_paused(tid, f"script exited rc={proc.returncode} with the VM "
                                      "still paused")
        return False
    try:
        _get_backup_ddb().Table(TENANTS_TABLE).update_item(
            Key={"id": tid},
            UpdateExpression="SET last_backup_at = :t",
            ExpressionAttributeValues={":t": _backup_now_iso(), ":self": INSTANCE_ID},
            # 三重条件,每一条都对应一个真实的坏结果:
            #  · attribute_exists(id) —— 租户可能在这次备份期间被删掉,无条件写会
            #    upsert 出一个只有 id + last_backup_at 的僵尸行;
            #  · host_id = :self —— 备份【开始时】归本机不代表【结束时】还归本机
            #    (迁移就发生在这几十秒内)。若已迁走,这次备的是旧盘,更不该把
            #    last_backup_at 刷新 —— 那会让新 owner 机以为刚备过而跳过它;
            #  · 条件不满足时抛 CCF → 本函数返 False → 不计入 done、下轮重来。
            ConditionExpression="attribute_exists(id) AND host_id = :self",
        )
    except Exception as e:  # noqa: BLE001
        print(f"backup: {tid} done but last_backup_at write failed: {e}")
        return False
    return True


def _backup_sweep(now_epoch=None):
    """扫本机租户,备到期的那些。返回 (backed_up, skipped_load, due_total)。

    与中心 Lambda 的关键差别:租户来源是 os.listdir(VM_DIR) —— 本机盘上真实存在的,
    不是全表 scan 再筛 host_id。所以天然只管自己这台,并发按机器打散,且不受 DDB
    scan 规模影响(10 万租户全表 scan 是中心方案追不上的原因之一)。
    """
    if now_epoch is None:
        now_epoch = time.time()
    if not TENANTS_TABLE:
        return 0, 0, 0
    # 脚本过期就整轮不备(codex 复审第二轮)。放在扫租户【之前】:判据与租户无关,
    # 早退能省掉整轮 DDB 读;更重要的是这条是 fail-closed —— 宁可不备并 fail-loud,
    # 也不能让旧脚本产出损坏/找不到的恢复点却把 last_backup_at 往前推。
    if not _backup_script_is_current():
        # ⑨ codex 独立复审第六轮 —— fail-closed 必须【可见】,否则就是静默停摆。
        #
        # 上面那句"宁可不备并 fail-loud"此前是【假的】:这里只 `return 0, 0, 0`,
        # 一行日志都没有;而调用方 _backup_loop 随后照常 _agent_loop_tick("backup")
        # 打健康心跳。叠上 config.yml.example 默认关掉中心调度(上线顺序要求置 false),
        # 一台脚本没换的既有 host 就是:**两侧都不备份,而所有信号都是绿的**。
        # 这不是"安全的默认",这是把 no-data-loss 的失效藏起来。
        #
        # 所以:① 打一条 grep 得到的 fail-loud 日志;② 记状态并从 /metrics 暴露
        # openclaw_backup_script_stale,让它可被告警。不自装脚本 —— 自装等于让 host
        # 自己决定拉哪个版本,而脚本投递归 setup.sh/init-host.sh(那条缺口本身由
        # tests/test_script_manifest.py 在红,已记入 UNRESOLVED_GAPS)。
        _agent_metrics["backup_script_stale"] = 1
        print(
            "backup: REFUSING to run — /home/ubuntu/backup-data.sh is missing or "
            "stale (does not contain all required sentinels). NO local backups are "
            "happening on this host. If the central backup schedule is also disabled "
            "(s3.backup_central_schedule_enabled=false), this host has NO backup "
            "mechanism at all. Fix: push the current backup-data.sh to "
            "s3://$ASSETS_BUCKET/deployment/scripts/ and install it on the host."
        )
        return 0, 0, 0
    _agent_metrics["backup_script_stale"] = 0
    try:
        tids = sorted(os.listdir(VM_DIR))
    except FileNotFoundError:
        return 0, 0, 0
    table = _get_backup_ddb().Table(TENANTS_TABLE)
    # 自适应软到期(codex 第十二轮):用 len(tids) —— 本机租户数,是"这一轮最多要排空多少个"
    # 的上界,且在读 DDB 【之前】就已知。拿它算提前量,保证整条队列能在 interval 内备完。
    _soft_h = _backup_soft_interval_hours(len(tids))
    due = []
    for tid in tids:
        if tid.startswith("."):  # tombstone(.purge-<tid>)等非租户目录
            continue
        try:
            item = table.get_item(Key={"id": tid}).get("Item")
        except Exception as e:  # noqa: BLE001 — 查不到就跳过,下轮再来
            print(f"backup: read {tid} failed: {e}")
            continue
        # 只备在役租户。停机/删除中的盘可能不一致,中心 Lambda 同款守卫
        # (backup/handler.py:35 只备 running);删前备份走 Lambda 的 pre_delete 入口。
        if not item or item.get("status") != "running":
            continue
        # **本机盘上有目录 ≠ 这个租户归本机**(codex 独立复审抓出的真问题)。
        # 租户迁走后源机的 VM 目录可能仍在(disk-gc 只清有 tombstone 的),此时两台机器
        # 的 listdir 都能看到它。若源机照备,它会用【旧盘】产出一个更新的 S3 对象、
        # 并把 last_backup_at 刷成现在 —— 恢复时选到它就是拿陈旧数据覆盖在役租户,
        # 而且真正的 owner 机反而因为"刚备过"被判未到期而跳过。
        # 判据用本仓既有形态:host_id = :self(同 :1131 的 promote 闸)。
        # INSTANCE_ID 为空(env 未注入)时【不备】——宁可不备也不误备别人的盘。
        if not INSTANCE_ID or item.get("host_id") != INSTANCE_ID:
            continue
        # ⑧ codex 独立复审第六轮 —— 让路窗口必须从间隔里【切出来】,不能加在后面。
        #
        # 此前:到期阈值 = interval(24h),而 _forced 要 overdue_h > MAX_DEFER(6h),
        # 即负载持续偏高时备份年龄最坏到 **30h** —— 而两处配置都明写「每租户至少每 24h
        # 备一次」(config.yml.example:189 / samples/config-sg-prod.yaml:288)。那是承诺,
        # 不是节拍,30h 就是静默违反 RPO。
        #
        # 改成:软到期 = interval - MAX_DEFER(默认 18h)开始【尝试】,硬期限 = interval
        # (24h)必须跑。这样"给客户让 CPU"的预算是从 24h 里切出来的,而不是叠在它之上;
        # 每个 tick 都有机会撞上一个不忙的瞬间,撞不上就在 24h 那一刻降 nice 插队。
        # 最坏年龄 = interval,与承诺一致。
        #
        # 代价诚实标注:空闲 host 上的实际节拍因此变成 interval - MAX_DEFER(默认 18h
        # 而非 24h),备份次数与 S3 写入约 +33%。要恢复"正好 24h、不让路",把
        # OC_BACKUP_MAX_DEFER_HOURS 设为 0 —— 那时软硬期限重合,行为退回单一阈值。
        # 这个取舍的方向由 no-data-loss 定:多备一次只是花钱,晚备一次是丢数据窗口。
        if _backup_due(item.get("last_backup_at"), now_epoch, _soft_h):
            due.append((item.get("last_backup_at") or "", tid))
    # 最久没备的先备:一轮跑不完时,下轮优先级自然正确(不会让同几个反复被备)。
    due.sort()

    # R7 的 CPU 让路,**带上界**(codex 独立复审第三轮)。
    # 顺序是有意的:先扫出 due(只 listdir + 每租户一次 get_item,毫秒级、无压缩无 IO,
    # 不构成"抢 CPU"),再判负载 —— 因为要判"最久没备的已经过期多久",不扫就不知道。
    # 真正吃 CPU 的是 pigz,它仍被下面的判据挡着。
    #
    # 无上界的让路 = 一台长期繁忙的 host 永远不备份,而中心调度关掉后没有第二个执行者
    # 兜底 → 24h 备份保证被静默违反。故超过硬上限就不再让路,改用 nice 降优先级跑:
    # 抢一点别人不要的时间片,比丢掉备份保证强得多。
    # 没有到期租户时用 -inf 而不是 0.0:0.0 现在的含义是"正好到硬期限",会让 _forced
    # 在空列表上成立(codex 第六轮改了 _forced 的判据后 0.0 不再是中性值)。
    overdue_h = float("-inf")
    if due:
        _oldest_ts = due[0][0]
        if not _oldest_ts:
            overdue_h = float("inf")  # 从未备份过 —— 按最紧急处理
        else:
            try:
                from datetime import datetime, timezone

                _dt = datetime.fromisoformat(str(_oldest_ts).replace("Z", "+00:00"))
                if _dt.tzinfo is None:
                    _dt = _dt.replace(tzinfo=timezone.utc)
                # 【可以为负】(codex 第六轮):现在 due 是按软阈值挑的,所以一个"软到期
                # 但还没到硬期限"的租户 overdue_h 是负数,绝对值就是它距硬期限还剩多久。
                # 此前这里 max(0.0, ...) 夹到 0,而 _forced 判的是 `> MAX_DEFER`,夹与不夹
                # 都不影响那个判据;现在 _forced 判 `>= 0`,再夹就会让每个软到期租户都被
                # 当成"已到硬期限"→ 让路功能整个失效。
                overdue_h = (
                    now_epoch - _dt.timestamp()
                ) / 3600.0 - _BACKUP_INTERVAL_HOURS
            except (ValueError, TypeError):
                overdue_h = float("inf")  # 解析不了按最紧急(同 _backup_due 的取向)

    load = _cpu_load_ratio()
    _busy = load is not None and load > _BACKUP_LOAD_CEILING
    # 硬期限就是配置的 interval(codex 第六轮)。overdue_h 是相对 interval 算的,所以
    # "已达硬期限" ⟺ overdue_h >= 0。此前这里要求 overdue_h > MAX_DEFER,等于把让路预算
    # 叠在 interval 之上 → 最坏 30h。现在预算在软到期那侧切出来了,这里只认硬期限。
    # ⑮ codex 独立复审第九轮 —— 硬期限必须【为排空留出时间】。
    #
    # 第六轮把窗口从 interval 之外挪进了 interval 之内(软到期 18h / 硬期限 24h),但
    # `_forced` 判的是"最老那个到 24h 了没"。而本轮是【串行】备的:一台 380 租户的 host
    # 排空一轮约 2.3h(单备份最坏 22s,ADR §2.4 真机)。于是最老那个在 24h 整点被插队时,
    # 队尾那个可能已经 23.9h,等它被备到时是 26h+ —— 对【它】而言 24h 承诺仍被违反。
    #
    # 修法:把预计排空时间从硬期限里减掉 —— 队列越长就越早开始插队,让整条队列在期限内
    # 备完。判据用 len(due) × 单租户实测值,而不是新引一个配置项:那个数已经有真机来源,
    # 再加一个旋钮只会多一处要维护的假设。
    # 排空提前量的上界 = 【本轮实际的】软到期窗口(codex 第十二轮修正)。
    #
    # 我第九轮把它夹在窗口的【一半】,理由是"夹满整个窗口时,刚软到期那一刻边界相等即
    # 成立 → 从软到期起一直插队,CPU 让路彻底失效"。那个理由**是错的取舍**:如果队列
    # 确实需要整个窗口才排得完,那么"从软到期起就一直跑"正是唯一能守住 interval 的做法 ——
    # 让路在那种密度下本来就没有余量可让。拿硬保证(RPO / no-data-loss)去换软保证
    # (CPU 礼貌),方向反了。
    #
    # 现在窗口本身随租户数自适应(见 _backup_soft_interval_hours),所以夹满窗口是自洽的:
    #   租户少 → 窗口 6h、排空 <6h → 仍有余量让路;
    #   租户多 → 窗口 = 排空所需 → 从软到期起持续跑,恰好在 interval 前备完。
    # 剩余风险(已记入 UNRESOLVED_GAPS):排空估算超过 interval/2 时窗口被下界夹住,
    # 队尾仍可能超期 —— 那是密度超出串行方案能力,解法是开本地并发或降 interval。
    _drain_h = min(
        len(due) * _BACKUP_PER_TENANT_ESTIMATE_SEC / 3600.0,
        _BACKUP_INTERVAL_HOURS - _soft_h,
    )
    _forced = _busy and overdue_h >= -_drain_h
    if _busy and not _forced:
        print(
            f"backup: host load {load:.2f} > {_BACKUP_LOAD_CEILING} "
            f"(1min loadavg / {os.cpu_count()} cores), yielding CPU to tenants "
            f"this round (oldest tenant is soft-due but still "
            f"{-overdue_h - _drain_h:.1f}h before the forcing point: "
            f"{_BACKUP_INTERVAL_HOURS}h deadline minus {_drain_h:.1f}h estimated "
            f"drain for {len(due)} due tenant(s))"
        )
        return 0, len(tids), len(due)
    if _forced:
        # 这条日志是运维的关键信号:host 长期繁忙到备份被迫降优先级插队。
        print(
            f"backup: oldest tenant reached the forcing point "
            f"({_BACKUP_INTERVAL_HOURS}h deadline minus {_drain_h:.1f}h estimated "
            f"drain for {len(due)} due tenant(s); overdue {overdue_h:.1f}h) "
            f"while load {load:.2f} > "
            f"{_BACKUP_LOAD_CEILING} — running at nice {_BACKUP_NICE} instead of "
            "deferring again (the backup guarantee outranks CPU politeness)"
        )
    done = 0
    for _, tid in due:
        # 每个租户备完重新看负载:一轮可能跑很久(1000 租户 3.1h),期间客户负载起来了
        # 就得让路,不能凭进入循环那一刻的判断跑到底。
        load = _cpu_load_ratio()
        # 中途涨负载就停 —— 但 _forced 那轮不停:那一轮本来就是因为已经超了硬上限才
        # 插队跑的,再被负载打断就又回到"永远不备"。降优先级已经把代价控住了。
        if not _forced and load is not None and load > _BACKUP_LOAD_CEILING:
            print(f"backup: load rose to {load:.2f} mid-sweep, stopping after {done}")
            break
        if _backup_one_tenant(tid, nice=_forced, table=table):
            done += 1
    if due:
        print(f"backup: {done}/{len(due)} tenant(s) backed up (interval={_BACKUP_INTERVAL_HOURS}h)")
    return done, 0, len(due)


def _backup_loop():
    """#469 R7 — 独立单例线程跑本机定时备份。

    与 disk-gc/disk-report 同款隔离理由:一轮 sweep 可能跑很久(1000 租户最坏 3.1h),
    绝不能塞进 poll 心跳 —— 那会让 host 被判 stale 而触发重启。
    """
    while True:
        try:
            _backup_sweep()
        except Exception as e:  # noqa: BLE001 — 绝不让备份线程崩掉
            print(f"backup loop error (non-fatal): {e}")
        _agent_loop_tick("backup")  # #387 self-stamped
        time.sleep(_BACKUP_TICK_SEC)


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
        # (push fan-out / ssm wake / _recover_vm)正在起同租户,是良性 skip,不是失败。
        # 返 "skip" 让调用点保持 assignment pending(不标 failed、不消耗 retry 预算),
        # 下一 tick 自然收敛或 winner 已起好后 vm.json 存在被跳过。systemd-cat 透传
        # 退出码(systemd 255 真机实测:exit 75→rc 75),故这里能拿到真实 75。
        if rc == 75:
            return "skip"
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
            # 半成品 VM(vm.json 在但 FC 没起/gateway 没活)误标 running(真机 28/300 卡的
            # 反面:误 promote 更糟)。统一由健康探测路径 _write_ddb 在 vm_health+app_health
            # (gateway 18789 应答)+ _ensure_route 都成立后才 creating→running(codex 判:唯一 promote 写手)。
        elif step["action"] == "over_budget":
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
            # 落 `if ok:` 会被当成功 _mark_assignment_done → 该租户其实没起(winner
            # 可能也失败),孤儿。skip = 良性(另一进程在起同租户),保持 pending 不动,
            # 不标 done/failed、不消耗 retry 预算、不重入队,下一 tick 由 DDB 行决定。
            if ok == "skip":
                continue
            # 终态)。与 "skip"(rc45/75,保持 pending)不同:deleted 是终态,标 assignment done
            # 停止重投(否则每 tick 反复叫醒 host 起一个已删租户的 VM)。必须在 `if ok:` 之前判
            # ——"abort" 也是 truthy 字符串,落 `if ok:` 会被 _mark_assignment_done 但走的是
            # "成功起了"的语义分支(误标 running)。这里显式终结:标 done、不重投、不算失败。
            if ok == "abort":
                _mark_assignment_done(table, INSTANCE_ID, tid)
                continue
            if ok:
                _mark_assignment_done(table, INSTANCE_ID, tid)
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

    Returns a summary {orphan_dnat, missing_dnat, ghost_descriptor, foreign_vm,
    checked} for logging + tests.

      · 字段整组缺席      = 这台从没跑过对账(循环未启动 / agent 版本旧)
      · checked=0 + 全零  = 跑了但没跑完(表名/IMDS 缺、iptables 列举失败、DDB scan 失败)
      · checked=1 + 计数  = 真实测量值
    此前所有早退路径都直接返回 empty 而【不写缓存】,于是"探测失败"和"从没探测过"在心跳里
    长得一模一样,排障分不出来 —— 与"缺席=未探测过而非谎报 0"的原则自相矛盾。真机实测
    (2026-08-19,新扩空机;主机坐标见 engineering/evidence/ 下本 issue 的证据记录)就是这么
    暴露出来的。
    """
    empty = {
        "orphan_dnat": 0,
        "missing_dnat": 0,
        "ghost_descriptor": 0,
        "foreign_vm": 0,
        "checked": 0,
    }

    def _cache(summary):
        """任何退出路径都把结果缓存给心跳 —— 包括没跑完的那些(checked=0)。"""
        with _lock:
            _agent_metrics["route_drift"] = dict(summary)
        return summary

    if not TENANTS_TABLE or not INSTANCE_ID:
        return _cache(empty)
    host_ip = _get_host_private_ip()
    if not host_ip:
        return _cache(empty)
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
            return _cache(empty)
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
            return _cache(empty)
        diff = route_ops.reconcile_drift(vm_dir_tids, ddb_desc, dnat_rules)
        summary = {k: len(v) for k, v in diff.items()}
        # 上面那三个信号都建立在"账本说这个租户在本机"之上(scan 用 host_private_ip=本机 过滤),
        # 所以它们发现不了最危险的那种分裂:【本机真有这个租户的 VM,而账本说它在别台】。
        # 那正是"同一份快照恢复出两个 VM"的可观测形态,而单机视角只看得见这一半 —— 控制面
        # 汇总各 host 的这一半才拼出全貌(汇总在 health_check 侧)。
        # 成本与异常量成正比而不是与机队规模成正比:正常情况下 candidates 为空,一次 get_item
        # 都不发;只有真出现归属不一致时才逐个查那几个。
        foreign = []
        for tid in sorted(vm_dir_tids - set(ddb_desc)):
            try:
                row = table.get_item(
                    Key={"id": tid},
                    ProjectionExpression="id, host_id, host_private_ip, #s",
                    ExpressionAttributeNames={"#s": "status"},
                ).get("Item")
            except Exception as e:
                print(f"route_drift: foreign 探测读 {tid} 失败(跳过): {e}")
                continue
            if not row:
                continue  # 表里没这行 —— 属于孤儿目录,不是归属分裂,别混进 foreign
            if (row.get("status") or "") in ("deleted", "deleting"):
                continue  # 正在删/已删,本机残留目录是清理滞后,不是双跑
            other_host = row.get("host_id") or ""
            other_ip = row.get("host_private_ip") or ""
            if (other_host and other_host != INSTANCE_ID) or (
                other_ip and other_ip != host_ip
            ):
                foreign.append(tid)
        summary["foreign_vm"] = len(foreign)
        if any(summary.values()):
            print(
                f"route_drift: orphan_dnat={summary['orphan_dnat']} "
                f"missing_dnat={summary['missing_dnat']} "
                f"ghost_descriptor={summary['ghost_descriptor']} "
                f"foreign_vm={summary['foreign_vm']}"
                + (f" foreign_tids={foreign[:5]}" if foreign else "")
            )
        # 跑完整一轮才算 checked=1;缓存给心跳上报(此前只 print,控制面看不见,
        # 也就无法跨 host 汇总)。
        summary["checked"] = 1
        return _cache(summary)
    except Exception as e:
        # Never let drift-reporting kill the housekeeping loop.
        print(f"route_drift error (non-fatal): {e}")
        return _cache(empty)


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


# #566 拆分② — egress_mode reconcile:控制面 API(POST /hosts/egress)把期望态写进本机
# host 行的 egress_mode 字段;host-agent 每轮 poll 读它并把 OPENCLAW-EGRESS 链收敛到期望态。
# 这是「扛 host 重建/重启」的持久化支点:纯 SSM live-apply 在重启后丢失,靠这里从 DDB 收敛回来。
# 只在 mode 变化时动 iptables(幂等 scratch-swap);apply 派生 LLM 洞与 init-host/API 同口径。
_egress_applied_mode = None
_EGRESS_CHAIN_SH = "/home/ubuntu/oc-egress-chain.sh"


def _derive_egress_env():
    """读 /etc/platform.env,派生 oc-egress-chain.sh 需要的 env(与 init-host.sh 同口径)。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    env = {}
    try:
        with open("/etc/platform.env") as fh:
            for line in fh:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k] = v.strip().strip('"')
    except OSError:
        return None
    vpc = env.get("EGRESS_VPC_CIDR", "")
    if not vpc:
        return None
    raw = env.get("LITELLM_HOST", "")
    u = urlparse(raw if "://" in raw else "//" + raw, scheme="")
    host = u.hostname or ""
    port = u.port
    scheme = (u.scheme or "").lower()
    if not port:
        port = 443 if scheme == "https" else (80 if scheme == "http" else 4000)
    ip = ""
    try:
        ip = socket.gethostbyname(host) if host else ""
    except OSError:
        ip = ""
    in_vpc = False
    try:
        in_vpc = bool(ip) and ipaddress.ip_address(ip) in ipaddress.ip_network(vpc, strict=False)
    except ValueError:
        in_vpc = False
    return {
        "VPC_CIDR": vpc,
        "LITELLM_HOST": ip if in_vpc else "",  # 公网网关不开内网洞,靠公网 RETURN
        "LITELLM_PORT": str(port),
        "SPIRE_SERVER": env.get("SPIRE_SERVER_IP", ""),
        "TAP_IFACE": "tap+",
        "DENY_RFC1918": "false",
    }


def _egress_chain_present():
    """OPENCLAW-EGRESS 链是否已在内核(state-based reconcile 的实测判据)。"""
    try:
        return (
            subprocess.run(
                ["iptables", "-S", "OPENCLAW-EGRESS"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except Exception:  # noqa: BLE001
        return False


def _reconcile_egress():
    """state-based reconcile:把 OPENCLAW-EGRESS 的【实际】状态收敛到 DDB 期望态。

    自愈 reboot(链随机器丢)与带外移除:desired=deny 但链缺→装;desired=off 但链在→拆;
    已收敛则不动内核(不每轮重复 apply)。这是「扛 host 重建」的持久化支点。
    """
    global _egress_applied_mode
    if not HOSTS_TABLE or not INSTANCE_ID:
        return
    if not os.path.exists(_EGRESS_CHAIN_SH):
        return  # 脚本未下发(egress 从未开过)→ 无需 reconcile
    try:
        item = (
            _get_ddb().Table(HOSTS_TABLE).get_item(Key={"instance_id": INSTANCE_ID})
        ).get("Item")
    except Exception as e:  # noqa: BLE001 — reconcile 失败绝不影响 poll
        print(f"egress reconcile read failed (non-fatal): {e}")
        return
    desired = (item or {}).get("egress_mode", "off")
    if desired not in ("off", "deny"):
        return
    present = _egress_chain_present()
    need_apply = desired == "deny" and not present
    need_teardown = desired == "off" and present
    if not need_apply and not need_teardown:
        _egress_applied_mode = desired  # 已收敛
        return
    try:
        if need_apply:
            extra = _derive_egress_env()
            if not extra:
                print("egress reconcile: cannot derive env (missing platform.env/VPC) — skip")
                return
            run_env = dict(os.environ)
            run_env.update(extra)
            if (item or {}).get("egress_deny_rfc1918") is True:
                run_env["DENY_RFC1918"] = "true"
            # #566 follow-up — 连同运维经 API 加的额外放行洞一起收敛(重启/重建后不丢端口)。
            run_env["EGRESS_EXTRA_ALLOW"] = str((item or {}).get("egress_extra_allow", "") or "")
            subprocess.run(["bash", _EGRESS_CHAIN_SH, "apply"], env=run_env, timeout=60, check=False)
        else:
            run_env = dict(os.environ)
            run_env.update({"VPC_CIDR": "10.0.0.0/8", "TAP_IFACE": "tap+"})
            subprocess.run(["bash", _EGRESS_CHAIN_SH, "teardown"], env=run_env, timeout=60, check=False)
        _egress_applied_mode = desired
        print(f"egress reconcile: converged to egress_mode={desired} (was drift: apply={need_apply} teardown={need_teardown})")
    except Exception as e:  # noqa: BLE001
        print(f"egress reconcile apply failed (non-fatal): {e}")


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
            _probe_ssm_buffer_full()  # #52 D: 同上,缓存在 poll 里,不在 scrape 时读日志
            _reconcile_egress()  # #566 拆分②:把 egress_mode 期望态收敛到本机(扛重建)
        except Exception as e:
            print(f"poll error: {e}")
        _agent_loop_tick("poll")  # #387: self-stamped (period=POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
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
                    "ssm_buffer_full": _agent_metrics.get("ssm_buffer_full"),
                    "route_drift": dict(_agent_metrics.get("route_drift") or {}),
                    # scrape 只读;未跑过 sweep 时键缺席而不是谎报 0。
                    "backup_script_stale": _agent_metrics.get("backup_script_stale"),
                    # 而不是谎报 0;发生过就一直是 1(**刻意不自动清零**:一个冻住的客户 VM
                    # 不会自己好,清零等于让告警自己消失,而问题还在)。
                    "backup_vm_left_paused": _agent_metrics.get("backup_vm_left_paused"),
                }
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
            stranding = _collect_stranding_stats()
            body = _render_metrics_text(
                data, port_stats, agent_stats, stranding
            ).encode()
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
    # stuck rm -rf never blocks heartbeat → no false stale-restart).
    g = threading.Thread(target=_disk_gc_loop, daemon=True)
    g.start()
    # probes so a busy host's disk timestamp stays fresh → stale ⟺ agent actually down,
    # letting dispatch fail-open safely on stale reads instead of mis-blocking).
    dr = threading.Thread(target=_disk_report_loop, daemon=True)
    dr.start()
    # 数学上追不上(每 30min 备 20 个 = 24h 上限 960 个);本机自驱按机器打散,一台 1000
    # 租户串行也只需 ~6h/轮。独立线程的理由同 disk-gc:一轮可能跑数小时,绝不能进 poll
    # 心跳(会被判 host stale 触发重启)。OC_BACKUP_LOOP=0 可关(灰度/回滚开关)。
    if _BACKUP_LOOP_ENABLED and TENANTS_TABLE:
        print(
            f"openclaw-agent backup loop: tick={_BACKUP_TICK_SEC}s "
            f"interval={_BACKUP_INTERVAL_HOURS}h load_ceiling={_BACKUP_LOAD_CEILING}"
        )
        bk = threading.Thread(target=_backup_loop, daemon=True)
        bk.start()
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
