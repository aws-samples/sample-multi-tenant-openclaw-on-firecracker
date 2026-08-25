# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deadline_executor — #562 G6:独立于 SQS 消费者的死线执行者。

## 它存在的理由(不是「再加一层兜底」)

客户的契约是「即使失败也必须是终态」。终态承诺【最需要被兑现的时刻,恰恰是消费者出故障的
时刻】—— 消费者一挂,靠消费者判死的那条路也一起没了,租户就永久停在非终态。所以这条路必须
不经过 SQS 消费者。

现状的两个真实缺口(核过代码,不是推测):
  1. `pending` 状态【全仓没有任何超时】。`health_check._reap_stuck_creating` 只扫
     `status=creating`;`scaler/handler.py:374` 反而把 `{"pending","creating","queued"}`
     当「在途需求」—— 一条卡死的 pending 会永久推高扩容需求。没有任何代码让它变终态。
  2. `creating` 的兜底是 `health_check.CREATING_TIMEOUT_SECONDS = 900`(15 分钟),
     且那个 Lambda 的 EventBridge 节拍是 `rate(5 minutes)`(线上实测,见下)。对 180s 死线
     慢两个数量级 —— 这正是客户看到「15 分钟还在创建中」的机制来源之一。

## 为什么挂在 dispatch.poller 的 rate(1 minute) 上

线上 apse1 实测(`aws events list-rules --region ap-southeast-1`,2026-08-21):

    OpenClawOrchestrator-DispatchDispatchPollerRule…   rate(1 minute)   ENABLED
    OpenClawOrchestrator-HealthCheckSchedule…          rate(5 minutes)  ENABLED
    OpenClawOrchestrator-ScalerSchedule…               rate(3 minutes)  ENABLED
    OpenClawOrchestrator-BackupSchedule…               rate(30 minutes) ENABLED

1 分钟是现有节拍里唯一能和 180s 死线同量级的。健康检查那条 5 分钟【不够】,且它所在的
reaper 是按 900s 固定超时设计的,改它等于改另一个 Lambda 的语义。新建 Lambda 被 issue 明确
列为单独一个 MR,本轮不混做(铁律 #2)。

**为什么不塞进 `poll_inflight` 内部**(这是本模块最要紧的一个位置决策):那个函数在
`DISPATCH_MODE=ddb` 下第一行就空转返回(#315:ddb 下 poller 动租户状态/容量会错扣被并发
命令占用的槽位)。三个事实要分清:`clients.py` 的代码默认是 `push`;**客户生产样例
`samples/config-sg-prod.yaml:185` 是 `ddb`**;我们这套 apse1 部署实测是 `push`
(2026-08-21 查 Lambda env)。也就是说塞进去在【客户生产形态下永不执行】,而在我们的测试
环境里却照常跑 —— 这是最坏的一类缺陷:自测全绿、客户侧静默失效。故与它并列,不嵌套。

**如实说明精度边界**:1 分钟节拍下,本执行者发现过期最坏比死线晚 60s(死线 T+180 → 最坏
T+240 才判死)。所以 **G1 的「180s 内全部终态」不靠本模块兑现**,靠的是消费侧的同步判定
(`dispatch_service` 消费前过期丢弃 + 「注定超不过」判死),那条路在毫秒级。本模块是
【消费者不工作时】的兑现者,量级是「分钟内必然终态」而不是「180s 内」。想把兜底也做到秒级
精确,需要一条 180s DelaySeconds 的标准队列(SQS 单消息延迟上限 900s,180s 在范围内)——
那是新基础设施,归 issue 里那个独立 MR。

## 为什么只翻状态、不在这里释放容量

释放 `creating` 租户的容量必须先拿到 host 侧权威停机确认(#412 blocker-B:`_confirm_vm_stopped`)
—— 否则会扣掉一个仍在跑的 VM 的账本,那是超卖红线。那个探针在 health_check Lambda 里。
本模块【只做围栏】:原子把 `creating`→`failed` 并【保留】`capacity_reservation_id`,剩下的
`stop-confirm → 释放`交给已存在的 `health_check._reap_orphan_reservations`(它的
`_ORPHAN_REAP_STATUSES` 已含 `"failed"`,正是为围栏步准备的)。

这不是偷懒,是刻意:把 host 权威探针复制进第二个 Lambda,就有了两个释放者 —— 双扣账本或
停掉活 VM 的两种事故都从那里来。代价是容量释放最坏晚 5 分钟(health_check 节拍),而【状态
终态性不受影响】,业务立刻能看到 failed 并重试。

## 扫描成本(写在这里防止后人「顺手优化」)

`openclaw-tenants` 线上实测:PAY_PER_REQUEST、6790 项、2.83 MB。全表 Scan 一次约 2.83MB/4KB
≈ 345 RRU(最终一致),1 分钟一次 ≈ 50 万 RRU/天,量级是每天几分钱。**不值得为它建 GSI**
(该表现在只有 `gsi_owner`)。若将来租户表涨到几十万行,再按 `status` 建稀疏 GSI。
FilterExpression 是服务端【读完之后】过滤,所以必须翻页(与 `_registered_host_count` 同款纪律)。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

import core.clients as clients
import core.create_deadline as create_deadline
import core.scheduling as scheduling

# 扫描面:**状态 → 它属于哪个操作**。死线字段名与失败原因字段名都是 per-action 的
# (`deadline_attr()` / `fail_reason_attr()`),所以扫描不能只知道状态、必须知道动作。
#
# create 的两个:核过 tenant_service 的三条创建路径 —— 队列占位写 "creating"、pending 路径
# 写 "pending"、同步路径写 "creating"。响应体里的 "queued"【不是】行状态(那是
# `utils._resp(202,…)` 的字面量),所以不扫它 —— 扫了会永远是空集合,是一种看起来在干活的假绿。
#
# #564 G4 新增三个:`suspending` / `restoring` / `deleting`。
# **restart 不在这里,而且不是漏了**:restart 没有中间态(它只经 SSM 下发 stop+start,
# 行上的 status 不变),所以没有"卡在中间态"这回事可判。它的 `restart_deadline` 由 **G3
# 的消费前检查**兑现(过期的 restart 不下发),而不是由状态围栏 —— 两条门各管一段,不重叠。
# backup 同理:手动备份的状态字段归 G7 建,建好后再进这张表。
#
# rebuild 也不在这里:它的在飞态**不是 status 值**,是 `rebuild_phase ∈
# {queued,running,verifying}`(`tenant_service._REBUILD_INFLIGHT_PHASES`),
# (scaler 的 `_REFRESH_SKIP_STATUS`、`gsi_status` 查询、客户已在用的契约字段),
# 为 rebuild 加一个 status 枚举值要同步改每一处,漏一处就是静默错误。
_STATUS_ACTION: Dict[str, str] = {
    "creating": create_deadline.ACTION_CREATE,
    "pending": create_deadline.ACTION_CREATE,
    "suspending": create_deadline.ACTION_SUSPEND,
    "restoring": create_deadline.ACTION_RESTORE,
    "deleting": create_deadline.ACTION_DELETE,
}

_ENFORCE_STATUSES: Tuple[str, ...] = tuple(_STATUS_ACTION)
"""保留这个名字:既有测试与注释都引它,改名只会制造无意义的 diff。"""

# rebuild 的在飞相位 —— 与 `tenant_service._REBUILD_INFLIGHT_PHASES` **必须一致**。
# 这里不 import tenant_service:那会让 `deadline_executor`(被 poller 调)依赖整个租户
# 编排模块,把冷启动和依赖图都拖坏。一致性由 `tests/test_564_g2g3g4_deadline_chain.py`
# 的一条断言机械保证(两处集合逐值比对),而不是靠"记得同步改"。
_REBUILD_INFLIGHT_PHASES: Tuple[str, ...] = ("queued", "running", "verifying")

# rebuild 判死时写进 `rebuild_phase` / `rebuild_status` 的终态值 —— 同样与
# tenant_service 的 `_REBUILD_STATUS_FAILED` 对齐,由同一条断言钉住。
_REBUILD_STATUS_FAILED = "failed"

# **没有任何自己的状态字段**的那一档:`restart` 只经 SSM 下发 stop+start,不把租户行的
# `status` 翻进任何中间值,也没有 `restart_phase` 之类的字段。所以到点时它的 `status` 是
# `running` 这类**健康值**,按常规围栏翻成 `failed` 就是**把健康租户标成失败**
# (Codex 独立复审第 1 轮抓出)。它只记录"这次尝试失败"(`<action>_fail_reason` +
# `<action>_fail_at`),不动 status。
#
# `backup` 在 #564 G7 之前也在这一列(它压根没有状态字段);G7 给了它 `backup_phase`,
# 所以它移到下面 `_PHASE_ACTIONS` 那张表里 —— 有自己的状态字段就该翻自己那个,而不是
# 只留一条原因。
_NO_INTERMEDIATE_STATUS = (
    create_deadline.ACTION_RESTART,
    # #604 —— `start` 与 restart 同类,**必须一起在这里**。它在执行期间不动 `status`
    # (真机实测:发 start 之后租户行仍是 `stopped`,成功才翻 running),所以它没有"卡住的
    # 中间态"可翻。漏掉它的后果是实的:走下面常规分支就会把一个**健康的 stopped 租户**
    # 在死线到点时标成 `failed` —— 那比不判死更糟,客户看到的是一个凭空坏掉的租户,而
    # `status` 有四个消费方(scaler 的跳过集合、`gsi_status` 查询、客户契约字段)会一起
    # 跟着误判。
    create_deadline.ACTION_START,
)

# **有自己的"相位"状态字段、但不进 `status` 中间态**的两档。表的形态:
#   动作 → (当做 CAS 锚的字段, 判死时要翻成 failed 的字段们)
#
# 为什么不进 `status`:`status` 有四个消费方(scaler 的 `_REFRESH_SKIP_STATUS`、`gsi_status`
# 查询键、客户已在用的契约字段),为它们各加一个枚举值要同步改每一处,漏一处就是静默错误
#
# **`backup` 只翻 `backup_phase`,绝不翻 `status`**:一次备份失败不代表这个租户失败 ——
# 它可能正在正常服务。把 `status` 翻成 `failed` 会让 scaler 与控制台把一个健康租户当故障。
# 表的形态:动作 → (相位字段, 判死时要翻成 failed 的字段们, **世代字段**)
#
# **世代字段(`<action>_op_id`)是必须的第三项**(Codex 独立复审第 1 轮抓出):相位 + 死线
# 这两个锚**分不开同一个租户的两次同类操作** —— 同一秒受理 + 同一档死线秒数,两次操作的
# `<action>_deadline` 会取到**同一个值**,相位也可能同为 `running`。这时一条陈旧的扫描/重投
# 就能把一次**全新的**操作判死。rebuild 那档我在 G3 里已经为此加了 op_id 校验,但**没有推广
# 到 backup** —— 那是我自己留下的不一致。
_PHASE_ACTIONS = {
    create_deadline.ACTION_REBUILD: (
        "rebuild_phase",
        ("rebuild_phase", "rebuild_status"),
        "rebuild_op_id",
    ),
    create_deadline.ACTION_BACKUP: (
        "backup_phase",
        ("backup_phase",),
        "backup_op_id",
    ),
}

# 备份的**在飞**相位 —— 与 `tenant_service._BACKUP_INFLIGHT_PHASES` 必须一致,
# 由 `tests/test_564_g6g7_dlq_backup.py` 的一条断言逐值比对(同 rebuild 那条的理由:
# 这个模块刻意不 import tenant_service,代价是两份定义)。
_BACKUP_INFLIGHT_PHASES: Tuple[str, ...] = ("queued", "running")

# 单次调用最多判死多少个。为什么要有上限:1800 个同时过期(压测的量级)会让一次 poller 调用
# 打 1800 次条件写。EventBridge 是 1 分钟一拍,超时/被 throttle 反而让【一个都没判死】。
# 分轮做:本轮打满上限,余量下一分钟继续。上限之外的剩余【必须打日志】—— 静默截断会让
# 「本轮判死 500 个」被读成「一共只有 500 个」。
MAX_ENFORCE_PER_RUN = 500

# 一次调用最多把 ASG desired 抬多少台(G14/G15)。防的是「一次判死一大批 → 一次要 60 台」
# 把机队直接顶到 MaxSize。分轮抬:每分钟最多这么多,给上一轮的机器留出被观测到的时间
# (host 冷启实测 166s golden AMI / 186s plain AMI,见 deploy/packer/CUSTOMER-GUIDE.md:30-33
# —— 即两到三拍;期间 in_flight>0 会自然抑制后续抬升)。
MAX_SCALE_OUT_PER_RUN = 4


def _scan_expired(now_epoch: int, limit: int) -> Tuple[List[Dict[str, Any]], int]:
    """翻页扫出「状态非终态 且 已过死线」的租户行。返回 (取到的行, 被上限截断掉的条数)。

    服务端 filter 三个条件都下推:状态、死线字段存在、死线 < now。
    `attribute_exists` 那条不能省 —— 升级期存量行没有死线字段,`create_deadline < :now`
    对缺字段的行不成立(DDB 比较缺失属性恒 false),但显式写出来才说得清意图:
    **没有死线的行不归本模块管**(它们由 health_check 的 900s reaper 兜,行为不变)。
    """
    found: List[Dict[str, Any]] = []
    truncated = 0

    def _drain(kwargs: Dict[str, Any], action: str) -> None:
        """翻完一条 scan 的所有页,把行塞进 found 并打上它属于哪个操作。"""
        nonlocal truncated
        lek = None
        while True:
            if lek:
                kwargs["ExclusiveStartKey"] = lek
            page = clients.tenants_table.scan(**kwargs)
            for item in page.get("Items", []):
                if len(found) >= limit:
                    truncated += 1
                    continue
                # 归属【随行携带】而不是让下游再从 status 反查:rebuild 那条分支的行
                # status 可能是任何活跃值,反查会算错动作,于是死线字段名和失败原因字段名
                # 全都写到错的列上 —— 那种错静默且难查。
                item["_dl_action"] = action
                found.append(item)
            lek = page.get("LastEvaluatedKey")
            if not lek:
                break

    for status, action in _STATUS_ACTION.items():
        attr = create_deadline.deadline_attr(action)
        _drain(
            {
                "FilterExpression": (
                    "#s = :st "
                    f"AND attribute_exists({attr}) "
                    f"AND {attr} < :now "
                    "AND (attribute_not_exists(synthetic) OR synthetic <> :true)"
                ),
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":st": status,
                    ":now": now_epoch,
                    ":true": True,
                },
                "ProjectionExpression": (
                    "id, #s, host_id, vm_num, capacity_reservation_id, "
                    f"vcpu, mem_mb, {attr}"
                ),
            },
            action,
        )

    # rebuild 与 backup:在飞态是自己的相位字段,不是 status(见 `_PHASE_ACTIONS` 的说明)。
    # 每个相位各扫一次而不是用 IN:DDB 的 FilterExpression 支持 IN,但多次单值比较的读成本
    # 与一次 IN 相同(都是全表 Scan 后过滤),而分开写让「哪个相位卡了多少」在日志里可分辨
    # —— 那是排查卡死时第一个要看的东西。
    #
    # **backup 这一档是 #564 G7 建的**:在它之前手动备份连状态字段都没有,所以 G2 写下的
    # `backup_deadline` 没有任何扫描者 —— 那就是"只建桥墩不铺桥面"。补上这条分支之后,
    # 一次卡死的手动备份会在 600s 后被围成 `backup_phase=failed` 并落封闭取值的原因。
    for _act, _phases in (
        (create_deadline.ACTION_REBUILD, _REBUILD_INFLIGHT_PHASES),
        (create_deadline.ACTION_BACKUP, _BACKUP_INFLIGHT_PHASES),
    ):
        _attr = create_deadline.deadline_attr(_act)
        # 三项都从表里取,**不在这里另拼字符串** —— 拼出来的名字和表里的名字漂开,
        # 结果是 scan 投影了一个不存在的列、CAS 的世代锚拿到 None、判死整档静默失灵。
        _phase_field, _, _op_field = _PHASE_ACTIONS[_act]
        for phase in _phases:
            _drain(
                {
                    "FilterExpression": (
                        f"{_phase_field} = :ph "
                        f"AND attribute_exists({_attr}) "
                        f"AND {_attr} < :now "
                        "AND (attribute_not_exists(synthetic) OR synthetic <> :true)"
                    ),
                    "ExpressionAttributeValues": {
                        ":ph": phase,
                        ":now": now_epoch,
                        ":true": True,
                    },
                    "ProjectionExpression": (
                        "id, #s, host_id, vm_num, capacity_reservation_id, "
                        f"vcpu, mem_mb, {_phase_field}, {_op_field}, {_attr}"
                    ),
                    # `status` 是 DDB 保留字,即使只出现在 ProjectionExpression 里也要走别名。
                    "ExpressionAttributeNames": {"#s": "status"},
                },
                _act,
            )
    return found, truncated


def _reason_for(item: Dict[str, Any]) -> str:
    """归因(G4)—— 【唯一】判定点,不许有第二处。

    行上有 host_id 说明已经真的发起过创建(装箱 CAS 成功、SSM 发过),那不是「没等到容量」
    而是「发起了但没在预算内跑完」。两类的处置不同(后者要回收已经起出来的资源,而且【不该
    触发扩容】—— 容量本来就给到了),混成一类会让压测的「100% 容量归因」变成一句没有信息量
    的话,还会让每次 host 侧执行故障都白多扩一台。

    为什么单独抽出来:这段判定原先在写 DDB 的地方和统计的地方各有一份。变异测试 M7 撞出了
    后果 —— 改了写入侧、统计侧还是老逻辑,于是「DDB 里记的原因」和「指标里报的原因」会不一致,
    而两边都各自"对"。归因只许有一个判定点。
    """
    action = item.get("_dl_action") or create_deadline.ACTION_CREATE
    if action != create_deadline.ACTION_CREATE:
        # #564 G4 —— 另外几档到点一律 `deadline_exceeded_in_flight`。
        #
        # **不能照搬 create 的 host_id 判据**:那条判的是「装箱成功了没」,只在创建流程里
        # 有意义。suspend/restore/delete/rebuild 的对象是一个**已经落在 host 上**的租户,
        # 行上必然有 host_id,照搬的话它们会全部被归成 `capacity_unavailable` ——
        # 而那个值压根不在 suspend/delete/rebuild 的取值子集里(`REASONS_FOR` 实测:
        # 只有 create 与 restore 含它),`assert_reason_valid` 会当场炸;更糟的是若断言
        # 被绕过,压测的「容量归因」统计会被这些与容量无关的失败灌满。
        return create_deadline.REASON_DEADLINE_EXCEEDED
    return (
        create_deadline.REASON_DEADLINE_EXCEEDED
        if item.get("host_id")
        else create_deadline.REASON_CAPACITY
    )


def _fence_failed(
    item: Dict[str, Any], now_epoch: int, reason: Optional[str] = None
) -> Tuple[str, str]:
    """把一行原子翻成终态 `failed`,【保留】容量令牌与放置字段(围栏语义)。

    返回 (结果, 实际写下的归因),结果 ∈ {"fenced","raced","error"}。返回归因而不是让调用方
    再判一次 —— 见 `_reason_for` 的说明。

    条件双锚 `status = :expected AND create_deadline = :dl`:
      · 锚 status —— 与 `dispatch_poller._promote`(条件锁 `status=creating`)互斥。它赢了
        说明 VM 真起来了,本次判死必须让路,【绝不能把一个刚 running 的租户标 failed】。
      · 锚 create_deadline —— 死线是受理时算定、此后只读的值。它变了说明这行已不是我扫到的
        那一行(理论上不会发生,但 CAS 的成本几乎为零,而误判的代价是错杀一个活租户)。

    为什么保留令牌而不是顺手清掉:清了 `_reap_orphan_reservations` 就再也找不到这份容量,
    host 账本上那一格永久搁浅(#412 反复处置过的同一个坑)。

    #564 G4 —— 三种写法,按动作分:

    四种形态都写 `<action>_fail_reason` + `<action>_fail_at`(**同一次写入**),区别只在
    「状态怎么翻」:

    | 动作 | 除原因与时刻之外还写什么 | 为什么 |
    | --- | --- | --- |
    | create/suspend/restore | `status=failed` | 常规围栏,中间态在 status 上 |
    | restart/backup | **什么都不写** | 没有中间态,没有卡住的状态要翻(见 `_NO_INTERMEDIATE_STATUS`) |
    | **delete** | `delete_reported_failed_at`,**不动 status** | 见下 |
    | **rebuild** | `rebuild_phase`/`rebuild_status=failed` | 在飞态不在 status 上 |

    **原因与时刻必须同一次写入**(契约文档 §4 把这条列为保证):否则轮询方会读到「有原因
    没时刻」的中间态,分不清这个原因是不是自己那次请求留下的。这条 2026-08-24 真机取证时
    被发现是假的 —— 常规档与 delete 档当时只写原因不写时刻,而 restart/backup/rebuild 档
    写了,五种形态里三种守约两种不守。补齐是加法,`create_fail_at` 因此首次出现(那个名字
    此前无消费者,见 `create_deadline.fail_at_attr` 的说明)。

    **delete 的例外是客户方定的,不是实现便利**:客户 2026-08-21 —— 600s 只约束**给上层的
    答复**,而**删除不得丢弃**。把 `deleting` 翻成 `failed` 会让删除链条上正在跑的
    backup/stop-vm 失去它的状态锚(那些 CAS 都以 `deleting` 为条件),等于**中止一次已经
    在进行的删除**,盘和 VM 就地搁浅。所以到点只**回报**失败:客户读到
    `status=deleting` + `delete_reported_failed_at` 有值 → 知道"我方已认失败但仍在努力,
    别用同名重建"。这满足了「失败必须终态且可归因」里的可归因,而不牺牲不得丢弃。
    """
    tid = item["id"]
    status = item.get("status")
    action = item.get("_dl_action") or create_deadline.ACTION_CREATE
    dl_attr = create_deadline.deadline_attr(action)
    reason_attr = create_deadline.fail_reason_attr(action)
    deadline = item.get(dl_attr)
    # #564 G6 —— `reason` 可由调用方给定,因为**触发不止一种**:
    #   · 死线到点(本模块的扫描 + G3 的消费前检查)→ 归因由 `_reason_for` 判;
    #   · **SQS 投递预算耗尽**(下一次失败消息就进 DLQ)→ 归因是 `system_error`,
    #     由调用方给出。这不违反 `_reason_for` 那条「唯一判定点」——那条防的是
    #     **同一个触发**被两处各判一次;不同触发本来就该有不同归因,而拿
    #     `deadline_exceeded_in_flight` 去描述一次"死线还没到但投递用完了"的失败
    #     是**撒谎**,契约文档明写那个值的含义是"已发起真实执行但没在预算内跑完"。
    reason = reason if reason is not None else _reason_for(item)
    # 取值必须先过契约断言再写库 —— 与 `tenant_service._mark_fail_reason` 同一条理由:
    # 取值集合是对外契约,一个越界的值漏出去就收不回来,所以在开发期炸。
    create_deadline.assert_reason_valid(action, reason)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch))

    if action in _NO_INTERMEDIATE_STATUS:
        # #564 G4(Codex 独立复审第 1 轮抓出的真缺陷)—— `restart` / `backup` **没有中间态**,
        # 到点时行上的 `status` 是 `running` 这类**健康值**。走下面那条常规分支会把它翻成
        # `failed`,即**把一个健康租户标成失败**;而契约文档 §3.1b 明写 restart「没有中间态,
        # 所以没有卡住的状态要翻」—— 那条通用围栏让文档变成了谎话。
        # 正确做法:只记录这次尝试失败(原因 + 时刻,两个字段 #565 G3 已经建好),不动 status。
        update = (
            f"SET {reason_attr} = :reason, "
            f"{create_deadline.fail_at_attr(action)} = :iso, "
            "deadline_enforced_at = :now"
        )
        # 只锚死线,不锚 status:这两档的 status 与本次操作无关(它压根不进中间态),
        # 拿它当条件只会在租户正常启停时随机 CCF。死线锚已经足够 —— 它是受理时算定、
        # 此后只读的值,变了就说明这行已不是我判过期的那一次操作。
        condition = f"{dl_attr} = :dl"
        names = None
    elif action == create_deadline.ACTION_DELETE:
        update = (
            f"SET {reason_attr} = :reason, "
            f"{create_deadline.fail_at_attr(action)} = :iso, "
            "delete_reported_failed_at = :now, deadline_enforced_at = :now"
        )
        # 幂等锚 `attribute_not_exists(delete_reported_failed_at)`:同一次删除在多轮里
        # 只回报一次。少了它,每一拍都会重写时间戳,轮询方分不清"刚刚又失败了一次"和
        # "还是那次失败" —— 而删除本来就允许长时间继续,重写会把它读成反复失败。
        condition = (
            f"#s = :expected AND {dl_attr} = :dl "
            "AND attribute_not_exists(delete_reported_failed_at)"
        )
        names = {"#s": "status"}
    elif action in _PHASE_ACTIONS:
        _anchor_field, _flip_fields, _op_field = _PHASE_ACTIONS[action]
        _sets = ", ".join(f"{f} = :failed" for f in _flip_fields)
        update = (
            f"SET {_sets}, {reason_attr} = :reason, "
            f"{create_deadline.fail_at_attr(action)} = :iso, "
            "deadline_enforced_at = :now"
        )
        # 三道锚:相位 + 死线 + **世代**。
        # 锚在自己的相位字段上而不是 status:这两档不进 status 中间态,拿 status 当条件
        # 既锚不住(它期间可能被别的写者合法改动)又会把本该判死的行漏掉。
        # **世代锚(`<action>_op_id`)不可省**:相位与死线分不开同一租户的两次同类操作
        # (同秒受理 + 同档秒数 → 死线同值,相位也可能同为 running),那时一条陈旧的扫描
        # 就能判死一次全新的操作。见 `_PHASE_ACTIONS` 的说明。
        condition = (
            f"{_anchor_field} = :expected AND {dl_attr} = :dl AND {_op_field} = :op"
        )
        names = None
    else:
        update = (
            f"SET #s = :failed, {reason_attr} = :reason, "
            f"{create_deadline.fail_at_attr(action)} = :iso, "
            "updated_at = :iso, deadline_enforced_at = :now"
        )
        condition = f"#s = :expected AND {dl_attr} = :dl"
        names = {"#s": "status"}

    # #564 G6 —— 行上**没有**死线字段时把死线锚摘掉。
    #
    # 为什么需要:死线到点这个触发天然保证行上有死线(不然判不出过期),但**投递预算耗尽**
    # 那个触发没有这个前提 —— 升级期队列里那批"发出时还没有死线字段"的在飞消息,行上也可能
    # 没有。留着 `{dl_attr} = :dl` 而 `:dl` 是 `None`,DDB 会拿 NULL 去比一个不存在的属性 →
    # 恒不成立 → CCF → 静默变成 `raced`,**该回写终态的那次一个字都没写**,而消息照旧进 DLQ。
    # 那正是本门要消灭的形态。
    # 摘掉之后仍有"当前值锚"(status / rebuild_phase)兜住"这行已经流转走了"的情形,
    # 只是失去了"区分同一状态下的两次不同操作"的能力 —— 对没有死线字段的过渡期行,
    # 那个能力本来就不存在。
    if deadline is None:
        condition = " AND ".join(
            p for p in condition.split(" AND ") if f"{dl_attr} = :dl" not in p
        )
        if not condition:
            # 唯一的条件就是死线锚(restart/backup 那支)→ 摘掉就没有条件了。
            # **绝不无条件写**:DDB 的 update_item 是 upsert,无条件写会凭空造出畸形租户行。
            condition = "attribute_exists(id)"

    expected = (
        item.get(_PHASE_ACTIONS[action][0])
        if action in _PHASE_ACTIONS
        else status
    )
    # 只放**表达式里真的出现过**的占位符:DDB 对未使用的 ExpressionAttributeValues 会直接
    # 报 ValidationException(`unused in expressions`),而那会让整档判死变成 `error`。
    # 上面四种形态用到的占位符各不相同(restart/backup 那支既不用 `:expected` 也不用
    # `:failed`),所以按出现与否装,不预先塞满。
    _candidates = {
        ":failed": (
            # rebuild 与 backup 的终态相位值、以及常规档的 `status=failed`,当前都是
            # 字面 "failed" —— 但保留这个分支是因为 `_REBUILD_STATUS_FAILED` 是与
            # `tenant_service._REBUILD_STATUS_FAILED` 对齐的**契约值**(有断言比对),
            # 而常规档那个 "failed" 是 `status` 的枚举值。两者恰好同字,不是同一件事。
            _REBUILD_STATUS_FAILED
            if action in _PHASE_ACTIONS
            else "failed"
        ),
        ":expected": expected,
        ":dl": deadline,
        ":reason": reason,
        ":now": now_epoch,
        ":iso": iso,
        # 世代锚的值:从**扫回来的那一行**取(scan 的 ProjectionExpression 已经带上它)。
        # 相位档才会用到,其余形态的表达式里没有 `:op`,下面按"真出现过"过滤时自动落掉。
        ":op": (
            item.get(_PHASE_ACTIONS[action][2])
            if action in _PHASE_ACTIONS
            else None
        ),
    }
    _expr = update + " " + condition
    values = {k: v for k, v in _candidates.items() if k in _expr}
    try:
        kwargs: Dict[str, Any] = {
            "Key": {"id": tid},
            "UpdateExpression": update,
            "ConditionExpression": condition,
            "ExpressionAttributeValues": values,
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        clients.tenants_table.update_item(**kwargs)
        print(
            f"[#564] deadline-enforced {tid}: action={action} from={expected} "
            f"reason={reason} deadline={deadline} now={now_epoch} "
            f"late_by={int(now_epoch) - int(deadline)}s"
            + (
                " (delete 继续进行,只回报失败)"
                if action == create_deadline.ACTION_DELETE
                else ""
            )
        )
        return "fenced", reason
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # 竞态输了(promote 赢 / 已被别的写者流转)。这是【正常结果不是错误】:
            # 说明这个租户已经有了更好的归宿,本模块无事可做。
            # delete 那档还有第二种正常成因:**已经回报过**(幂等锚
            # `attribute_not_exists(delete_reported_failed_at)` 不成立)。两种都是 raced,
            # 但日志要分得开 —— 否则运维会把"每拍都 skip 一个 deleting"读成有东西卡住了。
            print(
                f"[#564] deadline-skip {tid}: action={action} "
                + (
                    "已回报过或状态已流转 (fence CCF)"
                    if action == create_deadline.ACTION_DELETE
                    else "状态已流转 (fence CCF)"
                )
            )
            return "raced", reason
        # 其它 ClientError(throttle/冲突/网络)不吞:打出来,计进 errors,下一拍重来。
        # 不 raise —— 一个租户写失败不该让本轮剩下的都不判死。
        print(f"[#562] deadline-error {tid}: {e}")
        return "error", reason


def fence_expired_tenant(
    tenant_id: str,
    action: str,
    observed_deadline: Optional[Any] = None,
    now_epoch: Optional[int] = None,
    observed_op_id: Optional[str] = None,
) -> str:
    """#564 G3 用的入口:立刻把一个**已过期**的操作围成终态。返回 `_fence_failed` 的结果。

    **`observed_deadline` 必须传,而且必须是调用方判过期时用的那个值(消息体/payload 里的)。**
    这条是 Codex 独立复审第 1 轮抓出的真缺陷:本函数原来读回行、再让 `_fence_failed` 从
    **同一行**取"期望值"和"死线"当 CAS 锚 —— 那等于**拿这行比它自己**,条件永远成立,锚
    完全失效。后果不是理论上的:一条陈旧的重投消息(SQS 重投 / Lambda 异步重试)带着一个
    早就过期的死线到达时,租户行上可能已经是**另一次全新的、没过期的**同类操作,而这个
    失效的锚会把那次活操作直接判死。
    所以这里在读回之后先比一次:行上的死线 ≠ 我判过期时看到的那个 → 这行已经不是那次操作,
    返回 `"raced"` 并**不写任何东西**。

    为什么要它,而不是让消费者丢完消息就交给下面那个每分钟一拍的扫描:验收第 2 条要断言
    「过期消息被 ack 的同时**租户已终态**」。靠扫描的话两件事之间有最多一拍(60s)的窗口,
    那期间租户停在中间态 —— 取证时会是个随机的红/绿,而客户看到的是"操作没了、状态还卡着"。

    为什么复用 `_fence_failed` 而不是在消费者里另写一次写库:归因与写法只许有**一个**判定点
    (`_reason_for` 的 M7 教训:写入侧和统计侧各有一份,改了一处另一处还是老逻辑,而两边
    各自"对")。delete 的例外、rebuild 的相位锚、契约断言,全都在那一个函数里,这里只负责
    把行读回来。

    读不到行 / 行已流转 → 返回 `"raced"`,**不抛**:消费者在这之后还要 ack 消息,
    一次 DDB 抖动不该让一条已经判过期的消息重新进队列绕圈。
    """
    return _fence_row(
        tenant_id, action, observed_deadline, now_epoch, observed_op_id, None
    )


def fence_delivery_exhausted(
    tenant_id: str,
    action: str,
    observed_deadline: Optional[Any] = None,
    now_epoch: Optional[int] = None,
    observed_op_id: Optional[str] = None,
) -> str:
    """#564 G6 用的入口:**消息即将进 DLQ 之前**把租户回写成终态。返回同款结果。

    与 `fence_expired_tenant` 共用同一段写库逻辑(`_fence_row` → `_fence_failed`),
    差别只有归因:这里是 `system_error`,而不是 `deadline_exceeded_in_flight`。
    **不能沿用死线那个归因** —— 投递预算耗尽时死线可能压根没到,而契约文档明写
    `deadline_exceeded_in_flight` 的含义是"已发起真实执行但没在预算内跑完";拿它描述
    一次"重投用完了"的失败是撒谎,而客户是照契约做重试分支的。
    选 `system_error` 是因为它的已发布语义(**出现即缺陷、报障、重试无益**)与仓库那条
    「**DLQ 非空 = 100% 是 bug**」逐字对齐 —— 一条消息走到 DLQ 本身就是缺陷。

    **为什么必须在进 DLQ 之前写**:DLQ 只负责兜底告警(客户表格明文),不是正常失败通道。
    不写的话 DLQ 里那条消息成了这次操作的**唯一记录**,而客户看到的租户永远停在
    `suspending`/`restoring`/`deleting` —— 那正是 issue 零节那句「接口返回成功、实际操作
    没有发生,且调用方无从察觉」。#532 的真机证据(ap-southeast-1,两个租户卡 `deleting`、
    `ReceiveCount=6`)就是这个形态。

    **写入仍带条件锚**,所以「回写前先读租户当前状态再决定」这条要求是**结构性**满足的:
    `_fence_failed` 的 CAS 锚住它读到的那个中间态,若租户已经被别的路径推成 `failed`/
    `deleted`,条件不成立 → 什么都不写。#532 一手教训正是"盲目重投/重写一个已 failed 的
    租户会制造孤儿 VM"。
    """
    return _fence_row(
        tenant_id,
        action,
        observed_deadline,
        now_epoch,
        observed_op_id,
        create_deadline.REASON_SYSTEM,
    )


def _fence_row(
    tenant_id: str,
    action: str,
    observed_deadline: Optional[Any],
    now_epoch: Optional[int],
    observed_op_id: Optional[str],
    reason: Optional[str],
) -> str:
    """两个入口共用的body:读回行 → 两道锚 → `_fence_failed`。"""
    now = int(now_epoch if now_epoch is not None else time.time())
    try:
        got = clients.tenants_table.get_item(Key={"id": tenant_id})
    except Exception as e:  # noqa: BLE001 — 见 docstring 最后一段
        print(f"[#564] fence {tenant_id}: 读行失败({type(e).__name__}): {e}")
        return "error"
    item = got.get("Item")
    if not item:
        print(f"[#564] fence {tenant_id}: 行已不存在,无需围栏")
        return "raced"

    # 锚一:死线必须与调用方判过期时看到的那个一致(见 docstring 的缺陷说明)。
    if observed_deadline is not None:
        row_dl = item.get(create_deadline.deadline_attr(action))
        try:
            _same = int(row_dl) == int(observed_deadline)
        except (TypeError, ValueError):
            _same = False
        if not _same:
            print(
                f"[#564] fence-expired {tenant_id}: action={action} 行上的死线 {row_dl!r} "
                f"≠ 我判过期时看到的 {observed_deadline!r},这行已是另一次操作,不动它"
            )
            return "raced"

    # 锚二(**所有有世代字段的档**,不只 rebuild —— Codex 独立复审第 1 轮抓出我这处不一致):
    # 死线字段在**同一个租户的连续两次同类操作**之间可能取到相同值(同一秒受理 + 同一档
    # 秒数),那时死线锚分不开两次操作,而 `<action>_op_id` 能。
    if observed_op_id and action in _PHASE_ACTIONS:
        _op_field = _PHASE_ACTIONS[action][2]
        if item.get(_op_field) != observed_op_id:
            print(
                f"[#564] fence {tenant_id}: {_op_field} "
                f"{item.get(_op_field)!r} ≠ {observed_op_id!r},不是我那次,不动它"
            )
            return "raced"

    item["_dl_action"] = action
    outcome, _reason = _fence_failed(item, now, reason)
    return outcome


def _scale_out_for_deaths(capacity_deaths: int) -> Dict[str, Any]:
    """G14 —— 判死的那一刻触发扩容,一轮只做【一次】决策。

    为什么放在批这一层、而不是每个租户判死时各调一次:`scheduling._scale_out()` 的幂等性
    建立在「ASG desired 已计入正在启动的机器」上(#341),它在【串行低频】调用下成立;
    但 1800 个租户同时判死时,那个 fail-safe(读机队计数失败 → 无条件 +1)会变成 1800 次 +1
    —— 20 TPS 下账本读被 throttle 恰恰最可能发生。把触发点收到批这一层,调用频率从
    「每个死亡一次」降到「每分钟一次」,那条 fail-safe 就重新回到它被设计时的量级(每分钟
    最多多扩一台,防的是「租户饿死而没有机器在路上」)。这是 G15 的结论:**风暴的成因是
    调用频率,不是 fail-safe 本身**;所以修频率、保住 fail-safe 的防饿死性质。

    抬多少台:按本轮容量类死亡数换算,但压在 MAX_SCALE_OUT_PER_RUN 之内。不追求一轮补齐 ——
    host 冷启实测 166s(golden AMI)/ 186s(plain AMI),见 deploy/packer/CUSTOMER-GUIDE.md:30-33
    —— 一台机器要两到三拍才进机队,期间 `in_flight > 0` 会自然抑制。
    """
    if capacity_deaths <= 0:
        return {"triggered": False, "reason": "no_capacity_deaths"}
    want = min(capacity_deaths, MAX_SCALE_OUT_PER_RUN)
    print(
        f"[#562] G14 scale-out trigger: {capacity_deaths} capacity death(s) this run, "
        f"requesting +{want} (per-run cap={MAX_SCALE_OUT_PER_RUN})"
    )
    added = 0
    for _ in range(want):
        # 逐次调用而不是一次抬 want 台:复用【唯一】那份 ASG 逻辑(MaxSize 钳制、in_flight
        # 抑制、fail-safe 都在里面),不在这里重写一份并行的 ASG 判断。第二次调用起
        # in_flight 通常已 > 0,于是自动收敛到「一轮实际只多抬必要的那几台」。
        try:
            scheduling._scale_out()
            added += 1
        except Exception as e:  # noqa: BLE001 —— 扩容失败不能让判死也停摆
            print(f"[#562] G14 scale-out call failed: {type(e).__name__}: {e}")
            break
    return {"triggered": True, "requested": want, "calls": added}


def enforce_deadlines(now_epoch: Optional[int] = None) -> Dict[str, Any]:
    """扫过死线的非终态租户,判死为终态 `failed`,并按本轮死亡数触发一次扩容。

    返回统计 dict(进 poller 返回值 → 指标)。**不 raise** —— 调用方(handler 的 poller 分支)
    已经把它包在 try 里,但这里也不靠那个:单个租户的失败计进 errors、本轮继续。
    """
    now = int(now_epoch if now_epoch is not None else time.time())
    items, truncated = _scan_expired(now, MAX_ENFORCE_PER_RUN)
    stats: Dict[str, Any] = {
        "scanned_expired": len(items),
        "fenced": 0,
        "raced": 0,
        "errors": 0,
        "truncated": truncated,
        "by_reason": {},
        # #564 G4 —— 按动作分档。验收第 3 条要断言「四个中间态都在死线+60s 内进终态」,
        # 只有总数的话那条验不了:某一档整个失灵,总数照样非零。
        "by_action": {},
    }
    if truncated:
        # 没有静默截断:本轮少做了多少必须说出来,否则 fenced=500 会被读成「一共 500 个」。
        print(
            f"[#562] deadline executor: capped at {MAX_ENFORCE_PER_RUN} this run, "
            f"{truncated} expired tenant(s) deferred to the next tick"
        )
    capacity_deaths = 0
    for item in items:
        # 统计用【实际写进 DDB 的那个归因】,不在这里重新判一次:两处各判就会不一致(M7)。
        outcome, reason = _fence_failed(item, now)
        if outcome == "fenced":
            stats["fenced"] += 1
            stats["by_reason"][reason] = stats["by_reason"].get(reason, 0) + 1
            _act = item.get("_dl_action") or create_deadline.ACTION_CREATE
            stats["by_action"][_act] = stats["by_action"].get(_act, 0) + 1
            if reason == create_deadline.REASON_CAPACITY:
                capacity_deaths += 1
        elif outcome == "raced":
            stats["raced"] += 1
        else:
            stats["errors"] += 1
    stats["scale_out"] = _scale_out_for_deaths(capacity_deaths)
    if items or truncated:
        print(f"[#562] deadline executor: {stats}")
    return stats
