# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for host-agent's dead-zone recovery (issue #69).

A VM whose Firecracker process is alive but whose guest network is dead
(e.g. TAP stuck DOWN after a partial launch) is invisible to the original
_recover_vm path, which only fires when FC is *absent*. The guard counts
consecutive 'FC alive but guest unreachable' polls and forces a stop+relaunch
once they cross _NET_DEAD_THRESHOLD, while resetting on any reachable poll so
transient blips don't trigger an unnecessary rebuild.
"""

import importlib.util
import sys
from unittest.mock import patch, MagicMock

import pytest


# Import host-agent.py with mocked SDK (mirror test_monitoring.py setup)
_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    _mock_ddb.Table.side_effect = lambda name: MagicMock()
    spec = importlib.util.spec_from_file_location(
        "host_agent_deadzone", "deploy/userdata/host-agent.py")
    agent = importlib.util.module_from_spec(spec)
    sys.modules["host_agent_deadzone"] = agent
    spec.loader.exec_module(agent)


@pytest.fixture(autouse=True)
def _clear_counter():
    """Each test starts with an empty dead-poll counter."""
    agent._net_dead_polls.clear()
    yield
    agent._net_dead_polls.clear()


@pytest.mark.unit
class TestRegisterNetPoll:
    def test_reachable_returns_false_and_keeps_counter_clear(self):
        assert agent._register_net_poll("t1", guest_reachable=True) is False
        assert "t1" not in agent._net_dead_polls

    def test_single_dead_poll_does_not_trigger(self):
        # One blip must not rebuild — only at the threshold.
        assert agent._register_net_poll("t1", guest_reachable=False) is False
        assert agent._net_dead_polls["t1"] == 1

    def test_fires_exactly_at_threshold(self):
        thr = agent._NET_DEAD_THRESHOLD
        results = [agent._register_net_poll("t1", guest_reachable=False)
                   for _ in range(thr)]
        # False for the first thr-1 polls, True on the thr-th.
        assert results[:-1] == [False] * (thr - 1)
        assert results[-1] is True

    def test_counter_resets_after_firing(self):
        thr = agent._NET_DEAD_THRESHOLD
        for _ in range(thr):
            agent._register_net_poll("t1", guest_reachable=False)
        # After firing, the counter is cleared so the next dead poll starts at 1.
        assert "t1" not in agent._net_dead_polls
        assert agent._register_net_poll("t1", guest_reachable=False) is False
        assert agent._net_dead_polls["t1"] == 1

    def test_reachable_poll_resets_a_partial_streak(self):
        # 2 dead polls, then a good one, must NOT carry over toward a rebuild.
        agent._register_net_poll("t1", guest_reachable=False)
        agent._register_net_poll("t1", guest_reachable=False)
        assert agent._register_net_poll("t1", guest_reachable=True) is False
        assert "t1" not in agent._net_dead_polls
        # A fresh streak now needs the full threshold again.
        fired = [agent._register_net_poll("t1", guest_reachable=False)
                 for _ in range(agent._NET_DEAD_THRESHOLD)]
        assert fired[-1] is True

    def test_counters_are_per_tenant(self):
        thr = agent._NET_DEAD_THRESHOLD
        for _ in range(thr - 1):
            agent._register_net_poll("t1", guest_reachable=False)
        # t2's first dead poll must be independent of t1's near-threshold streak.
        assert agent._register_net_poll("t2", guest_reachable=False) is False
        assert agent._net_dead_polls["t1"] == thr - 1
        assert agent._net_dead_polls["t2"] == 1


@pytest.mark.unit
class TestForceRelaunch:
    def test_force_relaunch_stops_then_launches(self):
        """force-relaunch must stop-vm (tear down stale FC+TAP) BEFORE launch-vm
        (rebuild) — order matters, otherwise launch races the dead TAP."""
        agent._recovering.discard("t1")
        with patch.object(agent.subprocess, "run") as m_run, \
             patch.object(agent.subprocess, "Popen") as m_popen:
            agent._force_relaunch_vm("t1", {"vm_num": 2, "vcpu": 2, "mem_mb": 4096})
        # stop-vm.sh via run(), launch-vm.sh via Popen()
        assert m_run.called, "stop-vm.sh was not invoked"
        assert m_popen.called, "launch-vm.sh was not invoked"
        stop_cmd = m_run.call_args[0][0]
        launch_cmd = m_popen.call_args[0][0]
        assert "stop-vm.sh" in " ".join(stop_cmd)
        assert "launch-vm.sh" in " ".join(launch_cmd)
        # passes the right vm_num/vcpu/mem to launch
        assert launch_cmd[-3:] == ["2", "2", "4096"]

    def test_force_relaunch_skips_if_already_recovering(self):
        agent._recovering.add("t1")
        with patch.object(agent.subprocess, "run") as m_run, \
             patch.object(agent.subprocess, "Popen") as m_popen:
            agent._force_relaunch_vm("t1", {"vm_num": 1})
        assert not m_run.called
        assert not m_popen.called
        agent._recovering.discard("t1")
