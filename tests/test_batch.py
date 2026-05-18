# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for batch tenant operations (issue #23).

Endpoint: POST /batch/tenants
Body:
    {"action": "stop|start|delete|backup",
     "ids": ["t1","t2"]              ← exactly one of ids / filter
     "filter": {"tag": "team:ml"}}
Response:
    {"succeeded": [{"id":..., "action":...}, ...],
     "failed":    [{"id":..., "error":...}, ...]}

Covers:
- action whitelist + single source-of-tenants validation
- per-tenant errors are isolated (one failure doesn't abort the batch)
- delete / stop / start / backup all dispatch correctly
- filter by tag selects tenants matching every k:v pair
- Empty selections return 200 with empty arrays (not 400)
"""

import json
import sys
import importlib.util
import pytest
from unittest.mock import patch, MagicMock
from conftest import make_ddb_table


_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
_mock_s3 = MagicMock()
_mock_asg = MagicMock()
_mock_elbv2 = MagicMock()
_mock_lambda = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm, "s3": _mock_s3, "autoscaling": _mock_asg,
        "elbv2": _mock_elbv2, "lambda": _mock_lambda,
    }.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_batch", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_batch"] = api
    spec.loader.exec_module(api)


def _running_tenant(tid, tags=None):
    item = {
        "id": tid, "name": tid, "status": "running",
        "vcpu": 2, "mem_mb": 4096,
        "host_id": "i-1", "vm_num": 1,
        "guest_ip": "172.16.1.2", "host_port": 18789,
    }
    if tags is not None:
        item["tags"] = tags
    return item


def _invoke_batch(body):
    """Invoke the lambda_handler routing the POST /batch/tenants endpoint."""
    return api.lambda_handler({
        "httpMethod": "POST", "resource": "/batch/tenants",
        "pathParameters": {},
        "body": json.dumps(body),
    }, None)


# ═══════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════


class TestBatchValidation:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": []}

    @pytest.mark.unit
    def test_unknown_action_rejected(self):
        resp = _invoke_batch({"action": "explode", "ids": ["t1"]})
        assert resp["statusCode"] == 400
        assert "action" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_neither_ids_nor_filter_rejected(self):
        resp = _invoke_batch({"action": "stop"})
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_both_ids_and_filter_rejected(self):
        resp = _invoke_batch({
            "action": "stop", "ids": ["t1"], "filter": {"tag": "team:ml"},
        })
        assert resp["statusCode"] == 400
        assert "exactly one" in json.loads(resp["body"])["error"].lower() \
               or "both" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_too_many_ids_rejected(self):
        ids = [f"t{i}" for i in range(101)]
        resp = _invoke_batch({"action": "stop", "ids": ids})
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_empty_ids_returns_200_empty_arrays(self):
        """Edge: explicit empty ids list is fine (idempotent batch)."""
        resp = _invoke_batch({"action": "stop", "ids": []})
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body == {"succeeded": [], "failed": []}

    @pytest.mark.unit
    def test_missing_action_rejected(self):
        resp = _invoke_batch({"ids": ["t1"]})
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_filter_unknown_key_rejected(self):
        resp = _invoke_batch({
            "action": "stop", "filter": {"by_name": "x"},
        })
        assert resp["statusCode"] == 400


# ═══════════════════════════════════════════
# Action dispatch
# ═══════════════════════════════════════════


class TestBatchActionDispatch:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        # Each get_item returns the matching tenant
        def get_item(Key):
            tid = Key["id"]
            return {"Item": _running_tenant(tid)} if tid in self.existing else {}
        api.tenants_table.get_item.side_effect = get_item
        self.existing = set()
        api.hosts_table = make_ddb_table()

    @pytest.mark.unit
    def test_stop_dispatches_to_each_tenant(self):
        self.existing = {"t1", "t2"}
        # Mock tenant_action so we can assert without running the full pipeline
        with patch.object(api, "tenant_action", return_value=api._resp(200, {"id": "x", "status": "stopped"})) as m:
            resp = _invoke_batch({"action": "stop", "ids": ["t1", "t2"]})
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["succeeded"]) == 2
        assert {c["id"] for c in body["succeeded"]} == {"t1", "t2"}
        assert all(c["action"] == "stop" for c in body["succeeded"])
        assert m.call_count == 2

    @pytest.mark.unit
    def test_start_dispatched(self):
        self.existing = {"t1"}
        with patch.object(api, "tenant_action", return_value=api._resp(200, {"id": "t1", "status": "running"})):
            resp = _invoke_batch({"action": "start", "ids": ["t1"]})
        body = json.loads(resp["body"])
        assert body["succeeded"][0]["action"] == "start"

    @pytest.mark.unit
    def test_backup_dispatched(self):
        self.existing = {"t1"}
        with patch.object(api, "tenant_action", return_value=api._resp(202, {"id": "t1", "status": "started"})):
            resp = _invoke_batch({"action": "backup", "ids": ["t1"]})
        body = json.loads(resp["body"])
        assert body["succeeded"][0]["action"] == "backup"

    @pytest.mark.unit
    def test_delete_uses_delete_tenant(self):
        """delete dispatches to delete_tenant (different code path from action verbs)."""
        self.existing = {"t1"}
        with patch.object(api, "delete_tenant",
                          return_value=api._resp(200, {"id": "t1", "status": "deleted"})) as m:
            resp = _invoke_batch({"action": "delete", "ids": ["t1"]})
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["succeeded"][0]["action"] == "delete"
        m.assert_called_once()


# ═══════════════════════════════════════════
# Failure isolation
# ═══════════════════════════════════════════


class TestBatchFailureIsolation:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()

    @pytest.mark.unit
    def test_unknown_id_reported_as_failure(self):
        """Tenant not in DDB → falls into failed[] but doesn't abort batch."""
        # First tenant exists; second doesn't.
        def get_item(Key):
            return {"Item": _running_tenant("t1")} if Key["id"] == "t1" else {}
        api.tenants_table.get_item.side_effect = get_item

        with patch.object(api, "tenant_action",
                          return_value=api._resp(200, {"id": "t1", "status": "stopped"})):
            resp = _invoke_batch({"action": "stop", "ids": ["t1", "ghost"]})
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        succeeded_ids = {c["id"] for c in body["succeeded"]}
        failed_ids = {c["id"] for c in body["failed"]}
        assert succeeded_ids == {"t1"}
        assert failed_ids == {"ghost"}
        assert "not found" in body["failed"][0]["error"].lower()

    @pytest.mark.unit
    def test_action_exception_caught_per_tenant(self):
        """If one underlying call raises, others continue."""
        api.tenants_table.get_item.side_effect = lambda Key: {
            "Item": _running_tenant(Key["id"])
        }

        def fake_action(tid, action):
            if tid == "boom":
                raise RuntimeError("ssm dropped")
            return api._resp(200, {"id": tid, "status": "stopped"})

        with patch.object(api, "tenant_action", side_effect=fake_action):
            resp = _invoke_batch({"action": "stop", "ids": ["ok", "boom", "fine"]})
        body = json.loads(resp["body"])
        assert {c["id"] for c in body["succeeded"]} == {"ok", "fine"}
        assert {c["id"] for c in body["failed"]} == {"boom"}
        assert "ssm dropped" in body["failed"][0]["error"]


# ═══════════════════════════════════════════
# Filter by tag
# ═══════════════════════════════════════════


class TestBatchFilterByTag:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()

    @pytest.mark.unit
    def test_filter_selects_matching_tenants(self):
        """filter:{tag:'team:ml'} selects only matching tenants for the action."""
        api.tenants_table.scan.return_value = {"Items": [
            _running_tenant("a", tags={"team": "ml"}),
            _running_tenant("b", tags={"team": "infra"}),
            _running_tenant("c", tags={"team": "ml", "env": "dev"}),
        ]}
        # get_item returns tenant only for selected ids
        def get_item(Key):
            for t in api.tenants_table.scan.return_value["Items"]:
                if t["id"] == Key["id"]:
                    return {"Item": t}
            return {}
        api.tenants_table.get_item.side_effect = get_item
        with patch.object(api, "tenant_action",
                          return_value=api._resp(200, {"id": "x", "status": "stopped"})) as m:
            resp = _invoke_batch({
                "action": "stop", "filter": {"tag": "team:ml"},
            })
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        succeeded = {c["id"] for c in body["succeeded"]}
        assert succeeded == {"a", "c"}
        assert m.call_count == 2

    @pytest.mark.unit
    def test_filter_no_match_returns_empty(self):
        api.tenants_table.scan.return_value = {"Items": [
            _running_tenant("a", tags={"team": "ml"}),
        ]}
        api.tenants_table.get_item.side_effect = lambda Key: {}
        resp = _invoke_batch({
            "action": "stop", "filter": {"tag": "team:nope"},
        })
        body = json.loads(resp["body"])
        assert body == {"succeeded": [], "failed": []}

    @pytest.mark.unit
    def test_filter_excludes_deleted_tenants(self):
        api.tenants_table.scan.return_value = {"Items": [
            _running_tenant("a", tags={"team": "ml"}),
            {"id": "b", "status": "deleted", "tags": {"team": "ml"}},
        ]}
        api.tenants_table.get_item.side_effect = lambda Key: {
            "Item": next((t for t in api.tenants_table.scan.return_value["Items"]
                          if t["id"] == Key["id"]), None) or {}
        }
        with patch.object(api, "tenant_action",
                          return_value=api._resp(200, {"id": "a", "status": "stopped"})) as m:
            resp = _invoke_batch({"action": "stop", "filter": {"tag": "team:ml"}})
        body = json.loads(resp["body"])
        assert {c["id"] for c in body["succeeded"]} == {"a"}
        assert m.call_count == 1


# ═══════════════════════════════════════════
# Routing regression
# ═══════════════════════════════════════════


class TestBatchRouting:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_batch_route_does_not_collide_with_tenant_id(self):
        """The new /batch/tenants endpoint must not be matched by /tenants/{id}.
        We test the inverse: path-style /tenants/batch never enters batch handler.
        """
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": {"id": "batch", "status": "running"}}
        # Hitting /tenants/{id} with id=batch should call get_tenant, not batch
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/tenants/{id}",
            "pathParameters": {"id": "batch"},
        }, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["id"] == "batch"  # treated as a tenant id, not the batch route
