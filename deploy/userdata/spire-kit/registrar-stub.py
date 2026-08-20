#!/usr/bin/env python3
"""registrar-stub.py —— Entry Registrar 契约的最小实现(联调 / 验收 / 无 EKS 时用)

契约与《launch-vm.sh SPIRE 改造说明》一致,broker 的 `http` 后端可以直接指过来:

    POST /v1/entry/register  {"tenant_id": "...", "workload_uid": 1000}
      → {"join_token","ttl","node_entry_id","workload_entry_id","spiffe_id"}
    POST /v1/entry/evict     {"tenant_id": "..."} → {"evicted": true}

两种模式:
  --mode spire  真调本机 spire-server(token generate + entry create + agent evict)
  --mode fake   只发假 token,**必须** SPIRE_KIT_ALLOW_STUB=1,用于纯契约验收

这不是生产组件:生产用客户自己的 registrar(EKS 里那个)。放在 kit 里是为了让
"没有 registrar 也能把 host↔guest 这段链路验到底"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(event: str, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    rec.update(fields)
    print(json.dumps(rec, sort_keys=True), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "spire-kit-registrar-stub/1.0"

    def log_message(self, fmt, *args):
        return

    def _reply(self, code: int, payload: dict):
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        cfg = self.server.cfg  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            self._reply(400, {"error": "bad_json"})
            return
        tenant_id = str(req.get("tenant_id") or "")
        if not tenant_id:
            self._reply(400, {"error": "tenant_id_required"})
            return
        # 生产 registrar 必须校验 X-Host-Identity(EC2 IID);stub 只记录它在不在,
        # 免得验收时误以为"认证已经验过了"。
        has_iid = "X-Host-Identity" in self.headers
        node_id = f"spiffe://{cfg.trust_domain}/node/{tenant_id}"
        workload_id = f"spiffe://{cfg.trust_domain}/openclaw/{tenant_id}"

        if self.path == "/v1/entry/register":
            try:
                token = self._issue(cfg, tenant_id, node_id, workload_id)
            except Exception as exc:  # noqa: BLE001
                log("register_failed", tenant_id=tenant_id, error=repr(exc))
                self._reply(503, {"error": "issue_failed", "detail": str(exc)[:200]})
                return
            log("registered", tenant_id=tenant_id, mode=cfg.mode, host_identity_present=has_iid,
                token_fingerprint=hashlib.sha256(token.encode()).hexdigest()[:8])
            self._reply(200, {
                "join_token": token,
                "ttl": cfg.ttl,
                "node_entry_id": f"node-{tenant_id}",
                "workload_entry_id": f"wl-{tenant_id}",
                "spiffe_id": workload_id,
                "node_spiffe_id": node_id,
            })
            return

        if self.path == "/v1/entry/evict":
            if cfg.mode == "spire":
                subprocess.run(
                    [cfg.spire_server_bin, "agent", "evict", "-spiffeID", node_id,
                     "-socketPath", cfg.spire_server_socket],
                    capture_output=True, text=True, timeout=10,
                )
            log("evicted", tenant_id=tenant_id, mode=cfg.mode)
            self._reply(200, {"evicted": True})
            return

        self._reply(404, {"error": "not_found"})

    def _issue(self, cfg, tenant_id: str, node_id: str, workload_id: str) -> str:
        if cfg.mode == "fake":
            digest = hashlib.sha256(f"{tenant_id}:{time.time_ns()}".encode()).hexdigest()
            return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
        gen = subprocess.run(
            [cfg.spire_server_bin, "token", "generate", "-spiffeID", node_id,
             "-ttl", str(cfg.ttl), "-socketPath", cfg.spire_server_socket],
            capture_output=True, text=True, timeout=15,
        )
        if gen.returncode != 0:
            raise RuntimeError(f"token generate rc={gen.returncode}: {gen.stderr.strip()[:200]}")
        token = ""
        for line in gen.stdout.splitlines():
            if line.lower().startswith("token:"):
                token = line.split(":", 1)[1].strip()
        if not token:
            raise RuntimeError(f"no token in output: {gen.stdout.strip()[:200]}")
        ent = subprocess.run(
            [cfg.spire_server_bin, "entry", "create", "-parentID", node_id, "-spiffeID", workload_id,
             "-selector", f"unix:uid:{cfg.workload_uid}", "-socketPath", cfg.spire_server_socket],
            capture_output=True, text=True, timeout=15,
        )
        if ent.returncode != 0 and "already exists" not in (ent.stdout + ent.stderr).lower():
            raise RuntimeError(f"entry create rc={ent.returncode}: {ent.stderr.strip()[:200]}")
        return token


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--mode", choices=("spire", "fake"), default="spire")
    p.add_argument("--trust-domain", default="a1.1o")
    p.add_argument("--ttl", type=int, default=600)
    p.add_argument("--workload-uid", type=int, default=1000)
    p.add_argument("--spire-server-bin", default="/usr/local/bin/spire-server")
    p.add_argument("--spire-server-socket", default="/tmp/spire-server/private/api.sock")
    cfg = p.parse_args(argv)
    if cfg.mode == "fake" and os.environ.get("SPIRE_KIT_ALLOW_STUB") != "1":
        raise SystemExit("--mode fake 需要 SPIRE_KIT_ALLOW_STUB=1(仅测试用)")
    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.cfg = cfg  # type: ignore[attr-defined]
    log("listening", bind=cfg.bind, port=cfg.port, mode=cfg.mode, trust_domain=cfg.trust_domain)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
