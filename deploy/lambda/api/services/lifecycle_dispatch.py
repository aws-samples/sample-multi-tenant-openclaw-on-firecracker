# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/lifecycle_dispatch — 把 lifecycle 操作入 SQS 供 consumer 削峰消费。

handler-split #132 阶段3(只解依赖环,不碰 SQS 重构)——从 handler.py 逐字搬
`enqueue_lifecycle`,函数体零改动。

**为什么放 services 层(偏离 ADR §3 原划的 consumers)**:tenant_service 的
create_tenant/tenant_action 调 enqueue_lifecycle。若 enqueue 留 consumers 层,
就是 services→consumers 反向依赖(import-layers 门禁,design.md line 33)——这正是
tenant_service 一直拆不出的循环依赖根。enqueue 本身只依赖 core(sqs/queue url +
_get_caller_identity),不依赖任何编排/消费逻辑,本质是"发一条队列消息"的底层业务
操作,归 services 层名正言顺。真正的 consumer 回调 `_consume_lifecycle_sqs`
(调 create/delete/tenant_action)仍归 consumers 层,方向 consumers→services 合法。
环就此断开:tenant_service→lifecycle_dispatch(services→services,门只禁
routes/consumers/router,不禁 services 横向)。

sqs / LIFECYCLE_QUEUE_URL 走属性访问 `clients.X`:测试用
`patch.object(api, "LIFECYCLE_QUEUE_URL", ...)` / `api.sqs = Mock()` 重绑注入 fixture,
值绑定看不到重绑(scheduling/audit 域验证过的跨模块串染死结);属性访问下测试
patch `clients.X`(规范源)即全局生效。
"""

import json

import core.clients as clients
from core.auth import _get_caller_identity


def enqueue_lifecycle(action, tenant_id, event, extra=None):
    """把一个 lifecycle 操作入 SQS,供 consumer 受控并发消费。返回 True=已入队。

    LIFECYCLE_QUEUE_URL 未配 → 返 False,调用方回退同步路径(向后兼容)。
    幂等:MessageDeduplicationId/_idem 用 tenant_id+action,SQS FIFO 或 consumer 侧去重。
    捎带调用者身份(#56:异步消费不绕过 RBAC),与 _enqueue_batch_job 同款。
    """
    if not clients.sqs or not clients.LIFECYCLE_QUEUE_URL:
        return False
    ident = _get_caller_identity(event or {})
    msg = {
        "action": action,
        "tenant_id": tenant_id,
        "extra": extra or {},
        # 重放调用者身份,consumer 据此重建最小 event 做 owner 检查
        "_ident": {
            "owner_id": ident.get("owner_id"),
            "is_admin": ident.get("is_admin"),
            # #108 — carry platform scope so async replay stays in-namespace.
            # For create-via-queue this is also why we pin platform_id into the
            # queued body below (the sync path pins a local var that the body
            # snapshot didn't capture) — see create_tenant enqueue block.
            "platform_scope": ident.get("platform_scope"),
        },
        "_idem": f"{tenant_id}:{action}",
    }
    kwargs = {"QueueUrl": clients.LIFECYCLE_QUEUE_URL, "MessageBody": json.dumps(msg)}
    # FIFO 队列(.fifo 结尾)需要 group/dedup id;标准队列忽略这俩
    if clients.LIFECYCLE_QUEUE_URL.endswith(".fifo"):
        kwargs["MessageGroupId"] = tenant_id  # per-tenant 有序
        kwargs["MessageDeduplicationId"] = msg["_idem"]
    clients.sqs.send_message(**kwargs)
    return True
