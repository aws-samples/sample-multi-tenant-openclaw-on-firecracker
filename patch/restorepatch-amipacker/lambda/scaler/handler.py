# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
import json
import boto3
from datetime import datetime, timezone

ddb = boto3.resource("dynamodb")
# autoscaling client 的 standard retry config 在下方 _asg_retry 定义后重建
autoscaling = boto3.client("autoscaling")
ssm = boto3.client("ssm")
s3 = boto3.client("s3")
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])
tenants_table = (
    ddb.Table(os.environ["TENANTS_TABLE"]) if os.environ.get("TENANTS_TABLE") else None
)

ASG_NAME = os.environ["ASG_NAME"]
IDLE_TIMEOUT = int(os.environ["IDLE_TIMEOUT_MINUTES"])

# R8 — 空闲缩容总开关。**当前阶段硬禁自动缩容**(设计决策:去掉 ASG 自动缩容
# 功能):大规模集群里自动 terminate idle host 风险 > 收益——误删刚空但马上要接新租户的
# host、缩到 0 撞 pending 租户 → no-host → requires_intervention(memory
# scaler-idle-reclaim-races-pending-tenant)、下线本就慢(lifecycle 串行停 VM)。当前
# 功能+稳定优先(铁律#1)+ 成本不是约束,host 留池不回收比误删安全。
# 硬禁方式:总门 IDLE_RECLAIM_ENABLED 恒 False,不再读 env(env 设 true 也不缩)。idle
# 标记/恢复仍无条件做(无害,保留可观测:哪些 host 空了)。未来要恢复自动缩容:改回读 env +
# 补 pending 门 + 缩容前查在途租户,单独 issue 评审。
IDLE_RECLAIM_ENABLED = False  # 硬禁自动缩容(不读 env);idle 标记仍做,只是永不 terminate
# autoscaling client 配 standard retry(指数退避+jitter),对标 K8s CA
# aws_sdk_provider 复用 SDK 内置 retryer,不手搓(references.md#R8-Ref-4)。
try:
    from botocore.config import Config as _BotoConfig

    _asg_retry = _BotoConfig(retries={"max_attempts": 5, "mode": "standard"})
    autoscaling = boto3.client("autoscaling", config=_asg_retry)  # 覆盖上面的裸 client
except Exception:  # noqa: BLE001 — botocore 恒在,兜底不改行为
    _asg_retry = None

# delete is in flight. TTL处置对它必须是 no-op:租户正在被显式删除,若 TTL 循环
# 再对它下发 stop/delete,会把 "deleting" 逆转成活跃态(重开账本扣穿窗口)或抢在
# delete 主流程完成副作用前误标终态(致 _abort_restore_status 回滚失败 + slot 泄漏)。
# 已备份 S3、slot 已释放——TTL 的"释放资源"目的已达成,若再对它下发 stop/delete 会把状态
# 破坏(restore 找不到 suspended 态)、甚至走 delete 清理链删掉 S3 备份 = 数据丢失。
# suspending/restoring 是在途态,更不能被 TTL 打断。三态全部 no-op。
_TTL_TERMINAL = {
    "stopped",
    "deleted",
    "failed",
    "deleting",
    "suspending",
    "suspended",
    "restoring",
}

# Force-rotate every tenant onto the current golden image every N hours so
# image/skill/guardrail changes propagate by rebuild (never hot-patch a live
# VM). Because the host's read-only rootfs is fixed at host boot, a tenant is
# refreshed by RE-CREATING it on a host that already runs the new image —
# backup → launch-vm RESTORE on new-image host → repoint → drop old VM. Live
# snapshot migration is unavailable while balloon is on, so we use the
# backup/restore path the API recommends. Gated OFF by default for safe
# rollout; flip IMAGE_REFRESH_ENABLED=true once verified on a test node.
IMAGE_REFRESH_ENABLED = (
    os.environ.get("IMAGE_REFRESH_ENABLED", "false").lower() == "true"
)
REFRESH_INTERVAL_HOURS = int(os.environ.get("REFRESH_INTERVAL_HOURS", "48"))
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
ROOTFS_PREFIX = os.environ.get("ROOTFS_PREFIX", "deployment/rootfs")
BACKUP_PREFIX = os.environ.get("BACKUP_PREFIX", "backups")
# Cap how many tenants we rotate per tick so a fleet-wide refresh ripples
# gradually instead of stampeding every host at once.
REFRESH_MAX_PER_TICK = int(os.environ.get("REFRESH_MAX_PER_TICK", "1"))

# The base scaler only removes idle hosts; new tenants that find no room sit in
# `pending` until a host happens to free up or ASG reacts. A production fleet
# should keep a WARM BUFFER of free capacity so a burst of self-provisions lands
# instantly. This keeps free allocatable vCPU at or above a target — expressed
# as a PERCENT of total allocatable (RESERVE_PCT, e.g. 20) OR an absolute core
# floor (RESERVE_CORES), whichever is larger. If free dips below it, scale the
# ASG out (one step per tick, capped at MaxSize). Gated OFF by default for safe
# rollout; flip RESERVE_ENABLED=true once verified. Overcommit ratio must match
# the API's _find_host view so "free" means the same thing on both sides.
RESERVE_ENABLED = os.environ.get("RESERVE_ENABLED", "false").lower() == "true"
RESERVE_PCT = float(os.environ.get("RESERVE_PCT", "20") or "0")  # % of allocatable
RESERVE_CORES = float(os.environ.get("RESERVE_CORES", "0") or "0")  # absolute floor
CPU_OVERCOMMIT_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", "1.0") or "1.0")
RESERVE_SCALE_STEP = int(os.environ.get("RESERVE_SCALE_STEP", "1"))  # hosts per tick


def lambda_handler(event, context):
    _process_ttl_expirations()

    _reconcile_schedules()

    if IMAGE_REFRESH_ENABLED:
        _reconcile_image_refresh()

    now = datetime.now(timezone.utc)
    hosts = hosts_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", [])

    # R8 缩容总门:仅当开关开 且 全局无在途租户时才允许 terminate。算一次给整个 loop
    # 用(别每 host 重扫)。idle 标记/恢复无条件做,不受此门影响。
    scale_in_allowed = IDLE_RECLAIM_ENABLED and not _has_pending_tenants()

    for h in hosts:
        instance_id = h["instance_id"]
        status = h.get("status")
        vm_count = int(h.get("vm_count", 0))

        # active/回滚),scaler 一律不碰:不标 idle、不复位 active、不 terminate。金丝雀
        # 租户会让 vm_count>0,若不在此跳过,下面的 idle→active 复位会拍脏正在验证的 host。
        if status == "upgrading":
            print(f"{instance_id}: upgrading (pull-image in progress) — scaler skips")
            continue

        # host 停接 live 租户,违背"存量零影响"),所以上面那道 status 门保护不到它们。
        # 改用镜像操作 lease 判断:持有有效 lease 的 host 正在被镜像操作独占,scaler 一律
        # 不碰(不标 idle、不复位 active、不 terminate),避免与 pull/promote 并发拍脏。
        # 过期 lease 不算(可被接管),故用 image_lease_until 与当前时间比,不只看字段存在。
        if _holds_image_lease(h, now):
            print(f"{instance_id}: image operation in progress (lease held) — scaler skips")
            continue

        if vm_count > 0:
            # Has VMs — ensure active (recover from idle if tenant was assigned)
            if status == "idle":
                _set_status(instance_id, "active")
            continue

        # vm_count == 0
        if status == "active":
            idle_since = h.get("idle_since")
            if not idle_since:
                # First time seeing empty — record timestamp
                _set_idle_since(instance_id, now.isoformat())
            else:
                elapsed = (now - datetime.fromisoformat(idle_since)).total_seconds()
                if elapsed >= IDLE_TIMEOUT * 60:
                    _set_status(instance_id, "idle")
                    print(
                        f"{instance_id}: marked idle (empty for {int(elapsed / 60)}m)"
                    )

        elif status == "idle":
            # R8:idle 标记已在上面做完(host 留池)。terminate 仅在总门放行时执行——
            # 开关开(IDLE_RECLAIM_ENABLED)且全局无在途租户(_has_pending_tenants)。
            # 关闭时不 terminate,只保持 idle 态,不 requires_intervention。
            if not scale_in_allowed:
                print(
                    f"{instance_id}: idle, scale-in disabled or pending tenants exist — keeping host"
                )
                continue
            # Second round confirmation — terminate if ASG allows + 不在终止中
            if not _can_scale_in():
                print(f"{instance_id}: idle but at ASG min, skipping")
                continue
            if _lifecycle_terminating(instance_id):
                print(
                    f"{instance_id}: already terminating, skip (avoid double-decrement)"
                )
                continue
            print(f"{instance_id}: terminating idle host")
            try:
                autoscaling.terminate_instance_in_auto_scaling_group(
                    InstanceId=instance_id,
                    ShouldDecrementDesiredCapacity=True,
                )
            except Exception as e:  # noqa: BLE001 — standard retry 已在 client 层
                print(f"terminate failed: {e}")

    # Runs after idle bookkeeping so it sees current host usage. Gated OFF default.
    if RESERVE_ENABLED:
        _ensure_reserve_capacity(hosts)


def _holds_image_lease(host_item, now):
    """#394 —— 该 host 是否正被镜像操作(canary pull / promote / rollback / cleanup)独占。

    纯函数(便于单测):只看 host 记录里的 lease 字段。
    · 无 active_image_operation_id → 不持有;
    · image_lease_until <= now → 已过期(可被接管),不算持有 —— 否则一次崩溃的 pull 会
      让 scaler 永久跳过这台 host(既不标 idle 也不复位),等于静默漏管。
    """
    if not host_item.get("active_image_operation_id"):
        return False
    try:
        until = int(host_item.get("image_lease_until") or 0)
    except (TypeError, ValueError):
        return False
    return until > int(now.timestamp())


def _ensure_reserve_capacity(hosts):
    """Keep free allocatable vCPU at/above the reserve target; scale ASG out if
    below. Buffer = max(RESERVE_PCT% of total allocatable, RESERVE_CORES). Only
    counts active/idle hosts (draining/deleted don't serve new tenants)."""
    total_alloc = 0.0
    used = 0.0
    serving = 0
    for h in hosts:
        if h.get("status") not in ("active", "idle"):
            continue
        serving += 1
        total_alloc += float(h.get("total_vcpu", 0)) * CPU_OVERCOMMIT_RATIO
        used += float(h.get("used_vcpu", 0))
    free = total_alloc - used
    target = max(total_alloc * (RESERVE_PCT / 100.0), RESERVE_CORES)
    if free >= target:
        return  # buffer healthy, nothing to do
    # buffer breached → scale out one step (bounded by MaxSize)
    try:
        resp = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[ASG_NAME]
        )
        asg = resp["AutoScalingGroups"][0]
        desired, mx = asg["DesiredCapacity"], asg["MaxSize"]
        if desired >= mx:
            print(
                f"[reserve] free={free:.0f} < target={target:.0f} vCPU but ASG at MaxSize "
                f"({desired}/{mx}); cannot scale out — raise MaxSize or add a host pool."
            )
            return
        new_desired = min(mx, desired + RESERVE_SCALE_STEP)
        autoscaling.set_desired_capacity(
            AutoScalingGroupName=ASG_NAME,
            DesiredCapacity=new_desired,
            HonorCooldown=True,
        )
        print(
            f"[reserve] free={free:.0f} < target={target:.0f} vCPU "
            f"(serving {serving} hosts, alloc {total_alloc:.0f}, used {used:.0f}) "
            f"→ scale out {desired}→{new_desired}"
        )
    except Exception as e:
        print(f"[reserve] scale-out failed: {e}")


def _process_ttl_expirations():
    """Issue #15 — execute on_expiry action for tenants past their expires_at.

    Scans tenants table, finds expired non-terminal tenants, and triggers the
    configured action (stop or delete). Uses tenant.host_id + vm_num to ssm
    stop-vm.sh just like the API handler. Best-effort; errors are logged.
    """
    if tenants_table is None:
        return  # backward compat: if env var missing, no-op
    try:
        items = tenants_table.scan(
            FilterExpression="attribute_exists(expires_at)",
        ).get("Items", [])
    except Exception as e:
        print(f"ttl scan failed: {e}")
        return
    now = datetime.now(timezone.utc)
    for t in items:
        tid = t.get("id", "")
        status = t.get("status", "")
        if status in _TTL_TERMINAL:
            continue
        try:
            expires_at = datetime.fromisoformat(t["expires_at"])
        except Exception:
            continue  # malformed timestamp; skip
        if now < expires_at:
            continue
        action = t.get("on_expiry", "stop")
        host_id = t.get("host_id", "")
        if action == "stop":
            if host_id:
                vm_num = int(t.get("vm_num", 1))
                try:
                    ssm.send_command(
                        InstanceIds=[host_id],
                        DocumentName="AWS-RunShellScript",
                        Parameters={
                            "commands": [f"/home/ubuntu/stop-vm.sh {tid} {vm_num}"]
                        },
                        TimeoutSeconds=60,
                    )
                except Exception as e:
                    print(f"ttl stop SSM failed for {tid}: {e}")
            _update_tenant_status(tid, "stopped")
            print(f"ttl: stopped {tid} (expired at {t['expires_at']})")
        elif action == "delete":
            _update_tenant_status(tid, "deleted")
            print(f"ttl: deleted {tid} (expired at {t['expires_at']})")


def _update_tenant_status(tenant_id, status):
    # #501 — TTL delete 也是「把 status 写成 deleted」的路径,同样必须清健康位:health_check
    # sweep 跳过终态租户,不清就永久停在删除前的 up,把已删租户伪装成健康在役租户。只在 deleted
    # 时清(stopped 仍是可恢复态,保留最后一次观测)。
    expr = "SET #s = :s, updated_at = :t"
    if status == "deleted":
        expr += " REMOVE vm_health, app_health, last_health_check"
    try:
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=expr,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": status,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        print(f"ttl status update failed for {tenant_id}: {e}")


def _set_status(instance_id, status):
    # pull-image 置 upgrading,但 scan 快照还是 idle)→ 盲写会把正在灰度升级的 host 拍回
    # active/idle,leave upgrading_at 残留 + 打断金丝雀验证(真机实测:active + 仍 poison
    # 的 live 窗口)。加 ConditionExpression 只在 host 仍是 active/idle 时才写:host 已进
    # upgrading → CCF,跳过不动(pull 编排独占该 host 的 status,scaler 不碰)。
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET #s = :s",
            ConditionExpression="#s IN (:a, :i)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": status,
                ":a": "active",
                ":i": "idle",
            },
        )
    except hosts_table.meta.client.exceptions.ConditionalCheckFailedException:
        # host 不在 active/idle(多半正 upgrading / 已 terminating)→ scaler 不该改它的
        # status。静默跳过(下一 tick host 回 active/idle 后再正常参与 idle 回收)。
        print(
            f"{instance_id}: skip status→{status} (host not active/idle, likely upgrading)"
        )


def _set_idle_since(instance_id, ts):
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET idle_since = :t",
        ExpressionAttributeValues={":t": ts},
    )


def _can_scale_in():
    resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
    asg = resp["AutoScalingGroups"][0]
    return asg["DesiredCapacity"] > asg["MinSize"]


def _has_pending_tenants():
    """R8 缩容前置总门(对标 K8s CA:有 unschedulable pod 就整 tick 不缩,
    references.md#R8-Ref-1)。unschedulable pod = 我方 pending/creating/queued
    租户。缩到 0 时若有 pending → no-host → requires_intervention(坑源)。
    有任一在途 → 返 True,本 tick 不 terminate,把容量留给它。"""
    if tenants_table is None:
        return False
    in_flight = {"pending", "creating", "queued"}
    try:
        # 只取 status 属性,轻扫;规模大时 GSI 更优,当前 fleet 量级全扫够用
        resp = tenants_table.scan(
            ProjectionExpression="#s",
            ExpressionAttributeNames={"#s": "status"},
        )
        items = resp.get("Items", []) or []
        while resp.get("LastEvaluatedKey"):
            resp = tenants_table.scan(
                ProjectionExpression="#s",
                ExpressionAttributeNames={"#s": "status"},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items += resp.get("Items", []) or []
        return any(i.get("status") in in_flight for i in items)
    except Exception as e:  # noqa: BLE001 — fail-safe:查不出就当有 pending,宁可不缩
        print(
            f"pending-tenant gate scan failed ({e}); treating as pending (skip scale-in)"
        )
        return True


def _lifecycle_terminating(instance_id):
    """terminate 前查实例 lifecycle,已在终止中返 True 跳过,防重复 terminate 把
    ASG desired 多扣一次(对标 K8s CA auto_scaling_groups.go:353-367,
    references.md#R8-Ref-2)。查不到状态保守返 False(照旧尝试,不误跳)。"""
    try:
        resp = autoscaling.describe_auto_scaling_instances(InstanceIds=[instance_id])
        insts = resp.get("AutoScalingInstances", [])
        if not insts:
            return False
        state = insts[0].get("LifecycleState", "")
        return state.startswith("Terminating") or state == "Terminated"
    except Exception as e:  # noqa: BLE001
        print(f"lifecycle check {instance_id} failed ({e}); proceeding")
        return False


# ═══════════════════════════════════════════════════════════════════
# Tenants with a `schedule` field auto-stop outside their window and
# auto-start inside it. The check runs once per scaler tick.
# ═══════════════════════════════════════════════════════════════════

_SCHED_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _now_utc():
    """Indirection so tests can monkey-patch time."""
    return datetime.now(timezone.utc)


def _schedule_should_run(sched, now_utc):
    """Return True iff the schedule says the tenant should be running at
    `now_utc`. Pure function — easy to unit-test."""
    if not sched:
        return True
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        return True
    tz_name = sched.get("timezone", "UTC")
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        return True
    day_name = _SCHED_DAY_NAMES[local.weekday()]
    days = sched.get("days") or list(_SCHED_DAY_NAMES)
    if day_name not in days:
        return False
    start = sched.get("start", "00:00")
    stop = sched.get("stop", "23:59")
    cur = local.strftime("%H:%M")
    # Same-day window only (no wrap-around at midnight).
    if start <= stop:
        return start <= cur < stop
    # Wrap-around: start > stop means active across midnight.
    return cur >= start or cur < stop


def _reconcile_schedules():
    """Walk scheduled tenants and start/stop to match window."""
    if tenants_table is None:
        return
    items = tenants_table.scan().get("Items", []) or []
    now = _now_utc()
    for it in items:
        sched = it.get("schedule")
        if not sched:
            continue
        tid = it.get("id")
        status = it.get("status")
        host_id = it.get("host_id")
        if not host_id:
            continue
        should_run = _schedule_should_run(sched, now)
        if should_run and status == "stopped":
            vm_num = int(it.get("vm_num", 1))
            vcpu = int(it.get("vcpu", 2))
            mem_mb = int(it.get("mem_mb", 4096))
            # 老版本只填 4 位,CHAT_EP_ENABLED 恒空 → scheduler 唤醒后开关不生效。
            # 位 5-9/11 空占位(launch-vm 自 special-case ""),数据盘保留一次性字段。
            cee = bool(it.get("chat_endpoint_enabled", False))
            chat_ep_arg = "1" if cee else "0"
            launch_cmd = (
                f"/home/ubuntu/launch-vm.sh {tid} {vm_num} {vcpu} {mem_mb} "
                f'"" "" "" "" "" {chat_ep_arg} ""'
            )
            ssm.send_command(
                InstanceIds=[host_id],
                DocumentName="AWS-RunShellScript",
                Parameters={
                    "commands": [launch_cmd],
                },
            )
            tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "running"},
            )
        elif (not should_run) and status == "running":
            vm_num = int(it.get("vm_num", 1))
            ssm.send_command(
                InstanceIds=[host_id],
                DocumentName="AWS-RunShellScript",
                Parameters={
                    "commands": [
                        f"/home/ubuntu/stop-vm.sh {tid} {vm_num}",
                    ]
                },
            )
            tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "stopped"},
            )


# ═══════════════════════════════════════════════════════════════════
#
# Goal: every running tenant lands on the current golden image within
# REFRESH_INTERVAL_HOURS, by REBUILD (never hot-patch). A tenant carries its
# image version in `image_version` (falls back to the host's rootfs_version it
# was created on) and a `last_refresh` timestamp. When it falls behind the
# current manifest version AND last_refresh is older than the interval, we
# rebuild it onto a host that already runs the new image.
#
# Path (balloon is on, so NO live snapshot migration — use backup/restore, the
# same path the API recommends):
#   1. backup-data.sh on the source host → s3://bucket/backups/{tid}/...
#   2. launch-vm.sh on a NEW-IMAGE host with RESTORE_KEY → restore data disk
#   3. (caller/health_check repoints ALB + verifies) then stop old VM
#
# This function only INITIATES one refresh per eligible tenant per tick (capped
# by REFRESH_MAX_PER_TICK) by backing up + relaunching on the new host and
# flipping the tenant's host_id once relaunch is submitted. It is intentionally
# conservative: gated by IMAGE_REFRESH_ENABLED and capped, so a bad image can't
# stampede the fleet.
# ═══════════════════════════════════════════════════════════════════

# 发 rebuild/refresh SSM,与 delete 抢 SSM)。
_REFRESH_SKIP_STATUS = {
    "stopped",
    "deleted",
    "failed",
    "creating",
    "migrating",
    "deleting",
    # refresh 链会强制 launch 它到新镜像 = 破坏 suspended 态 + 可能空盘启动。在途态更不能碰。
    "suspending",
    "suspended",
    "restoring",
}


def _current_golden_version():
    """Read the current golden image version from the rootfs manifest in S3.
    Returns "" if unavailable (caller then skips refresh — fail safe)."""
    if not ASSETS_BUCKET:
        return ""
    try:
        obj = s3.get_object(Bucket=ASSETS_BUCKET, Key=f"{ROOTFS_PREFIX}/manifest.json")
        manifest = json.loads(obj["Body"].read())
        return str(manifest.get("version", ""))
    except Exception as e:
        print(f"image_refresh: cannot read manifest version: {e}")
        return ""


def _should_refresh_image(tenant, golden_version, now, interval_hours):
    """Pure predicate — True iff this tenant should be rolled onto the new
    image now. Pure so it unit-tests without AWS. Rules:
      - golden_version must be known (non-empty)
      - tenant status not terminal/in-flight
      - tenant's image_version (fallback rootfs_version) != golden_version
      - last_refresh (fallback created_at) older than interval_hours
    """
    if not golden_version:
        return False
    if tenant.get("status") in _REFRESH_SKIP_STATUS:
        return False
    # already mid-refresh? leave it to the in-flight advance
    if tenant.get("image_refresh_phase"):
        return False
    # 固定)不参与自动 image refresh。scaler 的 golden 来自 S3 manifest 活指针;活指针改写
    # (发新镜像/运维回滚)会让这些固定租户被判"落后"→ 发起跨 host backup+relaunch 迁移 →
    # 写 image_refresh_phase 后永久卡在该态(既没升也不再被处理)。显式固定的版本不该被自动
    # 改写(与 launch-vm fail-closed 读租户 image_snapshot_time 一致)。
    if (tenant.get("image_snapshot_time") or "").strip():
        return False
    cur = tenant.get("image_version") or tenant.get("rootfs_version") or ""
    if cur == golden_version:
        return False
    stamp = tenant.get("last_refresh") or tenant.get("created_at") or ""
    if not stamp:
        # no timestamp to reason about — treat as eligible (it predates the
        # field), but only if version is genuinely behind (checked above).
        return True
    try:
        age_h = (now - datetime.fromisoformat(stamp)).total_seconds() / 3600.0
    except Exception:
        return True
    return age_h >= interval_hours


def _find_new_image_host(golden_version, vcpu, mem_mb, exclude_host_id):
    """Pick an active host already running golden_version with capacity for
    (vcpu, mem_mb). Returns the host item or None. Same allocatable formula as
    the API's _find_host (reserved headroom already baked into total_*)."""
    hosts = hosts_table.scan(
        FilterExpression="#s = :a",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active"},
    ).get("Items", [])
    best = None
    for h in hosts:
        if h["instance_id"] == exclude_host_id:
            continue
        if str(h.get("rootfs_version", "")) != golden_version:
            continue
        free_vcpu = int(h.get("total_vcpu", 0)) - int(h.get("used_vcpu", 0))
        free_mem = int(h.get("total_mem_mb", 0)) - int(h.get("used_mem_mb", 0))
        if free_vcpu >= vcpu and free_mem >= mem_mb:
            # prefer the emptiest host to spread load
            score = free_vcpu
            if best is None or score > best[0]:
                best = (score, h)
    return best[1] if best else None


def _reconcile_image_refresh():
    """Initiate up to REFRESH_MAX_PER_TICK seamless tenant rebuilds onto the
    current golden image. Conservative + idempotent: marks image_refresh_phase
    so a tenant isn't double-initiated across ticks."""
    if tenants_table is None:
        return
    golden = _current_golden_version()
    if not golden:
        return  # fail safe — no manifest, no refresh
    now = _now_utc()
    items = tenants_table.scan().get("Items", []) or []
    started = 0
    for t in items:
        if started >= REFRESH_MAX_PER_TICK:
            break
        if not _should_refresh_image(t, golden, now, REFRESH_INTERVAL_HOURS):
            continue
        tid = t.get("id")
        src_host = t.get("host_id", "")
        if not src_host:
            continue
        vcpu = int(t.get("vcpu", 2))
        mem_mb = int(t.get("mem_mb", 4096))
        target = _find_new_image_host(golden, vcpu, mem_mb, src_host)
        if target is None:
            # No new-image host with capacity yet. ASG rolling-replace will
            # bring one; we retry next tick. Log so operators see the wait.
            print(
                f"image_refresh: {tid} eligible (cur="
                f"{t.get('image_version') or t.get('rootfs_version')} → {golden}) "
                f"but no new-image host with capacity; deferring"
            )
            continue
        target_host = target["instance_id"]
        target_ip = target.get("private_ip", "")
        # 1) back up the tenant's data disk to S3 (synchronous SSM).
        backup_cmd = (
            f"/home/ubuntu/backup-data.sh {tid} {ASSETS_BUCKET} {BACKUP_PREFIX}"
        )
        try:
            ssm.send_command(
                InstanceIds=[src_host],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [backup_cmd]},
                TimeoutSeconds=300,
            )
        except Exception as e:
            print(f"image_refresh: backup submit failed for {tid}: {e}")
            continue
        # 2) mark the tenant mid-refresh with the chosen target so a later
        # tick / health_check can advance restore→repoint→drop-old. We DO NOT
        # destroy the source VM here — that only happens after the new VM is
        # verified reachable (handled by the migration/refresh advancer).
        restore_key = f"{BACKUP_PREFIX}/{tid}/data.ext4.gz"
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET image_refresh_phase = :p, image_refresh_target = :th, "
                "image_refresh_target_ip = :tip, image_refresh_to = :ver, "
                "image_refresh_restore_key = :rk, image_refresh_src = :sh, "
                "image_refresh_started_at = :t, updated_at = :t"
            ),
            ExpressionAttributeValues={
                ":p": "backup",
                ":th": target_host,
                ":tip": target_ip,
                ":ver": golden,
                ":rk": restore_key,
                ":sh": src_host,
                ":t": now.isoformat(),
            },
        )
        started += 1
        print(
            f"image_refresh: initiated {tid} {src_host}→{target_host} "
            f"(→ image {golden}), backup submitted"
        )
