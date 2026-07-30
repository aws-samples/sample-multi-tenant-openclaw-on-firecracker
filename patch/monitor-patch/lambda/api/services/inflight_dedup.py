# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/inflight_dedup — R16.2 在途去重(同 owner+tenant_user_id 不双开).

设计(对齐 evidence/r16-idempotency-2026-07-12.md):
- 创建前在途占位(非事后锁):以 owner_id#tenant_user_id 为键做条件写
- 原子性:DDB attribute_not_exists(id) 条件写,两并发只一个成功
- 窗口关闭:终态(running/failed/deleted)时删除占位;TTL 30min 兜底防永久锁死
- 跨 owner 隔离:PK 含 owner_id,ownerA 的在途不影响 ownerB

占位 item 放 tenants 主表(PK = "inflight#<owner_id>#<tenant_user_id>"):
- 复用已有表,无需新建 DDB 表(不碰 CDK RemovalPolicy)
- TTL attribute "inflight_ttl" 通过 DDB TTL 特性自动清理过期锁
- 不出现在正常 tenant query(GSI 在 owner_id 上,锁记录 owner_id 字段不存在)

ref: IdempotentAPI — GetOrSet 占位 + 409 冲突 + 请求完成后替换/删除
"""

import time

from botocore.exceptions import ClientError

import core.clients as clients
import core.utils as utils

# 30 分钟 TTL 兜底(对齐 health_check reaper CREATING_TIMEOUT_SECONDS=1800)
_INFLIGHT_TTL_SECONDS = 1800


def _lock_key(owner_id: str, tenant_user_id: str) -> str:
    """占位 item 的主键:不可能与正常 tenant id 碰撞(tenant id = name-xxxx 或 t-xxxx)."""
    return f"inflight#{owner_id}#{tenant_user_id}"


def acquire_inflight_lock(owner_id, tenant_user_id):
    """尝试占位。成功返回 None;已有在途返回 (409_resp, existing_tenant_id).

    跳过条件(不做去重):
    - tenant_user_id 为空/None(匿名创建,无法归属)
    - owner_id 为空/None
    """
    if not owner_id or not tenant_user_id:
        return None

    lock_id = _lock_key(owner_id, tenant_user_id)
    now = utils._now()
    ttl_epoch = int(time.time()) + _INFLIGHT_TTL_SECONDS

    try:
        clients.tenants_table.put_item(
            Item={
                "id": lock_id,
                "status": "inflight_lock",
                "owner_id_lock": owner_id,
                "tenant_user_id_lock": tenant_user_id,
                "created_at": now,
                "inflight_ttl": ttl_epoch,
            },
            ConditionExpression="attribute_not_exists(id)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            existing = clients.tenants_table.get_item(
                Key={"id": lock_id}, ConsistentRead=True
            ).get("Item", {})
            existing_tid = existing.get("locked_tenant_id", "")
            return (
                utils._err(
                    409,
                    "INFLIGHT_DUPLICATE",
                    f"an in-flight create already exists for this owner+tenant_user_id"
                    f"{(' (id=' + existing_tid + ')') if existing_tid else ''}",
                    extra={"inflight_tenant_id": existing_tid}
                    if existing_tid
                    else None,
                ),
                existing_tid,
            )
        raise

    return None


def bind_tenant_id(owner_id, tenant_user_id, tenant_id):
    """占位成功后绑定实际 tenant_id(供 409 响应告知调用方查哪条)."""
    if not owner_id or not tenant_user_id:
        return
    lock_id = _lock_key(owner_id, tenant_user_id)
    try:
        clients.tenants_table.update_item(
            Key={"id": lock_id},
            UpdateExpression="SET locked_tenant_id = :tid",
            ExpressionAttributeValues={":tid": tenant_id},
        )
    except Exception:  # noqa: BLE001 — best-effort, lock 已占到位
        pass


def release_inflight_lock(owner_id, tenant_user_id):
    """终态时释放占位(best-effort;TTL 兜底防漏)."""
    if not owner_id or not tenant_user_id:
        return
    lock_id = _lock_key(owner_id, tenant_user_id)
    try:
        clients.tenants_table.delete_item(Key={"id": lock_id})
    except Exception:  # noqa: BLE001 — best-effort, TTL 兜底
        pass
