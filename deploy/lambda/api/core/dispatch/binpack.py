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


def _slots_needed(tenant: Dict[str, Any]) -> int:
    """一个租户消耗几个 slot。当前一租户=一 microVM=1 slot(vcpu 单位由 host 层保证)。"""
    return 1


def _rank_hosts_by_capacity(
    hosts: List[Dict[str, Any]], skip_simulated: bool
) -> List[Dict[str, Any]]:
    """按 free_slots 降序排;可选跳过 simulated host(push 模式)。inflight_ok=False 一律跳过。"""
    usable = []
    for h in hosts:
        if not h.get("inflight_ok", True):
            continue
        if skip_simulated and h.get("simulated"):
            continue
        if int(h.get("free_slots", 0) or 0) <= 0:
            continue
        usable.append(h)
    usable.sort(key=lambda h: int(h.get("free_slots", 0) or 0), reverse=True)
    return usable


def pack(
    pending: List[Dict[str, Any]],
    hosts: List[Dict[str, Any]],
    per_host_cap: Optional[int] = None,
    *,
    skip_simulated: bool = False,
) -> PackResult:
    """贪婪装箱:每步挑一个能装下最多剩余租户的 host,装满即闭合并下一 host。

    per_host_cap:单次批量拉起并行度上限(如 DISPATCH_MAX_PARALLEL=96);None=不限。
    skip_simulated:push 模式下 simulated host 不参与(pull 模式传 False)。
    """
    result = PackResult()
    if not pending:
        return result

    usable = _rank_hosts_by_capacity(hosts, skip_simulated=skip_simulated)
    if not usable:
        # 所有 host 都在途或没容量 → 全 unplaced,回队列退避重试
        result.unplaced = list(pending)
        return result

    remaining = list(pending)
    # host 剩余可用槽的可变字典,避免修改入参 dict
    host_free = {
        h["instance_id"]: min(
            int(h.get("free_slots", 0) or 0),
            per_host_cap
            if per_host_cap is not None
            else int(h.get("free_slots", 0) or 0),
        )
        for h in usable
    }

    for host in usable:
        hid = host["instance_id"]
        cap = host_free[hid]
        if cap <= 0:
            continue
        batch: List[Dict[str, Any]] = []
        # 从 remaining 顺序取,直到 cap 用光或 pending 用光
        i = 0
        while i < len(remaining) and len(batch) < cap:
            t = remaining[i]
            need = _slots_needed(t)
            if need <= cap - len(batch):
                batch.append(t)
                remaining.pop(i)
                # 不 i+=1:pop 后 i 已指向下一个
            else:
                i += 1
        if batch:
            result.assignments[hid] = batch
        if not remaining:
            break

    result.unplaced = remaining
    return result
