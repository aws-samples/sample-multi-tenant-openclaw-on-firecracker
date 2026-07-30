"""core/services 层 · host_service:host 注册/注销/清理 + rootfs 镜像清单/刷新/漂移。

handler-split #132 T1.7 —— 从 handler.py 逐字搬迁,行为零改动。
#187 转型:core.legacy_alb 已下线(数据面两级路由不再用 per-tenant ALB rule/TG),
调用点(_remove_alb_rule/_remove_host_tg)在 cleanup_terminated_host 中一并删。
依赖方向:services → core(clients/utils),不反向 import handler。
"""

import os
import json
import re
import shlex
import time
import uuid
import ipaddress

import boto3

from core.clients import (
    CPU_OVERCOMMIT_RATIO,
    MEM_OVERCOMMIT_RATIO,
    HOST_RESERVED_VCPU,
    HOST_RESERVED_MEM,
    asg_client,
    hosts_table,
    tenants_table,
    version_snapshots_table,
    s3,
    ssm,
)
from core.utils import (
    _now,
    _resp,
    _err,
    _parse_limit,
)
from core.pagination import decode_cursor, encode_cursor


def _public_hosts(items):
    # Filter out synthetic records (e.g. __az_failover_state__ used by the
    # health_check Lambda to remember per-AZ cooldown — added in 1.3.0).
    # Anything starting with "__" is reserved for internal bookkeeping and
    # must not appear in user-facing host lists.
    items = [h for h in items if not str(h.get("instance_id", "")).startswith("__")]
    for item in items:
        item["cpu_overcommit_ratio"] = CPU_OVERCOMMIT_RATIO
        item["mem_overcommit_ratio"] = MEM_OVERCOMMIT_RATIO
    return items


def list_hosts(query_params=None):
    query_params = query_params or {}
    parameterized = any(key in query_params for key in ("ip", "limit", "next_token"))
    scan_kwargs = {
        "FilterExpression": "#s <> :d",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":d": "deleted"},
    }

    # Preserve the legacy endpoint byte-for-byte: no parameters means one scan
    # and a bare array, including its existing 1 MB scan-page behavior.
    if not parameterized:
        items = hosts_table.scan(**scan_kwargs).get("Items", [])
        return _resp(200, _public_hosts(items))

    ip = query_params.get("ip")
    if ip is not None:
        try:
            parsed_ip = ipaddress.ip_address(str(ip))
            if parsed_ip.version != 4 or str(parsed_ip) != str(ip):
                raise ValueError
        except ValueError:
            return _err(400, "VALIDATION", "ip must be a canonical IPv4 address")
        scan_kwargs["FilterExpression"] += " AND private_ip = :ip"
        scan_kwargs["ExpressionAttributeValues"][":ip"] = str(ip)

    has_limit = "limit" in query_params or "next_token" in query_params
    if has_limit:
        limit, err = _parse_limit(query_params)
        if err is not None:
            return err
        condition = {"route": "/hosts", "ip": ip}
        try:
            start_key = decode_cursor(query_params.get("next_token"), condition)
        except ValueError as exc:
            return _err(400, "VALIDATION", str(exc))
        scan_kwargs["Limit"] = limit
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        out = hosts_table.scan(**scan_kwargs)
        items = _public_hosts(out.get("Items", []))
        return _resp(
            200,
            {
                "hosts": items,
                "next_token": encode_cursor(
                    out.get("LastEvaluatedKey"), condition
                ),
                "count": len(items),
            },
        )

    # ip-only must scan all bounded host pages so an exact match after the
    # first DynamoDB 1 MB page is not reported as a false negative.
    items = []
    while True:
        out = hosts_table.scan(**scan_kwargs)
        items.extend(out.get("Items", []))
        key = out.get("LastEvaluatedKey")
        if not key:
            break
        scan_kwargs["ExclusiveStartKey"] = key
    items = _public_hosts(items)
    return _resp(
        200,
        {"hosts": items, "next_token": None, "count": len(items)},
    )


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
        return _err(400, "VALIDATION", "missing body")
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
        # #187 转型:legacy_alb rule 已下线,两级路由无需再摘 per-tenant rule。
        tenants_table.update_item(
            Key={"id": t["id"]},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "deleted", ":t": _now()},
        )

    # #187 转型:host target group 也已下线(数据面走 EdgeTargetGroup + host DNAT)。

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


def _select_pull_files(bucket, files):
    """#317 — pull_image 版本选择:快照可能含【同一 kind 多个版本】的盘
    (openclaw-rootfs-v1.0 + -v1.1 都在),旧代码按前缀全留 → 都解压到扁平名
    openclaw-<kind>.ext4,后覆盖先、装哪版非确定。修:manifest.json 是唯一版本选择器,
    只装它点名的 3 个盘 + manifest 本身,忽略其它版本。

    返回 (host_files, err_msg, version)。host_files = manifest 条目 + basename ∈ manifest
    点名集的盘条目;err_msg 非 None 表示选择失败(调用方 fail-loud,不装 live)。
    version = 这份快照 manifest 的 version 字段(#343:pull 成功后写进 host.rootfs_version,
    本函数是读 manifest 的唯一处、故由它返回权威版本;读不到 version 时返 "" 不谎报)。
    读快照那份 manifest(按其精确 s3_version_id),不读 live 的——快照代表的是它自己
    那一刻的版本指针。"""
    rootfs_files = [f for f in files if f["path"].startswith(_HOST_PULL_PREFIXES)]
    manifest_entry = next(
        (f for f in rootfs_files if f["path"].endswith("rootfs/manifest.json")), None
    )
    if manifest_entry is None:
        return [], "snapshot has no manifest.json under deployment/rootfs/", ""
    # 读快照的 manifest(精确 VersionId),拿它点名的 3 个盘文件名。
    # #333(codex round-final)"null" 是【真实 VersionId】,不是"无版本"哨兵:versioning 开启前
    # 上传的对象,其 VersionId 字面就是 "null",要拿【那一版】必须显式传 VersionId="null";省略
    # 会读【最新版】(可能是后来 re-upload 的新版)→ manifest(版本选择器)与盘下载路径(line 885
    # 一律传 "null")读到不同版本,装错版本。故只在【真的没记 version_id】(空/None)时才省略;
    # 记了(含字面 "null")就精确传。与 _verify_lines 的 `vid or "null"` 语义对齐。
    try:
        get_kw = {"Bucket": bucket, "Key": manifest_entry["path"]}
        vid = manifest_entry.get("s3_version_id")
        if vid:  # 含字面 "null"(versioning 前对象的真实版本 id)——精确传,别读成 latest
            get_kw["VersionId"] = vid
        manifest = json.loads(s3.get_object(**get_kw)["Body"].read().decode())
    except Exception as e:
        return [], f"cannot read snapshot manifest.json: {e}", ""
    # #333 — kind 从 manifest 的【字段名】派生(唯一真相源),不再从文件名正则猜:
    # manifest 每个 field(rootfs/data_template/immutable)→ value(该盘的源文件名)。
    # 给选中的盘打 f["disk_kind"],下游装/备份/进度全读它,文件名不再被强制 openclaw-<kind>- 格式。
    # 但文件名会被插进下发到 host 的 shell(日志/_perr),故必须校验安全字符集(防 codex review
    # 的 shell 注入:含 $()/反引号/引号的 S3 key 可在 host 权限下执行命令)+ 非空字符串。
    named = {}  # 源文件名(value)→ 该盘的 kind(连字符)
    for kind, field in _MANIFEST_DISK_FIELD.items():
        val = manifest.get(field)
        if not val:
            continue
        if not isinstance(val, str):
            return [], f"manifest field {field} must be a string, got {type(val).__name__}", ""
        if not _SAFE_DISK_NAME_RE.match(val):
            # 字符集白名单(纵深防御,叠加下游 shell-quote):挡 $()/反引号/引号/空格/斜杠等。
            return [], f"manifest field {field} filename {val!r} has unsafe chars (only [A-Za-z0-9._-])", ""
        if val in named:
            # 多个 field 指向同一文件 → 只会装其中一种盘,其它盘保留旧版却校验通过(codex review)。
            return [], f"manifest points two disk fields at the same file {val!r} (must be distinct)", ""
        named[val] = kind
    if not named:
        return [], "manifest.json names no disks (rootfs/data_template/immutable)", ""
    # 只留 manifest 点名的盘(按 basename 匹配 manifest value);给每个打 disk_kind。
    selected = [manifest_entry]
    picked = set()
    for f in rootfs_files:
        if f is manifest_entry:
            continue
        base = f["path"].rsplit("/", 1)[-1]
        if base in named:
            f["disk_kind"] = named[base]   # #333 权威 kind(manifest 字段派生)
            selected.append(f)
            picked.add(base)
    missing = set(named) - picked
    if missing:
        return [], f"manifest names disks absent from snapshot: {sorted(missing)}", ""
    # #343 — 返回 manifest 的 version(权威:本函数是读 manifest 的唯一处),供 pull 成功后
    # 写进 host.rootfs_version。rootfs_version 语义 = 这台 host 装的 rootfs 盘版本,故:
    #   · manifest 点名了 rootfs 盘 → 本次装了新 rootfs,【必须】有合法非空版本号才写(codex
    #     review:缺失/null/非字符串则装了新 rootfs 却谎报旧版本 = 原 bug,fail-loud 拒装,
    #     与 refresh_rootfs(manifest["version"] 直接 KeyError)/_manifest_consistency_lines 一致)。
    #   · manifest 没点名 rootfs 盘(只 data-template/immutable)→ rootfs 没换,version 返 "",
    #     调用方不动 host.rootfs_version(保留旧值才是如实,不是谎报)。
    mver = manifest.get("version")
    version = mver if isinstance(mver, str) and mver else ""
    rootfs_installed = "rootfs" in named.values()
    if rootfs_installed and not version:
        return [], "manifest names a rootfs disk but has no valid version string", ""
    if not rootfs_installed:
        version = ""  # rootfs 未换 → 不写 host.rootfs_version(旧值即真实)
    return selected, None, version


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

    # #304 — host.rootfs_version = 该 host **已 staged(将服务)**的镜像版本:新建
    # 租户(tenant_service.py:1536)和 rebuild 采用(:2705)都读它。注意这是异步
    # send_command,此刻文件可能还没在盘上换完(set -eu + .tmp→mv 保证要么换成功
    # 要么整段失败,不会半成品;但本函数不等它跑完)。**关键的防谎报在采用侧**:
    # rebuild 分支(#304)relaunch 后校验 FC 未抱 (deleted) 旧 inode 才把该版本标到
    # 租户,校验不过就不标 → 即便这里 host 版本先行,租户级 GET /tenants 也不会谎报
    # "已升级"。原注释宣称"host-agent confirms after files are on disk"是不实的
    # (host-agent 健康检查不验版本),已删除该说法。
    for host_id in ids:
        hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="SET rootfs_version = :v",
            ExpressionAttributeValues={":v": version},
        )

    return _resp(200, {"message": "refresh started", "version": version, "hosts": ids})


# #309 V1 — pull_image 只拉 deployment/rootfs/ 前缀:镜像三盘
# (openclaw-{rootfs,data-template,immutable}-<VER>.ext4.gz)+ manifest.json 版本指针。
# 快照【记录】整个 deployment/(全记不漏),但 pull_image 拉到 host 时【只拉镜像盘那类】。
# deployment/scripts/(host-agent/route_ops/launch-vm 等)由 init-host.sh 开机各自 aws s3 cp
# 独立拉,不经此路径(owner 2026-07-17:pull_image 本次只管系统镜像,脚本不在范围);
# deployment/{edge,litellm,monitoring}/ 是别组件的部署物,同样不灌 microVM host。
_HOST_PULL_PREFIXES = ("deployment/rootfs/",)

# #333 — manifest 点名的盘文件名(value)会被下发到 host 的 shell 引用(即使已 shell-quote,
# 仍加一道字符集白名单做纵深防御:安全域宁可两道)。只允许文件名常见安全字符
# [A-Za-z0-9._-],挡 shell 元字符/路径分隔符;不合格拒绝整个快照(fail-loud,不装 live)。
_SAFE_DISK_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# #217 V2 — snapshot_time 主键格式 = snapshot-version.sh 生成的 ISO8601 UTC
# (YYYY-MM-DDTHH:MM:SSZ)。API 侧先校验格式:非法 → 400(参数错),合法但 DB 无 → 404
# (快照不存在)。别让乱输入默默查 DB 走成 404,误导调用方"快照不存在"(owner 2026-07-14)。
_SNAPSHOT_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# #376 — create_image_snapshot 的 label 是 API 调用方可控输入(shell 脚本从 manifest 读,
# 但 API 直接收任意文本)。label 会进 console 显示 + 日志,故白名单校验:长度 ≤128 + 只允许
# 文件名常见安全字符 [A-Za-z0-9._-],挡 shell 元字符/空格(纵深防御,不裸存未净化的值)。
_SNAPSHOT_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")

# #217 V2 — 镜像盘文件名 → 盘类型(build-rootfs.sh:1134-1136 命名):
# openclaw-{rootfs,data-template,immutable}-<VER>.ext4.gz。snapshot pull 要按此识别
# 出镜像盘,拉下来解压成 launch-vm 认的【扁平】名 openclaw-<kind>.ext4;非镜像(脚本)
# 直接落原路径不解压。
# #309 — manifest.json 里镜像字段名(build-rootfs 产出;live 实测:
# {"version","rootfs","data_template","immutable"})。盘 kind → manifest 字段:
# rootfs→rootfs,data-template→data_template(下划线),immutable→immutable。
_MANIFEST_DISK_FIELD = {
    "rootfs": "rootfs",
    "data-template": "data_template",
    "immutable": "immutable",
}


def _manifest_consistency_lines(host_files):
    """#309 — 装 live 前校验:本次拉的镜像盘 .gz 文件名,必须与暂存 manifest.json 里
    对应字段(rootfs/data_template/immutable)指向的文件名一致。防"快照的盘和它的版本
    指针对不齐"(如 snapshot-version.sh 在 manifest 还是旧版时打了快照)——那样 pull 会
    忠实复现一个 disks 与 version 指针矛盾的坏组合,却没人拦。不一致 → exit1(在
    INSTALLING=0 阶段,trap 复位 status→prev,live 未碰)。用 host 上 python3 读 JSON,
    不 grep(JSON 不能靠 grep 解析)。各值 shell-quote 防注入。"""
    q = shlex.quote
    # 收集本次拉的镜像盘 basename(脚本/manifest 本身不校验)。
    disk_bases = {}  # manifest 字段 → 本次拉的盘 basename
    for f in host_files:
        kind = _disk_kind_of(f)
        if kind is not None:
            disk_bases[_MANIFEST_DISK_FIELD[kind]] = f["path"].rsplit("/", 1)[-1]
    if not disk_bases:  # 无镜像盘(理论上 host_files 至少含盘)→ 无可校验,跳过
        return []
    expected_json = json.dumps(disk_bases)  # {"rootfs":"openclaw-rootfs-v1.0.ext4.gz",...}
    # manifest.json 已在 phase1 拉到暂存 $ARCH;读暂存那份(将要装 live 的那份)校验。
    manifest_staged = '"$ARCH"/manifest.json'
    return [
        '_p "phase1.5: verifying manifest.json version pointer matches pulled disks"',
        f'echo "[pull] phase1.5 manifest consistency check"',
        f"MANIFEST_CHECK=$(python3 - {q(expected_json)} {manifest_staged} <<'PYEOF'\n"
        "import json, sys\n"
        "expected = json.loads(sys.argv[1])\n"
        "try:\n"
        "    m = json.load(open(sys.argv[2]))\n"
        "except Exception as e:\n"
        "    print(f'MANIFEST_UNREADABLE {e}'); sys.exit(0)\n"
        "for field, want in expected.items():\n"
        "    got = m.get(field)\n"
        "    if got != want:\n"
        "        print(f'MISMATCH {field}: manifest={got!r} pulled={want!r}'); sys.exit(0)\n"
        "print('OK')\n"
        "PYEOF\n"
        ")",
        # 不是 OK 就 fail-loud(exit1,trap 在 INSTALLING=0 阶段复位 status→prev)。
        # MANIFEST_MISMATCH — manifest 版本指针与拉下来的盘文件名对不上(快照不自洽)。
        '[ "$MANIFEST_CHECK" = "OK" ] || { '
        'echo "MANIFEST_MISMATCH: $MANIFEST_CHECK" >&2; '
        '_perr "MANIFEST_MISMATCH manifest.json version pointer mismatch — $MANIFEST_CHECK"; '
        'exit 1; }',
    ]


def _disk_kind_of(f):
    """#333 — 读 _select_pull_files 打的 f["disk_kind"](manifest 字段派生的权威 kind:
    'rootfs'/'data-template'/'immutable');非镜像盘(manifest 本身/脚本)无此标签 → None。
    取代旧的文件名正则猜 kind:kind 现在唯一来自 manifest,源文件名彻底解放。纯函数。"""
    return f.get("disk_kind") if isinstance(f, dict) else None


# #217 V2 — live 资产根:launch-vm.sh:205-210 直接 mount 的位置。snapshot pull 校验
# 通过后把镜像装到这里的扁平名 openclaw-<kind>.ext4 → launch-vm 不用改就能起新版。
_LIVE = "/data/firecracker-assets"

# #309 owner 2026-07-20 — pull 暂存/解压区:先把 .gz 下载到这里、就地解压成 .ext4,
# 全部完成后再 mv 到 _LIVE 的置顶位置。就在 _LIVE 目录下(同一 /data/firecracker-assets)
# → mv 铁定同盘原子 rename(瞬间、不占双份空间、sparse 保留)。固定名 target(不按
# snapshot_time 分,每轮覆盖)。
_STAGE = f"{_LIVE}/snapshots/target"


def _script_live_dest(path):
    """<path> → (live_dest, is_service)。init-host.sh 是真相源:
    · deployment/rootfs/manifest.json → /data/firecracker-assets/manifest.json
      (版本指针,init-host.sh:356 装这;pull 新版必须一起更新它,否则镜像换了指针没换)。
    · deployment/scripts/*.py → /opt/openclaw(host-agent 常驻 service);
    · deployment/scripts/adot-config.yaml → (None, False) 模板 envsubst 渲染,只归档不装 live;
    · deployment/scripts/其余(含 lib/*) → /home/ubuntu/<rel>。"""
    # manifest.json 在 rootfs/ 前缀,不是 scripts/;单独映射到 host 真正读的 live 位置
    # (否则 split("deployment/scripts/") 失配 → rel 保留完整原路径 → 装到
    # /home/ubuntu/deployment/rootfs/manifest.json 错误嵌套路径,版本指针不更新)。
    if path.endswith("rootfs/manifest.json"):
        return (f"{_LIVE}/manifest.json", False)
    rel = path.split("deployment/scripts/", 1)[-1]
    if rel == "adot-config.yaml":
        return (None, False)
    if rel.endswith(".py"):
        return (f"/opt/openclaw/{rel}", True)
    return (f"/home/ubuntu/{rel}", False)


def _verify_lines(bucket, region, path, version_id, etag):
    """Phase1 — get-object 按精确 VersionId 拉到 archive $ARCH/<basename>,拿 get 返回的
    ETag 与快照记录的 etag 比对(S3-etag 对 S3-etag,multipart 也通用、零重算)。拉失败 →
    PULL_FAIL;etag 不符 → ETAG_MISMATCH。都 exit1,则 phase2 不执行(不装 live)。各值 quote。"""
    q = shlex.quote
    b, r, k, v = q(bucket), q(region), q(path), q(version_id)
    dst = f'"$ARCH"/{q(path.rsplit("/", 1)[-1])}'
    # #333 防注入(codex review):path/version_id 也走 shell 变量(K/V),不把它们直接插进
    # 双引号 echo/_perr —— 否则含 $()/反引号的 S3 key 在双引号内会被 shell 展开/执行(host 权限)。
    # KEY=... 用 q() 单引号包死,后续引用 "$KEY" 是纯数据。WANT 同理(带引号的 etag)。
    # 失败点各写 ERROR:<CODE>:DOWNLOAD_FAILED(拉不下)/ETAG_MISMATCH(内容不符),exit1 不装 live。
    return [
        f"KEY={q(path)}",
        f"VID={q(version_id)}",
        'echo "[pull] $KEY (version-id=$VID)"',
        f"WANT={q(etag or '')}",
        f"GOT=$(aws s3api get-object --bucket {b} --key {k} --version-id {v} "
        f"--region {r} --query ETag --output text {dst}) "
        f'|| {{ echo "PULL_FAIL key=$KEY version-id=$VID" >&2; '
        f'_perr "DOWNLOAD_FAILED s3 get-object failed key=$KEY version-id=$VID"; exit 1; }}',
        f'[ "$GOT" = "$WANT" ] '
        f'|| {{ echo "ETAG_MISMATCH key=$KEY want=$WANT got=$GOT" >&2; '
        f'_perr "ETAG_MISMATCH key=$KEY want=$WANT got=$GOT"; exit 1; }}',
    ]


# #333 owner 2026-07-21 — phase2 拆成【两个循环】:先全部 _stage_lines(解压/准备到 target
# 暂存区,live 完全不碰),全部 stage 成功后,再第二个循环 _commit_lines(逐个 mv/cp 到 live)。
# 为什么拆:交错"解一个装一个"会在中途失败时留混版 live(前几个新盘 + 后几个旧盘);拆开后,
# 任一盘解压失败 → live 一个字节没动(no-data-loss)。commit 段全是极快的同盘 rename,窗口极小。
# unzip(镜像盘解压)与 copy(脚本/manifest)是两码事,分别在各自 stage/commit 分支里处理,不混。


def _staged_disk_path(kind):
    """镜像盘解压到 target 暂存区的固定路径(去 .gz、扁平成 openclaw-<kind>.ext4)。"""
    return f'"$ARCH"/openclaw-{kind}.ext4'


def _script_staged_path():
    """脚本/manifest 在 target 暂存区的临时落点(commit 前的中转)。用 basename 命名,
    与镜像盘的 openclaw-<kind>.ext4 不撞(脚本 basename 不以 openclaw- 开头且非 .ext4)。"""
    return '"$ARCH"/staged-"$BASE"'


def _stage_lines(f):
    """Phase2 循环①(stage):把文件准备到 target 暂存区,【绝不碰 live】。
    · 镜像盘:pigz 解压 archive .gz → "$ARCH"/openclaw-<kind>.ext4;data-template 挖回 sparse。
    · 脚本/manifest:cp archive 原文件 → "$ARCH"/staged-<base>(仅中转,不设最终权限)。
    · adot-config.yaml(dest=None):只归档不装,stage 段跳过(commit 段也跳过)。
    失败(UNZIP_FAILED)exit1 → 此时 live 未碰,trap 复位(INSTALLING 仍 0)。"""
    q = shlex.quote
    path = f["path"]
    base = path.rsplit("/", 1)[-1]
    src = '"$ARCH"/"$BASE"'
    kind = _disk_kind_of(f)
    if kind is not None:
        staged = _staged_disk_path(kind)
        out = [
            f"BASE={q(base)}",
            f'echo "[stage] $BASE → unzip → {staged}"',
            f'pigz -dc {src} > {staged} '
            f'|| {{ _perr "UNZIP_FAILED pigz decompress failed for $BASE"; exit 1; }}',
            f'[ -s {staged} ] '
            f'|| {{ _perr "UNZIP_FAILED decompressed $BASE is empty (disk full?)"; exit 1; }}',
            # #333(对齐 bb refresh-rootfs 省盘)解压成功后立刻删 .gz 源 —— 否则暂存区里 .gz +
            # 解压后的 .ext4 同时占盘(拆两循环后所有盘的 .gz+.ext4 会全堆着,峰值占盘更糟)。
            f"rm -f {src}",
        ]
        if kind == "data-template":  # 解压后实占 8.6GB,挖回 sparse 否则吃满 host 盘
            out.append(f"fallocate --dig-holes {staged}")
        return out
    dest, _is_service = _script_live_dest(path)
    if dest is None:  # adot-config.yaml:模板(envsubst 渲染),只归档不装 live
        return [f"BASE={q(base)}", 'echo "[stage] $BASE archived only (templated, not installed live)"']
    staged = _script_staged_path()
    # 脚本/manifest cp 到暂存中转(archive → staged-<base>);最终权限在 commit 段设。
    return [f"BASE={q(base)}", f'echo "[stage] $BASE → {staged}"', f"cp {src} {staged}"]


def _commit_lines(f):
    """Phase2 循环②(commit):把已 stage 好的文件 mv/cp 到 live 原位置(launch-vm/service
    直接读的地方)。全是同盘原子 rename(镜像盘)或 cp(脚本),极快、窗口极小。
    · 镜像盘:mv "$ARCH"/openclaw-<kind>.ext4 → /data/firecracker-assets/openclaw-<kind>.ext4。
    · 脚本/manifest:mv "$ARCH"/staged-<base> → init-host 映射的 dest(.sh chmod+chown;
      常驻 .py service 装文件但不自动重启,只提示)。
    · adot-config.yaml(dest=None):stage 段已跳过,这里也跳过。
    失败 INSTALL_MV_FAILED exit1 → 此时可能已动部分 live,trap 不复位(留 upgrading 给 ops)。"""
    q = shlex.quote
    path = f["path"]
    base = path.rsplit("/", 1)[-1]
    kind = _disk_kind_of(f)
    if kind is not None:
        staged = _staged_disk_path(kind)
        live = f"{_LIVE}/openclaw-{kind}.ext4"
        return [
            f"BASE={q(base)}",
            f'echo "[commit] $BASE → {live}"',
            f'mv {staged} {live} '  # 同盘原子 rename → live
            f'|| {{ _perr "INSTALL_MV_FAILED mv $BASE to live failed"; exit 1; }}',
        ]
    dest, is_service = _script_live_dest(path)
    if dest is None:  # adot-config.yaml:stage 已跳过,commit 也跳过
        return [f"BASE={q(base)}"]
    d = q(dest)
    staged = _script_staged_path()
    out = [f"BASE={q(base)}", f'echo "[commit] $BASE → {dest}"',
           f'mkdir -p "$(dirname {d})"',
           f'mv {staged} {d} '  # 同盘原子 rename staged-<base> → live dest
           f'|| {{ _perr "INSTALL_MV_FAILED mv $BASE to {dest} failed"; exit 1; }}']
    if dest.endswith(".sh"):
        out += [f"chmod +x {d}", f"chown ubuntu:ubuntu {d}"]
    if is_service:  # 常驻 service:装文件不自动重启(避免打断在途生命周期),只提示
        out.append(f'echo "[commit] $BASE updated — a host-agent restart is required to apply" >&2')
    return out


def _reset_status_cmd(
    hosts_table, region, instance_id, status, snapshot_time=None, job_id=None, rootfs_version="",
):
    """host 侧写 hosts 表的一条 aws dynamodb update-item(host 实例角色已有 UpdateItem
    权限,host-agent 心跳在用)。失败时只复位 status→prev;成功时复位 + 写 snapshot_time
    (仅成功才记版本,不谎报)。`|| true`:DDB 写失败不该让整条 SSM 判失败(状态字段是
    旁路,主功能是装 live)。各值 shell-quote 防注入。
    #333(codex round9)owner-conditional:传 job_id 时加 ConditionExpression pull_command_id==job_id
    —— DynamoDB 模糊失败重试(客户端超时但服务端已写)可能在新 job CAS 后把状态覆盖回旧值;
    条件写关死:非当前 owner 的复位 CCF 失败(被 `|| true` 吞,无害)。
    #343 成功路径同步 rootfs_version:pull 装 live 换了 rootfs,却漏更新 host.rootfs_version
    (scaler/rebuild 采用逻辑、rootfs-drift 视图都读它,不同步 = 误判 host 未升级 + rebuild
    后谎报旧版)。故成功且拿到非空版本号时,连 rootfs_version 一起写(值来自 _select_pull_files
    读到的 manifest version)。失败路径【不】写(不谎报升级成功)。空版本号也不写(读不到时不覆盖)。"""
    q = shlex.quote
    base = (
        f"aws dynamodb update-item --table-name {q(hosts_table)} --region {q(region)} "
        f"--key {q(json.dumps({'instance_id': {'S': instance_id}}))} "
    )
    # 复位一律 REMOVE upgrading_at —— 该标记只在 upgrading 态有意义,复位回
    # active/idle 后必须清,否则残留时间戳干扰运维对账(卡死判断误报)。
    vals_map = {":s": {"S": status}}
    if snapshot_time is None:  # 失败路径:只复位 status
        expr = "SET #s = :s REMOVE upgrading_at"
    else:  # 成功路径:复位 status + 记这台 host 当前装的快照版本(补 G8 版本可查)
        expr = "SET #s = :s, snapshot_time = :t"
        vals_map[":t"] = {"S": snapshot_time}
        # #343 —— 成功且版本号非空才补写 rootfs_version(scaler/rebuild 采用逻辑读它)。
        if rootfs_version:
            expr += ", rootfs_version = :rv"
            vals_map[":rv"] = {"S": rootfs_version}
        expr += " REMOVE upgrading_at"
    names = json.dumps({"#s": "status"})
    cond = ""
    if job_id is not None:  # owner-conditional:只当前 owner 才复位(防模糊重试覆盖新 job)
        vals_map[":j"] = {"S": job_id}
        cond = f"--condition-expression {q('pull_command_id = :j')} "
    return (
        f"{base}--update-expression {q(expr)} "
        f"--expression-attribute-names {q(names)} "
        f"--expression-attribute-values {q(json.dumps(vals_map))} "
        f"{cond}>/dev/null 2>&1 || true"
    )


def _snapshot_pull_script(
    bucket, region, host_files, snapshot_time, hosts_table, instance_id, prev_status,
    job_id, rootfs_version="",
):
    """#309 V1 — SSM shell:照【已选好的】host_files(#317:_select_pull_files 已按 manifest
    点名的盘 + manifest 本身选好,不含多余版本),按【精确 VersionId】拉。两段式:
      Phase1 stage+校验:全部 get 到固定暂存区 $ARCH(=/data/firecracker-assets/snapshots/target,
        下载前先 rm 清空防上轮残留),每个文件比对 get 返回的 ETag == 快照记录的 etag
        (multipart 也通用);任一失败 exit1(不装 live)。
      Phase2 unzip+mv 装 live:全部校验通过后,镜像就地在 $ARCH 解压成 .ext4、挖 sparse,
        再 mv 到 launch-vm 直接读的 /data/firecracker-assets/openclaw-*.ext4(同 /data 盘
        原子 rename);manifest.json 直接 cp。

    进度(#309,owner 2026-07-20):每步往 /tmp/<job_id>.txt 追加【时间 + 做了什么】(英文),
    成功最后一行写 "SUCCESS";失败最后一行写 "FAIL"(前一行带 rc 诊断)。pull_image_progress
    直接 tail 这个文件的最后一行当状态。job_id 是 Lambda 生成的(SSM CommandId 要 dispatch
    后才有,脚本无法自知),已 shell-quote 防注入。

    status:Lambda(_pull_by_snapshot)已 CAS 置 upgrading。本脚本自管状态收尾(无金丝雀,
    survives Lambda 死):
      · phase1 拉/校验失败(INSTALLING=0)→ trap rm 暂存 + _reset_fail 复位 status→prev(live 未碰)。
      · phase2 装 live 失败(INSTALLING=1)→ trap【不复位】(live 可能半写坏,复位=谎报 active
        让租户落坏盘,踩 no-cross-tenant/no-data-loss)→ host 留 upgrading 供运维介入。
        V1 不自动 restore(留 V2)。
      · 全成功 → _reset_ok 复位 status→prev + 写 snapshot_time(仅成功记版本)。
    各值 shell-quote 防注入。"""
    n = len(host_files)
    q = shlex.quote
    # 失败复位(只 status)/ 成功复位(status + snapshot_time)两条 update-item。
    # 定义成 shell 函数(_reset_fail/_reset_ok),让 update-item 里 JSON 的单引号活在
    # 函数体内,而不是塞进 trap 的单引号串——否则 JSON 的 '{"S":..}' 会闭合 trap 的引号,
    # shell 把 `{S:` 当 trap 参数 → "bad trap" 脚本首行就崩(真机踩过,993b2330 Failed)。
    # #333(codex round9)owner-conditional 复位:带 job_id,只当前 owner 才复位(防 DDB 模糊重试
    # 在新 job CAS 后覆盖回旧状态)。脚本走到复位处已过 fence(status==upgrading + owner==job),故
    # 条件通常满足;真被新 job 抢占时 CCF(被 `|| true` 吞,不误覆盖)。
    reset_fail = _reset_status_cmd(hosts_table, region, instance_id, prev_status, None, job_id)
    reset_ok = _reset_status_cmd(
        hosts_table, region, instance_id, prev_status, snapshot_time, job_id, rootfs_version
    )

    # _p():进度写手,把【UTC 时间 + 消息】追加到 /tmp/<job_id>.txt(pull_image_progress tail 它)。
    # trap:任何 nonzero 退出都往进度文件写 FAILED;仅当 INSTALLING=0(还没动 live)才 rm 暂存
    # + _reset_fail 复位 status(phase2 已动 live 则留 upgrading,不谎报 active)。
    lines = [
        "set -eu",
        f"JOB=/tmp/{q(job_id)}.txt",
        # #309 owner 2026-07-20 — 下载+解压落 _STAGE(/data/firecracker-assets/snapshots/target),
        # 全部装完 mv 到 _LIVE(同盘原子 rename)。log 仍在 /tmp/<job_id>.txt(不变)。
        f"ARCH={_STAGE}",
        # #333(codex round7)持久化完成标记:装完 live 后写本 job_id 到该文件(先于 _reset_ok)。
        # 若 _reset_ok 的 DDB 写因 `|| true` 静默失败(status 没复位、owner 没变),延迟重复 worker
        # 会通过 status==upgrading+owner fence → 本会重装 live(踩幂等/no-data-loss)。fence 后加
        # 一道 marker 检查:marker==本 job → 本 job 已装完,绝不重装,只补跑 _reset_ok 修 DDB 后让位。
        f"DONE_MARKER={_LIVE}/.pull-last-done",
        # #338 — 每行进度 fan-out 到两个 sink:①/tmp/<job>.txt(progress API tail 用)②journald tag
        # claw-launch(host Fluent Bit 已在收 SYSLOG_IDENTIFIER=claw-launch → claw-logs-host 索引,与
        # host-agent/launch-vm 生命周期日志同一条路,零 FB 配置改动)。msg 只算一次。systemd-cat 兜底
        # `|| true`:极端情况(无 systemd)不 break progress 主功能。日志带 job_id 便于 OpenSearch 按字段查。
        f'_p() {{ msg="$(date -u +%Y-%m-%dT%H:%M:%SZ) [{q(job_id)}] $*"; echo "$msg" >> "$JOB"; '
        f'echo "$msg" | systemd-cat -t claw-launch 2>/dev/null || true; }}',
        # _perr():写具体 ERROR:<CODE> 行 + 置 HAS_ERR=1。各失败点用它(不用裸 _p "ERROR:..."),
        # trap 据 HAS_ERR 决定要不要补 UNKNOWN(codex review:trap 无条件写 UNKNOWN 会覆盖
        # 具体错码,因 progress 取最后一条 ERROR → 所有失败都误报 UNKNOWN)。
        'HAS_ERR=0',
        '_perr() { HAS_ERR=1; _p "ERROR:$*"; }',
        # 复位函数(体内含 update-item 的 JSON,与 trap 引号隔离)
        f"_reset_fail() {{ {reset_fail}; }}",
        f"_reset_ok() {{ {reset_ok}; }}",
        "INSTALLING=0",
        "LOCKED=0",  # 是否已抢到 flock + 确认 job owner。=1 才拥有 $ARCH/status,trap 才写终态/复位。
        # #333 并发正确性(owner + codex review 5 轮):异步 Lambda 是 at-least-once 投递【且超时
        # 不可吞】→ 同一个 job 可能有第二个 worker 并发跑(入口 CAS 只挡不同 pull,挡不住同 job
        # 重投)。故 host 侧必须:① host 级固定 flock(不带 job_id,否则不同 job 各拿一把不互斥)
        # ② job fencing:抢锁后强一致读 DDB pull_command_id 校验 == 本 job_id(非 owner 让位)。
        # LOCKED 门:抢到锁+确认 owner 才置 1;未持锁的 loser 绝不写共享进度文件终态(污染 winner)。
        #
        # trap 必须在任何可能失败步骤(含 flock/清空/mkdir)【之前】注册,否则清理失败不写 FAIL
        # 不复位 → 卡 upgrading。trap 仅 LOCKED=1(winner)才写终态:rc!=0 且无具体 ERROR(HAS_ERR=0)
        # 补 UNKNOWN(否则覆盖真错码);写 FAIL;INSTALLING=0(没动 live)才 rm 暂存 + 复位 prev
        # (动了 live 留 upgrading 给 ops,不自动还原/复位)。loser(LOCKED=0)只 echo stderr,trap 静默。
        # #333(codex round9/11)trap:INSTALLING=1(phase2 已动 live)失败时把 marker 升级成 "failed"
        # (best-effort,便于运维读状态)。但【真正的重装防线是进 phase2 前就落的 installing marker】
        # (见 phase2)——盘满/掉电/SIGKILL 会让 trap 写不成,重复 worker 靠 installing marker(非 done)
        # 就拒绝重装。INSTALLING=0(live 未碰)仍 rm 暂存 + 复位 prev。marker 原子写(tmp+mv)。
        f'trap \'rc=$?; if [ "$rc" != 0 ] && [ "$LOCKED" = 1 ]; then [ "$HAS_ERR" = 0 ] && _p "ERROR:UNKNOWN unexpected exit rc=$rc - see SSM stderr"; _p "FAIL"; if [ "$INSTALLING" = 0 ]; then rm -rf "$ARCH"; _reset_fail; else printf "%s" {q(job_id)}:failed > "$DONE_MARKER".tmp && mv "$DONE_MARKER".tmp "$DONE_MARKER"; fi; fi\' EXIT',
        # ① host 级固定锁(pull.lock,不带 job_id)。#333(codex round6)用【阻塞】等锁 -w 而非
        # -n 直接失败:成功路径在锁内复位 active(见文末),此后新 pull 可 CAS active→upgrading 并
        # fire 后继 worker——若后继用 -n 会抢锁失败退出不复位 → 卡 upgrading(round3 老 bug)。改
        # 阻塞等:持锁 worker 完成(含复位)释锁后,后继拿到锁再走 fence 重校验。真held 超时 → 让位。
        f'exec 9>{_LIVE}/pull.lock',
        'flock -w 120 9 || { echo "[pull] LOCK_HELD another pull worker holds the lock >120s; abort" >&2; exit 1; }',
        # ② fence:强一致读 DDB【status + pull_command_id】,必须 status==upgrading 且 owner==本 job。
        # #333(codex round6)光校 owner 不够:pull_command_id 成功后【保留】(progress 靠它读
        # Completed),故已完成 job 的延迟重复 worker owner 仍匹配 → 若不校 status 会在 host 已回
        # active 时重装 live(踩 no-cross-tenant/no-data-loss)。加 status==upgrading 关死:只有 host
        # 确实还在本轮 upgrading 才继续。--consistent-read 避免最终一致误杀刚写的 job。
        # 读失败(空)重试一次;仍空 → 无法判定所有权:echo OWNERSHIP_CHECK_FAILED 到 stderr 后 exit1
        # (LOCKED=0,绝不动 live/status——此刻 live 未碰)。这【不是】证实的 loser,Lambda 侧据此标记
        # 按真失败处理:job-conditional 记错误 + 复位回 prev(见 _run_pull_pipeline,别卡 upgrading)。
        f'FENCE=$(aws dynamodb get-item --table-name {q(hosts_table)} --region {q(region)} '
        f'--key {q(json.dumps({"instance_id": {"S": instance_id}}))} --consistent-read '
        f'--query "[Item.status.S, Item.pull_command_id.S]" --output text 2>/dev/null) || FENCE=""',
        f'if [ -z "$FENCE" ]; then FENCE=$(aws dynamodb get-item --table-name {q(hosts_table)} '
        f'--region {q(region)} --key {q(json.dumps({"instance_id": {"S": instance_id}}))} --consistent-read '
        f'--query "[Item.status.S, Item.pull_command_id.S]" --output text 2>/dev/null) || FENCE=""; fi',
        'if [ -z "$FENCE" ]; then echo "[pull] OWNERSHIP_CHECK_FAILED cannot read host state from DDB; abort" >&2; exit 1; fi',
        'ST=$(printf "%s" "$FENCE" | cut -f1); OWNER=$(printf "%s" "$FENCE" | cut -f2)',
        'if [ "$ST" != upgrading ]; then echo "[pull] STALE_JOB host status=$ST (not upgrading); superseded/completed, abort" >&2; exit 1; fi',
        f'if [ "$OWNER" != {q(job_id)} ]; then echo "[pull] STALE_JOB job={job_id} owner=$OWNER; superseded, abort" >&2; exit 1; fi',
        "LOCKED=1",  # 抢到锁 + 确认 upgrading + owner → 自此拥有 $ARCH/status 所有权,trap 可写终态/清理/复位
        # #333(codex round7/9)幂等重装防线:marker 记 "<job_id>:<state>"(done/failed)。若 marker
        # 的 job==本 job,说明本 job 的上一个 worker 已【终结】(装完 or phase2 已动 live 后失败),
        # 延迟重复 worker 【绝不】重装(否则重复解压覆盖 / 在半写坏 live 上再装,踩幂等/no-data-loss):
        #   · done   → 上一 worker 装成功(只是 _reset_ok DDB 写 `|| true` 静默失败没复位)→ 补
        #              _reset_ok 修 DDB + 写 SUCCESS 终态,释锁让位。
        #   · failed → 上一 worker phase2 已动 live 后失败(live 可能半写坏)→ 绝不重装,写 FAIL 终态
        #              (host 留 upgrading 给 ops),释锁让位。不自动还原(V1)。
        'MJOB=""; MSTATE=""',
        'if [ -f "$DONE_MARKER" ]; then MRAW=$(cat "$DONE_MARKER" 2>/dev/null); '
        'MJOB=${MRAW%%:*}; MSTATE=${MRAW#*:}; fi',
        # #333(codex round11)marker job==本 job:按 state 决定,但【只有 done 才 exit 0】,其它
        # (installing/failed/任何非 done)一律【拒绝重装 + exit 1】。因为 installing marker 是在
        # 【触碰 live 之前】就持久化的(见 phase2),故只要看到本 job 的 marker 非 done,就说明上一个
        # worker 【已经或即将】动 live(可能被 SIGKILL/掉电/盘满打断,live 半写坏)—— 绝不能重装:
        #   · done             → 上一 worker 装成功(只是 _reset_ok DDB 写静默失败没复位)→ 补
        #                        _reset_ok 修 DDB + SUCCESS,exit 0(Lambda finalize 复位 active 无害)。
        #   · installing/failed → 上一 worker 动 live 后未完成(live 可能半写坏)→ 写 FAIL,exit 1
        #                        (Lambda 见 Failed,job-conditional 记错误、绝不复位 active,留 upgrading
        #                         给 ops)。必须先 LOCKED=0 交出所有权(避免 exit 触发 trap 再写一次)。
        f'if [ "$MJOB" = {q(job_id)} ]; then '
        f'if [ "$MSTATE" = done ]; then _p "SUCCESS"; _reset_ok; exec 9>&-; LOCKED=0; '
        f'echo "[pull] job={job_id} already installed (marker done); reconciled, no reinstall"; exit 0; '
        f'else _perr "INSTALL_MV_FAILED prior worker touched live but did not finish (marker=$MSTATE); no reinstall"; _p "FAIL"; '
        f'exec 9>&-; LOCKED=0; '
        f'echo "[pull] job={job_id} prior worker did not finish (marker=$MSTATE); host stays upgrading" >&2; exit 1; fi; fi',
        # 清空 target 再下载(固定名每轮覆盖,清上轮残留)。不做 df 盘满预检(预估易漂);盘真满
        # 时下载(DOWNLOAD_FAILED)或解压(UNZIP_FAILED "disk full?")自然失败并 surface。
        f'rm -rf "$ARCH"; mkdir -p "$ARCH" {_LIVE}',
        f'_p "start: pull snapshot={snapshot_time} ({n} files) into stage $ARCH"',
        f'echo "[pull] snapshot={snapshot_time} files={n} → phase1 fetch+verify"',
        '_p "phase1: fetching files by version-id and verifying etag"',
    ]
    for i, f in enumerate(host_files, 1):  # Phase1:全部拉到暂存 + 校验 etag
        vid = f.get("s3_version_id", "") or "null"
        base = f["path"].rsplit("/", 1)[-1]
        # 每个文件下载前写一条进度(下载 = get-object by version-id,同时校 etag)。
        lines.append(f'_p "phase1 [{i}/{n}]: downloading + verifying {base}"')
        lines.extend(_verify_lines(bucket, region, f["path"], vid, f.get("etag", "")))
    # #309 phase1.5 — 校验 manifest.json 版本指针与拉下来的盘文件名一致(仍在
    # INSTALLING=0,不一致 exit1 → trap 复位 status→prev,live 未碰)。
    lines.extend(_manifest_consistency_lines(host_files))
    # #333 owner 2026-07-20 — 去掉装 live 前的 backup(原是 V2 自动还原源,但 V2 #313 已作废):
    # backup 占 ~18GB 峰值且现无用途。装 live 失败 → 让他 fail(host 留 upgrading,运维介入),
    # 不做自动还原。省空间 + 缓解盘满。
    lines.append('_p "phase1 OK: installing to live (no backup)"')
    lines.append('echo "[pull] phase1 OK — install to live (no backup)"')
    # #333 owner 2026-07-21 — phase2 拆两个循环:① stage(全部解压/准备到 target 暂存区,live 完全
    # 不碰,INSTALLING 仍 0,失败可安全复位)② commit(全部 stage 成功后,逐个 mv/cp 到 live)。
    # 好处:任一盘解压失败,live 一个字节没动(no-data-loss);commit 段全是极快的同盘 rename。
    # ── 循环① stage:解压/准备到暂存区(live 未碰)──
    lines.append('_p "phase2a: staging (unzip disks + prep scripts into target, live untouched)"')
    lines.append('echo "[pull] phase2a stage into target"')
    for i, f in enumerate(host_files, 1):
        base = f["path"].rsplit("/", 1)[-1]
        verb = "unzipping" if _disk_kind_of(f) is not None else "copying"
        lines.append(f'_p "phase2a [{i}/{n}]: {verb} {base} into target"')
        lines.extend(_stage_lines(f))
    # ── 全部 stage 成功 → 从这里起动了 live ──
    lines.append('_p "phase2a OK: all staged; committing to live"')
    lines.append('echo "[pull] phase2a OK — commit staged to live"')
    # 从这里起动了 live:失败不再复位 active(trap 靠 INSTALLING 判断)。
    lines.append("INSTALLING=1")
    # #333(codex round11)【触碰 live 之前】就持久化 "<job>:installing" marker(原子 tmp+mv)。
    # 关键:phase2 失败标记不能只靠 EXIT trap —— 盘满/掉电/SIGKILL(正是预期失败场景)会让 trap
    # 写不成 marker,延迟重复 worker 就会在半装的 live 上重装。故进 commit、动第一个盘【之前】先落
    # installing marker;成功后替换成 done(见文末)。重复 worker 见任何非 done 一律拒绝重装。
    lines.append(f'printf "%s" {q(job_id)}:installing > "$DONE_MARKER".tmp && mv "$DONE_MARKER".tmp "$DONE_MARKER"')
    # ── 循环② commit:逐个 mv/cp 暂存区 → live(同盘原子 rename,极快)──
    for i, f in enumerate(host_files, 1):
        base = f["path"].rsplit("/", 1)[-1]
        lines.append(f'_p "phase2b [{i}/{n}]: committing {base} to live"')
        lines.extend(_commit_lines(f))
    setting = json.dumps(
        {"snapshot_time": snapshot_time, "staged_dir": _STAGE, "file_count": n}
    )
    lines.append(f"printf '%s' {q(setting)} > {_LIVE}/setting.json")
    # #333(codex round7/9)装完 live 立刻写持久化完成标记 "<job_id>:done",【先于】_reset_ok。即便
    # _reset_ok 的 DDB 写 `|| true` 静默失败(status 没复位),延迟重复 worker 过 fence 后会在
    # LOCKED=1 处读到 marker job==本 job(done)→ 不重装,只补 _reset_ok 修 DDB(见 fence 后那段)。原子写。
    lines.append(f'printf "%s" {q(job_id)}:done > "$DONE_MARKER".tmp && mv "$DONE_MARKER".tmp "$DONE_MARKER"')
    # 装完 live 即成功 → 写终态 SUCCESS + 复位 active,【都在持锁内】(LOCKED=1 保护),最后才释锁。
    lines.append('_p "SUCCESS"')  # 终态先落(progress tail 判完成),持锁时写
    # #333(codex round6)复位 active 必须在【持锁内】、释锁【之前】:fence 校 status==upgrading,
    # 若先释锁再复位,会留一个"已释锁但仍 upgrading+owner 匹配"的窗口 —— 延迟重复 worker 拿到锁
    # 后 fence 通过(status 还是 upgrading)→ 在 host 即将 active 时重装 live。改为锁内复位:复位
    # →active 后任何后继 worker 拿锁再读 status=active → STALE_JOB 让位。round3 的"新 pull 抢不到
    # 锁卡 upgrading"已由阻塞等锁(flock -w)解决(后继 worker 等本 worker 释锁,不再 -n 直接失败)。
    # _reset_ok 内含 `|| true`(见 _reset_status_cmd),DDB 写失败不触发 trap、不影响已写的 SUCCESS。
    lines.append("_reset_ok")   # 锁内复位 status→active + 写 snapshot_time
    lines.append("exec 9>&-")   # 复位完成后才释放 host 级 flock
    lines.append("LOCKED=0")    # 已释锁交出所有权,trap 不再写终态
    lines.append(f'echo "[pull] installed to live + status finalized, snapshot={snapshot_time}"')
    return "\n".join(lines)


def list_image_versions():
    """#337(原#217 GET /snapshots,改名避免与 /images 列镜像文件混淆)— GET /list_image_versions:
    列快照表所有条目的元数据(snapshot_time + label + file_count),供 console 让运维选不同时间点去
    pull。按 snapshot_time 倒序(最新在前)。不回 files 大 JSON(那是 pull 时才逐文件读);
    表未配置 → 503 fail-loud。"""
    if version_snapshots_table is None:
        return _err(503, "NOT_CONFIGURED", "VERSION_SNAPSHOTS_TABLE not configured")
    items = version_snapshots_table.scan().get("Items", [])
    out = [
        {
            "snapshot_time": it.get("snapshot_time"),
            "label": it.get("label", ""),
            "file_count": int(it.get("file_count", 0)),
        }
        for it in items
        if it.get("snapshot_time")
    ]
    out.sort(key=lambda s: s["snapshot_time"], reverse=True)  # 最新在前
    return _resp(200, out)


def _scan_deployment_files(bucket):
    """#376 — 扫 S3 deployment/ 前缀下【全部】对象的当前版,采集
    {path, s3_version_id, etag}。用 list_object_versions + IsLatest 过滤(同 key 的旧版本
    不进快照);吃 paginator 处理多页。返回 list(可能为空,调用方 fail-loud)。"""
    files = []
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix="deployment/"):
        for v in page.get("Versions", []):
            if not v.get("IsLatest"):
                continue  # 只记当前版(快照 = 此刻当前版的定格)
            files.append({
                "path": v["Key"],
                "s3_version_id": v.get("VersionId"),
                "etag": v.get("ETag"),
            })
    return files


def create_image_snapshot(body):
    """POST /create-image-snapshot — 给 assets 桶打一个版本快照(等价 snapshot-version.sh)。

    扫 deployment/ 下全部对象(rootfs 镜像 + scripts + edge + litellm + monitoring)的
    {path, s3_version_id, etag},写一条快照到 DDB(主键 snapshot_time=ISO8601 UTC)。之后
    GET /list_image_versions 可列出、POST /hosts/{id}/pull-image?snapshot_time=<> 可按精确
    VersionId 拉回。

    · fail-loud:采集 0 文件 → 拒写空快照(500),不落一条无用记录。
    · label:未传则读 deployment/rootfs/manifest.json 的 version;传了校验字符集后用传的。
    · snapshot_time 用 %Y-%m-%dT%H:%M:%SZ(匹配 _SNAPSHOT_TIME_RE),否则 pull 判非法拉不动。"""
    if version_snapshots_table is None:
        return _err(503, "NOT_CONFIGURED", "VERSION_SNAPSHOTS_TABLE not configured")
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _err(503, "NOT_CONFIGURED", "ASSETS_BUCKET not configured")

    if isinstance(body, str):
        try:
            body = json.loads(body) if body.strip() else {}
        except (ValueError, TypeError):
            return _err(400, "VALIDATION", "body must be valid JSON")
    body = body or {}
    # 合法 JSON 标量/数组(如 "5"、"[1]")也会过 json.loads,但 body.get 会抛 → 先挡成 400。
    if not isinstance(body, dict):
        return _err(400, "VALIDATION", "body must be a JSON object")
    label = body.get("label")
    if label is not None:
        if not isinstance(label, str) or not _SNAPSHOT_LABEL_RE.match(label):
            return _err(400, "VALIDATION",
                        "label must match [A-Za-z0-9._-]{1,128}")
    else:
        label = _get_manifest().get("version", "")  # 缺省派生 rootfs 版本(读不到 → "")

    files = _scan_deployment_files(bucket)
    if not files:
        return _err(500, "EMPTY_SNAPSHOT",
                    "no files found under deployment/ — bucket/prefix incorrect or permission issue")

    # snapshot_time 主键:秒级 UTC + Z(与 snapshot-version.sh 逐字对齐,匹配 _SNAPSHOT_TIME_RE)。
    snap_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    item = {
        "snapshot_time": snap_ts,
        "files": json.dumps(files, separators=(",", ":")),
        "file_count": len(files),
    }
    if label:
        item["label"] = label
    # 条件写:snapshot_time 是主键,秒级精度下同秒并发/重放两次会静默覆盖前一条(破坏快照
    # 不可变性)。attribute_not_exists 挡住 → 撞了返 409 让调用方稍后重试(下一秒即不同键)。
    ccf = version_snapshots_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        version_snapshots_table.put_item(
            Item=item, ConditionExpression="attribute_not_exists(snapshot_time)"
        )
    except ccf:
        return _err(409, "CONFLICT",
                    f"a snapshot at {snap_ts} already exists; retry in a moment")
    return _resp(200, {"snapshot_time": snap_ts, "label": label, "file_count": len(files)})


def _set_host_upgrading(instance_id, job_id):
    """#217 ★A — CAS active/idle→upgrading。返回 (prev_status, err_response)。
    _find_host/_get_specific_host 白名单只认 active/idle → upgrading 自动排除,挡控制面
    新建。捕获 prev_status:host 复位时还原精确原态(idle host 别误报 active)。upgrading_at:
    host 宕机 trap 不触发卡 upgrading 时(★G)供运维判断。ConditionalCheckFailed(已
    upgrading/并发/host 不存在)→ 返回 409。成功 → (prev_status, None)。
    #333(codex round8)【原子】写:status→upgrading + pull_command_id=job_id + 清 last_pull_error
    全在【同一条】条件 UpdateItem(gate on active/idle)。此前 CAS 与写 pull_command_id 分两步,
    留一个"已 upgrading 但 pull_command_id 还是旧值"的窗口 —— 旧 worker 可在此窗口按旧 owner 条件
    finalize/reset 把新任务置回 active,新 worker 随后 STALE_JOB 静默退,调用方却已收到 202。合成一条
    原子写关死该窗口:置 upgrading 的【同一刻】owner 已是新 job_id。"""
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        pre = hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "SET #s = :u, upgrading_at = :t, pull_command_id = :j REMOVE last_pull_error"
            ),
            ConditionExpression="#s IN (:a, :i)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":u": "upgrading", ":a": "active", ":i": "idle", ":t": _now(), ":j": job_id,
            },
            ReturnValues="UPDATED_OLD",
        )
    except ccf:
        return None, _err(409, "CONFLICT", f"host {instance_id} not available for pull (status must be active/idle; already upgrading or missing)")
    return pre.get("Attributes", {}).get("status", "active"), None


def _pull_by_snapshot(instance_id, snapshot_time):
    """#217 V2 — read the snapshot from DDB, SSM-fetch host files by exact VersionId."""
    # 格式先行:非法 snapshot_time → 400(别拿去查 DB 走成 404 误导调用方)。
    # #336 — 统一错误信封 {error, code}(与 404/409 一致,客户按 code 判)。
    if not _SNAPSHOT_TIME_RE.match(snapshot_time):
        return _err(
            400, "VALIDATION",
            f"snapshot_time must be ISO8601 UTC (YYYY-MM-DDTHH:MM:SSZ); got {snapshot_time!r}",
        )
    if version_snapshots_table is None:
        return _err(503, "NOT_CONFIGURED", "VERSION_SNAPSHOTS_TABLE not configured")
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _err(503, "NOT_CONFIGURED", "ASSETS_BUCKET not configured")
    item = version_snapshots_table.get_item(
        Key={"snapshot_time": snapshot_time}
    ).get("Item")
    if not item:
        return _err(404, "NOT_FOUND", f"snapshot {snapshot_time} not found")
    try:
        files = json.loads(item.get("files", "[]"))
    except (ValueError, TypeError):
        files = []
    # #317 版本选择:只装 manifest.json 点名的 3 个盘 + manifest 本身(忽略快照里其它版本
    # 的盘,防非确定覆盖)。#309:scripts 由 init-host 开机各自拉、edge/litellm/monitoring
    # 不灌 microVM host。选择失败(无 manifest/点名盘缺失)→ fail-loud,不装。
    host_files, sel_err, _ = _select_pull_files(bucket, files)
    if sel_err:
        return _err(409, "CONFLICT", f"snapshot {snapshot_time}: {sel_err}")

    # #309 —— job_id 在【同步路径】生成(SSM CommandId 要 dispatch 后才有、脚本无法自知它,
    # 故用自生成 id 命名进度文件 /tmp/<job_id>.txt)。#333(codex round8)先生成 job_id,再在
    # _set_host_upgrading 的【同一条原子 UpdateItem】里连 pull_command_id 一起写(见该函数注释:
    # 消除 CAS 与写 owner 分两步的窗口)。pull_image_progress 据 pull_command_id tail 进度文件。
    job_id = f"pull-{uuid.uuid4().hex[:16]}"
    # #217 status(ADR §10 ★A):CAS active/idle→upgrading + 原子写 owner,挡 pull 期间控制面派新租户。
    prev_status, err = _set_host_upgrading(instance_id, job_id)
    if err:
        return err

    # #217 fix(504) —— stage + 校验 + 备份 + copy/unzip 装 live 需【数分钟】,远超 APIGW REST
    # 29s 硬上限 → 客户端必吃 504。故 CAS 置 upgrading 后【异步自调用】跑长链,立即回 202;
    # console 轮询 pull_image_progress(tail /tmp/<job_id>.txt)看进度,不依赖本响应。
    try:
        boto3.client("lambda").invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            InvocationType="Event",  # fire-and-forget(同 fleet_service 批处理 worker)
            Payload=json.dumps(
                {"_pull_image_async": {"instance_id": instance_id,
                                       "snapshot_time": snapshot_time,
                                       "prev_status": prev_status,
                                       "job_id": job_id}}
            ).encode("utf-8"),
        )
    except Exception as e:  # 自调用都没发出去 → 复位 status,别卡 upgrading
        _reset_host_status(instance_id, prev_status)
        # #336 — 统一错误信封 {error, code}。
        return _err(500, "DISPATCH_FAILED", f"failed to dispatch pull-image worker: {e}")
    return _resp(
        202,
        {"message": "pull-image started (async; poll pull-image-progress)",
         "instance_id": instance_id, "snapshot_time": snapshot_time,
         "status": "upgrading", "job_id": job_id},
    )


def _run_pull_pipeline(instance_id, snapshot_time, prev_status, job_id):
    """#309 V1 — 异步 worker:stage + 校验 + copy/unzip 装 live 的长链(数分钟)。#333 去 backup。
    由 pull_image 经 InvocationType=Event 自调用触发({"_pull_image_async": {...}}),
    在无客户端等待的 fire-and-forget 调用里跑满(可达 Lambda 900s)。host 已被 pull_image
    CAS 置 upgrading。金丝雀已移除(owner 2026-07-17),V1 失败不自动 restore(留 V2)。
    job_id 由 pull_image(sync)生成并存进 host DDB 项(pull_command_id),脚本据它把进度写
    /tmp/<job_id>.txt,pull_image_progress tail 之。幂等:重放装同版无害(固定名覆盖同内容)。"""
    # #333(codex round8)下发前失败的统一收尾:先 job-conditional 记 last_pull_error(否则 worker
    # 正常 return、不触发 handler 兜底、进度文件又不存在 → progress 永报 InProgress),再复位回 prev
    # (live 未碰,安全)。所有下发【前】的早退路径都走它,不漏记错误。
    def _fail_before_dispatch(reason):
        _record_pull_error(instance_id, reason, job_id)
        _reset_host_status(instance_id, prev_status, job_id)

    # SSM 下发【前】的所有步骤(读快照/选盘/拼脚本)都在 live 未碰的阶段:任何异常(含未预期的
    # DDB/S3 抛错)或已知早退都安全复位回 prev(别卡 upgrading),让 handler 兜底不至于永久占 upgrading。
    try:
        if version_snapshots_table is None:
            _fail_before_dispatch("VERSION_SNAPSHOTS_TABLE not configured")
            return {"statusCode": 503, "body": "VERSION_SNAPSHOTS_TABLE not configured"}
        bucket = os.environ.get("ASSETS_BUCKET", "")
        region = os.environ.get("AWS_REGION", "ap-northeast-1")
        item = version_snapshots_table.get_item(
            Key={"snapshot_time": snapshot_time}
        ).get("Item")
        if not item:
            _fail_before_dispatch(f"snapshot {snapshot_time} not found")
            return {"statusCode": 404, "body": f"snapshot {snapshot_time} not found"}
        try:
            files = json.loads(item.get("files", "[]"))
        except (ValueError, TypeError):
            files = []
        # #317 版本选择:只装 manifest.json 点名的盘(见 _select_pull_files)。
        # #343 rootfs_version:同一处返回 manifest 的 version,pull 成功后写进 host。
        host_files, sel_err, rootfs_version = _select_pull_files(bucket, files)
        if sel_err:
            _fail_before_dispatch(f"snapshot {snapshot_time}: {sel_err}")
            return {"statusCode": 409, "body": f"snapshot {snapshot_time}: {sel_err}"}
        hosts_table_name = os.environ.get("HOSTS_TABLE", "")
        install_cmd = _snapshot_pull_script(
            bucket, region, host_files, snapshot_time,
            hosts_table_name, instance_id, prev_status, job_id, rootfs_version,
        )
    except Exception as e:
        _fail_before_dispatch(f"pre-dispatch error: {e}")
        return _resp(500, {"error": str(e)})

    # #309 V1 —— 两段:① SSM 装 live(同步等)② 成功晋级 / 失败按阶段自决。下发后不再无条件复位。
    cmd_id, ok, tail = _ssm_wait(instance_id, install_cmd, timeout=900)
    if cmd_id is None:
        # 下发都没成功 → 脚本从未跑,live 未碰,安全复位回 prev(别卡 upgrading)+ 记错误供 progress。
        _fail_before_dispatch("pull-image SSM dispatch failed")
        return _resp(500, {"error": "pull-image SSM dispatch failed"})
    if not ok:
        reason = (tail or "")[-400:]
        # #333(codex round7/8)loser 让位不是真失败:同 job 的第二个 worker(等锁 flock -w 超时 /
        # status 已非 upgrading / STALE_JOB)会 exit1 → SSM 判 Failed → 这里 ok=False。它与 winner
        # 同 job_id,若照记 last_pull_error(job-conditional 会通过)→ 把仍在跑的 winner 误报 Failed。
        # 故 LOCK_HELD/STALE_JOB(【已证实】另有 worker 持锁/占有本 job)识别为让位:不写
        # last_pull_error、不动 status(winner 自会收尾),返回 409 表意"已有 worker 在处理"。
        if any(m in (tail or "") for m in ("LOCK_HELD", "STALE_JOB")):
            return _resp(
                409,
                {"message": "pull worker yielded to concurrent owner (no-op)",
                 "command_id": cmd_id, "detail": reason},
            )
        # #333(codex round8)OWNERSHIP_CHECK_FAILED 不是"证实的 loser"——它是 fence 读 DDB 失败、
        # 无法判定所有权(可能就是唯一合法 worker)。它发生在 LOCKED=1 【之前】(live 未碰),故按
        # 【真失败】处理:job-conditional 记错误 + 复位回 prev(别卡 upgrading、progress 别永 InProgress)。
        if "OWNERSHIP_CHECK_FAILED" in (tail or ""):
            _record_pull_error(instance_id, reason, job_id)
            _reset_host_status(instance_id, prev_status, job_id)  # live 未碰,安全复位
            return _resp(502, {"error": "pull-image fence DDB read failed (live untouched, reset)",
                               "command_id": cmd_id, "detail": reason})
        # #309 V1 —— 脚本【跑了】但失败(真失败)。status 由脚本 trap 按阶段自决(靠 INSTALLING):
        #   · phase1 拉/校验 阶段失败(INSTALLING=0)→ 脚本 trap rm 暂存 + 复位 status→prev;
        #   · phase2 装 live 失败(INSTALLING=1,unzip 坏)→ 脚本 trap 不复位,host 留 upgrading。
        # 故 Lambda【绝不】兜底复位 active:phase2 坏了复位 = 谎报 active 盖住半写坏的 live,让
        # 租户落到坏盘(踩 no-cross-tenant/no-data-loss)。Lambda 只记错误原因供 progress 透出。
        _record_pull_error(instance_id, reason, job_id)  # #334 job-conditional(仅当前 owner 记错)
        return _resp(
            502,
            {"error": "pull-image install failed (see SSM log; V1 no auto-restore)",
             "command_id": cmd_id, "detail": reason},
        )

    # 成功:脚本已 _reset_ok 自管收尾(survives Lambda 死);Lambda 再 _finalize_success 兜一次
    # (防脚本 host 侧 aws cli 的 `|| true` 静默失败),两条幂等同值,保证 DDB 终态反映真实。
    _finalize_success(instance_id, snapshot_time, prev_status, job_id, rootfs_version)
    return _resp(
        200,
        {"message": "pull-image installed to live, promoted",
         "snapshot_time": snapshot_time, "instance_id": instance_id,
         "install_command_id": cmd_id},
    )


def _reset_host_status(instance_id, status, job_id=None):
    """★B 兜底:Lambda 侧把 host status 复位到 prev(下发失败/worker 前置失败时用)。best-effort,
    复位失败不掩盖原始错误(但打日志,便于查卡 upgrading 的 host)。
    #333(codex round4):传了 job_id 时条件写(pull_command_id == job_id)——只有当前 owner 才复位,
    防 at-least-once/超时重投的旧 worker 复位掉新任务的 upgrading。入口 dispatch 失败复位不传
    job_id(那时本 job 仍是 owner,无条件复位即可)。非 owner → CCF 静默跳过。"""
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    kw = {
        "Key": {"instance_id": instance_id},
        "UpdateExpression": "SET #s = :s REMOVE upgrading_at",  # 复位一律清 upgrading_at
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":s": status},
    }
    if job_id is not None:
        kw["ConditionExpression"] = "pull_command_id = :j"
        kw["ExpressionAttributeValues"][":j"] = job_id
    try:
        hosts_table.update_item(**kw)
    except ccf:
        print(f"[pull] reset {instance_id}→{status} skipped: not current owner (job={job_id})")
    except Exception as e:
        print(f"[pull] WARN reset status for {instance_id}→{status} failed: {e}")


# #309 — SSM poll 间隔(_ssm_wait 用)。金丝雀已移除(owner 2026-07-17),原
# _CANARY_POLL_EVERY_S 更名 _SSM_POLL_EVERY_S(它一直也被 _ssm_wait 用,与金丝雀无关)。
_SSM_POLL_EVERY_S = 10


def _ssm_wait(instance_id, script, timeout=900):
    """同步下发一段【多行】shell 到 host,poll 到终态。返回 (command_id, ok, tail)。
    不用 ssm_dispatch._ssm_run:那个把命令拼进 `export ...&& cd ...&& <cmd>`(只保护
    首行,破坏多行 set -eu 脚本)。这里原样把整脚本作为一条 command 下发(与
    _pull_by_snapshot 旧写法同款),再 poll get_command_invocation。
    command_id=None 表示下发本身失败(调用方复位 status)。"""
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script], "executionTimeout": [str(timeout)]},
        )
    except Exception as e:
        print(f"[pull] send_command failed: {e}")
        return None, False, ""
    cmd_id = resp.get("Command", {}).get("CommandId")
    time.sleep(3)  # 等 invocation 注册
    deadline = timeout
    waited = 0
    while waited < deadline:
        try:
            r = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
            status = r["Status"]
            if status == "Success":
                return cmd_id, True, r.get("StandardOutputContent", "")
            if status in ("Failed", "TimedOut", "Cancelled"):
                return cmd_id, False, r.get("StandardErrorContent", "") or r.get(
                    "StandardOutputContent", ""
                )
        except ssm.exceptions.InvocationDoesNotExist:
            pass
        time.sleep(_SSM_POLL_EVERY_S)
        waited += _SSM_POLL_EVERY_S
    return cmd_id, False, "SSM timeout"


def _record_pull_error(instance_id, reason, job_id=None):
    """#309 — 装 live 失败时把简短原因记到 host DDB 项(last_pull_error),供
    pull_image_progress 透出。best-effort,不抛(别掩盖原始失败)。绝不在这里改 status
    (status 由脚本 trap 按阶段自决:phase1 已复位 prev / phase2 留 upgrading)。
    #334(codex round6)job-conditional:传 job_id 时条件写 pull_command_id==job_id。否则
    at-least-once/超时重投的【旧 worker】失败后,新 pull 已写入新 job,旧 Lambda 会把旧错误写到
    新 job 的槽 → pull_image_progress 的 DDB 终态优先逻辑据此把新任务误报 Failed。非 owner → CCF 跳过。"""
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    kw = {
        "Key": {"instance_id": instance_id},
        "UpdateExpression": "SET last_pull_error = :e",
        "ExpressionAttributeValues": {":e": (reason or "")[:400]},
    }
    if job_id is not None:
        kw["ConditionExpression"] = "pull_command_id = :j"
        kw["ExpressionAttributeValues"][":j"] = job_id
    try:
        hosts_table.update_item(**kw)
    except ccf:
        print(f"[pull] record error {instance_id} skipped: not current owner (job={job_id})")
    except Exception as e:
        print(f"[pull] WARN record error {instance_id} failed: {e}")


def _finalize_success(instance_id, snapshot_time, prev_status, job_id, rootfs_version=""):
    """装 live 成功 —— host status→prev + 写 snapshot_time(仅成功才记版本,不谎报),
    清 last_pull_error(本轮无错)。**保留 pull_command_id**:progress 据它 tail 进度文件才能
    读到末行 SUCCESS → 返回 Completed(codex review:删了 pull_command_id → progress 拿不到
    job_id → 永远 InProgress/no-job,观察不到成功)。下一轮 pull 的 _set_host_upgrading 覆盖它。
    #333(codex round4/12)条件写:pull_command_id == 本 job_id 【且 status == upgrading】—— 只有
    当前 owner 且 host 仍在本轮 upgrading 才能 finalize。防两类:① at-least-once/超时重投的【旧
    worker】finalize 掉新任务(脚本侧 flock 挡不住 Lambda 侧 DDB 写);② pull_command_id 成功后
    【保留】,若 host 已被移到 draining/deleted,延迟 worker 光凭 owner 匹配会把它错误复位回
    active(round12)。加 #s = :upg 关死:非 upgrading 一律 CCF 跳过。非 owner/非 upgrading → 静默跳过。
    #309:脚本内 _reset_ok 已自管复位(survives Lambda 死);本函数是 Lambda 侧再兜一次。
    #343(codex review):脚本侧 _reset_ok 写 rootfs_version 的 aws cli 若 `|| true` 静默失败,
    Lambda 兜底也必须写 rootfs_version,否则 status/snapshot 恢复了、版本字段仍停旧值(原 bug 重现)。
    故成功且版本号非空时,这条兜底 update 也补 rootfs_version(与脚本侧同值,幂等)。"""
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    # #343 —— 成功且非空版本才补 rootfs_version(空版本不覆盖真值,与 _reset_status_cmd 对齐)。
    expr = "SET #s = :s, snapshot_time = :st"
    vals = {":s": prev_status, ":st": snapshot_time, ":j": job_id, ":upg": "upgrading"}
    if rootfs_version:
        expr += ", rootfs_version = :rv"
        vals[":rv"] = rootfs_version
    expr += " REMOVE upgrading_at, last_pull_error"
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=expr,
            ConditionExpression="pull_command_id = :j AND #s = :upg",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=vals,
        )
    except ccf:
        # 非当前 owner(pull_command_id 被新 pull 覆盖)或 host 已非 upgrading(被移去
        # draining/deleted)→ 本就不该 finalize,静默跳过。
        print(f"[pull] finalize {instance_id} skipped: not current owner or not upgrading (job={job_id})")
    except Exception as e:
        print(f"[pull] WARN finalize {instance_id} failed: {e}")


def pull_image(instance_id, query_params):
    """#309 V1 — POST /hosts/{id}/pull-image?snapshot_time=<ISO>. 照快照按精确 VersionId
    拉 deployment/rootfs/(镜像三盘 + manifest.json),校验 etag 后 copy+unzip 装到 live 原位置
    (launch-vm 直接读的地方)。只作用一台 host。异步跑,立即回 202 + job_id;进度走
    pull_image_progress。金丝雀已移除,失败只报错不自动 restore(留 V2)。"""
    if not instance_id:
        return _err(400, "VALIDATION", "missing instance_id")
    snapshot_time = ((query_params or {}).get("snapshot_time") or "").strip()
    if not snapshot_time:
        return _err(400, "VALIDATION", "snapshot_time required (version mode removed)")
    return _pull_by_snapshot(instance_id, snapshot_time)


# #309 — copy-file 目标位置白名单根:只允许写 firecracker 资产目录,挡任意路径覆盖(越权)。
# copy-file 是给 host 脚本(deployment/scripts/,#309 从 pull_image 移出的 14 个文件)
# 更新用的,不碰镜像盘。目标只能落 init-host.sh 装脚本的两处 live 根(_script_live_dest):
# · /opt/openclaw/ —— 常驻 host-agent 的 .py service;· /home/ubuntu/ —— .sh + lib/*。
# 镜像盘目录 /data/firecracker-assets/ 不在此列(那是 pull_image 的活,不给手动 copy 覆盖)。
_COPY_FILE_ALLOWED_ROOTS = ("/opt/openclaw/", "/home/ubuntu/")


def _validate_copy_target(target):
    """#309 — 校验 copy-file 的目标 EC2 路径:必须落在 _COPY_FILE_ALLOWED_ROOTS 任一根下的
    绝对路径,禁 .. 穿越。返回 (ok, err_msg)。纯函数、易测。
    #334(codex round8)必须是【完整文件路径】,拒目录/尾斜杠:aws s3 cp 到目录会把文件放进去,
    但随后 chown "$DST" 改的是目录、真文件仍 root:root(属主修复在默认流程失效)。故要求含文件名。"""
    if not target or not target.startswith("/"):
        return False, "target must be an absolute path"
    if ".." in target.split("/"):
        return False, "target must not contain '..'"
    if target.endswith("/"):
        return False, "target must be a full file path (no trailing slash / directory)"
    if not any((target + "/").startswith(root) for root in _COPY_FILE_ALLOWED_ROOTS):
        allowed = " or ".join(_COPY_FILE_ALLOWED_ROOTS)
        return False, f"target must be under {allowed}"
    # target 必须【严格深于】某个根(即根下还有文件名),不能就是根本身(那是目录)。
    if any(target.rstrip("/") == root.rstrip("/") for root in _COPY_FILE_ALLOWED_ROOTS):
        return False, "target must include a filename under the allowed root (not the dir itself)"
    return True, ""


def copy_file_from_s3(instance_id, body):
    """#309 — POST /hosts/{id}/copy-file-from-s3:把【单个文件】从 S3 copy 到 EC2 指定位置。
    body: {"target": <EC2 绝对路径,限 _COPY_FILE_ALLOWED_ROOT 下>, "s3_uri": "s3://bucket/key"}。
    同步 SSM(单文件 copy 秒级,不需异步)。两参数严校验 + shlex.quote 防注入;目标限白名单
    前缀防越权覆盖。返回【两者合并】——失败走仓库标准 _err(字段 error+code,HTTP 映射),body 额外
    带 ProcessingJobStatus 让调用方只看 JSON 就能 parse 成败(不依赖 HTTP 码):
      成功 200 → {..., ProcessingJobStatus:"Completed", ExitCode:0}(无 message,靠 status 判)。
      参数错 400 → _err(VALIDATION)。
      脚本失败 502 → _err(COPY_FAILED) + ProcessingJobStatus:"Failed" + error(SSM 末段原因)。
      未下发 500 → _err(COPY_DISPATCH_FAILED) + ProcessingJobStatus:"Failed"。"""
    if not instance_id:
        return _err(400, "VALIDATION", "missing instance_id")
    # APIGW 把 body 作为 JSON 字符串传进来(同 create_tenant),先解析再取值——
    # 否则 body 是 str,下面 .get() 抛 "'str' object has no attribute 'get'"。
    try:
        body = json.loads(body) if isinstance(body, str) else (body or {})
    except (ValueError, TypeError):
        return _err(400, "VALIDATION", "invalid json body")
    if not isinstance(body, dict):
        return _err(400, "VALIDATION", "body must be a JSON object")
    target = (body.get("target") or "").strip()
    s3_uri = (body.get("s3_uri") or "").strip()
    if not s3_uri.startswith("s3://") or len(s3_uri) <= len("s3://"):
        return _err(400, "VALIDATION", "s3_uri must be s3://<bucket>/<key>")
    ok, msg = _validate_copy_target(target)
    if not ok:
        return _err(400, "VALIDATION", msg)
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    q = shlex.quote
    # #334 — SSM 以 root 跑,aws s3 cp 落地即 root:root;但白名单两处根(/opt/openclaw、
    # /home/ubuntu)都属 ubuntu:ubuntu,host-agent(以 ubuntu 跑)要读改这些文件。故 cp 后
    # chown ubuntu:ubuntu(与 pull-image _install_lines 对 .sh 的 chown 一致,补齐这条路径)。
    # #334(codex round8)防注入:s3_uri/target 走 shell 变量(q() 单引号包死),日志引用 "$SRC"/"$DST"
    # 是纯数据。绝不把用户原文直接插进双引号 echo —— 否则含 $()/反引号的值会在 root SSM 里被执行。
    # 允许根的真实路径(host 侧规范化父目录后,必须仍落在这些根下)。空格分隔喂给 POSIX for。
    allowed_roots_sh = " ".join(q(r.rstrip("/")) for r in _COPY_FILE_ALLOWED_ROOTS)
    script = "\n".join([
        # #334(codex round11)显式 POSIX sh:AWS-RunShellScript 在 Ubuntu 默认由 /bin/sh(dash)跑,
        # 不支持 bash 数组。全程用 POSIX 语法(for/case,无数组)。
        "set -eu",
        f"SRC={q(s3_uri)}",
        f"DST={q(target)}",
        f"ALLOWED_ROOTS={q(allowed_roots_sh)}",
        # TODO(#334-toctou): 以下 readlink -f 事前复核【非硬安全边界】——校验与 cp/chown 间存在
        # TOCTOU:host 上 ubuntu(host-agent 身份)可在检查后换软链,借 root SSM 越权。彻底封需
        # openat2(RESOLVE_NO_SYMLINKS)/O_NOFOLLOW helper 或只写 root-owned 根。不触及三条不可退底线,
        # 且需 host ubuntu 已陷才可利用(租户 microVM 不可达),按当前阶段排后为 follow-up。
        # #334(codex round9/10/11)host 侧防越权/属主失效(缓解,非硬边界):
        # ① 目标自身是已存在目录 → 拒(cp 会把文件放进去,chown 改的是目录、真文件仍 root:root)。
        # ② 目标自身是软链 → 拒(cp 会跟随软链写到别处)。
        # ③ 【父目录组件含软链】→ 拒:API 白名单只按字面前缀校验,挡不住 host 上
        #    /home/ubuntu/outside -> /etc 这类父级软链逃逸(写 .../outside/x 实际落 /etc/x,root 越权)。
        # round11:必须【先解析校验、后 mkdir】—— 若先 mkdir -p 一个软链祖先,会在白名单外先建目录。
        #    故先对【已存在的最深祖先】做 readlink -f 校验,通过了再 mkdir 剩余层级;再拼回 basename
        #    成真实写入路径 RDST(cp/检查/chown 全用 RDST,同一路径防 TOCTOU)。
        'if [ -d "$DST" ]; then echo "[copy-file] target is an existing directory: $DST" >&2; exit 1; fi',
        'if [ -L "$DST" ]; then echo "[copy-file] target is a symlink (refused): $DST" >&2; exit 1; fi',
        'DPARENT=$(dirname "$DST")',
        # 找【已存在的最深祖先】(逐级上溯),对它 readlink -f 拿真实路径 —— 未建的层级不含软链风险,
        # 已存在的祖先若是软链会在这里被解析出真身。校验祖先真身落在允许根下(先校验,后 mkdir:
        # 否则先 mkdir -p 一个软链祖先会在白名单外建目录)。
        'ANC="$DPARENT"; while [ ! -e "$ANC" ] && [ "$ANC" != / ]; do ANC=$(dirname "$ANC"); done',
        'RANC=$(readlink -f "$ANC") || { echo "[copy-file] cannot resolve ancestor: $ANC" >&2; exit 1; }',
        'INROOT=0; for r in $ALLOWED_ROOTS; do case "$RANC/" in "$r"/*) INROOT=1 ;; esac; done',
        'if [ "$INROOT" != 1 ]; then echo "[copy-file] existing ancestor escapes allowed roots (ancestor=$RANC; symlink escape?)" >&2; exit 1; fi',
        'mkdir -p "$DPARENT"',  # 校验通过后才建;此时已存在祖先已确认非越权软链
        # 建完对父目录整体 readlink -f 复核(挡校验后竞态 + 未建层级里可能新出现的软链),拼真实
        # 写入路径 RDST。cp/检查/chown 全用 RDST(同一路径,防 TOCTOU)。
        'RPARENT=$(readlink -f "$DPARENT") || { echo "[copy-file] cannot resolve parent: $DPARENT" >&2; exit 1; }',
        'INROOT2=0; for r in $ALLOWED_ROOTS; do case "$RPARENT/" in "$r"/*) INROOT2=1 ;; esac; done',
        'if [ "$INROOT2" != 1 ]; then echo "[copy-file] resolved parent escapes allowed roots: $RPARENT (symlink escape?)" >&2; exit 1; fi',
        'RDST="$RPARENT/$(basename "$DST")"',
        'if [ -d "$RDST" ] || [ -L "$RDST" ]; then echo "[copy-file] resolved target is dir/symlink: $RDST" >&2; exit 1; fi',
        # #334(codex round12)原子写:先下载到【同目录】临时文件 RTMP,校验+设权限后 mv 到 RDST
        # (同 FS 原子 rename)。直接 cp 到 live 目标,传输中断/失败会留半截损坏文件 —— host-agent.py
        # / 启动脚本被截断就坏了。失败清理 RTMP、保留旧文件。RTMP 用 basename 前缀 + $$(PID)避免撞名。
        'RTMP="$RPARENT/.copy-file.$(basename "$DST").$$.tmp"',
        'rm -f "$RTMP"',
        # 失败时清理临时文件(EXIT trap 兜底,不残留 .tmp)
        'trap \'rm -f "$RTMP"\' EXIT',
        f'aws s3 cp "$SRC" "$RTMP" --region {q(region)} --no-progress',
        # 校验临时文件确实落成普通文件(非目录/软链),再设属主+权限,最后原子 mv 到 live 目标。
        'if [ ! -f "$RTMP" ] || [ -L "$RTMP" ]; then echo "[copy-file] downloaded temp not a regular file: $RTMP" >&2; exit 1; fi',
        'chown ubuntu:ubuntu "$RTMP"',  # #334 属主纠正:root:root → ubuntu:ubuntu
        'chmod 755 "$RTMP"',  # #334 落地权限 -rwxr-xr-x(可执行:host 脚本/二进制拷过去要能跑)
        'mv -f "$RTMP" "$RDST"',  # 同 FS 原子 rename → live(传输已完整,不留半截)
        'echo "[copy-file] $SRC -> $RDST OK (atomic, chown ubuntu:ubuntu, chmod 755)"',
    ])
    cmd_id, ok2, tail = _ssm_wait(instance_id, script, timeout=300)
    # #334 owner 2026-07-21 — 返回【两者合并】:失败走仓库标准 _err(字段 error+code,与 create_tenant/
    # pull_image 入口一致),同时 body 额外带 ProcessingJobStatus 让调用方【只看 JSON】就能 parse
    # 成败(不必依赖 HTTP 码——有些客户端遇非 2xx 直接抛错拿不到 body)。copy 是同步的,故直接终态:
    #   · 成功 → 200 + ProcessingJobStatus:Completed + ExitCode:0 + target/s3_uri。
    #   · 脚本跑失败 → 502 + code:COPY_FAILED + ProcessingJobStatus:Failed + FailureReason(SSM 末段)。
    #   · SSM 没下发 → 500 + code:COPY_DISPATCH_FAILED + ProcessingJobStatus:Failed。
    base = {"instance_id": instance_id, "target": target, "s3_uri": s3_uri}
    if cmd_id is None:
        return _err(500, "COPY_DISPATCH_FAILED", "SSM send-command dispatch failed",
                    extra={**base, "ProcessingJobStatus": "Failed"})
    if not ok2:
        return _err(502, "COPY_FAILED", (tail or "")[-400:] or "copy-file failed (see SSM log)",
                    extra={**base, "ProcessingJobStatus": "Failed"})
    return _resp(200, {**base, "ProcessingJobStatus": "Completed", "ExitCode": 0})


def _pull_status_from_line(last_line):
    """#309 owner 2026-07-20 — 把进度文件末行翻译成 SageMaker ProcessingJob 风格三态。
    进度行格式是 `<UTC 时间戳> <消息>`(_p() 写的),故按【末 token】判终态词,不能整行 ==:
      末 token 'SUCCESS'(装脚本成功终态词)→ 'Completed';'FAIL'(trap 失败终态词)→ 'Failed';
      其它(还在跑某步 / 无文件 / 探测失败)→ 'InProgress'。
    脚本末行仍用 shell 的 SUCCESS/FAIL(host 侧终态词不变),API 层在此翻译成对外语义。纯函数。"""
    s = (last_line or "").strip()
    if not s:
        return "InProgress"
    last_token = s.split()[-1]  # `<time> SUCCESS` / `<time> FAIL` → 取末 token
    if last_token == "SUCCESS":
        return "Completed"
    if last_token == "FAIL":
        return "Failed"
    return "InProgress"


def pull_image_progress(instance_id):
    """#309 — GET /hosts/{id}/pull-image-progress:读 host DDB 项拿本轮 pull 的 job_id
    (pull_command_id),SSM 取进度文件的末行(判三态)+ 最近 ERROR 行(失败原因)。返回
    SageMaker ProcessingJob 风格 JSON(owner 2026-07-20):
      ProcessingJobStatus:'Completed'|'Failed'|'InProgress'(末行 SUCCESS/FAIL/其它翻译而来)
      Completed → 带 ExitCode:0
      Failed    → 带 ErrorCode(见 _PULL_ERROR_CODES:DOWNLOAD_FAILED/ETAG_MISMATCH/
                  MANIFEST_MISMATCH/UNZIP_FAILED/INSTALL_MV_FAILED/OWNERSHIP_CHECK_FAILED/UNKNOWN)
                  + FailureReason(详情)
      last_status:进度文件最后一行原文(带时间戳+做了什么,供 UI 展示细节)
    无 job_id → 从没 pull 过(ProcessingJobStatus='InProgress',last_status=None)。

    #333 真实响应样例(InProgress,phase2 正解压第 2/4 个盘,真机 2026-07-20 取):
      {
        "instance_id": "i-0abc123def4567890",
        "host_status": "upgrading",
        "job_id": "pull-491daf780b7d484a",
        "snapshot_time": "2026-07-20T10:21:17Z",
        "last_pull_error": null,
        "ProcessingJobStatus": "InProgress",
        "last_status": "2026-07-20T15:10:46Z phase2 [2/4]: unzipping openclaw-data-template-v1.2.ext4.gz to live",
        "command_id": "4cdd0e05-6a6b-4cfc-ad2a-c20f75c8a68d"
      }"""
    if not instance_id:
        return _err(400, "VALIDATION", "missing instance_id")
    item = hosts_table.get_item(Key={"instance_id": instance_id}).get("Item")
    if not item:
        return _err(404, "NOT_FOUND", f"host {instance_id} not found")
    job_id = item.get("pull_command_id")
    base = {
        "instance_id": instance_id,
        "host_status": item.get("status"),  # host DDB 态(active/idle/upgrading),别与 pull status 混
        "job_id": job_id,
        "snapshot_time": item.get("snapshot_time"),
        "last_pull_error": item.get("last_pull_error"),
    }
    if not job_id:
        return _resp(200, {**base, "ProcessingJobStatus": "InProgress", "last_status": None,
                           "message": "no pull-image job for this host"})
    q = shlex.quote
    jf = f"/tmp/{q(str(job_id))}.txt"
    # 一次 SSM 取两样:① 末行(判三态)② 最近一条 ERROR:<CODE> 行(作 FailureReason)。
    # 用 __LAST__/__ERR__ 前缀分隔,Lambda 侧解析(grep 无命中不致命,回空)。
    script = (
        f'echo "__LAST__$(tail -n 1 {jf} 2>/dev/null)"; '
        f'echo "__ERR__$(grep "ERROR:" {jf} 2>/dev/null | tail -n 1)"'
    )
    cmd_id, ok, tail = _ssm_wait(instance_id, script, timeout=60)
    last_line, err_line = _parse_progress_output(tail if ok else "")
    job_status = _pull_status_from_line(last_line)
    # #333(codex round4/5)终态持久化优先:worker 早退(SSM 未下发/进度文件没建)时,进度文件
    # 判不出终态(永远 InProgress),但 Lambda 侧 _record_pull_error 把失败写进了 DDB last_pull_error。
    # 故 DDB last_pull_error 有值 → 覆盖为 Failed(以持久化终态为准,不让 worker 早退永卡 InProgress)。
    ddb_err = item.get("last_pull_error")
    if job_status != "Completed" and ddb_err:
        job_status = "Failed"
    # SageMaker ProcessingJob 风格:Completed 带 ExitCode=0;Failed 带 FailureReason + ErrorCode。
    out = {**base, "ProcessingJobStatus": job_status,
           "last_status": last_line, "command_id": cmd_id}
    if job_status == "Completed":
        out["ExitCode"] = 0
    elif job_status == "Failed":
        code, reason = _parse_error_line(err_line)
        # FailureReason 优先级:进度文件的 ERROR 行 > DDB last_pull_error > 末行原文。
        out["ErrorCode"] = code                        # 结构化错误码(见 _PULL_ERROR_CODES)
        out["FailureReason"] = reason or ddb_err or last_line
    return _resp(200, out)


# #309 owner 2026-07-20 — pull-image 可返回的失败码(winner worker 各失败点 _perr "<CODE> ..."
# 写进度文件,pull_image_progress 提取成 ErrorCode + FailureReason)。枚举清楚便于 UI/运维按码处置。
# 并发防护两层:① Lambda 侧 _set_host_upgrading 原子 CAS 在入口挡(一台 host 同时只一个 pull);
# ② host 侧 flock + job-fencing 兜住 async Lambda at-least-once 重投(同一 job 起第二个 worker)。
# 注:LOCK_HELD / STALE_JOB 是 loser worker 的 stderr-only 信号(未持锁,【绝不】写共享进度文件,
# 否则会污染 winner 的进度),故【不】进本表(不会成为 ErrorCode);只有持锁后才失败的
# OWNERSHIP_CHECK_FAILED 经 _perr 写进度文件成 ErrorCode。
_PULL_ERROR_CODES = {
    "DOWNLOAD_FAILED": "S3 get-object 拉取失败(权限不足/网络/VersionId 不存在/盘满写不下)",
    "ETAG_MISMATCH": "拉下来的内容与快照记录 etag 不符(内容被改/传输损坏)",
    "MANIFEST_MISMATCH": "manifest 版本指针与拉下来的盘文件名不一致(快照不自洽)",
    "UNZIP_FAILED": "pigz 解压失败或产物为空(.gz 损坏 / 磁盘满)",
    "INSTALL_MV_FAILED": "解压后 mv 到 live 失败(盘满/权限不足/目标占用)",
    "OWNERSHIP_CHECK_FAILED": "持锁后无法从 DDB 读回 pull_command_id 确认所有权(DDB 读失败),fail-loud 不静默退",
    "UNKNOWN": "未标注的意外退出(见 SSM stderr)",
}


def _parse_progress_output(raw):
    """从 pull_image_progress 的 SSM 输出(__LAST__<末行>\\n__ERR__<error行>)解析出
    (last_line, err_line)。纯函数,好测。无输出/格式异常 → 兜底占位。"""
    last_line, err_line = "(progress unavailable)", ""
    for ln in (raw or "").splitlines():
        ln = ln.rstrip("\n")
        if ln.startswith("__LAST__"):
            last_line = ln[len("__LAST__"):].strip() or "(no progress file yet)"
        elif ln.startswith("__ERR__"):
            err_line = ln[len("__ERR__"):].strip()
    return last_line, err_line


def _parse_error_line(err_line):
    """从 `<time> ERROR:<CODE> <detail>` 行提取 (code, full_reason)。code 不在已知表 → UNKNOWN。
    无 ERROR 行 → (None, '')。纯函数,好测。"""
    s = (err_line or "").strip()
    idx = s.find("ERROR:")
    if idx < 0:
        return None, ""
    rest = s[idx + len("ERROR:"):].strip()      # `<CODE> <detail>`
    code = rest.split(None, 1)[0] if rest else "UNKNOWN"
    if code not in _PULL_ERROR_CODES:
        code = "UNKNOWN"
    return code, rest
