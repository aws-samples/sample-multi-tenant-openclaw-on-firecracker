# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""create_deadline — 创建死线的唯一口径。纯函数,零 boto3。

## 业务锚(#562,客户原话)

> 3 分钟内一个 firecracker 必须创建完成;即使失败了,也必须是终态 —— 这样对业务才有意义。
> 业务看到这个创建任务失败了,可以再从发起端自己重试就好了。

三条推论,本模块的每个函数都要回到这里对:
  1. 180s 是对「VM 真的可用」的硬 SLO,不是「控制面回了个答复」的期限;
  2. 失败必须是终态且可归因 —— 「还在创建」「稍后可能会好」不算终态,业务拿它做不了决策;
  3. 重试权归发起端。服务端不做无界后台重试:到点判死、说清原因、放手。

## 为什么这些常量是这些值(算术,不是拍的)

`180 = 攒批 2s + 排队 QUEUE_BUDGET + 执行 EXEC_BUDGET`

- **攒批 2s** 不可动(客户决策:不要攒那么久)。ESM `BatchSize=30`,20 TPS 下约 1.5s 就攒满,
  抬窗口会被 batch 上限打断,等于没抬。
- **执行段 = 128s**。这不是估的:`dispatch_service._exec_timeout()` 的 SSM `executionTimeout`
  = `ceil(batch × DISPATCH_PER_VM_BUDGET_SEC / 有效并发) + 120s`,batch=30 / per-vm=8s /
  concurrency=30 → `ceil(30×8/30) + 120 = 8 + 120 = 128s`。执行段【必须 ≥ 它】,否则会出现
  「死线执行者已判死、host 侧 SSM 还在跑」→ 起出一个没人认的 VM,占容量还计费。
  微观参照:`FACT-BASELINE` 实测 microVM 纯启动 p50 **1.74s**、launch→gateway 可用 **6.48s**,
  所以 128s 里真正花在起 VM 上的只是零头,余量是给 SSM 投递 + 批内串行 + 重试的。
- **排队段 = 180 - 2 - 128 = 50s**。这是「等容量」的全部预算。50s 内没等到就判死 ——
  依据是形态第 4 条:判定只看【当前已就绪机队】,扩容赶不上本次请求(`scaler.interval_minutes: 3`
  光轮询就吃满预算),所以等下去没有意义,不如判死 + 立刻触发扩容让客户端重试能成。

改这三个数必须一起改并重跑压测:抬排队段就得压执行段,而执行段低于 128s 就会制造孤儿 VM。
G13 的配置基线文档把这条联动写成显式约束。

## 为什么放在 core/ 而不是 services/

死线口径有【四个】消费者,散开写必然漂:
  1. `tenant_service` 创建时算死线、写租户行、放进消息体;
  2. `dispatch_service` 消费前检查是否已过期(过期即丢弃,G7);
  3. `dispatch_service` 判「注定超不过死线」时决定判死(形态第 4 条);
  4. `dispatch_poller` 的独立死线执行者兜底扫「creating 且已过死线」(G6)。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

# ── 死线预算三段(算术见模块 docstring)────────────────────────────────────
DEADLINE_TOTAL_SEC = 180
"""客户的业务契约:3 分钟。不可动。

#564 G5 —— 这个数现在是 `deadline_sec_for("create")` 的**默认值**,不再是唯一来源:
客户明文要求「改 Lambda env 即可修改每个 lifecycle 配置」,而 create 此前是唯一一个
连改都改不了的(它是 Python 常量)。三段预算断言仍按这个标称值自洽 —— 抬 env 只抬总额,
不改 `BATCH_WINDOW_SEC`/`EXEC_BUDGET_SEC` 的算术依据(那两个来自 ESM 与 SSM 的真实参数)。"""

BATCH_WINDOW_SEC = 2
"""ESM 攒批窗口。客户决策不可动(不要攒那么久)。"""

EXEC_BUDGET_SEC = 128
"""执行段。取自 `dispatch_service._derive_exec_timeout(30)` 在达标配置下的值。

【不许低于本次批的 SSM executionTimeout】—— 低了就会「死线判死、SSM 还在跑」→ 起出一个
没人认的 VM,占容量还计费。

**这个数有条件依赖,不是常量真理。** 该函数的真实公式是(核过源码,#327 codex 纠正过一次):
    executionTimeout = min( ceil(batch / slots) × per_vm + 120 ,  visibility - 60 )
其中 slots = DISPATCH_HOST_LAUNCH_CONCURRENCY(30)、per_vm = DISPATCH_PER_VM_BUDGET_SEC(8)。
达标配置 batch=30 → `ceil(30/30)×8 + 120 = 128`。但:
  · **batch 一超过 slots 就跳一整轮**:batch=31 → `ceil(31/30)=2` → `2×8+120 = 136 > 128`;
  · visibility 若被调到 < 188s,`min()` 那一支会把它压下来。
所以本模块提供 `exec_budget_for(batch, slots, per_vm, visibility)` 让调用方按【本次批的真实
参数】算,而 `EXEC_BUDGET_SEC` 只是达标配置下的标称值(给预算三段自洽断言与文档用)。
G13 的配置基线校验器必须把 `max_batch_size ≤ DISPATCH_HOST_LAUNCH_CONCURRENCY` 列成硬约束 ——
否则执行段被静默突破,而突破的表现是孤儿 VM,不是报错。
"""

QUEUE_BUDGET_SEC = DEADLINE_TOTAL_SEC - BATCH_WINDOW_SEC - EXEC_BUDGET_SEC
"""排队等容量的预算 = 50s。等不到就判死并触发扩容,不无限等。"""

# ── 租户行与消息体的字段名(单一真相,不许各处硬写字符串)──────────────────
ATTR_DEADLINE = "create_deadline"
"""租户行上的死线绝对时间戳(epoch 秒)。独立死线执行者按它扫。"""

ATTR_FAIL_REASON = "create_fail_reason"
"""机器可读的失败原因分类(G4 要求 100% 可归因)。"""

MSG_DEADLINE_KEY = "deadline"
"""dispatch 消息体里的死线字段(G7:消费前检查,过期即丢弃)。"""

# ── 失败原因分类(G4:每个 failed 都要带机器可读原因)────────────────────────
REASON_CAPACITY = "capacity_unavailable"
"""容量不足 —— 压测里【唯一】允许出现的失败类别(G4)。"""

REASON_DEADLINE_EXCEEDED = "deadline_exceeded_in_flight"
"""已发起真实创建但 host 侧超出执行段。这类要回收资源(形态未决项第 4 条)。"""

REASON_SYSTEM = "system_error"
"""系统故障。压测中出现【任何一个】就是 G4 不达标 —— 不许用它兜底「不知道为什么」。"""

ALL_REASONS = (REASON_CAPACITY, REASON_DEADLINE_EXCEEDED, REASON_SYSTEM)


# ═══════════════════════════════════════════════════════════════════════════
# #564 G1 —— per-action 死线口径:八个操作的单一真相
# ═══════════════════════════════════════════════════════════════════════════
#
# 八个操作 × 四条通道,散开写就是 32 个漂移点。本模块已经是 create 那条通道的口径来源、
# 且已有四个消费者;把另外七个操作接到同一份里,`rg` 才能证明"无第二处硬写秒数"
# (issue 验收第 1 条)。
#
# **本段全部是加法**:上面 create 专用的 `DEADLINE_TOTAL_SEC` / `ATTR_*` / `MSG_*` /
# `REASON_*` 与下面所有既有纯函数一个字节没动 —— 那四个消费者(`deadline_executor`、
# `tenant_service`、`dispatch_service`、`scripts/checks/create-deadline-config.py`)
# 无需同步改。零 boto3 的形态也保住:只多 import 一个 stdlib `os`。

ACTION_CREATE = "create"
ACTION_SUSPEND = "suspend"
ACTION_RESTORE = "restore"
ACTION_RESTART = "restart"
ACTION_START = "start"
ACTION_REBUILD = "rebuild"
ACTION_BACKUP = "backup"
"""**网关手动备份**,不是系统定时备份。

issue 明文划界:定时备份(`BACKUP_INTERVAL_HOURS`/`BACKUP_BATCH_LIMIT`)有自己的错峰语义,
**不受这个 600s 约束,不要顺手改它**。这个 key 只覆盖 `POST /tenants/<id>/action` 的
`backup` 那条(通道 D)。"""

ACTION_DELETE = "delete"

DEADLINE_ACTIONS = (
    ACTION_CREATE,
    ACTION_SUSPEND,
    ACTION_RESTORE,
    ACTION_RESTART,
    ACTION_START,
    ACTION_REBUILD,
    ACTION_BACKUP,
    ACTION_DELETE,
)
"""八个操作。客户表格最终版给了五档(create/suspend/restore/手动备份/delete),
客户 2026-08-21 追加 `rebuild` 与 `restart` 按 180s 档;#604 后续把 `start` 加入
`restart` 同档 —— 理由:同一条通道、同为不含数据步骤的动作。"""

_DEFAULT_DEADLINE_SEC = {
    ACTION_CREATE: DEADLINE_TOTAL_SEC,  # 180,#562 已落地的那条
    ACTION_SUSPEND: 180,
    ACTION_RESTORE: 180,
    ACTION_RESTART: 180,
    ACTION_START: 180,  # 与 restart 同档:同一条通道、同为不含数据步骤的动作
    ACTION_REBUILD: 180,
    ACTION_BACKUP: 600,
    ACTION_DELETE: 600,
}
"""客户表格最终版的值。**这些不是"兜底猜测"而是权威默认** —— 区别很重要,见
`deadline_sec_for` 里对「不许拿不到就用默认」那条要求的处置。"""

_ENV_PREFIX = "LIFECYCLE_DEADLINE_SEC_"

# ══════════════════════════════════════════════════════════════════════════
# #565 G1 —— 八档的三段预算分解
# ══════════════════════════════════════════════════════════════════════════
#
# ## 为什么要有这一段
#
# #564 交付了死线**机制**,但机制拦不住已经在飞的 SSM 命令 —— 没有任何办法撤回一条
# `SendCommand`。所以「到点给上层终态」与「底层别再动」是两件事,只有让**执行段本身装进
# 死线**才对齐。本轮实查发现的矛盾(bb=1cedf334):
#
#   suspend 的同步备份 300s、restore 的 launch 300s、restart 300s、rebuild 300s
#   —— 四个 180s 档的执行段**各自单项就超死线 120s**;delete 的 300+300 正好等于 600s,
#   排队段零余量。
#
# 而实测典型值只有 休眠 6.0s / 唤醒 3.7s / 注销 12.2s / 备份 6.6s / 恢复 16s
# (`FACT-BASELINE.md:35-36`)。那个 300 **没有任何出处** —— 它是 `_ssm_run` 的默认参数值被
# 复制开的结果,约为实测的 50 倍。后果不是变慢:suspend 在 t=180 被判 `failed`,而 SSM 在
# t=252 真的成功了 → **上层看到失败、底层其实成功**,正是客户压测投诉的那个现象。
#
# 这个矛盾此前被另一个更大的 bug 盖着:`770a26df` 把 `_ssm_run` 从「轮数上限」改成真实墙钟
# deadline 之后,`timeout=300` 才**真的**是 300s,矛盾才露出来。
#
# ## 处置(用户 2026-08-24 定):按实测重设各段预算,让三段之和真的等于死线
#
# 代价已知并接受:大盘 / host 忙时 suspend 会在预算内**失败**(fail-closed、不丢数据、
# 可重试)而不是慢慢成功。**不抬客户定的死线数字** —— 路 A 的整个意义就在这里。
#
# ## 三段的通道语义不同,别当成一套
#
# | 段 | 通道 A(create) | 通道 B(suspend/restore/restart/start/delete) | 通道 C/D(rebuild/手动备份) |
# | --- | --- | --- | --- |
# | `intake` | ESM 攒批窗口 2s | **0** —— 生命周期 ESM **根本没设** `max_batching_window`(`lambdas.py:1049-1061`),默认 0 | 0,直接 `invoke` |
# | `queue`  | 等容量 | 排队等消费槽,由消费能力反推(见 `tolerable_queue_depth`) | 留给 AWS 异步重试的余量(无排队概念) |
# | `exec`   | SSM executionTimeout | 该动作真实传给 `_ssm_run` 的墙钟预算 | 同 |
#
# ## 执行段的分步预算 —— 这是本段的单一真相源
#
# 每一步的数都从 `FACT-BASELINE` 的实测值或脚本自己的有界等待之和起算,依据写在行内。
# **`_WORST_EXEC_SEC` 与各处 `_ssm_run(timeout=…)` 都从这里取,不另写数字** —— 那正是
# 本仓反复处置的「同一公式散在 8 处」。
BACKUP_TERM_GRACE_SEC = 60
"""同步备份被 `timeout` TERM 掉之后,留给 `backup-data.sh` 的 EXIT trap 把 VM 从 Paused
恢复回 running 的宽限期。

**这个 60 不是本模块拍的**:它是 host-agent 的 `_BACKUP_TERM_GRACE_SEC`
(`deploy/userdata/host-agent.py:1104`,env `OC_BACKUP_TERM_GRACE_SEC` 可覆盖),按
`backup-data.sh` 里 resume 的最坏耗时算出来的 ——「5 次 × 5s max-time + (1+2+3+4) = 35s」
再加 25s 余量,并有 `test_469_r7_host_backup_loop_adversarial.py::TestResumeBudgetFitsInTheTermGrace`
把两个数锁在一起。这里抄一份是因为控制面与 host-agent 分处 Python 与 Python 但不同 asset,
共享不了常量;`tests/test_565_g1_budget_breakdown.py` 有一条断言把两处逐值比对,漂了就红。

**为什么它必须进预算**:不留这段宽限就只能 SIGKILL,而那会掐断 EXIT trap 里的 resume →
客户 VM 永久留在 Paused,且 reaper 救不了它(判 `fc_alive` 是进程存活,Paused 进程是活的)。
`backup-data.sh` 自己把这个后果评为「比丢一次备份严重:丢备份下轮会重来,而 Paused 不会
自己好」。所以任何含同步备份的档,它的 backup 步预算都必须 **> 60**。
"""

STOP_VM_LOCK_CONTENTION_SEC = 17  # stop-vm.sh:230 flock -w 2 + :266 flock -w 15
STOP_VM_FLUSH_WORST_SEC = 24  # :15 SHORT × 2 + :16 EXTENDED,SSH 次数硬封顶 3
STOP_VM_TERMINATE_SEC = 5  # :361 curl 2 + :364 sleep 2 + :371 sleep 1
STOP_VM_WORST_SEC = (
    STOP_VM_LOCK_CONTENTION_SEC
    + STOP_VM_FLUSH_WORST_SEC
    + STOP_VM_TERMINATE_SEC
)
"""`stop-vm.sh` 的最坏墙钟下界:17s 锁竞争(`:230 flock -w 2` + `:266 flock -w 15`)
+ 24s guest flush(`:15 SHORT × 2` + `:16 EXTENDED`,SSH 次数硬封顶 3)
+ 5s 停机(`:361 curl --max-time 2` + `:364 sleep 2` + `:371 sleep 1`)。

这些数的**唯一真相在 `deploy/userdata/stop-vm.sh`**;这里是控制面预算需要的抄本,由
`tests/test_626_stop_budget_covers_stop_vm_worst_adversarial.py` 逐项解析脚本并锁住 parity。

预算若低于 `STOP_VM_WORST_SEC`,控制面会先按死线判失败而 SSM 仍在跑,最终 VM 实际被停、
控制面却记成失败 —— 正是 #617 那一类状态不一致。**不许靠压缩 `flock -w` 让这个最坏值
变小**:#608 第一轮这样改过并被打回,会重新打开 #469 那一类生命周期并发问题。
"""

_EXEC_STEPS: Dict[str, Any] = {
    # create 不动:128s 已由 `EXEC_BUDGET_SEC` 从 batch/slots/per-vm 论证过(#562)。
    ACTION_CREATE: (("dispatch-ssm", EXEC_BUDGET_SEC),),
    # suspend = 无条件同步备份 + stop-vm。备份实测 6.6s/9.5MB → 90s(13.6×,给大盘留量);
    # stop-vm 的下界不能按 6.0s 典型值取倍数:flock -w / timeout 都是设计上会走到的
    # 有界路径,不是异常。50 = STOP_VM_WORST_SEC(46) + 4s 余量。
    ACTION_SUSPEND: (("backup", 90), ("stop-vm", 50)),
    # restore = 冷恢复 launch(sync=True)。实测恢复 16s → 120s(7.5×);它含 S3 下载+解密+
    # 解压+e2fsck,是八档里单步最重的一个,所以系数比 stop-vm 那种纯本地操作低。
    ACTION_RESTORE: (("launch-vm", 120),),
    # restart = 一条组命令 `stop-vm && sleep 2 && launch-vm`(:7386-7392),**没有备份**。
    # 从已测分量推导:stop 6.0s + 2s + launch 6.48s ≈ 14.5s → 75s(5.2×)。
    # 它与 start 都不含数据相关步骤;restart 还多了 stop+sleep,所以执行段是 75s、排队段
    # 105s。**不给它 120s 是刻意的**:多给执行段就是少给排队段,而它本来就不需要那么多。
    ACTION_RESTART: (("stop+launch", 75),),
    # start 只跑 launch,基准 6.48s;组命令还含 host_script_self_heal(可能从 S3 拉脚本,
    # 一次网络往返)与幂等 DNAT,所以给 60s(9.3×)。比 restart 的系数更宽是刻意的,
    # 不是抄漏。
    ACTION_START: (("launch-vm", 60),),
    # ⚠ **含同步备份的三档(suspend/rebuild/delete)的 backup 步一律 90s,不是"按需分配"。**
    # 那 90 = `BACKUP_TERM_GRACE_SEC`(60)+ 脚本自己的墙钟额度(30)。60 不是我拍的:它是
    # host-agent 的 `_BACKUP_TERM_GRACE_SEC`(`deploy/userdata/host-agent.py:1104`),按
    # `backup-data.sh` 的 EXIT trap 里 resume 的最坏耗时 35s 抬上来的,并有 parity 测试锁着。
    # backup 侧把脚本包成 `timeout --signal=TERM --kill-after=60 30`,于是这一步的真实墙钟
    # 上界就是 90 —— **预算第一次真的界住了 host,而不只是界住控制面的轮询。**
    # 详见 `deploy/lambda/backup/handler.py` 那处 `timeout` 包装旁边的说明。
    # rebuild = **强制同步备份** + `rebuild-vm.sh`。
    #
    # ⚠ **那次备份是 issue 分析表漏掉的一项。** issue 只写了 rebuild 有一个 300s 的
    # `_ssm_run`,但 `_rebuild_repin_apply`(:5661)在**每次** rebuild 都先做一次
    # `_force_backup_sync` —— 注释原文「both channels are resolved before the **mandatory**
    # backup」,且「任一方向、含 no-op 写路径都备」(codex review2 #1:短路不能跳过备份,
    # 否则丢 overlay 前无兜底)。所以 rebuild 的执行段是**两步**,不是一步。
    #
    # `rebuild-vm.sh` = stop-vm + overlay 提交 + launch-vm;overlay 提交是
    # `mv "${OVERLAY}" "${TOMBSTONE}"`(`deploy/userdata/rebuild-vm.sh:319`)—— 同文件系统
    # **重命名,O(1)**,不随盘大小涨。预算下界不能按 6.0s 典型 stop 值取倍数:
    # flock -w / timeout 都是设计上会走到的有界路径,不是异常。
    # 66 = STOP_VM_WORST_SEC(46) + O(1) rename + 20s(launch 实测 6.48s 的 3×)。
    # 备份必须先拿够 90(它装不下比 90 更小的数,见上面那段:60 宽限是硬的),
    # rebuild-vm 再拿 66。**第一版分成 55+65 是错的** —— 55 装不下 60s 宽限,
    # 于是那一步的墙钟上界压根界不住,预算又变回纸面的。
    #
    # **rebuild 的 queue 只有 24s,是八档里最紧的一个**:同一个 180s 死线里必须先给
    # backup 90s、再给 rebuild-vm 66s;把异步重试余量压到 24s 是本轮对齐最坏等待的代价。
    # 后果如实写进契约文档:
    # 一个能 suspend 成功的大盘租户,可能 rebuild 失败。**这不是取舍失误,是 180s 装不下
    # 两个数据相关步骤这件事本身**;要它更宽只能抬死线或把备份异步化(都不在本轮范围)。
    ACTION_REBUILD: (("backup", 90), ("rebuild-vm", 66)),
    # delete = 同步备份 + host 原子删除。备份同 suspend 取 90;host 删除实测注销 12.2s →
    # 120s(10×)。600s 档给了排队段 390s 的余量,是八档里最宽裕的。
    ACTION_DELETE: (("backup", 90), ("host-delete", 120)),
    # 手动备份走通道 D:**异步**,不受调用侧 `read_timeout` 约束(那条只管同步 invoke)。
    # 600s 死线下可以给大盘留足预算 —— 这是同一个 backup Lambda 在不同调用方式下**必须拿到
    # 不同预算**的原因,所以 `backup/handler.py` 的预算改成由调用方传入。
    ACTION_BACKUP: (("backup", 300),),
}
"""执行段的分步预算。键是 `_ssm_run` 的调用点标识,值是该步的墙钟秒数。"""


def exec_steps(action: str) -> Any:
    """该动作执行段的分步预算元组。取值必须过 `assert_reason_valid` 同款的已知性检查。"""
    key = str(action).strip().lower()
    if key not in _EXEC_STEPS:
        raise ValueError(f"未知的预算操作 {action!r}")
    return _EXEC_STEPS[key]


def exec_step_sec(action: str, step: str) -> int:
    """取某一步的墙钟预算 —— 各处 `_ssm_run(timeout=…)` 从这里取,不写字面量。"""
    for name, sec in exec_steps(action):
        if name == step:
            return int(sec)
    raise ValueError(f"{action!r} 的执行段里没有 {step!r} 这一步")


def exec_sec(action: str) -> int:
    """执行段总额 = 各步之和。"""
    return sum(int(s) for _n, s in exec_steps(action))


_INTAKE_SEC: Dict[str, int] = {
    ACTION_CREATE: BATCH_WINDOW_SEC,   # 通道 A 的 ESM 攒批窗口,客户定不可动
    ACTION_SUSPEND: 0,
    ACTION_RESTORE: 0,
    ACTION_RESTART: 0,
    ACTION_START: 0,                   # 通道 B:生命周期 ESM 没设 max_batching_window
    ACTION_DELETE: 0,                  # ↑ 通道 B:ESM 没设攒批窗口 → 0(设计,非漂移)
    ACTION_REBUILD: 0,
    ACTION_BACKUP: 0,                  # ↑ 通道 C/D:直接 invoke,无攒批
}
"""受理段。通道 B/C/D 都是 0,理由见上表 —— **不是没算,是算出来就是 0。**"""


def intake_sec(action: str) -> int:
    key = str(action).strip().lower()
    if key not in _INTAKE_SEC:
        raise ValueError(f"未知的预算操作 {action!r}")
    return _INTAKE_SEC[key]


_QUEUE_SEC: Dict[str, int] = {
    ACTION_CREATE: QUEUE_BUDGET_SEC,   # 50,#562 已论证
    ACTION_SUSPEND: 40,
    ACTION_RESTORE: 60,
    ACTION_RESTART: 105,               # stop+launch 75s,剩余 105s 给排队
    ACTION_START: 120,                 # 恒等式约束:0 + 120 + 60 = 180,必须显式写值
    ACTION_REBUILD: 24,                # 通道 C:这一段是「留给 AWS 异步重试的余量」
    ACTION_BACKUP: 300,                # 通道 D:同上
    ACTION_DELETE: 390,                # 600s 档,八档里最宽裕
}
"""排队段(通道 C/D 是「留给 AWS 异步重试的余量」)。

**为什么写成显式的表而不是 `死线 - 受理 - 执行`。** 第一版是算出来的,结果那让
`assert_all_budgets_consistent()` 里「三段之和 = 死线」这条检查变成**空操作** —— 改坏执行段
时排队段自动补偿,和永远等于死线,断言永远绿。写这条测试时实测到了(它「DID NOT RAISE」)。

issue 要的是「各段之和必须恰好等于死线值,**并有一条导入期断言守住**」;要守得住,三个数就
必须各自独立可读,恒等式才是一个真的约束。取值本身仍由消费能力反推(见
`tolerable_queue_depth` 的公式),写下来只是让它可被检查。
"""


def queue_sec(action: str) -> int:
    key = str(action).strip().lower()
    if key not in _QUEUE_SEC:
        raise ValueError(f"未知的预算操作 {action!r}")
    return _QUEUE_SEC[key]


def budget_breakdown_for(action: str) -> Dict[str, Any]:
    """某一档的三段预算,机器可读 —— 给校验器、`/system/info` 与证据用。"""
    key = str(action).strip().lower()
    return {
        "action": key,
        "total_sec": _DEFAULT_DEADLINE_SEC[key],
        "intake_sec": intake_sec(key),
        "queue_sec": queue_sec(key),
        "exec_sec": exec_sec(key),
        "exec_steps": [{"step": n, "sec": int(s)} for n, s in exec_steps(key)],
    }


def tolerable_queue_depth(action: str, batch_size: int, max_concurrency: int) -> int:
    """排队段能容忍多深的队列 —— **由消费能力反推,不是拍的**(G1 明文要求)。

        消费速率 = batch_size × maxConcurrency / 执行段        (条/秒)
        可容忍深度 D = 排队段 × 消费速率

    按**最坏执行**算而不是典型值:排队段若按典型值定,一旦负载让每条都跑满执行段,死线就破。
    `batch_size` / `maxConcurrency` 由调用方从 `config.yml` 的 `scaler.*` 传进来(与
    `deploy/stacks/lambdas.py:1034` 同源),**本模块不读配置** —— 它必须保持零 boto3、零 IO。

    只对通道 B 有意义;通道 C/D 没有排队,调用方不该问它们。
    """
    if int(batch_size) <= 0 or int(max_concurrency) <= 0:
        raise ValueError("batch_size 与 max_concurrency 必须为正")
    _exec = exec_sec(action)
    if _exec <= 0:
        raise ValueError(f"{action!r} 的执行段非正,预算表写坏了")
    return int(queue_sec(action) * int(batch_size) * int(max_concurrency) / _exec)


def assert_all_budgets_consistent() -> None:
    """导入期断言:**每一档**三段之和恰好等于该档死线,且执行段为正、排队段非负。

    照 `assert_budget_consistent()` 的理由(那条只管 create):三个数是联动的,谁单独调一个都会
    静默破坏死线口径 —— 配置写坏了要在部署时炸,不要等压测才发现。本条把它扩到八档,并额外
    守住两件事:
      · **覆盖面**:`_EXEC_STEPS` / `_INTAKE_SEC` 的键必须与 `DEADLINE_ACTIONS` 逐一对应 ——
        八档词汇表加一档而这里没跟上,就是一个没有预算的死线,而那正是本 issue 要消灭的形态;
      · **排队段不得为负**:负数意味着执行段已经吃掉了整个死线,到点必然「判死了还在跑」。
    """
    missing = set(DEADLINE_ACTIONS) - set(_EXEC_STEPS)
    extra = set(_EXEC_STEPS) - set(DEADLINE_ACTIONS)
    if missing or extra:
        raise AssertionError(
            f"预算表与八档词汇表不一致:缺 {sorted(missing)}、多 {sorted(extra)}"
        )
    if set(_INTAKE_SEC) != set(DEADLINE_ACTIONS):
        raise AssertionError("`_INTAKE_SEC` 的键与八档词汇表不一致")
    if set(_QUEUE_SEC) != set(DEADLINE_ACTIONS):
        raise AssertionError("`_QUEUE_SEC` 的键与八档词汇表不一致")
    for action in DEADLINE_ACTIONS:
        total = _DEFAULT_DEADLINE_SEC[action]
        e, i, q = exec_sec(action), intake_sec(action), queue_sec(action)
        if e <= 0:
            raise AssertionError(f"{action}:执行段 {e} 必须为正")
        if q < 0:
            raise AssertionError(
                f"{action}:排队段 {q} 为负 —— 执行段 {e} 已吃掉整个死线 {total},"
                "到点必然「判死了、SSM 还在跑」→ 孤儿资源"
            )
        if i + q + e != total:
            raise AssertionError(
                f"{action}:三段之和 {i}+{q}+{e}={i + q + e} != 死线 {total}"
            )


# ── 「单次最坏执行耗时」:死线的下界(G5 第 2 条 / G8 第 1 行)────────────────
# 「死线小于单次最坏执行 → 判死了、SSM 还在跑 → 孤儿资源」。所以这是**唯一一个
# 往小调必须被挡住**的约束。
#
# **#565 G1 之后八档都有权威值,且全部从 `_EXEC_STEPS` 推导** —— 不在这里重抄数字。
# 推导成立的前提是本轮同时把各处 `_ssm_run(timeout=…)` 也改成从 `exec_step_sec()` 取:
# 那之后「最坏执行」不再是一个估计,而是**代码里那个墙钟预算本身**。校验器另有一条断言
# 守住这个前提(执行段必须 ≥ 该动作真实传给 `_ssm_run` 的 timeout),防「预算写小了但代码
# 还在用大 timeout」这种纸面达标。
_WORST_EXEC_SEC: Dict[str, Optional[int]] = {
    action: exec_sec(action) for action in DEADLINE_ACTIONS
}


def _require_env() -> bool:
    """是否要求 env 必须存在(缺失即 fail-closed)。

    G5 原文要求「读不到 / 算不出 / 违反算术约束 → 非零退出,**不许「拿不到就用默认」**」。
    直接照字面实现会让本模块在**任何未注入 env 的地方 import 失败** —— 包括单测、
    `scripts/checks/*`、以及那四个既有消费者的本地运行,而 G1 明确要求不破坏它们。

    判据取 `AWS_LAMBDA_FUNCTION_NAME`:它只在 Lambda 运行时存在。于是
      · **线上(Lambda 内)**:env 缺失即 raise;
      · **仓内(单测/校验器/本地)**:用 `_DEFAULT_DEADLINE_SEC`,那是客户表格的权威值,
        不是猜测。
    这样"不许拿不到就用默认"在**它真正要防的场景**(线上跑着一份没人知道的死线)上成立,
    而不会把仓库工具链一起打死。这条解读写在这里,是为了让下一个人能反驳它而不是猜它。

    **两条真机核实的更正(2026-08-23),第一版注释把它们说错了**:

    ① 「冷启动就炸」不准确 —— 本模块是**懒加载**的:`handler.py` 顶层只 import
       `tenant_query_service`,`tenant_service`(它才 import 本模块)在 `:2166` 才 import。
       实测 `GET /system/info` 与 `GET /tenants` 在 env 被改坏(create=100)时都仍返 200,
       因为那两条路由压根没导入本模块。所以准确说法是「**第一次走到 tenant 写路径时炸**」——
       表现是那次请求 5xx,而不是 Lambda 起不来。这仍然是 fail-closed(不会有任何请求跑在
       一个坏死线上),只是发现时机取决于流量。形态与 `assert_budget_consistent()` 一致,
       G5 原文点名要求沿用的就是它。

    ② 「改 env 秒级生效」在当前架构下**不成立** —— api Lambda 的流量走 `live` alias →
       published version(实测 API Gateway 集成 URI = `function:openclaw-api:live`),而
       published version 的环境变量是**冻结的**;`update-function-configuration` 只改
       `$LATEST`。真机实测:改 `$LATEST` 的 `DEFAULT_NO_JWT_ROLE` 并等 75s,请求依然按
       version 18 的旧值判权。**在做出决策之前,改死线的唯一有效途径是改 `config.yml` +
       `cdk deploy`**;三条候选路见 `config.yml.example` 的 `lifecycle` 段注释。
    """
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def env_name_for(action: str) -> str:
    """该操作的死线 env 变量名 —— 单一口径,CDK 与运行时读同一个函数算出来的名字。

    形如 `LIFECYCLE_DEADLINE_SEC_SUSPEND`。名字也走单一真相:两边各拼一次字符串,
    就会出现"CDK 注入了 A、代码读 B"这种查不出来的静默失效。
    """
    key = str(action).strip().lower()
    if key not in _DEFAULT_DEADLINE_SEC:
        raise ValueError(
            f"未知的死线操作 {action!r};合法值: {', '.join(DEADLINE_ACTIONS)}"
        )
    return _ENV_PREFIX + key.upper()


PARAM_PREFIX = "/openclaw/lifecycle/deadline-sec/"
"""八档死线在 SSM Parameter Store 里的公共前缀(#564 G5 的运行时载体)。

路径形态照仓内惯例(`/openclaw/dispatch/config`、`/openclaw/litellm-host`),末尾带 `/`
以便 `GetParametersByPath` 直接用。

**为什么前缀与参数名放在这个零 boto3 的模块里,而不是放在读 SSM 的 `deadline_config.py`**:
`deploy/stacks/lambdas.py` 建 `StringParameter` 时要用参数名,而 **CDK synth 期碰不了 boto3**
—— 它已经在 import 本模块取 `env_name_for()`,参数名跟着放这里,CDK 就不用另拼字符串。
名字属于"口径",本就该和 env 名待在一处;读取实现才属于 `deadline_config`。
"""


def param_name_for(action: str) -> str:
    """该操作的 SSM 参数全名 —— 与 `env_name_for()` 同一条理由:名字只有一个来源。

    两边各拼一次字符串,就会出现「CDK 建了 A、运行时读 B」这种静默失效:参数建好了没人读,
    而读的那个永远 ParameterNotFound → 一路回落默认值,日志上完全看不出来。
    G1 已经在 env 名上踩过这条并留了断言,参数名照同一形态办。
    """
    key = str(action).strip().lower()
    if key not in _DEFAULT_DEADLINE_SEC:
        raise ValueError(
            f"未知的死线操作 {action!r};合法值: {', '.join(DEADLINE_ACTIONS)}"
        )
    return PARAM_PREFIX + key


def deadline_attr(action: str) -> str:
    """该操作在**租户行**上的死线绝对时间戳字段名(epoch 秒)。#564 G2 的字段口径。

    `create` 返回已发布的 `create_deadline`(`ATTR_DEADLINE`);其余按同一模式
    `<action>_deadline`。**这里不存在"改已发布字段名"的问题** —— `create_deadline` 本来就是
    `<action>_deadline` 这个形状,八档统一到同一模式是纯加法。

    那为什么 create 还要显式返回 `ATTR_DEADLINE` 而不是让它自然拼出来:已发布字段必须能被
    `rg ATTR_DEADLINE` 找到**唯一定义点**。靠"拼出来正好一样"的话,将来谁改了这里的拼法
    (比如加个前缀),已发布契约就会跟着静默改名,而 grep 那个常量的人什么都看不到。
    `fail_reason_attr()` 为同一条理由做了同样的事。

    与 `MSG_DEADLINE_KEY` 的分工:那个是**消息体/事件 payload** 里的键名(八档共用一个键,
    因为消息里已经带了 `action`,再把 action 拼进键名只会让消费侧多一次拼接);本函数是
    **租户行**上的列名,那里没有 action 上下文,必须带上。
    """
    key = str(action).strip().lower()
    if key not in _DEFAULT_DEADLINE_SEC:
        raise ValueError(
            f"未知的死线操作 {action!r};合法值: {', '.join(DEADLINE_ACTIONS)}"
        )
    if key == ACTION_CREATE:
        return ATTR_DEADLINE
    return f"{key}_deadline"


def worst_exec_sec_for(action: str) -> Optional[int]:
    """该操作「单次最坏执行耗时」;返 None = **未落地**(不是 0、不是"没有下界")。

    调用方必须把 None 当"我不知道"处理,绝不能当 0 —— 当 0 会让任何死线值都通过下界
    校验,把 G5 那条唯一防往小调的约束变成空操作。见 `_WORST_EXEC_SEC` 的说明。
    """
    if str(action).strip().lower() not in _DEFAULT_DEADLINE_SEC:
        raise ValueError(f"未知的死线操作 {action!r}")
    return _WORST_EXEC_SEC[str(action).strip().lower()]


def default_deadline_sec_for(action: str) -> int:
    """该操作的**权威默认**死线秒数(客户表格最终版的值),不看 env 也不看参数。

    专门给 **CDK synth / 校验器** 用:它们要注入或核对"config 没写那一档时该是多少",
    而 `deadline_sec_for()` 在 Lambda 里对缺 env 的那一档是 raise 的,拿不到这个值。
    这是唯一的公开读法 —— 调用方**不要另抄一份表**,否则会出现「CDK 注入 180、
    代码认 600」这种查不出来的静默分叉(与 `env_name_for()` 同一条理由)。
    """
    key = str(action).strip().lower()
    if key not in _DEFAULT_DEADLINE_SEC:
        raise ValueError(
            f"未知的死线操作 {action!r};合法值: {', '.join(DEADLINE_ACTIONS)}"
        )
    return int(_DEFAULT_DEADLINE_SEC[key])


def parse_deadline_sec(action: str, raw: Any, source: str) -> int:
    """把一个**外部来源**的死线原始值解析成正整数秒;非法一律 raise,**绝不回落默认**。

    #564 G5(MR 2)提出来共用的:死线值现在有**两个**载体 —— env `LIFECYCLE_DEADLINE_SEC_*`
    与 SSM Parameter `/openclaw/lifecycle/deadline-sec/<action>`。两边若各写一份解析/校验,
    就会出现「env 路径拒了、SSM 路径放行」这类静默分叉,而这正是 #430 那条教训
    (同一公式散在 8 处 = 改一处漏七处)。所以判定只留这一份。

    `source` 只进错误消息(如 `LIFECYCLE_DEADLINE_SEC_SUSPEND` 或
    `/openclaw/lifecycle/deadline-sec/suspend`),让运维一眼看出是哪个载体写坏了。

    **保持纯函数、零 boto3**:本模块的消费者里有 CDK synth 期与 `scripts/checks/*`,
    读 SSM 那一层单独放在 `core/deadline_config.py`。
    """
    key = str(action).strip().lower()
    if key not in _DEFAULT_DEADLINE_SEC:
        raise ValueError(
            f"未知的死线操作 {action!r};合法值: {', '.join(DEADLINE_ACTIONS)}"
        )
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"死线 {source}={raw!r} 不是整数 —— "
            "拒绝回落默认值(静默回落会让运维以为改生效了)"
        ) from None
    if val <= 0:
        raise ValueError(f"死线 {source}={val} 必须为正")
    return val


def deadline_sec_for(action: str) -> int:
    """该操作的死线秒数。env 优先,其次权威默认;**非法值一律 raise,绝不静默回落**。

    「非法即 raise、不回落默认」正是 G5 那条要求的核心:静默回落会让运维以为改生效了,
    而线上跑的是另一个数 —— 那比读不到更危险(与 `openclaw.json` 敏感字段被 DDB 单向
    收敛冲掉是同一类坑,issue G5 第 1 条点了这个类比)。

    往小调的下界校验在 `assert_deadline_config_sane()` 里(导入时跑一次),不在这里 ——
    这个函数在热路径上,每次调用都做一遍算术约束是浪费,而且它一旦 raise 就会把一次
    合法的客户请求打成 500。
    """
    key = str(action).strip().lower()
    if key not in _DEFAULT_DEADLINE_SEC:
        raise ValueError(
            f"未知的死线操作 {action!r};合法值: {', '.join(DEADLINE_ACTIONS)}"
        )
    raw = os.environ.get(_ENV_PREFIX + key.upper())
    # 口径:**env 不存在(None)才算「未设」**;存在而 strip 后为空("" / " ")是手误,走下面
    # 的 raise。区分理由:两者的危险度不同 —— 「没人设过」是部署形态问题(由 `_require_env`
    # 判),而「有人设了个空的」是一次改配置的手误,静默回落默认会让那个人以为改生效了,
    # 正是 G5 要防的那一件。
    if raw is None:
        if _require_env():
            raise ValueError(
                f"死线 env {_ENV_PREFIX + key.upper()} 未注入而本进程跑在 Lambda 里 —— "
                "fail-closed 拒绝用默认值继续(客户要求死线必须 env 可改;"
                "线上跑一份没人知道来源的死线比启动失败危险得多)"
            )
        return int(_DEFAULT_DEADLINE_SEC[key])
    # 解析/校验走共用的 `parse_deadline_sec`(#564 G5 提出来的)—— env 与 SSM Parameter
    # 两个载体必须用**同一份**判定,否则会出现「env 路径拒了、SSM 路径放行」的静默分叉。
    return parse_deadline_sec(key, raw, "env " + _ENV_PREFIX + key.upper())


def deadline_at_for(action: str, accepted_epoch: int) -> int:
    """从受理时刻算出该操作的死线绝对时间戳 —— **env/默认口径的零 boto3 原语**。

    计时起点 = **API 受理时刻**,与 #562 一致:上游业务侧在死线之上各留 30s 缓冲
    (180 档→210s、600 档→630s),而缓冲只有在两边起点对齐时才是真缓冲。

    ⚠ **生产路径不要调这个,调 `core.deadline_config.deadline_epoch_for()`。**
    #564 G5 之后死线秒数的运行时载体是 SSM Parameter Store,而本模块必须保持零 boto3
    (CDK synth 期与 `scripts/checks/*` 都在 import 它),所以本函数**看不见参数里的值**。
    在生产路径上用它 = 运维改了参数、`/system/info` 报 `source=ssm`,而真实死线仍按 env 走
    —— 正是 G5 要消灭的那个"看不见的失败"。本函数保留给校验器/测试作纯函数原语。
    """
    return int(accepted_epoch) + deadline_sec_for(action)


def all_deadline_sec() -> Dict[str, int]:
    """八个操作的死线现值 —— 给 G8 校验器、证据、以及日志自证用。

    验收第 4 条要求「改 Lambda env 后…死线值随之变化(**读取值要在日志或响应里可验证**)」,
    这个函数就是那个可验证点。
    """
    return {a: deadline_sec_for(a) for a in DEADLINE_ACTIONS}


def assert_deadline_config_sane() -> None:
    """八档死线的算术约束 —— 模块导入时跑一次,配置写坏了在启动期炸。

    形态沿用 `assert_budget_consistent()`(G5 原文点名要求),但校验对象不同:
      · 那条管 create 内部三段是否加满 180;
      · 这条管**每个操作的死线不得小于它单次最坏执行** —— 唯一一条防往小调的约束。
        「超出的后果不是变慢,是孤儿 VM」(#562 §2.2 原话)。

    最坏执行未落地(None)的操作**跳过**下界校验,并且**不静默** —— G8 的校验器会把
    它们列成显式的未覆盖项。这里不填猜测值的理由见 `_WORST_EXEC_SEC`。
    """
    for action in DEADLINE_ACTIONS:
        sec = deadline_sec_for(action)  # 非法值在这里就 raise
        worst = worst_exec_sec_for(action)
        if worst is None:
            continue
        if sec < worst:
            raise ValueError(
                f"死线配置不安全: {action} 的死线 {sec}s < 单次最坏执行 {worst}s —— "
                "判死之后 SSM 还在跑,会留下没人认的 VM(占容量且计费)。"
                f"往大调是安全方向;要往小调必须先压低 {action} 的最坏执行"
            )


def deadline_actions_without_worst_exec() -> tuple:
    """最坏执行未落地的操作 —— 给 G8 校验器报告"这几档的下界还没人守"。

    刻意做成一个函数而不是让校验器自己翻 `_WORST_EXEC_SEC`:那个字典是内部结构,
    将来 #565 落了预算分解就会改形状,而这个签名不会。
    """
    return tuple(a for a in DEADLINE_ACTIONS if worst_exec_sec_for(a) is None)


def deadline_at(accepted_epoch: int) -> int:
    """从受理时刻算出【创建】死线的绝对时间戳。

    用【绝对时间戳】而不是「剩余秒数」:消息可能在队列里躺任意久、可能被重投多次,
    相对量每经一跳都要重算一次,错一次就整条链路的死线口径不一致。绝对值只算一次。

    #564 G5 —— 内部改走 `deadline_at_for(ACTION_CREATE, ...)`,于是 create 的 180 也
    跟着变成 env 可改(客户明文:「每个 lifecycle 配置」都要能改,而 create 此前是唯一
    连改都改不了的)。签名与语义不变,四个既有消费者不用动。
    """
    return deadline_at_for(ACTION_CREATE, accepted_epoch)


# ── #565 G3 —— 另外七个根因类型,把词汇表补齐到覆盖八个操作 ────────────────────
#
# **上面 create 那三个一个字节没动。** 它们已经作为 `create_fail_reason` 的封闭取值发布过
# (`create-3min-deadline-contract.md` §1.2),而 `create` 的子集恰好就是 {那三个} —— 所以这里
# 是**纯加法**,不存在"扩展一个已发布封闭集合"那种契约变更(客户写的
# `if reason == "capacity_unavailable"` 一行都不用改)。
#
# 每个新值都指向**真实失败出口**,不是想出来的分类。举证在下面各常量的 docstring 里,
# 行号以 `tenant_service.py` 为准。取值口径照 #562:客户按这些**值**做分支,
# **不要按 `error` 文案做分支** —— 文案会变,值不会。

REASON_BACKUP_FAILED = "backup_failed"
"""强制前置备份失败,按 fail-closed 中止了操作。

出口:suspend `:3872/:3892/:3913/:3932`、delete `:3375/:3441`、rebuild `:4661`
(`REPIN_BACKUP_FAILED`)。**值得重试**:这类多为瞬时(S3 限流、SSM 抖动、per-tenant flock
被并发 launch 占住),而且备份是幂等的 —— 重试不会产生多余副作用。

注意 suspend `:3872` 那一支特殊:备份失败**且 VM 被留在 Paused**。原因分类相同(备份失败),
但那一支刻意不放生命周期围栏(#547),所以重试要等围栏租约过期。"""

REASON_BACKUP_MISSING = "backup_missing"
"""找不到可恢复的备份 —— 只有 restore 会出(`:4282`)。

**不值得重试**:重试一万次也不会长出一份备份来。数据可能已不可恢复,需要人介入查
S3/`restore_backup_key`。这条与 `backup_failed` 分开的理由就在这:一个该重试,一个不该,
而客户只能靠这个值区分。"""

REASON_HOST_UNREACHABLE = "host_unreachable"
"""host 侧状态探测不到,或 SSM 超时/节流/脚本非零退出。

出口:delete `:2812/:2872/:2907/:3527`、suspend `:4033/:4082`、restore `:4373`、
restart `:6098`、start 的 502。**值得重试**,隔 1–2 分钟 —— 但连续出现说明那台 host
有问题,该报障。

它覆盖两种表象不同、对客户动作相同的情形:①"探测不到 host 侧真实状态"(所以 fail-closed
拒绝继续,如 force-delete 的三个 502);②"命令下发了但没拿到成功回执"。刻意不细分:
客户拿到更细的分类也做不出不同的动作,而细分会让取值集合膨胀。"""

REASON_PREEMPTED = "preempted"
"""被另一个生命周期操作抢占(含 host 侧 per-tenant flock 被占)。

出口:suspend `:3799/:4006/:4060/:4135`、restore `:4300/:4345/:4421`、
rebuild `:4677`(`LIFECYCLE_SUPERSEDED`)、delete `:2974`。

**值得重试,而且可以立刻** —— 抢赢的那个操作会把租户带到某个终态;调用方**重读租户状态**
之后再决定要不要重发(可能压根不用重发了,比如你要 suspend 而别人已经 delete 了)。

与 `capacity_unavailable` 的分界:`preempted` 是**同一个租户**上的另一个操作抢了它;
容量类是**跟别的租户**争资源。两者动作建议都是重试,但重试的期望不同 —— 前者要先重读状态,
后者要等容量。"""

REASON_TENANT_NOT_STARTABLE = "tenant_not_startable"
"""start/restart 执行时租户已经是 `failed` 或 `requires_intervention`,当前状态不可起。

这既覆盖 API 受理时就看到坏状态的同步 409,也覆盖已返 202、consumer 执行前状态才恶化的
窗口。`failed` 是墓碑,客户必须换 `client_token` 重建;`requires_intervention` 必须先
`stop` 回到 `stopped`,再 `start`。原样重试 start/restart 不会自行收敛。"""

REASON_ROUTE_CLEANUP_BLOCKED = "route_cleanup_blocked"
"""路由/DNAT/端口位图/Redis 路由清理受阻 —— 只有 delete 会出(`:3304`)。

**不值得重试**,报障。它的形态是"租户有持久化的路由状态但没有 host_id",清理无处下手;
盲目重试只会反复撞同一个不一致,而放行会造成路由泄漏(那正是它 fail-closed 的原因)。"""

REASON_CAPACITY_RELEASE_PENDING = "capacity_release_pending"
"""破坏性删除**已经完成**,容量预留令牌的释放瞬时失败 —— 只有 delete 会出。

**值得重试,而且必须重试**。这一条与 `system_error` 的区别是本 issue 里最容易搞错的一处
(Codex 独立复审第二轮抓出来的:第一版把它归成了 `system_error`,而那个值的已发布语义是
「不值得重试,报障」——**照契约行事的调用方会让租户永久停在 deleting、容量永远搁浅**)。

它的形态(见 `tenant_service` 里 `_REL_RETRY` 那条出口的注释):释放瞬时失败 ⇒ 令牌可能仍占
着容量 ⇒ **绝不能继续标 deleted**(deleted 租户不被巡检兜底,令牌会永久搁浅)⇒ 刻意留在
`deleting` 并返 502,让队列消费者/调用方重投。重投时 status 仍是 deleting,副作用幂等
(stop/rm)、释放也幂等(令牌还在就消费一次,已消费则 already)。

所以它不是"系统坏了",而是"删除生效了、账本还差最后一步,再推一次就收敛"。连续出现才是
账本缺陷,那时报障。"""

# 八个操作的封闭子集。**客户据此收窄分支** —— 一个操作只会返回它子集内的值。
#
# 为什么是「共享词汇表 + 每操作子集」而不是「每操作各定一套」:同一个根因(备份失败)在
# suspend/delete/rebuild 三处如果叫三个名字,客户就要写三套 if/else,而它们该做的动作完全相同。
# 也不是「单一通用集合」:那样 restart 的契约里会出现 `backup_missing`(它压根不备份),
# 客户无法据此收窄任何分支。
#
# 子集**刻意偏小**:漏了一个值可以后加(客户的 else 分支兜住,兼容);而声明了一个永不出现的值
# 是永久的噪音,删它才是 breaking change。所以只列**已经有真实出口举证**的值。
REASONS_FOR = {
    ACTION_CREATE: (REASON_CAPACITY, REASON_DEADLINE_EXCEEDED, REASON_SYSTEM),
    ACTION_SUSPEND: (
        REASON_BACKUP_FAILED,
        REASON_HOST_UNREACHABLE,
        REASON_PREEMPTED,
        REASON_DEADLINE_EXCEEDED,
        REASON_SYSTEM,
    ),
    ACTION_RESTORE: (
        REASON_CAPACITY,
        REASON_BACKUP_MISSING,
        REASON_HOST_UNREACHABLE,
        REASON_PREEMPTED,
        REASON_DEADLINE_EXCEEDED,
        REASON_SYSTEM,
    ),
    ACTION_RESTART: (
        REASON_HOST_UNREACHABLE,
        REASON_PREEMPTED,
        REASON_TENANT_NOT_STARTABLE,
        REASON_DEADLINE_EXCEEDED,
        REASON_SYSTEM,
    ),
    ACTION_START: (
        REASON_HOST_UNREACHABLE,   # start 分支的 502 出口
        REASON_PREEMPTED,          # rc==75 的 flock-skip(#604)
        REASON_TENANT_NOT_STARTABLE,
        REASON_DEADLINE_EXCEEDED,  # 到点被 #564 兜底判死
        REASON_SYSTEM,
    ),
    ACTION_REBUILD: (
        REASON_BACKUP_FAILED,
        REASON_HOST_UNREACHABLE,
        REASON_PREEMPTED,
        REASON_DEADLINE_EXCEEDED,
        REASON_SYSTEM,
    ),
    ACTION_DELETE: (
        REASON_BACKUP_FAILED,
        REASON_HOST_UNREACHABLE,
        REASON_ROUTE_CLEANUP_BLOCKED,
        REASON_CAPACITY_RELEASE_PENDING,  # 只有 delete 会出 —— 见该常量的文档
        REASON_DEADLINE_EXCEEDED,
        REASON_PREEMPTED,
        REASON_SYSTEM,
    ),
    ACTION_BACKUP: (
        REASON_BACKUP_FAILED,
        REASON_HOST_UNREACHABLE,
        REASON_DEADLINE_EXCEEDED,
        REASON_SYSTEM,
    ),
}
"""`REASONS_FOR[action]` = 该操作**可能**落库的失败原因,封闭。

`ACTION_CREATE` 那一档就是已发布的 `ALL_REASONS`,列在这里是为了让「八档」完整可枚举
(校验器与测试要遍历它),**不代表 create 侧有任何行为变化**。

`ACTION_RESTORE` 含 `capacity_unavailable`:`_restore_reserve_slot` 有五个容量类 503
(`:4232` no host capacity / `:4242` contended / `:4244` slot allocation contended /
`:4254` phys tap contended / `:4260` no free physical slot)。它们在**辅助函数**里,
第一次扫失败出口时漏掉了 —— 记这一笔,因为"只扫主函数"会让覆盖率断言假绿。

`ACTION_REBUILD` **不含** `capacity_unavailable`:re-pin 唯一的"没 host"是
`REPIN_NO_HOST`(`:4642`),那是**前置条件不满足**(租户压根没被放置)而不是容量不够,
属同步拒绝,不进落库分类。

`ACTION_BACKUP`(网关手动备份)的落库点要等 #564 G7 建出操作句柄与状态字段;这里先把契约
定下来,因为它与 #565 G6 的字段形状必须是同一套(issue 明文"别各出一套")。"""

ALL_FAIL_REASONS = (
    REASON_BACKUP_FAILED,
    REASON_BACKUP_MISSING,
    REASON_CAPACITY,
    REASON_CAPACITY_RELEASE_PENDING,
    REASON_DEADLINE_EXCEEDED,
    REASON_HOST_UNREACHABLE,
    REASON_PREEMPTED,
    REASON_ROUTE_CLEANUP_BLOCKED,
    REASON_SYSTEM,
    REASON_TENANT_NOT_STARTABLE,
)
"""词汇表全集(十个值)。给校验器、文档生成与测试用;**不要**拿它当某个操作的取值集合 ——
那会让客户以为 restart 可能返回 `backup_missing`。

**为什么这里显式列举,而不是 `{r for rs in REASONS_FOR.values() for r in rs}`。**
第一版就是那个派生式。突变实验(往 `ACTION_RESTART` 子集里塞 `"disk_on_fire"`)当场证明
它让「子集 ⊆ 词汇表」变成**永真断言**:任何塞进某档子集的词都会自动进入"全集",于是
"封闭"这件事被自己绕过去了。词汇表是对外契约、发布后不可改 —— 它必须是**独立声明的
事实**,由下面的 `_assert_vocabulary_consistent()` 与各档子集互相对账。
与 #564 G1 那条同款教训:CDK 与代码各拼一次 env 名才能互相校验,单一派生源看着 DRY,
实际上让校验失效。"""


def _assert_vocabulary_consistent() -> None:
    """全集与八档子集的并集必须**严格相等** —— 两个独立声明互为对账。

    少了(子集里有全集外的词)= 契约漏了一个值,它会漏到客户那里而没有文档;
    多了(全集里有没人产出的词)= 客户白写一条永远走不到的重试分支。两个方向都当错。

    形态与 `assert_deadline_config_sane()` 一致:模块导入时跑一次,写坏了立刻炸。
    它只校验**代码里的字面量**(不读 env),所以线上不可能因为它挂 —— 任何 import 本模块的
    测试都会先在 CI 里红。
    """
    union = {r for rs in REASONS_FOR.values() for r in rs}
    missing = union - set(ALL_FAIL_REASONS)
    orphan = set(ALL_FAIL_REASONS) - union
    if missing or orphan:
        raise ValueError(
            "失败原因词汇表与各档子集不一致 —— "
            f"子集里有全集外的值 {sorted(missing)};全集里有无人产出的值 {sorted(orphan)}"
        )


_assert_vocabulary_consistent()


def reasons_for(action: str) -> tuple:
    """该操作的封闭失败原因集合;未知 action 一律 raise。

    做成函数而不是让调用方直接读 `REASONS_FOR`:字典是可变的,而这是对外契约 ——
    调用方不该有机会 `REASONS_FOR[x] += (...)`。
    """
    key = str(action).strip().lower()
    if key not in REASONS_FOR:
        raise ValueError(
            f"未知的失败原因操作 {action!r};合法值: {', '.join(DEADLINE_ACTIONS)}"
        )
    return REASONS_FOR[key]


def assert_reason_valid(action: str, reason: str) -> None:
    """`reason` 必须在该操作的封闭子集内,否则 raise。

    #565 G3 验收第 3 条要求「取值封闭,集合外的值即打红」。这条断言是那个"打红"的实现:
    它放在写库之前,所以一个拼错的取值在**开发期**就炸,而不是变成客户读到的垃圾值 ——
    对外契约一旦漏出一个集合外的值就再也收不回来了。
    """
    allowed = reasons_for(action)
    if reason not in allowed:
        raise ValueError(
            f"{action} 的失败原因 {reason!r} 不在封闭集合内: {', '.join(allowed)}。"
            "取值集合是对外契约(发布后不可改)—— 要新增值必须同时更新 "
            "engineering/customer-requirements/lifecycle-deadline-contract.md"
        )


def fail_reason_attr(action: str) -> str:
    """该操作在租户行上的失败原因字段名 —— 字段名也只有一个来源。

    `create` 保持已发布的 `create_fail_reason`(`ATTR_FAIL_REASON`);其余按同一模式
    `<action>_fail_reason`。跟 `fail` 而非 `failed`:`create_fail_reason` 是先例,而
    `rebuild_failed_reason`(自由文本、已 deprecated)是历史遗留,不作为命名依据。
    """
    key = str(action).strip().lower()
    if key not in REASONS_FOR:
        raise ValueError(f"未知的失败原因操作 {action!r}")
    if key == ACTION_CREATE:
        return ATTR_FAIL_REASON
    return f"{key}_fail_reason"


def fail_at_attr(action: str) -> str:
    """该操作最近一次失败的**时刻**字段名(ISO8601 UTC),与 `fail_reason_attr` 成对写入。

    为什么要这个伴随字段:`<action>_fail_reason` 记的是「最近一次失败」,不代表当前状态,
    也**不会被自动清除**(理由见 `tenant_service._mark_fail_reason` 的文档)。没有时间戳,
    轮询方分不清读到的原因是不是自己那次请求留下的 —— 有了它,只需与自己的发起时刻比较:
    比自己早 = 上一次的旧记录。

    `create` 同样按 `<action>_fail_at`:这是**新增**的伴随字段,已发布的
    `create_fail_reason` 那个名字不受影响,所以这里不必为 create 破例。
    """
    key = str(action).strip().lower()
    if key not in REASONS_FOR:
        raise ValueError(f"未知的失败原因操作 {action!r}")
    return f"{key}_fail_at"


ATTR_REBUILD_FAIL_REASON_DEPRECATED = "rebuild_failed_reason"
"""rebuild 的**旧**失败原因字段:自由文本,**已 deprecated**。

保留而不改名/不删:客户已经在读它,且 `ADR-rebuild-idempotency-sync-contract §5.4` 定过它的
语义。新代码读 `rebuild_fail_reason`(封闭取值);两个字段并存一个大版本,下个大版本移除旧的。

改名的收益不值那个 breaking change —— 这一点写进 `lifecycle-deadline-contract.md`。"""


def is_expired(deadline: Optional[Any], now_epoch: int) -> bool:
    """死线是否已过。

    fail-safe 的方向【刻意选择保守】:死线读不出来(缺字段/非数/None)一律返 False =
    「还没过期」。因为这个函数的两个调用点后果不对称:
      · 消费者用它决定「丢弃消息」—— 误判过期会丢掉一个客户还在等的合法创建;
      · 死线执行者用它决定「判死」—— 误判过期会把一个正常在建的租户标 failed。
    两边误判 True 的代价都远大于误判 False(后者只是让它多走一轮,由 poller 再兜一次)。
    """
    if deadline is None:
        return False
    try:
        return int(now_epoch) > int(deadline)
    except (TypeError, ValueError):
        return False


def remaining_sec(deadline: Optional[Any], now_epoch: int) -> Optional[int]:
    """距死线还剩几秒;读不出来返 None(调用方按「未知」处理,不要当 0)。

    返 None 与返 0 是【不同语义】:0 = 确知已到点,None = 不知道。把 None 当 0 会让
    读不出死线的租户被立刻判死 —— 与 is_expired 的保守方向自相矛盾。
    """
    if deadline is None:
        return None
    try:
        return int(deadline) - int(now_epoch)
    except (TypeError, ValueError):
        return None


def doomed_by_deadline(
    deadline: Optional[Any], now_epoch: int, exec_budget_sec: int = EXEC_BUDGET_SEC
) -> bool:
    """「注定超不过死线」判定(形态第 4 条)—— 剩余时间已装不下执行段。

    这是本 issue 最要紧的一个判定:它决定「不发起真实创建、直接判死」。
    判定只看时间,【不看容量】—— 容量判定在 binpack 那边,两者是 AND 关系:
    装不下(容量) 或 来不及(时间),都判死。分开是因为它们的失败原因分类不同(G4)。

    为什么不做预测模型:扩容赶不上本次请求(`scaler.interval_minutes: 3` 光轮询间隔就
    吃满预算),所以「等一会儿可能有容量」在 180s 尺度上不成立。形态第 4 条明确写了
    「判定只看当前已就绪机队容量,不需要任何预测模型」。

    fail-safe:死线未知(None)→ 返 False = 不判死。宁可让它走完正常链路,由 poller 兜底。
    """
    left = remaining_sec(deadline, now_epoch)
    if left is None:
        return False
    return left < int(exec_budget_sec)


def exec_budget_for(
    batch_size: int,
    slots: int,
    per_vm_sec: int,
    visibility_sec: int,
) -> int:
    """按【本次批的真实参数】算执行段所需秒数,口径与 dispatch_service._derive_exec_timeout 一致。

    刻意复刻那条公式而不是 import 它:本模块要保持零依赖(core 层、纯函数),而
    `_derive_exec_timeout` 在 services 层且从 `clients` 读全局 env。重复的代价用一条机械断言
    兜住 —— tests/test_562 里有一条拿同一批参数逐个比对两边结果,任一边改了公式就打红。
    这与 #462 里「scaler 跨部署单元重写 ratios 必须配机械护栏」是同一处置。
    """
    per_vm = max(1, int(per_vm_sec or 8))
    parallel = max(1, int(slots or 30))
    rounds = -(-int(batch_size) // parallel)  # ceil(batch/slots):#327 的那一整轮尾巴
    est = rounds * per_vm + 120
    cap = max(60, int(visibility_sec or 900) - 60)
    return min(est, cap)


def exec_budget_fits(
    batch_size: int,
    slots: int,
    per_vm_sec: int,
    visibility_sec: int,
) -> bool:
    """本次批的 SSM 执行段是否装得进我们的 EXEC_BUDGET_SEC。

    False = 该批的 SSM 可能跑到死线之后 → 会制造孤儿 VM。调用方应当 fail-loud 而不是
    默默继续:这类突破的表现是「客户收到 failed、我方却有它的 VM 在跑」,靠日志发现不了。
    """
    return exec_budget_for(batch_size, slots, per_vm_sec, visibility_sec) <= EXEC_BUDGET_SEC


def budget_breakdown() -> Dict[str, int]:
    """三段预算的机器可读形式 —— 给 G13 的配置基线校验器与证据用。"""
    return {
        "total_sec": DEADLINE_TOTAL_SEC,
        "batch_window_sec": BATCH_WINDOW_SEC,
        "queue_budget_sec": QUEUE_BUDGET_SEC,
        "exec_budget_sec": EXEC_BUDGET_SEC,
    }


def assert_budget_consistent() -> None:
    """三段必须恰好加满 180s,且执行段不得为负/为零。

    这个断言存在的理由:三个常量是【联动】的,谁单独调一个都会静默破坏死线口径。
    模块导入时就跑一次 —— 配置写坏了要在部署时炸,不要等压测才发现。
    """
    total = BATCH_WINDOW_SEC + QUEUE_BUDGET_SEC + EXEC_BUDGET_SEC
    if total != DEADLINE_TOTAL_SEC:
        raise ValueError(
            f"create_deadline 三段预算不自洽: {BATCH_WINDOW_SEC} + {QUEUE_BUDGET_SEC} + "
            f"{EXEC_BUDGET_SEC} = {total} != {DEADLINE_TOTAL_SEC}"
        )
    if EXEC_BUDGET_SEC <= 0 or QUEUE_BUDGET_SEC <= 0:
        raise ValueError(
            f"create_deadline 预算段必须为正: queue={QUEUE_BUDGET_SEC} exec={EXEC_BUDGET_SEC}"
        )


assert_budget_consistent()
assert_deadline_config_sane()  # #564 G5 —— 八档死线的下界约束,同款导入期 fail-closed
assert_all_budgets_consistent()  # #565 G1 —— 八档的三段预算之和必须恰好等于各自死线
