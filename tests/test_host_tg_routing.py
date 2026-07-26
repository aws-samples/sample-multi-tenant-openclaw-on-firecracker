# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3-1 Phase 1: shared host target group + /vm/* catch-all + nginx peer-map.

Phase 1 is the ADDITIVE data plane for replacing per-tenant ALB listener rules
(the ~499-tenant ceiling) with one shared target group + a catch-all rule +
an nginx peer-map on each host. It is behavior-neutral: the catch-all sits at
priority 999 so the Lambda-created per-tenant rules (1-499) still match first.

Two halves:
  * CDK — the shared TG / rule / SG ingress exist with the right shape;
  * host-agent — _sync_tenant_routes renders peer confs correctly and is safe
    (skips local tenants, honors the interval + reload-on-change gates).
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════════════════
# CDK: shared target group + catch-all rule + :8081 ingress
# ══════════════════════════════════════════════════════════════════════


class TestHostTgStack:
    @pytest.fixture(scope="class")
    def template(self):
        import yaml
        sys.path.insert(0, str(ROOT / "deploy"))
        import aws_cdk as cdk
        from aws_cdk import assertions
        if "stack" in sys.modules:
            del sys.modules["stack"]
        import stack as stack_mod
        stack_mod.CFG = yaml.safe_load((ROOT / "config.yml.example").read_text())
        app = cdk.App()
        s = stack_mod.OpenClawOrchestratorStack(
            app, "OpenClawOrchestrator",
            env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
        return assertions.Template.from_stack(s)

    def test_shared_target_group_exists(self, template):
        tgs = template.find_resources("AWS::ElasticLoadBalancingV2::TargetGroup")
        shared = [t for t in tgs.values()
                  if t["Properties"].get("Name") == "openclaw-hosts-shared"]
        assert shared, "shared hosts target group missing"
        p = shared[0]["Properties"]
        assert p["TargetType"] == "instance", "shared TG must be INSTANCE type"
        assert p["Port"] == 80
        assert p["HealthCheckPath"] == "/health"

    def test_catch_all_rule_priority_999(self, template):
        rules = template.find_resources("AWS::ElasticLoadBalancingV2::ListenerRule")
        vm = [r for r in rules.values() if r["Properties"].get("Priority") == 999]
        assert vm, "no /vm/* catch-all rule at priority 999"
        conds = vm[0]["Properties"]["Conditions"]
        # path-pattern /vm/*
        joined = str(conds)
        assert "/vm/*" in joined, f"catch-all rule not on /vm/*: {conds}"

    def test_catch_all_priority_is_high_so_legacy_rules_win(self, template):
        # The whole safety argument: 999 > the Lambda's 1-499 pool, and ALB
        # evaluates lowest-first, so per-tenant rules always match before the
        # catch-all. Assert nothing was created at a LOW static priority.
        rules = template.find_resources("AWS::ElasticLoadBalancingV2::ListenerRule")
        static_priorities = [r["Properties"].get("Priority") for r in rules.values()]
        assert 999 in static_priorities
        assert all(p is None or p >= 999 for p in static_priorities), (
            f"a static listener rule was created below 999 and could shadow "
            f"per-tenant rules: {static_priorities}")

    def test_host_sg_has_8081_self_ingress(self, template):
        # Self-referencing ingress on tcp 8081 (host-fleet-only peer proxy).
        ingress = template.find_resources("AWS::EC2::SecurityGroupIngress")
        got = [i["Properties"] for i in ingress.values()
               if i["Properties"].get("FromPort") == 8081]
        assert got, "no tcp/8081 ingress rule for the host peer-map"
        # Source must be the host SG itself (GroupId == SourceSecurityGroupId
        # both reference HostSG), not a CIDR — never widened to the VPC.
        r = got[0]
        assert "SourceSecurityGroupId" in r, "8081 ingress must be SG-scoped, not CIDR"
        assert "CidrIp" not in r, "8081 ingress must NOT be a CIDR rule"

    def test_asg_attached_to_shared_tg(self, template):
        # attach_to_application_target_group wires the ASG's TargetGroupARNs.
        asgs = template.find_resources("AWS::AutoScaling::AutoScalingGroup")
        assert asgs, "no ASG"
        blob = str(list(asgs.values()))
        assert "TargetGroupARNs" in blob, (
            "ASG not attached to the shared target group (TargetGroupARNs absent)")


# ══════════════════════════════════════════════════════════════════════
# host-agent: _sync_tenant_routes peer-map generator
# ══════════════════════════════════════════════════════════════════════


def _load_agent():
    mock_ddb = MagicMock()
    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", return_value=MagicMock()):
        mock_ddb.Table.side_effect = lambda name: MagicMock()
        spec = importlib.util.spec_from_file_location(
            "host_agent_t31", str(ROOT / "deploy" / "userdata" / "host-agent.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["host_agent_t31"] = mod
        spec.loader.exec_module(mod)
        return mod


agent = _load_agent()


class TestPeerConfRender:
    def test_render_matches_launch_vm_contract(self):
        conf = agent._render_peer_conf("acme", "10.0.1.5")
        # Regex location keyed to the tenant, proxying to the owner's :8081.
        assert "location ~ ^/vm/acme(/.*)?$" in conf
        assert "proxy_pass http://10.0.1.5:8081$1;" in conf
        # WebSocket-critical directives must survive the extra hop.
        assert "proxy_http_version 1.1;" in conf
        assert "proxy_set_header Upgrade $http_upgrade;" in conf
        assert "proxy_set_header Connection $connection_upgrade;" in conf
        assert "proxy_read_timeout 86400s;" in conf


class TestSyncTenantRoutes:
    def setup_method(self):
        import hashlib
        agent.TENANTS_TABLE = "t"
        agent.HOSTS_TABLE = "h"
        agent.INSTANCE_ID = "i-self"
        agent._last_route_sync = 0.0
        # Pre-seed the "empty peer set" hash so the skip-cases below assert the
        # STEADY-STATE behavior (no change → no reload). A first-ever sync with
        # an empty desired set legitimately reloads once to reconcile the dir to
        # empty; that's covered separately by the write/change tests.
        agent._last_peer_hash = hashlib.sha256(b"").hexdigest()

    def _run(self, tenants, hosts, local_tids=(), reload_ok=True):
        def _scan(table_name, projection, names=None):
            return tenants if table_name == "t" else hosts
        with patch.object(agent, "_scan_projected", side_effect=_scan), \
             patch.object(agent, "_get_ddb", return_value=MagicMock()), \
             patch("os.listdir", return_value=list(local_tids)), \
             patch("os.makedirs"), patch("os.remove"), \
             patch("builtins.open", MagicMock()), \
             patch("subprocess.run") as run, \
             patch("time.monotonic", return_value=10_000.0):
            agent._sync_tenant_routes()
            return run

    def test_writes_peer_conf_for_remote_tenant(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "running"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
        # nginx reload happened (peer set changed from empty).
        assert run.called
        assert run.call_args[0][0][:2] == ["nginx", "-s"]

    def test_skips_local_tenant_by_host_id(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-self", "status": "running"}],
            hosts=[{"instance_id": "i-self", "private_ip": "10.0.0.1"}])
        # No remote peers → no reload.
        run.assert_not_called()

    def test_skips_tenant_present_locally_even_if_hostid_lags(self):
        # host_id says i-other, but the VM is physically here (migration lag) →
        # must NOT write a peer conf that would shadow the local route.
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "running"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}],
            local_tids=["t1"])
        run.assert_not_called()

    def test_skips_non_routable_status(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "creating"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
        run.assert_not_called()

    def test_failover_queued_is_routable(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "failover_queued"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
        run.assert_called()

    def test_owner_without_private_ip_skipped(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "running"}],
            hosts=[{"instance_id": "i-other"}])  # no private_ip yet
        run.assert_not_called()

    def test_interval_gate_blocks_rapid_resync(self):
        agent._last_route_sync = 9_999.0  # 1s ago; interval is 60s
        with patch("time.monotonic", return_value=10_000.0), \
             patch.object(agent, "_scan_projected") as scan:
            agent._sync_tenant_routes()
        scan.assert_not_called()

    def test_no_reload_when_peer_set_unchanged(self):
        t = [{"id": "t1", "host_id": "i-other", "status": "running"}]
        h = [{"instance_id": "i-other", "private_ip": "10.0.2.9"}]
        self._run(t, h)                    # first sync sets _last_peer_hash
        agent._last_route_sync = 0.0        # re-open the interval gate
        run = self._run(t, h)               # identical desired set
        run.assert_not_called()             # hash unchanged → no reload

    def test_missing_instance_id_bails(self):
        agent.INSTANCE_ID = ""
        with patch.object(agent, "_scan_projected") as scan:
            agent._sync_tenant_routes()
        scan.assert_not_called()

    def test_never_raises_on_scan_error(self):
        agent._last_route_sync = 0.0
        with patch.object(agent, "_scan_projected", side_effect=RuntimeError("throttled")), \
             patch("time.monotonic", return_value=10_000.0):
            agent._sync_tenant_routes()  # must not raise
