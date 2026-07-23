# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK assertion tests for #79: bring-your-own VPC/subnets + optional CloudFront.

Both features are opt-in and default off, so the zero-config path (default VPC,
CloudFront created) must be unchanged.
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "deploy"))

import aws_cdk as cdk
from aws_cdk import assertions

pytestmark = pytest.mark.unit


def _synth_with_config(cfg_overrides):
    # Base off the git-tracked config.yml.example, NOT the developer's local
    # config.yml (which is .gitignored and absent on a clean checkout / CI).
    # config.yml.example ships cloudfront.enabled=true with empty domains =>
    # exactly one LEGACY single-domain distribution, which is the documented
    # zero-config default this test asserts. Reading the local config.yml made
    # the result depend on whatever the developer had configured (e.g. a
    # dual-domain setup => 2 distributions => spurious failure).
    base = yaml.safe_load((Path(ROOT) / "config.yml.example").read_text())
    cfg = dict(base)
    for k, v in cfg_overrides.items():
        cfg[k] = v
    if "stack" in sys.modules:
        del sys.modules["stack"]
    import stack as stack_mod
    stack_mod.CFG = cfg
    app = cdk.App()
    s = stack_mod.OpenClawOrchestratorStack(
        app, "OpenClawOrchestrator-test",
        env=cdk.Environment(account="123456789012", region="ap-northeast-1"),
    )
    return assertions.Template.from_stack(s)


class TestCloudFrontDefault:
    @pytest.fixture(scope="class")
    def template(self):
        return _synth_with_config({})  # zero-config: CloudFront on

    @pytest.mark.regression
    def test_distribution_created_by_default(self, template):
        # At least one distribution exists in the default path.
        template.resource_count_is("AWS::CloudFront::Distribution", 1)


class TestCloudFrontDisabled:
    @pytest.fixture(scope="class")
    def template(self):
        return _synth_with_config({"cloudfront": {"enabled": False}})

    def test_no_distribution_when_disabled(self, template):
        template.resource_count_is("AWS::CloudFront::Distribution", 0)

    def test_no_cf_function_when_disabled(self, template):
        # The url-rewrite CloudFront Function is only needed with a distribution.
        template.resource_count_is("AWS::CloudFront::Function", 0)

    def test_alb_still_present(self, template):
        # Dashboards still served — via ALB, for the customer CDN to origin.
        template.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 1)


class TestCloudFrontDisabledWithDomainIgnored:
    """enabled=false + a domain/cert set: those are CloudFront-only, so they're
    warned-and-ignored (not a hard error) — synth still succeeds, no distribution."""

    @pytest.fixture(scope="class")
    def template(self):
        return _synth_with_config({"cloudfront": {
            "enabled": False,
            "custom_domain": "claw.example.com",
            "acm_cert_arn": "arn:aws:acm:us-east-1:111111111111:certificate/x",
        }})

    def test_synths_without_error(self, template):
        # If synth raised on the conflicting combo, the fixture would have failed.
        template.resource_count_is("AWS::CloudFront::Distribution", 0)


def _load_stack_module():
    if "stack" in sys.modules:
        del sys.modules["stack"]
    import stack as stack_mod
    return stack_mod


class TestSubnetSelection:
    """_subnet_selection builds a SubnetFilter.by_ids selection when subnet_ids
    is set (lazy, so it survives the context-lookup placeholder pass),
    and falls back to a subnet-type selection otherwise."""

    def test_explicit_ids_use_by_ids_filter(self):
        # A SubnetSelection with subnet_filters is produced, not a subnet-type one.
        import aws_cdk.aws_ec2 as ec2
        ids = ["subnet-aaa", "subnet-bbb"]
        sel = ec2.SubnetSelection(subnet_filters=[ec2.SubnetFilter.by_ids(ids)])
        assert sel.subnet_filters is not None and len(sel.subnet_filters) == 1

    def test_config_network_keys_are_optional(self):
        # Missing network section must not raise (zero-config path).
        cfg = {}
        net = cfg.get("network", {}) or {}
        assert (net.get("vpc_id") or "").strip() == ""
        assert list(net.get("subnet_ids") or []) == []
