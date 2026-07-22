# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""binpack — 纯函数装箱器,零 boto3。

契约 (SPEC/specs/sqs-dispatch/interfaces.md):
- 输入:pending 租户列表 + hosts 快照(调用方已算好 free_slots/inflight_ok/simulated)。
- 输出:PackResult(assignments=每 host 一批租户, unplaced=容量不够的租户)。
- 策略:能整批放下的 host 优先(减少 SSM 命令数);放不下按最小分箱数拆。
- 跳过:inflight_ok=False 的 host(在途命令未过期);simulated=True 的 host 在
  push 模式跳过(调用方按 mode 决定)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PackResult:
    """装箱结果:assignments={instance_id: [tenant_dict,...]},unplaced=剩余租户。"""

    assignments: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    unplaced: List[Dict[str, Any]] = field(default_factory=list)


def normalize_spec(
    params: Optional[Dict[str, Any]], default_vcpu: int, default_mem: int
) -> tuple[int, int]:
    """从租户 params 提取【已校验】的 (vcpu, mem_mb)。非法/缺失/非正 → 回落默认值。

    #330:params 来自 SQS 消息体(信任边界外),会喂进容量账本算术。负数/非数字若直接参与
    会腐蚀 used_vcpu/used_mem_mb(下溢)或在算术里炸批。这里在装箱+CAS 求和的【唯一入口】统一
    校验:coerce int 失败或 <=0 一律回落默认(与旧 `.get(k, default)` 意图一致的 fail-safe,
    不把坏输入放进账本)。装箱与 CAS 用同一函数取数 → 两者口径必然一致,消除轴错配。
    非 dict 的 params(list/str/int 等畸形消息体)也 fail-safe 全回落默认,绝不炸批(codex review)。"""
    p = params if isinstance(params, dict) else {}

    def _pos_int(v: Any, dflt: int) -> int:
        # OverflowError:int(1e309)=inf 会炸;ValueError/TypeError:非数字字符串/None。
        # 全 fail-safe 回落默认,绝不把 inf/NaN/非正数放进容量算术(codex review)。
        try:
            iv = int(v)
        except (TypeError, ValueError, OverflowError):
            return dflt
        return iv if iv > 0 else dflt

    return _pos_int(p.get("vcpu"), default_vcpu), _pos_int(p.get("mem_mb"), default_mem)


def _rank_hosts_by_capacity(
    hosts: List[Dict[str, Any]], skip_simulated: bool
) -> List[Dict[str, Any]]:
    """按 free_vcpu 降序排;跳过 inflight_ok=False / simulated(push) / 无 vcpu 容量 /
    内存未知(mem_known=False,fail-safe:缺 total_mem_mb 的 host 不参与,不当无限内存)。

    #315 SPLIT_BY_MODE:inflight_ok 取值由调用方(_snapshot_hosts)按 dispatch 模式给。
    #330:排序键从 free_slots(VM_DEFAULT 折算的名额)改为 free_vcpu(真实剩余 vcpu),与
    CAS 的真实记账同轴,避免"装箱按名额给、CAS 按真实拒"的整批拒绝饿死。"""
    usable = []
    for h in hosts:
        if not h.get("inflight_ok", True):
            continue
        if skip_simulated and h.get("simulated"):
            continue
        if int(h.get("free_vcpu", 0) or 0) <= 0:
            continue
        if not h.get("mem_known", False):
            continue  # #330 fail-safe:内存容量未知的 host 不调度(待回填),不 fail-open 当无限
        if not h.get("disk_ok", True):
            continue  # #340 磁盘软门:/data 物理将满的 host 不接新租户(缺该键默认 True=旧行为)
        usable.append(h)
    usable.sort(key=lambda h: int(h.get("free_vcpu", 0) or 0), reverse=True)
    return usable


def pack(
    pending: List[Dict[str, Any]],
    hosts: List[Dict[str, Any]],
    per_host_cap: Optional[int] = None,
    *,
    skip_simulated: bool = False,
    default_vcpu: int = 2,
    default_mem: int = 4096,
) -> PackResult:
    """贪婪装箱:按【真实 vcpu+mem 预算】把租户塞进 host,预算或并行度上限用尽即下一 host。

    #330:装箱按每租户真实 vcpu/mem 扣减 host 的 free_vcpu/free_mem 双预算(不再一租户=1 名额),
    与 _try_reserve_host 的真实 CAS 同轴——装箱放得进的批,CAS 必不会因容量整批拒(消除饿死),
    且 1c:2G 租户能装到真实 vcpu 上限(r8g 564 而非 282 名额),达成 380 目标。
    per_host_cap:单批并行度上限(DISPATCH_MAX_PARALLEL,限 VM 数不限资源);None=不限。
    default_vcpu/default_mem:params 缺省时的回落规格(调用方传 clients.VM_DEFAULT_*)。
    """
    result = PackResult()
    if not pending:
        return result

    usable = _rank_hosts_by_capacity(hosts, skip_simulated=skip_simulated)
    if not usable:
        result.unplaced = list(pending)
        return result

    # #330 first-fit-decreasing(codex score):资源需求大的租户先放——它们最难塞,FIFO 下
    # 常被小租户占满预算后无处可去(饿死)。按 (vcpu, mem) 降序排,大租户优先落 host。
    # stable sort:同规格租户保持原始入队顺序(既有 identity-order 测试不变)。
    remaining = sorted(
        pending,
        key=lambda t: normalize_spec(t.get("params"), default_vcpu, default_mem),
        reverse=True,
    )
    # 每 host 可变预算:剩余 vcpu / 剩余 mem / 本批还能起几个 VM(并行度上限)
    hv = {h["instance_id"]: int(h.get("free_vcpu", 0) or 0) for h in usable}
    hm = {h["instance_id"]: int(h.get("free_mem", 0) or 0) for h in usable}
    vm_cap = {
        h["instance_id"]: per_host_cap if per_host_cap is not None else len(remaining)
        for h in usable
    }

    for host in usable:
        hid = host["instance_id"]
        batch: List[Dict[str, Any]] = []
        i = 0
        while i < len(remaining) and len(batch) < vm_cap[hid]:
            v, m = normalize_spec(remaining[i].get("params"), default_vcpu, default_mem)
            if v <= hv[hid] and m <= hm[hid]:
                batch.append(remaining.pop(i))
                hv[hid] -= v
                hm[hid] -= m
                # 不 i+=1:pop 后 i 已指向下一个
            else:
                i += 1  # 这个租户在本 host 放不下,试下一个(小租户可能仍能塞)
        if batch:
            result.assignments[hid] = batch
        if not remaining:
            break

    result.unplaced = remaining
    return result
