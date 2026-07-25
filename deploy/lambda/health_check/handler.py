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

import json
import os
import random
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from common import audit as _audit
from common import capacity as _capacity
from common import ddb as _ddb_helpers
from common import ssm as _ssm_helpers

ddb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")
sns = boto3.client("sns")
s3 = boto3.client("s3")
elbv2 = boto3.client("elbv2")
sqs = boto3.client("sqs")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

# Optional tables / topics — feature-flag friendly.
_AUDIT_TABLE_NAME = os.environ.get("AUDIT_TABLE", "")
audit_table = ddb.Table(_AUDIT_TABLE_NAME) if _AUDIT_TABLE_NAME else None
# T3-3: honor AUDIT_TTL_DAYS like the api/scaler writers. The old _emit_audit
# hard-coded 90 days, so health_check rows outlived everything else's when an
# operator shortened retention.
AUDIT_TTL_DAYS = int(os.environ.get("AUDIT_TTL_DAYS", "90"))
_SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

STALE_SECONDS = 120  # No health update for 2 min → agent may be down
RESTART_COOLDOWN_SECONDS = 600  # Don't restart agent more than once per 10 min

# T3-3: capacity math is now shared with the API scheduler (common.capacity).
# Defaults 1.0/1.0/0 reproduce legacy behavior for stacks that haven't been
# redeployed with these env vars injected into the health_check Lambda.
CPU_OVERCOMMIT_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", 1.0))
MEM_OVERCOMMIT_RATIO = float(os.environ.get("MEM_OVERCOMMIT_RATIO", 1.0))
MAX_VMS_PER_HOST = int(os.environ.get("MAX_VMS_PER_HOST", "0") or "0")

# AZ failover configuration (read from env, populated by stack.py).
AZ_FAILOVER_ENABLED = os.environ.get("AZ_FAILOVER_ENABLED", "false").lower() == "true"
AZ_UNHEALTHY_THRESHOLD_MINUTES = int(os.environ.get("AZ_UNHEALTHY_THRESHOLD_MINUTES", "10"))
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
# T3-3: the VPC to create a host target group in during failover. The API
# Lambda always used its VPC_ID env; health_check used to clone VpcId from an
# arbitrary existing target group (existing[0]), which breaks when none exist
# yet (cold start / after cleanup) or when a cross-VPC TG is present. Prefer the
# env; the clone stays only as a fallback for un-redeployed stacks.
VPC_ID = os.environ.get("VPC_ID", "")

# T3-2: AZ-failover detection/execution split. When FAILOVER_QUEUE_URL is set,
# the detector (this Lambda) only detects the outage, claims each affected
# tenant, reserves a target host+vm_num, and enqueues one SQS message per
# tenant; a separate worker Lambda (worker.lambda_handler, same asset dir)
# executes the launch/verify/repoint pipeline per tenant in PARALLEL. This
# makes full-AZ recovery O(one tenant) instead of O(5min × tenant count) — the
# old synchronous loop died mid-loop past ~1 tenant because a single failover
# can take ~240s > the Lambda's 180s timeout. When the flag is unset, the
# legacy synchronous path runs verbatim (no behavior change, TF-path safe).
FAILOVER_QUEUE_URL = os.environ.get("FAILOVER_QUEUE_URL", "")
# A tenant stuck in failover_queued/failover_recovering longer than this (a lost
# SQS message, a dead worker) is force-flipped to failover_failed + audited, so
# it can never be stranded invisibly. Mirrors MIGRATION_WATCHDOG_MINUTES.
FAILOVER_WATCHDOG_MINUTES = int(os.environ.get("FAILOVER_WATCHDOG_MINUTES", "30"))


def _scan_all(table, **kwargs):
    """Thin wrapper over the shared paginating scan (T3-3); kept as a
    module-level name so existing test monkeypatches stay put."""
    return _ddb_helpers.scan_all(table, **kwargs)


def lambda_handler(event, context):
    """Scan running tenants, recover host-agent if needed, then check AZ-level health.

    T3-2: an event carrying ``{"manual_failover_az": "<az>"}`` (fired by the API
    Lambda's POST /failover/{az}) forces a synthetic outage for that AZ even if
    the automatic detector wouldn't flag it, and bypasses the per-AZ cooldown for
    the forced AZ. Previously this payload was silently ignored, so the T2-8
    manual-failover endpoint was a no-op.
    """
    force_az = (event or {}).get("manual_failover_az") if isinstance(event, dict) else None
    force_azs = {force_az} if force_az else set()

    tenants = _scan_all(
        tenants_table,        FilterExpression="#s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "running"},
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
            except Exception:
                pass

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
        print(f"watchdog: {stale_count} stale tenant(s), {recovered} host-agent restart(s)")

    # ------- AZ-level failover (1.3.0) -------
    # A manual /failover/{az} invoke must act even when AZ_FAILOVER_ENABLED is
    # off (the operator explicitly asked for it); auto detection stays gated.
    if AZ_FAILOVER_ENABLED or force_azs:
        try:
            failover_summary = _check_and_handle_az_failover(now, tenants, force_azs=force_azs)
            if failover_summary["az_outages_detected"]:
                print(f"az_failover: {json.dumps(failover_summary)}")
        except Exception as e:
            # AZ failover failures must NEVER take down the watchdog.
            print(f"az_failover error (non-fatal): {e}")

    # ------- Failover watchdog (T3-2 P3) -------
    # A tenant stuck in failover_queued/failover_recovering past the watchdog
    # window (lost SQS message, dead worker) is force-flipped to failover_failed
    # so it surfaces to operators instead of hanging invisibly. Only meaningful
    # when the queue path is active; harmless otherwise.
    if FAILOVER_QUEUE_URL:
        try:
            _sweep_stuck_failovers(now)
        except Exception as e:
            print(f"failover watchdog error (non-fatal): {e}")

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
        migrating = _scan_all(
        tenants_table,            FilterExpression="#s = :m",
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


def _bump_target_counters(target_host_id, target_vm_num, vcpu, mem_mb):
    """Book a migrated/failed-over VM onto the target host's counters.

    Issue #77 follow-up: the old inline code did `SET next_vm_num = :n` with an
    absolute value computed from a STALE read taken when the move started. If a
    concurrent tenant create had already advanced next_vm_num past that value,
    the write would rewind it and a future create could hand out a duplicate
    vm_num (→ duplicate guest_ip / host_port). Split the write:
      1. counters (vm_count / used_vcpu / used_mem_mb) — plain atomic adds;
      2. next_vm_num — raised to target_vm_num+1 ONLY if that moves it forward
         (ConditionExpression), otherwise left alone.
    Best-effort like before: failures are logged, never raised.
    """
    try:
        hosts_table.update_item(
            Key={"instance_id": target_host_id},
            UpdateExpression=("SET vm_count = if_not_exists(vm_count, :z) + :one, "
                              "used_vcpu = if_not_exists(used_vcpu, :z) + :v, "
                              "used_mem_mb = if_not_exists(used_mem_mb, :z) + :m"),
            ExpressionAttributeValues={":one": 1, ":v": vcpu, ":m": mem_mb, ":z": 0},
        )
    except Exception as e:
        print(f"target counter inc failed (non-fatal): {e}")
    try:
        hosts_table.update_item(
            Key={"instance_id": target_host_id},
            UpdateExpression="SET next_vm_num = :nn",
            ConditionExpression=("attribute_not_exists(next_vm_num) OR "
                                 "next_vm_num < :nn"),
            ExpressionAttributeValues={":nn": target_vm_num + 1},
        )
    except Exception as e:
        # ConditionalCheckFailed = a concurrent create already moved it past
        # target_vm_num+1 — exactly the case we must NOT rewind. Anything else
        # is logged and tolerated (same best-effort contract as before).
        msg = str(e)
        if "ConditionalCheckFailed" not in msg:
            print(f"target next_vm_num raise failed (non-fatal): {e}")


def _rollback_migration(tenant, reason):
    """Roll a failed/stuck migration back to `running` and clear the async
    context. For LIVE migration the source VM was only briefly paused and then
    resumed by migrate-vm.sh, and host_id / counters / routing were never
    touched — so 'running' is the truthful state and there is nothing to undo.

    For COLD migration the source VM was STOPPED (its data volume shipped)
    before the target launch, so on failure the source has no running VM. We
    relaunch it on the source host so 'running' is truthful again. host_id was
    never flipped, so the tenant returns to exactly where it started.
    """
    tid = tenant["id"]
    migration_mode = tenant.get("migration_mode", "live")
    if migration_mode == "cold":
        # Re-launch on the (unchanged) source host from its data volume.
        src = tenant.get("migration_source", "") or tenant.get("host_id", "")
        vm_num = int(tenant.get("vm_num", 1))
        vcpu = int(tenant.get("vcpu", 2))
        mem_mb = int(tenant.get("mem_mb", 4096))
        if src:
            relaunch = _ssm_send_hc(
                src,
                f"/home/ubuntu/launch-vm.sh {tid} {vm_num} {vcpu} {mem_mb}",
                timeout=300,
            )
            print(f"cold rollback {tid}: relaunch on source {src} → {relaunch}")
    tenants_table.update_item(
        Key={"id": tid},
        UpdateExpression=(
            "SET #s = :r, migration_failed = :reason, updated_at = :t "
            "REMOVE migration_target, migration_target_vm_num, migration_source, "
            "migration_snap_cmd, migration_restore_cmd, migration_phase, "
            "migration_mode, migration_started_at, migration_snapshot_uri"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":r": "running", ":reason": reason[:500],
            ":t": datetime.now(timezone.utc).isoformat(),
        },
    )
    _emit_audit("MIGRATION_FAILED", {"tenant_id": tid, "reason": reason[:200],
                                     "mode": migration_mode})
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
    vm_num = int(tenant.get("vm_num", 1))
    # Issue #72: "cold" migration ships the (stopped) data volume and re-launches
    # on the target; "live" (default) does snapshot/restore. The restore verb
    # differs; everything else in the state machine is identical.
    migration_mode = tenant.get("migration_mode", "live")
    restore_verb = "cold-restore" if migration_mode == "cold" else "restore"

    # Watchdog — never let a tenant sit in `migrating` forever.
    started = tenant.get("migration_started_at", "")
    if started:
        try:
            elapsed_min = (now - datetime.fromisoformat(started)).total_seconds() / 60.0
            if elapsed_min > MIGRATION_WATCHDOG_MINUTES:
                _rollback_migration(tenant, f"watchdog: stuck in {phase} for "
                                            f"{int(elapsed_min)}min")
                return
        except Exception:
            pass

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
        # Snapshot/dump done — fire the (mode-appropriate) restore on the target.
        restore_cmd = _ssm_send_hc(
            target_host_id,
            f"/home/ubuntu/migrate-vm.sh {restore_verb} {tid} {target_vm_num} {snap_uri}",
            timeout=600,
        )
        if not restore_cmd:
            _rollback_migration(tenant, "failed to submit restore SSM command")
            return
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=("SET migration_phase = :p, "
                              "migration_restore_cmd = :rc, updated_at = :t"),
            ExpressionAttributeValues={
                ":p": "restore", ":rc": restore_cmd,
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

        # Restore succeeded on the data plane. Before flipping the source of
        # truth, gate on the public-path dashboard check — the same reality
        # check AZ failover uses (1.4.2). If the dashboard isn't reachable
        # through the ALB, the move isn't really done; roll back.
        target = hosts_table.get_item(
            Key={"instance_id": target_host_id}).get("Item") or {}
        target_ip = target.get("private_ip", "")
        try:
            if target_ip:
                _repoint_alb_rule(tid, target_host_id, target_ip)
            else:
                _rollback_migration(tenant, "target host has no private_ip")
                return
        except Exception as e:
            _rollback_migration(tenant, f"ALB repoint failed: {e}")
            return

        if PUBLIC_BASE_URL and not _verify_dashboard_reachable_via_alb(
                tid, PUBLIC_BASE_URL, timeout_sec=30, poll_sec=3):
            _rollback_migration(tenant, "dashboard not reachable via ALB after restore")
            return

        # Every gate passed — flip ownership + counters, status → running,
        # clear the async context. vcpu/mem come from the tenant record.
        vcpu = int(tenant.get("vcpu", 0))
        mem_mb = int(tenant.get("mem_mb", 0))
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET host_id = :h, vm_num = :n, #s = :running, updated_at = :t "
                "REMOVE migration_target, migration_target_vm_num, migration_source, "
                "migration_snap_cmd, migration_restore_cmd, migration_phase, "
                "migration_mode, migration_started_at, migration_snapshot_uri, "
                "migration_failed"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":h": target_host_id, ":n": target_vm_num, ":running": "running",
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
        # Source counters -- , target counters ++ (if_not_exists guards cold rows).
        if source_host_id:
            try:
                hosts_table.update_item(
                    Key={"instance_id": source_host_id},
                    UpdateExpression=("SET used_vcpu = if_not_exists(used_vcpu, :z) - :v, "
                                      "used_mem_mb = if_not_exists(used_mem_mb, :z) - :m, "
                                      "vm_count = if_not_exists(vm_count, :z) - :one"),
                    ExpressionAttributeValues={":v": vcpu, ":m": mem_mb,
                                               ":one": 1, ":z": 0},
                )
            except Exception as e:
                print(f"source counter dec failed (non-fatal): {e}")
        _bump_target_counters(target_host_id, target_vm_num, vcpu, mem_mb)

        # Best-effort: stop the old VM on the source + clean its nginx conf so
        # the slot is freed and it stops advertising as a backend.
        if source_host_id:
            try:
                _ssm_send_hc(
                    source_host_id,
                    f"/home/ubuntu/stop-vm.sh {tid} {vm_num} ; "
                    f"sudo rm -f /etc/nginx/conf.d/tenants/{tid}.conf "
                    f"&& sudo nginx -s reload",
                    timeout=60,
                )
            except Exception:
                pass

        _emit_audit("MIGRATION_COMPLETED", {
            "tenant_id": tid, "source_host_id": source_host_id,
            "target_host_id": target_host_id,
        })
        print(f"migration {tid}: COMPLETE → {target_host_id}")
        return

    # Unknown phase — don't strand the tenant.
    _rollback_migration(tenant, f"unknown migration_phase: {phase!r}")


def _poll_ssm(command_id, instance_id):
    """Single, non-blocking SSM status check → (done, ok). Thin wrapper over the
    shared helper (T3-3); kept as a module name for existing monkeypatches."""
    return _ssm_helpers.poll(ssm, command_id, instance_id)


def _ssm_send_hc(instance_id, command, timeout=120):
    """Fire-and-forget SSM from the health_check Lambda → CommandId or None.
    Thin wrapper over the shared helper (was a byte-identical copy of api
    _ssm_send)."""
    try:
        return _ssm_helpers.send(ssm, instance_id, command, timeout)
    except Exception as e:
        print(f"_ssm_send_hc error: {e}")
        return None


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
                print(f"skip restart {host_id}: cooldown ({int(elapsed)}s < {RESTART_COOLDOWN_SECONDS}s)")
                return False
        except Exception:
            pass

    # Restart host-agent via SSM (single command recovers all VMs on this host)
    try:
        ssm.send_command(
            InstanceIds=[host_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": ["systemctl restart host-agent"], "executionTimeout": ["30"]},
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
            out.append({
                "az": az,
                "host_ids": [h["instance_id"] for h in host_list],
                "host_count": len(host_list),
            })
    return out


def pick_target_host(hosts, now, threshold_minutes, exclude_azs,
                     required_vcpu=0, required_mem=0):
    """Choose the best healthy host outside ``exclude_azs`` for failover.

    T3-3: the health/AZ filtering (unhealthy, excluded AZ, deleted) stays here,
    but the *capacity* verdict + ranking now delegate to common.capacity — the
    SAME math the API scheduler uses. This closes the divergence where failover
    ignored the overcommit ratios, never checked memory, and never enforced
    MAX_VMS_PER_HOST, so it could place a VM on a host the API would reject.

    ``required_mem`` is new; callers should pass the tenant's mem_mb so a
    memory-exhausted host is rejected. Returns the host record, or None.
    """
    eligible = [
        h for h in hosts
        if h.get("az", "") not in exclude_azs
        and not is_host_unhealthy(h, now, threshold_minutes)
        and h.get("status") != "deleted"
    ]
    ranked = _capacity.rank_hosts(
        eligible, required_vcpu, required_mem,
        cpu_ratio=CPU_OVERCOMMIT_RATIO, mem_ratio=MEM_OVERCOMMIT_RATIO,
        max_vms=MAX_VMS_PER_HOST)
    return ranked[0] if ranked else None


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


def _check_and_handle_az_failover(now, tenants, force_azs=None):
    """End-to-end AZ failover: detect outages, pick target, relaunch tenants.

    This is the AWS-side entrypoint. Pure logic lives in the helpers above.
    Returns a summary dict for logging / audit.

    ``force_azs`` (T3-2): a set of AZ names to treat as down even if automatic
    detection wouldn't flag them — the manual /failover/{az} path. Forced AZs
    also bypass the per-AZ cooldown guard (the operator asked explicitly) while
    still stamping cooldown state to suppress the next automatic tick.

    When ``FAILOVER_QUEUE_URL`` is set, affected tenants are claimed + enqueued
    to the failover worker queue instead of being recovered synchronously here.
    """
    force_azs = force_azs or set()
    summary = {
        "az_outages_detected": 0,
        "tenants_failed_over": 0,
        "tenants_failed": 0,
        # 1.3.2: split out path-A "blocked" (no backup, refused to lose data)
        # from "failed" (SSM error, capacity exhausted, etc.) so summaries
        # accurately reflect WHY a tenant didn't migrate.
        "tenants_blocked": 0,
        # T3-2: tenants handed to the async worker queue (queue path only).
        "tenants_queued": 0,
        "skipped_cooldown": [],
    }

    # 1) Load hosts, excluding status=deleted so stale zombie rows (a
    #    terminated instance whose row lingered) can't be bucketed into their AZ
    #    and counted as "unhealthy", faking an AZ outage. Rows with no status
    #    field are kept (they're normal in the pre-status data model / tests);
    #    the __az_failover_state__ bookkeeping record has no `az` field so
    #    group_hosts_by_az already skips it.
    hosts = [h for h in _scan_all(hosts_table)
             if h.get("status") != "deleted"]

    # 2) Detect outage AZs, then splice in any operator-forced AZs that
    #    detection didn't already flag (synthetic outage = all non-deleted
    #    hosts in that AZ).
    outages = detect_unhealthy_azs(hosts, now, AZ_UNHEALTHY_THRESHOLD_MINUTES)
    detected_azs = {o["az"] for o in outages}
    for faz in force_azs:
        if faz in detected_azs:
            continue
        forced_host_ids = [h["instance_id"] for h in hosts if h.get("az") == faz]
        outages.append({"az": faz, "host_ids": forced_host_ids,
                        "host_count": len(forced_host_ids), "forced": True})
    if not outages:
        return summary
    summary["az_outages_detected"] = len(outages)

    # 3) Load AZ failover state (kept on a synthetic host record with id=__az_failover_state__).
    state_record = hosts_table.get_item(
        Key={"instance_id": "__az_failover_state__"}
    ).get("Item") or {}
    az_state = state_record.get("az_last_failover", {}) or {}

    # A forced AZ's hosts may still look healthy to is_host_unhealthy, so
    # exclude forced AZs explicitly — we must never pick a target inside an AZ
    # the operator just declared down.
    healthy_azs = {
        h.get("az") for h in hosts
        if h.get("az") and h.get("az") not in force_azs
        and not is_host_unhealthy(h, now, AZ_UNHEALTHY_THRESHOLD_MINUTES)
    }
    if not healthy_azs:
        # All AZs are out — nothing we can do; surface and bail.
        _emit_audit("AZ_FAILOVER_SKIPPED",
                    {"reason": "no_healthy_az", "outages": [o["az"] for o in outages]})
        return summary

    for outage in outages:
        az = outage["az"]
        forced = az in force_azs
        # Forced (manual) failover bypasses the cooldown guard — the operator
        # asked explicitly — but we still stamp cooldown state below to suppress
        # the next automatic tick from re-acting on the same AZ.
        if not forced and should_skip_az_for_cooldown(az_state, az, now, AZ_COOLDOWN_MINUTES):
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
            hosts_table.put_item(Item={
                "instance_id": "__az_failover_state__",
                "az_last_failover": az_state,
                "updated_at": now.isoformat(),
            })
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
            _emit_audit("AZ_FAILOVER_NO_TENANTS_AFFECTED",
                        {"az": az, "host_ids": outage["host_ids"]})
            _emit_sns_notification(az, outage, recovered_count=0)
            continue

        # Never place a recovered tenant back into ANY down AZ (this one plus
        # every other detected/forced outage in the same tick).
        exclude = {o["az"] for o in outages} | force_azs

        for tenant in tenants:
            if tenant["id"] not in affected_tenant_ids:
                continue
            target = pick_target_host(
                hosts, now,
                threshold_minutes=AZ_UNHEALTHY_THRESHOLD_MINUTES,
                exclude_azs=exclude,
                required_vcpu=int(tenant.get("vcpu") or 1),
                required_mem=int(tenant.get("mem_mb") or 4096),
            )
            if not target:
                summary["tenants_failed"] += 1
                _emit_audit("AZ_FAILOVER_NO_TARGET",
                            {"tenant_id": tenant["id"], "from_az": az})
                continue

            if FAILOVER_QUEUE_URL:
                # T3-2: enqueue for the parallel worker instead of recovering
                # synchronously here. _enqueue_failover does the no-backup
                # refusal, the running→failover_queued claim, and the atomic
                # target vm_num reservation, then sends one SQS message.
                outcome = _enqueue_failover(tenant, target, az, now)
                if outcome == "queued":
                    summary["tenants_queued"] += 1
                    # Book the reservation in-memory so the next tenant in this
                    # batch spreads onto capacity rather than colliding.
                    target["vm_count"] = int(target.get("vm_count") or 0) + 1
                    target["used_vcpu"] = int(target.get("used_vcpu") or 0) + int(tenant.get("vcpu") or 1)
                elif outcome == "blocked":
                    summary["tenants_blocked"] += 1
                else:
                    summary["tenants_failed"] += 1
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

        # 6) SNS notification (best-effort). Report queued count on the queue
        # path (recovery happens later in the workers), recovered otherwise.
        _emit_sns_notification(
            az, outage,
            summary["tenants_queued"] if FAILOVER_QUEUE_URL else summary["tenants_failed_over"])

    # State already persisted at the start of each outage handling above.
    return summary


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
    target_vm_num = int(target_host.get("next_vm_num") or 1)
    source_host_id = tenant.get("host_id", "")

    # 1) Find latest backup (path A: refuse if missing).
    backup_key = _find_latest_backup_key(tenant_id) if ASSETS_BUCKET else None
    if not backup_key:
        _emit_audit("AZ_FAILOVER_NO_BACKUP", {
            "tenant_id": tenant_id,
            "from_az": source_az,
            "reason": "no backup available — failover refused to avoid data loss",
        })
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
        except Exception:
            pass
        # SNS alert so a human can manually intervene.
        if _SNS_TOPIC_ARN:
            try:
                sns.publish(
                    TopicArn=_SNS_TOPIC_ARN,
                    Subject=f"[OpenClaw] AZ failover BLOCKED: {tenant_id} has no backup",
                    Message=json.dumps({
                        "event": "az_failover_blocked",
                        "tenant_id": tenant_id,
                        "reason": "no_backup_available",
                        "source_az": source_az,
                        "action_required": "manual recovery — restore from snapshot or accept data loss",
                    }, indent=2),
                )
            except Exception:
                pass
        return "blocked"  # 1.3.2: distinct from failures — caller buckets separately

    # 2) Mark recovering — with conditional update on host_id to prevent
    # concurrent Lambda invocations from both trying to migrate the same
    # tenant. If another invocation already moved it, ConditionalCheckFailed
    # raises; we skip cleanly and don't report failure.
    try:
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=("SET previous_host_id = :p, "
                              "failover_from_az = :az, failover_at = :t, "
                              "#s = :recover"),
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
        _emit_audit("AZ_FAILOVER_SKIPPED_CONCURRENT",
                    {"tenant_id": tenant_id, "from_az": source_az})
        return False

    # Claim held — run the launch/verify/repoint/flip pipeline. Extracted so
    # the T3-2 worker Lambda can reuse it after doing its own SQS-side claim.
    return _execute_failover(tenant, target_host, source_az, backup_key,
                             target_vm_num, now)


def _execute_failover(tenant, target_host, source_az, backup_key, target_vm_num, now):
    """Steps 3-8 of AZ failover: launch on target, verify gates, repoint ALB,
    flip DDB ownership. Assumes the caller already (a) found ``backup_key`` and
    (b) claimed the tenant into ``failover_recovering``. Shared by the
    synchronous detector path and the async worker (T3-2).

    Returns True on full recovery; False on failure (tenant flipped to
    failover_failed / failover_failed_partial, audit emitted). Never raises.
    """
    tenant_id = tenant["id"]
    vcpu = int(tenant.get("vcpu") or 2)
    mem_mb = int(tenant.get("mem_mb") or 4096)
    target_host_id = target_host["instance_id"]
    config_template = tenant.get("config_template") or ""
    source_host_id = tenant.get("host_id", "") or tenant.get("previous_host_id", "")

    try:
        # 3) Launch on target host via SSM with POSITIONAL args.
        #    launch-vm.sh signature:
        #      launch-vm.sh <tenant_id> <vm_num> <vcpu> <mem_mb>
        #                   <config_template> <restore_backup_key>
        #    config_template can be empty string ""; restore_backup_key
        #    is an S3 key (no s3:// prefix), e.g. backups/<tid>/<ts>.gz.
        launch_cmd = (
            f"/home/ubuntu/launch-vm.sh {tenant_id} {target_vm_num} "
            f"{vcpu} {mem_mb} \"{config_template}\" \"{backup_key}\""
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
            _emit_audit("AZ_FAILOVER_RECOVERED_BY_VERIFY", {
                "tenant_id": tenant_id,
                "from_az": source_az,
                "to_host": target_host_id,
                "ssm_err": ssm_err[:200] if ssm_err else "",
            })

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
                _emit_audit("AZ_FAILOVER_ALB_REPOINT_FAILED",
                            {"tenant_id": tenant_id, "error": str(e)[:200]})
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
            print("WARN: PUBLIC_BASE_URL not set; skipping cross-ALB verify gate. "
                  "Redeploy with 1.4.2+ stack to enable end-to-end dashboard verification.")

        # 7) Best-effort: tell source host to clean its nginx conf.
        #    If the source host is unreachable (which is the whole reason
        #    we're failing over), this SSM call will time out — that's fine,
        #    we don't gate failover success on it.
        if source_host_id:
            try:
                ssm.send_command(
                    InstanceIds=[source_host_id],
                    DocumentName="AWS-RunShellScript",
                    Parameters={"commands": [
                        f"sudo rm -f /etc/nginx/conf.d/tenants/{tenant_id}.conf "
                        f"&& sudo nginx -s reload || true"
                    ], "executionTimeout": ["10"]},
                )
            except Exception:
                pass  # Source unreachable is the expected case.

        # 8) Flip ownership in DDB. ONLY now that every gate has passed.
        #    Bump next_vm_num on target host as part of the same flip.
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=("SET host_id = :h, vm_num = :n, #s = :running, "
                              "restored_from = :b"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":h": target_host_id,
                ":n": target_vm_num,
                ":running": "running",
                ":b": backup_key,
            },
        )
        _bump_target_counters(target_host_id, target_vm_num, vcpu, mem_mb)

        _emit_audit("AZ_FAILOVER_TENANT_RECOVERED", {
            "tenant_id": tenant_id,
            "from_az": source_az,
            "to_host": target_host_id,
            "to_az": target_host.get("az", ""),
            "restored_from": backup_key,
        })
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
        except Exception:
            pass
        _emit_audit("AZ_FAILOVER_TENANT_FAILED", {
            "tenant_id": tenant_id,
            "error": err_str[:200],
            "status_set": new_status,
        })
        return False


def _reserve_target_vm_num(target_host_id):
    """Atomically reserve the next vm_num on the target host and return it.

    Uses an atomic increment with ReturnValues=UPDATED_OLD so parallel workers
    (T3-2) never hand out the same vm_num — the in-memory ``next_vm_num`` bump
    the synchronous path relied on is not safe once failovers run concurrently.
    A reserved number is never reused (leak-on-failure is intentional, same as
    the 1.3.2 sync path: a partially-created tap-vmN device would trip
    TUNSETIFF EBUSY if the number were recycled). Returns 1 on error.
    """
    try:
        resp = hosts_table.update_item(
            Key={"instance_id": target_host_id},
            UpdateExpression="SET next_vm_num = if_not_exists(next_vm_num, :one) + :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_OLD",
        )
        old = resp.get("Attributes", {}).get("next_vm_num")
        # UPDATED_OLD returns the value BEFORE the add; that's the slot to use.
        # if_not_exists made the pre-value absent on first use → slot 1.
        return int(old) if old is not None else 1
    except Exception as e:
        print(f"reserve vm_num on {target_host_id} failed (non-fatal): {e}")
        return 1


def _enqueue_failover(tenant, target_host, source_az, now):
    """Detector-side of the T3-2 split: do the no-backup refusal + the
    running→failover_queued claim + the atomic target vm_num reservation, then
    send one SQS message for the worker to execute. Returns "queued", "blocked"
    (no backup — data-loss refusal), or "failed" (claim lost / send error).

    The claim's ConditionExpression requires BOTH host_id==source AND
    status==running, so a manual + scheduled tick racing past cooldown can't
    double-enqueue the same tenant.
    """
    tenant_id = tenant["id"]
    source_host_id = tenant.get("host_id", "")
    target_host_id = target_host["instance_id"]

    # 1) No-backup refusal (path A) — run in the detector so blocked tenants
    #    never enqueue and the alert fires immediately. Mirrors the sync path.
    backup_key = _find_latest_backup_key(tenant_id) if ASSETS_BUCKET else None
    if not backup_key:
        _emit_audit("AZ_FAILOVER_NO_BACKUP", {
            "tenant_id": tenant_id, "from_az": source_az,
            "reason": "no backup available — failover refused to avoid data loss",
        })
        try:
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :b, failover_error = :e",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":b": "failover_blocked",
                                           ":e": "no_backup_available"},
            )
        except Exception:
            pass
        if _SNS_TOPIC_ARN:
            try:
                sns.publish(
                    TopicArn=_SNS_TOPIC_ARN,
                    Subject=f"[OpenClaw] AZ failover BLOCKED: {tenant_id} has no backup",
                    Message=json.dumps({
                        "event": "az_failover_blocked", "tenant_id": tenant_id,
                        "reason": "no_backup_available", "source_az": source_az,
                        "action_required": "manual recovery — restore from snapshot or accept data loss",
                    }, indent=2),
                )
            except Exception:
                pass
        return "blocked"

    # 2) Claim: running→failover_queued, guarded on host_id AND status so a
    #    concurrent enqueue for the same tenant loses the race cleanly.
    try:
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=("SET previous_host_id = :p, failover_from_az = :az, "
                              "failover_at = :t, #s = :queued"),
            ConditionExpression="host_id = :p AND #s = :running",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":p": source_host_id, ":az": source_az, ":t": now.isoformat(),
                ":queued": "failover_queued", ":running": "running",
            },
        )
    except tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        print(f"skip {tenant_id}: already claimed by a concurrent tick")
        _emit_audit("AZ_FAILOVER_SKIPPED_CONCURRENT",
                    {"tenant_id": tenant_id, "from_az": source_az})
        return "failed"

    # 3) Reserve the target vm_num atomically (parallel-worker safe).
    target_vm_num = _reserve_target_vm_num(target_host_id)

    # 4) Enqueue one job for the worker.
    try:
        sqs.send_message(
            QueueUrl=FAILOVER_QUEUE_URL,
            MessageBody=json.dumps({
                "tenant_id": tenant_id,
                "source_az": source_az,
                "source_host_id": source_host_id,
                "target_host_id": target_host_id,
                "target_vm_num": target_vm_num,
                "backup_key": backup_key,
                "queued_at": now.isoformat(),
            }),
        )
    except Exception as e:
        # Claim already flipped to failover_queued; the watchdog will reap it
        # if we can't enqueue. Report failed so the summary is truthful.
        print(f"enqueue failover for {tenant_id} failed: {e}")
        _emit_audit("AZ_FAILOVER_ENQUEUE_FAILED",
                    {"tenant_id": tenant_id, "error": str(e)[:200]})
        return "failed"

    _emit_audit("AZ_FAILOVER_TENANT_QUEUED", {
        "tenant_id": tenant_id, "from_az": source_az,
        "to_host": target_host_id, "target_vm_num": target_vm_num,
    })
    return "queued"


def _sweep_stuck_failovers(now):
    """Watchdog (T3-2 P3): tenants stuck in failover_queued/failover_recovering
    past FAILOVER_WATCHDOG_MINUTES (a lost SQS message, a dead worker) are
    force-flipped to failover_failed + audited so they surface to operators
    rather than hanging invisibly. Mirrors the migration watchdog.
    """
    cutoff = FAILOVER_WATCHDOG_MINUTES * 60
    stuck = _scan_all(
        tenants_table,
        FilterExpression="#s = :q OR #s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":q": "failover_queued", ":r": "failover_recovering"},
    )
    reaped = 0
    for tenant in stuck:
        at = tenant.get("failover_at", "")
        if at:
            try:
                if (now - datetime.fromisoformat(at)).total_seconds() < cutoff:
                    continue  # still within the grace window
            except Exception:
                pass
        tid = tenant["id"]
        try:
            tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression="SET #s = :f, failover_error = :e",
                ConditionExpression="#s = :q OR #s = :r",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":f": "failover_failed",
                    ":e": f"watchdog: stuck > {FAILOVER_WATCHDOG_MINUTES}min",
                    ":q": "failover_queued", ":r": "failover_recovering",
                },
            )
            reaped += 1
            _emit_audit("AZ_FAILOVER_WATCHDOG_REAPED",
                        {"tenant_id": tid, "stuck_since": at})
        except tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
            pass  # a worker just finished it — fine
        except Exception as e:
            print(f"failover watchdog reap {tid} failed (non-fatal): {e}")
    if reaped:
        print(f"failover watchdog: reaped {reaped} stuck tenant(s)")


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
    """Block until an SSM command completes → (ok, error_or_None). Thin wrapper
    over the shared helper (T3-3)."""
    return _ssm_helpers.wait(ssm, command_id, instance_id, timeout_sec, poll_sec)


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
        f"if [ \"$code\" = \"000\" ] || [ \"$code\" -ge 500 ] 2>/dev/null; then "
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
                    CommandId=cmd_id, InstanceId=host_id,
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


def _verify_dashboard_reachable_via_alb(tenant_id, public_base_url, timeout_sec=30, poll_sec=3):
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
    import urllib.error
    import urllib.request

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
    print(f"cross-ALB verify FAILED for {tenant_id}: "
          f"last_status={last_status}, last_err={last_err}")
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
        # T3-3: use the VPC_ID env (same source the API scheduler uses). Only
        # fall back to cloning from an existing TG on stacks not yet redeployed
        # with VPC_ID injected into this Lambda.
        vpc_id = VPC_ID
        if not vpc_id:
            existing = elbv2.describe_target_groups()["TargetGroups"]
            if not existing:
                raise RuntimeError(
                    "cannot create target group: VPC_ID env not set and no "
                    "existing target group to clone VPC from") from None
            vpc_id = existing[0]["VpcId"]
        tg_arn = elbv2.create_target_group(
            Name=tg_name, Protocol="HTTP", Port=80, VpcId=vpc_id,
            TargetType="ip", HealthCheckPath="/health",
            HealthCheckIntervalSeconds=10, HealthyThresholdCount=2,
        )["TargetGroups"][0]["TargetGroupArn"]
    # Make sure the host IP is registered (idempotent).
    try:
        elbv2.register_targets(
            TargetGroupArn=tg_arn,
            Targets=[{"Id": target_private_ip, "Port": 80}],
        )
    except Exception as e:
        print(f"register_targets {target_private_ip} on {tg_name}: {e}")

    # 2) Find the existing ALB rule for /vm/<tenant_id>* and modify its
    #    forward Action to point at the new target group.
    rules = elbv2.describe_rules(ListenerArn=ALB_LISTENER_ARN)["Rules"]
    rule_arn = None
    for r in rules:
        for c in r.get("Conditions", []):
            if c.get("Field") == "path-pattern" and \
               any(f"/vm/{tenant_id}" in v for v in c.get("Values", [])):
                rule_arn = r["RuleArn"]
                break
        if rule_arn:
            break
    if not rule_arn:
        # No existing rule — create a fresh one. Issue #77: pick a RANDOM free
        # priority and retry on PriorityInUseException (re-reading live rules
        # each attempt) instead of the racy read-once/lowest-free pattern.
        for _attempt in range(6):
            live = elbv2.describe_rules(ListenerArn=ALB_LISTENER_ARN)["Rules"]
            if any(f"/vm/{tenant_id}" in v
                   for r in live for c in r.get("Conditions", []) for v in c.get("Values", [])):
                break  # another actor created it meanwhile
            used = {int(r["Priority"]) for r in live if r["Priority"] != "default"}
            free = sorted(set(range(1, 500)) - used)
            if not free:
                raise RuntimeError("no free ALB listener rule priority (1-499 exhausted)")
            try:
                elbv2.create_rule(
                    ListenerArn=ALB_LISTENER_ARN, Priority=random.choice(free),
                    Conditions=[{"Field": "path-pattern",
                                 "Values": [f"/vm/{tenant_id}", f"/vm/{tenant_id}/*"]}],
                    Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
                )
                break
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "PriorityInUseException":
                    raise
    else:
        elbv2.modify_rule(
            RuleArn=rule_arn,
            Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )


# Issue #71 — map the health_check UPPERCASE operation constants to the same
# event-style vocabulary the API audit log uses, so both sources render in one
# console Logs table.
_HC_EVENT_MAP = {
    "MIGRATION_COMPLETED": "vm.migrated",
    "MIGRATION_FAILED": "vm.migrate_failed",
    "AZ_FAILOVER_TRIGGERED": "az.failover_triggered",
    "AZ_FAILOVER_COMPLETED": "az.failover_completed",
    "AZ_FAILOVER_NO_TENANTS_AFFECTED": "az.failover_noop",
    "AZ_FAILOVER_NO_TARGET": "az.failover_no_target",
    "AZ_FAILOVER_PARTIAL": "az.failover_partial",
    "AZ_FAILOVER_SKIPPED_COOLDOWN": "az.failover_cooldown",
    "HOST_TERMINATED": "host.terminated",
    "HOST_REAPED_TENANTS": "host.tenants_reaped",
}


def _emit_audit(operation, detail):
    """Best-effort audit log entry. Never raises.

    Writes the SAME schema as the API Lambda's _audit_write (issue #71) so the
    console Logs tab shows control-plane events (migration, AZ failover, host
    lifecycle) alongside API operations. `operation` is kept as the original
    UPPERCASE constant for back-compat (test_az_failover asserts on it); the
    enriched `event` / `object` / `actor` fields are added for the UI.
    """
    if not audit_table:
        return
    tenant_id = detail.get("tenant_id")
    az = detail.get("az")
    host_id = detail.get("host_id") or detail.get("instance_id")
    if tenant_id:
        obj, rid = f"tenant:{tenant_id}", tenant_id
    elif host_id:
        obj, rid = f"host:{host_id}", host_id
    elif az:
        obj, rid = f"az:{az}", az
    else:
        obj, rid = "system", ""
    # actor = the automated source that performed the action.
    source = detail.get("source") or ("az-failover" if az and "FAILOVER" in operation
                                       else "health-check")
    # T3-3: delegate the row write; honors AUDIT_TTL_DAYS (was hard-coded 90d).
    # `operation` keeps the UPPERCASE constant for back-compat (tests assert it);
    # `event` is the enriched lowercase name for the UI.
    _audit.put_audit_row(
        audit_table,
        event=_HC_EVENT_MAP.get(operation, operation.lower()),
        obj=obj, resource_id=rid, actor=f"system:{source}", actor_role="system",
        operation=operation, response_status=200, detail=detail,
        ttl_days=AUDIT_TTL_DAYS, api_key_id="system:health-check-lambda")


def _emit_sns_notification(az, outage, recovered_count):
    """Publish an AZ failover event to SNS if a topic is configured."""
    if not _SNS_TOPIC_ARN:
        return
    try:
        sns.publish(
            TopicArn=_SNS_TOPIC_ARN,
            Subject=f"[OpenClaw] AZ failover triggered: {az}",
            Message=json.dumps({
                "event": "az_failover",
                "az": az,
                "host_ids": outage["host_ids"],
                "host_count": outage["host_count"],
                "tenants_recovered": recovered_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, indent=2),
        )
    except Exception as e:
        print(f"SNS publish failed (non-fatal): {e}")
