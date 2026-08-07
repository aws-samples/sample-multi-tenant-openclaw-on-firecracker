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
import uuid

import core.clients as clients
from core.auth import _get_caller_identity


def enqueue_lifecycle(action, tenant_id, event, extra=None):
    """把一个 lifecycle 操作入 SQS,供 consumer 受控并发消费。返回 True=已入队。

    LIFECYCLE_QUEUE_URL 未配 → 返 False,调用方回退同步路径(向后兼容)。
    捎带调用者身份(#56:异步消费不绕过 RBAC),与 _enqueue_batch_job 同款。

    #411/6.1(承 #364)— MessageDeduplicationId 每次入队带唯一成分,不再用静态
    `{tenant_id}:{action}`。原键下,同一租户 5 分钟内的第二次同类操作(如两次
    stop)被 SQS FIFO 默认去重窗口判为 duplicate:接收成功但【不投递】,而 API 仍
    返 202 → 用户看到"queued"却实际没执行(客户实测 6.1)。每个 API 调用是一次
    独立的用户意图,应各自投递。改用 per-call operation_id 作 dedup id:仍拦真正的
    SQS 重发(同一条消息 SDK 重试),但不再把两次独立操作折叠成一次。
    per-tenant 有序性由 MessageGroupId=tenant_id 保留(FIFO 组内 SSM 串行下发)。
    重复投递的执行安全由既有幂等保证:stop/start-vm 脚本幂等,且 tenant_action 对
    deleted/deleting 有 409 status 闸(tenant_service:2447),重复同类操作是安全 no-op。
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
            # #143 sibling — carry the OIDC-federated caller's stable id so
            # CREATE_VIA_QUEUE replay lands tenant_user_id too. Without it a
            # federated Bearer user creating via the FIFO queue loses
            # tenant_user_id (consumer's rebuilt ident had no claims), the node
            # never enters gsi_tenant_user, and GET /users/{tuid}/tenants /
            # summary / bulk action silently miss it. Sync + dispatch paths
            # already land it; this was the FIFO-replay gap.
            "tenant_user_id": ident.get("tenant_user_id"),
        },
        # #411/6.1 — per-call 唯一操作 id;每次 API 调用一次独立意图,不被静态键折叠。
        "_op_id": uuid.uuid4().hex,
    }
    kwargs = {"QueueUrl": clients.LIFECYCLE_QUEUE_URL, "MessageBody": json.dumps(msg)}
    # FIFO 队列(.fifo 结尾)需要 group/dedup id;标准队列忽略这俩
    if clients.LIFECYCLE_QUEUE_URL.endswith(".fifo"):
        kwargs["MessageGroupId"] = tenant_id  # per-tenant 有序
        # dedup id = per-call op id:仍拦同一条消息的 SDK 重发,但两次独立的
        # 同类操作(5min 内两次 stop)各自投递,不再被静态 {tenant}:{action} 吞掉。
        kwargs["MessageDeduplicationId"] = msg["_op_id"]
    clients.sqs.send_message(**kwargs)
    return True
