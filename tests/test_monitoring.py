# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for per-VM resource monitoring (issue #3).

The host-agent collects per-VM metrics on each polling tick and writes them
to DynamoDB so operators (and the console) can see real-time CPU/memory/disk
usage without per-VM SSH.

Sources:
- memory_used_mb / memory_balloon_mib  : Firecracker /balloon/statistics
- disk_used_mb / disk_total_mb / pct   : dumpe2fs -h on data.ext4 (host-side,
                                          no SSH into the guest)
- cpu_pct                               : reserved field, populated to 0 in
                                          this PR (future PR will compute
                                          from cgroup or Firecracker stats)

Tenants without a probe failure get a `metrics` field on their DDB record:
    metrics: {memory_used_mb, memory_balloon_mib, disk_used_mb,
              disk_total_mb, disk_used_pct, cpu_pct}
"""

import json
import os
import sys
import importlib.util
import pytest
from unittest.mock import patch, MagicMock
from conftest import make_ddb_table


# Import host-agent.py with mocked SDK (mirror test_balloon.py setup)
_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    spec = importlib.util.spec_from_file_location(
        "host_agent_metrics", "deploy/userdata/host-agent.py")
    agent = importlib.util.module_from_spec(spec)
    sys.modules["host_agent_metrics"] = agent
    spec.loader.exec_module(agent)


def _stats(actual_mib=0, available_mb=2048, free_mb=1024):
    """Build a fake balloon /statistics response."""
    return {
        "actual_mib": actual_mib,
        "target_mib": actual_mib,
        "stats": {
            "available_memory": available_mb * 1024 * 1024,
            "free_memory": free_mb * 1024 * 1024,
            "total_memory": 4096 * 1024 * 1024,
        },
    }


# ═══════════════════════════════════════════
# Disk usage extraction (dumpe2fs)
# ═══════════════════════════════════════════


class TestDiskUsage:
    @pytest.mark.unit
    def test_parses_dumpe2fs_output(self):
        """Standard dumpe2fs -h output → (used_mb, total_mb)."""
        # Simulated output: 4KB blocks, 2048 blocks total, 512 free
        output = (
            "dumpe2fs 1.46.5 (30-Dec-2021)\n"
            "Block count:              2048\n"
            "Free blocks:              512\n"
            "Block size:               4096\n"
        )
        used_mb, total_mb = agent._parse_dumpe2fs_blocks(output)
        # 2048 * 4096 = 8 MB total; 1536 used blocks * 4KB = 6 MB
        assert total_mb == 8
        assert used_mb == 6

    @pytest.mark.unit
    def test_handles_missing_fields_gracefully(self):
        """Malformed dumpe2fs → (0, 0) without raising."""
        used_mb, total_mb = agent._parse_dumpe2fs_blocks("garbage output\n")
        assert used_mb == 0
        assert total_mb == 0

    @pytest.mark.unit
    def test_get_disk_usage_returns_zero_when_file_missing(self):
        """If the data.ext4 path doesn't exist, return zeros without error."""
        used_mb, total_mb, pct = agent._get_disk_usage("/nonexistent/data.ext4")
        assert used_mb == 0
        assert total_mb == 0
        assert pct == 0

    @pytest.mark.unit
    def test_get_disk_usage_computes_percent(self):
        """Mock subprocess to return a known dumpe2fs output."""
        fake_output = (
            "Block count:              1024\n"
            "Free blocks:              256\n"
            "Block size:               4096\n"
        )
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_output)
            used_mb, total_mb, pct = agent._get_disk_usage("/fake.ext4")
        assert total_mb == 4
        assert used_mb == 3
        assert pct == 75


# ═══════════════════════════════════════════
# Memory usage from balloon stats
# ═══════════════════════════════════════════


class TestMemoryUsage:
    @pytest.mark.unit
    def test_memory_used_from_balloon_stats(self):
        """memory_used_mb = vm_mem_mb - available_mb."""
        used_mb, balloon_mib = agent._get_memory_usage(
            _stats(actual_mib=512, available_mb=2048), vm_mem_mb=4096)
        # 4096 declared - 2048 available = 2048 used
        assert used_mb == 2048
        assert balloon_mib == 512

    @pytest.mark.unit
    def test_memory_used_handles_no_stats(self):
        used_mb, balloon_mib = agent._get_memory_usage(None, vm_mem_mb=4096)
        assert used_mb == 0
        assert balloon_mib == 0


# ═══════════════════════════════════════════
# Metrics composition
# ═══════════════════════════════════════════


class TestComposeMetrics:
    @pytest.mark.unit
    def test_compose_returns_full_dict(self):
        """_compose_metrics packages all sources into a single dict."""
        with patch.object(agent, "_get_memory_usage", return_value=(2048, 512)), \
             patch.object(agent, "_get_disk_usage", return_value=(3000, 8192, 36)):
            m = agent._compose_metrics(
                tenant_id="t1", vm_mem_mb=4096, sock_file="/tmp/fc.sock",
                data_file="/tmp/data.ext4")
        assert m["memory_used_mb"] == 2048
        assert m["memory_balloon_mib"] == 512
        assert m["disk_used_mb"] == 3000
        assert m["disk_total_mb"] == 8192
        assert m["disk_used_pct"] == 36
        # CPU is reserved for a future PR
        assert "cpu_pct" in m
        assert m["cpu_pct"] == 0

    @pytest.mark.unit
    def test_compose_returns_zeros_when_sock_missing(self):
        """No socket → memory metrics zeroed but disk still reported."""
        with patch.object(agent, "_get_memory_usage", return_value=(0, 0)), \
             patch.object(agent, "_get_disk_usage", return_value=(1, 8, 12)):
            m = agent._compose_metrics(
                tenant_id="t1", vm_mem_mb=4096, sock_file=None,
                data_file="/tmp/data.ext4")
        assert m["memory_used_mb"] == 0
        assert m["disk_used_mb"] == 1


# ═══════════════════════════════════════════
# DDB write integration
# ═══════════════════════════════════════════


class TestWriteMetricsToDDB:
    @pytest.mark.unit
    def test_write_ddb_includes_metrics_field(self):
        """When the VM is up and metrics are computed, DDB update should
        carry a `metrics` map field."""
        agent.tenants_table = make_ddb_table()
        agent.TENANTS_TABLE = "test"
        with patch.object(agent, "_get_ddb") as mock_ddb_resource, \
             patch.object(agent, "_compose_metrics", return_value={
                 "memory_used_mb": 2048, "memory_balloon_mib": 0,
                 "disk_used_mb": 100, "disk_total_mb": 8192,
                 "disk_used_pct": 1, "cpu_pct": 0,
             }), \
             patch.object(agent, "_read_gateway_token", return_value="token"):
            mock_ddb_resource.return_value.Table.return_value = agent.tenants_table
            results = {"t1": {"vm_health": "up", "app_health": "up", "guest_ip": "172.16.1.2"}}
            agent._write_ddb(results)
        # update_item should have been called with metrics in expression values
        calls = agent.tenants_table.update_item.call_args_list
        # Promotion path includes metrics
        for c in calls:
            vals = c.kwargs.get("ExpressionAttributeValues", {})
            if ":m" in vals or any("metrics" in str(v) for v in vals.values()):
                return
        # Fall back: the implementation might write metrics on a non-promotion
        # path. Either way, the field should appear in some update.
        assert any(
            "metrics" in (c.kwargs.get("UpdateExpression", "") or "")
            or "#m" in (c.kwargs.get("UpdateExpression", "") or "")
            for c in calls
        )

    @pytest.mark.unit
    @pytest.mark.regression
    def test_write_ddb_uses_attr_name_alias_for_metrics_reserved_keyword(self):
        """Regression for the reserved-keyword bug found during 1.2.4 E2E.

        `metrics` is a DynamoDB reserved keyword (along with status, name,
        timestamp, etc.). Using it as a literal attribute name in an
        UpdateExpression makes update_item fail with ValidationException:
        ``Attribute name is a reserved keyword; reserved keyword: metrics``.

        Every update that touches the metrics field MUST alias it via
        ExpressionAttributeNames (e.g. ``#m`` → ``metrics``). Without
        this, the host-agent silently fails to promote tenants from
        ``creating`` → ``running`` and they sit in ``creating`` forever.
        """
        agent.tenants_table = make_ddb_table()
        agent.TENANTS_TABLE = "test"
        with patch.object(agent, "_get_ddb") as mock_ddb_resource, \
             patch.object(agent, "_compose_metrics", return_value={
                 "memory_used_mb": 2048, "memory_balloon_mib": 0,
                 "disk_used_mb": 100, "disk_total_mb": 8192,
                 "disk_used_pct": 1, "cpu_pct": 0,
             }), \
             patch.object(agent, "_read_gateway_token", return_value="token"):
            mock_ddb_resource.return_value.Table.return_value = agent.tenants_table
            results = {"t1": {"vm_health": "up", "app_health": "up", "guest_ip": "172.16.1.2"}}
            agent._write_ddb(results)
        for c in agent.tenants_table.update_item.call_args_list:
            ue = c.kwargs.get("UpdateExpression", "") or ""
            ean = c.kwargs.get("ExpressionAttributeNames", {}) or {}
            # If this update mentions metrics in any form, the literal
            # "metrics" identifier must NOT appear; it must go through #m.
            if "metrics" in str(c.kwargs.get("ExpressionAttributeValues", {})) or "#m" in ue:
                # Must reference via alias placeholder
                assert "#m" in ue, \
                    f"metrics must be aliased as #m in UpdateExpression; got: {ue!r}"
                assert "metrics" in ean.values(), \
                    f"#m must map to 'metrics' in ExpressionAttributeNames; got: {ean!r}"
                # Must NOT use the bare reserved keyword
                # (allow occurrences inside :v values, but not as identifier)
                # crude check: " metrics " or ", metrics =" or "= metrics" patterns
                assert " metrics " not in ue, \
                    f"bare 'metrics' identifier in UpdateExpression: {ue!r}"
                assert ", metrics " not in ue, \
                    f"bare 'metrics' identifier in UpdateExpression: {ue!r}"
                assert "metrics =" not in ue.replace("#m =", "").replace(":metrics", ""), \
                    f"bare 'metrics =' assignment in UpdateExpression: {ue!r}"

    @pytest.mark.unit
    @pytest.mark.regression
    def test_write_ddb_skips_metrics_when_health_down(self):
        """When VM is down, no metrics; just health flag."""
        agent.tenants_table = make_ddb_table()
        with patch.object(agent, "_get_ddb") as mock_ddb_resource:
            mock_ddb_resource.return_value.Table.return_value = agent.tenants_table
            results = {"t1": {"vm_health": "down", "app_health": "down", "guest_ip": "172.16.1.2"}}
            agent._write_ddb(results)
        # Update was called, but should NOT include "metrics" in expression
        for c in agent.tenants_table.update_item.call_args_list:
            ue = c.kwargs.get("UpdateExpression", "")
            assert "metrics" not in ue
