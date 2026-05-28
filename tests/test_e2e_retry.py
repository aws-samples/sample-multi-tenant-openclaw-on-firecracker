# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the e2e _api() retry helper (1.4.3 SSL flake fix).

Why these are unit tests rather than e2e: the retry behavior must be
verifiable without a deployed API Gateway, and we need to inject
controlled failure sequences (URLError on call 1, 200 on call 2)
which can't be done against real infrastructure.
"""

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_test_e2e_module():
    """Load tests/test_e2e.py as a regular module so we can call _api()
    directly. The module's pytest.skip on missing .env.deploy is fine
    here because we only need the function definition; we patch urlopen
    before any call."""
    import os
    # Ensure env is set so the module's load-time skip doesn't trigger.
    os.environ.setdefault("API_URL", "https://test.example.com/v1")
    os.environ.setdefault("API_KEY", "test-key")
    # Clear cached version
    sys.modules.pop("e2e_module_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "e2e_module_under_test", str(ROOT / "tests/test_e2e.py"))
    mod = importlib.util.module_from_spec(spec)
    # Inject env into the module's globals BEFORE exec so the top-level
    # env reads pick up the test values.
    mod.__dict__["API_URL"] = "https://test.example.com/v1"
    mod.__dict__["API_KEY"] = "test-key"
    sys.modules["e2e_module_under_test"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # The module may pytest.skip at load time when .env.deploy is
        # missing — that's fine, the function definitions still exist.
        pass
    # Override ENV-derived globals after exec so the function uses our
    # test values regardless of what was loaded.
    mod.API_URL = "https://test.example.com/v1"
    mod.API_KEY = "test-key"
    return mod


def _make_response(status, body_dict):
    """Build a context-manager mock that mimics urlopen()'s return."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body_dict).encode()
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda *a: False
    return resp


@pytest.mark.unit
class TestApiRetry:
    """Cover _api() retry policy for the 1.4.3 SSL flake fix."""

    def setup_method(self):
        self.mod = _load_test_e2e_module()
        # Patch sleep so retries don't actually wait between attempts.
        self._sleep_patch = patch("time.sleep", lambda *a, **k: None)
        self._sleep_patch.start()

    def teardown_method(self):
        self._sleep_patch.stop()

    # ────────────────────────────────────────────────
    # Happy path: first call succeeds → no retry
    # ────────────────────────────────────────────────
    def test_first_call_success_no_retry(self):
        with patch("urllib.request.urlopen",
                   return_value=_make_response(200, {"ok": True})) as mock_open:
            status, body = self.mod._api("GET", "/test")
        assert status == 200
        assert body == {"ok": True}
        assert mock_open.call_count == 1

    # ────────────────────────────────────────────────
    # The bug we're fixing: SSL EOF on first call,
    # success on retry → must succeed after retry
    # ────────────────────────────────────────────────
    def test_ssl_eof_recovers_on_retry(self):
        import urllib.error
        responses = [
            urllib.error.URLError(
                "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"),
            _make_response(200, {"recovered": True}),
        ]
        call_count = [0]

        def fake_urlopen(req, timeout=30):
            r = responses[min(call_count[0], len(responses) - 1)]
            call_count[0] += 1
            if isinstance(r, Exception):
                raise r
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            status, body = self.mod._api("GET", "/test")
        assert status == 200
        assert body == {"recovered": True}
        assert call_count[0] == 2

    # ────────────────────────────────────────────────
    # 5xx on first call, 200 on retry → must recover
    # ────────────────────────────────────────────────
    def test_transient_5xx_recovers_on_retry(self):
        import urllib.error
        responses = [
            urllib.error.HTTPError("x", 502, "Bad Gateway", None, None),
            _make_response(200, {"recovered": True}),
        ]
        call_count = [0]

        def fake_urlopen(req, timeout=30):
            r = responses[min(call_count[0], len(responses) - 1)]
            call_count[0] += 1
            if isinstance(r, Exception):
                raise r
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            status, body = self.mod._api("GET", "/test")
        assert status == 200
        assert body == {"recovered": True}
        assert call_count[0] == 2

    # ────────────────────────────────────────────────
    # 4xx must NOT retry — surface immediately
    # ────────────────────────────────────────────────
    def test_4xx_no_retry(self):
        import urllib.error
        e = urllib.error.HTTPError("x", 401, "Unauthorized", None, None)
        e.read = lambda: b'{"error":"forbidden"}'
        with patch("urllib.request.urlopen", side_effect=e) as mock_open:
            status, body = self.mod._api("GET", "/test")
        assert status == 401
        assert body == {"error": "forbidden"}
        assert mock_open.call_count == 1

    # ────────────────────────────────────────────────
    # Exhausted retries on persistent SSL error → raise
    # ────────────────────────────────────────────────
    def test_persistent_ssl_eof_exhausts_and_raises(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING]")):
            with pytest.raises(urllib.error.URLError):
                self.mod._api("GET", "/test", max_retries=3)

    # ────────────────────────────────────────────────
    # Exhausted retries on persistent 5xx → return the 5xx (no raise)
    # ────────────────────────────────────────────────
    def test_persistent_5xx_returns_status_after_exhaustion(self):
        import urllib.error
        e = urllib.error.HTTPError("x", 503, "Unavailable", None, None)
        e.read = lambda: b'{"error":"service down"}'
        with patch("urllib.request.urlopen", side_effect=e):
            status, body = self.mod._api("GET", "/test", max_retries=3)
        assert status == 503
        assert body == {"error": "service down"}

    # ────────────────────────────────────────────────
    # Exponential backoff: 1s, 2s, 4s between attempts
    # ────────────────────────────────────────────────
    def test_exponential_backoff(self):
        import urllib.error
        sleeps = []
        # We're already patching time.sleep in setup_method via no-op;
        # for this test, capture the args.
        self._sleep_patch.stop()
        with patch("time.sleep", lambda s: sleeps.append(s)):
            with patch("urllib.request.urlopen",
                       side_effect=urllib.error.URLError("transient")):
                with pytest.raises(urllib.error.URLError):
                    self.mod._api("GET", "/test", max_retries=3)
        # 3 attempts → 2 sleeps (between attempts): 2^0=1, 2^1=2
        assert sleeps == [1, 2]
        # Restart the no-op patch so teardown doesn't fail
        self._sleep_patch = patch("time.sleep", lambda *a, **k: None)
        self._sleep_patch.start()

    # ────────────────────────────────────────────────
    # max_retries=1 → no retry, raise on first failure
    # ────────────────────────────────────────────────
    def test_max_retries_1_disables_retry(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("first failure")) as mock_open:
            with pytest.raises(urllib.error.URLError):
                self.mod._api("GET", "/test", max_retries=1)
        assert mock_open.call_count == 1
