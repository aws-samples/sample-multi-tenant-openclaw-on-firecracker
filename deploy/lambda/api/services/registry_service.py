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

# shipped default 模板的预置 Registry_Entries(publish_snapshot 的种子输入)
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


def load_current_snapshot(config_template):
    """单次 Query 拿全部行,按 current 指针选中快照。返回 (version, entries)。"""
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
