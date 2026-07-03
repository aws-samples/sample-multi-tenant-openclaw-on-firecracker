# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Pre-Token-Generation Lambda (issue #97 档 A claim injection).

Validates: federated user → custom:tenant_user_id/platform_id injected; native user →
sub + platform "native"; existing claim preserved; malformed identities fail-open (no
crash, login not blocked); injection never fabricates when no identity source.
"""

import importlib.util
import json
import sys

import pytest

pytestmark = pytest.mark.unit

_spec = importlib.util.spec_from_file_location(
    "ptg_handler", "deploy/lambda/pretokengen/handler.py"
)
ptg = importlib.util.module_from_spec(_spec)
sys.modules["ptg_handler"] = ptg
_spec.loader.exec_module(ptg)


def _event(user_attrs):
    return {"request": {"userAttributes": user_attrs}, "response": {}}


def _claims(ev):
    return (
        ev.get("response", {})
        .get("claimsOverrideDetails", {})
        .get("claimsToAddOrOverride", {})
    )


class TestFederatedIdentity:
    def test_federated_user_injects_both_claims(self):
        ev = _event(
            {
                "sub": "cognito-sub-1",
                "identities": json.dumps(
                    [{"userId": "ecom-user-42", "providerName": "demo-marketplace"}]
                ),
            }
        )
        out = ptg.handler(ev)
        c = _claims(out)
        assert c["custom:tenant_user_id"] == "ecom-user-42"
        assert c["custom:platform_id"] == "demo-marketplace"

    def test_identities_as_list_not_string(self):
        # Cognito sometimes provides identities already parsed
        ev = _event(
            {
                "sub": "s",
                "identities": [{"userId": "u9", "providerName": "shop"}],
            }
        )
        c = _claims(ptg.handler(ev))
        assert c["custom:tenant_user_id"] == "u9" and c["custom:platform_id"] == "shop"


class TestNativeUser:
    def test_native_user_falls_back_to_sub(self):
        ev = _event({"sub": "native-sub-7"})
        c = _claims(ptg.handler(ev))
        assert c["custom:tenant_user_id"] == "native-sub-7"
        assert c["custom:platform_id"] == "native"


class TestExistingClaimStable:
    def test_existing_tenant_user_id_preserved(self):
        ev = _event(
            {
                "sub": "s",
                "custom:tenant_user_id": "stable-id",
                "custom:platform_id": "p1",
            }
        )
        c = _claims(ptg.handler(ev))
        assert c["custom:tenant_user_id"] == "stable-id"
        assert c["custom:platform_id"] == "p1"


class TestFailOpen:
    def test_malformed_identities_no_crash_no_fabrication(self):
        # Garbage identities + no sub → must not crash, must not fabricate a claim
        ev = _event({"identities": "{not json,,,"})
        out = ptg.handler(ev)  # must return event, not raise
        assert out is ev
        # nothing to inject (no valid identity, no sub) → no claim override
        assert _claims(out) == {}

    def test_empty_event_returns_event(self):
        out = ptg.handler({})
        assert out == {}  # fail-open: returns what it got, no crash

    def test_missing_response_key_is_created(self):
        ev = {"request": {"userAttributes": {"sub": "s2"}}}
        out = ptg.handler(ev)
        assert _claims(out)["custom:tenant_user_id"] == "s2"

    def test_response_present_but_none(self):
        # #97 真机 bug:真 Cognito 事件的 response 是 present-but-None(不是缺失),
        # setdefault 返回 None → None.setdefault 崩(mock 用 {} 掩盖了)。必须注入成功不崩。
        ev = {"request": {"userAttributes": {"sub": "s5"}}, "response": None}
        out = ptg.handler(ev)
        assert _claims(out)["custom:tenant_user_id"] == "s5"
        assert _claims(out)["custom:platform_id"] == "native"

    def test_claimsoverridedetails_present_but_none(self):
        # 更深一层:response 在但 claimsOverrideDetails 是 None
        ev = {
            "request": {"userAttributes": {"sub": "s6"}},
            "response": {"claimsOverrideDetails": None},
        }
        out = ptg.handler(ev)
        assert _claims(out)["custom:tenant_user_id"] == "s6"

    def test_preserves_prior_claims_to_add(self):
        # If Cognito already staged some claims, we merge, not clobber
        ev = {
            "request": {"userAttributes": {"sub": "s3"}},
            "response": {
                "claimsOverrideDetails": {
                    "claimsToAddOrOverride": {"custom:foo": "bar"}
                }
            },
        }
        c = _claims(ptg.handler(ev))
        assert c["custom:foo"] == "bar"
        assert c["custom:tenant_user_id"] == "s3"
