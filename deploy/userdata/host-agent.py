#!/usr/bin/env python3
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
TENANTS_TABLE = os.environ.get("OC_TENANTS_TABLE", "")

# DynamoDB client (region auto-detected from instance metadata)
_ddb = None
_status = {}
_lock = threading.Lock()


def _get_ddb():
    global _ddb
    if _ddb is None:
        # Get region from IMDS
        try:
            import urllib.request
            tok = urllib.request.urlopen(urllib.request.Request(
                "http://169.254.169.254/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                method="PUT",
            ), timeout=2).read().decode()
            region = urllib.request.urlopen(urllib.request.Request(
                "http://169.254.169.254/latest/meta-data/placement/region",
                headers={"X-aws-ec2-metadata-token": tok},
            ), timeout=2).read().decode()
        except Exception:
            region = "ap-northeast-1"
        _ddb = boto3.resource("dynamodb", region_name=region,
                              config=BotoConfig(retries={"max_attempts": 2}))
    return _ddb


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
            with open(cfg_file) as f:
                cfg = json.load(f)
            guest_ip = cfg.get("guest_ip", "")
        except Exception:
            continue
        if not guest_ip:
            continue

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
        try:
            if info["vm_health"] == "up":
                # Promote creating → running + read gateway token
                token = _read_gateway_token(info["guest_ip"])
                update_expr = "SET #s = :r, vm_health = :vh, app_health = :ah, health_failures = :z, last_health_check = :t, updated_at = :t"
                update_vals = {
                    ":r": "running", ":c": "creating",
                    ":vh": info["vm_health"], ":ah": info["app_health"],
                    ":z": 0, ":t": now,
                }
                if token:
                    update_expr += ", gateway_token = :tk"
                    update_vals[":tk"] = token
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
            # Not in creating status, just update health
            try:
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
        except Exception as e:
            print(f"poll error: {e}")
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/"):
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
