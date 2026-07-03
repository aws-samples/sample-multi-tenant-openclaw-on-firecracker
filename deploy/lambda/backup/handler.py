# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
import time
import boto3

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
# Prefer the WORM + CMK backup bucket; fall back to assets bucket only if unset
# (so a half-deployed stack still backs up rather than crashing).
BUCKET = os.environ.get("BACKUP_BUCKET") or os.environ["ASSETS_BUCKET"]
PREFIX = os.environ.get("BACKUP_PREFIX", "backups")
CMK_KEY_ID = os.environ.get("BACKUP_CMK_KEY_ID", "")


def lambda_handler(event, context):
    """Triggered by EventBridge schedule or API Gateway (manual backup)."""
    # Manual single-tenant backup via API
    tenant_id = event.get("tenant_id")
    if tenant_id:
        item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
        if not item or item.get("status") != "running":
            return {"error": "tenant not running"}
        return backup_tenant(item)

    # Scheduled run. PRD 2.6 要求"每用户错峰备份(非开源版写死统一时间)+ 队列限并发,
    # 避免大量用户/机器同刻备份"。实现:EventBridge 高频触发(如每 30min),每次只挑
    #   ① 距上次备份已超过 BACKUP_INTERVAL_HOURS 的租户(到期才备,天然错峰)
    #   ② 本批最多 BACKUP_BATCH_LIMIT 个(限并发,削峰)
    # 这样每用户按自己上次备份时间错峰滚动,而不是全量同刻触发。
    interval_h = int(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
    batch_limit = int(os.environ.get("BACKUP_BATCH_LIMIT", "20"))
    now_dt = _now_dt()
    due = []
    last_evaluated = None
    while True:
        scan_kw = {
            "FilterExpression": "#s = :r",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":r": "running"},
        }
        if last_evaluated:
            scan_kw["ExclusiveStartKey"] = last_evaluated
        page = tenants_table.scan(**scan_kw)
        for t in page.get("Items", []):
            if _backup_due(t, now_dt, interval_h):
                due.append(t)
        last_evaluated = page.get("LastEvaluatedKey")
        if not last_evaluated:
            break
    # 错峰:最久没备份的优先(last_backup_at 升序;从未备份的排最前)
    due.sort(key=lambda t: t.get("last_backup_at") or "")
    batch = due[:batch_limit]
    results = [backup_tenant(t) for t in batch]
    return {
        "due_total": len(due),
        "batched": len(batch),
        "deferred": max(0, len(due) - len(batch)),
        "interval_hours": interval_h,
        "results": results,
    }


def _backup_due(tenant, now_dt, interval_h):
    """到期判断:从未备份 → 立即到期;否则距 last_backup_at 超过 interval_h 才到期。"""
    from datetime import datetime, timezone

    last = tenant.get("last_backup_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True  # 解析不了当作到期,宁可多备不漏备
    return (now_dt - last_dt).total_seconds() >= interval_h * 3600


def _now_dt():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def backup_tenant(tenant):
    tid = tenant["id"]
    host_id = tenant["host_id"]
    now = _now()

    # 4th arg = CMK key id → backup-data.sh does client-side envelope encryption
    # before upload, so even the S3 service / bucket admin never sees plaintext.
    cmd = f"/home/ubuntu/backup-data.sh {tid} {BUCKET} {PREFIX} {CMK_KEY_ID}"
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
                CommandId=cmd_id,
                InstanceId=instance_id,
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
