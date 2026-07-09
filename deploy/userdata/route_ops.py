#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Host-side routing operations for the P2 two-tier data plane.

Routing contract shared with the edge:
    - Redis key schema (`route:{tenant_id}` -> JSON host/port/guest_ip/updated_at)
    - Port bitmap 10000-10400 + iptables DNAT (atomic three-step)
    - DDB descriptor fields (host_private_ip, host_port, guest_ip)
    - HA self-recovery (Redis write fails degrade, DDB is authority)
    - Redis failover (primary endpoint, DNS TTL re-resolve, no infinite pool)

Kept as a thin module: pure functions where possible, one class per stateful
concern, small enough to reason about. host-agent.py wires it in at promotion.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time

# ─── Public constants (contract §3) ─────────────────────────────
PORT_RANGE_LOW = 10000
PORT_RANGE_HIGH = 10400  # inclusive; 401 slots
GATEWAY_GUEST_PORT = 18789  # OpenClaw gateway inside microVM (launch-vm.sh:747)


# ─── Port bitmap ────────────────────────────────────────────────
class PortAllocationError(RuntimeError):
    """Raised when the bitmap cannot satisfy an alloc (exhausted or race)."""


class PortBitmap:
    """Local bookkeeping for host-side DNAT ingress ports [10000, 10400].

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
        n<=401 so unmeasurably cheap. Raises on exhaustion (fail-loud)."""
        with self._lock:
            for port in range(self._low, self._high + 1):
                if port not in self._used:
                    self._used.add(port)
                    return port
            raise PortAllocationError(
                f"port bitmap exhausted (range [{self._low}, {self._high}])"
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


def rebuild_bitmap_from_iptables(bitmap: PortBitmap) -> int:
    """Boot recovery entrypoint. Returns the count of ports marked used."""
    rules = list_dnat_rules()
    n = 0
    for port in rules:
        if PORT_RANGE_LOW <= port <= PORT_RANGE_HIGH:
            bitmap.mark_used(port)
            n += 1
    return n


# ─── Atomic alloc + DNAT ────────────────────────────────────────
_alloc_lock = threading.Lock()


def alloc_and_dnat_atomic(bitmap: PortBitmap, guest_ip: str) -> int:
    """Three-step atomic (contract §3): alloc port -> verify not-in-use
    via `iptables -C` -> add DNAT. On any failure, roll back the alloc.

    Serialised globally with `_alloc_lock` so two threads never race for
    the same port even though PortBitmap is itself thread-safe.
    """
    with _alloc_lock:
        port = bitmap.alloc()
        try:
            if dnat_check(port, guest_ip):
                # Foreign identical rule — extremely unlikely (would mean
                # bootstrap didn't mark_used this port), but bail out
                # rather than silently double-count.
                raise RuntimeError(
                    f"DNAT rule already exists for port={port} guest={guest_ip} "
                    "(bitmap out of sync with iptables?)"
                )
            dnat_add(port, guest_ip)
            return port
        except Exception:
            bitmap.free(port)
            raise


def release_port_and_dnat(bitmap: PortBitmap, host_port: int, guest_ip: str) -> None:
    """Symmetric release. iptables first (safe if it fails — port stays
    reserved which is preferable to freeing a port whose rule remained)."""
    with _alloc_lock:
        dnat_remove(host_port, guest_ip)
        bitmap.free(host_port)


# ─── Redis route writer (contract §1, §8) ───────────────────────
ROUTE_KEY_PREFIX = "route:"


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
        # redis-like object exposing .set(k,v) and .delete(k). Production
        # leaves this None and gets a real redis.Redis lazily.
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
