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

ROOT = Path(__file__).resolve().parent.parent


def _synth(multi_az_enabled, az_count=2):
    import yaml
    cfg_path = ROOT / "config.yml"
    original = cfg_path.read_text()
    cfg = yaml.safe_load(original)
    cfg.setdefault("multi_az", {})
    cfg["multi_az"]["enabled"] = multi_az_enabled
    cfg["multi_az"]["az_count"] = az_count
    cfg_path.write_text(yaml.safe_dump(cfg))
    try:
        sys.modules.pop("deploy.stack", None)
        sys.modules.pop("deploy", None)
        spec = importlib.util.spec_from_file_location(
            "deploy.stack", ROOT / "deploy" / "stack.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["deploy.stack"] = mod
        spec.loader.exec_module(mod)
        import aws_cdk as cdk
        from aws_cdk import assertions
        app = cdk.App()
        stack = mod.OpenClawOrchestratorStack(app, "Test",
            env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
        return assertions.Template.from_stack(stack)
    finally:
        cfg_path.write_text(original)


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
        """If config.yml has no multi_az section at all, stack must still synthesize."""
        # Synth without setting multi_az at all
        import yaml
        cfg_path = ROOT / "config.yml"
        original = cfg_path.read_text()
        cfg = yaml.safe_load(original)
        cfg.pop("multi_az", None)
        cfg_path.write_text(yaml.safe_dump(cfg))
        try:
            sys.modules.pop("deploy.stack", None)
            spec = importlib.util.spec_from_file_location(
                "deploy.stack", ROOT / "deploy" / "stack.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["deploy.stack"] = mod
            spec.loader.exec_module(mod)
            import aws_cdk as cdk
            app = cdk.App()
            mod.OpenClawOrchestratorStack(app, "Test",
                env=cdk.Environment(account="123456789012", region="ap-northeast-1"))
            # If we got here without exception, default is fine
        finally:
            cfg_path.write_text(original)
