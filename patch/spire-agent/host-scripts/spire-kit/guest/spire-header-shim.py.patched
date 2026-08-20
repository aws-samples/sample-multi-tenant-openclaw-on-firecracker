#!/usr/bin/env python3
"""spire-header-shim —— guest 内本地反代,把 JWT-SVID 盖到出网请求上(零应用代码改动)

它解决什么
----------
客户方案要 OpenClaw 自己内嵌 spiffe-client 去取 JWT-SVID 并注入
`X-SPIFFE-JWT-SVID` —— 那是改上游应用代码。本 shim 换个位置做同一件事:

    OpenClaw ──► 127.0.0.1:18888(本 shim)──► 真网关
                     │
                     └─ 从 SPIRE Workload API(agent.sock)取 JWT-SVID,
                        按 exp 提前刷新,注入 header 后原样转发

OpenClaw 侧只需要把"网关地址"这一个**配置值**指向 shim —— 那个值本来就是平台注入的,
所以应用代码零改动。SVID 仍然由本 VM 自己持有(per-VM 属性不丢),不是 host 代签。

两种转发路径都覆盖
------------------
* 普通 HTTP:整请求解析后用 http.client 转发(Content-Length / chunked 都按标准处理),
  逐个请求注入 header(keep-alive 连接上的第 2、3 个请求也有)。
* WebSocket(`Upgrade: websocket`):注入 header 后转成**裸字节双向管道**,不碰帧内容
  —— wss 长连不会被打断。

失败语义(可配)
----------------
`--on-missing forward`(默认):取不到 SVID 时**照常转发但不带 header**,并大声记日志。
理由与 kit 的一贯原则一致:身份层不能成为新的可用性悬崖(agent 挂了 OpenClaw 照常服务)。
要"没身份就别出网",用 `--on-missing reject`(返回 503),这是 fail-closed 档。
两档都不静默:每次都留结构化日志。

诚实边界
--------
* shim 与 OpenClaw 同 VM 同 uid,拿得到 SVID 的人也拿得到 shim —— 它不提供额外的越权防护,
  它只负责"把已经签发给本 VM 的身份带上"。
* Firecracker 无 vTPM 这条不变:租户能读走 SVID(见 kit README 的边界一节)。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_LISTEN = "127.0.0.1:18888"
DEFAULT_HEADER = "X-SPIFFE-JWT-SVID"
DEFAULT_AUDIENCE = "bgw"
DEFAULT_SOCKET = "/run/spire/agent.sock"
DEFAULT_AGENT_BIN = "/usr/local/bin/spire-agent"

# 逐跳头:不能原样转给上游(RFC 7230 §6.1)
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def build_ssl_context(cfg) -> ssl.SSLContext:
    """上游 TLS 上下文:默认完整校验;私有 CA 走 --upstream-ca;insecure 只在联调用且大声告警。"""
    ctx = ssl.create_default_context(cafile=cfg.upstream_ca or None)
    if cfg.upstream_insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def log(event: str, level: str = "info", **fields) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "event": event}
    rec.update(fields)
    print(json.dumps(rec, sort_keys=True, ensure_ascii=False), flush=True)


def jwt_exp(token: str) -> int | None:
    """只读 JWT payload 的 exp(不验签 —— 验签是网关的事,这里只为定刷新时机)。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:  # noqa: BLE001
        return None


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


class SvidSource:
    """从 SPIRE Workload API 取 JWT-SVID,带 exp-skew 缓存与并发保护。

    用 `spire-agent api fetch jwt` 而不是自己实现 gRPC:kit 只用标准库,不引依赖;
    agent 二进制本来就在 guest 里。
    """

    def __init__(self, cfg: argparse.Namespace) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._token: str | None = None
        self._exp: int = 0

    def _fetch(self) -> str:
        cmd = [self.cfg.agent_bin, "api", "fetch", "jwt",
               "-audience", self.cfg.audience, "-socketPath", self.cfg.socket_path]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=self.cfg.fetch_timeout)
        if out.returncode != 0:
            raise RuntimeError(f"fetch jwt rc={out.returncode} stderr={out.stderr.strip()[:200]}")
        for line in out.stdout.splitlines():
            candidate = line.strip()
            if candidate.startswith("eyJ") and candidate.count(".") == 2:
                return candidate
        raise RuntimeError(f"no JWT in output: {out.stdout.strip()[:200]}")

    def token(self) -> str | None:
        """返回可用 token;取不到返回 None(由调用方按 --on-missing 决定行为)。"""
        now = time.time()
        with self._lock:
            if self._token and now < self._exp - self.cfg.refresh_skew:
                return self._token
            try:
                token = self._fetch()
            except Exception as exc:  # noqa: BLE001
                log("svid_fetch_failed", level="warn", error=repr(exc),
                    has_cached=bool(self._token), cached_exp=self._exp)
                # 旧 token 还没过期就继续用(网关自己会验 exp,这里不冒险用过期的)
                if self._token and now < self._exp:
                    return self._token
                self._token = None
                self._exp = 0
                return None
            exp = jwt_exp(token)
            if exp is None:
                log("svid_exp_unparsable", level="warn", token_fingerprint=fingerprint(token))
                exp = int(now + self.cfg.fallback_ttl)
            self._token = token
            self._exp = exp
            log("svid_refreshed", token_fingerprint=fingerprint(token), exp=exp,
                ttl_left=int(exp - now), audience=self.cfg.audience)
            return token


class ShimHandler(BaseHTTPRequestHandler):
    server_version = "spire-header-shim/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 统一走结构化日志
        return

    # ── 公共入口 ──────────────────────────────────────────────────────────────
    def _cfg(self) -> argparse.Namespace:
        return self.server.cfg  # type: ignore[attr-defined]

    def _svid(self) -> SvidSource:
        return self.server.svid  # type: ignore[attr-defined]

    def handle_one_request(self) -> None:
        # 拿到 header 后先判 upgrade;WebSocket 走裸管道,不能交给 BaseHTTPRequestHandler
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _dispatch(self) -> None:
        cfg = self._cfg()
        token = self._svid().token()
        if token is None and cfg.on_missing == "reject":
            log("rejected_no_svid", level="warn", path=self.path, status=cfg.reject_status)
            self.send_response(cfg.reject_status)
            self.send_header("Content-Type", "application/json")
            body = b'{"error":"no_svid","hint":"spire-agent not ready"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            self._tunnel_upgrade(token)
        else:
            self._proxy_http(token)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

    # ── 注入 header ───────────────────────────────────────────────────────────
    def _outbound_headers(self, token: str | None) -> list[tuple[str, str]]:
        cfg = self._cfg()
        out: list[tuple[str, str]] = []
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() == cfg.header.lower():
                continue  # 客户端自己带的同名 header 一律剥掉,不许伪造身份
            out.append((key, value))
        if token is not None:
            out.append((cfg.header, f"{cfg.header_value_prefix}{token}"))
        return out

    # ── 普通 HTTP ─────────────────────────────────────────────────────────────
    def _proxy_http(self, token: str | None) -> None:
        cfg = self._cfg()
        body = b""
        length = self.headers.get("Content-Length")
        if length:
            body = self.rfile.read(int(length))
        elif (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = self._read_chunked()
        conn_cls = HTTPSConnection if cfg.upstream_tls else HTTPConnection
        kwargs = {"timeout": cfg.upstream_timeout}
        if cfg.upstream_tls:
            kwargs["context"] = build_ssl_context(cfg)
        conn = conn_cls(cfg.upstream_host, cfg.upstream_port, **kwargs)  # type: ignore[arg-type]
        try:
            headers = dict(self._outbound_headers(token))
            headers["Host"] = cfg.upstream_host_header
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            log("proxied", method=self.command, path=self.path, status=resp.status,
                svid=bool(token), token_fingerprint=fingerprint(token) if token else None,
                bytes_out=len(payload))
        except Exception as exc:  # noqa: BLE001
            log("upstream_failed", level="error", method=self.command, path=self.path, error=repr(exc))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
        finally:
            conn.close()

    def _read_chunked(self) -> bytes:
        chunks = []
        while True:
            line = self.rfile.readline().strip()
            if not line:
                break
            size = int(line.split(b";")[0], 16)
            if size == 0:
                self.rfile.readline()
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        return b"".join(chunks)

    # ── WebSocket:注入后转裸管道 ─────────────────────────────────────────────
    def _tunnel_upgrade(self, token: str | None) -> None:
        cfg = self._cfg()
        head = [f"{self.command} {self.path} HTTP/1.1"]
        for key, value in self.headers.items():
            if key.lower() == cfg.header.lower() or key.lower() == "host":
                continue
            head.append(f"{key}: {value}")
        head.append(f"Host: {cfg.upstream_host_header}")
        if token is not None:
            head.append(f"{cfg.header}: {cfg.header_value_prefix}{token}")
        raw = ("\r\n".join(head) + "\r\n\r\n").encode()
        try:
            upstream = socket.create_connection((cfg.upstream_host, cfg.upstream_port), cfg.upstream_timeout)
            if cfg.upstream_tls:
                upstream = build_ssl_context(cfg).wrap_socket(
                    upstream, server_hostname=cfg.upstream_host_header)
            upstream.sendall(raw)
        except Exception as exc:  # noqa: BLE001
            log("upgrade_connect_failed", level="error", path=self.path, error=repr(exc))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        log("upgrade_tunneled", path=self.path, svid=bool(token),
            token_fingerprint=fingerprint(token) if token else None)
        self.close_connection = True
        client = self.connection
        self._pipe(client, upstream)

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        socks = [a, b]
        try:
            while True:
                readable, _, errored = select.select(socks, [], socks, 300)
                if errored or not readable:
                    break
                for src in readable:
                    dst = b if src is a else a
                    try:
                        data = src.recv(65536)
                    except (ConnectionResetError, ssl.SSLError, OSError):
                        return
                    if not data:
                        return
                    try:
                        dst.sendall(data)
                    except (BrokenPipeError, OSError):
                        return
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass


class ShimServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(description="guest 内本地反代:注入 JWT-SVID header")
    p.add_argument("--listen", default=env("SPIRE_SHIM_LISTEN", DEFAULT_LISTEN),
                   help="host:port,默认只听 127.0.0.1(不对外暴露)")
    p.add_argument("--upstream", default=env("SPIRE_SHIM_UPSTREAM", ""),
                   help="真网关,如 https://gw.example.com:443 或 http://10.0.0.1:8080(必填)")
    p.add_argument("--upstream-host-header", default=env("SPIRE_SHIM_UPSTREAM_HOST_HEADER", ""),
                   help="转发时用的 Host 头,默认取 --upstream 的主机名")
    p.add_argument("--upstream-ca", default=env("SPIRE_SHIM_UPSTREAM_CA", ""),
                   help="上游用私有 CA 时把 CA 证书路径给这里 —— 这是私有证书的**正确**做法,"
                        "不要用 --upstream-insecure")
    p.add_argument("--upstream-insecure", action="store_true",
                   default=env("SPIRE_SHIM_UPSTREAM_INSECURE", "") == "1",
                   help="跳过上游 TLS 校验。**只允许联调用**:关掉校验等于允许中间人。"
                        "生产用 --upstream-ca 把私有 CA 加进信任链")
    p.add_argument("--upstream-timeout", type=float, default=float(env("SPIRE_SHIM_UPSTREAM_TIMEOUT", "30")))
    p.add_argument("--header", default=env("SPIRE_SHIM_HEADER", DEFAULT_HEADER))
    p.add_argument("--header-value-prefix", default=env("SPIRE_SHIM_HEADER_PREFIX", ""),
                   help='如网关要 "Bearer <jwt>" 就设成 "Bearer "')
    p.add_argument("--audience", default=env("SPIRE_SHIM_AUDIENCE", DEFAULT_AUDIENCE))
    p.add_argument("--socket-path", default=env("SPIRE_SHIM_SOCKET", DEFAULT_SOCKET))
    p.add_argument("--agent-bin", default=env("SPIRE_SHIM_AGENT_BIN", DEFAULT_AGENT_BIN))
    p.add_argument("--refresh-skew", type=int, default=int(env("SPIRE_SHIM_REFRESH_SKEW", "60")),
                   help="提前多少秒刷新(默认 60;SVID TTL 常见 300s)")
    p.add_argument("--fallback-ttl", type=int, default=int(env("SPIRE_SHIM_FALLBACK_TTL", "60")),
                   help="JWT 里读不到 exp 时的保守缓存秒数")
    p.add_argument("--fetch-timeout", type=float, default=float(env("SPIRE_SHIM_FETCH_TIMEOUT", "5")))
    p.add_argument("--on-missing", choices=("forward", "reject"),
                   default=env("SPIRE_SHIM_ON_MISSING", "forward"),
                   help="取不到 SVID:forward=照常转发不带 header(默认,不制造可用性悬崖);reject=返 503")
    p.add_argument("--reject-status", type=int, default=int(env("SPIRE_SHIM_REJECT_STATUS", "503")))
    p.add_argument("--print-config", action="store_true")
    return p


def finalize(cfg: argparse.Namespace) -> argparse.Namespace:
    if not cfg.upstream:
        raise SystemExit("--upstream 必填(如 https://gw.example.com)")
    u = urllib.parse.urlparse(cfg.upstream if "//" in cfg.upstream else f"//{cfg.upstream}", scheme="http")
    cfg.upstream_tls = u.scheme in ("https", "wss")
    cfg.upstream_host = u.hostname or ""
    cfg.upstream_port = u.port or (443 if cfg.upstream_tls else 80)
    if not cfg.upstream_host:
        raise SystemExit(f"--upstream 解析不出主机名: {cfg.upstream}")
    cfg.upstream_host_header = cfg.upstream_host_header or cfg.upstream_host
    host, _, port = cfg.listen.rpartition(":")
    cfg.listen_host = host or "127.0.0.1"
    cfg.listen_port = int(port)
    return cfg


def main(argv: list[str] | None = None) -> int:
    cfg = build_parser().parse_args(argv)
    if cfg.print_config and not cfg.upstream:
        cfg.upstream = "http://placeholder"  # 只为打印默认值
    cfg = finalize(cfg)
    if cfg.print_config:
        print(json.dumps({k: v for k, v in vars(cfg).items()}, sort_keys=True, indent=2))
        return 0
    server = ShimServer((cfg.listen_host, cfg.listen_port), ShimHandler)
    server.cfg = cfg  # type: ignore[attr-defined]
    server.svid = SvidSource(cfg)  # type: ignore[attr-defined]
    if cfg.upstream_insecure:
        log("upstream_tls_verification_disabled", level="error",
            note="--upstream-insecure 只允许联调:等于允许中间人。生产请改用 --upstream-ca")
    log("listening", listen=f"{cfg.listen_host}:{cfg.listen_port}",
        upstream=f"{'https' if cfg.upstream_tls else 'http'}://{cfg.upstream_host}:{cfg.upstream_port}",
        header=cfg.header, audience=cfg.audience, on_missing=cfg.on_missing, pid=os.getpid())
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("shutdown")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
