#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Synthetic host-agent for local-dev (issue #24).
# Mimics the real host-agent's HTTP surface (/health, /metrics) with
# fake but plausible per-VM data so upstream consumers see realistic
# shape without needing a real Firecracker host.

import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("OC_AGENT_PORT", "8899"))
PROM_PORT = int(os.environ.get("OC_AGENT_PROM_PORT", "9090"))


def _fake_status():
    """Return two synthetic tenants — one healthy, one degraded."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "demo-1": {
            "vm_health": "up", "app_health": "up",
            "guest_ip": "172.16.1.2", "updated_at": now,
            "metrics": {
                "memory_used_mb": 2048, "memory_balloon_mib": 0,
                "disk_used_mb": 312, "disk_total_mb": 8192,
                "disk_used_pct": 4, "cpu_pct": 7,
            },
        },
        "demo-2": {
            "vm_health": "down", "app_health": "down",
            "guest_ip": "172.16.2.2", "updated_at": now,
        },
    }


def _render_prom(snapshots):
    """Emit Prometheus text exposition for the synthetic snapshots."""
    out = []
    for metric, key in (
        ("openclaw_vm_memory_used_mb", "memory_used_mb"),
        ("openclaw_vm_disk_used_mb", "disk_used_mb"),
        ("openclaw_vm_disk_used_pct", "disk_used_pct"),
        ("openclaw_vm_cpu_pct", "cpu_pct"),
    ):
        out.append(f"# HELP {metric} stub")
        out.append(f"# TYPE {metric} gauge")
        for tid, info in snapshots.items():
            m = info.get("metrics") or {}
            if key in m:
                out.append(f'{metric}{{tenant="{tid}"}} {m[key]}')
    out.append("# HELP openclaw_vm_health stub")
    out.append("# TYPE openclaw_vm_health gauge")
    for tid, info in snapshots.items():
        v = 1 if info.get("vm_health") == "up" else 0
        out.append(f'openclaw_vm_health{{tenant="{tid}"}} {v}')
    return ("\n".join(out) + "\n").encode()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        snapshots = _fake_status()
        if self.path in ("/health", "/"):
            body = json.dumps(snapshots).encode()
            ctype = "application/json"
        elif self.path == "/metrics":
            body = _render_prom(snapshots)
            ctype = "text/plain; version=0.0.4"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **k):
        pass


def main():
    print(f"openclaw stub host-agent on :{PORT} (health) and :{PROM_PORT} (metrics)")
    # Single port for simplicity in dev; the stub serves /metrics on the
    # same port as /health.
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


if __name__ == "__main__":
    main()
