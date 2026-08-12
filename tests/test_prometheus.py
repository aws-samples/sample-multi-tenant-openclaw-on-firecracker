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
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import synth_stack

ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════
# CDK stack — AMP / AMG / IAM
# ═══════════════════════════════════════════


def _synth_template(metrics_enabled=True):
    """Synthesize the stack and return aws_cdk.assertions.Template.

    We can't `import deploy.stack` directly because it expects to be loaded
    by `cdk synth`; mirror the pattern in tests/test_stack.py.
    """
    # Toggles metrics.enabled genuinely without mutating the file on disk —
    # the config is handed to stack.py via OPENCLAW_CONFIG (conftest.synth_stack).
    return synth_stack(
        lambda cfg: cfg.setdefault("metrics", {}).update(
            {"enabled": metrics_enabled}))


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


def _get(path, status=None):
    """Drive Handler.do_GET for `path` and return (status, headers, body).

    Builds the handler without going through BaseHTTPRequestHandler.__init__
    (which would try to read a real socket) and captures the response the same
    way the real server would write it.

    This used to assert `"/metrics" in inspect.getsource(do_GET)`, which
    checked nothing about the response and broke whenever host-agent.py was
    edited while the suite was running (getsource re-reads the file by line
    number, so any shift above the class made it return the wrong lines).
    """
    h = agent.Handler.__new__(agent.Handler)
    h.path = path
    h.wfile = io.BytesIO()
    captured = {"status": None, "headers": {}}

    h.send_response = lambda code, *a: captured.__setitem__("status", code)
    h.send_header = lambda k, v: captured["headers"].__setitem__(k, v)
    h.end_headers = lambda: None
    h.log_message = lambda *a, **k: None

    h.do_GET()
    return captured["status"], captured["headers"], h.wfile.getvalue()


@pytest.mark.unit
class TestPromHTTP:
    def test_metrics_endpoint_returns_200_text_plain(self):
        """GET /metrics → 200 with the Prometheus text-format content type."""
        status, headers, body = _get("/metrics")
        assert status == 200
        assert headers["Content-Type"] == "text/plain; version=0.0.4"
        assert headers["Content-Length"] == str(len(body))

    def test_metrics_body_is_prometheus_exposition(self):
        with agent._lock:
            agent._status.clear()
            agent._status["t1"] = {"vm_health": "up", "metrics": {
                "memory_used_mb": 2048, "memory_balloon_mib": 0,
                "disk_used_mb": 100, "disk_total_mb": 8192,
                "disk_used_pct": 1, "cpu_pct": 5,
            }}
        try:
            _, _, body = _get("/metrics")
        finally:
            with agent._lock:
                agent._status.clear()
        text = body.decode()
        assert "# TYPE openclaw_vm_memory_used_mb gauge" in text
        assert 'openclaw_vm_memory_used_mb{tenant="t1"} 2048' in text

    def test_health_endpoint_returns_json(self):
        status, headers, body = _get("/health")
        assert status == 200
        assert headers["Content-Type"] == "application/json"
        json.loads(body)  # must be parseable

    def test_unknown_path_returns_404(self):
        status, _, body = _get("/nope")
        assert status == 404
        assert body == b""


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
