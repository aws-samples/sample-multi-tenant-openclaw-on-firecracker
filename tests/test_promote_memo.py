# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Promotion cost and lifecycle regressions for issue #247 / PR #248.

Run real health and promote writes against Moto's DynamoDB expressions.
Mock only host metrics and route installation. A process-lifetime promote memo
stranded a healthy tenant after deleting -> creating rollback; observe status on
every poll without adding a read or a second steady-state write.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws


_AGENT_FILE = Path(__file__).resolve().parents[1] / "deploy/userdata/host-agent.py"
with patch("boto3.resource"), patch("boto3.client"):
    spec = importlib.util.spec_from_file_location("promote_agent", _AGENT_FILE)
    agent = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = agent
    spec.loader.exec_module(agent)


def _info(**overrides):
    return {
        "vm_health": "up", "app_health": "up", "guest_ip": "172.16.0.2",
        "phys_vm_num": 1, "fc_pid": 4242, **overrides,
    }


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
        agent._phys_backfilled.clear()
        # Keep the one-time backfill out of per-poll cost assertions.
        agent._phys_backfilled.add("t-1")
        with (
            patch.object(agent, "TENANTS_TABLE", table.name),
            patch.object(agent, "INSTANCE_ID", "i-test"),
            patch.object(agent, "VM_DIR", str(tmp_path)),
            patch.object(agent, "_get_ddb") as get_ddb,
            patch.object(agent, "_compose_metrics", return_value={"cpu_pct": 0}),
            patch.object(agent, "_ensure_route", return_value=("10.0.0.1", 10000)) as route,
            patch.object(table, "update_item", wraps=table.update_item) as writes,
            patch.object(table, "get_item", wraps=table.get_item) as reads,
        ):
            get_ddb.return_value.Table.return_value = table
            yield table, route, writes, reads
        agent._phys_backfilled.clear()


def _put(table, status="creating", **overrides):
    item = {
        "id": "t-1", "status": status, "host_id": "i-test", "vm_num": 1,
        "capacity_reservation_id": "reservation-1",
        "secret": "must-not-be-logged", **overrides,
    }
    table.put_item(Item=item)
    return item


def _item(table):
    return table.get_item(Key={"id": "t-1"}, ConsistentRead=True).get("Item")


def _tick(**info):
    agent._write_ddb({"t-1": _info(**info)})


def _promotes(writes):
    return [c for c in writes.call_args_list
            if ":r" in c.kwargs.get("ExpressionAttributeValues", {})]


def test_running_costs_one_write_per_tick_and_no_read(env, capsys):
    table, route, writes, reads = env
    _put(table, "running", guest_ip="172.16.0.99", host_port=9999)
    for _ in range(10):
        _tick()
    assert writes.call_count == 10
    assert reads.call_count == 0
    assert not _promotes(writes)
    assert route.call_count == 10
    item = _item(table)
    assert item["guest_ip"] == "172.16.0.2"
    assert item["host_port"] == 10000
    assert item["metrics"] == {"cpu_pct": 0}
    assert item["vm_health"] == item["app_health"] == "up"
    assert "must-not-be-logged" not in capsys.readouterr().out


def test_creating_promotes_then_costs_one_write_per_tick(env):
    table, _, writes, reads = env
    _put(table)
    _tick()
    assert len(_promotes(writes)) == 1
    item = _item(table)
    assert item["status"] == "running"
    assert "capacity_reservation_id" not in item
    assert item["host_private_ip"] == "10.0.0.1"
    assert item["host_port"] == 10000
    writes.reset_mock()
    reads.reset_mock()
    for _ in range(10):
        _tick()
    assert writes.call_count == 10
    assert reads.call_count == 0
    assert not _promotes(writes)


def test_failed_delete_rollback_promotes_on_next_tick(env):
    table, _, writes, _ = env
    item = _put(table)
    # Delete's CAS wins before the healthy VM's first poll; backup later fails.
    table.put_item(Item={**item, "status": "deleting", "delete_prev_status": "creating"})
    _tick()
    assert _item(table)["status"] == "deleting"
    table.put_item(Item=item)  # Model _abort_restore_status restoring prev_status.
    _tick()
    assert _item(table)["status"] == "running"
    assert len(_promotes(writes)) == 1


@pytest.mark.parametrize("status", ["running", "migrating", "stopped", "deleted"])
def test_observed_status_does_not_hide_later_creating(env, status):
    table, _, writes, _ = env
    _put(table, status)
    _tick()
    assert _item(table)["status"] == status
    assert not _promotes(writes)
    _put(table, "creating")
    _tick()
    assert _item(table)["status"] == "running"


def test_deleted_and_recreated_id_can_promote(env):
    table, _, writes, _ = env
    _put(table, "running")
    _tick()
    table.delete_item(Key={"id": "t-1"})
    _tick()
    assert _item(table) is None
    _put(table)
    _tick()
    assert _item(table)["status"] == "running"
    assert len(_promotes(writes)) == 1


@pytest.mark.parametrize("blocker", [
    {"vm_num": 2}, {"dispatch_settle": "in-flight"}, {"host_id": "i-other"},
])
def test_promotion_retries_after_guard_clears(env, blocker):
    table, _, _, _ = env
    _put(table, **blocker)
    for _ in range(2):
        _tick()
        assert _item(table)["status"] == "creating"
        assert _item(table)["capacity_reservation_id"] == "reservation-1"
    _put(table)
    _tick()
    assert _item(table)["status"] == "running"


@pytest.mark.parametrize("change", [
    {"status": "deleting"}, {"host_id": "i-other"}, {"vm_num": 2},
    {"dispatch_settle": "in-flight"}, None,
])
def test_promote_cas_blocks_changes_after_health_write(env, change):
    table, _, writes, _ = env
    _put(table)
    update = writes._mock_wraps

    def race(**kwargs):
        if ":r" in kwargs.get("ExpressionAttributeValues", {}):
            if change is None:
                table.delete_item(Key={"id": "t-1"})
            else:
                table.put_item(Item={**_item(table), **change})
        return update(**kwargs)

    writes.side_effect = race
    _tick()
    assert len(_promotes(writes)) == 1
    item = _item(table)
    if change is None:
        assert item is None
    else:
        assert item["status"] == change.get("status", "creating")
        assert item["capacity_reservation_id"] == "reservation-1"
        for key, value in change.items():
            assert item[key] == value


@pytest.mark.parametrize("health", [{"vm_health": "down"}, {"app_health": "down"}])
def test_both_health_signals_required_and_recovery_retries(env, health):
    table, route, writes, _ = env
    _put(table)
    _tick(**health)
    assert _item(table)["status"] == "creating"
    assert not _promotes(writes)
    assert route.call_count == 0
    _tick()
    assert _item(table)["status"] == "running"


@pytest.mark.parametrize("result", [("", 10000), ("10.0.0.1", None), RuntimeError("no route")])
def test_route_failure_blocks_promotion_and_retries(env, result):
    table, route, writes, _ = env
    _put(table)
    if isinstance(result, Exception):
        route.side_effect = result
    else:
        route.return_value = result
    _tick()
    assert not _promotes(writes)
    assert _item(table)["status"] == "creating"
    route.side_effect = None
    route.return_value = ("10.0.0.1", 10000)
    _tick()
    assert _item(table)["status"] == "running"


def test_health_write_failure_does_not_promote_or_prevent_retry(env):
    table, _, writes, _ = env
    _put(table)
    writes.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "retry"}},
        "UpdateItem",
    )
    _tick()
    assert not _promotes(writes)
    assert _item(table)["status"] == "creating"
    writes.side_effect = None
    _tick()
    assert _item(table)["status"] == "running"


def test_missing_returned_status_does_not_promote(env):
    table, _, writes, _ = env
    _put(table)
    writes.return_value = {}  # Unexpected/missing response must fail closed.
    _tick()
    assert not _promotes(writes)
    assert _item(table)["status"] == "creating"


def test_health_write_cannot_update_other_host_or_resurrect_row(env):
    table, _, writes, _ = env
    _put(table, host_id="i-other", guest_ip="172.16.0.99", host_port=9999)
    _tick()
    item = _item(table)
    assert item["guest_ip"] == "172.16.0.99"
    assert item["host_port"] == 9999
    assert not _promotes(writes)
    table.delete_item(Key={"id": "t-1"})
    _tick()
    assert _item(table) is None


def test_legacy_vm_without_physical_slot_still_promotes(env):
    table, _, _, _ = env
    _put(table)
    _tick(phys_vm_num=None)
    assert _item(table)["status"] == "running"
