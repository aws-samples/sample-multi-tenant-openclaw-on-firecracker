# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
import boto3
from datetime import datetime, timezone

ddb = boto3.resource("dynamodb")
autoscaling = boto3.client("autoscaling")
ssm = boto3.client("ssm")
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"]) if os.environ.get("TENANTS_TABLE") else None

ASG_NAME = os.environ["ASG_NAME"]
IDLE_TIMEOUT = int(os.environ["IDLE_TIMEOUT_MINUTES"])

# Issue #15 — terminal statuses where TTL processing is a no-op
_TTL_TERMINAL = {"stopped", "deleted", "failed"}


def lambda_handler(event, context):
    # Issue #15 — process expired tenants first (cheap, no SSM unless needed)
    _process_ttl_expirations()

    now = datetime.now(timezone.utc)
    hosts = hosts_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", [])

    for h in hosts:
        instance_id = h["instance_id"]
        status = h.get("status")
        vm_count = int(h.get("vm_count", 0))

        if vm_count > 0:
            # Has VMs — ensure active (recover from idle if tenant was assigned)
            if status == "idle":
                _set_status(instance_id, "active")
            continue

        # vm_count == 0
        if status == "active":
            idle_since = h.get("idle_since")
            if not idle_since:
                # First time seeing empty — record timestamp
                _set_idle_since(instance_id, now.isoformat())
            else:
                elapsed = (now - datetime.fromisoformat(idle_since)).total_seconds()
                if elapsed >= IDLE_TIMEOUT * 60:
                    _set_status(instance_id, "idle")
                    print(f"{instance_id}: marked idle (empty for {int(elapsed/60)}m)")

        elif status == "idle":
            # Second round confirmation — terminate if ASG allows
            if _can_scale_in():
                print(f"{instance_id}: terminating idle host")
                try:
                    autoscaling.terminate_instance_in_auto_scaling_group(
                        InstanceId=instance_id,
                        ShouldDecrementDesiredCapacity=True,
                    )
                except Exception as e:
                    print(f"terminate failed: {e}")
            else:
                print(f"{instance_id}: idle but at ASG min, skipping")


def _process_ttl_expirations():
    """Issue #15 — execute on_expiry action for tenants past their expires_at.

    Scans tenants table, finds expired non-terminal tenants, and triggers the
    configured action (stop or delete). Uses tenant.host_id + vm_num to ssm
    stop-vm.sh just like the API handler. Best-effort; errors are logged.
    """
    if tenants_table is None:
        return  # backward compat: if env var missing, no-op
    try:
        items = tenants_table.scan(
            FilterExpression="attribute_exists(expires_at)",
        ).get("Items", [])
    except Exception as e:
        print(f"ttl scan failed: {e}")
        return
    now = datetime.now(timezone.utc)
    for t in items:
        tid = t.get("id", "")
        status = t.get("status", "")
        if status in _TTL_TERMINAL:
            continue
        try:
            expires_at = datetime.fromisoformat(t["expires_at"])
        except Exception:
            continue  # malformed timestamp; skip
        if now < expires_at:
            continue
        action = t.get("on_expiry", "stop")
        host_id = t.get("host_id", "")
        if action == "stop":
            if host_id:
                vm_num = int(t.get("vm_num", 1))
                try:
                    ssm.send_command(
                        InstanceIds=[host_id],
                        DocumentName="AWS-RunShellScript",
                        Parameters={"commands": [
                            f"/home/ubuntu/stop-vm.sh {tid} {vm_num}"
                        ]},
                        TimeoutSeconds=60,
                    )
                except Exception as e:
                    print(f"ttl stop SSM failed for {tid}: {e}")
            _update_tenant_status(tid, "stopped")
            print(f"ttl: stopped {tid} (expired at {t['expires_at']})")
        elif action == "delete":
            _update_tenant_status(tid, "deleted")
            print(f"ttl: deleted {tid} (expired at {t['expires_at']})")


def _update_tenant_status(tenant_id, status):
    try:
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":t": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as e:
        print(f"ttl status update failed for {tenant_id}: {e}")


def _set_status(instance_id, status):
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status},
    )


def _set_idle_since(instance_id, ts):
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET idle_since = :t",
        ExpressionAttributeValues={":t": ts},
    )


def _can_scale_in():
    resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
    asg = resp["AutoScalingGroups"][0]
    return asg["DesiredCapacity"] > asg["MinSize"]
