# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import hashlib
import os
import re
import time
import boto3

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
asg_client = boto3.client("autoscaling")
sns = boto3.client("sns")
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])
# Issue #17 — optional audit log; absent in legacy deployments
audit_table = ddb.Table(os.environ["AUDIT_TABLE"]) if os.environ.get("AUDIT_TABLE") else None
AUDIT_TTL_DAYS = int(os.environ.get("AUDIT_TTL_DAYS", "90"))

# Issue #13 — optional SNS topic for tenant lifecycle events.
# Empty string disables publishing (no-op).
NOTIFICATIONS_TOPIC_ARN = os.environ.get("NOTIFICATIONS_TOPIC_ARN", "")

# Per-host limits (from config.yml via env)
HOST_RESERVED_VCPU = int(os.environ.get("HOST_RESERVED_VCPU", 1))
HOST_RESERVED_MEM = int(os.environ.get("HOST_RESERVED_MEM", 2048))
CPU_OVERCOMMIT_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", 1.0))
MEM_OVERCOMMIT_RATIO = float(os.environ.get("MEM_OVERCOMMIT_RATIO", 1.0))
VM_DEFAULT_VCPU = int(os.environ.get("VM_DEFAULT_VCPU", 2))
VM_DEFAULT_MEM = int(os.environ.get("VM_DEFAULT_MEM", 4096))
VM_DATA_DISK_MB = int(os.environ.get("VM_DATA_DISK_MB", 2048))
VM_PORT_BASE = int(os.environ.get("VM_PORT_BASE", 18789))
VM_SUBNET_PREFIX = os.environ.get("VM_SUBNET_PREFIX", "172.16")
ASG_NAME = os.environ.get("ASG_NAME", "openclaw-hosts-asg")
ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")
VPC_ID = os.environ.get("VPC_ID", "")
elbv2 = boto3.client("elbv2")

# Issue #16 / #9 — quota ceilings (0 = unlimited; ENABLED=false → no checks)
QUOTAS_ENABLED = os.environ.get("QUOTAS_ENABLED", "false").lower() == "true"
QUOTAS_MAX_VCPU = int(os.environ.get("QUOTAS_MAX_VCPU", "0") or "0")
QUOTAS_MAX_MEM_MB = int(os.environ.get("QUOTAS_MAX_MEM_MB", "0") or "0")
QUOTAS_MAX_DATA_DISK_MB = int(os.environ.get("QUOTAS_MAX_DATA_DISK_MB", "0") or "0")


# ════════════════════════════════════════════════════════════
# RBAC (issue #14)
# ════════════════════════════════════════════════════════════
#
# Cognito User Pool Groups carry the role assignment as a
# `cognito:groups` claim on the id_token. The console attaches the
# token as `Authorization: Bearer …`. We do NOT re-validate the JWT
# signature here — API Gateway's API key check already gated the
# request, and the worst case of a forged claim downgrades the user
# to viewer (least privilege).
#
# Backward compatibility: requests without a Bearer token are
# treated as admin so that existing CLI / curl flows authenticated
# purely via x-api-key continue to work.

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

# Endpoints that read state are open to viewers; everything else
# requires operator+ by default. Admin-only endpoints can be added here.
_VIEWER_OK = {
    ("GET", "/tenants"), ("GET", "/tenants/{id}"),
    ("GET", "/tenants/{id}/{action}"),
    ("GET", "/backups"), ("GET", "/hosts"),
    ("GET", "/hosts/rootfs-version"), ("GET", "/agentcore/status"),
    ("GET", "/agentcore/tools"), ("GET", "/system/info"),
    ("GET", "/audit-log"),
}


def _decode_jwt_payload(token):
    """Decode the JWT payload segment (no signature verification)."""
    import base64
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        # Pad the base64 string to a multiple of 4
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(seg.encode()))
    except Exception:
        return {}


def _get_user_role(event):
    """Return the highest-privilege role for the caller, or 'admin' if no token."""
    headers = event.get("headers") or {}
    # API Gateway lower-cases header names but real-world clients vary.
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return "admin"  # No JWT → API key-only path → full access (back-compat)
    token = auth[len("Bearer "):]
    claims = _decode_jwt_payload(token)
    groups = claims.get("cognito:groups", []) or []
    if isinstance(groups, str):
        groups = [groups]
    # Pick the most privileged known group; unknown groups → viewer (least priv).
    best = None
    for g in groups:
        if g in _ROLE_RANK and (best is None or _ROLE_RANK[g] > _ROLE_RANK[best]):
            best = g
    if best:
        return best
    return "viewer"


def _role_satisfies(actual, required):
    """True iff `actual` has at least the privilege of `required`."""
    return _ROLE_RANK.get(actual, -1) >= _ROLE_RANK.get(required, 99)


def _rbac_check(event, method, resource):
    """Return None if allowed, else a 403 response."""
    role = _get_user_role(event)
    needed = "viewer" if (method, resource) in _VIEWER_OK else "operator"
    if not _role_satisfies(role, needed):
        return _resp(403, {
            "error": "forbidden",
            "rbac": {"role": role, "required": needed},
        })
    return None


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
        ("GET", "/tenants"): lambda: list_tenants(
            event.get("queryStringParameters") or {},
            event.get("multiValueQueryStringParameters") or {},
        ),
        ("POST", "/tenants"): lambda: create_tenant(event.get("body")),
        ("GET", "/tenants/{id}"): lambda: get_tenant(path_params["id"]),
        ("DELETE", "/tenants/{id}"): lambda: delete_tenant(
            path_params["id"], event.get("queryStringParameters") or {}
        ),
        ("POST", "/tenants/{id}/{action}"): lambda: tenant_action(
            path_params["id"], path_params["action"], event.get("body")
        ),
        ("GET", "/tenants/{id}/{action}"): lambda: tenant_get_action(
            path_params["id"], path_params["action"]
        ),
        ("GET", "/backups"): list_all_backups,
        ("POST", "/batch/tenants"): lambda: batch_tenants(event.get("body")),
        ("GET", "/hosts"): list_hosts,
        ("POST", "/hosts"): lambda: register_host(event.get("body")),
        ("POST", "/hosts/refresh-rootfs"): refresh_rootfs,
        ("GET", "/hosts/rootfs-version"): rootfs_version,
        ("GET", "/agentcore/status"): agentcore_status,
        ("GET", "/agentcore/tools"): agentcore_tools,
        ("GET", "/system/info"): system_info,
        ("GET", "/audit-log"): lambda: _list_audit_log(event.get("queryStringParameters") or {}),
        ("DELETE", "/hosts/{instance_id}"): lambda: deregister_host(
            path_params["instance_id"]
        ),
    }

    handler = routes.get((method, resource))
    if not handler:
        return _resp(404, {"error": "not found"})
    # RBAC enforcement — checked AFTER routing so unknown paths still 404.
    forbidden = _rbac_check(event, method, resource)
    if forbidden is not None:
        return forbidden
    try:
        result = handler() if callable(handler) else handler
        # Issue #17 — audit-log mutating operations after they run so the
        # response_status is captured. GET requests skip auditing to avoid
        # noise; the audit-log route itself is read-only.
        if method in ("POST", "PUT", "DELETE"):
            _audit_write(method, resource, path_params, event, result)
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _resp(500, {"error": str(e)})


# ========== Tenant Operations ==========


def list_tenants(query_params=None, multi_query_params=None):
    items = tenants_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", [])
    # Ensure every record exposes a tags field so the console can render it
    for it in items:
        it.setdefault("tags", {})

    # Issue #10 — optional ?tag=key:value filter (AND across multiple)
    tag_filters = _collect_tag_filters(query_params, multi_query_params)
    if tag_filters:
        items = [it for it in items if _matches_all_tags(it, tag_filters)]

    return _resp(200, items)


def get_tenant(tenant_id):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})
    item.setdefault("tags", {})
    return _resp(200, item)


def create_tenant(body=None):
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body

    name = body.get("name", "")
    name_err = _validate_name(name)
    if name_err:
        return _resp(400, {"error": name_err})
    vcpu = int(body.get("vcpu", VM_DEFAULT_VCPU))
    mem_mb = int(body.get("mem_mb", VM_DEFAULT_MEM))
    data_disk_mb = int(body.get("data_disk_mb", VM_DATA_DISK_MB))

    # Issue #9 — quota check (no-op when env vars unset).
    quota_err = _check_quota(vcpu, mem_mb, data_disk_mb)
    if quota_err:
        return _resp(400, {"error": quota_err})

    config_template = body.get("config_template", "")
    restore_from = body.get("restore_from")
    clone_from = body.get("clone_from")

    # Issue #12 — clone_from is mutually exclusive with restore_from
    if clone_from and restore_from:
        return _resp(400, {"error": "clone_from and restore_from are mutually exclusive"})

    # Resolve clone source: must exist + be running. Forces same-host scheduling.
    clone_src = None
    if clone_from:
        clone_src = tenants_table.get_item(Key={"id": clone_from}).get("Item")
        if not clone_src:
            return _resp(404, {"error": f"clone source not found: {clone_from}"})
        if clone_src.get("status") != "running":
            return _resp(400, {
                "error": f"clone source must be running (current: {clone_src.get('status')})"
            })

    # Issue #10 — validate tags up-front (fail fast before any side effects)
    tags_err = _validate_tags(body.get("tags"))
    if tags_err:
        return _resp(400, {"error": tags_err})
    tags = body.get("tags") or {}

    # Issue #15 — optional TTL fields
    ttl_fields, ttl_err = _parse_ttl(body.get("ttl_hours"), body.get("on_expiry"))
    if ttl_err:
        return _resp(400, {"error": ttl_err})

    # Issue #11 — optional `schedule` field; validated then persisted.
    sched, sched_err = _parse_schedule(body.get("schedule"))
    if sched_err:
        return _resp(400, {"error": sched_err})

    restore_backup_key = ""
    if restore_from:
        src_id = restore_from.get("tenant_id")
        if not src_id:
            return _resp(400, {"error": "restore_from.tenant_id required"})
        ts = restore_from.get("timestamp")
        restore_backup_key = _resolve_backup(src_id, ts)
        if not restore_backup_key:
            if ts:
                return _resp(404, {"error": f"backup not found: {src_id}/{ts}"})
            return _resp(404, {"error": f"no backups found for tenant_id={src_id}"})

    tenant_id = _gen_id(name)
    now = _now()

    # Find host with capacity. The scheduler is normally automatic, but
    # operators occasionally need to pin a tenant to a specific host (e.g.
    # to drain a host before terminating it, or to keep two related VMs on
    # the same hardware). Three modes, in priority order:
    #   1. clone_from → must land on the source's host (local `cp` only)
    #   2. preferred_host_id (admin/operator) → land there or fail
    #   3. default → first host with capacity
    preferred_host_id = (body.get("preferred_host_id") or "").strip()
    if clone_src:
        host = _get_specific_host_with_capacity(clone_src["host_id"], vcpu, mem_mb)
        if not host:
            return _resp(400, {
                "error": f"clone source's host {clone_src['host_id']} lacks "
                         f"capacity for clone (vcpu={vcpu}, mem_mb={mem_mb})"
            })
    elif preferred_host_id:
        host = _get_specific_host_with_capacity(preferred_host_id, vcpu, mem_mb)
        if not host:
            # Distinguish "host doesn't exist" from "host full" so the
            # console can render the right message.
            existing = hosts_table.get_item(Key={"instance_id": preferred_host_id}).get("Item")
            if not existing or existing.get("status") in ("deleted", "draining"):
                return _resp(404, {"error": f"preferred_host_id {preferred_host_id} not found or draining"})
            return _resp(400, {
                "error": f"preferred_host_id {preferred_host_id} lacks capacity "
                         f"(vcpu={vcpu}, mem_mb={mem_mb})"
            })
    else:
        host = _find_host(vcpu, mem_mb)
    if not host:
        # No capacity — save as pending and scale out.
        # Persist config_template and restore_backup_key so process_pending() can apply them.
        item = {
            "id": tenant_id, "name": name,
            "vcpu": vcpu, "mem_mb": mem_mb,
            "status": "pending",
            "health_failures": 0,
            "config_template": config_template,
            "restore_backup_key": restore_backup_key,
            "tags": tags,
            "created_at": now, "updated_at": now,
        }
        item.update(ttl_fields)
        if sched:
            item["schedule"] = sched
        tenants_table.put_item(Item=item)
        _scale_out()
        _publish_event("tenant.created", tenant_id, {
            "name": name, "vcpu": vcpu, "mem_mb": mem_mb, "status": "pending",
        })
        return _resp(201, {"id": tenant_id, "status": "pending", "message": "scaling out, VM will be created when host is ready"})

    # Allocate vm_num from host
    vm_num = int(host.get("next_vm_num", 1))
    guest_ip = f"{VM_SUBNET_PREFIX}.{vm_num}.2"
    host_port = VM_PORT_BASE + vm_num - 1

    item = {
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
        "config_template": config_template,
        "restore_backup_key": restore_backup_key,
        "tags": tags,
        "creation_started_at": now,
        "created_at": now,
        "updated_at": now,
    }
    if clone_from:
        item["clone_from"] = clone_from
    # Persist optional TTL fields on the running path too (#48 follow-up).
    item.update(ttl_fields)
    if sched:
        item["schedule"] = sched
    tenants_table.put_item(Item=item)

    hosts_table.update_item(
        Key={"instance_id": host["instance_id"]},
        UpdateExpression="SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, vm_count = vm_count + :one, next_vm_num = :next, #s = :a REMOVE idle_since",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1, ":next": vm_num + 1, ":a": "active"},
    )

    # Issue #12 — for clones, snapshot source disks before launching the new VM.
    # clone-data.sh: pause src → cp --sparse data.ext4 + overlay.ext4 → resume src.
    if clone_src:
        src_vm_num = int(clone_src.get("vm_num", 1))
        clone_cmd = (f"/home/ubuntu/clone-data.sh {clone_from} {src_vm_num} "
                     f"{tenant_id} {vm_num}")
        if not _ssm_run(host["instance_id"], clone_cmd, timeout=180):
            # Roll back: undo the host counter increment + delete tenant row
            hosts_table.update_item(
                Key={"instance_id": host["instance_id"]},
                UpdateExpression="SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
                ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1},
            )
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :s, updated_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "deleted", ":t": _now()},
            )
            return _resp(502, {"error": "clone-data.sh failed; tenant rolled back"})

    _launch_vm(host["instance_id"], tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port, config_template, restore_backup_key)

    # ALB path-based routing
    tg_arn = _ensure_host_tg(host["instance_id"], host["private_ip"])
    _add_alb_rule(tenant_id, tg_arn)

    _publish_event("tenant.created", tenant_id, {
        "name": name, "vcpu": vcpu, "mem_mb": mem_mb,
        "host_id": host["instance_id"], "guest_ip": guest_ip,
    })

    return _resp(201, {
        "id": tenant_id, "host_id": host["instance_id"],
        "guest_ip": guest_ip, "host_port": host_port, "status": "creating",
    })


def delete_tenant(tenant_id, query_params):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})
    if item.get("status") == "deleted":
        return _resp(200, {"id": tenant_id, "status": "deleted"})

    keep_data = query_params.get("keep_data", "true").lower() == "true"
    host_id = item.get("host_id")

    if host_id:
        # Stop VM via SSM
        vm_num = int(item.get("vm_num", 1))
        _ssm_run(host_id, f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}")
        # Remove vm.json so host-agent won't try to recover
        _ssm_run(host_id, f"rm -f /data/firecracker-vms/{tenant_id}/vm.json")

    # Remove ALB rule
    _remove_alb_rule(tenant_id)

    if host_id:
        # Remove DNAT rule (best effort)
        _ssm_run(host_id,
            f"sudo iptables -t nat -D PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) -p tcp --dport {item.get('host_port',0)} -j DNAT --to-destination {item.get('guest_ip','')}:{VM_PORT_BASE} 2>/dev/null || true"
        )

        if not keep_data:
            _ssm_run(host_id, f"rm -rf /data/firecracker-vms/{tenant_id}")

        # Update host counters
        host_resp = hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
            ExpressionAttributeValues={
                ":v": item["vcpu"], ":m": item["mem_mb"], ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
        # Record idle_since when host becomes empty (defensive — mocks may
        # omit Attributes; treat as still-busy and skip).
        attrs = host_resp.get("Attributes") if isinstance(host_resp, dict) else None
        if attrs and int(attrs.get("vm_count", 0)) == 0:
            hosts_table.update_item(
                Key={"instance_id": host_id},
                UpdateExpression="SET idle_since = :t",
                ExpressionAttributeValues={":t": _now()},
            )

    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "deleted", ":t": _now()},
    )
    _publish_event("tenant.deleted", tenant_id, {"keep_data": keep_data})
    return _resp(200, {"id": tenant_id, "status": "deleted"})


def tenant_action(tenant_id, action, body=None):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})

    # ── Issue #16: live VM resize (hot-add vCPU) ──
    if action == "resize":
        return tenant_resize(tenant_id, body)

    # ── Issue #22: resize-disk (offline grow of data.ext4) ──
    if action == "resize-disk":
        try:
            payload = json.loads(body) if isinstance(body, str) else (body or {})
        except Exception:
            payload = {}
        new_size = payload.get("new_size_mb")
        if not isinstance(new_size, int):
            return _resp(400, {"error": "missing or invalid new_size_mb"})
        current = int(item.get("data_disk_mb", VM_DATA_DISK_MB))
        if new_size <= current:
            return _resp(400, {"error": f"new_size_mb must be larger (current {current}MB); shrink not supported"})
        if new_size > 1024 * 1024:
            return _resp(400, {"error": "new_size_mb exceeds 1 TiB ceiling"})
        host_id = item.get("host_id")
        vm_num = int(item.get("vm_num", 1))
        if not host_id:
            return _resp(400, {"error": "tenant has no host (still pending?)"})
        _ssm_send(host_id, f"/home/ubuntu/resize-disk.sh {tenant_id} {vm_num} {new_size}")
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET data_disk_mb = :s, updated_at = :t",
            ExpressionAttributeValues={":s": new_size, ":t": _now()},
        )
        return _resp(202, {
            "id": tenant_id, "status": "resizing",
            "old_size_mb": current, "new_size_mb": new_size,
        })

    if action == "migrate":
        # Live migration via Firecracker snapshot/restore (issue #20).
        # Body shape: {"target_host_id": "i-...."}
        try:
            payload = json.loads(body) if isinstance(body, str) else (body or {})
        except Exception:
            payload = {}
        target_host_id = payload.get("target_host_id")
        if not target_host_id:
            return _resp(400, {"error": "missing target_host_id"})
        source_host_id = item.get("host_id")
        if target_host_id == source_host_id:
            return _resp(400, {"error": "target_host_id must be different from source"})
        target = hosts_table.get_item(Key={"instance_id": target_host_id}).get("Item")
        if not target:
            return _resp(404, {"error": f"target host {target_host_id} not found"})

        vm_num = int(item.get("vm_num", 1))
        bucket = os.environ.get("ASSETS_BUCKET", "")
        snap_prefix = f"migrations/{tenant_id}"

        # 1) Source host: pause + snapshot + upload to S3.
        # The migrate-vm.sh script (deploy/userdata/migrate-vm.sh, ssm_run sees
        # the same path on every host) handles the Firecracker API calls.
        _ssm_send(source_host_id,
                  f"/home/ubuntu/migrate-vm.sh snapshot {tenant_id} {vm_num} "
                  f"s3://{bucket}/{snap_prefix}")

        # 2) Target host: download + restore.
        target_vm_num = int(target.get("next_vm_num", 1))
        _ssm_send(target_host_id,
                  f"/home/ubuntu/migrate-vm.sh restore {tenant_id} {target_vm_num} "
                  f"s3://{bucket}/{snap_prefix}")

        # 3) Update DDB: tenant.host_id flips, source.vm_count--, target.vm_count++.
        now = _now()
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=("SET host_id = :h, vm_num = :n, "
                              "migration_source = :s, updated_at = :t"),
            ExpressionAttributeValues={
                ":h": target_host_id, ":n": target_vm_num,
                ":s": source_host_id, ":t": now,
            },
        )
        return _resp(202, {
            "id": tenant_id, "status": "migrating",
            "source_host_id": source_host_id, "target_host_id": target_host_id,
            "snapshot_uri": f"s3://{bucket}/{snap_prefix}",
        })

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
        # Stop, delete overlay (force fresh layer), then launch
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        reset_cmd = f"rm -f /data/firecracker-vms/{tenant_id}/overlay.ext4"
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
        _publish_event("tenant.backup_started", tenant_id, {})
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
    # Issue #13 — publish lifecycle event for the action.
    # Map action verbs to lifecycle event names so consumers can filter.
    _action_to_event = {
        "stop": "tenant.stopped",
        "start": "tenant.started",
        "restart": "tenant.restarted",
        "pause": "tenant.paused",
        "resume": "tenant.resumed",
        "reset": "tenant.reset",
    }
    event_name = _action_to_event.get(action, f"tenant.{new_status}")
    _publish_event(event_name, tenant_id, {"action": action, "status": new_status})
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


def list_all_backups():
    """List all backups across all tenants, left-joined with tenants table to mark orphans."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")

    # Build tenant_id → (name, exists) map from DDB (include soft-deleted for name resolution)
    tenants = tenants_table.scan().get("Items", [])
    tenant_info = {
        t["id"]: {"name": t.get("name", ""), "exists": t.get("status") != "deleted"}
        for t in tenants
    }

    # Paginate S3 list to avoid missing objects when > 1000 backups exist
    paginator = s3.get_paginator("list_objects_v2")
    backups = []
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            # Expect: {prefix}/{tenant_id}/{timestamp}.gz
            if len(parts) < 3 or not parts[-1].endswith(".gz"):
                continue
            src_tenant_id = parts[-2]
            timestamp = parts[-1][:-3]  # strip ".gz"
            info = tenant_info.get(src_tenant_id, {"name": None, "exists": False})
            backups.append({
                "tenant_id": src_tenant_id,
                "tenant_name": info["name"],
                "tenant_exists": info["exists"],
                "timestamp": timestamp,
                "size_bytes": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            })

    backups.sort(key=lambda b: b["last_modified"], reverse=True)
    return _resp(200, backups)


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
        item["mem_overcommit_ratio"] = MEM_OVERCOMMIT_RATIO
    return _resp(200, items)


# Same _sizes / _mem_ratio fallback as deploy/stack.py (kept in sync
# manually because both are intentionally tiny constant tables — adding
# a shared module just to dedupe two dicts isn't worth the import cost
# in cold-start). When EC2 describe_instance_types() works this table
# is unused; it only triggers if the API call fails.
_SIZE_TO_VCPU = {"medium": 1, "large": 2, "xlarge": 4, "2xlarge": 8,
                 "4xlarge": 16, "8xlarge": 32, "12xlarge": 48,
                 "16xlarge": 64, "24xlarge": 96}
_FAMILY_LETTER_TO_MEM_PER_VCPU = {"c": 2048, "m": 4096, "r": 8192}


def _resolve_instance_memory_mb(ec2_client, instance_type):
    """Return the advertised RAM (MiB) for an EC2 instance type.

    Tries the authoritative AWS API first (describe_instance_types →
    MemoryInfo.SizeInMiB), falling back to a static lookup table when
    the API call fails (permission, throttling, malformed instance_type).
    The fallback keeps register_host() functional in environments that
    haven't granted ec2:DescribeInstanceTypes, but we log loudly so the
    operator notices.
    """
    if instance_type:
        try:
            resp = ec2_client.describe_instance_types(InstanceTypes=[instance_type])
            return int(resp["InstanceTypes"][0]["MemoryInfo"]["SizeInMiB"])
        except Exception as exc:
            print(f"register_host: ec2.describe_instance_types({instance_type}) "
                  f"failed: {exc}; falling back to static lookup")
    # Fallback: parse e.g. "m8i.xlarge" → family=m, size=xlarge → 4 * 4096 = 16384 MiB
    try:
        family, size = instance_type.split(".")
        vcpu = _SIZE_TO_VCPU[size]
        return vcpu * _FAMILY_LETTER_TO_MEM_PER_VCPU[family[0]]
    except (ValueError, KeyError, IndexError):
        # Last-ditch sane default. Logged so the operator notices.
        print(f"register_host: unable to parse instance_type={instance_type!r}; "
              f"defaulting mem_total to 16384 MiB. Add the type to "
              f"_SIZE_TO_VCPU or grant ec2:DescribeInstanceTypes.")
        return 16384



def register_host(body):
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body
    instance_id = body.get("instance_id")
    if not instance_id:
        return _resp(400, {"error": "missing instance_id"})

    # Fetch instance info
    ec2 = boto3.client("ec2")
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    private_ip = inst["PrivateIpAddress"]
    instance_type = inst.get("InstanceType", "")
    # Capture the AZ so the console can group/filter hosts and tenants by AZ
    # without an extra describe_instances call. Falls back to "" rather than
    # failing if Placement is missing (would be unusual but defensive).
    az = (inst.get("Placement") or {}).get("AvailabilityZone", "")
    vcpu_total = inst["CpuOptions"]["CoreCount"] * inst["CpuOptions"]["ThreadsPerCore"]

    # Resolve memory from the instance type via the EC2 API rather than
    # hard-coding 16384 (which silently wrote wrong values for any host
    # larger than xlarge — see register_host TODO removed in 1.2.4).
    # describe_instance_types returns SizeInMiB which IS exactly the
    # advertised RAM; we fall back to a heuristic only if the API errors.
    mem_total = _resolve_instance_memory_mb(ec2, instance_type)

    hosts_table.put_item(Item={
        "instance_id": instance_id,
        "private_ip": private_ip,
        "az": az,
        "total_vcpu": vcpu_total - HOST_RESERVED_VCPU,
        "total_mem_mb": mem_total - HOST_RESERVED_MEM,
        "used_vcpu": 0,
        "used_mem_mb": 0,
        "vm_count": 0,
        "next_vm_num": 1,
        "status": "active",
        "idle_since": _now(),
    })
    return _resp(201, {"instance_id": instance_id, "status": "active", "az": az})


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


# ════════════════════════════════════════════════════════════
# AgentCore tools listing (for console display)
# ════════════════════════════════════════════════════════════
#
# When AgentCore Gateway is enabled, three Lambda-backed MCP tools are
# registered (see deploy/stack.py — tools=hello/system_info/timestamp).
# The console wants to surface this list so operators can see what tools
# their VMs get for free without having to read the CDK code. The list is
# static (defined at deploy time), so the response is hard-coded here
# rather than calling out to bedrock-agentcore at request time — which
# would cost a control-plane API call per page load.
#
# If AgentCore is disabled, we return an empty list with a hint so the
# console can render an "AgentCore not enabled" placeholder.

_AGENTCORE_BUILTIN_TOOLS = [
    {
        "name": "hello",
        "description": "Say hello — test tool for verifying AgentCore Gateway connectivity",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name to greet"}},
        },
    },
    {
        "name": "system_info",
        "description": "Get Lambda runtime system information",
        "input_schema": {"type": "object"},
    },
    {
        "name": "timestamp",
        "description": "Get current UTC timestamp",
        "input_schema": {
            "type": "object",
            "properties": {"format": {"type": "string", "description": "iso or unix"}},
        },
    },
]


def agentcore_tools():
    """GET /agentcore/tools — list MCP tools registered with the Gateway.

    Today this is a static list (the tools are defined declaratively in
    stack.py at deploy time). A future PR can replace this with a live
    `bedrock-agentcore.list_targets()` call when the Gateway grows
    user-defined tools.
    """
    enabled = os.environ.get("AGENTCORE_ENABLED", "false") == "true"
    if not enabled:
        return _resp(200, {"enabled": False, "tools": []})
    return _resp(200, {"enabled": True, "tools": _AGENTCORE_BUILTIN_TOOLS})


# ════════════════════════════════════════════════════════════
# System info — feature flags / config snapshot for the console
# ════════════════════════════════════════════════════════════
#
# The console's Settings tab wants to surface "is multi-AZ on?",
# "is metrics on?", "is WAF on?" etc. without parsing config.yml.
# We expose the relevant env-derived flags here so the UI can render
# accurate state without an out-of-band copy of config.yml.

def system_info():
    """GET /system/info — feature flags + config snapshot for the console.

    Returns the subset of stack config the console needs to render
    Settings → Infrastructure: which optional features are enabled, and
    where to find their associated AWS resources (Grafana URL, SNS topic
    ARN, etc.). Values come from env vars wired in stack.py.
    """
    return _resp(200, {
        "version": os.environ.get("PROJECT_VERSION", "dev"),
        "region": os.environ.get("AWS_REGION", ""),
        "agentcore": {
            "enabled": os.environ.get("AGENTCORE_ENABLED", "false") == "true",
            "gateway_url": os.environ.get("AGENTCORE_GATEWAY_URL", "") or None,
        },
        "metrics": {
            "enabled": bool(os.environ.get("AMP_REMOTE_WRITE_URL")),
            "amp_remote_write_url": os.environ.get("AMP_REMOTE_WRITE_URL", "") or None,
            "grafana_url": os.environ.get("GRAFANA_WORKSPACE_URL", "") or None,
        },
        "multi_az": {
            "enabled": os.environ.get("MULTI_AZ_ENABLED", "false") == "true",
            "az_count": int(os.environ.get("MULTI_AZ_COUNT", "1") or "1"),
        },
        "waf": {"enabled": os.environ.get("WAF_ENABLED", "false") == "true"},
        "cognito": {
            # 1.2.9 fix: was checking COGNITO_USER_POOL_ID which is only
            # populated when console_auth.user_pool_id is *explicitly* set
            # in config.yml. The auto-created pool path leaves that env
            # empty even though Cognito IS deployed and the user is
            # actively logged in via OAuth — read CONSOLE_AUTH_ENABLED
            # (driven by config.yml console_auth.enabled) instead.
            "enabled": os.environ.get("CONSOLE_AUTH_ENABLED", "false") == "true",
            "user_pool_id": os.environ.get("COGNITO_USER_POOL_ID", "") or None,
        },
        "notifications": {
            "enabled": bool(NOTIFICATIONS_TOPIC_ARN),
            "topic_arn": NOTIFICATIONS_TOPIC_ARN or None,
        },
        "quotas": {
            "enabled": QUOTAS_ENABLED,
            "max_vcpu_per_tenant": QUOTAS_MAX_VCPU,
            "max_mem_mb_per_tenant": QUOTAS_MAX_MEM_MB,
            "max_data_disk_mb": QUOTAS_MAX_DATA_DISK_MB,
        },
        "host_config": {
            "cpu_overcommit_ratio": CPU_OVERCOMMIT_RATIO,
            "mem_overcommit_ratio": MEM_OVERCOMMIT_RATIO,
            "vm_default_vcpu": VM_DEFAULT_VCPU,
            "vm_default_mem_mb": VM_DEFAULT_MEM,
        },
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
    # Decompress to .tmp then rename — `pigz -dc src > dst` truncates dst at
    # redirect time, so a mid-pipe failure leaves a 0-byte rootfs that boots
    # silently into a kernel panic (issue surfaced 2026-05-22 on a v3.5 push).
    script = f"""
set -euo pipefail
ASSETS={assets}
BUCKET={bucket}
PREFIX={prefix}
REGION={region}
ROOTFS_GZ={manifest['rootfs']}
DATA_GZ={manifest['data_template']}
aws s3 cp "s3://$BUCKET/$PREFIX/manifest.json" "$ASSETS/manifest.json" --region "$REGION"
aws s3 cp "s3://$BUCKET/$PREFIX/$ROOTFS_GZ" "$ASSETS/rootfs.gz" --region "$REGION"
aws s3 cp "s3://$BUCKET/$PREFIX/$DATA_GZ" "$ASSETS/data.gz" --region "$REGION"
pigz -dc "$ASSETS/rootfs.gz" > "$ASSETS/openclaw-rootfs.ext4.tmp"
[ -s "$ASSETS/openclaw-rootfs.ext4.tmp" ]
mv "$ASSETS/openclaw-rootfs.ext4.tmp" "$ASSETS/openclaw-rootfs.ext4"
rm -f "$ASSETS/rootfs.gz"
pigz -dc "$ASSETS/data.gz" > "$ASSETS/openclaw-data-template.ext4.tmp"
[ -s "$ASSETS/openclaw-data-template.ext4.tmp" ]
mv "$ASSETS/openclaw-data-template.ext4.tmp" "$ASSETS/openclaw-data-template.ext4"
rm -f "$ASSETS/data.gz"
fallocate --dig-holes "$ASSETS/openclaw-data-template.ext4"
""".strip()
    try:
        ssm.send_command(
            InstanceIds=ids,
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script], "executionTimeout": ["600"]},
        )
    except Exception as e:
        return _resp(500, {"error": str(e)})

    # Mark version as in-flight; host-agent confirms after files are on disk.
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
            UpdateExpression="SET #s = :s, host_id = :h, vm_num = :n, guest_ip = :g, host_port = :p, rootfs_version = :rv, creation_started_at = :t, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "creating", ":h": host["instance_id"],
                ":n": vm_num, ":g": guest_ip, ":p": host_port,
                ":rv": host.get("rootfs_version", ""), ":t": now,
            },
        )

        hosts_table.update_item(
            Key={"instance_id": host["instance_id"]},
            UpdateExpression="SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, vm_count = vm_count + :one, next_vm_num = :next",
            ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1, ":next": vm_num + 1},
        )

        _launch_vm(host["instance_id"], tenant["id"], vm_num, vcpu, mem_mb, guest_ip, host_port,
                   tenant.get("config_template", ""), tenant.get("restore_backup_key", ""))
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
        allocatable_mem = int(int(h["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO)
        free_mem = allocatable_mem - int(h["used_mem_mb"])
        if free_vcpu >= vcpu_needed and free_mem >= mem_needed:
            return h
    return None


def _get_specific_host_with_capacity(instance_id, vcpu_needed, mem_needed):
    """Issue #12 — locate a specific host (used for same-host clone) and
    confirm it has capacity. Returns the host item or None."""
    hosts = hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
    ).get("Items", [])
    for h in hosts:
        if h["instance_id"] != instance_id:
            continue
        allocatable_vcpu = int(int(h["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
        free_vcpu = allocatable_vcpu - int(h["used_vcpu"])
        allocatable_mem = int(int(h["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO)
        free_mem = allocatable_mem - int(h["used_mem_mb"])
        if free_vcpu >= vcpu_needed and free_mem >= mem_needed:
            return h
        return None  # found host but no capacity
    return None


def _gen_id(name):
    """Generate tenant id: name-xxxx (4 char hash)."""
    raw = f"{name}{time.time()}"
    short = hashlib.sha256(raw.encode()).hexdigest()[:4]
    return f"{name}-{short}"


def _resolve_backup(src_tenant_id, timestamp=None):
    """Return the S3 key of a backup, or empty string if not found.
    If timestamp is given, look up that exact backup. Otherwise return the most recent.
    """
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{src_tenant_id}/")
    objs = resp.get("Contents", [])
    if not objs:
        return ""
    if timestamp:
        key = f"{prefix}/{src_tenant_id}/{timestamp}.gz"
        return key if any(o["Key"] == key for o in objs) else ""
    # Latest = highest LastModified
    return max(objs, key=lambda o: o["LastModified"])["Key"]


## ── ALB path-based routing ──

def _get_listener_arn():
    """Get ALB listener ARN for path-based routing rules."""
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
    arn = _get_listener_arn()
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
    arn = _get_listener_arn()
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
        arn = _get_listener_arn()
        if arn:
            rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
            for r in rules:
                for a in r.get("Actions", []):
                    if a.get("TargetGroupArn") == tg_arn:
                        elbv2.delete_rule(RuleArn=r["RuleArn"])
        elbv2.delete_target_group(TargetGroupArn=tg_arn)
    except Exception:
        pass


def _launch_vm(instance_id, tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port, config_template="", restore_backup_key=""):
    """Fire-and-forget: launch VM + set up DNAT.
    If restore_backup_key is non-empty, launch-vm.sh will restore data.ext4 from that S3 key instead of using the template.
    """
    # When restore is used but no template, still need a placeholder in arg 5 so positional args align.
    tpl_arg = config_template or '""'
    cmd = (f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} {tpl_arg} {restore_backup_key} && "
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


# ── Tag helpers (issue #10) ──

# Limits chosen to keep DynamoDB items small and avoid colon-conflict with the
# `?tag=k:v` query syntax. AWS resource tags use the same 50/256 model; we cap
# values at 100 chars (more than enough for typical labels) to be conservative.
_TAG_MAX_KEY_LEN = 50
_TAG_MAX_VALUE_LEN = 100
_TAG_MAX_COUNT = 20


_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")


def _validate_name(name):
    """Tenant name: lowercase DNS-label. Drives the tenant id, which gets
    embedded in URLs, Firecracker socket paths, and ALB rule conditions —
    all of which choke on whitespace or special chars."""
    if not isinstance(name, str) or not name:
        return "name is required"
    if len(name) > 32:
        return "name exceeds 32 characters"
    if not _NAME_RE.match(name):
        return ("name must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ "
                "(lowercase letters, digits, hyphens; cannot start/end with hyphen)")
    return None


def _validate_tags(tags):
    """Return None if valid, else an error message string."""
    if tags is None:
        return None  # absent → treated as {}
    if not isinstance(tags, dict):
        return "tags must be an object (key/value map)"
    if len(tags) > _TAG_MAX_COUNT:
        return f"too many tags (max {_TAG_MAX_COUNT})"
    for k, v in tags.items():
        if not isinstance(k, str) or not k:
            return "tag key must be a non-empty string"
        if not isinstance(v, str):
            return f"tag value for '{k}' must be a string"
        if ":" in k:
            return f"tag key '{k}' must not contain ':' (reserved for query syntax)"
        if ":" in v:
            return f"tag value '{v}' must not contain ':' (reserved for query syntax)"
        if len(k) > _TAG_MAX_KEY_LEN:
            return f"tag key '{k}' exceeds {_TAG_MAX_KEY_LEN} characters"
        if len(v) > _TAG_MAX_VALUE_LEN:
            return f"tag value for '{k}' exceeds {_TAG_MAX_VALUE_LEN} characters"
    return None


def _collect_tag_filters(query_params, multi_query_params):
    """Return list of (key, value) pairs from ?tag=k:v occurrences.

    API Gateway delivers repeated query params via multiValueQueryStringParameters.
    For single-value calls only queryStringParameters is populated.
    """
    raw = []
    if multi_query_params and "tag" in multi_query_params:
        raw = list(multi_query_params["tag"] or [])
    elif query_params and "tag" in query_params:
        raw = [query_params["tag"]]
    pairs = []
    for r in raw:
        if not r or ":" not in r:
            # Malformed filter — keep it so it matches nothing (defensive)
            pairs.append((None, None))
            continue
        k, v = r.split(":", 1)
        pairs.append((k, v))
    return pairs


def _matches_all_tags(item, filters):
    """Item must have every (k, v) pair to match (AND semantics)."""
    item_tags = item.get("tags") or {}
    for k, v in filters:
        if k is None or item_tags.get(k) != v:
            return False
    return True



# ═══════════════════════════════════════════════════════════════════════════
# Helpers restored after the v1.0.0-milestone-q2-2026 cross-PR merge.
# Issue #48 tracks the rationale: each helper was added by an early PR but
# lost when later PRs auto-resolved merge conflicts with `-X theirs`.
# Sources noted alongside each block. — fix/post-merge-regression
# ═══════════════════════════════════════════════════════════════════════════

# ----- TTL (#28 / issue #15, original 47158d2) -----
_TTL_MAX_HOURS = 8760  # 1 year
_TTL_VALID_ON_EXPIRY = ("stop", "delete")


def _parse_ttl(ttl_hours_raw, on_expiry_raw):
    """Validate and compute TTL fields.

    Returns (fields_dict, error_message). fields_dict is empty when no TTL is
    requested; otherwise contains {ttl_hours, on_expiry, expires_at}.
    """
    if ttl_hours_raw is None:
        if on_expiry_raw is not None:
            return {}, "on_expiry requires ttl_hours"
        return {}, None
    try:
        if isinstance(ttl_hours_raw, bool):
            raise TypeError
        ttl_hours = int(ttl_hours_raw)
    except (TypeError, ValueError):
        return {}, "ttl_hours must be a positive integer"
    if ttl_hours <= 0:
        return {}, "ttl_hours must be a positive integer"
    if ttl_hours > _TTL_MAX_HOURS:
        return {}, f"ttl_hours must be <= {_TTL_MAX_HOURS} (1 year)"
    on_expiry = on_expiry_raw or "stop"
    if on_expiry not in _TTL_VALID_ON_EXPIRY:
        return {}, (f"on_expiry must be one of {sorted(_TTL_VALID_ON_EXPIRY)}; "
                    f"got {on_expiry!r}")
    from datetime import datetime, timedelta, timezone
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    return {"ttl_hours": ttl_hours, "on_expiry": on_expiry, "expires_at": expires_at}, None


# ----- Schedule (#30 / issue #11, original af9434b). Validation only — the
# scaler's _schedule_should_run lives in deploy/lambda/scaler/handler.py.
_SCHED_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_schedule(raw):
    """Validate a {start, stop, timezone, days} schedule. Returns (dict, err)."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "schedule must be an object"
    start = raw.get("start"); stop = raw.get("stop")
    if not start: return None, "schedule.start required"
    if not stop:  return None, "schedule.stop required"
    import re
    from datetime import datetime as _dt
    if not re.match(r"^\d{2}:\d{2}$", str(start)) or not re.match(r"^\d{2}:\d{2}$", str(stop)):
        return None, "schedule.start/stop must be HH:MM"
    # Strict parse: rejects 08:60 etc.
    try:
        _dt.strptime(start, "%H:%M")
        _dt.strptime(stop, "%H:%M")
    except ValueError:
        return None, "schedule.start/stop must be a valid HH:MM time"
    if start == stop:
        return None, "schedule.start must differ from schedule.stop"
    tz = raw.get("timezone", "UTC")
    try:
        from zoneinfo import ZoneInfo  # noqa
        ZoneInfo(tz)
    except Exception:
        return None, f"unknown timezone: {tz}"
    days = raw.get("days") or list(_SCHED_DAYS)
    if not isinstance(days, list) or any(d not in _SCHED_DAYS for d in days):
        return None, f"schedule.days must be a subset of {list(_SCHED_DAYS)}"
    return {"start": start, "stop": stop, "timezone": tz, "days": days}, None


# ----- Audit log (#32 / issue #17, original 96d7496) -----
# audit_table is defined above (top of module). No re-binding needed here —
# the post-merge regression repair (#48) accidentally re-declared it; the
# top-of-module definition is authoritative.


def _audit_write(method, resource, path_params, event, result):
    """Best-effort audit-log writer; failures must NEVER break the API."""
    if audit_table is None:
        return
    try:
        import uuid, time as _t
        path_params = path_params or {}
        resource_id = path_params.get("id") or path_params.get("instance_id") or ""
        api_key_id = (event.get("requestContext") or {}).get("identity", {}).get("apiKeyId") \
            or (event.get("headers") or {}).get("x-api-key", "")[:32]
        # Auto-prune via DynamoDB TTL: 90-day retention.
        expires_ttl = int(_t.time()) + 90 * 86400
        audit_table.put_item(Item={
            "pk": "audit",
            "id": str(uuid.uuid4()),
            "ts": _now(),
            "operation": f"{method} {resource}",
            "resource_id": resource_id,
            "api_key_id": api_key_id,
            "response_status": result.get("statusCode") if isinstance(result, dict) else None,
            "expires_ttl": expires_ttl,
        })
    except Exception as e:
        print(f"audit_write failed: {e}")


def _list_audit_log(query_params):
    """GET /audit-log — return recent audit entries, newest first.

    Optional query params:
        limit  — int (default 50, max 500)
        since  — ISO-8601 timestamp; only entries >= this are returned
    """
    if audit_table is None:
        return _resp(200, [])
    qp = query_params or {}
    try:
        limit = min(int(qp.get("limit", 50)), 500)
    except (TypeError, ValueError):
        limit = 50
    since = qp.get("since")
    from boto3.dynamodb.conditions import Key
    key_cond = Key("pk").eq("audit")
    if since:
        key_cond = key_cond & Key("ts").gte(since)
    try:
        items = audit_table.query(
            KeyConditionExpression=key_cond,
            ScanIndexForward=False,  # newest first
            Limit=limit,
        ).get("Items", [])
    except Exception as e:
        print(f"audit query failed: {e}")
        items = []
    return _resp(200, items[:limit])


# ----- Quota (#34 / issue #9, original 79000fa) -----
# QUOTAS_ENABLED / QUOTAS_MAX_* are defined at the top of the module
# (default disabled, matches README "enabled: false default — no checks").
# The post-merge regression repair (#48) accidentally re-declared them with
# a different default; that re-declaration has been removed.


def _check_quota(vcpu, mem_mb, data_disk_mb):
    """Return None if within quota, else an error string."""
    if not QUOTAS_ENABLED:
        return None
    if QUOTAS_MAX_VCPU and vcpu > QUOTAS_MAX_VCPU:
        return f"vcpu={vcpu} exceeds quota (max {QUOTAS_MAX_VCPU})"
    if QUOTAS_MAX_MEM_MB and mem_mb > QUOTAS_MAX_MEM_MB:
        return f"mem_mb={mem_mb} exceeds quota (max {QUOTAS_MAX_MEM_MB})"
    if QUOTAS_MAX_DATA_DISK_MB and data_disk_mb > QUOTAS_MAX_DATA_DISK_MB:
        return f"data_disk_mb={data_disk_mb} exceeds quota (max {QUOTAS_MAX_DATA_DISK_MB})"
    return None


# ----- SNS lifecycle notifications (#33 / issue #13, original 1f1bffa) -----
# NOTIFICATIONS_TOPIC_ARN and the sns client are defined at the top of the
# module. The post-merge regression repair (#48) accidentally re-bound them;
# that duplication has been removed.


def _publish_event(event_name, tenant_id, details):
    """Publish a tenant lifecycle event to SNS. No-op when topic not set.

    Best-effort: SNS publish failures are logged but do not break the
    underlying API operation.
    """
    if not NOTIFICATIONS_TOPIC_ARN:
        return
    try:
        msg = {
            "event": event_name,
            "tenant_id": tenant_id,
            "timestamp": _now(),
            "details": details or {},
        }
        sns.publish(
            TopicArn=NOTIFICATIONS_TOPIC_ARN,
            Subject=f"OpenClaw: {event_name} ({tenant_id})",
            Message=json.dumps(msg, default=str),
            MessageAttributes={
                "event": {"DataType": "String", "StringValue": event_name},
                "tenant_id": {"DataType": "String", "StringValue": tenant_id},
            },
        )
    except Exception as e:
        print(f"SNS publish failed (operation succeeded): {e}")


# ----- Batch tenant operations (#29 / issue #23, original d05e107) -----
_BATCH_VALID_ACTIONS = {"stop", "start", "delete", "backup"}
_BATCH_VALID_FILTER_KEYS = {"tag"}
_BATCH_MAX_IDS = 100


def batch_tenants(body=None):
    """POST /batch/tenants — apply one action to many tenants in a single call."""
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body
    action = body.get("action")
    if action not in _BATCH_VALID_ACTIONS:
        return _resp(400, {"error": f"action must be one of {sorted(_BATCH_VALID_ACTIONS)}"})
    ids = body.get("ids")
    flt = body.get("filter")
    if ids is not None and flt is not None:
        return _resp(400, {"error": "specify exactly one of 'ids' or 'filter'"})
    if ids is None and flt is None:
        return _resp(400, {"error": "specify exactly one of 'ids' or 'filter'"})
    if ids is not None:
        if not isinstance(ids, list):
            return _resp(400, {"error": "ids must be an array"})
        if len(ids) > _BATCH_MAX_IDS:
            return _resp(400, {"error": f"too many ids (max {_BATCH_MAX_IDS})"})
        target_ids = list(ids)
    else:
        if not isinstance(flt, dict):
            return _resp(400, {"error": "filter must be an object"})
        unknown = set(flt.keys()) - _BATCH_VALID_FILTER_KEYS
        if unknown:
            return _resp(400, {"error": f"unknown filter key(s): {sorted(unknown)}"})
        target_ids = _resolve_filter(flt)
    succeeded, failed = [], []
    for tid in target_ids:
        try:
            tenant = tenants_table.get_item(Key={"id": tid}).get("Item")
            if not tenant:
                failed.append({"id": tid, "error": "tenant not found"})
                continue
            if action == "delete":
                result = delete_tenant(tid, {})
            else:
                result = tenant_action(tid, action)
            if result.get("statusCode", 500) >= 400:
                err = json.loads(result.get("body", "{}")).get("error", "unknown error")
                failed.append({"id": tid, "error": err})
            else:
                succeeded.append({"id": tid, "action": action})
        except Exception as e:
            failed.append({"id": tid, "error": str(e)})
    return _resp(200, {"succeeded": succeeded, "failed": failed})


def _resolve_filter(flt):
    """Convert filter dict → list of matching tenant ids (excludes soft-deleted)."""
    items = tenants_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", []) or []
    items = [it for it in items if it.get("status") != "deleted"]
    tag_expr = flt.get("tag", "")
    if tag_expr and ":" in tag_expr:
        k, v = tag_expr.split(":", 1)
        items = [it for it in items if (it.get("tags") or {}).get(k) == v]
    elif tag_expr:
        items = []
    return [it["id"] for it in items]


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


# ────────────────────────────────────────────────────────────
# Live VM resize (#35 / issue #16, original b3d48cf)
# ────────────────────────────────────────────────────────────


def tenant_resize(tenant_id, body):
    """POST /tenants/{id}/resize — hot-add vCPU on a running tenant."""
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body
    new_vcpu = body.get("vcpu")
    new_mem = body.get("mem_mb")
    if new_vcpu is None and new_mem is None:
        return _resp(400, {"error": "specify vcpu (memory live-resize not supported)"})
    if new_mem is not None:
        return _resp(400, {
            "error": "memory live-resize is not supported; "
                     "stop the tenant, recreate with new mem_mb, then start"
        })
    try:
        new_vcpu = int(new_vcpu)
    except (TypeError, ValueError):
        return _resp(400, {"error": "vcpu must be an integer"})
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})
    if item.get("status") != "running":
        return _resp(400, {"error": f"tenant must be running (current: {item.get('status')})"})
    current_vcpu = int(item.get("vcpu", 0))
    if new_vcpu <= current_vcpu:
        return _resp(400, {
            "error": f"vcpu must be greater than current ({current_vcpu}); "
                     "Firecracker cannot shrink — restart to decrease"
        })
    quota_err = _check_quota(new_vcpu, int(item.get("mem_mb", 0)),
                              int(item.get("data_disk_mb", 0)))
    if quota_err:
        return _resp(400, {"error": quota_err})
    host_id = item.get("host_id", "")
    if not host_id:
        return _resp(400, {"error": "tenant has no host assigned"})
    host = hosts_table.get_item(Key={"instance_id": host_id}).get("Item")
    if not host:
        return _resp(400, {"error": f"host {host_id} not found"})
    delta = new_vcpu - current_vcpu
    allocatable = int(int(host["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
    free = allocatable - int(host["used_vcpu"])
    if delta > free:
        return _resp(400, {
            "error": f"insufficient host capacity: need {delta} more vCPU, "
                     f"host has {free} free (allocatable={allocatable}, used={host['used_vcpu']})"
        })
    vm_dir = f"/data/firecracker-vms/{tenant_id}"
    cmd = (f'curl -sf --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/machine-config '
           f'-H "Content-Type: application/json" '
           f"-d '{{\"vcpu_count\":{new_vcpu},\"mem_size_mib\":{int(item['mem_mb'])}}}'")
    if not _ssm_run(host_id, cmd, timeout=30):
        return _resp(502, {"error": "Firecracker machine-config PATCH failed; tenant unchanged"})
    now = _now()
    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET vcpu = :v, updated_at = :t",
        ExpressionAttributeValues={":v": new_vcpu, ":t": now},
    )
    hosts_table.update_item(
        Key={"instance_id": host_id},
        UpdateExpression="SET used_vcpu = used_vcpu + :v",
        ExpressionAttributeValues={":v": delta},
    )
    return _resp(200, {
        "id": tenant_id,
        "vcpu": new_vcpu,
        "mem_mb": int(item["mem_mb"]),
        "delta": delta,
    })
