"""Unit tests for balloon memory overcommit logic in host-agent.py."""

import json
import os
import pytest
import importlib.util
import sys
from unittest.mock import patch, MagicMock, call
from conftest import make_ddb_table

# Import host-agent with mocked dependencies
_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    # Set balloon env vars before import
    os.environ["BALLOON_ENABLED"] = "true"
    os.environ["BALLOON_MAX_INFLATE_RATIO"] = "0.4"
    os.environ["BALLOON_MIN_GUEST_AVAILABLE_MB"] = "512"
    spec = importlib.util.spec_from_file_location("agent", "deploy/userdata/host-agent.py")
    agent = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = agent
    spec.loader.exec_module(agent)


def _make_stats(available_mb=2048, free_mb=1024, actual_mib=0):
    """Create mock balloon statistics response."""
    return {
        "actual_mib": actual_mib,
        "target_mib": actual_mib,
        "stats": {
            "available_memory": available_mb * 1024 * 1024,
            "free_memory": free_mb * 1024 * 1024,
            "total_memory": 4096 * 1024 * 1024,
        },
    }


class TestAdjustBalloons:
    """Test _adjust_balloons dynamic memory management."""

    def _setup_vm(self, tid="t1", mem_mb=4096):
        """Create vm.json and fc.sock for a test VM."""
        vm_dir = os.path.join("/tmp/test-vms", tid)
        os.makedirs(vm_dir, exist_ok=True)
        with open(os.path.join(vm_dir, "vm.json"), "w") as f:
            json.dump({"tenant_id": tid, "vm_num": 1, "guest_ip": "172.16.1.2", "mem_mb": mem_mb}, f)
        # Create a fake socket file
        sock = os.path.join(vm_dir, "fc.sock")
        open(sock, "w").close()
        return vm_dir

    def setup_method(self):
        """Set up test VM directory."""
        self._orig_vm_dir = agent.VM_DIR
        agent.VM_DIR = "/tmp/test-vms"
        agent.BALLOON_ENABLED = True
        agent.BALLOON_MAX_INFLATE_RATIO = 0.4
        agent.BALLOON_MIN_GUEST_AVAILABLE_MB = 512
        os.makedirs("/tmp/test-vms", exist_ok=True)

    def teardown_method(self):
        agent.VM_DIR = self._orig_vm_dir
        import shutil
        shutil.rmtree("/tmp/test-vms", ignore_errors=True)

    @pytest.mark.unit
    def test_disabled_does_nothing(self):
        """When balloon disabled, no action taken."""
        agent.BALLOON_ENABLED = False
        with patch.object(agent, "_get_host_mem_info") as mock_mem:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            mock_mem.assert_not_called()

    @pytest.mark.unit
    def test_host_pressure_low_inflates(self):
        """When host available < 20%, inflate balloon on VMs with spare memory."""
        self._setup_vm("t1", 4096)
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 2000)), \
             patch.object(agent, "_get_balloon_stats", return_value=_make_stats(available_mb=2048, actual_mib=0)), \
             patch.object(agent, "_set_balloon_target") as mock_set:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            mock_set.assert_called_once()
            target = mock_set.call_args[0][1]
            assert target > 0  # Should inflate
            assert target <= 4096 * 0.4  # Respects max ratio

    @pytest.mark.unit
    def test_host_pressure_low_respects_min_guest(self):
        """Inflate should not reduce guest available below min threshold."""
        self._setup_vm("t1", 4096)
        # Guest only has 600MB available, min is 512, so only 88MB reclaimable
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 2000)), \
             patch.object(agent, "_get_balloon_stats", return_value=_make_stats(available_mb=600, actual_mib=0)), \
             patch.object(agent, "_set_balloon_target") as mock_set:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            mock_set.assert_called_once()
            target = mock_set.call_args[0][1]
            assert target == 88  # 600 - 512 = 88

    @pytest.mark.unit
    def test_host_pressure_low_guest_already_tight(self):
        """If guest available <= min, don't inflate further."""
        self._setup_vm("t1", 4096)
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 2000)), \
             patch.object(agent, "_get_balloon_stats", return_value=_make_stats(available_mb=400, actual_mib=500)), \
             patch.object(agent, "_set_balloon_target") as mock_set:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            mock_set.assert_not_called()  # Can't reclaim, guest already tight

    @pytest.mark.unit
    def test_host_plenty_deflates(self):
        """When host available > 40%, deflate balloon to give memory back."""
        self._setup_vm("t1", 4096)
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 8000)), \
             patch.object(agent, "_get_balloon_stats", return_value=_make_stats(actual_mib=1000)), \
             patch.object(agent, "_set_balloon_target") as mock_set:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            mock_set.assert_called_once()
            assert mock_set.call_args[0][1] == 0  # Deflate to 0

    @pytest.mark.unit
    def test_host_plenty_already_zero(self):
        """When balloon already 0 and host has plenty, no action."""
        self._setup_vm("t1", 4096)
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 8000)), \
             patch.object(agent, "_get_balloon_stats", return_value=_make_stats(actual_mib=0)), \
             patch.object(agent, "_set_balloon_target") as mock_set:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            mock_set.assert_not_called()  # Already 0, nothing to do

    @pytest.mark.unit
    def test_host_moderate_no_action(self):
        """When host pressure between 20-40%, no action (hysteresis)."""
        self._setup_vm("t1", 4096)
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 5000)), \
             patch.object(agent, "_get_balloon_stats") as mock_stats, \
             patch.object(agent, "_set_balloon_target") as mock_set:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            mock_set.assert_not_called()

    @pytest.mark.unit
    def test_max_inflate_ratio_cap(self):
        """Balloon target should never exceed max_inflate_ratio * vm_mem."""
        self._setup_vm("t1", 4096)
        # Guest has tons of free memory, but cap at 40%
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 1000)), \
             patch.object(agent, "_get_balloon_stats", return_value=_make_stats(available_mb=3500, actual_mib=0)), \
             patch.object(agent, "_set_balloon_target") as mock_set:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            target = mock_set.call_args[0][1]
            assert target == int(4096 * 0.4)  # 1638

    @pytest.mark.unit
    def test_vm_down_skipped(self):
        """VMs that are down should not have balloon adjusted."""
        self._setup_vm("t1", 4096)
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 1000)), \
             patch.object(agent, "_get_balloon_stats") as mock_stats:
            agent._adjust_balloons({"t1": {"vm_health": "down"}})
            mock_stats.assert_not_called()

    @pytest.mark.unit
    def test_no_stats_skipped(self):
        """If balloon stats unavailable, skip VM."""
        self._setup_vm("t1", 4096)
        with patch.object(agent, "_get_host_mem_info", return_value=(16384, 1000)), \
             patch.object(agent, "_get_balloon_stats", return_value=None), \
             patch.object(agent, "_set_balloon_target") as mock_set:
            agent._adjust_balloons({"t1": {"vm_health": "up"}})
            mock_set.assert_not_called()


class TestGetHostMemInfo:
    @pytest.mark.unit
    def test_reads_meminfo(self):
        """Should parse /proc/meminfo correctly."""
        mock_content = "MemTotal:       16384000 kB\nMemFree:         2000000 kB\nMemAvailable:    8000000 kB\n"
        with patch("builtins.open", return_value=__import__("io").StringIO(mock_content)):
            total, available = agent._get_host_mem_info()
            assert total == 16000  # 16384000 // 1024
            assert available == 7812  # 8000000 // 1024
