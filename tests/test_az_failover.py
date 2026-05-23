# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for AZ-level failover (deploy/lambda/health_check/handler.py).

Two layers:

1. Pure-function logic — is_host_unhealthy / group_hosts_by_az /
   detect_unhealthy_azs / pick_target_host / should_skip_az_for_cooldown.
   These cover threshold edges, missing fields, sorting determinism, and
   exclusion rules without any AWS interaction.

2. Orchestration — _check_and_handle_az_failover / _failover_tenant_to_host.
   These mock DDB / SSM / SNS and assert the right calls happen in the
   right order, plus that errors never propagate up to the watchdog loop.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from conftest import make_ddb_table


# ──────────────────────────────────────────────────────────────────────
# Module loader (re-imports a fresh copy with mocked AWS for each test)
# ──────────────────────────────────────────────────────────────────────


def _load_hc_module(env_overrides=None):
    """Load the health_check Lambda module with mocked AWS clients.

    Re-imported per test so module-level state (e.g. environment-driven
    feature flags) reflects the override.
    """
    import os
    saved = {}
    overrides = env_overrides or {}
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    mock_ddb = MagicMock()
    mock_ssm = MagicMock()
    mock_sns = MagicMock()
    # Per-name table cache so the module gets distinct mocks for
    # tenants/hosts/audit and the test can poke each one.
    table_cache = {}

    def _table_factory(name):
        if name not in table_cache:
            table_cache[name] = make_ddb_table()
        return table_cache[name]

    mock_ddb.Table.side_effect = _table_factory

    def _client_factory(svc):
        return {"ssm": mock_ssm, "sns": mock_sns}.get(svc, MagicMock())

    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", side_effect=_client_factory):
        spec = importlib.util.spec_from_file_location(
            "hc_handler_az", "deploy/lambda/health_check/handler.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hc_handler_az"] = mod
        spec.loader.exec_module(mod)
    # Expose the mocks for the test to poke.
    mod._test_mocks = {"ddb": mock_ddb, "ssm": mock_ssm, "sns": mock_sns,
                       "tables": table_cache}

    # Restore the env after import so the module captures it.
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return mod


def _ago_iso(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ══════════════════════════════════════════════════════════════════════
# 1. is_host_unhealthy — boundary cases on the staleness window
# ══════════════════════════════════════════════════════════════════════


class TestIsHostUnhealthy:
    @pytest.fixture(scope="class")
    def hc(self):
        return _load_hc_module()

    @pytest.mark.unit
    def test_fresh_host_is_healthy(self, hc):
        now = datetime.now(timezone.utc)
        host = {"instance_id": "i-1", "az": "ap-northeast-1a",
                "last_health_check": _ago_iso(30)}
        assert hc.is_host_unhealthy(host, now, threshold_minutes=10) is False

    @pytest.mark.unit
    def test_stale_host_at_threshold_is_unhealthy(self, hc):
        now = datetime.now(timezone.utc)
        # Use a timestamp comfortably past the threshold to avoid floating-point
        # creep between _ago_iso() and the comparison inside is_host_unhealthy.
        host = {"instance_id": "i-1", "az": "az-a",
                "last_health_check": _ago_iso(10 * 60 + 5)}
        assert hc.is_host_unhealthy(host, now, threshold_minutes=10) is True

    @pytest.mark.unit
    def test_just_under_threshold_still_healthy(self, hc):
        now = datetime.now(timezone.utc)
        host = {"instance_id": "i-1", "az": "az-a",
                "last_health_check": _ago_iso(10 * 60 - 5)}
        assert hc.is_host_unhealthy(host, now, threshold_minutes=10) is False

    @pytest.mark.unit
    def test_missing_health_check_is_unhealthy(self, hc):
        now = datetime.now(timezone.utc)
        host = {"instance_id": "i-1", "az": "az-a"}
        assert hc.is_host_unhealthy(host, now, threshold_minutes=10) is True

    @pytest.mark.unit
    def test_falls_back_to_last_seen(self, hc):
        now = datetime.now(timezone.utc)
        host = {"instance_id": "i-1", "az": "az-a", "last_seen": _ago_iso(30)}
        assert hc.is_host_unhealthy(host, now, threshold_minutes=10) is False

    @pytest.mark.unit
    def test_deleted_host_is_unhealthy(self, hc):
        now = datetime.now(timezone.utc)
        host = {"instance_id": "i-1", "az": "az-a",
                "status": "deleted", "last_health_check": _ago_iso(5)}
        assert hc.is_host_unhealthy(host, now, threshold_minutes=10) is True

    @pytest.mark.unit
    def test_garbage_timestamp_treated_as_unhealthy(self, hc):
        now = datetime.now(timezone.utc)
        host = {"instance_id": "i-1", "az": "az-a", "last_health_check": "not-a-date"}
        assert hc.is_host_unhealthy(host, now, threshold_minutes=10) is True

    @pytest.mark.unit
    def test_none_host_is_unhealthy(self, hc):
        now = datetime.now(timezone.utc)
        assert hc.is_host_unhealthy(None, now, threshold_minutes=10) is True


# ══════════════════════════════════════════════════════════════════════
# 2. group_hosts_by_az
# ══════════════════════════════════════════════════════════════════════


class TestGroupHostsByAZ:
    @pytest.fixture(scope="class")
    def hc(self):
        return _load_hc_module()

    @pytest.mark.unit
    def test_groups_correctly(self, hc):
        hosts = [
            {"instance_id": "i-1", "az": "ap-northeast-1a"},
            {"instance_id": "i-2", "az": "ap-northeast-1c"},
            {"instance_id": "i-3", "az": "ap-northeast-1a"},
        ]
        out = hc.group_hosts_by_az(hosts)
        assert set(out.keys()) == {"ap-northeast-1a", "ap-northeast-1c"}
        assert len(out["ap-northeast-1a"]) == 2
        assert len(out["ap-northeast-1c"]) == 1

    @pytest.mark.unit
    def test_skips_hosts_without_az(self, hc):
        hosts = [
            {"instance_id": "i-1", "az": "az-a"},
            {"instance_id": "i-2"},  # no az
        ]
        out = hc.group_hosts_by_az(hosts)
        assert "az-a" in out
        assert all("instance_id" in h and h["instance_id"] != "i-2"
                   for hs in out.values() for h in hs)

    @pytest.mark.unit
    def test_empty_list_returns_empty_dict(self, hc):
        assert hc.group_hosts_by_az([]) == {}


# ══════════════════════════════════════════════════════════════════════
# 3. detect_unhealthy_azs
# ══════════════════════════════════════════════════════════════════════


class TestDetectUnhealthyAZs:
    @pytest.fixture(scope="class")
    def hc(self):
        return _load_hc_module()

    @pytest.mark.unit
    def test_all_hosts_in_az_stale_flags_az(self, hc):
        now = datetime.now(timezone.utc)
        hosts = [
            {"instance_id": "i-1", "az": "az-a", "last_health_check": _ago_iso(20 * 60)},
            {"instance_id": "i-2", "az": "az-a", "last_health_check": _ago_iso(15 * 60)},
            {"instance_id": "i-3", "az": "az-c", "last_health_check": _ago_iso(30)},
        ]
        out = hc.detect_unhealthy_azs(hosts, now, threshold_minutes=10)
        assert len(out) == 1
        assert out[0]["az"] == "az-a"
        assert set(out[0]["host_ids"]) == {"i-1", "i-2"}

    @pytest.mark.unit
    def test_one_healthy_host_keeps_az_healthy(self, hc):
        now = datetime.now(timezone.utc)
        hosts = [
            {"instance_id": "i-1", "az": "az-a", "last_health_check": _ago_iso(20 * 60)},
            {"instance_id": "i-2", "az": "az-a", "last_health_check": _ago_iso(60)},  # fresh
        ]
        assert hc.detect_unhealthy_azs(hosts, now, threshold_minutes=10) == []

    @pytest.mark.unit
    def test_az_with_no_hosts_not_flagged(self, hc):
        # No hosts at all → cannot flag any AZ
        now = datetime.now(timezone.utc)
        assert hc.detect_unhealthy_azs([], now, threshold_minutes=10) == []

    @pytest.mark.unit
    def test_multiple_failed_azs_returned_separately(self, hc):
        now = datetime.now(timezone.utc)
        hosts = [
            {"instance_id": "i-1", "az": "az-a", "last_health_check": _ago_iso(15 * 60)},
            {"instance_id": "i-2", "az": "az-b", "last_health_check": _ago_iso(15 * 60)},
            {"instance_id": "i-3", "az": "az-c", "last_health_check": _ago_iso(60)},
        ]
        out = hc.detect_unhealthy_azs(hosts, now, threshold_minutes=10)
        flagged = {o["az"] for o in out}
        assert flagged == {"az-a", "az-b"}


# ══════════════════════════════════════════════════════════════════════
# 4. pick_target_host
# ══════════════════════════════════════════════════════════════════════


class TestPickTargetHost:
    @pytest.fixture(scope="class")
    def hc(self):
        return _load_hc_module()

    @pytest.mark.unit
    def test_picks_host_with_most_spare_vcpu(self, hc):
        now = datetime.now(timezone.utc)
        hosts = [
            {"instance_id": "i-busy", "az": "az-c", "last_health_check": _ago_iso(30),
             "vcpu_total": 16, "vm_count": 7, "avg_vcpu_per_vm": 2},
            {"instance_id": "i-free", "az": "az-c", "last_health_check": _ago_iso(30),
             "vcpu_total": 16, "vm_count": 1, "avg_vcpu_per_vm": 2},
        ]
        target = hc.pick_target_host(hosts, now, threshold_minutes=10,
                                     exclude_azs={"az-a"})
        assert target["instance_id"] == "i-free"

    @pytest.mark.unit
    def test_excludes_failed_az(self, hc):
        now = datetime.now(timezone.utc)
        hosts = [
            {"instance_id": "i-bad", "az": "az-a", "last_health_check": _ago_iso(30),
             "vcpu_total": 16, "vm_count": 0, "avg_vcpu_per_vm": 2},
            {"instance_id": "i-ok", "az": "az-c", "last_health_check": _ago_iso(30),
             "vcpu_total": 16, "vm_count": 5, "avg_vcpu_per_vm": 2},
        ]
        target = hc.pick_target_host(hosts, now, threshold_minutes=10,
                                     exclude_azs={"az-a"})
        assert target["instance_id"] == "i-ok"

    @pytest.mark.unit
    def test_excludes_unhealthy_host(self, hc):
        now = datetime.now(timezone.utc)
        hosts = [
            {"instance_id": "i-stale", "az": "az-c", "last_health_check": _ago_iso(20 * 60),
             "vcpu_total": 16, "vm_count": 0, "avg_vcpu_per_vm": 2},
        ]
        assert hc.pick_target_host(hosts, now, threshold_minutes=10, exclude_azs=set()) is None

    @pytest.mark.unit
    def test_required_vcpu_filter(self, hc):
        now = datetime.now(timezone.utc)
        # Only host has 2 spare vcpu but we need 4 → no candidate.
        hosts = [
            {"instance_id": "i-tight", "az": "az-c", "last_health_check": _ago_iso(30),
             "vcpu_total": 4, "vm_count": 1, "avg_vcpu_per_vm": 2},
        ]
        assert hc.pick_target_host(hosts, now, threshold_minutes=10,
                                   exclude_azs=set(), required_vcpu=4) is None
        assert hc.pick_target_host(hosts, now, threshold_minutes=10,
                                   exclude_azs=set(), required_vcpu=2) is not None

    @pytest.mark.unit
    def test_no_candidates_returns_none(self, hc):
        now = datetime.now(timezone.utc)
        assert hc.pick_target_host([], now, threshold_minutes=10, exclude_azs=set()) is None

    @pytest.mark.unit
    def test_skips_deleted_hosts(self, hc):
        now = datetime.now(timezone.utc)
        hosts = [
            {"instance_id": "i-dead", "az": "az-c", "last_health_check": _ago_iso(30),
             "status": "deleted", "vcpu_total": 16, "vm_count": 0, "avg_vcpu_per_vm": 2},
        ]
        assert hc.pick_target_host(hosts, now, threshold_minutes=10, exclude_azs=set()) is None

    @pytest.mark.unit
    def test_deterministic_tiebreak(self, hc):
        """Two equally-good hosts → lexicographically smaller instance_id wins."""
        now = datetime.now(timezone.utc)
        hosts = [
            {"instance_id": "i-zzz", "az": "az-c", "last_health_check": _ago_iso(30),
             "vcpu_total": 16, "vm_count": 0, "avg_vcpu_per_vm": 2},
            {"instance_id": "i-aaa", "az": "az-c", "last_health_check": _ago_iso(30),
             "vcpu_total": 16, "vm_count": 0, "avg_vcpu_per_vm": 2},
        ]
        target = hc.pick_target_host(hosts, now, threshold_minutes=10, exclude_azs=set())
        assert target["instance_id"] == "i-aaa"


# ══════════════════════════════════════════════════════════════════════
# 5. should_skip_az_for_cooldown
# ══════════════════════════════════════════════════════════════════════


class TestCooldown:
    @pytest.fixture(scope="class")
    def hc(self):
        return _load_hc_module()

    @pytest.mark.unit
    def test_no_prior_failover_proceeds(self, hc):
        now = datetime.now(timezone.utc)
        assert hc.should_skip_az_for_cooldown({}, "az-a", now, cooldown_minutes=30) is False

    @pytest.mark.unit
    def test_recent_failover_within_cooldown_skips(self, hc):
        now = datetime.now(timezone.utc)
        state = {"az-a": _ago_iso(10 * 60)}  # 10 min ago, cooldown 30
        assert hc.should_skip_az_for_cooldown(state, "az-a", now, cooldown_minutes=30) is True

    @pytest.mark.unit
    def test_old_failover_outside_cooldown_proceeds(self, hc):
        now = datetime.now(timezone.utc)
        state = {"az-a": _ago_iso(45 * 60)}
        assert hc.should_skip_az_for_cooldown(state, "az-a", now, cooldown_minutes=30) is False

    @pytest.mark.unit
    def test_garbage_state_treated_as_no_skip(self, hc):
        now = datetime.now(timezone.utc)
        state = {"az-a": "this-is-not-a-date"}
        assert hc.should_skip_az_for_cooldown(state, "az-a", now, cooldown_minutes=30) is False


# ══════════════════════════════════════════════════════════════════════
# 6. _check_and_handle_az_failover — integration
# ══════════════════════════════════════════════════════════════════════


class TestAZFailoverOrchestration:
    """Mocked-AWS integration tests for the orchestrator."""

    def _make_hc(self):
        """Set up a fresh hc module with AZ failover ENABLED + mock tables."""
        return _load_hc_module(env_overrides={
            "AZ_FAILOVER_ENABLED": "true",
            "AZ_UNHEALTHY_THRESHOLD_MINUTES": "10",
            "AZ_COOLDOWN_MINUTES": "30",
            "AUDIT_TABLE": "openclaw-audit-log",
            "SNS_TOPIC_ARN": "arn:aws:sns:ap-northeast-1:123:openclaw",
            "ASSETS_BUCKET": "openclaw-assets-test",
        })

    @pytest.mark.unit
    def test_no_outage_returns_clean_summary(self):
        hc = self._make_hc()
        now = datetime.now(timezone.utc)
        # Set up mocks: 2 hosts in 2 AZs, both healthy.
        hc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "az": "az-a", "last_health_check": _ago_iso(30),
             "vcpu_total": 16, "vm_count": 2},
            {"instance_id": "i-2", "az": "az-c", "last_health_check": _ago_iso(30),
             "vcpu_total": 16, "vm_count": 2},
        ]}
        hc.hosts_table.get_item.return_value = {}
        result = hc._check_and_handle_az_failover(now, [])
        assert result["az_outages_detected"] == 0
        assert result["tenants_failed_over"] == 0

    @pytest.mark.unit
    def test_failover_full_path(self):
        hc = self._make_hc()
        now = datetime.now(timezone.utc)
        # az-a all dead, az-c healthy.
        hc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1a", "az": "az-a", "last_health_check": _ago_iso(15 * 60),
             "vcpu_total": 16, "vm_count": 1},
            {"instance_id": "i-2c", "az": "az-c", "last_health_check": _ago_iso(60),
             "vcpu_total": 16, "vm_count": 1, "next_vm_num": 2,
             "avg_vcpu_per_vm": 2},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {}}  # no cooldown state

        tenants = [
            {"id": "t-stuck", "host_id": "i-1a", "status": "running",
             "vcpu": 2, "mem_mb": 4096},
        ]
        result = hc._check_and_handle_az_failover(now, tenants)
        assert result["az_outages_detected"] == 1
        assert result["tenants_failed_over"] == 1
        # SSM was called to launch on the target host.
        ssm = hc._test_mocks["ssm"]
        assert ssm.send_command.called
        sent_args = ssm.send_command.call_args
        assert sent_args.kwargs["InstanceIds"] == ["i-2c"]
        cmd = sent_args.kwargs["Parameters"]["commands"][0]
        assert "launch-vm.sh t-stuck" in cmd
        # SNS notification fired.
        sns = hc._test_mocks["sns"]
        assert sns.publish.called
        msg = json.loads(sns.publish.call_args.kwargs["Message"])
        assert msg["az"] == "az-a"

    @pytest.mark.unit
    def test_cooldown_skips_repeat_outage(self):
        hc = self._make_hc()
        now = datetime.now(timezone.utc)
        hc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1a", "az": "az-a", "last_health_check": _ago_iso(15 * 60),
             "vcpu_total": 16, "vm_count": 0},
            {"instance_id": "i-2c", "az": "az-c", "last_health_check": _ago_iso(60),
             "vcpu_total": 16, "vm_count": 0, "next_vm_num": 1, "avg_vcpu_per_vm": 2},
        ]}
        # Cooldown still active (10 min ago, threshold 30 min).
        hc.hosts_table.get_item.return_value = {"Item": {
            "instance_id": "__az_failover_state__",
            "az_last_failover": {"az-a": _ago_iso(10 * 60)},
        }}
        tenants = [{"id": "t-stuck", "host_id": "i-1a", "status": "running",
                    "vcpu": 2, "mem_mb": 4096}]
        result = hc._check_and_handle_az_failover(now, tenants)
        assert "az-a" in result["skipped_cooldown"]
        # SSM not called → no relaunch.
        assert hc._test_mocks["ssm"].send_command.called is False

    @pytest.mark.unit
    def test_no_healthy_target_az_skips_with_audit(self):
        hc = self._make_hc()
        now = datetime.now(timezone.utc)
        # Both AZs failed.
        hc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1a", "az": "az-a", "last_health_check": _ago_iso(15 * 60),
             "vcpu_total": 16, "vm_count": 1},
            {"instance_id": "i-2c", "az": "az-c", "last_health_check": _ago_iso(15 * 60),
             "vcpu_total": 16, "vm_count": 1},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {}}
        tenants = [{"id": "t-stuck", "host_id": "i-1a", "status": "running",
                    "vcpu": 2, "mem_mb": 4096}]
        result = hc._check_and_handle_az_failover(now, tenants)
        # Outages flagged, but no tenants moved.
        assert result["az_outages_detected"] >= 1
        assert result["tenants_failed_over"] == 0
        # No SSM relaunch.
        assert hc._test_mocks["ssm"].send_command.called is False

    @pytest.mark.unit
    def test_audit_failure_does_not_break_orchestrator(self):
        hc = self._make_hc()
        now = datetime.now(timezone.utc)
        hc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1a", "az": "az-a", "last_health_check": _ago_iso(15 * 60),
             "vcpu_total": 16, "vm_count": 0},
            {"instance_id": "i-2c", "az": "az-c", "last_health_check": _ago_iso(60),
             "vcpu_total": 16, "vm_count": 0, "next_vm_num": 1, "avg_vcpu_per_vm": 2},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {}}
        # Force audit_table.put_item to raise.
        hc.audit_table.put_item.side_effect = Exception("kaboom")
        tenants = [{"id": "t-stuck", "host_id": "i-1a", "status": "running",
                    "vcpu": 2, "mem_mb": 4096}]
        # Must not raise.
        result = hc._check_and_handle_az_failover(now, tenants)
        assert result["tenants_failed_over"] == 1


# ══════════════════════════════════════════════════════════════════════
# 7. _failover_tenant_to_host — single tenant relaunch
# ══════════════════════════════════════════════════════════════════════


class TestFailoverTenant:
    def _make_hc(self):
        return _load_hc_module(env_overrides={
            "AZ_FAILOVER_ENABLED": "true",
            "ASSETS_BUCKET": "openclaw-assets-test",
        })

    @pytest.mark.unit
    def test_happy_path_updates_ddb_and_calls_ssm(self):
        hc = self._make_hc()
        now = datetime.now(timezone.utc)
        tenant = {"id": "t1", "host_id": "i-old", "vcpu": 4, "mem_mb": 8192,
                  "status": "running"}
        target = {"instance_id": "i-new", "az": "az-c", "next_vm_num": 5}
        ok = hc._failover_tenant_to_host(tenant, target, source_az="az-a", now=now)
        assert ok is True
        # tenants_table.update_item called twice: (1) mark recovering, (2) flip ownership.
        update_calls = hc.tenants_table.update_item.call_args_list
        assert len(update_calls) >= 2
        first = update_calls[0].kwargs
        assert "failover_recovering" in str(first)
        last = update_calls[-1].kwargs
        assert last["ExpressionAttributeValues"][":h"] == "i-new"
        assert last["ExpressionAttributeValues"][":n"] == 5
        # SSM launch command targeted the new host.
        ssm = hc._test_mocks["ssm"]
        cmd = ssm.send_command.call_args.kwargs["Parameters"]["commands"][0]
        assert "launch-vm.sh t1 5 4 8192" in cmd
        # restore-from S3 backup hint should be appended.
        assert "--restore-from s3://openclaw-assets-test/backups/t1/latest.gz" in cmd

    @pytest.mark.unit
    def test_ssm_failure_marks_tenant_failed(self):
        hc = self._make_hc()
        now = datetime.now(timezone.utc)
        hc._test_mocks["ssm"].send_command.side_effect = Exception("ssm down")
        tenant = {"id": "t1", "host_id": "i-old", "vcpu": 2, "mem_mb": 4096}
        target = {"instance_id": "i-new", "az": "az-c", "next_vm_num": 1}
        ok = hc._failover_tenant_to_host(tenant, target, source_az="az-a", now=now)
        assert ok is False
        # The status should be set to failover_failed at some point.
        seen = [c.kwargs["ExpressionAttributeValues"]
                for c in hc.tenants_table.update_item.call_args_list]
        assert any(":failed" in vals.get("__dummy__", "") or
                   "failover_failed" in str(vals) for vals in seen)


# ══════════════════════════════════════════════════════════════════════
# 8. Feature flag — disabled = noop in lambda_handler
# ══════════════════════════════════════════════════════════════════════


class TestFeatureFlag:
    @pytest.mark.unit
    def test_disabled_skips_orchestrator(self):
        hc = _load_hc_module(env_overrides={"AZ_FAILOVER_ENABLED": "false"})
        # Hook the orchestrator to spy.
        hc._check_and_handle_az_failover = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": []}
        hc.lambda_handler({}, None)
        assert hc._check_and_handle_az_failover.called is False
