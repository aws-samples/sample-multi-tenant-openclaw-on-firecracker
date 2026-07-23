# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Issue #72 — host-agent migration sentinel.

While migrate-vm.sh pauses a VM to snapshot it (or before it loads a snapshot
on the target), host-agent must NOT touch that VM: a paused VM stops answering
ping, so _probe_all's dead-zone detector would force-relaunch it mid-snapshot,
and its balloon polling would race /snapshot/create on Firecracker's single-
threaded API socket. migrate-vm.sh drops a per-tenant .migrating sentinel;
host-agent skips any tenant with a live (non-stale) sentinel.
"""

import importlib.util
import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    _mock_ddb.Table.side_effect = lambda name: MagicMock()
    spec = importlib.util.spec_from_file_location(
        "host_agent_sentinel", "deploy/userdata/host-agent.py")
    agent = importlib.util.module_from_spec(spec)
    sys.modules["host_agent_sentinel"] = agent
    spec.loader.exec_module(agent)

pytestmark = pytest.mark.unit


@pytest.fixture
def vmroot(tmp_path):
    """Point host-agent's VM_DIR at a temp dir for the duration of a test."""
    with patch.object(agent, "VM_DIR", str(tmp_path)):
        yield tmp_path


def _mk_tenant(root, tid, with_sentinel=False, sentinel_age=0.0):
    d = root / tid
    d.mkdir()
    (d / "vm.json").write_text('{"guest_ip":"172.16.1.2","vm_num":1,"mem_mb":4096}')
    if with_sentinel:
        s = d / ".migrating"
        s.write_text("")
        if sentinel_age:
            old = time.time() - sentinel_age
            os.utime(s, (old, old))
    return d


class TestIsMigrating:
    def test_no_sentinel_is_not_migrating(self, vmroot):
        _mk_tenant(vmroot, "t1")
        assert agent._is_migrating("t1") is False

    def test_fresh_sentinel_is_migrating(self, vmroot):
        _mk_tenant(vmroot, "t1", with_sentinel=True)
        assert agent._is_migrating("t1") is True

    def test_stale_sentinel_is_ignored(self, vmroot):
        # Older than TTL → treated as leaked by a crashed migrate-vm.sh.
        _mk_tenant(vmroot, "t1", with_sentinel=True,
                   sentinel_age=agent.MIGRATION_SENTINEL_TTL + 60)
        assert agent._is_migrating("t1") is False


class TestProbeSkipsMigrating:
    def test_probe_all_reports_migrating_and_skips_probe(self, vmroot):
        """The critical dead-zone fix: a migrating tenant must be reported as
        'migrating' and must NOT be pinged / pgrep'd (which would trigger a
        relaunch of the paused VM)."""
        _mk_tenant(vmroot, "t1", with_sentinel=True)
        with patch.object(agent, "subprocess") as mock_sub, \
             patch.object(agent, "_recover_vm") as mock_recover, \
             patch.object(agent, "_force_relaunch_vm") as mock_relaunch:
            results = agent._probe_all()
        assert results.get("t1", {}).get("vm_health") == "migrating"
        # No ping/pgrep and definitely no relaunch of the paused VM.
        mock_sub.run.assert_not_called()
        mock_recover.assert_not_called()
        mock_relaunch.assert_not_called()

    def test_non_migrating_tenant_is_probed_normally(self, vmroot):
        _mk_tenant(vmroot, "t1")  # no sentinel
        with patch.object(agent, "subprocess") as mock_sub:
            # pgrep → FC running; ping → up; curl → app up
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="123")
            results = agent._probe_all()
        assert results["t1"]["vm_health"] in ("up", "down", "recovering")
        assert mock_sub.run.called  # it WAS probed


class TestAdjustBalloonsSkipsMigrating:
    def test_adjust_balloons_skips_migrating_tenant(self, vmroot):
        _mk_tenant(vmroot, "t1", with_sentinel=True)
        with patch.object(agent, "BALLOON_ENABLED", True), \
             patch.object(agent, "_get_host_mem_info", return_value=(16000, 2000)), \
             patch.object(agent, "_get_balloon_stats") as mock_stats, \
             patch.object(agent, "_set_balloon_target") as mock_set:
            # Pass stale probe results that still say 'up' — the sentinel guard
            # must still prevent any balloon PATCH.
            agent._adjust_balloons({"t1": {"vm_health": "up", "guest_ip": "172.16.1.2"}})
        mock_stats.assert_not_called()
        mock_set.assert_not_called()
