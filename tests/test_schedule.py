# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for tenant schedule (issue #11) — auto-stop/start by office hours.

Schedule schema (per tenant):
    schedule: {
        "start": "08:00",                # HH:MM, 24h
        "stop":  "20:00",                # HH:MM, 24h
        "timezone": "Asia/Tokyo",        # IANA tz; default "UTC"
        "days": ["Mon","Tue","Wed","Thu","Fri"]  # default all 7
    }

Behavior:
- Inside the [start, stop) window on a scheduled day → tenant should run
- Outside window or off-day → tenant should be stopped
- Scaler tick reconciles actual status with the desired status
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

# ── Load api/handler.py with mocked SDK ──
_mock_ddb_api = MagicMock()
_mock_ssm_api = MagicMock()
_mock_s3_api = MagicMock()
_mock_asg_api = MagicMock()
_mock_elbv2_api = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb_api), \
     patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm_api, "s3": _mock_s3_api,
        "autoscaling": _mock_asg_api, "elbv2": _mock_elbv2_api,
    }.get(svc, MagicMock())
    _mock_ddb_api.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_sched", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_sched"] = api
    spec.loader.exec_module(api)


# ── Load scaler/handler.py with mocked SDK ──
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
        "scaler_handler_sched", "deploy/lambda/scaler/handler.py")
    sc = importlib.util.module_from_spec(spec)
    sys.modules["scaler_handler_sched"] = sc
    spec.loader.exec_module(sc)


def _prep_host():
    api.tenants_table = make_ddb_table()
    api.hosts_table = make_ddb_table()
    api.hosts_table.scan.return_value = {"Items": [
        {"instance_id": "i-test", "total_vcpu": 8, "total_mem_mb": 16384,
         "used_vcpu": 0, "used_mem_mb": 0, "status": "active",
         "next_vm_num": 1, "private_ip": "10.0.0.1", "rootfs_version": "v1.0"},
    ]}


# ═══════════════════════════════════════════
# create_tenant — schedule persistence
# ═══════════════════════════════════════════


class TestCreateTenantWithSchedule:
    @pytest.mark.unit
    def test_schedule_persisted(self):
        _prep_host()
        sched = {"start": "08:00", "stop": "20:00",
                 "timezone": "Asia/Tokyo", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
        resp = api.create_tenant(json.dumps({"name": "t", "schedule": sched}))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved["schedule"] == sched

    @pytest.mark.unit
    def test_no_schedule_field_persisted(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({"name": "t"}))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert "schedule" not in saved

    @pytest.mark.unit
    def test_default_timezone_utc(self):
        """Schedule without timezone → defaults to UTC."""
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t",
            "schedule": {"start": "08:00", "stop": "20:00"},
        }))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved["schedule"]["timezone"] == "UTC"

    @pytest.mark.unit
    def test_default_days_all_seven(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t",
            "schedule": {"start": "08:00", "stop": "20:00"},
        }))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert set(saved["schedule"]["days"]) == {
            "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}


# ═══════════════════════════════════════════
# Schedule validation
# ═══════════════════════════════════════════


class TestScheduleValidation:
    @pytest.mark.unit
    def test_schedule_must_be_object(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "schedule": "08:00-20:00",
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_missing_start_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "schedule": {"stop": "20:00"},
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_missing_stop_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "schedule": {"start": "08:00"},
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_invalid_time_format_rejected(self):
        _prep_host()
        for bad in ["8:00", "08:60", "25:00", "invalid", "08-00", "08:00:00"]:
            resp = api.create_tenant(json.dumps({
                "name": "t", "schedule": {"start": bad, "stop": "20:00"},
            }))
            assert resp["statusCode"] == 400, f"expected 400 for {bad!r}"

    @pytest.mark.unit
    def test_start_equals_stop_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "schedule": {"start": "08:00", "stop": "08:00"},
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_invalid_timezone_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t",
            "schedule": {"start": "08:00", "stop": "20:00", "timezone": "Atlantis/Lost"},
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_invalid_day_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t",
            "schedule": {"start": "08:00", "stop": "20:00",
                         "days": ["Mon", "Funday"]},
        }))
        assert resp["statusCode"] == 400


# ═══════════════════════════════════════════
# _is_in_window helper — window logic
# ═══════════════════════════════════════════


class TestWindowLogic:
    """Pure function tests — no DDB / no SSM. Uses scaler's _is_in_window."""

    @pytest.mark.unit
    def test_inside_window_weekday(self):
        # Monday, 12:00 in Asia/Tokyo
        now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)  # Mon UTC; JST = 21:00
        sched = {"start": "08:00", "stop": "23:00", "timezone": "UTC",
                 "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
        assert sc._schedule_should_run(sched, now) is True

    @pytest.mark.unit
    def test_outside_window(self):
        now = datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)  # 22:00 UTC Mon
        sched = {"start": "08:00", "stop": "20:00", "timezone": "UTC",
                 "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
        assert sc._schedule_should_run(sched, now) is False

    @pytest.mark.unit
    def test_off_day(self):
        now = datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc)  # Sunday 12:00 UTC
        sched = {"start": "08:00", "stop": "20:00", "timezone": "UTC",
                 "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
        assert sc._schedule_should_run(sched, now) is False

    @pytest.mark.unit
    def test_at_start_boundary(self):
        """Exactly at start time → in window (inclusive)."""
        now = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)  # Mon 08:00 UTC
        sched = {"start": "08:00", "stop": "20:00", "timezone": "UTC",
                 "days": ["Mon"]}
        assert sc._schedule_should_run(sched, now) is True

    @pytest.mark.unit
    def test_at_stop_boundary(self):
        """Exactly at stop time → out of window (exclusive)."""
        now = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)
        sched = {"start": "08:00", "stop": "20:00", "timezone": "UTC",
                 "days": ["Mon"]}
        assert sc._schedule_should_run(sched, now) is False

    @pytest.mark.unit
    def test_timezone_changes_window(self):
        """22:00 UTC = 07:00 JST next day → before 08:00 JST → out of window."""
        now = datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)  # Mon UTC = Tue 07:00 JST
        sched = {"start": "08:00", "stop": "20:00", "timezone": "Asia/Tokyo",
                 "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
        assert sc._schedule_should_run(sched, now) is False

    @pytest.mark.unit
    def test_timezone_inside_after_dst_simple(self):
        """01:00 UTC = 10:00 JST → in 08-20 window on Tue."""
        now = datetime(2026, 1, 6, 1, 0, tzinfo=timezone.utc)  # Tue 01:00 UTC = 10:00 JST
        sched = {"start": "08:00", "stop": "20:00", "timezone": "Asia/Tokyo",
                 "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}
        assert sc._schedule_should_run(sched, now) is True


# ═══════════════════════════════════════════
# Scaler — schedule reconciliation
# ═══════════════════════════════════════════


class TestScheduleReconciliation:
    """End-to-end scaler tick: scheduled tenants get stopped/started as needed."""

    def setup_method(self):
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": []}
        sc.tenants_table = make_ddb_table()
        sc.ssm = MagicMock()

    @pytest.mark.unit
    def test_running_outside_window_stopped(self):
        """Tenant is running but outside window → SSM stop-vm.sh + DDB status=stopped."""
        sc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running",
             "host_id": "i-1", "vm_num": 1,
             "guest_ip": "172.16.1.2", "host_port": 18789,
             "schedule": {"start": "08:00", "stop": "20:00", "timezone": "UTC",
                          "days": ["Mon"]}},
        ]}
        # Force "now" to a Sunday (outside days)
        with patch.object(sc, "_now_utc",
                          return_value=datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc)):
            sc.lambda_handler({}, None)
        sc.ssm.send_command.assert_called_once()
        cmd = sc.ssm.send_command.call_args[1]["Parameters"]["commands"][0]
        assert "stop-vm.sh" in cmd
        update = sc.tenants_table.update_item.call_args[1]["ExpressionAttributeValues"]
        assert update[":s"] == "stopped"

    @pytest.mark.unit
    def test_stopped_inside_window_started(self):
        """Tenant is stopped but inside window → SSM launch-vm.sh + DDB status=running."""
        sc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "stopped",
             "host_id": "i-1", "vm_num": 1, "vcpu": 2, "mem_mb": 4096,
             "guest_ip": "172.16.1.2", "host_port": 18789,
             "schedule": {"start": "08:00", "stop": "20:00", "timezone": "UTC",
                          "days": ["Mon"]}},
        ]}
        with patch.object(sc, "_now_utc",
                          return_value=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)):
            sc.lambda_handler({}, None)
        sc.ssm.send_command.assert_called_once()
        cmd = sc.ssm.send_command.call_args[1]["Parameters"]["commands"][0]
        assert "launch-vm.sh" in cmd
        update = sc.tenants_table.update_item.call_args[1]["ExpressionAttributeValues"]
        assert update[":s"] == "running"

    @pytest.mark.unit
    def test_running_inside_window_no_action(self):
        """No-op when current state matches schedule."""
        sc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running",
             "host_id": "i-1", "vm_num": 1,
             "schedule": {"start": "08:00", "stop": "20:00", "timezone": "UTC",
                          "days": ["Mon"]}},
        ]}
        with patch.object(sc, "_now_utc",
                          return_value=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)):
            sc.lambda_handler({}, None)
        sc.ssm.send_command.assert_not_called()

    @pytest.mark.unit
    def test_no_schedule_tenant_untouched(self):
        sc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "vm_num": 1},
        ]}
        sc.lambda_handler({}, None)
        sc.ssm.send_command.assert_not_called()

    @pytest.mark.unit
    def test_deleted_status_skipped(self):
        sc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "deleted",
             "schedule": {"start": "08:00", "stop": "20:00", "timezone": "UTC"}},
        ]}
        sc.lambda_handler({}, None)
        sc.ssm.send_command.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.regression
    def test_idle_logic_preserved(self):
        """Schedule processing must not break idle host reclamation."""
        sc.tenants_table.scan.return_value = {"Items": []}
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 0,
             "idle_since": (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()},
        ]}
        sc.lambda_handler({}, None)
        # Host should be marked idle (existing behavior)
        vals = sc.hosts_table.update_item.call_args[1]["ExpressionAttributeValues"]
        assert vals[":s"] == "idle"
