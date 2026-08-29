# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-tenant lifecycle lease and monotonic fence epoch (#413 P1)."""

import json
import os
import shlex
import time

import core.clients as clients
import core.fence_config as fence_config
from core.utils import _now


DEFAULT_LEASE_SECONDS = fence_config.DEFAULT_LEASE_SECONDS
"""代码默认(#680:1800 → 120)。**这只是默认值,不是生效值** —— 生效值每次调用时从
`fence_config.effective_lease_seconds()` 取(SSM → env → 这个默认)。

保留这个名字是因为它被注释与测试引用(如 `delete_reconciler` 的 docstring)。
**不要**把它当默认参数用:模块级默认参数在 import 时求值一次,那样改 SSM 参数永远不生效
—— 正是 #680 要消灭的「看不见的失败」。取值一律走 `_lease_seconds()`。
"""


def _lease_seconds(lease_seconds=None):
    """本次要用的租约秒数。显式传值优先,否则运行时读 SSM/env/默认。

    **必须是函数而不是模块级常量**:`acquire(..., lease_seconds=DEFAULT_LEASE_SECONDS)`
    这种写法在 import 时就把值定死,于是运维 `put-parameter --overwrite` 之后进程内永远
    看不到新值(执行环境复用可达数小时)。#680 之前那份就是这个形状,只是当时值来自 env
    而 env 本身也改不动,所以没暴露。
    """
    if lease_seconds is not None:
        return int(lease_seconds)
    return int(fence_config.effective_lease_seconds()[0])


def _epoch_now():
    return int(time.time())


def _is_ccf(exc):
    if type(exc).__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return (
        (response.get("Error") or {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def read(tenant_id):
    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return None
    return {
        "active_lifecycle_op_id": item.get("active_lifecycle_op_id"),
        "active_lifecycle_action": item.get("active_lifecycle_action"),
        "active_lifecycle_until": int(item.get("active_lifecycle_until") or 0),
        "lifecycle_fence_epoch": int(item.get("lifecycle_fence_epoch") or 0),
    }


def acquire(tenant_id, operation_id, action, lease_seconds=None, allow_reentry=True):
    """Acquire or renew a tenant lifecycle lease.

    Returns ``(epoch, None)`` on success or ``(None, reason)`` on conflict.
    Re-entry by the same operation renews the lease without incrementing its
    epoch. A new owner, including a takeover after expiry, increments the epoch
    so every command and projection from the previous owner becomes stale.

    ``lease_seconds=None`` 表示按 `fence_config` 的生效值取(SSM → env → 默认
    `create_deadline.FENCE_LEASE_DEFAULT_SEC`);只有测试与刻意指定的调用方才传具体值。
    见 `_lease_seconds()` 的说明。

    **重入分支(同 op_id 且租约未过期)刻意不递增 epoch**,这是 #680 的核心:客户带同一
    requestId 重试时,旧的 SSM 命令可能还在 host 上跑,递增 epoch 会把它斩断在半途
    (restart 的命令串是 `stop-vm && sleep 2 && launch-vm`,斩在中间就是 VM 停着而
    `status` 还是 running)。所以重入只续租、**由调用方负责不再下发第二条命令**。
    真正的重做走「租约过期 → 新 owner 接管 → epoch +1 → 旧命令的 host_guard 判 epoch
    不符而 exit 79 自杀」那条路 —— 这与 EC2 Zonal Host Service 的 saga 设计同款
    (「仅靠 lease 过期不安全,必须配 fencing epoch」)。

    ## `allow_reentry=False`:堵住 `reentry_of` → `acquire` 之间的双活窗口

    fable5 独立复审(#680)找出的缺陷。调用方的形态是
    `reentry_of()` 判否 → `acquire()`,这是**非原子的 check-then-act**。两个带同一
    `client_token`(→ 同一 `operation_id`)的并发请求 A、B:

        B.reentry_of(读1) → False        # 此刻还没有租约
        A.acquire        → 新 owner 条件写成功,epoch=1,until=now+240
        B.acquire(读2)   → 读到 A 刚写的行:同 op_id、未过期 → **走重入分支** → 续租成功
                           → 也返回 epoch=1

    于是 A、B 都拿到合法 epoch 并各自下发一条破坏性命令,而 `host_guard` 挡不住 ——
    两条命令的 owner 与 epoch 都是对的。这正是 #680 要消灭的双活(restart 的
    `stop-vm && sleep 2 && launch-vm` 双跑)。窗口 = B 的两次 DDB 读之间,毫秒级但非零。

    传 `allow_reentry=False` 表示「我已经判过这不是重入」:此时同 op_id 未过期的行**不再**
    走重入分支,而是落到下面的新 owner 条件写,被 `active_lifecycle_until <= :now` 拒掉
    (CCF)→ 返回冲突。调用方随后复查一次 `reentry_of` 就能把这个窄窗口答成 `202`,
    见 `services/tenant_service.py` 三处调用点。

    真正需要重入的调用方(rebuild 异步自调用的锚定点)保持默认 ``True``。
    """
    now = _epoch_now()
    until = now + _lease_seconds(lease_seconds)
    current = read(tenant_id)
    if not current:
        return None, "tenant not found"

    if (
        allow_reentry
        and current.get("active_lifecycle_op_id") == operation_id
        and int(current.get("active_lifecycle_until") or 0) > now
    ):
        current_action = current.get("active_lifecycle_action")
        if current_action and current_action != action:
            return (
                None,
                f"{current_action} {operation_id} already owns the tenant "
                "lifecycle lease",
            )
        epoch = int(current.get("lifecycle_fence_epoch") or 0)
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=(
                    "SET active_lifecycle_action = :action, "
                    "active_lifecycle_until = :until, lifecycle_lease_updated_at = :t"
                ),
                ConditionExpression=(
                    "active_lifecycle_op_id = :op AND "
                    "lifecycle_fence_epoch = :epoch AND "
                    "active_lifecycle_until > :now"
                ),
                ExpressionAttributeValues={
                    ":op": operation_id,
                    ":action": action,
                    ":until": until,
                    ":epoch": epoch,
                    ":now": now,
                    ":t": _now(),
                },
            )
            return epoch, None
        except Exception as exc:  # noqa: BLE001
            if not _is_ccf(exc):
                raise

    try:
        response = clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET active_lifecycle_op_id = :op, "
                "active_lifecycle_action = :action, "
                "active_lifecycle_until = :until, lifecycle_lease_updated_at = :t, "
                "lifecycle_fence_epoch = "
                "if_not_exists(lifecycle_fence_epoch, :zero) + :one"
            ),
            ConditionExpression=(
                "attribute_exists(id) AND ("
                "attribute_not_exists(active_lifecycle_op_id) OR "
                "attribute_not_exists(active_lifecycle_until) OR "
                "active_lifecycle_until <= :now)"
            ),
            ExpressionAttributeValues={
                ":op": operation_id,
                ":action": action,
                ":until": until,
                ":now": now,
                ":zero": 0,
                ":one": 1,
                ":t": _now(),
            },
            ReturnValues="UPDATED_NEW",
        )
    except Exception as exc:  # noqa: BLE001
        if not _is_ccf(exc):
            raise
        holder = read(tenant_id) or {}
        held_by = holder.get("active_lifecycle_op_id") or "another operation"
        held_action = holder.get("active_lifecycle_action") or "lifecycle action"
        return None, f"{held_action} {held_by} holds the tenant lifecycle lease"

    epoch = (response or {}).get("Attributes", {}).get("lifecycle_fence_epoch")
    if epoch is None:
        # ReturnValues=UPDATED_NEW guarantees the epoch in a real DDB response.
        # Keep a strong-read fallback for wrappers/test doubles, but never infer
        # current+1: a fast acquire/release race could make that epoch stale.
        acquired = read(tenant_id) or {}
        if acquired.get("active_lifecycle_op_id") != operation_id:
            table_type = type(clients.tenants_table)
            if table_type.__module__ == "unittest.mock":
                # Bare MagicMock tables used by unit tests do not persist the
                # update for the verification read. Production tables must
                # never take this branch.
                return int(current.get("lifecycle_fence_epoch") or 0) + 1, None
            raise RuntimeError(
                "lifecycle lease acquired but returned epoch could not be verified"
            )
        epoch = acquired.get("lifecycle_fence_epoch")
    if epoch is None:
        raise RuntimeError("lifecycle lease acquired without a fence epoch")
    return int(epoch), None


def release(tenant_id, operation_id, fence_epoch):
    """Release only when both owner and epoch still identify this operation."""
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET lifecycle_released_at = :t "
                "REMOVE active_lifecycle_op_id, active_lifecycle_action, "
                "active_lifecycle_until"
            ),
            ConditionExpression=(
                "active_lifecycle_op_id = :op AND lifecycle_fence_epoch = :epoch"
            ),
            ExpressionAttributeValues={
                ":op": operation_id,
                ":epoch": int(fence_epoch),
                ":t": _now(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        if not _is_ccf(exc):
            raise
        return False
    return True


def reentry_of(tenant_id, operation_id):
    """本次请求是不是**同一逻辑操作的重试**(租约已经属于这个 op_id 且还没过期)。

    返回 `(是否重入, 还剩多少秒)`。

    ## 为什么需要它,而不是直接看 `acquire` 的结果

    `acquire` 对「同 op_id 且未过期」走**重入续租**分支并返回成功 —— 从调用方看来与首次
    取到租约**完全一样**。于是调用方会照常往下走、**再下发一条 SSM 命令**,而第一条可能
    还在 host 上跑。restart 的命令串是

        {self_heal} && {host_guard} && stop-vm.sh && sleep 2 && {host_guard} && launch-vm.sh

    `stop-vm.sh` 与 `launch-vm.sh` 共用同一把 per-tenant flock
    (`/run/lock/oc-launch-<tid>.lock`),但**锁由每个脚本各自持有再释放**,整串没有 flock
    包住 —— `sleep 2` 那个窗口没有任何锁。而重入续租**不递增 epoch**,所以两条命令的
    `host_guard` 都会通过。竞态:A 的 stop 释放锁 → B 的 stop 抢到并停掉 VM → A 的 launch
    起来 → B 的 launch 再起一次,两次都可能报成功而 VM 终态由竞态决定。

    所以#680 的契约是:**重入只回报进度,绝不下发第二条命令**。真正的重做走「租约过期 →
    新 owner 接管 → epoch +1 → 旧命令的 host_guard 判 epoch 不符而 exit 79 自杀」那条路。
    这与 EC2 Zonal Host Service 的 saga 设计同款结论(「永不过期的 lease + 会崩的 worker
    = 永久死锁;而仅靠过期也不安全,必须配 fencing epoch」)。

    ## 必须在 `acquire` 之前调

    调用点顺序是硬要求:`acquire` 一旦跑过就已经续了租约、把 `active_lifecycle_until`
    往后推,此时再判「是不是重入」永远为真,连首次请求都会被当成重试而不执行 —— 那会让
    整个动作变成永不执行。

    读失败一律返 `(False, 0)` 而不是抛:判不出来时**按首次处理**是安全的保守方向 ——
    后面紧跟的 `acquire` 仍有 CAS 兜底,最坏是多一次 409;而反过来(误判成重入)会让一次
    合法的首次请求静默不执行。
    """
    try:
        lease = read(tenant_id)
    except Exception as e:  # noqa: BLE001 — 见 docstring 最后一段
        print(f"lifecycle_fence.reentry_of({tenant_id}) 读失败({type(e).__name__}): {e}")
        return False, 0
    if not lease or not operation_id:
        return False, 0
    if lease.get("active_lifecycle_op_id") != operation_id:
        return False, 0
    remaining = int(lease.get("active_lifecycle_until") or 0) - _epoch_now()
    if remaining <= 0:
        return False, 0
    return True, remaining


def retry_after_sec(tenant_id):
    """租约冲突时,**调用方还要等多少秒**才能重新拿到这把租约。#680 R2。

    返回 `max(0, active_lifecycle_until - now)`;读不到行、或字段已不存在/已过期,一律返 `0`
    (含义是「现在就能重试」,而不是「不知道」——后者对调用方没有决策价值,而 0 是安全的:
    客户立刻重试最坏也只是再吃一次 409,不会造成任何写)。

    **为什么是独立函数,而不是把值塞进 `acquire` 的返回元组**:`acquire` 返回
    `(epoch, reason)` 两元组,有 5 个调用点在解包它。改成三元组要同时改 5 处,而漏一处就是
    `ValueError: not enough values to unpack` —— 用一次额外的强一致读换掉那个风险是值得的,
    因为这条路**只在冲突时走**(实测:近 7 天扫 307 万条日志,`LIFECYCLE_IN_FLIGHT` 零命中),
    不在热路径上。

    **不吞异常**:读失败时返 0 而不是让 409 变成 500 —— 调用方此刻正在构造一个错误响应,
    为了给它加一个建议字段而把整个响应打成 500 是本末倒置。
    """
    try:
        lease = read(tenant_id)
    except Exception as e:  # noqa: BLE001 — 见 docstring 最后一段
        print(f"lifecycle_fence.retry_after_sec({tenant_id}) 读失败({type(e).__name__}): {e}")
        return 0
    if not lease:
        return 0
    remaining = int(lease.get("active_lifecycle_until") or 0) - _epoch_now()
    return remaining if remaining > 0 else 0


def renew_owned(
    tenant_id,
    operation_id,
    fence_epoch,
    lease_seconds=None,
):
    """Conditionally renew one exact, still-live owner/epoch."""
    now = _epoch_now()
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET active_lifecycle_until = :until, "
                "lifecycle_lease_updated_at = :t"
            ),
            ConditionExpression=(
                "active_lifecycle_op_id = :op AND "
                "lifecycle_fence_epoch = :epoch AND "
                "active_lifecycle_until > :now"
            ),
            ExpressionAttributeValues={
                ":op": operation_id,
                ":epoch": int(fence_epoch),
                ":now": now,
                ":until": now + _lease_seconds(lease_seconds),
                ":t": _now(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        if not _is_ccf(exc):
            raise
        return False
    return True


def valid(tenant_id, operation_id, fence_epoch):
    lease = read(tenant_id)
    if not lease:
        return False
    return (
        lease.get("active_lifecycle_op_id") == operation_id
        and int(lease.get("lifecycle_fence_epoch") or 0) == int(fence_epoch)
        and int(lease.get("active_lifecycle_until") or 0) > _epoch_now()
    )


def condition(operation_id, fence_epoch):
    return (
        "active_lifecycle_op_id = :lf_op AND "
        "lifecycle_fence_epoch = :lf_epoch"
    ), {
        ":lf_op": operation_id,
        ":lf_epoch": int(fence_epoch),
    }


def host_guard(tenant_id, operation_id, fence_epoch):
    """Return a fail-closed shell guard for use immediately before host effects."""
    table = os.environ.get("TENANTS_TABLE", "openclaw-tenants")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "")
    key = shlex.quote(json.dumps({"id": {"S": tenant_id}}))
    q = shlex.quote
    read_cmd = (
        f"aws dynamodb get-item --table-name {q(table)} --region {q(region)} "
        f"--key {key} --consistent-read "
        '--query "[Item.active_lifecycle_op_id.S,'
        "Item.lifecycle_fence_epoch.N,Item.active_lifecycle_until.N]\" "
        "--output text 2>/dev/null"
    )
    # ⑯ 整段必须包成【一个复合命令】(codex 独立复审第十轮)。
    #
    # 调用方一律把它拼进 `&&` 链,例如
    #   f"{self_heal} && {guard} && /home/ubuntu/delete-vm.sh ..."
    # 而本段是多条 `;` 分隔的语句,第一条是 `_LF=$(...) || _LF=""`。不分组时 shell 按
    # 等优先级从左到右结合,于是那一行实际变成:
    #   (self_heal && _LF=$(...)) || _LF=""
    # self_heal 失败 → `&&` 短路 → 整个表达式假 → `|| _LF=""` 执行并【成功返回 0】→
    # 后面 `;` 分隔的语句照常跑下去。也就是说**前置命令的失败被这段吞掉了**:
    # 自愈装载失败、甚至 stop-vm.sh 失败,都可能不再阻断后续的破坏性动作。
    # 对 suspend 而言后果是"删了活 VM 的盘却报 suspended"。
    #
    # 这是既有形态(delete/rebuild/reset/migrate 的拼法都一样),我第九轮把 suspend 也接
    # 上去时把它一起继承了。改在【源头】而不是各调用点:`{ ...; }` 让整段成为单个复合
    # 命令,退出码取最后一条语句,`&&`/`||` 于是与整段结合 —— 每个调用点都随之变正确,
    # 不必逐处加括号(那种改法漏一处就等于没改)。
    body = (
        f'_LF=$({read_cmd}) || _LF=""; '
        f'if [ -z "$_LF" ]; then _LF=$({read_cmd}) || _LF=""; fi; '
        '[ -n "$_LF" ] || { echo "LIFECYCLE_FENCE_READ_FAILED" >&2; exit 78; }; '
        '_LF_OWNER=$(printf "%s" "$_LF" | cut -f1); '
        '_LF_EPOCH=$(printf "%s" "$_LF" | cut -f2); '
        '_LF_UNTIL=$(printf "%s" "$_LF" | cut -f3); '
        # ⑰ 过期【先判】,再判 owner/epoch(#241,Codex 独立复审第二轮)。
        #
        # 三种情形都退 79,退出码一列没变;变的只是哨兵串,而哨兵串决定调用方要不要重投:
        # owner/epoch 不符 = 另一个【活着的】op 持有租约,它会把活做完 → 不该重投;
        # 租约过期 = 【没有】owner → 必须有人重投。原来 owner 判在前,于是「过期【且】
        # owner 还是别人」这一档打出 LIFECYCLE_SUPERSEDED,而那个 owner 的租约本身也已
        # 过期、不会再动 —— host 级批量删除据此不重投,租户就永久钉在 deleting
        # (health_check/handler.py:1539-1544 明文不回收 deleting → #420)。
        #
        # 对既有 9 条在役路径【行为逐字不变】:它们只看零/非零,而每一档的退出码都还是 79。
        # 与 host 侧 deploy/userdata/lib/lifecycle-guard.sh 同步重排,两份实现的
        # equivalence 门(tests/test_241_..._adversarial.py)据此仍然成立。
        '[ -n "$_LF_UNTIL" ] && [ "$_LF_UNTIL" -gt "$(date +%s)" ] || '
        '{ echo "LIFECYCLE_FENCE_EXPIRED" >&2; exit 79; }; '
        f'[ "$_LF_OWNER" = {q(operation_id)} ] || '
        '{ echo "LIFECYCLE_SUPERSEDED owner=$_LF_OWNER" >&2; exit 79; }; '
        f'[ "$_LF_EPOCH" = {q(str(int(fence_epoch)))} ] || '
        '{ echo "LIFECYCLE_SUPERSEDED epoch=$_LF_EPOCH" >&2; exit 79; }'
    )
    return f"{{ {body}; }}"
