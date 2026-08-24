# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/credential_reconciler — 消费 `vkey_revoke_failed` 标记,重试回收 LiteLLM vkey。

ISSUE-PROPOSAL-credential-reclaim-reconciler-2026-07-07.md`(不重新设计)。

**要修的是"标记打了没人读"**:删租户时 vkey 回收是 best-effort,失败就保留 `litellm_vkey`
并打 `vkey_revoke_failed`,让孤儿【可被发现】—— 但 bb 上没有任何消费者(`CHANGELOG.md` 自己
记着「`vkey_revoke_failed` 标记目前无 reconciler 消费」)。于是那把活 key 永久留在 LiteLLM:
凭据 + 预算泄漏,随 churn 累积。

⚠️ issue 正文把第 3 项描述成「CAS 提交 deleted 后、撤 vkey 前崩溃 → 活 vkey 残留」,那个窗口
在 bb 上**不存在**:两条删除路径的 revoke 都在 CAS/finalize 之前,而 marker 与「翻 deleted」
是同一条 `update_item`(本来就原子)。所以本模块**不新增 marker、不改 CAS**,只补消费者。

**落点是 api Lambda 的事件路由,不是 health_check**:实测 `openclaw-api` 的环境变量里有
`LITELLM_MASTER_KEY_SECRET`,而 `openclaw-health-check` 一个 LiteLLM 相关的都没有 ——
放在 health_check 里它连 master key 都读不到,是一个注定空转的 reconciler。

依赖方向合法:services → core(clients/utils/vkey/ddb_scan)。
"""

import core.clients as clients
import core.ddb_scan as ddb_scan
import core.utils as utils
import core.vkey as vkey

# 重试上限。超过即转 `credential_reclaim_exhausted` 并停手 —— LiteLLM 长期不可达是运维
# 问题,不是该无限重试的东西(无限重试会把一条真实故障磨成永久静默的背景噪音)。
# 15 分钟一拍 × 10 次 ≈ 覆盖 2.5 小时的瞬时故障,够长;再长该由人介入。
VKEY_RECLAIM_MAX_ATTEMPTS = 10


def _scan_pending():
    """扫出「已删且 vkey 回收失败、且还没判 exhausted」的租户行。

    标记稀疏(只在真回收失败时才打),所以低频 scan 可接受(设计文档已论证)。
    `scan_all` 而不是裸 `scan`:#432 —— DDB 单次 Scan 上限 1MB 且 FilterExpression 在那 1MB
    读【之后】才过滤,不翻页就是"明明有孤儿却扫不到",而且没有任何异常。
    """
    return ddb_scan.scan_all(
        clients.tenants_table,
        FilterExpression=(
            "#s = :deleted AND vkey_revoke_failed = :true "
            "AND attribute_exists(litellm_vkey) "
            "AND attribute_not_exists(credential_reclaim_exhausted)"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":deleted": "deleted", ":true": True},
    )


def _clear_marker(tenant_id, key):
    """回收确认成功 → 清 key 与标记。

    条件把这次写**钉在被扫到的那一行的那把 key 上**(codex 独立复审 blocker-1):
      · `litellm_vkey = :key` —— 只清我们【真的撤销过的】那把。只锁标记的话,若这一行的
        key 在"扫描 → 撤销 → 清理"之间被换成另一把,我们会把一把**没撤销过的**活 key
        字段删掉 → 那把 key 再也找不回来,正是本 issue 要防的方向。
      · `#s = :deleted` —— 只动终态行。行离开 deleted 就不再是孤儿,不该被这里碰。
    条件不满足即 CCF 出局(并发的第二个 invocation 也走这条),幂等且防 ABA。
    """
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET updated_at = :t "
                "REMOVE litellm_vkey, vkey_revoke_failed, vkey_reclaim_attempts"
            ),
            ConditionExpression=(
                "vkey_revoke_failed = :true AND litellm_vkey = :key AND #s = :deleted"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":true": True, ":key": key, ":deleted": "deleted",
                ":t": utils._now(),
            },
        )
        return True
    except Exception as e:  # noqa: BLE001 — CCF(别的 invocation 先清了)也走这里
        print(f"credential-reconciler: clear marker {tenant_id} skipped ({e})")
        return False


def _count_attempt(tenant_id, attempts, key):
    """回收仍失败 → 计一次重试;到上限则转 exhausted 并停手(fail-loud,等人工)。

    `ADD` 而不是 `SET attempts + 1`:多 invocation 并发时 ADD 是原子累加,读-改-写会丢计数。

    条件三条(codex 独立复审 blocker-2 收紧):
      · `vkey_revoke_failed = :true` —— 标记还在才计数,不给已清理的行凭空写回字段。
      · `attribute_not_exists(credential_reclaim_exhausted)` —— **已判 exhausted 就不再累加**。
        没有这条时,两个并发 invocation 各自读到 attempts=9、各自 ADD,计数会越过上限
        (虽然 exhausted 仍会被设上、扫描过滤仍会让它退出候选集,所以不会无限重试,
        但计数越界会让"重试了多少次"这个运维判据失真)。
      · `litellm_vkey = :key` —— 与 `_clear_marker` 同一条理由:只给被扫到的那把 key 计数。
    """
    reached = attempts + 1 >= VKEY_RECLAIM_MAX_ATTEMPTS
    expr = "ADD vkey_reclaim_attempts :one"
    vals = {":one": 1, ":true": True, ":key": key}
    if reached:
        expr += " SET credential_reclaim_exhausted = :true, updated_at = :t"
        vals[":t"] = utils._now()
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=expr,
            ConditionExpression=(
                "vkey_revoke_failed = :true AND litellm_vkey = :key "
                "AND attribute_not_exists(credential_reclaim_exhausted)"
            ),
            ExpressionAttributeValues=vals,
        )
    except Exception as e:  # noqa: BLE001
        print(f"credential-reconciler: count attempt {tenant_id} skipped ({e})")
        return False
    if reached:
        # fail-loud:停手这件事必须看得见,否则它与"已经修好了"在指标上长得一样。
        print(
            f"credential-reconciler: {tenant_id} EXHAUSTED after "
            f"{attempts + 1} attempts — a live LiteLLM key may still exist; "
            f"reclaim it manually and clear credential_reclaim_exhausted"
        )
    return reached


def reconcile_credentials():
    """一拍对账。返回统计 dict(供日志/指标),**任何单个租户的失败都不中断整轮**。

    幂等:每个租户的写都条件锁 `vkey_revoke_failed = :true`,所以重复投递 / 并发 invocation
    最多让第二个 CCF 出局,不会重复回收也不会重复计数。
    """
    stats = {"scanned": 0, "reclaimed": 0, "retried": 0, "exhausted": 0, "errors": 0}
    try:
        pending = _scan_pending()
    except Exception as e:  # noqa: BLE001 — 扫不到不该让整个 Lambda 失败
        print(f"credential-reconciler: scan failed ({e})")
        stats["errors"] += 1
        return stats
    stats["scanned"] = len(pending)
    for t in pending:
        tid = t.get("id")
        key = t.get("litellm_vkey")
        if not (tid and key):
            continue
        try:
            attempts = int(t.get("vkey_reclaim_attempts", 0) or 0)
            # 一把早已不存在的 key 会被一路重试到 exhausted 并打假告警 —— 本 reconciler
            # 能不能收敛,完全取决于那个判据。
            if vkey._revoke_tenant_vkey(key):
                if _clear_marker(tid, key):
                    stats["reclaimed"] += 1
                    print(f"credential-reconciler: reclaimed vkey for {tid}")
                continue
            if _count_attempt(tid, attempts, key):
                stats["exhausted"] += 1
            else:
                stats["retried"] += 1
        except Exception as e:  # noqa: BLE001 — 单个租户失败不拖累整轮
            print(f"credential-reconciler: {tid} failed ({e})")
            stats["errors"] += 1
    if stats["scanned"]:
        print(f"credential-reconciler: {stats}")
    return stats
