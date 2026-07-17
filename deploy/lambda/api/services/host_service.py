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
from core.utils import _now, _resp, _err


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


# #217 V2 — microVM host 真正要的文件前缀(和 init-host.sh 开机拉的一致):镜像三盘
# + 运维脚本。快照【记录】整个 deployment/(全记不漏),但拉到 host 时【只拉这两类】;
# deployment/{edge,litellm,monitoring}/ 是别组件(edge节点/LiteLLM/监控)的部署物,
# 不灌给 microVM host(owner 2026-07-14:只拉你要的文件)。
_HOST_PULL_PREFIXES = ("deployment/rootfs/", "deployment/scripts/")

# #217 V2 — snapshot_time 主键格式 = snapshot-version.sh 生成的 ISO8601 UTC
# (YYYY-MM-DDTHH:MM:SSZ)。API 侧先校验格式:非法 → 400(参数错),合法但 DB 无 → 404
# (快照不存在)。别让乱输入默默查 DB 走成 404,误导调用方"快照不存在"(owner 2026-07-14)。
_SNAPSHOT_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# #217 V2 — 镜像盘文件名 → 盘类型(build-rootfs.sh:1134-1136 命名):
# openclaw-{rootfs,data-template,immutable}-<VER>.ext4.gz。snapshot pull 要按此识别
# 出镜像盘,拉下来解压成 launch-vm 认的【扁平】名 openclaw-<kind>.ext4;非镜像(脚本)
# 直接落原路径不解压。
_IMAGE_DISK_RE = re.compile(r"^openclaw-(rootfs|data-template|immutable)-.+\.ext4\.gz$")


def _image_disk_kind(path):
    """镜像盘 → 'rootfs'/'data-template'/'immutable';脚本/其它 → None。
    launch-vm mount 的是扁平名 openclaw-<kind>.ext4,故镜像 .gz 必须解压成它。纯函数。"""
    m = _IMAGE_DISK_RE.match(path.rsplit("/", 1)[-1])
    return m.group(1) if m else None


# #217 V2 — live 资产根:launch-vm.sh:205-210 直接 mount 的位置。snapshot pull 校验
# 通过后把镜像装到这里的扁平名 openclaw-<kind>.ext4 → launch-vm 不用改就能起新版。
_LIVE = "/data/firecracker-assets"


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
    # WANT 走 shell 变量(不把带引号的 etag 直接塞进双引号 echo,否则引号会重解析)。
    return [
        f'echo "[pull] {path} (version-id={version_id})"',
        f"WANT={q(etag or '')}",
        f"GOT=$(aws s3api get-object --bucket {b} --key {k} --version-id {v} "
        f"--region {r} --query ETag --output text {dst}) "
        f'|| {{ echo "PULL_FAIL key={path} version-id={version_id}" >&2; exit 1; }}',
        f'[ "$GOT" = "$WANT" ] '
        f'|| {{ echo "ETAG_MISMATCH key={path} want=$WANT got=$GOT" >&2; exit 1; }}',
    ]


def _install_lines(path):
    """Phase2 — 全部校验通过后,把 archive 里的文件装到 live 原位置(launch-vm/service
    直接读的地方,launch-vm.sh 不用改)。镜像:pigz 解压 archive .gz → live 扁平
    openclaw-<kind>.ext4(.tmp→mv 防截断,data-template 挖回 sparse);脚本:cp 到
    init-host 映射的 dest(.sh chmod+chown;常驻 .py service 装文件但不自动重启,只提示)。"""
    q = shlex.quote
    base = path.rsplit("/", 1)[-1]
    src = f'"$ARCH"/{q(base)}'
    kind = _image_disk_kind(path)
    if kind is not None:
        live = f"{_LIVE}/openclaw-{kind}.ext4"
        out = [
            f'echo "[install] {base} → {live}"',
            f"pigz -dc {src} > {live}.tmp",
            f"[ -s {live}.tmp ]",
            f"mv {live}.tmp {live}",
        ]
        if kind == "data-template":  # 解压后实占 8.6GB,挖回 sparse 否则吃满 host 盘
            out.append(f"fallocate --dig-holes {live}")
        return out
    dest, is_service = _script_live_dest(path)
    if dest is None:  # adot-config.yaml:模板(envsubst 渲染),只归档不装 live
        return [f'echo "[install] {base} archived only (templated, not installed live)"']
    d = q(dest)
    out = [f'echo "[install] {base} → {dest}"', f'mkdir -p "$(dirname {d})"', f"cp {src} {d}"]
    if dest.endswith(".sh"):
        out += [f"chmod +x {d}", f"chown ubuntu:ubuntu {d}"]
    if is_service:  # 常驻 service:装文件不自动重启(避免打断在途生命周期),只提示
        out.append(f'echo "[install] {base} updated — a host-agent restart is required to apply" >&2')
    return out


def _sanitize_snapshot_dir(snapshot_time):
    """ISO snapshot_time → host 目录安全名:冒号换 -(避 scp/rsync/PATH 坑)。
    2026-07-13T17:02:10Z → 2026-07-13T17-02-10Z。DDB 主键仍用原 ISO(可读)。"""
    return snapshot_time.replace(":", "-")


_BACKUP_DIR = f"{_LIVE}/backup-pre-pull"


def _backup_live_lines(files):
    """#217 §10.3 步3 — 装 live 前,把当前 live 的那套文件备份到 $BK,供金丝雀验坏时
    还原(owner:旧版本要有备份)。镜像备 live 扁平 openclaw-<kind>.ext4(不是 archive .gz);
    脚本备各自 live dest。cp -a 保权限/时间;缺文件(首次无 live)不致命(|| true)。"""
    q = shlex.quote
    out = [
        f"BK={_BACKUP_DIR}",
        'rm -rf "$BK"',
        'mkdir -p "$BK"',
        'echo "[backup] snapshotting current live → $BK before install"',
    ]
    seen = set()
    for f in files:
        kind = _image_disk_kind(f["path"])
        if kind is not None:
            src = f"{_LIVE}/openclaw-{kind}.ext4"
        else:
            dest, _svc = _script_live_dest(f["path"])
            if dest is None:  # templated (adot) — not installed live, nothing to back up
                continue
            src = dest
        if src in seen:
            continue
        seen.add(src)
        # 备份保留相对 live 的相对路径,还原时按同 basename 放回原 dest。
        base = src.rsplit("/", 1)[-1]
        out.append(f'[ -e {q(src)} ] && cp -a {q(src)} "$BK"/{q(base)} || true')
    return out


def _restore_backup_script(files, hosts_table, region, instance_id, status):
    """#217 §10.3 回滚 — 金丝雀验坏时,把 $BK 里备份的 live 那套还原回原位置。
    **不变量(owner):回滚未把 poison 文件真的换回好版本之前,host 绝不复位回 active。**
    故 status 复位【只在全部文件成功还原后才做】:任一文件备份缺失(RESTORE_MISS)或 cp
    失败 → 标 RESTORE_FAILED + host 留在 upgrading(可被运维/巡检发现的显式坏态,绝不谎报
    active 盖住仍是 poison 的 live)。全部还原成功才 _reset → active(不写 snapshot_time,
    live 是旧版)。各值 shell-quote。"""
    q = shlex.quote
    reset = _reset_status_cmd(hosts_table, region, instance_id, status)
    lines = [
        "set -u",
        f"BK={_BACKUP_DIR}",
        "RESTORE_OK=1",  # 任一文件还原失败即清零 → 决定是否允许复位 active
        f"_reset() {{ {reset}; }}",
        'echo "[rollback] restoring live from $BK"',
    ]
    seen = set()
    for f in files:
        kind = _image_disk_kind(f["path"])
        if kind is not None:
            dst = f"{_LIVE}/openclaw-{kind}.ext4"
        else:
            dest, _svc = _script_live_dest(f["path"])
            if dest is None:
                continue
            dst = dest
        if dst in seen:
            continue
        seen.add(dst)
        base = dst.rsplit("/", 1)[-1]
        bk = f'"$BK"/{q(base)}'
        # 备份在才 cp;cp 失败或备份缺失都清 RESTORE_OK(此文件的 poison live 没换回)。
        lines.append(
            f'if [ -e {bk} ]; then mkdir -p "$(dirname {q(dst)})"; '
            f'cp -a {bk} {q(dst)} || {{ echo "RESTORE_FAIL {dst}" >&2; RESTORE_OK=0; }}; '
            f'else echo "RESTORE_MISS {dst}" >&2; RESTORE_OK=0; fi'
        )
    # 不变量门:只有全部文件真的还原成功(poison 已换回好版本)才复位 active;否则 host
    # 留 upgrading(显式坏态,不谎报),报 RESTORE_FAILED 供运维介入(别自动盖住风险)。
    lines.append(
        'if [ "$RESTORE_OK" = 1 ]; then _reset; '
        'echo "[rollback] DONE live restored to pre-pull, status reset"; '
        'else echo "[rollback] RESTORE_FAILED — live may still hold the bad version; '
        'host left in upgrading for operator intervention (NOT reset to active)" >&2; exit 1; fi'
    )
    return "\n".join(lines)


def _reset_status_cmd(hosts_table, region, instance_id, status, snapshot_time=None):
    """host 侧写 hosts 表的一条 aws dynamodb update-item(host 实例角色已有 UpdateItem
    权限,host-agent 心跳在用)。失败时只复位 status→prev;成功时复位 + 写 snapshot_time
    (仅成功才记版本,不谎报)。`|| true`:DDB 写失败不该让整条 SSM 判失败(状态字段是
    旁路,主功能是装 live)。各值 shell-quote 防注入。"""
    q = shlex.quote
    base = (
        f"aws dynamodb update-item --table-name {q(hosts_table)} --region {q(region)} "
        f"--key {q(json.dumps({'instance_id': {'S': instance_id}}))} "
    )
    # 复位一律 REMOVE upgrading_at —— 该标记只在 upgrading 态有意义,复位回
    # active/idle 后必须清,否则残留时间戳干扰运维对账(卡死判断误报)。
    if snapshot_time is None:  # 失败路径:只复位 status
        expr = "SET #s = :s REMOVE upgrading_at"
        names = json.dumps({"#s": "status"})
        vals = json.dumps({":s": {"S": status}})
    else:  # 成功路径:复位 status + 记这台 host 当前装的快照版本(补 G8 版本可查)
        expr = "SET #s = :s, snapshot_time = :t REMOVE upgrading_at"
        names = json.dumps({"#s": "status"})
        vals = json.dumps({":s": {"S": status}, ":t": {"S": snapshot_time}})
    return (
        f"{base}--update-expression {q(expr)} "
        f"--expression-attribute-names {q(names)} "
        f"--expression-attribute-values {q(vals)} >/dev/null 2>&1 || true"
    )


def _snapshot_pull_script(
    bucket, region, files, snapshot_time, hosts_table, instance_id, prev_status
):
    """#217 V2 增量3 — SSM shell:照快照 files 清单,只挑 host 要的(rootfs+scripts),
    按【精确 VersionId】拉。两段式(owner 2026-07-14):
      Phase1 拉+校验:全部 get 到 archive $ARCH(=snapshots/<time>/,版本归档 copy),
        每个文件比对 get 返回的 ETag == 快照记录的 etag(multipart 也通用);任一失败 exit1。
      Phase2 装 live:全部校验通过后,才把文件装到 launch-vm/service 直接读的原位置
        (镜像解压→/data/firecracker-assets/openclaw-*.ext4;脚本→init-host 映射的 dest)。
    → host 上一个版本有两份:archive(snapshots/<time>/,可回滚/审计)+ live(在用)。
    launch-vm.sh 不用改。VersionId=null(versioning 前存量)可正常拉。各值 shell-quote 防注入。
    status(owner 2026-07-14,ADR §10):Lambda 已把 status 置 upgrading(挡控制面新建)。
    本脚本 trap 兜底:任何异常退出都复位 status→prev(★C/D/E,别卡 upgrading);全成功后
    复位 status→prev + 写 snapshot_time(★F,仅成功记版本,补 G8)。"""
    host_files = [f for f in files if f["path"].startswith(_HOST_PULL_PREFIXES)]
    snap_dir = _sanitize_snapshot_dir(snapshot_time)
    n = len(host_files)
    q = shlex.quote
    # 失败复位(只 status)/ 成功复位(status + snapshot_time)两条 update-item。
    # 定义成 shell 函数(_reset_fail/_reset_ok),让 update-item 里 JSON 的单引号活在
    # 函数体内,而不是塞进 trap 的单引号串——否则 JSON 的 '{"S":..}' 会闭合 trap 的引号,
    # shell 把 `{S:` 当 trap 参数 → "bad trap" 脚本首行就崩(真机踩过,993b2330 Failed)。
    reset_fail = _reset_status_cmd(hosts_table, region, instance_id, prev_status)
    reset_ok = _reset_status_cmd(hosts_table, region, instance_id, prev_status, snapshot_time)

    # API 不做下发前存在性校验(某些文件可能已废弃,预检会误卡整批,owner 2026-07-14)。
    # 进度/成败打 log(进 SSM StandardOutput,查 command 即见);拉失败 PULL_FAIL、
    # etag 不符 ETAG_MISMATCH,都 exit1(set -eu)→ Phase2 不执行,live 不被半拉污染。
    # trap 兜底:异常退出 rm -rf archive(不留半拉 .gz)+ 调 _reset_fail 复位 status→prev
    # (别卡 upgrading);全成功后解除 trap,调 _reset_ok 复位 status + 写 snapshot_time。
    lines = [
        "set -eu",
        f"ARCH={_LIVE}/snapshots/{q(snap_dir)}",
        f'mkdir -p "$ARCH" {_LIVE}',
        # 复位函数(体内含 update-item 的 JSON,与 trap 引号隔离)
        f"_reset_fail() {{ {reset_fail}; }}",
        f"_reset_ok() {{ {reset_ok}; }}",
        'trap \'rc=$?; [ "$rc" != 0 ] && { echo "[pull] FAILED rc=$rc, cleaning $ARCH + status reset" >&2; rm -rf "$ARCH"; _reset_fail; }\' EXIT',
        f'echo "[pull] snapshot={snapshot_time} files={n} → phase1 fetch+verify"',
    ]
    for f in host_files:  # Phase1:全部拉到 archive + 校验 etag
        vid = f.get("s3_version_id", "") or "null"
        lines.extend(_verify_lines(bucket, region, f["path"], vid, f.get("etag", "")))
    lines.append('echo "[pull] phase1 OK — backup current live before install"')
    # #217 §10.3 步3 — 装 live 前备份当前 live 那套(金丝雀验坏的还原源;owner:成功也
    # 保留这份,下次 pull 前才被覆盖 → 这台 host 手上永远攥着一个可回退的好版本)。
    lines.extend(_backup_live_lines(host_files))
    lines.append('echo "[pull] phase2 install to live"')
    for f in host_files:  # Phase2:全部校验通过后才装 live
        lines.extend(_install_lines(f["path"]))
    lines.append("trap - EXIT")
    setting = json.dumps(
        {"snapshot_time": snapshot_time, "staged_dir": snap_dir, "file_count": n}
    )
    lines.append(f"printf '%s' {q(setting)} > {_LIVE}/setting.json")
    # #217 §10.3 — 装 live 完成【不复位 status】:host 保持 upgrading,由控制面 Lambda
    # 起金丝雀验证后再决定复位(成功→active+写 snapshot_time / 验坏→还原 live+复位)。
    # 故这里不调 _reset_ok(那是无金丝雀简化版的收尾)。reset_ok/reset_fail 仍定义:
    # reset_fail 供 trap 在【装 live 前】异常退出时复位(未碰 live,直接回 prev);
    # 装 live 成功后 trap 已解除(trap - EXIT),status 交给 Lambda 编排。
    lines.append(f'echo "[pull] installed to live (status stays upgrading; canary next), snapshot={snapshot_time}"')
    return "\n".join(lines)


def list_snapshots():
    """#217 — GET /snapshots:列快照表所有条目的元数据(snapshot_time + label +
    file_count),供 console 让运维选不同时间点去 pull。按 snapshot_time 倒序(最新在前)。
    不回 files 大 JSON(那是 pull 时才逐文件读);表未配置 → 503 fail-loud。"""
    if version_snapshots_table is None:
        return _resp(503, {"error": "VERSION_SNAPSHOTS_TABLE not configured"})
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


def _set_host_upgrading(instance_id):
    """#217 ★A — CAS active/idle→upgrading。返回 (prev_status, err_response)。
    _find_host/_get_specific_host 白名单只认 active/idle → upgrading 自动排除,挡控制面
    新建。捕获 prev_status:host 复位时还原精确原态(idle host 别误报 active)。upgrading_at:
    host 宕机 trap 不触发卡 upgrading 时(★G)供运维判断。ConditionalCheckFailed(已
    upgrading/并发/host 不存在)→ 返回 409。成功 → (prev_status, None)。"""
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        pre = hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET #s = :u, upgrading_at = :t",
            ConditionExpression="#s IN (:a, :i)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":u": "upgrading", ":a": "active", ":i": "idle", ":t": _now(),
            },
            ReturnValues="UPDATED_OLD",
        )
    except ccf:
        return None, _err(409, "CONFLICT", f"host {instance_id} not available for pull (status must be active/idle; already upgrading or missing)")
    return pre.get("Attributes", {}).get("status", "active"), None


def _pull_by_snapshot(instance_id, snapshot_time):
    """#217 V2 — read the snapshot from DDB, SSM-fetch host files by exact VersionId."""
    # 格式先行:非法 snapshot_time → 400(别拿去查 DB 走成 404 误导调用方)。
    if not _SNAPSHOT_TIME_RE.match(snapshot_time):
        return _resp(
            400,
            {"error": f"snapshot_time must be ISO8601 UTC (YYYY-MM-DDTHH:MM:SSZ); got {snapshot_time!r}"},
        )
    if version_snapshots_table is None:
        return _resp(503, {"error": "VERSION_SNAPSHOTS_TABLE not configured"})
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    item = version_snapshots_table.get_item(
        Key={"snapshot_time": snapshot_time}
    ).get("Item")
    if not item:
        return _err(404, "NOT_FOUND", f"snapshot {snapshot_time} not found")
    try:
        files = json.loads(item.get("files", "[]"))
    except (ValueError, TypeError):
        files = []
    # 只拉 host 要的(rootfs+scripts);快照虽记全 deployment/,但 edge/litellm/monitoring
    # 不灌 microVM host。空(快照无文件 or 无 host 相关文件)→ fail-loud,不发空拉取。
    host_files = [f for f in files if f["path"].startswith(_HOST_PULL_PREFIXES)]
    if not host_files:
        return _err(409, "CONFLICT", f"snapshot {snapshot_time} has no host-relevant files (rootfs/scripts)")

    # #217 status(ADR §10 ★A):CAS active/idle→upgrading,挡 pull 期间控制面往这台派新租户。
    prev_status, err = _set_host_upgrading(instance_id)
    if err:
        return err

    # #217 fix(504) —— 装 live + 金丝雀验证需【数分钟】,远超 APIGW REST 29s 硬上限
    # (browser→CloudFront→ALB→BFF 30s→APIGW 29s→本 Lambda 900s)→ 客户端必吃 504。
    # 旧写法靠"APIGW 断开但 Lambda 不中止、后台跑完"侥幸成功——脆弱且 UX 破(用户见
    # 504 以为失败)。改显式 run-and-forget:CAS 置 upgrading 后【异步自调用】跑长链,
    # 立即回 202;console 已在轮询 host status(upgrading→active/回滚),不依赖本响应。
    try:
        boto3.client("lambda").invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            InvocationType="Event",  # fire-and-forget(同 fleet_service 批处理 worker)
            Payload=json.dumps(
                {"_pull_image_async": {"instance_id": instance_id,
                                       "snapshot_time": snapshot_time,
                                       "prev_status": prev_status}}
            ).encode("utf-8"),
        )
    except Exception as e:  # 自调用都没发出去 → 复位 status,别卡 upgrading
        _reset_host_status(instance_id, prev_status)
        return _resp(500, {"error": f"failed to dispatch pull-image worker: {e}"})
    return _resp(
        202,
        {"message": "pull-image started (install + canary run async; poll host status)",
         "instance_id": instance_id, "snapshot_time": snapshot_time, "status": "upgrading"},
    )


def _run_pull_pipeline(instance_id, snapshot_time, prev_status):
    """#217 fix(504) — 异步 worker:装 live + 金丝雀验证 + 晋级/回滚的长链(数分钟)。
    由 pull_image 经 InvocationType=Event 自调用触发({"_pull_image_async": {...}}),
    在无客户端等待的 fire-and-forget 调用里跑满(可达 Lambda 900s)。host 已被 pull_image
    CAS 置 upgrading;本函数负责把它推进到 active(晋级)或回滚复位。幂等:重放时 host 已
    非 active/idle,但本函数不再 CAS(pull_image 已占位),直接按已装 live 继续——重复
    装同版无害(archive+backup 覆盖同内容),金丝雀重跑亦收敛。"""
    if version_snapshots_table is None:
        _reset_host_status(instance_id, prev_status)
        return {"statusCode": 503, "body": "VERSION_SNAPSHOTS_TABLE not configured"}
    bucket = os.environ.get("ASSETS_BUCKET", "")
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    item = version_snapshots_table.get_item(
        Key={"snapshot_time": snapshot_time}
    ).get("Item")
    if not item:
        _reset_host_status(instance_id, prev_status)
        return {"statusCode": 404, "body": f"snapshot {snapshot_time} not found"}
    try:
        files = json.loads(item.get("files", "[]"))
    except (ValueError, TypeError):
        files = []
    host_files = [f for f in files if f["path"].startswith(_HOST_PULL_PREFIXES)]
    if not host_files:
        _reset_host_status(instance_id, prev_status)
        return {"statusCode": 409, "body": f"snapshot {snapshot_time} has no host files"}
    hosts_table_name = os.environ.get("HOSTS_TABLE", "")

    # #217 §10.3 —— 分三段:① 装 live(SSM 同步等)② 起金丝雀验证 ③ 晋级/回滚。
    try:
        install_cmd = _snapshot_pull_script(
            bucket, region, files, snapshot_time,
            hosts_table_name, instance_id, prev_status,
        )
    except Exception as e:
        _reset_host_status(instance_id, prev_status)
        return _resp(500, {"error": str(e)})

    # ① 装 live:SSM 同步等(脚本内 trap 在装 live 前失败会自复位 status→prev;装完
    # 保持 upgrading,交给下面金丝雀)。SSM 下发失败/脚本失败 → 复位 + 报错(live 未污染
    # 或脚本 trap 已还原)。
    cmd_id, ok, tail = _ssm_wait(instance_id, install_cmd, timeout=900)
    if cmd_id is None:  # 下发都没成功
        _reset_host_status(instance_id, prev_status)
        return _resp(500, {"error": "pull-image SSM dispatch failed"})
    if not ok:  # 脚本失败(拉/校验/装 live 某步)——trap 已复位 status,best-effort 再兜一次
        _reset_host_status(instance_id, prev_status)
        return _resp(
            502,
            {"error": "pull-image install failed (see SSM log)", "command_id": cmd_id,
             "detail": tail[-400:]},
        )

    # ②③ 金丝雀验证 + 晋级/回滚。装 live 已成功、host 仍 upgrading。
    return _run_canary(
        instance_id, snapshot_time, host_files, hosts_table_name, region,
        prev_status, cmd_id,
    )


def _reset_host_status(instance_id, status):
    """★B 兜底:Lambda 侧把 host status 复位到 prev(下发失败时用)。best-effort,
    复位失败不掩盖原始 500(但打日志,便于查卡 upgrading 的 host)。"""
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            # 复位一律 REMOVE upgrading_at(该标记只在 upgrading 态有意义)。
            UpdateExpression="SET #s = :s REMOVE upgrading_at",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
        )
    except Exception as e:
        print(f"[pull] WARN reset status for {instance_id}→{status} failed: {e}")


# #217 §10.3 金丝雀参数(owner 可调):金丝雀 tenant 的规格 + 健康 poll 上限。
_CANARY_VCPU = 2
_CANARY_MEM_MB = 4096  # 与 ADR §10.2#5"占真 2C/4G"一致
# 起 Firecracker microVM 是秒级(实测 microVM 启动 1.74s);算上 gateway/device 握手
# + host-agent 探针 creating→running,健康的金丝雀 60s 内必到 running。超 90s 还没起
# = 判定失败走回滚(owner:起 FC 节点不该久,~60s 起不来就当失败),别让坏版本卡住
# host 数分钟(旧 300s 太长:bad-image 测试在 upgrading 干等 ~4min 才回滚)。
_CANARY_POLL_MAX_S = 90  # 60s 起 + 30s 余量;超时判 fail 走回滚
_CANARY_POLL_EVERY_S = 10


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
        time.sleep(_CANARY_POLL_EVERY_S)
        waited += _CANARY_POLL_EVERY_S
    return cmd_id, False, "SSM timeout"


def _poll_canary_healthy(tenant_id):
    """poll 金丝雀 tenant 的 DDB status:running(healthy,host-agent 探针 creating→running
    见 ADR §10.3 注)→ True;deleted/failed → False;超 _CANARY_POLL_MAX_S → False。
    复用 host-agent 现有探针语义,不另写健康检查。"""
    waited = 0
    while waited < _CANARY_POLL_MAX_S:
        item = tenants_table.get_item(Key={"id": tenant_id}).get("Item") or {}
        st = item.get("status")
        if st == "running":
            return True
        if st in ("failed", "deleted"):
            return False
        time.sleep(_CANARY_POLL_EVERY_S)
        waited += _CANARY_POLL_EVERY_S
    return False  # 超时判 unhealthy → 回滚


def _delete_canary(tenant_service, canary_id):
    """删金丝雀(throwaway test 租户,无真数据)——检查【返回码】不只吞异常。
    delete_tenant 对失败场景返回【非 2xx dict】而非抛异常(如 pre-delete 备份失败 502),
    旧代码只 try/except 会漏掉这种"没删成"→ 金丝雀占着 vm_num 槽 = 容量泄漏(真机踩过,
    需人工删)。这里:① keep_data=false + skip_backup=true 强制全回收,绝不让备份失败挡删
    (金丝雀无值得留的数据);② 校验返回码,非 2xx 重试一次;③ 仍失败 loud log 标 ORPHAN
    供巡检/运维发现,不静默。返回 True=已删/已是 deleted,False=留下孤儿。"""
    qp = {"keep_data": "false", "skip_backup": "true"}
    for attempt in (1, 2):
        try:
            resp = tenant_service.delete_tenant(canary_id, qp, {})
        except Exception as e:
            print(f"[canary] WARN delete canary {canary_id} attempt {attempt} raised: {e}")
            continue
        code = resp.get("statusCode") if isinstance(resp, dict) else None
        if code in (200, 202, 204):
            return True
        body = str((resp or {}).get("body", ""))[:200] if isinstance(resp, dict) else ""
        print(f"[canary] WARN delete canary {canary_id} attempt {attempt} returned {code}: {body}")
    print(f"[canary] ORPHAN canary {canary_id} NOT deleted after retries — "
          f"holds a vm_num slot (capacity leak); needs reaper/manual cleanup")
    return False


def _run_canary(
    instance_id, snapshot_time, host_files, hosts_table_name, region,
    prev_status, install_cmd_id,
):
    """#217 §10.3 步5-6 —— live 已装新版、host 仍 upgrading。起金丝雀 tenant(走
    create_tenant 拿真 vm_num + 占真容量,§10.2#5)验证新版能起 VM:
      healthy → 删金丝雀 + status→prev + 写 snapshot_time(晋级)
      unhealthy/超时 → SSM 从 backup 还原 live + 复位 status(回滚)+ 删金丝雀
    金丝雀走 create_tenant(_canary_host=instance_id) 豁免落到 upgrading 的这台。"""
    # 横向 import(services 间允许,见模块头);函数内 import 避免与 tenant_service 循环。
    import services.tenant_service as tenant_service

    canary_name = "canary-" + instance_id[-8:]  # 稳定名(name 去重会挡重复,pull 前应已清)
    body = {
        "name": canary_name,
        "vcpu": _CANARY_VCPU,
        "memory_mb": _CANARY_MEM_MB,
        "preferred_host_id": instance_id,
    }
    # api-key/admin 内部路径:event 传空 → _get_caller_identity 走 API_KEY_OWNER admin。
    created = tenant_service.create_tenant(body, {}, _canary_host=instance_id)
    code = created.get("statusCode") if isinstance(created, dict) else None
    if code not in (200, 201, 202):
        # 金丝雀起不来(容量不足/名冲突/launch 失败)→ 视作验证失败,回滚 live。
        print(f"[canary] create_tenant failed code={code} body={created}")
        _rollback_live(instance_id, host_files, hosts_table_name, region, prev_status)
        return _resp(
            502,
            {"error": "canary create failed; live rolled back to previous",
             "instance_id": instance_id, "canary_result": created.get("body", "")},
        )
    try:
        canary_id = json.loads(created.get("body", "{}")).get("id")
    except (ValueError, TypeError):
        canary_id = None

    healthy = _poll_canary_healthy(canary_id) if canary_id else False

    # 无论成败都删金丝雀(它只为验证存在,不长驻);event 空走 admin 删。校验返回码 +
    # 重试 + 孤儿告警(_delete_canary),不静默吞非 2xx → 治容量泄漏。
    if canary_id:
        _delete_canary(tenant_service, canary_id)

    if healthy:
        # 晋级:host status→prev + 写 snapshot_time(GET /hosts 可查生效版本)。
        _finalize_success(instance_id, snapshot_time, prev_status)
        return _resp(
            200,
            {"message": "pull-image + canary verified, promoted",
             "snapshot_time": snapshot_time, "instance_id": instance_id,
             "canary_id": canary_id, "install_command_id": install_cmd_id},
        )
    # 回滚:从 backup 还原 live + 复位 status。
    _rollback_live(instance_id, host_files, hosts_table_name, region, prev_status)
    return _resp(
        502,
        {"error": "canary unhealthy; live rolled back to previous version",
         "instance_id": instance_id, "canary_id": canary_id},
    )


def _finalize_success(instance_id, snapshot_time, prev_status):
    """金丝雀通过 —— host status→prev + 写 snapshot_time(仅成功才记版本,不谎报)。"""
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET #s = :s, snapshot_time = :st REMOVE upgrading_at",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": prev_status, ":st": snapshot_time},
        )
    except Exception as e:
        print(f"[pull] WARN finalize {instance_id} failed: {e}")


def _rollback_live(instance_id, host_files, hosts_table_name, region, prev_status):
    """金丝雀验坏 —— SSM 同步跑还原脚本从 backup 还原 live。还原脚本【只在全部文件成功
    还原后】才复位 status→prev(见 _restore_backup_script 的不变量门);还原失败它 exit 1
    且 host 留 upgrading。
    **不变量(owner):poison 文件没换回好版本之前,host 绝不回 active。** 故这里 SSM 失败
    时【绝不再兜底 _reset_host_status→active】(那会谎报 active 盖住仍是 poison 的 live)——
    宁可留 host 在 upgrading(显式坏态,GET /hosts 可见 + upgrading_at 时间戳供巡检发现),
    让运维介入,也不自动把风险藏起来。"""
    script = _restore_backup_script(
        host_files, hosts_table_name, region, instance_id, prev_status
    )
    cmd_id, ok, tail = _ssm_wait(instance_id, script, timeout=300)
    if cmd_id is None or not ok:
        # 回滚没成功 → host 保持 upgrading(不复位 active,守不变量)。fail-loud 告警。
        print(f"[pull] ROLLBACK_FAILED host={instance_id} (cmd={cmd_id}) — live may still "
              f"hold the bad version; left in upgrading for operator intervention: {tail[-300:]}")


def pull_image(instance_id, query_params):
    """#217 V2 — POST /hosts/{id}/pull-image?snapshot_time=<ISO>. 照快照按精确
    VersionId 拉整个 host 相关 deployment/(镜像+脚本),校验 etag 后装到 live 原位置
    (launch-vm/service 直接读的地方)。只作用一台 host。旧 ?version=/versions/<ver> 模式
    已废弃(owner 2026-07-14:统一快照模型,只留这一个 API)。"""
    if not instance_id:
        return _err(400, "VALIDATION", "missing instance_id")
    snapshot_time = ((query_params or {}).get("snapshot_time") or "").strip()
    if not snapshot_time:
        return _err(400, "VALIDATION", "snapshot_time required (version mode removed)")
    return _pull_by_snapshot(instance_id, snapshot_time)
