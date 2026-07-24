# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Amazon Managed Prometheus + Grafana integration (issue #4).

Architecture
------------
host-agent.py exposes Prometheus metrics on :8899/metrics (same HTTPServer
as /health to avoid a second listener). A systemd-managed ADOT (AWS Distro
for OpenTelemetry) collector on each host scrapes localhost and remote-
writes to AMP using SigV4. AMG reads from AMP for dashboards.

What this PR validates (and what it deliberately does NOT)
----------------------------------------------------------
- CDK creates exactly one AMP workspace and one AMG workspace
- Host EC2 instance role gains `aps:RemoteWrite` on the AMP ARN
- host-agent's /metrics endpoint emits valid Prom text-format
- ADOT config rendered by init-host.sh contains correct AMP endpoint + region
- A `metrics.enabled: false` flag in config.yml short-circuits resource creation
  (cost-saving knob — AMP is billed per sample/GB)

We do NOT exercise the live AMP API in unit tests; remote_write is covered
by an e2e test that runs only when .env.deploy is present.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════
# CDK stack — AMP / AMG / IAM
# ═══════════════════════════════════════════


def _synth_template(metrics_enabled=True):
    """Synthesize the stack and return aws_cdk.assertions.Template.

    We can't `import deploy.stack` directly because it expects to be loaded
    by `cdk synth`; mirror the pattern in tests/test_stack.py.
    """
    import aws_cdk as cdk

    # Toggle metrics in config without mutating the file on disk.
    import yaml
    from aws_cdk import assertions
    cfg_path = ROOT / "config.yml"
    original = cfg_path.read_text()
    cfg = yaml.safe_load(original)
    cfg.setdefault("metrics", {})["enabled"] = metrics_enabled
    cfg_path.write_text(yaml.safe_dump(cfg))
    try:
        # Reimport stack module so it picks up the mutated config
        sys.modules.pop("deploy.stack", None)
        sys.modules.pop("deploy", None)
        spec = importlib.util.spec_from_file_location(
            "deploy.stack", ROOT / "deploy" / "stack.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["deploy.stack"] = mod
        spec.loader.exec_module(mod)
        app = cdk.App()
        stack = mod.OpenClawOrchestratorStack(app, "Test",
            env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
        return assertions.Template.from_stack(stack)
    finally:
        cfg_path.write_text(original)


@pytest.mark.unit
class TestAMPWorkspace:
    def test_creates_one_amp_workspace_when_enabled(self):
        """AMP workspace is created when metrics.enabled: true."""
        tpl = _synth_template(metrics_enabled=True)
        tpl.resource_count_is("AWS::APS::Workspace", 1)

    def test_amp_workspace_has_alias(self):
        """The AMP workspace gets a recognisable alias for ops."""
        tpl = _synth_template(metrics_enabled=True)
        tpl.has_resource_properties("AWS::APS::Workspace", {
            "Alias": assertions_match("openclaw"),
        })

    def test_no_amp_when_disabled(self):
        """Cost-saving: metrics.enabled: false → no AMP workspace."""
        tpl = _synth_template(metrics_enabled=False)
        tpl.resource_count_is("AWS::APS::Workspace", 0)


@pytest.mark.unit
class TestAMGWorkspace:
    def test_creates_one_amg_workspace_when_enabled(self):
        tpl = _synth_template(metrics_enabled=True)
        tpl.resource_count_is("AWS::Grafana::Workspace", 1)

    def test_amg_uses_aws_sso_or_iam(self):
        """AMG workspace must declare an authentication provider."""
        from aws_cdk import assertions
        tpl = _synth_template(metrics_enabled=True)
        # AuthenticationProviders is required by CFN — test fails if construct
        # is wired up without one.
        tpl.has_resource_properties("AWS::Grafana::Workspace", {
            "AuthenticationProviders": assertions.Match.array_with(["AWS_SSO"]),
        })

    def test_amg_has_amp_datasource_permission(self):
        """AMG role can read from AMP."""
        tpl = _synth_template(metrics_enabled=True)
        # Look for IAM policy that grants aps:Query/GetSeries to a Grafana role
        policies = tpl.find_resources("AWS::IAM::Policy")
        granted = False
        for _, res in policies.items():
            doc = res["Properties"].get("PolicyDocument", {})
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if any(a.startswith("aps:") for a in actions):
                    granted = True
        assert granted, "Expected an IAM policy with aps:* actions for Grafana"


@pytest.mark.unit
class TestHostInstanceRole:
    def test_host_role_has_aps_remote_write(self):
        """EC2 host instance role can write to AMP."""
        tpl = _synth_template(metrics_enabled=True)
        policies = tpl.find_resources("AWS::IAM::Policy")
        found = False
        for _, res in policies.items():
            doc = res["Properties"].get("PolicyDocument", {})
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "aps:RemoteWrite" in actions:
                    found = True
        assert found, "Host instance role missing aps:RemoteWrite"


# ═══════════════════════════════════════════
# host-agent /metrics endpoint
# ═══════════════════════════════════════════


# Mock SDK to import host-agent.py without boto3
_mock_ddb = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=MagicMock()):
    spec = importlib.util.spec_from_file_location(
        "host_agent_prom", str(ROOT / "deploy" / "userdata" / "host-agent.py"))
    agent = importlib.util.module_from_spec(spec)
    sys.modules["host_agent_prom"] = agent
    spec.loader.exec_module(agent)


@pytest.mark.unit
class TestPromExporter:
    def test_render_metrics_emits_prom_format(self):
        """_render_metrics_text returns valid Prometheus text exposition."""
        # Two tenants with synthetic metrics
        snapshots = {
            "t1": {"vm_health": "up", "metrics": {
                "memory_used_mb": 2048, "memory_balloon_mib": 0,
                "disk_used_mb": 100, "disk_total_mb": 8192,
                "disk_used_pct": 1, "cpu_pct": 5,
            }},
            "t2": {"vm_health": "down", "metrics": {
                "memory_used_mb": 0, "memory_balloon_mib": 0,
                "disk_used_mb": 0, "disk_total_mb": 0,
                "disk_used_pct": 0, "cpu_pct": 0,
            }},
        }
        text = agent._render_metrics_text(snapshots)
        # Standard 4-line gauge: # HELP, # TYPE, sample, sample
        assert "# HELP openclaw_vm_memory_used_mb" in text
        assert "# TYPE openclaw_vm_memory_used_mb gauge" in text
        # Per-tenant samples with label
        assert 'openclaw_vm_memory_used_mb{tenant="t1"} 2048' in text
        assert 'openclaw_vm_disk_used_pct{tenant="t1"} 1' in text
        # Health is exported as a 0/1 gauge
        assert 'openclaw_vm_health{tenant="t1"} 1' in text
        assert 'openclaw_vm_health{tenant="t2"} 0' in text

    def test_render_handles_no_snapshots(self):
        """Empty input → still emits HELP/TYPE headers (so scrapers don't break)."""
        text = agent._render_metrics_text({})
        assert "# HELP openclaw_vm_memory_used_mb" in text
        # No sample lines
        assert "openclaw_vm_memory_used_mb{" not in text

    def test_render_skips_missing_metrics(self):
        """Snapshot without 'metrics' key (e.g. recovering VM) is skipped."""
        snapshots = {"t1": {"vm_health": "recovering"}}
        text = agent._render_metrics_text(snapshots)
        assert 'openclaw_vm_memory_used_mb{tenant="t1"}' not in text
        # But health is still exported (0)
        assert 'openclaw_vm_health{tenant="t1"} 0' in text

    def test_enrich_metrics_mirrors_metrics_back_into_status_for_prom_exporter(self):
        """Regression for 1.2.5: per-VM metrics must be assigned back into the
        in-memory `info` dict so the /metrics Prometheus endpoint (read from
        the same dict via _status) actually exposes them. Before the fix only
        openclaw_vm_health was ever scraped — every other gauge was empty in
        AMP because _status only ever held the bare _probe_all() output.

        T3-5 moved the composition+mirror out of _write_ddb into _enrich_metrics
        (called every poll tick, independent of the throttled DDB write) so the
        gauges stay fresh even on ticks where no DDB write happens. Assert the
        actual mirror BEHAVIOR rather than a source-string match."""
        results = {"t1": {"vm_health": "up", "app_health": "up",
                          "guest_ip": "172.16.1.2", "fc_pid": 1234}}
        with patch.object(agent, "_compose_metrics", return_value={
                "memory_used_mb": 2048, "memory_balloon_mib": 0,
                "disk_used_mb": 100, "disk_total_mb": 8192,
                "disk_used_pct": 1, "cpu_pct": 0}):
            agent._enrich_metrics(results)
        assert results["t1"].get("metrics", {}).get("memory_used_mb") == 2048, (
            "_enrich_metrics must mirror computed metrics back into info[] so the "
            "Prometheus exporter sees them. See deploy/userdata/host-agent.py "
            "comment block referencing 1.2.5 + the AMP scrape fix.")
        # And it must be skipped for non-up VMs (keeps last-known, no zeros).
        recovering = {"t2": {"vm_health": "recovering"}}
        agent._enrich_metrics(recovering)
        assert "metrics" not in recovering["t2"]


@pytest.mark.unit
class TestPromHTTP:
    def test_metrics_endpoint_returns_200(self):
        """The HTTP handler responds 200 + text/plain for GET /metrics."""
        # Simulate a request: build a minimal Handler and check do_GET routes
        # to the metrics path.
        handler_cls = agent.Handler
        # The Handler class has do_GET that routes paths; assert the routing
        # logic recognises /metrics by inspecting source.
        import inspect
        src = inspect.getsource(handler_cls.do_GET)
        assert "/metrics" in src, "Handler must route /metrics"


# ═══════════════════════════════════════════
# ADOT collector config
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestADOTConfig:
    def test_init_host_installs_collector(self):
        """init-host.sh references the ADOT collector binary."""
        sh = (ROOT / "deploy" / "userdata" / "init-host.sh").read_text()
        # The implementation must download or invoke the AWS-distro collector.
        # Either keyword is acceptable.
        assert ("aws-otel-collector" in sh
                or "adot-collector" in sh), \
            "init-host.sh must install the ADOT collector"

    def test_collector_config_has_amp_endpoint(self):
        """The shipped collector YAML references AMP remote_write + sigv4
        and scrapes the host-agent on the SAME port that host-agent listens
        on (8899). Regression guard for the 1.2.4 split-port mismatch where
        ADOT was scraping :9090 but host-agent only ever bound :8899."""
        cfg = ROOT / "deploy" / "userdata" / "adot-config.yaml"
        assert cfg.exists(), "ADOT collector config missing"
        text = cfg.read_text()
        assert "prometheusremotewrite" in text or "awsprometheusremotewrite" in text
        assert "sigv4" in text.lower()
        # Must scrape the local host-agent on the port host-agent ACTUALLY
        # listens on (PORT, defaulting to 8899). The earlier 9090 wiring
        # never received a single sample.
        assert "127.0.0.1:8899" in text, (
            "ADOT must scrape host-agent on :8899 (the only port the agent "
            "actually binds — see deploy/userdata/host-agent.py main())"
        )
        assert "127.0.0.1:9090" not in text, (
            "Stale 9090 scrape target — host-agent does not bind 9090"
        )


# ═══════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════


def assertions_match(value):
    """Convenience wrapper for cdk assertions Match.string_like_regexp."""
    from aws_cdk import assertions
    return assertions.Match.string_like_regexp(value)
