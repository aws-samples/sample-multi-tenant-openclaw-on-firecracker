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
import random
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timezone, timedelta

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


def lambda_handler(event, context):
    """Scan running tenants, recover host-agent if needed, then check AZ-level health."""
    tenants = tenants_table.scan(
        FilterExpression="#s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "running"},
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


def _rollback_migration(tenant, reason):
    """Roll a failed/stuck migration back to `running` and clear the async
    context. The source VM was only briefly paused for the snapshot and then
    resumed by migrate-vm.sh, and host_id / counters / routing were never
    touched — so 'running' is the truthful state and there is nothing to undo
    on the data plane. We record migration_failed + the reason for operators.
    """
    tid = tenant["id"]
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
            ":r": "running", ":reason": reason[:500],
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
    vm_num = int(tenant.get("vm_num", 1))

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
                "migration_started_at, migration_snapshot_uri, migration_failed"
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
        try:
            hosts_table.update_item(
                Key={"instance_id": target_host_id},
                UpdateExpression=("SET next_vm_num = :nn, "
                                  "vm_count = if_not_exists(vm_count, :z) + :one, "
                                  "used_vcpu = if_not_exists(used_vcpu, :z) + :v, "
                                  "used_mem_mb = if_not_exists(used_mem_mb, :z) + :m"),
                ExpressionAttributeValues={":nn": target_vm_num + 1, ":one": 1,
                                           ":v": vcpu, ":m": mem_mb, ":z": 0},
            )
        except Exception as e:
            print(f"target counter inc failed (non-fatal): {e}")

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
            CommandId=command_id, InstanceId=instance_id,
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
        print(f"_poll_ssm {command_id}: {status} - "
              f"{(inv.get('StandardErrorContent') or '')[:200]}")
        return True, False
    return False, False  # Pending / InProgress / Delayed


def _ssm_send_hc(instance_id, command, timeout=120):
    """Fire-and-forget SSM from the health_check Lambda; returns CommandId or
    None. Mirrors the api Lambda's _ssm_send (wraps HOME/cd, returns the id so
    the sweep can poll it on the next tick)."""
    try:
        wrapped = f'export HOME=/home/ubuntu && cd /home/ubuntu && {command}'
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
        raw_total = (h.get("total_vcpu") or h.get("vcpu_total")
                     or h.get("max_vcpu") or 0)
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
    state_record = hosts_table.get_item(
        Key={"instance_id": "__az_failover_state__"}
    ).get("Item") or {}
    az_state = state_record.get("az_last_failover", {}) or {}

    healthy_azs = {
        h.get("az") for h in hosts
        if h.get("az") and not is_host_unhealthy(h, now, AZ_UNHEALTHY_THRESHOLD_MINUTES)
    }
    if not healthy_azs:
        # All AZs are out — nothing we can do; surface and bail.
        _emit_audit("AZ_FAILOVER_SKIPPED",
                    {"reason": "no_healthy_az", "outages": [o["az"] for o in outages]})
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

        for tenant in tenants:
            if tenant["id"] not in affected_tenant_ids:
                continue
            target = pick_target_host(
                hosts, now,
                threshold_minutes=AZ_UNHEALTHY_THRESHOLD_MINUTES,
                exclude_azs={az},
                required_vcpu=int(tenant.get("vcpu") or 1),
            )
            if not target:
                summary["tenants_failed"] += 1
                _emit_audit("AZ_FAILOVER_NO_TARGET",
                            {"tenant_id": tenant["id"], "from_az": az})
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
    target_vm_num = int(target_host.get("next_vm_num") or 1)
    config_template = tenant.get("config_template") or ""
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

    try:
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
        try:
            hosts_table.update_item(
                Key={"instance_id": target_host_id},
                UpdateExpression=("SET next_vm_num = :n, "
                                  "vm_count = if_not_exists(vm_count, :z) + :one, "
                                  "used_vcpu = if_not_exists(used_vcpu, :z) + :v, "
                                  "used_mem_mb = if_not_exists(used_mem_mb, :z) + :m"),
                ExpressionAttributeValues={
                    ":n": target_vm_num + 1,
                    ":one": 1,
                    ":v": vcpu,
                    ":m": mem_mb,
                    ":z": 0,
                },
            )
        except Exception as e:
            print(f"host counter update failed (non-fatal): {e}")

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
                CommandId=command_id, InstanceId=instance_id,
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
        # Need VPC ID for create — pulled from existing TG of source host.
        existing = elbv2.describe_target_groups()["TargetGroups"]
        if not existing:
            raise RuntimeError("no existing target groups to clone VPC from")
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
    try:
        import uuid
        ttl = int(datetime.now(timezone.utc).timestamp()) + 90 * 86400
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
        audit_table.put_item(Item={
            "pk": "audit",
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "operation": operation,          # kept for back-compat
            "resource_id": rid,
            "api_key_id": "system:health-check-lambda",
            "response_status": 200,
            "detail": json.dumps(detail)[:1000],
            "expires_ttl": ttl,
            # Issue #71 enrichment (unified schema):
            "event": _HC_EVENT_MAP.get(operation, operation.lower()),
            "object": obj,
            "actor": f"system:{source}",
            "actor_role": "system",
        })
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
