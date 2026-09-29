# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Creation latency on dense hosts: fast promote, one pgrep per pass.

A creating tenant used to be promoted only by the serial poll pass, whose length
grows with the VMs on the host (~25 s at 290 VMs), so API -> running was ~50 s on
a full host and ~30 s on an empty one. The fast-promote loop promotes it within
about a second of the gateway answering, through the same guarded writes.
"""

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


_USERDATA = Path(__file__).resolve().parents[1] / "deploy/userdata"
with patch("boto3.resource"), patch("boto3.client"):
    spec = importlib.util.spec_from_file_location(
        "fast_promote_agent", _USERDATA / "host-agent.py"
    )
    agent = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = agent
    spec.loader.exec_module(agent)
route_ops = agent.route_ops
_ensure_route = agent._ensure_route  # the env fixture stubs it; _real_route restores it


def _proc(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# ─── one process-table scan per pass ────────────────────────────


def test_pid_index_maps_each_socket_to_its_lowest_pid():
    out = "\n".join([
        "700 firecracker --api-sock /data/firecracker-vms/a/fc.sock --log-path x",
        "650 firecracker --api-sock /data/firecracker-vms/a/fc.sock --level Info",
        "701 firecracker --api-sock /data/firecracker-vms/b/fc.sock",
        "702 bash -c pgrep api-sock",
        "garbage line",
    ])
    with patch.object(agent.subprocess, "run", return_value=_proc(out)) as run:
        index = agent._fc_pid_index()
    assert run.call_count == 1
    assert index == {
        "/data/firecracker-vms/a/fc.sock": 650,
        "/data/firecracker-vms/b/fc.sock": 701,
    }


def test_pid_index_no_match_is_empty_but_failure_is_none():
    with patch.object(agent.subprocess, "run", return_value=_proc("", 1)):
        assert agent._fc_pid_index() == {}
    with patch.object(agent.subprocess, "run", return_value=_proc("", 2)):
        assert agent._fc_pid_index() is None
    with patch.object(agent.subprocess, "run", side_effect=OSError("boom")):
        assert agent._fc_pid_index() is None


def test_pid_lookup_rechecks_a_vm_missing_from_the_snapshot():
    sock = "/data/firecracker-vms/new/fc.sock"
    with patch.object(agent.subprocess, "run") as run:
        assert agent._fc_pid(sock, {sock: 42}) == 42
        run.assert_not_called()
        run.return_value = _proc("977\n")
        assert agent._fc_pid(sock, {}) == 977  # launched after the snapshot
        run.return_value = _proc("", 1)
        assert agent._fc_pid(sock, None) is None
    assert run.call_args.args[0] == ["pgrep", "-f", f"api-sock {sock}"]


def test_probe_all_scans_the_process_table_once(tmp_path):
    for i in range(5):
        vm = tmp_path / f"t-{i}"
        vm.mkdir()
        (vm / "vm.json").write_text(json.dumps({"guest_ip": f"172.16.0.{i}"}))
    pgrep_out = "\n".join(
        f"{100 + i} firecracker --api-sock {tmp_path}/t-{i}/fc.sock" for i in range(5)
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args[0])
        return _proc(pgrep_out) if args[0] == "pgrep" else _proc("", 0)

    with (
        patch.object(agent, "VM_DIR", str(tmp_path)),
        patch.object(agent.subprocess, "run", side_effect=fake_run),
        patch.object(agent, "_probe_app_health", return_value="up"),
        patch.object(agent, "_fc_boot_iso", return_value=""),
    ):
        results = agent._probe_all()
    assert calls.count("pgrep") == 1
    assert calls.count("ping") == 5
    assert {r["fc_pid"] for r in results.values()} == {100, 101, 102, 103, 104}
    assert all(isinstance(r["probed_at"], float) for r in results.values())
    assert all(r["vm_health"] == "up" for r in results.values())


# ─── fast promote ───────────────────────────────────────────────


@pytest.fixture
def env(tmp_path):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="ap-southeast-1")
        table = ddb.create_table(
            TableName="test-tenants",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        for state in (agent._observed_status, agent._fast_retry_at,
                      agent._tenant_write_locks, agent._phys_backfilled,
                      agent._last_probe_at, agent._status):
            state.clear()
        agent._phys_backfilled.add("t-1")
        with (
            patch.object(agent, "TENANTS_TABLE", table.name),
            patch.object(agent, "INSTANCE_ID", "i-test"),
            patch.object(agent, "VM_DIR", str(tmp_path)),
            patch.object(agent, "_get_ddb") as get_ddb,
            patch.object(agent, "_compose_metrics", return_value={"cpu_pct": 0}),
            patch.object(agent, "_ensure_route", return_value=("10.0.0.1", 10000)) as route,
            patch.object(agent, "_fc_boot_iso", return_value="2026-09-28T00:00:00Z"),
            patch.object(agent, "_mounted_image_snapshots", return_value={}),
            patch.object(table, "update_item", wraps=table.update_item) as writes,
        ):
            get_ddb.return_value.Table.return_value = table
            yield SimpleNamespace(table=table, route=route, writes=writes, dir=tmp_path)


def _vm(env, tid="t-1", age=0, stopped=False, vm_num=1):
    vm = env.dir / tid
    vm.mkdir()
    cfg = vm / "vm.json"
    cfg.write_text(json.dumps({"guest_ip": "172.16.0.2", "vm_num": vm_num,
                               "image_snapshot_time": "snap-1"}))
    if age:
        t = time.time() - age
        os.utime(cfg, (t, t))
    if stopped:
        (vm / ".stopped").touch()
    return f"{vm}/fc.sock"


def _put(env, tid="t-1", status="creating", **overrides):
    env.table.put_item(Item={
        "id": tid, "status": status, "host_id": "i-test", "vm_num": 1,
        "capacity_reservation_id": "reservation-1", **overrides,
    })


def _status(env, tid="t-1"):
    return env.table.get_item(Key={"id": tid}, ConsistentRead=True)["Item"]["status"]


def _host(pids, gateway="up", ping_rc=0):
    """Patch the host probes: pgrep snapshot, ping and the gateway check."""
    pgrep_out = "\n".join(f"{pid} firecracker --api-sock {sock}" for sock, pid in pids.items())

    def fake_run(args, **kwargs):
        if args[0] == "pgrep":
            return _proc(pgrep_out, 0 if pids else 1)
        if args[0] == "ping":
            return _proc("", ping_rc)
        raise AssertionError(f"unexpected command {args}")

    return (
        patch.object(agent.subprocess, "run", side_effect=fake_run),
        patch.object(agent, "_probe_app_health", return_value=gateway),
    )


def _fast(pids, **kw):
    """One fast-promote tick: probe every candidate and write each result."""
    run, app = _host(pids, **kw)
    with run, app, ThreadPoolExecutor(max_workers=4) as pool:
        inflight = {}
        agent._fast_promote_submit(pool, inflight)
        written = 0
        while inflight:
            written += agent._fast_promote_drain(inflight, 5)
        return written


def test_fast_path_promotes_as_soon_as_gateway_answers(env, capsys):
    sock = _vm(env)
    _put(env)
    assert _fast({sock: 4242}, gateway="down") == 0
    assert _status(env) == "creating"
    env.writes.assert_not_called()  # a not-ready VM costs no DDB write
    assert _fast({sock: 4242}) == 1
    assert _status(env) == "running"
    item = env.table.get_item(Key={"id": "t-1"})["Item"]
    assert "capacity_reservation_id" not in item
    assert item["host_port"] == 10000
    assert item["observed_image_snapshot_time"] == "snap-1"
    assert agent._observed_status["t-1"] == "running"
    assert "via=fast" in capsys.readouterr().out
    # Promoted: no longer a candidate, the poll loop owns it from here.
    env.writes.reset_mock()
    assert _fast({sock: 4242}) == 0
    env.writes.assert_not_called()


@pytest.mark.parametrize("case", ["no_fc", "ping_down", "stopped", "old", "running"])
def test_fast_path_skips_what_it_must_not_touch(env, case):
    sock = _vm(env, stopped=case == "stopped", age=3600 if case == "old" else 0)
    _put(env)
    if case == "running":
        agent._observed_status["t-1"] = "running"
    pids = {} if case == "no_fc" else {sock: 4242}
    assert _fast(pids, ping_rc=1 if case == "ping_down" else 0) == 0
    env.writes.assert_not_called()
    env.route.assert_not_called()
    assert _status(env) == "creating"


def test_fast_path_never_recovers_or_relaunches(env):
    sock = _vm(env)
    _put(env)
    with (
        patch.object(agent, "_recover_vm") as recover,
        patch.object(agent, "_force_relaunch_vm") as relaunch,
    ):
        for _ in range(5):
            _fast({}, ping_rc=1)
            _fast({sock: 1}, ping_rc=1)
    recover.assert_not_called()
    relaunch.assert_not_called()
    assert agent._net_dead_polls.get("t-1") is None


def test_fast_path_skips_a_tenant_the_poll_loop_is_writing(env):
    sock = _vm(env)
    _put(env)
    lk = agent._tenant_write_lock("t-1")
    with lk:
        assert _fast({sock: 4242}) == 0
    env.route.assert_not_called()
    assert _status(env) == "creating"
    assert _fast({sock: 4242}) == 1
    assert _status(env) == "running"


def test_poll_and_fast_path_share_one_lock_per_tenant(env):
    _put(env)
    seen = []
    real_write = agent._write_tenant

    def spy(table, tid, info, now, via="poll"):
        seen.append(agent._tenant_write_lock(tid).locked())
        return real_write(table, tid, info, now, via=via)

    with patch.object(agent, "_write_tenant", side_effect=spy):
        agent._write_ddb({"t-1": {"vm_health": "up", "app_health": "up",
                                  "guest_ip": "172.16.0.2", "phys_vm_num": 1,
                                  "fc_pid": 1}})
    assert seen == [True]
    assert agent._observed_status["t-1"] == "running"


def test_blocked_promote_backs_off_instead_of_writing_every_second(env):
    sock = _vm(env)
    _put(env, dispatch_settle="in-flight")
    assert _fast({sock: 4242}) == 1
    assert _status(env) == "creating"
    assert agent._observed_status["t-1"] == "creating"
    env.writes.reset_mock()
    assert _fast({sock: 4242}) == 0  # inside FAST_PROMOTE_RETRY_SEC
    env.writes.assert_not_called()
    agent._fast_retry_at["t-1"] = 0
    _put(env)  # guard cleared
    assert _fast({sock: 4242}) == 1
    assert _status(env) == "running"


@pytest.mark.parametrize("item", [None, {"host_id": "i-other"}, {"vm_num": 2}])
def test_fast_path_keeps_every_ownership_guard(env, item):
    sock = _vm(env)
    if item is not None:
        _put(env, **item)
    _fast({sock: 4242})
    got = env.table.get_item(Key={"id": "t-1"}).get("Item")
    if item is None:
        assert got is None  # never resurrects a deleted tenant
        assert agent._observed_status["t-1"] == "not-ours"
    else:
        assert got["status"] == "creating"


def test_poll_pass_reopens_a_rolled_back_tenant_to_the_fast_path(env):
    sock = _vm(env)
    _put(env, status="deleting")
    agent._write_ddb({"t-1": {"vm_health": "up", "app_health": "down",
                              "guest_ip": "172.16.0.2", "phys_vm_num": 1}})
    assert agent._observed_status["t-1"] == "deleting"
    assert _fast({sock: 4242}) == 0
    _put(env)  # delete failed, rolled back to creating
    agent._write_ddb({"t-1": {"vm_health": "up", "app_health": "down",
                              "guest_ip": "172.16.0.2", "phys_vm_num": 1}})
    assert agent._observed_status["t-1"] == "creating"
    assert _fast({sock: 4242}) == 1
    assert _status(env) == "running"


def test_prune_forgets_tenants_that_left_the_host():
    agent._observed_status.update({"gone": "running", "here": "creating"})
    agent._fast_retry_at["gone"] = 1
    agent._last_probe_at["gone"] = 1.0
    agent._tenant_write_lock("gone")
    held = agent._tenant_write_lock("held")
    with held:
        agent._prune_tenant_state({"here"})
    assert "gone" not in agent._observed_status
    assert "gone" not in agent._fast_retry_at
    assert "gone" not in agent._last_probe_at
    assert "gone" not in agent._tenant_write_locks
    assert agent._tenant_write_locks.get("held") is held  # never drop a held lock
    assert agent._observed_status["here"] == "creating"


# ─── routes always check the live DNAT rules ────────────────────


@pytest.fixture
def nat():
    rules = {10000: "172.16.0.2"}
    lists = []

    def fake_list():
        lists.append(1)
        return dict(rules)

    with (
        patch.object(route_ops, "list_dnat_rules", side_effect=fake_list),
        patch.object(route_ops, "dnat_check", side_effect=lambda p, g: rules.get(p) == g),
        patch.object(route_ops, "dnat_add", side_effect=lambda p, g: rules.__setitem__(p, g)),
        patch.object(route_ops, "dnat_remove_all", side_effect=lambda p, g: rules.pop(p, None)),
        patch.object(route_ops, "add_quarantine"),
    ):
        yield SimpleNamespace(rules=rules, lists=lists, bitmap=route_ops.PortBitmap())


def test_ready_tenant_is_written_while_another_probe_is_still_running(env):
    _vm(env)
    _vm(env, tid="slow")
    _put(env)
    ready = {"vm_health": "up", "app_health": "up", "guest_ip": "172.16.0.2",
             "phys_vm_num": 1, "fc_pid": 4242, "probed_at": time.monotonic()}
    release = threading.Event()

    def probe(tid, index):
        if tid == "slow":
            release.wait(5)
            return None
        return ready

    inflight = {}
    with (
        patch.object(agent, "_fc_pid_index", return_value={}),
        patch.object(agent, "_fast_probe", side_effect=probe),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        try:
            assert agent._fast_promote_submit(pool, inflight) == 2
            deadline = time.monotonic() + 3
            while _status(env) != "running" and time.monotonic() < deadline:
                agent._fast_promote_drain(inflight, 0.2)
            assert _status(env) == "running"
            assert list(inflight.values()) == ["slow"]  # still probing
            # The next tick does not start a second probe of the slow tenant.
            assert agent._fast_promote_submit(pool, inflight) == 0
        finally:
            release.set()
        while inflight:
            agent._fast_promote_drain(inflight, 5)


def test_fast_probe_bounds_the_gateway_check(env):
    sock = _vm(env)
    seen = {}

    def app(ip, chat_ep, timeout=8):
        seen["timeout"] = timeout
        return "down"

    with (
        patch.object(agent, "_probe_app_health", side_effect=app),
        patch.object(agent.subprocess, "run", return_value=_proc("", 0)),
    ):
        assert agent._fast_probe("t-1", {sock: 1}) is None
    assert seen["timeout"] == agent.FAST_PROMOTE_PROBE_TIMEOUT_SEC < 8


def test_poll_result_probed_before_a_fast_write_is_dropped(env):
    sock = _vm(env)
    _put(env)
    before = time.monotonic()  # the poll pass probes t-1 (gateway still down)...
    old = {"vm_health": "up", "app_health": "down", "guest_ip": "172.16.0.2",
           "phys_vm_num": 1, "fc_pid": 4242, "probed_at": before}
    assert _fast({sock: 4242}) == 1  # ...the fast loop promotes it meanwhile...
    env.writes.reset_mock()
    agent._write_ddb({"t-1": old})  # ...then the poll pass reaches the lock
    env.writes.assert_not_called()
    item = env.table.get_item(Key={"id": "t-1"})["Item"]
    assert (item["status"], item["app_health"]) == ("running", "up")
    # A probe that started after the fast write is written as usual.
    agent._write_ddb({"t-1": {**old, "probed_at": time.monotonic()}})
    assert env.table.get_item(Key={"id": "t-1"})["Item"]["app_health"] == "down"


def test_blocked_tenant_still_gets_poll_metrics(env):
    # A CAS guard holds the tenant in creating, so the fast loop rewrites it every
    # FAST_PROMOTE_RETRY_SEC. Those writes promote nothing and must not make every
    # poll result (probed earlier in a longer pass) look stale.
    sock = _vm(env)
    _put(env, dispatch_settle="in-flight")
    before = time.monotonic()
    assert _fast({sock: 4242}) == 1
    assert _status(env) == "creating"
    assert "t-1" not in agent._status  # the fast write promoted nothing
    agent._write_ddb({"t-1": {"vm_health": "up", "app_health": "up",
                              "guest_ip": "172.16.0.2", "phys_vm_num": 1,
                              "fc_pid": 4242, "probed_at": before}})
    item = env.table.get_item(Key={"id": "t-1"})["Item"]
    assert (item["status"], item["metrics"]) == ("creating", {"cpu_pct": 0})


def test_metrics_snapshot_keeps_the_newer_probe():
    fast = {"app_health": "up", "probed_at": 2.0}
    assert agent._newer_status(fast, {"app_health": "down", "probed_at": 1.0}) is fast
    newer = {"app_health": "down", "probed_at": 3.0}
    assert agent._newer_status(fast, newer) is newer
    legacy = {"app_health": "down"}
    assert agent._newer_status(fast, legacy) is legacy
    assert agent._newer_status(None, legacy) is legacy


class _Redis:
    def __init__(self):
        self.routes = {}

    def set_route(self, tid, host_ip, port, guest_ip):
        self.routes[tid] = (port, guest_ip)
        return True


@contextlib.contextmanager
def _real_route(nat):
    """The real _ensure_route against fake iptables; yields the fake Redis."""
    redis = _Redis()
    with (
        patch.object(agent, "_ensure_route", _ensure_route),
        patch.object(agent, "_get_host_private_ip", return_value="10.0.0.1"),
        patch.object(agent, "_get_port_bitmap", return_value=nat.bitmap),
        patch.object(agent, "_get_redis_writer", return_value=redis),
    ):
        yield redis


def _released_by_another_process(nat, port=10000):
    # What delete-vm.sh (route_ops.py delete-route) does from its own process:
    # the agent is not told, and the port becomes reusable after the quarantine.
    nat.rules.pop(port)
    nat.bitmap.free(port)


def test_running_tenant_route_is_never_left_on_a_released_port(env, nat):
    # The poll pass probed a running tenant up, then delete-vm.sh released its
    # port before the pass reached it. Its route must not stay on that port: once
    # the quarantine expires the port is handed to the next tenant created here.
    _put(env, status="running")
    up = {"vm_health": "up", "app_health": "up", "guest_ip": "172.16.0.2",
          "phys_vm_num": 1, "fc_pid": 1}
    with _real_route(nat) as redis:
        agent._write_ddb({"t-1": dict(up)})
        _released_by_another_process(nat)
        agent._write_ddb({"t-1": dict(up)})
    port, _ = redis.routes["t-1"]
    assert nat.rules.get(port) == "172.16.0.2"


def test_promote_checks_live_dnat(env, nat):
    sock = _vm(env)
    _put(env)
    route_ops.ensure_port_and_dnat(nat.bitmap, "172.16.0.2")
    _released_by_another_process(nat)
    with _real_route(nat) as redis:
        assert _fast({sock: 4242}) == 1
    item = env.table.get_item(Key={"id": "t-1"})["Item"]
    assert item["status"] == "running"
    assert nat.rules.get(int(item["host_port"])) == "172.16.0.2"
    assert redis.routes["t-1"] == (int(item["host_port"]), "172.16.0.2")


def test_fast_write_leaves_metrics_to_the_poll_loop(env):
    # Balloon stats and dumpe2fs can take seconds each; the fast loop writes one
    # tenant at a time, so collecting them there held back every other ready tenant.
    sock = _vm(env)
    _put(env, metrics={"cpu_pct": 7})  # collected by an earlier poll pass
    with patch.object(agent, "_compose_metrics", side_effect=AssertionError("slow")):
        assert _fast({sock: 4242}) == 1
    item = env.table.get_item(Key={"id": "t-1"})["Item"]
    assert item["status"] == "running"
    assert item["metrics"] == {"cpu_pct": 7}  # not blanked by the promote
    agent._write_ddb({"t-1": {"vm_health": "up", "app_health": "up",
                              "guest_ip": "172.16.0.2", "phys_vm_num": 1,
                              "fc_pid": 4242, "probed_at": time.monotonic()}})
    assert env.table.get_item(Key={"id": "t-1"})["Item"]["metrics"] == {"cpu_pct": 0}


def test_poll_promote_still_writes_metrics(env):
    _put(env)
    agent._write_ddb({"t-1": {"vm_health": "up", "app_health": "up",
                              "guest_ip": "172.16.0.2", "phys_vm_num": 1,
                              "fc_pid": 4242}})
    item = env.table.get_item(Key={"id": "t-1"})["Item"]
    assert (item["status"], item["metrics"]) == ("running", {"cpu_pct": 0})


class _EndPass(BaseException):
    pass


def _one_poll_pass(probe_all):
    names = ["_write_host_heartbeat", "_reap_orphan_firecrackers", "_adjust_balloons",
             "_probe_ssm_agent", "_probe_ssm_buffer_full", "_reconcile_egress"]
    patches = [patch.object(agent, n) for n in names]
    patches += [patch.object(agent, "_probe_all", side_effect=probe_all),
                patch.object(agent, "_agent_loop_tick", side_effect=_EndPass)]
    for p in patches:
        p.start()
    try:
        with pytest.raises(_EndPass):
            agent._poll_loop()
    finally:
        for p in patches:
            p.stop()


def test_poll_pass_keeps_a_tenant_published_after_its_scan(env):
    sock = _vm(env)
    _put(env)

    def probe_all():  # VM_DIR was listed before t-1 appeared...
        assert _fast({sock: 4242}) == 1  # ...and the fast loop promotes it meanwhile
        return {}

    _one_poll_pass(probe_all)
    assert _status(env) == "running"
    assert agent._status["t-1"]["app_health"] == "up"
    # A later pass that also misses it (the tenant left) drops it.
    (env.dir / "t-1" / "vm.json").unlink()
    (env.dir / "t-1").rmdir()
    _one_poll_pass(lambda: {})
    assert "t-1" not in agent._status


def test_poll_pass_drops_an_entry_older_than_its_scan(env):
    agent._status["gone"] = {"app_health": "up", "probed_at": time.monotonic()}
    (env.dir / "gone").mkdir()
    _one_poll_pass(lambda: {})  # the pass saw the directory and did not report it
    assert "gone" not in agent._status


# ─── per-tap iptables rules are removed on stop ─────────────────

_SAVE = {
    "filter": """*filter
:INPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
-A INPUT -i tap-vm7 -p tcp -m tcp --dport 8899 -j DROP
-A INPUT -i tap-vm70 -p tcp -m tcp --dport 8899 -j DROP
-A FORWARD -d 169.254.169.254/32 -i tap-vm7 -j DROP
-A FORWARD -d 169.254.169.254/32 -i tap-vm70 -j DROP
-A FORWARD -j OPENCLAW-EGRESS
-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A FORWARD -i tap-vm7 -o ens5 -j ACCEPT
COMMIT
""",
    "nat": """*nat
:PREROUTING ACCEPT [0:0]
-A PREROUTING -i tap-vm7 -p udp -m udp --dport 53 -j DNAT --to-destination 172.16.0.1:53
-A PREROUTING -p tcp -m tcp --dport 10000 -j DNAT --to-destination 172.16.0.2:18789
COMMIT
""",
}


def _purge(tmp_path, restore_rc=0):
    """Run stop-vm.sh's purge_tap_rules against stubbed iptables tools."""
    src = (_USERDATA / "stop-vm.sh").read_text()
    start = src.index("purge_tap_rules() {")
    body = src[start:src.index("\n}\n", start) + 3]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for table, text in _SAVE.items():
        (tmp_path / f"{table}.save").write_text(text)
    stubs = {
        "sudo": 'exec "$@"',
        "iptables-save": f'if [ "$1" = -t ]; then cat {tmp_path}/"$2".save; '
                         f'else cat {tmp_path}/filter.save {tmp_path}/nat.save; fi',
        "iptables-restore": f'cat >> {tmp_path}/restore.in; exit {restore_rc}',
        "iptables": f'echo "$*" >> {tmp_path}/iptables.calls',
    }
    for name, code in stubs.items():
        p = bin_dir / name
        p.write_text(f"#!/bin/bash\n{code}\n")
        p.chmod(0o755)
    script = f'log() {{ echo "LOG $*"; }}\n{body}\npurge_tap_rules tap-vm7\n'
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    read = lambda n: (tmp_path / n).read_text() if (tmp_path / n).exists() else ""
    return read("restore.in"), read("iptables.calls"), r.stdout


def test_stop_removes_only_this_taps_rules(tmp_path):
    restore, calls, _ = _purge(tmp_path)
    assert calls == ""
    assert restore == (
        "*filter\n"
        "-D INPUT -i tap-vm7 -p tcp -m tcp --dport 8899 -j DROP\n"
        "-D FORWARD -d 169.254.169.254/32 -i tap-vm7 -j DROP\n"
        "-D FORWARD -i tap-vm7 -o ens5 -j ACCEPT\n"
        "COMMIT\n"
        "*nat\n"
        "-D PREROUTING -i tap-vm7 -p udp -m udp --dport 53 -j DNAT --to-destination 172.16.0.1:53\n"
        "COMMIT\n"
    )
    # Never another tap's rules, the shared rules, or the gateway DNAT (#548).
    assert "tap-vm70" not in restore
    assert "OPENCLAW-EGRESS" not in restore
    assert "18789" not in restore


def test_stop_falls_back_to_one_rule_at_a_time(tmp_path):
    _, calls, out = _purge(tmp_path, restore_rc=1)
    assert calls.splitlines() == [
        "-w 5 -t filter -D INPUT -i tap-vm7 -p tcp -m tcp --dport 8899 -j DROP",
        "-w 5 -t filter -D FORWARD -d 169.254.169.254/32 -i tap-vm7 -j DROP",
        "-w 5 -t filter -D FORWARD -i tap-vm7 -o ens5 -j ACCEPT",
        "-w 5 -t nat -D PREROUTING -i tap-vm7 -p udp -m udp --dport 53 -j DNAT "
        "--to-destination 172.16.0.1:53",
    ]
    # The stubs never really delete, so the survivor check must say so.
    assert "WARN: 4 iptables rules for tap-vm7 survived cleanup" in out


@pytest.mark.parametrize("tap_left", [False, True])
def test_stop_purges_only_once_the_tap_is_gone(tmp_path, tap_left):
    # A tap that survived `ip link del` is still up; purging its isolation DROPs
    # would leave it reachable with no IMDS / east-west / management-port guard.
    src = (_USERDATA / "stop-vm.sh").read_text()
    start = src.index('sudo ip link del "tap-vm${VM_NUM}"')
    block = src[start:src.index('rm -f "${VM_DIR}/fc.sock"', start)]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, code in {"sudo": 'exec "$@"',
                       "ip": f'[ "$2" = show ] && exit {0 if tap_left else 1}; exit 0'}.items():
        (bin_dir / name).write_text(f"#!/bin/bash\n{code}\n")
        (bin_dir / name).chmod(0o755)
    script = ('log() { echo "LOG $*"; }\n'
              'purge_tap_rules() { echo "PURGE $1"; }\n'
              f"VM_NUM=7\n{block}")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    out = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                         text=True, check=True).stdout
    if tap_left:
        assert "PURGE" not in out
        assert "WARN: tap-vm7 still exists" in out
    else:
        assert out == "PURGE tap-vm7\n"
