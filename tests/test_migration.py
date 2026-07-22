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
# These migration tests assume balloon is OFF (the balloon migrate guard,
# handler.py:957, short-circuits to 409 when BALLOON_ENABLED). The handler
# reads BALLOON_ENABLED once, at the module import below, so pin it here in
# case an earlier-collected test module (test_balloon.py) left it "true".
os.environ["BALLOON_ENABLED"] = "false"

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


# 1.5.0: RBAC fail-safes no-token requests to `viewer`, which would 403 these
# write-path tests before they reach migrate logic. RBAC is covered by
# tests/test_rbac.py; here we assume an authenticated admin.
@pytest.fixture(autouse=True)
def _authenticated_admin():
    with patch.object(handler, "_get_user_role", return_value="admin"):
        yield


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
# Successful path — ASYNC (1.4.4, issue #64)
#
# migrate is now async: API Gateway caps a synchronous request at 29s, far
# less than a multi-GB snapshot+restore. POST /migrate validates, fires the
# snapshot SSM command (fire-and-forget via _ssm_send, which returns a
# CommandId), records `migrating` + the async context in DDB, and returns 202.
# The health_check sweep (_advance_migration, tested in test_migration_sweep.py)
# polls the command, triggers restore, verifies the dashboard, and only then
# flips host_id/counters/routing. So here we assert the *trigger* contract, not
# the completed move.
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestMigrationOrchestration:
    def test_returns_202_and_marks_migrating(self):
        """A valid migrate request returns 202 and marks the tenant migrating
        without flipping host_id (the move happens out-of-band)."""
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "guest_ip": "172.16.1.2",
                     "host_port": 18789, "status": "running"},
        }
        handler.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                     "next_vm_num": 1, "used_vcpu": 0, "used_mem_mb": 0,
                     "vm_count": 0, "total_vcpu": 4, "total_mem_mb": 16384,
                     "status": "active"},
        }
        handler.tenants_table.update_item.reset_mock()
        with patch.object(handler, "_ssm_send", return_value="cmd-abc") as mock_send:
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 202, f"expected 202 got {r}"
        body = json.loads(r["body"])
        assert body["status"] == "migrating"
        assert body["target_host_id"] == "i-target"
        # The snapshot command must be fired on the SOURCE host exactly once.
        assert mock_send.call_count == 1, "expected one snapshot _ssm_send"
        assert mock_send.call_args.args[0] == "i-source"
        assert "migrate-vm.sh snapshot" in mock_send.call_args.args[1]

    def test_persists_async_migration_context(self):
        """The 202 path must stash everything the sweep needs: target, source,
        snapshot CommandId, phase=snapshot, and status=migrating."""
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "status": "running"},
        }
        handler.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                     "next_vm_num": 3, "used_vcpu": 0, "used_mem_mb": 0,
                     "vm_count": 0, "total_vcpu": 4, "total_mem_mb": 16384,
                     "status": "active"},
        }
        handler.tenants_table.update_item.reset_mock()
        with patch.object(handler, "_ssm_send", return_value="cmd-xyz"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            handler.lambda_handler(ev, None)
        # Find the update that set status=migrating and assert the context.
        ctx = None
        for c in handler.tenants_table.update_item.call_args_list:
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            if vals.get(":s") == "migrating":
                ctx = vals
                break
        assert ctx is not None, "no status=migrating write observed"
        assert ctx[":tgt"] == "i-target"
        assert ctx[":src"] == "i-source"
        assert ctx[":scmd"] == "cmd-xyz"
        assert ctx[":ph"] == "snapshot"
        assert ctx[":tvn"] == 3, "target_vm_num should come from next_vm_num"

    def test_does_not_flip_host_id_synchronously(self):
        """The 202 path must NOT flip host_id — that only happens in the sweep
        after the whole move is proven."""
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "status": "running"},
        }
        handler.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                     "next_vm_num": 1, "used_vcpu": 0, "used_mem_mb": 0,
                     "vm_count": 0, "total_vcpu": 4, "total_mem_mb": 16384,
                     "status": "active"},
        }
        handler.tenants_table.update_item.reset_mock()
        handler.hosts_table.update_item.reset_mock()
        with patch.object(handler, "_ssm_send", return_value="cmd-abc"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            handler.lambda_handler(ev, None)
        for c in handler.tenants_table.update_item.call_args_list:
            expr = c.kwargs.get("UpdateExpression", "")
            assert "host_id = :h" not in expr, "host_id flipped synchronously"
        # No host counter mutation in the request path either.
        assert handler.hosts_table.update_item.call_count == 0, (
            "host counters mutated synchronously"
        )

    def test_502_when_ssm_submit_fails(self):
        """If the snapshot SSM command can't even be submitted (_ssm_send →
        None), return 502 and do NOT mark migrating."""
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "status": "running"},
        }
        handler.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                     "next_vm_num": 1, "used_vcpu": 0, "used_mem_mb": 0,
                     "vm_count": 0, "total_vcpu": 4, "total_mem_mb": 16384,
                     "status": "active"},
        }
        handler.tenants_table.update_item.reset_mock()
        with patch.object(handler, "_ssm_send", return_value=None):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 502, f"expected 502 got {r}"
        for c in handler.tenants_table.update_item.call_args_list:
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            assert vals.get(":s") != "migrating", (
                "tenant marked migrating despite SSM submit failure"
            )


# ═══════════════════════════════════════════
# Failure-path fail-safe is now enforced by the health_check sweep
# (_advance_migration), covered in tests/test_migration_sweep.py. The API
# request path itself can only fail at SSM submit (above) — every other
# failure (snapshot/restore command failure, dashboard verify) is handled
# out-of-band, where the rollback-to-running contract lives.
# ═══════════════════════════════════════════


# ═══════════════════════════════════════════
# Capacity check (issue #60)
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestMigrationCapacityCheck:
    """Regression: #60 — migrate must reject targets without free vcpu/mem."""

    def _set_tenant_and_target(self, target_used_vcpu=0, target_used_mem_mb=0,
                                target_total_vcpu=4, target_total_mem_mb=16384,
                                target_status="active"):
        handler.tenants_table.get_item.return_value = {
            "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                     "vcpu": 2, "mem_mb": 4096, "status": "running"},
        }
        handler.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                     "next_vm_num": 1,
                     "used_vcpu": target_used_vcpu,
                     "used_mem_mb": target_used_mem_mb,
                     "vm_count": 1,
                     "total_vcpu": target_total_vcpu,
                     "total_mem_mb": target_total_mem_mb,
                     "status": target_status},
        }

    def test_rejects_insufficient_vcpu(self):
        # Tenant needs vcpu=2. Build target with allocatable < 2.
        # allocatable = total_vcpu × CPU_OVERCOMMIT_RATIO; pick numbers that
        # work regardless of overcommit ratio: total_vcpu=1, used=0 → max
        # allocatable is `total_vcpu × ratio`, but for any ratio ≤ 2 the free
        # vcpu is at most 2 and used_vcpu raises that. We force free < 2 by
        # setting used_vcpu high relative to total.
        self._set_tenant_and_target(target_total_vcpu=1, target_used_vcpu=1)
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        with patch.object(handler, "_ssm_send"):
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 409, f"expected 409 got {r}"
        assert "capacity" in r["body"].lower() or "vcpu" in r["body"].lower()

    def test_rejects_insufficient_mem(self):
        # Tenant needs mem_mb=4096. Set used close to total so free < 4096.
        self._set_tenant_and_target(target_total_mem_mb=4096, target_used_mem_mb=3000)
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        with patch.object(handler, "_ssm_send"):
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 409
        assert "capacity" in r["body"].lower() or "mem" in r["body"].lower()

    def test_rejects_draining_target(self):
        self._set_tenant_and_target(target_status="draining")
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        with patch.object(handler, "_ssm_send"):
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 409
        assert "draining" in r["body"].lower()


# ═══════════════════════════════════════════
# Issue #72 — balloon-aware migration. Firecracker v1.15.1 CAN snapshot a
# balloon VM; the migrate strategy is chosen by BALLOON_MIGRATE_MODE:
#   reject → 409; cold (default) → cold-dump then relaunch; live → snapshot.
# Balloon-off tenants always use the live snapshot path.
# ═══════════════════════════════════════════


def _running_tenant():
    handler.tenants_table.get_item.return_value = {
        "Item": {"id": "t1", "host_id": "i-source", "vm_num": 1,
                 "vcpu": 2, "mem_mb": 4096, "status": "running"},
    }


def _active_target():
    handler.hosts_table.get_item.return_value = {
        "Item": {"instance_id": "i-target", "private_ip": "10.0.0.5",
                 "next_vm_num": 1, "used_vcpu": 0, "used_mem_mb": 0,
                 "vm_count": 0, "total_vcpu": 4, "total_mem_mb": 16384,
                 "status": "active"},
    }


@pytest.mark.unit
class TestMigrationBalloonMode:
    def test_reject_mode_returns_409_before_ssm(self):
        _running_tenant()
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        with patch.object(handler, "BALLOON_ENABLED", True), \
             patch.object(handler, "BALLOON_MIGRATE_MODE", "reject"), \
             patch.object(handler, "_ssm_send") as mock_send:
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 409, f"expected 409 got {r}"
        assert json.loads(r["body"])["reason"] == "balloon_enabled"
        mock_send.assert_not_called()

    def test_cold_mode_fires_cold_dump_and_returns_202(self):
        _running_tenant(); _active_target()
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        with patch.object(handler, "BALLOON_ENABLED", True), \
             patch.object(handler, "BALLOON_MIGRATE_MODE", "cold"), \
             patch.object(handler, "_ssm_send", return_value="cmd-cold") as mock_send:
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 202, f"expected 202 got {r}"
        body = json.loads(r["body"])
        assert body["mode"] == "cold"
        # The source-side command must be the cold-dump verb, not snapshot.
        sent_cmd = mock_send.call_args[0][1]
        assert "migrate-vm.sh cold-dump" in sent_cmd
        # migration_mode is persisted so the sweep runs cold-restore.
        upd = handler.tenants_table.update_item.call_args[1]["ExpressionAttributeValues"]
        assert upd[":mode"] == "cold"

    def test_live_mode_fires_snapshot_and_returns_202(self):
        _running_tenant(); _active_target()
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        with patch.object(handler, "BALLOON_ENABLED", True), \
             patch.object(handler, "BALLOON_MIGRATE_MODE", "live"), \
             patch.object(handler, "_ssm_send", return_value="cmd-live") as mock_send:
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 202
        assert json.loads(r["body"])["mode"] == "live"
        assert "migrate-vm.sh snapshot" in mock_send.call_args[0][1]

    def test_balloon_off_uses_live_snapshot(self):
        _running_tenant(); _active_target()
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        with patch.object(handler, "BALLOON_ENABLED", False), \
             patch.object(handler, "BALLOON_MIGRATE_MODE", "cold"), \
             patch.object(handler, "_ssm_send", return_value="cmd-1") as mock_send:
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 202, f"expected 202 got {r}"
        # balloon off → always live snapshot, regardless of migrate_mode.
        assert json.loads(r["body"])["mode"] == "live"
        assert "migrate-vm.sh snapshot" in mock_send.call_args[0][1]

    def test_fresh_migrate_clears_stale_migration_failed(self):
        """A new migration must REMOVE any migration_failed left by a prior
        failed attempt, or a poller watching that field aborts immediately."""
        _running_tenant(); _active_target()
        ev = _migrate_event("t1", body={"target_host_id": "i-target"})
        with patch.object(handler, "BALLOON_ENABLED", False), \
             patch.object(handler, "_ssm_send", return_value="cmd-1"):
            handler.lambda_handler(ev, None)
        upd = handler.tenants_table.update_item.call_args[1]["UpdateExpression"]
        assert "REMOVE migration_failed" in upd
