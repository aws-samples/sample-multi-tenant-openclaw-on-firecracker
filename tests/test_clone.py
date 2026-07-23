# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for tenant snapshot/clone (issue #12).

API extension on POST /tenants:
    {"name": "clone", "clone_from": "src-tenant-id"}

Behavior:
- Source tenant must exist and be in `running` status
- Clone is scheduled on the SAME host as source (avoids cross-host rsync)
- SSM runs clone-data.sh on the host: pause src → cp data.ext4 + overlay.ext4
  → resume src
- Clone is launched with the copied disks (NEEDS_INIT skipped — disks
  already exist)
- `clone_from` is mutually exclusive with `restore_from`

Mutation isolation: deleting the clone does NOT affect source.
"""

import importlib.util
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
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
        "api_handler_clone", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_clone"] = api
    spec.loader.exec_module(api)


def _running_tenant(tid="src", host_id="i-1", vm_num=1):
    return {"id": tid, "name": tid, "status": "running",
            "host_id": host_id, "vm_num": vm_num,
            "vcpu": 2, "mem_mb": 4096,
            "guest_ip": "172.16.1.2", "host_port": 18789}


def _host(used_vcpu=2, total_vcpu=8, used_mem_mb=4096, total_mem_mb=16384, instance_id="i-1"):
    return {"instance_id": instance_id, "total_vcpu": total_vcpu,
            "total_mem_mb": total_mem_mb,
            "used_vcpu": used_vcpu, "used_mem_mb": used_mem_mb,
            "status": "active", "next_vm_num": 5,
            "private_ip": "10.0.0.1", "rootfs_version": "v1.0"}


# ═══════════════════════════════════════════
# Successful clone
# ═══════════════════════════════════════════


class TestCloneSuccess:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        # Source tenant exists and is running
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("src", "i-1", 1)}
        # Hosts scan returns the source's host with capacity
        api.hosts_table.scan.return_value = {"Items": [_host()]}

    @pytest.mark.unit
    def test_clone_creates_new_tenant_201(self):
        with patch.object(api, "_ssm_run", return_value=True), \
             patch.object(api, "_ensure_host_tg", return_value="arn:..."), \
             patch.object(api, "_add_alb_rule"):
            resp = api.create_tenant(json.dumps({
                "name": "clone", "clone_from": "src",
            }))
        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        assert body["id"].startswith("clone-")
        assert body["status"] == "creating"

    @pytest.mark.unit
    def test_clone_persists_clone_from_field(self):
        """The new tenant record should record its provenance for traceability."""
        with patch.object(api, "_ssm_run", return_value=True), \
             patch.object(api, "_ensure_host_tg", return_value="arn:..."), \
             patch.object(api, "_add_alb_rule"):
            api.create_tenant(json.dumps({"name": "clone", "clone_from": "src"}))
        # Look at the put_item that wrote the clone (latest call)
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved.get("clone_from") == "src"

    @pytest.mark.unit
    def test_clone_runs_clone_data_script(self):
        """SSM should invoke clone-data.sh with src tenant id + clone id."""
        ssm_calls = []
        def fake_ssm(host_id, cmd, timeout=60):
            ssm_calls.append((host_id, cmd))
            return True
        with patch.object(api, "_ssm_run", side_effect=fake_ssm), \
             patch.object(api, "_ensure_host_tg", return_value="arn:..."), \
             patch.object(api, "_add_alb_rule"):
            api.create_tenant(json.dumps({"name": "clone", "clone_from": "src"}))
        # Find the clone-data.sh invocation
        clone_cmds = [c for _, c in ssm_calls if "clone-data.sh" in c]
        assert len(clone_cmds) >= 1
        assert "src" in clone_cmds[0]  # source tenant id passed

    @pytest.mark.unit
    def test_clone_lands_on_same_host_as_source(self):
        """Clone is forced onto the source's host so cp can be local."""
        with patch.object(api, "_ssm_run", return_value=True), \
             patch.object(api, "_ensure_host_tg", return_value="arn:..."), \
             patch.object(api, "_add_alb_rule"):
            api.create_tenant(json.dumps({"name": "clone", "clone_from": "src"}))
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert saved["host_id"] == "i-1"


# ═══════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════


class TestCloneValidation:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host()]}

    @pytest.mark.unit
    def test_clone_from_unknown_source_404(self):
        api.tenants_table.get_item.return_value = {}
        resp = api.create_tenant(json.dumps({
            "name": "x", "clone_from": "ghost",
        }))
        assert resp["statusCode"] == 404
        assert "ghost" in json.loads(resp["body"])["error"]

    @pytest.mark.unit
    def test_clone_from_non_running_source_rejected(self):
        api.tenants_table.get_item.return_value = {"Item": {**_running_tenant(), "status": "stopped"}}
        resp = api.create_tenant(json.dumps({
            "name": "x", "clone_from": "src",
        }))
        assert resp["statusCode"] == 400
        assert "running" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_clone_from_and_restore_from_mutually_exclusive(self):
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("src")}
        resp = api.create_tenant(json.dumps({
            "name": "x",
            "clone_from": "src",
            "restore_from": {"tenant_id": "other"},
        }))
        assert resp["statusCode"] == 400

    @pytest.mark.unit
    def test_clone_with_no_capacity_on_source_host_rejected(self):
        """Clone must land on source's host. If that host has no capacity, reject."""
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("src", "i-1", 1)}
        # Host i-1 fully used
        api.hosts_table.scan.return_value = {"Items": [
            _host(used_vcpu=8, total_vcpu=8),  # at allocatable cap
        ]}
        api.CPU_OVERCOMMIT_RATIO = 1.0
        try:
            resp = api.create_tenant(json.dumps({
                "name": "x", "clone_from": "src",
            }))
            assert resp["statusCode"] == 400
            err = json.loads(resp["body"])["error"].lower()
            assert "host" in err or "capacity" in err
        finally:
            api.CPU_OVERCOMMIT_RATIO = 2.0


# ═══════════════════════════════════════════
# Failure isolation
# ═══════════════════════════════════════════


class TestCloneFailureIsolation:
    def setup_method(self):
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.tenants_table.get_item.return_value = {"Item": _running_tenant("src", "i-1", 1)}
        api.hosts_table.scan.return_value = {"Items": [_host()]}

    @pytest.mark.unit
    def test_clone_data_script_failure_returns_502(self):
        """If clone-data.sh fails (cp error), return 502; tenant record cleaned."""
        with patch.object(api, "_ssm_run", return_value=False), \
             patch.object(api, "_ensure_host_tg", return_value="arn:..."), \
             patch.object(api, "_add_alb_rule"):
            resp = api.create_tenant(json.dumps({"name": "x", "clone_from": "src"}))
        assert resp["statusCode"] in (500, 502)


# ═══════════════════════════════════════════
# Backward compatibility
# ═══════════════════════════════════════════


class TestCloneBackwardCompat:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_create_without_clone_from_unaffected(self):
        """Plain create_tenant (no clone_from) takes the normal scheduling path."""
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host()]}
        with patch.object(api, "_ssm_run", return_value=True), \
             patch.object(api, "_ensure_host_tg", return_value="arn:..."), \
             patch.object(api, "_add_alb_rule"):
            resp = api.create_tenant(json.dumps({"name": "normal"}))
        assert resp["statusCode"] == 201
        saved = api.tenants_table.put_item.call_args_list[-1].kwargs["Item"]
        assert "clone_from" not in saved or saved["clone_from"] == ""

    @pytest.mark.unit
    @pytest.mark.regression
    def test_restore_from_still_works(self):
        """Restoring from a backup is still supported alongside clone_from."""
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [_host()]}
        import datetime
        _mock_s3.list_objects_v2.return_value = {"Contents": [
            {"Key": "backups/src/20260101-000000.gz",
             "LastModified": datetime.datetime(2026, 1, 1), "Size": 100},
        ]}
        with patch.object(api, "_ssm_run", return_value=True), \
             patch.object(api, "_ensure_host_tg", return_value="arn:..."), \
             patch.object(api, "_add_alb_rule"):
            resp = api.create_tenant(json.dumps({
                "name": "x", "restore_from": {"tenant_id": "src"},
            }))
        assert resp["statusCode"] == 201
