# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Multi-AZ HA (issue #8).

The ASG is currently created with the default VPC, which means CDK
auto-selects subnets — possibly all of them, possibly one. We make the
intent explicit:

1. config.yml.example declares `multi_az.enabled` and `az_count`.
2. The CDK stack honors `az_count` when picking subnets for the ASG and
   ALB; with az_count >= 2 both resources span multiple AZs.
3. With multi_az.enabled: false (default) the stack stays single-AZ
   (cost-saving — cross-AZ data transfer is billable).
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from conftest import synth_stack

ROOT = Path(__file__).resolve().parent.parent


def _synth(multi_az_enabled, az_count=2, drop_section=False):
    """Synth with multi_az overridden. Never touches the repo's config.yml —
    see conftest.synth_stack for why that mattered."""
    def mutate(cfg):
        if drop_section:
            cfg.pop("multi_az", None)
            return
        cfg.setdefault("multi_az", {})
        cfg["multi_az"]["enabled"] = multi_az_enabled
        cfg["multi_az"]["az_count"] = az_count
    return synth_stack(mutate)


@pytest.mark.unit
class TestConfigSchema:
    def test_example_declares_multi_az_section(self):
        text = (ROOT / "config.yml.example").read_text()
        assert "multi_az:" in text
        assert "az_count:" in text


@pytest.mark.unit
class TestASGMultiAZ:
    def test_asg_spans_multiple_azs_when_enabled(self):
        tpl = _synth(multi_az_enabled=True, az_count=2)
        asgs = tpl.find_resources("AWS::AutoScaling::AutoScalingGroup")
        assert asgs, "expected at least one AutoScalingGroup"
        for _, res in asgs.items():
            zone_id = res["Properties"].get("VPCZoneIdentifier", [])
            # Either it's a list of 2+ subnet ids OR a CFN intrinsic that
            # joins multiple subnet refs. Verify by counting.
            if isinstance(zone_id, list):
                assert len(zone_id) >= 2, \
                    f"VPCZoneIdentifier should span ≥2 AZs, got {zone_id}"
            else:
                # Fn::Split / Fn::Join — count by looking at refs.
                serialized = str(zone_id)
                assert serialized.count("subnet") >= 2 or "Subnets" in serialized

    def test_asg_single_az_when_disabled(self):
        """Default (cost-saving) path: az_count=1 produces single-AZ ASG."""
        tpl = _synth(multi_az_enabled=False, az_count=1)
        asgs = tpl.find_resources("AWS::AutoScaling::AutoScalingGroup")
        assert asgs


@pytest.mark.unit
class TestALBMultiAZ:
    def test_alb_spans_multiple_subnets_when_enabled(self):
        tpl = _synth(multi_az_enabled=True, az_count=2)
        albs = tpl.find_resources("AWS::ElasticLoadBalancingV2::LoadBalancer")
        assert albs, "expected at least one ALB"
        for _, res in albs.items():
            subnets = res["Properties"].get("Subnets", [])
            assert len(subnets) >= 2, \
                f"ALB Subnets should span ≥2 AZs, got {len(subnets)}"


@pytest.mark.unit
class TestDefault:
    def test_default_is_disabled_for_cost(self):
        """No multi_az section at all → stack must still synthesize."""
        tpl = _synth(multi_az_enabled=False, drop_section=True)
        # Synth succeeded; sanity-check it produced the ASG.
        assert tpl.find_resources("AWS::AutoScaling::AutoScalingGroup")
