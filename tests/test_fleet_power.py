# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for fleet power — start/stop EVERY VM via host-local fan-out.

Endpoint: POST /hosts/fleet-power   Body: {"action": "start"|"stop"}

The 1-minute fleet-power goal hinges on this NOT being one-SSM-per-VM. These
tests pin the architecture invariant: ONE send_command carrying the full host
list (concurrency = host count, not VM count), admin-gated, and the host-local
script + bounded-parallel arg are what's dispatched.
"""

import json
import sys
import importlib.util
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError
from conftest import make_ddb_table
import pytest

# All tests in this module are pure-mock unit tests (no real AWS); mark them
# so `pytest -m unit` includes them (loop 2026-07-02: found 136 tests were
# silently excluded from the unit suite for lack of this marker).
pytestmark = pytest.mark.unit


_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
_mock_s3 = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm,
        "s3": _mock_s3,
    }.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_fleet", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_fleet"] = api
    spec.loader.exec_module(api)


def _admin_event():
    """Patch identity to admin (API-key path equivalent)."""
    return {"headers": {}, "requestContext": {}}


def _setup_hosts(host_ids):
    """Point api.hosts_table.scan at a fixed host list."""
    tbl = make_ddb_table()
    tbl.scan.return_value = {
        "Items": [{"instance_id": h, "status": "active"} for h in host_ids]
    }
    api.hosts_table = tbl


def _ssm_returns(cmd_id="cmd-123"):
    _mock_ssm.reset_mock()
    _mock_ssm.send_command.return_value = {"Command": {"CommandId": cmd_id}}


def test_non_admin_forbidden():
    _setup_hosts(["i-1"])
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": False}):
        resp = api.fleet_power(json.dumps({"action": "stop"}), _admin_event())
    assert resp["statusCode"] == 403
    assert "admin" in json.loads(resp["body"])["error"]


def test_invalid_action():
    _setup_hosts(["i-1"])
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "reboot"}), _admin_event())
    assert resp["statusCode"] == 400


def test_no_hosts_returns_200_zero():
    _setup_hosts([])
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "start"}), _admin_event())
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["hosts"] == 0
    # No hosts → no SSM dispatched.
    assert _mock_ssm.send_command.call_count == 0


def test_stop_one_command_all_hosts():
    """THE invariant: one send_command, InstanceIds = every host, stop script."""
    hosts = [f"i-{n}" for n in range(20)]
    _setup_hosts(hosts)
    _ssm_returns("cmd-stop")
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "stop"}), _admin_event())
    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["hosts"] == 20
    assert body["command_id"] == "cmd-stop"
    # Exactly ONE SSM call for all 20 hosts (not 20, not per-VM).
    assert _mock_ssm.send_command.call_count == 1
    kwargs = _mock_ssm.send_command.call_args.kwargs
    assert sorted(kwargs["InstanceIds"]) == sorted(hosts)
    assert "stop-all-vms.sh" in kwargs["Parameters"]["commands"][0]
    # SSM fans out to all hosts at once.
    assert kwargs["MaxConcurrency"] == "100%"


def test_start_dispatches_start_script_with_parallel_arg():
    hosts = ["i-a", "i-b"]
    _setup_hosts(hosts)
    _ssm_returns("cmd-start")
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "start"}), _admin_event())
    assert resp["statusCode"] == 202
    cmd = _mock_ssm.send_command.call_args.kwargs["Parameters"]["commands"][0]
    assert "start-all-vms.sh" in cmd
    # bounded-parallel arg is passed (default 96 = metal vCPU count; measured
    # flat vs higher on 380-VM start, so 96 is the sweet spot).
    assert "start-all-vms.sh 96" in cmd


def test_ssm_failure_returns_502():
    _setup_hosts(["i-1"])
    _mock_ssm.reset_mock()
    _mock_ssm.send_command.side_effect = Exception("throttled")
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "stop"}), _admin_event())
    assert resp["statusCode"] == 502
    _mock_ssm.send_command.side_effect = None


def test_stop_reconciles_tenant_status_to_stopped():
    """Loop 2026-07-01 bugfix: fleet-power stop must reconcile DDB tenant.status
    to 'stopped' for tenants on the affected hosts — fleet_power itself doesn't
    touch the tenant table and host-agent skips .stopped VMs, so without this
    the console would show a stale 'running' forever."""
    _setup_hosts(["i-1", "i-2"])
    _ssm_returns("cmd-stop")
    # tenants: 2 running on affected hosts + 1 on another host + 1 deleted
    tenants_tbl = make_ddb_table()
    tenants_tbl.scan.return_value = {
        "Items": [
            {"id": "t1", "host_id": "i-1", "status": "running"},
            {"id": "t2", "host_id": "i-2", "status": "running"},
            {"id": "t3", "host_id": "i-OTHER", "status": "running"},
            {"id": "t4", "host_id": "i-1", "status": "deleted"},
        ]
    }
    api.tenants_table = tenants_tbl
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "stop"}), _admin_event())
    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    # t1 + t2 reconciled to stopped; t3 (other host) + t4 (deleted, filtered) not.
    assert body["reconciled"] == 2
    updated_ids = {
        c.kwargs["Key"]["id"] for c in tenants_tbl.update_item.call_args_list
    }
    assert updated_ids == {"t1", "t2"}
    for c in tenants_tbl.update_item.call_args_list:
        assert c.kwargs["ExpressionAttributeValues"][":s"] == "stopped"


def test_start_reconciles_tenant_status_to_running():
    _setup_hosts(["i-1"])
    _ssm_returns("cmd-start")
    tenants_tbl = make_ddb_table()
    tenants_tbl.scan.return_value = {
        "Items": [
            {"id": "s1", "host_id": "i-1", "status": "stopped"},
            {"id": "s2", "host_id": "i-1", "status": "running"},  # already running
        ]
    }
    api.tenants_table = tenants_tbl
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "start"}), _admin_event())
    assert resp["statusCode"] == 202
    # only s1 (stopped→running) reconciled; s2 already running → skipped.
    assert json.loads(resp["body"])["reconciled"] == 1
    updated_ids = {
        c.kwargs["Key"]["id"] for c in tenants_tbl.update_item.call_args_list
    }
    assert updated_ids == {"s1"}


def test_stop_does_not_clobber_transitional_states():
    """Loop 2026-07-01 boundary bugfix: fleet-power stop only flips running→
    stopped; a tenant mid-creating/pending/migrating/paused must NOT be forced
    to stopped (owned by its own flow — clobbering races host-agent's
    creating→running promotion)."""
    _setup_hosts(["i-1"])
    _ssm_returns("cmd-stop")
    tenants_tbl = make_ddb_table()
    tenants_tbl.scan.return_value = {
        "Items": [
            {"id": "r1", "host_id": "i-1", "status": "running"},  # → stopped
            {"id": "c1", "host_id": "i-1", "status": "creating"},  # skip
            {"id": "p1", "host_id": "i-1", "status": "pending"},  # skip
            {"id": "m1", "host_id": "i-1", "status": "migrating"},  # skip
            {"id": "pa1", "host_id": "i-1", "status": "paused"},  # skip
        ]
    }
    api.tenants_table = tenants_tbl
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "stop"}), _admin_event())
    assert resp["statusCode"] == 202
    # only the running tenant reconciled; transitional ones untouched.
    assert json.loads(resp["body"])["reconciled"] == 1
    updated_ids = {
        c.kwargs["Key"]["id"] for c in tenants_tbl.update_item.call_args_list
    }
    assert updated_ids == {"r1"}


def test_start_only_flips_stopped_not_creating():
    """start only flips stopped→running; creating/paused left alone."""
    _setup_hosts(["i-1"])
    _ssm_returns("cmd-start")
    tenants_tbl = make_ddb_table()
    tenants_tbl.scan.return_value = {
        "Items": [
            {"id": "s1", "host_id": "i-1", "status": "stopped"},  # → running
            {"id": "c1", "host_id": "i-1", "status": "creating"},  # skip
            {"id": "pa1", "host_id": "i-1", "status": "paused"},  # skip
        ]
    }
    api.tenants_table = tenants_tbl
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "start"}), _admin_event())
    assert json.loads(resp["body"])["reconciled"] == 1
    updated_ids = {
        c.kwargs["Key"]["id"] for c in tenants_tbl.update_item.call_args_list
    }
    assert updated_ids == {"s1"}


def test_reconcile_cas_failure_is_swallowed_not_counted():
    """Loop 2026-07-02 race coverage: scan reads a row in the source state, but
    by the time update_item runs a concurrent lifecycle transition already moved
    it — the CAS ConditionExpression fails. fleet_power must swallow that
    ConditionalCheckFailedException (lose safely to the concurrent write), NOT
    count it as reconciled, NOT crash, and still return 202. Without this the
    stop/start interleave could error out or double-count."""
    _setup_hosts(["i-1"])
    _ssm_returns("cmd-stop")
    tenants_tbl = make_ddb_table()
    tenants_tbl.scan.return_value = {
        "Items": [
            {"id": "r1", "host_id": "i-1", "status": "running"},  # CAS will fail
            {"id": "r2", "host_id": "i-1", "status": "running"},  # CAS ok
        ]
    }

    # r1's update loses the CAS race (concurrent promotion/stop already moved
    # it); r2 succeeds. ClientError with ConditionalCheckFailedException is the
    # exact shape boto3 raises on a failed ConditionExpression.
    ccf = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "stale"}},
        "UpdateItem",
    )

    def _update(**kwargs):
        if kwargs["Key"]["id"] == "r1":
            raise ccf
        return {}

    tenants_tbl.update_item.side_effect = _update
    api.tenants_table = tenants_tbl
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "stop"}), _admin_event())
    # 202 despite the CAS race; only r2 counted (r1 lost the race, not counted).
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["reconciled"] == 1
    # both were attempted (proves we didn't stop on the first failure).
    attempted = {c.kwargs["Key"]["id"] for c in tenants_tbl.update_item.call_args_list}
    assert attempted == {"r1", "r2"}


def test_reconcile_non_cas_error_does_not_abort_fleet():
    """A non-CAS DDB error on one tenant's reconcile must be logged and skipped,
    not abort the whole fleet-power (SSM already dispatched). The other tenant
    still reconciles and the response is still 202."""
    _setup_hosts(["i-1"])
    _ssm_returns("cmd-stop")
    tenants_tbl = make_ddb_table()
    tenants_tbl.scan.return_value = {
        "Items": [
            {"id": "r1", "host_id": "i-1", "status": "running"},  # throws
            {"id": "r2", "host_id": "i-1", "status": "running"},  # ok
        ]
    }
    boom = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
        "UpdateItem",
    )

    def _update(**kwargs):
        if kwargs["Key"]["id"] == "r1":
            raise boom
        return {}

    tenants_tbl.update_item.side_effect = _update
    api.tenants_table = tenants_tbl
    with patch.object(api, "_get_caller_identity", return_value={"is_admin": True}):
        resp = api.fleet_power(json.dumps({"action": "stop"}), _admin_event())
    assert resp["statusCode"] == 202
    # r1 failed (not counted), r2 still reconciled — fleet not aborted.
    assert json.loads(resp["body"])["reconciled"] == 1
    attempted = {c.kwargs["Key"]["id"] for c in tenants_tbl.update_item.call_args_list}
    assert attempted == {"r1", "r2"}
