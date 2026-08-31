# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/scheduling — host 选择 / 容量预留回滚 / ASG 扩容 / 配额检查。

handler-split #132 —— 从 handler.py 逐字搬迁,函数体零改动。
依赖方向:core/scheduling → core.clients（表句柄 + overcommit/quota 常量），
不反向 import handler，不横向 import 其它 core 域（这 5 个函数互不调用）。
facade:handler.py re-export `_scale_out/_release_slot/_find_host/
_get_specific_host_with_capacity/_check_quota`,旧 patch/调用路径全程有效。

表句柄走属性访问(design.md 授权死结解法):`import core.clients as clients` +
函数体内 `clients.hosts_table`/`clients.asg_client`,不用 `from core.clients import
hosts_table`。原因:特征测试用 `_prep_host_with_capacity` 重绑表句柄注入 fixture
数据,值绑定(from-import)会让本模块持有原始对象、看不到测试重绑,`_find_host`
就扫到空表误判无容量(judge 预警的跨模块串染)。同理 overcommit 比率也被
test_no_overcommit_strict 等测试重绑来验证严格/超卖行为,故表句柄、asg、
overcommit/quota 常量**全部**走 `clients.X` 属性访问,不用 from-import——
本模块不持有任何 clients 符号的独立绑定,测试重绑 `clients.X` 即全局生效。
"""

import os
import random
import time

from botocore.exceptions import ClientError

import core.capacity as capacity
import core.clients as clients
import core.host_profile as host_profile
import core.ddb_scan as ddb_scan  # #432 —— Scan 必须翻页
import core.host_taint as host_taint


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

# #661 —— 真机 openclaw-dispatch → openclaw-api 的 ESM 参数是 BatchSize B=30、
# batching window W=2s、MaximumConcurrency C=10。目标创建 TPS 为 T、可用 host 数为 H、
# 本闸门为 S 时，实际 batch=min(B,T*W)，批速率 R_batch=T/batch；当前两处 SendCommand
# 都是 InstanceIds=[instance_id]，所以每批 target_hosts=min(batch,H,S)，总调用率为
# R_cmd=(T/batch)*min(batch,H,S)。
#
# SendCommand 上界：#646 引 engineering/evidence/stress-run-2026-08-11.md 的真机实测，
# 服务端约 6.6 rps 即限流，且没有对应服务配额项、不能自助提额。工程预算只取 5.0 rps，
# 留余量给 delete/stop/rebuild 的 SendCommand、andon 单读和 PutParameter 分片，因此
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
# 上下界联立 6<=S<=150/T 还推出 T<=25：G=5 时系统创建 TPS 的数学天花板是 25。
# 再提高只能走 #646 的聚合 SendCommand（InstanceIds=<多台> + MaxConcurrency），把
# 调用条数从 H 降到 ceil(H/50)；那属于 #646 范围，本 MR 不做。binpack 的零依赖
# 默认值由测试机械校验一致。
SPREAD_MAX_HOSTS_PER_BATCH = 6

# 与本文件其余运行开关同样统一挂在 clients.X 上，测试可重绑、调用方也只读一个来源。
# core.clients 正式声明这些 #661 专用旋钮，本模块只消费配置，不再靠 import 副作用补齐；
# 非法值与 CPU_OVERCOMMIT_RATIO 等配置一样 fail-loud，避免静默采用运维未预期的参数。


def balanced_headroom_score(free_vcpu, allocatable_vcpu, free_mem, allocatable_mem):
    """返回 vCPU/内存两维中更紧的一维余量比例，分母无效时按 0 处理。"""
    vcpu_ratio = free_vcpu / allocatable_vcpu if allocatable_vcpu else 0
    mem_ratio = free_mem / allocatable_mem if allocatable_mem else 0
    return min(vcpu_ratio, mem_ratio)


def spread_top_candidates(candidates, score_fn, tier_fn=None, rng=None):
    """跨 tier 保持优先级，同 tier 按 score 权重生成完整随机顺序。

    这是同步 create 与队列批量装箱共享的纯函数：调用方先完成各自的容量/污点/心跳
    硬门，本函数只在同一亲和档位内加权随机，不会跨档位吃掉暖池顺序。
    ``rng`` 可注入 ``random.Random``，让高并发分散行为可被确定性测试锁住。
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
            weight = (
                max(score, clients.HOST_SELECTION_SCORE_FLOOR)
                ** clients.HOST_SELECTION_WEIGHT_ALPHA
            )
            # Efraimidis-Spirakis 加权无放回排序：一次给完整列表定序，后续换机可沿
            # 同一顺序继续；稳定 sort 也让极小概率的相同 key 保留原输入次序。
            weighted.append((source.random() ** (1.0 / weight), candidate))
        weighted.sort(key=lambda item: item[0], reverse=True)
        ranked.extend(candidate for _, candidate in weighted)
    return ranked


def _scale_out():
    """Bump ASG desired capacity toward covering the pending backlog, but only
    for capacity that is not already on the way (#341).

    Old behavior was an unconditional `desired += 1` on every no-capacity
    create. A metal host is slow to boot and only registers in the hosts table
    after init-host.sh finishes, so N creates arriving in the cold-start window
    each fired their own +1 — over-provisioning N-1 empty hosts that one booting
    host would have absorbed (真机 2026-07-21: 3 creates → 4 hosts, 3 idle).

    Fix: count in-flight ASG capacity = desired - registered(active/idle) hosts.
    A positive value means a host is already booting but not yet in the ledger,
    so it will absorb this pending tenant — skip the redundant +1. Only bump when
    no un-registered host is coming (in_flight <= 0). This makes concurrent
    no-capacity creates idempotent: the first raises desired, the rest see the
    booting host in-flight and don't stack. Capacity safety is unchanged — the
    create-path CAS (_reserve_slot) still gates actual placement; this only stops
    cost waste. Fail-safe: on any read error fall back to the old +1 (better to
    over-provision than strand a pending tenant with no host coming)."""
    try:
        resp = clients.asg_client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[clients.ASG_NAME]
        )
        group = resp["AutoScalingGroups"][0]
        desired = group["DesiredCapacity"]
        max_size = group["MaxSize"]
        if desired >= max_size:
            print(f"ASG at max capacity ({max_size}), cannot scale out")
            return
        try:
            registered = _registered_host_count()
            in_flight = desired - registered
        except Exception as e:
            # Fail-safe: the ledger read failed but ASG state is known. Fall back
            # to the old unconditional +1 rather than strand a pending tenant with
            # no host coming — over-provisioning is cheaper than a stuck tenant.
            print(f"Scale out: registered-host count unavailable ({e}); bumping +1")
            registered, in_flight = -1, 0
        if in_flight > 0:
            # A host is already booting (ASG desired counts it, hosts table
            # doesn't yet) — it will absorb this pending tenant. Don't stack.
            print(
                f"ASG scale-out skipped: {in_flight} host(s) already in flight "
                f"(desired={desired}, registered={registered})"
            )
            return
        clients.asg_client.set_desired_capacity(
            AutoScalingGroupName=clients.ASG_NAME,
            DesiredCapacity=desired + 1,
        )
        print(
            f"ASG scaled out: {desired} → {desired + 1} "
            f"(registered={registered}, no host in flight)"
        )
    except Exception as e:
        print(f"Scale out error: {e}")


def _registered_host_count():
    """Count hosts already registered as active/idle in the ledger (#341).

    These are hosts whose init-host.sh finished and wrote their DDB row, so they
    can serve tenants now. ASG desired minus this count = hosts still booting
    (in flight). Strong read so a sibling create's just-registered host is seen
    and we don't double-count it as still-missing. On error the caller's
    try/except falls back to the old unconditional +1 (fail-safe).

    Paginates through LastEvaluatedKey: a DynamoDB Scan returns at most 1MB per
    call, and openclaw-hosts accumulates `deleted` rows over its lifetime. A
    single Scan page could hold only part of the table, undercount active/idle,
    falsely see in-flight capacity, and permanently skip legitimate scale-out —
    stranding pending tenants. The FilterExpression is applied server-side AFTER
    the 1MB read, so pagination is required even though the match set is small.
    Same discipline as scaler/handler.py _has_pending_tenants."""
    # #540 — 污点机器【不算】"已注册可服务"。它不接新租户,若仍被计入,desired - registered
    # 就把在途容量算大,_scale_out 于是判定"还有机器在路上"而跳过扩容 —— 现场表现是
    # "标完一批 1:6 机器,新机器起不来,租户无处可迁",正是污点功能要支持的那个操作被自己
    # 堵死。代价是可能多扩一台(标记取消后它又被算回来),比租户饿死好。
    #
    # 用服务端 FilterExpression 而不是取回来在 Python 里过滤:本函数只要一个计数,已经用
    # ProjectionExpression 把负载压到只剩 instance_id;加进 filter 既不用改投影,也不多传数据。
    # 这里可以用裸的 attribute_not_exists —— 与四处 CAS 不同,计数【偏保守是安全的】:
    # 脏值 is_tainted="false" 会让这台不被计入,于是可能多扩一台;而 CAS 那边偏保守会变成
    # "选得中、订不上"(见 host_taint.NOT_TAINTED_CONDITION 的说明),两处的失败代价不对称。
    kwargs = {
        "FilterExpression": (
            f"#s IN (:a, :i) AND attribute_not_exists({host_taint.ATTR_IS_TAINTED})"
        ),
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":a": "active", ":i": "idle"},
        "ConsistentRead": True,
        "ProjectionExpression": "instance_id",
    }
    count = 0
    resp = clients.hosts_table.scan(**kwargs)
    count += len(resp.get("Items", []))
    while resp.get("LastEvaluatedKey"):
        resp = clients.hosts_table.scan(
            ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs
        )
        count += len(resp.get("Items", []))
    return count


# ========== Helpers ==========

PHYS_SLOT_PREFIX = "ps_"
# DDB 表达式最多 300 个 operator。n 个号的不存在条件约占 2n+1 个,
# 取 120 给容量、乐观锁和 inflight 条件留足余量。
MAX_ATOMIC_SLOT_CLAIM = 120
# #671 —— 用模块级计数而不是穿参:phys_occupied_pairs 有四条调用链(dispatch 预取、
# reserve 重试环里的 next_free_phys_run、同步 create 的 phys_tap_occupied、restore),
# 穿参要改四条链的签名。Lambda 执行模型保证同一 container 内 invocation 串行(不并发),
# 而 dispatch_batch 在入口处归零,所以一次 invocation 读到的就是它自己的账。
_SCAN_STATS = {"calls": 0, "pages": 0}


def reset_scan_stats():
    _SCAN_STATS["calls"] = 0
    _SCAN_STATS["pages"] = 0


def get_scan_stats():
    return dict(_SCAN_STATS)


def phys_slot_attr(num):
    """物理号对应的 host 扁平占号属性。"""
    return f"{PHYS_SLOT_PREFIX}{int(num)}"


def slot_claim_clause(nums, alias_prefix="ps"):
    """返回批量原子占号所需的 condition/set/name/value-key 片段。"""
    conditions = []
    assignments = []
    names = {}
    value_keys = []
    for index, num in enumerate(nums):
        name_key = f"#{alias_prefix}{index}"
        value_key = f":{alias_prefix}v{index}"
        names[name_key] = phys_slot_attr(num)
        conditions.append(f"attribute_not_exists({name_key})")
        assignments.append(f"{name_key} = {value_key}")
        value_keys.append(value_key)
    return " AND ".join(conditions), ", ".join(assignments), names, value_keys


def _host_claimed_slots(host_item, exclude_ids=None):
    """返回 host item 上未被排除 owner 占用的 {物理号: owner}。"""
    skip = exclude_ids or frozenset()
    claimed = {}
    for key, owner in (host_item or {}).items():
        if not key.startswith(PHYS_SLOT_PREFIX) or owner in skip:
            continue
        try:
            claimed[int(key[len(PHYS_SLOT_PREFIX):])] = owner
        except (TypeError, ValueError):
            continue
    return claimed


def phys_occupied_pairs(host_ids):
    """一次读取返回多个 host 的 {(owner_id, phys_num)} 占用映射。

    host_ids 中每个 id 都必有键。owner_id 让 dispatch 在预取后按各 host 自己的
    batch_ids 排除本批租户；索引/扫描或任一 host 强一致读失败时返回 None，
    调用方必须 fail-closed。

    TENANT_QUERY_ENABLED=true 时，驻留租户走 gsi_host Query，迁入租户走
    gsi_status Query，避免热路径强一致扫描整张 tenants 表。该开关只会在查询索引
    全部就绪后启用。GSI 只提供最终一致提示；真正的新占号仍由下方 hosts 表强一致
    读取的 ps_*、reserve 条件写和调用方的 tap 复检兜底。索引查询未启用时保留原
    Scan 行为，兼容默认部署。
    """
    requested = list(dict.fromkeys(host_ids))
    occupied = {host_id: set() for host_id in requested}
    if not requested:
        # #671 —— 空 host 集没有真正发起 scan,故不计 calls/pages。
        return occupied
    _SCAN_STATS["calls"] += 1
    try:
        use_indexes = (
            os.environ.get("TENANT_QUERY_ENABLED", "false").lower() == "true"
        )
        if use_indexes:
            for host_id in requested:
                start_key = None
                while True:
                    kwargs = {
                        "IndexName": "gsi_host",
                        "KeyConditionExpression": "host_id = :h",
                        "FilterExpression": "#s <> :d",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {":h": host_id, ":d": "deleted"},
                        "ProjectionExpression": "id, host_id, vm_num, phys_vm_num, #s",
                    }
                    if start_key:
                        kwargs["ExclusiveStartKey"] = start_key
                    resp = clients.tenants_table.query(**kwargs)
                    _SCAN_STATS["pages"] += 1
                    for item in resp.get("Items", []):
                        phys = item.get("phys_vm_num", item.get("vm_num"))
                        try:
                            occupied[host_id].add((item.get("id"), int(phys)))
                        except (TypeError, ValueError):
                            continue
                    start_key = resp.get("LastEvaluatedKey")
                    if not start_key:
                        break

            start_key = None
            while True:
                kwargs = {
                    "IndexName": "gsi_status",
                    "KeyConditionExpression": "#s = :m",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":m": "migrating"},
                    "ProjectionExpression": (
                        "id, migration_target, vm_num, phys_vm_num, #s"
                    ),
                }
                if start_key:
                    kwargs["ExclusiveStartKey"] = start_key
                resp = clients.tenants_table.query(**kwargs)
                _SCAN_STATS["pages"] += 1
                for item in resp.get("Items", []):
                    migration_target = item.get("migration_target")
                    if migration_target not in occupied:
                        continue
                    phys = item.get("phys_vm_num", item.get("vm_num"))
                    try:
                        occupied[migration_target].add((item.get("id"), int(phys)))
                    except (TypeError, ValueError):
                        continue
                start_key = resp.get("LastEvaluatedKey")
                if not start_key:
                    break
        else:
            scan_kwargs = {
                "FilterExpression": "#s <> :d",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":d": "deleted"},
                # 内存里同时判 resident 与 migration_target,故投影保留 host/status 字段。
                "ProjectionExpression": (
                    "id, host_id, migration_target, vm_num, phys_vm_num, #s"
                ),
                "ConsistentRead": True,
            }
            start_key = None
            while True:
                kwargs = dict(scan_kwargs)
                if start_key:
                    kwargs["ExclusiveStartKey"] = start_key
                resp = clients.tenants_table.scan(**kwargs)
                _SCAN_STATS["pages"] += 1
                for item in resp.get("Items", []):
                    phys = item.get("phys_vm_num", item.get("vm_num"))
                    try:
                        phys_num = int(phys)
                    except (TypeError, ValueError):
                        continue
                    pair = (item.get("id"), phys_num)
                    host_id = item.get("host_id")
                    if host_id in occupied:
                        occupied[host_id].add(pair)
                    migration_target = item.get("migration_target")
                    if (
                        migration_target in occupied
                        and item.get("status") == "migrating"
                    ):
                        occupied[migration_target].add(pair)
                start_key = resp.get("LastEvaluatedKey")
                if not start_key:
                    break

        for host_id in requested:
            host_item = (
                clients.hosts_table.get_item(
                    Key={"instance_id": host_id}, ConsistentRead=True
                ).get("Item")
                or {}
            )
            occupied[host_id].update(
                (owner, phys_num)
                for phys_num, owner in _host_claimed_slots(host_item).items()
            )
        return occupied
    except Exception as e:  # noqa: BLE001 — 未知必须 fail-closed
        print(f"phys_occupied_pairs({requested}) read failed → fail-closed: {e}")
        return None


def phys_occupied_map(host_ids, exclude_ids=None):
    """一次 tenants 表 scan 拿回多个 host 各自的物理 tap 占用号。

    返回 {host_id: set(phys_num)}；host_ids 里的每个 id 都必有键（无占用 → 空集合）。
    扫描失败返回 None（= 未知），调用方必须 fail-closed。
    """
    pairs = phys_occupied_pairs(host_ids)
    if pairs is None:
        return None
    skip = exclude_ids or frozenset()
    return {
        host_id: {
            phys_num
            for owner_id, phys_num in host_pairs
            if owner_id not in skip
        }
        for host_id, host_pairs in pairs.items()
    }


def _occupied_union(host_id, exclude_ids=None):
    """占用集合 = 租户表权威记录 ∪ host 上先写入的 ps_* 原子占号。"""
    occupied = phys_occupied_map([host_id], exclude_ids=exclude_ids)
    return None if occupied is None else occupied[host_id]


def phys_tap_occupied(host_id, phys_num, exclude_id=None):
    """#208 — target host 上物理 tap-vm{phys_num} 是否已被别的租户占用?

    #491 —— 从 services/tenant_service.py 机械搬迁到 core(函数体逐字不变,只改名去掉
    前导下划线)。原因:队列 dispatch 路径也必须过这道门,它此前零覆盖 —— 发号器一旦被
    回退(init-host 整项覆写 / register_host 无条件 put_item),reserve 的 CAS 会把已在用
    的号再发一遍且每次都成功,launch-vm.sh 随后 `ip link del`+`kill -KILL` 抢占先到者的
    tap = 跨租户劫持(已真机复现)。依赖方向仍是 core → core.clients,不反向 import services。

    "物理占用"= 某租户当前活在 host_id 上、且它 microVM 实际挂的 tap-vm 号 == phys_num。
    物理 tap 号的权威是 **phys_vm_num**(创建时写,迁移不改;host-agent 从 vm.json 回填
    历史/迁移前建的租户)。老租户可能还没回填 phys_vm_num → 回退到 vm_num:对**从未迁移**
    的租户 vm_num == 物理 tap 号,判定正确;迁移过的租户在回填前是残余盲区(见 MR 描述
    "已知残余"),host-agent 一个 tick 内即回填补齐。

    覆盖两个来源(与原 #208 双 scan 同构,只把 key 从 vm_num 换成物理 tap 号):
      ① 已驻留 host_id 的租户(host_id=:h, 非 deleted)
      ② 正在迁入 host_id 的租户(migration_target=:h, status=migrating)——它一旦 restore
         成功就会在本 host 挂 tap-vm{它的 phys_vm_num},必须提前算进占用。
    任一命中即占用。fail-closed:scan 异常时当作"已占"(宁可让调用方重试/换号,不放行撞号)。
    exclude_id:排除租户自身(重入/自迁移场景不算撞自己)。
    """
    try:
        n = int(phys_num)
    except (TypeError, ValueError):
        return True  # 号非法 → fail-closed
    occupied = _occupied_union(
        host_id, exclude_ids={exclude_id} if exclude_id else None
    )
    return True if occupied is None else n in occupied


def phys_occupied_nums(host_id, exclude_ids=None):
    """#491 —— 一次扫描拿回 host_id 上【全部】已占用的物理 tap 号(批量版)。

    为什么要批量版:队列 dispatch 一批最多 DISPATCH_MAX_PARALLEL(默认 96)个租户,逐个调
    phys_tap_occupied 就是 96 次 scan(FilterExpression 是全表扫后过滤)。当前表几百行
    还扛得住,但本项目的目标规模是十万级租户 —— 那会变成 96 次十万行全表扫描,把装箱
    主路径拖垮。多个 host 的调用方应复用 phys_occupied_pairs/phys_occupied_map 一次预取。

    exclude_ids:**必须**把本批租户自己传进来。reserve 事务已经写了它们的
    vm_num/phys_vm_num(dispatch_service:587-617),不排除的话每个租户都会"撞自己",
    整批被误判撞号 → 释放重投 → 再撞 → 活锁。单号版靠 exclude_id 排自己且同批号互不相同
    才不受影响,批量版没有这层保护,故这里把它写成硬要求。

    返回:占用号集合;**扫描失败返回 None(= 未知)**。调用方看到 None 必须 fail-closed
    (当作可能撞号处理),绝不能把它当空集合 —— 那等于 fail-open 放行撞号。

    单 host 入口保持只接受 str。#671 曾让本函数按参数类型分流(str → 集合、
    list → pair map),那样一个安全门函数会有两种返回类型,调用方要靠 isinstance
    嗅探才知道拿到的是什么 —— 嗅探漏一个分支就是 fail-open。整批预取请直接调
    phys_occupied_pairs。
    """
    return _occupied_union(host_id, exclude_ids=exclude_ids)


def release_phys_slot(host_id, phys_num, tenant_id):
    """仅当 ps_<n> 仍属于 tenant_id 时释放；owner 不匹配视为已接手。"""
    try:
        clients.hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="REMOVE #ps",
            ConditionExpression="#ps = :tid",
            ExpressionAttributeNames={"#ps": phys_slot_attr(phys_num)},
            ExpressionAttributeValues={":tid": tenant_id},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        print(f"release_phys_slot({host_id},{phys_num}) failed: {e}")
        return False
    except Exception as e:  # noqa: BLE001 — 对账会重试孤儿
        print(f"release_phys_slot({host_id},{phys_num}) failed: {e}")
        return False


def reap_orphan_phys_slots(host_id, alive_tenant_ids, dry_run=True):
    """清理 ps_<n> owner 已不在役的孤儿；租户表是 owner 存活性的权威。"""
    item = (
        clients.hosts_table.get_item(
            Key={"instance_id": host_id}, ConsistentRead=True
        ).get("Item")
        or {}
    )
    claimed = _host_claimed_slots(item)
    orphans = sorted(
        (num, owner) for num, owner in claimed.items() if owner not in alive_tenant_ids
    )
    if not dry_run:
        for num, owner in orphans:
            release_phys_slot(host_id, num, owner)
    if orphans:
        print(
            f"reap_orphan_phys_slots({host_id}) dry_run={dry_run} "
            f"orphans={orphans[:20]}{'...' if len(orphans) > 20 else ''}"
        )
    return orphans


def next_free_phys_num(host_id, start, exclude_ids=None, limit=4096):
    """#491(review2)—— 从 start 起找第一个【未被物理占用】的号。

    为什么不「试号→撞了归还→再试」:那个框架有三个补不掉的洞 ——
      · "试几次就放弃"的上限任选都不对:发号器被回退到 1 而该 host 上有几百个在役租户时,
        低位号段全被占,任何有限次数都会误报「无容量」;
      · 每轮试号都要归还刚认领的记账,而这两条路径没有 capacity_reservation_id 令牌,
        归还做不到既幂等又可确认(限流/超时/响应丢失时无法判断扣减是否提交);
      · 靠 SQS 或下次事件重试换号又受 dlq_max_receive_count=3 与「事件才触发」限制。
    先算出空号、再用跳号 CAS 一次认领它,三个洞一起消失:不试号、不回滚、不依赖重投。

    返回 (num, occupied_set)。(None, None) = 占用集合读失败 → 调用方必须 fail-closed;
    (None, occupied) = 搜索上界内无空号。limit 是防御性上界(单 host 物理槽位远小于它),
    不是重试次数。
    """
    occupied = phys_occupied_nums(host_id, exclude_ids=exclude_ids)
    if occupied is None:
        return None, None
    try:
        n = int(start)
    except (TypeError, ValueError):
        return None, occupied
    end = n + int(limit)
    while n < end:
        if n not in occupied:
            return n, occupied
        n += 1
    return None, occupied


def first_free_phys_run(start, count, occupied, limit=4096):
    """从 start 起找第一个长度 >= count 的连续空号段起点。纯计算,零 I/O。

    #671 —— 从 next_free_phys_run 里原样抽出来的循环体。抽出来的唯一目的是让
    reserve 重试环能复用【同一份】占用集合重算 base,而不是每圈重扫一遍全表。
    行为必须与原实现逐字一致(含 limit 与「起点跳到冲突号之后」的推进方式)。
    """
    try:
        n = int(start)
        need = int(count)
    except (TypeError, ValueError):
        return None
    if need <= 0:
        return n
    end = n + int(limit)
    while n < end:
        blocked_at = None
        for k in range(need):
            if (n + k) in occupied:
                blocked_at = n + k
                break
        if blocked_at is None:
            return n
        n = blocked_at + 1  # 起点跳到冲突号之后,不必逐个回退
    return None


def next_free_phys_run(host_id, start, count, exclude_ids=None, limit=4096):
    """#491(review2)—— 从 start 起找第一个长度 >= count 的【连续】空号段起点。

    dispatch 批量必须要连续段:reserve 事务按 base+offset 给批内每个租户定号,
    _write_assignments / _put_manifest_parts 也按同一 offset 推算,非连续会让号错位。
    语义与 next_free_phys_num 一致((None,None)=读失败→fail-closed)。
    """
    occupied = phys_occupied_nums(host_id, exclude_ids=exclude_ids)
    if occupied is None:
        return None, None
    return first_free_phys_run(start, count, occupied, limit), occupied


def _release_slot(instance_id, vcpu, mem_mb, phys_num=None, tenant_id=None):
    """Roll back a capacity reservation made by the create/clone CAS when a
    later step (put_item / launch) fails. Decrements used_vcpu / used_mem_mb /
    vm_count but deliberately does NOT decrement next_vm_num — vm_num is a
    monotonic counter, and rewinding it could hand a just-freed number to a
    concurrent allocation that already claimed the next slot. Leaving a gap in
    the numbering is harmless; reusing a number is not. Best-effort; never
    raises (rollback failure must not mask the original error)."""
    kwargs = {
        "Key": {"instance_id": instance_id},
        "UpdateExpression": (
            "SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, "
            "vm_count = vm_count - :one"
        ),
        "ConditionExpression": (
            "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
        ),
        "ExpressionAttributeValues": {":v": vcpu, ":m": mem_mb, ":one": 1},
    }
    if phys_num is not None and tenant_id:
        # 占号先还,且【独立于】容量释放的结果:存量租户没有 ps_*,容量释放不能依赖该属性;
        # 反过来容量条件失败(重复回滚/字段缺失)也不能连带把号泄漏掉 —— 号还被占着时
        # reaper 也救不了(owner 仍在役),故用 owner 条件单独删,不抛。
        release_phys_slot(instance_id, phys_num, tenant_id)
    try:
        clients.hosts_table.update_item(**kwargs)
    except ClientError as e:
        print(f"_release_slot {instance_id} (non-fatal): {e}")
    except Exception as e:  # noqa: BLE001
        print(f"_release_slot {instance_id} (non-fatal): {e}")


def _find_host(vcpu_needed, mem_needed, exclude=None, rng=None):
    """Find an active or idle host with enough free resources.

    Spreads load across the warm pool with headroom-weighted ordering instead
    of packing onto whichever host the DynamoDB scan returns first.
    The old "return first fit" behaviour funneled every tenant onto the same
    host until it was overcommitted, leaving the rest of the pool idle.

    by (mem_tier, -family_rank, balance) so the pool fills in the required
    order r8g.metal-24xl > r7g.metal > m8g.metal-24xl > m7g.metal. Affinity
    outranks free capacity ON PURPOSE: the requirement is "fill R first, keep
    M in reserve", so an emptier M-series host must NOT win over an r8g that
    still fits. Within one family the balance term keeps the spread.

    `exclude` is the set of instance_ids this caller already lost a CAS race
    on. Without it a caller that loses on the top-tier host re-picks the SAME
    host every retry and burns its budget while lower tiers sit idle.
    #661 的同 tier 加权顺序只降低并发首次同点概率，不能替代 exclude 的确定性“不再重试输家”。
    """
    # Phase 6: strong read. Under a 380-create burst the spread ranking must see
    # each host's freshest used_* (which sibling creates just incremented via the
    # CAS), or every caller ranks the same stale "least-loaded" host and they all
    # pile onto it — exactly the PriorityInUse / "all packed on one host" failure
    # mode. _reserve_slot's CAS still prevents oversell; this makes the spread
    # correct instead of merely safe.
    # FilterExpression 在那 1MB 读之后才过滤,而 openclaw-hosts 累积 deleted 死行
    # (实测 39 行里 33 行是 deleted,403 字节/行 → 1MB ≈ 2601 行)。看不见后页的 host
    # 就等于「明明有容量却选不出来」→ 租户拿到容量不足,而机器空着。
    hosts = ddb_scan.scan_all(
        clients.hosts_table,
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    )

    # Rank by the host's *tightest* remaining resource, not vCPU alone.
    # Ranking on free_vcpu only mis-orders hosts when vCPU is loose but memory
    # is tight (observed live: a host showed free_vcpu=32 yet free_mem was
    # negative under aggressive MEM_OVERCOMMIT — vCPU-only ranking would still
    # rank it "best"). The hard capacity gate (free_* >= needed) keeps such a
    # host from being chosen, but the spread is wrong whenever one dimension is
    # near-full. Score each host by min(free_vcpu_ratio, free_mem_ratio) so we
    # spread toward the host with the most balanced headroom.
    candidates = []
    now_epoch = int(time.time())
    tainted_skipped = 0
    stale_skipped = 0
    for h in hosts:
        if exclude and h["instance_id"] in exclude:
            continue
        # #540 — 污点(cordon)机器不接新租户。判定走写侧的纯函数,不在这里各写一遍 filter。
        #
        # 放在循环里而不是上面 scan 的 FilterExpression 里,是刻意的:服务端过滤会让污点机
        # 彻底隐形,于是"机队里根本没机器"和"有机器但全被标了"在日志上长得一模一样 ——
        # 后者是运维刚标完一批、正等新机的正常中间态,前者是故障。分不开就没法排障。
        # 计数在下面 return 前打进日志,代价是多扫几行内存里的 dict(scan 本来就已经取回)。
        #
        # 这里只管【选点】。已在该机器上跑的租户不受影响(不驱逐/不停机/不迁移),
        # 运维广播(fleet action / refresh-rootfs)也仍然覆盖污点机器 —— 它们各自独立 scan,
        # 不经本函数;那台机器上还有租户在跑,脚本与配置更新不能漏。
        if host_taint.is_tainted(h):
            tainted_skipped += 1
            continue
        # #549 — 心跳陈旧闸:last_seen 超阈值的 host 不接新租户(独立于 SSM/租户状态)。
        # 放循环里而非 scan 的 FilterExpression 里,与污点门同理:服务端过滤会让"没机器"和
        # "有机器但心跳都陈旧"在日志上分不开。缺信号/未来时间戳 fail-open,只挡有据可查的陈旧。
        if not capacity.seen_fresh(h, clients.HOST_SEEN_STALE_SEC, now_epoch):
            stale_skipped += 1
            continue
        # family carries no override; today all four types run the uniform 1:4).
        cpu_ratio, mem_ratio_cfg = host_profile.ratios(
            h,
            (clients.CPU_OVERCOMMIT_RATIO, clients.MEM_OVERCOMMIT_RATIO),
            clients.OVERCOMMIT_BY_FAMILY,
        )
        allocatable_vcpu = capacity.allocatable(int(h["total_vcpu"]), cpu_ratio)
        free_vcpu = allocatable_vcpu - int(h["used_vcpu"])
        allocatable_mem = capacity.allocatable(int(h["total_mem_mb"]), mem_ratio_cfg)
        free_mem = allocatable_mem - int(h["used_mem_mb"])
        # DECLARED memory is not oversold; a tenant's real footprint can exceed
        # its declaration (balloon reclaim is best-effort). Same three-branch
        # shape as the disk gate: closed / no signal / stale all fail open, only
        # a fresh confirmed shortfall blocks.
        if not capacity.mem_ok(
            h,
            clients.MEM_SAFETY_FLOOR_RATIO,
            clients.MEM_CHECK_TTL_SEC,
            now_epoch,
            needed_mb=mem_needed,
        ):
            continue
        # Hard gate unchanged: must actually fit on both dimensions.
        if free_vcpu >= vcpu_needed and free_mem >= mem_needed:
            score = balanced_headroom_score(
                free_vcpu, allocatable_vcpu, free_mem, allocatable_mem
            )  # tightest dimension wins
            tier = (
                host_profile.affinity_tier(h, clients.FAMILY_ORDER)
                if clients.AFFINITY_ENABLED
                else (0, 0)
            )
            candidates.append((h, tier, score))
    ranked = spread_top_candidates(
        candidates,
        score_fn=lambda candidate: candidate[2],
        tier_fn=lambda candidate: candidate[1],
        rng=rng,
    )
    best = ranked[0][0] if ranked else None
    # #540 — 只在真的跳过了污点机时才打,免得给每次 create 加一行噪音。
    # 选不到机器时尤其要打:那一刻最需要知道"是没机器,还是机器都被标了"。
    if tainted_skipped or stale_skipped:
        print(
            f"_find_host: skipped {tainted_skipped} tainted + {stale_skipped} stale "
            f"host(s); picked={(best or {}).get('instance_id', 'none')}"
        )
    return best


def _get_specific_host_with_capacity(instance_id, vcpu_needed, mem_needed):
    """Issue #12 — locate a specific host (used for same-host clone) and
    confirm it has capacity. Returns the host item or None.

    allow_upgrading widening (for pull_image's canary tenant, #217 §10.6) was
    removed with the canary: an upgrading host must NEVER accept a tenant
    (no-cross-tenant — a tenant must not land on a host mid image-swap).
    """
    # Phase 6: strong read so the capacity gate for a pinned/clone host sees the
    # freshest used_* a concurrent create may have just reserved.
    #
    #   ① `instance_id` 就是本表的 HASH 主键,拿主键找一行本该 get_item(O(1)),
    #      扫全表只为了丢掉除一行以外的所有行;
    #   ② 更要紧的是它不翻页 —— 目标 host 若落在 1MB 之后的页里就【找不到】,
    #      于是对一个显式指定的 host 返 None,调用方读成「这台没容量」。
    #      对同机 clone / 钉死放置来说,那是一个用户可见的错答案。
    # status 门(active/idle)原来靠 FilterExpression 表达,现在读回来自己判 —— 语义不变,
    _item = clients.hosts_table.get_item(
        Key={"instance_id": instance_id}, ConsistentRead=True
    ).get("Item")
    hosts = [_item] if _item and _item.get("status") in ("active", "idle") else []
    now_epoch = int(time.time())
    for h in hosts:
        if h["instance_id"] != instance_id:
            continue
        # #540 — 污点机器即使被显式指定也不接新租户,返 None。
        # 豁免(注释原话 no-cross-tenant 无例外),这里同款处理。代价(AMI 验证机的测试租户
        # 须走 out-of-band)已在 #536 明确接受。
        # 调用方靠它自己那次诊断性 get_item 区分原因 → 污点返 409(与 draining 的 404、
        # 容量不足的 400 三者分开),见 tenant_service.py 的 preferred_host_id 分支。
        if host_taint.is_tainted(h):
            return None
        # #549 — pinned 路径同门:显式指定的 host 若心跳陈旧也不给(不提供逃生口)。
        if not capacity.seen_fresh(h, clients.HOST_SEEN_STALE_SEC, now_epoch):
            return None
        # single source of truth; per-family overcommit applies here too, or a
        # pinned/clone target would be judged by a different yardstick).
        cpu_ratio, mem_ratio = host_profile.ratios(
            h,
            (clients.CPU_OVERCOMMIT_RATIO, clients.MEM_OVERCOMMIT_RATIO),
            clients.OVERCOMMIT_BY_FAMILY,
        )
        allocatable_vcpu = capacity.allocatable(int(h["total_vcpu"]), cpu_ratio)
        free_vcpu = allocatable_vcpu - int(h["used_vcpu"])
        allocatable_mem = capacity.allocatable(int(h["total_mem_mb"]), mem_ratio)
        free_mem = allocatable_mem - int(h["used_mem_mb"])
        # not just _find_host. A pinned/clone target skipping it would be a hole
        # straight through the water-mark protection: the ledger says there is
        # room while the host's measured MemAvailable is already under the floor.
        if not capacity.mem_ok(
            h,
            clients.MEM_SAFETY_FLOOR_RATIO,
            clients.MEM_CHECK_TTL_SEC,
            int(time.time()),
            needed_mb=mem_needed,
        ):
            return None
        if free_vcpu >= vcpu_needed and free_mem >= mem_needed:
            return h
        return None  # found host but no capacity
    return None


def _check_quota(vcpu, mem_mb, data_disk_mb):
    """Return None if within quota, else an error string."""
    if not clients.QUOTAS_ENABLED:
        return None
    if clients.QUOTAS_MAX_VCPU and vcpu > clients.QUOTAS_MAX_VCPU:
        return f"vcpu={vcpu} exceeds quota (max {clients.QUOTAS_MAX_VCPU})"
    if clients.QUOTAS_MAX_MEM_MB and mem_mb > clients.QUOTAS_MAX_MEM_MB:
        return f"mem_mb={mem_mb} exceeds quota (max {clients.QUOTAS_MAX_MEM_MB})"
    if clients.QUOTAS_MAX_DATA_DISK_MB and data_disk_mb > clients.QUOTAS_MAX_DATA_DISK_MB:
        return (
            f"data_disk_mb={data_disk_mb} exceeds quota (max {clients.QUOTAS_MAX_DATA_DISK_MB})"
        )
    return None
