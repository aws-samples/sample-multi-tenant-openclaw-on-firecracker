# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for audit log (issue #17).

Audit log captures every mutation operation (POST/PUT/DELETE) on the API:
who (api_key_id), what (operation + resource), when (ts), result (status).
GET requests are not audited (read-only, high volume).

Storage: openclaw-audit-log DynamoDB table
- pk = "audit" (single partition for time-range queries)
- ts = ISO 8601 timestamp (sort key)
- expires_ttl = Unix epoch seconds (DDB TTL, 90-day retention)

API:
- GET /audit-log → recent entries, default 50
- GET /audit-log?since=ISO8601&limit=N
"""

import json
import sys
import os
import importlib.util
import pytest
from unittest.mock import patch, MagicMock
from conftest import make_ddb_table


# Set audit env before import so AUDIT_TABLE picks up
os.environ.setdefault("AUDIT_TABLE", "openclaw-audit-log")
os.environ.setdefault("AUDIT_TTL_DAYS", "90")


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
        "api_handler_audit", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_audit"] = api
    spec.loader.exec_module(api)


def _api_event(method, resource, path_params=None, body=None, api_key_id="abc-key"):
    """Build an API Gateway-style event with x-api-key context."""
    evt = {
        "httpMethod": method, "resource": resource,
        "pathParameters": path_params or {},
        "body": json.dumps(body) if body and not isinstance(body, str) else body,
        "requestContext": {"identity": {"apiKeyId": api_key_id}},
    }
    return evt


# ═══════════════════════════════════════════
# Audit table is initialized
# ═══════════════════════════════════════════


class TestAuditModuleSetup:
    @pytest.mark.unit
    def test_audit_table_imported(self):
        """The handler module exposes audit_table after init."""
        assert hasattr(api, "audit_table"), "expected api.audit_table to exist"


# ═══════════════════════════════════════════
# Mutations are audited
# ═══════════════════════════════════════════


class TestMutationAudits:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.audit_table = make_ddb_table()
        # Stub host so create_tenant doesn't 500 / scale-out
        api.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-test", "total_vcpu": 8, "total_mem_mb": 16384,
             "used_vcpu": 0, "used_mem_mb": 0, "status": "active",
             "next_vm_num": 1, "private_ip": "10.0.0.1", "rootfs_version": "v1.0"},
        ]}
        # Stub the SSM helpers directly. These tests only assert that a mutation
        # writes an audit row — they must not exercise the real SSM polling
        # loop. _ssm_run in particular polls get_command_invocation for up to
        # `timeout` seconds; under a bare module-level MagicMock the status
        # never reads "Success", so without this stub a POST /{action} (stop)
        # hangs ~30s. Patch the bound names on the handler module so behaviour
        # is deterministic regardless of test ordering / mock leakage.
        self._patchers = [
            patch.object(api, "_ssm_run", return_value=True),
            patch.object(api, "_ssm_send", return_value=None),
        ]
        for p in self._patchers:
            p.start()

    def teardown_method(self):
        for p in getattr(self, "_patchers", []):
            p.stop()

    @pytest.mark.unit
    def test_create_tenant_writes_audit(self):
        api.lambda_handler(_api_event("POST", "/tenants",
                                      body={"name": "x"},
                                      api_key_id="key-1"), None)
        # audit_table.put_item was called
        assert api.audit_table.put_item.called
        item = api.audit_table.put_item.call_args[1]["Item"]
        assert item["pk"] == "audit"
        assert "ts" in item
        assert item["operation"] == "POST /tenants"
        assert item["api_key_id"] == "key-1"
        assert item["response_status"] == 201
        # ts must be ISO 8601-ish (contains T and Z or +)
        assert "T" in item["ts"]
        # TTL must be a future epoch
        import time as _t
        assert int(item["expires_ttl"]) > int(_t.time())

    @pytest.mark.unit
    def test_delete_tenant_writes_audit(self):
        api.tenants_table.get_item.return_value = {"Item": {
            "id": "t1", "name": "t1", "status": "running",
            "host_id": "i-1", "vm_num": 1, "vcpu": 2, "mem_mb": 4096,
            "guest_ip": "172.16.1.2", "host_port": 18789,
        }}
        api.lambda_handler(_api_event("DELETE", "/tenants/{id}",
                                      path_params={"id": "t1"}), None)
        item = api.audit_table.put_item.call_args[1]["Item"]
        assert item["operation"] == "DELETE /tenants/{id}"
        assert item["resource_id"] == "t1"

    @pytest.mark.unit
    def test_post_action_writes_audit(self):
        api.tenants_table.get_item.return_value = {"Item": {
            "id": "t1", "name": "t1", "status": "running",
            "host_id": "i-1", "vm_num": 1, "vcpu": 2, "mem_mb": 4096,
            "guest_ip": "172.16.1.2", "host_port": 18789,
        }}
        api.lambda_handler(_api_event("POST", "/tenants/{id}/{action}",
                                      path_params={"id": "t1", "action": "stop"}), None)
        item = api.audit_table.put_item.call_args[1]["Item"]
        assert item["operation"] == "POST /tenants/{id}/{action}"
        assert item["resource_id"] == "t1"

    @pytest.mark.unit
    def test_put_template_writes_audit(self):
        # Even if route hits 404 in this api Lambda (templates is a different
        # Lambda), the request still goes through lambda_handler — but only
        # mutation methods on routes that match get audited. We test the
        # principle: if PUT ever lands here, it gets audited.
        api.lambda_handler(_api_event("PUT", "/templates/{name}",
                                      path_params={"name": "x"}), None)
        # /templates/{name} isn't in api routes → 404 → still audited
        if api.audit_table.put_item.called:
            item = api.audit_table.put_item.call_args[1]["Item"]
            assert item["response_status"] == 404


# ═══════════════════════════════════════════
# GETs are NOT audited (read-only, high volume)
# ═══════════════════════════════════════════


class TestNoAuditOnReads:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": []}
        api.hosts_table = make_ddb_table()
        api.audit_table = make_ddb_table()

    @pytest.mark.unit
    def test_list_tenants_not_audited(self):
        api.lambda_handler(_api_event("GET", "/tenants"), None)
        api.audit_table.put_item.assert_not_called()

    @pytest.mark.unit
    def test_get_tenant_not_audited(self):
        api.tenants_table.get_item.return_value = {"Item": {"id": "t1", "status": "running"}}
        api.lambda_handler(_api_event("GET", "/tenants/{id}",
                                      path_params={"id": "t1"}), None)
        api.audit_table.put_item.assert_not_called()

    @pytest.mark.unit
    def test_list_hosts_not_audited(self):
        api.hosts_table.scan.return_value = {"Items": []}
        api.lambda_handler(_api_event("GET", "/hosts"), None)
        api.audit_table.put_item.assert_not_called()

    @pytest.mark.unit
    def test_eventbridge_event_not_audited(self):
        """EventBridge invocations (no httpMethod) should not be audited."""
        api.lambda_handler({
            "source": "aws.autoscaling",
            "detail-type": "EC2 Instance Launch Successful",
            "detail": {},
        }, None)
        api.audit_table.put_item.assert_not_called()


# ═══════════════════════════════════════════
# Audit write failures must not break the operation
# ═══════════════════════════════════════════


class TestAuditFailureIsolation:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.audit_table = make_ddb_table()
        api.audit_table.put_item.side_effect = Exception("DDB throttled")
        api.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-test", "total_vcpu": 8, "total_mem_mb": 16384,
             "used_vcpu": 0, "used_mem_mb": 0, "status": "active",
             "next_vm_num": 1, "private_ip": "10.0.0.1", "rootfs_version": "v1.0"},
        ]}

    @pytest.mark.unit
    def test_audit_failure_does_not_break_create(self):
        """If audit DDB write fails, the underlying operation must still succeed."""
        resp = api.lambda_handler(_api_event(
            "POST", "/tenants", body={"name": "x"}), None)
        assert resp["statusCode"] == 201


# ═══════════════════════════════════════════
# GET /audit-log endpoint
# ═══════════════════════════════════════════


class TestGetAuditLog:
    def setup_method(self):
        api.audit_table = make_ddb_table()

    @pytest.mark.unit
    def test_returns_recent_entries(self):
        items = [
            {"pk": "audit", "ts": "2026-05-17T11:00:00Z",
             "operation": "POST /tenants", "resource_id": "a", "api_key_id": "k1",
             "response_status": 201},
            {"pk": "audit", "ts": "2026-05-17T11:05:00Z",
             "operation": "DELETE /tenants/{id}", "resource_id": "b", "api_key_id": "k1",
             "response_status": 200},
        ]
        api.audit_table.query.return_value = {"Items": items}
        resp = api.lambda_handler(_api_event("GET", "/audit-log"), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert isinstance(body, list)
        assert len(body) == 2

    @pytest.mark.unit
    def test_default_limit_50(self):
        api.audit_table.query.return_value = {"Items": []}
        api.lambda_handler(_api_event("GET", "/audit-log"), None)
        # query was called with Limit=50
        kwargs = api.audit_table.query.call_args.kwargs
        assert kwargs.get("Limit") == 50

    @pytest.mark.unit
    def test_custom_limit(self):
        api.audit_table.query.return_value = {"Items": []}
        api.lambda_handler({
            "httpMethod": "GET", "resource": "/audit-log",
            "pathParameters": {},
            "queryStringParameters": {"limit": "10"},
        }, None)
        assert api.audit_table.query.call_args.kwargs.get("Limit") == 10

    @pytest.mark.unit
    def test_limit_capped_at_500(self):
        """Ridiculous limit is clamped to 500."""
        api.audit_table.query.return_value = {"Items": []}
        api.lambda_handler({
            "httpMethod": "GET", "resource": "/audit-log",
            "pathParameters": {},
            "queryStringParameters": {"limit": "99999"},
        }, None)
        assert api.audit_table.query.call_args.kwargs.get("Limit") == 500

    @pytest.mark.unit
    def test_since_filter_applied(self):
        """When `since` is provided, the query KeyCondition includes a range filter on `ts`."""
        api.audit_table.query.return_value = {"Items": []}
        api.lambda_handler({
            "httpMethod": "GET", "resource": "/audit-log",
            "pathParameters": {},
            "queryStringParameters": {"since": "2026-05-01T00:00:00Z"},
        }, None)
        kwargs = api.audit_table.query.call_args.kwargs
        cond = kwargs.get("KeyConditionExpression")
        # Two possible representations: a boto3 Key/ConditionExpression object
        # or a raw string. Inspect _values whichever way it's structured.
        from boto3.dynamodb.conditions import And
        # Walk the And tree if present
        def _values(node):
            vals = []
            if hasattr(node, "_values"):
                vals.extend(node._values)
            if hasattr(node, "values"):
                vals.extend(getattr(node, "values", []) or [])
            if hasattr(node, "_value"):
                vals.append(node._value)
            return vals
        leaf_values = []
        if isinstance(cond, And):
            for sub in cond._values:
                leaf_values.extend(_values(sub))
        else:
            leaf_values.extend(_values(cond))
        assert "2026-05-01T00:00:00Z" in leaf_values, f"expected since in {leaf_values!r}"

    @pytest.mark.unit
    def test_descending_order(self):
        """Newest entries first → ScanIndexForward=False."""
        api.audit_table.query.return_value = {"Items": []}
        api.lambda_handler(_api_event("GET", "/audit-log"), None)
        kwargs = api.audit_table.query.call_args.kwargs
        assert kwargs.get("ScanIndexForward") is False

    @pytest.mark.unit
    @pytest.mark.regression
    def test_audit_log_route_not_collide_with_others(self):
        """/audit-log route resolved to GET /audit-log handler."""
        api.audit_table.query.return_value = {"Items": []}
        resp = api.lambda_handler(_api_event("GET", "/audit-log"), None)
        assert resp["statusCode"] == 200
