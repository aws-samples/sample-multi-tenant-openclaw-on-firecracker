# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Self-service: a logged-in user provisions their OWN openclaw node via
POST /tenants/self. Verifies: requires a real logged-in user (not api-key /
unverified), forces owner to the caller, enforces the per-user cap, refuses
when authority is external, and otherwise delegates to create_tenant.
"""

import importlib.util
import json
import sys
from unittest.mock import MagicMock, patch

from conftest import make_ddb_table
import pytest

# All tests in this module are pure-mock unit tests (no real AWS); mark them
# so `pytest -m unit` includes them (loop 2026-07-02: found 136 tests were
# silently excluded from the unit suite for lack of this marker).
pytestmark = pytest.mark.unit

_mock_ddb = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: MagicMock()
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_self", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_self"] = api
    spec.loader.exec_module(api)


def _user_event():
    # a verified Cognito user (Bearer token present + decodes)
    return {"headers": {"Authorization": "Bearer x"}}


class TestSelfProvision:
    def test_requires_logged_in_user_not_apikey(self):
        # api-key path → is_admin/api_key_only → self-service refused (401)
        with patch.object(
            api,
            "_get_caller_identity",
            return_value={"owner_id": api.API_KEY_OWNER, "api_key_only": True},
        ):
            r = api.create_tenant_self("{}", {"headers": {}})
        assert r["statusCode"] == 401

    def test_external_authz_disables_self(self):
        with (
            patch.object(api, "EXTERNAL_AUTHZ", True),
            patch.object(
                api,
                "_get_caller_identity",
                return_value={"owner_id": "sub-bob", "api_key_only": False},
            ),
        ):
            r = api.create_tenant_self("{}", _user_event())
        assert r["statusCode"] == 403

    def test_cap_blocks_when_at_limit(self):
        with (
            patch.object(api, "EXTERNAL_AUTHZ", False),
            patch.object(api, "SELF_MAX_NODES_PER_USER", 1),
            patch.object(
                api,
                "_get_caller_identity",
                return_value={"owner_id": "sub-bob", "api_key_only": False},
            ),
            patch.object(api, "_count_owner_tenants", return_value=1),
        ):
            r = api.create_tenant_self("{}", _user_event())
        assert r["statusCode"] == 409

    def test_under_cap_delegates_to_create_tenant(self):
        captured = {}

        def _fake_create(body, event):
            captured["body"] = body
            return {"statusCode": 201, "body": json.dumps({"id": "u-bob-ab12"})}

        with (
            patch.object(api, "EXTERNAL_AUTHZ", False),
            patch.object(api, "SELF_MAX_NODES_PER_USER", 1),
            patch.object(
                api,
                "_get_caller_identity",
                return_value={"owner_id": "sub-bob", "api_key_only": False},
            ),
            patch.object(api, "_count_owner_tenants", return_value=0),
            patch.object(api, "create_tenant", side_effect=_fake_create),
        ):
            r = api.create_tenant_self("{}", _user_event())
        assert r["statusCode"] == 201
        # a default per-user name was injected, owner fields stripped
        assert captured["body"]["name"].startswith("u-")
        assert "owner_id" not in captured["body"] and "owner" not in captured["body"]

    def test_caller_cannot_set_owner_for_someone_else(self):
        captured = {}

        def _fake_create(body, event):
            captured["body"] = body
            return {"statusCode": 201, "body": "{}"}

        with (
            patch.object(api, "EXTERNAL_AUTHZ", False),
            patch.object(api, "SELF_MAX_NODES_PER_USER", 0),  # unlimited
            patch.object(
                api,
                "_get_caller_identity",
                return_value={"owner_id": "sub-bob", "api_key_only": False},
            ),
            patch.object(api, "create_tenant", side_effect=_fake_create),
        ):
            # attacker tries to smuggle owner_id for another user
            r = api.create_tenant_self(
                json.dumps(
                    {"owner_id": "sub-alice", "owner": "sub-alice", "name": "x"}
                ),
                _user_event(),
            )
        assert r["statusCode"] == 201
        # owner fields must be stripped — create_tenant derives owner from the
        # verified identity (sub-bob), never the body
        assert "owner_id" not in captured["body"]
        assert "owner" not in captured["body"]

    def test_self_route_in_viewer_allowlist(self):
        # RBAC gate must let any verified user reach the handler (which then does
        # its own self-only + cap checks)
        assert ("POST", "/tenants/self") in api._VIEWER_OK
