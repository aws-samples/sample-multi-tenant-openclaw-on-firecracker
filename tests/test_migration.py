# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for live VM migration (issue #20).

Live migration uses Firecracker's native snapshot/restore. The flow:

1. POST /tenants/{id}/migrate {target_host_id} hits the API
2. Lambda calls SSM on the SOURCE host to:
   - pause the VM
   - snapshot to /data/firecracker-vms/{id}/snapshot.{vm,mem}
   - aws s3 cp the two files to s3://{assets}/migrations/{id}/
3. Lambda calls SSM on the TARGET host to:
   - aws s3 cp the snapshot files down
   - launch a new firecracker process bound to the snapshot
4. Lambda updates DDB tenant.host_id, removes ALB rule on source,
   adds ALB rule on target.

This PR ships the **API + orchestration skeleton** — the heavy lifting
script `migrate-vm.sh` runs on the host and is invoked via SSM. We do
NOT exercise the full data path in unit tests; we assert:

- The endpoint accepts `{target_host_id}` and 400s without it
- 400 when source == target
- 404 when tenant doesn't exist
- 404 when target host isn't in the hosts table
- Successful migration triggers SSM commands on BOTH hosts and updates DDB
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════
# Load handler with mocked SDK
# ═══════════════════════════════════════════


_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
os.environ.setdefault("HOSTS_TABLE", "openclaw-hosts")
os.environ.setdefault("ASSETS_BUCKET", "test")
os.environ.setdefault("ROOTFS_PREFIX", "deployment/rootfs")

# Make tenants_table and hosts_table independent MagicMocks so per-table
# return_values don't bleed across calls.
_tenants_mock = MagicMock()
_hosts_mock = MagicMock()
def _table_factory(name):
    if "tenant" in name:
        return _tenants_mock
    return _hosts_mock
_mock_ddb.Table.side_effect = _table_factory

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    spec = importlib.util.spec_from_file_location(
        "mig_handler", str(ROOT / "deploy" / "lambda" / "api" / "handler.py"))
    handler = importlib.util.module_from_spec(spec)
    sys.modules["mig_handler"] = handler
    spec.loader.exec_module(handler)


# Helpful aliases used in test setUp lines.
handler.tenants_table = _tenants_mock
handler.hosts_table = _hosts_mock


def _migrate_event(tenant_id, body=None):
    """Build the API Gateway event for POST /tenants/{id}/migrate."""
    return {
        "httpMethod": "POST",
        "resource": "/tenants/{id}/{action}",
        "headers": {"x-api-key": "test"},
        "pathParameters": {"id": tenant_id, "action": "migrate"},
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
    }


# ═══════════════════════════════════════════
# Helpers / pure validation
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestMigrationValidation:
    def test_rejects_missing_target_host(self):
        """POST /tenants/t1/migrate without body → 400."""
        ev = _migrate_event("t1", body={})
        # Simulate tenant exists
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "guest_ip": "172.16.1.2",
                     "host_port": 18789, "status": "running"},
        }
        r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 400, f"expected 400, got {r}"
        assert "target" in r["body"].lower()

    def test_rejects_same_host(self):
        """target_host_id == source.host_id → 400."""
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "guest_ip": "172.16.1.2",
                     "host_port": 18789, "status": "running"},
        }
        ev = _migrate_event("t1", body={"target_host_id": "i-source"})
        r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 400
        assert "same" in r["body"].lower() or "different" in r["body"].lower()

    def test_404_when_tenant_missing(self):
        handler.tenants_table.get_item.return_value = {}
        ev = _migrate_event("t-missing", body={"target_host_id": "i-x"})
        r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 404

    def test_404_when_target_host_missing(self):
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "guest_ip": "172.16.1.2",
                     "host_port": 18789, "status": "running"},
        }
        # First get is tenant, second get is host_id lookup
        handler.hosts_table.get_item.return_value = {}
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 404
        assert "host" in r["body"].lower()


# ═══════════════════════════════════════════
# Successful path
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestMigrationOrchestration:
    def test_invokes_ssm_on_both_hosts(self):
        """Migration triggers SSM on source AND target."""
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "guest_ip": "172.16.1.2",
                     "host_port": 18789, "status": "running"},
        }
        handler.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                     "next_vm_num": 1, "used_vcpu": 0, "used_mem_mb": 0,
                     "vm_count": 0},
        }
        with patch.object(handler, "_ssm_send") as mock_send:
            mock_send.return_value = "cmd-123"
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] in (200, 202), f"expected 200/202 got {r}"
        # _ssm_send should have been called for both source and target
        called_hosts = {c.args[0] for c in mock_send.call_args_list}
        assert "i-source" in called_hosts, f"source not called: {called_hosts}"
        assert "i-target" in called_hosts, f"target not called: {called_hosts}"

    def test_ssm_command_references_snapshot(self):
        """The SSM payload must mention snapshot operations."""
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "guest_ip": "172.16.1.2",
                     "host_port": 18789, "status": "running"},
        }
        handler.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                     "next_vm_num": 1, "used_vcpu": 0, "used_mem_mb": 0,
                     "vm_count": 0},
        }
        with patch.object(handler, "_ssm_send") as mock_send:
            mock_send.return_value = "cmd-1"
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            handler.lambda_handler(ev, None)
        all_cmds = " ".join(c.args[1] for c in mock_send.call_args_list)
        assert "snapshot" in all_cmds.lower() or "migrate-vm" in all_cmds.lower()

    def test_updates_tenant_host_id(self):
        """After migration, tenant.host_id flips to the target."""
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "guest_ip": "172.16.1.2",
                     "host_port": 18789, "status": "running"},
        }
        handler.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                     "next_vm_num": 1, "used_vcpu": 0, "used_mem_mb": 0,
                     "vm_count": 0},
        }
        handler.tenants_table.update_item.reset_mock()
        with patch.object(handler, "_ssm_send", return_value="cmd-1"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            handler.lambda_handler(ev, None)
        # Look for an update that sets host_id to i-target
        updated = False
        for c in handler.tenants_table.update_item.call_args_list:
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            if any("i-target" in str(v) for v in vals.values()):
                updated = True
                break
        assert updated, "tenant.host_id was not updated to i-target"
