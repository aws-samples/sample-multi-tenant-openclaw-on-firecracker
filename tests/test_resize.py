# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for live VM resize (issue #16).

Endpoint: POST /tenants/{id}/resize
Body:    {"vcpu": 4}                  # optional
         {"mem_mb": 8192}             # rejected — needs restart in this PR
         {"vcpu": 4, "mem_mb": 8192}  # vcpu honored, mem_mb rejected

Firecracker constraints we honor:
- vCPU can ONLY be increased (cannot shrink without restart)
- Memory hot-add is only feasible via balloon deflate, which assumes the
  VM was launched with extra memory pre-allocated. The current launch-vm.sh
  doesn't do that, so memory resize requires restart in this PR — return
  a 400 with explicit message so callers know to use stop+restart.

Behavior:
- Status must be "running"
- New vcpu must be > current vcpu (Firecracker constraint)
- Host must have free vCPU equal to the delta (respects overcommit ratio)
- Quotas (issue #9) still apply when enabled
- Updates tenant.vcpu and host.used_vcpu atomically (best-effort: SSM call
  must succeed before DDB updates).
"""

import json
import sys
import os
import importlib.util
import pytest
from unittest.mock import patch, MagicMock
from conftest import make_ddb_table


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
        "api_handler_resize", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_resize"] = api
    spec.loader.exec_module(api)


def _running_tenant(tid="t1", vcpu=2, mem_mb=4096):
    return {"id": tid, "name": tid, "status": "running",
            "host_id": "i-1", "vm_num": 1,
            "vcpu": vcpu, "mem_mb": mem_mb,
            "guest_ip": "172.16.1.2", "host_port": 18789}


def _host(used_vcpu=2, total_vcpu=8, used_mem_mb=4096, total_mem_mb=16384):
    return {"instance_id": "i-1", "total_vcpu": total_vcpu,
            "total_mem_mb": total_mem_mb,
            "used_vcpu": used_vcpu, "used_mem_mb": used_mem_mb,
            "status": "active", "next_vm_num": 2,
            "private_ip": "10.0.0.1", "rootfs_version": "v1.0"}


def _resize_event(tid, body):
    """Build event matching what API Gateway sends for POST /tenants/{id}/resize.

    The stack registers a single `/tenants/{id}/{action}` resource, so
    `event.resource` is the parametric template and `pathParameters.action`
    carries the literal "resize". The lambda_handler routes that to
    tenant_action(tenant_id, "resize", body), which in turn dispatches to
    tenant_resize.
    """
    return {
        "httpMethod": "POST", "resource": "/tenants/{id}/{action}",
        "pathParameters": {"id": tid, "action": "resize"},
        "body": json.dumps(body),
    }


# ═══════════════════════════════════════════
# Routing — POST /tenants/{id}/resize
# ═══════════════════════════════════════════


class TestResizeRouting:
    @pytest.mark.unit
    def test_route_recognized(self):
        """The /tenants/{id}/resize route must be wired in lambda_handler."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant()}
        api.hosts_table = make_ddb_table()
        api.hosts_table.get_item.return_value = {"Item": _host()}
        with patch.object(api, "_ssm_run", return_value=True):
            resp = api.lambda_handler(_resize_event("t1", {"vcpu": 4}), None)
        # Should not 404 (which would mean route not registered)
        assert resp["statusCode"] != 404


# ═══════════════════════════════════════════
# Successful resize
# ═══════════════════════════════════════════


class TestResizeSuccess:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant(vcpu=2)}
        api.hosts_table.get_item.return_value = {"Item": _host(used_vcpu=2)}
        api.QUOTAS_ENABLED = False

    @pytest.mark.unit
    def test_increase_vcpu_calls_firecracker_patch(self):
        with patch.object(api, "_ssm_run", return_value=True) as m:
            resp = api.lambda_handler(_resize_event("t1", {"vcpu": 4}), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["vcpu"] == 4
        # SSM was called with PATCH machine-config
        cmd = m.call_args[0][1]
        assert "machine-config" in cmd
        assert "vcpu_count" in cmd

    @pytest.mark.unit
    def test_increase_vcpu_updates_tenant_record(self):
        with patch.object(api, "_ssm_run", return_value=True):
            api.lambda_handler(_resize_event("t1", {"vcpu": 4}), None)
        # Tenant table updated to vcpu=4
        update_calls = api.tenants_table.update_item.call_args_list
        assert any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":v") == 4
            for c in update_calls
        )

    @pytest.mark.unit
    def test_increase_vcpu_updates_host_used(self):
        with patch.object(api, "_ssm_run", return_value=True):
            api.lambda_handler(_resize_event("t1", {"vcpu": 4}), None)
        # Host used_vcpu incremented by delta (2 → 4 = +2)
        host_updates = api.hosts_table.update_item.call_args_list
        # Find the call that adjusts used_vcpu
        delta_call = next(
            (c for c in host_updates
             if c.kwargs.get("ExpressionAttributeValues", {}).get(":v") == 2),
            None,
        )
        assert delta_call is not None


# ═══════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════


class TestResizeValidation:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant(vcpu=4)}
        api.hosts_table.get_item.return_value = {"Item": _host(used_vcpu=4)}
        api.QUOTAS_ENABLED = False

    @pytest.mark.unit
    def test_missing_body_returns_400(self):
        resp = api.lambda_handler({
            "httpMethod": "POST", "resource": "/tenants/{id}/{action}",
            "pathParameters": {"id": "t1", "action": "resize"},
            "body": None,
        }, None)
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_no_vcpu_no_mem_returns_400(self):
        resp = api.lambda_handler(_resize_event("t1", {}), None)
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_vcpu_smaller_rejected(self):
        """Firecracker cannot shrink — request to lower vcpu rejected."""
        resp = api.lambda_handler(_resize_event("t1", {"vcpu": 2}), None)
        assert resp["statusCode"] == 400
        err = json.loads(resp["body"])["error"].lower()
        assert "increase" in err or "shrink" in err or "greater" in err

    @pytest.mark.unit
    def test_vcpu_equal_rejected(self):
        """No-op resize is rejected — caller should not need to call this."""
        resp = api.lambda_handler(_resize_event("t1", {"vcpu": 4}), None)
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_mem_resize_rejected_with_restart_message(self):
        """Memory live-resize is out of scope — instruct caller to restart."""
        resp = api.lambda_handler(_resize_event("t1", {"vcpu": 4, "mem_mb": 8192}), None)
        assert resp["statusCode"] == 400
        err = json.loads(resp["body"])["error"].lower()
        assert "restart" in err or "stop" in err or "memory" in err

    @pytest.mark.unit
    def test_non_running_tenant_rejected(self):
        api.tenants_table.get_item.return_value = {"Item": {**_running_tenant(), "status": "stopped"}}
        resp = api.lambda_handler(_resize_event("t1", {"vcpu": 8}), None)
        assert resp["statusCode"] == 400
        assert "running" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_unknown_tenant_returns_404(self):
        api.tenants_table.get_item.return_value = {}
        resp = api.lambda_handler(_resize_event("ghost", {"vcpu": 8}), None)
        assert resp["statusCode"] == 404

    @pytest.mark.unit
    def test_insufficient_host_capacity_rejected(self):
        """Host overcommit-allocatable < new vcpu → 400."""
        # Host total 8, used 7. Tenant has vcpu=4. Want vcpu=8 (delta=4 > 1).
        api.tenants_table.get_item.return_value = {"Item": _running_tenant(vcpu=4)}
        api.hosts_table.get_item.return_value = {"Item": _host(used_vcpu=7, total_vcpu=8)}
        # CPU_OVERCOMMIT_RATIO is 2.0 by default in conftest → allocatable = 16, used 7,
        # free = 9, delta = 4 → fits. Tighten:
        api.CPU_OVERCOMMIT_RATIO = 1.0
        try:
            resp = api.lambda_handler(_resize_event("t1", {"vcpu": 8}), None)
            assert resp["statusCode"] == 400
            assert "host" in json.loads(resp["body"])["error"].lower() \
                   or "capacity" in json.loads(resp["body"])["error"].lower()
        finally:
            api.CPU_OVERCOMMIT_RATIO = 2.0


# ═══════════════════════════════════════════
# Quota integration
# ═══════════════════════════════════════════


class TestResizeQuota:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant(vcpu=2)}
        api.hosts_table.get_item.return_value = {"Item": _host(used_vcpu=2)}

    @pytest.mark.unit
    def test_quota_blocks_oversized_resize(self):
        """When quotas enabled, resize past max_vcpu is rejected."""
        api.QUOTAS_ENABLED = True
        api.QUOTAS_MAX_VCPU = 4
        try:
            resp = api.lambda_handler(_resize_event("t1", {"vcpu": 8}), None)
            assert resp["statusCode"] == 400
            assert "quota" in json.loads(resp["body"])["error"].lower() \
                   or "max" in json.loads(resp["body"])["error"].lower()
        finally:
            api.QUOTAS_ENABLED = False

    @pytest.mark.unit
    def test_quota_allows_within_limit(self):
        api.QUOTAS_ENABLED = True
        api.QUOTAS_MAX_VCPU = 4
        try:
            with patch.object(api, "_ssm_run", return_value=True):
                resp = api.lambda_handler(_resize_event("t1", {"vcpu": 4}), None)
            assert resp["statusCode"] == 200
        finally:
            api.QUOTAS_ENABLED = False


# ═══════════════════════════════════════════
# SSM failure → tenant record NOT updated
# ═══════════════════════════════════════════


class TestResizeSSMFailure:
    @pytest.mark.unit
    def test_ssm_failure_does_not_update_state(self):
        """If Firecracker PATCH fails (returned via SSM), DDB state stays at
        old vcpu so subsequent retries see the truth."""
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant(vcpu=2)}
        api.hosts_table.get_item.return_value = {"Item": _host(used_vcpu=2)}
        api.QUOTAS_ENABLED = False
        with patch.object(api, "_ssm_run", return_value=False):
            resp = api.lambda_handler(_resize_event("t1", {"vcpu": 4}), None)
        assert resp["statusCode"] == 502 or resp["statusCode"] == 500
        # Tenant record was NOT updated to the new vcpu
        update_calls = api.tenants_table.update_item.call_args_list
        assert not any(
            c.kwargs.get("ExpressionAttributeValues", {}).get(":v") == 4
            for c in update_calls
        )
