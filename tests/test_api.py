"""Unit tests for deploy/lambda/api/handler.py.
Covers: scheduling (_find_host), overcommit, tenant CRUD, host ops, routing.
"""

import json
import pytest
import importlib.util
import sys
from unittest.mock import patch, MagicMock
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
