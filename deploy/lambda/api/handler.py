import json
import hashlib
import os
import time
import boto3

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
asg_client = boto3.client("autoscaling")
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

# Per-host limits (from config.yml via env)
HOST_RESERVED_VCPU = int(os.environ.get("HOST_RESERVED_VCPU", 1))
HOST_RESERVED_MEM = int(os.environ.get("HOST_RESERVED_MEM", 2048))
CPU_OVERCOMMIT_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", 1.0))
VM_DEFAULT_VCPU = int(os.environ.get("VM_DEFAULT_VCPU", 2))
VM_DEFAULT_MEM = int(os.environ.get("VM_DEFAULT_MEM", 4096))
VM_DATA_DISK_MB = int(os.environ.get("VM_DATA_DISK_MB", 2048))
VM_PORT_BASE = int(os.environ.get("VM_PORT_BASE", 18789))
VM_SUBNET_PREFIX = os.environ.get("VM_SUBNET_PREFIX", "172.16")
ASG_NAME = os.environ.get("ASG_NAME", "openclaw-hosts-asg")
ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")
VPC_ID = os.environ.get("VPC_ID", "")
elbv2 = boto3.client("elbv2")


def lambda_handler(event, context):
    # EventBridge: new host InService → process pending tenants
    if event.get("source") == "aws.autoscaling":
        detail_type = event.get("detail-type", "")
        if "terminate" in detail_type.lower():
            return cleanup_terminated_host(event)
        return process_pending()

    method = event["httpMethod"]
    resource = event["resource"]
    path_params = event.get("pathParameters") or {}

    routes = {
        ("GET", "/tenants"): list_tenants,
        ("POST", "/tenants"): lambda: create_tenant(event.get("body")),
        ("GET", "/tenants/{id}"): lambda: get_tenant(path_params["id"]),
        ("DELETE", "/tenants/{id}"): lambda: delete_tenant(
            path_params["id"], event.get("queryStringParameters") or {}
        ),
        ("POST", "/tenants/{id}/{action}"): lambda: tenant_action(
            path_params["id"], path_params["action"]
        ),
        ("GET", "/tenants/{id}/{action}"): lambda: tenant_get_action(
            path_params["id"], path_params["action"]
        ),
        ("GET", "/hosts"): list_hosts,
        ("POST", "/hosts"): lambda: register_host(json.loads(event["body"])),
        ("POST", "/hosts/refresh-rootfs"): refresh_rootfs,
        ("GET", "/hosts/rootfs-version"): rootfs_version,
        ("GET", "/agentcore/status"): agentcore_status,
        ("DELETE", "/hosts/{instance_id}"): lambda: deregister_host(
            path_params["instance_id"]
        ),
    }

    handler = routes.get((method, resource))
    if not handler:
        return _resp(404, {"error": "not found"})
    try:
        return handler() if callable(handler) else handler
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _resp(500, {"error": str(e)})


# ========== Tenant Operations ==========


def list_tenants():
    items = tenants_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", [])
    return _resp(200, items)


def get_tenant(tenant_id):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})
    return _resp(200, item)


def create_tenant(body=None):
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body

    name = body.get("name", "")
    vcpu = int(body.get("vcpu", VM_DEFAULT_VCPU))
    mem_mb = int(body.get("mem_mb", VM_DEFAULT_MEM))
    tenant_id = _gen_id(name)
    now = _now()

    # Find host with capacity
    host = _find_host(vcpu, mem_mb)
    if not host:
        # No capacity — save as pending and scale out
        tenants_table.put_item(Item={
            "id": tenant_id, "name": name,
            "vcpu": vcpu, "mem_mb": mem_mb,
            "status": "pending",
            "health_failures": 0,
            "created_at": now, "updated_at": now,
        })
        _scale_out()
        return _resp(201, {"id": tenant_id, "status": "pending", "message": "scaling out, VM will be created when host is ready"})

    # Allocate vm_num from host
    vm_num = int(host.get("next_vm_num", 1))
    guest_ip = f"{VM_SUBNET_PREFIX}.{vm_num}.2"
    host_port = VM_PORT_BASE + vm_num - 1

    tenants_table.put_item(Item={
        "id": tenant_id,
        "name": name,
        "host_id": host["instance_id"],
        "vm_num": vm_num,
        "guest_ip": guest_ip,
        "host_port": host_port,
        "vcpu": vcpu,
        "mem_mb": mem_mb,
        "status": "creating",
        "health_failures": 0,
        "rootfs_version": host.get("rootfs_version", ""),
        "creation_started_at": now,
        "created_at": now,
        "updated_at": now,
    })

    hosts_table.update_item(
        Key={"instance_id": host["instance_id"]},
        UpdateExpression="SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, vm_count = vm_count + :one, next_vm_num = :next, #s = :a REMOVE idle_since",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1, ":next": vm_num + 1, ":a": "active"},
    )

    _launch_vm(host["instance_id"], tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port)

    # ALB path-based routing
    tg_arn = _ensure_host_tg(host["instance_id"], host["private_ip"])
    _add_alb_rule(tenant_id, tg_arn)

    return _resp(201, {
        "id": tenant_id, "host_id": host["instance_id"],
        "guest_ip": guest_ip, "host_port": host_port, "status": "creating",
    })


def delete_tenant(tenant_id, query_params):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})

    keep_data = query_params.get("keep_data", "true").lower() == "true"

    # Stop VM via SSM
    vm_num = int(item.get("vm_num", 1))
    _ssm_run(item["host_id"], f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}")

    # Remove ALB rule
    _remove_alb_rule(tenant_id)

    # Remove DNAT rule (best effort)
    _ssm_run(item["host_id"],
        f"sudo iptables -t nat -D PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) -p tcp --dport {item['host_port']} -j DNAT --to-destination {item['guest_ip']}:{VM_PORT_BASE} 2>/dev/null || true"
    )

    if not keep_data:
        _ssm_run(item["host_id"],
            f"rm -rf /data/firecracker-vms/{tenant_id}"
        )

    # Update host counters
    host_resp = hosts_table.update_item(
        Key={"instance_id": item["host_id"]},
        UpdateExpression="SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
        ExpressionAttributeValues={
            ":v": item["vcpu"], ":m": item["mem_mb"], ":one": 1,
        },
        ReturnValues="ALL_NEW",
    )
    # Record idle_since when host becomes empty
    if int(host_resp["Attributes"].get("vm_count", 0)) == 0:
        hosts_table.update_item(
            Key={"instance_id": item["host_id"]},
            UpdateExpression="SET idle_since = :t",
            ExpressionAttributeValues={":t": _now()},
        )

    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "deleted", ":t": _now()},
    )
    return _resp(200, {"id": tenant_id, "status": "deleted"})


def tenant_action(tenant_id, action):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})

    if action == "restart":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        launch_cmd = f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}"
        # Re-add DNAT after restart
        dnat_cmd = (
            f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
            f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
        ) if guest_ip and host_port else ""
        full_cmd = f"{stop_cmd} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        _ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "stop":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        # Remove DNAT rule
        dnat_del = (
            f"sudo iptables -t nat -D PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
            f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE} 2>/dev/null || true"
        ) if guest_ip and host_port else ""
        full_cmd = stop_cmd
        if dnat_del:
            full_cmd += f" && {dnat_del}"
        _ssm_run(item["host_id"], full_cmd)
        new_status = "stopped"
    elif action == "start":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        launch_cmd = f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}"
        dnat_cmd = (
            f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
            f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
        ) if guest_ip and host_port else ""
        full_cmd = launch_cmd
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        _ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "reset":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        # Stop, delete rootfs (force fresh copy), then launch
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        reset_cmd = f"rm -f /data/firecracker-vms/{tenant_id}/rootfs.ext4"
        launch_cmd = f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}"
        dnat_cmd = (
            f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
            f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
        ) if guest_ip and host_port else ""
        full_cmd = f"{stop_cmd} && {reset_cmd} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        _ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "pause":
        vm_dir = f"/data/firecracker-vms/{tenant_id}"
        _ssm_run(item["host_id"],
            f'curl -s --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/vm '
            f'-H "Content-Type: application/json" -d \'{{"state":"Paused"}}\'')
        new_status = "paused"
    elif action == "resume":
        vm_dir = f"/data/firecracker-vms/{tenant_id}"
        _ssm_run(item["host_id"],
            f'curl -s --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/vm '
            f'-H "Content-Type: application/json" -d \'{{"state":"Resumed"}}\'')
        new_status = "running"
    elif action == "backup":
        # Async invoke Backup Lambda with single tenant
        lambda_client = boto3.client("lambda")
        lambda_client.invoke(
            FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
            InvocationType="Event",  # async, returns immediately
            Payload=json.dumps({"tenant_id": tenant_id}).encode(),
        )
        return _resp(202, {"id": tenant_id, "action": "backup", "status": "started"})
    else:
        return _resp(400, {"error": f"unknown action: {action}"})

    update_expr = "SET #s = :s, updated_at = :t"
    expr_values = {":s": new_status, ":t": _now()}
    if action == "reset":
        host = hosts_table.get_item(Key={"instance_id": item["host_id"]}).get("Item", {})
        update_expr += ", rootfs_version = :rv"
        expr_values[":rv"] = host.get("rootfs_version", "")

    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=expr_values,
    )
    return _resp(200, {"id": tenant_id, "status": new_status})


def list_backups(tenant_id):
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{tenant_id}/")
    backups = []
    for obj in sorted(resp.get("Contents", []), key=lambda o: o["Key"], reverse=True):
        name = obj["Key"].rsplit("/", 1)[-1]
        backups.append({
            "key": obj["Key"],
            "timestamp": name.replace(".gz", ""),
            "size_mb": round(obj["Size"] / 1048576, 1),
        })
    return _resp(200, {"tenant_id": tenant_id, "backups": backups})


def tenant_get_action(tenant_id, action):
    if action == "backups":
        return list_backups(tenant_id)
    return _resp(400, {"error": f"unknown GET action: {action}"})


# ========== Host Operations ==========


def list_hosts():
    items = hosts_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", [])
    for item in items:
        item["cpu_overcommit_ratio"] = CPU_OVERCOMMIT_RATIO
    return _resp(200, items)


def register_host(body):
    instance_id = body["instance_id"]

    # Fetch instance info
    ec2 = boto3.client("ec2")
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    private_ip = inst["PrivateIpAddress"]
    # m8i.xlarge = 4 vCPU / 16384 MB
    vcpu_total = inst["CpuOptions"]["CoreCount"] * inst["CpuOptions"]["ThreadsPerCore"]
    # Approximate memory from instance type (API doesn't return RAM directly)
    mem_total = 16384  # TODO: lookup from instance type

    hosts_table.put_item(Item={
        "instance_id": instance_id,
        "private_ip": private_ip,
        "total_vcpu": vcpu_total - HOST_RESERVED_VCPU,
        "total_mem_mb": mem_total - HOST_RESERVED_MEM,
        "used_vcpu": 0,
        "used_mem_mb": 0,
        "vm_count": 0,
        "next_vm_num": 1,
        "status": "active",
        "idle_since": _now(),
    })
    return _resp(201, {"instance_id": instance_id, "status": "active"})


def deregister_host(instance_id):
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "draining"},
    )
    # Terminate via ASG API to trigger termination lifecycle hook
    try:
        asg_client.terminate_instance_in_auto_scaling_group(
            InstanceId=instance_id,
            ShouldDecrementDesiredCapacity=False,
        )
    except Exception as e:
        print(f"Failed to terminate {instance_id}: {e}")
    return _resp(200, {"instance_id": instance_id, "status": "draining"})


def cleanup_terminated_host(event):
    """Called by termination lifecycle hook — cleanup DynamoDB then complete hook."""
    detail = event["detail"]
    instance_id = detail["EC2InstanceId"]
    print(f"cleanup_terminated_host: {instance_id}")

    # Delete all tenants on this host
    tenants = tenants_table.scan(
        FilterExpression="host_id = :h AND #s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":h": instance_id, ":d": "deleted"},
    ).get("Items", [])
    for t in tenants:
        _remove_alb_rule(t["id"])
        tenants_table.update_item(
            Key={"id": t["id"]},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "deleted", ":t": _now()},
        )

    # Remove host target group
    _remove_host_tg(instance_id)

    # Delete host
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "deleted", ":t": _now()},
    )
    print(f"cleaned up host {instance_id}, {len(tenants)} tenants deleted")

    # Complete lifecycle hook
    try:
        asg_client.complete_lifecycle_action(
            LifecycleHookName=detail["LifecycleHookName"],
            AutoScalingGroupName=detail["AutoScalingGroupName"],
            LifecycleActionResult="CONTINUE",
            InstanceId=instance_id,
        )
    except Exception as e:
        print(f"complete_lifecycle_action failed: {e}")


def rootfs_version():
    manifest = _get_manifest()
    return _resp(200, {"version": manifest.get("version", "unknown")})


def agentcore_status():
    enabled = os.environ.get("AGENTCORE_ENABLED", "false") == "true"
    gateway_url = os.environ.get("AGENTCORE_GATEWAY_URL", "")
    return _resp(200, {
        "enabled": enabled,
        "gateway_url": gateway_url if enabled else None,
    })


def _get_manifest():
    """Read manifest.json from S3, return dict."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("ROOTFS_PREFIX", "rootfs")
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{prefix}/manifest.json")
        return json.loads(obj["Body"].read().decode())
    except Exception:
        return {}


def refresh_rootfs():
    """Download rootfs + data template per manifest.json to all active/idle hosts."""
    manifest = _get_manifest()
    if not manifest:
        return _resp(500, {"error": "manifest.json not found"})

    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("ROOTFS_PREFIX", "rootfs")
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    version = manifest["version"]

    hosts = hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
    ).get("Items", [])

    if not hosts:
        return _resp(200, {"message": "no active hosts", "updated": 0})

    ids = [h["instance_id"] for h in hosts]
    assets = "/data/firecracker-assets"
    cmds = [
        f"aws s3 cp s3://{bucket}/{prefix}/manifest.json {assets}/manifest.json --region {region}",
        f"aws s3 cp s3://{bucket}/{prefix}/{manifest['rootfs']} {assets}/rootfs.gz --region {region}",
        f"aws s3 cp s3://{bucket}/{prefix}/{manifest['data_template']} {assets}/data.gz --region {region}",
        f"pigz -dc {assets}/rootfs.gz > {assets}/openclaw-rootfs.ext4 && rm -f {assets}/rootfs.gz",
        f"pigz -dc {assets}/data.gz > {assets}/openclaw-data-template.ext4 && rm -f {assets}/data.gz",
    ]
    try:
        ssm.send_command(
            InstanceIds=ids,
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": cmds, "executionTimeout": ["300"]},
        )
    except Exception as e:
        return _resp(500, {"error": str(e)})

    # Update hosts table with new version
    for host_id in ids:
        hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="SET rootfs_version = :v",
            ExpressionAttributeValues={":v": version},
        )

    return _resp(200, {"message": "refresh started", "version": version, "hosts": ids})


# ========== Pending Tenant Processing ==========


def process_pending():
    """Called when a new host becomes InService. Assign pending tenants to available hosts."""
    pending = tenants_table.scan(
        FilterExpression="#s = :p",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":p": "pending"},
    ).get("Items", [])

    if not pending:
        return {"statusCode": 200, "body": "no pending tenants"}

    pending.sort(key=lambda x: x.get("created_at", ""))

    assigned = 0
    for tenant in pending:
        vcpu = int(tenant["vcpu"])
        mem_mb = int(tenant["mem_mb"])
        host = _find_host(vcpu, mem_mb)
        if not host:
            break

        vm_num = int(host.get("next_vm_num", 1))
        guest_ip = f"{VM_SUBNET_PREFIX}.{vm_num}.2"
        host_port = VM_PORT_BASE + vm_num - 1
        now = _now()

        # Update pending tenant with host assignment
        tenants_table.update_item(
            Key={"id": tenant["id"]},
            UpdateExpression="SET #s = :s, host_id = :h, vm_num = :n, guest_ip = :g, host_port = :p, creation_started_at = :t, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "creating", ":h": host["instance_id"],
                ":n": vm_num, ":g": guest_ip, ":p": host_port, ":t": now,
            },
        )

        hosts_table.update_item(
            Key={"instance_id": host["instance_id"]},
            UpdateExpression="SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, vm_count = vm_count + :one, next_vm_num = :next",
            ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1, ":next": vm_num + 1},
        )

        _launch_vm(host["instance_id"], tenant["id"], vm_num, vcpu, mem_mb, guest_ip, host_port)
        tg_arn = _ensure_host_tg(host["instance_id"], host["private_ip"])
        _add_alb_rule(tenant["id"], tg_arn)
        assigned += 1

    return {"statusCode": 200, "body": f"assigned {assigned}/{len(pending)} pending tenants"}


def _scale_out():
    """Increment ASG desired capacity by 1 (capped at max)."""
    try:
        resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
        group = resp["AutoScalingGroups"][0]
        desired = group["DesiredCapacity"]
        max_size = group["MaxSize"]
        if desired < max_size:
            asg_client.set_desired_capacity(
                AutoScalingGroupName=ASG_NAME,
                DesiredCapacity=desired + 1,
            )
            print(f"ASG scaled out: {desired} → {desired + 1}")
        else:
            print(f"ASG at max capacity ({max_size}), cannot scale out")
    except Exception as e:
        print(f"Scale out error: {e}")


# ========== Helpers ==========


def _find_host(vcpu_needed, mem_needed):
    """Find an active or idle host with enough free resources."""
    hosts = hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
    ).get("Items", [])

    for h in hosts:
        allocatable_vcpu = int(int(h["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
        free_vcpu = allocatable_vcpu - int(h["used_vcpu"])
        free_mem = int(h["total_mem_mb"]) - int(h["used_mem_mb"])
        if free_vcpu >= vcpu_needed and free_mem >= mem_needed:
            return h
    return None


def _gen_id(name):
    """Generate tenant id: name-xxxx (4 char hash)."""
    raw = f"{name}{time.time()}"
    short = hashlib.sha256(raw.encode()).hexdigest()[:4]
    return f"{name}-{short}"


## ── ALB path-based routing ──

def _get_https_listener_arn():
    """Get HTTPS (443) listener ARN, fallback to HTTP listener from env."""
    if not ALB_LISTENER_ARN:
        return ""
    try:
        alb_arn = ALB_LISTENER_ARN.replace(":listener/", ":loadbalancer/").rsplit("/", 1)[0]
        resp = elbv2.describe_listeners(LoadBalancerArn=alb_arn)
        for l in resp["Listeners"]:
            if l["Port"] == 443:
                return l["ListenerArn"]
    except Exception as e:
        print(f"_get_https_listener_arn error: {e}")
    return ALB_LISTENER_ARN


def _ensure_host_tg(instance_id, private_ip):
    """Create or return target group ARN for a host."""
    tg_name = f"oc-{instance_id[-8:]}"
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        return resp["TargetGroups"][0]["TargetGroupArn"]
    except Exception:
        pass
    resp = elbv2.create_target_group(
        Name=tg_name, Protocol="HTTP", Port=80, VpcId=VPC_ID,
        TargetType="ip", HealthCheckPath="/health",
        HealthCheckIntervalSeconds=10, HealthyThresholdCount=2,
    )
    tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
    elbv2.register_targets(TargetGroupArn=tg_arn, Targets=[{"Id": private_ip, "Port": 80}])
    return tg_arn


def _add_alb_rule(tenant_id, tg_arn):
    """Add ALB listener rule for /vm/{tenant_id}*."""
    arn = _get_https_listener_arn()
    if not arn:
        return
    rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
    if any(f"/vm/{tenant_id}" in v for r in rules for c in r.get("Conditions", []) for v in c.get("Values", [])):
        return
    used = {int(r["Priority"]) for r in rules if r["Priority"] != "default"}
    priority = next(i for i in range(1, 500) if i not in used)
    elbv2.create_rule(
        ListenerArn=arn, Priority=priority,
        Conditions=[{"Field": "path-pattern", "Values": [f"/vm/{tenant_id}", f"/vm/{tenant_id}/*"]}],
        Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
    )


def _remove_alb_rule(tenant_id):
    """Remove ALB listener rule for a tenant."""
    arn = _get_https_listener_arn()
    if not arn:
        return
    rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
    for r in rules:
        for c in r.get("Conditions", []):
            if c.get("Field") == "path-pattern" and f"/vm/{tenant_id}" in c.get("Values", []):
                elbv2.delete_rule(RuleArn=r["RuleArn"])
                return


def _remove_host_tg(instance_id):
    """Delete target group for a host."""
    tg_name = f"oc-{instance_id[-8:]}"
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
        arn = _get_https_listener_arn()
        if arn:
            rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
            for r in rules:
                for a in r.get("Actions", []):
                    if a.get("TargetGroupArn") == tg_arn:
                        elbv2.delete_rule(RuleArn=r["RuleArn"])
        elbv2.delete_target_group(TargetGroupArn=tg_arn)
    except Exception:
        pass


def _launch_vm(instance_id, tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port):
    """Fire-and-forget: launch VM + set up DNAT."""
    cmd = (f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} && "
           f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
           f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}")
    _ssm_send(instance_id, cmd, timeout=300)


def _ssm_send(instance_id, command, timeout=120):
    """Fire-and-forget SSM command. Status tracked by health check."""
    try:
        wrapped = f'export HOME=/home/ubuntu && cd /home/ubuntu && {command}'
        ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
    except Exception as e:
        print(f"SSM send error: {e}")


def _ssm_run(instance_id, command, timeout=30):
    """Execute command on host via SSM Run Command. Returns True on success."""
    try:
        # SSM runs as root; set HOME so ~ resolves to /home/ubuntu
        wrapped = f'export HOME=/home/ubuntu && cd /home/ubuntu && {command}'
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        cmd_id = resp["Command"]["CommandId"]
        time.sleep(3)  # Wait for invocation to register
        for _ in range(timeout // 2):
            try:
                result = ssm.get_command_invocation(
                    CommandId=cmd_id, InstanceId=instance_id,
                )
                status = result["Status"]
                if status == "Success":
                    return True
                if status in ("Failed", "TimedOut", "Cancelled"):
                    print(f"SSM failed: {status} - {result.get('StandardErrorContent', '')}")
                    return False
            except ssm.exceptions.InvocationDoesNotExist:
                pass
            time.sleep(2)
        print(f"SSM timeout waiting for command {cmd_id}")
        return False
    except Exception as e:
        print(f"SSM error: {e}")
        return False


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,x-api-key",
        },
        "body": json.dumps(body, default=str),
    }
