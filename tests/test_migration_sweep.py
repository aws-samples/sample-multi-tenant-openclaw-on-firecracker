# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the async live-migration sweep (1.4.4, issue #64).

POST /tenants/{id}/migrate is async (API Gateway caps a synchronous request at
29s, far less than a multi-GB snapshot+restore). The API fires the snapshot SSM
command, marks the tenant ``migrating`` with the async context, and returns 202.
The health_check Lambda's 5-min sweep — ``_advance_migration`` — is the
out-of-band driver that finishes the move:

    snapshot phase: poll snapshot cmd
        Success      → fire restore on target, phase → restore
        Failed/Timed → rollback to running (source VM still there)
        InProgress   → no-op, re-check next tick

    restore phase: poll restore cmd
        Success      → repoint ALB → verify dashboard → flip host_id/counters
                       → running, clear async context, audit MIGRATION_COMPLETED
        Failed/Timed → rollback to running
        InProgress   → no-op, re-check next tick

    watchdog: any phase stuck > MIGRATION_WATCHDOG_MINUTES → rollback

The fake DDB tables (conftest.make_ddb_table) are bare MagicMocks — they do not
persist writes — so these tests assert on the *update_item call arguments* and
on whether a restore SSM command was fired, not on read-back state.
"""

import importlib.util
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from conftest import make_ddb_table


def _load_hc(env_overrides=None):
    """Load health_check handler with mocked AWS. Mirrors test_az_failover."""
    import os
    saved = {}
    overrides = env_overrides or {}
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    mock_ddb = MagicMock()
    mock_ssm = MagicMock()
    mock_sns = MagicMock()
    mock_s3 = MagicMock()
    mock_elbv2 = MagicMock()
    # Default: SSM command Success. Per-test overrides drive the state machine.
    mock_ssm.get_command_invocation.return_value = {
        "Status": "Success", "StandardOutputContent": "",
    }
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "restore-cmd"}}

    class _FakeInvocationDoesNotExist(Exception):
        pass
    mock_ssm.exceptions.InvocationDoesNotExist = _FakeInvocationDoesNotExist
    mock_elbv2.describe_target_groups.return_value = {
        "TargetGroups": [{"TargetGroupArn": "arn:tg:target", "VpcId": "vpc-1"}],
    }
    mock_elbv2.describe_rules.return_value = {"Rules": []}

    table_cache = {}

    def _table_factory(name):
        if name not in table_cache:
            table_cache[name] = make_ddb_table()
        return table_cache[name]
    mock_ddb.Table.side_effect = _table_factory

    def _client_factory(svc):
        return {"ssm": mock_ssm, "sns": mock_sns,
                "s3": mock_s3, "elbv2": mock_elbv2}.get(svc, MagicMock())

    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", side_effect=_client_factory):
        spec = importlib.util.spec_from_file_location(
            "hc_handler_sweep", "deploy/lambda/health_check/handler.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hc_handler_sweep"] = mod
        spec.loader.exec_module(mod)
    mod._test_mocks = {"ddb": mock_ddb, "ssm": mock_ssm, "sns": mock_sns,
                       "s3": mock_s3, "elbv2": mock_elbv2, "tables": table_cache}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return mod


def _migrating_tenant(phase="snapshot", **extra):
    """A tenant DDB item mid-migration, as the API's 202 path would write it."""
    t = {
        "id": "t-mig", "host_id": "i-source", "vm_num": 1,
        "vcpu": 2, "mem_mb": 4096, "status": "migrating",
        "migration_source": "i-source", "migration_target": "i-target",
        "migration_target_vm_num": 5, "migration_phase": phase,
        "migration_snap_cmd": "snap-cmd",
        "migration_snapshot_uri": "s3://test/migrations/t-mig",
        "migration_started_at": datetime.now(timezone.utc).isoformat(),
    }
    if phase == "restore":
        t["migration_restore_cmd"] = "restore-cmd"
    t.update(extra)
    return t


def _ssm_status(mod, status, stdout=""):
    mod._test_mocks["ssm"].get_command_invocation.return_value = {
        "Status": status, "StandardOutputContent": stdout,
        "StandardErrorContent": "",
    }


def _tenant_updates(mod):
    return mod.tenants_table.update_item.call_args_list


def _now(mod):
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════
# snapshot phase
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestSnapshotPhase:
    def test_snapshot_success_fires_restore_and_advances_phase(self):
        mod = _load_hc()
        _ssm_status(mod, "Success")
        mod._advance_migration(_migrating_tenant("snapshot"), _now(mod))
        # restore SSM command fired on the target host
        sends = mod._test_mocks["ssm"].send_command.call_args_list
        assert sends, "no restore SSM command fired"
        cmd = " ".join(str(c.kwargs.get("Parameters", {})) for c in sends)
        assert "migrate-vm.sh restore" in cmd
        assert "i-target" in str(sends[0].kwargs.get("InstanceIds"))
        # phase advanced to restore (no rollback to running)
        upds = _tenant_updates(mod)
        phase_writes = [c.kwargs["ExpressionAttributeValues"].get(":p")
                        for c in upds if ":p" in c.kwargs.get("ExpressionAttributeValues", {})]
        assert "restore" in phase_writes, f"phase not advanced: {phase_writes}"

    def test_snapshot_failure_rolls_back_to_running(self):
        mod = _load_hc()
        _ssm_status(mod, "Failed")
        mod._advance_migration(_migrating_tenant("snapshot"), _now(mod))
        # no restore fired, status rolled back to running
        assert not mod._test_mocks["ssm"].send_command.called, \
            "restore fired despite snapshot failure"
        _assert_rolled_back_to_running(mod)

    def test_snapshot_in_progress_is_noop(self):
        mod = _load_hc()
        _ssm_status(mod, "InProgress")
        mod._advance_migration(_migrating_tenant("snapshot"), _now(mod))
        # nothing fired, no status write at all (wait for next tick)
        assert not mod._test_mocks["ssm"].send_command.called
        for c in _tenant_updates(mod):
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            assert vals.get(":r") != "running", "rolled back while still in progress"
            assert vals.get(":p") != "restore", "advanced while still in progress"


# ═══════════════════════════════════════════
# restore phase
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestRestorePhase:
    def test_restore_success_flips_host_id_to_target(self):
        mod = _load_hc({"PUBLIC_BASE_URL": ""})  # skip dashboard gate in unit
        mod.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.9"}}
        _ssm_status(mod, "Success")
        with patch.object(mod, "_repoint_alb_rule"):
            mod._advance_migration(_migrating_tenant("restore"), _now(mod))
        # host_id flipped to target in some update
        flipped = False
        for c in _tenant_updates(mod):
            expr = c.kwargs.get("UpdateExpression", "")
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            if "host_id = :h" in expr and vals.get(":h") == "i-target":
                flipped = True
        assert flipped, "host_id not flipped to target on restore success"

    def test_restore_success_increments_target_decrements_source(self):
        mod = _load_hc({"PUBLIC_BASE_URL": ""})
        mod.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.9"}}
        _ssm_status(mod, "Success")
        with patch.object(mod, "_repoint_alb_rule"):
            mod._advance_migration(_migrating_tenant("restore"), _now(mod))
        by_host = {}
        for c in mod.hosts_table.update_item.call_args_list:
            iid = c.kwargs.get("Key", {}).get("instance_id")
            by_host.setdefault(iid, []).append(c.kwargs.get("UpdateExpression", ""))
        assert "i-source" in by_host and "i-target" in by_host
        assert any("used_vcpu, :z) - :v" in e for e in by_host["i-source"])
        assert any("used_vcpu, :z) + :v" in e for e in by_host["i-target"])

    def test_restore_failure_rolls_back_to_running(self):
        mod = _load_hc({"PUBLIC_BASE_URL": ""})
        _ssm_status(mod, "Failed")
        mod._advance_migration(_migrating_tenant("restore"), _now(mod))
        _assert_rolled_back_to_running(mod)
        # host_id must NOT have flipped
        for c in _tenant_updates(mod):
            if "host_id = :h" in c.kwargs.get("UpdateExpression", ""):
                pytest.fail("host_id flipped despite restore failure")

    def test_restore_dashboard_unreachable_rolls_back(self):
        mod = _load_hc({"PUBLIC_BASE_URL": "http://alb.example"})
        mod.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.9"}}
        _ssm_status(mod, "Success")
        # restore SSM ok, ALB repoint ok, but the public dashboard probe fails
        with patch.object(mod, "_repoint_alb_rule"), \
             patch.object(mod, "_verify_dashboard_reachable_via_alb",
                          return_value=False):
            mod._advance_migration(_migrating_tenant("restore"), _now(mod))
        _assert_rolled_back_to_running(mod)
        for c in _tenant_updates(mod):
            if "host_id = :h" in c.kwargs.get("UpdateExpression", ""):
                pytest.fail("host_id flipped despite dashboard verify failure")


# ═══════════════════════════════════════════
# watchdog + edge cases
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestWatchdogAndEdges:
    def test_watchdog_rolls_back_stuck_migration(self):
        mod = _load_hc()
        stale = (datetime.now(timezone.utc)
                 - timedelta(minutes=mod.MIGRATION_WATCHDOG_MINUTES + 5)).isoformat()
        t = _migrating_tenant("snapshot", migration_started_at=stale)
        mod._advance_migration(t, _now(mod))
        # watchdog fires BEFORE polling SSM → no SSM poll, rolled back
        _assert_rolled_back_to_running(mod)

    def test_unknown_phase_rolls_back(self):
        mod = _load_hc()
        mod._advance_migration(_migrating_tenant("bogus"), _now(mod))
        _assert_rolled_back_to_running(mod)

    def test_missing_snap_cmd_rolls_back(self):
        mod = _load_hc()
        t = _migrating_tenant("snapshot")
        del t["migration_snap_cmd"]
        mod._advance_migration(t, _now(mod))
        _assert_rolled_back_to_running(mod)


def _assert_rolled_back_to_running(mod):
    """A rollback writes status=running and REMOVEs the migration_* context."""
    found = False
    for c in mod.tenants_table.update_item.call_args_list:
        expr = c.kwargs.get("UpdateExpression", "")
        vals = c.kwargs.get("ExpressionAttributeValues", {})
        if vals.get(":r") == "running" and "migration_failed" in expr \
                and "REMOVE" in expr:
            found = True
    assert found, "no rollback-to-running (status=running + migration_failed + REMOVE)"
