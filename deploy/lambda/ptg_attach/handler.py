# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#144 — attach the Pre-Token-Generation trigger to an IMPORTED Cognito pool.

Why a custom resource: `UserPool.from_user_pool_id` returns an interface proxy
with no `add_trigger`, and CloudFormation has no standalone "user pool lambda
config" resource — the only way to set LambdaConfig on a pool the stack doesn't
own is the UpdateUserPool API.

Why read-merge-write: UpdateUserPool RESETS every field you don't pass to its
default (AWS API reference, "If you don't provide a value for an attribute,
Amazon Cognito sets it to its default value"). A naive call with only
LambdaConfig would wipe the imported pool's password policy / MFA / email
config. So: DescribeUserPool → copy the updatable fields → overlay our
PreTokenGeneration arn → UpdateUserPool.

Also validates (fail-loud, #144 DoD) that the imported pool's schema already
has the custom attribute the IdP attribute-mapping writes into
(`custom:tenant_user_id`) — pool schema is immutable from CDK, so a missing
attribute means federation would silently break attribution; better to fail
the deployment with a clear message.

Delete is best-effort: detach only if PreTokenGeneration still points at us
(never block stack deletion on an imported resource we don't own).
"""

import json

import boto3

# DescribeUserPool response fields UpdateUserPool accepts back (API reference;
# Name→PoolName is the one rename). Everything else (Schema, Id, Arn, dates,
# username config) is create-only and must NOT be echoed into the update.
_UPDATABLE = (
    "Policies",
    "DeletionProtection",
    "LambdaConfig",
    "AutoVerifiedAttributes",
    "SmsVerificationMessage",
    "EmailVerificationMessage",
    "EmailVerificationSubject",
    "VerificationMessageTemplate",
    "SmsAuthenticationMessage",
    "UserAttributeUpdateSettings",
    "MfaConfiguration",
    "DeviceConfiguration",
    "EmailConfiguration",
    "SmsConfiguration",
    "UserPoolTags",
    "AdminCreateUserConfig",
    "UserPoolAddOns",
    "AccountRecoverySetting",
    "UserPoolTier",
)

idp = boto3.client("cognito-idp")


def _merged_update_params(pool_id):
    pool = idp.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    params = {"UserPoolId": pool_id}
    for key in _UPDATABLE:
        if key in pool:
            params[key] = pool[key]
    if "Name" in pool:
        params["PoolName"] = pool["Name"]
    return pool, params


def _require_custom_attr(pool, attr_name):
    names = {a.get("Name") for a in pool.get("SchemaAttributes", [])}
    # Cognito stores custom attributes with a "custom:" prefix.
    if f"custom:{attr_name}" not in names and attr_name not in names:
        raise RuntimeError(
            f"imported user pool {pool.get('Id')} lacks required custom "
            f"attribute 'custom:{attr_name}' (pool schema is immutable from "
            f"CDK — add it to the pool first, or disable exchange_idp). "
            f"Failing loud instead of deploying a silently broken federation "
            f"attribute mapping (#144)."
        )


def handler(event, _ctx):
    print(json.dumps({k: event.get(k) for k in ("RequestType", "ResourceProperties")}))
    req = event["RequestType"]
    props = event.get("ResourceProperties") or {}
    pool_id = props["UserPoolId"]
    lambda_arn = props["LambdaArn"]

    if req == "Delete":
        try:
            pool, params = _merged_update_params(pool_id)
            lc = dict(pool.get("LambdaConfig") or {})
            if lc.get("PreTokenGeneration") == lambda_arn:
                lc.pop("PreTokenGeneration", None)
                lc.pop("PreTokenGenerationConfig", None)
                params["LambdaConfig"] = lc
                idp.update_user_pool(**params)
        except Exception as e:  # noqa: BLE001 — never block stack deletion
            print(f"[ptg-attach] detach best-effort failed: {e}")
        return {"PhysicalResourceId": f"ptg-attach-{pool_id}"}

    # Create / Update — fail loud on any error (deployment must not pretend).
    pool, params = _merged_update_params(pool_id)
    required_attr = props.get("RequiredCustomAttr")
    if required_attr:
        _require_custom_attr(pool, required_attr)
    lc = dict(pool.get("LambdaConfig") or {})
    lc["PreTokenGeneration"] = lambda_arn
    params["LambdaConfig"] = lc
    idp.update_user_pool(**params)
    return {"PhysicalResourceId": f"ptg-attach-{pool_id}"}
