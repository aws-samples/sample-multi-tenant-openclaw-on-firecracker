#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""OpenClaw Host Agent — probes local VMs and writes health status to DynamoDB.
Replaces per-tenant SSM health checks. Runs as systemd service on each host.
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
from botocore.config import Config as BotoConfig

POLL_INTERVAL = int(os.environ.get("OC_AGENT_POLL_INTERVAL", "15"))
PORT = int(os.environ.get("OC_AGENT_PORT", "8899"))
# Health/control AND Prometheus /metrics are served by the SAME HTTPServer
# on PORT (8899). The earlier OC_AGENT_PROM_PORT=9090 split-port design was
# never wired into main(), causing the ADOT collector to fail every scrape
# (issue #4 regression, fixed in 1.2.5).
VM_DIR = "/data/firecracker-vms"
GATEWAY_PORT = 18789
TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "")

# Issue #72 — migration sentinel. While a tenant is being live-migrated,
# migrate-vm.sh (a transient SSM subprocess) pauses the VM and takes a
# Firecracker snapshot. During that window host-agent must NOT touch the VM:
#   * balloon paths (GET /balloon/statistics, PATCH /balloon) racing
#     PUT /snapshot/create on Firecracker's single-threaded API socket was the
#     most likely cause of the #72 "empty reply / curl exit 52";
#   * MORE IMPORTANTLY, a Paused VM stops answering ping, so _probe_all's
#     dead-zone detector would fire _force_relaunch_vm (stop+launch) mid-
#     snapshot, and on the target _recover_vm would race /snapshot/load.
# migrate-vm.sh drops a sentinel file per migrating tenant; host-agent skips
# any tenant with a live sentinel. A sentinel older than MIGRATION_SENTINEL_TTL
# is treated as leaked (migrate-vm.sh crashed) and ignored, so a dead migration
# can't wedge a VM out of health-checking forever.
MIGRATION_SENTINEL_TTL = int(os.environ.get("OC_MIGRATION_SENTINEL_TTL", "900"))


def _is_migrating(tenant_id):
    """True if a live (non-stale) migration sentinel exists for this tenant."""
    sentinel = os.path.join(VM_DIR, tenant_id, ".migrating")
    try:
        age = time.time() - os.path.getmtime(sentinel)
    except OSError:
        return False
    if age > MIGRATION_SENTINEL_TTL:
        # Leaked by a crashed migrate-vm.sh — resume normal supervision.
        return False
    return True
# Since 1.3.0: host-agent also writes a heartbeat to the hosts table so the
# health_check Lambda can do AZ-level failover (it needs to know which hosts
# are still alive at the host level, not just whether their tenants reported).
HOSTS_TABLE = os.environ.get("HOSTS_TABLE", "")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "")

# Write-amplification control (T3-5). At 1000 VMs the old "SSH + failed
# conditional promote + health write, every 15s, per VM" cost ~11.5M wasted
# DDB writes/day + 5.7M SSH execs (~$430/mo). We now:
#   * SSH-read the gateway token + attempt the creating→running promote ONLY
#     until a tenant is known past `creating` (tracked in _promoted); and
#   * write tenant health to DDB only when (vm_health, app_health) changes,
#     plus a slow liveness refresh so the health_check watchdog never sees a
#     stable tenant as stale.
# OC_AGENT_DDB_REFRESH is clamped below STALE_SECONDS (=120 in
# health_check/handler.py) so a stable tenant is always re-stamped at least
# twice per staleness window — a misconfigured refresh can't fake staleness.
_DDB_REFRESH = min(int(os.environ.get("OC_AGENT_DDB_REFRESH", "60")), 110)

# Tenants known to be past `creating` — skip the SSH token read + the
# guaranteed-to-fail conditional promote for these. Seeded on a successful
# promote OR a ConditionalCheckFailedException (already running). Reset on
# agent restart (one cheap SSH+failed-promote per VM on the first tick) and
# GC'd when a tenant disappears, so a recreated tenant re-promotes.
_promoted = set()

# tid -> (vm_health, app_health, monotonic_ts_of_last_write). Drives the
# state-change-or-refresh write decision. Only updated on a SUCCESSFUL write,
# so a failed write is retried on the next tick (never-raise contract kept).
_last_ddb_write = {}


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
    ("openclaw_vm_memory_used_mb",   "Per-VM memory in active use (MB)",     "memory_used_mb"),
    ("openclaw_vm_memory_balloon_mib", "Balloon size held by the host (MiB)", "memory_balloon_mib"),
    ("openclaw_vm_disk_used_mb",     "Per-VM data disk used (MB)",            "disk_used_mb"),
    ("openclaw_vm_disk_total_mb",    "Per-VM data disk capacity (MB)",        "disk_total_mb"),
    ("openclaw_vm_disk_used_pct",    "Per-VM data disk used (percent)",       "disk_used_pct"),
    ("openclaw_vm_cpu_pct",          "Per-VM CPU usage (percent of allocated vcpus)",  "cpu_pct"),
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
BALLOON_MIN_GUEST_AVAILABLE_MB = int(os.environ.get("BALLOON_MIN_GUEST_AVAILABLE_MB", "512"))

# DynamoDB client (region auto-detected from instance metadata)
_ddb = None
_status = {}
_lock = threading.Lock()


def _get_ddb():
    global _ddb
    if _ddb is None:
        # Get region from IMDS (IMDSv2 with session token)
        # EC2 IMDS only supports http:// on link-local 169.254.169.254
        try:
            import urllib.request
            tok = urllib.request.urlopen(urllib.request.Request(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
                "http://169.254.169.254/latest/api/token",  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-urllib-request
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                method="PUT",
            ), timeout=2).read().decode()
            region = urllib.request.urlopen(urllib.request.Request(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
                "http://169.254.169.254/latest/meta-data/placement/region",  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-urllib-request
                headers={"X-aws-ec2-metadata-token": tok},
            ), timeout=2).read().decode()
        except Exception:
            region = "ap-northeast-1"
        _ddb = boto3.resource("dynamodb", region_name=region,
                              config=BotoConfig(retries={"max_attempts": 2}))
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
            ["bash", "/home/ubuntu/launch-vm.sh", str(tenant_id), str(vm_num), str(vcpu), str(mem_mb)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
    print(f"force-relaunch {tenant_id} (vm{vm_num}): FC alive but guest unreachable "
          f"for {_NET_DEAD_THRESHOLD} polls — rebuilding network")
    try:
        subprocess.run(
            ["bash", "/home/ubuntu/stop-vm.sh", str(tenant_id), str(vm_num)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        subprocess.Popen(
            ["bash", "/home/ubuntu/launch-vm.sh", str(tenant_id), str(vm_num), str(vcpu), str(mem_mb)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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

        # Issue #72: skip VMs mid-migration. A paused (snapshot) or not-yet-
        # loaded (restore) VM must NOT be probed, or the dead-zone detector
        # relaunches it / _recover_vm races /snapshot/load. Report a neutral
        # "migrating" status so the metrics/DDB view is truthful.
        if _is_migrating(tenant_id):
            results[tenant_id] = {"vm_health": "migrating", "app_health": "down",
                                  "guest_ip": ""}
            continue

        # Auto-recover: vm.json exists but Firecracker not running.
        # Capture pid here too so the metrics composer can read /proc/<pid>
        # without re-running pgrep on every gauge.
        sock_file = os.path.join(vm_path, "fc.sock")
        pgrep = subprocess.run(
            ["pgrep", "-f", f"api-sock {sock_file}"],
            capture_output=True, text=True)
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
            results[tenant_id] = {"vm_health": "recovering", "app_health": "down", "guest_ip": guest_ip}
            continue

        _recovering.discard(tenant_id)

        vm_health = "down"
        app_health = "down"

        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "2", guest_ip],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                vm_health = "up"
        except Exception:
            pass

        if vm_health == "up":
            _register_net_poll(tenant_id, guest_reachable=True)
            try:
                r = subprocess.run(
                    ["curl", "-sf", "-o", "/dev/null", "--connect-timeout", "3",
                     f"http://{guest_ip}:{GATEWAY_PORT}/"],
                    capture_output=True, timeout=8)
                if r.returncode == 0:
                    app_health = "up"
            except Exception:
                pass
        elif _register_net_poll(tenant_id, guest_reachable=False):
            # FC alive but guest unreachable past the threshold — rebuild network.
            _force_relaunch_vm(tenant_id, cfg)
            results[tenant_id] = {"vm_health": "recovering", "app_health": "down",
                                  "guest_ip": guest_ip}
            continue

        results[tenant_id] = {"vm_health": vm_health, "app_health": app_health,
                              "guest_ip": guest_ip, "fc_pid": fc_pid}

    return results


def _read_gateway_token(guest_ip):
    """SSH into VM and read gateway token from openclaw.json."""
    try:
        # 1.5.0 security: key-based SSH. The private key is per-host
        # (/etc/openclaw/host_vm_key, generated at boot by init-host.sh) and
        # never leaves this host; the matching public key was injected into
        # the VM's data disk at launch. No shared password, no secret over SSM.
        r = subprocess.run(
            ["ssh", "-i", "/etc/openclaw/host_vm_key",
             "-o", "StrictHostKeyChecking=no",
             "-o", "IdentitiesOnly=yes",
             f"agent@{guest_ip}", "jq -r .gateway.auth.token .openclaw/openclaw.json"],
            capture_output=True, text=True, timeout=10,
        )
        token = r.stdout.strip()
        return token if token and token != "null" else ""
    except Exception as e:
        print(f"read token from {guest_ip}: {e}")
        return ""


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


def _enrich_metrics(results):
    """Compose per-VM metrics for healthy VMs and mirror them into the probe
    results in place, so the Prometheus exporter (/metrics, scraped by ADOT →
    AMP) always sees fresh gauges — independent of whether _write_ddb decides
    to skip the DDB write on this tick (T3-5).

    Skipped for down/recovering/migrating VMs so their last-known metrics are
    kept rather than overwritten with zeros (which would mask the failure).
    Also keeps _sample_cpu_pct's rolling baseline advancing every tick.
    """
    for tid, info in results.items():
        if info.get("vm_health") != "up":
            continue
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
        try:
            info["metrics"] = _compose_metrics(
                tid, vm_mem_mb, sock_file, data_file,
                fc_pid=info.get("fc_pid"), vcpu=vm_vcpu)
        except Exception as e:
            print(f"compose_metrics {tid}: {e}")


def _write_ddb(results):
    """Update tenant health in DynamoDB. Promote creating → running when VM is up.

    T3-5 write-amplification fix: only touches DDB when a tenant's
    (vm_health, app_health) changed since its last successful write, or when
    the OC_AGENT_DDB_REFRESH liveness timer elapsed. The SSH token read + the
    creating→running conditional promote run only until a tenant is in
    _promoted. Metrics are composed by _enrich_metrics in the poll loop, so
    this function just reads info["metrics"].
    """
    if not TENANTS_TABLE:
        return
    if not results:
        # No VMs on this host — every tracked tenant is gone; GC and bail
        # (no table round-trip needed).
        _promoted.clear()
        _last_ddb_write.clear()
        return
    table = _get_ddb().Table(TENANTS_TABLE)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    seen = set()
    for tid, info in results.items():
        seen.add(tid)
        metrics = info.get("metrics") if info.get("vm_health") == "up" else None

        # Promote-once: attempt the SSH token read + creating→running write
        # only while the tenant hasn't been confirmed past `creating`.
        if info["vm_health"] == "up" and tid not in _promoted:
            token = _read_gateway_token(info["guest_ip"])
            if not token:
                continue  # Gateway not ready yet — retry next tick, stay un-promoted
            try:
                table.update_item(
                    Key={"id": tid},
                    UpdateExpression=(
                        "SET #s = :r, vm_health = :vh, app_health = :ah, "
                        "health_failures = :z, last_health_check = :t, "
                        "updated_at = :t, gateway_token = :tk, #m = :m"),
                    ConditionExpression="#s = :c",
                    ExpressionAttributeNames={"#s": "status", "#m": "metrics"},
                    ExpressionAttributeValues={
                        ":r": "running", ":c": "creating",
                        ":vh": info["vm_health"], ":ah": info["app_health"],
                        ":z": 0, ":t": now, ":tk": token,
                        ":m": metrics or {},
                    },
                )
                print(f"promoted {tid} creating → running")
                _promoted.add(tid)
                _last_ddb_write[tid] = (info["vm_health"], info["app_health"], time.monotonic())
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                # Already running — the promote write was a no-op. Mark promoted
                # and fall through to the steady-state writer to refresh health.
                _promoted.add(tid)
            except Exception as e:
                print(f"ddb promote {tid}: {e}")
                continue
            else:
                continue  # promote wrote everything; nothing more to do this tick

        # Steady-state: write only on state change or when the refresh timer
        # elapsed. Keeps a stable tenant at 1 write / _DDB_REFRESH instead of
        # 2 writes / poll while never breaching the staleness watchdog.
        state = (info["vm_health"], info["app_health"])
        prev = _last_ddb_write.get(tid)
        due = (prev is None
               or prev[:2] != state
               or (time.monotonic() - prev[2]) >= _DDB_REFRESH)
        if not due:
            continue

        try:
            if metrics is not None:
                # `metrics` is a DDB reserved keyword — alias via #m.
                table.update_item(
                    Key={"id": tid},
                    UpdateExpression=("SET vm_health = :vh, app_health = :ah, "
                                      "last_health_check = :t, #m = :m"),
                    ExpressionAttributeNames={"#m": "metrics"},
                    ExpressionAttributeValues={
                        ":vh": info["vm_health"], ":ah": info["app_health"],
                        ":t": now, ":m": metrics,
                    },
                )
            else:
                table.update_item(
                    Key={"id": tid},
                    UpdateExpression="SET vm_health = :vh, app_health = :ah, last_health_check = :t",
                    ExpressionAttributeValues={
                        ":vh": info["vm_health"], ":ah": info["app_health"], ":t": now,
                    },
                )
            _last_ddb_write[tid] = (state[0], state[1], time.monotonic())
        except Exception as e:
            # Never-raise: leave _last_ddb_write untouched so the next tick retries.
            print(f"ddb update {tid}: {e}")

    # GC per-tenant state for VMs no longer present (deleted / migrated away),
    # so a recreated tenant with the same id re-runs the SSH promote and the
    # dicts don't grow unboundedly.
    for gone in _promoted - seen:
        _promoted.discard(gone)
    for gone in set(_last_ddb_write) - seen:
        _last_ddb_write.pop(gone, None)


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
        r = subprocess.run(["dumpe2fs", "-h", data_file],
                           capture_output=True, text=True, timeout=5)
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
    fields = line[rparen + 1:].split()
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


def _compute_cpu_pct(prev_jiffies, prev_ts, cur_jiffies, cur_ts, vcpu, clk_tck=_CLK_TCK):
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
            ["curl", "-sf", "--unix-socket", sock_file, "http://localhost/balloon/statistics"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def _set_balloon_target(sock_file, amount_mib):
    """Set balloon target size (inflate/deflate)."""
    try:
        subprocess.run(
            ["curl", "-sf", "--unix-socket", sock_file, "-X", "PATCH",
             "http://localhost/balloon", "-H", "Content-Type: application/json",
             "-d", json.dumps({"amount_mib": amount_mib})],
            capture_output=True, timeout=5)
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
        # Issue #72: never PATCH /balloon for a migrating tenant, even if these
        # probe_results were captured just before the sentinel appeared.
        if _is_migrating(tid):
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
        guest_available_mb = stats.get("stats", {}).get("available_memory", 0) // (1024 * 1024)

        if host_pressure < 0.20:
            # Host under pressure — try to reclaim from this VM
            reclaimable = guest_available_mb - BALLOON_MIN_GUEST_AVAILABLE_MB
            if reclaimable > 0:
                target = min(current_balloon_mib + reclaimable, max_balloon)
                if target > current_balloon_mib:
                    _set_balloon_target(sock_file, target)
                    print(f"balloon inflate {tid}: {current_balloon_mib}→{target}MB "
                          f"(host_avail={host_available}MB guest_avail={guest_available_mb}MB)")

        elif host_pressure > 0.40:
            # Host has plenty of memory — give back to VMs
            if current_balloon_mib > 0:
                _set_balloon_target(sock_file, 0)
                print(f"balloon deflate {tid}: {current_balloon_mib}→0MB (host_avail={host_available}MB)")


def _poll_loop():
    while True:
        try:
            # 1.3.0: heartbeat at the start so failures in tenant probing
            # don't suppress the host-level liveness signal.
            _write_host_heartbeat()
            results = _probe_all()
            # Compose metrics every tick (T3-5) so /metrics keeps 15s
            # resolution even on ticks where the DDB write is throttled.
            _enrich_metrics(results)
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
    print(f"openclaw-agent starting on :{PORT}, poll every {POLL_INTERVAL}s, table={TENANTS_TABLE}")
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
