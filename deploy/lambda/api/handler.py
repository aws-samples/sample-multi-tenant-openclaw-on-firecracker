import json
import hashlib
import os
import time
import boto3

ssm = boto3.client("ssm")
asg_client = boto3.client("autoscaling")
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

# Per-host limits (from config.yml via env)
HOST_RESERVED_VCPU = int(os.environ.get("HOST_RESERVED_VCPU", 1))
HOST_RESERVED_MEM = int(os.environ.get("HOST_RESERVED_MEM", 2048))
VM_DEFAULT_VCPU = int(os.environ.get("VM_DEFAULT_VCPU", 2))
VM_DEFAULT_MEM = int(os.environ.get("VM_DEFAULT_MEM", 4096))
VM_DATA_DISK_MB = int(os.environ.get("VM_DATA_DISK_MB", 2048))
VM_PORT_BASE = int(os.environ.get("VM_PORT_BASE", 18789))
VM_SUBNET_PREFIX = os.environ.get("VM_SUBNET_PREFIX", "172.16")
ASG_NAME = os.environ.get("ASG_NAME", "openclaw-hosts-asg")


def lambda_handler(event, context):
    # EventBridge: new host InService → process pending tenants
    if event.get("source") == "aws.autoscaling":
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
        ("GET", "/hosts"): list_hosts,
        ("POST", "/hosts"): lambda: register_host(json.loads(event["body"])),
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
    _ssm_run(item["host_id"], f"~/stop-vm.sh {tenant_id} {vm_num}")

    # Remove DNAT rule (best effort)
    _ssm_run(item["host_id"],
        f"sudo iptables -t nat -D PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) -p tcp --dport {item['host_port']} -j DNAT --to-destination {item['guest_ip']}:{VM_PORT_BASE} 2>/dev/null || true"
    )

    if not keep_data:
        _ssm_run(item["host_id"],
            f"rm -f ~/firecracker-assets/{tenant_id}-data.ext4"
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
        _ssm_run(item["host_id"], f"~/stop-vm.sh {tenant_id} {vm_num} && sleep 2 && ~/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}")
        new_status = "running"
    elif action == "stop":
        vm_num = int(item.get("vm_num", 1))
        _ssm_run(item["host_id"], f"~/stop-vm.sh {tenant_id} {vm_num}")
        new_status = "stopped"
    elif action == "start":
        vm_num = int(item.get("vm_num", 1))
        _ssm_run(item["host_id"], f"~/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}")
        new_status = "running"
    else:
        return _resp(400, {"error": f"unknown action: {action}"})

    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": new_status, ":t": _now()},
    )
    return _resp(200, {"id": tenant_id, "status": new_status})


# ========== Host Operations ==========


def list_hosts():
    return _resp(200, hosts_table.scan().get("Items", []))


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
    return _resp(200, {"instance_id": instance_id, "status": "draining"})


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
            UpdateExpression="SET #s = :s, host_id = :h, vm_num = :n, guest_ip = :g, host_port = :p, updated_at = :t",
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
        free_vcpu = int(h["total_vcpu"]) - int(h["used_vcpu"])
        free_mem = int(h["total_mem_mb"]) - int(h["used_mem_mb"])
        if free_vcpu >= vcpu_needed and free_mem >= mem_needed:
            return h
    return None


def _gen_id(name):
    """Generate tenant id: name-xxxx (4 char hash)."""
    raw = f"{name}{time.time()}"
    short = hashlib.sha256(raw.encode()).hexdigest()[:4]
    return f"{name}-{short}"


def _launch_vm(instance_id, tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port):
    """Fire-and-forget: launch VM + set up DNAT."""
    cmd = (f"~/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} && "
           f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
           f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}")
    _ssm_send(instance_id, cmd)


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
    except Exception:
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
