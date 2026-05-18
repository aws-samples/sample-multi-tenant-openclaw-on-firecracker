# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for SNS lifecycle notifications (issue #13).

When `notifications.enabled: true` in config.yml, the API Lambda publishes
a JSON message to an SNS topic on each tenant lifecycle transition that
the API itself initiates: created, deleted, stopped, started, restarted,
paused, resumed, reset, backup_started.

Async transitions (e.g. host-agent promoting "creating → running") are
out of scope for this PR — would need DDB Streams.

Topic ARN is read from `NOTIFICATIONS_TOPIC_ARN` env var. When absent,
publishing is a no-op (legacy/local mode).
"""

import json
import os
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
_mock_sns = MagicMock()
_mock_lambda = MagicMock()


# Set notifications env BEFORE import
os.environ["NOTIFICATIONS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:123:openclaw-tenant-events"

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: {
        "ssm": _mock_ssm, "s3": _mock_s3, "autoscaling": _mock_asg,
        "elbv2": _mock_elbv2, "sns": _mock_sns, "lambda": _mock_lambda,
    }.get(svc, MagicMock())
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "api_handler_notify", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_notify"] = api
    spec.loader.exec_module(api)


def _prep_host():
    api.tenants_table = make_ddb_table()
    api.hosts_table = make_ddb_table()
    api.hosts_table.scan.return_value = {"Items": [
        {"instance_id": "i-test", "total_vcpu": 8, "total_mem_mb": 16384,
         "used_vcpu": 0, "used_mem_mb": 0, "status": "active",
         "next_vm_num": 1, "private_ip": "10.0.0.1", "rootfs_version": "v1.0"},
    ]}


def _running_tenant(tid):
    return {"id": tid, "name": tid, "status": "running",
            "host_id": "i-1", "vm_num": 1,
            "vcpu": 2, "mem_mb": 4096,
            "guest_ip": "172.16.1.2", "host_port": 18789}


# ═══════════════════════════════════════════
# Module setup
# ═══════════════════════════════════════════


class TestNotificationsModule:
    @pytest.mark.unit
    def test_sns_client_initialized(self):
        assert hasattr(api, "sns"), "expected api.sns SNS client"

    @pytest.mark.unit
    def test_topic_arn_loaded_from_env(self):
        assert api.NOTIFICATIONS_TOPIC_ARN == "arn:aws:sns:us-east-1:123:openclaw-tenant-events"


# ═══════════════════════════════════════════
# Lifecycle publishes
# ═══════════════════════════════════════════


class TestLifecyclePublish:
    def setup_method(self):
        api.sns = MagicMock()

    @pytest.mark.unit
    def test_create_publishes_created_event(self):
        _prep_host()
        api.sns = MagicMock()
        # Re-bind reference inside module so create_tenant uses our mock
        with patch.object(api, "sns", api.sns):
            api.create_tenant(json.dumps({"name": "x"}))
        api.sns.publish.assert_called_once()
        kwargs = api.sns.publish.call_args.kwargs
        msg = json.loads(kwargs["Message"])
        assert msg["event"] == "tenant.created"
        assert msg["tenant_id"].startswith("x-")
        assert msg["details"]["vcpu"] == 2
        assert kwargs["TopicArn"] == api.NOTIFICATIONS_TOPIC_ARN

    @pytest.mark.unit
    def test_pending_create_also_publishes_created(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": []}
        _mock_asg.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 1, "MaxSize": 5}]}
        api.sns = MagicMock()
        api.create_tenant(json.dumps({"name": "x"}))
        api.sns.publish.assert_called_once()
        msg = json.loads(api.sns.publish.call_args.kwargs["Message"])
        assert msg["event"] == "tenant.created"

    @pytest.mark.unit
    def test_delete_publishes_deleted_event(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        # delete_tenant decrements host counters and reads Attributes back
        api.hosts_table.update_item.return_value = {"Attributes": {"vm_count": 0}}
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("t1")}
        api.sns = MagicMock()
        # Avoid real AWS calls in delete_tenant (SSM, ELBv2)
        with patch.object(api, "_ssm_run", return_value=True), \
             patch.object(api, "_remove_alb_rule"):
            api.delete_tenant("t1", {"keep_data": "true"})
        api.sns.publish.assert_called_once()
        msg = json.loads(api.sns.publish.call_args.kwargs["Message"])
        assert msg["event"] == "tenant.deleted"
        assert msg["tenant_id"] == "t1"

    @pytest.mark.unit
    def test_stop_action_publishes(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("t1")}
        api.sns = MagicMock()
        api.tenant_action("t1", "stop")
        api.sns.publish.assert_called_once()
        msg = json.loads(api.sns.publish.call_args.kwargs["Message"])
        assert msg["event"] == "tenant.stopped"

    @pytest.mark.unit
    def test_start_action_publishes(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("t1")}
        api.sns = MagicMock()
        api.tenant_action("t1", "start")
        api.sns.publish.assert_called_once()
        msg = json.loads(api.sns.publish.call_args.kwargs["Message"])
        assert msg["event"] == "tenant.started"

    @pytest.mark.unit
    def test_backup_action_publishes(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("t1")}
        api.sns = MagicMock()
        # backup creates a fresh boto3 lambda client inside tenant_action — patch boto3.client
        fake_lambda = MagicMock()
        with patch("boto3.client", side_effect=lambda svc, **kw: fake_lambda if svc == "lambda" else MagicMock()):
            api.tenant_action("t1", "backup")
        api.sns.publish.assert_called_once()
        msg = json.loads(api.sns.publish.call_args.kwargs["Message"])
        assert msg["event"] == "tenant.backup_started"

    @pytest.mark.unit
    def test_message_attributes_include_event_type(self):
        """SNS MessageAttributes must include `event` for filter policies."""
        _prep_host()
        api.sns = MagicMock()
        api.create_tenant(json.dumps({"name": "x"}))
        attrs = api.sns.publish.call_args.kwargs.get("MessageAttributes", {})
        assert "event" in attrs
        assert attrs["event"]["StringValue"] == "tenant.created"
        assert attrs["event"]["DataType"] == "String"


# ═══════════════════════════════════════════
# Failure isolation
# ═══════════════════════════════════════════


class TestPublishFailureIsolation:
    @pytest.mark.unit
    def test_sns_failure_does_not_break_create(self):
        _prep_host()
        api.sns = MagicMock()
        api.sns.publish.side_effect = Exception("SNS unreachable")
        resp = api.create_tenant(json.dumps({"name": "x"}))
        # Underlying operation succeeded
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_sns_failure_does_not_break_action(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("t1")}
        api.sns = MagicMock()
        api.sns.publish.side_effect = Exception("SNS unreachable")
        resp = api.tenant_action("t1", "stop")
        assert resp["statusCode"] == 200


# ═══════════════════════════════════════════
# Disabled (no topic ARN) → no-op
# ═══════════════════════════════════════════


class TestNotificationsDisabled:
    """When NOTIFICATIONS_TOPIC_ARN is empty, publishing is a no-op."""

    @pytest.mark.unit
    @pytest.mark.regression
    def test_no_publish_when_topic_unset(self):
        _prep_host()
        api.sns = MagicMock()
        original = api.NOTIFICATIONS_TOPIC_ARN
        try:
            api.NOTIFICATIONS_TOPIC_ARN = ""
            api.create_tenant(json.dumps({"name": "x"}))
            api.sns.publish.assert_not_called()
        finally:
            api.NOTIFICATIONS_TOPIC_ARN = original


# ═══════════════════════════════════════════
# GET requests do not publish
# ═══════════════════════════════════════════


class TestNoPublishOnReads:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_list_tenants_does_not_publish(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": []}
        api.sns = MagicMock()
        api.list_tenants()
        api.sns.publish.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.regression
    def test_get_tenant_does_not_publish(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("t1")}
        api.sns = MagicMock()
        api.get_tenant("t1")
        api.sns.publish.assert_not_called()
