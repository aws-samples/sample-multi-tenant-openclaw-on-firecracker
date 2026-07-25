# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the shared deploy/lambda/common/ package (T3-3, Phase 1).

Covers ddb.scan_all pagination and the capacity math that is now the single
source of truth for both the API scheduler and AZ-failover placement.
"""

from unittest.mock import MagicMock, patch

import pytest
from common import audit, capacity, ddb
from common import ssm as ssm_helpers

pytestmark = pytest.mark.unit


class _FakeInvocationDoesNotExist(Exception):
    pass


def _ssm_mock():
    m = MagicMock()
    m.exceptions.InvocationDoesNotExist = _FakeInvocationDoesNotExist
    return m


# ══════════════════════════════════════════════════════════════════════
# ddb.scan_all — pagination
# ══════════════════════════════════════════════════════════════════════


class TestScanAll:
    def test_follows_last_evaluated_key_across_pages(self):
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [{"id": "a"}], "LastEvaluatedKey": {"id": "a"}},
            {"Items": [{"id": "b"}], "LastEvaluatedKey": {"id": "b"}},
            {"Items": [{"id": "c"}]},  # no LEK → last page
        ]
        out = ddb.scan_all(table)
        assert [i["id"] for i in out] == ["a", "b", "c"]
        assert table.scan.call_count == 3

    def test_forwards_kwargs_to_every_page_no_start_key_on_first(self):
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"id": "x"}},
            {"Items": []},
        ]
        ddb.scan_all(table, FilterExpression="#s = :r",
                     ExpressionAttributeNames={"#s": "status"})
        first, second = table.scan.call_args_list
        assert "ExclusiveStartKey" not in first.kwargs
        assert first.kwargs["FilterExpression"] == "#s = :r"
        # Page 2 carries the cursor AND the same filter kwargs.
        assert second.kwargs["ExclusiveStartKey"] == {"id": "x"}
        assert second.kwargs["FilterExpression"] == "#s = :r"

    def test_single_page(self):
        table = MagicMock()
        table.scan.return_value = {"Items": [{"id": "only"}]}
        assert ddb.scan_all(table) == [{"id": "only"}]


# ══════════════════════════════════════════════════════════════════════
# capacity — allocatable / host_free / host_fits / rank_hosts
# ══════════════════════════════════════════════════════════════════════


def _host(iid="i-1", total_vcpu=8, total_mem_mb=16384,
          used_vcpu=0, used_mem_mb=0, vm_count=0):
    return {"instance_id": iid, "total_vcpu": total_vcpu,
            "total_mem_mb": total_mem_mb, "used_vcpu": used_vcpu,
            "used_mem_mb": used_mem_mb, "vm_count": vm_count}


class TestCapacityMath:
    def test_allocatable_applies_ratios_with_int_truncation(self):
        h = _host(total_vcpu=8, total_mem_mb=1000)
        assert capacity.allocatable(h, 1.5, 1.5) == (12, 1500)
        # int() truncates, matching historical API math (8*1.3=10.4→10).
        assert capacity.allocatable(h, 1.3, 1.0) == (10, 1000)

    def test_host_free_subtracts_used_defaulting_zero(self):
        h = _host(total_vcpu=8, used_vcpu=3, total_mem_mb=16384, used_mem_mb=4096)
        assert capacity.host_free(h, 1.0, 1.0) == (5, 12288)
        # Missing used_* fields default to 0.
        bare = {"instance_id": "i", "total_vcpu": 4, "total_mem_mb": 2048}
        assert capacity.host_free(bare, 1.0, 1.0) == (4, 2048)

    def test_host_fits_checks_both_dims(self):
        h = _host(total_vcpu=4, total_mem_mb=4096, used_vcpu=2, used_mem_mb=2048)
        # free = (2, 2048)
        assert capacity.host_fits(h, 2, 2048, 1.0, 1.0) is True
        assert capacity.host_fits(h, 3, 2048, 1.0, 1.0) is False   # vcpu short
        assert capacity.host_fits(h, 2, 2049, 1.0, 1.0) is False   # mem short

    def test_host_fits_enforces_max_vms_when_set(self):
        h = _host(vm_count=3)
        assert capacity.host_fits(h, 1, 1024, 1.0, 1.0, max_vms=0) is True   # 0 = no cap
        assert capacity.host_fits(h, 1, 1024, 1.0, 1.0, max_vms=5) is True
        assert capacity.host_fits(h, 1, 1024, 1.0, 1.0, max_vms=3) is False  # at cap

    def test_rank_hosts_spreads_most_free_first(self):
        a = _host("i-a", used_vcpu=6)   # free 2
        b = _host("i-b", used_vcpu=1)   # free 7  ← most free
        c = _host("i-c", used_vcpu=4)   # free 4
        ranked = capacity.rank_hosts([a, b, c], 1, 1024, cpu_ratio=1.0, mem_ratio=1.0)
        assert [h["instance_id"] for h in ranked] == ["i-b", "i-c", "i-a"]

    def test_rank_hosts_deterministic_instance_id_tiebreak(self):
        a = _host("i-z", used_vcpu=2)
        b = _host("i-a", used_vcpu=2)   # same free → lower id wins
        ranked = capacity.rank_hosts([a, b], 1, 1024, cpu_ratio=1.0, mem_ratio=1.0)
        assert [h["instance_id"] for h in ranked] == ["i-a", "i-z"]

    def test_rank_hosts_excludes_and_filters_unfit(self):
        a = _host("i-a", used_vcpu=0)
        full = _host("i-full", used_vcpu=8)   # free 0 → unfit
        ranked = capacity.rank_hosts([a, full], 1, 1024, cpu_ratio=1.0, mem_ratio=1.0,
                                     exclude_ids={"i-a"})
        assert ranked == []  # a excluded, full doesn't fit


# ══════════════════════════════════════════════════════════════════════
# ssm — send / run / poll / wait
# ══════════════════════════════════════════════════════════════════════


class TestSsmHelpers:
    def test_send_wraps_home_and_returns_command_id(self):
        m = _ssm_mock()
        m.send_command.return_value = {"Command": {"CommandId": "c-1"}}
        cid = ssm_helpers.send(m, "i-1", "launch-vm.sh x", timeout=60)
        assert cid == "c-1"
        cmd = m.send_command.call_args.kwargs["Parameters"]["commands"][0]
        assert cmd.startswith("export HOME=/home/ubuntu && cd /home/ubuntu &&")
        assert "launch-vm.sh x" in cmd

    def test_send_returns_none_on_error(self):
        m = _ssm_mock()
        m.send_command.side_effect = RuntimeError("throttled")
        assert ssm_helpers.send(m, "i-1", "cmd") is None

    def test_run_success_returns_output(self):
        m = _ssm_mock()
        m.send_command.return_value = {"Command": {"CommandId": "c-1"}}
        m.get_command_invocation.return_value = {"Status": "Success",
                                                 "StandardOutputContent": "ok"}
        with patch.object(ssm_helpers.time, "sleep"):
            assert ssm_helpers.run(m, "i-1", "cmd", timeout=9) == (True, "ok")

    def test_run_failure_returns_stderr(self):
        m = _ssm_mock()
        m.send_command.return_value = {"Command": {"CommandId": "c-1"}}
        m.get_command_invocation.return_value = {"Status": "Failed",
                                                 "StandardErrorContent": "boom"}
        with patch.object(ssm_helpers.time, "sleep"):
            assert ssm_helpers.run(m, "i-1", "cmd", timeout=9) == (False, "boom")

    @pytest.mark.parametrize("status,expected", [
        ("Success", (True, True)),
        ("Failed", (True, False)),
        ("TimedOut", (True, False)),
        ("InProgress", (False, False)),
        ("Pending", (False, False)),
    ])
    def test_poll_truth_table(self, status, expected):
        m = _ssm_mock()
        m.get_command_invocation.return_value = {"Status": status}
        assert ssm_helpers.poll(m, "c-1", "i-1") == expected

    def test_poll_not_registered_is_not_done(self):
        m = _ssm_mock()
        m.get_command_invocation.side_effect = _FakeInvocationDoesNotExist()
        assert ssm_helpers.poll(m, "c-1", "i-1") == (False, False)

    def test_wait_success(self):
        m = _ssm_mock()
        m.get_command_invocation.return_value = {"Status": "Success"}
        with patch.object(ssm_helpers.time, "sleep"):
            assert ssm_helpers.wait(m, "c-1", "i-1", timeout_sec=9) == (True, None)

    def test_wait_failure_reports_status(self):
        m = _ssm_mock()
        m.get_command_invocation.return_value = {"Status": "Failed",
                                                 "StandardErrorContent": "x"}
        with patch.object(ssm_helpers.time, "sleep"):
            ok, err = ssm_helpers.wait(m, "c-1", "i-1", timeout_sec=9)
        assert ok is False and "Failed" in err


# ══════════════════════════════════════════════════════════════════════
# audit — unified row writer (fixes health_check's hard-coded 90d TTL)
# ══════════════════════════════════════════════════════════════════════


class TestAuditWriter:
    def test_writes_full_71_schema(self):
        t = MagicMock()
        audit.put_audit_row(t, event="vm.migrated", obj="tenant:t1",
                            resource_id="t1", actor="system:health-check",
                            ttl_days=30, ts="2026-07-25T00:00:00Z")
        item = t.put_item.call_args.kwargs["Item"]
        for k in ("pk", "id", "ts", "operation", "resource_id", "api_key_id",
                  "response_status", "expires_ttl", "event", "object", "actor",
                  "actor_role"):
            assert k in item, f"missing {k}"
        assert item["pk"] == "audit"
        assert item["event"] == "vm.migrated"
        assert item["ts"] == "2026-07-25T00:00:00Z"

    def test_ttl_honors_ttl_days(self):
        import time as _t
        t = MagicMock()
        audit.put_audit_row(t, event="e", obj="o", resource_id="r", actor="a",
                            ttl_days=7)
        ttl = t.put_item.call_args.kwargs["Item"]["expires_ttl"]
        # ~ now + 7 days, allow a few seconds of slack.
        assert abs(ttl - (int(_t.time()) + 7 * 86400)) < 10

    def test_detail_truncated_to_1000(self):
        t = MagicMock()
        audit.put_audit_row(t, event="e", obj="o", resource_id="r", actor="a",
                            detail={"big": "x" * 5000})
        assert len(t.put_item.call_args.kwargs["Item"]["detail"]) == 1000

    def test_none_table_is_noop(self):
        # Must not raise when audit is disabled.
        audit.put_audit_row(None, event="e", obj="o", resource_id="r", actor="a")

    def test_raising_table_never_propagates(self):
        t = MagicMock()
        t.put_item.side_effect = RuntimeError("ddb down")
        # Best-effort: swallow the error.
        audit.put_audit_row(t, event="e", obj="o", resource_id="r", actor="a")
