# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK synth tests for #97 档A — Pre-Token-Generation Lambda trigger wiring.

Config-gated:
  - federation OFF (default) → NO openclaw-pretokengen Lambda, UserPool has no
    PreTokenGeneration trigger (existing deployments byte-unchanged);
  - federation ON (exchange_idp.enabled + issuer_url, stack-owned pool) → exactly
    one openclaw-pretokengen Lambda + UserPool LambdaConfig.PreTokenGeneration wired.

No AWS calls — aws_cdk.assertions.Template only.
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "deploy"))

import aws_cdk as cdk
from aws_cdk import assertions


def _synth(cfg_overrides):
    base = yaml.safe_load((Path(ROOT) / "config.yml").read_text())
    cfg = dict(base)
    cfg.update(cfg_overrides)
    if "stack" in sys.modules:
        del sys.modules["stack"]
    import stack as stack_mod

    stack_mod.CFG = cfg
    app = cdk.App()
    s = stack_mod.OpenClawOrchestratorStack(
        app,
        "OpenClawOrchestrator-test-ptg",
        env=cdk.Environment(account="123456789012", region="ap-northeast-1"),
    )
    return assertions.Template.from_stack(s)


def _ptg_functions(template):
    fns = template.find_resources("AWS::Lambda::Function")
    return {
        k: v
        for k, v in fns.items()
        if v.get("Properties", {}).get("FunctionName") == "openclaw-pretokengen"
    }


class TestFederationOff:
    """默认联邦关 → 无 pretokengen Lambda / 无 trigger(存量零变化)。"""

    @pytest.fixture(scope="class")
    def template(self):
        return _synth(
            {
                "console_auth": {"enabled": True, "user_pool_id": ""},
                "exchange_idp": {"enabled": False},
            }
        )

    def test_no_pretokengen_lambda(self, template):
        assert _ptg_functions(template) == {}

    def test_tenant_idp_map_table_exists(self, template):
        # #97 档A — the /tenantmatch routing table is created unconditionally
        # (routing config, not gated on federation being on).
        tables = template.find_resources("AWS::DynamoDB::Table")
        names = [t.get("Properties", {}).get("TableName") for t in tables.values()]
        assert "openclaw-tenant-idp-map" in names
        # partition key must be platform_id
        for t in tables.values():
            if t.get("Properties", {}).get("TableName") == "openclaw-tenant-idp-map":
                keys = t["Properties"]["KeySchema"]
                assert keys[0]["AttributeName"] == "platform_id"

    def test_no_pretoken_trigger_on_pool(self, template):
        pools = template.find_resources("AWS::Cognito::UserPool")
        for _, res in pools.items():
            lam = res.get("Properties", {}).get("LambdaConfig", {}) or {}
            assert "PreTokenGeneration" not in lam


class TestFederationOn:
    """联邦开(stack-owned pool)→ pretokengen Lambda + PreTokenGeneration trigger。"""

    @pytest.fixture(scope="class")
    def template(self):
        return _synth(
            {
                "console_auth": {"enabled": True, "user_pool_id": ""},
                "exchange_idp": {
                    "enabled": True,
                    "issuer_url": "https://cognito-idp.ap-northeast-1.amazonaws.com/ap-northeast-1_demoMkt",
                    "provider_name": "demo-marketplace",
                    "client_id": "demo-client",
                    "scopes": ["openid", "email"],
                },
            }
        )

    def test_pretokengen_lambda_exists(self, template):
        fns = _ptg_functions(template)
        assert len(fns) == 1, "should synth exactly one openclaw-pretokengen Lambda"
        props = list(fns.values())[0]["Properties"]
        assert props["Handler"] == "handler.handler"
        assert props["Runtime"] == "python3.12"

    def test_pretoken_trigger_wired_on_pool(self, template):
        pools = template.find_resources("AWS::Cognito::UserPool")
        wired = False
        for _, res in pools.items():
            lam = res.get("Properties", {}).get("LambdaConfig", {}) or {}
            if "PreTokenGeneration" in lam:
                wired = True
        assert wired, (
            "UserPool must have LambdaConfig.PreTokenGeneration when federation on"
        )
