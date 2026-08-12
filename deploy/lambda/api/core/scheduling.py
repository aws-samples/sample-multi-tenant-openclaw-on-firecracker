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

import time

import core.capacity as capacity
import core.clients as clients
import core.host_profile as host_profile


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
    kwargs = {
        "FilterExpression": "#s IN (:a, :i)",
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


def _release_slot(instance_id, vcpu, mem_mb):
    """Roll back a capacity reservation made by the create/clone CAS when a
    later step (put_item / launch) fails. Decrements used_vcpu / used_mem_mb /
    vm_count but deliberately does NOT decrement next_vm_num — vm_num is a
    monotonic counter, and rewinding it could hand a just-freed number to a
    concurrent allocation that already claimed the next slot. Leaving a gap in
    the numbering is harmless; reusing a number is not. Best-effort; never
    raises (rollback failure must not mask the original error)."""
    try:
        clients.hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, "
                "vm_count = vm_count - :one"
            ),
            ConditionExpression="used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one",
            ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1},
        )
    except Exception as e:
        print(f"_release_slot {instance_id} (non-fatal): {e}")


def _find_host(vcpu_needed, mem_needed, exclude=None):
    """Find an active or idle host with enough free resources.

    Spreads load across the warm pool (least-loaded / max-free-vcpu first)
    instead of packing onto whichever host the DynamoDB scan returns first.
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
    """
    # Phase 6: strong read. Under a 380-create burst the spread ranking must see
    # each host's freshest used_* (which sibling creates just incremented via the
    # CAS), or every caller ranks the same stale "least-loaded" host and they all
    # pile onto it — exactly the PriorityInUse / "all packed on one host" failure
    # mode. _reserve_slot's CAS still prevents oversell; this makes the spread
    # correct instead of merely safe.
    hosts = clients.hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    ).get("Items", [])

    # Rank by the host's *tightest* remaining resource, not vCPU alone.
    # Ranking on free_vcpu only mis-orders hosts when vCPU is loose but memory
    # is tight (observed live: a host showed free_vcpu=32 yet free_mem was
    # negative under aggressive MEM_OVERCOMMIT — vCPU-only ranking would still
    # rank it "best"). The hard capacity gate (free_* >= needed) keeps such a
    # host from being chosen, but the spread is wrong whenever one dimension is
    # near-full. Score each host by min(free_vcpu_ratio, free_mem_ratio) so we
    # spread toward the host with the most balanced headroom.
    best = None
    best_key = None
    now_epoch = int(time.time())
    for h in hosts:
        if exclude and h["instance_id"] in exclude:
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
            vcpu_ratio = free_vcpu / allocatable_vcpu if allocatable_vcpu else 0
            mem_ratio = free_mem / allocatable_mem if allocatable_mem else 0
            score = min(vcpu_ratio, mem_ratio)  # tightest dimension wins
            tier = (
                host_profile.affinity_tier(h, clients.FAMILY_ORDER)
                if clients.AFFINITY_ENABLED
                else (0, 0)
            )
            key = (tier[0], tier[1], score)
            if best_key is None or key > best_key:
                best = h
                best_key = key
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
    hosts = clients.hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    ).get("Items", [])
    for h in hosts:
        if h["instance_id"] != instance_id:
            continue
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
