# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The public-path verify gate must not pass on a 1/N-working tenant.

The bug
-------
`_verify_dashboard_reachable_via_alb` returned True on the FIRST acceptable
response. Under host-tg routing the `/vm/*` catch-all round-robins every new
connection across all in-service hosts, and only the owner (or a host whose
peer conf has converged) can serve the tenant. So a single sample passing meant
"at least one host works" — not "the tenant is reachable". The gate would flip
DynamoDB to `running` and emit MIGRATION_COMPLETED / recovery audits for a
tenant that 404s for most users.

This is the 1.4.2 "fake failover" defect reincarnated one level up: 1.4.2 fixed
*what* counts as a success (404 no longer passes), this fixes *how many samples*
are required. Both halves are needed — a strict status check sampled once is
still only 1/N confidence.

Design points asserted here:
  * per-tenant routing keeps K=1, so its behaviour is unchanged (the tenant's
    listener rule pins traffic to one target group, so one sample IS
    conclusive; raising K there would triple migration verify time for nothing);
  * host-tg defaults to K=3;
  * the streak RESETS on any failure, so an intermittent host cannot accumulate
    successes across the polling window;
  * probes must not reuse a keep-alive connection, which would resample the
    same target and defeat the mechanism.
"""

import importlib.util
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.unit


def _load_hc(routing_mode="host-tg", **extra_env):
    env = {
        "ROUTING_MODE": routing_mode,
        "PUBLIC_BASE_URL": "http://alb.example.com",
        "ALB_LISTENER_ARN": "arn:aws:elasticloadbalancing:x:1:listener/app/a/b/c",
        "VPC_ID": "vpc-test",
    }
    env.update(extra_env)
    mock_ddb = MagicMock()
    with patch("boto3.resource", return_value=mock_ddb), \
         patch("boto3.client", return_value=MagicMock()), \
         patch.dict("os.environ", env):
        mock_ddb.Table.side_effect = lambda name: MagicMock()
        name = f"hc_verify_{routing_mode}_{len(extra_env)}"
        spec = importlib.util.spec_from_file_location(
            name, str(ROOT / "deploy" / "lambda" / "health_check" / "handler.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


class _Responses:
    """Replays a scripted sequence of statuses; records each request."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        status = self.statuses.pop(0) if self.statuses else 404
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {}, None)
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        return resp


def _verify(mod, statuses, timeout_sec=60):
    """Drive the gate over a scripted status sequence.

    poll_sec must be NON-zero and time.sleep must NOT be patched here:
    conftest._fast_clock turns sleep() into "advance a virtual clock offset",
    so a non-zero poll makes the deadline arrive instantly. With poll_sec=0 the
    virtual clock never moves and the loop spins against the real wall clock
    until timeout_sec actually elapses.
    """
    responses = _Responses(statuses)
    with patch("urllib.request.urlopen", side_effect=responses):
        got = mod._verify_dashboard_reachable_via_alb(
            "t1", "http://alb.example.com", timeout_sec=timeout_sec, poll_sec=1)
    return got, responses


class TestHostTgNeedsConsecutiveSuccesses:
    def setup_method(self):
        self.hc = _load_hc("host-tg")

    def test_default_k_is_three(self):
        assert self.hc.VERIFY_CONSECUTIVE_OK == 3

    def test_single_success_is_not_enough(self):
        """The exact bug: one lucky probe used to pass the gate."""
        ok, r = _verify(self.hc, [200, 404, 404, 404, 404, 404, 404, 404])
        assert ok is False, (
            "a single successful probe passed the gate — under the catch-all "
            "that only proves 1/N of the fleet can serve this tenant")

    def test_three_consecutive_successes_pass(self):
        ok, r = _verify(self.hc, [200, 200, 200])
        assert ok is True
        assert len(r.requests) == 3, "should stop as soon as the streak is met"

    def test_alternating_pass_fail_never_passes(self):
        """A partially-converged fleet produces exactly this pattern."""
        ok, _ = _verify(self.hc, [200, 404] * 12)
        assert ok is False, (
            "alternating success/failure passed — the streak did not reset, so "
            "an intermittent host accumulated credit over the window")

    def test_streak_resets_then_can_still_succeed(self):
        ok, _ = _verify(self.hc, [200, 200, 404, 200, 200, 200])
        assert ok is True, "a genuine recovery after a reset must still pass"

    def test_streak_reset_is_logged(self, capsys):
        _verify(self.hc, [200, 404] * 12)
        out = capsys.readouterr().out
        assert "streak reset" in out, (
            "an alternating pattern is the signature of a half-converged fleet "
            "and must be visible in the logs")

    def test_failure_log_reports_the_streak(self, capsys):
        _verify(self.hc, [404] * 20)
        assert "streak=" in capsys.readouterr().out


class TestPerTenantIsUnchanged:
    """One sample IS conclusive when a listener rule pins the target group."""

    def setup_method(self):
        self.hc = _load_hc("per-tenant")

    def test_default_k_is_one(self):
        assert self.hc.VERIFY_CONSECUTIVE_OK == 1, (
            "raising K under per-tenant routing triples migration verify time "
            "with no correctness gain — traffic is pinned to one target group")

    def test_single_success_passes(self):
        ok, r = _verify(self.hc, [200])
        assert ok is True
        assert len(r.requests) == 1


class TestStatusSemanticsPreserved:
    """1.4.2 + T3-1: what counts as success must not regress."""

    def setup_method(self):
        self.hc = _load_hc("host-tg", VERIFY_CONSECUTIVE_OK="1")

    @pytest.mark.parametrize("status", [200, 204, 302, 401, 403])
    def test_alive_backend_codes_pass(self, status):
        ok, _ = _verify(self.hc, [status])
        assert ok is True, f"{status} should count as backend-alive"

    @pytest.mark.parametrize("status", [404, 400, 500, 502, 503])
    def test_not_reachable_codes_do_not_pass(self, status):
        ok, _ = _verify(self.hc, [status] * 20)
        assert ok is False, (
            f"{status} passed the gate — 404 in particular is what a host "
            "without the tenant's nginx conf returns under the catch-all")

    def test_connection_error_does_not_pass(self):
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            ok = self.hc._verify_dashboard_reachable_via_alb(
                "t1", "http://alb.example.com", timeout_sec=5, poll_sec=1)
        assert ok is False

    def test_missing_base_url_fails_closed(self):
        assert self.hc._verify_dashboard_reachable_via_alb("t1", "") is False


class TestProbesResampleTheFleet:
    def setup_method(self):
        self.hc = _load_hc("host-tg")

    def test_connection_close_is_requested(self):
        """Keep-alive would pin every probe to one target and defeat the check."""
        ok, r = _verify(self.hc, [200, 200, 200])
        assert ok is True
        for req in r.requests:
            assert req.get_header("Connection") == "close", (
                "probes must not reuse a connection — otherwise all K samples "
                "hit the SAME host and K consecutive successes prove nothing")

    def test_env_override_is_honoured(self):
        hc = _load_hc("host-tg", VERIFY_CONSECUTIVE_OK="5")
        assert hc.VERIFY_CONSECUTIVE_OK == 5
        ok, _ = _verify(hc, [200] * 4 + [404] * 20)
        assert ok is False
        ok, _ = _verify(hc, [200] * 5)
        assert ok is True

    def test_k_below_one_is_clamped(self):
        """A misconfigured 0 must not disable the gate entirely."""
        hc = _load_hc("host-tg", VERIFY_CONSECUTIVE_OK="0")
        ok, r = _verify(hc, [404] * 20)
        assert ok is False, "K=0 must not auto-pass without any probe"
        assert r.requests, "K=0 must still probe at least once"
