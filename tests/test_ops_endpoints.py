# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T2-8: operator endpoints — cancel-migration, host drain, manual AZ failover."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

os.environ.setdefault("HEALTH_CHECK_FUNCTION", "openclaw-health-check")

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
_mock_s3 = MagicMock()
_mock_asg = MagicMock()
_mock_elbv2 = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm, "s3": _mock_s3, "autoscaling": _mock_asg,
        "elbv2": _mock_elbv2}.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "ops_handler", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["ops_handler"] = api
    spec.loader.exec_module(api)

pytestmark = pytest.mark.unit


class TestCancelMigration:
    def setup_method(self):
        api.tenants_table = make_ddb_table()

    def test_cancel_reverts_migrating_to_running(self):
        api.tenants_table.get_item.return_value = {"Item": {
            "id": "t1", "status": "migrating", "host_id": "i-src",
            "migration_phase": "snapshot"}}
        r = api.tenant_action("t1", "cancel-migration")
        assert r["statusCode"] == 200
        assert json.loads(r["body"])["status"] == "running"
        upd = api.tenants_table.update_item.call_args[1]
        assert upd["ExpressionAttributeValues"][":r"] == "running"
        assert "REMOVE migration_target" in upd["UpdateExpression"]

    def test_cancel_rejected_when_not_migrating(self):
        api.tenants_table.get_item.return_value = {"Item": {
            "id": "t1", "status": "running"}}
        r = api.tenant_action("t1", "cancel-migration")
        assert r["statusCode"] == 409
        assert not api.tenants_table.update_item.called


class TestDrainHost:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()

    def test_missing_host_404(self):
        api.hosts_table.get_item.return_value = {}
        r = api.drain_host("i-gone")
        assert r["statusCode"] == 404

    def test_empty_host_marks_draining_no_migrations(self):
        api.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        api.tenants_table.scan.return_value = {"Items": []}
        r = api.drain_host("i-1")
        assert r["statusCode"] == 202
        body = json.loads(r["body"])
        assert body["migrations_started"] == []
        # host flipped to draining
        upd = api.hosts_table.update_item.call_args[1]
        assert upd["ExpressionAttributeValues"][":s"] == "draining"

    def test_migrates_each_running_tenant_off(self):
        api.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-src", "status": "active"}}
        api.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "host_id": "i-src", "status": "running", "vcpu": 1, "mem_mb": 2048},
            {"id": "t2", "host_id": "i-src", "status": "running", "vcpu": 1, "mem_mb": 2048}]}
        with patch.object(api, "_find_host", return_value={"instance_id": "i-dst"}), \
             patch.object(api, "tenant_action",
                          return_value={"statusCode": 202, "body": "{}"}) as mig:
            r = api.drain_host("i-src")
        body = json.loads(r["body"])
        assert len(body["migrations_started"]) == 2
        # each migration targets the OTHER host, via the migrate action
        for c in mig.call_args_list:
            assert c[0][1] == "migrate"
            assert c[0][2]["target_host_id"] == "i-dst"

    def test_no_target_capacity_reports_failed(self):
        api.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-src", "status": "active"}}
        api.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "host_id": "i-src", "status": "running", "vcpu": 1, "mem_mb": 2048}]}
        with patch.object(api, "_find_host", return_value=None):
            r = api.drain_host("i-src")
        body = json.loads(r["body"])
        assert body["migrations_started"] == []
        assert body["failed"][0]["id"] == "t1"


class TestTriggerFailover:
    def test_invokes_health_check_lambda(self):
        fake_lambda = MagicMock()
        with patch.object(api, "boto3") as b, \
             patch.dict(os.environ, {"HEALTH_CHECK_FUNCTION": "openclaw-health-check"}):
            b.client.return_value = fake_lambda
            # trigger_failover reads env at call time
            with patch.object(api.os, "environ", os.environ):
                r = api.trigger_failover("ap-northeast-1a")
        assert r["statusCode"] == 202
        assert fake_lambda.invoke.called
        args = fake_lambda.invoke.call_args[1]
        assert args["FunctionName"] == "openclaw-health-check"
        assert args["InvocationType"] == "Event"

    def test_missing_function_env_returns_501(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEALTH_CHECK_FUNCTION", None)
            r = api.trigger_failover("ap-northeast-1a")
        assert r["statusCode"] == 501


class TestOpsRbac:
    def test_failover_requires_admin(self):
        assert ("POST", "/failover/{az}") in api._ADMIN_ONLY
        with patch.object(api, "RBAC_ENABLED", True), \
             patch.object(api, "_get_user_role", return_value="operator"):
            forbidden = api._rbac_check({}, "POST", "/failover/{az}")
        assert forbidden is not None and forbidden["statusCode"] == 403
        assert json.loads(forbidden["body"])["rbac"]["required"] == "admin"

    def test_drain_allowed_for_operator(self):
        with patch.object(api, "RBAC_ENABLED", True), \
             patch.object(api, "_get_user_role", return_value="operator"):
            assert api._rbac_check({}, "POST", "/hosts/{instance_id}/drain") is None
