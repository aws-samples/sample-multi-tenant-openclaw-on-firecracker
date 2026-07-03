# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""10h-goal #19: golden-image inventory + per-tenant data snapshot for the
console. GET /images lists S3 rootfs artifacts + live manifest version (no
bytes). GET /tenants/{id}/{action=data} returns the control-plane's metadata
view of a tenant — IDOR-guarded, billing vkey shown as boolean only (never the
value), no guest secrets.
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
_mock_s3 = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: _mock_s3 if svc == "s3" else MagicMock()
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_imgdata", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_imgdata"] = api
    spec.loader.exec_module(api)


def _admin_event():
    return {"headers": {}}


class TestListImages:
    def test_lists_artifacts_with_live_version(self):
        api.s3 = MagicMock()
        # _get_manifest reads manifest.json
        api.s3.get_object.return_value = {
            "Body": MagicMock(
                read=lambda: json.dumps(
                    {"version": "v13-arm64-claw-hardened"}
                ).encode()
            )
        }

        class _Dt:
            def isoformat(self):
                return "2026-06-27T00:00:00"

        api.s3.get_paginator.return_value.paginate.return_value = [
            {
                "Contents": [
                    {
                        "Key": "deployment/rootfs/openclaw-data-template-v13-arm64-claw-hardened.ext4.gz",
                        "Size": 9529808,
                        "LastModified": _Dt(),
                    },
                    {
                        "Key": "deployment/rootfs/golden-image.sha256",
                        "Size": 2048,
                        "LastModified": _Dt(),
                    },
                    {
                        "Key": "deployment/rootfs/manifest.json",
                        "Size": 235,
                        "LastModified": _Dt(),
                    },
                    {
                        "Key": "deployment/rootfs/manifest.json.bak-v1.0",
                        "Size": 113,
                        "LastModified": _Dt(),
                    },
                ]
            }
        ]
        with patch.dict(
            api.os.environ, {"ASSETS_BUCKET": "b", "ROOTFS_PREFIX": "deployment/rootfs"}
        ):
            r = api.list_images({})
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["live_version"] == "v13-arm64-claw-hardened"
        assert body["artifact_count"] == 4
        kinds = {a["name"]: a["kind"] for a in body["artifacts"]}
        assert kinds["golden-image.sha256"] == "integrity-baseline"
        assert any(a["is_backup"] for a in body["artifacts"])  # the .bak one
        # never leaks bytes — only inventory
        assert all("body" not in a and "content" not in a for a in body["artifacts"])

    def test_no_bucket_503(self):
        with patch.dict(api.os.environ, {"ASSETS_BUCKET": ""}, clear=False):
            # ensure empty
            api.os.environ["ASSETS_BUCKET"] = ""
            r = api.list_images({})
        assert r["statusCode"] == 503


class TestTenantData:
    def test_data_snapshot_metadata_only(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {
            "Item": {
                "id": "u-bob",
                "owner_id": "api-key",
                "status": "running",
                "host_id": "i-h",
                "guest_ip": "172.16.0.5",
                "vcpu": 2,
                "litellm_vkey": "sk-SECRET-should-not-leak",
            }
        }
        api.s3 = MagicMock()
        api.s3.list_objects_v2.return_value = {"KeyCount": 3}
        with (
            patch.object(api, "RBAC_ENABLED", False),
            patch.dict(
                api.os.environ, {"ASSETS_BUCKET": "b", "BACKUP_PREFIX": "backups"}
            ),
        ):
            r = api.tenant_get_action("u-bob", "data", _admin_event())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["status"] == "running"
        assert body["backup_count"] == 3
        # CRITICAL: vkey value never leaked — only a boolean
        assert body["has_billing_vkey"] is True
        assert "litellm_vkey" not in body
        assert "sk-SECRET" not in json.dumps(body)

    def test_data_idor_guarded(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {
            "Item": {"id": "t-alice", "owner_id": "sub-alice", "status": "running"}
        }
        with (
            patch.object(api, "RBAC_ENABLED", True),
            patch.object(
                api,
                "_get_caller_identity",
                return_value={
                    "owner_id": "sub-bob",
                    "is_admin": False,
                    "api_key_only": False,
                },
            ),
        ):
            r = api.tenant_get_action(
                "t-alice", "data", {"headers": {"Authorization": "Bearer x"}}
            )
        assert r["statusCode"] in (403, 404)  # owner mismatch → denied

    def test_data_route_in_viewer_allowlist(self):
        # GET /tenants/{id}/{action} is already viewer-level; data rides it
        assert ("GET", "/tenants/{id}/{action}") in api._VIEWER_OK
        assert ("GET", "/images") in api._VIEWER_OK
