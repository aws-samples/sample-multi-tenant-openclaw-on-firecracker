# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#394 step6 — 同步槽位操作的 intent/result 持久化(ADR §4.9)。

为什么需要:promote/rollback/cleanup 是同步接口,但"响应丢了"是常态(客户端超时、
API GW 29s 断开)。此时调用方只能重试。光靠 CAS 幂等能保证**安全**(不会重复提升),
但保证不了**同一答案** —— 第二次调用看到的世界已经变了(canary 已空、generation 已 +1),
返回值必然不同,调用方无法确定"我那次到底成没成"。

故按 ADR §4.9:同步操作在【发起 host 命令之前】持久化 intent,提交后持久化 result;
带同一 Idempotency-Key 重放时直接返回已存的 result。

复用 pull Job 表(openclaw-image-jobs):同一张表存两类记录,用 job_id 前缀区分
(`pull-*` 是异步 pull,`op-*` 是同步槽位操作),省一张表、省一套 IAM/CDK 接线。
两者字段形状本来就一致(state/result/error/idempotency_key + TTL)。
"""

import core.clients as clients
from core import image_jobs
from core.utils import _now

# 与 image_jobs 同表(见模块 docstring)。惰性读,理由同 image_jobs._table。
def _table():
    return getattr(clients, "image_jobs_table", None)


# 同步操作的状态:在跑 + 两个终态 + 一个"结果未知"态(#394 P1-1)。
STATE_IN_PROGRESS = "IN_PROGRESS"
STATE_SUCCEEDED = "SUCCEEDED"
STATE_FAILED = "FAILED"
# #394 P1-1 —— UNKNOWN:host 命令超时/回执丢失,可能【已提交也可能没】。绝不能当 FAILED
# (那样同 key 重试会被 409 OPERATION_FAILED_PREVIOUSLY 挡死,违背"503 后同 key 重试对账"承诺)。
# 重放到 UNKNOWN 记录时【重新执行】对账:promote/cleanup/reclaim 都幂等,已提交则再跑收敛成
# already_promoted / 已空 / no-op,未提交则真正做完。
STATE_UNKNOWN = "UNKNOWN"


def find_by_idempotency_key(instance_id, idempotency_key, operation):
    """同 Idempotency-Key 重放 → 返回已存记录(含 result)。

    校验 operation 一致:同一个 key 被用在不同操作上属调用方错误,返回记录让上层判成
    IDEMPOTENCY_KEY_REUSED(不能拿 promote 的结果去答别的操作)。

    #394 P1-1 —— 同一 key 可能有多条记录(UNKNOWN 重试会各写一条新 op_id 行)。选取优先级:
    ① 同 operation 且 SUCCEEDED(一旦有一次成功,重放就返回那个确定答案);
    ② 否则同 operation 的最近一条(UNKNOWN/IN_PROGRESS/FAILED,决定重放该怎么走);
    ③ 没有同 operation 记录但有别 operation 记录 → 返回它(上层报 IDEMPOTENCY_KEY_REUSED)。
    """
    table = _table()
    if table is None or not idempotency_key or not instance_id:
        return None
    resp = table.query(
        IndexName="gsi_idempotency",
        KeyConditionExpression="instance_id = :i AND idempotency_key = :k",
        ExpressionAttributeValues={":i": instance_id, ":k": idempotency_key},
        Limit=25,
    )
    items = resp.get("Items") or []
    same_op = [it for it in items if it.get("operation") == operation]
    if same_op:
        succeeded = [it for it in same_op if it.get("state") == STATE_SUCCEEDED]
        if succeeded:
            return succeeded[0]
        # 最近一条(created_at 字符串 ISO8601 可直接比较)
        return max(same_op, key=lambda it: it.get("created_at") or "")
    return items[0] if items else None


def record_intent(operation_id, instance_id, operation, expected, idempotency_key=None):
    """提交【前】落 intent。返回 True=已写。

    为什么必须先写:若只在提交后写,那么"已 rename slots.json 但 Lambda 随即挂掉"这一
    窗口里没有任何记录 —— 重试时既查不到 result,也无法区分"没做"与"做了没回执"。
    """
    table = _table()
    if table is None:
        return False
    item = {
        "job_id": operation_id,
        "instance_id": instance_id,
        "operation": operation,
        "state": STATE_IN_PROGRESS,
        "expected": expected or {},
        "created_at": _now(),
        "updated_at": _now(),
    }
    if idempotency_key:
        item["idempotency_key"] = idempotency_key
    # 与 pull Job 同表同 TTL 属性(expires_at),保留 30 天供排障/幂等重放窗口。
    item["expires_at"] = image_jobs._ttl_epoch()
    ccf = table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
    except ccf:
        return False
    return True


def record_result(operation_id, ok, result=None, error=None, state=None):
    """提交【后】落 result(重放时原样返回它)。best-effort:不因记账失败而否定已提交的事实。

    #394 P1-1 —— 可显式传 state(如 STATE_UNKNOWN);不传时按 ok 落 SUCCEEDED/FAILED(旧语义)。
    UNKNOWN 用于"host 命令超时/回执丢失,可能已提交"——绝不落 FAILED(否则同 key 重试被挡死)。
    """
    table = _table()
    if table is None:
        return False
    resolved_state = state or (STATE_SUCCEEDED if ok else STATE_FAILED)
    expr = ["#s = :s", "updated_at = :t"]
    names = {"#s": "state"}
    values = {":s": resolved_state, ":t": _now()}
    if result is not None:
        expr.append("#r = :res")
        names["#r"] = "result"
        values[":res"] = result
    if error is not None:
        expr.append("#e = :err")
        names["#e"] = "error"
        values[":err"] = error
    try:
        table.update_item(
            Key={"job_id": operation_id},
            UpdateExpression="SET " + ", ".join(expr),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:  # noqa: BLE001 — 见 docstring
        return False
    return True
