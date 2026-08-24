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

import sys
import os
import json
import shlex
import time
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

#
# Lambda 运行时:本包目录(deploy/lambda/health_check/ 或 scaler/)就是部署根,`import ddb_scan`
# 天然可解析。但测试用 `importlib.util.spec_from_file_location(name, ".../handler.py")` 按
# 【文件路径】加载时,Python **不会**把该文件所在目录放进 sys.path —— 于是裸导入 ModuleNotFound。
# 实测代价:不加这两行,43 个既有用例(6 个文件)集体 ModuleNotFoundError。
#
# 为什么改生产侧而不是给那 6 个测试文件各加一行 sys.path:那是一笔【持续的税】——
# 以后每一个加载这两个 handler 的新测试都得记得加,而"忘记加"的表现是整文件 collection error,
# 与本 issue 要消灭的静默失效同族。这里两行、就近、有注释,一次付清。
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
import ddb_scan  # #432 —— Scan 必须翻页(本包独立打包,故各带一份)
from datetime import datetime, timezone

ddb = boto3.resource("dynamodb")
ssm = boto3.client(
    "ssm", config=Config(retries={"max_attempts": 8, "mode": "adaptive"})
)
sns = boto3.client("sns")
s3 = boto3.client("s3")
elbv2 = boto3.client("elbv2")
# 冷启动一次、单测直接替 handler.cloudwatch 注入 mock(与本文件其它 client 同款做法)。
cloudwatch = boto3.client("cloudwatch")
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

#
# 为什么需要:suspend/restore 用 CAS 抢单赢家中间态(tenant_service.py:3023 / :3301),
# 正常路径有回滚(:3040 / :3317),但回滚【只在 Lambda 还活着、能执行到那几行时才发生】。
# Lambda 被杀(consumer 超时/OOM/部署中断/SSM 长时无响应)→ 状态永久停在中间态。dispatch
# 侧有 stale 回收(DISPATCH_CLAIM_STALE_SEC),生命周期中间态此前【没有任何等价机制】。
# 5 次卡死后整台 host(约 380 个租户)的生命周期操作全部停止受理;而 P2 使卡死租户连
# delete 都被拒,卡死数量只增不减。
#
# 取值必须【显著大于】lifecycle consumer 的硬超时,否则会把仍在合法执行的 suspend 误判
# 成卡死。实测口径(deploy/stacks/lambdas.py:232-235):consumer timeout=900s、队列
# visibility=960s(=900+60 余量)。既有 CREATING_TIMEOUT_SECONDS 取 900 与 consumer 相等
# 是安全的 —— `creating` 的执行者是 fire-and-forget 的 launch,不占 consumer 槽;而
# `suspending` 的执行者【就是】consumer 本身,所以必须留余量:1200 = 960 + 240 缓冲。
LIFECYCLE_STUCK_TIMEOUT_SECONDS = int(
    os.environ.get("LIFECYCLE_STUCK_TIMEOUT_SECONDS", "1200")
)

# 单次 invocation 里本 sweep 最多处理多少个卡死租户。与 _REAP_STOP_CONFIRM_MAX 同款
# 意图(见其注释):每个判定要发一条同步 SSM 探 host,不设上限则数台不可达 host 上的卡死
# 租户会串行耗光整个 invocation,把后面的 orphan 清扫 / AZ failover / 迁移 sweep 饿死。
_LIFECYCLE_STUCK_MAX_PER_SWEEP = int(
    os.environ.get("LIFECYCLE_STUCK_MAX_PER_SWEEP", "10")
)

# AZ failover configuration (read from env, populated by stack.py).
AZ_FAILOVER_ENABLED = os.environ.get("AZ_FAILOVER_ENABLED", "false").lower() == "true"
AZ_UNHEALTHY_THRESHOLD_MINUTES = int(
    os.environ.get("AZ_UNHEALTHY_THRESHOLD_MINUTES", "10")
)
AZ_COOLDOWN_MINUTES = int(os.environ.get("AZ_COOLDOWN_MINUTES", "30"))
# 是同一个物理问题(这台机器还在报心跳吗),不为同一件事再开一个语义重复的旋钮;要单独调
# 已陈旧 1 小时的僵尸账本,让调度把新租户派到一台 stopped 的 host,租户卡 creating 16m50s。
# 当时无任何机制降级它 —— 心跳只被 AZ 级 failover 读(要整个 AZ 全挂才触发),调度只看 status。
STALE_HOST_DEMOTE_MINUTES = int(
    os.environ.get("STALE_HOST_DEMOTE_MINUTES", str(AZ_UNHEALTHY_THRESHOLD_MINUTES))
)
# P0 停滞的正解,默认关等于没修。
STALE_HOST_DEMOTE_ENABLED = (
    os.environ.get("STALE_HOST_DEMOTE_ENABLED", "true").lower() == "true"
)
# 正常 failover 自身也会处于 failover_recovering，且单次 Lambda 最长可跑到 180s。
# 默认留 15 分钟的大余量，避免把仍在健康推进的迁移误判成悬挂并抽走它正在使用的号。
FAILOVER_STUCK_MINUTES = int(os.environ.get("FAILOVER_STUCK_MINUTES", "15"))
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
# backup-data.sh:16 `BUCKET="${2:-${BACKUP_BUCKET:-${ASSETS_BUCKET}}}"` —— host 上注入了
# BACKUP_BUCKET(WORM+CMK 专用桶)时备份就写在那儿,只读 ASSETS_BUCKET 会永远 list 空,
# 于是每个租户都命中 no-backup 拒绝 = AZ failover 实质不可用。回退 ASSETS_BUCKET 是为了
# 兼容没注入 BACKUP_BUCKET 的旧部署。
BACKUP_BUCKET = os.environ.get("BACKUP_BUCKET") or ASSETS_BUCKET
BACKUP_PREFIX = os.environ.get("BACKUP_PREFIX", "backups")
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
    # 永远不被健康检查,坏了也没人发现。openclaw-tenants 实测 6790 行 / 2.83MB,
    # 已超 1MB 近三倍 —— 这一处现在就在漏。
    tenants = ddb_scan.scan_all(
        tenants_table,
        FilterExpression=(
            "#s = :r AND (attribute_not_exists(synthetic) OR synthetic <> :true)"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "running", ":true": True},
    )

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

    # 放在 creating-reaper 之后:两者都要 scan tenants,但本 sweep 还要发同步 SSM 探 host,
    # 故受 _LIFECYCLE_STUCK_MAX_PER_SWEEP 限额,不与后面的 orphan/failover/迁移 争预算。
    # 三个计数都要打:unconfirmed 非零意味着"有租户卡着但连状态都确认不了",那是比卡死
    try:
        _lc_rb, _lc_marked, _lc_unconf = _reap_stuck_lifecycle(now)
        if _lc_rb or _lc_marked or _lc_unconf:
            print(
                f"lifecycle-stuck: {_lc_rb} rolled back, {_lc_marked} currently marked stuck, "
                f"{_lc_unconf} unconfirmed (host unreachable or over per-sweep budget)"
            )
        _emit_stuck_metric(_lc_marked, _lc_unconf)
    except Exception as e:
        # 与其它 sweep 同约定:巡检永远不该拖垮 watchdog。
        print(f"lifecycle-stuck error (non-fatal): {e}")

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

    # ------- #491 物理号孤儿对账 -------
    try:
        cleaned = reap_orphan_phys_slots(dry_run=False)
        print(f"reap-phys-slots: cleaned={cleaned}")
    except Exception as e:
        print(f"reap phys-slot error (non-fatal): {e}")

    # ------- #500 卡死 failover_recovering 接手 -------
    try:
        stuck_failovers = reap_stuck_failover_recovering(dry_run=False)
        print(f"reap-failover-recovering: summary={stuck_failovers}")
    except Exception as e:
        print(f"reap failover-recovering error (non-fatal): {e}")

    # 放在 AZ failover 之前:两者读同一个心跳判据,先把单台僵尸降下去,AZ 判定看到的
    # 就是更接近真相的机队视图。非致命 —— 降级失败不该让整个 watchdog 停摆。
    try:
        stale_demoted = demote_stale_hosts(dry_run=False)
        if stale_demoted["demoted"] or stale_demoted["skipped_ssm_ok"]:
            print(f"stale-demote: summary={json.dumps(stale_demoted)}")
    except Exception as e:
        print(f"stale-demote error (non-fatal): {e}")

    try:
        agent_signals = sweep_agent_signals(dry_run=False)
        if agent_signals["spontaneous_restart"] or agent_signals["foreign_vm_total"]:
            print(f"agent-signals: summary={json.dumps(agent_signals)}")
    except Exception as e:
        print(f"agent-signals error (non-fatal): {e}")

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
        migrating = ddb_scan.scan_all(
            tenants_table,
            FilterExpression="#s = :m",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":m": "migrating"},
        )
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
# ㉓ 回滚前要求租约【已过期至少这么久】(codex 第十五轮)。
#
# 第九轮起 reaper 只在 `active_lifecycle_until <= now` 时回滚,而 suspend 的破坏性命令
# 都带 host_guard(guard 会校验租约未过期,过期即 exit 79 什么都不做)。但 Codex 指出
# 租约过期【不证明没有在途命令】:一条 stop 命令可能在过期前一瞬通过了它的前置 guard,
# 随后才真正执行 —— 而 reaper 此刻探到 VM 还活着、把行回滚成活跃态,那条命令接着把 VM
#
# 这个窗口【无法在当前架构下完全关闭】:per-tenant flock 在 host 上,而 reaper 的回滚是
# Lambda 里的一次 DDB 写,锁跨不过去。要全关需把回滚本身下沉到 host(在锁内做探测+决策),
# 那是 reaper 的架构改动,已记入 UNRESOLVED_GAPS。
#
# 但可以把它从"过期瞬间的秒级竞态"收窄成【被 SSM 超时上界界定】的窗口:多等一段宽限期
# 再回滚。suspend 的两条破坏性命令用的都是默认 timeout=30s
# (tenant_service :3735 的 stop-vm、:3775 的 rm -rf),所以 300s 宽限远超它们的执行上界 ——
# 一条在过期前通过 guard 的命令,300s 后必然已经结束(成功或超时),而它的结果会被下一轮
# 的探测看到,于是 reaper 的判断基于事实而不是竞态。
# 代价:卡死租户的自动回滚多等 5 分钟。相对 1200s 的判定阈值,这是可接受的。
LIFECYCLE_ROLLBACK_LEASE_GRACE_SECONDS = int(
    os.environ.get("LIFECYCLE_ROLLBACK_LEASE_GRACE_SECONDS", "300")
)

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
    # 新鲜度前置门(codex 独立复审第六轮)—— 旧版 stop-vm.sh 在【VM 目录已被回收】时
    # 直接 `exit 0` 报"已停",而 rm -rf 掉目录不会杀死持有那些 fd 的 Firecracker。
    # 于是本函数会对一个还在跑的 VM 返回"已确认停机",调用方据此释放容量 —— 正是
    # 本 Lambda 没有 host_script_self_heal(那是 api 包的),所以不自装,改为按本函数
    # 自己的原则 fail-closed:脚本缺失/过期(grep 不到新语义哨兵)→ 命令非零 →
    # ok=False → 本轮【不释放】,下轮再试。宁可慢,不可释放可能仍在跑的容量。
    # 哨兵与 tenant_service 强制删除那条路同源(OC_STOP_ORPHAN_NO_VMDIR),一处改、
    # 两处一起红。
    ok, _out = _ssm_run_capture(
        host_id,
        f"grep -q OC_STOP_ORPHAN_NO_VMDIR /home/ubuntu/stop-vm.sh && "
        f"/home/ubuntu/stop-vm.sh {_q_tid} {_q_vm}",
        timeout=STOP_CONFIRM_TIMEOUT,
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
        # 持有的 lifecycle fence 也一并释放。reapply 的 worker 在 rebuild_status=unconfirmed
        # (host adoption 回执没在 300s 窗内回来,但 host 其实成功)之后【继续持有】fence,
        # 指望 finalize_async_rebuild_success 来释放;而那个 finalize 的 ConditionExpression
        # 要求 rebuild_status=done,在 unconfirmed 路径上永不触发 → fence 会一直挂到 ~30min
        # 租约过期才被抢占,期间该租户的后续 lifecycle 动作全撞 409 LIFECYCLE_IN_FLIGHT。
        # 这条释放走【与上面完全相同的 op_id+epoch 条件】:若这期间来了新的 lifecycle 操作
        # 把 epoch 抬高了,整条 update 会 CCF 出局 —— 绝不会碰到别的 owner 的 fence。
        # 只对【带 fence 的 op】(op_id 与 epoch 都在)释放;不带 fence 的老记录行为不变。
        if op_id and fence_epoch is not None:
            expr += ", lifecycle_released_at = :t"
            expr += (
                " REMOVE active_lifecycle_op_id, active_lifecycle_action, "
                "active_lifecycle_until"
            )
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


#
# 覆盖 suspending / restoring。设计上【只做安全的事】:标记 + 回滚(仅在证明安全时)+ 告警,
# **绝不碰容量账本**。原因是实查结论,不是保守偏好:
#
#   · suspend 全程只有【两次】DDB 写 —— :3023 进 suspending、:3155 翻 suspended(与
#     restore_backup_key/suspended_at 同一条 update)。中途【零进度标记】,所以从 DDB
#     读不出"卡在哪一步"。判据只能来自 host 侧事实。
#   · 账本扣减在 :3149 `scheduling._release_slot`,而它【不幂等】(scheduling.py:123 只有
#     下溢守卫、无互斥锚)。若 reaper 也去扣,会与原 Lambda 恢复后的扣减【双扣】——
#     capacity_reservation_id 这个一次性令牌做互斥锚;suspend 路径【没有】这个锚。
#
# 于是分工:reaper 负责"把卡死变成可见 + 可操作",容量收敛交给 P2 的强制出口。要让
# predelete_backup_at),那要改 suspend 的破坏性路径,风险高一个量级 → 独立子项。
#
# 三种现场按 host 侧事实区分(见 _lifecycle_stuck_verdict 的判定矩阵)。


#
# 权威实现在 `deploy/lambda/api/services/tenant_service.py::_classify_release_cancel`。
# health_check 独立打包、**不能 import api/core**(见本文件 reap_orphan_phys_slots 的同一条
# 说明),所以这里必须自持一份。判定与常量值逐字一致,任何一侧改动必须同步改另一侧 ——
# `tests/test_438_hibernate_token_adversarial.py` 有一条对抗用例把同一组
# `CancellationReasons` 组合喂给两份实现,断言逐项相同,漂了就红。
#
# ⚠️ **TransactItems 顺序契约:[0]=host 账本项、[1]=tenant 令牌项。**
# 本文件里已有的 `_reap_orphan_reservations` 用的是【相反】的顺序([0]=tenant、[1]=host),
# 但它不按位次判(只把 TransactionCanceledException 一律当幂等跳过),所以两者不冲突。
# 新写的释放者必须用 host-first,否则这个判定会读错位次 —— 别去"统一"那个旧函数的顺序。
_REL_CONSUMED = "consumed"
_REL_ALREADY = "already"
_REL_RETRY = "retry"


def _classify_release_cancel(e, tenant_id, tag):
    """见上方说明。与 api 侧 `_classify_release_cancel` 逐字同源。"""
    retryable = {"TransactionConflict", "ThrottlingError",
                 "ProvisionedThroughputExceeded", "RequestLimitExceeded"}
    if not isinstance(e, ClientError):
        print(f"{tag}: token release {tenant_id} error (retry): {e}")
        return _REL_RETRY
    code = e.response["Error"]["Code"]
    if code == "TransactionCanceledException":
        reasons = e.response.get("CancellationReasons", []) or []

        def _code_at(idx):
            return reasons[idx].get("Code", "") if idx < len(reasons) else ""

        host_code, tenant_code = _code_at(0), _code_at(1)
        if host_code in retryable or tenant_code in retryable:
            print(f"{tag}: release {tenant_id} retryable cancel "
                  f"{[host_code, tenant_code]}")
            return _REL_RETRY
        if tenant_code == "ConditionalCheckFailed":
            return _REL_ALREADY
        if host_code == "ConditionalCheckFailed":
            print(f"{tag}: release {tenant_id} host underflow — retry+alarm")
            return _REL_RETRY
        print(f"{tag}: release {tenant_id} cancel w/o reasons — retry")
        return _REL_RETRY
    if code in retryable:
        print(f"{tag}: release {tenant_id} retryable error {code}")
        return _REL_RETRY
    print(f"{tag}: token release {tenant_id} error (retry): {e}")
    return _REL_RETRY


def _probe_host_tenant_state(host_id, tenant_id):
    """探一个租户在 host 上的真实状态。返回 (ok, {"vm_dir": bool, "fc_alive": bool})。

    ok=False 表示【探不到】(host 不可达 / SSM 失败) → 调用方必须【什么都不做】,留下轮再试。
    这条与 _confirm_vm_stopped 的"确认不了就不释放"同一条原则:时序不替代正确性。

    两个事实就够区分三种现场:
      · vm_dir  —— VM 目录是否还在。suspend 的 rm -rf 删的是整个 `/data/firecracker-vms/
        <tid>`(tenant_service.py:3134),故"目录不在" ⟺ 已过 rm -rf 那一步。
      · fc_alive —— Firecracker 是否还在跑。判据抄 stop-vm.sh:68 的权威形态:匹配
        `--api-sock <VM_DIR>/fc.sock`,而不是拿 tenant_id 去 pgrep 整条命令行(后者会被
        别的租户的路径子串命中 → 误判活 VM,no-cross-tenant 方向的错)。

    tenant_id 经 shlex.quote 进 root shell(纵深防御,与 _confirm_vm_stopped 同)。"""
    if not host_id:
        return False, {}
    _q_tid = shlex.quote(str(tenant_id))
    # 用 printf 输出两个固定标记,避免解析 ls/pgrep 的多样输出。
    cmd = (
        f'_d=/data/firecracker-vms/{_q_tid}; '
        f'if [ -d "$_d" ]; then echo VMDIR=yes; else echo VMDIR=no; fi; '
        f'_n=0; for _p in /proc/[0-9]*; do '
        # comm 而不是 exe 的 basename(codex 第十轮):二进制被替换/删除后
        # `readlink /proc/<pid>/exe` 返回 `... (deleted)`,basename 判据漏判 —— 而滚动
        # 升级换镜像正是这个场景。漏判 fc_alive 会让强制删除以为"VM 已停"而放行。
        # comm 恒为进程名、不带后缀,截断到 15 字符("firecracker" 11 字符,安全)。
        # 与 stop-vm.sh 的 _oc_is_firecracker 同一判据。
        f'  [ "$(cat "$_p/comm" 2>/dev/null)" = firecracker ] || continue; '
        f'  tr "\\0" " " < "$_p/cmdline" 2>/dev/null '
        f'    | grep -q -- "--api-sock $_d/fc.sock" && _n=$((_n+1)); '
        f'done; echo FC=$_n'
    )
    ok, out = _ssm_run_capture(host_id, cmd, timeout=STOP_CONFIRM_TIMEOUT)
    if not ok:
        return False, {}
    text = out or ""
    if "VMDIR=" not in text or "FC=" not in text:
        # 命令跑了但输出不完整(被截断/污染)→ 视作探不到,不猜。
        return False, {}
    vm_dir = "VMDIR=yes" in text
    fc_alive = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("FC="):
            try:
                fc_alive = int(line[3:]) > 0
            except ValueError:
                return False, {}
    return True, {"vm_dir": vm_dir, "fc_alive": fc_alive}


def _lifecycle_stuck_verdict(status, probe, has_token=False):
    """判定矩阵(纯函数:不碰 DDB、不发 SSM,故可被单测穷举 —— 抄 _rebuild_verdict 的形态)。

    入参 probe 是 _probe_host_tenant_state 的第二个返回值。返回
    (action, reason),action ∈ {"rollback", "mark_stuck", "finalize_suspend",
    "promote_restore"}(后两个仅在 has_token=True 时可能出现,见下方 #438 段)。

    | status     | vm_dir | fc_alive | 含义                          | 处置 |
    | suspending | yes    | yes      | 崩在 backup/stop 之前,VM 还活 | rollback(账本本就未扣) |
    | suspending | yes    | no       | 崩在 stop 与 rm 之间          | mark_stuck(盘还在,数据未丢,但备份是否已成不可知) |
    | suspending | no     | *        | 已过 rm -rf                   | mark_stuck(账本可能已扣,不敢回滚成活跃态) |
    | restoring  | yes    | yes      | 盘已恢复且 VM 已起,只是没翻态 | mark_stuck(推进成 running 需要证明 app 就绪,不在本函数职责内) |
    | restoring  | 其它    | *        | 恢复未完成                    | mark_stuck |

    为什么 restoring 一律 mark_stuck 而不回滚成 suspended:restore 的破坏性动作是
    "在新 host 上预留 slot + 起 VM"(tenant_service.py:3607 `_restore_reserve_slot`)。
    回滚成 suspended 就必须释放那个新 slot,而这又落回"账本不幂等"的同一个坑;且若 VM
    其实已经起来了,回滚成 suspended 等于谎报"已休眠"而实际有活 VM 在跑(#268 的形态)。

    ⚠️ 卡死 restoring 目前【没有自动出口】(codex 独立复审第四轮)。此处原先写"交给 P2
    强制出口",但那条出口对 restoring 是【破坏性】的,已被关掉(tenant_service.py:2467
    只放行 suspending)。根因是同一个:restore 直到最后那次 finalize update_item
    (tenant_service.py:3767)才把 host_id/vm_num 写进租户行,在那之前行里仍是【旧】
    host/vm_num,而 slot 预留在 `_find_host()` 选出的【新】host 上 —— 新 host 是谁
    从未落库。所以既不能回滚(不知道该释放哪台的账本),也不能删(按旧行去删会扣穿旧
    host 的账本、停错 VM、泄漏新 host 的预留)。
    要给它一个安全出口,前置是"目标 host/slot + 幂等 reservation 令牌先落库"
    (#412 的 capacity_reservation_id 那种锚),属独立子项。在那之前:标记 + 告警 +
    人工介入,由 openclaw-lifecycle-stuck-marked 与新增的 reaper 心跳告警保证可见。

    另注:上表 restoring 两行的 vm_dir/fc_alive 来自 :1122 `t.get("host_id")` 即【旧】
    host 的探测结果 —— 对 restoring 而言那不是 VM 真正被拉起的地方。因为 restoring 两个
    分支都 mark_stuck,【动作不受影响】,只有 reason 文案可能误导;要探对地方同样得等新
    host 落库,故此处不改,只标注。

    唯一敢回滚的是 suspending + 盘在 + VM 活:此时 suspend 的三个破坏性动作(backup、
    stop-vm、rm -rf)一个都没成功落地过(rm 没跑 ⟸ 盘在;stop 没成 ⟸ VM 活),账本也没动过
    (:3149 在 rm 之后),所以回滚到 prev 是把租户放回它本来的样子,零副作用。

    ── #438 —— `has_token=True` 时多两个【消费令牌】的收敛出口 ──────────────────
    上面那段"绝不碰容量账本"的论证有一个明确的前提:suspend 路径没有一次性锚,reaper 补扣
    会与原 Lambda 双扣。#438 给了锚(`suspend_release_id` / `restore_reservation_id`,与账本
    变更同事务),于是"令牌还在 ⟺ 账本还没扣"成立,补扣变得安全 —— **但只对下面两格**:

    | status     | 令牌 | vm_dir | fc_alive | 处置 |
    | suspending | 有   | no     | *        | `finalize_suspend`(盘已回收 = 破坏性步骤做完了,凭令牌把账本扣掉 + 翻 suspended) |
    | restoring  | 有   | yes    | yes      | `promote_restore`(盘在 + VM 活 = 恢复真的成了,凭令牌转正坐标,**不动账本**) |

    这两格为什么**不需要生命周期租约**(而 :1262-1287 那道租约门是为回滚设的):
      · 本函数的两个新动作都只写 DDB,**一条 host 侧命令都不发**,所以不存在"延迟落地的
        命令在回滚之后才生效"那条时序 —— 那才是租约门要挡的东西。
      · 互斥由令牌本身完成:原 Lambda 的收尾条件也是同一个 `<token> = :rid`,所以谁先到谁
        赢,后到的拿 ALREADY 幂等 no-op。账本恰好扣一次,与谁先跑无关。
      · `finalize_suspend` 做的事与原 Lambda 的收尾**逐字相同**(同一个事务形态、同一个
        令牌条件),它不是"另一个写手",而是"同一步由谁来做"。

    ⚠️ **已知盲区(未修)——`fc_alive` 认不出 Paused 的 VM。**
    `fc_alive` 是**进程存活**判据(`_probe_host_tenant_state` 匹配 `--api-sock <vm_dir>/fc.sock`
    的 firecracker 进程),而一个被 **Paused** 的 Firecracker 进程照样活着 ——
    `api/services/tenant_service.py` 的 ㉘ 段落(`_tenant_suspend` 里那个
    `vm_left_paused` 分支)已为 suspend 逐字记过同一条:「**reaper 救不了这一种**:它的
    fc_alive 是进程存活检查,一个 Paused 的 Firecracker 进程照样活着」。
    于是 `promote_restore` 理论上可能把一个**冻住的** VM 转正成 `running` —— 租户看着在跑、
    实际停在 Paused。
    部分兜住(不是修好):转正时主动写 `app_health = down`(见 `_promote_stuck_restore`),
    host-agent 下一 tick 探不到 gateway 应答就不会把它写回 `up`,所以就绪信号不会说谎。
    但「探不出 Paused」这个盲区本身**没有修**:要修得让 host 侧多吐一个 VM state 事实
    (类似 backup-data.sh 的 `OC_BACKUP_VM_LEFT_PAUSED` 哨兵那种),属独立子项。
    在那之前:`status=running` 且 `app_health` 长期 `down` 的租户要按"可能被冻住"排查。

    `restoring` 且 VM **没**起来那一格**仍然只标记**,不自动释放。这不是保守偏好:
    要安全地把它回滚成 suspended,必须同时具备 #469 第七/八/九轮确立的**两半** —— 租约
    (证明原操作不会再动手)**和** 命令上的 `host_guard`(挡住已在途的命令)。而 restore 的
    `ssm_dispatch._launch_vm` 目前【不带 guard】,给它加 guard 要动一个被 create/dispatch/
    restore 共用的函数。只有租约那一半的版本正是第七、八轮被证伪过的版本:一次延迟的
    launch 会在释放之后把 VM 拉起来,留下「row=suspended / 账本已扣 / VM 在跑」——
    孤儿 VM + 少记账(超卖方向)。故归独立子项,本轮只把标记做准(见下面探测目标的修正)。

    `has_token=False` 时逐字返回改动前的结果 —— 存量卡死行(#438 之前进的中间态)没有令牌,
    它们的处置必须与 bb 完全一致。"""
    vm_dir = bool(probe.get("vm_dir"))
    fc_alive = bool(probe.get("fc_alive"))
    if status == "suspending" and vm_dir and fc_alive:
        return "rollback", "suspend never reached a destructive step (disk + live VM)"
    if status == "suspending" and vm_dir:
        return "mark_stuck", "suspend stopped the VM but disk not reclaimed"
    if status == "suspending":
        if has_token:
            return (
                "finalize_suspend",
                "suspend reclaimed the disk and still holds its release token — "
                "the ledger provably has NOT been decremented, so finishing the "
                "original finalize is safe and idempotent",
            )
        return "mark_stuck", "suspend already reclaimed the disk (ledger state unknown)"
    if status == "restoring" and vm_dir and fc_alive:
        if has_token:
            return (
                "promote_restore",
                "restore booted the VM and still holds its reservation token — "
                "promoting the persisted coordinates finishes it without touching "
                "the ledger (capacity was already added at reserve time)",
            )
        return "mark_stuck", "restore booted the VM but never flipped to running"
    return "mark_stuck", "restore did not complete"


#
# 它在本文件里出现在三处(reaper 回滚、finalize_suspend、promote_restore),三处必须
# 逐字一致:少清一个字段就留下一条陈旧事实,而这些字段全都是下游判据的输入 ——
# `lifecycle_stuck_at` 是 P2 强制删除的准入凭据(残留 = 对健康租户开了 force-delete 的门),
# `suspend_backup_key` 残留会让 api 侧的重投续做支读到【上一轮】的备份 key
# (no-data-loss:两轮之间新写的数据会被恢复覆盖掉)。写三遍必然漂,故提成常量。
#
# 不含 `lifecycle_prev_status`:回滚要读它决定回到哪个态,所以它在各处单独列。
_STUCK_AND_TOKEN_REMOVES = (
    "lifecycle_stuck_at, lifecycle_stuck_vm_dir, lifecycle_stuck_fc_alive, "
    "updated_at_stuck_seen, lifecycle_rollback_deferred_until, "
    "lifecycle_probe_attempted_at, suspend_release_id, suspend_backup_key, "
    "restore_reservation_id, restore_host_id, restore_vm_num, "
    "restore_guest_ip, restore_host_port"
)


def _finalize_stuck_suspend(t, now):
    """#438 —— 凭 `suspend_release_id` 把一个卡死的 suspend 收尾:扣 host 账本 + 翻
    `suspended` + 消费令牌,一个 TransactWriteItems。返回 True 表示本轮把它收敛掉了。

    **与 api 侧 `tenant_service._suspend_finalize_txn` 是同一步**(同样的事务形态、同样的
    `#s = :suspending AND suspend_release_id = :rid` 条件),所以它不是"第二个写手":谁先到
    谁扣一次账本,后到的拿 ALREADY 幂等 no-op。

    `restore_backup_key` / `suspended_from` 取自库里的阶段快照
    (`suspend_backup_key` / `lifecycle_prev_status`)—— 这正是 #438 第一轮把它们落库的用途:
    原 Lambda 的内存已经没了,没有它们这一步就写不出可恢复的终态。缺任一即 fail-closed
    (退回标记),绝不猜:猜一个 backup_key 会让 restore 恢复到【别的时间点】的数据。

    不再调 `_confirm_vm_stopped`:调用方的探测已经拿到 `vm_dir=False AND fc_alive=False`,
    那比它更强(它只查进程)。而事务条件锁住 status+令牌,探测到写之间被人改过一律 CCF 出局。
    """
    tid = t.get("id")
    host_id = t.get("host_id")
    rid = t.get("suspend_release_id")
    backup_key = t.get("suspend_backup_key")
    prev = t.get("lifecycle_prev_status")
    if not (host_id and rid and backup_key and prev in ("running", "stopped")):
        print(
            f"lifecycle-stuck {tid}: finalize_suspend preconditions incomplete "
            f"(host={bool(host_id)} rid={bool(rid)} key={bool(backup_key)} "
            f"prev={prev!r}) — falling back to mark"
        )
        return False
    vcpu = int(t.get("vcpu", 0) or 0)
    mem_mb = int(t.get("mem_mb", 0) or 0)
    stamp = now.isoformat()
    try:
        hosts_table.meta.client.transact_write_items(TransactItems=[
            # 顺序契约:[0]=host、[1]=tenant(见 _classify_release_cancel 上方说明)。
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
            {"Update": {
                "TableName": tenants_table.table_name,
                "Key": {"id": tid},
                "UpdateExpression": (
                    "SET #s = :suspended, restore_backup_key = :bk, "
                    "suspended_at = :t, suspended_from = :prev, updated_at = :t, "
                    "lifecycle_stuck_reason = :r "
                    "REMOVE lifecycle_prev_status, " + _STUCK_AND_TOKEN_REMOVES
                ),
                # status + 令牌双锚。刻意【不】锁 updated_at:本动作与回滚不同 ——
                # 它做的事与原 Lambda 的收尾逐字相同,即使中间态被重新进入过(新一轮
                # suspend),那一轮的令牌也是新的,rid 不匹配自然 CCF,无需再拿
                # updated_at 当版本号。
                "ConditionExpression": (
                    "#s = :suspending AND suspend_release_id = :rid"
                ),
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":suspended": "suspended",
                    ":suspending": "suspending",
                    ":bk": backup_key,
                    ":prev": prev,
                    ":t": stamp,
                    ":rid": rid,
                    ":r": "reaped: finalized a stuck suspend via its release token",
                },
            }},
        ])
    except Exception as e:  # noqa: BLE001 — 判定在共享分类器里
        rel = _classify_release_cancel(e, tid, "lifecycle-stuck #438")
        if rel == _REL_ALREADY:
            # 令牌已被原 Lambda 的重投消费掉了 → 它自己收敛了,本轮无事可做。
            print(f"lifecycle-stuck {tid}: suspend already finalized by its own re-drive")
            return True
        print(f"lifecycle-stuck {tid}: finalize_suspend not applied ({rel}); retry next sweep")
        return False
    # 占号单独还(与 api 侧收尾同款分工:ps_<n> 有自己的 owner 条件,是独立幂等单元)。
    phys = t.get("phys_vm_num", t.get("vm_num"))
    if phys is not None:
        _release_phys_slot(host_id, phys, tid)
    print(
        f"lifecycle-stuck: {tid} suspending → suspended via token {rid} "
        f"(ledger decremented exactly once, backup_key from the persisted phase snapshot)"
    )
    _emit_audit(
        "tenant.lifecycle_converged",
        {"tenant_id": tid, "action": "finalize_suspend", "host_id": host_id},
    )
    return True


def _promote_stuck_restore(t, now):
    """#438 —— 凭 `restore_reservation_id` 把一个"VM 已起来但没翻态"的 restore 转正。

    **绝不碰账本**:容量在预留时(`_reserve_slot_on` 的事务)就加过了,转正只是把临时坐标
    变成永久坐标 —— 与 `dispatch_poller` 的 promote / host-agent 的 mark-running 同款。

    坐标全部【照抄】库里的 `restore_*`,零派生逻辑:guest_ip 的 /30 编址口径只有
    `api/core/auth._guest_ip` 一处,在这里再算一遍就是给跨租户网络串号开口子。

    `app_health = down` 与 api 侧收尾一致(#571):转正只证明 VM 起来了,不证明 app 就绪,
    由 host-agent 下一 tick 探到 gateway 应答再写回 up。

    ⚠️ 这个 `down` 同时是**那条已知盲区的部分兜底**:调用方的准入判据 `fc_alive` 只看
    firecracker 进程在不在,认不出 **Paused**,所以本函数可能把一个冻住的 VM 转正成
    `running`。写 `down` 让就绪信号不说谎(host-agent 探不到应答就不会写回 `up`),但盲区
    本身没修 —— 完整说明与出口在 `_lifecycle_stuck_verdict` 的「已知盲区」段。
    """
    tid = t.get("id")
    rid = t.get("restore_reservation_id")
    rhost = t.get("restore_host_id")
    rvm = t.get("restore_vm_num")
    rip = t.get("restore_guest_ip")
    rport = t.get("restore_host_port")
    if not (rid and rhost and rvm is not None and rip and rport is not None):
        print(
            f"lifecycle-stuck {tid}: promote_restore preconditions incomplete "
            f"(rid={bool(rid)} host={bool(rhost)} vm={rvm!r} ip={bool(rip)} "
            f"port={rport!r}) — falling back to mark"
        )
        return False
    stamp = now.isoformat()
    try:
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET #s = :running, host_id = :h, vm_num = :vn, phys_vm_num = :vn, "
                "guest_ip = :gip, host_port = :hp, updated_at = :t, "
                "app_health = :down, last_health_check = :t, "
                "lifecycle_stuck_reason = :r "
                "REMOVE suspended_at, restore_backup_key, suspended_from, "
                "lifecycle_prev_status, " + _STUCK_AND_TOKEN_REMOVES
            ),
            # 同 finalize_suspend:status + 令牌双锚就够,不需要 updated_at 当版本号。
            ConditionExpression=(
                "#s = :restoring AND restore_reservation_id = :rid"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":restoring": "restoring",
                ":rid": rid,
                ":h": rhost,
                ":vn": rvm,
                ":gip": rip,
                ":hp": rport,
                ":down": "down",
                ":t": stamp,
                ":r": "reaped: promoted a booted-but-unflipped restore via its token",
            },
        )
    except Exception as e:  # noqa: BLE001
        if _is_conditional_failure(e):
            # status 变了或令牌已被原 Lambda 消费 → 它自己收敛了。
            print(f"lifecycle-stuck {tid}: restore already finalized by its own re-drive")
            return True
        print(f"lifecycle-stuck {tid}: promote_restore failed ({e}); retry next sweep")
        return False
    print(
        f"lifecycle-stuck: {tid} restoring → running on {rhost} vm={rvm} "
        f"via token {rid} (ledger untouched — capacity was added at reserve time)"
    )
    _emit_audit(
        "tenant.lifecycle_converged",
        {"tenant_id": tid, "action": "promote_restore", "host_id": rhost},
    )
    return True


def _reap_stuck_lifecycle(now):
    """#469 P1/P3 —— 给卡在 suspending/restoring 的租户一个出口。

    返回 (rolled_back, marked, unconfirmed):回滚数、**当前卡着且已带标记的总数**
    (含本轮新标记与此前已标记的 —— 指标名 LifecycleStuckMarked 问的是"现在有多少
    卡着",不是"这一轮新增多少";后者会在稳态下掉回 0 而让告警误判问题已解决)、
    以及【探不到 host 而本轮跳过】的数量。第三个数必须被上报(见调用点):它代表"有租户卡着但我们连状态都
    确认不了",若它持续非零,是比卡死本身更需要人介入的信号(#469 P6 要求卡死可观测,
    而"探不到"是可观测性里最容易被静默掉的一类)。

    每一步都【先围栏再动作】:所有写都条件锁住当前中间态(#s = :cur),使一个正在正常
    收尾的 suspend/restore(它会把 status 翻走)让本 sweep CCF 出局 —— 这是 #412
    blocker-B 教训的直接套用:先 fence 再碰 host,否则会动到一个仍在合法执行的操作。"""
    rolled_back = 0
    marked = 0
    unconfirmed = 0
    stuck = []
    lek = None
    while True:
        kw = {
            "FilterExpression": (
                "(#s = :susp OR #s = :rest) AND "
                "(attribute_not_exists(synthetic) OR synthetic <> :true)"
            ),
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":susp": "suspending",
                ":rest": "restoring",
                ":true": True,
            },
        }
        if lek:
            kw["ExclusiveStartKey"] = lek
        page = tenants_table.scan(**kw)
        stuck += page.get("Items", [])
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break

    # ㉒ 探测预算必须【轮转】,否则前 10 个探不到的会永久饿死后面的(codex 第十五轮)。
    #
    # 缺陷链:预算固定 10 个/轮(_LIFECYCLE_STUCK_MAX_PER_SWEEP),而 processed 在【探测
    # 之前】就自增 —— 探不到也算用掉。DDB scan 的返回顺序是稳定的,所以只要前 10 个租户
    # 的 host 持续不可达,它们每轮都会占满预算,**后面的租户永远拿不到一次探测**:
    # 不被标记 → 拿不到 lifecycle_stuck_at → P2 的 force 出口对它们永久不可用,
    # 而 P4 说的正是"卡死数只增不减"。
    # 上面那道"已标记的跳过"守卫救不了它们 —— 它们从来【没被标记过】(探不到就不标记)。
    #
    # 修法:按 lifecycle_probe_attempted_at 升序排,最久没探过的先探。该字段在下面探测
    # 失败时写(成功的那些会被标记或回滚,自然离开候选集)。没有该字段的排最前 ——
    # 从未探过的优先级最高。
    # 为什么不用 lifecycle_stuck_at:那个字段一写上就触发饿死守卫、从此不再复探
    # (第八轮的教训),而这里要的恰恰是"下轮还得再看它"。
    stuck.sort(key=lambda _t: str(_t.get("lifecycle_probe_attempted_at") or ""))

    if stuck:
        print(
            f"lifecycle-stuck: scanned {len(stuck)} tenant(s) in suspending/restoring, "
            f"timeout={LIFECYCLE_STUCK_TIMEOUT_SECONDS}s"
        )
    processed = 0
    for t in stuck:
        tid = t.get("id")
        status = t.get("status")
        # updated_at 由 _cas_status(tenant_service.py:2984)在翻中间态时写,所以它就是
        # "进入该中间态的时刻",无需新增字段。缺失/不可解析 → 不猜,跳过(与 reap 同)。
        dt = _parse_iso(t.get("updated_at"))
        if dt is None:
            print(f"lifecycle-stuck-skip {tid}: no/bad updated_at")
            continue
        elapsed = (now - dt).total_seconds()
        if elapsed < LIFECYCLE_STUCK_TIMEOUT_SECONDS:
            continue  # 仍可能在合法执行(consumer 预算 900s)——绝不误判
        # 已标记的不再占探测预算(codex 独立复审第二轮抓出的饿死):标记是 P2 强制
        # 出口的前置,而预算固定 10 个/轮。若已标记的每轮仍占位,卡死数超过 10 之后
        # 后面的记录【永远】拿不到 lifecycle_stuck_at → 永远无法 force-delete,
        # 而 P4 说的正是"卡死数只增不减"。已标记者的状态不会再变(标记用
        # if_not_exists 且不 restamp),重复探它除了烧预算没有任何新信息。
        # 仍计入 marked 指标:告警要反映"当前有多少卡着",不能因为跳过探测就不报。
        if t.get("lifecycle_stuck_at"):
            marked += 1
            continue
        if processed >= _LIFECYCLE_STUCK_MAX_PER_SWEEP:
            # 超本轮预算:剩下的留下轮。计入 unconfirmed 让它在日志/指标里可见,
            # 否则"因为限额没看的"会与"探不到的"混在一起变成静默。
            unconfirmed += 1
            continue
        processed += 1

        # host_id 直到收尾才更新,窗口内它仍指 suspend 之前那台旧机。改动前这里一律用
        # host_id,所以对 restoring 是【探错地方】—— 本函数原 docstring 自己标注过这条,
        # 才修得了。旧行没有该字段 → 回落 host_id,行为与改动前一致。
        host_id = t.get("restore_host_id") or t.get("host_id", "")
        ok, probe = _probe_host_tenant_state(host_id, tid)
        if not ok:
            unconfirmed += 1
            # 记下"这一轮探过它"(codex 第十五轮)。只用于上面那次排序,让预算在不可达
            # 租户之间【轮转】而不是永远停在前 10 个。best-effort:写失败只是这一轮的
            # 排序不理想,不影响任何判定,所以不 raise、不改任何状态。
            # 刻意不写 lifecycle_stuck_at —— 那会让它落进饿死守卫、再也不被复探。
            try:
                tenants_table.update_item(
                    Key={"id": tid},
                    UpdateExpression="SET lifecycle_probe_attempted_at = :t",
                    # 同款围栏:只在它仍处于本轮读到的那个中间态时写,避免给一次全新的
                    # 操作留下陈旧字段。
                    ConditionExpression="#s = :cur",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":t": now.isoformat(), ":cur": status},
                )
            except Exception as e:  # noqa: BLE001 — 纯排序辅助,失败无害
                if not _is_conditional_failure(e):
                    print(f"lifecycle-stuck {tid}: probe-stamp write failed ({e})")
            print(
                f"lifecycle-stuck {tid}: host {host_id or '(none)'} unreachable — no action "
                f"this cycle (elapsed={int(elapsed)}s); retry next sweep"
            )
            continue

        # 拿错字段会让一个 suspending 行走上 restore 的判定分支。
        _tok = (
            t.get("suspend_release_id") if status == "suspending"
            else t.get("restore_reservation_id")
        )
        action, reason = _lifecycle_stuck_verdict(status, probe, has_token=bool(_tok))
        # 回滚目标只认【进中间态时原子记下】的 lifecycle_prev_status
        # (tenant_service._cas_status 的 stash_prev)。读不到就【不回滚】,退化成标记。
        #
        # 为什么不能兜底成 "running"(codex 独立复审 blocker):`suspended_from` 只在
        # suspend 走到终态时才写,卡在 suspending 的租户根本没到那一步、该字段不存在;
        # 而 suspend 允许从 running 【或 stopped】进入(tenant_service:3071)。写死
        # "running" 会把一个原本 stopped 的租户标成 running —— 「无 VM 却 running」的
        # 猜一个状态比留着卡死更糟:留着有标记、有告警、有 P2 出口;猜错则悄无声息地
        # 把租户置于一个它从未处于过的状态。
        # 命令都不发,互斥完全由令牌完成(与原 Lambda 的收尾同一个 rid 条件,谁先到谁赢),
        # 所以不存在租约门要挡的那条"延迟命令在写之后才落地"的时序。理由详见
        # 失败(前提不全 / 事务未应用)→ 退化成 mark_stuck 走下面的老路,绝不静默跳过。
        if action in ("finalize_suspend", "promote_restore"):
            _done = (
                _finalize_stuck_suspend(t, now) if action == "finalize_suspend"
                else _promote_stuck_restore(t, now)
            )
            if _done:
                # 计入 rolled_back：它与回滚同属"reaper 把租户带离了中间态"这一类
                # (指标 LifecycleStuckMarked 问的是"现在还有多少卡着",收敛掉的自然不算)。
                rolled_back += 1
                continue
            action, reason = "mark_stuck", f"{reason} — convergence deferred, marked"

        prev = t.get("lifecycle_prev_status")
        if action == "rollback" and not prev:
            action = "mark_stuck"
            reason = (
                "cannot determine pre-suspend status (no lifecycle_prev_status — "
                "tenant entered the intermediate state before this field existed); "
                "refusing to guess, marked for the force-delete/reset exit"
            )
        # ⑩ 回滚必须等【生命周期租约】到期,不只看时长(codex 第七轮提出,第九轮修正前提)。
        #
        # 两个时长本来就不齐:LIFECYCLE_STUCK_TIMEOUT_SECONDS 默认 1200s,而
        # LIFECYCLE_FENCE_LEASE_SECONDS 默认 1800s。中间那 600 秒里,本 sweep 认为它
        # "卡死了"并把行回滚成 prev(活跃态),而原来那次操作【仍持有效租约】,完全可以
        # 继续往下走 → VM 被停、盘被删,而它自己收尾的 CAS 因 status 已改而 CCF 失败 ——
        #
        # 本函数 docstring 里"suspend 的三个破坏性动作一个都没成功落地过"那句,只对
        # 【观测那一刻】成立,不保证未来 —— 这是那句话缺的一半。
        #
        # ⚠ 前提更正(codex 第九轮):我第七、八轮写这段时声称它守着 suspend 的窗口,
        # 那是【错的】—— `active_lifecycle_until` 只由 _FENCED_LIFECYCLE_ACTIONS 里的
        # 动作写,而 suspend 当时【不在】那个集合里,所以对一个 suspend 卡死的租户这道门
        # 通常根本不存在字段、完全空转。第九轮把 suspend 纳入了 fence,并给它的 stop-vm
        # 与 rm -rf 都加上 host_guard —— 于是这道门才真的有意义。
        #
        # 现在两者【组合】才闭合:host_guard 同时校验 owner + epoch + **租约未过期**
        # (core/lifecycle_fence.py:264),而本 sweep 只在租约过期后才回滚。所以任何延迟
        # 落地的 suspend 命令必然撞 LIFECYCLE_FENCE_EXPIRED、exit 79,一步都不做。
        # 单靠任一侧都不够:只有 guard 而不等租约,回滚会发生在 guard 仍然放行的窗口内;
        # 只等租约而没有 guard(即第七、八轮那两版),延迟命令照样删盘。
        #
        # 修法:租约还活着就【不回滚】。等租约过期后的下一轮再回滚 —— 那时原 worker 的
        # 每一个 host 侧动作都会被自己的 guard 挡掉。这与本文件"确认不了就不做"的原则
        # 同源:时序不替代正确性。
        _lease_until = int(t.get("active_lifecycle_until") or 0)
        _now_epoch = int(now.timestamp())
        # 宽限期(codex 第十五轮):不只要求租约过期,还要求【过期够久】——
        # 见 LIFECYCLE_ROLLBACK_LEASE_GRACE_SECONDS 上方的说明。
        # 属性不存在(存量租户/从未走过 fence)时 _lease_until=0,加宽限仍为过去时刻,
        # 所以这些租户的行为不变 —— 不能让它们被这道门挡住(那会让 P1 出口对存量全失效)。
        _rollback_ok_after = (
            _lease_until + LIFECYCLE_ROLLBACK_LEASE_GRACE_SECONDS if _lease_until else 0
        )
        if action == "rollback" and _rollback_ok_after > _now_epoch:
            # ⑫ codex 独立复审第八轮 —— 这里【不能】走 mark_stuck。
            #
            # 上一轮(第七轮)我写的是 `action = "mark_stuck"`,并在 reason 里承诺
            # "will roll back after the lease expires"。那句承诺兑现不了:上面 :1139
            # 的饿死守卫 `if t.get("lifecycle_stuck_at"): marked += 1; continue` 会让
            # 【任何已标记】的租户在后续每一轮都被跳过、不再复探。于是这个本来只是
            # "再等 10 分钟就能安全回滚"的租户被永久钉在标记态,自动出口彻底失效,
            # 只剩人工介入 —— 比原缺陷更糟(原缺陷至少会回滚,只是可能谎报)。
            #
            # 根因是那条守卫的前提变了:它的注释写着"已标记者的状态不会再变(标记用
            # if_not_exists 且不 restamp),重复探它除了烧预算没有任何新信息"。对第七轮
            # 之前的标记成立;而"因租约未到期而推迟"的标记【恰恰会变】—— 租约一过期,
            # 同一个租户的正确处置就从"等"变成"回滚"。
            #
            # 修法:租约推迟【不落 lifecycle_stuck_at】,只落一个独立的、会被 restamp 的
            # 字段 lifecycle_rollback_deferred_until,并本轮什么都不做。下一轮它没有
            # lifecycle_stuck_at,于是照常进入探测 → 租约过期后自然走回滚。
            # 代价:它每轮仍占一个探测预算。这是有意的 —— 它是一个【等着被自动修好】的
            # 租户,不是一个已判定卡死、只能等人工的租户,值得那一次探测。
            # 用 continue 而不是 action="skip":后者要在下面每个分支都加判断,而这里的
            # 语义就是"本轮不做任何处置"。
            _defer_left = _rollback_ok_after - _now_epoch
            print(
                f"lifecycle-stuck-defer {tid}: lease not yet expired-plus-grace, "
                f"{_defer_left}s to go (active_lifecycle_until={_lease_until} + "
                f"{LIFECYCLE_ROLLBACK_LEASE_GRACE_SECONDS}s grace); the original "
                "operation may still stop the VM and reclaim the disk, and suspend's "
                "destructive steps carry no host guard — NOT rolling back and NOT "
                "marking (a mark would make later sweeps skip it forever). Will "
                "re-evaluate next sweep."
            )
            try:
                tenants_table.update_item(
                    Key={"id": tid},
                    # 只记"推迟到什么时候",不碰 lifecycle_stuck_at。每轮 restamp
                    # (不用 if_not_exists)—— 它是当前观测,不是"首次卡死时刻"。
                    UpdateExpression=(
                        "SET lifecycle_rollback_deferred_until = :until, "
                        "lifecycle_stuck_reason = :r"
                    ),
                    # 同款围栏:状态与 updated_at 都锁,避免给一次全新的操作写上这个字段。
                    ConditionExpression="#s = :cur AND updated_at = :seen",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":until": _rollback_ok_after,
                        ":cur": status,
                        ":seen": t.get("updated_at"),
                        ":r": (
                            f"rollback deferred: lifecycle lease active for "
                            f"{_defer_left}s more (elapsed={int(elapsed)}s)"
                        ),
                    },
                )
            except Exception as e:  # noqa: BLE001 — best-effort,只是可观测性
                if not _is_conditional_failure(e):
                    print(f"lifecycle-stuck-defer {tid}: note write failed ({e})")
            # 计入 unconfirmed:它确实"卡着且本轮未被处置",与"探不到"同一类需要被看见的
            # 状态。若不计,一个持续被租约推迟的租户在指标上完全隐形。
            unconfirmed += 1
            continue
        if action == "rollback":
            try:
                tenants_table.update_item(
                    Key={"id": tid},
                    # 教训(_abort_restore_status 回滚时 REMOVE predelete_backup_at):标记只
                    # 对"本次卡死"有效,租户回到活跃态后可能再写新数据、再发起新的 suspend;
                    # 留着陈旧标记会让 P2 的 ?force=true 对一个【健康】租户放行 → 误删。
                    # **不清 `suspend_backup_key` 是一条 no-data-loss 缺陷**:它是回滚到
                    # 活跃态后【残留】的,而 api 侧的重投续做支恰好以「status=suspending
                    # + 令牌 + 阶段快照」为准入。于是下一轮 suspend 若在写自己的快照之前
                    # 崩溃,续做会读到【上一轮】的 backup_key,把 restore_backup_key 指向
                    # 判定过的同一条形态)。REMOVE 不存在的属性是 no-op,故对存量零影响。
                    UpdateExpression=(
                        "SET #s = :prev, updated_at = :t, lifecycle_stuck_reason = :r "
                        "REMOVE lifecycle_prev_status, "
                        + _STUCK_AND_TOKEN_REMOVES
                    ),
                    # 围栏必须【同时】锁 status 与 updated_at(codex 独立复审第二轮)。
                    # 只锁 status 挡不住这条时序:本 sweep 读到 suspending → 探 host →
                    # 期间那次旧 suspend 跑完(翻 suspended)、紧接着一次【新】suspend 又
                    # 进 suspending → 条件 `#s = :cur` 仍然成立,于是我们拿【旧】的 host
                    # 事实去回滚/标记一次全新的操作。
                    # updated_at 由 _cas_status 在每次翻中间态时重写,故它是这条链上现成
                    # 的版本号:值变了就说明中间态被重新进入过,必须 CCF 出局、下轮重来。
                    # 租约条件也要进 CAS(codex 第七轮):上面那次判断与这次写之间仍有
                    # 窗口 —— 原 worker 可能正好在此刻续租(lifecycle_fence 的 acquire 会
                    # 把 active_lifecycle_until 往后推)。只在决策处检查等于"检查完再动手"
                    # 的经典竞态,必须让写本身携带同一条件。
                    # 属性不存在(老租户/从未走过 fence 的路径)时视为无租约,故用
                    # `attribute_not_exists OR <= :now`。
                    ConditionExpression=(
                        "#s = :cur AND updated_at = :seen AND ("
                        "attribute_not_exists(active_lifecycle_until) OR "
                        # 写时也带宽限(codex 第十五轮):只判"已过期"会让这次写在
                        # 宽限期内成功,与上面的决策不一致。:now_epoch 已减去宽限。
                        "active_lifecycle_until <= :now_epoch)"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":prev": prev,
                        ":cur": status,
                        ":seen": t.get("updated_at"),
                        ":t": now.isoformat(),
                        # 减去宽限:等价于"active_lifecycle_until + grace <= now",
                        # 但 DDB 条件表达式不支持算术,所以把宽限挪到右侧的常量里。
                        ":now_epoch": _now_epoch - LIFECYCLE_ROLLBACK_LEASE_GRACE_SECONDS,
                        ":r": f"reaped: {reason} (elapsed={int(elapsed)}s)",
                    },
                )
            except Exception as e:
                if _is_conditional_failure(e):
                    print(f"lifecycle-stuck-skip {tid}: status moved (fence CCF)")
                else:
                    print(f"lifecycle-stuck-skip {tid}: rollback failed ({e})")
                continue
            rolled_back += 1
            print(
                f"lifecycle-stuck: {tid} {status} → {prev} (elapsed={int(elapsed)}s, "
                f"{reason}); ledger untouched (never decremented)"
            )
            continue

        # mark_stuck:留在中间态,但打上时间戳标记 —— 它同时是 P2 强制出口的准入凭据
        # (?force=true 只对【reaper 已判定卡死】的租户放行,不能由调用方自称卡死)。
        # 幂等:attribute_not_exists 保证 lifecycle_stuck_at 只记【第一次】判定时刻,
        # 重复 sweep 不 restamp(否则"卡了多久"这个信息会被每轮刷新掉,告警永远看不到增长)。
        try:
            tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression=(
                    "SET lifecycle_stuck_at = if_not_exists(lifecycle_stuck_at, :t), "
                    "lifecycle_stuck_reason = :r, updated_at_stuck_seen = :t, "
                    # 结构化落两个探测事实,而不是只留一句人读的 reason。P2 的强制删除
                    # 要靠它们决定走哪条删除路径(账本已扣与否决定能不能再扣一次),
                    # 靠 grep reason 文本太脆 —— 改一个字就把下游判据打断。
                    "lifecycle_stuck_vm_dir = :vd, lifecycle_stuck_fc_alive = :fa"
                ),
                # 同回滚那条围栏:status 与 updated_at 都要锁。只锁 status 时,
                # "旧 suspend 跑完 + 新 suspend 又进来"这条时序会让我们用旧 host 事实
                # 给一次全新的操作打上卡死标记 —— 而那个标记是 P2 强制删除的准入凭据,
                # 误标记就等于给一个正在正常执行的租户开了 force-delete 的门。
                ConditionExpression="#s = :cur AND updated_at = :seen",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":cur": status,
                    ":seen": t.get("updated_at"),
                    ":t": now.isoformat(),
                    ":r": f"{reason} (elapsed={int(elapsed)}s)",
                    ":vd": bool(probe.get("vm_dir")),
                    ":fa": bool(probe.get("fc_alive")),
                },
            )
        except Exception as e:
            if _is_conditional_failure(e):
                print(f"lifecycle-stuck-skip {tid}: status moved (fence CCF)")
            else:
                print(f"lifecycle-stuck-skip {tid}: mark failed ({e})")
            continue
        marked += 1
        _publish_stuck_event(tid, status, reason, int(elapsed), probe)
        print(
            f"lifecycle-stuck: {tid} stays {status}, marked (elapsed={int(elapsed)}s, "
            f"{reason}, vm_dir={probe.get('vm_dir')}, fc_alive={probe.get('fc_alive')}); "
            f"force-delete/reset now permitted"
        )
    return rolled_back, marked, unconfirmed


def _publish_stuck_event(tenant_id, status, reason, elapsed, probe):
    """把一次卡死判定写进 audit。

    复用本文件既有的 _emit_audit(:2762)—— 它已经用对了 audit 表的复合主键
    (pk="audit" HASH + ts RANGE)并带 TTL。我第一版自己拼了 Item(用 "id" 当主键),
    真机实测直接 ValidationException: Missing the key pk in the item —— 事件一条都
    没写进去(best-effort 的 try/except 把它变成了一行日志,更容易被忽略)。
    这类"自创 schema"的错误单测看不见(mock 表不校验主键),只有真机会说话。"""
    _emit_audit(
        "tenant.lifecycle_stuck",
        {
            "tenant_id": tenant_id,
            "status": status,
            "reason": reason,
            "elapsed_seconds": elapsed,
            "vm_dir_present": bool(probe.get("vm_dir")),
            "fc_alive": bool(probe.get("fc_alive")),
        },
    )


def _emit_stuck_metric(marked, unconfirmed):
    """把两个数发成 CloudWatch 自定义指标,供 alarm 订阅(#469 P6:卡死不能等客户报障)。

    形态抄 dispatch_service._emit_circuit_open(:1024):同 Namespace 前缀风格、
    best-effort try/except。**每轮都发**(包括 0),否则告警在"从有到无"时会因缺数据点
    而停在 ALARM/INSUFFICIENT_DATA,分不清"好了"还是"巡检本身挂了"。

    两个指标分开而不合成一个:
      · LifecycleStuckMarked   —— 确实卡死、已标记、等 P2 出口处理的数量
      · LifecycleStuckUnconfirmed —— 卡着但连 host 状态都探不到(或超本轮限额)的数量。
        它持续非零比第一个更严重:意味着我们对这些租户【毫无判断】。"""
    try:
        cloudwatch.put_metric_data(
            Namespace="OpenClaw/Lifecycle",
            MetricData=[
                {
                    "MetricName": "LifecycleStuckMarked",
                    "Value": float(marked),
                    "Unit": "Count",
                },
                {
                    "MetricName": "LifecycleStuckUnconfirmed",
                    "Value": float(unconfirmed),
                    "Unit": "Count",
                },
                # 心跳(codex 独立复审第四轮)。上面两个数【本身不能证明巡检活着】:
                # 调用方 :224 的 `except Exception` 按约定把巡检异常降级成一行日志,
                # Lambda 因此【不】报错 → 那条「health_check Lambda error」告警永不触发;
                # 而指标发不出去时,告警侧的 NOT_BREACHING 又把「无数据」判成健康。
                # 两条静默叠起来 = reaper 永久坏掉、IAM 被收权、或 put_metric_data 一直失败,
                # 而租户持续卡死却【一个告警都不响】。
                # 这个心跳是「本轮巡检跑完了」的显式肯定信号:只有走到这里才发 1。异常路径
                # 到不了此处,发送本身失败也发不出去 —— 两种坏法都表现为【缺数据】,由
                # alarms.py 那条 treat_missing_data=BREACHING 的告警接住。
                # 不与上面两个数合成一个指标:那两个的正常值就是 0,无法与"没发"区分。
                {
                    "MetricName": "LifecycleReaperHeartbeat",
                    "Value": 1.0,
                    "Unit": "Count",
                },
            ],
        )
    except Exception as e:
        print(f"lifecycle-stuck: metric emit failed (non-fatal): {e}")


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
# 不到它。此处把 failed 纳入孤儿兜底:下轮同样 stop-confirm 后释放。安全性:写出【failed + 令牌】
# 的写者是【已知且封闭的一组】(poller 转 running 删令牌、转 requires_intervention 属另一孤儿态、
# _release_reservation 释放即删令牌);且 failed 绝不会被 promote 成活 VM(promote 条件锁 creating)。
#
# #562 —— 这组写者【现在是两个】,原注释「全仓仅 reaper 的围栏步」已不成立:
# api Lambda 的 `services/deadline_executor.py` 对「过创建死线」的 creating 行做同款围栏
# (条件 status=creating AND create_deadline=:dl,保留令牌),然后就把释放交给这里。它刻意
# 不复制 _confirm_vm_stopped —— 两个释放者才是双扣账本/停掉活 VM 的来源。上面那条安全性论证
# 对它同样成立(它也锁 creating、也不删令牌),所以本函数无需改动;但读者必须知道 failed+令牌
# 的行现在有两个来源,否则会以为节拍只有 5 分钟一次。
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
            target_vm_num = int(tenant.get("migration_target_vm_num", 1))
            # 占号单独还,不并进下面的容量条件写:存量租户 host item 上没有 ps_*,一旦把
            # `#ps = :tid` 加进容量的 floor guard,这些租户的容量就永远释放不掉(CCF);
            # 反过来容量条件失败也不能连带泄漏号(租户回到 source 后仍在役,reaper 不清)。
            _release_phys_slot(target_host_id, target_vm_num, tid)
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
        old_vm_num = int(tenant.get("migration_old_vm_num", tenant.get("vm_num", 1)))
        if source_host_id:
            # 占号单独还(理由同 _rollback_migration):存量租户没有 ps_*,把 `#ps = :tid`
            # 并进容量的 floor guard 会让它们的 source 容量永远释放不掉。
            _release_phys_slot(source_host_id, old_vm_num, tid)
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
# =====================================================================


def _ssm_ping_map():
    """一次列全机队的 SSM 视图,返回 {instance_id: PingStatus}。判不出来返回 None。

    None 与"空 map"必须分开:describe 本身失败(限流/权限/网络)时若当成"谁都不可达",
    一次 SSM 抖动就会把整个机队降级 —— 那比漏降一台严重得多。调用方据此在 None 时跳过本轮。

    一次列全量而不逐台带 filter:机队规模是个位数到几十台,一次分页调用比 N 次调用更省配额,
    也不依赖 DescribeInstanceInformation 的 filter key 契约。
    """
    out = {}
    token = None
    try:
        while True:
            kwargs = {"MaxResults": 50}
            if token:
                kwargs["NextToken"] = token
            resp = ssm.describe_instance_information(**kwargs)
            for info in resp.get("InstanceInformationList") or []:
                iid = info.get("InstanceId")
                if iid:
                    out[iid] = info.get("PingStatus") or ""
            token = resp.get("NextToken")
            if not token:
                return out
    except Exception as e:
        print(f"stale-demote: SSM describe_instance_information 失败({e});本轮跳过")
        return None


def demote_stale_hosts(dry_run=True, now=None, ping_map=None):
    """把心跳失效【且 SSM 也看不见】的单台 active/idle host 降级为 draining。

    为什么降级成 draining 而不是新造一个状态:调度的 status 门是严格 active/idle
    (core/scheduling.py 注释原文 "#309 — status gate is strictly active/idle with NO
    exception"),draining 天然被排除 → 零改调度侧,也不与 #540 要改的那条 host_cond 撞车。
    且全仓没有任何逻辑因为 draining 就去终止一台 host(scaler/handler.py:213 原文
    "draining/deleted don't serve new tenants"),恢复路径现成:重新注册把 status 写回 active
    (services/host_service.py:249 注释原文)。

    为什么要 SSM 双重判据而不是只看心跳:心跳陈旧但 SSM 健康的 host 已经有
    _restart_host_agent() 那条补救路径接手;把它也降级会在 host-agent 热更新或短暂抖动时
    误杀在役机器。2026-08-10 那台的形态恰是两者同时成立(心跳陈旧 1 小时 + SSM 查不到该实例)。

    返回 {"scanned", "demoted", "skipped_ssm_ok", "skipped_ssm_unknown", "would_demote"}。
    """
    result = {
        "scanned": 0,
        "demoted": [],
        "skipped_ssm_ok": [],
        "skipped_ssm_unknown": 0,
        "would_demote": [],
    }
    if not STALE_HOST_DEMOTE_ENABLED:
        return result
    now = now or datetime.now(timezone.utc)
    if ping_map is None:
        ping_map = _ssm_ping_map()
    if ping_map is None:
        # SSM 判不出来 —— 宁可漏降一台,也不在 SSM 抖动时降一片。
        result["skipped_ssm_unknown"] = -1
        return result

    start_key = None
    while True:
        kwargs = {
            "FilterExpression": "#s IN (:active, :idle)",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":active": "active", ":idle": "idle"},
            "ConsistentRead": True,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = hosts_table.scan(**kwargs)
        for host in page.get("Items", []):
            hid = host.get("instance_id")
            if not hid or hid.startswith("__"):
                continue  # __az_failover_state__ 之类的合成行不是真 host
            result["scanned"] += 1
            if not is_host_unhealthy(host, now, STALE_HOST_DEMOTE_MINUTES):
                continue
            ping = ping_map.get(hid)
            if ping == "Online":
                # 心跳停了但 SSM 还通 —— 交给 _restart_host_agent() 那条路,不降级。
                result["skipped_ssm_ok"].append(hid)
                continue
            reason = "ssm_absent" if ping is None else f"ssm_ping_{ping or 'empty'}"
            if dry_run:
                result["would_demote"].append({"host": hid, "reason": reason})
                continue
            try:
                hosts_table.update_item(
                    Key={"instance_id": hid},
                    UpdateExpression=(
                        "SET #s = :draining, stale_demoted_at = :t, "
                        "stale_demote_reason = :r"
                    ),
                    # 条件写:只降 active/idle。并发里别人已把它改成 deleted/draining/
                    # upgrading 时不覆盖 —— 覆盖会把别人的终态写回去。
                    ConditionExpression="#s IN (:active, :idle)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":draining": "draining",
                        ":active": "active",
                        ":idle": "idle",
                        ":t": now.isoformat(),
                        ":r": reason,
                    },
                )
            except Exception as e:  # 含 ConditionalCheckFailed:状态已被别人改,不是错误
                print(f"stale-demote: {hid} 未降级({e})")
                continue
            result["demoted"].append({"host": hid, "reason": reason})
            _emit_audit(
                "HOST_DEMOTED_STALE",
                {
                    "instance_id": hid,
                    "reason": reason,
                    "last_health_check": host.get("last_health_check")
                    or host.get("last_seen"),
                    "threshold_minutes": STALE_HOST_DEMOTE_MINUTES,
                    "vm_count": int(host.get("vm_count") or 0),
                },
            )
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return result


def classify_agent_restart(host):
    """纯逻辑:据 host 行判断 agent 是否重启过,以及是不是我们自己重启的。

    返回 ("none"|"first_seen"|"expected"|"spontaneous", started_at)。拆成纯函数是为了
    能不碰 AWS 直接单测边界(缺字段、首次观测、我们自己重启 vs 它自己挂了又起)。

    为什么要区分 expected 和 spontaneous:_restart_host_agent() 会主动重启 agent 并写
    agent_restart_at。那种重启是我们自己干的、预期内的;而"没人动它却换了代"才是 #52 要抓
    的假活 —— ping 通 + gateway 200 在进程崩了又被拉起、内存态全丢时照样绿。
    """
    started = host.get("agent_started_at") or ""
    if not started:
        return "none", ""  # 老 agent 还没上报这个字段 —— 不是异常,别噪声
    seen = host.get("agent_started_at_seen") or ""
    if not seen:
        return "first_seen", started
    if started == seen:
        return "none", started
    # 换代了。若我们在上一次观测之后主动重启过它,就算预期内。
    restarted_at = host.get("agent_restart_at") or ""
    if restarted_at and restarted_at >= seen:
        return "expected", started
    return "spontaneous", started


def sweep_agent_signals(dry_run=True, now=None):
    """#52 B + C 的控制面侧:读 host 上报的 agent 信号,检测重启与跨 host 漂移。

    B(进程重启/状态丢失):比对 agent_started_at 与上轮记下的 agent_started_at_seen。
      自发换代 → 记 agent_spontaneous_restart_at + 审计。**只标记不自动补救**:重启后
      哪些租户的内存态真丢了需要逐个判,盲目重建的爆炸半径远大于收益。

    C(跨 host 漂移):汇总各 host 上报的 drift_foreign_vm(本机有 VM 而账本说租户在别台)。
      单机视角只看得见自己那一半,汇总才拼出"同一租户被两台 host 同时认领"的全貌。
      同样只报不改 —— host-agent 侧 _report_route_drift 的注释已经把自动修复的爆炸半径
      写清楚了(给陈旧行写 DNAT 可能遮蔽同端口的活租户),remediation 属于另一个审计过的 PR。

    B 与 C 合在一个 sweep 里是因为它们读同一份 host 扫描,分两个函数要扫两遍全表。
    """
    result = {
        "scanned": 0,
        "first_seen": [],
        "expected_restart": [],
        "spontaneous_restart": [],
        "foreign_vm_total": 0,
        "foreign_vm_hosts": [],
    }
    now = now or datetime.now(timezone.utc)
    start_key = None
    while True:
        kwargs = {"ConsistentRead": True}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = hosts_table.scan(**kwargs)
        for host in page.get("Items", []):
            hid = host.get("instance_id")
            if not hid or hid.startswith("__"):
                continue
            if (host.get("status") or "") == "deleted":
                continue  # 已拆除的行不再产生有意义的 agent 信号
            result["scanned"] += 1

            # ── C 汇总 ──
            foreign = int(host.get("drift_foreign_vm") or 0)
            if foreign:
                result["foreign_vm_total"] += foreign
                result["foreign_vm_hosts"].append({"host": hid, "foreign_vm": foreign})

            # ── B 检测 ──
            verdict, started = classify_agent_restart(host)
            if verdict == "none":
                continue
            result[
                {
                    "first_seen": "first_seen",
                    "expected": "expected_restart",
                    "spontaneous": "spontaneous_restart",
                }[verdict]
            ].append(hid)
            if dry_run:
                continue
            expr = "SET agent_started_at_seen = :st"
            vals = {":st": started}
            if verdict == "spontaneous":
                expr += ", agent_spontaneous_restart_at = :t"
                vals[":t"] = now.isoformat()
            try:
                hosts_table.update_item(
                    Key={"instance_id": hid},
                    UpdateExpression=expr,
                    ExpressionAttributeValues=vals,
                )
            except Exception as e:
                print(f"agent-signals: {hid} 记录重启失败({e})")
                continue
            if verdict == "spontaneous":
                _emit_audit(
                    "HOST_AGENT_RESTARTED_SPONTANEOUS",
                    {
                        "instance_id": hid,
                        "agent_started_at": started,
                        "previous_seen": host.get("agent_started_at_seen"),
                        "vm_count": int(host.get("vm_count") or 0),
                        "note": (
                            "in-memory agent state may have been lost; "
                            "flagged only, no auto-remediation"
                        ),
                    },
                )
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            break

    if not dry_run and result["foreign_vm_total"]:
        _emit_audit(
            "CROSS_HOST_VM_DRIFT",
            {
                "foreign_vm_total": result["foreign_vm_total"],
                "hosts": result["foreign_vm_hosts"][:10],
                "note": (
                    "a host holds a VM the ledger attributes elsewhere — possible "
                    "same-tenant dual-run; reported only, no auto-remediation"
                ),
            },
        )
    return result


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
    # 所以 1MB 上限完全按全表字节数算(实测 39 行里 33 行是 deleted,全都被扫)。
    # 漏页的后果在这里最重:AZ 健康是拿 host 集合算的 ——
    #   · 少看见一批健康 host → 误判该 AZ 故障 → 触发【全量迁移】;
    #   · 少看见一批不健康 host → 漏掉真故障 → 租户留在死 AZ。
    # 两个方向都不会报错,只会给出一个自信的错判断。
    hosts = ddb_scan.scan_all(hosts_table)

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


PHYS_SLOT_PREFIX = "ps_"


def _phys_slot_attr(num):
    return f"{PHYS_SLOT_PREFIX}{int(num)}"


def _host_claimed_slots(host_item, exclude_ids=None):
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


def _release_phys_slot(host_id, num, owner):
    """owner 条件释放 ps_<n>;owner 不匹配 = 已被别人接手,不动。回滚路径调用,不抛。"""
    try:
        hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="REMOVE #ps",
            ConditionExpression="#ps = :tid",
            ExpressionAttributeNames={"#ps": _phys_slot_attr(num)},
            ExpressionAttributeValues={":tid": owner},
        )
        return True
    except Exception as e:  # noqa: BLE001
        if not _is_conditional_failure(e):
            print(
                f"release phys slot host={host_id} num={num} owner={owner} "
                f"failed (non-fatal): {e}"
            )
        return False


def reap_orphan_phys_slots(dry_run=True):
    """与 core/scheduling.py 同款物理号对账。

    health_check 独立打包，不能 import api/core，故两处各持一份；判据必须同步演进。
    租户表中除 deleted/suspended 外的 owner 均保守视为在役，只清明确无主的 ps_*。
    """
    alive = set()
    start_key = None
    while True:
        kwargs = {
            "ProjectionExpression": "id, #s",
            "ExpressionAttributeNames": {"#s": "status"},
            "ConsistentRead": True,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = tenants_table.scan(**kwargs)
        for tenant in page.get("Items", []):
            if tenant.get("status") not in ("deleted", "suspended"):
                alive.add(tenant["id"])
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            break

    cleaned = []
    start_key = None
    while True:
        kwargs = {
            "FilterExpression": "#s = :active",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":active": "active"},
            "ConsistentRead": True,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = hosts_table.scan(**kwargs)
        for host in page.get("Items", []):
            host_id = host["instance_id"]
            for num, owner in _host_claimed_slots(host).items():
                if owner in alive:
                    continue
                cleaned.append((host_id, num, owner))
                if dry_run:
                    continue
                try:
                    hosts_table.update_item(
                        Key={"instance_id": host_id},
                        UpdateExpression="REMOVE #ps",
                        ConditionExpression="#ps = :tid",
                        ExpressionAttributeNames={"#ps": _phys_slot_attr(num)},
                        ExpressionAttributeValues={":tid": owner},
                    )
                except Exception as e:  # noqa: BLE001
                    if not _is_conditional_failure(e):
                        print(
                            f"reap-phys-slot host={host_id} num={num} owner={owner} "
                            f"failed (non-fatal): {e}"
                        )
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            break
    return cleaned


def reap_stuck_failover_recovering(dry_run=True):
    """接手超过 age-gate 仍停在 failover_recovering 的容量与物理号。"""
    now = datetime.now(timezone.utc)
    result = {"scanned": 0, "reaped": [], "skipped": [], "would_reap": []}
    start_key = None

    while True:
        kwargs = {
            "FilterExpression": "#s = :recovering",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":recovering": "failover_recovering",
            },
            "ConsistentRead": True,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = tenants_table.scan(**kwargs)
        for tenant in page.get("Items", []):
            result["scanned"] += 1
            tenant_id = tenant.get("id", "")
            failover_at = tenant.get("failover_at")
            if not failover_at:
                print(
                    f"reap-failover-recovering skip tenant={tenant_id}: "
                    "missing failover_at"
                )
                result["skipped"].append(
                    {"tenant_id": tenant_id, "reason": "missing_failover_at"}
                )
                continue
            try:
                age_minutes = (
                    now - datetime.fromisoformat(str(failover_at))
                ).total_seconds() / 60
            except (TypeError, ValueError) as e:
                print(
                    f"reap-failover-recovering skip tenant={tenant_id}: "
                    f"invalid failover_at={failover_at!r} ({e})"
                )
                result["skipped"].append(
                    {"tenant_id": tenant_id, "reason": "invalid_failover_at"}
                )
                continue

            if age_minutes <= FAILOVER_STUCK_MINUTES:
                result["skipped"].append(
                    {"tenant_id": tenant_id, "reason": "below_age_gate"}
                )
                continue

            reservation_id = tenant.get("failover_reservation_id")
            if not reservation_id:
                print(
                    f"reap-failover-recovering skip tenant={tenant_id}: "
                    "missing failover_reservation_id"
                )
                result["skipped"].append(
                    {
                        "tenant_id": tenant_id,
                        "reason": "missing_failover_reservation_id",
                    }
                )
                continue
            try:
                target_host_id, raw_vm_num, nonce = str(reservation_id).split(":")
            except ValueError:
                print(
                    f"reap-failover-recovering skip tenant={tenant_id}: "
                    f"invalid failover_reservation_id={reservation_id!r}"
                )
                result["skipped"].append(
                    {
                        "tenant_id": tenant_id,
                        "reason": "invalid_failover_reservation_id",
                    }
                )
                continue
            if not target_host_id or not raw_vm_num or not nonce:
                print(
                    f"reap-failover-recovering skip tenant={tenant_id}: "
                    f"invalid failover_reservation_id={reservation_id!r}"
                )
                result["skipped"].append(
                    {
                        "tenant_id": tenant_id,
                        "reason": "invalid_failover_reservation_id",
                    }
                )
                continue
            try:
                vm_num = int(raw_vm_num)
            except ValueError:
                print(
                    f"reap-failover-recovering skip tenant={tenant_id}: "
                    f"invalid vm_num={raw_vm_num!r}"
                )
                result["skipped"].append(
                    {"tenant_id": tenant_id, "reason": "invalid_failover_vm_num"}
                )
                continue

            entry = {
                "tenant_id": tenant_id,
                "target_host_id": target_host_id,
                "vm_num": vm_num,
                "age_minutes": round(age_minutes, 2),
            }
            if dry_run:
                print(
                    f"reap-failover-recovering dry-run tenant={tenant_id} "
                    f"target={target_host_id} vm_num={vm_num} "
                    f"age_minutes={entry['age_minutes']}"
                )
                result["would_reap"].append(entry)
                continue

            # 未确认 target VM 已停就归还号，下一租户可能拿到仍在运行的 VM 的 tap，
            # 直接突破 no-cross-tenant 红线；因此必须先同步停机，确认后才释放。
            stopped, _ = _ssm_run_capture(
                target_host_id,
                f"/home/ubuntu/stop-vm.sh {shlex.quote(tenant_id)} "
                f"{shlex.quote(str(vm_num))}",
                timeout=60,
            )
            if not stopped:
                print(
                    f"reap-failover-recovering retained tenant={tenant_id}: "
                    "target stop unconfirmed"
                )
                result["skipped"].append(
                    {"tenant_id": tenant_id, "reason": "target_stop_unconfirmed"}
                )
                continue

            vcpu = int(tenant.get("vcpu") or 2)
            mem_mb = int(tenant.get("mem_mb") or 4096)
            released = _release_failover_reservation(
                tenant_id,
                target_host_id,
                vm_num,
                reservation_id,
                vcpu,
                mem_mb,
            )
            if not released:
                print(
                    f"reap-failover-recovering no-op tenant={tenant_id}: "
                    "reservation already consumed or owner changed"
                )
                result["skipped"].append(
                    {"tenant_id": tenant_id, "reason": "reservation_release_noop"}
                )
                continue

            try:
                tenants_table.update_item(
                    Key={"id": tenant_id},
                    UpdateExpression="SET #s = :failed, failover_error = :e",
                    ConditionExpression="#s = :recovering",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":failed": "failover_failed",
                        ":recovering": "failover_recovering",
                        ":e": "stuck_in_failover_recovering_reaped",
                    },
                )
            except ClientError as e:
                code = (e.response.get("Error") or {}).get("Code")
                if code == "ConditionalCheckFailedException":
                    print(
                        f"reap-failover-recovering tenant={tenant_id}: "
                        "status advanced concurrently; not overwriting"
                    )
                else:
                    print(
                        f"reap-failover-recovering tenant={tenant_id}: "
                        f"status update failed (non-fatal): {e}"
                    )

            result["reaped"].append(entry)
            _emit_audit(
                "AZ_FAILOVER_STUCK_REAPED",
                {
                    "tenant_id": tenant_id,
                    "target_host_id": target_host_id,
                    "vm_num": vm_num,
                    "age_minutes": entry["age_minutes"],
                },
            )

        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            break

    return result


def _iter_phys_nums(host_id, exclude_ids=None):
    """遍历 host_id 上(已驻留 + 迁入中)租户的物理 tap 号。异常向上抛,调用方定 fail 策略。

    #491 —— 与 core/scheduling.py 的同名骨架是【同一判据的第二份实现】。不能 import 共享:
    health_check 是独立打包的 Lambda(deploy/stacks/lambdas.py 用
    Code.from_asset("deploy/lambda/health_check")),包里没有 api 侧的 core/ —— 本文件的
    CAS 也正是同样原因各持一份。两份实现必须同步演进;判据由 #208 冻结(双来源 + 分页)。

    scan(FilterExpression) 分页:每页最多扫 1MB 就返回 + LastEvaluatedKey。命中的迁入租户
    可能落在后页,不翻页会漏判 → fail-open 重开安全洞,故必须翻完。
    """
    skip = exclude_ids or frozenset()
    for expr, extra in (
        ("host_id = :h AND #s <> :d", {":d": "deleted"}),
        ("migration_target = :h AND #s = :mig", {":mig": "migrating"}),
    ):
        vals = {":h": host_id}
        vals.update(extra)
        start_key = None
        while True:
            kw = {
                "FilterExpression": expr,
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": vals,
                "ProjectionExpression": "id, vm_num, phys_vm_num",
                "ConsistentRead": True,
            }
            if start_key:
                kw["ExclusiveStartKey"] = start_key
            resp = tenants_table.scan(**kw)
            for it in resp.get("Items", []):
                if it.get("id") in skip:
                    continue
                phys = it.get("phys_vm_num", it.get("vm_num"))
                try:
                    yield int(phys)
                except (TypeError, ValueError):
                    continue
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break


def _next_free_phys_num(host_id, start, exclude_ids=None, limit=4096):
    """#491(review2)—— 从 start 起找第一个未被物理占用的号,供 failover 跳号认领。

    为什么不试号:试号要一个"试几次放弃"的上限,而 target 上连续被占的号可能有几百个
    (发号器被回退时),有限次试号会把「换个号就能迁」误报成「无容量」并终止 AZ 恢复;
    而且每轮试号都要归还刚认领的记账,本路径没有 capacity_reservation_id 令牌,
    归还做不到既幂等又可确认。先算空号、再一次跳号 CAS,两个问题都不存在。

    返回 (num, occupied_set):(None, None) = 读失败 → 调用方 fail-closed;
    (None, occupied) = 上界内无空号。limit 是防御性上界,不是重试次数。
    """
    try:
        occupied = set(_iter_phys_nums(host_id, exclude_ids=exclude_ids))
        host_item = (
            hosts_table.get_item(
                Key={"instance_id": host_id}, ConsistentRead=True
            ).get("Item")
            or {}
        )
        occupied.update(_host_claimed_slots(host_item, exclude_ids))
    except Exception as e:  # noqa: BLE001 — 返回 None 让调用方 fail-closed
        print(f"_next_free_phys_num({host_id}) scan failed → fail-closed: {e}")
        return None, None
    try:
        n = int(start)
    except (TypeError, ValueError):
        return None, occupied
    end_n = n + int(limit)
    while n < end_n:
        if n not in occupied:
            return n, occupied
        n += 1
    return None, occupied


def _release_failover_reservation(
    tenant_id, target_host_id, vm_num, reservation_id, vcpu, mem_mb
):
    """用租户令牌作幂等锚，原子归还 failover 容量与物理号。"""
    txn_items = [
        {
            "Update": {
                "TableName": hosts_table.table_name,
                "Key": {"instance_id": target_host_id},
                "UpdateExpression": (
                    "SET used_vcpu = used_vcpu - :v, "
                    "used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one "
                    "REMOVE #ps"
                ),
                "ConditionExpression": (
                    "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one "
                    "AND #ps = :tid"
                ),
                "ExpressionAttributeNames": {"#ps": _phys_slot_attr(vm_num)},
                "ExpressionAttributeValues": {
                    ":v": int(vcpu),
                    ":m": int(mem_mb),
                    ":one": 1,
                    ":tid": tenant_id,
                },
            }
        },
        {
            "Update": {
                "TableName": tenants_table.table_name,
                "Key": {"id": tenant_id},
                "UpdateExpression": "REMOVE failover_reservation_id",
                "ConditionExpression": "failover_reservation_id = :rid",
                "ExpressionAttributeValues": {":rid": reservation_id},
            }
        },
    ]
    try:
        hosts_table.meta.client.transact_write_items(TransactItems=txn_items)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            return False  # 令牌已消费/owner 已变化/重复释放：整个事务幂等 no-op
        print(f"failover release {tenant_id} failed (non-fatal): {e}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"failover release {tenant_id} failed (non-fatal): {e}")
        return False


def _reserve_target_vm_num(
    target_host_id,
    vcpu,
    mem_mb,
    attempts=8,
    tenant_id=None,
    reservation_id=None,
):
    """#排雷 D2 — AZ failover 在 target host 上**原子**占一个 vm_num + 记账,替代旧的
    裸读 next_vm_num + 无条件 SET next_vm_num=target+1。与 create 路径 _reserve_slot、
    migrate 路径 _reserve_migration_slot 同款 CAS(#50/#172):一次条件写,只有 next_vm_num
    自读取未变、且容量不超卖才自增 next_vm_num/used_*/vm_count。CCF(并发 create/failover
    抢到同一 next_vm_num)则重读重试。返回认领的(自增前)vm_num,或 None(无容量/CAS 耗尽)。

    修复前:两步(裸读 + 绝对赋值)间无 CAS,并发 create 的递增会被 failover 的
    `SET next_vm_num=target+1` 覆盖 → 两租户拿同一 vm_num → guest_ip/tap 重叠 → 跨租户
    网络串(数据安全轴①)。此 CAS 把分配收敛成单次原子条件写,消除该窗口。
    """
    # #491(review2)—— 不试号:先算出 target host 上第一个未被物理占用的号,再用跳号 CAS
    # 一次认领。返回值仍从 CAS 的 Attributes 取(「自增后 -1」),与本函数原有契约完全一致
    # —— CAS 成功 ⇒ next_vm_num 已被设成 target+1,所以两者恒等。跳号是内部改进,
    # 不改变「号从哪来」的对外语义。
    for _ in range(attempts):
        # 强一致读:本实现按读到的 next_vm_num 算空号,陈旧读只会白撞一次 CCF 再重来。
        h = (
            hosts_table.get_item(
                Key={"instance_id": target_host_id}, ConsistentRead=True
            ).get("Item")
            or {}
        )
        expected = int(h.get("next_vm_num", 1))
        target, occ = _next_free_phys_num(
            target_host_id, expected, exclude_ids={tenant_id} if tenant_id else None
        )
        if occ is None:
            # 占用未知 → fail-closed 拒这次 failover(调用方据此 raise 并标 failed)。
            print(
                f"[failover] PHYS OCCUPANCY UNKNOWN host={target_host_id} "
                f"tenant={tenant_id} — refusing failover (fail-closed)"
            )
            return None
        if target is None:
            print(
                f"[failover] NO FREE PHYS SLOT host={target_host_id} from={expected}"
            )
            return None
        if target != expected:
            print(
                f"[failover] SKIP OCCUPIED tenant={tenant_id} host={target_host_id} "
                f"expected={expected} target={target}"
            )
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
                # 跳号:next_vm_num 直接推到 target+1(绝对值)。条件仍是
                # next_vm_num = :expected,并发者改过就 CCF 重来,被跳过的号不会被同时认领。
                UpdateExpression=(
                    "SET used_vcpu = if_not_exists(used_vcpu, :z) + :v, "
                    "used_mem_mb = if_not_exists(used_mem_mb, :z) + :m, "
                    "vm_count = if_not_exists(vm_count, :z) + :one, "
                    "next_vm_num = :next_after, #ps = :tid"
                ),
                ConditionExpression=(
                    "next_vm_num = :expected AND used_vcpu <= :cap_v "
                    "AND used_mem_mb <= :cap_m AND attribute_not_exists(#ps)"
                ),
                ExpressionAttributeNames={"#ps": _phys_slot_attr(target)},
                ExpressionAttributeValues={
                    ":v": vcpu,
                    ":m": mem_mb,
                    ":one": 1,
                    ":z": 0,
                    ":expected": expected,
                    ":next_after": target + 1,
                    ":cap_v": cap_v,
                    ":cap_m": cap_m,
                    ":tid": tenant_id or "unknown",
                },
                ReturnValues="UPDATED_NEW",
            )
            # 取号:优先用 CAS 的返回值(与本函数原有契约一致),取不到则回退 target。
            # 两者**恒等** —— CAS 成功意味着写入生效,next_vm_num 已被设成 target+1,
            # 所以「自增后 -1」== target。优先用返回值是为了不改变对外语义;回退 target 是
            # 为了不依赖 DDB 一定回传 Attributes(部分调用方/测试替身不提供它)。
            try:
                claimed = int(r["Attributes"]["next_vm_num"]) - 1
            except (KeyError, TypeError, ValueError):
                claimed = target
            if reservation_id and tenant_id:
                full_reservation_id = (
                    f"{target_host_id}:{claimed}:{reservation_id}"
                )
                try:
                    tenants_table.update_item(
                        Key={"id": tenant_id},
                        UpdateExpression="SET failover_reservation_id = :rid",
                        ConditionExpression=(
                            "#s = :recover AND "
                            "(attribute_not_exists(failover_reservation_id) "
                            "OR failover_reservation_id = :rid)"
                        ),
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={
                            ":rid": full_reservation_id,
                            ":recover": "failover_recovering",
                        },
                    )
                except Exception:
                    # 令牌未落库时不能走令牌事务；用刚写入的 owner 条件补偿本次占号。
                    try:
                        hosts_table.update_item(
                            Key={"instance_id": target_host_id},
                            UpdateExpression=(
                                "SET used_vcpu = used_vcpu - :v, "
                                "used_mem_mb = used_mem_mb - :m, "
                                "vm_count = vm_count - :one REMOVE #ps"
                            ),
                            ConditionExpression=(
                                "used_vcpu >= :v AND used_mem_mb >= :m "
                                "AND vm_count >= :one AND #ps = :tid"
                            ),
                            ExpressionAttributeNames={"#ps": _phys_slot_attr(claimed)},
                            ExpressionAttributeValues={
                                ":v": vcpu,
                                ":m": mem_mb,
                                ":one": 1,
                                ":tid": tenant_id,
                            },
                        )
                    except Exception as rollback_error:  # noqa: BLE001
                        print(
                            f"failover token persist compensation failed tenant={tenant_id}: "
                            f"{rollback_error}"
                        )
                    raise
            return claimed
        except Exception as e:  # noqa: BLE001 — CCF 重试,其它异常传播(fail-loud)
            if "ConditionalCheckFailed" not in type(
                e
            ).__name__ and "ConditionalCheckFailed" not in str(e):
                raise
            continue  # 竞争/超卖 → 重读 next_vm_num 重算
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
    failover_reservation_nonce = None
    failover_reservation_id = None
    config_template = tenant.get("config_template") or ""
    source_host_id = tenant.get("host_id", "")

    # 1) Find latest backup (path A: refuse if missing).
    # 门看 BACKUP_BUCKET(它已回退到 ASSETS_BUCKET):只看 ASSETS_BUCKET 的话,只注入了
    # BACKUP_BUCKET 的部署会连查都不查就判无备份。
    backup_key = _find_latest_backup_key(tenant_id) if BACKUP_BUCKET else None
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
        failover_reservation_nonce = str(time.time_ns())
        target_vm_num = _reserve_target_vm_num(
            target_host_id,
            vcpu,
            mem_mb,
            tenant_id=tenant_id,
            reservation_id=failover_reservation_nonce,
        )
        if target_vm_num is None:
            raise RuntimeError(
                f"target {target_host_id} 无法原子占 vm_num(无容量/CAS 竞争耗尽);"
                f"拒绝 failover 避免跨租户 vm_num 串号"
            )
        failover_reservation_id = (
            f"{target_host_id}:{target_vm_num}:{failover_reservation_nonce}"
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
            # #491(review3)—— 必须同时写 phys_vm_num。它是【物理 tap 号的权威】,撞号守卫
            # 按 `phys_vm_num`(缺失才回退 vm_num)判断某个号是否已被在役租户占用。
            # failover 实际 launch 的是 target_vm_num,真实网卡就是 tap-vm{target_vm_num};
            # 若只翻 vm_num 而留下迁移前的旧 phys_vm_num,守卫会以为这个号没人用 →
            # 放行另一个租户认领它 → 跨租户 tap 接管。host-agent 的 if_not_exists 回填
            # 也修不了它(字段已存在,只是值是旧的)。
            UpdateExpression=(
                "SET host_id = :h, vm_num = :n, phys_vm_num = :n, "
                "#s = :running, restored_from = :b "
                "REMOVE failover_reservation_id"
            ),
            ConditionExpression="failover_reservation_id = :rid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":h": target_host_id,
                ":n": target_vm_num,
                ":running": "running",
                ":b": backup_key,
                ":rid": failover_reservation_id,
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
        if target_vm_num is not None and failover_reservation_id:
            # verify/ALB 等后置失败时目标 VM 可能已活；先同步停机，再原子还容量+占号。
            stopped, _ = _ssm_run_capture(
                target_host_id,
                f"/home/ubuntu/stop-vm.sh {shlex.quote(tenant_id)} "
                f"{shlex.quote(str(target_vm_num))}",
                timeout=60,
            )
            if stopped:
                _release_failover_reservation(
                    tenant_id,
                    target_host_id,
                    target_vm_num,
                    failover_reservation_id,
                    vcpu,
                    mem_mb,
                )
            else:
                print(
                    f"failover release retained tenant={tenant_id}: target stop unconfirmed"
                )
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
        s3://${BACKUP_BUCKET:-${ASSETS_BUCKET}}/${BACKUP_PREFIX}/<tenant_id>/<ISO>.gz
    加密模式(默认)产出 `<ISO>.gz.enc`,并**额外**上传一个 `<ISO>.gz.key`(信封加密的
    数据密钥,不是数据本体)。没有 'latest' 别名 —— list 后按 LastModified 排序取最新。

    `api/services/tenant_service.py` 的 `_resolve_backup`):
      • **桶**:原来读 ASSETS_BUCKET,而备份写在 BACKUP_BUCKET → 永远 list 空 →
        每个租户都被 no-backup 拒绝,AZ failover 实质不可用(真机实测
        `tenants_blocked: 1` / `failover_error=no_backup_available`)。
      • **`.key` 对象**:`.gz.enc` 和 `.gz.key` 只差一两秒上传,按 LastModified 倒序
        可能把**数据密钥**当备份返回,交给 launch-vm.sh restore 就是拿错文件。必须排除。
    """
    if not BACKUP_BUCKET or not tenant_id:
        return None
    try:
        prefix = f"{BACKUP_PREFIX}/{tenant_id}/"
        # ㉛ 必须【翻完整个前缀】(codex 独立复审第二十五轮)。与
        # tenant_service._resolve_backup 是同一个洞的另一半 —— 而这一半更要紧:这里是
        # **AZ failover** 路径,整个可用区挂掉时靠它给每个租户挑恢复点。
        # 只取第一页(上限 1000 key)有两个后果,第二个更隐蔽:
        #   · S3 按【字典序】返回而对象名是 ISO 时间戳 → 最新的排在最后。攒够 1000 个 key
        #     (约 500 次备份;备份桶 Object Lock COMPLIANCE 让它们删不掉)之后,第一页全是
        #     最旧的,"选最新"恒选不到真正的最新;
        #   · 配对判据会【误杀】:`.enc` 在第一页、它的 `.key` 落到第二页时会被判成孤儿 ——
        #     一个完全可用的恢复点被当成不可解,恰是那道过滤要防的事的反面。
        #
        # ⚠ 硬上限而不是只靠 IsTruncated 退出:分页循环正是本分支开局修掉的那个
        # `_ssm_ping_map` 死循环形态(`resp.get(...)` 在 MagicMock 上恒真 → 永不退出)。
        _MAX_PAGES = 50  # 50 × 1000 = 5 万 key,远超任何真实租户的备份数
        objs = []
        _tok = None
        for _page in range(_MAX_PAGES):
            _kw = {"Bucket": BACKUP_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
            if _tok:
                _kw["ContinuationToken"] = _tok
            resp = s3.list_objects_v2(**_kw)
            objs.extend(resp.get("Contents") or [])
            if not resp.get("IsTruncated"):
                break
            _tok = resp.get("NextContinuationToken")
            if not _tok:
                break
        else:
            print(
                f"_find_latest_backup_key({tenant_id}): stopped after {_MAX_PAGES} list "
                f"pages ({len(objs)} keys); selection may miss newer backups"
            )
        # 先滤掉 .key(数据密钥不是数据本体),再排序 —— 顺序不能反,否则空列表判断会
        # 把"只有 .key"误当成"有备份"。
        _all_keys = {str(o.get("Key", "")) for o in objs}
        objs = [o for o in objs if not str(o.get("Key", "")).endswith(".key")]
        # ⑬ codex 独立复审第八轮 —— 加密备份必须有【配对的 .key】才算可恢复。
        #
        # 与 tenant_service._resolve_backup 同一个洞的另一半,而这一半更要紧:这里是
        # **AZ failover** 路径,整个可用区挂掉时靠它给每个租户挑恢复点。
        # `.enc` 与 `.key` 是两次独立上传;backup-data.sh 本轮已改成"先传 .key、最后传
        # .enc",但那只保证【今后】不再产生孤儿。改之前那个顺序留下的孤儿(.key 上传
        # 失败而 .enc 已落地)还在桶里,且 Object Lock 让它删不掉。
        # 选到一个解不开的 .enc 会把更早那个【可用】恢复点盖住 → failover 起不来,
        # 而数据其实还在 —— 在 AZ 全挂的场景下这是最不能出的错。
        def _decryptable(o):
            k = str(o.get("Key", ""))
            if not k.endswith(".enc"):
                return True
            if k[: -len(".enc")] + ".key" in _all_keys:
                return True
            print(
                f"_find_latest_backup_key({tenant_id}): skipping {k} — no matching "
                ".key; an undecryptable .enc must not shadow an older usable backup"
            )
            return False

        objs = [o for o in objs if _decryptable(o)]
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
