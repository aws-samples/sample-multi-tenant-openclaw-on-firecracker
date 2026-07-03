# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for control-plane scale-out: per-tenant-user fleet management
(PRD #50-58). Covers the indexed reverse lookup, pagination cursor, per-user
scope authorization, the summary roll-up, and bulk start/stop — the interface
the external backend uses to manage thousands of openclaw nodes by user, without
k8s and without full-table scans.
"""

import json
import importlib.util
import sys
from unittest.mock import patch, MagicMock
from conftest import make_ddb_table
import pytest

# All tests in this module are pure-mock unit tests (no real AWS); mark them
# so `pytest -m unit` includes them (loop 2026-07-02: found 136 tests were
# silently excluded from the unit suite for lack of this marker).
pytestmark = pytest.mark.unit

# ── Import handler with mocked AWS SDK (same pattern as test_api.py) ──
_mock_ddb = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: MagicMock()
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_fleet", "deploy/lambda/api/handler.py"
    )
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_fleet"] = api
    spec.loader.exec_module(api)


def _admin_event():
    # api-key path (no Bearer) → is_admin True → may manage any user's fleet
    return {"headers": {}}


# ═══════════════ pagination cursor (pure) ═══════════════


class TestPaginationCursor:
    def test_roundtrip(self):
        key = {"id": "tenant-abc"}
        tok = api._encode_next_token(key)
        assert tok and isinstance(tok, str)
        assert api._decode_next_token(tok) == key

    def test_none_key_is_none_token(self):
        assert api._encode_next_token(None) is None
        assert api._encode_next_token({}) is None

    def test_bad_token_decodes_to_none(self):
        assert api._decode_next_token("!!!not-base64!!!") is None
        assert api._decode_next_token(None) is None


class TestParseLimit:
    # #95 D-series: _clamp_limit (silent-degrade) replaced by _parse_limit
    # (fail-loud). A malformed ?limit now returns (None, 400) instead of being
    # coerced to a valid default/floor. Full matrix in test_issue93.
    def test_default_when_absent(self):
        assert api._parse_limit({}) == (api._USER_PAGE_DEFAULT, None)
        assert api._parse_limit(None) == (api._USER_PAGE_DEFAULT, None)

    def test_clamped_to_max(self):
        # over-ceiling positive int is valid, just capped — not an error.
        val, err = api._parse_limit({"limit": "999999"})
        assert err is None and val == api._USER_PAGE_MAX

    def test_zero_and_negative_rejected(self):
        # was silently floored to 1; now a loud 400 (D-01/D-04).
        for bad in ("0", "-5"):
            val, err = api._parse_limit({"limit": bad})
            assert val is None and err is not None and err["statusCode"] == 400

    def test_garbage_rejected(self):
        # was silently defaulted; now a loud 400 (D-03).
        val, err = api._parse_limit({"limit": "abc"})
        assert val is None and err is not None and err["statusCode"] == 400


# ═══════════════ per-user scope authorization ═══════════════


class TestUserScopeAuth:
    def test_empty_user_id_400(self):
        r = api._authorize_user_scope("", _admin_event())
        assert r is not None and r["statusCode"] == 400

    def test_admin_allowed(self):
        # api-key path is is_admin → None means allowed
        assert api._authorize_user_scope("tenant-user-1", _admin_event()) is None

    def test_rbac_disabled_allowed(self):
        with patch.object(api, "RBAC_ENABLED", False):
            assert api._authorize_user_scope("any-user", {"headers": {}}) is None

    def test_federated_user_own_fleet_allowed(self):
        with (
            patch.object(api, "RBAC_ENABLED", True),
            patch.object(
                api,
                "_get_caller_identity",
                return_value={"is_admin": False, "tenant_user_id": "bob"},
            ),
        ):
            assert api._authorize_user_scope("bob", {"headers": {}}) is None

    def test_federated_user_other_fleet_403(self):
        with (
            patch.object(api, "RBAC_ENABLED", True),
            patch.object(
                api,
                "_get_caller_identity",
                return_value={"is_admin": False, "tenant_user_id": "bob"},
            ),
        ):
            r = api._authorize_user_scope("alice", {"headers": {}})
            assert r is not None and r["statusCode"] == 403


# ═══════════════ indexed reverse lookup (GSI query, not scan) ═══════════════


class TestQueryUserTenants:
    def test_uses_gsi_query_not_scan(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.query.return_value = {
            "Items": [{"id": "t1", "tenant_user_id": "bob", "status": "running"}],
        }
        items, token = api._query_user_tenants("bob")
        # must query the tenant-user GSI, never a full-table scan
        assert api.tenants_table.query.called
        assert not api.tenants_table.scan.called
        kwargs = api.tenants_table.query.call_args.kwargs
        assert kwargs["IndexName"] == api.GSI_TENANT_USER
        assert items[0]["id"] == "t1"
        assert token is None

    def test_pagination_token_propagates(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.query.return_value = {
            "Items": [{"id": "t1"}],
            "LastEvaluatedKey": {"id": "t1"},
        }
        items, token = api._query_user_tenants("bob", limit=1)
        assert token is not None
        # the encoded token decodes back to the LastEvaluatedKey
        assert api._decode_next_token(token) == {"id": "t1"}


# ═══════════════ GET /users/{id}/tenants ═══════════════


class TestListUserTenants:
    def test_lists_with_pagination_envelope(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.query.return_value = {
            "Items": [
                {"id": "t1", "tenant_user_id": "bob", "status": "running"},
                {"id": "t2", "tenant_user_id": "bob", "status": "stopped"},
            ]
        }
        r = api.list_user_tenants("bob", {}, _admin_event())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["count"] == 2
        assert {t["id"] for t in body["tenants"]} == {"t1", "t2"}
        assert "next_token" in body

    def test_non_admin_other_user_forbidden(self):
        with (
            patch.object(api, "RBAC_ENABLED", True),
            patch.object(
                api,
                "_get_caller_identity",
                return_value={"is_admin": False, "tenant_user_id": "bob"},
            ),
        ):
            r = api.list_user_tenants("alice", {}, {"headers": {}})
            assert r["statusCode"] == 403


# ═══════════════ GET /users/{id}/summary ═══════════════


class TestUserSummary:
    def test_counts_by_status(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.query.return_value = {
            "Items": [
                {"id": "t1", "status": "running"},
                {"id": "t2", "status": "running"},
                {"id": "t3", "status": "stopped"},
            ]
        }
        r = api.user_summary("bob", _admin_event())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["total"] == 3
        assert body["by_status"] == {"running": 2, "stopped": 1}
        assert body["truncated"] is False


# ═══════════════ POST /users/{id}/action (bulk start/stop) ═══════════════


class TestUserAction:
    def test_invalid_action_400(self):
        r = api.user_action("bob", json.dumps({"action": "explode"}), _admin_event())
        assert r["statusCode"] == 400

    def test_bulk_stop_applies_to_whole_fleet(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.query.return_value = {
            "Items": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]
        }
        calls = []

        def _fake_action(tid, action, body, event):
            calls.append((tid, action))
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        with patch.object(api, "tenant_action", side_effect=_fake_action):
            r = api.user_action("bob", json.dumps({"action": "stop"}), _admin_event())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert len(body["succeeded"]) == 3
        assert body["failed"] == []
        # every node got the stop action, resolved from the GSI (not client ids)
        assert sorted(c[0] for c in calls) == ["t1", "t2", "t3"]
        assert all(c[1] == "stop" for c in calls)

    def test_failure_isolated_into_failed_list(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.query.return_value = {"Items": [{"id": "ok"}, {"id": "bad"}]}

        def _fake_action(tid, action, body, event):
            if tid == "bad":
                return {"statusCode": 409, "body": json.dumps({"error": "boom"})}
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

        with patch.object(api, "tenant_action", side_effect=_fake_action):
            r = api.user_action("bob", {"action": "start"}, _admin_event())
        body = json.loads(r["body"])
        assert [s["id"] for s in body["succeeded"]] == ["ok"]
        assert body["failed"][0]["id"] == "bad"
        assert body["failed"][0]["error"] == "boom"


# ═══════════════ list_tenants pagination (#53) backward compat ═══════════════


class TestListTenantsPagination:
    def test_no_limit_returns_bare_array(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {
            "Items": [{"id": "t1", "status": "running", "host_id": "i-h1"}]
        }
        with patch.object(api, "RBAC_ENABLED", False):
            r = api.list_tenants({}, {}, _admin_event())
        body = json.loads(r["body"])
        # legacy shape: a bare list, not an envelope
        assert isinstance(body, list)
        assert body[0]["id"] == "t1"

    def test_limit_returns_envelope_with_next_token(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {
            "Items": [{"id": "t1", "status": "running", "host_id": "i-h1"}],
            "LastEvaluatedKey": {"id": "t1"},
        }
        with patch.object(api, "RBAC_ENABLED", False):
            r = api.list_tenants({"limit": "1"}, {}, _admin_event())
        body = json.loads(r["body"])
        assert isinstance(body, dict)
        assert body["count"] == 1
        assert body["next_token"] is not None
        # the paginated scan passed a Limit to DynamoDB
        assert api.tenants_table.scan.call_args.kwargs.get("Limit") == 1


# ═══════════════ async batch jobs (#54) ═══════════════


class TestAsyncBatch:
    def test_large_batch_enqueues_and_returns_202(self):
        # >100 ids with the jobs table present → async job + self-invoke worker
        api.tenants_table = make_ddb_table()
        api.batch_jobs_table = make_ddb_table()
        ids = [f"t{i}" for i in range(150)]
        fake_lambda = MagicMock()
        with (
            patch.object(api.boto3, "client", return_value=fake_lambda),
            patch.dict(api.os.environ, {"AWS_LAMBDA_FUNCTION_NAME": "api-fn"}),
        ):
            r = api.batch_tenants(
                json.dumps({"action": "stop", "ids": ids}), _admin_event()
            )
        assert r["statusCode"] == 202
        body = json.loads(r["body"])
        assert body["job_id"] and body["total"] == 150
        # worker was self-invoked asynchronously with the job marker
        assert fake_lambda.invoke.called
        kw = fake_lambda.invoke.call_args.kwargs
        assert kw["InvocationType"] == "Event"
        assert json.loads(kw["Payload"])["_batch_job"] == body["job_id"]

    def test_large_batch_without_jobs_table_503(self):
        api.tenants_table = make_ddb_table()
        with patch.object(api, "batch_jobs_table", None):
            ids = [f"t{i}" for i in range(150)]
            r = api.batch_tenants(
                json.dumps({"action": "stop", "ids": ids}), _admin_event()
            )
        assert r["statusCode"] == 503

    def test_small_batch_stays_synchronous(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": {"id": "t1"}}
        with patch.object(
            api, "tenant_action", return_value={"statusCode": 200, "body": "{}"}
        ):
            r = api.batch_tenants(
                json.dumps({"action": "stop", "ids": ["t1"]}), _admin_event()
            )
        # sync path returns 200 with succeeded/failed inline (no job_id)
        assert r["statusCode"] == 200
        assert "job_id" not in json.loads(r["body"])

    def test_worker_runs_job_and_marks_done(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": {"id": "x"}}
        jobs = make_ddb_table()
        job_record = {
            "job_id": "batch-1",
            "action": "stop",
            "ids": ["a", "b", "c"],
            "status": "queued",
            "actor_is_admin": True,
            "actor_owner_id": "api-key",
        }
        jobs.get_item.return_value = {"Item": job_record}
        updates = []
        jobs.update_item.side_effect = lambda **kw: updates.append(kw)
        with (
            patch.object(api, "batch_jobs_table", jobs),
            patch.object(
                api, "tenant_action", return_value={"statusCode": 200, "body": "{}"}
            ),
        ):
            r = api.run_batch_job("batch-1")
        assert r["statusCode"] == 200
        # final update set status=done
        final = updates[-1]["ExpressionAttributeValues"]
        assert final[":s"] == "done"

    def test_worker_skips_already_done_job(self):
        jobs = make_ddb_table()
        jobs.get_item.return_value = {"Item": {"job_id": "j", "status": "done"}}
        with patch.object(api, "batch_jobs_table", jobs):
            r = api.run_batch_job("j")
        assert r["statusCode"] == 200 and "already" in r["body"]

    def test_get_batch_job_reports_progress(self):
        jobs = make_ddb_table()
        jobs.get_item.return_value = {
            "Item": {
                "job_id": "j1",
                "action": "stop",
                "status": "running",
                "total": 10,
                "done": 4,
                "succeeded": [{"id": "a"}],
                "failed": [],
            }
        }
        with patch.object(api, "batch_jobs_table", jobs):
            r = api.get_batch_job("j1", _admin_event())
        body = json.loads(r["body"])
        assert body["status"] == "running" and body["done"] == 4 and body["total"] == 10
        # the raw ids list is NOT echoed (can be huge)
        assert "ids" not in body

    def test_worker_route_in_lambda_handler(self):
        jobs = make_ddb_table()
        jobs.get_item.return_value = {"Item": {"job_id": "j", "status": "done"}}
        with patch.object(api, "batch_jobs_table", jobs):
            r = api.lambda_handler({"_batch_job": "j"}, None)
        assert r["statusCode"] == 200
