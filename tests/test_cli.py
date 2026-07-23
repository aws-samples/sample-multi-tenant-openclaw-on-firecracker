# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the `oc` CLI (issue #21).

The CLI is a single-file argparse Python tool (`cli/oc.py`) that wraps
the orchestrator's API. We test the *intent* of each subcommand by
mocking urllib so no network calls fire:

    oc list                    → GET   /tenants
    oc get <id>                → GET   /tenants/<id>
    oc create <name> [...]     → POST  /tenants  body={name, ...}
    oc delete <id>             → DELETE /tenants/<id>
    oc <action> <id>           → POST  /tenants/<id>/<action>  (restart/start/stop/...)
    oc backups                 → GET   /backups
    oc hosts                   → GET   /hosts
    oc version                 → prints version string, exit 0

Configuration is loaded from `.env.deploy` or env vars
`OC_API_URL` / `OC_API_KEY`.
"""

import importlib.util
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════
# Load CLI module
# ═══════════════════════════════════════════


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "oc_cli", str(ROOT / "cli" / "oc.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["oc_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


def _mock_urlopen_factory(captured, body=b'{}', status=200):
    """Return a fake urllib.request.urlopen that records the request."""
    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        resp = MagicMock()
        resp.read.return_value = body
        resp.status = status
        resp.__enter__ = lambda self: self
        resp.__exit__ = lambda self, *a: False
        return resp
    return fake_urlopen


# ═══════════════════════════════════════════
# Configuration / env
# ═══════════════════════════════════════════


@pytest.fixture(autouse=True)
def _api_env(monkeypatch):
    monkeypatch.setenv("OC_API_URL", "https://api.example.com/v1/")
    monkeypatch.setenv("OC_API_KEY", "test-key-123")


@pytest.mark.unit
class TestEnvLoading:
    def test_reads_from_env(self):
        cli = _load_cli()
        api_url, api_key = cli._load_env()
        assert api_url == "https://api.example.com/v1/"
        assert api_key == "test-key-123"

    def test_strips_trailing_slash_in_api_url(self):
        cli = _load_cli()
        # _api_url joins endpoint without double slashes
        url = cli._build_url("https://api.example.com/v1/", "/tenants")
        assert url == "https://api.example.com/v1/tenants"

    def test_handles_missing_credentials_gracefully(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OC_API_URL", raising=False)
        monkeypatch.delenv("OC_API_KEY", raising=False)
        # Isolate from the project's real `.env.deploy`: oc.py walks up from
        # the cwd looking for one. Run the test inside an empty tmp directory
        # so the file lookup walks above the project root and finds nothing.
        monkeypatch.chdir(tmp_path)
        # Force fixture-less behavior; expect _load_env returns (None, None)
        cli = _load_cli()
        api_url, api_key = cli._load_env()
        assert api_url is None or api_url == ""
        assert api_key is None or api_key == ""


# ═══════════════════════════════════════════
# Subcommands
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestListCommand:
    def test_list_calls_get_tenants(self):
        captured = {}
        cli = _load_cli()
        with patch("urllib.request.urlopen",
                   _mock_urlopen_factory(captured, body=b'{"items":[]}')):
            rc = cli.main(["list"])
        assert rc == 0
        assert captured["url"].endswith("/tenants")
        assert captured["method"] == "GET"
        assert captured["headers"].get("X-api-key") == "test-key-123"


@pytest.mark.unit
class TestGetCommand:
    def test_get_calls_specific_tenant(self):
        captured = {}
        cli = _load_cli()
        with patch("urllib.request.urlopen",
                   _mock_urlopen_factory(captured, body=b'{"id":"t1"}')):
            rc = cli.main(["get", "t1"])
        assert rc == 0
        assert captured["url"].endswith("/tenants/t1")
        assert captured["method"] == "GET"


@pytest.mark.unit
class TestCreateCommand:
    def test_create_with_name_only(self):
        captured = {}
        cli = _load_cli()
        with patch("urllib.request.urlopen",
                   _mock_urlopen_factory(captured, body=b'{"id":"t1"}', status=201)):
            rc = cli.main(["create", "demo"])
        assert rc == 0
        assert captured["url"].endswith("/tenants")
        assert captured["method"] == "POST"
        body = json.loads(captured["body"])
        assert body["name"] == "demo"

    def test_create_with_resources(self):
        captured = {}
        cli = _load_cli()
        with patch("urllib.request.urlopen",
                   _mock_urlopen_factory(captured, body=b'{"id":"t2"}', status=201)):
            rc = cli.main(["create", "demo2", "--vcpu", "4", "--mem-mb", "8192"])
        assert rc == 0
        body = json.loads(captured["body"])
        assert body["vcpu"] == 4
        assert body["mem_mb"] == 8192


@pytest.mark.unit
class TestDeleteCommand:
    def test_delete_with_force_flag(self):
        captured = {}
        cli = _load_cli()
        with patch("urllib.request.urlopen",
                   _mock_urlopen_factory(captured, body=b'{"ok":true}')):
            rc = cli.main(["delete", "t1", "--force"])
        assert rc == 0
        assert captured["url"].endswith("/tenants/t1") or "t1?" in captured["url"]
        assert captured["method"] == "DELETE"

    def test_delete_without_force_prompts(self, monkeypatch):
        """Without --force, should refuse via stdin no."""
        cli = _load_cli()
        monkeypatch.setattr("builtins.input", lambda _: "n")
        rc = cli.main(["delete", "t1"])
        # Either prompted-no (rc=1) or refused without making a call.
        assert rc != 0


@pytest.mark.unit
class TestActionCommands:
    @pytest.mark.parametrize("action", ["restart", "start", "stop", "pause", "resume", "backup"])
    def test_action_calls_post(self, action):
        captured = {}
        cli = _load_cli()
        with patch("urllib.request.urlopen",
                   _mock_urlopen_factory(captured, body=b'{"ok":true}')):
            rc = cli.main([action, "t1"])
        assert rc == 0
        assert captured["url"].endswith(f"/tenants/t1/{action}")
        assert captured["method"] == "POST"


@pytest.mark.unit
class TestSecondaryCommands:
    def test_backups_lists_all(self):
        captured = {}
        cli = _load_cli()
        with patch("urllib.request.urlopen",
                   _mock_urlopen_factory(captured, body=b'{"items":[]}')):
            rc = cli.main(["backups"])
        assert rc == 0
        assert captured["url"].endswith("/backups")
        assert captured["method"] == "GET"

    def test_hosts_lists_hosts(self):
        captured = {}
        cli = _load_cli()
        with patch("urllib.request.urlopen",
                   _mock_urlopen_factory(captured, body=b'{"items":[]}')):
            rc = cli.main(["hosts"])
        assert rc == 0
        assert captured["url"].endswith("/hosts")
        assert captured["method"] == "GET"


@pytest.mark.unit
class TestVersionCommand:
    def test_version_prints_and_exits_zero(self, capsys):
        cli = _load_cli()
        rc = cli.main(["version"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "oc" in out.lower() or "version" in out.lower()


@pytest.mark.unit
class TestErrorHandling:
    def test_unknown_command_returns_nonzero(self, capsys):
        cli = _load_cli()
        # argparse will SystemExit(2) for invalid subcommand
        with pytest.raises(SystemExit) as e:
            cli.main(["bogus-command"])
        assert e.value.code != 0

    def test_http_error_returns_nonzero(self):
        cli = _load_cli()
        # Simulate 500
        def boom(req, timeout=None):
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 500, "boom",
                                          {"content-type": "application/json"},
                                          BytesIO(b'{"error":"boom"}'))
        with patch("urllib.request.urlopen", boom):
            rc = cli.main(["list"])
        assert rc != 0

    def test_missing_credentials_errors(self, monkeypatch, capsys, tmp_path):
        monkeypatch.delenv("OC_API_URL", raising=False)
        monkeypatch.delenv("OC_API_KEY", raising=False)
        # See test_handles_missing_credentials_gracefully — chdir away from
        # the project root so oc.py's `.env.deploy` lookup finds nothing.
        monkeypatch.chdir(tmp_path)
        cli = _load_cli()
        # Force missing-creds path via a method that needs them
        rc = cli.main(["list"])
        assert rc != 0
        err = capsys.readouterr().err + capsys.readouterr().out
        assert "OC_API_URL" in err or "credential" in err.lower() or "missing" in err.lower()
