# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cognito Pre-Token-Generation Lambda trigger — 档 A 联邦方案的 claim 注入。

关联:ADR-dataplane-external-saas-auth 档 A / issue #97。

作用:外部 SaaS 平台(如二手电商)的用户经外部 IdP 联邦进本平台 Cognito 后,Cognito 在
签发 id_token 前触发本 Lambda。本 Lambda 把「该用户属于哪个外部平台、外部平台里的用户 id」
盖进 JWT 的 custom claim,使下游(控制面 broker 代开租户、hub 换 token)能拿到稳定的
tenant_user_id / platform_id,而不必再解析联邦身份的原始结构。

照抄 aws-samples/amazon-cognito-example-for-multi-tenant 的 pretokengeneration 模式
(lambda/pretokengeneration/src/index.ts:48-93),按本仓约定改写为 Python + 注入我们的
custom:tenant_user_id / custom:platform_id(对齐 handler.py:_tenant_user_id_from_claims
已能读的 claim 名)。

身份来源优先级(与控制面 _tenant_user_id_from_claims 一致,不自造):
  1. 联邦身份 identities[].userId(外部 IdP 的用户 id) + providerName(哪个平台)
  2. 已有的 custom:tenant_user_id(重签场景保持稳定)
  3. 回退 Cognito sub(原生用户,无外部平台)

platform_id 来源:联邦 identities[].providerName(= Cognito 里注册的外部 IdP 名,
即我们发给客户的 platform_id);原生用户标 "native"。

纪律:本 Lambda 只做 claim 注入(把可信来源的值搬进 claim),**不做授权判定**
(授权仍在 hub authorizeSubForTenant 查 owner_id/authorized_users,server 端不信 claim)。
fail-open 安全:任何异常都原样返回 event 不阻断登录(Pre-Token-Gen 抛错会让用户登不进),
但注入失败时不伪造 claim(下游 fallback 到 sub)。
"""

import json


def _extract_identity(user_attrs):
    """从联邦用户属性提取 (tenant_user_id, platform_id)。不抛异常。"""
    # 联邦用户的 identities 是一个 JSON 字符串数组(Cognito 注入)
    identities_raw = user_attrs.get("identities")
    if identities_raw:
        try:
            identities = (
                json.loads(identities_raw)
                if isinstance(identities_raw, str)
                else identities_raw
            )
            if isinstance(identities, list) and identities:
                ent = identities[0] or {}
                uid = ent.get("userId") or ent.get("user_id")
                provider = ent.get("providerName") or ent.get("provider_name")
                if uid:
                    return str(uid), str(provider or "federated")
        except (ValueError, TypeError):
            pass
    # 已有 custom:tenant_user_id(重签保持稳定)
    existing = user_attrs.get("custom:tenant_user_id")
    if existing:
        return str(existing), str(user_attrs.get("custom:platform_id") or "federated")
    # 原生 Cognito 用户:无外部平台
    sub = user_attrs.get("sub")
    if sub:
        return str(sub), "native"
    return None, None


def handler(event, context=None):
    """Cognito Pre-Token-Generation trigger 入口。

    event.request.userAttributes 含用户属性(含联邦 identities)。
    在 event.response.claimsOverrideDetails.claimsToAddOrOverride 注入 custom claim。
    """
    try:
        req = event.get("request", {}) or {}
        user_attrs = req.get("userAttributes", {}) or {}
        tenant_user_id, platform_id = _extract_identity(user_attrs)

        add = {}
        if tenant_user_id:
            add["custom:tenant_user_id"] = tenant_user_id
        if platform_id:
            add["custom:platform_id"] = platform_id

        if add:
            # Real Cognito events send response/claimsOverrideDetails as
            # present-but-None (not absent), so setdefault returns None and
            # None.setdefault crashes. Use `or {}` to handle present-but-None
            # (the mock had {} so this only surfaced on real infra — #97 真机).
            resp = event.get("response") or {}
            details = resp.get("claimsOverrideDetails") or {}
            existing_add = details.get("claimsToAddOrOverride") or {}
            existing_add.update(add)
            details["claimsToAddOrOverride"] = existing_add
            resp["claimsOverrideDetails"] = details
            event["response"] = resp
    except Exception as e:  # noqa: BLE001 — Pre-Token-Gen 抛错会阻断登录,fail-open
        print(
            f"pretokengen: claim injection skipped (non-fatal): {type(e).__name__}: {e}"
        )
    return event
