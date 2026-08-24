# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/delete_reconciler — 接管卡在 `deleting` 的删除,重新入队让它跑完。

#532。**要修的是「重试上限之后没人接手」**:host 侧 `delete-vm.sh` 非零时控制面正确地保留
`status=deleting` 让 lifecycle SQS 重试(不敢谎报 `deleted`,盘可能还在;也不敢回滚成
`running`,可能已被部分销毁)。但 `max_receive_count=5`(`deploy/stacks/lambdas.py:283`)
之后消息进 DLQ,**链路到此为止** —— 即使根因已修,租户永远停在 `deleting`,只能人工再点一次删除。

真机实例(issue 正文,`ap-southeast-1` 2026-08-18):两个租户卡住,行上是
`delete_retryable=true` + claim 已过期,主队列深度 0 / DLQ 深度 7、`ApproximateReceiveCount=6`;
根因是 S3 上当时没有 `deployment/scripts/delete-vm.sh`,host 自愈也拉不到。脚本补回后
**已进 DLQ 的消息不会自己回主队列**,两个租户继续 `deleting`。

## 本模块**不做**什么(这是设计,不是省事)

- **不新增 `status` 值。** 重投续做的准入门条件锁 `status = deleting`
  (`tenant_service.py` 的 delete claim 段),一翻新状态那条现成的重试路径就死;而 `failed`
  已被 `_ORPHAN_REAP_STATUSES`(`health_check/handler.py:1892`,孤儿容量回收)占用,其安全性
  论证明写「写出 `failed`+令牌的写者是**已知且封闭的一组**」,加一个新写者就把那条论证破了。
- **不自己扣账本、不自己释放令牌、不自己翻 `deleted`。** 只做**重新入队**,让那条完整的
  删除路径重跑一次 —— lifecycle fence、`capacity_reservation_id` 令牌、账本下溢守卫、
  `stop-vm`/`rm -rf` 的幂等全部原样生效。这同时绕开了 reaper 那条顾虑
  (`health_check/handler.py:1868-1877`:`deleting` 的 VM 可能还活着,单靠 age 证明不了
  `stop-vm` 已跑,所以不能抄近路直接回收)—— 我们不做任何近路判定。
- **不碰 DLQ。** DDB 是权威:一行只要还是 `deleting + delete_retryable`,就该被推完,
  与那条消息此刻在主队列、在 DLQ、还是已被消费掉**无关**。重复投递是安全的
  (claim TTL + 令牌条件 + 脚本幂等三重兜底)。

依赖方向合法:services → services(同 `tenant_service.py` import `lifecycle_dispatch`)
+ services → core。
"""

import time

import core.clients as clients
import core.ddb_scan as ddb_scan
import core.utils as utils
import services.lifecycle_dispatch as lifecycle_dispatch

# 重投上限。超过即转 `delete_redrive_exhausted` 并停手 —— 一个反复失败的删除是运维问题,
# 不是该无限重投的东西(无限重投会把一条真实故障磨成永久静默的背景噪音,而且每一轮都真发
# 一次 SSM)。10 次足够覆盖一次「发现根因 → 修 S3/host → 生效」的运维往返,再长该由人介入。
DELETE_REDRIVE_MAX_ATTEMPTS = 10


def _scan_stuck_deletes(now_epoch):
    """扫出「卡在 deleting、标记可重试、claim 已过期、且还没判 exhausted」的租户行。

    判据逐字取自 issue 的 Required behavior(「对 `deleting + delete_retryable=true +
    claim expired` 提供有界、幂等的自动收敛机制」),不擅自放宽:

      · `delete_retryable = true` —— 每条失败路径返 502 前都会 `_mark_delete_retryable()`
        (`tenant_service.py` 共 8 处失败点),所以一个真失败的删除**一定**带这个标记。
      · `claim 已过期` —— 没过期说明另一个执行者还持着这次删除的所有权
        (`_DELETE_CLAIM_TTL_SECONDS`),此刻重投只会撞 CCF、白发一次 SSM。
      · `attribute_not_exists(delete_redrive_exhausted)` —— 已判上限的行退出候选集,
        这是「有界」的**机械**保证(不依赖计数器读得准)。
      · **生命周期租约必须也已过期** —— 判据复用 `lifecycle_fence.acquire` 自己看的那个字段
        (`active_lifecycle_until <= now`),不另造一套。**这条是真机抓出来的**:
        claim TTL 是 900s(`_DELETE_CLAIM_TTL_SECONDS`),而租约 TTL 是 1800s
        (`lifecycle_fence.DEFAULT_LEASE_SECONDS`);delete 返 502 时 wrapper 刻意**扣住租约
        不释放**(#469:502 可能意味着有在途 SSM,放掉租约会让延迟落地的命令撞上新执行者)。
        于是「claim 过期」比「租约过期」早整整 900 秒 —— 只盯 claim 就会在租约仍被持有时重投,
        而 `_delete_tenant_inner` 的第一步 `acquire` 会返 **409 `LIFECYCLE_IN_FLIGHT`**,
        409 是 4xx ⇒ consumer **明确不重投** ⇒ 消息被吃掉、这次重投配额白烧、租户仍卡着。
        实测(us-west-2,2026-08-23):claim 1787504493 过期 / 租约 1787505393 才过期,
        在 1787504495 重投 → 生产 consumer 57ms 返回、一个字都没写、DLQ 空。
        这与 #469 P2 记的「409 → 消息消费掉、删除永不发生」是同一条形态。

    **为什么不顺手把 `delete_retryable=false + claim 已过期` 也捞进来**:那种行是执行者在跑到
    `_mark_delete_retryable()` 之前就被硬杀(超时/OOM)。但它的 SQS 消息**没有被 ack**,会被
    重投,而准入门接受「retryable 为真 **或** claim 已过期」,所以它自己会收敛;真走到进 DLQ
    那一步时,中间每一轮失败都已把标记写回 `true`。故本判据对 DLQ 场景是完备的,放宽只增加误伤面。

    `scan_all` 而不是裸 `scan`:#432 —— DDB 单次 Scan 上限 1MB,且 `FilterExpression` 在那
    1MB 读【之后】才过滤,不翻页就是"明明有卡死的却扫不到",而且没有任何异常。
    """
    return ddb_scan.scan_all(
        clients.tenants_table,
        FilterExpression=(
            "#s = :deleting AND delete_retryable = :true "
            "AND attribute_exists(delete_claim_expires_at_epoch) "
            "AND delete_claim_expires_at_epoch <= :now "
            "AND attribute_not_exists(delete_redrive_exhausted) "
            # 租约也必须已过期(见上方说明:租约 1800s 比 claim 900s 多活一倍,
            # 在租约窗口内重投会被 acquire 返 409 → 4xx → 消息被吃掉)
            "AND (attribute_not_exists(active_lifecycle_until) "
            "OR active_lifecycle_until <= :now)"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":deleting": "deleting",
            ":true": True,
            ":now": now_epoch,
        },
    )


def _replay_identity(t):
    """给重投消息造调用者身份。

    消费侧 `_get_caller_identity`(`core/auth.py`)把 `_consumer_ident` 当**受信内部路径**
    (消息只能由本账号的 api Lambda 用 SQS 发送权限投递),据此重建 identity;而 delete 唯一的
    鉴权点是 `_assert_owner_or_admin`(`core/auth.py:462`)。

    三个取值都是读过 `core/auth.py:462-503` 之后定的:

      · `is_admin=True` —— 本 reconciler 是平台内部执行者,推进的是一次**已经被授权过**的
        删除(那行能翻成 `deleting`,正说明它当初通过了同一道 `_assert_owner_or_admin`)。
        为什么不用最小权限(`is_admin=False` + 行上的 owner_id):`:466-468` 明写「没有
        `owner_id` 的行(legacy / API-key 建的)只有 admin 能动」,而 `:497-501` 对 `owner`
        为空的行直接返 403。403 是 4xx ⇒ 消费侧**明确不重投** ⇒ 消息被吃掉 ⇒ 那类租户
        **永远收敛不了**,正是本 issue 要消灭的形态(与 #469 P2 踩过的「force 没透传 →
        409 → 删除永不发生」同一条)。
      · `owner_id` 仍取行上的值 —— 保住审计归属,不把动作记成平台自己发起的。
      · `platform_scope=None` —— **刻意不设**。`:487-492` 的 scope 检查排在 `is_admin`
        **之前**,设错一个 scope 就是 403 + 静默不收敛;而 reconciler 不是被限定命名空间的
        API key,它只推进「按 id 找到的、当初已通过 scope 检查」的那一行,不会因此多碰到
        任何别的平台的租户。
    """
    return {
        "_consumer_ident": {
            "owner_id": t.get("owner_id"),
            "is_admin": True,
            "platform_scope": None,
            "tenant_user_id": None,
        }
    }


def _count_attempt(tenant_id, attempts):
    """在**入队成功之后**记一次重投,到上限则转 exhausted。返回 `(counted, reached)`。

    ⚠️ **顺序是 enqueue → count,不是反过来**(Codex 独立复审 blocker-6)。
    第一版写的是先计数再入队,理由是"入队成功而计数失败会让上限失守;计数成功而入队失败只是
    白烧一次配额,方向安全"。那个理由只对**单次**成立:累积起来,**10 次 SQS 瞬时失败就会把
    `delete_redrive_exhausted` 写上,而一次真正的重投都没发生过** —— 该租户从此永远不再被
    自动收敛。一次基础设施抖动换来永久失去恢复能力,这是 liveness 缺陷,不是"方向安全"。

    改成入队成功后才计数,而"上限失守"这个担心在这条路径上**不成立**:
      · 入队成功 ⇒ 消费者会 claim 并把 `delete_retryable` 翻 false ⇒ 该行**离开候选集**,
        本对账器扫不到它,不可能空转;它只在删除**又失败一次**时才回到候选集,而那次失败
        本身就该被计一次。
      · 真正"计数写失败"的情形(DDB 抖动/CCF)顶多让计数少涨一次 ⇒ 上限**软失守一次**,
        代价是多重投一轮;而反向的代价是永久放弃一个租户。两害相权很清楚。

    `ADD` 而不是 `SET attempts + 1`:多 invocation 并发时 ADD 是原子累加,读-改-写会丢计数。

    条件三条:
      · `#s = :deleting` —— 只动仍在中间态的行。
      · `delete_retryable = :true` —— 扫描到写之间它若已被别人推进(claim 到手 →
        `delete_retryable` 翻 false),CCF 出局,不给一个正在被处理的删除凭空计数。
      · `attribute_not_exists(delete_redrive_exhausted)` —— 已判上限就不再累加,否则两个
        并发 invocation 各自 ADD 会让计数越过上限,把"重投了多少次"这个运维判据搞失真。
    """
    reached = attempts + 1 >= DELETE_REDRIVE_MAX_ATTEMPTS
    expr = "ADD delete_redrive_attempts :one"
    vals = {":one": 1, ":true": True, ":deleting": "deleting"}
    if reached:
        expr += " SET delete_redrive_exhausted = :true, updated_at = :t"
        vals[":t"] = utils._now()
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=expr,
            ConditionExpression=(
                "#s = :deleting AND delete_retryable = :true "
                "AND attribute_not_exists(delete_redrive_exhausted)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=vals,
        )
    except Exception as e:  # noqa: BLE001 — CCF(别人已推进/已判上限)也走这里
        print(f"delete-reconciler: count attempt {tenant_id} skipped ({e})")
        return False, False
    if reached:
        # fail-loud:停手这件事必须看得见,否则它与"已经修好了"在指标上长得一样。
        print(
            f"delete-reconciler: {tenant_id} EXHAUSTED after {attempts + 1} redrives "
            f"— it is still in `deleting`; inspect delete_fail_reason, fix the root "
            f"cause, then clear delete_redrive_exhausted to resume"
        )
    return True, reached


def reconcile_deletes():
    """一拍对账。返回统计 dict(供日志/指标),**任何单个租户的失败都不中断整轮**。

    幂等:每个写都条件锁 `status=deleting AND delete_retryable=true`,所以重复投递 / 并发
    invocation 最多让第二个 CCF 出局;而重复**入队**本身也是安全的 —— 删除路径有 claim TTL、
    令牌条件与脚本幂等三重兜底(重复投递的执行安全见 `lifecycle_dispatch.enqueue_lifecycle`
    的说明)。
    """
    stats = {
        "scanned": 0,
        "redriven": 0,
        "exhausted": 0,
        "skipped": 0,
        "errors": 0,
    }
    # 队列没配 ⇒ 根本没有异步路径可重投(同步路径下 delete 直接跑完,不存在本 issue 的形态)。
    # 早返回而不是扫一遍空转:省一次全表 Scan,且日志里能看出是"没开队列"而不是"没有卡死的"。
    if not clients.LIFECYCLE_QUEUE_URL:
        print("delete-reconciler: LIFECYCLE_QUEUE_URL unset — nothing to redrive")
        stats["skipped"] = -1
        return stats
    now_epoch = int(time.time())
    try:
        pending = _scan_stuck_deletes(now_epoch)
    except Exception as e:  # noqa: BLE001 — 扫不到不该让整个 Lambda 失败
        print(f"delete-reconciler: scan failed ({e})")
        stats["errors"] += 1
        return stats
    stats["scanned"] = len(pending)
    for t in pending:
        tid = t.get("id")
        if not tid:
            continue
        try:
            attempts = int(t.get("delete_redrive_attempts", 0) or 0)
            # 上限先**读**着判,但不在这里写 —— 写在入队成功之后(见 _count_attempt 的说明:
            # 先写会让 10 次 SQS 瞬时失败把一个租户永久判死)。这里只做"已经到上限就别再投"。
            if attempts >= DELETE_REDRIVE_MAX_ATTEMPTS:
                # 正常不会走到:到上限那次已写 delete_redrive_exhausted,扫描判据会把它排除。
                # 留这一支是防"标记写失败但计数已到"的边界,fail-loud 不静默重投。
                print(
                    f"delete-reconciler: {tid} already at cap "
                    f"({attempts}/{DELETE_REDRIVE_MAX_ATTEMPTS}) without the exhausted "
                    f"marker — not redriving; clear the counter after fixing the cause"
                )
                stats["exhausted"] += 1
                continue
            # 意图**必须**取自库里(#532 的另一半):`keep_data`/`skip_backup`/`force` 只活在
            # 原始 query 或原消息的 extra 里,消息进 DLQ 后就只剩 DDB 这一份。缺了它,
            # 一次 `?keep_data=false` 的硬删会被重投成软删 → 该销毁的盘留下来 → 磁盘泄漏。
            intent = dict(t.get("delete_intent") or {})
            op_id = lifecycle_dispatch.enqueue_lifecycle(
                "delete", tid, _replay_identity(t), extra=intent
            )
            if not op_id:
                # enqueue 返 False 只在队列未配时发生,而上面已早返回;真到这里说明配置
                # 在本轮中途变了 —— 不静默,记一条。**不计数**(见下)。
                print(f"delete-reconciler: {tid} enqueue declined (queue unset?)")
                stats["skipped"] += 1
                continue
            # 入队**确实成功**之后才记账。enqueue 抛异常时这行不会执行 ⇒ 一次 SQS 抖动
            # 不消耗配额,租户不会因基础设施问题被永久判死(Codex 独立复审 blocker-6)。
            counted, reached = _count_attempt(tid, attempts)
            if reached:
                stats["exhausted"] += 1
            stats["redriven"] += 1
            print(
                f"delete-reconciler: redrove delete for {tid} op_id={op_id} "
                f"attempt={attempts + 1}/{DELETE_REDRIVE_MAX_ATTEMPTS} "
                f"counted={counted} intent={intent or '{}(defaults)'}"
            )
        except Exception as e:  # noqa: BLE001 — 单个租户失败不拖累整轮
            # 入队抛异常也落这里:**没有计数**,下一拍会重试。这正是要的行为。
            print(f"delete-reconciler: {tid} failed ({e})")
            stats["errors"] += 1
    if stats["scanned"]:
        print(f"delete-reconciler: {stats}")
    return stats
