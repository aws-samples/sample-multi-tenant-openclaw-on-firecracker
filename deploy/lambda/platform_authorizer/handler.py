# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#108 — REQUEST Lambda authorizer: 把「这把 API key 属于哪个平台」解析成
requestContext.authorizer.platform_id,注入给控制面 handler 做命名空间隔离。

为什么需要它(承重约束,见 (internal design docs)):API Gateway Lambda proxy
集成下,只有配了 authorizer 时 requestContext.authorizer.* 才会传给后端;纯
usage-plan API key(仅 api_key_required)拿不到「keyId→平台」的可信映射。所以平台
身份必须由这个 REQUEST authorizer 解析并注入,handler 只信注入值(不读 x-api-key
明文比对,避免密钥进日志)。

映射存 DDB PlatformKeyMap:分区键 key_hash(sha256(x-api-key) 十六进制,**不存明文
key**),字段 platform_id。authorizer 拿到请求头的 x-api-key → sha256 → 查表:
  • 命中 → Allow + context.platform_id=<平台>(该 key 作用域限该平台)。
  • 未命中 → 运维超管 key(不在映射表里的合法 usage-plan key)走 Allow 但不注入
    platform_id(handler 侧 platform_scope=None,保持现有全量 admin 行为,过渡期兼容)。
    真正非法的 key 由 API Gateway 的 usage-plan 层先挡(本 authorizer 只解析归属)。

注:本 authorizer 只解析「归属」,不做「认证」——认证仍由 API GW usage-plan(key 有效性)
+ 下游 handler 的 Cognito/RBAC 承担。它是 additive 的授权信息注入层。
"""

import hashlib
import os

import boto3

_ddb = boto3.resource("dynamodb")
_TABLE = os.environ.get("PLATFORM_KEY_TABLE")
_table = _ddb.Table(_TABLE) if _TABLE else None


def _hash_key(raw):
    """sha256(x-api-key) 十六进制。空 key → None(交给上游 usage-plan 挡)。"""
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _lookup_platform(key_hash):
    """查 PlatformKeyMap:命中返 platform_id,否则 None。查表失败 fail-open 到
    None(= 无平台作用域 = 现有全量行为),不因映射表抖动把所有平台请求打挂——
    隔离是加固层,底层 usage-plan + Cognito/RBAC 仍在。"""
    if _table is None or not key_hash:
        return None
    try:
        item = _table.get_item(Key={"key_hash": key_hash}).get("Item")
    except Exception as e:  # noqa: BLE001 — fail-open 到无作用域,打日志不静默吞
        print(f"platform_authorizer: PlatformKeyMap lookup failed: {e}")
        return None
    if not item:
        return None
    pid = item.get("platform_id")
    return str(pid) if pid else None


def _extract_api_key(event):
    """从 REQUEST authorizer 事件取 x-api-key(header 名大小写不敏感)。"""
    headers = event.get("headers") or {}
    for k, v in headers.items():
        if k and k.lower() == "x-api-key":
            return v
    return None


def _policy(effect, resource, platform_id=None):
    """构造 authorizer 返回的 IAM 策略 + 注入 context。context 值必须是标量
    (str/num/bool),API GW 不接受嵌套对象。"""
    ctx = {}
    if platform_id:
        ctx["platform_id"] = platform_id
    return {
        "principalId": platform_id or "platform-unscoped",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource,
                }
            ],
        },
        "context": ctx,
    }


def lambda_handler(event, context=None):
    # methodArn 是本次调用的资源;用 "*" 允许该 API 全部方法(usage-plan key 已限流,
    # 归属解析不额外收窄路由——路由级授权在 handler 的 RBAC/owner/platform_scope)。
    method_arn = event.get("methodArn") or event.get("routeArn") or "*"
    raw = _extract_api_key(event)
    key_hash = _hash_key(raw)
    platform_id = _lookup_platform(key_hash)
    # 一律 Allow(认证/限流交给 usage-plan);差异只在是否注入 platform_id 作用域。
    return _policy("Allow", method_arn, platform_id)
