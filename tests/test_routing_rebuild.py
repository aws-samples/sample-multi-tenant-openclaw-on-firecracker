# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3-1 P3: the host-tg cutover must be reversible.

Why this endpoint exists
------------------------
Switching `routing.mode` to `host-tg` used to be a one-way door:

* `_add_alb_rule` is only reachable from `create_tenant` / `process_pending`,
  so nothing ever re-created rules for tenants that already existed;
* `_ensure_host_tg` returns early under host-tg, so hosts registered while
  host-tg was active have no `oc-<last8>` target group to point a rule at.

Flipping the config back therefore restored the Lambda env var but NOT the ALB
rule set, and the listener's default action is `fixed_response 404` — i.e. every
tenant down. Past ~100 tenants (the AWS *default* rules-per-ALB quota, not the
code's 1-499 range) per-tenant is not even a reachable state without a quota
increase that takes hours to days.

`rebuild_routing` closes that door, and `purge_per_tenant_routing` makes the
cutover incremental: deleting one tenant's rule moves exactly that tenant onto
the shared catch-all and is undone by rebuilding that one rule. The two
together are what make the flip safe to attempt at all, so the properties
asserted below are the ones that matter:

  * rebuild works WHILE ROUTING_MODE == "host-tg" (the gates must be bypassed,
    which is the entire point — a rebuild that respects the gate is a no-op);
  * it is idempotent (existing rules are reported, not duplicated);
  * dry-run is the DEFAULT (fleet-wide live-data-plane mutation);
  * a single bad tenant row cannot abort the run and hide the rest;
  * purge never touches the static `/vm/*` catch-all;
  * purge can be scoped to specific tenants (the canary).
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.unit


def _load_api(routing_mode="host-tg"):
    """Load the api handler with ROUTING_MODE forced (module-level constant)."""
    mock_ddb = MagicMock()
    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", return_value=MagicMock()), \
         patch.dict("os.environ", {
             "ROUTING_MODE": routing_mode,
             "ALB_LISTENER_ARN": "arn:aws:elasticloadbalancing:x:1:listener/app/a/b/c",
             "VPC_ID": "vpc-test",
         }):
        mock_ddb.Table.side_effect = lambda name: MagicMock()
        spec = importlib.util.spec_from_file_location(
            f"api_rebuild_{routing_mode}",
            str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod


api = _load_api("host-tg")


def _tenant(tid, host_id="i-host1", status="running"):
    return {"id": tid, "host_id": host_id, "status": status}


def _rule(priority, values):
    return {
        "RuleArn": f"arn:rule/{priority}",
        "Priority": str(priority),
        "Conditions": [{"Field": "path-pattern", "Values": values}],
    }


class _Harness:
    """Wire the module's ELB + DynamoDB doubles for one call."""

    def __init__(self, tenants, rules, host_ip="10.0.1.5"):
        self.created_tgs = []
        self.created_rules = []
        self.deleted_rules = []
        api.tenants_table = MagicMock()
        api.tenants_table.scan.return_value = {"Items": tenants}
        api.hosts_table = MagicMock()
        api.hosts_table.get_item.return_value = (
            {"Item": {"instance_id": "i-host1", "private_ip": host_ip}}
            if host_ip else {})
        elb = MagicMock()
        elb.describe_rules.return_value = {"Rules": rules}
        elb.describe_target_groups.side_effect = Exception("not found")

        def _create_tg(**kw):
            self.created_tgs.append(kw["Name"])
            return {"TargetGroups": [{"TargetGroupArn": f"arn:tg/{kw['Name']}"}]}

        def _create_rule(**kw):
            self.created_rules.append(kw)
            return {}

        def _delete_rule(**kw):
            self.deleted_rules.append(kw["RuleArn"])
            return {}

        elb.create_target_group.side_effect = _create_tg
        elb.create_rule.side_effect = _create_rule
        elb.delete_rule.side_effect = _delete_rule
        api.elbv2 = elb
        self.elb = elb

    def rebuild(self, **body):
        with patch.object(api, "_audit_system"):
            resp = api.rebuild_routing(json.dumps(body) if body else None)
        return resp["statusCode"], json.loads(resp["body"])

    def purge(self, **body):
        with patch.object(api, "_audit_system"):
            resp = api.purge_per_tenant_routing(json.dumps(body) if body else None)
        return resp["statusCode"], json.loads(resp["body"])


class TestRebuildWorksUnderHostTg:
    """The gates must be bypassed — otherwise the rebuild is a no-op."""

    def test_creates_rules_while_routing_mode_is_host_tg(self):
        h = _Harness([_tenant("t1"), _tenant("t2")], rules=[])
        code, body = h.rebuild(dry_run=False)
        assert code == 200
        assert body["routing_mode_env"] == "host-tg", "fixture did not force host-tg"
        assert body["counts"].get("created") == 2, body
        assert len(h.created_rules) == 2, (
            "no listener rules created — the host-tg gate was respected, which "
            "makes rollback impossible (that gate is what this endpoint exists "
            "to bypass)")

    def test_creates_the_missing_per_host_target_group(self):
        """Hosts registered under host-tg have no oc-<last8> TG at all."""
        h = _Harness([_tenant("t1")], rules=[])
        code, body = h.rebuild(dry_run=False)
        assert code == 200
        # Name must match _ensure_host_tg's scheme exactly, or a later
        # describe_target_groups lookup misses and duplicates the group.
        assert h.created_tgs == ["oc-" + "i-host1"[-8:]], (
            f"expected exactly one oc-<last8> target group, got {h.created_tgs}")
        assert h.elb.register_targets.called, (
            "target group created but no targets registered — a rule pointing "
            "at an empty TG still 404s")

    def test_target_group_is_reused_across_tenants_on_one_host(self):
        h = _Harness([_tenant("t1"), _tenant("t2"), _tenant("t3")], rules=[])
        h.rebuild(dry_run=False)
        assert len(h.created_tgs) == 1, (
            f"created {len(h.created_tgs)} target groups for one host — "
            "wasteful and risks hitting the TG quota")


class TestRebuildIsIdempotent:
    def test_existing_rules_are_reported_not_duplicated(self):
        h = _Harness([_tenant("t1"), _tenant("t2")],
                     rules=[_rule(5, ["/vm/t1", "/vm/t1/*"])])
        code, body = h.rebuild(dry_run=False)
        assert code == 200
        assert body["counts"].get("existing") == 1, body
        assert body["counts"].get("created") == 1, body
        assert len(h.created_rules) == 1, "re-created a rule that already existed"

    def test_catch_all_rule_is_not_mistaken_for_a_tenant(self):
        """`/vm/*` is the static catch-all, not tenant `*`."""
        h = _Harness([_tenant("t1")], rules=[_rule(999, ["/vm/*"])])
        code, body = h.rebuild(dry_run=False)
        assert body["counts"].get("created") == 1, (
            f"the catch-all was read as an existing tenant rule: {body}")


class TestDryRunIsTheDefault:
    def test_no_body_means_dry_run(self):
        h = _Harness([_tenant("t1")], rules=[])
        code, body = h.rebuild()
        assert body["dry_run"] is True, (
            "a fleet-wide mutation of the live data plane must not run by "
            "default — an operator has to see the plan first")
        assert h.created_rules == [] and h.created_tgs == []

    def test_dry_run_reports_what_would_change(self):
        h = _Harness([_tenant("t1"), _tenant("t2")],
                     rules=[_rule(5, ["/vm/t1", "/vm/t1/*"])])
        code, body = h.rebuild(dry_run=True)
        assert body["counts"] == {"existing": 1, "would_create": 1}, body

    def test_non_boolean_dry_run_is_rejected(self):
        """`dry_run: "false"` must not be read as truthy-then-ignored."""
        h = _Harness([_tenant("t1")], rules=[])
        code, body = h.rebuild(dry_run="false")
        assert code == 400, body

    def test_purge_defaults_to_dry_run_too(self):
        h = _Harness([], rules=[_rule(5, ["/vm/t1", "/vm/t1/*"])])
        code, body = h.purge()
        assert body["dry_run"] is True
        assert h.deleted_rules == []


class TestPartialFailureIsReported:
    def test_one_bad_row_does_not_abort_the_run(self):
        h = _Harness([_tenant("t1"), _tenant("t2", host_id=None), _tenant("t3")],
                     rules=[])
        code, body = h.rebuild(dry_run=False)
        assert code == 200
        assert body["counts"].get("created") == 2, body
        assert body["counts"].get("skipped") == 1, body
        skipped = [r for r in body["results"] if r["action"] == "skipped"]
        assert skipped[0]["tenant_id"] == "t2"
        assert "reason" in skipped[0], "a skip with no reason is unactionable"

    def test_host_without_private_ip_is_a_reported_failure(self):
        h = _Harness([_tenant("t1")], rules=[], host_ip=None)
        code, body = h.rebuild(dry_run=False)
        assert body["counts"].get("failed") == 1, body
        assert "private_ip" in body["results"][0]["reason"]

    def test_per_tenant_rule_failure_is_recorded(self):
        h = _Harness([_tenant("t1"), _tenant("t2")], rules=[])
        h.elb.create_rule.side_effect = RuntimeError("PriorityInUse")
        code, body = h.rebuild(dry_run=False)
        assert code == 200, "a per-tenant failure must not 5xx the whole rebuild"
        assert body["counts"].get("failed") == 2, body


class TestRebuildScope:
    def test_only_routable_statuses_are_queried(self):
        """A stopped tenant needs no route; a failing-over one still does."""
        h = _Harness([], rules=[])
        h.rebuild(dry_run=True)
        kwargs = api.tenants_table.scan.call_args.kwargs
        vals = kwargs["ExpressionAttributeValues"]
        assert set(vals.values()) == {
            "running", "failover_queued", "failover_recovering"}, vals

    def test_unsupported_mode_is_rejected(self):
        h = _Harness([], rules=[])
        code, body = h.rebuild(mode="host-tg")
        assert code == 400
        assert "per-tenant" in body["error"]


class TestPurgeIsSafe:
    def test_never_deletes_the_static_catch_all(self):
        h = _Harness([], rules=[_rule(999, ["/vm/*"]),
                                _rule(5, ["/vm/t1", "/vm/t1/*"])])
        code, body = h.purge(dry_run=False)
        assert h.deleted_rules == ["arn:rule/5"], (
            "purge must leave the /vm/* catch-all alone — it is the only thing "
            f"serving traffic after the cutover; deleted {h.deleted_rules}")

    def test_can_be_scoped_to_a_single_tenant_canary(self):
        h = _Harness([], rules=[_rule(5, ["/vm/t1", "/vm/t1/*"]),
                                _rule(6, ["/vm/t2", "/vm/t2/*"])])
        code, body = h.purge(dry_run=False, tenant_ids=["t2"])
        assert h.deleted_rules == ["arn:rule/6"], (
            f"scoped purge deleted the wrong rules: {h.deleted_rules}")

    def test_default_action_rule_is_ignored(self):
        h = _Harness([], rules=[{"RuleArn": "arn:rule/default",
                                 "Priority": "default", "Conditions": []}])
        code, body = h.purge(dry_run=False)
        assert h.deleted_rules == []

    def test_tenant_ids_must_be_a_list(self):
        h = _Harness([], rules=[])
        code, body = h.purge(tenant_ids="t1")
        assert code == 400, body


class TestRoundTrip:
    """purge → rebuild must restore the rules purge removed."""

    def test_purge_then_rebuild_restores_the_same_tenants(self):
        rules = [_rule(999, ["/vm/*"]),
                 _rule(5, ["/vm/t1", "/vm/t1/*"]),
                 _rule(6, ["/vm/t2", "/vm/t2/*"])]
        h = _Harness([_tenant("t1"), _tenant("t2")], rules=rules)
        h.purge(dry_run=False)
        assert set(h.deleted_rules) == {"arn:rule/5", "arn:rule/6"}

        # Rules are gone; only the catch-all remains.
        h.elb.describe_rules.return_value = {"Rules": [_rule(999, ["/vm/*"])]}
        code, body = h.rebuild(dry_run=False)
        assert body["counts"].get("created") == 2, body
        restored = {c["Conditions"][0]["Values"][0] for c in h.created_rules}
        assert restored == {"/vm/t1", "/vm/t2"}, (
            f"rebuild did not restore the purged tenants: {restored}")


class TestGatesStillHoldForNormalFlow:
    """force= must be opt-in: ordinary creates must not resurrect per-tenant rules."""

    def test_ensure_host_tg_still_gated_without_force(self):
        assert api._ensure_host_tg("i-x", "10.0.0.1") == "", (
            "host-tg mode must still skip per-host target groups on the normal "
            "create path")

    def test_add_alb_rule_still_gated_without_force(self):
        h = _Harness([], rules=[])
        api._add_alb_rule("t9", "arn:tg/x")
        assert h.created_rules == [], (
            "host-tg mode must still skip per-tenant rules on the normal path")

    def test_admin_routes_require_admin_role(self):
        assert ("POST", "/admin/routing/rebuild") in api._ADMIN_ONLY
        assert ("POST", "/admin/routing/purge-per-tenant") in api._ADMIN_ONLY
