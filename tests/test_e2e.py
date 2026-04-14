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
