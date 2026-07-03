# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Phase 2 — create_tenant via FIFO queue (load-shed a burst).

Pins the contract:
- CREATE_VIA_QUEUE off            → create runs synchronously (no enqueue).
- CREATE_VIA_QUEUE on  + queue    → cheap validation still synchronous (bad
  body → 400 immediately, NOT queued), good body → enqueued + 202 with the
  pre-assigned id.
- consumer replay (_consumer_ident) → NOT re-enqueued (no infinite loop), and
  reuses the _assigned_tenant_id (no second _gen_id → no ghost tenant).
"""

import json
import sys
import importlib.util
from unittest.mock import patch, MagicMock
from conftest import make_ddb_table
import pytest

# All tests in this module are pure-mock unit tests (no real AWS); mark them
# so `pytest -m unit` includes them (loop 2026-07-02: found 136 tests were
# silently excluded from the unit suite for lack of this marker).
pytestmark = pytest.mark.unit


_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
_mock_s3 = MagicMock()
_mock_sqs = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm,
        "s3": _mock_s3,
        "sqs": _mock_sqs,
    }.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_cvq", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_cvq"] = api
    spec.loader.exec_module(api)


def _fresh_tables():
    api.tenants_table = make_ddb_table()
    api.hosts_table = make_ddb_table()
    api.groups_table = make_ddb_table()


def _admin():
    return {
        "owner_id": "admin-sub",
        "is_admin": True,
        "tenant_user_id": None,
    }


def test_queue_off_runs_sync():
    """CREATE_VIA_QUEUE off → no enqueue, falls through to sync provisioning."""
    _fresh_tables()
    _mock_sqs.reset_mock()
    with (
        patch.object(api, "CREATE_VIA_QUEUE", False),
        patch.object(api, "LIFECYCLE_QUEUE_URL", "http://q.fifo"),
        patch.object(api, "_get_caller_identity", return_value=_admin()),
        patch.object(api, "_find_host", return_value=None),
        patch.object(api, "_scale_out"),
    ):
        # no host → pending path, but the point is: NOT queued.
        resp = api.create_tenant(json.dumps({"name": "alpha"}), {})
    assert api.enqueue_lifecycle  # sanity
    # sync path returns 201 (pending), never 202 queued.
    assert resp["statusCode"] == 201
    assert _mock_sqs.send_message.call_count == 0


def test_queue_on_enqueues_and_returns_202():
    _fresh_tables()
    _mock_sqs.reset_mock()
    with (
        patch.object(api, "CREATE_VIA_QUEUE", True),
        patch.object(api, "LIFECYCLE_QUEUE_URL", "http://q.fifo"),
        patch.object(api, "sqs", _mock_sqs),
        patch.object(api, "_get_caller_identity", return_value=_admin()),
    ):
        resp = api.create_tenant(json.dumps({"name": "beta"}), {})
    assert resp["statusCode"] == 202
    body = json.loads(resp["body"])
    assert body["status"] == "queued"
    assert body["id"]  # pre-assigned id handed to caller
    # enqueued exactly once, FIFO group/dedup set, body carries the assigned id.
    assert _mock_sqs.send_message.call_count == 1
    kwargs = _mock_sqs.send_message.call_args.kwargs
    assert kwargs["MessageGroupId"] == body["id"]
    sent = json.loads(kwargs["MessageBody"])
    assert sent["action"] == "create"
    assert sent["extra"]["_assigned_tenant_id"] == body["id"]
    assert sent["extra"]["_enqueued_at"]


def test_queue_on_bad_body_still_sync_400():
    """Cheap validation stays synchronous: a bad name 400s immediately, NOT 202."""
    _fresh_tables()
    _mock_sqs.reset_mock()
    with (
        patch.object(api, "CREATE_VIA_QUEUE", True),
        patch.object(api, "LIFECYCLE_QUEUE_URL", "http://q.fifo"),
        patch.object(api, "_get_caller_identity", return_value=_admin()),
    ):
        resp = api.create_tenant(json.dumps({"name": "Invalid Name With Spaces!"}), {})
    assert resp["statusCode"] == 400
    assert _mock_sqs.send_message.call_count == 0


def test_consumer_replay_not_requeued_and_reuses_id():
    """Consumer replay must NOT re-enqueue (loop) and must reuse the assigned id."""
    _fresh_tables()
    _mock_sqs.reset_mock()
    assigned = "beta-deadbeef"
    ev = {"_consumer_ident": {"owner_id": "admin-sub", "is_admin": True}}
    body = {"name": "beta", "_assigned_tenant_id": assigned}
    with (
        patch.object(api, "CREATE_VIA_QUEUE", True),
        patch.object(api, "LIFECYCLE_QUEUE_URL", "http://q.fifo"),
        patch.object(api, "_get_caller_identity", return_value=_admin()),
        patch.object(api, "_find_host", return_value=None),
        patch.object(api, "_scale_out"),
    ):
        resp = api.create_tenant(json.dumps(body), ev)
    # replay provisions (pending here, no host) — and was NOT re-queued.
    assert resp["statusCode"] == 201
    assert _mock_sqs.send_message.call_count == 0
    # reused the assigned id, didn't mint a new one.
    assert json.loads(resp["body"])["id"] == assigned


def test_launch_ssm_throttled_rolls_back_and_502():
    """Loop 2026-07-01 bugfix: if launch-vm's SSM SendCommand is throttled
    (_launch_vm returns None), create_tenant must roll back the capacity slot +
    mark the tenant deleted + return 502 — so the SQS consumer re-queues instead
    of leaving it stuck 'creating' with a leaked slot forever."""
    _fresh_tables()
    host = {
        "instance_id": "i-1",
        "total_vcpu": 96,
        "total_mem_mb": 700000,
        "used_vcpu": 0,
        "used_mem_mb": 0,
        "next_vm_num": 1,
        "vm_count": 0,
        "private_ip": "10.0.0.1",
    }
    # _reserve_slot is a nested closure inside create_tenant, not patchable;
    # let it run against the mock table (update_item returns next_vm_num=2 →
    # claims slot 1). We drive the failure via _launch_vm returning None.
    with (
        patch.object(api, "CREATE_VIA_QUEUE", False),
        patch.object(api, "LIFECYCLE_QUEUE_URL", ""),
        patch.object(api, "_get_caller_identity", return_value=_admin()),
        patch.object(api, "_find_host", return_value=host),
        patch.object(api, "_launch_vm", return_value=None) as mlaunch,
        patch.object(api, "_release_slot") as mrelease,
        patch.object(api, "_mint_tenant_vkey", return_value=""),
    ):
        resp = api.create_tenant(json.dumps({"name": "thr"}), {})
    assert resp["statusCode"] == 502
    assert (
        "throttled" in json.loads(resp["body"])["error"].lower()
        or "retry" in json.loads(resp["body"])["error"].lower()
    )
    mlaunch.assert_called_once()
    # slot rolled back (не leaked)
    mrelease.assert_called_once()
    # tenant marked deleted (not left creating)
    upd = [
        c
        for c in api.tenants_table.update_item.call_args_list
        if c.kwargs.get("ExpressionAttributeValues", {}).get(":s") == "deleted"
    ]
    assert upd, "tenant should be marked deleted on launch failure"


def test_launch_success_no_rollback():
    """Normal path: _launch_vm returns a CommandId → no rollback, 201 creating."""
    _fresh_tables()
    host = {
        "instance_id": "i-1",
        "total_vcpu": 96,
        "total_mem_mb": 700000,
        "used_vcpu": 0,
        "used_mem_mb": 0,
        "next_vm_num": 1,
        "vm_count": 0,
        "private_ip": "10.0.0.1",
    }
    with (
        patch.object(api, "CREATE_VIA_QUEUE", False),
        patch.object(api, "LIFECYCLE_QUEUE_URL", ""),
        patch.object(api, "_get_caller_identity", return_value=_admin()),
        patch.object(api, "_find_host", return_value=host),
        patch.object(api, "_launch_vm", return_value="cmd-ok"),
        patch.object(api, "_release_slot") as mrelease,
        patch.object(api, "_mint_tenant_vkey", return_value=""),
        patch.object(api, "_ensure_host_tg", return_value="tg-arn"),
        patch.object(api, "_add_alb_rule"),
    ):
        resp = api.create_tenant(json.dumps({"name": "ok"}), {})
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["status"] == "creating"
    mrelease.assert_not_called()
