# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for RBAC (issue #14).

Model
-----
Cognito User Pool **Groups** = roles. Three groups:
    admin     — full access (CRUD + RBAC management)
    operator  — CRUD + actions, no RBAC management
    viewer    — read-only

JWT id_token from Cognito carries `cognito:groups` claim. The console
attaches it as `Authorization: Bearer <id_token>` on every request; the
Lambda handler decodes the JWT (without re-validating the signature —
API Gateway already validated the API key, and we only use the claim
for authorization, not authentication).

Backward compatibility
----------------------
Requests without a Bearer token are treated as admin (preserves existing
CLI / curl flows that authenticate purely via x-api-key).
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
# Load handler with mocked SDK
# ═══════════════════════════════════════════


_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

# Set required env vars before import
os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
os.environ.setdefault("HOSTS_TABLE", "openclaw-hosts")
os.environ.setdefault("ASSETS_BUCKET", "test")
os.environ.setdefault("ROOTFS_PREFIX", "deployment/rootfs")

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    spec = importlib.util.spec_from_file_location(
        "rbac_handler", str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
    handler = importlib.util.module_from_spec(spec)
    sys.modules["rbac_handler"] = handler
    spec.loader.exec_module(handler)


def _make_id_token(groups):
    """Build an unsigned JWT with cognito:groups claim.

    We don't sign — the Lambda only decodes for the role claim.
    """
    import base64
    header = {"alg": "none", "typ": "JWT"}
    payload = {"cognito:groups": groups, "email": "test@example.com"}
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{b64(header)}.{b64(payload)}."


def _event(method="GET", path="/tenants", role=None):
    """Build an API Gateway event, optionally with an id_token in Authorization."""
    headers = {"x-api-key": "test"}
    if role is not None:
        headers["Authorization"] = f"Bearer {_make_id_token([role] if isinstance(role, str) else role)}"
    return {
        "httpMethod": method,
        "resource": path,
        "headers": headers,
        "queryStringParameters": None,
        "pathParameters": None,
        "body": None,
    }


# ═══════════════════════════════════════════
# Pure helpers
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestRoleResolution:
    def test_admin_extracted_from_groups(self):
        ev = _event(role="admin")
        assert handler._get_user_role(ev) == "admin"

    def test_operator_extracted(self):
        ev = _event(role="operator")
        assert handler._get_user_role(ev) == "operator"

    def test_viewer_extracted(self):
        ev = _event(role="viewer")
        assert handler._get_user_role(ev) == "viewer"

    def test_no_token_returns_admin_for_backcompat(self):
        """No Authorization header → admin (CLI/curl path with api key)."""
        ev = _event()
        assert handler._get_user_role(ev) == "admin"

    def test_multiple_groups_picks_highest(self):
        """A user in admin+operator gets admin (most privileged wins)."""
        ev = _event(role=["operator", "admin"])
        assert handler._get_user_role(ev) == "admin"

    def test_unknown_group_falls_back_to_viewer(self):
        """Group name not in admin/operator/viewer → viewer (least privileged)."""
        ev = _event(role="random-team")
        assert handler._get_user_role(ev) == "viewer"


@pytest.mark.unit
class TestRoleHierarchy:
    def test_admin_satisfies_all(self):
        assert handler._role_satisfies("admin", "admin") is True
        assert handler._role_satisfies("admin", "operator") is True
        assert handler._role_satisfies("admin", "viewer") is True

    def test_operator_does_not_satisfy_admin(self):
        assert handler._role_satisfies("operator", "admin") is False
        assert handler._role_satisfies("operator", "operator") is True
        assert handler._role_satisfies("operator", "viewer") is True

    def test_viewer_only_satisfies_viewer(self):
        assert handler._role_satisfies("viewer", "admin") is False
        assert handler._role_satisfies("viewer", "operator") is False
        assert handler._role_satisfies("viewer", "viewer") is True


# ═══════════════════════════════════════════
# Handler-level enforcement
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestEndpointEnforcement:
    def test_viewer_can_list_tenants(self):
        """GET /tenants is read-only — viewer allowed."""
        with patch.object(handler, "list_tenants", return_value=handler._resp(200, {"items": []})):
            r = handler.lambda_handler(_event("GET", "/tenants", role="viewer"), None)
        assert r["statusCode"] == 200

    def test_viewer_cannot_create_tenant(self):
        """POST /tenants requires operator+. Viewer → 403."""
        ev = _event("POST", "/tenants", role="viewer")
        ev["body"] = json.dumps({"name": "demo"})
        r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 403
        assert "forbidden" in r["body"].lower() or "rbac" in r["body"].lower()

    def test_operator_can_create_tenant(self):
        """POST /tenants — operator allowed."""
        ev = _event("POST", "/tenants", role="operator")
        ev["body"] = json.dumps({"name": "demo"})
        with patch.object(handler, "create_tenant",
                          return_value=handler._resp(201, {"id": "t1"})):
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 201

    def test_viewer_cannot_delete_tenant(self):
        ev = _event("DELETE", "/tenants/{id}", role="viewer")
        ev["pathParameters"] = {"id": "t1"}
        r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 403

    def test_viewer_cannot_run_action(self):
        """POST /tenants/{id}/{action} (e.g. restart) — viewer 403."""
        ev = _event("POST", "/tenants/{id}/{action}", role="viewer")
        ev["pathParameters"] = {"id": "t1", "action": "restart"}
        r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 403


@pytest.mark.unit
class TestCDKCognitoGroups:
    def test_three_user_pool_groups_when_auth_enabled(self):
        """When console_auth.enabled: true, CDK creates admin/operator/viewer groups."""
        import yaml
        cfg_path = ROOT / "config.yml"
        original = cfg_path.read_text()
        cfg = yaml.safe_load(original)
        cfg.setdefault("console_auth", {})["enabled"] = True
        cfg_path.write_text(yaml.safe_dump(cfg))
        try:
            sys.modules.pop("deploy.stack", None)
            sys.modules.pop("deploy", None)
            spec = importlib.util.spec_from_file_location(
                "deploy.stack", ROOT / "deploy" / "stack.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["deploy.stack"] = mod
            spec.loader.exec_module(mod)
            import aws_cdk as cdk
            from aws_cdk import assertions
            app = cdk.App()
            stack = mod.OpenClawOrchestratorStack(app, "Test",
                env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
            tpl = assertions.Template.from_stack(stack)
            tpl.resource_count_is("AWS::Cognito::UserPoolGroup", 3)
            for group_name in ("admin", "operator", "viewer"):
                tpl.has_resource_properties("AWS::Cognito::UserPoolGroup", {
                    "GroupName": group_name,
                })
        finally:
            cfg_path.write_text(original)
