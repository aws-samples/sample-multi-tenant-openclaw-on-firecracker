# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guest addressing must scale past 254 VMs per host — and stay compatible.

The cap
-------
`launch-vm.sh` derived both the guest IP and the guest MAC from VM_NUM:

    GUEST_IP="${SUBNET_PREFIX}.${VM_NUM}.2"
    GUEST_MAC="AA:FC:00:00:00:$(printf '%02x' ${VM_NUM})"

Third IP octet and last MAC byte are both 8-bit, so VM_NUM = 255 yielded an
invalid IP *and* a MAC that wrapped to an already-live VM — two tenants on one
L2 segment, which is a cross-tenant data path. Nothing refused the request;
the corruption was silent. That capped a host at 254 microVMs, below the ~300
production density target (372 theoretical on a 768 GiB host), so it blocked
the density story outright.

The fix spreads VM_NUM over octets 2+3 and two MAC bytes, choosing the split so
1..254 renders EXACTLY as before — running VMs keep their addresses across an
upgrade. That backward-compatibility property is the thing most likely to be
broken by a later "simplification", so it is asserted value-by-value.

The derivation is executed through bash rather than reimplemented in Python:
a Python copy of the formula would happily agree with itself while the shell
script drifted.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCH = ROOT / "deploy" / "userdata" / "launch-vm.sh"

pytestmark = pytest.mark.unit


def _derive(vm_num, prefix="172.16", vpc_cidr=""):
    """Run launch-vm.sh's addressing block for VM_NUM and return the result.

    Extracts the block between the addressing banner and the GUEST_MAC
    assignment, so the test exercises the real shell arithmetic.
    """
    src = LAUNCH.read_text()
    start = src.index("# ── Guest addressing")
    end = src.index("GUEST_MAC=", start)
    end = src.index("\n", end)
    block = src[start:end]

    script = f"""
set -e
VM_NUM={vm_num}
SUBNET_PREFIX="{prefix}"
OC_VPC_CIDR="{vpc_cidr}"
log() {{ echo "LOG:$*"; }}
{block}
echo "IP=${{GUEST_IP}}"
echo "TAP_IP=${{HOST_TAP_IP}}"
echo "MAC=${{GUEST_MAC}}"
"""
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    out = dict(re.findall(r"^(IP|TAP_IP|MAC)=(.+)$", p.stdout, re.M))
    return p.returncode, out, p.stdout + p.stderr


class TestBackwardCompatible:
    """1..254 must render byte-identically to the pre-fix scheme."""

    @pytest.mark.parametrize("vm_num", [1, 2, 7, 100, 253, 254])
    def test_ip_unchanged(self, vm_num):
        rc, got, log = _derive(vm_num)
        assert rc == 0, log
        assert got["IP"] == f"172.16.{vm_num}.2", (
            "existing VMs would change IP on upgrade")
        assert got["TAP_IP"] == f"172.16.{vm_num}.1"

    @pytest.mark.parametrize("vm_num", [1, 5, 16, 254])
    def test_mac_unchanged(self, vm_num):
        rc, got, log = _derive(vm_num)
        assert rc == 0, log
        assert got["MAC"] == f"AA:FC:00:00:00:{vm_num:02x}", (
            "existing VMs would change MAC on upgrade")

    def test_default_prefix_fallback_still_works(self):
        rc, got, log = _derive(3, prefix="")
        assert rc == 0, log
        assert got["IP"] == "10.0.3.2", "the 10.0 fallback regressed"


class TestPast254:
    """The whole point: >254 must be valid, unique and non-colliding."""

    def test_255_is_valid_not_invalid(self):
        rc, got, log = _derive(255)
        assert rc == 0, log
        octets = [int(o) for o in got["IP"].split(".")]
        assert all(0 <= o <= 255 for o in octets), \
            f"invalid IP for VM_NUM=255: {got['IP']}"
        assert got["IP"] == "172.17.1.2"

    def test_255_does_not_collide_with_1(self):
        """The original bug: 255 wrapped onto VM_NUM 1's MAC."""
        _, a, _ = _derive(1)
        _, b, _ = _derive(255)
        assert a["MAC"] != b["MAC"], "MAC collision — two tenants share an L2 id"
        assert a["IP"] != b["IP"], "IP collision"

    def test_no_collisions_across_a_realistic_density(self):
        """400 VMs (past the 300 production target) must be all-distinct."""
        ips, macs = set(), set()
        for n in list(range(1, 12)) + [253, 254, 255, 256, 300, 380, 400]:
            rc, got, log = _derive(n)
            assert rc == 0, log
            assert got["IP"] not in ips, f"duplicate IP at VM_NUM={n}"
            assert got["MAC"] not in macs, f"duplicate MAC at VM_NUM={n}"
            ips.add(got["IP"])
            macs.add(got["MAC"])

    def test_guest_and_tap_share_a_subnet(self):
        """Point-to-point link: .1 and .2 of the same /24, at any VM_NUM."""
        for n in (1, 254, 255, 509):
            rc, got, log = _derive(n)
            assert rc == 0, log
            assert got["IP"].rsplit(".", 1)[0] == got["TAP_IP"].rsplit(".", 1)[0], \
                f"VM_NUM={n}: guest {got['IP']} and tap {got['TAP_IP']} differ in subnet"
            assert got["IP"].endswith(".2") and got["TAP_IP"].endswith(".1")


class TestFailsLoudly:
    """Silent corruption was the real defect — refusals must be explicit."""

    def test_zero_vm_num_is_rejected(self):
        rc, _, log = _derive(0)
        assert rc != 0, "VM_NUM=0 must be refused"
        assert "FATAL" in log

    def test_address_space_overflow_is_rejected(self):
        """Walking off the end of octet 2 must fail, not silently wrap."""
        rc, _, log = _derive(254 * 40, prefix="255.250")
        assert rc != 0, "overflow past 255 must be refused"
        assert "FATAL" in log

    def test_vpc_overlap_is_rejected(self):
        """An overlapping guest subnet blackholes the guest's VPC traffic."""
        rc, _, log = _derive(1, prefix="172.31", vpc_cidr="172.31.0.0/16")
        assert rc != 0, (
            "a guest subnet inside the VPC CIDR must be refused — the guest's "
            "default route is the host tap, so VPC traffic would blackhole")
        assert "collides" in log.lower()

    def test_non_overlapping_vpc_is_accepted(self):
        rc, got, log = _derive(1, prefix="172.16", vpc_cidr="172.31.0.0/16")
        assert rc == 0, log
        assert got["IP"] == "172.16.1.2"


class TestMigrateStaysInSync:
    """migrate-vm.sh restores onto the SAME address launch-vm.sh would pick."""

    def test_restore_derives_tap_ip_from_recorded_guest_ip(self):
        src = (ROOT / "deploy" / "userdata" / "migrate-vm.sh").read_text()
        assert 'R_HOST_TAP_IP="${SRC_GUEST_IP%.*}.1"' in src, (
            "the host tap address must be derived from the recorded guest IP; "
            "computing them by two different routes lets them disagree and "
            "silently strands the restored VM on the wrong subnet")

    def test_restore_fallback_uses_the_16bit_split(self):
        src = (ROOT / "deploy" / "userdata" / "migrate-vm.sh").read_text()
        assert "(SRC_VM_NUM - 1) / 254" in src and "(SRC_VM_NUM - 1) % 254" in src, (
            "migrate-vm.sh still uses the old 8-bit formula — a VM_NUM > 254 "
            "would restore onto an invalid or colliding address")
