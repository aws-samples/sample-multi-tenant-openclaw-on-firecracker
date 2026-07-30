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
import boto3
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
                # #218: bad timestamp shape → fall through to stale (safe
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

    # ------- AZ-level failover (1.3.0) -------
    if AZ_FAILOVER_ENABLED:
        try:
            failover_summary = _check_and_handle_az_failover(now, tenants)
            if failover_summary["az_outages_detected"]:
                print(f"az_failover: {json.dumps(failover_summary)}")
        except Exception as e:
            # AZ failover failures must NEVER take down the watchdog.
            print(f"az_failover error (non-fatal): {e}")

    # ------- In-flight live-migration sweep (1.4.4, issue #64) -------
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


def _reap_stuck_creating(now):
    """Mark tenants stuck in `creating` past CREATING_TIMEOUT_SECONDS as failed
    and release their host capacity reservation. Returns the count reaped.

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
        # Flip status only if still creating (don't clobber a just-succeeded launch).
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
                    ":r": f"reaped: stuck in creating > {CREATING_TIMEOUT_SECONDS}s",
                },
            )
        except Exception as e:
            # #218: expected race (status already flipped) or throttle —
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


def _rollback_migration(tenant, reason):
    """Roll a failed/stuck migration back to `running` and clear the async
    context. The source VM was only briefly paused for the snapshot and then
    resumed by migrate-vm.sh, so source host_id / routing are untouched — 'running'
    is the truthful state there.

    #172: the migrate API RESERVES the TARGET host's slot up-front via
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
    tenants_table.update_item(
        Key={"id": tid},
        UpdateExpression=(
            "SET #s = :r, migration_failed = :reason, updated_at = :t "
            "REMOVE migration_target, migration_target_vm_num, migration_source, "
            "migration_snap_cmd, migration_restore_cmd, migration_phase, "
            "migration_started_at, migration_snapshot_uri"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":r": "running",
            ":reason": reason[:500],
            ":t": datetime.now(timezone.utc).isoformat(),
        },
    )
    _emit_audit("MIGRATION_FAILED", {"tenant_id": tid, "reason": reason[:200]})
    print(f"migration rollback {tid}: {reason}")


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
    source_host_id = tenant.get("migration_source", "")
    target_host_id = tenant.get("migration_target", "")
    target_vm_num = int(tenant.get("migration_target_vm_num", 1))
    snap_uri = tenant.get("migration_snapshot_uri", "")

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
            # #218: bad migration_started_at → skip watchdog (safer than
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
            f"/home/ubuntu/migrate-vm.sh restore {tid} {target_vm_num} {snap_uri}",
            timeout=600,
        )
        if not restore_cmd:
            _rollback_migration(tenant, "failed to submit restore SSM command")
            return
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET migration_phase = :p, migration_restore_cmd = :rc, updated_at = :t"
            ),
            ExpressionAttributeValues={
                ":p": "restore",
                ":rc": restore_cmd,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
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
            f"python3 /opt/openclaw/route_ops.py ready-route {tid}",
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
            f"python3 /opt/openclaw/route_ops.py commit-route "
            f"{tid} {target_ip} {new_host_port} {new_guest_ip}",
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
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET host_id = :h, vm_num = :n, updated_at = :t, "
                "host_private_ip = :hpi, host_port = :hp, guest_ip = :gip, "
                "migration_phase = :draining, migration_committed_at = :t, "
                "migration_old_host_port = :ohp, migration_old_guest_ip = :ogip, "
                "migration_old_vm_num = :ovn"
            ),
            ExpressionAttributeValues={
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
            },
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
            tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression=(
                    "SET #s = :running, updated_at = :t "
                    "REMOVE migration_target, migration_target_vm_num, migration_source, "
                    "migration_snap_cmd, migration_restore_cmd, migration_phase, "
                    "migration_started_at, migration_snapshot_uri, migration_failed, "
                    "migration_committed_at, migration_old_host_port, "
                    "migration_old_guest_ip, migration_old_vm_num"
                ),
                ConditionExpression="#s = :migrating AND migration_phase = :draining",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":running": "running",
                    ":migrating": "migrating",
                    ":draining": "draining",
                    ":t": datetime.now(timezone.utc).isoformat(),
                },
            )
        except tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
            print(f"migration {tid}: draining already finalized by concurrent sweep")
            return

        # 赢家专属:减 SOURCE 计数 + stop 源 VM + release-route 源(硬伤③/R5)。
        # #172 — 只减 SOURCE;TARGET 的 slot 在 migrate 发起时已 CAS 占用,绝不再动。
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
                _ssm_send_hc(
                    source_host_id,
                    f"/home/ubuntu/stop-vm.sh {tid} {old_vm_num} ; "
                    f"sudo rm -f /etc/nginx/conf.d/tenants/{tid}.conf "
                    f"&& sudo nginx -s reload",
                    timeout=60,
                )
            except Exception as e:
                # #218: source side cleanup is best-effort (source may be
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
                _ssm_send_hc(
                    source_host_id,
                    f"python3 /opt/openclaw/route_ops.py release-route "
                    f"{tid} {int(old_host_port)} {old_guest_ip}",
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
            # #218: bad agent_restart_at → let the restart proceed (no
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
            # #218: DDB flip to failover_blocked failed — the AZ_FAILOVER_NO_BACKUP
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
                # #218: SNS best-effort — audit event captures the same signal,
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
        # #41 — failover 是 wake 场景(从 backup 恢复到 target host),必须穿透
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
        #    与 #172 defect B 同源)。只减/加 host 计数的活全归 CAS 占槽 + source 侧回收。
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
            # #218: same class as the no-backup path — audit still fires
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
