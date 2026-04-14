"""Unit tests for deploy/lambda/scaler/handler.py.
Covers: two-round idle reclamation, ASG min protection, recovery.
"""

import pytest
import importlib.util
import sys
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from conftest import make_ddb_table

_mock_ddb = MagicMock()
_mock_asg = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_asg):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location("sc_handler", "deploy/lambda/scaler/handler.py")
    sc = importlib.util.module_from_spec(spec)
    sys.modules["sc_handler"] = sc
    spec.loader.exec_module(sc)


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class TestScalerTwoRound:
    @pytest.mark.unit
    def test_host_with_vms_stays_active(self):
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 2},
        ]}
        sc.lambda_handler({}, None)
        sc.hosts_table.update_item.assert_not_called()

    @pytest.mark.unit
    def test_idle_host_with_vms_recovers(self):
        """Idle host that got a VM → recover to active."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "idle", "vm_count": 1},
        ]}
        sc.lambda_handler({}, None)
        vals = sc.hosts_table.update_item.call_args[1]["ExpressionAttributeValues"]
        assert vals[":s"] == "active"

    @pytest.mark.unit
    def test_empty_active_no_idle_since_records_it(self):
        """Empty active host without idle_since → record timestamp."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 0},
        ]}
        sc.lambda_handler({}, None)
        sc.hosts_table.update_item.assert_called_once()

    @pytest.mark.unit
    def test_empty_active_within_timeout_no_change(self):
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 0, "idle_since": _ago(60)},
        ]}
        sc.lambda_handler({}, None)
        sc.hosts_table.update_item.assert_not_called()

    @pytest.mark.unit
    def test_empty_active_past_timeout_marked_idle(self):
        """Round 1: past timeout → mark idle."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 0, "idle_since": _ago(700)},
        ]}
        sc.lambda_handler({}, None)
        vals = sc.hosts_table.update_item.call_args[1]["ExpressionAttributeValues"]
        assert vals[":s"] == "idle"

    @pytest.mark.unit
    def test_idle_terminated_when_asg_allows(self):
        """Round 2: idle + ASG desired > min → terminate."""
        sc.hosts_table = make_ddb_table()
        sc.autoscaling = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "idle", "vm_count": 0},
        ]}
        sc.autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 2, "MinSize": 1}]}
        sc.lambda_handler({}, None)
        sc.autoscaling.terminate_instance_in_auto_scaling_group.assert_called_once()

    @pytest.mark.unit
    def test_idle_not_terminated_at_min(self):
        """ASG at min capacity → don't terminate."""
        sc.hosts_table = make_ddb_table()
        sc.autoscaling = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "idle", "vm_count": 0},
        ]}
        sc.autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 1, "MinSize": 1}]}
        sc.lambda_handler({}, None)
        sc.autoscaling.terminate_instance_in_auto_scaling_group.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.regression
    def test_deleted_hosts_excluded(self):
        """Deleted hosts should be filtered by scan."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": []}  # scan filters deleted
        sc.lambda_handler({}, None)
        sc.hosts_table.update_item.assert_not_called()
