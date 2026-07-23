# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for tenant tagging (issue #10).

Covers:
- create_tenant accepts and persists tags
- create_tenant validates tag format and limits
- list_tenants supports ?tag=k:v filtering (single + multiple, AND semantics)
- get_tenant returns tags (defaulting to {} for legacy records)
- Tagging is fully optional — old callers unaffected
"""

import importlib.util
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

# ── Re-import handler with mocked AWS SDK (mirrors test_api.py pattern) ──
_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
_mock_s3 = MagicMock()
_mock_asg = MagicMock()
_mock_elbv2 = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm, "s3": _mock_s3, "autoscaling": _mock_asg,
        "elbv2": _mock_elbv2,
    }.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_tag", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_tag"] = api
    spec.loader.exec_module(api)


# Common host fixture
def _host_with_capacity():
    return {
        "instance_id": "i-test", "total_vcpu": 8, "total_mem_mb": 16384,
        "used_vcpu": 0, "used_mem_mb": 0, "status": "active",
        "next_vm_num": 1, "private_ip": "10.0.0.1", "rootfs_version": "v1.0",
    }


def _prep_host():
    """Set up a host with capacity. Reset both DDB tables."""
    api.tenants_table = make_ddb_table()
    api.hosts_table = make_ddb_table()
    api.hosts_table.scan.return_value = {"Items": [_host_with_capacity()]}


# ═══════════════════════════════════════════
# create_tenant — tag persistence
# ═══════════════════════════════════════════


class TestCreateTenantWithTags:
    @pytest.mark.unit
    def test_tags_persisted_to_ddb(self):
        """Tags from request body are stored on the tenant record."""
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"team": "ml", "env": "dev"},
        }))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved["tags"] == {"team": "ml", "env": "dev"}

    @pytest.mark.unit
    def test_tags_default_empty(self):
        """Caller without tags → empty dict (forward-compatible default)."""
        _prep_host()
        resp = api.create_tenant(json.dumps({"name": "t"}))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved.get("tags") == {}

    @pytest.mark.unit
    def test_tags_persisted_on_pending(self):
        """Pending tenant (no host capacity) also retains tags for later."""
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": []}  # no capacity
        _mock_asg.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 1, "MaxSize": 5}]}
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"team": "ml"},
        }))
        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        assert body["status"] == "pending"
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved["tags"] == {"team": "ml"}


# ═══════════════════════════════════════════
# create_tenant — validation
# ═══════════════════════════════════════════


class TestTagValidation:
    @pytest.mark.unit
    def test_tags_must_be_object(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": ["team", "ml"],  # array, not object
        }))
        assert resp["statusCode"] == 400
        assert "object" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_tag_key_with_colon_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"team:env": "ml"},
        }))
        assert resp["statusCode"] == 400
        assert "':'" in json.loads(resp["body"])["error"] \
               or "colon" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_tag_value_with_colon_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"team": "ml:dev"},
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_tag_key_too_long(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"x" * 51: "ml"},  # 51 chars
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_tag_value_too_long(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"team": "x" * 101},
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_too_many_tags(self):
        _prep_host()
        many = {f"k{i}": f"v{i}" for i in range(21)}  # 21 tags > 20 limit
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": many,
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_non_string_value_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"team": 123},  # not string
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_empty_key_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"": "ml"},
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_boundary_max_50_chars_key(self):
        """Exactly at the limit should pass."""
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"x" * 50: "ml"},
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_boundary_max_100_chars_value(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {"team": "x" * 100},
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_boundary_exactly_20_tags(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "tags": {f"k{i}": f"v{i}" for i in range(20)},
        }))
        assert resp["statusCode"] == 201


# ═══════════════════════════════════════════
# list_tenants — query filtering
# ═══════════════════════════════════════════


def _tenant(tid, tags=None, status="running"):
    item = {
        "id": tid, "name": tid, "status": status,
        "vcpu": 2, "mem_mb": 4096,
    }
    if tags is not None:
        item["tags"] = tags
    return item


class TestListTenantsTagFilter:
    @pytest.mark.unit
    def test_no_filter_returns_all(self):
        """Existing behavior — no query params returns all (regression)."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [
            _tenant("a", tags={"team": "ml"}),
            _tenant("b", tags={"team": "infra"}),
            _tenant("c"),
        ]}
        # No query string
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/tenants",
            "pathParameters": {}, "queryStringParameters": None,
        }, None)
        assert resp["statusCode"] == 200
        ids = [t["id"] for t in json.loads(resp["body"])]
        assert set(ids) == {"a", "b", "c"}

    @pytest.mark.unit
    def test_single_tag_filter(self):
        """?tag=team:ml — only matching tenants returned."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [
            _tenant("a", tags={"team": "ml"}),
            _tenant("b", tags={"team": "infra"}),
            _tenant("c"),
        ]}
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/tenants",
            "pathParameters": {},
            "queryStringParameters": {"tag": "team:ml"},
        }, None)
        assert resp["statusCode"] == 200
        ids = [t["id"] for t in json.loads(resp["body"])]
        assert ids == ["a"]

    @pytest.mark.unit
    def test_multi_tag_filter_and_semantics(self):
        """?tag=team:ml&tag=env:dev — AND semantics, all must match."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [
            _tenant("a", tags={"team": "ml", "env": "dev"}),
            _tenant("b", tags={"team": "ml", "env": "prod"}),
            _tenant("c", tags={"team": "ml"}),
        ]}
        # API Gateway delivers multi-value via multiValueQueryStringParameters
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/tenants",
            "pathParameters": {},
            "queryStringParameters": {"tag": "env:dev"},  # last-value form
            "multiValueQueryStringParameters": {"tag": ["team:ml", "env:dev"]},
        }, None)
        assert resp["statusCode"] == 200
        ids = [t["id"] for t in json.loads(resp["body"])]
        assert ids == ["a"]

    @pytest.mark.unit
    def test_filter_excludes_tenants_without_tag(self):
        """Tenants with no tags field → excluded when filter is set."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [
            _tenant("a", tags={"team": "ml"}),
            _tenant("legacy"),  # no tags field
        ]}
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/tenants",
            "pathParameters": {},
            "queryStringParameters": {"tag": "team:ml"},
        }, None)
        ids = [t["id"] for t in json.loads(resp["body"])]
        assert ids == ["a"]

    @pytest.mark.unit
    def test_filter_no_match_returns_empty(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [
            _tenant("a", tags={"team": "ml"}),
        ]}
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/tenants",
            "pathParameters": {},
            "queryStringParameters": {"tag": "team:nope"},
        }, None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == []

    @pytest.mark.unit
    def test_filter_malformed_ignored(self):
        """tag without colon — silently treated as no match (defensive)."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [
            _tenant("a", tags={"team": "ml"}),
        ]}
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/tenants",
            "pathParameters": {},
            "queryStringParameters": {"tag": "noColon"},
        }, None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == []


# ═══════════════════════════════════════════
# get_tenant — tags in response
# ═══════════════════════════════════════════


class TestGetTenantWithTags:
    @pytest.mark.unit
    def test_existing_tags_returned(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": {
            "id": "a", "name": "a", "status": "running",
            "tags": {"team": "ml"},
        }}
        resp = api.get_tenant("a")
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["tags"] == {"team": "ml"}

    @pytest.mark.unit
    def test_legacy_no_tags_field_returns_empty_dict(self):
        """Legacy tenants without tags field → response has tags: {}."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": {
            "id": "a", "name": "a", "status": "running",
        }}
        resp = api.get_tenant("a")
        body = json.loads(resp["body"])
        assert body.get("tags") == {}


# ═══════════════════════════════════════════
# Backward compatibility regression
# ═══════════════════════════════════════════


class TestBackwardCompatibility:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_create_without_tags_still_works(self):
        """Legacy callers (no tags field at all) get same behavior."""
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "t", "vcpu": 2, "mem_mb": 4096,
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    @pytest.mark.regression
    def test_list_returns_tags_when_present(self):
        """Listing should expose tags so console can render them."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [
            {"id": "a", "name": "a", "status": "running",
             "tags": {"team": "ml"}},
        ]}
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/tenants",
            "pathParameters": {}, "queryStringParameters": None,
        }, None)
        body = json.loads(resp["body"])
        assert body[0]["tags"] == {"team": "ml"}
