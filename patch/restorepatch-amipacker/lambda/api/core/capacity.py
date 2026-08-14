# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""capacity — host 可分配资源向量的唯一口径。纯函数,零 boto3。

tenant_service ×4 / dispatch_service ×1 / handler ×1),改一处漏七处。收敛到这里。

一切按【资源向量】算,不按【VM 个数】算:调用方拿 (alloc_vcpu, alloc_mem_mb),
个数只是资源÷规格的副产物。规格一混(1c2G / 2c4G)个数口径立刻失效,资源口径不变。

双维准入(cpu_overcommit=4.0 即 1:4,mem_overcommit=1.0)下每机型上限:
    r8g.metal-24xl  95vcpu/784384MB -> alloc 380/784384 -> 1c2G 380 个(CPU 闸)
    r7g.metal       63vcpu/522240MB -> alloc 252/522240 -> 1c2G 252 个(CPU 闸)
    m8g.metal-24xl  95vcpu/391168MB -> alloc 380/391168 -> 1c2G 191 个(MEM 闸)
    m7g.metal       63vcpu/260096MB -> alloc 252/260096 -> 1c2G 127 个(MEM 闸)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def allocatable(total: int, ratio: float) -> int:
    """某一维的可分配量 = total × ratio。

    ratio=1.0 时返回值 == total —— 内存维用 1.0 即"声明永不超物理",这条不变量是
    声明内存达到物理的 150%。

    fail-safe:total<=0(旧 host 缺 total_mem_mb / 字段未回填)-> 0,由调用方按
    "容量未知"处理(binpack 侧已有 mem_known 语义,scheduling 侧硬闸 free>=needed
    天然拒 0)。绝不返负数 —— 负值进 CAS 的 cap_m 会让 ConditionExpression 恒真,
    那是放行 bug 而非拒绝。
    """
    if total <= 0:
        return 0
    return max(0, int(total * float(ratio or 1.0)))


def stranded(
    free_vcpu: int, free_mem_mb: int, min_vcpu: int, min_mem_mb: int
) -> Tuple[int, int]:
    """2D 搁浅【资源量】:(stranded_vcpu, stranded_mem_mb)。

    搁浅 = 本维还有余量,但【另一维】已装不下最小规格租户,导致本维余量永远用不
    出去(死容量)。#352 的 m8g 正是此形态:内存耗尽后剩余 189 个 vcpu 槽再也放不
    出任何租户。

    统一 1:4 下 M 系必然搁浅(供给 4.02 GB/vCPU < 需求 2.0×2),这是已接受的代价;
    本函数把它量化出来,让搁浅率指标可观测、扩容决策不误判"还有 CPU 可用"。
    """
    fv = max(0, int(free_vcpu))
    fm = max(0, int(free_mem_mb))
    return (fv if fm < min_mem_mb else 0), (fm if fv < min_vcpu else 0)


def mem_headroom_mb(
    h: Dict[str, Any], floor_ratio: float, ttl_sec: int, now_epoch: int
) -> Optional[int]:
    """水位【之上】还剩多少实测物理内存;None = 无可信信号(调用方 fail-open)。

    为什么批量路径需要它而不是 mem_ok(needed_mb=...):同步 create 一次只放一个租户,
    传本次 needed_mb 即可;批量 dispatch 的 binpack 一次放【整批】,逐个问"这一个放不放
    得下"永远看的是同一份 avail —— 每次都通过,累加起来照样跌破水位。做法是把水位以上
    的余量当【预算】交给 binpack,由它逐租户扣减(binpack 已有 free_mem 双预算机制),
    批内累加就自然被水位夹住。

    与 mem_ok 同款三段 fail-open(门关/无信号/陈旧 → None),口径必须一致:两个函数
    对同一台 host 的判断不能矛盾。返回 0 表示"恰好贴着水位、一个都放不下",这与
    None(无信号)是不同语义 —— 0 会真的挡住放置,None 不挡。
    """
    if floor_ratio <= 0:
        return None
    avail, total = h.get("mem_avail_mb"), h.get("mem_total_mb")
    try:
        avail_mb = int(avail) if avail is not None else None
        total_mb = int(total) if total is not None else None
    except (TypeError, ValueError):
        return None
    if avail_mb is None or total_mb is None or total_mb <= 0:
        return None
    ts = int(h.get("mem_check_ts_epoch", 0) or 0)
    if ttl_sec > 0 and (not ts or now_epoch - ts > ttl_sec):
        return None
    return max(0, avail_mb - int(total_mb * float(floor_ratio)))


def mem_ok(
    h: Dict[str, Any],
    floor_ratio: float,
    ttl_sec: int,
    now_epoch: int,
    needed_mb: int = 0,
) -> bool:
    """物理内存软门:True=可接新租户(含所有 fail-open 情形);False=新鲜确认物理内存
    不足、跳过该 host。

    与既有磁盘软门(dispatch_service._host_disk_ok)【同款三段逻辑】,不另发明:
      - 门关(floor_ratio<=0)                     -> True
      - 无信号(缺字段/字段非数)                   -> True  旧 host / agent 未升级
      - 信号陈旧(now - ts > ttl)                  -> True  agent 死是正交问题,交
                                                          health_check + ASG 兜底,
                                                          磁盘门同款不越权永久封锁
      - 新鲜且 avail - needed < total×floor       -> False 唯一阻断分支

    ★ `mem_avail_mb=0` 必须走【阻断】而不是 fail-open。用 `not avail` 判空会把 0
    ——也就是物理内存真正耗尽、最该拦的那个值——误判成"无信号"而放行(=1 反而被拒,
    逻辑正好反了)。故这里用 `is None` + 类型校验区分"字段缺失"与"值为 0"。

    ★ needed_mb 做【预测准入】:门必须保证"放置之后"仍高于水位,而不是"放置之前"。
    只看当前 avail 会让一个刚好卡在水位上的 host 接下新租户后立即跌破水位。
    调用方传本次请求的 mem_mb;传 0 退化为纯当前值判定(供不知道请求规格的巡检路径用)。

    为什么需要它:声明维(账本 used_mem_mb)只保证"声明不超卖",但租户真实占用可能
    超出声明(balloon 是 best-effort、不保证回收)。物理维用 host 自报的实测
    MemAvailable 兜底,两维职责分离。
    """
    if floor_ratio <= 0:
        return True
    avail = h.get("mem_avail_mb")
    total = h.get("mem_total_mb")
    # 字段缺失或非数值 -> 无信号,fail-open。值为 0 是【有效读数】,继续往下判。
    try:
        avail_mb = int(avail) if avail is not None else None
        total_mb = int(total) if total is not None else None
    except (TypeError, ValueError):
        return True
    if avail_mb is None or total_mb is None or total_mb <= 0:
        return True
    ts = int(h.get("mem_check_ts_epoch", 0) or 0)
    if ttl_sec > 0 and (not ts or now_epoch - ts > ttl_sec):
        return True
    floor_mb = int(total_mb * float(floor_ratio))
    return (avail_mb - max(0, int(needed_mb))) >= floor_mb
