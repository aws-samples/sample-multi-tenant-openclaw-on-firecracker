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

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
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
             patch.object(agent, "_get_disk_usage", return_value=(3000, 8192, 36)), \
             patch.object(agent, "_read_proc_status_rss_kb", return_value=None), \
             patch.object(agent, "_sample_cpu_pct", return_value=0):
            m = agent._compose_metrics(
                tenant_id="t1", vm_mem_mb=4096, sock_file="/tmp/fc.sock",
                data_file="/tmp/data.ext4")
        assert m["memory_used_mb"] == 2048
        assert m["memory_balloon_mib"] == 512
        assert m["disk_used_mb"] == 3000
        assert m["disk_total_mb"] == 8192
        assert m["disk_used_pct"] == 36
        # cpu_pct is now real (1.2.9), but with a None pid the sampler
        # short-circuits to 0 — verify the path still produces an int.
        assert "cpu_pct" in m
        assert isinstance(m["cpu_pct"], int)

    @pytest.mark.unit
    def test_compose_prefers_vmrss_when_available(self):
        """1.2.9 — when /proc/<pid>/status returns VmRSS, prefer it over balloon."""
        with patch.object(agent, "_read_proc_status_rss_kb", return_value=512 * 1024), \
             patch.object(agent, "_get_balloon_stats", return_value=None), \
             patch.object(agent, "_get_disk_usage", return_value=(0, 0, 0)), \
             patch.object(agent, "_sample_cpu_pct", return_value=42):
            m = agent._compose_metrics(
                tenant_id="t1", vm_mem_mb=4096, sock_file="/tmp/fc.sock",
                data_file="/tmp/data.ext4", fc_pid=12345, vcpu=2)
        # 512 * 1024 KB = 524288 KB → 524288 // 1024 = 512 MB
        assert m["memory_used_mb"] == 512
        assert m["cpu_pct"] == 42  # passed through from sampler

    @pytest.mark.unit
    def test_compose_returns_zeros_when_sock_missing(self):
        """No socket → memory metrics zeroed but disk still reported."""
        with patch.object(agent, "_get_memory_usage", return_value=(0, 0)), \
             patch.object(agent, "_get_disk_usage", return_value=(1, 8, 12)), \
             patch.object(agent, "_read_proc_status_rss_kb", return_value=None), \
             patch.object(agent, "_sample_cpu_pct", return_value=0):
            m = agent._compose_metrics(
                tenant_id="t1", vm_mem_mb=4096, sock_file=None,
                data_file="/tmp/data.ext4")
        assert m["memory_used_mb"] == 0
        assert m["disk_used_mb"] == 1


# ═══════════════════════════════════════════
# Real CPU / memory sampling (1.2.9)
# ═══════════════════════════════════════════


class TestProcStatJiffies:
    """Verify _read_proc_stat_cpu_jiffies parses the /proc/<pid>/stat format
    correctly, including the comm field with parens + spaces.
    """

    @pytest.mark.unit
    def test_returns_none_for_no_pid(self):
        assert agent._read_proc_stat_cpu_jiffies(None) is None
        assert agent._read_proc_stat_cpu_jiffies(0) is None

    @pytest.mark.unit
    def test_returns_none_when_proc_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # PID guaranteed not to exist for this test
        assert agent._read_proc_stat_cpu_jiffies(99999999) is None

    @pytest.mark.unit
    def test_parses_simple_stat_line(self, tmp_path, monkeypatch):
        """Pid 1234 with utime=100, stime=200 → 300."""
        proc_pid = tmp_path / "proc" / "1234"
        proc_pid.mkdir(parents=True)
        # Field positions (1-indexed): pid=1, comm=2, state=3, ppid=4, pgrp=5,
        # session=6, tty_nr=7, tpgid=8, flags=9, minflt=10, cminflt=11,
        # majflt=12, cmajflt=13, utime=14, stime=15, ...
        # After ')' we drop fields 1+2, so remaining indices are 0=state, ...
        # utime is at index 11, stime at index 12 in the post-')' split.
        (proc_pid / "stat").write_text(
            "1234 (firecracker) S 1 1234 0 0 -1 4194304 0 0 0 0 100 200 0 0 20 0 1 0 0\n"
        )
        # Patch open() to read from our fake /proc tree
        original_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).startswith("/proc/1234/"):
                return original_open(str(proc_pid / path.split("/")[-1]), *args, **kwargs)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)
        assert agent._read_proc_stat_cpu_jiffies(1234) == 300

    @pytest.mark.unit
    def test_handles_comm_with_spaces_and_parens(self, tmp_path, monkeypatch):
        """comm can contain spaces + parens. We split on the *trailing* `)`."""
        proc_pid = tmp_path / "proc" / "5678"
        proc_pid.mkdir(parents=True)
        (proc_pid / "stat").write_text(
            "5678 (weird (name) with) S 1 5678 0 0 -1 4194304 0 0 0 0 50 75 0 0\n"
        )
        original_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).startswith("/proc/5678/"):
                return original_open(str(proc_pid / path.split("/")[-1]), *args, **kwargs)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)
        assert agent._read_proc_stat_cpu_jiffies(5678) == 125  # 50 + 75


class TestProcStatusRss:
    @pytest.mark.unit
    def test_returns_none_for_no_pid(self):
        assert agent._read_proc_status_rss_kb(None) is None
        assert agent._read_proc_status_rss_kb(0) is None

    @pytest.mark.unit
    def test_parses_vmrss_line(self, tmp_path, monkeypatch):
        proc_pid = tmp_path / "proc" / "1234"
        proc_pid.mkdir(parents=True)
        (proc_pid / "status").write_text(
            "Name:\tfirecracker\n"
            "State:\tS (sleeping)\n"
            "VmPeak:\t  524288 kB\n"
            "VmSize:\t  524288 kB\n"
            "VmRSS:\t  102400 kB\n"  # 100 MB
            "VmData:\t  100000 kB\n"
        )
        original_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).startswith("/proc/1234/"):
                return original_open(str(proc_pid / path.split("/")[-1]), *args, **kwargs)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)
        assert agent._read_proc_status_rss_kb(1234) == 102400


class TestComputeCpuPct:
    @pytest.mark.unit
    def test_first_sample_returns_zero(self):
        # No prior baseline → 0 (correct: we can't compute a rate from one point).
        assert agent._compute_cpu_pct(None, None, 100, 1.0, vcpu=1) == 0

    @pytest.mark.unit
    def test_steady_50_percent_one_vcpu(self):
        """Over 1 second, 50 jiffies of CPU on 100 jiffies/sec clock + 1 vcpu = 50%."""
        pct = agent._compute_cpu_pct(
            prev_jiffies=1000, prev_ts=0.0,
            cur_jiffies=1050, cur_ts=1.0,
            vcpu=1, clk_tck=100,
        )
        assert pct == 50

    @pytest.mark.unit
    def test_full_load_one_vcpu(self):
        """100 jiffies in 1s on 1 vcpu = 100%."""
        pct = agent._compute_cpu_pct(1000, 0.0, 1100, 1.0, vcpu=1, clk_tck=100)
        assert pct == 100

    @pytest.mark.unit
    def test_full_load_two_vcpus_is_50_percent_of_allocated(self):
        """100 jiffies in 1s on 2 vcpus = 50% of allocated."""
        pct = agent._compute_cpu_pct(1000, 0.0, 1100, 1.0, vcpu=2, clk_tck=100)
        assert pct == 50

    @pytest.mark.unit
    def test_capped_at_100(self):
        """Burst beyond 100% (cgroup catch-up) is clamped to 100."""
        pct = agent._compute_cpu_pct(1000, 0.0, 1500, 1.0, vcpu=1, clk_tck=100)
        assert pct == 100

    @pytest.mark.unit
    def test_negative_delta_returns_zero(self):
        """Pid reuse / counter wrap → discard, don't return a negative %."""
        pct = agent._compute_cpu_pct(2000, 0.0, 1000, 1.0, vcpu=1, clk_tck=100)
        assert pct == 0

    @pytest.mark.unit
    def test_zero_elapsed_returns_zero(self):
        pct = agent._compute_cpu_pct(1000, 1.0, 1100, 1.0, vcpu=1, clk_tck=100)
        assert pct == 0

    @pytest.mark.unit
    def test_zero_vcpu_returns_zero(self):
        # Defensive: vm.json with vcpu=0 should not divide-by-zero.
        pct = agent._compute_cpu_pct(1000, 0.0, 1100, 1.0, vcpu=0, clk_tck=100)
        assert pct == 0


class TestSampleCpuPct:
    """Integration of _compute_cpu_pct with the rolling-sample state."""

    def setup_method(self):
        agent._CPU_SAMPLES.clear()

    @pytest.mark.unit
    def test_first_call_returns_zero_and_records_baseline(self, monkeypatch):
        """First sample → 0%, baseline cached."""
        monkeypatch.setattr(agent, "_read_proc_stat_cpu_jiffies", lambda pid: 1000)
        pct = agent._sample_cpu_pct("t1", 1234, vcpu=1)
        assert pct == 0
        assert "t1" in agent._CPU_SAMPLES

    @pytest.mark.unit
    def test_second_call_uses_baseline(self, monkeypatch):
        """Second sample → real %, baseline updated."""
        # Seed t1 with a known prior baseline.
        agent._CPU_SAMPLES["t1"] = (1000, 0.0)
        # Now read 1100 jiffies "1 second later" (we patch monotonic to do so).
        monkeypatch.setattr(agent, "_read_proc_stat_cpu_jiffies", lambda pid: 1100)
        monkeypatch.setattr(agent.time, "monotonic", lambda: 1.0)
        pct = agent._sample_cpu_pct("t1", 1234, vcpu=1)
        # 100 jiffies / 100 hz / 1 vcpu = 100%
        assert pct == 100

    @pytest.mark.unit
    def test_no_pid_short_circuits(self):
        pct = agent._sample_cpu_pct("t1", None, vcpu=1)
        assert pct == 0

    @pytest.mark.unit
    def test_proc_unreadable_returns_zero_no_baseline(self, monkeypatch):
        """If /proc read fails, don't leave a stale baseline."""
        monkeypatch.setattr(agent, "_read_proc_stat_cpu_jiffies", lambda pid: None)
        pct = agent._sample_cpu_pct("t1", 1234, vcpu=1)
        assert pct == 0
        assert "t1" not in agent._CPU_SAMPLES


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



# ══════════════════════════════════════════════════════════════════════
# Host-level heartbeat (1.3.0) — required for AZ failover
# ══════════════════════════════════════════════════════════════════════


class TestHostHeartbeat:
    """The 1.3.0 AZ-failover path needs host-level liveness to distinguish
    "host alive but tenant misbehaving" from "host (and AZ) actually down".
    These tests verify the heartbeat writes the right shape and that
    failures degrade gracefully (the poll loop must NEVER crash on a
    transient DDB error).
    """

    @pytest.mark.unit
    def test_heartbeat_writes_last_seen_and_last_health_check(self):
        """Both fields are required: last_seen for the human eye in the
        console, last_health_check for the AZ-failover threshold check.
        """
        with patch.object(agent, "HOSTS_TABLE", "openclaw-hosts"), \
             patch.object(agent, "INSTANCE_ID", "i-test"), \
             patch.object(agent, "_get_ddb") as mock_ddb_resource:
            mock_table = MagicMock()
            mock_ddb_resource.return_value.Table.return_value = mock_table
            agent._write_host_heartbeat()
        # Exactly one update_item call.
        assert mock_table.update_item.call_count == 1
        call = mock_table.update_item.call_args
        assert call.kwargs["Key"] == {"instance_id": "i-test"}
        ue = call.kwargs["UpdateExpression"]
        assert "last_seen" in ue and "last_health_check" in ue
        # Both fields share the same timestamp value.
        vals = call.kwargs["ExpressionAttributeValues"]
        assert ":t" in vals
        # ISO-8601-ish format (YYYY-MM-DDTHH:MM:SSZ).
        assert "T" in vals[":t"] and vals[":t"].endswith("Z")

    @pytest.mark.unit
    def test_heartbeat_skips_when_table_not_configured(self):
        """Older deployments without HOSTS_TABLE in /etc/platform.env must
        not crash — heartbeat is a 1.3.0 addition; the rest of the agent
        keeps working on legacy hosts.
        """
        with patch.object(agent, "HOSTS_TABLE", ""), \
             patch.object(agent, "INSTANCE_ID", "i-test"), \
             patch.object(agent, "_get_ddb") as mock_ddb_resource:
            agent._write_host_heartbeat()
            mock_ddb_resource.assert_not_called()

    @pytest.mark.unit
    def test_heartbeat_skips_when_instance_id_missing(self):
        """If IMDS lookup failed at boot, INSTANCE_ID is empty — better to
        skip than to write a heartbeat under the wrong key.
        """
        with patch.object(agent, "HOSTS_TABLE", "openclaw-hosts"), \
             patch.object(agent, "INSTANCE_ID", ""), \
             patch.object(agent, "_get_ddb") as mock_ddb_resource:
            agent._write_host_heartbeat()
            mock_ddb_resource.assert_not_called()

    @pytest.mark.unit
    def test_heartbeat_swallows_ddb_failure(self):
        """A DDB throttle / network error must NEVER take down the poll
        loop. The next poll will retry; one missed heartbeat will not flip
        AZ failover (10-min threshold by default, polls every 5s).
        """
        with patch.object(agent, "HOSTS_TABLE", "openclaw-hosts"), \
             patch.object(agent, "INSTANCE_ID", "i-test"), \
             patch.object(agent, "_get_ddb") as mock_ddb_resource:
            mock_table = MagicMock()
            mock_table.update_item.side_effect = Exception("Throttled")
            mock_ddb_resource.return_value.Table.return_value = mock_table
            # Must not raise.
            agent._write_host_heartbeat()
