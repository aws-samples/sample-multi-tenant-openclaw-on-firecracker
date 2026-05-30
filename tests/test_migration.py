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
        """Migration triggers SSM on source AND target.

        Issue #64 fix: the handler now runs migrate-vm.sh *synchronously* via
        ``_ssm_run`` (which blocks on SSM completion) instead of fire-and-forget
        ``_ssm_send``. We mock ``_ssm_run`` to return True (both SSM commands
        succeed) and assert it was invoked for both hosts.
        """
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
        with patch.object(handler, "_ssm_run", return_value=True) as mock_run, \
             patch.object(handler, "_ssm_send"), \
             patch.object(handler, "_ensure_host_tg", return_value="tg-arn"), \
             patch.object(handler, "_repoint_alb_rule_to_tg"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] == 200, f"expected 200 got {r}"
        # _ssm_run should have been called for both source and target.
        called_hosts = {c.args[0] for c in mock_run.call_args_list}
        assert "i-source" in called_hosts, f"source not called: {called_hosts}"
        assert "i-target" in called_hosts, f"target not called: {called_hosts}"

    def test_ssm_command_references_snapshot_and_restore(self):
        """The SSM payloads must drive migrate-vm.sh snapshot + restore."""
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
        with patch.object(handler, "_ssm_run", return_value=True) as mock_run, \
             patch.object(handler, "_ssm_send"), \
             patch.object(handler, "_ensure_host_tg", return_value="tg-arn"), \
             patch.object(handler, "_repoint_alb_rule_to_tg"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            handler.lambda_handler(ev, None)
        all_cmds = " ".join(c.args[1] for c in mock_run.call_args_list)
        assert "migrate-vm.sh snapshot" in all_cmds
        assert "migrate-vm.sh restore" in all_cmds

    def test_updates_tenant_host_id(self):
        """After a SUCCESSFUL migration, tenant.host_id flips to the target."""
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
        with patch.object(handler, "_ssm_run", return_value=True), \
             patch.object(handler, "_ssm_send"), \
             patch.object(handler, "_ensure_host_tg", return_value="tg-arn"), \
             patch.object(handler, "_repoint_alb_rule_to_tg"):
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

    def test_updates_both_host_counters(self):
        """Regression: #59 — migrate must -= source counters, += target counters."""
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
        handler.hosts_table.update_item.reset_mock()
        with patch.object(handler, "_ssm_run", return_value=True), \
             patch.object(handler, "_ssm_send"), \
             patch.object(handler, "_ensure_host_tg", return_value="tg-arn"), \
             patch.object(handler, "_repoint_alb_rule_to_tg"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            handler.lambda_handler(ev, None)

        calls_by_host = {}
        for c in handler.hosts_table.update_item.call_args_list:
            iid = c.kwargs.get("Key", {}).get("instance_id")
            calls_by_host.setdefault(iid, []).append(c)
        assert "i-source" in calls_by_host, "source host was not updated"
        assert "i-target" in calls_by_host, "target host was not updated"

        # Source decrements
        src_expr = " ".join(c.kwargs["UpdateExpression"] for c in calls_by_host["i-source"])
        assert "used_vcpu - :v" in src_expr
        assert "used_mem_mb - :m" in src_expr
        assert "vm_count - :one" in src_expr

        # Target increments
        tgt_expr = " ".join(c.kwargs["UpdateExpression"] for c in calls_by_host["i-target"])
        assert "used_vcpu + :v" in tgt_expr
        assert "used_mem_mb + :m" in tgt_expr
        assert "vm_count + :one" in tgt_expr


# ═══════════════════════════════════════════
# Failure path (issue #64 acceptance criteria) — on SSM failure the data
# plane never moved, so DDB host_id must NOT flip and the API must 5xx.
# These are the assertions that the old fire-and-forget code could never make.
# ═══════════════════════════════════════════


@pytest.mark.unit
class TestMigrationFailurePath:
    def _setup(self):
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
        handler.hosts_table.update_item.reset_mock()

    def _host_id_was_flipped(self):
        for c in handler.tenants_table.update_item.call_args_list:
            expr = c.kwargs.get("UpdateExpression", "")
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            if "host_id" in expr and any("i-target" in str(v) for v in vals.values()):
                return True
        return False

    def test_snapshot_failure_does_not_flip_ddb(self):
        """If snapshot fails on the source, host_id must stay on source and
        the API returns 5xx. (migrate-vm.sh missing → _ssm_run False.)"""
        self._setup()
        with patch.object(handler, "_ssm_run", return_value=False) as mock_run, \
             patch.object(handler, "_ssm_send"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] >= 500, f"expected 5xx on snapshot failure, got {r}"
        assert not self._host_id_was_flipped(), (
            "host_id flipped to target despite snapshot failure — DDB corrupted"
        )
        # Only the source snapshot should have been attempted (fail-fast: no
        # restore on the target after a failed snapshot).
        cmds = " ".join(c.args[1] for c in mock_run.call_args_list)
        assert "snapshot" in cmds
        assert "restore" not in cmds, "restore attempted after snapshot already failed"

    def test_restore_failure_does_not_flip_ddb(self):
        """If snapshot succeeds but restore fails on the target, host_id must
        still NOT flip (VM is still on source) and the API returns 5xx."""
        self._setup()
        # First _ssm_run call (snapshot) succeeds, second (restore) fails.
        with patch.object(handler, "_ssm_run", side_effect=[True, False]), \
             patch.object(handler, "_ssm_send"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            r = handler.lambda_handler(ev, None)
        assert r["statusCode"] >= 500, f"expected 5xx on restore failure, got {r}"
        assert not self._host_id_was_flipped(), (
            "host_id flipped to target despite restore failure — VM does not "
            "exist on target, DDB corrupted"
        )
        # Counters must not move either.
        for c in handler.hosts_table.update_item.call_args_list:
            expr = c.kwargs.get("UpdateExpression", "")
            assert "used_vcpu" not in expr, (
                "host counters mutated despite failed migration"
            )

    def test_status_returns_to_prior_on_failure(self):
        """On failure the tenant status must not be left as 'migrating'."""
        self._setup()
        with patch.object(handler, "_ssm_run", return_value=False), \
             patch.object(handler, "_ssm_send"):
            ev = _migrate_event("t1", body={"target_host_id": "i-target"})
            handler.lambda_handler(ev, None)
        # The LAST status write must restore 'running' (not leave 'migrating').
        status_writes = []
        for c in handler.tenants_table.update_item.call_args_list:
            expr = c.kwargs.get("UpdateExpression", "")
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            if "#s = :s" in expr or "#s = :st" in expr:
                for v in vals.values():
                    if v in ("running", "migrating", "stopped"):
                        status_writes.append(v)
        assert status_writes, "no status write observed"
        assert status_writes[-1] != "migrating", (
            f"tenant left in 'migrating' after failure: {status_writes}"
        )


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
