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
import os
import subprocess
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

    def test_shared_tg_health_check_is_readiness_not_liveness(self, template):
        """Must probe /ready, never /health.

        /health is a constant 200 from the moment nginx starts — before the
        rootfs download and before host-agent's first peer-map sync. Probing it
        marks a brand-new host healthy while it cannot route any cross-host
        tenant, so it 404s its whole 1/N share of traffic and every ASG
        scale-out becomes a partial outage.
        """
        tgs = template.find_resources("AWS::ElasticLoadBalancingV2::TargetGroup")
        shared = [t for t in tgs.values()
                  if t["Properties"].get("Name") == "openclaw-hosts-shared"]
        assert shared, "shared hosts target group missing"
        assert shared[0]["Properties"]["HealthCheckPath"] == "/ready", (
            "shared TG must health-check /ready (readiness), not /health "
            "(which is always 200)")

    def test_per_tenant_target_groups_still_use_health(self, template):
        """The legacy routing path must not be disturbed by the /ready split."""
        tgs = template.find_resources("AWS::ElasticLoadBalancingV2::TargetGroup")
        for t in tgs.values():
            p = t["Properties"]
            if p.get("Name") != "openclaw-hosts-shared":
                hc = p.get("HealthCheckPath")
                assert hc in (None, "/health"), (
                    f"non-shared TG changed health check path to {hc!r}")

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
        assert "proxy_pass http://10.0.1.5:8081;" in conf
        # WebSocket-critical directives must survive the extra hop.
        assert "proxy_http_version 1.1;" in conf
        assert "proxy_set_header Upgrade $http_upgrade;" in conf
        assert "proxy_set_header Connection $connection_upgrade;" in conf
        assert "proxy_read_timeout 86400s;" in conf

    def test_peer_proxy_pass_must_not_strip_the_vm_prefix(self):
        """Regression guard for the 2-hop 404.

        The peer hop targets ANOTHER HOST'S nginx :8081, whose only locations
        are `^/vm/<tid>(/.*)?$`. A `$1` (or any URI part) on proxy_pass makes
        nginx forward only the suffix, so the owner host sees `/foo` instead of
        `/vm/<tid>/foo` and answers 404 — silently killing every cross-host
        request the moment the catch-all actually receives traffic.

        Do NOT relax this into a substring check: `$1` is exactly what the
        local (guest-facing) block in launch-vm.sh legitimately uses, so the
        two blocks look nearly identical and the wrong one is easy to copy.
        """
        conf = agent._render_peer_conf("acme", "10.0.1.5")
        pp = [ln.strip() for ln in conf.splitlines() if "proxy_pass" in ln]
        assert pp == ["proxy_pass http://10.0.1.5:8081;"], (
            "peer proxy_pass must carry NO URI part so nginx passes the "
            f"original request URI through unchanged; got {pp}")

    def test_peer_conf_semantics_match_owner_side_locations(self):
        """The URI the owner host receives MUST match its tenant location regex.

        Simulates nginx's regex-location + proxy_pass URI rules rather than
        asserting on the generated string, so this test fails on a semantic
        break even if the config text is refactored.
        """
        import re as _re
        tid = "acme"
        conf = agent._render_peer_conf(tid, "10.0.1.5")

        # What the OWNER host's :8081 will match (rendered by launch-vm.sh).
        owner_re = _re.compile(rf"^/vm/{tid}(/.*)?$")

        # Split host[:port] from any URI part. The URI part must NOT be matched
        # with a leading-`/` requirement: the buggy form is `...:8081$1`, whose
        # URI part starts with `$` — an earlier version of this test used
        # `(/\S*)?` and silently passed on the very bug it was meant to catch.
        m = _re.search(
            r"proxy_pass\s+http://(?P<host>[\w.\-]+(?::\d+)?)(?P<uri>[^;\s]*);",
            conf)
        assert m, f"no proxy_pass found in peer conf: {conf}"
        uri_part = m.group("uri")

        for incoming in (f"/vm/{tid}", f"/vm/{tid}/", f"/vm/{tid}/api/x"):
            if uri_part == "":
                forwarded = incoming          # nginx passes the URI unchanged
            else:
                # proxy_pass WITH a URI part sends that URI instead; `$1`
                # expands to the regex capture (everything after /vm/<tid>).
                cap = owner_re.match(incoming).group(1) or ""
                forwarded = uri_part.replace("$1", cap)
            assert owner_re.match(forwarded), (
                f"owner host would 404: {incoming} forwarded as {forwarded!r}, "
                f"which does not match {owner_re.pattern}")


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

    def _run(self, tenants, hosts, local_tids=(), peer_files=(),
             check_rc=0, reload_rc=0):
        """Drive one _sync_tenant_routes tick.

        `local_tids` are tenants this host serves LOCALLY. They are returned
        for conf.d/tenants/ (as `<tid>.conf`) — deliberately NOT for VM_DIR,
        because the whole point of the fix is that a leftover data directory
        must no longer count as "local". A blanket `patch("os.listdir")` that
        answers the same for every directory cannot tell the two apart, so it
        would silently pass either implementation.
        """
        def _scan(table_name, projection, names=None):
            return tenants if table_name == "t" else hosts

        def _listdir(path):
            if path == agent._LOCAL_CONF_DIR:
                return [f"{t}.conf" for t in local_tids]
            if path == agent._PEER_DIR:
                return list(peer_files)
            if path == agent.VM_DIR:
                # Stale data dirs live on forever (stop-vm.sh preserves them);
                # they must NOT influence the peer-map any more.
                return [f"{t}" for t in local_tids] + ["migrated-away-tenant"]
            return []

        def _proc(cmd, **kw):
            rc = check_rc if cmd[:2] == ["nginx", "-t"] else reload_rc
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="boom")

        # Stub only writes under /etc/nginx (not writable in a test run) and let
        # everything else hit the real filesystem, so _mark_ready's marker is
        # observable. A blanket patch of os.makedirs/open made the readiness
        # marker untestable — a broken marker looked identical to a good one.
        real_open, real_makedirs = open, os.makedirs

        def _is_nginx(path):
            return str(path).startswith("/etc/nginx")

        def _open(path, *a, **kw):
            return MagicMock() if _is_nginx(path) else real_open(path, *a, **kw)

        def _makedirs(path, *a, **kw):
            if _is_nginx(path):
                return None
            return real_makedirs(path, *a, **kw)

        with patch.object(agent, "_scan_projected", side_effect=_scan), \
             patch.object(agent, "_get_ddb", return_value=MagicMock()), \
             patch("os.listdir", side_effect=_listdir), \
             patch("os.remove"), \
             patch("os.makedirs", side_effect=_makedirs), \
             patch("builtins.open", side_effect=_open), \
             patch("subprocess.run", side_effect=_proc) as run, \
             patch("time.monotonic", return_value=10_000.0):
            agent._sync_tenant_routes()
            return run

    @staticmethod
    def _reloaded(run):
        """True if an actual `nginx -s reload` was issued."""
        return any(c.args and c.args[0][:2] == ["nginx", "-s"]
                   for c in run.call_args_list)

    def test_writes_peer_conf_for_remote_tenant(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "running"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
        # nginx reload happened (peer set changed from empty).
        assert self._reloaded(run)

    def test_validates_config_before_reloading(self):
        """`nginx -t` must run BEFORE `nginx -s reload`.

        A bad peer conf invalidates the whole nginx config, after which every
        later reload fails too — including the ones launch-vm.sh/stop-vm.sh
        issue, so no new local tenant can start on this host either.
        """
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "running"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
        cmds = [c.args[0][:2] for c in run.call_args_list if c.args]
        assert ["nginx", "-t"] in cmds, "config not validated before reload"
        assert cmds.index(["nginx", "-t"]) < cmds.index(["nginx", "-s"])

    def test_skips_local_tenant_by_host_id(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-self", "status": "running"}],
            hosts=[{"instance_id": "i-self", "private_ip": "10.0.0.1"}])
        # No remote peers → no reload.
        assert not self._reloaded(run)

    def test_skips_tenant_present_locally_even_if_hostid_lags(self):
        # host_id says i-other, but the VM is physically here (migration lag) →
        # must NOT write a peer conf that would shadow the local route.
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "running"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}],
            local_tids=["t1"])
        assert not self._reloaded(run)

    def test_stale_data_dir_does_not_suppress_peer_conf(self):
        """Regression guard: a migrated-away tenant MUST get a peer conf.

        stop-vm.sh keeps /data/firecracker-vms/<tid>/ on purpose ("data volume
        preserved"), and delete_tenant defaults to keep_data=true. Keying
        "is it local?" off that directory meant the source host suppressed the
        tenant's peer conf FOREVER while its local conf was already deleted —
        a permanent 1/N 404 for that tenant, i.e. the intermittent failure that
        looks like "just refresh a few times".

        `_run` returns "migrated-away-tenant" from VM_DIR but NOT from
        conf.d/tenants/, so this fails if the predicate regresses to VM_DIR.
        """
        run = self._run(
            tenants=[{"id": "migrated-away-tenant", "host_id": "i-other",
                      "status": "running"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
        assert self._reloaded(run), (
            "a tenant that merely left a data directory behind must still get "
            "a peer conf — otherwise it 404s on this host permanently")

    def test_skips_non_routable_status(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "creating"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
        assert not self._reloaded(run)

    def test_failover_queued_is_routable(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "failover_queued"}],
            hosts=[{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
        assert self._reloaded(run)

    def test_owner_without_private_ip_skipped(self):
        run = self._run(
            tenants=[{"id": "t1", "host_id": "i-other", "status": "running"}],
            hosts=[{"instance_id": "i-other"}])  # no private_ip yet
        assert not self._reloaded(run)

    def test_interval_gate_blocks_rapid_resync(self):
        agent._last_route_sync = 9_999.0  # 1s ago; interval is 60s
        with patch("time.monotonic", return_value=10_000.0), \
             patch.object(agent, "_scan_projected") as scan:
            agent._sync_tenant_routes()
        scan.assert_not_called()

    def test_no_reload_when_peer_set_unchanged(self):
        t = [{"id": "t1", "host_id": "i-other", "status": "running"}]
        h = [{"instance_id": "i-other", "private_ip": "10.0.2.9"}]
        self._run(t, h)                     # first sync sets _last_peer_hash
        agent._last_route_sync = 0.0        # re-open the interval gate
        run = self._run(t, h)               # identical desired set
        assert not self._reloaded(run)      # hash unchanged → no reload

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


class TestPeerMapFailureIsNotSilent:
    """A failed apply must be retried and must be visible.

    The old code ran reload with stderr → DEVNULL, no `check`, and then set
    `_last_peer_hash = digest` unconditionally. A failed reload was therefore
    recorded as applied; the next tick saw an unchanged desired set, returned
    early, and never reloaded again — the running config diverged from disk
    permanently, with nothing in the logs.
    """

    def setup_method(self):
        agent.TENANTS_TABLE = "t"
        agent.HOSTS_TABLE = "h"
        agent.INSTANCE_ID = "i-self"
        agent._last_route_sync = 0.0
        agent._last_peer_hash = "PREVIOUS"
        agent._peer_reload_failures = 0
        agent._peer_last_success = 0.0

    _TENANTS = [{"id": "t1", "host_id": "i-other", "status": "running"}]
    _HOSTS = [{"instance_id": "i-other", "private_ip": "10.0.2.9"}]

    def _tick(self, **kw):
        return TestSyncTenantRoutes._run(self, self._TENANTS, self._HOSTS, **kw)

    def test_validation_failure_keeps_hash_so_next_tick_retries(self):
        self._tick(check_rc=1)
        assert agent._last_peer_hash == "PREVIOUS", (
            "hash advanced despite `nginx -t` failing — the next tick would "
            "short-circuit and the drift would become permanent")
        assert agent._peer_reload_failures == 1
        assert agent._peer_last_success == 0.0

    def test_reload_failure_keeps_hash_so_next_tick_retries(self):
        self._tick(reload_rc=1)
        assert agent._last_peer_hash == "PREVIOUS"
        assert agent._peer_reload_failures == 1
        assert agent._peer_last_success == 0.0

    def test_success_advances_hash_and_records_success(self):
        self._tick()
        assert agent._last_peer_hash != "PREVIOUS"
        assert agent._peer_reload_failures == 0
        assert agent._peer_last_success > 0

    def test_failed_apply_is_logged(self, capsys):
        self._tick(reload_rc=1)
        out = capsys.readouterr().out
        assert "FAILED" in out, "a failed apply must not be silent"
        assert "boom" in out, "underlying nginx stderr must be surfaced"


class TestReadinessMarker:
    """A host must not advertise readiness before it can route cross-host."""

    def test_marker_written_only_after_a_successful_apply(self, tmp_path):
        agent.TENANTS_TABLE = "t"
        agent.HOSTS_TABLE = "h"
        agent.INSTANCE_ID = "i-self"
        agent._last_peer_hash = "PREVIOUS"
        prev_dir = agent._READY_DIR
        agent._READY_DIR = str(tmp_path / "run")
        marker = tmp_path / "run" / "ready"
        try:
            # nginx -t rejects the config → NOT ready.
            TestSyncTenantRoutes._run(
                self,
                [{"id": "t1", "host_id": "i-other", "status": "running"}],
                [{"instance_id": "i-other", "private_ip": "10.0.2.9"}],
                check_rc=1)
            assert not marker.exists(), (
                "readiness published despite the peer-map failing to apply — "
                "the shared TG would send this host traffic it cannot route")

            agent._last_route_sync = 0.0
            TestSyncTenantRoutes._run(
                self,
                [{"id": "t1", "host_id": "i-other", "status": "running"}],
                [{"instance_id": "i-other", "private_ip": "10.0.2.9"}])
            assert marker.exists(), "readiness not published after a good sync"
            assert marker.read_text().strip() == "ok"
        finally:
            agent._READY_DIR = prev_dir

    def test_marker_failure_does_not_kill_the_poll_loop(self, capsys):
        """Best-effort, but audible."""
        prev_dir = agent._READY_DIR
        agent._READY_DIR = "/proc/definitely-not-writable"
        try:
            agent._mark_ready()          # must not raise
        finally:
            agent._READY_DIR = prev_dir
        assert "readiness" in capsys.readouterr().out.lower()

    def test_nginx_serves_ready_from_a_tmpfs_file_with_503_fallback(self):
        init = (ROOT / "deploy" / "userdata" / "init-host.sh").read_text()
        assert "location = /ready" in init, "no /ready location in the :80 server"
        block = init[init.index("location = /ready"):]
        block = block[:block.index("}") + 1]
        assert "try_files" in block and "=503" in block, (
            "/ready must fall back to 503 when the marker is absent, otherwise "
            f"it is just another constant 200: {block}")
        assert "/run/" in block, "the marker must live on tmpfs so reboots reset it"

    def test_health_is_still_a_plain_200(self):
        """Legacy per-tenant target groups depend on /health being unconditional."""
        init = (ROOT / "deploy" / "userdata" / "init-host.sh").read_text()
        assert "location /health { return 200 'ok'" in init


class TestPeerMapMetrics:
    """`/metrics` must always carry the three peer-map series.

    A host whose peer-map stopped converging still answers the shared target
    group's /health with 200 while 404-ing every cross-host tenant, so these
    are the only signal that cross-host routing is actually alive. They are
    emitted unconditionally so an alert on staleness can't be defeated by the
    series simply vanishing.
    """

    def test_metrics_present_even_when_idle(self):
        text = agent._render_metrics_text({})
        assert "openclaw_peer_map_reload_failures_total 0" in text
        assert "openclaw_peer_map_entries 0" in text
        # Never applied → -1, which is distinguishable from "applied just now".
        assert "openclaw_peer_map_last_success_age_seconds -1" in text

    def test_metrics_reflect_supplied_state(self):
        import time as _t
        text = agent._render_metrics_text({}, peer={
            "reload_failures": 7, "last_success": _t.time() - 120,
            "entries": 42,
        })
        assert "openclaw_peer_map_reload_failures_total 7" in text
        assert "openclaw_peer_map_entries 42" in text
        age = int([ln.split()[-1] for ln in text.splitlines()
                   if ln.startswith("openclaw_peer_map_last_success_age_seconds")][0])
        assert 115 <= age <= 125, f"age should be ~120s, got {age}"

    def test_help_and_type_headers_present(self):
        text = agent._render_metrics_text({})
        for m in ("openclaw_peer_map_reload_failures_total",
                  "openclaw_peer_map_last_success_age_seconds",
                  "openclaw_peer_map_entries"):
            assert f"# HELP {m} " in text
            assert f"# TYPE {m} " in text



# ══════════════════════════════════════════════════════════════════════
# T3-1 Phase 2: ROUTING_MODE gates + tightened reachability
# ══════════════════════════════════════════════════════════════════════


def _load_api():
    mock_ddb = MagicMock()
    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", return_value=MagicMock()):
        mock_ddb.Table.side_effect = lambda name: MagicMock()
        spec = importlib.util.spec_from_file_location(
            "api_t31", str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["api_t31"] = mod
        spec.loader.exec_module(mod)
        return mod


class TestApiRoutingModeGates:
    def test_default_mode_is_per_tenant(self):
        api = _load_api()
        assert api.ROUTING_MODE == "per-tenant"

    def test_host_tg_gates_add_alb_rule(self):
        api = _load_api()
        api.elbv2 = MagicMock()
        with patch.object(api, "ROUTING_MODE", "host-tg"):
            api._add_alb_rule("t1", "arn:tg")
        api.elbv2.describe_rules.assert_not_called()
        api.elbv2.create_rule.assert_not_called()

    def test_host_tg_ensure_host_tg_returns_empty_no_call(self):
        api = _load_api()
        api.elbv2 = MagicMock()
        with patch.object(api, "ROUTING_MODE", "host-tg"):
            assert api._ensure_host_tg("i-1", "10.0.0.1") == ""
        api.elbv2.create_target_group.assert_not_called()

    def test_host_tg_gates_remove_alb_rule(self):
        api = _load_api()
        api.elbv2 = MagicMock()
        with patch.object(api, "ROUTING_MODE", "host-tg"):
            api._remove_alb_rule("t1")
        api.elbv2.describe_rules.assert_not_called()

    def test_host_tg_gates_repoint_to_tg_dead_code(self):
        # The 4th mutator the recon flagged — its modify_rule must be gated too.
        api = _load_api()
        api.elbv2 = MagicMock()
        with patch.object(api, "ROUTING_MODE", "host-tg"):
            api._repoint_alb_rule_to_tg("t1", "arn:tg")
        api.elbv2.modify_rule.assert_not_called()
        api.elbv2.describe_rules.assert_not_called()

    def test_per_tenant_mode_still_creates_rules(self):
        # Regression guard: default mode must NOT be gated.
        api = _load_api()
        api.elbv2 = MagicMock()
        api.elbv2.describe_rules.return_value = {"Rules": []}
        api._get_listener_arn = MagicMock(return_value="arn:listener")
        with patch.object(api, "ROUTING_MODE", "per-tenant"):
            api._add_alb_rule("t1", "arn:tg")
        api.elbv2.create_rule.assert_called()


class TestHealthRepointGate:
    def _load_hc(self):
        with patch("boto3.resource", return_value=MagicMock()), \
             patch("boto3.client", return_value=MagicMock()):
            spec = importlib.util.spec_from_file_location(
                "hc_t31gate", str(ROOT / "deploy" / "lambda" / "health_check" / "handler.py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules["hc_t31gate"] = mod
            spec.loader.exec_module(mod)
            return mod

    def test_host_tg_gates_repoint(self):
        hc = self._load_hc()
        hc.elbv2 = MagicMock()
        hc.ALB_LISTENER_ARN = "arn:listener"
        with patch.object(hc, "ROUTING_MODE", "host-tg"):
            hc._repoint_alb_rule("t1", "i-tgt", "10.0.0.9")
        hc.elbv2.describe_rules.assert_not_called()
        hc.elbv2.create_target_group.assert_not_called()

    def test_reachable_status_allowset(self):
        hc = self._load_hc()
        # 2xx/3xx and auth 401/403 are "alive"; 404 and 5xx are not.
        assert hc._reachable_status(200) is True
        assert hc._reachable_status(302) is True
        assert hc._reachable_status(401) is True
        assert hc._reachable_status(403) is True
        assert hc._reachable_status(404) is False   # the host-tg catch-all miss
        assert hc._reachable_status(500) is False
        assert hc._reachable_status(502) is False


class TestRoutingModeStackFanout:
    def test_all_three_lambdas_get_routing_mode(self):
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
        tmpl = assertions.Template.from_stack(s)
        fns = tmpl.find_resources("AWS::Lambda::Function")
        for name in ("openclaw-api", "openclaw-health-check", "openclaw-failover-worker"):
            fn = next(f for f in fns.values()
                      if f["Properties"].get("FunctionName") == name)
            assert "ROUTING_MODE" in fn["Properties"]["Environment"]["Variables"], \
                f"{name} missing ROUTING_MODE"

    def test_invalid_routing_mode_raises_at_synth(self):
        import yaml
        sys.path.insert(0, str(ROOT / "deploy"))
        if "stack" in sys.modules:
            del sys.modules["stack"]
        import stack as stack_mod
        cfg = yaml.safe_load((ROOT / "config.yml.example").read_text())
        cfg.setdefault("routing", {})["mode"] = "bogus"
        with pytest.raises(ValueError, match="routing.mode"):
            stack_mod._normalize_config(cfg)
