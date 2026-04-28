# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E tests — real AWS API Gateway calls.
Requires: .env.deploy with API_URL and API_KEY.
Run: pytest tests/test_e2e.py -m e2e -v

These tests create and delete real resources. They are idempotent and clean up after themselves.
"""

import os
import time
import json
import pytest
import urllib.request
import urllib.error
from conftest import load_env_deploy

ENV = load_env_deploy()
pytestmark = pytest.mark.e2e

if not ENV:
    pytest.skip("No .env.deploy found — skipping E2E tests", allow_module_level=True)

API_URL = ENV.get("API_URL", "").rstrip("/")
API_KEY = ENV.get("API_KEY", "")

if not API_URL or not API_KEY:
    pytest.skip("API_URL or API_KEY not set — skipping E2E tests", allow_module_level=True)


def _api(method, path, body=None, timeout=30):
    """Call the real API Gateway."""
    url = f"{API_URL}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-api-key": API_KEY,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else {"error": str(e)}
        except json.JSONDecodeError:
            return e.code, {"error": raw or str(e)}


# ═══════════════════════════════════════════
# API connectivity
# ═══════════════════════════════════════════

class TestAPIConnectivity:
    def test_list_tenants(self):
        """GET /tenants should return 200."""
        status, body = _api("GET", "tenants")
        assert status == 200
        assert isinstance(body, list)

    def test_list_hosts(self):
        """GET /hosts should return 200 with overcommit ratios."""
        status, body = _api("GET", "hosts")
        assert status == 200
        assert isinstance(body, list)
        if body:
            assert "cpu_overcommit_ratio" in body[0]
            # mem_overcommit_ratio only present after that feature is deployed

    def test_rootfs_version(self):
        """GET /hosts/rootfs-version should return version string."""
        status, body = _api("GET", "hosts/rootfs-version")
        assert status == 200
        assert "version" in body

    def test_invalid_api_key_rejected(self):
        """Request with wrong API key should be rejected."""
        url = f"{API_URL}/tenants"
        req = urllib.request.Request(url, method="GET", headers={
            "x-api-key": "invalid-key-12345",
            "Content-Type": "application/json",
        })
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "Should have been rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 403


# ═══════════════════════════════════════════
# Tenant lifecycle (create → verify → delete)
# ═══════════════════════════════════════════

class TestTenantLifecycle:
    """Create a test tenant, verify it exists, then delete it."""

    TENANT_NAME = "e2e-test-vm"

    def test_full_lifecycle(self):
        """Create → Get → Delete a tenant."""
        # Create
        status, body = _api("POST", "tenants", {"name": self.TENANT_NAME, "vcpu": 1, "mem_mb": 2048})
        if status == 500 and "AccessDenied" in str(body):
            pytest.skip("Environment IAM permissions insufficient — redeploy with latest stack.py")
        assert status == 201, f"Create failed: {body}"
        tenant_id = body["id"]
        assert tenant_id.startswith(f"{self.TENANT_NAME}-")
        assert body["status"] in ("creating", "pending")

        try:
            # Get
            status, body = _api("GET", f"tenants/{tenant_id}")
            assert status == 200
            assert body["id"] == tenant_id
            assert body["name"] == self.TENANT_NAME
            assert int(body["vcpu"]) == 1
            assert int(body["mem_mb"]) == 2048
        finally:
            # Delete (always clean up)
            time.sleep(2)
            status, body = _api("DELETE", f"tenants/{tenant_id}")
            assert status == 200
            assert body["status"] == "deleted"

        # Verify deleted
        status, body = _api("GET", f"tenants/{tenant_id}")
        # After delete, get may return the item with status=deleted or 404
        if status == 200:
            assert body.get("status") == "deleted"

    def test_get_nonexistent_tenant(self):
        """GET /tenants/nonexistent should return 404."""
        status, body = _api("GET", "tenants/nonexistent-0000")
        assert status == 404


# ═══════════════════════════════════════════
# AgentCore status
# ═══════════════════════════════════════════

class TestAgentCoreStatus:
    def test_agentcore_status_endpoint(self):
        """GET /agentcore/status should return enabled flag."""
        status, body = _api("GET", "agentcore/status")
        assert status == 200
        assert "enabled" in body


# ═══════════════════════════════════════════
# GET /backups — cross-tenant aggregate
# ═══════════════════════════════════════════

class TestListAllBackups:
    def test_list_all_backups(self):
        """GET /backups returns a list with expected fields and orphan flag."""
        status, body = _api("GET", "backups")
        assert status == 200
        assert isinstance(body, list)
        if not body:
            pytest.skip("No backups present in environment — roundtrip test will populate one")
        b = body[0]
        for field in ["tenant_id", "tenant_name", "tenant_exists",
                      "timestamp", "size_bytes", "last_modified"]:
            assert field in b, f"Missing field: {field}"


# ═══════════════════════════════════════════
# Backup → Restore roundtrip
# ═══════════════════════════════════════════

def _wait_for_status(tenant_id, expected, timeout=180, interval=5):
    """Poll GET /tenants/{id} until status matches expected, or timeout."""
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        status, body = _api("GET", f"tenants/{tenant_id}")
        if status == 200:
            last_status = body.get("status")
            if last_status == expected:
                return body
            if last_status == "failed":
                raise AssertionError(f"Tenant {tenant_id} entered failed state")
        time.sleep(interval)
    raise AssertionError(f"Tenant {tenant_id} did not reach {expected!r} within {timeout}s "
                         f"(last status: {last_status!r})")


def _wait_for_backup(tenant_id, timeout=180, interval=5):
    """Poll GET /tenants/{id}/backups until at least one backup exists."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = _api("GET", f"tenants/{tenant_id}/backups")
        if status == 200 and body.get("backups"):
            return body["backups"][0]  # sorted desc, first is newest
        time.sleep(interval)
    raise AssertionError(f"No backup appeared for {tenant_id} within {timeout}s")


class TestBackupRestoreRoundtrip:
    """End-to-end: create → backup → restore → verify. Slow (~5 min)."""

    SRC_NAME = "e2e-restore-src"
    DST_NAME = "e2e-restore-dst"

    @pytest.mark.e2e
    def test_backup_and_restore_from_latest(self):
        # 1. Create source tenant
        status, body = _api("POST", "tenants", {"name": self.SRC_NAME, "vcpu": 1, "mem_mb": 2048})
        if status == 500 and "AccessDenied" in str(body):
            pytest.skip("IAM permissions insufficient")
        assert status == 201, f"Create source failed: {body}"
        src_id = body["id"]
        dst_id = None

        try:
            # 2. Wait for source to be running
            _wait_for_status(src_id, "running", timeout=240)

            # 3. Trigger backup (async, returns 202)
            status, body = _api("POST", f"tenants/{src_id}/backup")
            assert status == 202, f"Backup trigger failed: {body}"

            # 4. Wait for backup to appear in S3
            backup = _wait_for_backup(src_id, timeout=240)
            assert backup["timestamp"]
            assert backup["size_mb"] > 0

            # 5. Restore into a new tenant (latest backup, no timestamp)
            status, body = _api("POST", "tenants", {
                "name": self.DST_NAME,
                "vcpu": 1, "mem_mb": 2048,
                "restore_from": {"tenant_id": src_id},
            })
            assert status == 201, f"Restore create failed: {body}"
            dst_id = body["id"]
            assert dst_id != src_id

            # 6. Wait for restored tenant to be running
            _wait_for_status(dst_id, "running", timeout=240)

            # 7. Verify GET /backups shows the src backup with tenant_exists=true
            status, body = _api("GET", "backups")
            assert status == 200
            matching = [b for b in body if b["tenant_id"] == src_id]
            assert matching, f"Expected backup entry for {src_id}"
            assert matching[0]["tenant_exists"] is True
            assert matching[0]["tenant_name"] == self.SRC_NAME
        finally:
            # Clean up both tenants
            for tid in (dst_id, src_id):
                if tid:
                    try:
                        _api("DELETE", f"tenants/{tid}", timeout=30)
                    except Exception as e:
                        print(f"cleanup failed for {tid}: {e}")

    @pytest.mark.e2e
    def test_restore_with_bad_timestamp_returns_404(self):
        """Quick negative test — no resources created, just validates error path."""
        status, body = _api("POST", "tenants", {
            "name": "e2e-restore-badts",
            "restore_from": {"tenant_id": "nonexistent-xxxx", "timestamp": "20990101-000000"},
        })
        assert status == 404

    @pytest.mark.e2e
    def test_restore_missing_tenant_id_returns_400(self):
        status, body = _api("POST", "tenants", {
            "name": "e2e-restore-nobody",
            "restore_from": {"timestamp": "20260101-000000"},
        })
        assert status == 400


# ═══════════════════════════════════════════
# Regression: existing features still work
# ═══════════════════════════════════════════

class TestRegression:
    @pytest.mark.regression
    def test_hosts_have_expected_fields(self):
        """Hosts should have all expected fields."""
        status, body = _api("GET", "hosts")
        assert status == 200
        if body:
            h = body[0]
            for field in ["instance_id", "private_ip", "total_vcpu", "total_mem_mb",
                          "used_vcpu", "used_mem_mb", "vm_count", "status"]:
                assert field in h, f"Missing field: {field}"

    @pytest.mark.regression
    def test_tenants_have_expected_fields(self):
        """Running tenants should have all expected fields."""
        status, body = _api("GET", "tenants")
        assert status == 200
        running = [t for t in body if t.get("status") == "running"]
        if running:
            t = running[0]
            for field in ["id", "name", "host_id", "vcpu", "mem_mb", "guest_ip", "status"]:
                assert field in t, f"Missing field: {field}"
