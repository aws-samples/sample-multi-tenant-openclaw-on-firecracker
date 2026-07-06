"""core/services 层 · host_service:host 注册/注销/清理 + rootfs 镜像清单/刷新/漂移。

handler-split #132 T1.7 —— 从 handler.py 逐字搬迁,行为零改动。
依赖方向:services → core(clients/utils/legacy_alb),不反向 import handler。
"""
import os
import json

import boto3

from core.clients import (
    CPU_OVERCOMMIT_RATIO,
    MEM_OVERCOMMIT_RATIO,
    HOST_RESERVED_VCPU,
    HOST_RESERVED_MEM,
    asg_client,
    hosts_table,
    tenants_table,
    s3,
    ssm,
)
from core.utils import _now, _resp
from core.legacy_alb import _remove_alb_rule, _remove_host_tg


def list_hosts():
    items = hosts_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", [])
    # Filter out synthetic records (e.g. __az_failover_state__ used by the
    # health_check Lambda to remember per-AZ cooldown — added in 1.3.0).
    # Anything starting with "__" is reserved for internal bookkeeping and
    # must not appear in user-facing host lists.
    items = [h for h in items if not str(h.get("instance_id", "")).startswith("__")]
    for item in items:
        item["cpu_overcommit_ratio"] = CPU_OVERCOMMIT_RATIO
        item["mem_overcommit_ratio"] = MEM_OVERCOMMIT_RATIO
    return _resp(200, items)

# Same _sizes / _mem_ratio fallback as deploy/stack.py (kept in sync
# manually because both are intentionally tiny constant tables — adding
# a shared module just to dedupe two dicts isn't worth the import cost
# in cold-start). When EC2 describe_instance_types() works this table
# is unused; it only triggers if the API call fails.
_SIZE_TO_VCPU = {
    "medium": 1,
    "large": 2,
    "xlarge": 4,
    "2xlarge": 8,
    "4xlarge": 16,
    "8xlarge": 32,
    "12xlarge": 48,
    "16xlarge": 64,
    "24xlarge": 96,
}

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
            print(
                f"register_host: ec2.describe_instance_types({instance_type}) "
                f"failed: {exc}; falling back to static lookup"
            )
    # Fallback: parse e.g. "m8i.xlarge" → family=m, size=xlarge → 4 * 4096 = 16384 MiB
    try:
        family, size = instance_type.split(".")
        vcpu = _SIZE_TO_VCPU[size]
        return vcpu * _FAMILY_LETTER_TO_MEM_PER_VCPU[family[0]]
    except (ValueError, KeyError, IndexError):
        # Last-ditch sane default. Logged so the operator notices.
        print(
            f"register_host: unable to parse instance_type={instance_type!r}; "
            f"defaulting mem_total to 16384 MiB. Add the type to "
            f"_SIZE_TO_VCPU or grant ec2:DescribeInstanceTypes."
        )
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

    hosts_table.put_item(
        Item={
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
        }
    )
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

def rootfs_drift():
    """GET /hosts/rootfs-drift — which tenants are NOT on the current rootfs.

    Phase 4: the rolling-upgrade companion to refresh_rootfs + the `rebuild`
    action. refresh_rootfs stages the new image on hosts; `rebuild` adopts it
    per-tenant; this endpoint shows WHO still needs rebuilding (their
    rootfs_version != the manifest's current version), so an operator can drive
    a rolling upgrade to completion instead of guessing. Pure read.
    """
    manifest = _get_manifest()
    current = manifest.get("version", "unknown")
    # Page the tenants table; only non-deleted tenants matter for upgrade drift.
    stale, up_to_date, unknown = [], 0, 0
    scan_kwargs = {
        "FilterExpression": "#s <> :d",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":d": "deleted"},
    }
    start_key = None
    while True:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        out = tenants_table.scan(**scan_kwargs)
        for t in out.get("Items", []):
            v = t.get("rootfs_version", "")
            if not v:
                unknown += 1
            elif v == current:
                up_to_date += 1
            else:
                stale.append(
                    {"id": t["id"], "rootfs_version": v, "host_id": t.get("host_id")}
                )
        start_key = out.get("LastEvaluatedKey")
        if not start_key:
            break
    return _resp(
        200,
        {
            "current_version": current,
            "up_to_date": up_to_date,
            "unknown": unknown,
            "stale_count": len(stale),
            "stale": stale,
        },
    )

def _get_manifest():
    """Read manifest.json from S3, return dict."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("ROOTFS_PREFIX", "rootfs")
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{prefix}/manifest.json")
        return json.loads(obj["Body"].read().decode())
    except Exception:
        return {}

def list_images(query_params=None):
    """GET /images — list golden-image artifacts in S3 + the live manifest (10h
    -goal #19: 查看黄金镜像内容). Read-only: enumerates the rootfs prefix (rootfs
    / data-template / kernel / golden-image.sha256 per version) with size + last
    modified, and reports which version manifest.json currently points at (the
    one new hosts boot). Does NOT download/expose image bytes — just the
    inventory + integrity-baseline presence, so an operator can see what's baked
    and which version is live without SSHing a host."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("ROOTFS_PREFIX", "rootfs")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    manifest = _get_manifest()
    live_version = manifest.get("version", "unknown")
    artifacts = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                name = key.rsplit("/", 1)[-1]
                if not name:
                    continue
                # classify by filename so the UI can group rootfs/data/kernel/hash
                lname = name.lower()
                if "data-template" in lname:
                    kind = "data-template"
                elif "rootfs" in lname or "openclaw-rootfs" in lname:
                    kind = "rootfs"
                elif "vmlinux" in lname or "kernel" in lname:
                    kind = "kernel"
                elif "sha256" in lname:
                    kind = "integrity-baseline"
                elif "manifest" in lname:
                    kind = "manifest"
                else:
                    kind = "other"
                artifacts.append(
                    {
                        "name": name,
                        "kind": kind,
                        "size_bytes": obj.get("Size", 0),
                        "last_modified": obj.get("LastModified").isoformat()
                        if obj.get("LastModified")
                        else None,
                        "is_backup": ".bak" in lname,
                    }
                )
    except Exception as e:
        return _resp(500, {"error": f"list images failed: {e}"})
    artifacts.sort(key=lambda a: (a["kind"], a["name"]))
    return _resp(
        200,
        {
            "live_version": live_version,
            "manifest": manifest,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        },
    )

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
set -eu
ASSETS={assets}
BUCKET={bucket}
PREFIX={prefix}
REGION={region}
ROOTFS_GZ={manifest["rootfs"]}
DATA_GZ={manifest["data_template"]}
IMMUTABLE_GZ={manifest.get("immutable", "")}
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
# Immutable authority disk (identity + ops skills, read-only). MUST be refreshed
# too — new skills + the routing AGENTS.md live ONLY here, so skipping it means a
# rolling rebuild silently ships stale skills. Same .tmp→mv anti-truncation guard.
if [ -n "$IMMUTABLE_GZ" ]; then
  aws s3 cp "s3://$BUCKET/$PREFIX/$IMMUTABLE_GZ" "$ASSETS/immutable.gz" --region "$REGION"
  pigz -dc "$ASSETS/immutable.gz" > "$ASSETS/openclaw-immutable.ext4.tmp"
  [ -s "$ASSETS/openclaw-immutable.ext4.tmp" ]
  mv "$ASSETS/openclaw-immutable.ext4.tmp" "$ASSETS/openclaw-immutable.ext4"
  rm -f "$ASSETS/immutable.gz"
fi
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
