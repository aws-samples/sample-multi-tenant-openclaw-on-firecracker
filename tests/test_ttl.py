# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for tenant TTL (issue #15).

Covers:
- create_tenant accepts ttl_hours and on_expiry, computes expires_at
- Validation: ttl_hours range, on_expiry whitelist
- Scaler scans tenants table and acts on expired ones
  - on_expiry=stop → SSM stop-vm.sh + DDB status=stopped
  - on_expiry=delete → DDB status=deleted
- Non-expired and TTL-less tenants are untouched (regression)
"""

import json
import os
import sys
import importlib.util
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from conftest import make_ddb_table


# ── Import api/handler.py with mocked AWS SDK ──
_mock_ddb_api = MagicMock()
_mock_ssm_api = MagicMock()
_mock_s3_api = MagicMock()
_mock_asg_api = MagicMock()
_mock_elbv2_api = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb_api), \
     patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm_api, "s3": _mock_s3_api, "autoscaling": _mock_asg_api,
        "elbv2": _mock_elbv2_api,
    }.get(svc, MagicMock())
    _mock_ddb_api.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_ttl", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_ttl"] = api
    spec.loader.exec_module(api)


# ── Import scaler/handler.py with mocked AWS SDK ──
_mock_ddb_sc = MagicMock()
_mock_asg_sc = MagicMock()
_mock_ssm_sc = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb_sc), \
     patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "autoscaling": _mock_asg_sc,
        "ssm": _mock_ssm_sc,
    }.get(svc, MagicMock())
    _mock_ddb_sc.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "scaler_handler_ttl", "deploy/lambda/scaler/handler.py")
    sc = importlib.util.module_from_spec(spec)
    sys.modules["scaler_handler_ttl"] = sc
    spec.loader.exec_module(sc)


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _from_now(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _prep_host():
    api.tenants_table = make_ddb_table()
    api.hosts_table = make_ddb_table()
    api.hosts_table.scan.return_value = {"Items": [
        {"instance_id": "i-test", "total_vcpu": 8, "total_mem_mb": 16384,
         "used_vcpu": 0, "used_mem_mb": 0, "status": "active",
         "next_vm_num": 1, "private_ip": "10.0.0.1", "rootfs_version": "v1.0"},
    ]}


# ═══════════════════════════════════════════
# create_tenant — TTL fields
# ═══════════════════════════════════════════


class TestCreateTenantWithTTL:
    @pytest.mark.unit
    def test_ttl_persisted_to_ddb(self):
        """ttl_hours=24 → expires_at ~24h ahead, on_expiry default stop."""
        _prep_host()
        before = datetime.now(timezone.utc)
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": 24,
        }))
        after = datetime.now(timezone.utc)
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved["ttl_hours"] == 24
        assert saved["on_expiry"] == "stop"  # default
        # expires_at lies in [now+24h-ε, now+24h+ε]
        exp = datetime.fromisoformat(saved["expires_at"])
        assert before + timedelta(hours=24) - timedelta(seconds=5) <= exp
        assert exp <= after + timedelta(hours=24) + timedelta(seconds=5)

    @pytest.mark.unit
    def test_on_expiry_delete(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": 1, "on_expiry": "delete",
        }))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved["on_expiry"] == "delete"

    @pytest.mark.unit
    def test_no_ttl_no_fields_persisted(self):
        """No ttl_hours → tenant has no expires_at / on_expiry / ttl_hours."""
        _prep_host()
        resp = api.create_tenant(json.dumps({"name": "t"}))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert "expires_at" not in saved
        assert "on_expiry" not in saved
        assert "ttl_hours" not in saved


# ═══════════════════════════════════════════
# create_tenant — validation
# ═══════════════════════════════════════════


class TestTTLValidation:
    @pytest.mark.unit
    def test_zero_hours_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": 0,
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_negative_hours_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": -5,
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_over_one_year_rejected(self):
        """ttl_hours > 8760 (1 year) rejected as guard against typos."""
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": 8761,
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_unknown_on_expiry_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": 1, "on_expiry": "explode",
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_non_int_ttl_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": "abc",
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_boundary_max_8760_hours(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": 8760,  # exactly 1 year
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_boundary_min_1_hour(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "ttl_hours": 1,
        }))
        assert resp["statusCode"] == 201


# ═══════════════════════════════════════════
# Scaler — expiry processing
# ═══════════════════════════════════════════


def _tenant(tid, expires_at=None, on_expiry="stop", status="running", host_id="i-1", vm_num=1):
    item = {
        "id": tid, "name": tid, "status": status,
        "vcpu": 2, "mem_mb": 4096,
        "host_id": host_id, "vm_num": vm_num,
        "guest_ip": "172.16.1.2", "host_port": 18789,
    }
    if expires_at is not None:
        item["expires_at"] = expires_at
        item["on_expiry"] = on_expiry
    return item


class TestScalerExpiryProcessing:
    @pytest.mark.unit
    def test_no_ttl_tenant_untouched(self):
        sc.hosts_table = make_ddb_table()
        sc.tenants_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": []}
        sc.tenants_table.scan.return_value = {"Items": [
            _tenant("a"),  # no expires_at
        ]}
        sc.lambda_handler({}, None)
        # Tenant table not updated; SSM not called
        sc.tenants_table.update_item.assert_not_called()

    @pytest.mark.unit
    def test_future_expiry_untouched(self):
        sc.hosts_table = make_ddb_table()
        sc.tenants_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": []}
        sc.tenants_table.scan.return_value = {"Items": [
            _tenant("a", expires_at=_from_now(3600)),  # 1h in future
        ]}
        sc.lambda_handler({}, None)
        sc.tenants_table.update_item.assert_not_called()

    @pytest.mark.unit
    def test_expired_stop_calls_ssm_and_updates_status(self):
        sc.hosts_table = make_ddb_table()
        sc.tenants_table = make_ddb_table()
        sc.ssm = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": []}
        sc.tenants_table.scan.return_value = {"Items": [
            _tenant("a", expires_at=_ago(60), on_expiry="stop"),
        ]}
        sc.lambda_handler({}, None)
        # SSM stop-vm.sh invoked
        sc.ssm.send_command.assert_called_once()
        cmd = sc.ssm.send_command.call_args[1]["Parameters"]["commands"][0]
        assert "stop-vm.sh" in cmd
        assert "a" in cmd  # tenant id
        # DDB status updated to stopped
        update_calls = sc.tenants_table.update_item.call_args_list
        assert any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s") == "stopped"
            for c in update_calls
        )

    @pytest.mark.unit
    def test_expired_delete_marks_status(self):
        sc.hosts_table = make_ddb_table()
        sc.tenants_table = make_ddb_table()
        sc.ssm = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": []}
        sc.tenants_table.scan.return_value = {"Items": [
            _tenant("a", expires_at=_ago(60), on_expiry="delete"),
        ]}
        sc.lambda_handler({}, None)
        # DDB status updated to deleted
        update_calls = sc.tenants_table.update_item.call_args_list
        assert any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":s") == "deleted"
            for c in update_calls
        )

    @pytest.mark.unit
    def test_already_stopped_tenant_with_expiry_skipped(self):
        """If tenant is already stopped, on_expiry=stop should not re-run SSM."""
        sc.hosts_table = make_ddb_table()
        sc.tenants_table = make_ddb_table()
        sc.ssm = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": []}
        sc.tenants_table.scan.return_value = {"Items": [
            _tenant("a", expires_at=_ago(60), on_expiry="stop", status="stopped"),
        ]}
        sc.lambda_handler({}, None)
        sc.ssm.send_command.assert_not_called()

    @pytest.mark.unit
    def test_already_deleted_tenant_skipped(self):
        sc.hosts_table = make_ddb_table()
        sc.tenants_table = make_ddb_table()
        sc.ssm = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": []}
        sc.tenants_table.scan.return_value = {"Items": [
            _tenant("a", expires_at=_ago(60), on_expiry="delete", status="deleted"),
        ]}
        sc.lambda_handler({}, None)
        sc.ssm.send_command.assert_not_called()
        sc.tenants_table.update_item.assert_not_called()

    @pytest.mark.unit
    def test_multiple_tenants_independently_processed(self):
        sc.hosts_table = make_ddb_table()
        sc.tenants_table = make_ddb_table()
        sc.ssm = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": []}
        sc.tenants_table.scan.return_value = {"Items": [
            _tenant("expired", expires_at=_ago(60), on_expiry="stop"),
            _tenant("future", expires_at=_from_now(3600)),
            _tenant("noexp"),
        ]}
        sc.lambda_handler({}, None)
        assert sc.ssm.send_command.call_count == 1
        cmd = sc.ssm.send_command.call_args[1]["Parameters"]["commands"][0]
        assert "expired" in cmd
        assert "future" not in cmd

    @pytest.mark.unit
    @pytest.mark.regression
    def test_existing_host_idle_logic_preserved(self):
        """TTL processing must not interfere with host idle reclamation."""
        sc.hosts_table = make_ddb_table()
        sc.tenants_table = make_ddb_table()
        sc.tenants_table.scan.return_value = {"Items": []}
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 0,
             "idle_since": _ago(700)},
        ]}
        sc.lambda_handler({}, None)
        # idle_since exceeded → host marked idle
        vals = sc.hosts_table.update_item.call_args[1]["ExpressionAttributeValues"]
        assert vals[":s"] == "idle"
