#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Host-side routing operations for the P2 two-tier data plane.

Contract: engineering/00-knowledge-base/SPEC/11-ENGINE-TRANSFORM/INTERFACE-CONTRACT.md
    §1 Redis key schema (`route:{tenant_id}` -> JSON host/port/guest_ip/updated_at)
    §3 Port bitmap [10000, dnat_port_high] + iptables DNAT (atomic three-step)
    §4 DDB descriptor fields (host_private_ip, host_port, guest_ip)
    §6 HA self-recovery (Redis write fails degrade, DDB is authority)
    §8 Redis failover (primary endpoint, DNS TTL re-resolve, no infinite pool)

Kept as a thin module: pure functions where possible, one class per stateful
concern, small enough to reason about. host-agent.py wires it in at promotion.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import threading
import time

# ─── Public constants (contract §3) ─────────────────────────────
# 上界读运行环境注入值(init-host.sh 写 /etc/platform.env,值与 CDK 渲染 SG
# 所用 edge.dnat_port_high 同源,见 R1)。SG 上界跟 config 走而位图是死值,
# config 一抬高两边即裂开(静默失败:SG 放行到 15000,位图只肯分到旧值)。
# 缺省回退 15000(5001 槽),与 config.yml.example 默认同步。
PORT_RANGE_LOW = int(os.environ.get("DNAT_PORT_LOW", "10000"))
PORT_RANGE_HIGH = int(os.environ.get("DNAT_PORT_HIGH", "15000"))  # inclusive
GATEWAY_GUEST_PORT = 18789  # OpenClaw gateway inside microVM (launch-vm.sh:747)

# ─── R5.3 端口 quarantine (冷却期) ───
# 端口 release 后进冷却期,冷却期内 SHALL NOT 被 alloc 复用;防"旧 DNAT 未清 +
# 端口立即复用 → 残留在途流量打到新租户"(见 spec R5.3)。冷却期需 ≥ 迁移
# drain 窗口 + 安全余量,让内核 conntrack 表条目也过期。
#
# 默认 20s = MIGRATION_DRAIN_SECONDS(5s) * 2 + 10s 安全余量;可经 env
# PORT_QUARANTINE_SECONDS 覆盖(config.yml → edge.port_quarantine_seconds
# → init-host.sh 写 /etc/platform.env)。0 = 关(存量回退,不建议)。
#
# 跨进程可见:host-agent 与 route_ops CLI (由 SSM release-route 触发)
# 都会写读同一个 JSON 文件,配 fcntl 建议锁保证并发一致。重启存活。
PORT_QUARANTINE_SECONDS = int(os.environ.get("PORT_QUARANTINE_SECONDS", "20"))
PORT_QUARANTINE_FILE = os.environ.get(
    "PORT_QUARANTINE_FILE", "/data/openclaw/port-quarantine.json"
)
_quarantine_file_lock = threading.Lock()  # 本进程内序列化,配文件锁跨进程


# ─── R5.3 端口 quarantine 持久化 ─────────────────────────────────
def _quarantine_load_and_prune(now_ts: float | None = None) -> dict[int, float]:
    """读 quarantine 文件,过滤掉已过冷却期的端口,返回 {port: release_ts}。
    IO 失败(文件缺失/坏 JSON)按空 map 处理(fail-open:quarantine 是加固层,
    读不到只回退到无冷却态,不阻塞 alloc/release 主路径)。调用方持文件锁。"""
    now_ts = time.time() if now_ts is None else now_ts
    try:
        with open(PORT_QUARANTINE_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    live: dict[int, float] = {}
    for k, v in raw.items():
        try:
            port = int(k)
            ts = float(v)
        except (TypeError, ValueError):
            continue
        if now_ts - ts < PORT_QUARANTINE_SECONDS:
            live[port] = ts
    return live


def _quarantine_write(state: dict[int, float]) -> None:
    """原子写:tmp 文件 + rename。调用方持文件锁。空 map 也写(表示已清空)。"""
    try:
        os.makedirs(os.path.dirname(PORT_QUARANTINE_FILE), exist_ok=True)
    except OSError:
        pass  # 目录建不出,rename 会 fail-loud
    tmp = f"{PORT_QUARANTINE_FILE}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in state.items()}, fh)
        os.rename(tmp, PORT_QUARANTINE_FILE)
    except OSError as e:
        # fail-loud:quarantine 是 no-cross-tenant 底线,写不进能被察觉。
        print(f"quarantine write failed: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _quarantine_with_lock(fn):
    """封装:开(创)锁文件 → LOCK_EX → 调 fn(state) → 关。fn 返回新 state 就写回,
    返回 None 就不写(纯读)。IO 失败降级为无 quarantine 行为。"""
    if PORT_QUARANTINE_SECONDS <= 0:
        return fn({}) if callable(fn) else None
    lock_path = PORT_QUARANTINE_FILE + ".lock"
    with _quarantine_file_lock:
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        except OSError:
            pass
        try:
            fh = open(lock_path, "a+")
        except OSError:
            # 目录不可写(非 host 环境如本地测试)静默 fail-open:quarantine 是
            # 加固层,拿不到锁文件就退回无冷却态,不干扰 stdout(下游断言吃 OK 行)。
            return fn({})
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # 无 flock 支持(极少见),退化为无锁
            state = _quarantine_load_and_prune()
            new_state = fn(state)
            if new_state is not None:
                _quarantine_write(new_state)
            return new_state if new_state is not None else state
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


def add_quarantine(port: int) -> None:
    """port 释放后压 quarantine,冷却期内不再被 alloc(R5.3)。"""
    if PORT_QUARANTINE_SECONDS <= 0:
        return
    now = time.time()

    def _add(state):
        state[int(port)] = now
        return state

    _quarantine_with_lock(_add)


def get_quarantined_ports() -> set[int]:
    """当前仍在冷却期的端口集合。alloc 时用来把这些端口从可用集合排除。"""
    if PORT_QUARANTINE_SECONDS <= 0:
        return set()
    state = _quarantine_with_lock(lambda s: None)  # 纯读 + prune
    return set((state or {}).keys())


# ─── Port bitmap ────────────────────────────────────────────────
class PortAllocationError(RuntimeError):
    """Raised when the bitmap cannot satisfy an alloc (exhausted or race)."""


class PortBitmap:
    """Local bookkeeping for host-side DNAT ingress ports [low, high].

    Thread-safe. Not persisted across restarts — reconstruct with
    `mark_used()` from the live `iptables -t nat -L PREROUTING` at boot,
    so host-agent recovers from crash without leaking or reusing ports.

    Contract §3: atomic alloc + iptables check + write DNAT is enforced
    by the caller holding `alloc_lock` across the three steps, not here.
    """

    def __init__(self, low: int = PORT_RANGE_LOW, high: int = PORT_RANGE_HIGH) -> None:
        if not (0 < low <= high < 65536):
            raise ValueError(f"invalid port range [{low}, {high}]")
        self._low = low
        self._high = high
        self._used: set[int] = set()
        self._lock = threading.Lock()

    def alloc(self) -> int:
        """Return the smallest free port in the range. O(n) worst case;
        n<=401 so unmeasurably cheap. Raises on exhaustion (fail-loud).

        R5.3:跳过 quarantine 冷却期内的端口(见 add_quarantine),防迁移
        release 后端口立即被复用把残留在途流量打到新租户。冷却期端口视作已用,
        alloc 找下一个空闲槽。"""
        quarantined = get_quarantined_ports()
        with self._lock:
            for port in range(self._low, self._high + 1):
                if port in self._used or port in quarantined:
                    continue
                self._used.add(port)
                return port
            raise PortAllocationError(
                f"port bitmap exhausted (range [{self._low}, {self._high}], "
                f"in_quarantine={len(quarantined)})"
            )

    def free(self, port: int) -> None:
        """Idempotent release. Silent no-op if port never held; symmetric
        with iptables `-D` (also idempotent-friendly)."""
        with self._lock:
            self._used.discard(port)

    def mark_used(self, port: int) -> None:
        """Bootstrap recovery: record a port as taken without failing if
        it was already taken (bootstrap is idempotent by nature)."""
        if not (self._low <= port <= self._high):
            return  # outside our range — someone else's rule
        with self._lock:
            self._used.add(port)

    def is_used(self, port: int) -> bool:
        with self._lock:
            return port in self._used

    def used_count(self) -> int:
        with self._lock:
            return len(self._used)

    def snapshot(self) -> set[int]:
        with self._lock:
            return set(self._used)

    def replace_used(self, ports: set[int]) -> None:
        """Replace local state with one authoritative iptables snapshot."""
        with self._lock:
            self._used = {
                port for port in ports if self._low <= port <= self._high
            }


# ─── iptables DNAT ──────────────────────────────────────────────
_IPTABLES_TIMEOUT_SEC = 5


def _run_iptables(args: list[str]) -> subprocess.CompletedProcess:
    """Single choke point for `iptables` invocations. Failures return
    non-zero rc; caller decides fail-loud vs. tolerate.

    Split out so tests can patch one thing, and so `--wait 3` is uniform
    (iptables xt_lock contention is a real thing on busy hosts)."""
    cmd = ["iptables", "--wait", "3", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_IPTABLES_TIMEOUT_SEC,
    )


def dnat_rule_args(host_port: int, guest_ip: str) -> list[str]:
    """Return the argv slice specifying THIS tenant's DNAT rule (chain + match
    + target only, NO `-t nat`). Extracted so add/remove/check share one
    definition — no drift between them.

    `-t nat` must NOT live here: iptables requires the table selector to
    precede the command flag (`iptables -t nat -A …`, not `iptables -A -t
    nat …`). Callers prepend `-t nat` before the `-A/-C/-D` command; see
    _dnat_argv. (Real-host bug: `-A -t nat PREROUTING` made iptables read
    `nat` as the -A chain arg → rc=2 "Bad argument `nat'`", every DNAT add
    failed and no tenant ever promoted onto the two-tier route.)"""
    return [
        "PREROUTING",
        "-p",
        "tcp",
        "--dport",
        str(host_port),
        "-j",
        "DNAT",
        "--to-destination",
        f"{guest_ip}:{GATEWAY_GUEST_PORT}",
    ]


def _dnat_argv(command: str, host_port: int, guest_ip: str) -> list[str]:
    """Full iptables argv with the table selector first: `-t nat <command>
    PREROUTING …`. command is one of -A/-C/-D."""
    return ["-t", "nat", command, *dnat_rule_args(host_port, guest_ip)]


def dnat_check(host_port: int, guest_ip: str) -> bool:
    """`iptables -C` — returns True if the exact rule already exists."""
    r = _run_iptables(_dnat_argv("-C", host_port, guest_ip))
    return r.returncode == 0


def dnat_add(host_port: int, guest_ip: str) -> None:
    """Idempotent add: -C first, -A only on miss. Fail-loud on real error."""
    if dnat_check(host_port, guest_ip):
        return
    r = _run_iptables(_dnat_argv("-A", host_port, guest_ip))
    if r.returncode != 0:
        raise RuntimeError(
            f"iptables DNAT add failed (port={host_port} guest={guest_ip}): "
            f"rc={r.returncode} stderr={r.stderr.strip()!r}"
        )


def dnat_remove(host_port: int, guest_ip: str) -> None:
    """Idempotent remove: absence is success. Fail-loud on real error only."""
    r = _run_iptables(_dnat_argv("-D", host_port, guest_ip))
    if r.returncode == 0:
        return
    # rc=1 with iptables "does a matching rule exist" message is benign here
    if "No chain/target/match by that name" in r.stderr:
        return
    if "does a matching rule exist" in r.stderr.lower():
        return
    raise RuntimeError(
        f"iptables DNAT remove failed (port={host_port} guest={guest_ip}): "
        f"rc={r.returncode} stderr={r.stderr.strip()!r}"
    )


def dnat_remove_all(host_port: int, guest_ip: str) -> None:
    """Remove every duplicate of one exact rule without hiding real errors."""
    while True:
        checked = _run_iptables(_dnat_argv("-C", host_port, guest_ip))
        if checked.returncode != 0:
            missing = checked.returncode == 1 and (
                "No chain/target/match by that name" in checked.stderr
                or "does a matching rule exist" in checked.stderr.lower()
            )
            if missing:
                return
            raise RuntimeError(
                f"iptables DNAT check failed (port={host_port} guest={guest_ip}): "
                f"rc={checked.returncode} stderr={checked.stderr.strip()!r}"
            )
        dnat_remove(host_port, guest_ip)


# Rules our own shape (`iptables -A PREROUTING -p tcp --dport N -j DNAT
# --to-destination IP:18789`) round-trip through `iptables -S` with the
# same `--dport N` / `--to-destination IP:PORT` argv, so a plain scan is
# stable. Note `iptables -L -n` uses `dpt:N` / `to:IP:PORT` instead — we
# stick to `-S` (rule-save) to keep this parser simple.
_LIST_DPORT_RE = re.compile(r"--dport\s+(\d+)")
_LIST_TO_RE = re.compile(r"--to-destination\s+(\d+\.\d+\.\d+\.\d+):(\d+)")


def list_dnat_rules() -> dict[int, str]:
    """Enumerate PREROUTING DNAT rules matching our shape. Returns
    {host_port: guest_ip}. Ignores rules that don't target :18789 — those
    are foreign (e.g. the DNS interception rules in launch-vm.sh:604).

    Boot recovery reads this to rebuild the bitmap without persistence.
    """
    r = _run_iptables(["-t", "nat", "-S", "PREROUTING"])
    if r.returncode != 0:
        raise RuntimeError(
            f"iptables list PREROUTING failed rc={r.returncode} "
            f"stderr={r.stderr.strip()!r}"
        )
    out: dict[int, str] = {}
    for line in r.stdout.splitlines():
        if "DNAT" not in line or f":{GATEWAY_GUEST_PORT}" not in line:
            continue
        port_m = _LIST_DPORT_RE.search(line)
        to_m = _LIST_TO_RE.search(line)
        if not port_m or not to_m:
            continue
        port = int(port_m.group(1))
        guest_ip = to_m.group(1)
        gw_port = int(to_m.group(2))
        if gw_port != GATEWAY_GUEST_PORT:
            continue
        out[port] = guest_ip
    return out


_alloc_lock = threading.Lock()


def _refresh_bitmap_locked(bitmap: PortBitmap) -> dict[int, str]:
    """Replace the bitmap from live rules while the allocation lock is held."""
    rules = list_dnat_rules()
    bitmap.replace_used(
        {
            port
            for port in rules
            if PORT_RANGE_LOW <= port <= PORT_RANGE_HIGH
        }
    )
    return rules


def rebuild_bitmap_from_iptables(bitmap: PortBitmap) -> int:
    """Boot recovery entrypoint. Returns the count of ports marked used."""
    with _alloc_lock:
        rules = _refresh_bitmap_locked(bitmap)
        return sum(
            PORT_RANGE_LOW <= port <= PORT_RANGE_HIGH for port in rules
        )


# ─── Atomic alloc + DNAT ────────────────────────────────────────
def _alloc_and_dnat_locked(bitmap: PortBitmap, guest_ip: str) -> int:
    port = bitmap.alloc()
    try:
        if dnat_check(port, guest_ip):
            raise RuntimeError(
                f"DNAT rule already exists for port={port} guest={guest_ip} "
                "(bitmap out of sync with iptables?)"
            )
        dnat_add(port, guest_ip)
        return port
    except Exception:
        bitmap.free(port)
        raise


def alloc_and_dnat_atomic(bitmap: PortBitmap, guest_ip: str) -> int:
    """Three-step atomic (contract §3): alloc port -> verify not-in-use
    via `iptables -C` -> add DNAT. On any failure, roll back the alloc.

    Serialised globally with `_alloc_lock` so two threads never race for
    the same port even though PortBitmap is itself thread-safe.
    """
    with _alloc_lock:
        return _alloc_and_dnat_locked(bitmap, guest_ip)


def ensure_port_and_dnat(bitmap: PortBitmap, guest_ip: str) -> int:
    """Refresh live state, then reuse or allocate one route atomically."""
    with _alloc_lock:
        rules = _refresh_bitmap_locked(bitmap)
        for port, rule_guest in sorted(rules.items()):
            if (
                rule_guest == guest_ip
                and PORT_RANGE_LOW <= port <= PORT_RANGE_HIGH
            ):
                return port
        return _alloc_and_dnat_locked(bitmap, guest_ip)


def release_port_and_dnat(bitmap: PortBitmap, host_port: int, guest_ip: str) -> None:
    """Symmetric release. iptables first (safe if it fails — port stays
    reserved which is preferable to freeing a port whose rule remained).

    R2.3 reclaim boundary — only DELETE reclaims, STOP never does:
    this function is the single reclaim path (called on tenant delete). The
    stop path (stop-vm.sh) deliberately deletes only the tap link + nginx conf
    and leaves the DNAT rule, port bitmap entry, and `route:{tenant_id}` Redis
    key intact, so a stopped tenant wakes on the SAME host_port with no
    re-alloc (verified: stop-vm.sh touches no iptables/bitmap/Redis). Do NOT
    add reclaim to the stop path — that would strand the route on wake.

    R5.3:release 后压 quarantine (PORT_QUARANTINE_SECONDS 冷却期),防迁移
    完成后端口立即复用把残留在途流量打到新租户。"""
    with _alloc_lock:
        dnat_remove_all(host_port, guest_ip)
        bitmap.free(host_port)
    # 冷却期落盘在 alloc_lock 之外,避免文件 IO 挡住关键路径
    add_quarantine(host_port)


# ─── Redis route writer (contract §1, §8) ───────────────────────
ROUTE_KEY_PREFIX = "route:"
# 租户号形状白名单。这些串是从 Redis 的 key 里读出来的 —— 谁能写 Redis 就能控制它们,
# 而它们接下来要作为参数进 root shell(经 SSM 下发)。shlex.quote 已经在控制面侧做了
# 转义,这里是纵深防御的第二道:形状不对的一律不删、计入 failed,而不是「尽力删一下」。
_TENANT_ID_SHAPE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

ROUTE_INVENTORY_DEFAULT_COUNT = 200
ROUTE_INVENTORY_MAX_COUNT = 500
ROUTE_INVENTORY_OUTPUT_BUDGET_BYTES = 20000
ROUTE_INVENTORY_MAX_VALUE_BYTES = 4096
_REDIS_CURSOR_MAX = (1 << 64) - 1
COMPARE_AND_DELETE_LUA = """local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
if current == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return -1"""


def _redis_cursor_text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii")
    text = str(value)
    if not text or not text.isdigit():
        raise ValueError(f"invalid Redis cursor {text!r}")
    number = int(text)
    if number > _REDIS_CURSOR_MAX:
        raise ValueError(f"Redis cursor out of range {text!r}")
    return str(number)


def _bounded_b64(raw: bytes, limit: int = 96) -> str:
    token = base64.b64encode(raw).decode("ascii")
    return token if len(token) <= limit else token[:limit]


def route_value(host_private_ip: str, host_port: int, guest_ip: str) -> str:
    """Contract §1: JSON payload for `route:{tenant_id}`. Field names and
    types locked — P2 edge (backend.lua:parse_value) reads exactly these."""
    return json.dumps(
        {
            "host": host_private_ip,
            "port": int(host_port),
            "guest_ip": guest_ip,
            "updated_at": int(time.time()),
        },
        separators=(",", ":"),  # compact — reduces Redis storage & network
    )


class RedisRouteWriter:
    """Wraps a redis-py client with the failover-safe defaults from
    contract §8:

    - `host` is the ElastiCache **primary endpoint DNS name**, never a
      node IP. On failover, that DNS record repoints and we reconnect.
    - `retry` on `ConnectionError`/`TimeoutError` so a transient blip
      during failover doesn't propagate as a promotion failure.
    - `health_check_interval=30` so idle sockets are validated with PING
      and don't hold a dead half-open connection to the pre-failover
      primary.
    - `socket_connect_timeout` short so DNS re-resolution + reconnect
      happens fast, well within one 15s host-agent poll cycle.
    - Failure to write is **not fatal**: DDB is the authority (contract
      §6). route.lua fail-static serves L2 stale until the next promotion
      write recovers the key.
    """

    def __init__(
        self,
        primary_endpoint: str,
        port: int = 6379,
        socket_connect_timeout: float = 2.0,
        socket_timeout: float = 2.0,
        health_check_interval: int = 30,
        max_retries: int = 2,
        client_factory=None,
    ) -> None:
        self._endpoint = primary_endpoint
        self._port = int(port)
        self._sock_connect_to = socket_connect_timeout
        self._sock_to = socket_timeout
        self._hc = health_check_interval
        self._max_retries = max_retries
        # Test seam: caller passes a zero-arg callable that returns a
        # redis-like object exposing the operations used by the selected path.
        # Production leaves this None and gets a real redis.Redis lazily.
        self._client_factory = client_factory
        self._client = None
        self._client_lock = threading.Lock()

    def _build_default_client(self):
        """Construct a real redis.Redis with contract §8 failover-safe
        defaults. Lazy so tests don't need redis-py installed."""
        import redis  # noqa: PLC0415
        from redis.backoff import ExponentialBackoff  # noqa: PLC0415
        from redis.retry import Retry  # noqa: PLC0415

        retry = Retry(ExponentialBackoff(cap=1.0, base=0.05), self._max_retries)
        return redis.Redis(
            host=self._endpoint,
            port=self._port,
            socket_connect_timeout=self._sock_connect_to,
            socket_timeout=self._sock_to,
            health_check_interval=self._hc,
            retry=retry,
            retry_on_error=[
                redis.exceptions.ConnectionError,
                redis.exceptions.TimeoutError,
            ],
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                self._client = self._build_default_client()
            return self._client

    def _reset_client(self) -> None:
        with self._client_lock:
            self._client = None

    def set_route(
        self, tenant_id: str, host_private_ip: str, host_port: int, guest_ip: str
    ) -> bool:
        """Write `route:{tenant_id}` = JSON payload. Contract §8: NO TTL —
        route keys are semi-static, delete/migrate MUST explicitly DEL.
        Returns True on success, False on any error (fail-open per HA §6).
        """
        key = ROUTE_KEY_PREFIX + tenant_id
        value = route_value(host_private_ip, host_port, guest_ip)
        try:
            self._get_client().set(key, value)  # no ex/px kwargs — no TTL
            return True
        except Exception as e:
            # Fail-open: log & drop connection so next attempt re-resolves
            # the endpoint DNS. Do NOT raise — DDB is authoritative.
            print(f"redis set_route {tenant_id} failed (degraded): {e}")
            self._reset_client()
            return False

    def del_route(self, tenant_id: str) -> bool:
        """DEL `route:{tenant_id}`. Idempotent (Redis DEL returns 0 for
        already-gone keys). Fail-open like set_route."""
        key = ROUTE_KEY_PREFIX + tenant_id
        try:
            self._get_client().delete(key)
            return True
        except Exception as e:
            print(f"redis del_route {tenant_id} failed (degraded): {e}")
            self._reset_client()
            return False

    def del_route_count(self, tenant_id: str) -> int | None:
        """Return Redis DEL's truthful count, or None when deletion failed."""
        key = ROUTE_KEY_PREFIX + tenant_id
        try:
            return int(self._get_client().delete(key))
        except Exception as e:
            print(f"redis del_route_count {tenant_id} failed: {e}")
            self._reset_client()
            return None

    def compare_and_delete_route(
        self, tenant_id: str, expected_bytes: bytes
    ) -> str:
        """Atomically delete only the exact inventoried route value."""
        key = ROUTE_KEY_PREFIX + tenant_id
        try:
            result = int(
                self._get_client().eval(
                    COMPARE_AND_DELETE_LUA,
                    1,
                    key,
                    expected_bytes,
                )
            )
        except Exception as e:
            print(f"redis compare_and_delete {tenant_id} failed: {e}")
            self._reset_client()
            return "failed"
        if result == 1:
            return "deleted"
        if result == 0:
            return "absent"
        if result == -1:
            return "changed"
        print(
            f"redis compare_and_delete {tenant_id} returned unexpected result "
            f"{result}"
        )
        return "failed"

    def ping(self) -> bool:
        """Prove the connection before anyone reads a count off a SCAN.

        Without this, a Redis that cannot be reached is indistinguishable from a
        Redis that legitimately holds zero routes: both surface as an empty
        inventory, and the control plane would read "no orphans" off a probe that
        was never wired up. Fail-loud here so the caller reports INCONCLUSIVE.
        """
        try:
            return bool(self._get_client().ping())
        except Exception as e:
            print(f"redis ping failed: {e}")
            self._reset_client()
            return False

    def scan_routes(self, cursor="0", count=ROUTE_INVENTORY_DEFAULT_COUNT) -> dict:
        """Read one SCAN page and account for every returned key."""
        input_cursor = _redis_cursor_text(cursor)
        count = max(1, min(int(count), ROUTE_INVENTORY_MAX_COUNT))
        routes = []
        skipped = []
        try:
            client = self._get_client()
            next_cursor, keys = client.scan(
                cursor=int(input_cursor),
                match=f"{ROUTE_KEY_PREFIX}*",
                count=count,
            )
        except Exception as e:
            self._reset_client()
            raise RuntimeError(f"Redis route scan failed: {e}") from e

        for raw_key in keys:
            key_bytes = (
                raw_key if isinstance(raw_key, bytes) else str(raw_key).encode("utf-8")
            )
            try:
                key = key_bytes.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(
                    {
                        "key_b64": _bounded_b64(key_bytes),
                        "reason": "bad_key_utf8",
                    }
                )
                continue
            if not key.startswith(ROUTE_KEY_PREFIX):
                skipped.append(
                    {
                        "key_b64": _bounded_b64(key_bytes),
                        "reason": "bad_key_prefix",
                    }
                )
                continue
            tenant_id = key[len(ROUTE_KEY_PREFIX) :]
            if not _TENANT_ID_SHAPE.fullmatch(tenant_id):
                skipped.append(
                    {
                        "key_b64": _bounded_b64(key_bytes),
                        "reason": "bad_key_shape",
                    }
                )
                continue
            try:
                raw_value = client.get(raw_key)
            except Exception as e:
                self._reset_client()
                raise RuntimeError(f"Redis route GET failed for {key}: {e}") from e
            if raw_value is None:
                skipped.append(
                    {"tenant": tenant_id, "reason": "absent_after_scan"}
                )
                continue
            value_bytes = (
                raw_value
                if isinstance(raw_value, bytes)
                else str(raw_value).encode("utf-8")
            )
            if len(value_bytes) > ROUTE_INVENTORY_MAX_VALUE_BYTES:
                skipped.append(
                    {"tenant": tenant_id, "reason": "value_too_large"}
                )
                continue
            try:
                value = json.loads(value_bytes)
                if not isinstance(value, dict):
                    raise ValueError("value_not_object")
                host = value["host"]
                guest_ip = value["guest_ip"]
                if not isinstance(host, str) or not isinstance(guest_ip, str):
                    raise ValueError("route_string_field_invalid")
                host = str(ipaddress.IPv4Address(host))
                guest_ip = str(ipaddress.IPv4Address(guest_ip))
                port = int(value["port"])
                updated_at = int(value["updated_at"])
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                skipped.append(
                    {"tenant": tenant_id, "reason": "bad_route_value"}
                )
                continue
            routes.append(
                {
                    "tenant": tenant_id,
                    "host": host,
                    "port": port,
                    "guest_ip": guest_ip,
                    "updated_at": updated_at,
                    "expected_b64": base64.b64encode(value_bytes).decode("ascii"),
                }
            )
        try:
            normalized_next = _redis_cursor_text(next_cursor)
        except (UnicodeDecodeError, ValueError) as e:
            self._reset_client()
            raise RuntimeError(f"Redis route scan returned bad cursor: {e}") from e
        return {
            "cursor": input_cursor,
            "next_cursor": normalized_next,
            "count": count,
            "scanned": len(keys),
            "routes": routes,
            "skipped": skipped,
        }


# ─── Drift reconciliation (contract §3 `_probe_all` diff) ───────
def reconcile_drift(
    vm_dir_tids: set[str],
    ddb_desc: dict[str, dict],
    dnat_rules: dict[int, str],
) -> dict[str, set]:
    """Compute the three-set diff for host-side route hygiene.

    Inputs (each a snapshot taken close together in time):
      vm_dir_tids  — tenant_ids with a live vm.json + running FC
      ddb_desc     — {tenant_id: {host_port, guest_ip, ...}} from DDB
      dnat_rules   — {host_port: guest_ip} from `iptables -S PREROUTING`

    Outputs (all pure — caller decides log level and remediation):
      orphan_dnat        — DNAT rules whose (port, guest_ip) has no
                           matching DDB descriptor row.
      missing_dnat       — DDB descriptor says (port, guest_ip) but
                           iptables has no matching rule.
      ghost_descriptor   — DDB descriptor references a tenant with no
                           vm.json/FC alive locally.

    We DO NOT auto-fix here (blast radius: writing DNAT for a stale
    tenant could shadow a currently-live tenant with the same port).
    Contract §3 asked for "差集告警/修复"; the caller emits the alert,
    a separate reviewer-audited PR wires the mutations.
    """
    # DDB descriptors that name a (port, guest_ip) pair on this host
    ddb_ports: dict[int, str] = {}
    ghost_descriptor: set[str] = set()
    for tid, row in ddb_desc.items():
        port = row.get("host_port")
        guest_ip = row.get("guest_ip")
        if port is not None and guest_ip:
            ddb_ports[int(port)] = guest_ip
        if tid not in vm_dir_tids:
            ghost_descriptor.add(tid)

    orphan_dnat: set[tuple[int, str]] = set()
    for port, guest_ip in dnat_rules.items():
        if ddb_ports.get(port) != guest_ip:
            orphan_dnat.add((port, guest_ip))

    missing_dnat: set[tuple[int, str]] = set()
    for port, guest_ip in ddb_ports.items():
        if dnat_rules.get(port) != guest_ip:
            missing_dnat.add((port, guest_ip))

    return {
        "orphan_dnat": orphan_dnat,
        "missing_dnat": missing_dnat,
        "ghost_descriptor": ghost_descriptor,
    }


# ─── CLI(控制面经 SSM 调 host 侧路由操作)───
# 控制面 Lambda 不在 VPC 内、无 Redis 客户端也无 host 位图。以下三条命令让控制面
# 在 delete/迁移完成时发一条 SSM 到对应 host 上调本 CLI,由 host 侧权威地维护
# 路由(§4 DDB 描述符 / §1 Redis / §3 DNAT 位图)。
#   del-route <tid> <expected_b64> [...]    — 原子比对清点值后删 Redis route: 键
#   list-routes [cursor] [count]            — 分页 SCAN Redis route: 键供控制面对账
#   ready-route <tid>                       — R6 阶段1 建:target 侧读本地 vm.json 拿
#                                             guest_ip → alloc 端口 + 写 DNAT,**不碰 Redis**;
#                                             STDOUT `OK <host_port> <guest_ip>` 供探活+commit。
#                                             此刻无流量进 target(Redis 仍指源)。
#   commit-route <tid> <ip> <port> <gip>    — R6 阶段2 切:探活通过后原子写 Redis route:{tid}
#                                             指向 target(幂等单 key SET)。DNAT 已由 ready 建。
#   ensure-route <tid> <host_ip>            — Legacy 一步式(建+切耦合):alloc+DNAT+Redis。
#                                             保留兼容;R6 迁移改用 ready+commit 分离(插探活门)。
#   release-route <tid> <host_port> <gip>   — 迁移完成 source 侧:释放位图端口 + 删 DNAT
#                                             (R5)。不碰 Redis(route 已指 target)。幂等。
_ROUTE_VALUE_HOST_KEY = "host"  # route_value() JSON field


def _cli_redis_writer():
    """Build a RedisRouteWriter from env, or None if Redis 未接线."""
    ep = os.environ.get("ENGINE_REDIS_ENDPOINT", "")
    if not ep:
        return None
    return RedisRouteWriter(
        primary_endpoint=ep,
        port=int(os.environ.get("ENGINE_REDIS_PORT", "6379")),
    )


def _read_vm_guest_ip(tenant_id: str) -> str:
    """Read guest_ip from this host's local vm.json (launch-vm.sh authority).
    Empty string if missing — caller fails loud rather than guessing."""
    path = os.path.join("/data/firecracker-vms", tenant_id, "vm.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return str(json.load(fh).get("guest_ip", "")).strip()
    except (OSError, ValueError):
        return ""


def _alloc_or_reuse_dnat(guest_ip: str) -> int:
    """Alloc a port + write PREROUTING DNAT for guest_ip, reusing an existing
    in-range rule for this guest_ip if present (idempotent). Returns host_port.
    Shared by ready-route (R6 阶段1) and the legacy ensure-route."""
    bitmap = PortBitmap()
    rebuild_bitmap_from_iptables(bitmap)
    for p, g in list_dnat_rules().items():
        if g == guest_ip and PORT_RANGE_LOW <= p <= PORT_RANGE_HIGH:
            bitmap.mark_used(p)
            return p
    return alloc_and_dnat_atomic(bitmap, guest_ip)


def _probe_guest_healthz(guest_ip: str, timeout: int = 3) -> bool:
    """R6.2 黑洞防护自检:gateway HTTP server 是否在应答(路径无关)。

    判据与 host-agent app_health promote 门(host-agent.py:475-503)同款:探 `/`
    用 `-w %{http_code}` 不带 `-f`——curl 退出码 0 且 http_code 非 000(含
    404/401/200)= gateway HTTP server 活着在应答;拒连(7)/超时(28)/000 = 真 down。
    为什么不探 /healthz 期望 200:openclaw 2026.2.26 的 gateway 没有 /healthz 端点,
    任何路径都 404(#207 真机抓到:旧版 `-fsS /healthz` 遇 404 返回非 0 → 恒判
    NOT_READY → 迁移永卡 restore,连健康 resident VM 都过不了这门)。未就绪
    ready-route fail-loud 不打 OK,控制面不 commit 切流(此刻 Redis 仍指源)。"""
    try:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                str(timeout),
                "--max-time",
                str(timeout),
                f"http://{guest_ip}:{GATEWAY_GUEST_PORT}/",
            ],
            capture_output=True,
            timeout=timeout + 2,
        )
        code = (r.stdout or b"").decode(errors="replace").strip()
        return r.returncode == 0 and code.isdigit() and code != "000"
    except (subprocess.SubprocessError, OSError):
        return False


def _cli_ready_route(tenant_id: str) -> int:
    """R6 阶段1 Ready(建):target 侧 alloc 端口 + 写 DNAT + 自检 guest /healthz,
    **不碰 Redis/ALB**。VM 已本地 restore。幂等——复用该 guest_ip 已有的 in-range
    DNAT。自检通过才打印 `OK <host_port> <guest_ip>`(供控制面探活确认 + 阶段2
    commit 回传);未就绪打 NOT_READY 并 exit 1,控制面不切流(R6.2 黑洞防护:
    此刻 Redis 仍指源,源还活着,老流量正常)。"""
    guest_ip = _read_vm_guest_ip(tenant_id)
    if not guest_ip:
        print(f"ready-route {tenant_id}: no vm.json/guest_ip locally — FAIL")
        return 1
    port = _alloc_or_reuse_dnat(guest_ip)
    if not _probe_guest_healthz(guest_ip):
        print(f"NOT_READY {port} {guest_ip}")
        return 1
    print(f"OK {port} {guest_ip}")
    return 0


def _cli_commit_route(
    tenant_id: str, host_ip: str, host_port: int, guest_ip: str
) -> int:
    """R6 阶段2 Commit(切):target DNAT 已就绪 + 探活通过后,原子写 Redis
    route:{tid} 指向 target。幂等单 key SET(重入不产生双写)。DNAT 由阶段1
    ready-route 建好,这里只切导航——把"建"和"切"分开,才有探活门可插(R6.1)。"""
    writer = _cli_redis_writer()
    if writer is None:
        print(f"commit-route {tenant_id}: no ENGINE_REDIS_ENDPOINT — FAIL")
        return 1
    if not host_ip or host_port <= 0 or not guest_ip:
        print(f"commit-route {tenant_id}: bad args host_ip/port/guest_ip — FAIL")
        return 1
    writer.set_route(tenant_id, host_ip, host_port, guest_ip)
    print(f"OK committed {host_ip} {host_port} {guest_ip}")
    return 0


def _cli_ensure_route(tenant_id: str, host_ip: str) -> int:
    """Legacy 一步式(建+切耦合):alloc 端口 + DNAT + Redis。保留兼容旧调用;
    R6 迁移路径改用 ready-route(建)+ commit-route(切)分离,以便在两步间插
    探活门。新代码勿用此路。Prints `OK <host_port> <guest_ip>` (R3/R4)."""
    guest_ip = _read_vm_guest_ip(tenant_id)
    if not guest_ip:
        print(f"ensure-route {tenant_id}: no vm.json/guest_ip locally — FAIL")
        return 1
    port = _alloc_or_reuse_dnat(guest_ip)
    writer = _cli_redis_writer()
    if writer is not None and host_ip:
        writer.set_route(tenant_id, host_ip, port, guest_ip)
    print(f"OK {port} {guest_ip}")
    return 0


def _cli_release_route(tenant_id: str, host_port: int, guest_ip: str) -> int:
    """Source-side migration completion: release ONLY the source host's port
    bitmap slot + PREROUTING DNAT rule. Idempotent (release_port_and_dnat
    tolerates already-gone rules). Only the migration path calls this — normal
    stop preserves the port (R2.3).

    Deliberately does NOT touch Redis `route:{tenant_id}`: that key is
    per-tenant (not per-host) and after migration correctly points at the
    TARGET, which ensure-route just wrote. Deleting it here would blank the
    live route (source release runs after target ensure). tenant_id is taken
    only for logging."""
    bitmap = PortBitmap()
    rebuild_bitmap_from_iptables(bitmap)
    try:
        release_port_and_dnat(bitmap, host_port, guest_ip)
    except Exception as e:
        print(f"release-route {tenant_id}: release_port_and_dnat failed: {e}")
        return 1
    print(f"OK released {host_port} {guest_ip}")
    return 0


def _cli_delete_route(
    tenant_id: str, host_port: int, guest_ip: str, legacy_port: int
) -> int:
    """Delete live/legacy DNAT and Redis state as one fail-loud gate."""
    if (host_port > 0 or legacy_port > 0) and not guest_ip:
        print(
            f"delete-route {tenant_id}: port state exists without guest_ip; "
            "refusing partial cleanup"
        )
        return 1

    writer = _cli_redis_writer()
    if writer is None:
        print(
            f"delete-route {tenant_id}: no ENGINE_REDIS_ENDPOINT; "
            "refusing partial cleanup"
        )
        return 1
    deleted_count = writer.del_route_count(tenant_id)
    if deleted_count is None:
        print(f"delete-route {tenant_id}: Redis DEL failed")
        return 1

    try:
        if guest_ip and host_port > 0:
            if PORT_RANGE_LOW <= host_port <= PORT_RANGE_HIGH:
                bitmap = PortBitmap()
                rebuild_bitmap_from_iptables(bitmap)
                release_port_and_dnat(bitmap, host_port, guest_ip)
            else:
                dnat_remove_all(host_port, guest_ip)
        if guest_ip and legacy_port > 0 and legacy_port != host_port:
            dnat_remove_all(legacy_port, guest_ip)
    except Exception as exc:
        print(f"delete-route {tenant_id}: DNAT release failed: {exc}")
        return 1

    print(
        f"OK deleted route tenant={tenant_id} port={host_port} "
        f"legacy_port={legacy_port} guest={guest_ip}"
    )
    return 0


def _cli_del_routes(argv: list[str]) -> int:
    """Compare-and-delete `<tenant> <expected_b64>` pairs."""
    if not argv or len(argv) % 2:
        print("OC_ROUTE_ERROR reason=bad_del_route_argv")
        return 1

    pairs = []
    rejected = []
    for index in range(0, len(argv), 2):
        tenant_id = argv[index]
        token = argv[index + 1]
        if not _TENANT_ID_SHAPE.fullmatch(tenant_id):
            rejected.append(tenant_id)
            continue
        try:
            expected_bytes = base64.b64decode(token.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError):
            rejected.append(tenant_id)
            continue
        if not expected_bytes:
            rejected.append(tenant_id)
            continue
        pairs.append((tenant_id, expected_bytes))

    for tenant_id in rejected:
        print(f"OC_ROUTE_OP op=del tenant={tenant_id} result=rejected_shape")

    writer = _cli_redis_writer()
    if writer is None:
        for tenant_id, _expected_bytes in pairs:
            print(
                f"OC_ROUTE_OP op=del tenant={tenant_id} result=unconfigured"
            )
        return 1

    failed = bool(rejected)
    for tenant_id, expected_bytes in pairs:
        result = writer.compare_and_delete_route(tenant_id, expected_bytes)
        print(f"OC_ROUTE_OP op=del tenant={tenant_id} result={result}")
        if result == "failed":
            failed = True
    return 1 if failed else 0


def _route_inventory_lines(page) -> list[str]:
    lines = [
        "OC_ROUTE_PAGE "
        f"cursor={page['cursor']} next_cursor={page['next_cursor']} "
        f"count={page['count']}"
    ]
    for route in page["routes"]:
        lines.append(
            "OC_ROUTE_ITEM "
            f"tenant={route['tenant']} host={route['host']} "
            f"port={route['port']} guest_ip={route['guest_ip']} "
            f"updated_at={route['updated_at']} "
            f"expected_b64={route['expected_b64']}"
        )
    for skipped in page["skipped"]:
        identity = (
            f"tenant={skipped['tenant']}"
            if skipped.get("tenant")
            else f"key_b64={skipped.get('key_b64', '')}"
        )
        lines.append(
            f"OC_ROUTE_SKIPPED {identity} reason={skipped['reason']}"
        )
    lines.append(
        f"OC_ROUTE_TOTAL n={len(page['routes'])} "
        f"skipped={len(page['skipped'])} scanned={page['scanned']}"
    )
    return lines


def _serialized_output_bytes(lines: list[str]) -> int:
    return sum(len(line.encode("utf-8")) + 1 for line in lines)


def _cli_list_routes(
    cursor="0", count=ROUTE_INVENTORY_DEFAULT_COUNT
) -> int:
    """Print one sealed, byte-bounded Redis route inventory page."""
    try:
        cursor = _redis_cursor_text(cursor)
        count = max(1, min(int(count), ROUTE_INVENTORY_MAX_COUNT))
    except (TypeError, ValueError, UnicodeDecodeError):
        print("OC_ROUTE_ERROR reason=bad_cursor_or_count")
        return 1
    writer = _cli_redis_writer()
    if writer is None:
        print("OC_ROUTE_ERROR reason=unconfigured")
        return 1
    # PING 自证必须在 SCAN 之前:连不上的 Redis 和「真的零条 route」在输出上一模一样,
    # 都是空清单。少了这一步,控制面会把一次连接失败读成「没有残留」并据此收工。
    if not writer.ping():
        print("OC_ROUTE_ERROR reason=ping_failed")
        return 1

    attempt_count = count
    while True:
        try:
            page = writer.scan_routes(cursor=cursor, count=attempt_count)
        except Exception as e:
            reason = " ".join(str(e).split())
            print(f"OC_ROUTE_ERROR reason={reason}")
            return 1
        lines = _route_inventory_lines(page)
        if _serialized_output_bytes(lines) <= ROUTE_INVENTORY_OUTPUT_BUDGET_BYTES:
            for line in lines:
                print(line)
            return 0
        if attempt_count > 1:
            attempt_count = max(1, attempt_count // 2)
            continue

        # Redis SCAN may return multiple keys even for COUNT 1. Advancing the
        # returned cursor without accounting for them would silently lose keys
        # from this pass, while retrying forever would wedge the reconciler.
        # Emit one bounded, explicit skip record covering the entire returned
        # batch and let the next full pass retry those keys.
        fallback = [
            "OC_ROUTE_PAGE "
            f"cursor={page['cursor']} next_cursor={page['next_cursor']} count=1",
            "OC_ROUTE_SKIPPED_BATCH "
            f"n={page['scanned']} reason=page_over_budget_at_count_1",
            f"OC_ROUTE_TOTAL n=0 skipped={page['scanned']} "
            f"scanned={page['scanned']}",
        ]
        if (
            _serialized_output_bytes(fallback)
            > ROUTE_INVENTORY_OUTPUT_BUDGET_BYTES
        ):
            print("OC_ROUTE_ERROR reason=min_count_fallback_over_budget")
            return 1
        for line in fallback:
            print(line)
        return 0


if __name__ == "__main__":
    import sys

    _cmd = sys.argv[1] if len(sys.argv) >= 2 else ""

    if _cmd == "del-route" and len(sys.argv) >= 3:
        sys.exit(_cli_del_routes(sys.argv[2:]))

    if _cmd == "list-routes" and 2 <= len(sys.argv) <= 4:
        _cursor = sys.argv[2] if len(sys.argv) >= 3 else "0"
        _count = (
            sys.argv[3]
            if len(sys.argv) >= 4
            else ROUTE_INVENTORY_DEFAULT_COUNT
        )
        sys.exit(_cli_list_routes(_cursor, _count))

    if _cmd == "ready-route" and len(sys.argv) >= 3:
        sys.exit(_cli_ready_route(sys.argv[2]))

    if _cmd == "commit-route" and len(sys.argv) >= 6:
        sys.exit(
            _cli_commit_route(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5])
        )

    if _cmd == "ensure-route" and len(sys.argv) >= 4:
        sys.exit(_cli_ensure_route(sys.argv[2], sys.argv[3]))

    if _cmd == "release-route" and len(sys.argv) >= 5:
        sys.exit(_cli_release_route(sys.argv[2], int(sys.argv[3]), sys.argv[4]))

    if _cmd == "delete-route" and len(sys.argv) >= 6:
        sys.exit(
            _cli_delete_route(
                sys.argv[2], int(sys.argv[3]), sys.argv[4], int(sys.argv[5])
            )
        )

    print(
        "usage: route_ops.py del-route <tid> <expected_b64> "
        "[<tid> <expected_b64> ...]\n"
        "       route_ops.py list-routes [<cursor> [<count>]]\n"
        "       route_ops.py ready-route <tid>\n"
        "       route_ops.py commit-route <tid> <host_ip> <host_port> <guest_ip>\n"
        "       route_ops.py ensure-route <tid> <host_ip>\n"
        "       route_ops.py release-route <tid> <host_port> <guest_ip>\n"
        "       route_ops.py delete-route <tid> <host_port> <guest_ip> <legacy_port>"
    )
    sys.exit(2)
