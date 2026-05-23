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
from datetime import datetime, timezone, timedelta

ddb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")
sns = boto3.client("sns")
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
        # Estimate spare capacity. Hosts publish vcpu_total / vm_count.
        vcpu_total = int(h.get("vcpu_total") or h.get("max_vcpu") or 0)
        vm_count = int(h.get("vm_count") or 0)
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

        # 4) Find tenants on the failed AZ.
        affected_tenant_ids = set()
        for t in tenants:
            host_id = t.get("host_id", "")
            if host_id in outage["host_ids"]:
                affected_tenant_ids.add(t["id"])
        if not affected_tenant_ids:
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
            ok = _failover_tenant_to_host(tenant, target, az, now)
            if ok:
                summary["tenants_failed_over"] += 1
            else:
                summary["tenants_failed"] += 1

        # 5) Update cooldown state for this AZ.
        az_state[az] = now.isoformat()

        # 6) SNS notification (best-effort).
        _emit_sns_notification(az, outage, summary["tenants_failed_over"])

    # 7) Persist state.
    if outages:
        hosts_table.put_item(Item={
            "instance_id": "__az_failover_state__",
            "az_last_failover": az_state,
            "updated_at": now.isoformat(),
        })

    return summary


def _failover_tenant_to_host(tenant, target_host, source_az, now):
    """Relaunch a tenant on a healthy target host.

    Strategy:
      * Mark tenant as ``failover_recovering``.
      * On the target host, run launch-vm.sh (data dir empty) — cannot live-migrate
        because the source AZ is unreachable. The tenant's data volume from the
        old host is gone; we restore from the most recent backup if one exists.
      * Update DDB: tenant.host_id = target, vm_num = target.next_vm_num,
        previous_host_id, failover_at, status='running'.
    """
    tenant_id = tenant["id"]
    vcpu = int(tenant.get("vcpu") or 2)
    mem_mb = int(tenant.get("mem_mb") or 4096)
    target_host_id = target_host["instance_id"]
    target_vm_num = int(target_host.get("next_vm_num") or 1)

    try:
        # 1) Mark recovering.
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=("SET previous_host_id = :p, "
                              "failover_from_az = :az, failover_at = :t, "
                              "#s = :recover"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":p": tenant.get("host_id", ""),
                ":az": source_az,
                ":t": now.isoformat(),
                ":recover": "failover_recovering",
            },
        )

        # 2) Launch on the target. We try restoring from latest backup first
        # so the tenant's /home/agent state is preserved if backups exist.
        backup_uri = ""
        if ASSETS_BUCKET:
            backup_uri = f"s3://{ASSETS_BUCKET}/backups/{tenant_id}/latest.gz"
        launch_cmd = (
            f"/home/ubuntu/launch-vm.sh {tenant_id} {target_vm_num} {vcpu} {mem_mb}"
        )
        if backup_uri:
            # Best-effort restore: launch with --restore flag if launch-vm.sh
            # supports it; otherwise it's ignored. The host-agent script handles
            # missing backups gracefully.
            launch_cmd += f" --restore-from {backup_uri}"

        ssm.send_command(
            InstanceIds=[target_host_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [launch_cmd], "executionTimeout": ["600"]},
        )

        # 3) Flip ownership in DDB.
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=("SET host_id = :h, vm_num = :n, #s = :running"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":h": target_host_id,
                ":n": target_vm_num,
                ":running": "running",
            },
        )

        _emit_audit("AZ_FAILOVER_TENANT_RECOVERED", {
            "tenant_id": tenant_id,
            "from_az": source_az,
            "to_host": target_host_id,
            "to_az": target_host.get("az", ""),
        })
        return True
    except Exception as e:
        print(f"failover failed for {tenant_id}: {e}")
        try:
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :failed",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":failed": "failover_failed"},
            )
        except Exception:
            pass
        _emit_audit("AZ_FAILOVER_TENANT_FAILED", {
            "tenant_id": tenant_id, "error": str(e)[:200],
        })
        return False


def _emit_audit(operation, detail):
    """Best-effort audit log entry. Never raises."""
    if not audit_table:
        return
    try:
        import uuid
        ttl = int(datetime.now(timezone.utc).timestamp()) + 90 * 86400
        audit_table.put_item(Item={
            "pk": "audit",
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "resource_id": detail.get("tenant_id") or detail.get("az") or "",
            "api_key_id": "system:health-check-lambda",
            "response_status": 200,
            "detail": json.dumps(detail)[:1000],
            "expires_ttl": ttl,
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
