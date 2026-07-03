# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""10h-goal #17: reserve-capacity warm pool. The scaler keeps a free-vCPU buffer
(max of RESERVE_PCT% of allocatable, RESERVE_CORES) by proactively scaling the
ASG out when free dips below it. Verifies: buffer healthy → no scale; breached →
scale out one step; at MaxSize → no scale (logs a warning); only active/idle
hosts count toward capacity.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch
import pytest

# All tests in this module are pure-mock unit tests (no real AWS); mark them
# so `pytest -m unit` includes them (loop 2026-07-02: found 136 tests were
# silently excluded from the unit suite for lack of this marker).
pytestmark = pytest.mark.unit

os.environ.setdefault("HOSTS_TABLE", "openclaw-hosts")
os.environ.setdefault("TENANTS_TABLE", "openclaw-tenants")
os.environ.setdefault("ASG_NAME", "openclaw-hosts-asg")
os.environ.setdefault("IDLE_TIMEOUT_MINUTES", "10")

_mock_ddb = MagicMock()
_mock_asg = MagicMock()
with patch("boto3.resource", return_value=_mock_ddb), patch("boto3.client") as _mc:
    _mc.side_effect = lambda svc, **kw: (
        _mock_asg if svc == "autoscaling" else MagicMock()
    )
    spec = importlib.util.spec_from_file_location(
        "scaler_handler", "deploy/lambda/scaler/handler.py"
    )
    sc = importlib.util.module_from_spec(spec)
    sys.modules["scaler_handler"] = sc
    spec.loader.exec_module(sc)


def _host(vcpu=96, used=0, status="active"):
    return {
        "instance_id": "i-x",
        "total_vcpu": vcpu,
        "used_vcpu": used,
        "status": status,
    }


def _asg(desired, mn, mx):
    return {
        "AutoScalingGroups": [
            {"DesiredCapacity": desired, "MinSize": mn, "MaxSize": mx}
        ]
    }


class TestReservePool:
    def test_buffer_healthy_no_scale(self):
        # 1 host 96 vCPU, 0 used → free 96, target 20% of 96 = 19.2 → healthy
        sc.autoscaling = MagicMock()
        with (
            patch.object(sc, "RESERVE_PCT", 20),
            patch.object(sc, "RESERVE_CORES", 0),
            patch.object(sc, "CPU_OVERCOMMIT_RATIO", 1.0),
        ):
            sc._ensure_reserve_capacity([_host(96, 0)])
        sc.autoscaling.set_desired_capacity.assert_not_called()

    def test_buffer_breached_scales_out(self):
        # 1 host 96 vCPU, 90 used → free 6 < target 19.2 → scale out
        sc.autoscaling = MagicMock()
        sc.autoscaling.describe_auto_scaling_groups.return_value = _asg(1, 1, 5)
        with (
            patch.object(sc, "RESERVE_PCT", 20),
            patch.object(sc, "RESERVE_CORES", 0),
            patch.object(sc, "CPU_OVERCOMMIT_RATIO", 1.0),
            patch.object(sc, "RESERVE_SCALE_STEP", 1),
        ):
            sc._ensure_reserve_capacity([_host(96, 90)])
        sc.autoscaling.set_desired_capacity.assert_called_once()
        kw = sc.autoscaling.set_desired_capacity.call_args.kwargs
        assert kw["DesiredCapacity"] == 2  # 1 → 2

    def test_at_maxsize_no_scale(self):
        sc.autoscaling = MagicMock()
        sc.autoscaling.describe_auto_scaling_groups.return_value = _asg(5, 1, 5)
        with (
            patch.object(sc, "RESERVE_PCT", 20),
            patch.object(sc, "RESERVE_CORES", 0),
            patch.object(sc, "CPU_OVERCOMMIT_RATIO", 1.0),
        ):
            sc._ensure_reserve_capacity([_host(96, 95)])
        sc.autoscaling.set_desired_capacity.assert_not_called()

    def test_absolute_core_floor(self):
        # RESERVE_CORES=30 dominates 20% (=19.2). free 25 < 30 → scale out
        sc.autoscaling = MagicMock()
        sc.autoscaling.describe_auto_scaling_groups.return_value = _asg(2, 1, 10)
        with (
            patch.object(sc, "RESERVE_PCT", 20),
            patch.object(sc, "RESERVE_CORES", 30),
            patch.object(sc, "CPU_OVERCOMMIT_RATIO", 1.0),
        ):
            sc._ensure_reserve_capacity([_host(96, 71)])  # free 25
        sc.autoscaling.set_desired_capacity.assert_called_once()

    def test_draining_host_excluded(self):
        # draining host shouldn't count as serving capacity → free 0 < target → scale
        sc.autoscaling = MagicMock()
        sc.autoscaling.describe_auto_scaling_groups.return_value = _asg(1, 1, 5)
        with (
            patch.object(sc, "RESERVE_PCT", 20),
            patch.object(sc, "RESERVE_CORES", 0),
            patch.object(sc, "CPU_OVERCOMMIT_RATIO", 1.0),
        ):
            sc._ensure_reserve_capacity([_host(96, 0, status="draining")])
        # total_alloc=0 → target 0 → free 0 >= 0 → no scale (no serving hosts, target 0)
        sc.autoscaling.set_desired_capacity.assert_not_called()

    def test_overcommit_raises_allocatable(self):
        # overcommit 2.0 → 96 vCPU host has 192 allocatable, 100 used → free 92, healthy
        sc.autoscaling = MagicMock()
        with (
            patch.object(sc, "RESERVE_PCT", 20),
            patch.object(sc, "RESERVE_CORES", 0),
            patch.object(sc, "CPU_OVERCOMMIT_RATIO", 2.0),
        ):
            sc._ensure_reserve_capacity([_host(96, 100)])
        sc.autoscaling.set_desired_capacity.assert_not_called()
