# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Issue #77 — scheduler spread, concurrency-safe ALB priority, atomic host
reservation, and per-host VM ceiling.

Regression coverage for the three independent bugs the batch-provisioning
incident exposed:
  1. _find_host returned first-fit → all tenants piled onto one host.
  2. _add_alb_rule read priorities once → PriorityInUseException 500s under
     concurrency.
  3. host capacity check + counter write were not atomic → double-booking.
"""

import importlib.util
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from conftest import make_ddb_table

_mock_ddb = MagicMock()
_mock_elbv2 = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {"elbv2": _mock_elbv2}.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "sched_handler", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["sched_handler"] = api
    spec.loader.exec_module(api)

pytestmark = pytest.mark.unit


def _host(iid, total_vcpu=15, used_vcpu=0, total_mem_mb=32768, used_mem_mb=0,
          vm_count=0, status="active", next_vm_num=1):
    return {"instance_id": iid, "total_vcpu": total_vcpu, "used_vcpu": used_vcpu,
            "total_mem_mb": total_mem_mb, "used_mem_mb": used_mem_mb,
            "vm_count": vm_count, "status": status, "next_vm_num": next_vm_num}


def _reset_overcommit(cpu=2.0, mem=1.5, max_vms=0):
    api.CPU_OVERCOMMIT_RATIO = cpu
    api.MEM_OVERCOMMIT_RATIO = mem
    api.MAX_VMS_PER_HOST = max_vms


class TestFindHostSpread:
    def test_picks_least_loaded_host(self):
        """With three hosts of differing load, _find_host must return the one
        with the MOST free vCPU, not the first in scan order (issue #77)."""
        _reset_overcommit()
        api.hosts_table = make_ddb_table()
        # scan order deliberately puts the busiest host first.
        api.hosts_table.scan.return_value = {"Items": [
            _host("i-busy", used_vcpu=28),   # 15*2=30 allocatable, 2 free
            _host("i-idle", used_vcpu=0),    # 30 free  ← should win
            _host("i-mid", used_vcpu=15),    # 15 free
        ]}
        picked = api._find_host(1, 1024)
        assert picked["instance_id"] == "i-idle"

    def test_tie_break_is_deterministic(self):
        """Equal free capacity → lowest instance_id wins (stable ordering)."""
        _reset_overcommit()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [
            _host("i-bbb", used_vcpu=10),
            _host("i-aaa", used_vcpu=10),
        ]}
        assert api._find_host(1, 1024)["instance_id"] == "i-aaa"

    def test_none_when_no_capacity(self):
        _reset_overcommit()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host("i-full", used_vcpu=30)]}
        assert api._find_host(2, 0) is None

    def test_vm_ceiling_excludes_full_host(self):
        """MAX_VMS_PER_HOST caps a host even when the ratio math still fits."""
        _reset_overcommit(max_vms=5)
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [
            _host("i-capped", used_vcpu=0, vm_count=5),   # at ceiling → excluded
            _host("i-ok", used_vcpu=20, vm_count=1),      # room by count
        ]}
        assert api._find_host(1, 1024)["instance_id"] == "i-ok"
        _reset_overcommit()  # restore


class TestAddAlbRulePriorityRace:
    def _listener(self):
        api.ALB_LISTENER_ARN = "arn:aws:elasticloadbalancing:::listener/x"

    def test_retries_on_priority_in_use(self):
        """First create_rule hits PriorityInUseException; the retry re-reads
        and succeeds instead of surfacing a 500 (issue #77)."""
        self._listener()
        api.elbv2 = MagicMock()
        api.elbv2.describe_rules.return_value = {"Rules": [{"Priority": "default"}]}
        conflict = ClientError(
            {"Error": {"Code": "PriorityInUseException", "Message": "in use"}},
            "CreateRule")
        api.elbv2.create_rule.side_effect = [conflict, None]
        api._add_alb_rule("tt-1", "arn:tg")
        assert api.elbv2.create_rule.call_count == 2

    def test_idempotent_when_rule_exists(self):
        """If a concurrent create already added the tenant rule, do nothing."""
        self._listener()
        api.elbv2 = MagicMock()
        api.elbv2.describe_rules.return_value = {"Rules": [
            {"Priority": "5", "Conditions": [
                {"Field": "path-pattern", "Values": ["/vm/tt-1", "/vm/tt-1/*"]}]},
        ]}
        api._add_alb_rule("tt-1", "arn:tg")
        api.elbv2.create_rule.assert_not_called()

    def test_non_priority_error_reraised(self):
        self._listener()
        api.elbv2 = MagicMock()
        api.elbv2.describe_rules.return_value = {"Rules": [{"Priority": "default"}]}
        api.elbv2.create_rule.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "CreateRule")
        with pytest.raises(ClientError):
            api._add_alb_rule("tt-1", "arn:tg")


class TestReserveHostSlotAtomic:
    def test_condition_failure_returns_none(self):
        """A ConditionalCheckFailedException (host filled up) → None, so the
        caller falls back to pending instead of double-booking."""
        _reset_overcommit()
        api.hosts_table = make_ddb_table()
        api.hosts_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "full"}},
            "UpdateItem")
        assert api._reserve_host_slot(_host("i-x"), 2, 4096) is None

    def test_returns_vm_num_from_atomic_counter(self):
        """The reserved vm_num comes from the atomically-incremented
        next_vm_num DynamoDB returns (UPDATED_NEW), guaranteeing uniqueness."""
        _reset_overcommit()
        api.hosts_table = make_ddb_table()
        api.hosts_table.update_item.return_value = {"Attributes": {"next_vm_num": 8}}
        assert api._reserve_host_slot(_host("i-x", next_vm_num=7), 1, 1024) == 7

    def test_condition_includes_vm_cap_when_set(self):
        """When MAX_VMS_PER_HOST is set, the reservation's ConditionExpression
        must also guard vm_count."""
        _reset_overcommit(max_vms=10)
        api.hosts_table = make_ddb_table()
        api.hosts_table.update_item.return_value = {"Attributes": {"next_vm_num": 2}}
        api._reserve_host_slot(_host("i-x"), 1, 1024)
        _, kwargs = api.hosts_table.update_item.call_args
        assert "vm_count < :maxvm" in kwargs["ConditionExpression"]
        assert kwargs["ExpressionAttributeValues"][":maxvm"] == 10
        _reset_overcommit()

    def test_condition_has_no_arithmetic(self):
        """Live-deploy regression (1.5.8): DynamoDB ConditionExpression does
        NOT support arithmetic — `used_vcpu + :v <= :cap` is a
        ValidationException on real DynamoDB, but MagicMock happily accepted
        it, so only the live e2e caught it. Guard the syntax statically: the
        capacity ceiling must be precomputed client-side."""
        _reset_overcommit()
        api.hosts_table = make_ddb_table()
        api.hosts_table.update_item.return_value = {"Attributes": {"next_vm_num": 2}}
        api._reserve_host_slot(_host("i-x"), 2, 4096)
        _, kwargs = api.hosts_table.update_item.call_args
        cond = kwargs["ConditionExpression"]
        assert "+" not in cond and "-" not in cond and "*" not in cond, (
            f"arithmetic in ConditionExpression is invalid DynamoDB syntax: {cond}")
        # The precomputed ceilings must reflect allocatable - requested.
        vals = kwargs["ExpressionAttributeValues"]
        assert vals[":max_used_v"] == int(15 * 2.0) - 2
        assert vals[":max_used_m"] == int(32768 * 1.5) - 4096
