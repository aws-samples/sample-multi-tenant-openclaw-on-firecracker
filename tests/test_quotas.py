# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for per-tenant resource quotas (issue #9).

Quotas guard against noisy-neighbor: an API caller cannot request a tenant
with vcpu/mem_mb/data_disk_mb above the configured ceilings.

Quotas are opt-in via config.yml:
    quotas:
      enabled: true
      max_vcpu_per_tenant: 4
      max_mem_mb_per_tenant: 8192
      max_data_disk_mb: 16384

When `enabled: false` (default), no checks are performed — backward compat.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ddb_table

# Set quotas env BEFORE import
os.environ["QUOTAS_ENABLED"] = "true"
os.environ["QUOTAS_MAX_VCPU"] = "4"
os.environ["QUOTAS_MAX_MEM_MB"] = "8192"
os.environ["QUOTAS_MAX_DATA_DISK_MB"] = "16384"


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
        "api_handler_quota", "deploy/lambda/api/handler.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["api_handler_quota"] = api
    spec.loader.exec_module(api)


def _prep_host():
    api.tenants_table = make_ddb_table()
    api.hosts_table = make_ddb_table()
    api.hosts_table.scan.return_value = {"Items": [
        {"instance_id": "i-test", "total_vcpu": 8, "total_mem_mb": 65536,
         "used_vcpu": 0, "used_mem_mb": 0, "status": "active",
         "next_vm_num": 1, "private_ip": "10.0.0.1", "rootfs_version": "v1.0"},
    ]}


# ═══════════════════════════════════════════
# Quotas module setup
# ═══════════════════════════════════════════


class TestQuotaModuleSetup:
    @pytest.mark.unit
    def test_quotas_enabled_loaded(self):
        assert api.QUOTAS_ENABLED is True

    @pytest.mark.unit
    def test_max_vcpu_loaded(self):
        assert api.QUOTAS_MAX_VCPU == 4

    @pytest.mark.unit
    def test_max_mem_loaded(self):
        assert api.QUOTAS_MAX_MEM_MB == 8192


# ═══════════════════════════════════════════
# Enforcement on create_tenant
# ═══════════════════════════════════════════


class TestQuotaEnforcement:
    def setup_method(self):
        api.QUOTAS_ENABLED = True
        api.QUOTAS_MAX_VCPU = 4
        api.QUOTAS_MAX_MEM_MB = 8192
        api.QUOTAS_MAX_DATA_DISK_MB = 16384

    @pytest.mark.unit
    def test_vcpu_under_limit_passes(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 4, "mem_mb": 4096,
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_vcpu_over_limit_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 5, "mem_mb": 4096,
        }))
        assert resp["statusCode"] == 400
        assert "vcpu" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_mem_under_limit_passes(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 2, "mem_mb": 8192,
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_mem_over_limit_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 2, "mem_mb": 16384,
        }))
        assert resp["statusCode"] == 400
        assert "mem" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_data_disk_over_limit_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 2, "mem_mb": 4096, "data_disk_mb": 32768,
        }))
        assert resp["statusCode"] == 400
        assert "data_disk" in json.loads(resp["body"])["error"].lower()

    @pytest.mark.unit
    def test_data_disk_under_limit_passes(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 2, "mem_mb": 4096, "data_disk_mb": 16384,
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_error_message_includes_limit(self):
        """Error message should tell user the actual limit so they can retry."""
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 99, "mem_mb": 4096,
        }))
        body = json.loads(resp["body"])
        assert "4" in body["error"]  # the configured limit


# ═══════════════════════════════════════════
# Disabled mode — fully backward compatible
# ═══════════════════════════════════════════


class TestQuotasDisabled:
    @pytest.mark.unit
    @pytest.mark.regression
    def test_disabled_allows_anything(self):
        api.QUOTAS_ENABLED = False
        try:
            _prep_host()
            resp = api.create_tenant(json.dumps({
                "name": "x", "vcpu": 999, "mem_mb": 999999,
            }))
            # The host scheduler may reject due to no capacity, but quota
            # itself shouldn't 400 — accept either pending (201) or 201.
            # Specifically: not a quota 400.
            if resp["statusCode"] == 400:
                err = json.loads(resp["body"])["error"].lower()
                assert "quota" not in err and "exceed" not in err
        finally:
            api.QUOTAS_ENABLED = True


# ═══════════════════════════════════════════
# Boundary cases
# ═══════════════════════════════════════════


class TestQuotaBoundaries:
    def setup_method(self):
        api.QUOTAS_ENABLED = True
        api.QUOTAS_MAX_VCPU = 4
        api.QUOTAS_MAX_MEM_MB = 8192
        api.QUOTAS_MAX_DATA_DISK_MB = 16384

    @pytest.mark.unit
    def test_exactly_max_vcpu_allowed(self):
        """Limit is inclusive: vcpu == max → allowed."""
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 4, "mem_mb": 4096,
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_exactly_max_mem_allowed(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 2, "mem_mb": 8192,
        }))
        assert resp["statusCode"] == 201

    @pytest.mark.unit
    def test_one_over_max_rejected(self):
        _prep_host()
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 5, "mem_mb": 4096,
        }))
        assert resp["statusCode"] == 400


# ═══════════════════════════════════════════
# Composability with restore (must still enforce quota on restore body's vcpu)
# ═══════════════════════════════════════════


class TestQuotaWithRestore:
    @pytest.mark.unit
    def test_restore_with_oversized_vcpu_rejected(self):
        """restore_from doesn't bypass quota — caller must still respect limits."""
        api.QUOTAS_ENABLED = True
        api.QUOTAS_MAX_VCPU = 4
        api.QUOTAS_MAX_MEM_MB = 8192
        _prep_host()
        # Even with restore, vcpu in body is still validated
        resp = api.create_tenant(json.dumps({
            "name": "x", "vcpu": 99, "mem_mb": 4096,
            "restore_from": {"tenant_id": "src", "timestamp": "20260101-000000"},
        }))
        assert resp["statusCode"] == 400
