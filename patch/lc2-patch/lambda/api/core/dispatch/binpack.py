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

import os
import random
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Dict, List, Optional


# #661 —— α=2 在“偏向空机”和“避免重新退化成最优单点”之间取中值：α=1 对余量差异
# 过平，α 继续放大会让高分 host 重新接近独占。FLOOR 是“分散 vs 装箱紧密”的唯一旋钮：
# 越高越分散、装箱越松，极限 FLOOR=1.0 等于完全均匀随机（Kepler RM 的做法，装箱率
# 最差）；越低越接近原来的“取最优单台”，并发 create 会重新惊群。本轮取 0.5 折中。
#
# 固定场景“3 台空 score=1.0 + 其余 ratio=0.2”，权重 ∝ max(score, FLOOR) ** 2：
#                          池 300                  池 18                   池 10
# 分位数 P=25% 档内均匀   3/32  =  9.4%          3/4   = 75.0%          3/3   = 100.0%
# 加权随机 FLOOR=0.25     3/(3+297*0.0625)=13.9% 3/(3+15*0.0625)=76.0% 3/(3+7*0.0625)=87.0%
# 加权随机 FLOOR=0.50     3/(3+297*0.25)= 3.9%   3/(3+15*0.25)=44.4%   3/(3+7*0.25)=63.2%
#
# 分位数在小池会把候选档收缩成恰好 3 台空机而退化到 100%；加权随机让所有已通过
# 资格门的有负载 host 保有非零权重，其数量天然进入分母并随池规模自适应。FLOOR 只改变
# 选点顺序，不改变 host 能否装下；容量/内存等硬门仍在 filter 层，所以调它不会引入 OOM
# 风险。约 10 台的小池可分流 host 本来就少，分散上限是物理约束，残余碰撞由换机重试消化。
HOST_SELECTION_WEIGHT_ALPHA = 2.0
HOST_SELECTION_SCORE_FLOOR = 0.5

# #661 —— 加权随机只决定“谁排第一”，first-fit 仍会把每台塞满 cap 才换下一台，
# 所以目标台数必须随本批规模与【过完全部资格门后的】候选台数伸缩；同时调用方传入的
# min(DISPATCH_MAX_PARALLEL,DISPATCH_HOST_LAUNCH_CONCURRENCY) 仍是绝对上界，动态打散
# 只能收紧不能放宽。一批 30 个在 18 台可用机上先受 S=6 夹到 6 台，cap=ceil(30/6)=5；
# 只有 2 台时 cap=ceil(30/2)=15，仍正好铺满两台，不在物理小池里强行压出 unplaced。
#
# 真机 openclaw-dispatch → openclaw-api 的 ESM 参数是 BatchSize B=30、batching window
# W=2s、MaximumConcurrency C=10。目标创建 TPS 为 T、可用 host 数为 H、本闸门为 S 时，
# 实际 batch=min(B,T*W)，批速率 R_batch=T/batch；当前每个目标 host 各发一条
# InstanceIds=[instance_id] 的 SendCommand，所以 target_hosts=min(batch,H,S)，
# R_cmd=(T/batch)*min(batch,H,S)。代价仍是 SSM 调用量等于实际落位 host 数（一条
# SendCommand 带一台的 manifest）。
#
# SendCommand 上界：SSM 是已知节流源（#573 曾修复 SendCommand 节流毒 DLQ）；#646 引
# engineering/evidence/stress-run-2026-08-11.md 的真机实测显示服务端约 6.6 rps 即限流，
# 且没有对应服务配额项、不能自助提额。工程预算只取 5.0 rps，留余量给
# delete/stop/rebuild 的 SendCommand、andon 单读和 PutParameter 分片，因此
# S<=5.0*batch/T；batch=30 时 T=20 得 S<=7.5、T=25 得 S<=6、T=40 得 S<=3.75。
#
# 不挤出下界：cap=max(MIN_PER_HOST_CAP,ceil(batch/S))，而真实 effective 还会被
# min(DISPATCH_MAX_PARALLEL,G) 夹住，其中 G=DISPATCH_HOST_LAUNCH_CONCURRENCY
# （clients.py 当前 30，#646 记录 R1 要求 <=5，口径尚未统一）。要满足
# S*effective>=batch；当 ceil(B/S)>G 时必须有 S>=B/G。故 G=30 时 S>=1 恒满足，
# G=5 时 S>=6；S=5 会得到 cap=6、effective=5、总槽位 25，挤出 5 个 unplaced。
#
# exec 段边界：DISPATCH_PER_VM_BUDGET_SEC P=8，create_deadline 的执行预算 E=128s；
# rounds=ceil(effective/G) 必须为 1，才能有 1*8+120=128s=E。S=6 时 cap=ceil(30/6)=5，
# G=5 与 G=30 下 effective 都是 5、rounds 都是 1，因此两种闸门口径都贴合。
#
# 所以 S=6 是同时满足 T=20 上界 7.5、G=5 下界 6、exec rounds=1 的唯一保守值；
# 此时 R_cmd=(20/30)*6=4.0 rps，对 5.0 工程预算留 20%，对 6.6 实测墙留 39%。
# 这是保护下游 SSM 的闸门，不是打散能力上限；打散度受它约束是刻意取舍，因为撞墙会
# 直接变成 ThrottlingException → 502，比打散不足严重得多。MIN_PER_HOST_CAP 仍保留为
# 运维需要时收紧打散度的旋钮，默认 1 表示不额外限制打散度。
# 上下界联立 6<=S<=150/T 还推出 T<=25：G=5 时系统创建 TPS 的数学天花板是 25。
# 再提高只能走 #646 的聚合 SendCommand（InstanceIds=<多台> + MaxConcurrency），把
# 调用条数从 H 降到 ceil(H/50)；那属于 #646 范围，本 MR 不做。
SPREAD_MAX_HOSTS_PER_BATCH = 6
MIN_PER_HOST_CAP = int(os.environ.get("MIN_PER_HOST_CAP", "1"))


@dataclass
class PackResult:
    """装箱结果:assignments={instance_id: [tenant_dict,...]},unplaced=剩余租户。"""

    assignments: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    unplaced: List[Dict[str, Any]] = field(default_factory=list)
    host_order: List[str] = field(default_factory=list)


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


def _affinity_key(h: Dict[str, Any]) -> tuple:
    """#430 四级亲和排序键 (mem_tier, -family_rank, balance) —— 降序取最大。

    前两位由调用方(_snapshot_hosts)预先算好、以【扁平字段 affinity_tier】传入:
    本模块是纯函数(零 boto3,被 tests 以 importlib 脱包加载),不能 import
    clients/host_profile,也不读私有 "raw" 结构。

    第三位 balance = min(free_vcpu_ratio, free_mem_ratio) 是木桶短板,只在【同机型
    内】分散(避免热点);跨机型不分散 —— 业务要求 R 系先填满再用 M 系,所以 tier
    优先于余量。这也是为什么 balance 排第三而不并入主键。

    分母为 0 时该维比例取 0(不除零崩、也不当无限资源)。
    """
    tier = h.get("affinity_tier") or (0, 0)
    av = int(h.get("allocatable_vcpu", 0) or 0)
    am = int(h.get("allocatable_mem", 0) or 0)
    fv = int(h.get("free_vcpu", 0) or 0)
    fm = int(h.get("free_mem", 0) or 0)
    balance = min(fv / av if av else 0.0, fm / am if am else 0.0)
    return (int(tier[0]), int(tier[1]), balance)


def _spread_top_candidates(
    candidates,
    score_fn,
    tier_fn=None,
    rng=None,
    weight_alpha=HOST_SELECTION_WEIGHT_ALPHA,
    score_floor=HOST_SELECTION_SCORE_FLOOR,
):
    """跨 tier 保持优先级，同 tier 按 score 权重生成完整随机顺序。

    刻意复刻 scheduling.spread_top_candidates 的公式而不是 import 它:本模块要保持
    零依赖(纯函数、被 tests/test_dispatch_binpack.py 以 importlib 脱包加载)。重复的代价
    用 tests/test_661 里一条机械断言兜住 —— 同一批输入和固定 rng 逐项比对两边结果,
    任一边改了公式就打红。
    """
    if not candidates:
        return []
    tier_of = tier_fn or (lambda _candidate: (0, 0))
    source = rng or random
    by_tier = {}
    for candidate in candidates:
        by_tier.setdefault(tier_of(candidate), []).append(candidate)

    ranked = []
    for tier in sorted(by_tier, reverse=True):
        weighted = []
        for candidate in by_tier[tier]:
            score = max(0.0, float(score_fn(candidate)))
            weight = max(score, score_floor) ** weight_alpha
            # Efraimidis-Spirakis 加权无放回排序：一次给完整列表定序，后续换机可沿
            # 同一顺序继续；稳定 sort 也让极小概率的相同 key 保留原输入次序。
            weighted.append((source.random() ** (1.0 / weight), candidate))
        weighted.sort(key=lambda item: item[0], reverse=True)
        ranked.extend(candidate for _, candidate in weighted)
    return ranked


def _rank_hosts_by_capacity(
    hosts: List[Dict[str, Any]],
    skip_simulated: bool,
    affinity: bool = False,
    spread_hosts: bool = False,
    rng=None,
    host_selection_weight_alpha: float = HOST_SELECTION_WEIGHT_ALPHA,
    host_selection_score_floor: float = HOST_SELECTION_SCORE_FLOOR,
) -> List[Dict[str, Any]]:
    """先过资格门，再按既有容量顺序或同 tier 加权顺序排列 host。

    跳过 inflight_ok=False / simulated(push) / 无 vcpu 容量 / 内存未知
    (mem_known=False,fail-safe:缺 total_mem_mb 的 host 不参与,不当无限内存)/
    taint_ok=False(#540 污点机器)。

    #315 SPLIT_BY_MODE:inflight_ok 取值由调用方(_snapshot_hosts)按 dispatch 模式给。
    #330:排序键从 free_slots(VM_DEFAULT 折算的名额)改为 free_vcpu(真实剩余 vcpu),与
    CAS 的真实记账同轴,避免"装箱按名额给、CAS 按真实拒"的整批拒绝饿死。
    #430:affinity=False 时【严格保持】原 free_vcpu 排序键,逐字节等价旧行为(回退开关)。"""
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
        if not h.get("mem_ok", True):
            continue  # #430 物理内存软门:实测 MemAvailable 跌破水位的 host 不接(缺键 True=旧行为)
        if not h.get("taint_ok", True):
            continue  # #540 污点(cordon):运维显式标记不收新租户(缺键 True=未标记,旧行为)
        if not h.get("seen_ok", True):
            continue  # #549 心跳陈旧闸:last_seen 超阈值的 host 不接新租户(缺键 True=无信号/旧快照,fail-open)
        usable.append(h)
    if affinity:
        usable.sort(key=_affinity_key, reverse=True)
    else:
        usable.sort(key=lambda h: int(h.get("free_vcpu", 0) or 0), reverse=True)
    if spread_hosts:
        # #661 —— 必须在资格门之后生成完整加权顺序:放在调用方会被上面的 sort 覆盖,
        # 复制资格门又会让两层随演进静默漂移。默认 False 完整回退旧顺序。
        usable = _spread_top_candidates(
            usable,
            score_fn=lambda h: min(
                int(h.get("free_vcpu", 0) or 0)
                / int(h.get("allocatable_vcpu", 0) or 0)
                if int(h.get("allocatable_vcpu", 0) or 0)
                else 0.0,
                int(h.get("free_mem", 0) or 0)
                / int(h.get("allocatable_mem", 0) or 0)
                if int(h.get("allocatable_mem", 0) or 0)
                else 0.0,
            ),
            tier_fn=lambda h: (
                (h.get("affinity_tier") or (0, 0)) if affinity else (0, 0)
            ),
            rng=rng,
            weight_alpha=host_selection_weight_alpha,
            score_floor=host_selection_score_floor,
        )
    return usable


def select_host_for_batch(
    pending: List[Dict[str, Any]],
    hosts: List[Dict[str, Any]],
    candidate_order: List[str],
    per_host_cap: Optional[int] = None,
    *,
    exclude_host_ids=None,
    skip_simulated: bool = False,
    default_vcpu: int = 2,
    default_mem: int = 4096,
    affinity: bool = False,
) -> Optional[Dict[str, Any]]:
    """用新快照重过完整资格门，并沿既定候选顺序找能容纳整批的下一台 host。

    dispatch 的换机重试只传“已试输家”和首次装箱产生的完整顺序；inflight、simulated、
    磁盘、内存、污点、心跳与双资源容量仍全部由本模块唯一判定，避免编排层复制后漂移。
    """
    if not pending:
        return None
    usable = _rank_hosts_by_capacity(
        hosts,
        skip_simulated=skip_simulated,
        affinity=affinity,
    )
    usable_by_id = {host["instance_id"]: host for host in usable}
    excluded = exclude_host_ids or frozenset()
    specs = [normalize_spec(t.get("params"), default_vcpu, default_mem) for t in pending]
    sum_vcpu = sum(vcpu for vcpu, _ in specs)
    sum_mem = sum(mem for _, mem in specs)
    if per_host_cap is not None and len(pending) > per_host_cap:
        return None
    for host_id in candidate_order:
        if host_id in excluded:
            continue
        host = usable_by_id.get(host_id)
        if not host:
            continue
        if (
            sum_vcpu <= int(host.get("free_vcpu", 0) or 0)
            and sum_mem <= int(host.get("free_mem", 0) or 0)
        ):
            return host
    return None


def pack(
    pending: List[Dict[str, Any]],
    hosts: List[Dict[str, Any]],
    per_host_cap: Optional[int] = None,
    *,
    skip_simulated: bool = False,
    default_vcpu: int = 2,
    default_mem: int = 4096,
    affinity: bool = False,
    spread_hosts: bool = False,
    spread_max_hosts_per_batch: Optional[int] = None,
    rng=None,
    host_selection_weight_alpha: float = HOST_SELECTION_WEIGHT_ALPHA,
    host_selection_score_floor: float = HOST_SELECTION_SCORE_FLOOR,
) -> PackResult:
    """贪婪装箱:按【真实 vcpu+mem 预算】把租户塞进 host,预算或并行度上限用尽即下一 host。

    #330:装箱按每租户真实 vcpu/mem 扣减 host 的 free_vcpu/free_mem 双预算(不再一租户=1 名额),
    与 _try_reserve_host 的真实 CAS 同轴——装箱放得进的批,CAS 必不会因容量整批拒(消除饿死),
    且 1c:2G 租户能装到真实 vcpu 上限(r8g 564 而非 282 名额),达成 380 目标。
    per_host_cap:单批并行度上限(DISPATCH_MAX_PARALLEL,限 VM 数不限资源);None=不限。
    default_vcpu/default_mem:params 缺省时的回落规格(调用方传 clients.VM_DEFAULT_*)。
    affinity:#430 四级机型亲和排序开关(调用方传 clients.AFFINITY_ENABLED)。走参数
      而非在本模块读 clients —— 本模块必须保持零依赖(纯函数契约 + 脱包加载测试)。
      False = 逐字节回落原 free_vcpu 排序。
    spread_hosts/rng:#661 同 tier 全列表加权随机排序。False 默认关闭、保持旧排序;
      调用方显式传开关值,rng 可注入以锁死并发选点测试。weight_alpha/score_floor
      也由调用方从 clients 传入，使本模块继续零依赖且支持环境变量调参。
    spread_max_hosts_per_batch:#661 单批最多纳入动态打散计算的 host 数；None 保持既有
      固定 per_host_cap。非 None 时按 pending 数和 usable 数伸缩目标台数，再由
      per_host_cap 绝对上界裁剪。调用方显式传值以保持本模块零依赖。
    """
    result = PackResult()
    if not pending:
        return result

    usable = _rank_hosts_by_capacity(
        hosts,
        skip_simulated=skip_simulated,
        affinity=affinity,
        spread_hosts=spread_hosts,
        rng=rng,
        host_selection_weight_alpha=host_selection_weight_alpha,
        host_selection_score_floor=host_selection_score_floor,
    )
    result.host_order = [host["instance_id"] for host in usable]
    if not usable:
        result.unplaced = list(pending)
        return result

    effective_per_host_cap = per_host_cap
    if spread_max_hosts_per_batch is not None:
        # #661 —— 候选数必须取完整资格门后的 usable；在编排层用原始 hosts 计数会把
        # inflight/磁盘/内存/污点/心跳失败的机器算进分母，cap 被虚假压小并制造 unplaced。
        # pending 也必须进 min：租户数少于机器数时每租户一台已是最大分散度，继续放大
        # 目标台数不会减少 cap，只会虚增读者对 SendCommand 扇出的预期。
        target_hosts = min(
            len(pending),
            len(usable),
            spread_max_hosts_per_batch,
        )
        spread_cap = max(
            MIN_PER_HOST_CAP,
            ceil(len(pending) / max(1, target_hosts)),
        )
        # 既有 per_host_cap 是 DDB 事务项数与 host 并行度的硬上界；动态打散只能收紧，
        # 不能借目标台数反向放宽。若环境把上界配到 MIN 以下，硬上界仍优先以保持安全。
        effective_per_host_cap = (
            min(spread_cap, per_host_cap)
            if per_host_cap is not None
            else spread_cap
        )

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
        h["instance_id"]: (
            effective_per_host_cap
            if effective_per_host_cap is not None
            else len(remaining)
        )
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
