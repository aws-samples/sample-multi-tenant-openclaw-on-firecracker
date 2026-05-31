# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for offline data-disk resize (issue #22).

The data-disk grows ext4 sparse files on the host (no in-guest
partprobe needed because data.ext4 is the whole device, no partition
table). Flow:

1. POST /tenants/{id}/resize-disk {new_size_mb}
2. Validation: tenant exists, new_size > current, ≤ a sane ceiling.
3. SSM on the host: pause VM → `truncate -s ${new}M data.ext4` →
   `e2fsck -fy && resize2fs data.ext4` → resume VM.
4. DDB update: tenant.data_disk_mb = new_size_mb.

We *don't* exercise the on-host operations — those run via SSM and a
small `resize-disk.sh` script. Unit tests assert the API contract +
payload validation + SSM command shape + DDB update.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
os.environ.setdefault("HOSTS_TABLE", "openclaw-hosts")
os.environ.setdefault("ASSETS_BUCKET", "test")
os.environ.setdefault("ROOTFS_PREFIX", "deployment/rootfs")

_tenants = MagicMock()
_hosts = MagicMock()
_mock_ddb.Table.side_effect = lambda name: _tenants if "tenant" in name else _hosts

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    spec = importlib.util.spec_from_file_location(
        "rd_handler", str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
    handler = importlib.util.module_from_spec(spec)
    sys.modules["rd_handler"] = handler
    spec.loader.exec_module(handler)


# 1.5.0: RBAC now fail-safes no-token requests to `viewer`, which would 403
# these write-path tests before they reach resize-disk logic. RBAC itself is
# covered by tests/test_rbac.py; here we assume an authenticated admin so we
# can exercise the business logic.
@pytest.fixture(autouse=True)
def _authenticated_admin():
    with patch.object(handler, "_get_user_role", return_value="admin"):
        yield


def _ev(tenant_id, body=None):
    return {
        "httpMethod": "POST",
        "resource": "/tenants/{id}/{action}",
        "headers": {"x-api-key": "test"},
        "pathParameters": {"id": tenant_id, "action": "resize-disk"},
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.mark.unit
class TestResizeDiskValidation:
    def test_404_when_tenant_missing(self):
        _tenants.get_item.return_value = {}
        r = handler.lambda_handler(_ev("t-x", body={"new_size_mb": 16384}), None)
        assert r["statusCode"] == 404

    def test_400_when_missing_new_size(self):
        _tenants.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-1", "vm_num": 1,
                     "data_disk_mb": 8192, "status": "running"},
        }
        r = handler.lambda_handler(_ev("t1", body={}), None)
        assert r["statusCode"] == 400

    def test_400_when_shrink(self):
        """Shrinking risks data loss (resize2fs may refuse anyway). Reject."""
        _tenants.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-1", "vm_num": 1,
                     "data_disk_mb": 8192, "status": "running"},
        }
        r = handler.lambda_handler(_ev("t1", body={"new_size_mb": 4096}), None)
        assert r["statusCode"] == 400
        assert "shrink" in r["body"].lower() or "smaller" in r["body"].lower()

    def test_400_when_over_ceiling(self):
        """Sanity: refuse > 1 TB to catch fat-finger typos."""
        _tenants.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-1", "vm_num": 1,
                     "data_disk_mb": 8192, "status": "running"},
        }
        r = handler.lambda_handler(_ev("t1", body={"new_size_mb": 1024 * 1024 * 2}), None)
        assert r["statusCode"] == 400


@pytest.mark.unit
class TestResizeDiskOrchestration:
    def test_invokes_ssm_on_host(self):
        _tenants.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-1", "vm_num": 1,
                     "data_disk_mb": 8192, "status": "running"},
        }
        # Issue #64-class fix: resize-disk now runs synchronously via _ssm_run
        # (blocks on SSM completion) and only persists the new size on success.
        with patch.object(handler, "_ssm_run", return_value=True) as mock_send:
            r = handler.lambda_handler(_ev("t1", body={"new_size_mb": 16384}), None)
        assert r["statusCode"] in (200, 202)
        called = {c.args[0] for c in mock_send.call_args_list}
        assert "i-1" in called

    def test_command_references_resize2fs(self):
        _tenants.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-1", "vm_num": 1,
                     "data_disk_mb": 8192, "status": "running"},
        }
        with patch.object(handler, "_ssm_run", return_value=True) as mock_send:
            handler.lambda_handler(_ev("t1", body={"new_size_mb": 16384}), None)
        all_cmds = " ".join(c.args[1] for c in mock_send.call_args_list)
        assert "resize" in all_cmds.lower() or "resize-disk" in all_cmds.lower()

    def test_updates_ddb_data_disk_mb(self):
        _tenants.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-1", "vm_num": 1,
                     "data_disk_mb": 8192, "status": "running"},
        }
        _tenants.update_item.reset_mock()
        with patch.object(handler, "_ssm_run", return_value=True):
            handler.lambda_handler(_ev("t1", body={"new_size_mb": 16384}), None)
        updated = False
        for c in _tenants.update_item.call_args_list:
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            if any(v == 16384 for v in vals.values()):
                updated = True
                break
        assert updated, "tenant.data_disk_mb was not updated to 16384"

    def test_ssm_failure_does_not_persist_new_size(self):
        """Issue #64-class: if resize-disk.sh fails on the host, DDB must NOT
        claim the new size and the API must return 5xx."""
        _tenants.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-1", "vm_num": 1,
                     "data_disk_mb": 8192, "status": "running"},
        }
        _tenants.update_item.reset_mock()
        with patch.object(handler, "_ssm_run", return_value=False):
            r = handler.lambda_handler(_ev("t1", body={"new_size_mb": 16384}), None)
        assert r["statusCode"] >= 500, f"expected 5xx on resize failure, got {r}"
        # data_disk_mb must NOT have been bumped to the requested size.
        for c in _tenants.update_item.call_args_list:
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            assert not any(v == 16384 for v in vals.values()), (
                "data_disk_mb persisted to 16384 despite the host resize failing"
            )


@pytest.mark.unit
class TestResizeDiskScript:
    def test_resize_script_exists(self):
        f = ROOT / "deploy" / "userdata" / "resize-disk.sh"
        assert f.is_file(), "resize-disk.sh missing"

    def test_resize_script_uses_resize2fs(self):
        f = ROOT / "deploy" / "userdata" / "resize-disk.sh"
        text = f.read_text()
        assert "resize2fs" in text
        assert "truncate" in text or "fallocate" in text
