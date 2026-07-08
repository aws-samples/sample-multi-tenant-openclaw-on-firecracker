#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Single-host Hetzner control plane for SwarmClaw Firecracker tenants.

This replaces the AWS Lambda/DynamoDB/S3/SSM control plane with local
filesystem state and direct subprocess calls. It is designed for one Ubuntu
bare-metal Hetzner host with /dev/kvm.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import re
import shutil
import string
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(os.environ.get("SCF_BASE_DIR", "/data/swarmclaw-firecracker"))
STATE_FILE = Path(os.environ.get("SCF_STATE_FILE", BASE_DIR / "state" / "tenants.json"))
BACKUPS_DIR = Path(os.environ.get("SCF_BACKUPS_DIR", BASE_DIR / "backups"))
VM_DIR = Path(os.environ.get("SCF_VM_DIR", "/data/firecracker-vms"))
SCRIPTS_DIR = Path(os.environ.get("SCF_SCRIPTS_DIR", "/opt/swarmclaw-firecracker/scripts"))
API_KEY_FILE = Path(os.environ.get("SCF_API_KEY_FILE", "/etc/swarmclaw-firecracker/api-key"))
PUBLIC_BASE_URL = os.environ.get("SCF_PUBLIC_BASE_URL", "").rstrip("/")

HOST_PORT_BASE = int(os.environ.get("SCF_HOST_PORT_BASE", "18789"))
APP_PORT = int(os.environ.get("SCF_APP_PORT", "3456"))
SUBNET_PREFIX = os.environ.get("SCF_SUBNET_PREFIX", "172.16")
DEFAULT_VCPU = int(os.environ.get("SCF_DEFAULT_VCPU", "2"))
DEFAULT_MEM_MB = int(os.environ.get("SCF_DEFAULT_MEM_MB", "4096"))
POLL_INTERVAL = int(os.environ.get("SCF_POLL_INTERVAL", "10"))

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
_STATE_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", (name or "tenant").lower()).strip("-")
    value = re.sub(r"-+", "-", value)[:24].strip("-")
    return value or "tenant"


def _suffix() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(6))


def _ensure_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    VM_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        _save_state({"next_vm_num": 1, "tenants": {}})


def _load_state() -> dict:
    _ensure_state()
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        broken = STATE_FILE.with_suffix(f".broken-{int(time.time())}.json")
        STATE_FILE.replace(broken)
        data = {"next_vm_num": 1, "tenants": {}}
        _save_state(data)
        return data


def _save_state(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def _read_api_key() -> str:
    try:
        return API_KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _tenant_dir(tenant_id: str) -> Path:
    return VM_DIR / tenant_id


def _access_key(tenant_id: str) -> str:
    try:
        return (_tenant_dir(tenant_id) / "access-key").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _public_tenant(item: dict) -> dict:
    tenant = dict(item)
    tenant["access_key"] = _access_key(item["id"])
    path = f"/vm/{item['id']}/"
    tenant["dashboard_path"] = path
    tenant["dashboard_url"] = f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path
    return tenant


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _script(name: str) -> str:
    path = SCRIPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"missing script: {path}")
    return str(path)


def _launch(item: dict) -> None:
    args = [
        _script("launch-vm.sh"),
        item["id"],
        str(item["vm_num"]),
        str(item["vcpu"]),
        str(item["mem_mb"]),
        item.get("config_template") or '""',
        '""',
        ",".join(item.get("skills") or []) or '""',
    ]
    result = _run(args, timeout=420)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "launch failed")


def _stop(item: dict) -> None:
    result = _run([_script("stop-vm.sh"), item["id"], str(item["vm_num"])], timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "stop failed")


def _allocate_vm_num(state: dict) -> int:
    vm_num = int(state.get("next_vm_num") or 1)
    state["next_vm_num"] = vm_num + 1
    return vm_num


def _new_tenant(body: dict) -> dict:
    name = _slug(body.get("name") or "tenant")
    tenant_id = f"{name}-{_suffix()}"
    vm_num = body.get("vm_num")  # set by caller after state lock when absent
    return {
        "id": tenant_id,
        "name": name,
        "status": "creating",
        "vm_health": "unknown",
        "app_health": "unknown",
        "vm_num": int(vm_num or 0),
        "guest_ip": "",
        "host_port": 0,
        "vcpu": int(body.get("vcpu") or DEFAULT_VCPU),
        "mem_mb": int(body.get("mem_mb") or DEFAULT_MEM_MB),
        "config_template": (body.get("config_template") or "").strip(),
        "skills": body.get("skills") or [],
        "created_at": _now(),
        "updated_at": _now(),
    }


def _restore_backup_to_vm(backup_path: Path, tenant_id: str) -> None:
    if not backup_path.is_file():
        raise FileNotFoundError(f"backup not found: {backup_path}")
    target_dir = _tenant_dir(tenant_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(backup_path, "rb") as src, (target_dir / "data.ext4").open("wb") as dst:
        shutil.copyfileobj(src, dst)


def _resolve_backup(value) -> Path:
    if isinstance(value, dict):
        tenant_id = value.get("tenant_id")
        timestamp = value.get("timestamp")
        if not tenant_id:
            raise ValueError("restore_from.tenant_id is required")
        candidates = sorted((BACKUPS_DIR / tenant_id).glob("*.gz"))
        if timestamp:
            candidates = [p for p in candidates if p.stem == timestamp]
        if not candidates:
            raise FileNotFoundError("no matching local backup found")
        return candidates[-1]
    if isinstance(value, str) and value:
        path = Path(value)
        return path if path.is_absolute() else BACKUPS_DIR / path
    raise ValueError("restore_from must be a backup path or object")


def _create_tenant(body: dict) -> dict:
    if "skills" in body and not isinstance(body["skills"], list):
        raise ValueError("skills must be a list")
    with _STATE_LOCK:
        state = _load_state()
        item = _new_tenant(body)
        item["vm_num"] = _allocate_vm_num(state)
        item["guest_ip"] = f"{SUBNET_PREFIX}.{item['vm_num']}.2"
        item["host_port"] = HOST_PORT_BASE + item["vm_num"] - 1
        state["tenants"][item["id"]] = item

        clone_from = body.get("clone_from")
        if clone_from:
            source = state["tenants"].get(str(clone_from))
            if not source:
                raise FileNotFoundError(f"clone source not found: {clone_from}")
            result = _run([
                _script("clone-data.sh"),
                source["id"],
                str(source["vm_num"]),
                item["id"],
                str(item["vm_num"]),
            ], timeout=600)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "clone failed")

        restore_from = body.get("restore_from")
        if restore_from:
            _restore_backup_to_vm(_resolve_backup(restore_from), item["id"])

        _save_state(state)

    try:
        _launch(item)
    except Exception:
        with _STATE_LOCK:
            state = _load_state()
            state["tenants"][item["id"]]["status"] = "launch_failed"
            state["tenants"][item["id"]]["updated_at"] = _now()
            _save_state(state)
        raise
    return _public_tenant(item)


def _curl_fc(sock: Path, method: str, path: str, body: str = "") -> None:
    args = ["curl", "-sf", "--unix-socket", str(sock), "-X", method, f"http://localhost{path}"]
    if body:
        args += ["-H", "Content-Type: application/json", "-d", body]
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def _backup_tenant(item: dict) -> dict:
    tenant_id = item["id"]
    data_file = _tenant_dir(tenant_id) / "data.ext4"
    if not data_file.exists():
        raise FileNotFoundError("tenant data.ext4 not found")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = BACKUPS_DIR / tenant_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{timestamp}.gz"
    sock = _tenant_dir(tenant_id) / "fc.sock"
    if sock.exists():
        _curl_fc(sock, "PATCH", "/vm", '{"state":"Paused"}')
    try:
        with data_file.open("rb") as src, gzip.open(out_file, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
    finally:
        if sock.exists():
            _curl_fc(sock, "PATCH", "/vm", '{"state":"Resumed"}')
    return {"tenant_id": tenant_id, "timestamp": timestamp, "path": str(out_file), "size": out_file.stat().st_size}


def _probe_app(guest_ip: str) -> str:
    for path in ("/api/healthz", "/"):
        try:
            with urllib.request.urlopen(f"http://{guest_ip}:{APP_PORT}{path}", timeout=2) as resp:
                if 200 <= resp.status < 500:
                    return "up"
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
    return "down"


def _firecracker_running(tenant_id: str) -> bool:
    sock = _tenant_dir(tenant_id) / "fc.sock"
    result = subprocess.run(["pgrep", "-f", f"api-sock {sock}"], capture_output=True, text=True)
    return result.returncode == 0


def _health_loop() -> None:
    while True:
        try:
            with _STATE_LOCK:
                state = _load_state()
                changed = False
                for item in state.get("tenants", {}).values():
                    if item.get("status") in {"deleted"}:
                        continue
                    fc_up = _firecracker_running(item["id"])
                    app = _probe_app(item["guest_ip"]) if fc_up else "down"
                    status = item.get("status")
                    if fc_up and app == "up" and status in {"creating", "starting", "launch_failed"}:
                        status = "running"
                    elif not fc_up and status == "running":
                        status = "stopped"
                    new_values = {
                        "vm_health": "up" if fc_up else "down",
                        "app_health": app,
                        "status": status,
                        "last_health_check": _now(),
                    }
                    for key, value in new_values.items():
                        if item.get(key) != value:
                            item[key] = value
                            changed = True
                if changed:
                    _save_state(state)
        except Exception as exc:
            print(f"[hetzner-api] health loop error: {exc}", flush=True)
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    server_version = "SwarmClawHetzner/0.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[hetzner-api] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, payload: dict | list | str) -> None:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        body = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _authorized(self) -> bool:
        key = _read_api_key()
        return not key or self.headers.get("x-api-key") == key

    def _require_auth(self) -> bool:
        if self.path == "/health":
            return True
        if self._authorized():
            return True
        self._send(401, {"error": "missing or invalid x-api-key"})
        return False

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        try:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            with _STATE_LOCK:
                state = _load_state()
                if self.path == "/health":
                    self._send(200, {"ok": True})
                elif parts == ["tenants"]:
                    self._send(200, [_public_tenant(t) for t in state["tenants"].values()])
                elif len(parts) == 2 and parts[0] == "tenants":
                    item = state["tenants"].get(parts[1])
                    self._send(200, _public_tenant(item)) if item else self._send(404, {"error": "tenant not found"})
                elif parts == ["backups"]:
                    backups = []
                    for path in sorted(BACKUPS_DIR.glob("*/*.gz")):
                        backups.append({"tenant_id": path.parent.name, "timestamp": path.stem, "path": str(path), "size": path.stat().st_size})
                    self._send(200, backups)
                else:
                    self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        try:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            body = self._body()
            if parts == ["tenants"]:
                self._send(201, _create_tenant(body))
                return
            if len(parts) < 3 or parts[0] != "tenants":
                self._send(404, {"error": "not found"})
                return
            tenant_id, action = parts[1], parts[2]
            with _STATE_LOCK:
                state = _load_state()
                item = state["tenants"].get(tenant_id)
                if not item:
                    self._send(404, {"error": "tenant not found"})
                    return
            if action == "stop":
                _stop(item)
                item["status"] = "stopped"
            elif action == "start":
                item["status"] = "starting"
                _launch(item)
            elif action == "restart":
                _stop(item)
                item["status"] = "starting"
                _launch(item)
            elif action == "backup":
                self._send(202, _backup_tenant(item))
                return
            elif action == "resize-disk":
                new_size = int(body.get("data_disk_mb") or body.get("new_size_mb") or 0)
                if new_size <= 0:
                    raise ValueError("data_disk_mb is required")
                result = _run([_script("resize-disk.sh"), item["id"], str(item["vm_num"]), str(new_size)], timeout=300)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "resize failed")
                item["data_disk_mb"] = new_size
            elif action == "clone":
                clone_body = dict(body)
                clone_body["clone_from"] = tenant_id
                self._send(201, _create_tenant(clone_body))
                return
            else:
                self._send(404, {"error": "unknown action"})
                return
            item["updated_at"] = _now()
            with _STATE_LOCK:
                state = _load_state()
                state["tenants"][tenant_id] = item
                _save_state(state)
            self._send(202, _public_tenant(item))
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def do_DELETE(self) -> None:
        if not self._require_auth():
            return
        try:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            keep_data = "keep_data=true" in self.path
            if len(parts) != 2 or parts[0] != "tenants":
                self._send(404, {"error": "not found"})
                return
            tenant_id = parts[1]
            with _STATE_LOCK:
                state = _load_state()
                item = state["tenants"].get(tenant_id)
            if not item:
                self._send(404, {"error": "tenant not found"})
                return
            try:
                _stop(item)
            except Exception:
                pass
            if not keep_data:
                shutil.rmtree(_tenant_dir(tenant_id), ignore_errors=True)
            with _STATE_LOCK:
                state = _load_state()
                state["tenants"].pop(tenant_id, None)
                _save_state(state)
            self._send(200, {"deleted": tenant_id, "keep_data": keep_data})
        except Exception as exc:
            self._send(500, {"error": str(exc)})


def main() -> None:
    _ensure_state()
    threading.Thread(target=_health_loop, daemon=True).start()
    host = os.environ.get("SCF_API_HOST", "0.0.0.0")
    port = int(os.environ.get("SCF_API_PORT", "8080"))
    print(f"[hetzner-api] listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
