# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for RBAC with JWT signature verification (1.5.0 hardening).

Model
-----
Cognito User Pool **Groups** = roles. Three groups:
    admin     — full access (CRUD + RBAC management)
    operator  — CRUD + actions, no RBAC management
    viewer    — read-only

The console attaches a Cognito **id_token** as ``Authorization: Bearer
<jwt>``. As of 1.5.0 the handler VERIFIES the token's RS256 signature against
the User Pool's JWKS (``_verify_and_decode``) before trusting any claim. The
pre-1.5.0 behavior — decode-without-verify, and "no token ⇒ admin" — was a
real, exploitable privilege-escalation hole:

    * An attacker could base64-craft ``{"cognito:groups":["admin"]}`` with
      ``alg:none`` and no signature, and the handler would grant admin.
    * Any request without a Bearer token defaulted to admin (fail-open).

Fail-safe contract now under test
---------------------------------
    no Bearer token            → DEFAULT_NO_JWT_ROLE (default: viewer)
    token, signature INVALID   → viewer  (forged / alg:none / wrong key)
    token, EXPIRED             → viewer
    token, wrong issuer/aud    → viewer
    token, signature VALID     → role from cognito:groups (admin>operator>viewer)

How these tests verify *real* signatures
-----------------------------------------
We generate an RSA keypair in-process, sign tokens with the private key, and
point the handler's verification at the matching public key by patching the
one seam — ``_get_jwks_client`` — to return a fake JWKS client backed by our
public key. Forgery tests sign with a *different* (attacker) key, so a correct
implementation rejects them. We never patch ``_verify_and_decode`` itself for
the security cases — that function is exactly what we are testing.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Crypto deps are provided by the `test` dependency group (pyjwt, cryptography)
# and bundled into the Lambda asset for prod. Skip the whole module cleanly if
# somehow absent, rather than erroring at collection.
jwt = pytest.importorskip("jwt", reason="PyJWT required for RBAC signature tests")
_crypto = pytest.importorskip(
    "cryptography.hazmat.primitives.asymmetric.rsa",
    reason="cryptography required for RBAC signature tests")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402


# ═══════════════════════════════════════════
# Load handler with mocked SDK + Cognito env
# ═══════════════════════════════════════════

_TEST_POOL = "ap-northeast-1_TESTPOOL"
_TEST_REGION = "ap-northeast-1"
_TEST_ISSUER = f"https://cognito-idp.{_TEST_REGION}.amazonaws.com/{_TEST_POOL}"

os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
os.environ.setdefault("HOSTS_TABLE", "openclaw-hosts")
os.environ.setdefault("ASSETS_BUCKET", "test")
os.environ.setdefault("ROOTFS_PREFIX", "deployment/rootfs")
# Drive the verification path: a configured pool + region means
# _get_jwks_client() would build a real client — we patch it per-test.
os.environ["COGNITO_USER_POOL_ID"] = _TEST_POOL
os.environ["AWS_REGION"] = _TEST_REGION
os.environ.setdefault("DEFAULT_NO_JWT_ROLE", "viewer")

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    spec = importlib.util.spec_from_file_location(
        "rbac_handler", str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
    handler = importlib.util.module_from_spec(spec)
    sys.modules["rbac_handler"] = handler
    spec.loader.exec_module(handler)


# ═══════════════════════════════════════════
# RSA keypairs: one "real" (matches JWKS), one "attacker"
# ═══════════════════════════════════════════

def _gen_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


_REAL_KEY = _gen_key()       # the key the fake JWKS will hand back
_ATTACKER_KEY = _gen_key()   # a different key — forgery attempts use this


class _FakeSigningKey:
    """Mimics PyJWKClient.get_signing_key_from_jwt(...).key — exposes .key."""
    def __init__(self, public_key):
        self.key = public_key


class _FakeJWKSClient:
    """Returns our real public key regardless of the token's kid."""
    def __init__(self, public_key):
        self._pub = public_key

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(self._pub)


def _install_real_jwks():
    """Patch the handler so verification uses _REAL_KEY's public half.

    Returns a patcher context manager. Forged tokens (signed by _ATTACKER_KEY)
    therefore fail signature verification against _REAL_KEY's public key.
    """
    fake = _FakeJWKSClient(_REAL_KEY.public_key())
    return patch.object(handler, "_get_jwks_client", return_value=fake)


def _sign(claims, key=_REAL_KEY, alg="RS256", headers=None):
    """Sign a JWT with the given RSA private key."""
    return jwt.encode(claims, key, algorithm=alg, headers=headers)


def _claims(groups=None, *, exp_in=3600, iss=_TEST_ISSUER, **extra):
    now = int(time.time())
    c = {"iss": iss, "iat": now, "exp": now + exp_in,
         "email": "test@example.com", "token_use": "id"}
    if groups is not None:
        c["cognito:groups"] = groups
    c.update(extra)
    return c


def _event(method="GET", path="/tenants", token=None):
    headers = {"x-api-key": "test"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return {
        "httpMethod": method, "resource": path, "headers": headers,
        "queryStringParameters": None, "pathParameters": None, "body": None,
    }


# ═══════════════════════════════════════════
# Signature verification — the core anti-forgery contract
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestSignatureVerification:
    def test_valid_admin_token_resolves_admin(self):
        tok = _sign(_claims(["admin"]))
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=tok)) == "admin"

    def test_valid_operator_token_resolves_operator(self):
        tok = _sign(_claims(["operator"]))
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=tok)) == "operator"

    def test_valid_viewer_token_resolves_viewer(self):
        tok = _sign(_claims(["viewer"]))
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=tok)) == "viewer"

    def test_multiple_groups_picks_highest(self):
        tok = _sign(_claims(["operator", "admin", "viewer"]))
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=tok)) == "admin"

    def test_unknown_group_falls_back_to_viewer(self):
        tok = _sign(_claims(["random-team"]))
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=tok)) == "viewer"

    # ── forgery / tampering must NOT grant privilege ──

    def test_forged_signature_admin_claim_denied(self):
        """Attacker signs an admin token with their OWN key → rejected → viewer.

        This is the exact pre-1.5.0 exploit. A correct verifier rejects it
        because the signature does not match the pool's (real) public key.
        """
        forged = _sign(_claims(["admin"]), key=_ATTACKER_KEY)
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=forged)) == "viewer"

    def test_alg_none_unsigned_admin_claim_denied(self):
        """The classic 'alg:none' bypass: unsigned token with admin claim."""
        import base64

        def b64(obj):
            return base64.urlsafe_b64encode(
                json.dumps(obj).encode()).rstrip(b"=").decode()
        unsigned = (b64({"alg": "none", "typ": "JWT"})
                    + "." + b64(_claims(["admin"])) + ".")
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=unsigned)) == "viewer"

    def test_expired_token_denied(self):
        """A correctly-signed but EXPIRED admin token → viewer."""
        tok = _sign(_claims(["admin"], exp_in=-60))  # expired 60s ago
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=tok)) == "viewer"

    def test_wrong_issuer_denied(self):
        """Signed by the real key but issued by a DIFFERENT pool → viewer."""
        tok = _sign(_claims(["admin"], iss="https://evil.example/pool"))
        with _install_real_jwks():
            assert handler._get_user_role(_event(token=tok)) == "viewer"

    def test_garbage_token_denied(self):
        with _install_real_jwks():
            assert handler._get_user_role(_event(token="not.a.jwt")) == "viewer"

    def test_wrong_audience_denied_when_client_id_pinned(self):
        """When COGNITO_CLIENT_ID is set, a token for another app → viewer."""
        tok = _sign(_claims(["admin"], aud="some-other-app-client"))
        with _install_real_jwks(), \
             patch.object(handler, "COGNITO_CLIENT_ID", "our-app-client"):
            assert handler._get_user_role(_event(token=tok)) == "viewer"


# ═══════════════════════════════════════════
# Fail-safe defaults (no token / verification unavailable)
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestFailSafeDefaults:
    def test_no_token_uses_default_no_jwt_role(self):
        """No Authorization header → DEFAULT_NO_JWT_ROLE (viewer), NOT admin.

        This is the inversion of the pre-1.5.0 fail-open behavior.
        """
        with patch.object(handler, "DEFAULT_NO_JWT_ROLE", "viewer"):
            assert handler._get_user_role(_event()) == "viewer"

    def test_no_token_default_is_configurable(self):
        """Trusted-automation deployments may set the default to admin."""
        with patch.object(handler, "DEFAULT_NO_JWT_ROLE", "admin"):
            assert handler._get_user_role(_event()) == "admin"

    def test_non_bearer_authorization_uses_default(self):
        ev = _event()
        ev["headers"]["Authorization"] = "Basic dXNlcjpwYXNz"
        with patch.object(handler, "DEFAULT_NO_JWT_ROLE", "viewer"):
            assert handler._get_user_role(ev) == "viewer"

    def test_verification_unavailable_token_present_denies(self):
        """If JWKS client can't be built (no pool id), a Bearer token → viewer.

        Even a real-looking token must not be trusted when we cannot verify.
        """
        tok = _sign(_claims(["admin"]))
        with patch.object(handler, "_get_jwks_client", return_value=None):
            assert handler._get_user_role(_event(token=tok)) == "viewer"


# ═══════════════════════════════════════════
# Role hierarchy (pure helper, unchanged by 1.5.0)
# ═══════════════════════════════════════════


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

    def test_role_from_claims_maps_highest(self):
        assert handler._role_from_claims({"cognito:groups": ["viewer", "admin"]}) == "admin"
        assert handler._role_from_claims({"cognito:groups": "operator"}) == "operator"
        assert handler._role_from_claims({}) == "viewer"


# ═══════════════════════════════════════════
# Handler-level enforcement (end-to-end through lambda_handler)
# ═══════════════════════════════════════════


def _role_event(method, path, role):
    """Event whose verified role is `role` (we patch _verify_and_decode here —
    enforcement wiring is the unit under test, not signature checking)."""
    return _event(method, path, token="dummy"), role


@pytest.mark.unit
class TestEndpointEnforcement:
    def _run(self, method, path, role, **ev_extra):
        ev = _event(method, path, token="dummy")
        ev.update(ev_extra)
        claims = {"cognito:groups": [role]} if role else {}
        with patch.object(handler, "_verify_and_decode", return_value=claims):
            return handler.lambda_handler(ev, None)

    def test_viewer_can_list_tenants(self):
        with patch.object(handler, "list_tenants",
                          return_value=handler._resp(200, {"items": []})):
            r = self._run("GET", "/tenants", "viewer")
        assert r["statusCode"] == 200

    def test_viewer_cannot_create_tenant(self):
        r = self._run("POST", "/tenants", "viewer", body=json.dumps({"name": "demo"}))
        assert r["statusCode"] == 403
        assert "forbidden" in r["body"].lower() or "rbac" in r["body"].lower()

    def test_operator_can_create_tenant(self):
        with patch.object(handler, "create_tenant",
                          return_value=handler._resp(201, {"id": "t1"})):
            r = self._run("POST", "/tenants", "operator", body=json.dumps({"name": "demo"}))
        assert r["statusCode"] == 201

    def test_viewer_cannot_delete_tenant(self):
        r = self._run("DELETE", "/tenants/{id}", "viewer",
                      pathParameters={"id": "t1"})
        assert r["statusCode"] == 403

    def test_viewer_cannot_run_action(self):
        r = self._run("POST", "/tenants/{id}/{action}", "viewer",
                      pathParameters={"id": "t1", "action": "restart"})
        assert r["statusCode"] == 403

    def test_forged_token_cannot_create_tenant(self):
        """End-to-end: a forged admin token is rejected → POST denied (403).

        Exercises the REAL verification path (no _verify_and_decode patch):
        attacker-signed token + real JWKS ⇒ downgraded to viewer ⇒ 403.
        """
        forged = _sign(_claims(["admin"]), key=_ATTACKER_KEY)
        ev = _event("POST", "/tenants", token=forged)
        ev["body"] = json.dumps({"name": "demo"})
        with _install_real_jwks():
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 403


# ═══════════════════════════════════════════
# CDK: Cognito role groups still provisioned
# ═══════════════════════════════════════════


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
