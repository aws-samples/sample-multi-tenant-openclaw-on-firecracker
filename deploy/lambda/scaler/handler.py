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

# Issue #11 — day name → Python weekday() index
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _now_utc():
    """Indirection so tests can patch the clock."""
    return datetime.now(timezone.utc)


def lambda_handler(event, context):
    # Issue #11 — reconcile scheduled tenants (start/stop by office hours)
    _process_schedules()

    now = _now_utc()
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


def _process_schedules():
    """Issue #11 — start/stop scheduled tenants based on current time vs schedule."""
    if tenants_table is None:
        return
    try:
        items = tenants_table.scan(
            FilterExpression="attribute_exists(schedule)",
        ).get("Items", [])
    except Exception as e:
        print(f"schedule scan failed: {e}")
        return
    now = _now_utc()
    for t in items:
        if t.get("status") in {"deleted", "creating", "pending", "failed"}:
            continue
        sched = t.get("schedule")
        try:
            should_run = _schedule_should_run(sched, now)
        except Exception as e:
            print(f"schedule eval failed for {t.get('id')}: {e}")
            continue
        status = t.get("status", "")
        tid = t.get("id", "")
        if should_run and status == "stopped":
            _start_tenant(t)
            _set_tenant_status(tid, "running")
            print(f"schedule: started {tid}")
        elif not should_run and status == "running":
            _stop_tenant(t)
            _set_tenant_status(tid, "stopped")
            print(f"schedule: stopped {tid}")


def _schedule_should_run(sched, now_utc):
    """Return True iff `now_utc` falls inside the schedule window [start, stop)
    on a configured day, evaluated in the schedule's timezone.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(sched.get("timezone", "UTC"))
    local = now_utc.astimezone(tz)
    day_name = _DAYS[local.weekday()]
    if day_name not in (sched.get("days") or _DAYS):
        return False
    start_h, start_m = sched["start"].split(":")
    stop_h, stop_m = sched["stop"].split(":")
    minutes_now = local.hour * 60 + local.minute
    minutes_start = int(start_h) * 60 + int(start_m)
    minutes_stop = int(stop_h) * 60 + int(stop_m)
    if minutes_start < minutes_stop:
        return minutes_start <= minutes_now < minutes_stop
    # Overnight window (start > stop): in window if before stop OR after start.
    return minutes_now >= minutes_start or minutes_now < minutes_stop


def _start_tenant(t):
    host_id = t.get("host_id")
    if not host_id:
        return
    cmd = (f"/home/ubuntu/launch-vm.sh {t['id']} {int(t.get('vm_num', 1))} "
           f"{int(t.get('vcpu', 2))} {int(t.get('mem_mb', 4096))}")
    try:
        ssm.send_command(
            InstanceIds=[host_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [cmd]},
            TimeoutSeconds=120,
        )
    except Exception as e:
        print(f"schedule start SSM failed for {t.get('id')}: {e}")


def _stop_tenant(t):
    host_id = t.get("host_id")
    if not host_id:
        return
    cmd = f"/home/ubuntu/stop-vm.sh {t['id']} {int(t.get('vm_num', 1))}"
    try:
        ssm.send_command(
            InstanceIds=[host_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [cmd]},
            TimeoutSeconds=60,
        )
    except Exception as e:
        print(f"schedule stop SSM failed for {t.get('id')}: {e}")


def _set_tenant_status(tenant_id, status):
    try:
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":t": _now_utc().isoformat()},
        )
    except Exception as e:
        print(f"schedule status update failed for {tenant_id}: {e}")


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
