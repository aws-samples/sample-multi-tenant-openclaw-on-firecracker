"""Health check Lambda — watchdog for host-agent.
Runs every 5 minutes. Detects stale agent data and recovers host-agent.

Normal health updates are done by host-agent directly to DynamoDB.
This Lambda is the second line of defense: if host-agent itself goes down,
all tenants on that host will show stale health data. When detected, this
Lambda restarts the host-agent service via a single SSM command per host.
"""

import os
import boto3
from datetime import datetime, timezone

ddb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

STALE_SECONDS = 120  # No health update for 2 min → agent may be down
RESTART_COOLDOWN_SECONDS = 600  # Don't restart agent more than once per 10 min


def lambda_handler(event, context):
    """Scan running tenants, detect stale health data, recover host-agent if needed."""
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
