# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""WI-002 — CDK synth tests for the channel-plane Cognito machine-user client.

Verifies the config-gated end-to-end Cognito plane:
  - default OFF (or console_auth disabled) → NO extra app client, NO cognito-idp
    IAM (existing deployments are byte-unchanged);
  - enabled → exactly one USER_PASSWORD_AUTH public app client + the api Lambda
    role gets AdminCreateUser/SetPassword/DeleteUser on the pool.

No AWS calls — aws_cdk.assertions.Template only.
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


def _synth_with_config(cfg_overrides):
    """Synthesize the stack with config.yml temporarily patched in-memory."""
    base = yaml.safe_load((Path(ROOT) / "config.yml").read_text())
    cfg = dict(base)
    for k, v in cfg_overrides.items():
        cfg[k] = v
    if "stack" in sys.modules:
        del sys.modules["stack"]
    import stack as stack_mod

    stack_mod.CFG = cfg
    app = cdk.App()
    s = stack_mod.OpenClawOrchestratorStack(
        app,
        "OpenClawOrchestrator-test",
        env=cdk.Environment(account="123456789012", region="ap-northeast-1"),
    )
    return assertions.Template.from_stack(s)


def _channel_clients(template):
    """All USER_PASSWORD_AUTH-only app clients (the machine-user client shape)."""
    clients = template.find_resources("AWS::Cognito::UserPoolClient")
    out = {}
    for cid, res in clients.items():
        flows = res.get("Properties", {}).get("ExplicitAuthFlows", [])
        if flows == ["ALLOW_USER_PASSWORD_AUTH"]:
            out[cid] = res
    return out


# ═══════════════════════════════════════════
# Disabled (default) — no channel Cognito plane
# ═══════════════════════════════════════════


class TestChannelCognitoDisabled:
    @pytest.fixture(scope="class")
    def template(self):
        # console_auth on (so a user pool exists) but channel plane OFF.
        return _synth_with_config(
            {"console_auth": {"enabled": True, "channel_cognito_auth": False}}
        )

    @pytest.mark.unit
    @pytest.mark.regression
    def test_no_machine_user_client_when_disabled(self, template):
        """No USER_PASSWORD_AUTH-only app client when the plane is off."""
        assert len(_channel_clients(template)) == 0

    @pytest.mark.unit
    @pytest.mark.regression
    def test_no_cognito_admin_iam_when_disabled(self, template):
        """No AdminCreateUser policy statement anywhere when the plane is off."""
        policies = template.find_resources("AWS::IAM::Policy")
        for res in policies.values():
            doc = res.get("Properties", {}).get("PolicyDocument", {})
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                assert "cognito-idp:AdminCreateUser" not in actions


# ═══════════════════════════════════════════
# Enabled — machine-user client + admin IAM
# ═══════════════════════════════════════════


class TestChannelCognitoEnabled:
    @pytest.fixture(scope="class")
    def template(self):
        return _synth_with_config(
            {"console_auth": {"enabled": True, "channel_cognito_auth": True}}
        )

    @pytest.mark.unit
    def test_exactly_one_machine_user_client(self, template):
        """Exactly one USER_PASSWORD_AUTH-only public app client is created."""
        clients = _channel_clients(template)
        assert len(clients) == 1

    @pytest.mark.unit
    def test_machine_user_client_is_public_no_secret(self, template):
        """The machine-user client must be public (no secret) — no in-guest SECRET_HASH."""
        client = next(iter(_channel_clients(template).values()))
        # GenerateSecret absent or False both mean "public client".
        assert client["Properties"].get("GenerateSecret", False) is False

    @pytest.mark.unit
    def test_cognito_admin_iam_present(self, template):
        """Some Lambda role gets AdminCreateUser/SetPassword/DeleteUser when enabled."""
        policies = template.find_resources("AWS::IAM::Policy")
        found = {
            "AdminCreateUser": False,
            "AdminSetUserPassword": False,
            "AdminDeleteUser": False,
        }
        for res in policies.values():
            doc = res.get("Properties", {}).get("PolicyDocument", {})
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                for a in actions:
                    if a == "cognito-idp:AdminCreateUser":
                        found["AdminCreateUser"] = True
                    if a == "cognito-idp:AdminSetUserPassword":
                        found["AdminSetUserPassword"] = True
                    if a == "cognito-idp:AdminDeleteUser":
                        found["AdminDeleteUser"] = True
        assert all(found.values()), f"missing cognito-idp admin actions: {found}"
