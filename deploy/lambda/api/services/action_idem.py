# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#456 — tenant_action 的 client_token 幂等记录(ADR-rebuild-idempotency-sync-contract §5.1)。

**为什么需要**:客户接口表声明 rebuild 的 `client_token`「供上游做幂等」,而控制面此前
全文零命中该字段 —— 承诺了却没实现。而 rebuild 是 `stop && rm overlay && launch`:客户
超时后带**同一个 token** 重试,若控制面不认这个 token,就会第二次删掉 overlay,抹掉两次
之间落盘的写入(no-data-loss 红线)。

`REBUILD_IN_FLIGHT` 闸挡不住这一类:它只拦「上一次还在飞」,而重试常常发生在上一次已经
收敛之后(客户没收到响应,但服务端早已 done)。那时闸放行,于是又跑一遍破坏性操作。

**形状**(沿用 `core/image_ops.py` 已验证的 intent→result 模式,不发明新机制):

- 键:`idem#<tenant_id>#<action>#<sha256(owner_id\0client_token)[:16]>`,写 tenants 主表
  (与 `inflight#` / `activename#` 同表前缀隔离)。owner 进哈希是因为客户侧的 token 未必
  全局唯一,不同 owner 用同一字符串不该互撞。
- 发起破坏性步骤【之前】写 intent(`attribute_not_exists(id)`),内含派生出的 op_id。
  条件写失败 = 同 token 的操作已在跑或已完成 → 强一致读回:
    · 有终态 result → 原样返回它(同一答案,不重跑);
    · 仍 IN_PROGRESS → 409 + 该 op_id,让调用方轮询而不是重试。
- 完成后写 result。
- **结果未知态必须落 UNKNOWN 而非 FAILED**:`image_ops.py` 已论证过 —— 落 FAILED 会让
  同 token 的重试被永久挡死,违背「重试可对账」。UNKNOWN 允许重放去做对账。

**TTL 的坑(ADR 未提,实测得出)**:ADR 说「复用 `image_jobs._ttl_epoch()` 同款 30 天」,
但那是 image-jobs 表的 `expires_at`;**tenants 表的真实 TTL 属性是 `inflight_ttl`**
(`storage.py:50`,DDB 每表仅一个)。写 `expires_at` 到 tenants 表不会被清理,记录会永久
堆积。故这里写 `inflight_ttl`。同时**刻意不用 30 天**:幂等窗口只需覆盖「客户重试」的
现实跨度(分钟到小时级),而 30 天的记录会让一个月前用过的 token 意外命中、把陈旧结果当
成本次答案返回。取 24h,可用环境变量覆盖。
"""

import hashlib
import os
import time

import core.clients as clients
from core.utils import _now

# 与 image_ops 同名同义的四态。UNKNOWN 的存在理由见模块 docstring。
STATE_IN_PROGRESS = "IN_PROGRESS"
STATE_SUCCEEDED = "SUCCEEDED"
STATE_FAILED = "FAILED"
STATE_UNKNOWN = "UNKNOWN"
# The operation definitely did not reach its destructive dispatch point. Unlike
# UNKNOWN, replaying this state is unconditionally safe.
STATE_NOT_STARTED = "NOT_STARTED"

_TERMINAL_STATES = (STATE_SUCCEEDED, STATE_FAILED)

# 幂等窗口。见 docstring 的 TTL 说明:不取 30 天,避免陈旧 token 命中。
_IDEM_TTL_SECONDS = int(os.environ.get("ACTION_IDEM_TTL_SECONDS", str(24 * 3600)))

# 只对【破坏性且不可自然幂等】的 action 启用。start/stop/restart 的脚本本身幂等,重复执行
# 是安全 no-op,给它们加幂等记录只增加写放大与失败面。rebuild/reset 会丢 overlay,
# 重复执行会抹掉两次之间的写入 —— 正是需要 token 幂等的那一类。
IDEMPOTENT_ACTIONS = frozenset({"rebuild", "reset"})


def _ttl_epoch():
    return int(time.time()) + _IDEM_TTL_SECONDS


def idem_id(tenant_id, action, owner_id, client_token):
    """幂等记录的主键。owner 与 token 一起哈希:客户侧 token 未必全局唯一,
    不同 owner 用同一字符串不该互相命中(A 的重试拿到 B 的结果 = 跨租户信息泄漏)。

    用 \0 分隔而非 ':':token 里可能含冒号,拼接歧义会让
    (owner="a:b", token="c") 与 (owner="a", token="b:c") 撞到同一个键。
    """
    digest = hashlib.sha256(
        f"{owner_id}\0{client_token}".encode("utf-8")
    ).hexdigest()[:16]
    return f"idem#{tenant_id}#{action}#{digest}"


def derive_op_id(owner_id, tenant_id, action, client_token):
    """operation-stable op_id(ADR §5.1 末段 / #413 P0 要的那个)。

    同一逻辑操作的所有重投(同步重试、SQS 重投)共享它,不同操作必然不同 —— 这正是
    事后对账「迟到的确认绝不 restamp」所依赖的判据:条件写绑这个 id,旧操作的结论就
    盖不到新操作头上。

    不传 client_token 时调用方【不应】调本函数(保持现状:同步路径无 op_id、队列路径
    继续用 lifecycle_dispatch 的 per-call uuid,不回退 #364 的 FIFO dedup 修复)。
    """
    return hashlib.sha256(
        f"{owner_id}\0{tenant_id}\0{action}\0{client_token}".encode("utf-8")
    ).hexdigest()


def begin(tenant_id, action, owner_id, client_token):
    """在发起破坏性步骤【之前】占位。返回 (op_id, existing)。

    · (op_id, None)   —— 抢到了,调用方继续执行,完成后必须调 finish()。
    · (op_id, item)   —— 同 token 已有记录,调用方据其 state 决定:终态则原样返回它的
                          result,IN_PROGRESS 则 409 让调用方轮询。

    条件写失败后走**强一致读**:弱一致读可能读不到刚写入的那条,从而误判成「没有记录」
    而放行第二次破坏性操作 —— 那正是本模块要防的事。

    表未配置(本地/单测环境)→ 返回 (op_id, None) 放行,不因为可观测设施缺失而拒绝服务。
    """
    op_id = derive_op_id(owner_id, tenant_id, action, client_token)
    table = getattr(clients, "tenants_table", None)
    if table is None:
        return op_id, None
    key = idem_id(tenant_id, action, owner_id, client_token)
    try:
        table.put_item(
            Item={
                "id": key,
                "state": STATE_IN_PROGRESS,
                "op_id": op_id,
                "tenant_id": tenant_id,
                "action": action,
                "created_at": _now(),
                "inflight_ttl": _ttl_epoch(),
            },
            ConditionExpression="attribute_not_exists(id)",
        )
        return op_id, None
    except Exception as e:  # noqa: BLE001 — 含 ConditionalCheckFailedException
        if type(e).__name__ != "ConditionalCheckFailedException" and not _is_ccf(e):
            # 真正的写失败(限流/权限)。fail-open:幂等是防重复的加固,不该让它的
            # 故障把一次合法操作也挡掉。记日志由调用方决定如何处理。
            print(f"action_idem begin failed for {key}: {e}")
            return op_id, None
        existing = table.get_item(Key={"id": key}, ConsistentRead=True).get("Item")
        return op_id, existing


def _is_ccf(exc):
    """boto3 的 ConditionalCheckFailedException 在不同封装下类名/属性不一致,统一判定。"""
    code = getattr(getattr(exc, "response", None), "get", lambda *_: None)("Error") or {}
    return (code or {}).get("Code") == "ConditionalCheckFailedException"


def finish(tenant_id, action, owner_id, client_token, state, result=None):
    """写结果。state 取 SUCCEEDED / FAILED / UNKNOWN / NOT_STARTED。

    绝不新建记录(`attribute_exists(id)`):若 begin 那步没落库(表缺失/写失败 fail-open),
    这里也不该凭空造一条 —— 否则会出现「没占位却有结果」的记录,让后续同 token 请求拿到
    一个从未真正占位过的答案。

    SUCCEEDED 单调:迟到的失败/未知回执不得把已确认成功退回非终态。

    写失败只记日志:结果记录是给重放用的加固,拿不到它远好于让一次已经成功的操作报错。
    """
    table = getattr(clients, "tenants_table", None)
    if table is None:
        return
    key = idem_id(tenant_id, action, owner_id, client_token)
    try:
        condition = "attribute_exists(id)"
        values = {
            ":s": state,
            ":r": result or {},
            ":t": _now(),
            ":ttl": _ttl_epoch(),
        }
        if state != STATE_SUCCEEDED:
            condition += " AND #st <> :succeeded"
            values[":succeeded"] = STATE_SUCCEEDED
        table.update_item(
            Key={"id": key},
            UpdateExpression=(
                "SET #st = :s, #rs = :r, finished_at = :t, inflight_ttl = :ttl"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#st": "state", "#rs": "result"},
            ExpressionAttributeValues=values,
        )
    except Exception as e:  # noqa: BLE001
        if _is_ccf(e):
            return
        print(f"action_idem finish({state}) failed for {key}: {e}")


def claim_rerun(tenant_id, action, owner_id, client_token, expected_state):
    """Atomically move one retryable record back to IN_PROGRESS.

    Multiple callers can read UNKNOWN/NOT_STARTED concurrently. Only the one
    that wins this conditional update may dispatch the operation again.
    """
    if expected_state not in (STATE_UNKNOWN, STATE_NOT_STARTED):
        return False
    table = getattr(clients, "tenants_table", None)
    if table is None:
        return True
    key = idem_id(tenant_id, action, owner_id, client_token)
    try:
        table.update_item(
            Key={"id": key},
            UpdateExpression=(
                "SET #st = :in_progress, resumed_at = :t, inflight_ttl = :ttl "
                "REMOVE #rs, finished_at"
            ),
            ConditionExpression="attribute_exists(id) AND #st = :expected",
            ExpressionAttributeNames={"#st": "state", "#rs": "result"},
            ExpressionAttributeValues={
                ":in_progress": STATE_IN_PROGRESS,
                ":expected": expected_state,
                ":t": _now(),
                ":ttl": _ttl_epoch(),
            },
        )
        return True
    except Exception as e:  # noqa: BLE001
        if not _is_ccf(e):
            print(f"action_idem claim_rerun failed for {key}: {e}")
        return False


def replay_decision(existing):
    """把已存记录翻译成「重放该怎么走」。返回 (kind, payload)。

    · ("return", result)  —— 有确定答案,原样返回(同一 token 得到同一答案)。
    · ("poll", op_id)     —— 仍在跑,让调用方轮询,**不要重试**。
    · ("rerun", op_id)    —— UNKNOWN:可能已执行也可能没,重放去做对账。
    · ("rerun", op_id)    —— NOT_STARTED:明确未下发,安全重试。

    UNKNOWN 走 rerun 而非 return,是 image_ops.py 已论证过的结论:落 FAILED/直接返回会
    让同 token 重试被挡死,而 UNKNOWN 本身就表示「需要再确认一次」。
    调用方对 rebuild 应把 rerun 视为「允许继续,但仍受 REBUILD_IN_FLIGHT 闸约束」。
    """
    state = (existing or {}).get("state") or ""
    op_id = (existing or {}).get("op_id") or ""
    if state in _TERMINAL_STATES:
        return "return", (existing or {}).get("result") or {}
    if state in (STATE_UNKNOWN, STATE_NOT_STARTED):
        return "rerun", op_id
    return "poll", op_id
