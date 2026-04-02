"""Health check Lambda — watchdog for host-agent.
Runs every 5 minutes. Detects stale agent data and alerts.
Normal health updates are done by host-agent directly to DynamoDB.
Auto-restart / auto-recovery to be added when alerting is in place.
"""

import os
import boto3
from datetime import datetime, timezone

ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])

STALE_SECONDS = 120  # If no health update for 2 min, agent may be down


def lambda_handler(event, context):
    """Scan running tenants, detect stale health data, mark accordingly."""
    tenants = tenants_table.scan(
        FilterExpression="#s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "running"},
    ).get("Items", [])

    now = datetime.now(timezone.utc)
    stale_count = 0

    for tenant in tenants:
        tid = tenant["id"]
        last_check = tenant.get("last_health_check", "")

        if last_check:
            try:
                elapsed = (now - datetime.fromisoformat(last_check)).total_seconds()
                if elapsed < STALE_SECONDS:
                    continue  # Agent is alive and reporting
            except Exception:
                pass

        # Stale or missing health data — agent may be down
        stale_count += 1
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression="SET vm_health = :vh, app_health = :ah",
            ExpressionAttributeValues={":vh": "stale", ":ah": "unknown"},
        )
        print(f"stale: {tid} (last_check={last_check})")

    if stale_count:
        print(f"watchdog: {stale_count} tenant(s) with stale health data")
        # TODO: SNS alert when stale_count > 0
