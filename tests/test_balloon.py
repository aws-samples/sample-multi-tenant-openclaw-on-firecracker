# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the balloon memory reclamation controller in host-agent.py."""

import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with (
    patch("boto3.resource", return_value=_mock_ddb),
    patch("boto3.client", return_value=_mock_ssm),
):
    os.environ["BALLOON_ENABLED"] = "true"
    os.environ["BALLOON_MAX_INFLATE_RATIO"] = "0.4"
    os.environ["BALLOON_MIN_GUEST_AVAILABLE_MB"] = "512"
    spec = importlib.util.spec_from_file_location(
        "agent", "deploy/userdata/host-agent.py"
    )
    agent = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = agent
    spec.loader.exec_module(agent)

# Clear the balloon env vars after import so BALLOON_ENABLED=true does not leak
# process-wide into other test modules.
for _k in (
    "BALLOON_ENABLED",
    "BALLOON_MAX_INFLATE_RATIO",
    "BALLOON_MIN_GUEST_AVAILABLE_MB",
):
    os.environ.pop(_k, None)


def _make_vm(tmp_path, tenant_id, mem_mb=4096):
    vm_dir = tmp_path / tenant_id
    vm_dir.mkdir()
    (vm_dir / "vm.json").write_text(
        json.dumps({"mem_mb": mem_mb}), encoding="utf-8"
    )
    (vm_dir / "fc.sock").touch()
    return str(vm_dir / "fc.sock")


class TestBalloonController:
    @pytest.fixture(autouse=True)
    def _grant_lifecycle_lock(self):
        """Hand out a real, closeable fd for the per-tenant lifecycle flock.

        The controller holds `/run/lock/oc-launch-<tid>.lock` across the deflate
        re-read/identity/PATCH sequence. `/run` is not writable in a test sandbox, and
        the point of these tests is the decision logic, not the kernel's flock. Tests
        that care about lock contention patch `_acquire_tenant_lock` themselves.
        """
        opened = []

        def grant(tid, wait_sec=None, who="balloon"):
            fd = os.open(os.devnull, os.O_RDWR)
            opened.append(fd)
            return fd

        with patch.object(agent, "_acquire_tenant_lock", side_effect=grant):
            yield
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass

    def setup_method(self):
        self.original_vm_dir = agent.VM_DIR
        agent.BALLOON_ENABLED = True
        agent.BALLOON_MAX_INFLATE_RATIO = 0.4
        agent.BALLOON_MIN_GUEST_AVAILABLE_MB = 512
        agent.BALLOON_STEP_MIB = 64
        agent.BALLOON_CUSHION_MIB = 64
        agent.BALLOON_CONVERGE_TOLERANCE_MIB = 16
        agent.BALLOON_MAX_ACTIONS_PER_CYCLE = 20
        agent.BALLOON_DEFLATE_BATCH = 5
        agent.BALLOON_ALLOW_BLIND_INFLATE = False
        agent.BALLOON_MAX_ATTEMPTS_PER_CYCLE = 10
        agent.BALLOON_CYCLE_BUDGET_SEC = 30.0
        agent._balloon_deflate_cursor = 0
        agent._balloon_scan_cursor = 0

        agent._balloon_metrics = None

    def teardown_method(self):
        agent.VM_DIR = self.original_vm_dir
        agent._balloon_metrics = None

    @pytest.mark.unit
    def test_top_level_available_memory_is_parsed(self):
        stats = {
            "available_memory": 1024 * 1024 * 1024,
            "target_mib": 0,
            "actual_mib": 0,
        }

        assert agent._balloon_available_mib(stats) == 1024

    @pytest.mark.unit
    def test_nested_available_memory_is_missing(self):
        stats = {"stats": {"available_memory": 1024 * 1024 * 1024}}

        assert agent._balloon_available_mib(stats) is None

    @pytest.mark.unit
    def test_inflate_happens_on_healthy_signal(self, tmp_path):
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        sock_file = _make_vm(tmp_path, tenant_id, mem_mb=1024)
        stats = {
            "available_memory": 1024 * 1024 * 1024,
            "target_mib": 0,
            "actual_mib": 0,
        }

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 100)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_called_once_with(sock_file, 64)

    @pytest.mark.unit
    def test_missing_stats_warns_and_does_not_inflate(self, tmp_path, capsys):
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 0, "actual_mib": 0}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 100)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        assert "balloon stats unavailable tenant-a" in capsys.readouterr().out
        assert agent._balloon_cycle["stats_unavailable"] == 1
        set_target.assert_not_called()

    @pytest.mark.unit
    def test_blind_inflate_opt_in(self, tmp_path):
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        agent.BALLOON_ALLOW_BLIND_INFLATE = True
        sock_file = _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 0, "actual_mib": 0}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 100)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_called_once_with(sock_file, 64)

    @pytest.mark.unit
    def test_guest_reserve_clamps_inflate_step(self, tmp_path):
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        sock_file = _make_vm(tmp_path, tenant_id)
        stats = {
            "available_memory": 600 * 1024 * 1024,
            "target_mib": 0,
            "actual_mib": 0,
        }

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 100)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_called_once_with(sock_file, 24)

    @pytest.mark.unit
    def test_inflate_cap_is_respected(self, tmp_path):
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id, mem_mb=1000)
        stats = {
            "available_memory": 1024 * 1024 * 1024,
            "target_mib": 400,
            "actual_mib": 400,
        }

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 100)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_not_called()

    @pytest.mark.unit
    def test_inflate_waits_for_convergence(self, tmp_path):
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id)
        stats = {
            "available_memory": 1024 * 1024 * 1024,
            "target_mib": 128,
            "actual_mib": 0,
        }

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 100)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_not_called()

    @pytest.mark.unit
    def test_deflate_steps_down_without_zeroing(self, tmp_path):
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        sock_file = _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 512, "actual_mib": 512}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_called_once_with(sock_file, 448)

    @pytest.mark.unit
    def test_deflate_is_not_blocked_by_unconverged_target(self, tmp_path):
        """`deflate_on_oom` drives actual below target; deflation must still run.

        If the convergence gate applied to deflation too, a guest that handed
        pages back under its own memory pressure could never have the target it
        cannot reach lowered, and would stay pinned against it indefinitely while
        the driver kept retrying.
        """
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        sock_file = _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 512, "actual_mib": 192}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target", return_value=True) as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        # Steps down from actual (192), not from the unreachable target (512).
        set_target.assert_called_once_with(sock_file, 128)

    @pytest.mark.unit
    def test_deflate_cancels_a_target_the_guest_fully_gave_back(self, tmp_path):
        """actual_mib == 0 with target_mib > 0 is the worst case, not a no-op."""
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        sock_file = _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 512, "actual_mib": 0}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target", return_value=True) as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_called_once_with(sock_file, 0)

    @pytest.mark.unit
    def test_deflate_does_not_starve_behind_a_persistently_failing_vm(self, tmp_path):
        """A VM whose PATCH always fails must not block the tenants behind it.

        The batch is "this many landed PATCHes", not "the first N candidates".
        """
        agent.VM_DIR = str(tmp_path)
        agent.BALLOON_DEFLATE_BATCH = 2
        probe_results = {}
        socks = {}
        for index in range(5):
            tenant_id = f"tenant-{index}"
            socks[tenant_id] = _make_vm(tmp_path, tenant_id)
            probe_results[tenant_id] = {"vm_health": "up"}
        stats = {"target_mib": 128, "actual_mib": 128}
        wedged = {socks["tenant-0"], socks["tenant-1"]}

        def flaky(sock_file, amount_mib):
            return sock_file not in wedged

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target", side_effect=flaky) as set_target,
        ):
            agent._adjust_balloons(probe_results)

        landed = [
            c.args[0] for c in set_target.call_args_list if c.args[0] not in wedged
        ]
        assert len(landed) == 2, "two PATCHes must land despite the wedged head"
        assert agent._balloon_cycle["actions"] == 2

    @pytest.mark.unit
    def test_deflate_bounds_attempts_so_the_poll_loop_cannot_stall(self, tmp_path):
        """Walking past failures must be bounded, not unbounded.

        Each failing PATCH can burn curl's --max-time, and _adjust_balloons runs
        inline in the poll loop, so an unbounded walk over a large failing fleet
        would stall the host heartbeat and the DDB reconcile for minutes.
        """
        agent.VM_DIR = str(tmp_path)
        agent.BALLOON_DEFLATE_BATCH = 5
        agent.BALLOON_MAX_ATTEMPTS_PER_CYCLE = 6
        agent._balloon_deflate_cursor = 0
        probe_results = {}
        for index in range(30):
            tenant_id = f"tenant-{index:02d}"
            _make_vm(tmp_path, tenant_id)
            probe_results[tenant_id] = {"vm_health": "up"}
        stats = {"target_mib": 128, "actual_mib": 128}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target", return_value=False) as set_target,
        ):
            agent._adjust_balloons(probe_results)

        assert set_target.call_count == 6, "attempts must stop at the cap"
        assert agent._balloon_cycle["actions"] == 0

    @pytest.mark.unit
    def test_deflate_cursor_rotates_so_bounding_does_not_re_add_starvation(
        self, tmp_path
    ):
        """Bounding attempts alone would let a wedged head eat the budget forever."""
        agent.VM_DIR = str(tmp_path)
        agent.BALLOON_DEFLATE_BATCH = 5
        agent.BALLOON_MAX_ATTEMPTS_PER_CYCLE = 4
        agent._balloon_deflate_cursor = 0
        probe_results = {}
        for index in range(12):
            tenant_id = f"tenant-{index:02d}"
            _make_vm(tmp_path, tenant_id)
            probe_results[tenant_id] = {"vm_health": "up"}
        stats = {"target_mib": 128, "actual_mib": 128}

        def run_one_cycle():
            with (
                patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
                patch.object(agent, "_get_balloon_stats", return_value=stats),
                patch.object(
                    agent, "_set_balloon_target", return_value=False
                ) as set_target,
            ):
                agent._adjust_balloons(probe_results)
            return [c.args[0] for c in set_target.call_args_list]

        first = run_one_cycle()
        second = run_one_cycle()

        assert len(first) == 4 and len(second) == 4
        assert not set(first) & set(second), (
            "the second cycle must reach candidates the first cycle never attempted"
        )

    @pytest.mark.unit
    def test_deflate_discards_a_target_staged_for_an_earlier_incarnation(
        self, tmp_path, capsys
    ):
        """fc.sock is a fixed path per tenant, so PID reuse must be checked.

        A VM stopped and relaunched between staging the candidate and applying the
        PATCH is reachable at the same socket; applying the staged target would
        silently INFLATE a fresh VM that asked for nothing.
        """
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 512, "actual_mib": 512}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(
                agent, "_read_proc_start_ticks", side_effect=[111, 222]
            ),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons(
                {tenant_id: {"vm_health": "up", "fc_pid": 4242}}
            )

        set_target.assert_not_called()
        assert "VM identity changed" in capsys.readouterr().out

    @pytest.mark.unit
    def test_deflate_applies_when_identity_is_unchanged(self, tmp_path):
        """The identity guard must not block the normal path."""
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        sock_file = _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 512, "actual_mib": 512}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_read_proc_start_ticks", return_value=111),
            patch.object(agent, "_set_balloon_target", return_value=True) as set_target,
        ):
            agent._adjust_balloons(
                {tenant_id: {"vm_health": "up", "fc_pid": 4242}}
            )

        set_target.assert_called_once_with(sock_file, 448)

    @pytest.mark.unit
    def test_deflate_never_raises_a_target_that_is_already_zero(self, tmp_path):
        """target_mib=0 with the guest still draining must not be pushed back up.

        actual_mib > step while target is already 0 means a full deflate is in
        flight. Computing `actual - step` yields a positive number, so without the
        guard the PATCH would raise the target from 0 and reverse the drain.
        """
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 0, "actual_mib": 256}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target", return_value=True) as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_not_called()

    @pytest.mark.unit
    def test_deflate_skips_a_tenant_whose_lifecycle_lock_is_held(self, tmp_path):
        """A tenant mid launch/stop/delete/migrate must be left alone this cycle."""
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 512, "actual_mib": 512}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_acquire_tenant_lock", return_value=None),
            patch.object(agent, "_set_balloon_target", return_value=True) as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        set_target.assert_not_called()

    @pytest.mark.unit
    def test_cycle_time_budget_bounds_the_statistics_scan(self, tmp_path, capsys):
        """Every VM costs a statistics GET; the pass must be time-bounded.

        The attempt cap only bounds PATCHes. A fleet of slow sockets would otherwise
        hold the poll loop — and therefore the heartbeat — for minutes.
        """
        agent.VM_DIR = str(tmp_path)
        agent.BALLOON_CYCLE_BUDGET_SEC = 0.05
        probe_results = {}
        for index in range(40):
            tenant_id = f"tenant-{index:02d}"
            _make_vm(tmp_path, tenant_id)
            probe_results[tenant_id] = {"vm_health": "up"}
        stats = {"target_mib": 128, "actual_mib": 128}

        def slow_stats(sock_file):
            time.sleep(0.01)
            return stats

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", side_effect=slow_stats),
            patch.object(agent, "_set_balloon_target", return_value=True),
        ):
            agent._adjust_balloons(probe_results)

        out = capsys.readouterr().out
        assert "cycle time budget" in out
        assert agent._balloon_scan_cursor not in (0,), (
            "the scan cursor must advance so the untouched tail is reached next cycle"
        )

    @pytest.mark.unit
    def test_deflate_obeys_batch_limit(self, tmp_path):
        agent.VM_DIR = str(tmp_path)
        agent.BALLOON_DEFLATE_BATCH = 2
        probe_results = {}
        for index in range(4):
            tenant_id = f"tenant-{index}"
            _make_vm(tmp_path, tenant_id)
            probe_results[tenant_id] = {"vm_health": "up"}
        stats = {"target_mib": 128, "actual_mib": 128}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 500)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons(probe_results)

        assert set_target.call_count == 2
        assert all(call.args[1] == 64 for call in set_target.call_args_list)

    @pytest.mark.unit
    def test_deflate_refuses_when_host_cannot_absorb_batch(self, tmp_path):
        agent.VM_DIR = str(tmp_path)
        agent.BALLOON_DEFLATE_BATCH = 3
        probe_results = {}
        for index in range(3):
            tenant_id = f"tenant-{index}"
            _make_vm(tmp_path, tenant_id)
            probe_results[tenant_id] = {"vm_health": "up"}
        stats = {"target_mib": 128, "actual_mib": 128}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(300, 130)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons(probe_results)

        set_target.assert_not_called()

    @pytest.mark.unit
    def test_per_cycle_action_budget(self, tmp_path):
        agent.VM_DIR = str(tmp_path)
        agent.BALLOON_MAX_ACTIONS_PER_CYCLE = 2
        probe_results = {}
        for index in range(4):
            tenant_id = f"tenant-{index}"
            _make_vm(tmp_path, tenant_id)
            probe_results[tenant_id] = {"vm_health": "up"}
        stats = {
            "available_memory": 1024 * 1024 * 1024,
            "target_mib": 0,
            "actual_mib": 0,
        }

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 100)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons(probe_results)

        assert set_target.call_count == 2

    @pytest.mark.unit
    def test_disabled_controller_does_nothing(self):
        agent.BALLOON_ENABLED = False

        with patch.object(agent, "_get_host_mem_info") as get_host_mem_info:
            agent._adjust_balloons({"tenant-a": {"vm_health": "up"}})

        get_host_mem_info.assert_not_called()

    @pytest.mark.unit
    def test_set_target_reports_failure_on_nonzero_curl(self, capsys):
        """A rejected PATCH must not look like a landed one."""
        failed = MagicMock(returncode=22, stderr=b"curl: (22) HTTP 400")

        with patch.object(agent.subprocess, "run", return_value=failed):
            assert agent._set_balloon_target("/tmp/fc.sock", 128) is False

        assert "balloon set failed" in capsys.readouterr().out

        ok = MagicMock(returncode=0, stderr=b"")
        with patch.object(agent.subprocess, "run", return_value=ok):
            assert agent._set_balloon_target("/tmp/fc.sock", 128) is True

    @pytest.mark.unit
    def test_failed_patch_is_not_counted_as_an_action(self, tmp_path, capsys):
        """`actions` must count landed PATCHes, not attempts.

        Counting attempts is the same silent-failure-with-green-telemetry shape
        the controller exists to remove.
        """
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id)
        stats = {
            "available_memory": 1024 * 1024 * 1024,
            "target_mib": 0,
            "actual_mib": 0,
        }

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 100)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target", return_value=False),
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        assert agent._balloon_cycle["actions"] == 0
        assert "balloon inflate" not in capsys.readouterr().out

    @pytest.mark.unit
    def test_unusable_stats_are_counted_outside_host_pressure(self, tmp_path):
        """The gauge must not depend on the branch that happens to be active.

        The inflate path only runs under host pressure, so counting the broken
        signal there would mean an operator only finds out the controller is
        blind at the moment it is needed. host_pressure 0.30 here is neither
        the inflate (<0.20) nor the deflate (>0.40) branch.
        """
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id)
        stats = {"target_mib": 0, "actual_mib": 0}

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 300)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target") as set_target,
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        assert agent._balloon_cycle["stats_unavailable"] == 1
        set_target.assert_not_called()

    @pytest.mark.unit
    def test_metrics_are_published_only_after_a_completed_cycle(self, tmp_path):
        tenant_id = "tenant-a"
        agent.VM_DIR = str(tmp_path)
        _make_vm(tmp_path, tenant_id)
        stats = {
            "available_memory": 1024 * 1024 * 1024,
            "target_mib": 0,
            "actual_mib": 128,
        }

        assert agent._balloon_metrics is None
        assert "openclaw_host_balloon_" not in agent._render_metrics_text(
            {}, balloon_stats=agent._balloon_metrics
        )

        with (
            patch.object(agent, "_get_host_mem_info", return_value=(1000, 300)),
            patch.object(agent, "_get_balloon_stats", return_value=stats),
            patch.object(agent, "_set_balloon_target"),
        ):
            agent._adjust_balloons({tenant_id: {"vm_health": "up"}})

        assert agent._balloon_metrics == {
            "actions": 0,
            "stats_unavailable": 0,
            "actual_mib": 128,
        }
        # Published snapshot must be a copy, not the live accumulator, so a
        # scrape that lands mid-cycle cannot observe the post-reset zeros.
        assert agent._balloon_metrics is not agent._balloon_cycle

    @pytest.mark.unit
    def test_balloon_metrics_are_optional(self):
        without_balloon = agent._render_metrics_text({}, balloon_stats=None)
        balloon_stats = {
            "actual_mib": 256,
            "stats_unavailable": 2,
            "actions": 1,
        }

        with_balloon = agent._render_metrics_text(
            {}, balloon_stats=balloon_stats
        )

        assert "openclaw_host_balloon_" not in without_balloon
        assert "openclaw_host_balloon_actual_mib 256" in with_balloon
        assert "openclaw_host_balloon_stats_unavailable 2" in with_balloon
        assert "openclaw_host_balloon_actions 1" in with_balloon
