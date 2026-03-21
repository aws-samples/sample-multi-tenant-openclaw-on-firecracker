import os
import time
import boto3

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

MAX_FAILURES = 3
CREATING_GRACE_MINUTES = 10  # Don't touch creating VMs for this long


def lambda_handler(event, context):
    tenants = tenants_table.scan(
        FilterExpression="#s IN (:r, :c)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "running", ":c": "creating"},
    ).get("Items", [])

    for tenant in tenants:
        if tenant.get("status") == "creating":
            check_creating(tenant)
        else:
            check_running(tenant)


def check_creating(tenant):
    """Lightweight check for creating VMs: only promote to running if alive.
    No auto-restart — that would flood SSM while disks are still copying."""
    tid = tenant["id"]
    now = _now()

    # Grace period: don't even ping if recently created (disk copy in progress)
    created = tenant.get("creation_started_at") or tenant.get("created_at", "")
    if created:
        from datetime import datetime, timezone
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(created)).total_seconds()
            if elapsed < CREATING_GRACE_MINUTES * 60:
                return  # Still within grace period, skip entirely
        except Exception:
            pass

    # Past grace period — try a single ping to see if VM is up
    host_id = tenant.get("host_id")
    guest_ip = tenant.get("guest_ip")
    if not host_id or not guest_ip:
        return

    vm_alive = _ssm_run(host_id, f"ping -c 1 -W 2 {guest_ip} > /dev/null 2>&1")
    if vm_alive:
        # Promote to running
        app_alive = _ssm_run(host_id,
            f"curl -sf -o /dev/null --connect-timeout 3 http://{guest_ip}:18789/")
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression="SET #s = :s, vm_health = :vh, app_health = :ah, health_failures = :z, last_health_check = :t, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "running", ":vh": "up",
                ":ah": "up" if app_alive else "down",
                ":z": 0, ":t": now,
            },
        )


def check_running(tenant):
    """Full health check for running VMs with auto-restart on repeated failure."""
    tid = tenant["id"]
    host_id = tenant["host_id"]
    guest_ip = tenant["guest_ip"]
    now = _now()

    vm_alive = _ssm_run(host_id, f"ping -c 1 -W 2 {guest_ip} > /dev/null 2>&1")

    if not vm_alive:
        failures = int(tenant.get("health_failures", 0)) + 1
        update_expr = "SET health_failures = :f, last_health_check = :t, vm_health = :vh, app_health = :ah"
        update_vals = {":f": failures, ":t": now, ":vh": "down", ":ah": "unknown"}

        if failures >= MAX_FAILURES:
            vm_num = int(tenant.get("vm_num", 1))
            _ssm_run(host_id,
                f"export HOME=/home/ubuntu && cd /home/ubuntu && ~/stop-vm.sh {tid} {vm_num} && sleep 2 && ~/launch-vm.sh {tid} {vm_num} {tenant['vcpu']} {tenant['mem_mb']}",
                timeout=60,
            )
            update_vals[":f"] = 0
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=update_vals,
        )
        return

    app_alive = _ssm_run(host_id,
        f"curl -sf -o /dev/null --connect-timeout 3 http://{guest_ip}:18789/")

    tenants_table.update_item(
        Key={"id": tid},
        UpdateExpression="SET health_failures = :z, last_health_check = :t, vm_health = :vh, app_health = :ah",
        ExpressionAttributeValues={
            ":z": 0, ":t": now, ":vh": "up",
            ":ah": "up" if app_alive else "down",
        },
    )


def _ssm_run(instance_id, command, timeout=30):
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=timeout,
        )
        cmd_id = resp["Command"]["CommandId"]
        time.sleep(3)
        for _ in range(timeout):
            try:
                result = ssm.get_command_invocation(
                    CommandId=cmd_id, InstanceId=instance_id,
                )
                if result["Status"] == "Success":
                    return True
                if result["Status"] in ("Failed", "TimedOut", "Cancelled"):
                    return False
            except Exception as e:
                if "InvocationDoesNotExist" in str(e):
                    time.sleep(1)
                    continue
                return False
            time.sleep(1)
        return False
    except Exception as ex:
        print(f"_ssm_run outer error: {ex}")
        return False


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
