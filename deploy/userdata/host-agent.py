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
from http.server import HTTPServer, BaseHTTPRequestHandler

import boto3
from botocore.config import Config as BotoConfig

POLL_INTERVAL = int(os.environ.get("OC_AGENT_POLL_INTERVAL", "15"))
PORT = int(os.environ.get("OC_AGENT_PORT", "8899"))
VM_DIR = "/data/firecracker-vms"
GATEWAY_PORT = 18789
TENANTS_TABLE = os.environ.get("TENANTS_TABLE", "")
PROM_PORT = int(os.environ.get("OC_AGENT_PROM_PORT", "9090"))


# ═══════════════════════════════════════════
# Prometheus exporter (issue #4)
# ═══════════════════════════════════════════
#
# Exposes a /metrics endpoint in Prom text-exposition format on port 9090
# (same HTTPServer as /health to avoid a second listener). An ADOT collector
# running as a sibling systemd service scrapes this and remote-writes to AMP.
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
    ("openclaw_vm_cpu_pct",          "Per-VM CPU usage (percent, reserved)",  "cpu_pct"),
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

        # Auto-recover: vm.json exists but Firecracker not running
        sock_file = os.path.join(vm_path, "fc.sock")
        fc_running = subprocess.run(
            ["pgrep", "-f", f"api-sock {sock_file}"],
            capture_output=True).returncode == 0

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
            try:
                r = subprocess.run(
                    ["curl", "-sf", "-o", "/dev/null", "--connect-timeout", "3",
                     f"http://{guest_ip}:{GATEWAY_PORT}/"],
                    capture_output=True, timeout=8)
                if r.returncode == 0:
                    app_health = "up"
            except Exception:
                pass

        results[tenant_id] = {"vm_health": vm_health, "app_health": app_health, "guest_ip": guest_ip}

    return results


def _read_gateway_token(guest_ip):
    """SSH into VM and read gateway token from openclaw.json."""
    try:
        r = subprocess.run(
            ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no",
             f"agent@{guest_ip}", "jq -r .gateway.auth.token .openclaw/openclaw.json"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "SSHPASS": "OpenCl@w2026"},
        )
        token = r.stdout.strip()
        return token if token and token != "null" else ""
    except Exception as e:
        print(f"read token from {guest_ip}: {e}")
        return ""


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
            try:
                with open(cfg_file, encoding="utf-8") as f:
                    vm_mem_mb = json.load(f).get("mem_mb", 4096)
            except Exception:
                pass
            try:
                metrics = _compose_metrics(tid, vm_mem_mb, sock_file, data_file)
            except Exception as e:
                print(f"compose_metrics {tid}: {e}")

        try:
            if info["vm_health"] == "up":
                # Promote creating → running + read gateway token
                token = _read_gateway_token(info["guest_ip"])
                if not token:
                    continue  # Wait for SSH/gateway to be ready
                update_expr = ("SET #s = :r, vm_health = :vh, app_health = :ah, "
                               "health_failures = :z, last_health_check = :t, "
                               "updated_at = :t, gateway_token = :tk, metrics = :m")
                update_vals = {
                    ":r": "running", ":c": "creating",
                    ":vh": info["vm_health"], ":ah": info["app_health"],
                    ":z": 0, ":t": now, ":tk": token,
                    ":m": metrics or {},
                }
                table.update_item(
                    Key={"id": tid},
                    UpdateExpression=update_expr,
                    ConditionExpression="#s = :c",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues=update_vals,
                )
                print(f"promoted {tid} creating → running (token={'yes' if token else 'no'})")
            else:
                table.update_item(
                    Key={"id": tid},
                    UpdateExpression="SET vm_health = :vh, app_health = :ah, last_health_check = :t",
                    ExpressionAttributeValues={
                        ":vh": info["vm_health"], ":ah": info["app_health"], ":t": now,
                    },
                )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            # Not in creating status — already running. Refresh health + metrics.
            try:
                if metrics is not None:
                    table.update_item(
                        Key={"id": tid},
                        UpdateExpression=("SET vm_health = :vh, app_health = :ah, "
                                          "last_health_check = :t, metrics = :m"),
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


def _compose_metrics(tenant_id, vm_mem_mb, sock_file, data_file):
    """Build the per-VM metrics dict written to DDB.

    cpu_pct is a stub (0) for this PR — a future change can plug in cgroup
    accounting or Firecracker host-side stats.
    """
    stats = _get_balloon_stats(sock_file) if sock_file else None
    mem_used, balloon_mib = _get_memory_usage(stats, vm_mem_mb)
    disk_used, disk_total, disk_pct = _get_disk_usage(data_file)
    return {
        "memory_used_mb": int(mem_used),
        "memory_balloon_mib": int(balloon_mib),
        "disk_used_mb": int(disk_used),
        "disk_total_mb": int(disk_total),
        "disk_used_pct": int(disk_pct),
        "cpu_pct": 0,
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
        guest_free_mb = stats.get("stats", {}).get("free_memory", 0) // (1024 * 1024)

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
    print(f"openclaw-agent starting on :{PORT}, poll every {POLL_INTERVAL}s, table={TENANTS_TABLE}")
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
