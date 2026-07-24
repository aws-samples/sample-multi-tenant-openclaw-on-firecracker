# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3-3 headline regression: capacity verdict parity across call sites.

Before T3-3, three places decided "can this VM go on this host?" differently:
the API scheduler (_host_fits) applied overcommit + memory + MAX_VMS; the
health_check AZ-failover placer (pick_target_host) applied none of those. So
failover could place a VM on a memory-exhausted or VM-capped host the API would
reject. This test pins that they now agree — one function, both call sites.
"""

import importlib.util
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from common import capacity

pytestmark = pytest.mark.unit


def _load_hc(env=None):
    import os
    saved = {}
    for k, v in (env or {}).items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    with patch("boto3.resource", return_value=MagicMock()), \
         patch("boto3.client", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location(
            "hc_parity", "deploy/lambda/health_check/handler.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hc_parity"] = mod
        spec.loader.exec_module(mod)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return mod


def _host(iid="i-1", total_vcpu=8, total_mem_mb=16384,
          used_vcpu=0, used_mem_mb=0, vm_count=0, seconds_fresh=5):
    ts = (datetime.now(timezone.utc)).isoformat()
    return {"instance_id": iid, "az": "az-b", "status": "active",
            "last_health_check": ts, "total_vcpu": total_vcpu,
            "total_mem_mb": total_mem_mb, "used_vcpu": used_vcpu,
            "used_mem_mb": used_mem_mb, "vm_count": vm_count}


PARAMS = [
    # (cpu_ratio, mem_ratio, max_vms, host_kwargs, need_vcpu, need_mem)
    (1.0, 1.0, 0, dict(used_vcpu=0, used_mem_mb=0), 2, 4096),          # plenty
    (1.0, 1.0, 0, dict(used_vcpu=7), 2, 4096),                        # cpu short
    (1.0, 1.0, 0, dict(used_mem_mb=16384), 1, 1024),                  # MEM FULL — the bug
    (1.5, 1.0, 0, dict(used_vcpu=10), 2, 4096),                       # overcommit lets it fit
    (1.0, 1.0, 3, dict(vm_count=3), 1, 1024),                         # vm-capped
    (1.0, 1.0, 5, dict(vm_count=3), 1, 1024),                         # under cap
]


class TestCapacityParity:
    @pytest.mark.parametrize("cpu,mem,maxvms,hk,nv,nm", PARAMS)
    def test_api_fits_equals_failover_selects(self, cpu, mem, maxvms, hk, nv, nm):
        env = {"CPU_OVERCOMMIT_RATIO": str(cpu), "MEM_OVERCOMMIT_RATIO": str(mem),
               "MAX_VMS_PER_HOST": str(maxvms)}
        hc = _load_hc(env)
        host = _host(**hk)

        # The unified predicate (what the API scheduler uses).
        api_fits = capacity.host_fits(host, nv, nm, cpu, mem, maxvms)

        # What AZ failover would do: does pick_target_host select this host?
        now = datetime.now(timezone.utc)
        picked = hc.pick_target_host([host], now, threshold_minutes=10,
                                     exclude_azs=set(),
                                     required_vcpu=nv, required_mem=nm)
        failover_selects = picked is not None

        assert api_fits == failover_selects, (
            f"divergence: api_fits={api_fits} but failover_selects="
            f"{failover_selects} for host={hk} need=({nv},{nm}) "
            f"ratios=({cpu},{mem}) max_vms={maxvms}")

    def test_memory_exhausted_host_rejected_by_failover(self):
        """The exact bug: a host with free vCPU but used_mem == total_mem must
        NOT be selected. Old pick_target_host (no mem check) would pick it."""
        hc = _load_hc({"CPU_OVERCOMMIT_RATIO": "1.0", "MEM_OVERCOMMIT_RATIO": "1.0",
                       "MAX_VMS_PER_HOST": "0"})
        host = _host(used_vcpu=0, used_mem_mb=16384)  # tons of vCPU, zero mem
        now = datetime.now(timezone.utc)
        picked = hc.pick_target_host([host], now, threshold_minutes=10,
                                     exclude_azs=set(),
                                     required_vcpu=2, required_mem=4096)
        assert picked is None, "failover must reject a memory-exhausted host"

    def test_vm_capped_host_rejected_by_failover(self):
        hc = _load_hc({"CPU_OVERCOMMIT_RATIO": "1.0", "MEM_OVERCOMMIT_RATIO": "1.0",
                       "MAX_VMS_PER_HOST": "3"})
        host = _host(vm_count=3)  # at the cap, but plenty of raw capacity
        now = datetime.now(timezone.utc)
        picked = hc.pick_target_host([host], now, threshold_minutes=10,
                                     exclude_azs=set(),
                                     required_vcpu=1, required_mem=1024)
        assert picked is None, "failover must respect MAX_VMS_PER_HOST"
