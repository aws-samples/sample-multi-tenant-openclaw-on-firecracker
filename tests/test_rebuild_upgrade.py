# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Phase 4 — in-place rootfs upgrade (rebuild action + drift view).

- tenant_action(rebuild): stop + drop overlay + relaunch (data.ext4 preserved),
  then record the host's current rootfs_version on the tenant.
- GET /hosts/rootfs-drift: which non-deleted tenants are NOT on the manifest's
  current version.
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

with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm,
        "s3": _mock_s3,
    }.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_rebuild", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_rebuild"] = api
    spec.loader.exec_module(api)


def _admin():
    return {"owner_id": "admin", "is_admin": True, "tenant_user_id": None}


def test_rebuild_drops_overlay_relaunches_and_records_version():
    api.tenants_table = make_ddb_table()
    api.hosts_table = make_ddb_table()
    api.tenants_table.get_item.return_value = {
        "Item": {
            "id": "t1",
            "host_id": "i-1",
            "vm_num": 3,
            "vcpu": 2,
            "mem_mb": 2048,
            "guest_ip": "172.16.0.14",
            "host_port": 18791,
            "owner_id": "admin",
        }
    }
    # host carries the NEW version that rebuild should stamp onto the tenant
    api.hosts_table.get_item.return_value = {"Item": {"rootfs_version": "v9-new"}}
    _mock_ssm.reset_mock()
    with (
        patch.object(api, "_get_caller_identity", return_value=_admin()),
        patch.object(api, "LIFECYCLE_QUEUE_URL", ""),
    ):  # force synchronous (no enqueue) so we see the SSM command
        resp = api.tenant_action("t1", "rebuild", None, {})
    assert resp["statusCode"] == 200
    # SSM command: stop-vm + rm overlay + launch-vm, in that order
    cmd = _mock_ssm.send_command.call_args.kwargs["Parameters"]["commands"][0]
    assert "stop-vm.sh t1 3" in cmd
    assert "rm -f /data/firecracker-vms/t1/overlay.ext4" in cmd
    assert "launch-vm.sh t1 3" in cmd
    # data.ext4 is NOT deleted (only the overlay) — upgrade preserves user data
    assert "data.ext4" not in cmd
    # tenant row updated with the host's current rootfs_version
    upd = api.tenants_table.update_item.call_args.kwargs
    assert upd["ExpressionAttributeValues"][":rv"] == "v9-new"


def test_rebuild_is_async_when_queue_enabled():
    api.tenants_table = make_ddb_table()
    api.tenants_table.get_item.return_value = {
        "Item": {"id": "t1", "host_id": "i-1", "vm_num": 1, "owner_id": "admin"}
    }
    _mock_sqs = MagicMock()
    with (
        patch.object(api, "_get_caller_identity", return_value=_admin()),
        patch.object(api, "LIFECYCLE_QUEUE_URL", "http://q.fifo"),
        patch.object(api, "sqs", _mock_sqs),
    ):
        resp = api.tenant_action("t1", "rebuild", None, {})
    # rebuild is in _async_actions → enqueued + 202
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["status"] == "queued"
    assert _mock_sqs.send_message.call_count == 1


def test_rootfs_drift_lists_stale_tenants():
    api.tenants_table = make_ddb_table()
    api.tenants_table.scan.return_value = {
        "Items": [
            {"id": "a", "rootfs_version": "v9", "host_id": "i-1"},  # current
            {"id": "b", "rootfs_version": "v8", "host_id": "i-1"},  # stale
            {"id": "c", "rootfs_version": "", "host_id": "i-2"},  # unknown
            {"id": "d", "rootfs_version": "v7", "host_id": "i-2"},  # stale
        ]
    }
    with patch.object(api, "_get_manifest", return_value={"version": "v9"}):
        resp = api.rootfs_drift()
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["current_version"] == "v9"
    assert body["up_to_date"] == 1
    assert body["unknown"] == 1
    assert body["stale_count"] == 2
    assert {s["id"] for s in body["stale"]} == {"b", "d"}
