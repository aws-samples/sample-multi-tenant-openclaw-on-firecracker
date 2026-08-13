# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""East-west isolation between tenants must exist, not just be documented.

The gap
-------
README advertised "iptables FORWARD DROP between tenant subnets", but
launch-vm.sh only ever installed:

  * `-I FORWARD 1 -i tap -d 169.254.169.254/.253 -j DROP`  (IMDS)
  * `-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT`
  * `-A FORWARD -i tap -o <host iface> -j ACCEPT`           (egress)

There was no `-P FORWARD DROP` and no tap↔tap rule, and the chain policy is
ACCEPT — so anything the two ACCEPTs didn't cover was forwarded anyway. Three
distinct vectors were open:

  1. VM↔VM on the same host: both taps are local, the kernel forwards between
     them directly.
  2. guest→its own host's listeners: that traffic goes through INPUT, which had
     no rules at all. nginx :80/:8081 serve every tenant's dashboard on the
     host; :8899 is host-agent's all-tenant health.
  3. guest→OTHER hosts' listeners: guest egress is MASQUERADEd to the host's
     private IP, so it looks fleet-internal and the security groups pass it
     (:80 is open to the whole VPC CIDR for the ALB health check). The T3-1
     peer-map makes every host a proxy for the entire fleet, so this reads ANY
     tenant's dashboard.

These are static assertions over the rendered script: the failure mode is a
missing/removed line, and there is no way to exercise real iptables in unit
tests. `tests/test_e2e.py` is where a live fleet gets probed.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCH = ROOT / "deploy" / "userdata" / "launch-vm.sh"
INIT_HOST = ROOT / "deploy" / "userdata" / "init-host.sh"

pytestmark = pytest.mark.unit


def _rules(text):
    """Every iptables invocation in the script, whitespace-normalised."""
    return [re.sub(r"\s+", " ", ln).strip()
            for ln in text.splitlines() if "iptables" in ln]


@pytest.fixture(scope="module")
def launch_src():
    return LAUNCH.read_text()


@pytest.fixture(scope="module")
def launch_rules(launch_src):
    return _rules(launch_src)


class TestVmToVmBlocked:
    """Vector 1 — the classic cross-tenant path."""

    def test_tap_to_tap_drop_exists(self, launch_rules):
        got = [r for r in launch_rules
               if "FORWARD" in r and "tap-vm+" in r and "-j DROP" in r]
        assert got, (
            "no rule blocks tap→tap forwarding; tenant A can reach tenant B's "
            "guest IP on the same host")

    def test_tap_to_tap_drop_is_inserted_before_conntrack_accept(self, launch_rules):
        """Must use -I (insert), not -A (append).

        The conntrack ESTABLISHED,RELATED ACCEPT is appended, so a DROP added
        after it would let an already-established cross-tenant flow continue.
        """
        drop = [r for r in launch_rules
                if "tap-vm+" in r and "-j DROP" in r and "-C " not in r]
        assert drop, "no tap→tap DROP install line (only an existence check?)"
        assert any("-I FORWARD 1" in r for r in drop), (
            f"tap→tap DROP must be inserted at the head of FORWARD: {drop}")


class TestGuestToOwnHostBlocked:
    """Vector 2 — INPUT chain, which had no rules whatsoever."""

    def test_input_drop_for_host_service_ports(self, launch_rules):
        got = [r for r in launch_rules if "INPUT" in r and "-j DROP" in r]
        assert got, (
            "no INPUT rule; a guest can reach its host's nginx :80/:8081 "
            "(every tenant's dashboard) and host-agent :8899")

    @pytest.mark.parametrize("port", ["80", "8081", "8899"])
    def test_each_sensitive_port_is_covered(self, launch_rules, port):
        covered = any("INPUT" in r and "-j DROP" in r and port in r
                      for r in launch_rules)
        assert covered, f"INPUT DROP does not cover tcp/{port}"

    def test_input_rule_is_tap_scoped(self, launch_rules):
        """Never drop these ports for non-guest sources — the ALB needs :80."""
        for r in launch_rules:
            if "INPUT" in r and "-j DROP" in r:
                assert "-i ${TAP}" in r or "-i tap" in r, (
                    f"INPUT DROP must be scoped to the guest tap, else it "
                    f"blocks the ALB health check: {r}")

    def test_dns_is_not_blocked(self, launch_src):
        """The guest resolves through the host — udp/53 must stay open."""
        for r in _rules(launch_src):
            if "-j DROP" in r and ("--dports" in r or "--dport" in r):
                assert "53" not in re.sub(r"\b8899\b|\b8081\b|\b80\b", "", r), \
                    f"a DROP rule appears to cover port 53: {r}"


class TestGuestToFleetBlocked:
    """Vector 3 — amplified by the T3-1 peer-map to the whole fleet."""

    def test_vpc_scoped_drop_exists(self, launch_rules):
        got = [r for r in launch_rules
               if "FORWARD" in r and "OC_VPC_CIDR" in r and "-j DROP" in r]
        assert got, (
            "no rule blocks guest→VPC:80/8081/8899; with the peer-map this "
            "reads any tenant's dashboard in the fleet")

    def test_drop_is_cidr_scoped_not_blanket(self, launch_rules):
        """Blanket --dport 80 would break the agent's public HTTP egress."""
        for r in launch_rules:
            if "FORWARD" in r and "-j DROP" in r and "--dports" in r:
                assert "-d " in r, (
                    f"east-west port DROP must be destination-scoped: {r}")

    def test_vpc_cidr_is_plumbed_end_to_end(self):
        """The rule is inert unless the CIDR actually reaches the host."""
        init = INIT_HOST.read_text()
        assert "OC_VPC_CIDR={{VPC_CIDR}}" in init, \
            "init-host.sh must export OC_VPC_CIDR into /etc/platform.env"
        stack = (ROOT / "deploy" / "stack.py").read_text()
        assert '"{{VPC_CIDR}}"' in stack and "vpc_cidr_block" in stack, \
            "stack.py must substitute {{VPC_CIDR}} with the real VPC CIDR"

    def test_missing_cidr_is_loud_not_silent(self, launch_src):
        """An unsubstituted placeholder must warn, never look like success."""
        assert re.search(r"OC_VPC_CIDR.*\n.*WARN|WARN.*OC_VPC_CIDR",
                         launch_src, re.I | re.S), (
            "when OC_VPC_CIDR is unset the script must log a warning — a "
            "silently-skipped security control is worse than a missing one")


class TestImdsStillBlocked:
    """Pre-existing control that the new rules must not disturb."""

    @pytest.mark.parametrize("addr", ["169.254.169.254", "169.254.169.253"])
    def test_imds_drop_present(self, launch_rules, addr):
        got = [r for r in launch_rules if addr in r and "-j DROP" in r]
        assert got, f"IMDS DROP for {addr} missing"

    def test_egress_still_allowed(self, launch_rules):
        got = [r for r in launch_rules
               if "FORWARD" in r and "HOST_IFACE" in r and "-j ACCEPT" in r]
        assert got, "guest internet egress rule disappeared"
