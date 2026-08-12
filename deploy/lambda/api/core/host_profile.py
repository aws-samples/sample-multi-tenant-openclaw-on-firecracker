# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""host_profile — 机型资源画像与四级亲和排序。纯函数,零 boto3。


为什么排序键是【两级】而不是单纯用 mem_per_vcpu 连续量:
    aws ec2 describe-instance-types 实测 4 机型 GB/vCPU:
        r8g.metal-24xl 8.00 | r7g.metal 8.00 | m8g.metal-24xl 4.00 | m7g.metal 4.00
    连续量只能分出【2 档】(R 系 8.0 / M 系 4.0)。r8g→r7g 与 m8g→m7g 的先后完全由
    【代际】决定,资源比例里看不见。故:
        主键 mem_tier    : 由 total_vcpu/total_mem_mb 算(已有字段)-> 分 R/M 大类
        次键 family_rank : 由 FAMILY_ORDER 定 -> 分同档内代际
    优先级表是业务给定的顺序(不是推导值),故显式配置;新机型上线改 config 一行。

主键用已有字段的好处:instance_type 尚未回填的存量 host,R/M 大类先后依然正确,
只在同档内退到最后 —— 这让"补 instance_type 字段"与"启用亲和排序"可解耦交付。
"""

from __future__ import annotations

from typing import Any, Dict, Sequence, Tuple

# 业务给定的四级优先顺序(config scheduling.family_order 可覆盖)。
# 索引越小越优先;不在表内的 family 排最后 —— 未知机型不得抢占已知的高优先级。
DEFAULT_FAMILY_ORDER: Tuple[str, ...] = ("r8g", "r7g", "m8g", "m7g")

# mem_per_vcpu 分档边界(GB/vCPU)。R 系 8.0 过 3 条 -> tier 3;M 系 4.0 过 2 条 -> tier 2。
# 用边界而非精确值:同系不同 size 的比值有小幅浮动(如 headroom 扣减后 8.06/8.10),
# 分档吸收这种浮动,避免同系机型因零点几的差异跨档。
_MEM_TIER_BOUNDS: Tuple[float, ...] = (6.0, 3.0, 1.5)


def mem_per_vcpu_gb(host: Dict[str, Any]) -> float:
    """内存/vCPU 比(GB)。缺字段或非法 -> 0.0(落最低档,fail-safe)。

    用 DDB 的 total_*(已扣 reserved headroom)算:分子分母同源,headroom 对比值的
    影响可忽略(2048 MB 相对 384-768 GB),不改变分档结论。
    """
    try:
        vcpu = int(host.get("total_vcpu", 0) or 0)
        mem_mb = int(host.get("total_mem_mb", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if vcpu <= 0 or mem_mb <= 0:
        return 0.0
    return mem_mb / 1024.0 / vcpu


def family(instance_type: Any) -> str:
    """'r8g.metal-24xl' -> 'r8g'。非法/空/缺失 -> ''。"""
    if not instance_type or not isinstance(instance_type, str):
        return ""
    return instance_type.split(".")[0]


def affinity_tier(
    host: Dict[str, Any], family_order: Sequence[str] = DEFAULT_FAMILY_ORDER
) -> Tuple[int, int]:
    """亲和档位 (mem_tier, -family_rank) —— 越大越优先填,可直接进 sort key 前两位。

    实算(4 机型注册容量):
        r8g.metal-24xl -> (3,  0)
        r7g.metal      -> (3, -1)
        m8g.metal-24xl -> (2, -2)
        m7g.metal      -> (2, -3)
    降序取最大即 r8g > r7g > m8g > m7g,与业务要求的四级顺序逐项一致。

    fail-safe:instance_type 缺失/未知 -> family_rank 落表尾,但 mem_tier 仍由已有
    字段算出 -> 该 host 落【本档最后】而非全局最低。宁可少接不可当高优先级猛塞
    (与 binpack 既有 mem_known fail-safe 同一立场:缺信息 -> 保守)。
    """
    mpv = mem_per_vcpu_gb(host)
    mem_tier = sum(1 for b in _MEM_TIER_BOUNDS if mpv >= b)
    try:
        rank = list(family_order).index(family(host.get("instance_type", "")))
    except ValueError:
        rank = len(family_order)
    return mem_tier, -rank


def ratios(
    host: Dict[str, Any],
    defaults: Tuple[float, float],
    by_family: Dict[str, Dict[str, Any]] | None = None,
) -> Tuple[float, float]:
    """(cpu_ratio, mem_ratio) —— per-family 覆盖,缺项回落全局默认。

    当前策略(统一 1:4):4 机型全用 cpu=4.0 / mem=1.0,即全部回落 defaults。
    机制保留是因为"每类机型可分别设不同超卖比"是明确需求;数值可推导 ——
    理想 cpu 比 = 物理供给(GB/vCPU) ÷ 租户需求(GB/vCPU):
        R 系 8.06 / 2.0 ≈ 4.03 -> 1:4 恰好匹配(这就是 1:4 的物理依据)
        M 系 4.02 / 2.0 ≈ 2.01 -> 统一 1:4 时 M 系多出的 CPU 槽会搁浅(已接受)
    非法/非 dict 的 by_family 条目 fail-safe 回落默认,不炸调度。
    """
    entry = (by_family or {}).get(family(host.get("instance_type", "")))
    if not isinstance(entry, dict):
        entry = {}
    try:
        cpu = float(entry.get("cpu", defaults[0]))
        mem = float(entry.get("mem", defaults[1]))
    except (TypeError, ValueError):
        return float(defaults[0]), float(defaults[1])
    return cpu, mem
