import os
import time
import boto3

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
BUCKET = os.environ["ASSETS_BUCKET"]
PREFIX = os.environ.get("BACKUP_PREFIX", "backups")


def lambda_handler(event, context):
    """Triggered by EventBridge schedule or API Gateway (manual backup)."""
    # Manual single-tenant backup via API
    tenant_id = event.get("tenant_id")
    if tenant_id:
        item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
        if not item or item.get("status") != "running":
            return {"error": "tenant not running"}
        return backup_tenant(item)

    # Scheduled: backup all running tenants
    tenants = tenants_table.scan(
        FilterExpression="#s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "running"},
    ).get("Items", [])

    results = []
    for t in tenants:
        results.append(backup_tenant(t))
    return results


def backup_tenant(tenant):
    tid = tenant["id"]
    host_id = tenant["host_id"]
    now = _now()

    cmd = f"/home/ubuntu/backup-data.sh {tid} {BUCKET} {PREFIX}"
    success, output = _ssm_run(host_id, cmd, timeout=300)

    result = {"tenant_id": tid, "success": success, "timestamp": now}
    if success:
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression="SET last_backup_at = :t",
            ExpressionAttributeValues={":t": now},
        )
    else:
        result["error"] = output
        print(f"Backup failed for {tid}: {output}")

    return result


def _ssm_run(instance_id, command, timeout=300):
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=timeout,
        )
        cmd_id = resp["Command"]["CommandId"]
        time.sleep(5)
        for _ in range(timeout // 3):
            result = ssm.get_command_invocation(
                CommandId=cmd_id, InstanceId=instance_id,
            )
            if result["Status"] == "Success":
                return True, result.get("StandardOutputContent", "")
            if result["Status"] in ("Failed", "TimedOut", "Cancelled"):
                return False, result.get("StandardErrorContent", "")
            time.sleep(3)
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
