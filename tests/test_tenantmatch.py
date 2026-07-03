# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for #97 档A — GET /tenantmatch (external-platform → Cognito IdP routing).

Covers happy path + adversarial: missing/malformed platform_id, federation not
configured (no table), platform not registered, upstream error fail-loud, and that
/tenantmatch is in _RBAC_SKIP (pre-login, no JWT required) but leaks no secrets.
"""

import importlib.util
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

pytestmark = pytest.mark.unit

_mock_ddb = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: MagicMock()
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_tm", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_tm"] = api
    spec.loader.exec_module(api)


def _body(resp):
    return json.loads(resp["body"])


class TestTenantMatchValidation:
    def test_missing_platform_id_400(self):
        r = api.tenant_match({})
        assert r["statusCode"] == 400 and _body(r)["code"] == "VALIDATION"

    def test_empty_platform_id_400(self):
        r = api.tenant_match({"platform_id": "  "})
        assert r["statusCode"] == 400 and _body(r)["code"] == "VALIDATION"

    @pytest.mark.parametrize(
        "bad",
        ["has space", "semi;colon", "slash/x", "a" * 129, "inj'ection", "back`tick"],
    )
    def test_malformed_platform_id_400(self, bad):
        r = api.tenant_match({"platform_id": bad})
        assert r["statusCode"] == 400, f"{bad!r} should be rejected"
        assert _body(r)["code"] == "VALIDATION"


class TestTenantMatchLookup:
    def test_not_configured_404(self):
        # federation table absent → 404 NOT_CONFIGURED (front-end falls back)
        with patch.object(api, "tenant_idp_table", None):
            r = api.tenant_match({"platform_id": "demo-marketplace"})
        assert r["statusCode"] == 404 and _body(r)["code"] == "NOT_CONFIGURED"

    def test_platform_not_registered_404(self):
        t = MagicMock()
        t.get_item.return_value = {}  # no Item
        with patch.object(api, "tenant_idp_table", t):
            r = api.tenant_match({"platform_id": "unknown-shop"})
        assert r["statusCode"] == 404 and _body(r)["code"] == "NOT_FOUND"

    def test_happy_path_returns_idp(self):
        t = MagicMock()
        t.get_item.return_value = {
            "Item": {
                "platform_id": "demo-marketplace",
                "idp_provider_name": "demo-marketplace",
                "issuer_url": "https://cognito-idp.ap-northeast-1.amazonaws.com/x",
            }
        }
        with patch.object(api, "tenant_idp_table", t):
            r = api.tenant_match({"platform_id": "demo-marketplace"})
        assert r["statusCode"] == 200
        b = _body(r)
        assert b["idp_provider_name"] == "demo-marketplace"
        assert b["platform_id"] == "demo-marketplace"

    def test_upstream_error_fail_loud_502(self):
        t = MagicMock()
        t.get_item.side_effect = RuntimeError("ddb boom")
        with patch.object(api, "tenant_idp_table", t):
            r = api.tenant_match({"platform_id": "demo-marketplace"})
        assert r["statusCode"] == 502 and _body(r)["code"] == "UPSTREAM"

    def test_item_without_idp_name_404(self):
        # malformed row (no idp_provider_name) → treated as not-found, not 500
        t = MagicMock()
        t.get_item.return_value = {"Item": {"platform_id": "x"}}
        with patch.object(api, "tenant_idp_table", t):
            r = api.tenant_match({"platform_id": "x"})
        assert r["statusCode"] == 404

    def test_response_leaks_no_secret_fields(self):
        # even if the row carries stray sensitive-looking fields, response only
        # returns routing fields (platform_id/idp_provider_name/issuer_url)
        t = MagicMock()
        t.get_item.return_value = {
            "Item": {
                "platform_id": "p",
                "idp_provider_name": "p",
                "issuer_url": "u",
                "client_secret": "SHOULD-NOT-LEAK",
            }
        }
        with patch.object(api, "tenant_idp_table", t):
            r = api.tenant_match({"platform_id": "p"})
        assert "SHOULD-NOT-LEAK" not in r["body"]
        assert set(_body(r).keys()) == {
            "platform_id",
            "idp_provider_name",
            "issuer_url",
        }


class TestTenantMatchRbac:
    def test_in_rbac_skip(self):
        # pre-login route: must bypass RBAC (browser calls before any JWT)
        assert ("GET", "/tenantmatch") in api._RBAC_SKIP
