# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for deploy/lambda/skills/handler.py (T2-1 zero-coverage gap).

The skills Lambda serves GET /skills — the catalog + per-tenant scoped view
(tenant.skills ∪ group.skills). Previously had ZERO coverage.
"""

import importlib.util
import io
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

os.environ.setdefault("ASSETS_BUCKET", "test-bucket")
os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
os.environ.setdefault("GROUPS_TABLE", "openclaw-groups")

_mock_s3 = MagicMock()
_mock_ddb = MagicMock()
with patch("boto3.client", return_value=_mock_s3), \
     patch("boto3.resource", return_value=_mock_ddb):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "skills_handler", "deploy/lambda/skills/handler.py")
    skills = importlib.util.module_from_spec(spec)
    sys.modules["skills_handler"] = skills
    spec.loader.exec_module(skills)

pytestmark = pytest.mark.unit


def _skill_md(desc):
    return {"Body": io.BytesIO(f"---\ndescription: {desc}\n---\n# body".encode())}


def _list_prefixes(*names):
    return {"CommonPrefixes": [{"Prefix": f"skills/{n}/"} for n in names]}


import json as _json


def _body(resp):
    return _json.loads(resp["body"])


class TestListSkillsCatalog:
    def setup_method(self):
        skills.s3 = MagicMock()
        skills.tenants_table = make_ddb_table()
        skills.groups_table = make_ddb_table()

    def test_lists_all_skills_broadcast(self):
        skills.s3.list_objects_v2.return_value = _list_prefixes("k8s", "incident")
        skills.s3.get_object.side_effect = lambda **k: _skill_md("a skill")
        resp = skills.lambda_handler({"httpMethod": "GET", "path": "/skills"}, None)
        assert resp["statusCode"] == 200
        names = {s["name"] for s in _body(resp)["skills"]}
        assert names == {"k8s", "incident"}

    def test_unknown_route_404(self):
        resp = skills.lambda_handler({"httpMethod": "POST", "path": "/skills"}, None)
        assert resp["statusCode"] == 404

    def test_s3_error_returns_500(self):
        skills.s3.list_objects_v2.side_effect = RuntimeError("s3 down")
        resp = skills.lambda_handler({"httpMethod": "GET", "path": "/skills"}, None)
        assert resp["statusCode"] == 500


class TestPerTenantScoping:
    def setup_method(self):
        skills.s3 = MagicMock()
        skills.s3.list_objects_v2.return_value = _list_prefixes("k8s", "incident", "debug")
        skills.s3.get_object.side_effect = lambda **k: _skill_md("d")
        skills.tenants_table = make_ddb_table()
        skills.groups_table = make_ddb_table()

    def test_unknown_tenant_returns_404(self):
        skills.tenants_table.get_item.return_value = {}
        resp = skills.lambda_handler(
            {"httpMethod": "GET", "path": "/skills",
             "queryStringParameters": {"tenant": "ghost"}}, None)
        assert resp["statusCode"] == 404

    def test_tenant_with_skills_is_scoped(self):
        skills.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "skills": ["k8s"]}}
        resp = skills.lambda_handler(
            {"httpMethod": "GET", "path": "/skills",
             "queryStringParameters": {"tenant": "t1"}}, None)
        b = _body(resp)
        assert b["scope"] == "scoped"
        assert {s["name"] for s in b["skills"]} == {"k8s"}

    def test_tenant_without_scope_is_broadcast(self):
        skills.tenants_table.get_item.return_value = {"Item": {"id": "t1"}}
        resp = skills.lambda_handler(
            {"httpMethod": "GET", "path": "/skills",
             "queryStringParameters": {"tenant": "t1"}}, None)
        b = _body(resp)
        assert b["scope"] == "broadcast"
        assert len(b["skills"]) == 3  # all skills

    def test_group_skills_union(self):
        skills.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "skills": ["k8s"], "group": "sre"}}
        skills.groups_table.get_item.return_value = {
            "Item": {"name": "sre", "skills": ["incident"]}}
        resp = skills.lambda_handler(
            {"httpMethod": "GET", "path": "/skills",
             "queryStringParameters": {"tenant": "t1"}}, None)
        assert {s["name"] for s in _body(resp)["skills"]} == {"k8s", "incident"}


class TestSkillDescriptionParse:
    def setup_method(self):
        skills.s3 = MagicMock()

    def test_reads_frontmatter_description(self):
        skills.s3.get_object.return_value = _skill_md("does the thing")
        assert skills._read_skill_description("k8s") == "does the thing"

    def test_missing_skill_md_returns_empty(self):
        skills.s3.get_object.side_effect = RuntimeError("no such key")
        assert skills._read_skill_description("k8s") == ""
