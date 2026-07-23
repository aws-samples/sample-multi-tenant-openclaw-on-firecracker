# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""E2E tests — real AWS API Gateway calls.
Requires: .env.deploy with API_URL and API_KEY.
Run: pytest tests/test_e2e.py -m e2e -v

These tests create and delete real resources. They are idempotent and clean up after themselves.
"""

import json
import os
import time
import urllib.error
import urllib.request

import pytest
from conftest import load_env_deploy

ENV = load_env_deploy()
pytestmark = pytest.mark.e2e

if not ENV:
    pytest.skip("No .env.deploy found — skipping E2E tests", allow_module_level=True)

API_URL = ENV.get("API_URL", "").rstrip("/")
API_KEY = ENV.get("API_KEY", "")

if not API_URL or not API_KEY:
    pytest.skip("API_URL or API_KEY not set — skipping E2E tests", allow_module_level=True)


def _api(method, path, body=None, timeout=30, max_retries=3):
    """Call the real API Gateway with automatic retry on transient errors.

    1.4.3: long-running e2e flows (backup → restore takes ~5 minutes,
    spanning many _api() calls) occasionally hit ``urllib.error.URLError``
    with ``[SSL: UNEXPECTED_EOF_WHILE_READING]`` when the API Gateway /
    ALB closes a TLS keep-alive connection mid-request. urllib has no
    built-in retry, so a one-off TLS reset would fail an otherwise
    healthy backup-restore test.

    The retry policy is conservative — only network-layer / 5xx errors
    are retried. 4xx (auth / validation) are returned immediately so
    bugs surface fast. Exponential backoff: 1 s, 2 s, 4 s.

    The fix lives only in this helper because the test cases above
    already treat _api() as the atomic API call. Any test calling
    _api() inherits the retry transparently.
    """
    import time
    url = f"{API_URL}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "x-api-key": API_KEY,
        "Content-Type": "application/json",
    }
    # 1.5.8: RBAC-enabled deployments downgrade API-key-only requests to
    # `viewer`, which 403s every write-path e2e test. Operators can supply a
    # verified Cognito id_token (e.g. from admin-initiate-auth) so the write
    # tests actually exercise the live API instead of skipping.
    id_token = os.environ.get("OC_E2E_ID_TOKEN", "")
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # 5xx may be transient (Lambda cold start, throttle, ALB
            # backend reset); 4xx is a real client error and surfaces
            # immediately so tests don't waste 7 seconds retrying a 401.
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                last_err = f"HTTP {e.code}"
                time.sleep(2 ** attempt)
                continue
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw) if raw else {"error": str(e)}
            except json.JSONDecodeError:
                return e.code, {"error": raw or str(e)}
        except urllib.error.URLError as e:
            # SSL: UNEXPECTED_EOF_WHILE_READING, Connection reset, etc.
            # All transient — retry with backoff.
            last_err = f"URLError: {getattr(e, 'reason', str(e))}"
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    # Exhausted retries on transient error. Re-raise the last one as
    # a URLError so callers see the same exception shape as before.
    raise urllib.error.URLError(f"exhausted {max_retries} retries: {last_err}")


def _skip_if_rbac_forbidden(status, body):
    """Skip (don't fail) when the API key lacks the operator role.

    Write-path e2e tests need an `operator`+ key. When RBAC is on and the
    configured key resolves to `viewer`, the handler returns 403 with an
    `rbac` body BEFORE doing anything — so no resource is created and it's
    safe to treat this as an environment gap, not a code failure. This is a
    read-only check on a response we already have; it never issues a probe
    write of its own.
    """
    if status in (401, 403) and isinstance(body, dict) and body.get("rbac"):
        pytest.skip(f"API key role={body['rbac'].get('role')!r} lacks "
                    f"{body['rbac'].get('required')!r} — skipping write-path e2e")


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
        _skip_if_rbac_forbidden(status, body)
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

def _wait_for_status(tenant_id, expected, timeout=360, interval=5):
    """Poll GET /tenants/{id} until status matches expected, or timeout.

    1.4.3: bumped default 180→360s. Restore-from-backup tests boot a
    Firecracker VM, decompress and ext4-fsck a multi-GB rootfs, and
    install OpenClaw — on a cold pool that's a real ~5min not a flaky
    240s. The retry _api() helper handles SSL flakes; this timeout is
    about giving real cold-start work time to complete.
    """
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
        # Pre-flight: this test boots Firecracker VMs, which requires that the
        # rootfs has been built and uploaded to S3 by `./build-rootfs.sh`.
        # When the data plane isn't ready yet (fresh deploy, rootfs not built),
        # the tenant will sit in "pending" forever and the wait below times
        # out — that's an environment problem, not a code regression. Skip
        # cleanly so CI on a control-plane-only deploy stays green.
        status, rootfs_info = _api("GET", "hosts/rootfs-version")
        if status != 200 or rootfs_info.get("version", "unknown") == "unknown":
            pytest.skip("rootfs not built/uploaded to S3 (run ./build-rootfs.sh on a "
                        "Linux host) — VM data plane not ready, skipping roundtrip")
        status, hosts = _api("GET", "hosts")
        active_hosts = [h for h in (hosts or []) if h.get("status") in ("active", "idle")]
        if status != 200 or not active_hosts:
            pytest.skip("no active hosts available (host-agent not registered yet) — "
                        "skipping roundtrip")

        # Pre-flight hygiene: earlier e2e cases create tenants that may not
        # have fully torn down their Firecracker process before this test
        # runs (DELETE /tenants → stop-vm.sh races with launch-vm.sh's late
        # init steps). Sweep up any leftover `e2e-…` tenants from previous
        # runs so the host has clean capacity for src + dst here.
        status, all_t = _api("GET", "tenants")
        if status == 200:
            for t in all_t or []:
                name = t.get("name", "")
                if name.startswith("e2e-") and name not in (self.SRC_NAME, self.DST_NAME):
                    try:
                        _api("DELETE", f"tenants/{t['id']}", timeout=30)
                    except Exception:
                        pass

        # 1. Create source tenant
        status, body = _api("POST", "tenants", {"name": self.SRC_NAME, "vcpu": 1, "mem_mb": 2048})
        if status == 500 and "AccessDenied" in str(body):
            pytest.skip("IAM permissions insufficient")
        _skip_if_rbac_forbidden(status, body)
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
        _skip_if_rbac_forbidden(status, body)
        assert status == 404

    @pytest.mark.e2e
    def test_restore_missing_tenant_id_returns_400(self):
        status, body = _api("POST", "tenants", {
            "name": "e2e-restore-nobody",
            "restore_from": {"timestamp": "20260101-000000"},
        })
        _skip_if_rbac_forbidden(status, body)
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


# ═══════════════════════════════════════════
# AgentCore E2E: Memory, Code Interpreter, Browser
# ═══════════════════════════════════════════

class TestAgentCoreMemoryE2E:
    """E2E: Memory create_event + batch_create_memory_records."""

    @pytest.mark.e2e
    def test_memory_write_and_list_events(self):
        """Write conversation events to Memory, verify they exist."""
        import datetime
        import uuid

        import boto3
        ac_status = _api("GET", "agentcore/status")[1]
        if not ac_status.get("enabled"):
            pytest.skip("AgentCore not enabled")

        session = boto3.Session(
            profile_name=ENV.get("PROFILE", "default"),
            region_name=ENV.get("REGION", "ap-northeast-1"),
        )
        rt = session.client("bedrock-agentcore")

        # Find memory ID from CloudFormation
        cf = session.client("cloudformation")
        resources = cf.describe_stack_resources(StackName="OpenClawOrchestrator")["StackResources"]
        mem_res = next(r["PhysicalResourceId"] for r in resources
                      if r["ResourceType"] == "AWS::BedrockAgentCore::Memory")
        # PhysicalResourceId may be ARN; extract the memory ID after the last /
        mem_id = mem_res.rsplit("/", 1)[-1] if "/" in mem_res else mem_res

        actor = f"e2e-mem-{uuid.uuid4().hex[:6]}"
        sid = f"sess-{uuid.uuid4().hex[:8]}"

        # Create events
        for role, text in [("USER", "I like Python."), ("ASSISTANT", "Noted!")]:
            rt.create_event(
                memoryId=mem_id, actorId=actor, sessionId=sid,
                eventTimestamp=datetime.datetime.now(datetime.timezone.utc),
                payload=[{"conversational": {"role": role, "content": {"text": text}}}],
            )

        # Verify events exist
        resp = rt.list_events(memoryId=mem_id, actorId=actor, sessionId=sid)
        assert len(resp.get("events", [])) == 2


class TestAgentCoreCodeInterpreterE2E:
    """E2E: Code Interpreter start → execute → stop."""

    @pytest.mark.e2e
    def test_execute_python_code(self):
        """Start session, execute Python, verify output, stop."""
        import boto3
        ac_status = _api("GET", "agentcore/status")[1]
        if not ac_status.get("enabled"):
            pytest.skip("AgentCore not enabled")

        session = boto3.Session(
            profile_name=ENV.get("PROFILE", "default"),
            region_name=ENV.get("REGION", "ap-northeast-1"),
        )
        rt = session.client("bedrock-agentcore")
        cf = session.client("cloudformation")
        resources = cf.describe_stack_resources(StackName="OpenClawOrchestrator")["StackResources"]
        ci_id = next(r["PhysicalResourceId"] for r in resources
                     if r["ResourceType"] == "AWS::BedrockAgentCore::CodeInterpreterCustom")

        # Start
        resp = rt.start_code_interpreter_session(codeInterpreterIdentifier=ci_id)
        ci_sid = resp["sessionId"]
        assert ci_sid

        try:
            # Execute
            resp = rt.invoke_code_interpreter(
                codeInterpreterIdentifier=ci_id, sessionId=ci_sid,
                name="executeCode",
                arguments={"code": "print(sum(range(1, 101)))", "language": "python"},
            )
            # Read stream
            result_text = ""
            for event in resp.get("stream", []):
                r = event.get("result", {})
                sc = r.get("structuredContent", {})
                result_text = sc.get("stdout", "")
                assert sc.get("exitCode") == 0
                assert not r.get("isError")
            assert "5050" in result_text
        finally:
            rt.stop_code_interpreter_session(codeInterpreterIdentifier=ci_id, sessionId=ci_sid)


class TestAgentCoreBrowserE2E:
    """E2E: Browser start → get status → stop."""

    @pytest.mark.e2e
    def test_browser_session_lifecycle(self):
        """Start browser session, verify READY status, stop."""
        import time

        import boto3
        ac_status = _api("GET", "agentcore/status")[1]
        if not ac_status.get("enabled"):
            pytest.skip("AgentCore not enabled")

        session = boto3.Session(
            profile_name=ENV.get("PROFILE", "default"),
            region_name=ENV.get("REGION", "ap-northeast-1"),
        )
        rt = session.client("bedrock-agentcore")
        cf = session.client("cloudformation")
        resources = cf.describe_stack_resources(StackName="OpenClawOrchestrator")["StackResources"]
        br_id = next(r["PhysicalResourceId"] for r in resources
                     if r["ResourceType"] == "AWS::BedrockAgentCore::BrowserCustom")

        # Start
        resp = rt.start_browser_session(browserIdentifier=br_id)
        br_sid = resp["sessionId"]
        assert br_sid

        try:
            time.sleep(3)
            # Get session info
            resp = rt.get_browser_session(browserIdentifier=br_id, sessionId=br_sid)
            assert resp["status"] == "READY"
            assert "streams" in resp  # WebSocket endpoint exists
        finally:
            rt.stop_browser_session(browserIdentifier=br_id, sessionId=br_sid)
