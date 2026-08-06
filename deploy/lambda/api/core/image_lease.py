# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#394 step1 — Host 级 image-operation lease + fence(ADR §4.8)。

为什么存在:今天"一台 host 同时只跑一个 pull"靠 `host.status active/idle→upgrading`
的 CAS(host_service.py:1091 `_set_host_upgrading`)+ 单值 `pull_command_id` 兜。这套在
per-VM 灰度下不够用:
  · canary pull 【不能】改 host.status(否则该 host 停接 live 租户,违背"存量零影响"),
    于是原来的 status CAS 失去互斥作用;
  · 单值 `pull_command_id` 分不清并发的不同镜像操作,也管不住 promote/rollback/cleanup。

故把"镜像操作互斥"从 host 调度状态里【分离】出来,做成独立 lease:
    active_image_operation_id / image_lease_owner / image_lease_until / image_fence_epoch

fence 的作用(ADR §4.8 规则 4):lease 过期被接管时 `image_fence_epoch` 递增,旧执行者
带着旧 epoch 就再也提交不了——这挡的是"Lambda 超时后旧命令晚到,把新任务的结果覆盖掉"。

本步只提供 acquire/release/read 与 fence 递增;把 pull/promote/rollback/cleanup 全部改
成走 lease 是 step 4-5 的事(本步不改现有 live pull 的 status CAS 行为)。
"""

import time

from core.clients import hosts_table
from core.utils import _now

# lease 默认时长:pull 最长可跑满 Lambda 900s,留足冗余到 20 分钟;到期即可被接管
# (接管方递增 fence_epoch,旧执行者失去提交资格)。
DEFAULT_LEASE_SECONDS = 1200


def _epoch_now():
    return int(time.time())


def read(instance_id):
    """读该 host 当前 lease 状态。返回 dict(缺字段给默认值),host 不存在 → None。"""
    item = hosts_table.get_item(
        Key={"instance_id": instance_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return None
    return {
        "active_image_operation_id": item.get("active_image_operation_id"),
        "image_lease_owner": item.get("image_lease_owner"),
        "image_lease_until": int(item.get("image_lease_until") or 0),
        "image_fence_epoch": int(item.get("image_fence_epoch") or 0),
        "image_lease_released_at_epoch": int(item.get("image_lease_released_at_epoch") or 0),
        "image_lease_released_operation_id": item.get("image_lease_released_operation_id"),
        "status": item.get("status"),
    }


def is_held(lease, now=None):
    """lease 是否仍被别人有效持有(纯函数,便于单测)。

    过期(image_lease_until <= now)视为未持有 —— 可被接管。没有 operation_id 也视为未持有。
    """
    if not lease:
        return False
    if not lease.get("active_image_operation_id"):
        return False
    now = _epoch_now() if now is None else now
    return lease.get("image_lease_until", 0) > now


def acquire(instance_id, operation_id, owner, lease_seconds=DEFAULT_LEASE_SECONDS):
    """抢该 host 的 image lease。返回 (fence_epoch, None) 或 (None, reason)。

    条件写语义(单条原子 UpdateItem,不留 check-then-write 窗口):
      · 没有 active_image_operation_id → 可抢;
      · 或 image_lease_until <= now(上一个持有者的 lease 已过期)→ 可接管;
      · 或 active_image_operation_id 就是自己(同一操作重入/重试)→ 续租。
    接管过期 lease 时 image_fence_epoch +1,旧执行者带旧 epoch 无法再提交(ADR §4.8 规则 4)。
    返回的 fence_epoch 是本次持有者应携带的值。
    """
    now = _epoch_now()
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        resp = hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "SET active_image_operation_id = :op, image_lease_owner = :own, "
                "image_lease_until = :until, image_lease_acquired_at = :t, "
                "image_fence_epoch = if_not_exists(image_fence_epoch, :zero) + :bump"
            ),
            ConditionExpression=(
                "attribute_exists(instance_id) AND ("
                "attribute_not_exists(active_image_operation_id) "
                "OR active_image_operation_id = :op "
                "OR image_lease_until <= :now)"
            ),
            ExpressionAttributeValues={
                ":op": operation_id,
                ":own": owner,
                ":until": now + int(lease_seconds),
                ":t": _now(),
                ":now": now,
                ":zero": 0,
                ":bump": 1,
            },
            ReturnValues="UPDATED_NEW",
        )
    except ccf:
        return None, "another image operation holds the lease on this host"
    epoch = resp.get("Attributes", {}).get("image_fence_epoch")
    return int(epoch or 0), None


def release(instance_id, operation_id):
    """释放 lease(只有持有者能释放)。返回 True=已释放,False=不是持有者/host 不存在。

    条件 active_image_operation_id == operation_id:防止"超时后的旧执行者"把接管者的
    lease 误释放(那会让第三方立刻抢到锁,与接管者并发写同一 host)。
    """
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "REMOVE active_image_operation_id, image_lease_owner, image_lease_until "
                "SET image_lease_released_at = :t, image_lease_released_at_epoch = :e, "
                "image_lease_released_operation_id = :op"
            ),
            ConditionExpression="active_image_operation_id = :op",
            ExpressionAttributeValues={
                ":op": operation_id, ":t": _now(), ":e": _epoch_now()
            },
        )
    except ccf:
        return False
    return True


def fence_valid(instance_id, operation_id, fence_epoch):
    """提交前校验:本执行者是否仍是 lease 持有者、且 fence_epoch 未被接管递增。

    任何一项不符 → False,调用方必须放弃提交(无副作用退出)。强一致读,不看缓存。
    """
    lease = read(instance_id)
    if not lease:
        return False
    if not is_held(lease):
        return False
    if lease.get("active_image_operation_id") != operation_id:
        return False
    return int(lease.get("image_fence_epoch") or 0) == int(fence_epoch)
