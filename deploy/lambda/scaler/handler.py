import os
import boto3
from datetime import datetime, timezone

ddb = boto3.resource("dynamodb")
autoscaling = boto3.client("autoscaling")
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

ASG_NAME = os.environ["ASG_NAME"]
IDLE_TIMEOUT = int(os.environ["IDLE_TIMEOUT_MINUTES"])


def lambda_handler(event, context):
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
