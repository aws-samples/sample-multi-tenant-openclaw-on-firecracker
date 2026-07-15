# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""registry_service — Parameter_Registry 数据层(tenant-credential-contract)。

DDB 表 `openclaw-param-registry`(env PARAM_REGISTRY_TABLE):
  pk = config_template
  sk = "snapshot#<version>"(不可变快照行,含 entries)| "current"(指针行,current_version)

快照只追加从不改;current 指针用事务原子推进(put snapshot + 条件写 current),
回滚 = 只移指针。读路径单次 Query 全分区,内存里按指针选中快照,不做长缓存。
"""

import os

from boto3.dynamodb.conditions import Key

from core import clients
from core.utils import _now

TABLE_NAME = os.environ.get("PARAM_REGISTRY_TABLE", "openclaw-param-registry")

# 空 config_template 映射到的 shipped 模板分区名(与 tenant_service `config_template
# or "default"` / dispatch 同一约定)。只有它随部署自动补种;其它模板名走 admin
# POST /registry/{tpl} 发布,无指针仍 fail-loud(挡错名/typo)。
DEFAULT_TEMPLATE = "default"

# shipped default 模板的预置 Registry_Entries(publish_snapshot 的种子输入)
#
# R14.2 llm_key 注入契约(文档化,防再踩新加坡 kpweqnkwm9 2026-07-11 的 400):
#   llm_key 与平台凭据走同一条 injected_parameters 通道、同一套 envelope 校验
#   (envelope._validate_injected_parameters_v2)。**接受两种形态,拒第三种**:
#     ① `enc:v1:` 信封 → owner_id 绑定加密路径(与平台凭据同等 owner 隔离,首选)
#     ② 裸 base64(≤8192)→ 原值透传、**无 owner 绑定**;仅可承载自隔离的 opaque
#        值(如 per-tenant mint 的 litellm vkey),SHALL NOT 放需 owner 隔离的敏感值
#     ③ 原始 `sk-...` key(既非 enc:v1: 也非合法 base64)→ 400 must be base64(真机
#        撞的就是这个:上游直接塞原始 key。上游要么 base64 编码,要么走 enc:v1:)
#   空值走下方 empty_fallback=LITELLM_SHARED_VKEY(平台 shared vkey,仅上游未传时兜底,
#   优先级低于上游传入;见 launch-vm.sh:474 _APIKEY per-tenant > shared)。
SHIPPED_DEFAULT_ENTRIES = {
    "llm_key": {
        "param_class": "config",
        "injection_target": "models.providers.litellm.apiKey",
        "sensitive": True,
        "required": False,
        "empty_fallback": "LITELLM_SHARED_VKEY",
    },
    "api_key": {
        "param_class": "env",
        "injection_target": "EXCHANGE_API_KEY",
        "sensitive": True,
        "required": False,
    },
    "api_secret_key": {
        "param_class": "env",
        "injection_target": "EXCHANGE_API_SECRET_KEY",
        "sensitive": True,
        "required": False,
    },
    "subaccount_api_key": {
        "param_class": "env",
        "injection_target": "EXCHANGE_SUBACCOUNT_API_KEY",
        "sensitive": True,
        "required": False,
    },
    "subaccount_api_secret_key": {
        "param_class": "env",
        "injection_target": "EXCHANGE_SUBACCOUNT_API_SECRET_KEY",
        "sensitive": True,
        "required": False,
    },
}


def _table():
    return clients.ddb.Table(TABLE_NAME)


# 注意:事务走 clients.ddb.meta.client(resource 派生的 client 自带类型注入器),
# TransactItems 直接传 Python 原生类型,不要手动 TypeSerializer(会双重序列化炸掉)。


def _query_all(config_template):
    """单分区全行(快照数量级小,单次 Query 足够;ponytail: 不分页,超 1MB 再说)。"""
    resp = _table().query(
        KeyConditionExpression=Key("config_template").eq(config_template),
        ConsistentRead=True,
    )
    return resp.get("Items", [])


def ensure_default_seeded():
    """幂等补种 shipped `default` 模板的 v1 快照 + current 指针。

    根因(N6,新加坡 2026-07-11 真机):SHIPPED_DEFAULT_ENTRIES 定义了但从没被发布,
    致空 config_template(→"default")的注入参数租户首次 POST 就撞 no current pointer
    400,每个新 region/重部署都要人肉先 POST /registry/default 才能起租户。这里在读到
    default 无指针时补种一次。只补 default,不代替 admin 对具名模板的显式发布。
    幂等:已有 current 指针就 no-op(不重复 publish 出 snapshot#2——publish_snapshot
    只会把 version 越推越高)。并发补种撞车(两方都读到无指针)→ 一方赢、另一方
    publish_snapshot 抛 TransactionCanceledException,吞掉(赢家已写好指针,调用方
    重查即见),其它错误照常 fail-loud。
    """
    items = _query_all(DEFAULT_TEMPLATE)
    if any(i["sk"] == "current" for i in items):
        return  # 已补种过,幂等 no-op
    txn_cancelled = getattr(
        clients.ddb.meta.client.exceptions,
        "TransactionCanceledException",
        None,
    )
    try:
        publish_snapshot(DEFAULT_TEMPLATE, SHIPPED_DEFAULT_ENTRIES)
    except Exception as e:  # noqa: BLE001 — 仅吞并发补种撞车,余皆 re-raise
        if txn_cancelled is not None and isinstance(e, txn_cancelled):
            return
        raise


def load_current_snapshot(config_template):
    """单次 Query 拿全部行,按 current 指针选中快照。返回 (version, entries)。

    config_template == DEFAULT_TEMPLATE 且无指针 → 补种一次再读(shipped 模板随部署
    自愈);其它模板名无指针仍 fail-loud(挡错名/typo,不静默用空模板起租户)。
    """
    items = _query_all(config_template)
    current = next((i for i in items if i["sk"] == "current"), None)
    if current is None and config_template == DEFAULT_TEMPLATE:
        ensure_default_seeded()
        items = _query_all(config_template)
        current = next((i for i in items if i["sk"] == "current"), None)
    if current is None:
        raise LookupError(f"registry: no current pointer for {config_template}")
    version = int(current["current_version"])
    snap = next((i for i in items if i["sk"] == f"snapshot#{version}"), None)
    if snap is None:
        raise LookupError(
            f"registry: current points to missing snapshot#{version} "
            f"for {config_template}"
        )
    return version, snap["entries"]


def publish_snapshot(config_template, entries):
    """追加 snapshot#<version+1> 并原子推进 current 指针,返回新 version。

    从不原地改已有快照。事务两腿:put 新快照(条件不存在,防重复 version 覆盖)
    + current 指针条件推进(current_version == old;首发时条件为指针行不存在)。
    并发发布撞车 → TransactionCanceledException,fail-loud 交上游重试。
    """
    items = _query_all(config_template)
    current = next((i for i in items if i["sk"] == "current"), None)
    old_version = int(current["current_version"]) if current else 0
    new_version = old_version + 1

    snapshot_item = {
        "config_template": config_template,
        "sk": f"snapshot#{new_version}",
        "version": new_version,
        "created_at": _now(),
        "entries": entries,
    }
    if current:
        pointer_leg = {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": {"config_template": config_template, "sk": "current"},
                "UpdateExpression": "SET current_version = :new",
                "ConditionExpression": "current_version = :old",
                "ExpressionAttributeValues": {":new": new_version, ":old": old_version},
            }
        }
    else:
        pointer_leg = {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    "config_template": config_template,
                    "sk": "current",
                    "current_version": new_version,
                },
                "ConditionExpression": "attribute_not_exists(config_template)",
            }
        }
    clients.ddb.meta.client.transact_write_items(
        TransactItems=[
            {
                "Put": {
                    "TableName": TABLE_NAME,
                    "Item": snapshot_item,
                    "ConditionExpression": "attribute_not_exists(config_template)",
                }
            },
            pointer_leg,
        ]
    )
    return new_version


def rollback(config_template, version):
    """仅移动 current 指针回指 version;事务里 ConditionCheck 确认该快照存在。"""
    version = int(version)
    clients.ddb.meta.client.transact_write_items(
        TransactItems=[
            {
                "ConditionCheck": {
                    "TableName": TABLE_NAME,
                    "Key": {
                        "config_template": config_template,
                        "sk": f"snapshot#{version}",
                    },
                    "ConditionExpression": "attribute_exists(config_template)",
                }
            },
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {"config_template": config_template, "sk": "current"},
                    "UpdateExpression": "SET current_version = :v",
                    "ConditionExpression": "attribute_exists(config_template)",
                    "ExpressionAttributeValues": {":v": version},
                }
            },
        ]
    )
    return version
