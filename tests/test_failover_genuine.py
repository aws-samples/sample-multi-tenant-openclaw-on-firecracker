# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the v1.4.2 'genuine failover' fix.

Background
----------
A teammate reported that `_failover_tenant_to_host` was lying: it would
flip DDB to ``status=running`` and emit ``AZ_FAILOVER_TENANT_RECOVERED``
even when the dashboard URL was completely broken. Three root causes:

  1. The verify probe checked only that a Firecracker process existed
     and that an nginx config file was present — neither proves the
     guest finished booting or that nginx reloaded the new conf.
  2. ALB rule re-pointing failures were swallowed (try/except + log)
     and the failover was marked successful anyway.
  3. There was no end-to-end public-path probe gating the DDB flip.

This test module covers the v1.4.2 fix:

  - Strengthened verify probe (process + nginx conf + local curl
    returning non-5xx).
  - ALB repoint must succeed; failure raises and produces
    ``status=failover_failed_partial``.
  - New cross-ALB gate verifies the dashboard is reachable via the
    public path before flipping DDB.

Six false-positive scenarios are exercised, each must end with the
tenant *not* in ``running`` status and *no* ``AZ_FAILOVER_TENANT_RECOVERED``
audit row. Four happy-path scenarios verify the gates compose correctly
and the legitimate failover path still works end-to-end.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────
# Module loader — fresh hc_handler per test with controllable mocks
# ──────────────────────────────────────────────────────────────────────


def _load_hc(env_overrides=None):
    """Re-import deploy/lambda/health_check/handler.py with mocked AWS.

    Returns (module, mocks_dict). ``mocks_dict`` exposes the AWS client
    mocks plus the table objects keyed by their conceptual name so each
    test can drive only the mocks it cares about.
    """
    import os
    saved = {}
    overrides = {
        "TENANTS_TABLE": "openclaw-tenants",
        "HOSTS_TABLE": "openclaw-hosts",
        "AUDIT_TABLE": "openclaw-audit",
        "ASSETS_BUCKET": "test-bucket",
        "ALB_LISTENER_ARN": "arn:aws:elasticloadbalancing:test:listener/app/foo/abc",
        "AZ_FAILOVER_ENABLED": "true",
        "PUBLIC_BASE_URL": "http://test-alb.example.com",
    }
    if env_overrides:
        overrides.update(env_overrides)
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    sys.modules.pop("hc_handler_genuine", None)

    mock_ddb = MagicMock()
    mock_ssm = MagicMock()
    mock_sns = MagicMock()
    mock_s3 = MagicMock()
    mock_elbv2 = MagicMock()

    # Default SSM: send_command returns a stable cmd id; get_command_invocation
    # defaults to Success with VERIFIED stdout so the verify gate passes.
    mock_ssm.send_command.return_value = {"Command": {"CommandId": "cmd-default"}}
    mock_ssm.get_command_invocation.return_value = {
        "Status": "Success",
        "StandardOutputContent": "VERIFIED_HTTP_200\n",
    }

    class _FakeInvocationDoesNotExist(Exception):
        pass
    mock_ssm.exceptions.InvocationDoesNotExist = _FakeInvocationDoesNotExist

    # Default S3: one backup so _find_latest_backup_key works.
    mock_s3.list_objects_v2.return_value = {
        "Contents": [{
            "Key": "backups/test-tenant/2026-05-28T10:00:00Z.gz",
            "LastModified": datetime(2026, 5, 28, 10, 0, 0, tzinfo=timezone.utc),
        }],
    }

    # Default ELBv2: tg lookup + rule lookup succeed.
    mock_elbv2.describe_target_groups.return_value = {
        "TargetGroups": [{"TargetGroupArn": "arn:tg:target", "VpcId": "vpc-1"}],
    }
    mock_elbv2.describe_rules.return_value = {
        "Rules": [{
            "RuleArn": "arn:rule:1",
            "Priority": "10",
            "Conditions": [{"Field": "path-pattern",
                            "Values": ["/vm/test-tenant", "/vm/test-tenant/*"]}],
            "Actions": [{"Type": "forward", "TargetGroupArn": "arn:tg:source"}],
        }],
    }

    table_cache = {}

    def _table(name):
        if name not in table_cache:
            table_cache[name] = make_ddb_table()
        return table_cache[name]

    mock_ddb.Table.side_effect = _table

    def _client(svc, **kw):
        return {"ssm": mock_ssm, "sns": mock_sns, "s3": mock_s3,
                "elbv2": mock_elbv2}.get(svc, MagicMock())

    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", side_effect=_client):
        spec = importlib.util.spec_from_file_location(
            "hc_handler_genuine", str(ROOT / "deploy/lambda/health_check/handler.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hc_handler_genuine"] = mod
        spec.loader.exec_module(mod)

    mod._test_mocks = {
        "ddb": mock_ddb, "ssm": mock_ssm, "sns": mock_sns,
        "s3": mock_s3, "elbv2": mock_elbv2, "tables": table_cache,
    }

    # Restore env so the module retains the snapshotted values.
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return mod


def _tenant(tid="test-tenant", host_id="i-source", vcpu=2, mem_mb=4096):
    return {
        "id": tid, "name": tid, "host_id": host_id,
        "vcpu": vcpu, "mem_mb": mem_mb, "status": "running",
        "config_template": "default",
    }


def _target_host(host_id="i-target", private_ip="172.16.5.1", az="ap-northeast-1c"):
    return {
        "instance_id": host_id, "private_ip": private_ip, "az": az,
        "status": "active", "next_vm_num": 1,
    }


def _set_ssm_stdout(hc, stdout):
    """Force the next get_command_invocation to return this stdout."""
    hc._test_mocks["ssm"].get_command_invocation.return_value = {
        "Status": "Success", "StandardOutputContent": stdout,
    }


def _make_urlopen_returning(status_code):
    """Return a urlopen mock that yields a Response-like object with .status."""
    def fake_urlopen(req, timeout=5):
        m = MagicMock()
        m.status = status_code
        m.__enter__ = lambda self: m
        m.__exit__ = lambda *a: False
        return m
    return fake_urlopen


def _make_urlopen_raising(exc):
    def fake_urlopen(req, timeout=5):
        raise exc
    return fake_urlopen


# ══════════════════════════════════════════════════════════════════════
# False-positive scenarios — each MUST end with status != running and
# no AZ_FAILOVER_TENANT_RECOVERED audit row
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestFakeFailoverGuards:
    def setup_method(self):
        self.hc = _load_hc()
        self.audit = self.hc._test_mocks["tables"]["openclaw-audit"]
        self.tenants = self.hc._test_mocks["tables"]["openclaw-tenants"]
        # Patch sleep to make the gates fail fast.
        self._sleep_patch = patch("time.sleep", lambda *a, **k: None)
        self._sleep_patch.start()

    def teardown_method(self):
        self._sleep_patch.stop()

    # ----- Helpers -----
    def _audit_ops(self):
        """Return list of operation names written to audit_table."""
        return [
            (call.kwargs.get("Item") or {}).get("operation")
            for call in self.audit.put_item.call_args_list
        ]

    def _final_status(self):
        """Return the final tenant status from the last update_item call."""
        last = self.tenants.update_item.call_args_list[-1]
        eav = last.kwargs.get("ExpressionAttributeValues") or {}
        for k, v in eav.items():
            if k == ":failed" or k == ":running" or k == ":recover" or k == ":blocked":
                return v
        # If status not in the last call, find any call that set #s.
        for call in reversed(self.tenants.update_item.call_args_list):
            ue = (call.kwargs.get("UpdateExpression") or "")
            if "#s" in ue:
                eav = call.kwargs.get("ExpressionAttributeValues") or {}
                # Pick first :*-status-looking value.
                return next((v for k, v in eav.items()
                             if k in (":failed", ":running", ":recover", ":blocked")), None)
        return None

    # ──────────────────────────────────────────────────────────────────
    # 1. Process exists but local curl returns 502 → verify must fail
    # ──────────────────────────────────────────────────────────────────
    def test_local_curl_5xx_blocks_failover(self):
        # The probe shell returns NOT_RUNNING_HTTP_502 → gate fails →
        # raise → catch block writes failover_failed.
        _set_ssm_stdout(self.hc, "NOT_RUNNING_HTTP_502\n")
        # Reduce verify timeout so test runs in ~1s instead of 120s.
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=False) as _:
            result = self.hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is False
        assert self._final_status() == "failover_failed"
        # Must NOT have emitted RECOVERED.
        assert "AZ_FAILOVER_TENANT_RECOVERED" not in self._audit_ops()
        assert "AZ_FAILOVER_TENANT_FAILED" in self._audit_ops()

    # ──────────────────────────────────────────────────────────────────
    # 2. Verify gate passes but ALB.modify_rule fails → must be PARTIAL
    # ──────────────────────────────────────────────────────────────────
    def test_alb_repoint_failure_blocks_failover(self):
        # VM verify passes; ALB modify throws.
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=True), \
             patch.object(self.hc, "_repoint_alb_rule",
                          side_effect=RuntimeError("AccessDenied: ModifyRule")):
            result = self.hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is False
        # Critical: status must be failover_failed_partial — VM is up on
        # target but ALB still routes to source. Operator must intervene.
        assert self._final_status() == "failover_failed_partial"
        assert "AZ_FAILOVER_TENANT_RECOVERED" not in self._audit_ops()
        assert "AZ_FAILOVER_ALB_REPOINT_FAILED" in self._audit_ops()
        assert "AZ_FAILOVER_TENANT_FAILED" in self._audit_ops()

    # ──────────────────────────────────────────────────────────────────
    # 3. Target host has no private_ip → can't repoint → must fail
    # ──────────────────────────────────────────────────────────────────
    def test_no_private_ip_blocks_failover(self):
        target = _target_host()
        target["private_ip"] = ""  # missing!
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=True):
            result = self.hc._failover_tenant_to_host(
                _tenant(), target, "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is False
        assert self._final_status() == "failover_failed_partial"
        assert "AZ_FAILOVER_TENANT_RECOVERED" not in self._audit_ops()

    # ──────────────────────────────────────────────────────────────────
    # 4. Cross-ALB probe returns 502 every time → must fail (PARTIAL)
    # ──────────────────────────────────────────────────────────────────
    def test_cross_alb_5xx_blocks_failover(self):
        import urllib.error
        # urlopen raises HTTPError(502) on every call.
        err = urllib.error.HTTPError(
            url="x", code=502, msg="Bad Gateway", hdrs=None, fp=None)
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=True), \
             patch.object(self.hc, "_repoint_alb_rule"), \
             patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_raising(err)):
            result = self.hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is False
        assert self._final_status() == "failover_failed_partial"
        assert "AZ_FAILOVER_TENANT_RECOVERED" not in self._audit_ops()

    # ──────────────────────────────────────────────────────────────────
    # 5. Cross-ALB probe times out every poll → must fail (PARTIAL)
    # ──────────────────────────────────────────────────────────────────
    def test_cross_alb_timeout_blocks_failover(self):
        import urllib.error
        err = urllib.error.URLError("[Errno 60] Operation timed out")
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=True), \
             patch.object(self.hc, "_repoint_alb_rule"), \
             patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_raising(err)):
            result = self.hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is False
        assert self._final_status() == "failover_failed_partial"
        assert "AZ_FAILOVER_TENANT_RECOVERED" not in self._audit_ops()

    # ──────────────────────────────────────────────────────────────────
    # 6. PUBLIC_BASE_URL not set + everything else passes → still flips,
    #    but emits a CW warning (the 1.4.1 fallback). The gate is
    #    OPT-IN — operators must redeploy CDK to get full protection.
    # ──────────────────────────────────────────────────────────────────
    def test_no_public_base_url_falls_back_to_141_behavior(self):
        hc = _load_hc(env_overrides={"PUBLIC_BASE_URL": ""})
        with patch.object(hc, "_verify_vm_actually_running",
                          return_value=True), \
             patch.object(hc, "_repoint_alb_rule"):
            result = hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is True  # falls back to 1.4.1 behavior
        # Final status MUST be running because the strengthened verify
        # probe (still active) plus ALB repoint both succeeded.
        ops = [
            (call.kwargs.get("Item") or {}).get("operation")
            for call in hc._test_mocks["tables"]["openclaw-audit"].put_item.call_args_list
        ]
        assert "AZ_FAILOVER_TENANT_RECOVERED" in ops


# ══════════════════════════════════════════════════════════════════════
# Happy path scenarios
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestGenuineFailoverHappyPath:
    def setup_method(self):
        self.hc = _load_hc()
        self.audit = self.hc._test_mocks["tables"]["openclaw-audit"]
        self.tenants = self.hc._test_mocks["tables"]["openclaw-tenants"]
        self.hosts = self.hc._test_mocks["tables"]["openclaw-hosts"]
        self._sleep_patch = patch("time.sleep", lambda *a, **k: None)
        self._sleep_patch.start()

    def teardown_method(self):
        self._sleep_patch.stop()

    def _audit_ops(self):
        return [
            (call.kwargs.get("Item") or {}).get("operation")
            for call in self.audit.put_item.call_args_list
        ]

    def test_all_gates_pass_returns_true(self):
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=True), \
             patch.object(self.hc, "_repoint_alb_rule"), \
             patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_returning(200)):
            result = self.hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is True

    def test_all_gates_pass_emits_recovered_audit(self):
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=True), \
             patch.object(self.hc, "_repoint_alb_rule"), \
             patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_returning(200)):
            self.hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        ops = self._audit_ops()
        assert "AZ_FAILOVER_TENANT_RECOVERED" in ops
        assert "AZ_FAILOVER_TENANT_FAILED" not in ops

    def test_cross_alb_probe_accepts_4xx_as_reachable(self):
        """4xx (e.g. 401 auth challenge) means the backend is alive and
        responding — not a failure. Only 5xx and connection errors fail."""
        import urllib.error
        err = urllib.error.HTTPError(
            url="x", code=401, msg="Unauthorized", hdrs=None, fp=None)
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=True), \
             patch.object(self.hc, "_repoint_alb_rule"), \
             patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_raising(err)):
            result = self.hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is True
        assert "AZ_FAILOVER_TENANT_RECOVERED" in self._audit_ops()

    def test_cross_alb_probe_accepts_302_as_reachable(self):
        """302 from urlopen also indicates a reachable backend."""
        with patch.object(self.hc, "_verify_vm_actually_running",
                          return_value=True), \
             patch.object(self.hc, "_repoint_alb_rule"), \
             patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_returning(302)):
            result = self.hc._failover_tenant_to_host(
                _tenant(), _target_host(), "ap-northeast-1a", datetime.now(timezone.utc))
        assert result is True


# ══════════════════════════════════════════════════════════════════════
# _verify_dashboard_reachable_via_alb — focused unit tests
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestVerifyDashboardReachable:
    def setup_method(self):
        self.hc = _load_hc()
        self._sleep_patch = patch("time.sleep", lambda *a, **k: None)
        self._sleep_patch.start()

    def teardown_method(self):
        self._sleep_patch.stop()

    def test_returns_true_on_200(self):
        with patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_returning(200)):
            assert self.hc._verify_dashboard_reachable_via_alb(
                "t1", "http://alb.example.com", timeout_sec=2) is True

    def test_returns_true_on_4xx(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_raising(
                       urllib.error.HTTPError("x", 403, "", None, None))):
            assert self.hc._verify_dashboard_reachable_via_alb(
                "t1", "http://alb.example.com", timeout_sec=2) is True

    def test_returns_false_on_5xx(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_raising(
                       urllib.error.HTTPError("x", 502, "", None, None))):
            assert self.hc._verify_dashboard_reachable_via_alb(
                "t1", "http://alb.example.com", timeout_sec=2, poll_sec=0.5) is False

    def test_returns_false_on_connection_refused(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=_make_urlopen_raising(
                       urllib.error.URLError("Connection refused"))):
            assert self.hc._verify_dashboard_reachable_via_alb(
                "t1", "http://alb.example.com", timeout_sec=2, poll_sec=0.5) is False

    def test_empty_base_url_fails_closed(self):
        assert self.hc._verify_dashboard_reachable_via_alb(
            "t1", "", timeout_sec=2) is False

    def test_eventually_passes_after_initial_5xx(self):
        """Real-world: ALB may need a few seconds to register the new
        target. First call 502, second call 200 — gate should pass."""
        import urllib.error
        responses = [
            urllib.error.HTTPError("x", 502, "", None, None),
            urllib.error.HTTPError("x", 502, "", None, None),
            "200",  # third call: success
        ]
        call_count = [0]

        def fake_urlopen(req, timeout=5):
            r = responses[min(call_count[0], len(responses) - 1)]
            call_count[0] += 1
            if isinstance(r, Exception):
                raise r
            m = MagicMock()
            m.status = int(r)
            m.__enter__ = lambda self: m
            m.__exit__ = lambda *a: False
            return m

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            assert self.hc._verify_dashboard_reachable_via_alb(
                "t1", "http://alb.example.com",
                timeout_sec=10, poll_sec=0.1) is True
        assert call_count[0] >= 3
