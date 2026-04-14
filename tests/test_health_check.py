"""Unit tests for deploy/lambda/health_check/handler.py.
Covers: stale detection, host-agent recovery, cooldown, edge cases.
"""

import pytest
import importlib.util
import sys
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from conftest import make_ddb_table

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location("hc_handler", "deploy/lambda/health_check/handler.py")
    hc = importlib.util.module_from_spec(spec)
    sys.modules["hc_handler"] = hc
    spec.loader.exec_module(hc)


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ═══════════════════════════════════════════
# Stale detection
# ═══════════════════════════════════════════

class TestStaleDetection:
    @pytest.mark.unit
    def test_fresh_tenant_not_stale(self):
        hc.tenants_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(30)},
        ]}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_not_called()

    @pytest.mark.unit
    def test_stale_tenant_marked(self):
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_called()
        vals = hc.tenants_table.update_item.call_args[1]["ExpressionAttributeValues"]
        assert vals[":vh"] == "stale"
        assert vals[":ah"] == "unknown"

    @pytest.mark.unit
    def test_missing_health_check_is_stale(self):
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1"},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_called()

    @pytest.mark.unit
    def test_no_running_tenants_noop(self):
        hc.tenants_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": []}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_not_called()

    @pytest.mark.unit
    def test_boundary_exactly_120s(self):
        """Exactly at STALE_SECONDS boundary — should still be fresh."""
        hc.tenants_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(119)},
        ]}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_not_called()


# ═══════════════════════════════════════════
# Host-agent recovery
# ═══════════════════════════════════════════

class TestHostAgentRecovery:
    @pytest.mark.unit
    def test_all_stale_triggers_restart(self):
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
            {"id": "t2", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_called_once()
        assert "systemctl restart host-agent" in hc.ssm.send_command.call_args[1]["Parameters"]["commands"][0]

    @pytest.mark.unit
    def test_partial_stale_no_restart(self):
        """One fresh + one stale on same host → agent is alive, don't restart."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
            {"id": "t2", "status": "running", "host_id": "i-1", "last_health_check": _ago(30)},
        ]}
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_not_called()

    @pytest.mark.unit
    def test_cooldown_blocks_restart(self):
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-1", "status": "active", "agent_restart_at": _ago(120)}
        }
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_not_called()

    @pytest.mark.unit
    def test_cooldown_expired_allows_restart(self):
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-1", "status": "active", "agent_restart_at": _ago(900)}
        }
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_called_once()

    @pytest.mark.unit
    def test_deleted_host_skipped(self):
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "deleted"}}
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_not_called()

    @pytest.mark.unit
    def test_multi_host_independent(self):
        """Host i-1 all stale → restart. Host i-2 has fresh tenant → skip."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
            {"id": "t2", "status": "running", "host_id": "i-2", "last_health_check": _ago(30)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)
        assert hc.ssm.send_command.call_count == 1
        assert hc.ssm.send_command.call_args[1]["InstanceIds"] == ["i-1"]

    @pytest.mark.unit
    def test_ssm_failure_handled_gracefully(self):
        """SSM send_command failure should not crash the Lambda."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.ssm.send_command.side_effect = Exception("SSM unavailable")
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)  # Should not raise
