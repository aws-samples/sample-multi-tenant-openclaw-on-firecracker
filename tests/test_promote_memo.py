# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the promote-memo fix in host-agent.py `_write_ddb` (issue #247).

The bug: every already-`running` tenant paid for one promote conditional write per
poll tick. The condition is gated on `#s = :c` (creating), so for a running tenant it
can only evaluate to false — and DynamoDB bills a failed conditional write at the full
item size. That is what made the base table burn exactly 2x every ALL-projection GSI.

The fix must be exact, not heuristic: a `ConditionalCheckFailedException` has four
causes, and two of them leave the stored status AT `creating` and therefore MUST keep
retrying. These tests pin both directions.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest


# host-agent.py does a bare `import route_ops` (its own directory), so that
# directory has to be importable. Doing it here keeps this module self-contained:
# it does not rely on conftest.py or on another test module having run first.
_UD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy", "userdata")
if _UD not in sys.path:
    sys.path.insert(0, _UD)

os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
os.environ.setdefault("HOSTS_TABLE", "openclaw-hosts")
os.environ.setdefault("ASSETS_BUCKET", "test-bucket")
os.environ.setdefault(
    "PORT_QUARANTINE_FILE", os.path.join("/tmp", f"oc-quarantine-{os.getpid()}.json")
)

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with (
    patch("boto3.resource", return_value=_mock_ddb),
    patch("boto3.client", return_value=_mock_ssm),
):
    spec = importlib.util.spec_from_file_location(
        "agent", "deploy/userdata/host-agent.py"
    )
    agent = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = agent
    spec.loader.exec_module(agent)


class FakeCCF(Exception):
    """Stands in for ConditionalCheckFailedException, carrying botocore's response.

    `Item` is deliberately in RAW DynamoDB shape, because that is what the resource
    layer actually hands back for ReturnValuesOnConditionCheckFailure — verified
    against a live table, not assumed.
    """

    def __init__(self, item=None):
        super().__init__("The conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        if item is not None:
            self.response["Item"] = item


def _table():
    t = MagicMock()
    t.meta.client.exceptions.ConditionalCheckFailedException = FakeCCF
    return t


def _info(guest_ip="172.16.0.2", phys=1):
    return {
        "vm_health": "up",
        "app_health": "up",
        "guest_ip": guest_ip,
        "phys_vm_num": phys,
        "fc_pid": 4242,
    }


def _promote_calls(table):
    """update_item calls that are the promote conditional write, not the phys backfill."""
    out = []
    for c in table.update_item.call_args_list:
        expr = c.kwargs.get("UpdateExpression", "")
        if "#s = :r" in expr or ":r" in c.kwargs.get("ExpressionAttributeValues", {}):
            out.append(c)
    return out


@pytest.fixture(autouse=True)
def _isolate():
    """Fresh module state per test, and never touch a real socket or DDB."""
    agent._promoted.clear()
    agent._phys_backfilled.clear()
    with (
        patch.object(agent, "TENANTS_TABLE", "openclaw-tenants"),
        patch.object(agent, "INSTANCE_ID", "i-0test"),
        patch.object(agent, "_compose_metrics", return_value={"cpu_pct": 0}),
        patch.object(
            agent, "_ensure_route", return_value=("10.30.4.105", 10000)
        ) as route,
        patch.object(agent, "_refresh_health") as refresh,
    ):
        yield route, refresh
    agent._promoted.clear()
    agent._phys_backfilled.clear()


def _run(table, results):
    with patch.object(agent, "_get_ddb") as g:
        g.return_value.Table.return_value = table
        agent._write_ddb(results)


# ───────────────────────── the saving ─────────────────────────


def test_running_tenant_pays_for_the_condition_only_once(_isolate):
    """Tick 1 burns the doomed condition; tick 2 must not. This IS the cost fix."""
    t = _table()
    agent._phys_backfilled.add("t-1")  # take the backfill write out of the picture
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})

    _run(t, {"t-1": _info()})
    assert len(_promote_calls(t)) == 1, "tick 1 should issue the promote write once"
    assert "t-1" in agent._promoted, "a stored status of `running` proves it is past creating"

    t.update_item.reset_mock()
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})
    _run(t, {"t-1": _info()})
    assert _promote_calls(t) == [], "tick 2 must issue NO promote write"


def test_successful_promote_is_recorded(_isolate):
    """A fresh creating->running promote still happens, and is remembered."""
    t = _table()
    agent._phys_backfilled.add("t-2")
    t.update_item.side_effect = None  # succeeds

    _run(t, {"t-2": _info()})
    assert len(_promote_calls(t)) == 1
    assert "t-2" in agent._promoted

    t.update_item.reset_mock()
    _run(t, {"t-2": _info()})
    assert _promote_calls(t) == [], "a promoted tenant must not be re-promoted"


def test_all_old_is_requested(_isolate):
    """Without ALL_OLD the handler cannot tell the CCF causes apart."""
    t = _table()
    agent._phys_backfilled.add("t-3")
    t.update_item.side_effect = None
    _run(t, {"t-3": _info()})
    kw = _promote_calls(t)[0].kwargs
    assert kw.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD"


# ─────────────── the two causes that MUST keep retrying ───────────────


def test_vm_num_gate_mismatch_keeps_retrying(_isolate):
    """A stale local vm.json fails the `vm_num = :phys` gate while status is STILL
    `creating`. Recording that tenant would strand it: this host would never try to
    promote it again, even after the orphan VM is reaped.

    Reverse check: drop the `.get("S")` unwrap in the handler and this test turns red,
    because `{"S": "creating"} != "creating"` is always true.
    """
    t = _table()
    agent._phys_backfilled.add("t-4")
    t.update_item.side_effect = FakeCCF({"status": {"S": "creating"}})

    _run(t, {"t-4": _info()})
    assert "t-4" not in agent._promoted, "status is still `creating` — must not memoise"

    t.update_item.reset_mock()
    t.update_item.side_effect = FakeCCF({"status": {"S": "creating"}})
    _run(t, {"t-4": _info()})
    assert len(_promote_calls(t)) == 1, "tick 2 must retry the promote"


def test_dispatch_settle_inflight_keeps_retrying(_isolate):
    """`attribute_not_exists(dispatch_settle)` can fail while status is `creating`.
    Same rule: keep retrying, because settle clears and the promote then succeeds."""
    t = _table()
    agent._phys_backfilled.add("t-5")
    t.update_item.side_effect = FakeCCF(
        {"status": {"S": "creating"}, "dispatch_settle": {"S": "2026-09-18T00:00:00Z"}}
    )
    _run(t, {"t-5": _info()})
    assert "t-5" not in agent._promoted


# ─────────────── the causes that are safe to memoise ───────────────


@pytest.mark.parametrize(
    "item, why",
    [
        ({"status": {"S": "running"}}, "already running (the common case)"),
        ({"status": {"S": "migrating"}}, "migrated away — not ours to promote"),
        ({"status": {"S": "stopped"}}, "past creating"),
        (None, "row gone: deleted, or its dispatch reservation was released"),
    ],
)
def test_causes_that_are_past_creating_are_memoised(_isolate, item, why):
    t = _table()
    agent._phys_backfilled.add("t-6")
    t.update_item.side_effect = FakeCCF(item)
    _run(t, {"t-6": _info()})
    assert "t-6" in agent._promoted, f"should memoise: {why}"


# ─────────── what must NOT regress (the three self-heal contracts) ───────────


def test_ensure_route_still_runs_on_every_tick(_isolate):
    """The memo path skips ONLY the update_item. _ensure_route must still run, because
    the host_port reconciliation on the refresh path depends on its live return value.
    Skipping it would leave a restored tenant advertising a port never installed in
    iptables — unreachable while every health field reads green."""
    route, _ = _isolate
    t = _table()
    agent._phys_backfilled.add("t-7")
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})

    _run(t, {"t-7": _info()})
    _run(t, {"t-7": _info()})
    assert route.call_count == 2, "_ensure_route must be called on BOTH ticks"


def test_refresh_health_receives_host_port_on_the_memo_path(_isolate):
    """The memoised tick must hand host_port down, exactly like the CCF fallback did."""
    _, refresh = _isolate
    t = _table()
    agent._phys_backfilled.add("t-8")
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})
    _run(t, {"t-8": _info()})          # tick 1: CCF fallback
    _run(t, {"t-8": _info()})          # tick 2: memo path
    assert refresh.call_count == 2
    for c in refresh.call_args_list:
        assert c.kwargs.get("host_port") == 10000, (
            "host_port must reach _refresh_health on both the CCF and the memo path"
        )


def test_refresh_health_still_runs_on_the_memo_path(_isolate):
    """guest_ip drift, host_port reconciliation and restore's app_health=down flip all
    depend on _refresh_health running every tick."""
    _, refresh = _isolate
    t = _table()
    agent._phys_backfilled.add("t-9")
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})
    _run(t, {"t-9": _info()})
    _run(t, {"t-9": _info()})
    _run(t, {"t-9": _info()})
    assert refresh.call_count == 3, "every tick must still refresh health"


def test_restart_costs_exactly_one_extra_attempt(_isolate):
    """Process state is cleared on restart. That is self-healing, and its cost is
    bounded at one extra conditional write per tenant."""
    t = _table()
    agent._phys_backfilled.add("t-10")
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})
    _run(t, {"t-10": _info()})
    assert len(_promote_calls(t)) == 1

    agent._promoted.clear()            # simulate an agent restart
    t.update_item.reset_mock()
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})
    _run(t, {"t-10": _info()})
    assert len(_promote_calls(t)) == 1, "one extra attempt after restart, then quiet"
    t.update_item.reset_mock()
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})
    _run(t, {"t-10": _info()})
    assert _promote_calls(t) == []


def test_memo_is_per_tenant(_isolate):
    """One tenant going quiet must not silence another."""
    t = _table()
    agent._phys_backfilled.update({"t-a", "t-b"})
    t.update_item.side_effect = FakeCCF({"status": {"S": "running"}})
    _run(t, {"t-a": _info(phys=1)})
    assert agent._promoted == {"t-a"}

    t.update_item.reset_mock()
    t.update_item.side_effect = FakeCCF({"status": {"S": "creating"}})
    _run(t, {"t-b": _info(guest_ip="172.16.0.6", phys=2)})
    assert agent._promoted == {"t-a"}, "t-b is still creating and must stay retryable"
    assert len(_promote_calls(t)) == 1


def test_gate_still_requires_both_health_signals(_isolate):
    """The memo must not widen the promote gate: a VM whose gateway is down still gets
    a health-only refresh and no promote."""
    route, refresh = _isolate
    t = _table()
    agent._phys_backfilled.add("t-11")
    info = _info()
    info["app_health"] = "down"
    _run(t, {"t-11": info})
    assert _promote_calls(t) == []
    assert route.call_count == 0, "the gateway-down path must not allocate a route"
    assert refresh.call_count == 1
    assert "t-11" not in agent._promoted


def test_ensure_route_failure_does_not_memoise(_isolate):
    """If the route cannot be established the tenant stays at creating; memoising it
    would strand it at `creating` forever."""
    route, _ = _isolate
    route.side_effect = RuntimeError("bitmap exhausted")
    t = _table()
    agent._phys_backfilled.add("t-12")
    _run(t, {"t-12": _info()})
    assert _promote_calls(t) == []
    assert "t-12" not in agent._promoted
