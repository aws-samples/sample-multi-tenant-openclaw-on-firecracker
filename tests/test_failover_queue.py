# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3-2 detector: AZ failover enqueue path + manual force + watchdog.

When FAILOVER_QUEUE_URL is set, _check_and_handle_az_failover claims each
affected tenant (running→failover_queued), reserves a target vm_num atomically,
and sends one SQS message per tenant instead of recovering synchronously. These
tests pin that behavior, the manual-failover force path (T3-2 P2), and the
stuck-failover watchdog (T3-2 P3). The legacy synchronous path (flag unset) is
covered by the untouched test_az_failover.py suite.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

pytestmark = pytest.mark.unit

QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/123/openclaw-failover"


def _load_hc(env_overrides=None):
    """Load health_check/handler.py with mocked AWS incl. an SQS client,
    following the test_az_failover.py loader style."""
    import os
    saved = {}
    for k, v in (env_overrides or {}).items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    mock_ddb = MagicMock()
    mock_ssm = MagicMock()
    mock_sns = MagicMock()
    mock_s3 = MagicMock()
    mock_elbv2 = MagicMock()
    mock_sqs = MagicMock()

    from datetime import timezone as _tz
    mock_s3.list_objects_v2.return_value = {
        "Contents": [{"Key": "backups/t1/2026-05-23T18:00:00Z.gz",
                      "LastModified": datetime(2026, 5, 23, 18, 0, 0, tzinfo=_tz.utc)}],
    }

    table_cache = {}

    def _table_factory(name):
        if name not in table_cache:
            table_cache[name] = make_ddb_table()
        return table_cache[name]

    mock_ddb.Table.side_effect = _table_factory

    def _client_factory(svc):
        return {"ssm": mock_ssm, "sns": mock_sns, "s3": mock_s3,
                "elbv2": mock_elbv2, "sqs": mock_sqs}.get(svc, MagicMock())

    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", side_effect=_client_factory):
        spec = importlib.util.spec_from_file_location(
            "hc_handler_q", "deploy/lambda/health_check/handler.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hc_handler_q"] = mod
        spec.loader.exec_module(mod)
    mod._test_mocks = {"ddb": mock_ddb, "ssm": mock_ssm, "sns": mock_sns,
                       "s3": mock_s3, "elbv2": mock_elbv2, "sqs": mock_sqs,
                       "tables": table_cache}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return mod


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _stale_host(iid, az, seconds_stale=3600):
    return {"instance_id": iid, "az": az, "status": "active",
            "last_health_check": _ago(seconds_stale)}


def _healthy_host(iid, az, total_vcpu=16):
    return {"instance_id": iid, "az": az, "status": "active",
            "last_health_check": _ago(5), "total_vcpu": total_vcpu,
            "used_vcpu": 0, "vm_count": 0, "next_vm_num": 1,
            "private_ip": "10.0.0.9"}


def _tenant(tid, host_id, vcpu=2):
    return {"id": tid, "status": "running", "host_id": host_id,
            "vcpu": vcpu, "mem_mb": 4096}


# ══════════════════════════════════════════════════════════════════════
# Enqueue path
# ══════════════════════════════════════════════════════════════════════


class TestEnqueuePath:
    def _setup(self, tenants, hosts):
        hc = _load_hc({"FAILOVER_QUEUE_URL": QUEUE_URL, "ASSETS_BUCKET": "b",
                       "AZ_FAILOVER_ENABLED": "true"})
        hc.hosts_table.scan = MagicMock(return_value={"Items": hosts})
        hc.hosts_table.get_item = MagicMock(return_value={"Item": {}})
        # Atomic vm_num reservation returns UPDATED_OLD.
        hc.hosts_table.update_item = MagicMock(
            return_value={"Attributes": {"next_vm_num": 1}})
        hc.hosts_table.put_item = MagicMock()
        hc.tenants_table.update_item = MagicMock()
        return hc

    def test_one_message_per_affected_tenant_and_no_sync_failover(self):
        hosts = [_stale_host("i-dead", "az-a"), _healthy_host("i-ok", "az-b")]
        tenants = [_tenant("t1", "i-dead"), _tenant("t2", "i-dead")]
        hc = self._setup(tenants, hosts)
        now = datetime.now(timezone.utc)
        with patch.object(hc, "_failover_tenant_to_host",
                          side_effect=AssertionError("sync path must not run")):
            summary = hc._check_and_handle_az_failover(now, tenants)
        assert summary["tenants_queued"] == 2
        sends = hc._test_mocks["sqs"].send_message.call_args_list
        assert len(sends) == 2
        bodies = [json.loads(c.kwargs["MessageBody"]) for c in sends]
        assert {b["tenant_id"] for b in bodies} == {"t1", "t2"}
        for b in bodies:
            assert b["target_host_id"] == "i-ok"
            assert b["backup_key"].startswith("backups/")
            assert "target_vm_num" in b
        # Each tenant claimed running→failover_queued.
        claim_calls = [c for c in hc.tenants_table.update_item.call_args_list
                       if c.kwargs.get("ExpressionAttributeValues", {}).get(":queued") == "failover_queued"]
        assert len(claim_calls) == 2

    def test_no_backup_tenant_is_blocked_not_queued(self):
        hosts = [_stale_host("i-dead", "az-a"), _healthy_host("i-ok", "az-b")]
        tenants = [_tenant("t1", "i-dead")]
        hc = self._setup(tenants, hosts)
        hc._test_mocks["s3"].list_objects_v2.return_value = {"Contents": []}
        now = datetime.now(timezone.utc)
        summary = hc._check_and_handle_az_failover(now, tenants)
        assert summary["tenants_blocked"] == 1
        assert summary["tenants_queued"] == 0
        hc._test_mocks["sqs"].send_message.assert_not_called()

    def test_vm_num_reserved_with_updated_old(self):
        hosts = [_stale_host("i-dead", "az-a"), _healthy_host("i-ok", "az-b")]
        tenants = [_tenant("t1", "i-dead")]
        hc = self._setup(tenants, hosts)
        hc.hosts_table.update_item = MagicMock(
            return_value={"Attributes": {"next_vm_num": 7}})
        now = datetime.now(timezone.utc)
        hc._check_and_handle_az_failover(now, tenants)
        # The reservation call uses an atomic increment + ReturnValues.
        reserve = [c for c in hc.hosts_table.update_item.call_args_list
                   if c.kwargs.get("ReturnValues") == "UPDATED_OLD"]
        assert reserve, "vm_num reservation must use ReturnValues=UPDATED_OLD"
        body = json.loads(hc._test_mocks["sqs"].send_message.call_args.kwargs["MessageBody"])
        assert body["target_vm_num"] == 7  # the pre-increment slot

    def test_claim_conflict_skips_without_message(self):
        hosts = [_stale_host("i-dead", "az-a"), _healthy_host("i-ok", "az-b")]
        tenants = [_tenant("t1", "i-dead")]
        hc = self._setup(tenants, hosts)
        exc = hc.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
        hc.tenants_table.update_item = MagicMock(
            side_effect=exc({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"))
        now = datetime.now(timezone.utc)
        summary = hc._check_and_handle_az_failover(now, tenants)
        assert summary["tenants_queued"] == 0
        hc._test_mocks["sqs"].send_message.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# Manual force (T3-2 P2) — closes the T2-8 no-op gap
# ══════════════════════════════════════════════════════════════════════


class TestManualForce:
    def test_lambda_handler_forces_synthetic_outage_for_healthy_az(self):
        # az-a hosts are HEALTHY (recent heartbeat) so auto-detection would NOT
        # flag them; the manual force must still enqueue their tenants.
        hosts = [_healthy_host("i-a", "az-a"), _healthy_host("i-b", "az-b")]
        tenants = [_tenant("t1", "i-a")]
        hc = _load_hc({"FAILOVER_QUEUE_URL": QUEUE_URL, "ASSETS_BUCKET": "b",
                       "AZ_FAILOVER_ENABLED": "false"})  # even with auto OFF
        hc.tenants_table.scan = MagicMock(return_value={"Items": tenants})
        hc.hosts_table.scan = MagicMock(return_value={"Items": hosts})
        hc.hosts_table.get_item = MagicMock(return_value={"Item": {}})
        hc.hosts_table.update_item = MagicMock(
            return_value={"Attributes": {"next_vm_num": 1}})
        hc.hosts_table.put_item = MagicMock()
        hc.tenants_table.update_item = MagicMock()
        hc.lambda_handler({"manual_failover_az": "az-a"}, None)
        # t1 on the forced AZ got enqueued to a target OUTSIDE az-a.
        assert hc._test_mocks["sqs"].send_message.called
        body = json.loads(hc._test_mocks["sqs"].send_message.call_args.kwargs["MessageBody"])
        assert body["tenant_id"] == "t1"
        assert body["target_host_id"] == "i-b"

    def test_forced_az_bypasses_cooldown(self):
        hosts = [_healthy_host("i-a", "az-a"), _healthy_host("i-b", "az-b")]
        tenants = [_tenant("t1", "i-a")]
        hc = _load_hc({"FAILOVER_QUEUE_URL": QUEUE_URL, "ASSETS_BUCKET": "b",
                       "AZ_FAILOVER_ENABLED": "true"})
        hc.hosts_table.scan = MagicMock(return_value={"Items": hosts})
        # Cooldown state says az-a failed over 1 minute ago (well inside 30min).
        hc.hosts_table.get_item = MagicMock(return_value={"Item": {
            "instance_id": "__az_failover_state__",
            "az_last_failover": {"az-a": _ago(60)}}})
        hc.hosts_table.update_item = MagicMock(
            return_value={"Attributes": {"next_vm_num": 1}})
        hc.hosts_table.put_item = MagicMock()
        hc.tenants_table.update_item = MagicMock()
        now = datetime.now(timezone.utc)
        summary = hc._check_and_handle_az_failover(now, tenants, force_azs={"az-a"})
        # Cooldown did NOT skip it despite the recent stamp.
        assert "az-a" not in summary["skipped_cooldown"]
        assert summary["tenants_queued"] == 1


# ══════════════════════════════════════════════════════════════════════
# Watchdog (T3-2 P3)
# ══════════════════════════════════════════════════════════════════════


class TestFailoverWatchdog:
    def test_stuck_queued_tenant_flipped_to_failed(self):
        hc = _load_hc({"FAILOVER_QUEUE_URL": QUEUE_URL,
                       "FAILOVER_WATCHDOG_MINUTES": "30"})
        stuck = {"id": "t1", "status": "failover_queued", "failover_at": _ago(3600)}
        hc.tenants_table.scan = MagicMock(return_value={"Items": [stuck]})
        hc.tenants_table.update_item = MagicMock()
        hc._sweep_stuck_failovers(datetime.now(timezone.utc))
        flip = hc.tenants_table.update_item.call_args
        assert flip.kwargs["ExpressionAttributeValues"][":f"] == "failover_failed"

    def test_recent_queued_tenant_left_alone(self):
        hc = _load_hc({"FAILOVER_QUEUE_URL": QUEUE_URL,
                       "FAILOVER_WATCHDOG_MINUTES": "30"})
        fresh = {"id": "t1", "status": "failover_queued", "failover_at": _ago(60)}
        hc.tenants_table.scan = MagicMock(return_value={"Items": [fresh]})
        hc.tenants_table.update_item = MagicMock()
        hc._sweep_stuck_failovers(datetime.now(timezone.utc))
        hc.tenants_table.update_item.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# Legacy path regression guard
# ══════════════════════════════════════════════════════════════════════


class TestLegacyFallback:
    def test_unset_flag_uses_synchronous_path(self):
        hosts = [_stale_host("i-dead", "az-a"), _healthy_host("i-ok", "az-b")]
        tenants = [_tenant("t1", "i-dead")]
        hc = _load_hc({"ASSETS_BUCKET": "b", "AZ_FAILOVER_ENABLED": "true"})
        # FAILOVER_QUEUE_URL not set → empty
        assert hc.FAILOVER_QUEUE_URL == ""
        hc.hosts_table.scan = MagicMock(return_value={"Items": hosts})
        hc.hosts_table.get_item = MagicMock(return_value={"Item": {}})
        hc.hosts_table.put_item = MagicMock()
        hc.hosts_table.update_item = MagicMock()
        hc.tenants_table.update_item = MagicMock()
        now = datetime.now(timezone.utc)
        with patch.object(hc, "_failover_tenant_to_host", return_value=True) as sync:
            summary = hc._check_and_handle_az_failover(now, tenants)
        sync.assert_called_once()
        assert summary["tenants_failed_over"] == 1
        hc._test_mocks["sqs"].send_message.assert_not_called()
