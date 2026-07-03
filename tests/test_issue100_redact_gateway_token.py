# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for #100 — GET /tenants must not leak gateway_token (credential exposure).

真机探测(API-PROBE-2026-07-03)坐实 GET /tenants 列表明文返回每租户 gateway_token,
凭一个 x-api-key 可一次拉走全部租户的 gateway_token。修:把 gateway_token 加进
_TENANT_SECRET_FIELDS,由 _redact_tenant 单一 choke point 剥离。
"""

import importlib.util
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
        "api_handler_i100", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_i100"] = api
    spec.loader.exec_module(api)


class TestGatewayTokenRedaction:
    def test_gateway_token_in_secret_fields(self):
        assert "gateway_token" in api._TENANT_SECRET_FIELDS

    def test_redact_strips_gateway_token(self):
        item = {
            "id": "t1",
            "name": "acme",
            "status": "running",
            "gateway_token": "gwt-super-secret",
            "guest_ip": "172.16.0.5",
        }
        red = api._redact_tenant(item)
        assert "gateway_token" not in red
        # non-secret fields survive
        assert red["id"] == "t1" and red["status"] == "running"
        assert red["guest_ip"] == "172.16.0.5"

    def test_all_secrets_stripped_together(self):
        item = {
            "id": "t2",
            "gateway_token": "g",
            "channel_secret": "c",
            "litellm_vkey": "sk-x",
            "cognito_channel_password": "p",
            "name": "keep",
        }
        red = api._redact_tenant(item)
        for secret in (
            "gateway_token",
            "channel_secret",
            "litellm_vkey",
            "cognito_channel_password",
        ):
            assert secret not in red, f"{secret} leaked"
        assert red["name"] == "keep"

    def test_redact_non_dict_passthrough(self):
        assert api._redact_tenant("not-a-dict") == "not-a-dict"

    def test_redact_absent_gateway_token_ok(self):
        # tenant record without gateway_token still redacts cleanly
        red = api._redact_tenant({"id": "t3", "name": "n"})
        assert red == {"id": "t3", "name": "n"}
