#!/usr/bin/env python3
"""registrar-mock-customer.py —— 按【客户 2026-08-20 定稿接口】逐字实现的 mock

与 registrar-stub.py 的分工:
  * stub 是早期平层契约(顶层 {"join_token",...}),供 kit 自己的链路验收;
  * 本 mock 是客户 join-token API server 的替身,响应带信封
    {"code": 200, "data": {...}} —— broker `http` 后端对接客户前,用它验
    "broker 发什么、怎么解客户的响应"这一跳。字段名、包裹层级照客户文档,不做扩展。

客户接口(原文):
    3.1 POST /v1/entry/register
        Header: X-Host-Identity: <EC2 IID pkcs7>   # 第一版仅 log,未来对接 AWS 校验
        Body:   {"tenant_id": "t-xxx", "workload_uid": 1000}
        200:    {"code": 200, "data": {"join_token": "...", "ttl": 600,
                 "spiffe_id": "spiffe://test.main.byai.io/openclaw/t-xxx"}}
    3.2 POST /v1/entry/evict
        Body:   {"tenant_id": "t-xxx"}
        200:    {"code": 200, "data": {"evicted": true}}

仅测试用(假 token),必须 SPIRE_KIT_ALLOW_STUB=1 才起——与 stub 同一道闸,防止
它被误当生产 registrar。为联调多给了两个可选行为开关(客户服务端没有,纯为测 broker
的容错):
    --deny            register 一律返回信封 {"code": 403, ...}(HTTP 仍 200)——
                      验 broker 把信封非 200 当明确拒绝、不重试
    --require-identity  缺 X-Host-Identity 头返回 HTTP 400 —— 验 broker fail-closed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(event: str, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    rec.update(fields)
    print(json.dumps(rec, sort_keys=True), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "customer-registrar-mock/1.0"

    def log_message(self, fmt, *args):
        return

    def _reply(self, http_code: int, payload: dict):
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(http_code)
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
            self._reply(400, {"code": 400, "message": "bad_json"})
            return

        # 客户第一版行为:X-Host-Identity 仅 log(记指纹,不落原文——pkcs7 上 KB 级)
        identity = self.headers.get("X-Host-Identity") or ""
        if cfg.require_identity and not identity:
            self._reply(400, {"code": 400, "message": "missing X-Host-Identity"})
            return
        id_fp = hashlib.sha256(identity.encode()).hexdigest()[:16] if identity else ""

        tenant_id = str(req.get("tenant_id") or "")
        if not tenant_id:
            self._reply(400, {"code": 400, "message": "tenant_id required"})
            return

        if self.path == "/v1/entry/register":
            if cfg.deny:
                log("register_denied", tenant_id=tenant_id, identity_fp=id_fp)
                self._reply(200, {"code": 403, "message": "denied by policy"})
                return
            token = str(uuid.uuid4())
            log("register", tenant_id=tenant_id,
                workload_uid=req.get("workload_uid"),
                identity_fp=id_fp, identity_bytes=len(identity),
                idempotency_key=self.headers.get("Idempotency-Key") or "",
                token_fp=hashlib.sha256(token.encode()).hexdigest()[:8])
            self._reply(200, {
                "code": 200,
                "data": {
                    "join_token": token,
                    "ttl": cfg.ttl,
                    "spiffe_id": f"spiffe://{cfg.trust_domain}/openclaw/{tenant_id}",
                },
            })
            return

        if self.path == "/v1/entry/evict":
            log("evict", tenant_id=tenant_id, identity_fp=id_fp)
            self._reply(200, {"code": 200, "data": {"evicted": True}})
            return

        self._reply(404, {"code": 404, "message": "not_found"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9090)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--ttl", type=int, default=600)
    ap.add_argument("--trust-domain", default="test.main.byai.io")
    ap.add_argument("--deny", action="store_true")
    ap.add_argument("--require-identity", dest="require_identity", action="store_true")
    cfg = ap.parse_args()

    if os.environ.get("SPIRE_KIT_ALLOW_STUB") != "1":
        print("拒绝启动:这是测试 mock(发假 token),必须 SPIRE_KIT_ALLOW_STUB=1", file=sys.stderr)
        return 1

    srv = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    srv.cfg = cfg  # type: ignore[attr-defined]
    log("mock_started", bind=cfg.bind, port=cfg.port, deny=cfg.deny,
        require_identity=cfg.require_identity, trust_domain=cfg.trust_domain)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
