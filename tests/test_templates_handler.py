# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for deploy/lambda/templates/handler.py (T2-1 zero-coverage gap).

The templates Lambda is CRUD over config templates in S3, with the `default`
template protected as read-only. Previously had ZERO coverage.
"""

import importlib.util
import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ASSETS_BUCKET", "test-bucket")

_mock_s3 = MagicMock()
with patch("boto3.client", return_value=_mock_s3):
    spec = importlib.util.spec_from_file_location(
        "templates_handler", "deploy/lambda/templates/handler.py")
    templates = importlib.util.module_from_spec(spec)
    sys.modules["templates_handler"] = templates
    spec.loader.exec_module(templates)

pytestmark = pytest.mark.unit


def _body(resp):
    return json.loads(resp["body"])


def _evt(method, name=None, body=None):
    return {"httpMethod": method,
            "pathParameters": {"name": name} if name else {},
            "body": body}


class TestListTemplates:
    def setup_method(self):
        templates.s3 = MagicMock()

    def test_lists_with_metadata(self):
        import datetime
        templates.s3.list_objects_v2.return_value = {
            "CommonPrefixes": [{"Prefix": "templates/openclaw/bedrock/"}]}
        templates.s3.head_object.return_value = {
            "ContentLength": 512, "LastModified": datetime.datetime(2026, 1, 1)}
        resp = templates.lambda_handler(_evt("GET"), None)
        assert resp["statusCode"] == 200
        t = _body(resp)["templates"][0]
        assert t["name"] == "bedrock" and t["size"] == 512

    def test_missing_openclaw_json_tolerated(self):
        templates.s3.list_objects_v2.return_value = {
            "CommonPrefixes": [{"Prefix": "templates/openclaw/partial/"}]}
        templates.s3.head_object.side_effect = RuntimeError("404")
        resp = templates.lambda_handler(_evt("GET"), None)
        assert resp["statusCode"] == 200
        assert _body(resp)["templates"][0]["size"] == 0


class TestGetTemplate:
    def setup_method(self):
        templates.s3 = MagicMock()
        templates.s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

    def test_returns_parsed_content(self):
        templates.s3.get_object.return_value = {
            "Body": io.BytesIO(b'{"model":"claude"}')}
        resp = templates.lambda_handler(_evt("GET", "bedrock"), None)
        assert resp["statusCode"] == 200
        assert _body(resp)["content"] == {"model": "claude"}

    def test_missing_returns_404(self):
        templates.s3.get_object.side_effect = templates.s3.exceptions.NoSuchKey()
        resp = templates.lambda_handler(_evt("GET", "ghost"), None)
        assert resp["statusCode"] == 404


class TestPutTemplate:
    def setup_method(self):
        templates.s3 = MagicMock()

    def test_default_is_read_only(self):
        resp = templates.lambda_handler(_evt("PUT", "default", '{"x":1}'), None)
        assert resp["statusCode"] == 403
        assert not templates.s3.put_object.called

    def test_saves_valid_json(self):
        resp = templates.lambda_handler(_evt("PUT", "custom", '{"x":1}'), None)
        assert resp["statusCode"] == 200
        assert _body(resp)["status"] == "saved"
        assert templates.s3.put_object.called

    def test_invalid_json_returns_400(self):
        resp = templates.lambda_handler(_evt("PUT", "custom", "not-json{"), None)
        assert resp["statusCode"] == 400
        assert not templates.s3.put_object.called


class TestDeleteTemplate:
    def setup_method(self):
        templates.s3 = MagicMock()

    def test_default_is_protected(self):
        resp = templates.lambda_handler(_evt("DELETE", "default"), None)
        assert resp["statusCode"] == 403
        assert not templates.s3.delete_object.called

    def test_deletes_custom(self):
        resp = templates.lambda_handler(_evt("DELETE", "custom"), None)
        assert resp["statusCode"] == 200
        assert templates.s3.delete_object.called


class TestUnknownRoute:
    def test_post_without_name_404(self):
        templates.s3 = MagicMock()
        resp = templates.lambda_handler(_evt("POST"), None)
        assert resp["statusCode"] == 404
