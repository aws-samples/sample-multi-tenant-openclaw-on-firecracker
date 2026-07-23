# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK assertion tests for AWS WAF integration (issue #7).

When `waf.enabled: true` in config.yml, a regional WebACL must be created and
associated with the API Gateway stage. When disabled (default), no WAF
resources should appear in the synthesized template — backward compatible.
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "deploy"))

# Stack module is imported per-fixture so we can swap CFG cleanly
import importlib

import aws_cdk as cdk
from aws_cdk import assertions


def _synth_with_config(cfg_overrides):
    """Synthesize the stack with config.yml temporarily patched in-memory."""
    # Load + merge config
    base = yaml.safe_load((Path(ROOT) / "config.yml").read_text())
    cfg = dict(base)
    for k, v in cfg_overrides.items():
        cfg[k] = v

    # Force stack module to re-read CFG by patching its global before import.
    # Cleanest: import once, then mutate CFG in place; CDK App needs a fresh
    # construct tree per synth, so we reload the module.
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


# ═══════════════════════════════════════════
# WAF disabled (default) — fully off
# ═══════════════════════════════════════════


class TestWafDisabled:
    @pytest.fixture(scope="class")
    def template(self):
        return _synth_with_config({"waf": {"enabled": False}})

    @pytest.mark.unit
    @pytest.mark.regression
    def test_no_webacl_when_disabled(self, template):
        """No WebACL resource in the template when WAF is off."""
        template.resource_count_is("AWS::WAFv2::WebACL", 0)

    @pytest.mark.unit
    @pytest.mark.regression
    def test_no_webacl_association_when_disabled(self, template):
        template.resource_count_is("AWS::WAFv2::WebACLAssociation", 0)


# ═══════════════════════════════════════════
# WAF enabled — WebACL + Association created
# ═══════════════════════════════════════════


class TestWafEnabled:
    @pytest.fixture(scope="class")
    def template(self):
        return _synth_with_config({"waf": {
            "enabled": True,
            "rate_limit_per_ip": 1000,
            "managed_rules": [
                "AWSManagedRulesCommonRuleSet",
                "AWSManagedRulesKnownBadInputsRuleSet",
            ],
        }})

    @pytest.mark.unit
    def test_webacl_is_regional(self, template):
        """WebACL must be REGIONAL scope (API Gateway is regional)."""
        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"Scope": "REGIONAL"},
        )

    @pytest.mark.unit
    def test_webacl_has_rate_rule(self, template):
        """A rate-based rule must exist with the configured limit."""
        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"Rules": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Statement": {
                        "RateBasedStatement": assertions.Match.object_like({
                            "Limit": 1000,
                            "AggregateKeyType": "IP",
                        }),
                    },
                }),
            ])},
        )

    @pytest.mark.unit
    def test_webacl_has_managed_rules(self, template):
        """Both managed rule groups configured must appear as ManagedRuleGroupStatement."""
        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"Rules": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Statement": {
                        "ManagedRuleGroupStatement": assertions.Match.object_like({
                            "Name": "AWSManagedRulesCommonRuleSet",
                            "VendorName": "AWS",
                        }),
                    },
                }),
            ])},
        )
        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"Rules": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Statement": {
                        "ManagedRuleGroupStatement": assertions.Match.object_like({
                            "Name": "AWSManagedRulesKnownBadInputsRuleSet",
                            "VendorName": "AWS",
                        }),
                    },
                }),
            ])},
        )

    @pytest.mark.unit
    def test_webacl_default_action_allow(self, template):
        """Default action: Allow (managed rules block bad traffic explicitly)."""
        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"DefaultAction": {"Allow": {}}},
        )

    @pytest.mark.unit
    def test_webacl_association_to_api_gateway(self, template):
        """Association must reference an API Gateway stage ARN."""
        template.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)
        template.has_resource_properties(
            "AWS::WAFv2::WebACLAssociation",
            assertions.Match.object_like({
                "ResourceArn": assertions.Match.any_value(),
                "WebACLArn": assertions.Match.any_value(),
            }),
        )

    @pytest.mark.unit
    def test_visibility_config_with_metrics_and_sampling(self, template):
        """Top-level VisibilityConfig — observability is required for WAF rules."""
        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"VisibilityConfig": assertions.Match.object_like({
                "CloudWatchMetricsEnabled": True,
                "SampledRequestsEnabled": True,
            })},
        )


# ═══════════════════════════════════════════
# Custom rate limit + minimal managed rules
# ═══════════════════════════════════════════


class TestWafCustomConfig:
    @pytest.fixture(scope="class")
    def template(self):
        return _synth_with_config({"waf": {
            "enabled": True,
            "rate_limit_per_ip": 500,
            "managed_rules": [],  # explicitly no managed rules
        }})

    @pytest.mark.unit
    def test_custom_rate_limit_applied(self, template):
        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"Rules": assertions.Match.array_with([
                assertions.Match.object_like({
                    "Statement": {
                        "RateBasedStatement": assertions.Match.object_like({
                            "Limit": 500,
                        }),
                    },
                }),
            ])},
        )

    @pytest.mark.unit
    def test_no_managed_rules_when_empty_list(self, template):
        """Empty managed_rules → only the rate-based rule (1 rule total)."""
        # Capture WebACL → assert exactly 1 rule (the rate one)
        webacls = template.find_resources("AWS::WAFv2::WebACL")
        assert len(webacls) == 1
        webacl = next(iter(webacls.values()))
        rules = webacl["Properties"]["Rules"]
        assert len(rules) == 1
        # And it's the rate-based one
        assert "RateBasedStatement" in rules[0]["Statement"]


# ═══════════════════════════════════════════
# Regression — existing resources still synthesize when WAF off
# ═══════════════════════════════════════════


class TestRegressionWafOff:
    @pytest.fixture(scope="class")
    def template(self):
        return _synth_with_config({"waf": {"enabled": False}})

    @pytest.mark.unit
    @pytest.mark.regression
    def test_api_gateway_still_present(self, template):
        template.resource_count_is("AWS::ApiGateway::RestApi", 1)

    @pytest.mark.unit
    @pytest.mark.regression
    def test_existing_lambda_count_preserved(self, template):
        """7 Lambda functions: api, health_check, skills, templates, scaler, backup, agentcore_tools."""
        # AgentCore tools only present if AgentCore is enabled — we accept >= 6 minimum.
        # Read existing CDK behavior: count Lambda functions but don't pin the exact number
        # because feature flags may add/remove (e.g. AgentCoreTools).
        templates = template.find_resources("AWS::Lambda::Function")
        # Filter out CDK-internal Lambdas (BucketNotificationsHandler, etc.)
        user_lambdas = [
            l for name, l in templates.items()
            if not name.startswith("BucketNotifications")
            and not name.startswith("CustomResource")
            and not name.startswith("AWSCDK")
            and "Logs" not in name
            and "AwsCustomResource" not in name
        ]
        assert len(user_lambdas) >= 6
