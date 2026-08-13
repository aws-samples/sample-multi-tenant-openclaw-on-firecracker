# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3-1 P3: prove the per-tenant ALB rule ceiling is gone under host-tg.

The ceiling
-----------
Per-tenant routing consumes one ALB listener rule per tenant. The visible limit
in the code was `range(1, 500)` — but the *binding* constraint is the AWS quota
"Rules per Application Load Balancer", whose **default is 100**. So the real
ceiling was ~5x lower than the code implied, and it surfaced as a create
returning 500 once exhausted.

host-tg routing removes it by construction: one static `/vm/*` catch-all serves
every tenant and the per-tenant nginx peer-map does the rest, so the create path
consumes **no** ALB resource at all.

How this is tested
------------------
Not by creating 500 real tenants. The property that matters is "the create path
no longer consumes a finite shared resource", which is checkable directly: run
600 creates against an ELB double that raises on ANY mutating call. If the
ceiling still existed in any form, one of those calls would happen.

The reverse case matters just as much: per-tenant remains the documented escape
hatch, so its exhaustion must stay an explicit, actionable error. The easiest way
to "remove" the cap during a refactor is to widen the window into the priorities
reserved for static rules — which would let a tenant rule shadow, or fail the
deploy of, the `/vm/*` catch-all itself.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.unit


class _DenyWriteElb:
    """An ELB client that fails loudly on any mutating call."""

    MUTATORS = ("create_rule", "delete_rule", "modify_rule",
                "create_target_group", "delete_target_group",
                "register_targets", "deregister_targets")

    def __init__(self):
        self.reads = 0

    def describe_rules(self, **kw):
        self.reads += 1
        return {"Rules": [{"RuleArn": "arn:rule/999", "Priority": "999",
                           "Conditions": [{"Field": "path-pattern",
                                           "Values": ["/vm/*"]}]}]}

    def describe_target_groups(self, **kw):
        self.reads += 1
        return {"TargetGroups": [{"TargetGroupArn": "arn:tg/shared"}]}

    def __getattr__(self, name):
        if name in self.MUTATORS:
            def _boom(**kw):
                raise AssertionError(
                    f"host-tg create path called elbv2.{name}({kw!r}) — the "
                    "per-tenant ALB rule ceiling is NOT actually removed")
            return _boom
        raise AttributeError(name)


def _load_api(routing_mode):
    mock_ddb = MagicMock()
    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", return_value=MagicMock()), \
         patch.dict("os.environ", {
             "ROUTING_MODE": routing_mode,
             "ALB_LISTENER_ARN": "arn:aws:elasticloadbalancing:x:1:listener/app/a/b/c",
             "VPC_ID": "vpc-test",
         }):
        mock_ddb.Table.side_effect = lambda name: MagicMock()
        name = f"api_ceiling_{routing_mode}"
        spec = importlib.util.spec_from_file_location(
            name, str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


def _host(iid="i-host1", free_vcpu=4096, free_mem=4194304):
    """A deliberately huge host so capacity never becomes the limiting factor —
    this test is about ALB resources, not scheduling."""
    return {
        "instance_id": iid, "status": "active",
        "total_vcpu": free_vcpu, "used_vcpu": 0,
        "total_mem_mb": free_mem, "used_mem_mb": 0,
        "vm_count": 0, "next_vm_num": 1,
        "private_ip": "10.0.1.5", "az": "ap-northeast-1a",
    }


class TestHostTgHasNoAlbCeiling:
    def setup_method(self):
        self.api = _load_api("host-tg")
        self.elb = _DenyWriteElb()
        self.api.elbv2 = self.elb
        self.api.hosts_table = MagicMock()
        self.api.hosts_table.scan.return_value = {"Items": [_host()]}
        self.api.hosts_table.get_item.return_value = {"Item": _host()}
        self.api.tenants_table = MagicMock()
        self.api.tenants_table.get_item.return_value = {}
        self.api.tenants_table.scan.return_value = {"Items": []}

    def test_600_creates_touch_no_alb_mutation(self):
        """600 > the old 499 window AND > the real 100-rule quota."""
        created = 0
        with patch.object(self.api, "_launch_vm"), \
             patch.object(self.api, "_publish_event"), \
             patch.object(self.api, "_audit_write"), \
             patch.object(self.api, "_reserve_host_slot",
                          side_effect=lambda h, v, m: 1):
            for i in range(600):
                resp = self.api.create_tenant(
                    {"name": f"t{i:04d}", "vcpu": 1, "mem_mb": 1024})
                assert resp["statusCode"] in (201, 202), (
                    f"tenant #{i} failed: {resp}")
                assert json.loads(resp["body"])["status"] != "pending", (
                    f"tenant #{i} was queued rather than placed — capacity, not "
                    "ALB, became the limit and this test proves nothing")
                created += 1
        assert created == 600

    def test_add_alb_rule_is_a_noop_under_host_tg(self):
        """The direct unit-level statement of the same property."""
        self.api._add_alb_rule("t1", "arn:tg/x")   # would raise via _DenyWriteElb
        self.api._remove_alb_rule("t1")
        assert self.api._ensure_host_tg("i-host1", "10.0.1.5") == ""

    def test_priority_window_is_never_consulted(self):
        """No priority is allocated, so the window cannot be exhausted."""
        assert self.elb.reads == 0
        self.api._add_alb_rule("t1", "arn:tg/x")
        assert self.elb.reads == 0, (
            "host-tg still reads the live rule set to allocate a priority — it "
            "should not look at ALB rules at all")


class TestPerTenantExhaustionStaysExplicit:
    """per-tenant is the documented escape hatch; its limit must be honest."""

    def setup_method(self):
        self.api = _load_api("per-tenant")
        self.elb = MagicMock()
        self.api.elbv2 = self.elb

    def _fill_window(self):
        lo = self.api.PER_TENANT_PRIORITY_MIN
        hi = self.api.PER_TENANT_PRIORITY_MAX
        self.elb.describe_rules.return_value = {
            "Rules": [{"RuleArn": f"arn:rule/{p}", "Priority": str(p),
                       "Conditions": []} for p in range(lo, hi + 1)]}

    def test_exhaustion_raises_not_silently_misbehaves(self):
        self._fill_window()
        with pytest.raises(RuntimeError, match="no free ALB listener rule"):
            self.api._add_alb_rule("t1", "arn:tg/x")
        self.elb.create_rule.assert_not_called()

    def test_error_names_the_real_constraint_and_the_way_out(self):
        """The 1-499 window was never the binding limit — the quota is."""
        self._fill_window()
        with pytest.raises(RuntimeError) as ei:
            self.api._add_alb_rule("t1", "arn:tg/x")
        msg = str(ei.value)
        assert "quota" in msg.lower(), (
            f"error must name the ALB rules quota (default 100), not just the "
            f"priority window: {msg}")
        assert "host-tg" in msg, (
            f"error must point at the fix — an operator hitting this needs to "
            f"know per-tenant is the wrong mode at this scale: {msg}")


class TestStaticPrioritiesAreReserved:
    """A tenant rule must never be able to occupy a static rule's priority."""

    def setup_method(self):
        self.api = _load_api("per-tenant")
        self.elb = MagicMock()
        self.elb.describe_rules.return_value = {"Rules": []}
        self.api.elbv2 = self.elb

    def test_window_floor_leaves_room_for_static_rules(self):
        assert self.api.PER_TENANT_PRIORITY_MIN > 1, (
            "priority 1 must not be allocatable to a tenant: a live tenant "
            "holding it makes `cdk deploy` fail with PriorityInUseException if "
            "a static rule ever wants that slot, and lets a tenant rule shadow "
            "a static one")

    def test_allocated_priority_never_below_the_floor(self):
        floor = self.api.PER_TENANT_PRIORITY_MIN
        for _ in range(200):
            self.elb.create_rule.reset_mock()
            self.api._add_alb_rule("t1", "arn:tg/x")
            got = int(self.elb.create_rule.call_args.kwargs["Priority"])
            assert floor <= got <= self.api.PER_TENANT_PRIORITY_MAX, (
                f"allocated priority {got} outside the reserved window")

    def test_catch_all_priority_is_outside_the_tenant_window(self):
        """999 (the catch-all) must not be allocatable either."""
        assert not (self.api.PER_TENANT_PRIORITY_MIN <= 999
                    <= self.api.PER_TENANT_PRIORITY_MAX), (
            "the /vm/* catch-all's priority is inside the per-tenant window — a "
            "tenant create could take it and silently break shared routing")


class TestRoutingStatusReportsHeadroom:
    """"How many more tenants can this take?" must be answerable without code."""

    def _status(self, mode, rules):
        api = _load_api(mode)
        elb = MagicMock()
        elb.describe_rules.return_value = {"Rules": rules}
        api.elbv2 = elb
        resp = api.routing_status()
        return resp["statusCode"], json.loads(resp["body"])

    _CATCH_ALL = {"RuleArn": "a", "Priority": "999",
                  "Conditions": [{"Field": "path-pattern", "Values": ["/vm/*"]}]}

    @staticmethod
    def _tenant_rule(p, tid):
        return {"RuleArn": f"a{p}", "Priority": str(p),
                "Conditions": [{"Field": "path-pattern",
                                "Values": [f"/vm/{tid}", f"/vm/{tid}/*"]}]}

    def test_per_tenant_ceiling_is_quota_headroom_not_499(self):
        rules = [self._tenant_rule(p, f"t{p}") for p in range(10, 40)]
        code, body = self._status("per-tenant", rules)
        assert code == 200
        assert body["per_tenant_rules"] == 30
        assert body["tenant_ceiling"] == body["quota_headroom"] == 70, (
            f"ceiling should be quota(100) - rules(30), not the 499 window: {body}")

    def test_host_tg_ceiling_is_host_capacity(self):
        code, body = self._status("host-tg", [self._CATCH_ALL])
        assert "hosts" in str(body["tenant_ceiling"]), body
        assert body["has_shared_catch_all"] is True
        assert body["cutover_complete"] is True

    def test_host_tg_flags_remaining_legacy_rules(self):
        """Legacy rules still WIN over the catch-all — that must be surfaced."""
        rules = [self._CATCH_ALL, self._tenant_rule(11, "old")]
        code, body = self._status("host-tg", rules)
        assert body["cutover_complete"] is False
        assert body["per_tenant_rules"] == 1
        assert "purge" in body["note"], (
            "operators need to be told how to finish the cutover, not just "
            f"that it is incomplete: {body}")

    def test_catch_all_is_not_counted_as_a_tenant(self):
        code, body = self._status("host-tg", [self._CATCH_ALL])
        assert body["per_tenant_rules"] == 0
        assert body["static_rules"] == 1
