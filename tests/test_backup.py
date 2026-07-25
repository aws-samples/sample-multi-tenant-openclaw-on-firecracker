# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for deploy/lambda/backup/handler.py (#audit-1, T2-1).

The backup Lambda guards the one thing tenants cannot recover themselves —
their data volume. It ran with ZERO coverage. Covers both triggers:
  - manual single-tenant backup (API: event has tenant_id)
  - scheduled all-running backup (EventBridge: no tenant_id)
and the success / failure / not-running / not-found paths.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
os.environ.setdefault("ASSETS_BUCKET", "test-bucket")
os.environ.setdefault("BACKUP_PREFIX", "backups")

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "backup_handler", "deploy/lambda/backup/handler.py")
    backup = importlib.util.module_from_spec(spec)
    sys.modules["backup_handler"] = backup
    spec.loader.exec_module(backup)

pytestmark = pytest.mark.unit


def _running_tenant(tid="t1", host="i-1"):
    return {"id": tid, "host_id": host, "status": "running",
            "vm_num": 1, "vcpu": 2, "mem_mb": 4096}


class TestManualBackup:
    def setup_method(self):
        backup.tenants_table = make_ddb_table()

    def test_tenant_not_found_returns_error(self):
        backup.tenants_table.get_item.return_value = {}  # no Item
        r = backup.lambda_handler({"tenant_id": "ghost"}, None)
        assert r == {"error": "tenant not running"}

    def test_tenant_not_running_returns_error(self):
        backup.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "status": "stopped", "host_id": "i-1"}}
        r = backup.lambda_handler({"tenant_id": "t1"}, None)
        assert r == {"error": "tenant not running"}
        # A stopped tenant must never be SSM-backed-up.
        assert not backup.tenants_table.update_item.called

    def test_success_updates_last_backup_at(self):
        backup.tenants_table.get_item.return_value = {"Item": _running_tenant()}
        with patch.object(backup, "_ssm_run", return_value=(True, "ok")):
            r = backup.lambda_handler({"tenant_id": "t1"}, None)
        assert r["success"] is True
        assert r["tenant_id"] == "t1"
        assert "timestamp" in r
        # Durability marker persisted only on success.
        assert backup.tenants_table.update_item.called
        expr = backup.tenants_table.update_item.call_args[1]["UpdateExpression"]
        assert "last_backup_at" in expr

    def test_ssm_failure_reports_error_and_skips_marker(self):
        backup.tenants_table.get_item.return_value = {"Item": _running_tenant()}
        with patch.object(backup, "_ssm_run", return_value=(False, "disk full")):
            r = backup.lambda_handler({"tenant_id": "t1"}, None)
        assert r["success"] is False
        assert r["error"] == "disk full"
        # Must NOT record a backup that never happened.
        assert not backup.tenants_table.update_item.called

    def test_backup_command_targets_the_hosts_script(self):
        backup.tenants_table.get_item.return_value = {"Item": _running_tenant(host="i-42")}
        with patch.object(backup, "_ssm_run", return_value=(True, "")) as run:
            backup.lambda_handler({"tenant_id": "t1"}, None)
        host_arg, cmd = run.call_args[0][0], run.call_args[0][1]
        assert host_arg == "i-42"
        assert cmd.startswith("/home/ubuntu/backup-data.sh t1 ")


class TestScheduledBackup:
    def setup_method(self):
        backup.tenants_table = make_ddb_table()

    def test_backs_up_all_running_tenants(self):
        backup.tenants_table.scan.return_value = {"Items": [
            _running_tenant("t1", "i-1"), _running_tenant("t2", "i-2")]}
        with patch.object(backup, "_ssm_run", return_value=(True, "")) as run:
            results = backup.lambda_handler({}, None)
        assert isinstance(results, list) and len(results) == 2
        assert {r["tenant_id"] for r in results} == {"t1", "t2"}
        assert run.call_count == 2

    def test_scan_filters_to_running_only(self):
        backup.tenants_table.scan.return_value = {"Items": []}
        results = backup.lambda_handler({}, None)
        assert results == []
        # The scan must filter on status=running.
        kw = backup.tenants_table.scan.call_args[1]
        assert kw["ExpressionAttributeValues"][":r"] == "running"


class TestSsmRun:
    # T3-3: _ssm_run delegates to common.ssm.run, so the blocking sleep now
    # lives there — patch that module's time.sleep.
    def test_success_status_returns_true(self):
        from common import ssm as _ssm
        backup.ssm = MagicMock()
        backup.ssm.send_command.return_value = {"Command": {"CommandId": "c1"}}
        backup.ssm.get_command_invocation.return_value = {
            "Status": "Success", "StandardOutputContent": "done"}
        with patch.object(_ssm.time, "sleep"):
            ok, out = backup._ssm_run("i-1", "cmd", timeout=9)
        assert ok is True and out == "done"

    def test_failed_status_returns_false_with_stderr(self):
        from common import ssm as _ssm
        backup.ssm = MagicMock()
        backup.ssm.send_command.return_value = {"Command": {"CommandId": "c1"}}
        backup.ssm.get_command_invocation.return_value = {
            "Status": "Failed", "StandardErrorContent": "boom"}
        with patch.object(_ssm.time, "sleep"):
            ok, out = backup._ssm_run("i-1", "cmd", timeout=9)
        assert ok is False and out == "boom"

    def test_send_command_exception_is_caught(self):
        from common import ssm as _ssm
        backup.ssm = MagicMock()
        backup.ssm.send_command.side_effect = RuntimeError("throttled")
        with patch.object(_ssm.time, "sleep"):
            ok, out = backup._ssm_run("i-1", "cmd", timeout=9)
        assert ok is False and "throttled" in out
