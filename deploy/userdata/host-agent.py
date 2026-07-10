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
# /opt/openclaw/. See project interface spec §1/§3/§4/§6/§8.
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


def _render_metrics_text(snapshots):
    """Render the in-memory snapshots dict as Prometheus exposition text.

    Pure function — no I/O — so it is easy to assert against in unit tests.
    Always emits HELP/TYPE headers (even with zero samples) so that scrapers
    that validate metadata don't choke on a quiet host.
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
    # 1. Check whether iptables already has a rule for this guest_ip that
    #    was just recovered at bootstrap. If yes, reuse it — same tenant
    #    same slot after a restart.
    try:
        existing = route_ops.list_dnat_rules()
    except Exception:
        existing = {}
    port: int | None = None
    for p, g in existing.items():
        if g == guest_ip and route_ops.PORT_RANGE_LOW <= p <= route_ops.PORT_RANGE_HIGH:
            bitmap.mark_used(p)
            port = p
            break
    if port is None:
        port = route_ops.alloc_and_dnat_atomic(bitmap, guest_ip)
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


def _get_ddb():
    global _ddb
    if _ddb is None:
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
            region = (
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
            region = "ap-northeast-1"
        _ddb = boto3.resource(
            "dynamodb",
            region_name=region,
            config=BotoConfig(retries={"max_attempts": 2}),
        )
    return _ddb


_recovering = set()  # Track VMs being recovered to avoid duplicate launches

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


def _recover_vm(tenant_id, cfg):
    """Launch VM that has vm.json but no running Firecracker process."""
    if tenant_id in _recovering:
        return
    _recovering.add(tenant_id)
    vm_num = cfg.get("vm_num", 1)
    vcpu = cfg.get("vcpu", 2)
    mem_mb = cfg.get("mem_mb", 4096)
    print(f"recovering {tenant_id} (vm{vm_num} {vcpu}vCPU/{mem_mb}MB)")
    try:
        subprocess.Popen(
            [
                "bash",
                "/home/ubuntu/launch-vm.sh",
                str(tenant_id),
                str(vm_num),
                str(vcpu),
                str(mem_mb),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"recover {tenant_id} failed: {e}")
        _recovering.discard(tenant_id)


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
            ["bash", "/home/ubuntu/stop-vm.sh", str(tenant_id), str(vm_num)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.Popen(
            [
                "bash",
                "/home/ubuntu/launch-vm.sh",
                str(tenant_id),
                str(vm_num),
                str(vcpu),
                str(mem_mb),
            ],
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
            guest_ip = cfg.get("guest_ip", "")
        except Exception:
            continue
        if not guest_ip:
            continue

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
            }
            continue

        _recovering.discard(tenant_id)

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
                r = subprocess.run(
                    [
                        "curl",
                        "-sf",
                        "-o",
                        "/dev/null",
                        "--connect-timeout",
                        "3",
                        # gateway 对 / 返回 404(无内容是预期),curl -f 把 404 当失败
                        # → app_health 误判 down(健康 gateway 亮红灯)。探 /healthz(gateway
                        # 起来即 200),-f 才反映真实存活。实测:/ =404、/healthz =200。
                        f"http://{guest_ip}:{GATEWAY_PORT}/healthz",
                    ],
                    capture_output=True,
                    timeout=8,
                )
                if r.returncode == 0:
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
            }
            continue

        results[tenant_id] = {
            "vm_health": vm_health,
            "app_health": app_health,
            "guest_ip": guest_ip,
            "fc_pid": fc_pid,
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


def _write_host_heartbeat():
    """Update this host's ``last_seen`` and ``last_health_check`` timestamps in
    the hosts table. Called every poll so the health_check Lambda can detect
    AZ-level outages by checking host-level freshness (not just tenant-level
    health, which goes stale only when a tenant exists). Best-effort; never
    raises.
    """
    if not HOSTS_TABLE or not INSTANCE_ID:
        return
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        table = _get_ddb().Table(HOSTS_TABLE)
        table.update_item(
            Key={"instance_id": INSTANCE_ID},
            UpdateExpression="SET last_seen = :t, last_health_check = :t",
            ExpressionAttributeValues={":t": ts},
        )
    except Exception as e:
        # Heartbeat failures must never crash the poll loop.
        print(f"host heartbeat failed (non-fatal): {e}")


def _write_ddb(results):
    """Update tenant health in DynamoDB. Promote creating → running when VM is up."""
    if not TENANTS_TABLE or not results:
        return
    table = _get_ddb().Table(TENANTS_TABLE)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for tid, info in results.items():
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
            if info["vm_health"] == "up":
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
                    continue
                if not host_private_ip or host_port is None:
                    print(f"ensure_route {tid} degraded (host_ip or port missing)")
                    continue
                # NOTE: `metrics` is a DynamoDB reserved keyword, so it must be
                # referenced via an ExpressionAttributeNames placeholder (#m).
                # Same for `status` (#s, already aliased). Without #m the
                # update_item call returns ValidationException and the tenant
                # never gets promoted to running.
                update_expr = (
                    "SET #s = :r, vm_health = :vh, app_health = :ah, "
                    "health_failures = :z, last_health_check = :t, "
                    "updated_at = :t, host_private_ip = :hpi, "
                    "host_port = :hp, guest_ip = :gi, #m = :m"
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
                    ":m": metrics or {},
                }
                table.update_item(
                    Key={"id": tid},
                    UpdateExpression=update_expr,
                    ConditionExpression="#s = :c",
                    ExpressionAttributeNames={"#s": "status", "#m": "metrics"},
                    ExpressionAttributeValues=update_vals,
                )
                print(
                    f"promoted {tid} creating → running "
                    f"(host={host_private_ip}:{host_port} guest={info['guest_ip']})"
                )
            else:
                # attribute_exists(id) guards against upserting an orphan: if the
                # tenant's main record was already deleted (control-plane DELETE)
                # but its FC process is still alive on this host, a bare
                # update_item would resurrect a ghost row carrying only health
                # fields (no status/host/capacity). Loop 2026-07-02 found 1254
                # such orphans (id, app_health, vm_health, metrics only). The
                # condition fails silently (caught below) once the tenant is gone.
                table.update_item(
                    Key={"id": tid},
                    UpdateExpression="SET vm_health = :vh, app_health = :ah, last_health_check = :t",
                    ConditionExpression="attribute_exists(id)",
                    ExpressionAttributeValues={
                        ":vh": info["vm_health"],
                        ":ah": info["app_health"],
                        ":t": now,
                    },
                )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            # promote's `#s = :c` failed. Two causes: (a) tenant is already
            # running (normal — refresh health/metrics below), or (b) the tenant
            # record no longer exists (deleted while its FC lingered). Both
            # refresh paths carry attribute_exists(id) so case (b) fails cleanly
            # instead of upserting a health-only orphan row (loop 2026-07-02).
            try:
                if metrics is not None:
                    # `metrics` is a DDB reserved keyword — alias via #m.
                    table.update_item(
                        Key={"id": tid},
                        UpdateExpression=(
                            "SET vm_health = :vh, app_health = :ah, "
                            "last_health_check = :t, #m = :m"
                        ),
                        ConditionExpression="attribute_exists(id)",
                        ExpressionAttributeNames={"#m": "metrics"},
                        ExpressionAttributeValues={
                            ":vh": info["vm_health"],
                            ":ah": info["app_health"],
                            ":t": now,
                            ":m": metrics,
                        },
                    )
                else:
                    table.update_item(
                        Key={"id": tid},
                        UpdateExpression="SET vm_health = :vh, app_health = :ah, last_health_check = :t",
                        ConditionExpression="attribute_exists(id)",
                        ExpressionAttributeValues={
                            ":vh": info["vm_health"],
                            ":ah": info["app_health"],
                            ":t": now,
                        },
                    )
            except Exception as e:
                print(f"ddb update {tid}: {e}")
        except Exception as e:
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
        table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :r, running_ts = :t",
            ConditionExpression="#s = :c",
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
    """Atomic tenants.dispatch_retries += 1. Poller cap check is separate."""
    if not TENANTS_TABLE:
        return
    try:
        table = _get_ddb().Table(TENANTS_TABLE)
        table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="ADD dispatch_retries :one",
            ExpressionAttributeValues={":one": 1},
        )
    except Exception as e:
        print(f"bump retries {tenant_id} failed: {e}")


def _flag_requires_intervention(tenant_id):
    """Budget exhausted: tenants.status=requires_intervention (no reset). Only
    from creating/failed to avoid clobbering a subsequent recovery."""
    if not TENANTS_TABLE:
        return
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
    except Exception as e:
        if "ConditionalCheckFailedException" not in str(e):
            print(f"requires_intervention {tenant_id} failed: {e}")


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
            [
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
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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
            _promote_tenant_running(tid)
        elif step["action"] == "over_budget":
            _mark_assignment_failed(
                table, INSTANCE_ID, tid, reason="retry budget exhausted"
            )
            _flag_requires_intervention(tid)
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
            if ok:
                _mark_assignment_done(table, INSTANCE_ID, tid)
                _promote_tenant_running(tid)
            else:
                # P0.1: classify the failure. Transient (throttle/timeout) →
                # short jittered retry, do NOT burn retry budget. Real
                # failure (launch-vm rc≠0) → 10s backoff + bump retries; the
                # budget cap still catches genuinely stuck tenants.
                delay = _backoff_for_reason(err_reason)
                is_transient = delay <= DISPATCH_BACKOFF_TRANSIENT_SEC
                _mark_assignment_failed(table, INSTANCE_ID, tid, reason=err_reason)
                _dispatch_enqueue(tid, delay)
                if not is_transient:
                    _bump_dispatch_retries(tid)


def _dispatch_loop():
    """Poll thread. Only started when ASSIGNMENTS_TABLE is set (systemd env)."""
    while True:
        try:
            table = _get_ddb().Table(ASSIGNMENTS_TABLE)
            _dispatch_tick(table)
        except Exception as e:
            # Never crash — main _poll_loop keeps the host alive.
            print(f"dispatch loop error (non-fatal): {e}")
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
        except Exception as e:
            print(f"poll error: {e}")
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            # Prometheus text exposition (issue #4). Scraped by sibling
            # ADOT collector that remote-writes to AMP.
            with _lock:
                data = dict(_status)
            body = _render_metrics_text(data).encode()
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
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
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
