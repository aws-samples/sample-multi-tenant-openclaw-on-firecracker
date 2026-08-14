# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Health check Lambda — watchdog + AZ-level failover orchestrator.

Runs every 5 minutes (configurable). Two responsibilities:

1) Per-host watchdog (since 1.0)
   * Detect tenants whose host-agent has stopped writing health updates.
   * If ALL tenants on a host go stale at once, restart host-agent via SSM.

2) AZ-level failover (since 1.3.0)
   * If every host in an AZ has been continuously stale for at least
     ``unhealthy_threshold_minutes``, treat that AZ as unavailable.
   * Pick a target AZ that still has at least one healthy host with
     spare vCPU capacity.
   * Re-launch each running tenant on the target host. The source host
     is unreachable in an AZ outage, so we cannot live-migrate;
     instead we boot a fresh VM (from the latest backup if one exists).
   * Each per-AZ event is rate-limited by ``cooldown_minutes`` to
     prevent flapping.

The AZ failover path is implemented as pure functions plus a thin AWS
shell so it can be unit-tested without DDB/SSM/SNS access.
"""

import os
import json
import shlex
import time
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timezone

ddb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")
sns = boto3.client("sns")
s3 = boto3.client("s3")
elbv2 = boto3.client("elbv2")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

# Optional tables / topics — feature-flag friendly.
_AUDIT_TABLE_NAME = os.environ.get("AUDIT_TABLE", "")
audit_table = ddb.Table(_AUDIT_TABLE_NAME) if _AUDIT_TABLE_NAME else None
_SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

STALE_SECONDS = 120  # No health update for 2 min → agent may be down
RESTART_COOLDOWN_SECONDS = 600  # Don't restart agent more than once per 10 min
# A tenant stuck in `creating` past this never became healthy (launch failed /
# channel never registered / killed mid-boot). It still holds its host capacity
# reservation (used_vcpu/used_mem_mb/vm_count), so leaked creating rows silently
# exhaust a host's LEDGER (scheduler sees "full" and 503s new launches) while the
# real RAM is free. The 158 `lt-*` load-test zombies that wedged 300/500 load
# testing on 2026-06-29 were exactly this. Reap them: mark failed + release the
# slot so the ledger reflects reality. 15 min is comfortably past the ~6s p50
# creating→running so a genuinely-slow boot is never reaped prematurely.
CREATING_TIMEOUT_SECONDS = int(os.environ.get("CREATING_TIMEOUT_SECONDS", "900"))

# AZ failover configuration (read from env, populated by stack.py).
AZ_FAILOVER_ENABLED = os.environ.get("AZ_FAILOVER_ENABLED", "false").lower() == "true"
AZ_UNHEALTHY_THRESHOLD_MINUTES = int(
    os.environ.get("AZ_UNHEALTHY_THRESHOLD_MINUTES", "10")
)
AZ_COOLDOWN_MINUTES = int(os.environ.get("AZ_COOLDOWN_MINUTES", "30"))
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
# 1.4.2 (#fake-failover fix): public URL of the ALB (or CloudFront domain
# in single-domain mode, or app domain in dual-domain mode) used to
# cross-verify that a tenant's dashboard is genuinely reachable through
# the public path before flipping DDB to status=running. Empty string
# disables the gate, in which case the legacy 1.3.x behavior applies
# (and operators get a CloudWatch warning each failover).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")


def lambda_handler(event, context):
    """Scan running tenants, recover host-agent if needed, then check AZ-level health."""
    _stop_confirm_budget["n"] = 0  # #412 blocker-B:每次 invocation 重置 stop-confirm 预算
    tenants = tenants_table.scan(
        FilterExpression=(
            "#s = :r AND (attribute_not_exists(synthetic) OR synthetic <> :true)"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "running", ":true": True},
    ).get("Items", [])

    now = datetime.now(timezone.utc)
    stale_count = 0
    stale_by_host = {}  # host_id → [tenant_ids]

    for tenant in tenants:
        tid = tenant["id"]
        last_check = tenant.get("last_health_check", "")

        if last_check:
            try:
                elapsed = (now - datetime.fromisoformat(last_check)).total_seconds()
                if elapsed < STALE_SECONDS:
                    continue
            except Exception as e:
                # default) but leave a trail so we can spot ISO drift.
                print(f"stale-check: bad last_check for {tid}: {last_check!r} ({e})")

        stale_count += 1
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression="SET vm_health = :vh, app_health = :ah",
            ExpressionAttributeValues={":vh": "stale", ":ah": "unknown"},
        )
        host_id = tenant.get("host_id", "")
        if host_id:
            stale_by_host.setdefault(host_id, []).append(tid)
        print(f"stale: {tid} on {host_id} (last_check={last_check})")

    # Recover: if ALL tenants on a host are stale, host-agent is likely down
    recovered = 0
    for host_id, tids in stale_by_host.items():
        host_tenants = [t for t in tenants if t.get("host_id") == host_id]
        if len(tids) < len(host_tenants):
            continue  # Some tenants still healthy → agent is alive, individual VM issue

        if _restart_host_agent(host_id, now):
            recovered += 1

    if stale_count:
        print(
            f"watchdog: {stale_count} stale tenant(s), {recovered} host-agent restart(s)"
        )

    # ------- rebuild 对账(ADR §5.4a/§5.5)——让 unconfirmed 自己收敛 -------
    # 放在 stale 扫描之后、reaper 之前:此时 host-agent 刚上报过的实际版本已经在表里,
    # 对账拿到的是最新一手数据。与容量 reaper 无耦合(只动 rebuild_* 字段和 rootfs_version,
    # 不碰 status/容量账本),顺序不影响正确性,仅影响数据新鲜度。
    try:
        _rb_done, _rb_failed, _rb_left = _reconcile_unconfirmed_rebuilds(now)
        if _rb_done or _rb_failed or _rb_left:
            print(
                f"rebuild-reconcile: {_rb_done} → done, {_rb_failed} → failed, "
                f"{_rb_left} still unconfirmed"
            )
    except Exception as e:
        # 与其他 sweep 同约定:对账永远不该拖垮 watchdog 本身。
        print(f"rebuild-reconcile error (non-fatal): {e}")

    # ------- Reap stuck `creating` tenants (capacity-ledger anti-drift) -------
    # Tenants that never left `creating` past CREATING_TIMEOUT_SECONDS are dead
    # launches still holding a host capacity reservation. Reaping releases the
    # slot so the scheduler's ledger reflects real free RAM (see constant above).
    try:
        reaped = _reap_stuck_creating(now)
        if reaped:
            print(f"reap: released {reaped} stuck creating tenant(s)")
    except Exception as e:
        # Never let ledger reaping take down the watchdog.
        print(f"reap error (non-fatal): {e}")

    # dispatch 失败超预算的租户被 poller 转 requires_intervention(非 creating),逃出上面
    # 只扫 creating 的 reaper。若它仍带 capacity_reservation_id,那份容量就永久搁浅。此扫把
    # 【requires_intervention 且仍持令牌】的租户令牌化释放(扣 host + 删令牌一个事务),兜底
    # 任何令牌路径的残留(reserve 提交后崩溃转 RI、释放 retry 反复失败等)。
    try:
        orphans = _reap_orphan_reservations(now)
        if orphans:
            print(f"reap: released {orphans} orphan reservation token(s)")
    except Exception as e:
        print(f"reap orphan-token error (non-fatal): {e}")

    # ------- AZ-level failover (1.3.0) -------
    if AZ_FAILOVER_ENABLED:
        try:
            failover_summary = _check_and_handle_az_failover(now, tenants)
            if failover_summary["az_outages_detected"]:
                print(f"az_failover: {json.dumps(failover_summary)}")
        except Exception as e:
            # AZ failover failures must NEVER take down the watchdog.
            print(f"az_failover error (non-fatal): {e}")

    # POST /tenants/{id}/migrate is async: it fires the snapshot SSM command,
    # marks the tenant `migrating` with the async context, and returns 202
    # (API Gateway caps a synchronous request at 29s, far less than a multi-GB
    # snapshot+restore). This sweep is the out-of-band driver that advances
    # each in-flight migration: poll the snapshot command → trigger restore →
    # verify the dashboard → flip host_id/counters/routing → running. A failure
    # at any step (or a watchdog timeout) rolls status back to running with
    # host_id untouched, so the tenant is never stranded. `migrating` tenants
    # are NOT in the `running` scan above, so query them separately.
    try:
        migrating = tenants_table.scan(
            FilterExpression="#s = :m",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":m": "migrating"},
        ).get("Items", [])
        for tenant in migrating:
            # Defensive re-filter: only advance tenants that are *actually*
            # migrating with an in-flight phase. Guards against a scan that
            # over-returns (and keeps unit tests that stub scan with a single
            # return_value from accidentally feeding running tenants here).
            if tenant.get("status") != "migrating" or not tenant.get("migration_phase"):
                continue
            try:
                _advance_migration(tenant, now)
            except Exception as e:
                # One stuck migration must not break the others or the watchdog.
                print(f"_advance_migration error for {tenant.get('id')}: {e}")
    except Exception as e:
        print(f"migration sweep scan error (non-fatal): {e}")


# Watchdog: a migration that hasn't reached a terminal state within this many
# minutes is force-rolled-back to `running` (the source VM is still there).
MIGRATION_WATCHDOG_MINUTES = int(os.environ.get("MIGRATION_WATCHDOG_MINUTES", "15"))

# R6 阶段3 drain 窗口:阶段2 切流(Redis+ALB 指 target)成功后,源旧 DNAT 保留这么
# 多秒让在途老流量走完,下一轮 sweep 才拆源(不在单次 invocation sleep——Lambda
# 时长贵且脆,drain 靠 migration_committed_at 时间戳跨 sweep 判定,天然可重入)。
MIGRATION_DRAIN_SECONDS = int(os.environ.get("MIGRATION_DRAIN_SECONDS", "5"))
LIFECYCLE_FENCE_LEASE_SECONDS = int(
    os.environ.get("LIFECYCLE_FENCE_LEASE_SECONDS", "1800")
)


# stop-confirm 有上限。每条 stop-vm 阻塞至多 STOP_CONFIRM_TIMEOUT 秒;若不设上限,数台不可达
# host 上的卡住租户会串行耗光整个 invocation(默认 180s),把后面的 orphan 清扫 / AZ failover /
# 迁移 sweep 全饿死。超预算则本轮不再 confirm(返 False = 不释放,留下轮再试),不是错误。
_REAP_STOP_CONFIRM_MAX = int(os.environ.get("REAP_STOP_CONFIRM_MAX", "6"))
STOP_CONFIRM_TIMEOUT = int(os.environ.get("STOP_CONFIRM_TIMEOUT", "20"))
_stop_confirm_budget = {"n": 0}  # 每次 lambda_handler 顶部重置(warm invocation 复用模块态)


def _is_conditional_failure(exc):
    if type(exc).__name__ == "ConditionalCheckFailedException":
        return True
    ccf = tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    if isinstance(ccf, type) and isinstance(exc, ccf):
        return True
    response = getattr(exc, "response", None)
    return (
        isinstance(response, dict)
        and (response.get("Error") or {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _migration_fence(tenant):
    op_id = tenant.get("migration_lifecycle_op_id")
    epoch = tenant.get("migration_lifecycle_fence_epoch")
    if not op_id or epoch is None:
        return None
    return str(op_id), int(epoch)


def _migration_fence_condition(tenant, phase=None):
    op_id, epoch = _migration_fence(tenant)
    now_epoch = int(time.time())
    condition = (
        "#s = :migrating AND active_lifecycle_op_id = :lf_op AND "
        "lifecycle_fence_epoch = :lf_epoch AND "
        "migration_lifecycle_op_id = :lf_op AND "
        "migration_lifecycle_fence_epoch = :lf_epoch AND "
        "active_lifecycle_until > :lf_now"
    )
    values = {
        ":migrating": "migrating",
        ":lf_op": op_id,
        ":lf_epoch": epoch,
        ":lf_now": now_epoch,
    }
    if phase is not None:
        condition += " AND migration_phase = :lf_phase"
        values[":lf_phase"] = phase
    return condition, values


def _renew_migration_fence(tenant):
    fence = _migration_fence(tenant)
    if fence is None:
        print(
            f"migration {tenant.get('id')}: missing lifecycle fence; "
            "refusing to advance legacy in-flight migration"
        )
        return False
    condition, values = _migration_fence_condition(
        tenant, tenant.get("migration_phase")
    )
    values[":lf_until"] = int(time.time()) + LIFECYCLE_FENCE_LEASE_SECONDS
    values[":lf_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        tenants_table.update_item(
            Key={"id": tenant["id"]},
            UpdateExpression=(
                "SET active_lifecycle_until = :lf_until, "
                "lifecycle_lease_updated_at = :lf_updated"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if not _is_conditional_failure(exc):
            raise
        print(
            f"migration {tenant.get('id')}: lifecycle fence superseded or expired; "
            "refusing to advance"
        )
        return False
    tenant["active_lifecycle_until"] = values[":lf_until"]
    return True


def _release_migration_fence(tenant):
    fence = _migration_fence(tenant)
    if fence is None:
        return False
    op_id, epoch = fence
    try:
        tenants_table.update_item(
            Key={"id": tenant["id"]},
            UpdateExpression=(
                "SET lifecycle_released_at = :lf_updated "
                "REMOVE active_lifecycle_op_id, active_lifecycle_action, "
                "active_lifecycle_until"
            ),
            ConditionExpression=(
                "active_lifecycle_op_id = :lf_op AND "
                "lifecycle_fence_epoch = :lf_epoch"
            ),
            ExpressionAttributeValues={
                ":lf_op": op_id,
                ":lf_epoch": epoch,
                ":lf_updated": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        if not _is_conditional_failure(exc):
            raise
        return False
    return True


def _migration_host_guard(tenant):
    op_id, epoch = _migration_fence(tenant)
    table = os.environ["TENANTS_TABLE"]
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "")
    key = shlex.quote(json.dumps({"id": {"S": tenant["id"]}}))
    q = shlex.quote
    read_cmd = (
        f"aws dynamodb get-item --table-name {q(table)} --region {q(region)} "
        f"--key {key} --consistent-read "
        '--query "[Item.active_lifecycle_op_id.S,'
        "Item.lifecycle_fence_epoch.N,Item.active_lifecycle_until.N]\" "
        "--output text 2>/dev/null"
    )
    return (
        f'_LF=$({read_cmd}) || _LF=""; '
        f'if [ -z "$_LF" ]; then _LF=$({read_cmd}) || _LF=""; fi; '
        '[ -n "$_LF" ] || { echo "LIFECYCLE_FENCE_READ_FAILED" >&2; exit 78; }; '
        '_LF_OWNER=$(printf "%s" "$_LF" | cut -f1); '
        '_LF_EPOCH=$(printf "%s" "$_LF" | cut -f2); '
        '_LF_UNTIL=$(printf "%s" "$_LF" | cut -f3); '
        f'[ "$_LF_OWNER" = {q(op_id)} ] || '
        '{ echo "LIFECYCLE_SUPERSEDED owner=$_LF_OWNER" >&2; exit 79; }; '
        f'[ "$_LF_EPOCH" = {q(str(epoch))} ] || '
        '{ echo "LIFECYCLE_SUPERSEDED epoch=$_LF_EPOCH" >&2; exit 79; }; '
        '[ -n "$_LF_UNTIL" ] && [ "$_LF_UNTIL" -gt "$(date +%s)" ] || '
        '{ echo "LIFECYCLE_FENCE_EXPIRED" >&2; exit 79; }'
    )


def _confirm_vm_stopped(host_id, tenant_id, vm_num):
    """#412 blocker-B(reviewer 复现):reaper 在【释放容量/消费令牌之前】必须先确认该租户的
    VM 确已停止,否则会释放一个仍在跑的 Firecracker 的容量记账 → 未记账活 VM(overcommit,
    账本红线)。launch-vm.sh 明确在失败后可能保留活 FC(:345),故"卡 creating/requires_
    intervention 超时"不等于"VM 已停"。这里走与 delete 同款权威停机:同步跑 stop-vm.sh(幂等,
    已停的 VM 再停是 no-op success),SSM 回执 Success 才算确认。stop-vm 失败/未确认 → 返 False,
    调用方本轮【不释放】,留容量账本原样,下轮 reaper 再试(时序不替代正确性:确认了才扣)。

    调用前必须已【围栏】该行(flip 到 failed 且条件锁 creating+令牌),使正在 promote 的 running
    租户 CCF 出局——否则会 stop 掉一个刚起来的活 VM(codex #2)。host_id 缺失 → False,不释放。
    tenant_id/vm_num 经 shlex.quote 进 root shell(纵深防御,与 delete 同)。每次 invocation 的
    stop-confirm 数受 _REAP_STOP_CONFIRM_MAX 限,超额直接返 False(不烧 SSM),防饿死后续工作。"""
    if not host_id:
        return False
    if _stop_confirm_budget["n"] >= _REAP_STOP_CONFIRM_MAX:
        print(f"reap: stop-confirm budget ({_REAP_STOP_CONFIRM_MAX}) exhausted — deferring "
              f"{tenant_id} to next sweep (avoid starving orphan/failover/migration work)")
        return False
    _stop_confirm_budget["n"] += 1
    _q_tid = shlex.quote(str(tenant_id))
    _q_vm = shlex.quote(str(int(vm_num)))
    ok, _out = _ssm_run_capture(
        host_id, f"/home/ubuntu/stop-vm.sh {_q_tid} {_q_vm}", timeout=STOP_CONFIRM_TIMEOUT
    )
    if not ok:
        print(f"reap: {tenant_id} on {host_id} stop-vm unconfirmed — NOT releasing this "
              f"cycle (avoid releasing a possibly-live VM's capacity); retry next sweep")
    return ok


# ═════════ rebuild 对账 — ADR-rebuild-idempotency-sync-contract §5.4a/§5.5 ═════════
# 让 `rebuild_status=unconfirmed` 自己收敛,不再是终点。
#
# 背景:rebuild 的采用校验依赖 SSM 回执,真机上约 1/3 的 rebuild 是「VM 真重启并升级了,
# 但回执没在超时内回来」。控制面据此标 unconfirmed(而不是谎报 failed 去引导客户重试 ——
# 重试会再删一次 overlay、抹掉两次之间的写入)。但 unconfirmed 只是诚实,不是答案。
#
# 两条事后对账把它变成答案,且都零新增基础设施:
#   路 1 — 回头问 SSM。命令执行记录在服务端留 30 天;「超时」只是控制面不想再等,不是记录
#          消失。api Lambda 已在受理那一刻把 CommandId 落进 rebuild_ssm_command_id。
#   路 2 — 看宿主机上报的实际运行版本(observed_image_snapshot_time,host-agent 每 15s 从
#          该租户 vm.json 带上来)。期望 == 实际即确认成功,完全不依赖那次 SSM 调用的成败。
#
# 判定矩阵(ADR §5.4a):
#   | SSM 回音      | 宿主机上报版本 | 结论 |
#   | Success       | 任意/无        | done   |
#   | 超时/查不到    | == 目标        | done   ← 这就是那 1/3,由路 2 救回 |
#   | Failed        | != 目标        | failed(双证,可安全重试) |
#   | 超时          | != 目标/无     | 仍 unconfirmed,留下轮;超 REBUILD_UNCONFIRMED_
#                                     TIMEOUT 后转 failed(§5.5 兜底,不无限悬着) |
#
# 三条必须遵守的约束,全部落在下面代码里:
#   1. 只比【具体版本值】,不比「变了没有」。只看「变了」会把普通 restart/recovery 误判成
#   2. 路 2 只能【确认成功】,不能【判定失败】。launch-vm 写 vm.json 是 best-effort,且记的
#      是「启动时打算用哪个版本」而非「进程真挂着哪个 rootfs」。宽松方向安全,严格方向误杀。
#   3. 迟到的确认必须【条件写】,绝不 restamp。所有更新都绑 rebuild_op_id 仍是当次那个,
#      否则一次晚到的成功回音会盖掉之后一次【新】操作的结果。与 fence ADR §4 同一条不变量,
#      本阶段就必须遵守,不能等 fence 落地。

# 兜底超时(§5.5):两条对账都长时间给不出结论的 unconfirmed,不能永远悬着 —— 客户无法判断
# 能否重试,租户也不会被任何巡检收拾。超过这个时长转 failed(明确"可安全重试"),并留下
# reason 说明是兜底判定而非观测到失败。默认 30 分钟:远大于一次 rebuild 的正常收敛时间
# (真机约 15s ~ 数分钟),又不至于让客户等一整天。
REBUILD_UNCONFIRMED_TIMEOUT_SECONDS = int(
    os.environ.get("REBUILD_UNCONFIRMED_TIMEOUT_SECONDS", "1800")
)


def _parse_iso(ts):
    """把 DDB 里的 ISO 时间串解析成 aware datetime;失败返 None(调用方自行兜底)。

    与 _reap_stuck_creating 同款归一化:fromisoformat(3.11 前)吃不下结尾的 'Z',
    naive 时间也没法跟 aware 的 now 相减。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def _rebuild_verdict(tenant, now):
    """对一个 unconfirmed 的租户做判定,返回 (status, phase, reason) 或 None(留在 unconfirmed)。

    纯函数:不碰 DDB、不发 SSM,SSM 回音以 ssm_done/ssm_ok 形式由调用方查好传进来
    (放在 tenant 的两个私有键里)。这样判定矩阵本身可被单测穷举,不需要 mock AWS。
    """
    target = (tenant.get("rebuild_target_snapshot_time") or "").strip()
    observed = (tenant.get("observed_image_snapshot_time") or "").strip()
    ssm_done = tenant.get("_ssm_done")
    ssm_ok = tenant.get("_ssm_ok")

    # 路 1 命中成功:SSM 明确说 Success → 确认成功。
    if ssm_done and ssm_ok:
        return (
            "done",
            "done",
            "confirmed by SSM invocation record (receipt arrived late; "
            "the rebuild had in fact succeeded on the host)",
        )

    # 路 2 命中成功:需要【两个】条件同时成立 —— 版本相符 **且** 这个 Firecracker 进程是
    # 本次 rebuild 之后新起的。
    #
    # 为什么不能只看版本(reviewer 抓到的假成功):launch-vm.sh 在起 firecracker 之前 800+
    # 行就把版本写进了 vm.json,所以"版本==目标"只证明启动流程走到了那一行。两种假成功:
    #   ① 中途失败 → 版本相符但 VM 没起(host-agent 侧已用 vm_health=up 挡掉,不再上报);
    #   ② 旧 FC 没被 stop-vm 杀掉 → VM ping 得通、vm.json 已被改成新版本,但进程还挂着
    #      【旧】rootfs。这种只能靠进程启动时刻识别:它早于本次 rebuild 的发起时刻。
    # 故要求 observed_boot_at > rebuild_started_at。没有启动证据(读不到 /proc → 空串)时
    # **不判 done**,保守留在 unconfirmed 等路 1 或兜底 —— 宁可多等,不可谎报成功。
    if target and observed and observed == target:
        boot_at = _parse_iso(tenant.get("observed_boot_at"))
        started = _parse_iso(tenant.get("rebuild_started_at"))
        if boot_at and started and boot_at >= started:
            return (
                "done",
                "done",
                "confirmed by host: running image version matches the rebuild target "
                f"({target}) AND the Firecracker process was (re)started at "
                f"{tenant.get('observed_boot_at')}, after this rebuild began",
            )
        # 版本相符但没有"本次新起"的证据 → 不下 done。这里刻意不 return,继续往下走:
        # 路 1 的 SSM 回音仍可能给出结论,超时兜底也仍适用。

    # 路 1 命中失败:SSM 明确 Failed/TimedOut/Cancelled → 确认失败,可安全重试。
    # 注意这里【只信 SSM】,不因 observed != target 就判失败(约束 2:路 2 不可判失败 ——
    # vm.json 是 best-effort 写,缺失或滞后都不代表没升成)。
    if ssm_done and not ssm_ok:
        return (
            "failed",
            "failed",
            "SSM invocation record reports the rebuild command failed on the host; "
            "rootfs_version not advanced — retrying the rebuild is safe",
        )

    # 兜底(§5.5):两条路都给不出结论,且已经悬了太久 → 转 failed。
    # 说"可安全重试"是权衡后的选择:悬着不动客户什么也做不了,而这个时长之后 VM 若真升成了,
    # 宿主机早该把版本报上来(每 15s 一次)。reason 里明确写这是超时兜底、非观测到失败。
    started = _parse_iso(tenant.get("rebuild_started_at"))
    if started and (now - started).total_seconds() >= REBUILD_UNCONFIRMED_TIMEOUT_SECONDS:
        mins = REBUILD_UNCONFIRMED_TIMEOUT_SECONDS // 60
        return (
            "failed",
            "failed",
            f"no confirmation from either the SSM invocation record or the host's "
            f"reported image version within {mins} minutes; giving up on "
            "reconciliation. This is a timeout verdict, NOT an observed failure — "
            "verify the tenant's actual version before retrying",
        )

    return None  # 还有希望,留在 unconfirmed 下轮再看


def _reconcile_unconfirmed_rebuilds(now):
    """扫 rebuild_status=unconfirmed 的租户,用两条对账把它们推向 done/failed。

    返回 (done_count, failed_count, still_unconfirmed_count)。

    写入受两层保护:
      * ConditionExpression 绑 rebuild_op_id 仍是当次那个 + rebuild_status 仍是
        unconfirmed(约束 3)。若这期间客户又发了一次 rebuild,op_id 已变 → CCF 出局,
        绝不用旧结论盖新操作。
      * 确认成功时才补标 rootfs_version,且只在有具体目标版本时补 —— 不编造版本号。
    """
    items = []
    lek = None
    while True:
        kw = {
            "FilterExpression": "rebuild_status = :u",
            "ExpressionAttributeValues": {":u": "unconfirmed"},
        }
        if lek:
            kw["ExclusiveStartKey"] = lek
        page = tenants_table.scan(**kw)
        items += page.get("Items", [])
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break

    if not items:
        return 0, 0, 0

    print(f"rebuild-reconcile: {len(items)} unconfirmed tenant(s) to check")
    n_done = n_failed = n_left = 0
    for t in items:
        tid = t["id"]
        # 路 1:有 CommandId 就回头问 SSM。没有(老记录/下发前就失败)则只靠路 2 与兜底。
        cmd_id = (t.get("rebuild_ssm_command_id") or "").strip()
        host_id = (t.get("host_id") or "").strip()
        if cmd_id and host_id:
            t["_ssm_done"], t["_ssm_ok"] = _poll_ssm(cmd_id, host_id)

        verdict = _rebuild_verdict(t, now)
        if verdict is None:
            n_left += 1
            continue
        status, phase, reason = verdict

        expr = (
            "SET rebuild_status = :s, rebuild_phase = :p, "
            "rebuild_failed_reason = :r, updated_at = :t"
        )
        vals = {
            ":s": status,
            ":p": phase,
            ":r": reason,
            ":t": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ":u": "unconfirmed",
        }
        # 确认成功且知道具体目标版本 → 补标版本(此前因未确认而没标,GET /tenants 一直
        # 显示旧版本)。target 为空时不补:没有具体值可写,绝不编造。
        target = (t.get("rebuild_target_snapshot_time") or "").strip()
        if status == "done" and target:
            expr += ", rootfs_version = :rv"
            vals[":rv"] = target
            if len(target.encode("utf-8")) <= 256:
                expr += ", q_rootfs_version = :qrv"
                vals[":qrv"] = target

        # 约束 3 —— 绑当次 op_id + 仍是 unconfirmed。op_id 缺失(老记录)时退化为只绑
        # status,仍能防"已被别的路径改成 done/failed 后又被本轮覆盖"。
        cond = "rebuild_status = :u"
        op_id = (t.get("rebuild_op_id") or "").strip()
        if op_id:
            cond += " AND rebuild_op_id = :o"
            vals[":o"] = op_id
        fence_epoch = t.get("rebuild_lifecycle_fence_epoch")
        if fence_epoch is not None:
            # A released owner may be reconciled only while its monotonic epoch
            # is still the latest tenant lifecycle epoch. Any later lifecycle
            # claim increments the epoch and permanently invalidates this stale
            # rebuild result, even after that newer owner releases its lease.
            cond += " AND lifecycle_fence_epoch = :fe"
            if op_id:
                cond += (
                    " AND (attribute_not_exists(active_lifecycle_op_id) OR "
                    "active_lifecycle_op_id = :o)"
                )
            else:
                cond += " AND attribute_not_exists(active_lifecycle_op_id)"
            vals[":fe"] = int(fence_epoch)
        try:
            tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression=expr,
                ConditionExpression=cond,
                ExpressionAttributeValues=vals,
            )
            if status == "done":
                n_done += 1
            else:
                n_failed += 1
            print(f"rebuild-reconcile {tid}: unconfirmed → {status} ({reason[:80]})")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # 期间又发了一次 rebuild(op_id 变了),或已被别处改掉 → 本轮结论作废。
                # 这正是约束 3 要防的:绝不用旧操作的结论盖新操作的状态。
                n_left += 1
                print(f"rebuild-reconcile {tid}: superseded (op_id/status changed) — skip")
            else:
                n_left += 1
                print(f"rebuild-reconcile {tid} (non-fatal): {e}")
        except Exception as e:  # noqa: BLE001 — 单个租户的失败不拖垮整轮对账
            n_left += 1
            print(f"rebuild-reconcile {tid} (non-fatal): {e}")

    return n_done, n_failed, n_left


def _reap_stuck_creating(now):
    """Mark tenants stuck in `creating` past CREATING_TIMEOUT_SECONDS as failed
    and release their host capacity reservation. Returns the count reaped.

    VM 的容量(overcommit 账本红线)。stop 未确认则本轮跳过、下轮再试。

    Capacity is decremented with the same if_not_exists-guarded, conditional
    pattern the create/delete paths use, so a row whose slot was already released
    (or a host with cold counters) can't drive the ledger negative. The status
    flip is conditional on still being `creating` to avoid racing a launch that
    just succeeded. vcpu/mem come from the tenant record (what was reserved)."""
    stuck = []
    lek = None
    while True:
        kw = {
            "FilterExpression": (
                "#s = :c AND "
                "(attribute_not_exists(synthetic) OR synthetic <> :true)"
            ),
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":c": "creating", ":true": True},
        }
        if lek:
            kw["ExclusiveStartKey"] = lek
        page = tenants_table.scan(**kw)
        stuck += page.get("Items", [])
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break

    print(
        f"reap: scanned {len(stuck)} creating tenants, timeout={CREATING_TIMEOUT_SECONDS}s"
    )
    reaped = 0
    for t in stuck:
        started = t.get("creation_started_at") or t.get("created_at") or ""
        if not started:
            print(f"reap-skip {t.get('id')}: no creation timestamp")
            continue
        try:
            # Normalize: fromisoformat (pre-3.11) chokes on a trailing 'Z', and a
            # naive timestamp can't be subtracted from the aware `now`.
            s = started.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            elapsed = (now - dt).total_seconds()
        except Exception as e:
            print(f"reap-skip {t.get('id')}: bad timestamp {started!r}: {e}")
            continue
        if elapsed < CREATING_TIMEOUT_SECONDS:
            continue  # genuinely still booting — leave it alone
        print(
            f"reap-hit {t.get('id')}: elapsed={int(elapsed)}s > {CREATING_TIMEOUT_SECONDS}s"
        )

        tid = t["id"]
        host_id = t.get("host_id", "")
        vcpu = int(t.get("vcpu", 1) or 1)
        mem_mb = int(t.get("mem_mb", 2048) or 2048)
        rid = t.get("capacity_reservation_id")
        # 回滚的互斥锚(防 ABA 双扣):谁先消费令牌谁扣一次,其余幂等。释放事务条件双锚:tenant 侧
        # status=failed AND capacity_reservation_id=:rid,host 侧下溢守卫。令牌缺失(同步 create
        # fence 保留令牌,release 前崩溃 → failed+令牌由 _reap_orphan_reservations 兜底,故不留无主增量)。
        _reaped_reason = f"reaped: stuck in creating > {CREATING_TIMEOUT_SECONDS}s"
        if rid and host_id:
            # ① 原子把 creating→failed(条件 status=creating AND token=rid),【保留】令牌+放置。
            #    一个并发 promote(creating→running,条件也锁 creating)与本 flip 互斥:promote
            #    已赢则本 flip CCF → 跳过,【绝不 stop 那个刚起来的活 VM】;本 flip 赢则该行进 failed
            #    终态,promote 再也提交不了(其条件锁 creating)——此后 stop 该 VM 恒安全。
            # ② failed 后才 stop-confirm(不能在 stop 后释放前才判 promote,那样已停了活 VM)。
            # ③ 确认停了才释放:REMOVE 令牌+放置(条件 status=failed AND token=rid)+ 扣 host。
            # 崩在①②③之间:行停在 failed 且仍带令牌 → _reap_orphan_reservations(含 failed)下轮
            # 兜底(同样 stop-confirm 后释放)。故释放与围栏拆两步不会永久搁浅令牌。
            try:
                tenants_table.update_item(
                    Key={"id": tid},
                    UpdateExpression="SET #s = :f, updated_at = :t, reaped_reason = :r",
                    ConditionExpression="#s = :c AND capacity_reservation_id = :rid",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":f": "failed", ":c": "creating", ":rid": rid,
                        ":t": now.isoformat(), ":r": _reaped_reason,
                    },
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    # promote 赢了(已 running)/ 已被别的释放者流转 → 跳过,不 stop、不释放。
                    print(f"reap-skip {tid}: promoted or status moved (fence CCF)")
                    continue
                print(f"reap-skip {tid}: fence flip failed ({e})")
                continue
            # 行已 failed(不可再 promote)→ 停 VM 恒安全。未确认停止 → 留 failed+令牌,下轮兜底。
            if not _confirm_vm_stopped(host_id, tid, t.get("vm_num", 1)):
                continue
            try:
                hosts_table.meta.client.transact_write_items(
                    TransactItems=[
                        {
                            "Update": {
                                "TableName": tenants_table.table_name,
                                "Key": {"id": tid},
                                "UpdateExpression": (
                                    "REMOVE capacity_reservation_id, dispatch_settle, "
                                    "host_id, vm_num, guest_ip, host_port"
                                ),
                                "ConditionExpression": (
                                    "#s = :f AND capacity_reservation_id = :rid"
                                ),
                                "ExpressionAttributeNames": {"#s": "status"},
                                "ExpressionAttributeValues": {":f": "failed", ":rid": rid},
                            }
                        },
                        {
                            "Update": {
                                "TableName": hosts_table.table_name,
                                "Key": {"instance_id": host_id},
                                "UpdateExpression": (
                                    "SET used_vcpu = used_vcpu - :v, "
                                    "used_mem_mb = used_mem_mb - :m, "
                                    "vm_count = vm_count - :one"
                                ),
                                "ConditionExpression": (
                                    "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                                ),
                                "ExpressionAttributeValues": {
                                    ":v": vcpu,
                                    ":m": mem_mb,
                                    ":one": 1,
                                },
                            }
                        },
                    ]
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "TransactionCanceledException":
                    # 令牌已被别的释放者消费 / 下溢守卫命中 → 幂等跳过(不是错误)。
                    print(f"reap-skip {tid}: reservation already released or status moved")
                    continue
                print(f"reap-skip {tid}: token release txn failed ({e})")
                continue
            reaped += 1
            print(f"reap: {tid} on {host_id} token-release (elapsed={int(elapsed)}s)")
            continue

        # (条件 creating,promote 赢则 CCF 跳过)后 guarded 释放 slot,不 stop-confirm。这条路
        # 仅存在于上面令牌分支,已改为 fence→stop→release)。
        try:
            tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression="SET #s = :f, updated_at = :t, reaped_reason = :r",
                ConditionExpression="#s = :c",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":f": "failed",
                    ":c": "creating",
                    ":t": now.isoformat(),
                    ":r": _reaped_reason,
                },
            )
        except Exception as e:
            # keep skipping but leave a trail so we can distinguish the
            # two in journalctl if reap stops making progress.
            print(f"reap-skip {tid}: conditional update failed ({e})")
            continue
        # Release the slot (guarded so it can't go negative).
        if host_id:
            try:
                hosts_table.update_item(
                    Key={"instance_id": host_id},
                    UpdateExpression=(
                        "SET used_vcpu = if_not_exists(used_vcpu, :z) - :v, "
                        "used_mem_mb = if_not_exists(used_mem_mb, :z) - :m, "
                        "vm_count = if_not_exists(vm_count, :z) - :one"
                    ),
                    ConditionExpression=(
                        "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                    ),
                    ExpressionAttributeValues={
                        ":z": 0,
                        ":v": vcpu,
                        ":m": mem_mb,
                        ":one": 1,
                    },
                )
            except Exception as e:
                print(f"reap: {tid} status flipped but slot release skipped: {e}")
        reaped += 1
        print(f"reap: {tid} on {host_id} (stuck creating, elapsed={int(elapsed)}s)")
    return reaped


# _reap_stuck_creating 扫不到;若仍带 capacity_reservation_id,那份容量永久搁浅:
#   - deleting:delete 的令牌释放遇瞬时失败返 502 留 deleting,而 delete replay 若在到达释放前
#     意图(VM 正被拆),释放其容量恒安全。
# requires_intervention:终态失败,VM 非活跃语义,令牌可【立即】回收(reserve 已提交、无论
# VM 起没起都不再重试)。**deleting 不在此列**(codex review9 #1 + review10 #1):deleting 的 VM
# 可能【还活着】(delete 先翻 deleting 再 backup+stop-vm,释放在 stop 成功之后),单靠 age 证明
# 不了 stop-vm 已跑——一个在 stop 前崩溃的 delete 会被误 reap 掉活 VM 的容量/放置(欠记账红线)。
# deleting 的令牌搁浅已由 delete 自身的重投兜底(队列 502 replay 带 _consumer_ident 重跑释放;
#
# 【先围栏(creating→failed 保留令牌)→ stop-confirm → 释放】三步做,避免 stop 掉一个刚 promote
# 成 running 的活 VM。若 stop 本轮未确认,行会停在【failed 且仍带令牌】,creating-reaper 再也扫
# 不到它。此处把 failed 纳入孤儿兜底:下轮同样 stop-confirm 后释放。安全性:全仓仅 reaper 的
# 围栏步会写出【failed + 令牌】(poller 转 running 删令牌、转 requires_intervention 属另一孤儿态、
# _release_reservation 释放即删令牌);且 failed 绝不会被 promote 成活 VM(promote 条件锁 creating)。
_ORPHAN_REAP_STATUSES = ("requires_intervention", "failed")


def _reap_orphan_reservations(now=None):
    """#412(review2 #4)—— 清扫【requires_intervention 且仍持 capacity_reservation_id】的令牌
    孤儿(dispatch 失败超预算转此、非 creating,逃出 _reap_stuck_creating)。令牌化释放:扣 host
    + 删令牌 + 清放置一个 TransactWriteItems,tenant 项条件 status=requires_intervention AND
    capacity_reservation_id=:rid(与其它释放者同锚,幂等不双扣),host 项下溢守卫。释放后 status
    保持不变(仍待上层处置),只是不再占容量。全量翻页。now 参数保留(签名稳定,当前未用)。"""
    released = 0
    for st in _ORPHAN_REAP_STATUSES:
        lek = None
        while True:
            kw = {
                "FilterExpression": (
                    "#s = :st AND attribute_exists(capacity_reservation_id)"
                ),
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":st": st},
            }
            if lek:
                kw["ExclusiveStartKey"] = lek
            page = tenants_table.scan(**kw)
            for t in page.get("Items", []):
                tid = t["id"]
                host_id = t.get("host_id", "")
                rid = t.get("capacity_reservation_id")
                if not (host_id and rid):
                    continue
                vcpu = int(t.get("vcpu", 1) or 1)
                mem_mb = int(t.get("mem_mb", 2048) or 2048)
                # 可能仍在跑,launch-vm.sh 失败后会留活 FC)。未确认 → 本轮跳过,下轮再试。
                if not _confirm_vm_stopped(host_id, tid, t.get("vm_num", 1)):
                    continue
                try:
                    hosts_table.meta.client.transact_write_items(TransactItems=[
                        {"Update": {
                            "TableName": tenants_table.table_name,
                            "Key": {"id": tid},
                            "UpdateExpression": (
                                "REMOVE capacity_reservation_id, dispatch_settle, "
                                "host_id, vm_num, guest_ip, host_port"
                            ),
                            # 条件锁在【该行当前状态】+ 令牌:防扫描后 status 流转(如 deleting→
                            # deleted 或被别的释放者消费)误扣。
                            "ConditionExpression": (
                                "#s = :st AND capacity_reservation_id = :rid"
                            ),
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {":st": st, ":rid": rid},
                        }},
                        {"Update": {
                            "TableName": hosts_table.table_name,
                            "Key": {"instance_id": host_id},
                            "UpdateExpression": (
                                "SET used_vcpu = used_vcpu - :v, "
                                "used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one"
                            ),
                            "ConditionExpression": (
                                "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                            ),
                            "ExpressionAttributeValues": {":v": vcpu, ":m": mem_mb, ":one": 1},
                        }},
                    ])
                    released += 1
                    print(f"reap-orphan: released token {rid} tenant={tid} host={host_id} st={st}")
                except ClientError as e:
                    if e.response["Error"]["Code"] == "TransactionCanceledException":
                        # 令牌已被别的释放者消费 / status 已流转 → 幂等跳过。
                        continue
                    print(f"reap-orphan {tid}: release failed (non-fatal): {e}")
            lek = page.get("LastEvaluatedKey")
            if not lek:
                break
    return released


def _rollback_migration(tenant, reason):
    """Roll a failed/stuck migration back to `running` and clear the async
    context. The source VM was only briefly paused for the snapshot and then
    resumed by migrate-vm.sh, so source host_id / routing are untouched — 'running'
    is the truthful state there.

    _reserve_migration_slot's CAS (used_vcpu/used_mem_mb/vm_count += 1 on the
    target before status=migrating is written). So on failure we MUST release
    that target reservation here, or every failed migration (SSM throttle,
    restore fail, dashboard flaky, watchdog timeout — 8 call sites) permanently
    leaks one slot of target capacity → ledger drift → target wrongly seen full
    → new launches 503. next_vm_num is monotonic (not rewound, per _release_slot's
    rule); only used_*/vm_count are released, with a floor guard so a double
    rollback can't drive the ledger negative.
    """
    tid = tenant["id"]
    condition, values = _migration_fence_condition(
        tenant, tenant.get("migration_phase")
    )
    values.update(
        {
            ":r": "running",
            ":reason": reason[:500],
            ":t": datetime.now(timezone.utc).isoformat(),
        }
    )
    try:
        # Claim rollback first. Only the owner of this exact migration epoch may
        # release the target reservation; stale sweep invocations do nothing.
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET #s = :r, migration_failed = :reason, updated_at = :t "
                "REMOVE migration_target, migration_target_vm_num, migration_source, "
                "migration_snap_cmd, migration_restore_cmd, migration_phase, "
                "migration_started_at, migration_snapshot_uri, "
                "migration_lifecycle_op_id, migration_lifecycle_fence_epoch"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if not _is_conditional_failure(exc):
            raise
        print(f"migration rollback {tid}: stale owner; skipped ({reason})")
        return False

    target_host_id = tenant.get("migration_target")
    if target_host_id:
        try:
            vcpu = int(tenant.get("vcpu", 0))
            mem_mb = int(tenant.get("mem_mb", 0))
            hosts_table.update_item(
                Key={"instance_id": target_host_id},
                UpdateExpression=(
                    "SET used_vcpu = used_vcpu - :v, "
                    "used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one"
                ),
                # floor guard:只在够减时减,防并发/重复 rollback 把账本扣成负。
                ConditionExpression=(
                    "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                ),
                ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1},
            )
        except Exception as e:
            # CCF(已释放过/账本不足)或其它 → non-fatal,不重复扣、不崩 rollback。
            print(
                f"migration rollback target slot release skipped/failed (non-fatal): {e}"
            )
    _release_migration_fence(tenant)
    _emit_audit("MIGRATION_FAILED", {"tenant_id": tid, "reason": reason[:200]})
    print(f"migration rollback {tid}: {reason}")
    return True


def _advance_migration(tenant, now):
    """Advance one in-flight migration by exactly one step per sweep tick.

    State machine (migration_phase):
      snapshot → (SSM snapshot Success) → fire restore, phase=restore
               → (SSM Failed/TimedOut)  → rollback to running
      restore  → (SSM restore Success)  → verify dashboard → flip → running
               → (SSM Failed/TimedOut)  → rollback to running
    InProgress at either phase: do nothing, re-check next tick. A watchdog
    rolls back migrations stuck past MIGRATION_WATCHDOG_MINUTES.
    """
    tid = tenant["id"]
    phase = tenant.get("migration_phase", "")
    # Guard: only tenants with an explicit migration_phase are mid-migration.
    # A tenant with status=migrating but no phase (shouldn't happen via the
    # API, but be defensive against stray scans / manual DDB edits) is left
    # untouched rather than force-rolled-back. Empty phase = nothing to advance.
    if not phase:
        return
    if not _renew_migration_fence(tenant):
        return
    source_host_id = tenant.get("migration_source", "")
    target_host_id = tenant.get("migration_target", "")
    target_vm_num = int(tenant.get("migration_target_vm_num", 1))
    snap_uri = tenant.get("migration_snapshot_uri", "")
    host_guard = _migration_host_guard(tenant)

    # Watchdog — never let a tenant sit in `migrating` forever.
    # EXCEPTION: phase=draining is POST-commit — DDB/Redis/ALB already point at
    # target, source is being torn down. _rollback_migration assumes source
    # untouched + releases the target slot as a reservation; running it on a
    # draining tenant would corrupt state (un-flip to a dead source, double-free
    # target capacity). Stage 3 is seconds of drain + idempotent cleanup that
    # self-heals on re-entry, so never watchdog-rollback it.
    started = tenant.get("migration_started_at", "")
    if started and phase != "draining":
        try:
            elapsed_min = (now - datetime.fromisoformat(started)).total_seconds() / 60.0
            if elapsed_min > MIGRATION_WATCHDOG_MINUTES:
                _rollback_migration(
                    tenant, f"watchdog: stuck in {phase} for {int(elapsed_min)}min"
                )
                return
        except Exception as e:
            # rollback on garbled input) but surface the drift.
            print(
                f"migration watchdog {tenant.get('id')}: bad started_at "
                f"{started!r} ({e}) — skipping"
            )

    if phase == "snapshot":
        cmd_id = tenant.get("migration_snap_cmd", "")
        if not cmd_id:
            _rollback_migration(tenant, "snapshot phase but no migration_snap_cmd")
            return
        done, ok = _poll_ssm(cmd_id, source_host_id)
        if not done:
            return  # still running; check again next tick
        if not ok:
            _rollback_migration(tenant, "snapshot command failed on source host")
            return
        # Snapshot done — fire restore on the target host.
        restore_cmd = _ssm_send_hc(
            target_host_id,
            f"{host_guard} && "
            f"/home/ubuntu/migrate-vm.sh restore {shlex.quote(tid)} "
            f"{target_vm_num} {shlex.quote(snap_uri)} && {host_guard}",
            timeout=600,
        )
        if not restore_cmd:
            _rollback_migration(tenant, "failed to submit restore SSM command")
            return
        condition, values = _migration_fence_condition(tenant, "snapshot")
        values.update(
            {
                ":p": "restore",
                ":rc": restore_cmd,
                ":t": datetime.now(timezone.utc).isoformat(),
            }
        )
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET migration_phase = :p, migration_restore_cmd = :rc, updated_at = :t"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
        print(f"migration {tid}: snapshot done → restore fired ({restore_cmd})")
        return

    if phase == "restore":
        cmd_id = tenant.get("migration_restore_cmd", "")
        if not cmd_id:
            _rollback_migration(tenant, "restore phase but no migration_restore_cmd")
            return
        done, ok = _poll_ssm(cmd_id, target_host_id)
        if not done:
            return
        if not ok:
            _rollback_migration(tenant, "restore command failed on target host")
            return

        # ─── R6 阶段1 Ready(建)——不碰 Redis/ALB,源始终是 fallback ───
        # restore 成功后,先让 TARGET host 权威地建好数据面路由(alloc 端口 + 写
        # PREROUTING DNAT)并自检 guest /healthz=200 才回 OK(R6.2 黑洞防护:未就绪
        # 不切流,Redis 仍指源,源还活着老流量正常)。ready-route **不写 Redis**——
        # 把"建"和"切"分开,才有探活门可插在两步之间。失败 fail-loud:保持
        # migrating 不切流,下轮 sweep 重入重试(不静默吞、不留半态)。
        target = (
            hosts_table.get_item(Key={"instance_id": target_host_id}).get("Item") or {}
        )
        target_ip = target.get("private_ip", "")
        if not target_ip:
            _rollback_migration(tenant, "target host has no private_ip")
            return
        ok, out = _ssm_run_capture(
            target_host_id,
            f"{host_guard} && python3 /opt/openclaw/route_ops.py ready-route "
            f"{shlex.quote(tid)} && {host_guard}",
        )
        # ready-route 打 `OK <host_port> <guest_ip>` 仅当 DNAT 建好且 guest 自检通过;
        # 未就绪打 `NOT_READY ...` 且 exit≠0 → ok=False。任一非就绪都不切流。
        if not ok or not out.startswith("OK "):
            print(f"migration {tid}: target not ready ({out!r}); stay migrating (R6.2)")
            return
        parts = out.split()
        if len(parts) < 3 or not parts[1].isdigit():
            print(f"migration {tid}: ready-route bad output ({out!r}); stay migrating")
            return
        new_host_port = int(parts[1])
        new_guest_ip = parts[2]

        # ─── R6 阶段2 Commit(切)——target 就绪确认后才一次性切导航 ───
        # 顺序:repoint ALB → 经 ALB 探活确认 target 真可达 → commit-route 原子写
        # Redis(切流的临界点,edge 的权威路由源)。ALB/探活任一失败 → 回滚,此刻
        # Redis 仍未 commit、仍指源(源是 fallback)。Redis 是最后一步,切完才翻 DDB。
        if not _renew_migration_fence(tenant):
            return
        try:
            _repoint_alb_rule(tid, target_host_id, target_ip)
        except Exception as e:
            _rollback_migration(tenant, f"ALB repoint failed: {e}")
            return
        if PUBLIC_BASE_URL and not _verify_dashboard_reachable_via_alb(
            tid, PUBLIC_BASE_URL, timeout_sec=30, poll_sec=3
        ):
            _rollback_migration(tenant, "dashboard not reachable via ALB after restore")
            return
        ok, cout = _ssm_run_capture(
            target_host_id,
            f"{host_guard} && python3 /opt/openclaw/route_ops.py commit-route "
            f"{shlex.quote(tid)} {shlex.quote(target_ip)} {new_host_port} "
            f"{shlex.quote(new_guest_ip)} && {host_guard}",
        )
        if not ok or not cout.startswith("OK "):
            _rollback_migration(tenant, f"commit-route (Redis) failed: {cout!r}")
            return

        # 切流成功——翻 DDB source-of-truth 到 target,但**不立即拆源**:进 draining
        # 阶段,保留源旧 DNAT 一个 drain 窗口(R6.1 阶段3),让在途老流量走完。保存
        # 源侧收尾需要的旧值(host_port/guest_ip/vm_num)+ 打 committed 时间戳。DDB
        # 描述符、Redis route、target DNAT 三方此刻一致(Property 4)。status 仍
        # migrating(phase=draining),下轮 sweep 才拆源。
        old_host_port = tenant.get("host_port")
        old_guest_ip = tenant.get("guest_ip", "")
        old_vm_num = int(tenant.get("vm_num", 1))
        condition, values = _migration_fence_condition(tenant, "restore")
        values.update(
            {
                ":h": target_host_id,
                ":n": target_vm_num,
                ":t": datetime.now(timezone.utc).isoformat(),
                ":hpi": target_ip,
                ":hp": new_host_port,
                ":gip": new_guest_ip,
                ":draining": "draining",
                ":ohp": int(old_host_port) if old_host_port else 0,
                ":ogip": old_guest_ip,
                ":ovn": old_vm_num,
            }
        )
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET host_id = :h, vm_num = :n, updated_at = :t, "
                "host_private_ip = :hpi, host_port = :hp, guest_ip = :gip, "
                "migration_phase = :draining, migration_committed_at = :t, "
                "migration_old_host_port = :ohp, migration_old_guest_ip = :ogip, "
                "migration_old_vm_num = :ovn"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
        _emit_audit(
            "MIGRATION_COMMITTED",
            {
                "tenant_id": tid,
                "source_host_id": source_host_id,
                "target_host_id": target_host_id,
            },
        )
        print(f"migration {tid}: COMMITTED → {target_host_id}, draining source")
        return

    if phase == "draining":
        # ─── R6 阶段3 Drain + 删 —— 切流已成功,拆源在 drain 窗口后 ───
        # 等 migration_committed_at 起算满 MIGRATION_DRAIN_SECONDS(跨 sweep 判定,
        # 不在单次 invocation sleep)。窗口内源旧 DNAT 保留,在途老流量走完。
        committed = tenant.get("migration_committed_at", "")
        if committed:
            try:
                elapsed = (now - datetime.fromisoformat(committed)).total_seconds()
                if elapsed < MIGRATION_DRAIN_SECONDS:
                    return  # drain 未满,下一 tick 再拆
            except ValueError:
                pass  # 坏时间戳 → 直接进拆源(committed 已过,不再等)

        # 恰好一次收口:CAS 翻 running(条件 status=migrating AND phase=draining)。
        # 赢的 sweep 才做源清理,重入的 loser CCF → 跳过,杜绝双减/双删(Property 6)。
        try:
            condition, values = _migration_fence_condition(tenant, "draining")
            values.update(
                {
                    ":running": "running",
                    ":draining": "draining",
                    ":t": datetime.now(timezone.utc).isoformat(),
                }
            )
            tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression=(
                    "SET #s = :running, updated_at = :t "
                    "REMOVE migration_target, migration_target_vm_num, migration_source, "
                    "migration_snap_cmd, migration_restore_cmd, migration_phase, "
                    "migration_started_at, migration_snapshot_uri, migration_failed, "
                    "migration_committed_at, migration_old_host_port, "
                    "migration_old_guest_ip, migration_old_vm_num, "
                    "migration_lifecycle_op_id, migration_lifecycle_fence_epoch"
                ),
                ConditionExpression=condition,
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise
            print(f"migration {tid}: draining already finalized by concurrent sweep")
            return

        # 赢家专属:减 SOURCE 计数 + stop 源 VM + release-route 源(硬伤③/R5)。
        vcpu = int(tenant.get("vcpu", 0))
        mem_mb = int(tenant.get("mem_mb", 0))
        if source_host_id:
            try:
                hosts_table.update_item(
                    Key={"instance_id": source_host_id},
                    UpdateExpression=(
                        "SET used_vcpu = used_vcpu - :v, "
                        "used_mem_mb = used_mem_mb - :m, "
                        "vm_count = vm_count - :one"
                    ),
                    # floor guard:够减才减,防账本扣成负。
                    ConditionExpression=(
                        "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                    ),
                    ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1},
                )
            except Exception as e:
                print(f"source counter dec skipped/failed (non-fatal): {e}")

        old_vm_num = int(tenant.get("migration_old_vm_num", tenant.get("vm_num", 1)))
        if source_host_id:
            try:
                _ssm_run_capture(
                    source_host_id,
                    f"{host_guard} && /home/ubuntu/stop-vm.sh "
                    f"{shlex.quote(tid)} {old_vm_num} && {host_guard} && "
                    f"sudo rm -f /etc/nginx/conf.d/tenants/{tid}.conf "
                    f"&& sudo nginx -s reload && {host_guard}",
                    timeout=60,
                )
            except Exception as e:
                # unreachable); note it so we know why tap/nginx residue
                # occasionally survives migration.
                print(
                    f"migration {tid}: source stop-vm/nginx dispatch failed "
                    f"(non-fatal): {e}"
                )

        # R5/R6.3①:拆源 PREROUTING DNAT + 释放端口位图槽。DNAT 一拆,源该 port 无
        # 监听者,内核对残留在途流量立即 RST(fail-closed,不静默转发)。用切流时
        # 保存的旧 host_port/guest_ip(DDB 已翻成新值)。release-route 不碰 Redis
        # (route 已指 target)。幂等:容忍已删规则,重入不重复扣减(Property 6)。
        old_host_port = tenant.get("migration_old_host_port")
        old_guest_ip = tenant.get("migration_old_guest_ip", "")
        if source_host_id and old_host_port and old_guest_ip:
            try:
                _ssm_run_capture(
                    source_host_id,
                    f"{host_guard} && python3 /opt/openclaw/route_ops.py "
                    f"release-route {shlex.quote(tid)} {int(old_host_port)} "
                    f"{shlex.quote(old_guest_ip)} && {host_guard}",
                    timeout=60,
                )
            except Exception as e:
                print(
                    f"migration {tid}: source release-route dispatch failed (non-fatal): {e}"
                )

        _emit_audit(
            "MIGRATION_COMPLETED",
            {
                "tenant_id": tid,
                "source_host_id": source_host_id,
                "target_host_id": target_host_id,
            },
        )
        _release_migration_fence(tenant)
        print(f"migration {tid}: COMPLETE → {target_host_id}")
        return

    # Unknown phase — don't strand the tenant.
    _rollback_migration(tenant, f"unknown migration_phase: {phase!r}")


def _poll_ssm(command_id, instance_id):
    """Single, instantaneous check of an SSM command's status. Returns
    (done, ok):
      (False, _)    — Pending / InProgress / Delayed / not yet registered;
                      re-check on the next sweep tick (do NOT block here)
      (True, True)  — Success
      (True, False) — Failed / TimedOut / Cancelled

    Deliberately does NOT reuse _wait_ssm_done: that helper blocks in a sleep
    loop and collapses 'still running' and 'failed' into the same (False, msg),
    which the sweep must distinguish. We read Status once and return."""
    try:
        inv = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
        )
    except ssm.exceptions.InvocationDoesNotExist:
        return False, False  # not registered yet; try next tick
    except Exception as e:
        print(f"_poll_ssm error {command_id}/{instance_id}: {e}")
        return False, False
    status = inv.get("Status", "Pending")
    if status == "Success":
        return True, True
    if status in ("Failed", "TimedOut", "Cancelled"):
        print(
            f"_poll_ssm {command_id}: {status} - "
            f"{(inv.get('StandardErrorContent') or '')[:200]}"
        )
        return True, False
    return False, False  # Pending / InProgress / Delayed


def _ssm_send_hc(instance_id, command, timeout=120):
    """Fire-and-forget SSM from the health_check Lambda; returns CommandId or
    None. Mirrors the api Lambda's _ssm_send (wraps HOME/cd, returns the id so
    the sweep can poll it on the next tick)."""
    try:
        wrapped = f"export HOME=/home/ubuntu && cd /home/ubuntu && {command}"
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        return resp["Command"]["CommandId"]
    except Exception as e:
        print(f"_ssm_send_hc error: {e}")
        return None


def _ssm_run_capture(instance_id, command, timeout=60):
    """Run one SSM command, block until done, return (ok, stdout). Used by the
    migration completion branch to call route_ops ensure-route/release-route on
    the target/source host and read back host-authoritative values. Unlike
    _ssm_send_hc (fire-and-forget), this waits so the completion branch can act
    on the result (fail-loud + keep migrating if the host-side route op fails)."""
    try:
        wrapped = (
            "set -a; . /etc/environment 2>/dev/null; . /etc/platform.env 2>/dev/null; "
            f"set +a; export HOME=/home/ubuntu && cd /home/ubuntu && {command}"
        )
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        cmd_id = resp["Command"]["CommandId"]
    except Exception as e:
        print(f"_ssm_run_capture send error {instance_id}: {e}")
        return False, ""
    ok, err = _wait_ssm_done(cmd_id, instance_id, timeout_sec=timeout, poll_sec=2)
    if not ok:
        print(f"_ssm_run_capture {instance_id}: {err}")
        return False, ""
    try:
        inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        return True, (inv.get("StandardOutputContent") or "").strip()
    except Exception as e:
        print(f"_ssm_run_capture read stdout error {instance_id}: {e}")
        return False, ""


def _restart_host_agent(host_id, now):
    """Restart host-agent service via SSM. Returns True if restart was issued."""
    # Cooldown: check last restart time
    host = hosts_table.get_item(Key={"instance_id": host_id}).get("Item")
    if not host or host.get("status") == "deleted":
        return False

    last_restart = host.get("agent_restart_at", "")
    if last_restart:
        try:
            elapsed = (now - datetime.fromisoformat(last_restart)).total_seconds()
            if elapsed < RESTART_COOLDOWN_SECONDS:
                print(
                    f"skip restart {host_id}: cooldown ({int(elapsed)}s < {RESTART_COOLDOWN_SECONDS}s)"
                )
                return False
        except Exception as e:
            # cooldown enforced) but surface the timestamp corruption.
            print(
                f"restart {host_id}: bad agent_restart_at {last_restart!r} "
                f"({e}) — bypassing cooldown"
            )

    # Restart host-agent via SSM (single command recovers all VMs on this host)
    try:
        ssm.send_command(
            InstanceIds=[host_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": ["systemctl restart host-agent"],
                "executionTimeout": ["30"],
            },
        )
        hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="SET agent_restart_at = :t",
            ExpressionAttributeValues={":t": now.isoformat()},
        )
        print(f"restarted host-agent on {host_id}")
        return True
    except Exception as e:
        print(f"failed to restart host-agent on {host_id}: {e}")
        return False


# =====================================================================
# AZ-level failover (since 1.3.0). The pure-logic helpers are kept
# separate from the AWS shell so they can be unit-tested directly.
# =====================================================================


def is_host_unhealthy(host, now, threshold_minutes):
    """Return True if the host is considered unhealthy for AZ-failover purposes.

    A host is unhealthy if:
      * status == 'deleted' (already torn down), OR
      * last_health_check older than threshold_minutes, OR
      * last_health_check missing entirely.

    Hosts with status 'idle' but recent health updates are still 'healthy'
    from an AZ-availability perspective — they can take new VMs.
    """
    if not host:
        return True
    if host.get("status") == "deleted":
        return True

    last = host.get("last_health_check") or host.get("last_seen") or ""
    if not last:
        return True
    try:
        elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        return elapsed >= threshold_minutes * 60
    except Exception:
        return True


def group_hosts_by_az(hosts):
    """Return {az_name: [host_records]}; hosts without an az field skipped."""
    out = {}
    for h in hosts:
        az = h.get("az")
        if not az:
            continue
        out.setdefault(az, []).append(h)
    return out


def detect_unhealthy_azs(hosts, now, threshold_minutes):
    """Identify AZs where every host is unhealthy.

    Returns a list of dicts: ``[{"az": "...", "host_ids": [...], "host_count": N}]``.
    AZs that contain no hosts at all are *not* flagged — only AZs that
    used to host capacity and have lost it.
    """
    az_buckets = group_hosts_by_az(hosts)
    out = []
    for az, host_list in az_buckets.items():
        if not host_list:
            continue
        if all(is_host_unhealthy(h, now, threshold_minutes) for h in host_list):
            out.append(
                {
                    "az": az,
                    "host_ids": [h["instance_id"] for h in host_list],
                    "host_count": len(host_list),
                }
            )
    return out


def pick_target_host(hosts, now, threshold_minutes, exclude_azs, required_vcpu=0):
    """Choose the best healthy host outside ``exclude_azs`` for failover.

    Priority:
      1. Healthy host with the most spare vCPU (capacity - vm_count * default_vcpu).
      2. Tie-breaker: lowest current vm_count.
      3. Tie-breaker: lexicographic instance_id (deterministic).

    Returns the host record, or None if no candidate exists.
    """
    candidates = []
    for h in hosts:
        az = h.get("az", "")
        if az in exclude_azs:
            continue
        if is_host_unhealthy(h, now, threshold_minutes):
            continue
        if h.get("status") == "deleted":
            continue
        # Estimate spare capacity. Hosts publish total_vcpu (decimal stored
        # as Number in DDB; we coerce to int via Decimal-friendly path).
        # Fall back to legacy field names just in case.
        raw_total = h.get("total_vcpu") or h.get("vcpu_total") or h.get("max_vcpu") or 0
        vcpu_total = int(raw_total)
        vm_count = int(h.get("vm_count") or 0)
        raw_used = h.get("used_vcpu")
        if raw_used is not None:
            # Prefer the actual booked vCPU when host-agent publishes it.
            spare = vcpu_total - int(raw_used)
        else:
            # Approximate per-VM cost; fallback to 2 if unknown.
            avg_vcpu = int(h.get("avg_vcpu_per_vm") or 2)
            spare = vcpu_total - vm_count * avg_vcpu
        if required_vcpu and spare < required_vcpu:
            continue
        candidates.append((-spare, vm_count, h["instance_id"], h))

    if not candidates:
        return None
    candidates.sort()
    return candidates[0][3]


def should_skip_az_for_cooldown(az_state, az, now, cooldown_minutes):
    """az_state[az] = ISO timestamp of last failover, or absent. Returns True
    if a previous failover for the same AZ is still inside cooldown."""
    last = az_state.get(az)
    if not last:
        return False
    try:
        elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        return elapsed < cooldown_minutes * 60
    except Exception:
        return False


def _check_and_handle_az_failover(now, tenants):
    """End-to-end AZ failover: detect outages, pick target, relaunch tenants.

    This is the AWS-side entrypoint. Pure logic lives in the helpers above.
    Returns a summary dict for logging / audit.
    """
    summary = {
        "az_outages_detected": 0,
        "tenants_failed_over": 0,
        "tenants_failed": 0,
        # 1.3.2: split out path-A "blocked" (no backup, refused to lose data)
        # from "failed" (SSM error, capacity exhausted, etc.) so summaries
        # accurately reflect WHY a tenant didn't migrate.
        "tenants_blocked": 0,
        "skipped_cooldown": [],
    }

    # 1) Load all hosts.
    hosts = hosts_table.scan().get("Items", [])

    # 2) Detect outage AZs.
    outages = detect_unhealthy_azs(hosts, now, AZ_UNHEALTHY_THRESHOLD_MINUTES)
    if not outages:
        return summary
    summary["az_outages_detected"] = len(outages)

    # 3) Load AZ failover state (kept on a synthetic host record with id=__az_failover_state__).
    state_record = (
        hosts_table.get_item(Key={"instance_id": "__az_failover_state__"}).get("Item")
        or {}
    )
    az_state = state_record.get("az_last_failover", {}) or {}

    healthy_azs = {
        h.get("az")
        for h in hosts
        if h.get("az") and not is_host_unhealthy(h, now, AZ_UNHEALTHY_THRESHOLD_MINUTES)
    }
    if not healthy_azs:
        # All AZs are out — nothing we can do; surface and bail.
        _emit_audit(
            "AZ_FAILOVER_SKIPPED",
            {"reason": "no_healthy_az", "outages": [o["az"] for o in outages]},
        )
        return summary

    for outage in outages:
        az = outage["az"]
        if should_skip_az_for_cooldown(az_state, az, now, AZ_COOLDOWN_MINUTES):
            summary["skipped_cooldown"].append(az)
            continue

        # 1.3.2: Mark cooldown as soon as we *act* on the outage, even
        # before tenant-level work. Reasons:
        #   1. Prevents alert spam — without this, an outage with no
        #      affected tenants would re-detect every Lambda tick (5 min)
        #      and re-emit audit + SNS until the AZ recovers.
        #   2. Provides idempotency for concurrent Lambda invocations: the
        #      second invocation sees az_state[az] set, hits the
        #      should_skip_az_for_cooldown guard, and bails before
        #      duplicating tenant migrations.
        # We persist immediately rather than at end-of-loop so concurrent
        # invokes pick this up.
        az_state[az] = now.isoformat()
        try:
            hosts_table.put_item(
                Item={
                    "instance_id": "__az_failover_state__",
                    "az_last_failover": az_state,
                    "updated_at": now.isoformat(),
                }
            )
        except Exception as e:
            print(f"persist cooldown state failed (non-fatal): {e}")

        # 4) Find tenants on the failed AZ.
        affected_tenant_ids = set()
        for t in tenants:
            host_id = t.get("host_id", "")
            if host_id in outage["host_ids"]:
                affected_tenant_ids.add(t["id"])

        # Even if no tenants need migration, still emit audit + SNS once
        # so an operator knows an AZ went down. Cooldown above prevents repeat.
        if not affected_tenant_ids:
            _emit_audit(
                "AZ_FAILOVER_NO_TENANTS_AFFECTED",
                {"az": az, "host_ids": outage["host_ids"]},
            )
            _emit_sns_notification(az, outage, recovered_count=0)
            continue

        for tenant in tenants:
            if tenant["id"] not in affected_tenant_ids:
                continue
            target = pick_target_host(
                hosts,
                now,
                threshold_minutes=AZ_UNHEALTHY_THRESHOLD_MINUTES,
                exclude_azs={az},
                required_vcpu=int(tenant.get("vcpu") or 1),
            )
            if not target:
                summary["tenants_failed"] += 1
                _emit_audit(
                    "AZ_FAILOVER_NO_TARGET", {"tenant_id": tenant["id"], "from_az": az}
                )
                continue
            outcome = _failover_tenant_to_host(tenant, target, az, now)
            # 1.3.2: outcome can be True (migrated), False (real failure),
            # or "blocked" (path-A no-backup refusal — accounted separately).
            if outcome is True:
                summary["tenants_failed_over"] += 1
                target["vm_count"] = int(target.get("vm_count") or 0) + 1
            elif outcome == "blocked":
                summary["tenants_blocked"] += 1
            else:
                summary["tenants_failed"] += 1
            # 1.3.2: bump in-memory next_vm_num on the target REGARDLESS of
            # outcome. Even on failure, launch-vm.sh has likely already
            # created a partially-set-up tap-vmN device that's left behind.
            # Re-using the same vm_num for the next tenant in the same
            # batch then trips ioctl(TUNSETIFF) 'Device or resource busy'.
            # Skip this only on 'blocked' since path-A doesn't touch SSM.
            if outcome != "blocked":
                target["next_vm_num"] = int(target.get("next_vm_num") or 1) + 1

        # 6) SNS notification (best-effort).
        _emit_sns_notification(az, outage, summary["tenants_failed_over"])

    # State already persisted at the start of each outage handling above.
    return summary


def _reserve_target_vm_num(target_host_id, vcpu, mem_mb, attempts=8):
    """#排雷 D2 — AZ failover 在 target host 上**原子**占一个 vm_num + 记账,替代旧的
    裸读 next_vm_num + 无条件 SET next_vm_num=target+1。与 create 路径 _reserve_slot、
    migrate 路径 _reserve_migration_slot 同款 CAS(#50/#172):一次条件写,只有 next_vm_num
    自读取未变、且容量不超卖才自增 next_vm_num/used_*/vm_count。CCF(并发 create/failover
    抢到同一 next_vm_num)则重读重试。返回认领的(自增前)vm_num,或 None(无容量/CAS 耗尽)。

    修复前:两步(裸读 + 绝对赋值)间无 CAS,并发 create 的递增会被 failover 的
    `SET next_vm_num=target+1` 覆盖 → 两租户拿同一 vm_num → guest_ip/tap 重叠 → 跨租户
    网络串(数据安全轴①)。此 CAS 把分配收敛成单次原子条件写,消除该窗口。
    """
    for _ in range(attempts):
        h = hosts_table.get_item(Key={"instance_id": target_host_id}).get("Item") or {}
        expected = int(h.get("next_vm_num", 1))
        total_v = int(
            h.get("total_vcpu") or h.get("vcpu_total") or h.get("max_vcpu") or 0
        )
        total_m = int(
            h.get("total_mem_mb") or h.get("mem_total_mb") or h.get("max_mem_mb") or 0
        )
        # cap 复检:used_* 自增后不得超过 total(无 total 记录 → cap 设极大值=不卡容量,
        # 仍走 CAS 保 next_vm_num 唯一;fail-safe 不因缺字段拒 failover)。
        cap_v = (total_v - vcpu) if total_v else 10**9
        cap_m = (total_m - mem_mb) if total_m else 10**12
        try:
            r = hosts_table.update_item(
                Key={"instance_id": target_host_id},
                UpdateExpression=(
                    "SET used_vcpu = if_not_exists(used_vcpu, :z) + :v, "
                    "used_mem_mb = if_not_exists(used_mem_mb, :z) + :m, "
                    "vm_count = if_not_exists(vm_count, :z) + :one, "
                    "next_vm_num = next_vm_num + :one"
                ),
                ConditionExpression=(
                    "next_vm_num = :expected AND used_vcpu <= :cap_v "
                    "AND used_mem_mb <= :cap_m"
                ),
                ExpressionAttributeValues={
                    ":v": vcpu,
                    ":m": mem_mb,
                    ":one": 1,
                    ":z": 0,
                    ":expected": expected,
                    ":cap_v": cap_v,
                    ":cap_m": cap_m,
                },
                ReturnValues="UPDATED_NEW",
            )
            return int(r["Attributes"]["next_vm_num"]) - 1
        except Exception as e:  # noqa: BLE001 — CCF 重试,其它异常传播(fail-loud)
            if "ConditionalCheckFailed" not in type(
                e
            ).__name__ and "ConditionalCheckFailed" not in str(e):
                raise
            continue  # 竞争/超卖 → 重读 next_vm_num 重试
    return None


def _failover_tenant_to_host(tenant, target_host, source_az, now):
    """Relaunch a tenant on a healthy target host (real, end-to-end).

    Strategy:
      1) Find the most recent backup. If none exists, refuse failover and
         emit an alert audit (path A: never silently lose data).
      2) Mark tenant as ``failover_recovering`` in DDB.
      3) Run launch-vm.sh on the target host via SSM with the correct
         positional arguments: <tenant_id> <vm_num> <vcpu> <mem_mb>
         <config_template> <restore_backup_key>. Wait synchronously
         (60s) for completion so we know whether the VM actually came up.
      4) Update the ALB rule for /vm/<tenant_id> to point at the target
         host's target group. Without this step CloudFront keeps routing
         to the dead source host.
      5) Tell the source host (best-effort) to clean its leftover nginx
         conf for this tenant. If the source host is fully down this
         is a no-op.
      6) Flip DDB ownership: tenant.host_id, vm_num, status=running.
         Bump target host's next_vm_num.
      7) Emit audit log + SNS event.

    Returns True iff the VM came up on the target host AND ALB rule was
    re-pointed. On failure, marks tenant ``failover_failed`` and emits
    an audit row so a human can act.
    """
    tenant_id = tenant["id"]
    vcpu = int(tenant.get("vcpu") or 2)
    mem_mb = int(tenant.get("mem_mb") or 4096)
    target_host_id = target_host["instance_id"]
    # #排雷 D2 — vm_num 不在此处裸读 target_host.next_vm_num(裸读+后面无条件绝对赋值
    # 之间无 CAS,并发 create 会抢同一 num → 跨租户 guest_ip/tap 重叠)。改为在所有早返回门
    # (无 backup / verify 失败)之后、真正 launch 之前,用 _reserve_target_vm_num 原子占槽
    # (见 :launch 段),避免早返回泄漏已占 slot。
    target_vm_num = None
    config_template = tenant.get("config_template") or ""
    source_host_id = tenant.get("host_id", "")

    # 1) Find latest backup (path A: refuse if missing).
    backup_key = _find_latest_backup_key(tenant_id) if ASSETS_BUCKET else None
    if not backup_key:
        _emit_audit(
            "AZ_FAILOVER_NO_BACKUP",
            {
                "tenant_id": tenant_id,
                "from_az": source_az,
                "reason": "no backup available — failover refused to avoid data loss",
            },
        )
        try:
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :failed, failover_error = :e",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":failed": "failover_blocked",
                    ":e": "no_backup_available",
                },
            )
        except Exception as e:
            # audit is already emitted above, but if the status write disappears
            # silently, later sweeps re-attempt failover forever. Surface it.
            print(
                f"az-failover {tenant_id}: mark failover_blocked failed "
                f"(non-fatal): {e}"
            )
        # SNS alert so a human can manually intervene.
        if _SNS_TOPIC_ARN:
            try:
                sns.publish(
                    TopicArn=_SNS_TOPIC_ARN,
                    Subject=f"[OpenClaw] AZ failover BLOCKED: {tenant_id} has no backup",
                    Message=json.dumps(
                        {
                            "event": "az_failover_blocked",
                            "tenant_id": tenant_id,
                            "reason": "no_backup_available",
                            "source_az": source_az,
                            "action_required": "manual recovery — restore from snapshot or accept data loss",
                        },
                        indent=2,
                    ),
                )
            except Exception as e:
                # but silence made "alert never fired" indistinguishable from
                # "alert fired and was ignored". Log so we can tell.
                print(
                    f"az-failover {tenant_id}: SNS blocked-alert publish "
                    f"failed (non-fatal): {e}"
                )
        return "blocked"  # 1.3.2: distinct from failures — caller buckets separately

    try:
        # 2) Mark recovering — with conditional update on host_id to prevent
        # concurrent Lambda invocations from both trying to migrate the same
        # tenant. If another invocation already moved it, ConditionalCheckFailed
        # raises; we skip cleanly and don't report failure.
        try:
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=(
                    "SET previous_host_id = :p, "
                    "failover_from_az = :az, failover_at = :t, "
                    "#s = :recover"
                ),
                ConditionExpression="host_id = :p",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":p": source_host_id,
                    ":az": source_az,
                    ":t": now.isoformat(),
                    ":recover": "failover_recovering",
                },
            )
        except tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
            # Another invocation already migrated this tenant — back off cleanly.
            print(f"skip {tenant_id}: already migrated by concurrent invocation")
            _emit_audit(
                "AZ_FAILOVER_SKIPPED_CONCURRENT",
                {"tenant_id": tenant_id, "from_az": source_az},
            )
            return False

        # 3) Launch on target host via SSM with POSITIONAL args.
        #    launch-vm.sh signature (11 位):
        #      launch-vm.sh <tenant_id> <vm_num> <vcpu> <mem_mb>
        #                   <config_template> <restore_backup_key>
        #                   <scoped_skills> <litellm_vkey> <channel_secret>
        #                   <chat_endpoint_enabled> <cognito_b64>
        #    config_template can be empty string ""; restore_backup_key
        #    is an S3 key (no s3:// prefix), e.g. backups/<tid>/<ts>.gz.
        # chat_endpoint_enabled 到 launch-vm 幂等段(第 10 位);位 7/8/9/11 空占位
        # (数据盘从 backup 恢复,首铸字段随备份带回来,不重铸)。老版本只填 6 位。
        # #排雷 D2 — 所有早返回门已过,现在原子占 target host 的 vm_num + 记账(CAS)。
        # 占槽放在 launch 前:无 backup / verify 失败等早返回不会泄漏 slot。CAS None =
        # target 无容量或持续输竞争 → 拒绝 failover(标 failed_no_capacity,不裸分配串号)。
        target_vm_num = _reserve_target_vm_num(target_host_id, vcpu, mem_mb)
        if target_vm_num is None:
            raise RuntimeError(
                f"target {target_host_id} 无法原子占 vm_num(无容量/CAS 竞争耗尽);"
                f"拒绝 failover 避免跨租户 vm_num 串号"
            )
        cee = bool(tenant.get("chat_endpoint_enabled", False))
        chat_ep_arg = "1" if cee else "0"
        launch_cmd = (
            f"/home/ubuntu/launch-vm.sh {tenant_id} {target_vm_num} "
            f'{vcpu} {mem_mb} "{config_template}" "{backup_key}" '
            f'"" "" "" {chat_ep_arg} ""'
        )
        ssm_resp = ssm.send_command(
            InstanceIds=[target_host_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [launch_cmd], "executionTimeout": ["600"]},
        )
        cmd_id = ssm_resp["Command"]["CommandId"]

        # Wait synchronously for the SSM command to finish (max 90s).
        # launch-vm.sh runs in <60s on a warm host with cached rootfs.
        ok, ssm_err = _wait_ssm_done(cmd_id, target_host_id, timeout_sec=90)
        if not ok:
            # 1.3.2: SSM exit code may not reflect reality. host-agent's
            # auto-recovery loop (every 5s) might salvage a launch that
            # transiently failed (e.g. TUNSETIFF EBUSY on a stale tap).
            # Fall through to verify gate — verify is the source of truth.
            print(f"SSM reported failure ({ssm_err}); verify gate decides.")

        # 4) GATE: verify the VM is genuinely running on the target host.
        #    1.4.2: this now includes a curl probe against
        #    http://127.0.0.1/vm/<tid>/ to catch the case where the
        #    Firecracker process exists but the guest never finished
        #    booting / nginx never reloaded the new conf. Without this
        #    gate, the previous code would return True for "VM half-up"
        #    and the dashboard would 502 in production.
        if not _verify_vm_actually_running(target_host_id, tenant_id, timeout_sec=120):
            raise RuntimeError(
                f"VM verify gate failed on target {target_host_id} "
                f"(process / nginx conf / local HTTP all checked); "
                f"refusing to flip DDB to running. SSM err: {ssm_err or '(none)'}"
            )
        # If we got here despite SSM failure, host-agent's auto-recovery
        # likely salvaged the launch — emit an informational audit row.
        if not ok:
            _emit_audit(
                "AZ_FAILOVER_RECOVERED_BY_VERIFY",
                {
                    "tenant_id": tenant_id,
                    "from_az": source_az,
                    "to_host": target_host_id,
                    "ssm_err": ssm_err[:200] if ssm_err else "",
                },
            )

        # 5) GATE: re-point ALB rule to the target host's target group.
        #    1.4.2: this MUST succeed. The previous code swallowed ALB
        #    errors and continued to flip DDB, producing the canonical
        #    "fake failover" where audit shows RECOVERED but the public
        #    dashboard URL still 502s because traffic still routes to the
        #    dead source host. We refuse to silently leave that state.
        if ALB_LISTENER_ARN:
            target_private_ip = target_host.get("private_ip")
            if not target_private_ip:
                raise RuntimeError(
                    f"target host {target_host_id} has no private_ip in DDB; "
                    f"cannot re-point ALB. Refusing to flip status to running."
                )
            try:
                _repoint_alb_rule(tenant_id, target_host_id, target_private_ip)
            except Exception as e:
                _emit_audit(
                    "AZ_FAILOVER_ALB_REPOINT_FAILED",
                    {"tenant_id": tenant_id, "error": str(e)[:200]},
                )
                raise RuntimeError(f"ALB repoint failed: {e}") from e

        # 6) GATE: cross-ALB reachability check. Hit the public URL the
        #    way a real user would. This is the bug-fix gate that turns
        #    "DDB says running" into "dashboard genuinely opens".
        #
        #    Skipped (with a CW warning) when PUBLIC_BASE_URL isn't set —
        #    e.g. when the operator hasn't redeployed since 1.4.2 and the
        #    env var was never injected. In that case we fall back to the
        #    1.4.1 gates (process + conf + local curl) which still catch
        #    most fake-failover cases.
        if PUBLIC_BASE_URL:
            if not _verify_dashboard_reachable_via_alb(
                tenant_id, PUBLIC_BASE_URL, timeout_sec=30, poll_sec=3
            ):
                raise RuntimeError(
                    f"dashboard not reachable via ALB at {PUBLIC_BASE_URL}/vm/{tenant_id}/ "
                    f"after 30s of polling (5xx or connection refused). "
                    f"Refusing to flip DDB — operator must investigate."
                )
        else:
            print(
                "WARN: PUBLIC_BASE_URL not set; skipping cross-ALB verify gate. "
                "Redeploy with 1.4.2+ stack to enable end-to-end dashboard verification."
            )

        # 7) Best-effort: tell source host to clean its nginx conf.
        #    If the source host is unreachable (which is the whole reason
        #    we're failing over), this SSM call will time out — that's fine,
        #    we don't gate failover success on it.
        if source_host_id:
            try:
                ssm.send_command(
                    InstanceIds=[source_host_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={
                        "commands": [
                            f"sudo rm -f /etc/nginx/conf.d/tenants/{tenant_id}.conf "
                            f"&& sudo nginx -s reload || true"
                        ],
                        "executionTimeout": ["10"],
                    },
                )
            except Exception:
                pass  # Source unreachable is the expected case.

        # 8) Flip ownership in DDB. ONLY now that every gate has passed.
        #    #排雷 D2 — target host 的 next_vm_num/used_*/vm_count 记账已在
        #    _reserve_target_vm_num 的 CAS 里原子完成(launch 前),这里**只翻 tenant 记录**,
        #    绝不再无条件 SET next_vm_num=target+1(那会覆盖并发 create 的递增 → 串号,
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET host_id = :h, vm_num = :n, #s = :running, restored_from = :b"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":h": target_host_id,
                ":n": target_vm_num,
                ":running": "running",
                ":b": backup_key,
            },
        )

        _emit_audit(
            "AZ_FAILOVER_TENANT_RECOVERED",
            {
                "tenant_id": tenant_id,
                "from_az": source_az,
                "to_host": target_host_id,
                "to_az": target_host.get("az", ""),
                "restored_from": backup_key,
            },
        )
        return True
    except Exception as e:
        print(f"failover failed for {tenant_id}: {e}")
        # 1.4.2: distinguish three failure shapes so operators / tests
        # can tell what was actually wrong:
        #   - failover_failed_partial: VM verified up on target, but ALB
        #     repoint or cross-ALB probe failed → DDB still points at
        #     source. Manual ALB cleanup may be needed.
        #   - failover_failed: VM verify itself failed → target is in an
        #     unknown state. host-agent should garbage-collect.
        err_str = str(e)
        is_partial = (
            "ALB repoint failed" in err_str
            or "dashboard not reachable" in err_str
            or "no private_ip" in err_str
        )
        new_status = "failover_failed_partial" if is_partial else "failover_failed"
        try:
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :failed, failover_error = :e",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":failed": new_status,
                    ":e": err_str[:500],
                },
            )
        except Exception as e:
            # below, but a silent status miss means the next sweep keeps
            # retrying failover on a tenant that's already broken.
            print(f"az-failover {tenant_id}: mark {new_status} failed (non-fatal): {e}")
        _emit_audit(
            "AZ_FAILOVER_TENANT_FAILED",
            {
                "tenant_id": tenant_id,
                "error": err_str[:200],
                "status_set": new_status,
            },
        )
        return False


def _find_latest_backup_key(tenant_id):
    """Return the most recent backup S3 key for a tenant, or None.

    Backups are uploaded by backup-data.sh as
        s3://${ASSETS_BUCKET}/backups/<tenant_id>/<ISO timestamp>.gz
    There is no 'latest.gz' alias — we list and sort by LastModified.
    """
    if not ASSETS_BUCKET or not tenant_id:
        return None
    try:
        prefix = f"backups/{tenant_id}/"
        resp = s3.list_objects_v2(Bucket=ASSETS_BUCKET, Prefix=prefix, MaxKeys=1000)
        objs = resp.get("Contents") or []
        if not objs:
            return None
        # Most recent first by LastModified, return key only (no s3:// prefix)
        # because launch-vm.sh expects the key, not the full URI.
        objs.sort(key=lambda o: o.get("LastModified"), reverse=True)
        return objs[0]["Key"]
    except Exception as e:
        print(f"_find_latest_backup_key({tenant_id}) error: {e}")
        return None


def _wait_ssm_done(command_id, instance_id, timeout_sec=90, poll_sec=3):
    """Block until an SSM command completes. Returns (ok, error_or_None)."""
    import time as _t

    deadline = _t.time() + timeout_sec
    last_status = "Pending"
    while _t.time() < deadline:
        _t.sleep(poll_sec)
        try:
            inv = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        except Exception as e:
            return False, f"get_command_invocation: {e}"
        last_status = inv.get("Status", "Unknown")
        if last_status in ("Success",):
            return True, None
        if last_status in ("Cancelled", "TimedOut", "Failed"):
            err = (inv.get("StandardErrorContent") or "")[:500]
            return False, f"SSM {last_status}: {err}"
        # else: keep polling (InProgress, Pending, Delayed)
    return False, f"SSM timeout after {timeout_sec}s (last_status={last_status})"


def _verify_vm_actually_running(host_id, tenant_id, timeout_sec=90, poll_sec=10):
    """Cross-verify that a tenant's VM is really running on a host.

    Why this exists (1.3.2 / strengthened 1.4.2):
      SSM exit code from launch-vm.sh isn't always reliable. A transient
      kernel race (TUNSETIFF EBUSY on a stale tap, e.g.) can make the
      first launch attempt exit non-zero, but host-agent's auto-recovery
      loop (every 5s, see host-agent.py::_recover_vm) often picks it up
      and retries successfully a few seconds later. Without this verify
      step, the Lambda would mark the tenant ``failover_failed`` even
      though the VM is up.

    What we check (1.4.2 — three signals, all must pass):
      1. Firecracker process exists with the right ``api-sock`` path.
      2. The nginx tenant config file exists.
      3. **NEW (#fake-failover fix)**: ``curl`` against
         ``http://127.0.0.1/vm/<tid>/`` returns a *non-5xx* status.
         A 200/302/401/403 all count as "service is alive and serving"
         — only 5xx or connection-refused mean nginx is up but the VM
         backend is dead. Without this, a Firecracker process whose
         guest never finished booting would still pass verification
         and we'd flip DDB to ``running`` for a dashboard nobody can
         actually open. That is exactly the bug operators reported.

    Implementation: send a small SSM probe and poll get_command_invocation.
    Returns True iff all three signals are present within ``timeout_sec``.
    Best-effort; on any AWS error we conservatively return False so the
    Lambda still marks failure (no false-positive success reports).
    """
    import time as _t

    deadline = _t.time() + timeout_sec
    # The probe runs as one shell command that prints VERIFIED only when
    # all three checks pass. Using `--max-time 5` keeps the probe itself
    # fast; the host-side nginx → VM:18789 path should respond in <500ms.
    probe_cmd = (
        f"pgrep -f 'api-sock /data/firecracker-vms/{tenant_id}/fc.sock' "
        f">/dev/null || (echo NOT_RUNNING_NO_PROCESS; exit 1); "
        f"test -f /etc/nginx/conf.d/tenants/{tenant_id}.conf "
        f"|| (echo NOT_RUNNING_NO_NGINX_CONF; exit 1); "
        # curl returns the HTTP status code only. We accept anything < 500
        # as proof the VM backend is at least reachable through nginx.
        # 000 = curl could not connect (nginx not reloaded, backend down).
        # 5xx = nginx returned a backend error.
        # Anything else = the request reached *some* HTTP-speaking process.
        f"code=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 "
        f"http://127.0.0.1/vm/{tenant_id}/); "
        f'if [ "$code" = "000" ] || [ "$code" -ge 500 ] 2>/dev/null; then '
        f"echo NOT_RUNNING_HTTP_$code; exit 1; "
        f"else echo VERIFIED_HTTP_$code; fi"
    )
    while _t.time() < deadline:
        try:
            resp = ssm.send_command(
                InstanceIds=[host_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [probe_cmd], "executionTimeout": ["15"]},
            )
            cmd_id = resp["Command"]["CommandId"]
            ok, _ = _wait_ssm_done(cmd_id, host_id, timeout_sec=20, poll_sec=2)
            if ok:
                inv = ssm.get_command_invocation(
                    CommandId=cmd_id,
                    InstanceId=host_id,
                )
                stdout = inv.get("StandardOutputContent") or ""
                if "VERIFIED" in stdout:
                    return True
                # Probe ran successfully but reports NOT_RUNNING_*.
                # Print the reason to CloudWatch logs so operators can
                # diagnose whether it's process / config / HTTP failure.
                print(f"verify_vm probe says: {stdout.strip()[:200]}")
        except Exception as e:
            print(f"verify_vm_actually_running probe error: {e}")
        _t.sleep(poll_sec)
    return False


def _verify_dashboard_reachable_via_alb(
    tenant_id, public_base_url, timeout_sec=30, poll_sec=3
):
    """Cross-check that the tenant's dashboard URL is **reachable through
    the public path** (ALB → nginx → VM), not just locally on the host.

    Why this exists (1.4.2 — the core fix for the 'fake failover' bug):
      The previous health_check Lambda flipped DDB ``status=running`` and
      emitted ``AZ_FAILOVER_TENANT_RECOVERED`` as long as
      (a) launch-vm.sh exited 0 OR the local SSM verify probe passed, and
      (b) ALB rule re-pointing didn't throw — and even ALB throwing was
      swallowed and didn't fail the failover. That meant operators saw
      audit-log success while the dashboard URL still 502'd because:
        - ALB rule was never updated to point at the new host's TG, or
        - the new host's nginx config never reloaded, or
        - the VM came up but the OpenClaw service inside hadn't started.

      This verify gate is the public-path reality check: hit the same URL
      a real user would hit, through CloudFront's origin (the ALB), and
      only declare success if the response code is **non-5xx** within
      ``timeout_sec``. That guarantees that flipping DDB → running
      coincides with the dashboard actually working for end users.

    Returns True iff a non-5xx response is received within the deadline.
    Returns False on connection refused, timeout, or persistent 5xx.

    NOTE: ``public_base_url`` should be the ALB DNS (or CloudFront domain)
    *without* trailing slash, e.g.
    ``http://openclaw-alb-12345.ap-northeast-1.elb.amazonaws.com``. When
    the deployment uses a custom domain via CloudFront, the API Gateway's
    ALB origin is still the right target — CloudFront caches don't matter
    here because we're probing freshness anyway.
    """
    import time as _t
    import urllib.request
    import urllib.error

    if not public_base_url:
        # Couldn't resolve a base URL — fail closed. Operator must inject
        # PUBLIC_BASE_URL via CDK or skip this gate explicitly.
        print("cross-ALB verify SKIPPED: PUBLIC_BASE_URL not set")
        return False

    base = public_base_url.rstrip("/")
    url = f"{base}/vm/{tenant_id}/"
    deadline = _t.time() + timeout_sec
    last_status = None
    last_err = None
    while _t.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            # Force IPv4-friendly behavior; ALB targets are usually private
            # IPv4 only. Don't follow redirects — a 302 is fine evidence.
            with urllib.request.urlopen(req, timeout=5) as resp:
                last_status = resp.status
                if last_status < 500:
                    return True
        except urllib.error.HTTPError as e:
            # 4xx is reachable evidence (auth challenge, CORS preflight,
            # etc.) — only 5xx counts as backend dead.
            last_status = e.code
            if e.code < 500:
                return True
        except urllib.error.URLError as e:
            last_err = str(e.reason) if hasattr(e, "reason") else str(e)
        except Exception as e:
            last_err = str(e)
        _t.sleep(poll_sec)
    print(
        f"cross-ALB verify FAILED for {tenant_id}: "
        f"last_status={last_status}, last_err={last_err}"
    )
    return False


def _repoint_alb_rule(tenant_id, target_host_id, target_private_ip):
    """Update the ALB rule for /vm/<tenant_id>* to point at target host's TG.

    Each host has a target group named oc-<last8 of instance_id>. Each
    tenant has one ALB rule whose Action.TargetGroupArn determines which
    host serves /vm/<tenant_id>* traffic. After a cross-host migration
    or failover, this Action must be updated, otherwise CloudFront/ALB
    keeps sending traffic to the dead source host.
    """
    if not ALB_LISTENER_ARN:
        return

    # 1) Find or create the target host's target group, register its IP.
    tg_name = f"oc-{target_host_id[-8:]}"
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
    except Exception:
        # Host TG doesn't exist yet (e.g. host registered without API path).
        # Need VPC ID for create — pulled from existing TG of source host.
        existing = elbv2.describe_target_groups()["TargetGroups"]
        if not existing:
            raise RuntimeError("no existing target groups to clone VPC from")
        vpc_id = existing[0]["VpcId"]
        tg_arn = elbv2.create_target_group(
            Name=tg_name,
            Protocol="HTTP",
            Port=8899,
            VpcId=vpc_id,
            TargetType="ip",
            HealthCheckPath="/health",
            HealthCheckIntervalSeconds=10,
            HealthyThresholdCount=2,
        )["TargetGroups"][0]["TargetGroupArn"]
    # Make sure the host IP is registered (idempotent).
    try:
        elbv2.register_targets(
            TargetGroupArn=tg_arn,
            Targets=[{"Id": target_private_ip, "Port": 8899}],
        )
    except Exception as e:
        print(f"register_targets {target_private_ip} on {tg_name}: {e}")

    # 2) Find the existing ALB rule for /vm/<tenant_id>* and modify its
    #    forward Action to point at the new target group.
    rules = elbv2.describe_rules(ListenerArn=ALB_LISTENER_ARN)["Rules"]
    rule_arn = None
    for r in rules:
        for c in r.get("Conditions", []):
            if c.get("Field") == "path-pattern" and any(
                f"/vm/{tenant_id}" in v for v in c.get("Values", [])
            ):
                rule_arn = r["RuleArn"]
                break
        if rule_arn:
            break
    if not rule_arn:
        # No existing rule — create a fresh one. Pick a free priority.
        used = {int(r["Priority"]) for r in rules if r["Priority"] != "default"}
        priority = next(i for i in range(1, 500) if i not in used)
        elbv2.create_rule(
            ListenerArn=ALB_LISTENER_ARN,
            Priority=priority,
            Conditions=[
                {
                    "Field": "path-pattern",
                    "Values": [f"/vm/{tenant_id}", f"/vm/{tenant_id}/*"],
                }
            ],
            Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )
    else:
        elbv2.modify_rule(
            RuleArn=rule_arn,
            Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )


def _emit_audit(operation, detail):
    """Best-effort audit log entry. Never raises."""
    if not audit_table:
        return
    try:
        import uuid

        ttl = int(datetime.now(timezone.utc).timestamp()) + 90 * 86400
        audit_table.put_item(
            Item={
                "pk": "audit",
                "id": str(uuid.uuid4()),
                "ts": datetime.now(timezone.utc).isoformat(),
                "operation": operation,
                "resource_id": detail.get("tenant_id") or detail.get("az") or "",
                "api_key_id": "system:health-check-lambda",
                "response_status": 200,
                "detail": json.dumps(detail)[:1000],
                "expires_ttl": ttl,
            }
        )
    except Exception as e:
        print(f"audit emit failed (non-fatal): {e}")


def _emit_sns_notification(az, outage, recovered_count):
    """Publish an AZ failover event to SNS if a topic is configured."""
    if not _SNS_TOPIC_ARN:
        return
    try:
        sns.publish(
            TopicArn=_SNS_TOPIC_ARN,
            Subject=f"[OpenClaw] AZ failover triggered: {az}",
            Message=json.dumps(
                {
                    "event": "az_failover",
                    "az": az,
                    "host_ids": outage["host_ids"],
                    "host_count": outage["host_count"],
                    "tenants_recovered": recovered_count,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
        )
    except Exception as e:
        print(f"SNS publish failed (non-fatal): {e}")
