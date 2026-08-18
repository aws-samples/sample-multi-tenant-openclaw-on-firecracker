"""core/services 层 · host_service:host 注册/注销/清理 + rootfs 镜像清单/刷新/漂移。

handler-split #132 T1.7 —— 从 handler.py 逐字搬迁,行为零改动。
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
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from core.clients import (
    CPU_OVERCOMMIT_RATIO,
    MEM_OVERCOMMIT_RATIO,
    asg_client,
    hosts_table,
    tenants_table,
    version_snapshots_table,
    s3,
    ssm,
)
from core.utils import _now, _resp, _err, _parse_limit
from core.pagination import decode_cursor, encode_cursor
from core import image_jobs
from core import image_slots
from core import image_lease


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


def _upsert_host_row(
    instance_id,
    instance_type,
    private_ip,
    az,
    vcpu_total,
    mem_total,
    rootfs_version="",
):
    """#491 —— 重入安全的 host 注册写入。

    此前是无条件 put_item(整项覆盖)且把四个运行时记账字段写成首启常量 0/0/0/1:对一台
    已有在役租户的 host 再调一次 POST /hosts,账本被抹回初值、发号器随之回退到 1,之后
    dispatch 的 reserve CAS 会把已在用的号再发一遍且每次都成功 → launch-vm.sh 抢占先到者
    的 tap = 跨租户劫持(#491 已真机复现)。这与 init-host.sh 的自注册是同一缺陷的两个副本,

    账本四字段(used_vcpu/used_mem_mb/vm_count/next_vm_num)的权威是控制面的认领/释放 CAS,
    注册路径只能【补】不能【改】—— 读回再写也不行:读与写之间落地的并发 create 会被抹掉。
    故首启走条件 put(记账起点只由这一次写产生),已存在则只刷静态字段 + 用 if_not_exists
    补齐缺失的记账字段(缺字段的行会让 reserve 的 CAS 条件恒假 → 每次分配 CCF → 503,
    见 #445 在 apse1 的实测)。范式与 init-host.sh(#445)对齐。

    与 #470 的合流:rootfs_version 是【静态字段】(每次 bootstrap 拉到的 manifest 版本可能
    不同),首启写入、重入刷新。取不到版本时【不写】—— DDB 拒空 S,写空还会假称"无版本"
    (遵 #343/#304 非空才写),更不能擦掉行上已有的版本。
    """
    item = {
        "instance_id": instance_id,
        # 上面已从 describe_instances 取到(只用于查内存),这里一并持久化;缺失回落
        # "unknown"(DDB 的 S 不接受空串),排序侧对未知 family 落表尾。
        "instance_type": instance_type or "unknown",
        "private_ip": private_ip,
        "az": az,
        # 这两个值本来就是标称的:CoreCount×ThreadsPerCore 与
        # describe_instance_types 的 MemoryInfo.SizeInMiB 都是广告值。此前又扣
        # HOST_RESERVED_*,于是同一台机器走 API 注册比走 init-host.sh 少一截容量,
        # 达不到标称理论上限(384/256/192/128),两条路径口径分叉。
        # host OS/Firecracker 驻留内存的保护改由 scheduling.mem_safety_floor_ratio
        # 物理水位门承担(读 host 自报实测 MemAvailable)—— 与标称注册是一套。
        "total_vcpu": vcpu_total,
        "total_mem_mb": mem_total,
        "used_vcpu": 0,
        "used_mem_mb": 0,
        "vm_count": 0,
        "next_vm_num": 1,
        "status": "active",
        # idle_since 只在首启写:重入覆盖它会把一台正在服务的 host 的空闲起点
        # 刷新掉,scaler 的 idle 回收判定会跟着错。
        "idle_since": _now(),
    }
    if rootfs_version:
        item["rootfs_version"] = rootfs_version
    try:
        hosts_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(instance_id)",
        )
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise  # 限流/权限/网络等真错误照原样上抛,不能与"已存在"混为一谈
    # CCF = 本实例已注册过(重入)。status 仍写 active:重新注册的意图就是让 draining 的
    # host 重新可调度。status 是 DDB 保留字,必须走 ExpressionAttributeNames 别名。
    sets = [
        "instance_type = :it",
        "private_ip = :ip",
        "az = :az",
        "total_vcpu = :tv",
        "total_mem_mb = :tm",
        "#s = :st",
        "used_vcpu = if_not_exists(used_vcpu, :zero)",
        "used_mem_mb = if_not_exists(used_mem_mb, :zero)",
        "vm_count = if_not_exists(vm_count, :zero)",
        "next_vm_num = if_not_exists(next_vm_num, :one)",
    ]
    vals = {
        ":it": instance_type or "unknown",
        ":ip": private_ip,
        ":az": az,
        ":tv": vcpu_total,
        ":tm": mem_total,
        ":st": "active",
        ":zero": 0,
        ":one": 1,
    }
    if rootfs_version:
        sets.append("rootfs_version = :rv")
        vals[":rv"] = rootfs_version
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET " + ", ".join(sets),
        ConditionExpression="attribute_exists(instance_id)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=vals,
    )


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

    # init-host.sh, which self-registers via a direct DDB put and already stamps
    # rootfs_version from the manifest it pulled. This API path left the field
    # unset, so a host registered here carried no version for tenants to inherit.
    # Stamp it from the live S3 manifest (same source as GET /hosts/rootfs-version)
    # bb); this only covers hosts registered through the API. Omit on unknown/empty
    # rather than writing "" — DDB rejects empty S and an empty value would falsely
    rootfs_version = _get_manifest().get("version", "")
    # #491 — 写入走重入安全的 upsert(条件 put + CCF 分支只刷静态字段、if_not_exists 补账本)。
    _upsert_host_row(
        instance_id,
        instance_type,
        private_ip,
        az,
        vcpu_total,
        mem_total,
        rootfs_version,
    )
    return _resp(201, {"instance_id": instance_id, "status": "active", "az": az})


def deregister_host(instance_id):
    # terminate 会留下"控制面以为装好了、机器已不存在"的不可对账状态。持有【有效】lease
    # 时拒绝(过期 lease 不拦:那台机器上的操作早已死掉,不该永久挡住运维回收)。
    lease = image_lease.read(instance_id)
    if image_lease.is_held(lease):
        return _err(
            409, "IMAGE_OPERATION_IN_PROGRESS",
            f"host {instance_id} has an image operation in progress "
            f"({lease.get('active_image_operation_id')}); wait for it to finish or expire",
        )
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


def _backup_tenant_for_evacuation(tenant):
    """同步备份 terminating host 上的租户；双层校验失败一律 fail-closed。"""
    tid = tenant["id"]
    try:
        lambda_client = boto3.client("lambda")
        resp = lambda_client.invoke(
            FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
            InvocationType="RequestResponse",
            Payload=json.dumps({"tenant_id": tid, "pre_delete": True}).encode("utf-8"),
        )
        invoke_ok = resp.get("StatusCode", 500) == 200 and "FunctionError" not in resp
        if not invoke_ok:
            return False, "", "backup invoke failed (StatusCode/FunctionError)"
        try:
            raw = resp["Payload"].read()
            result = json.loads(raw) if raw else {}
        except Exception as e:  # noqa: BLE001 — 解析失败不能假称租户可恢复
            return False, "", f"backup response parse error: {e}"
        if result.get("success") is not True:
            return False, "", result.get("error") or "backup reported failure"
        backup_key = result.get("backup_key") or result.get("key") or ""
        if not backup_key:
            return False, "", "backup reported success without a restorable key"
        return True, backup_key, ""
    except Exception as e:  # noqa: BLE001 — invoke 异常必须回落 deleted，不能启动空盘
        return False, "", f"backup error ({e})"


def _write_terminated_tenant_state(tenant, ok, backup_key, err):
    tid = tenant["id"]
    if ok:
        # data.ext4 会随 host 销毁；只有本次备份 key 可用时才能交给 pending 恢复链。
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=(
                "SET #s = :s, restore_backup_key = :k, updated_at = :t "
                "REMOVE host_id, vm_num, phys_vm_num, host_port, guest_ip, "
                "host_private_ip"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "pending",
                ":k": backup_key,
                ":t": _now(),
            },
        )
        print(f"cleanup_terminated_host: tenant {tid} queued from {backup_key}")
        return True
    print(
        f"CRITICAL cleanup_terminated_host: tenant {tid} backup unusable ({err}); "
        "marking deleted to avoid a false recovery onto a blank disk"
    )
    # #501 — host 终止撤租户也是终态写入点:健康位由 health_check sweep 写而 sweep 只扫
    # running,不清就永久停在删除前的 up,已删租户伪装成健康在役租户误导排障。
    tenants_table.update_item(
        Key={"id": tid},
        UpdateExpression=(
            "SET #s = :s, updated_at = :t "
            "REMOVE vm_health, app_health, last_health_check"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "deleted", ":t": _now()},
    )
    return False


def _trigger_pending_placement(instance_id, evacuated):
    """把刚撤下来的 pending 租户推给 process_pending() 重新放置。

    为什么必须显式触发:process_pending() 全仓只有一个调用点(handler.py 的
    aws.autoscaling 非 terminate 分支,即 HostReady),handler.py:1028/1040 自己也写着
    "只由 HostReady 触发,没有保证会来的下一 tick"。而 refresh 是【先起后停】:
    替换机 InService(HostReady 发生)时租户还是 running,没什么可放置;等老机终止、
    本函数把租户翻成 pending 时,那个事件早过去了,不会再来第二次 → 租户永久悬挂在
    pending(数据在 S3 安全,但再也回不来)。所以这里补发一个 HostReady 形状的事件,
    顶层 dispatcher 原样路由到 process_pending(),handler.py 无需改动。

    异步(Event)且放在 complete_lifecycle_action 之后:放置耗时不得挤占 120s 钩子窗口,
    也不得让放置失败反过来拖住 ASG。失败只记日志不抛——租户已是 pending + 有可用
    restore_backup_key,下一次 HostReady 仍能兜回来。
    """
    if not evacuated:
        return
    try:
        boto3.client("lambda").invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            InvocationType="Event",  # fire-and-forget(同上方 pull_image 自调用)
            Payload=json.dumps(
                {
                    "source": "aws.autoscaling",
                    "detail-type": "EC2 Instance Launch Successful",
                    "detail": {
                        "_reason": f"evacuated {evacuated} tenant(s) from {instance_id}"
                    },
                }
            ).encode("utf-8"),
        )
        print(f"cleanup_terminated_host: placement triggered for {evacuated} tenant(s)")
    except Exception as e:  # noqa: BLE001 — 触发失败不能反过来拖住 ASG 钩子
        print(
            f"CRITICAL cleanup_terminated_host: placement trigger failed ({e}); "
            f"{evacuated} tenant(s) stay pending until the next HostReady"
        )


def _extend_terminate_hook(detail):
    """每撤离一个租户前给终止钩子续一次心跳。

    #510 —— 钩子的 HeartbeatTimeout 是 120s(真机 describe-lifecycle-hooks 实读),而每个
    租户的同步备份实测 6.2s、最坏可达 backup Lambda 的 SSM 上限 300s。也就是说约 19 个
    租户就会把 120s 走完,ASG 于此放行终止 → 剩下的租户连着数据盘一起消失,而一台 host 的
    容量是几百个租户。续心跳是唯一能把窗口撑开的手段:每次调用把 120s 重新计时,总上限由
    钩子的 GlobalTimeout 兜住(真机实读 12000s,足够几百个租户串行备份)。

    失败只记日志:心跳续不上最坏是回到修复前的 120s 窗口,不能反过来让撤离中断。
    """
    try:
        asg_client.record_lifecycle_action_heartbeat(
            LifecycleHookName=detail["LifecycleHookName"],
            AutoScalingGroupName=detail["AutoScalingGroupName"],
            InstanceId=detail["EC2InstanceId"],
        )
        return True
    except Exception as e:  # noqa: BLE001 — 续期失败不能中断撤离
        print(f"cleanup_terminated_host: heartbeat failed ({e}); 窗口回落到 120s")
        return False


# 单次 Lambda 调用留给撤离的墙钟预算。api Lambda 超时 900s;留 300s 余量给收尾
# (host 行更新、complete_lifecycle_action、放置触发),超出就自调用续跑。
_EVACUATE_BUDGET_SEC = 600


def _continue_evacuation(event, remaining):
    """预算用尽但还有租户没撤 → 异步自调用同一个事件接着撤。

    为什么可以直接重投同一个事件:已撤离的租户在 _write_terminated_tenant_state 里被
    REMOVE 掉了 host_id,备份失败的被标 deleted,两者都不再匹配下一轮的
    `host_id = :h AND status <> deleted` —— 续跑天然只会捡到还没碰过的,幂等由数据形状
    保证,不需要额外游标。
    """
    try:
        boto3.client("lambda").invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            InvocationType="Event",
            Payload=json.dumps(event).encode("utf-8"),
        )
        print(f"cleanup_terminated_host: budget 用尽,已自调用续撤 {remaining} 个租户")
        return True
    except Exception as e:  # noqa: BLE001
        print(
            f"CRITICAL cleanup_terminated_host: 续跑自调用失败 ({e});"
            f" 仍有 {remaining} 个租户未撤离,它们会停在 running 且 host_id 指向已销毁实例"
        )
        return False


def cleanup_terminated_host(event):
    """Called by termination lifecycle hook — cleanup DynamoDB then complete hook."""
    detail = event["detail"]
    instance_id = detail["EC2InstanceId"]
    print(f"cleanup_terminated_host: {instance_id}")

    # terminating host 的数据盘会随实例销毁；逐租户备份，避免例行 refresh 删除活租户。
    tenants = tenants_table.scan(
        FilterExpression="host_id = :h AND #s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":h": instance_id, ":d": "deleted"},
    ).get("Items", [])
    evacuated = 0
    started = time.monotonic()
    for idx, t in enumerate(tenants):
        # #510 —— 预算检查放在【动手之前】:宁可把剩下的交给续跑,也不要开始一个注定被
        # Lambda 超时打断的备份(那会留下既没备份完、状态也没落定的租户)。
        if idx and time.monotonic() - started > _EVACUATE_BUDGET_SEC:
            _continue_evacuation(event, len(tenants) - idx)
            # 不放行钩子:心跳已经把窗口撑开,让续跑那一轮撤完再放行。
            _trigger_pending_placement(instance_id, evacuated)
            return
        _extend_terminate_hook(detail)
        ok, backup_key, err = _backup_tenant_for_evacuation(t)
        if _write_terminated_tenant_state(t, ok, backup_key, err):
            evacuated += 1


    # Delete host
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "deleted", ":t": _now()},
    )
    print(f"cleaned up host {instance_id}, {len(tenants)} tenants processed")

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

    # 放在钩子放行【之后】:见 _trigger_pending_placement 的说明。
    _trigger_pending_placement(instance_id, evacuated)


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

    # 租户(tenant_service.py:1536)和 rebuild 采用(:2705)都读它。注意这是异步
    # send_command,此刻文件可能还没在盘上换完(set -eu + .tmp→mv 保证要么换成功
    # 要么整段失败,不会半成品;但本函数不等它跑完)。**关键的防谎报在采用侧**:
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


# (openclaw-{rootfs,data-template,immutable}-<VER>.ext4.gz)+ manifest.json 版本指针。
# 快照【记录】整个 deployment/(全记不漏),但 pull_image 拉到 host 时【只拉镜像盘那类】。
# deployment/scripts/(host-agent/route_ops/launch-vm 等)由 init-host.sh 开机各自 aws s3 cp
# 独立拉,不经此路径(owner 2026-07-17:pull_image 本次只管系统镜像,脚本不在范围);
# deployment/{edge,litellm,monitoring}/ 是别组件的部署物,同样不灌 microVM host。
_HOST_PULL_PREFIXES = ("deployment/rootfs/",)

# 仍加一道字符集白名单做纵深防御:安全域宁可两道)。只允许文件名常见安全字符
# [A-Za-z0-9._-],挡 shell 元字符/路径分隔符;不合格拒绝整个快照(fail-loud,不装 live)。
_SAFE_DISK_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# (YYYY-MM-DDTHH:MM:SSZ)。API 侧先校验格式:非法 → 400(参数错),合法但 DB 无 → 404
# (快照不存在)。别让乱输入默默查 DB 走成 404,误导调用方"快照不存在"(owner 2026-07-14)。
_SNAPSHOT_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Host agent normally reconciles slots.json every 15s. A destructive global delete only trusts
# a mirror refreshed within this window; missing/stale evidence fails closed. The generous
# default tolerates transient throttling while still making a stopped/old agent visible.
_IMAGE_SLOTS_MIRROR_MAX_AGE_S = int(os.environ.get("IMAGE_SLOTS_MIRROR_MAX_AGE_S", "120"))
_DELETE_GATE_STALE_S = int(os.environ.get("IMAGE_SNAPSHOT_DELETE_STALE_S", "300"))

# 但 API 直接收任意文本)。label 会进 console 显示 + 日志,故白名单校验:长度 ≤128 + 只允许
# 文件名常见安全字符 [A-Za-z0-9._-],挡 shell 元字符/空格(纵深防御,不裸存未净化的值)。
_SNAPSHOT_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")

# openclaw-{rootfs,data-template,immutable}-<VER>.ext4.gz。snapshot pull 要按此识别
# 出镜像盘,拉下来解压成 launch-vm 认的【扁平】名 openclaw-<kind>.ext4;非镜像(脚本)
# 直接落原路径不解压。
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


# 通过后把镜像装到这里的扁平名 openclaw-<kind>.ext4 → launch-vm 不用改就能起新版。
_LIVE = "/data/firecracker-assets"

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


def _commit_lines(f, snapshot_time=None):
    """Phase2 循环②(commit):把已 stage 好的文件 mv/cp 到最终位置。全是同盘原子 rename
    (镜像盘)或 cp(脚本),极快、窗口极小。
    · 镜像盘:
      - #394 传了 snapshot_time → mv 到【不可变版本目录】
        /data/firecracker-assets/versions/<snapshot_time>/openclaw-<kind>.ext4。
        装进版本目录【不】改变任何 VM 的启动版本 —— 启动版本只由 slots.json 指针决定,
        故装 canary 期间同 host 的 live 租户完全不受影响(ADR §11.1)。
      - 未传(兼容路径)→ 保持历史行为 mv 到扁平 live 位。
    · 脚本/manifest:mv "$ARCH"/staged-<base> → init-host 映射的 dest(.sh chmod+chown;
      常驻 .py service 装文件但不自动重启,只提示)。
    · adot-config.yaml(dest=None):stage 段已跳过,这里也跳过。
    失败 INSTALL_MV_FAILED exit1。注:装版本目录时 live 指针尚未改,失败不影响存量。"""
    q = shlex.quote
    path = f["path"]
    base = path.rsplit("/", 1)[-1]
    kind = _disk_kind_of(f)
    if kind is not None:
        staged = _staged_disk_path(kind)
        if snapshot_time:
            dest_disk = image_slots.disk_path(snapshot_time, kind)
        else:
            dest_disk = f"{_LIVE}/openclaw-{kind}.ext4"
        return [
            f"BASE={q(base)}",
            f'echo "[commit] $BASE → {dest_disk}"',
            f'mkdir -p "$(dirname {q(dest_disk)})"',
            f'mv {staged} {q(dest_disk)} '  # 同盘原子 rename
            f'|| {{ _perr "INSTALL_MV_FAILED mv $BASE to {dest_disk} failed"; exit 1; }}',
        ]
    dest, is_service = _script_live_dest(path)
    # 一目录,才能事后核对"这套盘是哪版")。扁平 live 位的 manifest.json 由 promote 时另写。
    if snapshot_time and dest is not None and path.endswith("rootfs/manifest.json"):
        dest = image_slots.manifest_path(snapshot_time)
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


def _mirror_canary_slot(instance_id, snapshot_time, live_snapshot_time=None):
    """#394 —— canary 装好后把槽位投影写到 host 记录(create-tenant 准入读它)。

    投影 canary + generation 自增;host 上的 slots.json 才是真值(ADR §4.2),这是控制面副本。
    best-effort(不抛):写失败时 create-tenant 读不到 canary → 返回 CANARY_NOT_READY 让调用方
    重试(fail-closed,不会误把 live 当 canary 给出去),故不值得让 pull 结果失败。
    live_snapshot_time(self-heal 时传):旧 host 刚把扁平 live 迁进版本目录、host 侧已解析
    slots.live;此处把控制面镜像的 live 一并回填(仅当当前镜像 live 为空,避免覆盖已有 live)。
    否则镜像 live 仍 null → promote 出 previous_live=null / 前端 undefined。
    """
    # 组装 SET:canary + generation 恒写;live 仅在传入且当前为空时补(if_not_exists 防覆盖)。
    set_parts = [
        "image_slots.#c = :s",
        "image_slots.#g = if_not_exists(image_slots.#g, :zero) + :one",
    ]
    names = {"#c": "canary", "#g": "generation"}
    values = {":s": snapshot_time, ":zero": 0, ":one": 1}
    if live_snapshot_time:
        set_parts.append("image_slots.#l = if_not_exists(image_slots.#l, :lv)")
        names["#l"] = "live"
        values[":lv"] = live_snapshot_time
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attribute_exists(image_slots)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:  # noqa: BLE001 — 含 image_slots 尚不存在(首次)的 CCF
        # 首次:整个 image_slots map 还不存在 → 建一份(self-heal 时连 live 一起建)。
        try:
            hosts_table.update_item(
                Key={"instance_id": instance_id},
                UpdateExpression="SET image_slots = :m",
                ExpressionAttributeValues={
                    ":m": {"canary": snapshot_time, "generation": 1,
                           "live": live_snapshot_time or None, "previous_live": None},
                },
            )
        except Exception as e:  # noqa: BLE001
            print(f"[pull] WARN mirror canary slot to host record failed: {e}")


# 提交点 fence 的有界续租窗口(秒):conditional renew 成功后,该窗口内无人能接管 lease
# 的 python 进程里、紧贴 os.rename 的前一条语句(无 shell 级调度缝),该常量作为续租秒数传入。
# 60s 远大于 rename 耗时,又远小于 lease TTL(1200s),不影响正常接管时序。
_COMMIT_FENCE_RENEW_S = 60


def _slots_commit_lines(slot, snapshot_time, hosts_table, region, instance_id, job_id=None):
    """#394 —— host 侧就地 read-modify-write slots.json(唯一提交点)+ 回写 DDB 镜像。

    为什么在 host 上算而不是 Lambda 预先算好整份 JSON:slots.json 的当前值是 host 本地
    真值(ADR §4.2 "Host slots.json 是启动路径本地真值"),Lambda 读到的可能已过期;
    盲写整份会把并发/上一轮的 generation 与 previous_live 覆盖掉。故把迁移逻辑用 python3
    在 host 上对【刚读到的】那份施加 —— 与 core/image_slots.apply_pull 语义保持一致:
      · canary:只填 canary,live/previous_live 不动;
      · live:旧 live 落 previous_live(仅当版本不同,重装同版保留回滚锚点);
      · generation +1。
    并发保护由外层 pull.lock(flock)+ 控制面 image lease 提供;此处只保证单次写原子。
    写坏/失败一律 _perr + exit 1,绝不留半个指针。
    """
    q = shlex.quote
    # 先备好 tmp 文件,再做条件续租(boto3 conditional update-item:owner==本 job 且 until>now →
    # 同写续租到 now+窗口),【紧接着】os.rename。fence 与 rename 是相邻 python 语句,无 shell 级
    # 调度缝;续租成功后接管者的 acquire(要求 until<=now)在窗口内必失败,且 worker 全程持
    # pull.lock 使并发 worker 无法同时进 commit 段。job_id/region 为空(扁平兼容路径)→ 跳过 fence,
    # 保持既有行为(那些路径不走版本-lease 模型)。
    _fenced = bool(job_id and region and hosts_table)
    fence_py = ""
    if _fenced:
        fence_py = (
            "import boto3,time as _t\n"
            "_c=boto3.client('dynamodb',region_name=sys.argv[4])\n"
            "_now=int(_t.time()); _renew=_now+int(sys.argv[6])\n"
            "try:\n"
            "    _c.update_item(TableName=sys.argv[5],\n"
            "        Key={'instance_id':{'S':sys.argv[7]}},\n"
            "        UpdateExpression='SET image_lease_until = :r',\n"
            "        ConditionExpression='active_image_operation_id = :j AND image_lease_until > :n',\n"
            "        ExpressionAttributeValues={':j':{'S':sys.argv[8]},':n':{'N':str(_now)},':r':{'N':str(_renew)}})\n"
            "except Exception as e:\n"
            "    raise SystemExit('COMMIT_FENCED lease not held/renewable at commit (superseded/expired): %s' % e)\n"
        )
    py = (
        "import json,os,sys\n"
        "p=sys.argv[1]; slot=sys.argv[2]; snap=sys.argv[3]\n"
        "s={'generation':0,'live':None,'canary':None,'previous_live':None}\n"
        "if os.path.exists(p):\n"
        "    with open(p) as fh: cur=json.load(fh)\n"
        "    if not isinstance(cur,dict): raise SystemExit('slots.json not an object')\n"
        "    s['generation']=int(cur.get('generation') or 0)\n"
        "    for k in ('live','canary','previous_live'): s[k]=cur.get(k) or None\n"
        "if slot=='canary':\n"
        "    s['canary']=snap\n"
        "else:\n"
        "    if s['live'] and s['live']!=snap: s['previous_live']=s['live']\n"
        "    s['live']=snap\n"
        "s['generation']=s['generation']+1\n"
        "tmp=p+'.tmp'\n"
        "with open(tmp,'w') as fh:\n"
        "    json.dump(s,fh,sort_keys=True,separators=(',',':')); fh.flush(); os.fsync(fh.fileno())\n"
        # ↓↓↓ fence 就在这里:tmp 已就绪,续租成功【紧接着】rename;二者相邻,无中间语句。
        + fence_py +
        "os.rename(tmp,p)\n"
        "d=os.open(os.path.dirname(p),os.O_RDONLY); os.fsync(d); os.close(d)\n"
        "print(json.dumps(s))\n"
    )
    # fence 需要的额外 argv:region, hosts_table, renew_seconds, instance_id, job_id。
    fence_args = (
        f" {q(region)} {q(hosts_table)} {q(str(_COMMIT_FENCE_RENEW_S))} "
        f"{q(instance_id)} {q(job_id)}"
        if _fenced else ""
    )
    out = [f"mkdir -p {q(image_slots.LIVE_ROOT)}"]
    out += [
        f"SLOTS_NEW=$(python3 - {q(image_slots.SLOTS_FILE)} {q(slot)} {q(snapshot_time)}"
        f"{fence_args} <<'SLOTSEOF'\n"
        f"{py}SLOTSEOF\n"
        f') || {{ _perr "SLOTS_WRITE_FAILED could not update slots.json ({slot}) — see COMMIT_FENCED if lease lost"; exit 1; }}',
        '_p "slots.json updated: $SLOTS_NEW"',
    ]
    # 与 host 真值漂移:之前只由 Lambda 增量 patch canary,live/generation/previous_live 都
    # 会对不上,导致 promote 误报 CANARY_CHANGED、UI 显示 live=null)。host 角色有 hosts
    # 表 UpdateItem 权(心跳在用)。用 python3 把 SLOTS_NEW(纯 JSON)转成 DDB M 格式写入。
    # best-effort(|| true):镜像回写失败不该让已成功的 slots 提交判失败;下次操作会再纠。
    out += image_slots.slots_mirror_writeback_lines(hosts_table, region, instance_id)
    return out


def _slots_selfheal_lines(flat_snapshot_time):
    """#394 —— 旧版 host self-heal:pull canary 前,若 slots.live 未解析(旧扁平布局:
    live=null / 无 slots.json),把当前扁平 live 三盘【硬链接】进 versions/<flat_snap>/ 并
    把 slots.live 指向它。之后 canary/promote/rollback 才有合法 live 锚点(否则 promote 出
    previous_live=null 的半状态,前端显示 undefined;rollback 无锚点)。

    为什么用硬链接:扁平盘与版本目录盘在【同一文件系统】,`ln`(非 cp)= 同 inode、零额外
    占盘、瞬时;运行中 VM 仍持扁平路径 inode 不受影响;rootfs 只读挂载,共享 inode 无写偏斜。
    幂等:versions/<flat_snap>/ 的盘已在 + slots.live 已解析 → 整段跳过(不重复链、不改指针)。
    fail-loud:扁平盘缺失(真空 host,无 live 可迁)→ 不建半个版本目录,交给上层 preflight
    (这里只在扁平盘齐全时迁;缺盘则 slots.live 仍空,canary 装完 live 仍走扁平回落,不恶化)。
    """
    q = shlex.quote
    root = image_slots.LIVE_ROOT
    snap = flat_snapshot_time
    vdir = image_slots.version_dir(snap)
    slots = image_slots.SLOTS_FILE
    # python3 就地做:读 slots.live;为空则(扁平三盘齐全时)硬链接进版本目录 + 写 slots.live。
    # rootfs/data-template 必须齐(immutable 可选,与 launch-vm 一致)。
    py = (
        "import json,os,sys\n"
        "root,snap,vdir,slots=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]\n"
        "cur={}\n"
        "if os.path.exists(slots):\n"
        "    with open(slots) as fh: cur=json.load(fh)\n"
        "    if not isinstance(cur,dict): raise SystemExit('slots.json not an object')\n"
        "if cur.get('live'):\n"
        "    print('live already resolved: '+cur['live']); raise SystemExit(0)\n"
        # live 未解析 → 检查扁平盘
        "flat={'rootfs':root+'/openclaw-rootfs.ext4','data-template':root+'/openclaw-data-template.ext4','immutable':root+'/openclaw-immutable.ext4'}\n"
        "if not (os.path.exists(flat['rootfs']) and os.path.exists(flat['data-template'])):\n"
        "    print('no flat live disks to migrate; leaving live unresolved'); raise SystemExit(0)\n"
        "os.makedirs(vdir,exist_ok=True)\n"
        "for kind,src in flat.items():\n"
        "    if not os.path.exists(src): continue\n"
        "    dst=vdir+'/openclaw-'+kind+'.ext4'\n"
        "    if not os.path.exists(dst):\n"
        "        os.link(src,dst)\n"   # 硬链接:同 inode 零拷贝
        # manifest:有就 cp 进版本目录(版本目录自洽);扁平位 manifest 名固定
        "m=root+'/manifest.json'\n"
        "if os.path.exists(m) and not os.path.exists(vdir+'/manifest.json'):\n"
        "    import shutil; shutil.copy2(m,vdir+'/manifest.json')\n"
        # 原子写 slots.live(tmp+fsync+rename+fsync parent),不动 canary/previous_live
        "cur.setdefault('generation',0); cur['live']=snap\n"
        "cur.setdefault('canary',None); cur.setdefault('previous_live',None)\n"
        "cur['generation']=int(cur.get('generation') or 0)+1\n"
        "tmp=slots+'.tmp'\n"
        "with open(tmp,'w') as fh:\n"
        "    json.dump(cur,fh,sort_keys=True,separators=(',',':')); fh.flush(); os.fsync(fh.fileno())\n"
        "os.rename(tmp,slots)\n"
        "d=os.open(os.path.dirname(slots),os.O_RDONLY); os.fsync(d); os.close(d)\n"
        "print('migrated flat live → '+snap)\n"
    )
    return [
        f"mkdir -p {q(root)}",
        f"SELFHEAL=$(python3 - {q(root)} {q(snap)} {q(vdir)} {q(slots)} <<'HEALEOF'\n"
        f"{py}HEALEOF\n"
        f') || {{ _perr "SELFHEAL_FAILED could not migrate flat live to version dir"; exit 1; }}',
        '_p "self-heal: $SELFHEAL"',
    ]


def _reset_status_cmd(
    hosts_table, region, instance_id, status, snapshot_time=None, job_id=None, rootfs_version="",
):
    """host 侧写 hosts 表的一条 aws dynamodb update-item(host 实例角色已有 UpdateItem
    权限,host-agent 心跳在用)。失败时只复位 status→prev;成功时复位 + 写 snapshot_time
    (仅成功才记版本,不谎报)。`|| true`:DDB 写失败不该让整条 SSM 判失败(状态字段是
    旁路,主功能是装 live)。各值 shell-quote 防注入。
    —— DynamoDB 模糊失败重试(客户端超时但服务端已写)可能在新 job CAS 后把状态覆盖回旧值;
    条件写关死:非当前 owner 的复位 CCF 失败(被 `|| true` 吞,无害)。
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
    job_id, rootfs_version="", slot=None, flat_live_snapshot_time=None,
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
    # 在新 job CAS 后覆盖回旧状态)。脚本走到复位处已过 fence(status==upgrading + owner==job),故
    # 条件通常满足;真被新 job 抢占时 CCF(被 `|| true` 吞,不误覆盖)。
    #  · 没置过 upgrading,"复位"会凭空把别人的维护门解除;
    #  · host.snapshot_time 语义是"本机 live 版本",canary 装好并没换 live,写了就是谎报。
    # 故 canary 的两个 reset 函数体降级成 `:`(shell no-op),脚本骨架、fence、marker、
    # 进度输出全部保持不变(改动面最小),只是不发那两条 DDB update-item。
    if slot == image_slots.SLOT_CANARY:
        reset_fail = ":"
        reset_ok = ":"
    else:
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
        # 全部装完 mv 到 _LIVE(同盘原子 rename)。log 仍在 /tmp/<job_id>.txt(不变)。
        f"ARCH={_STAGE}",
        # 若 _reset_ok 的 DDB 写因 `|| true` 静默失败(status 没复位、owner 没变),延迟重复 worker
        # 会通过 status==upgrading+owner fence → 本会重装 live(踩幂等/no-data-loss)。fence 后加
        # 一道 marker 检查:marker==本 job → 本 job 已装完,绝不重装,只补跑 _reset_ok 修 DDB 后让位。
        f"DONE_MARKER={_LIVE}/.pull-last-done",
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
        # 不可吞】→ 同一个 job 可能有第二个 worker 并发跑(入口 CAS 只挡不同 pull,挡不住同 job
        # 重投)。故 host 侧必须:① host 级固定 flock(不带 job_id,否则不同 job 各拿一把不互斥)
        # ② job fencing:抢锁后强一致读 DDB pull_command_id 校验 == 本 job_id(非 owner 让位)。
        # LOCKED 门:抢到锁+确认 owner 才置 1;未持锁的 loser 绝不写共享进度文件终态(污染 winner)。
        #
        # trap 必须在任何可能失败步骤(含 flock/清空/mkdir)【之前】注册,否则清理失败不写 FAIL
        # 不复位 → 卡 upgrading。trap 仅 LOCKED=1(winner)才写终态:rc!=0 且无具体 ERROR(HAS_ERR=0)
        # 补 UNKNOWN(否则覆盖真错码);写 FAIL;INSTALLING=0(没动 live)才 rm 暂存 + 复位 prev
        # (动了 live 留 upgrading 给 ops,不自动还原/复位)。loser(LOCKED=0)只 echo stderr,trap 静默。
        # (best-effort,便于运维读状态)。但【真正的重装防线是进 phase2 前就落的 installing marker】
        # (见 phase2)——盘满/掉电/SIGKILL 会让 trap 写不成,重复 worker 靠 installing marker(非 done)
        # 就拒绝重装。INSTALLING=0(live 未碰)仍 rm 暂存 + 复位 prev。marker 原子写(tmp+mv)。
        f'trap \'rc=$?; if [ "$rc" != 0 ] && [ "$LOCKED" = 1 ]; then [ "$HAS_ERR" = 0 ] && _p "ERROR:UNKNOWN unexpected exit rc=$rc - see SSM stderr"; _p "FAIL"; if [ "$INSTALLING" = 0 ]; then rm -rf "$ARCH"; _reset_fail; else printf "%s" {q(job_id)}:failed > "$DONE_MARKER".tmp && mv "$DONE_MARKER".tmp "$DONE_MARKER"; fi; fi\' EXIT',
        # -n 直接失败:成功路径在锁内复位 active(见文末),此后新 pull 可 CAS active→upgrading 并
        # fire 后继 worker——若后继用 -n 会抢锁失败退出不复位 → 卡 upgrading(round3 老 bug)。改
        # 阻塞等:持锁 worker 完成(含复位)释锁后,后继拿到锁再走 fence 重校验。真held 超时 → 让位。
        f'exec 9>{_LIVE}/pull.lock',
        'flock -w 120 9 || { echo "[pull] LOCK_HELD another pull worker holds the lock >120s; abort" >&2; exit 1; }',
        # ② fence:强一致读 DDB【status + pull_command_id】,必须 status==upgrading 且 owner==本 job。
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
        # 陈旧 worker 由控制面 image lease + fence_epoch 拦(ADR §4.8),不靠 host.status。
        # owner 门对两者都保留(pull_command_id 只在 live 路径写,canary 用 lease owner,
        # 故 canary 也跳过 owner 门 —— 见下面 CANARY_FENCE 注释)。
        *([] if slot == image_slots.SLOT_CANARY else [
            'if [ "$ST" != upgrading ]; then echo "[pull] STALE_JOB host status=$ST (not upgrading); superseded/completed, abort" >&2; exit 1; fi',
            f'if [ "$OWNER" != {q(job_id)} ]; then echo "[pull] STALE_JOB job={job_id} owner=$OWNER; superseded, abort" >&2; exit 1; fi',
        ]),
        # Unified lease fence for both slots. Owner equality alone is insufficient because an
        # expired lease keeps its owner field; after expiry a reconciler may declare the Job
        # recoverable and another operation may take over. Old workers must not commit then.
        f'LEASE=$(aws dynamodb get-item --table-name {q(hosts_table)} --region {q(region)} '
        f'--key {q(json.dumps({"instance_id": {"S": instance_id}}))} --consistent-read '
        f'--query "[Item.active_image_operation_id.S, Item.image_lease_until.N]" --output text 2>/dev/null) || LEASE=""',
        'LEASE_OWNER=$(printf "%s" "$LEASE" | cut -f1); LEASE_UNTIL=$(printf "%s" "$LEASE" | cut -f2)',
        f'if [ "$LEASE_OWNER" != {q(job_id)} ]; then echo "[pull] STALE_JOB lease owner=$LEASE_OWNER job={job_id}; superseded, abort" >&2; exit 1; fi',
        'if [ -z "$LEASE_UNTIL" ] || [ "$LEASE_UNTIL" -le "$(date +%s)" ]; then echo "[pull] STALE_JOB lease expired; abort" >&2; exit 1; fi',
        "LOCKED=1",  # 抢到锁 + 确认 upgrading + owner → 自此拥有 $ARCH/status 所有权,trap 可写终态/清理/复位
        # 的 job==本 job,说明本 job 的上一个 worker 已【终结】(装完 or phase2 已动 live 后失败),
        # 延迟重复 worker 【绝不】重装(否则重复解压覆盖 / 在半写坏 live 上再装,踩幂等/no-data-loss):
        #   · done   → 上一 worker 装成功(只是 _reset_ok DDB 写 `|| true` 静默失败没复位)→ 补
        #              _reset_ok 修 DDB + 写 SUCCESS 终态,释锁让位。
        #   · failed → 上一 worker phase2 已动 live 后失败(live 可能半写坏)→ 绝不重装,写 FAIL 终态
        #              (host 留 upgrading 给 ops),释锁让位。不自动还原(V1)。
        'MJOB=""; MSTATE=""',
        'if [ -f "$DONE_MARKER" ]; then MRAW=$(cat "$DONE_MARKER" 2>/dev/null); '
        'MJOB=${MRAW%%:*}; MSTATE=${MRAW#*:}; fi',
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
    ]
    # 跳过下载/解压,直接翻 slots 指针(秒级)。这让"回滚 = pull 老版到 live"零下载:老版目录
    # 已在盘上(promote/上次 pull 装的,reclaim 之前一直保留)→ 只翻指针。半装目录(下载一半盘满、
    # 无 .complete)判不完整 → 落到下面正常重下自愈(fail-safe,绝不翻半盘)。兼容扁平路径(slot=None)
    # 没有版本目录概念,不走快路径。快路径成功后走与文末【完全相同】的收尾(slots 提交 + 终态 + 复位)。
    if slot:
        fast = ['_p "checking if version already installed on disk (fast-path)"']
        fast += image_slots.version_complete_check_lines(snapshot_time)
        fast.append('if [ "$VER_COMPLETE" = 1 ]; then')
        fast.append(f'  _p "fast-path: version {snapshot_time} already complete on disk; flipping {slot} pointer (no download)"')
        fast.append(f'  echo "[pull] fast-path: {snapshot_time} already installed; skip download, flip {slot}"')
        # canary self-heal(与慢路径同):翻 canary 指针前若 live 未解析先迁扁平 live。
        if slot == image_slots.SLOT_CANARY and flat_live_snapshot_time:
            fast += ["  " + ln for ln in _slots_selfheal_lines(flat_live_snapshot_time)]
        # 唯一提交点:翻 slots 指针(原子)。与慢路径 phase2c 同一函数,语义一致。
        fast += ["  " + ln for ln in _slots_commit_lines(slot, snapshot_time, hosts_table, region, instance_id, job_id)]
        # 收尾:done marker + SUCCESS + 复位 + 释锁(与文末慢路径完全相同,持锁内)。
        fast.append(f'  printf "%s" {q(job_id)}:done > "$DONE_MARKER".tmp && mv "$DONE_MARKER".tmp "$DONE_MARKER"')
        fast.append('  _p "SUCCESS"')
        fast.append("  _reset_ok")
        fast.append("  exec 9>&-")
        fast.append("  LOCKED=0")
        fast.append(f'  echo "[pull] fast-path done: {slot}={snapshot_time}, no download"')
        fast.append("  exit 0")
        fast.append("fi")
        lines += fast
    lines += [
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
    # INSTALLING=0,不一致 exit1 → trap 复位 status→prev,live 未碰)。
    lines.extend(_manifest_consistency_lines(host_files))
    # backup 占 ~18GB 峰值且现无用途。装 live 失败 → 让他 fail(host 留 upgrading,运维介入),
    # 不做自动还原。省空间 + 缓解盘满。
    lines.append('_p "phase1 OK: installing to live (no backup)"')
    lines.append('echo "[pull] phase1 OK — install to live (no backup)"')
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
    # 关键:phase2 失败标记不能只靠 EXIT trap —— 盘满/掉电/SIGKILL(正是预期失败场景)会让 trap
    # 写不成 marker,延迟重复 worker 就会在半装的 live 上重装。故进 commit、动第一个盘【之前】先落
    # installing marker;成功后替换成 done(见文末)。重复 worker 见任何非 done 一律拒绝重装。
    lines.append(f'printf "%s" {q(job_id)}:installing > "$DONE_MARKER".tmp && mv "$DONE_MARKER".tmp "$DONE_MARKER"')
    # ── 循环② commit:逐个 mv/cp 暂存区 → 目标位置(同盘原子 rename,极快)──
    # 兼容模式(slot 为空)保持历史扁平 live 位。
    dest_desc = f"version dir {snapshot_time}" if slot else "live"
    for i, f in enumerate(host_files, 1):
        base = f["path"].rsplit("/", 1)[-1]
        lines.append(f'_p "phase2b [{i}/{n}]: committing {base} to {dest_desc}"')
        lines.extend(_commit_lines(f, snapshot_time if slot else None))
    # 任何 VM 的启动版本都没变:装 canary 完全不影响存量 live;装 live 失败也不会让存量
    # 租户拿到半套盘(ADR §4.1"提交点只有一个小文件 rename")。
    if slot:
        # (先于翻指针)。它是"这套版本目录已装齐"的唯一信号,快路径据它判断"本地已完整→秒级
        # 翻指针"。半装目录(装到一半盘满/掉电)没有标记 → 下次 pull 快路径不认它、走重下自愈。
        version_disks = [
            image_slots.disk_path(snapshot_time, _disk_kind_of(f))
            for f in host_files if _disk_kind_of(f) is not None
        ]
        lines.append('_p "phase2b.9: writing .complete marker (version dir fully installed)"')
        lines.extend(image_slots.write_complete_marker_lines(snapshot_time, version_disks))
        # 并解析 slots.live(仅当 live 未解析且有已知的扁平 live 版本时)。否则 promote 会产出
        # previous_live=null 的半状态(前端 undefined),rollback 无锚点。幂等,已迁则跳过。
        if slot == image_slots.SLOT_CANARY and flat_live_snapshot_time:
            lines.append('_p "phase2b.5: self-heal legacy flat live → version dir (if unresolved)"')
            lines.extend(_slots_selfheal_lines(flat_live_snapshot_time))
        lines.append(f'_p "phase2c: updating slots.json ({slot} → {snapshot_time})"')
        lines.extend(_slots_commit_lines(slot, snapshot_time, hosts_table, region, instance_id, job_id))
    setting = json.dumps(
        {"snapshot_time": snapshot_time, "staged_dir": _STAGE, "file_count": n}
    )
    lines.append(f"printf '%s' {q(setting)} > {_LIVE}/setting.json")
    # _reset_ok 的 DDB 写 `|| true` 静默失败(status 没复位),延迟重复 worker 过 fence 后会在
    # LOCKED=1 处读到 marker job==本 job(done)→ 不重装,只补 _reset_ok 修 DDB(见 fence 后那段)。原子写。
    lines.append(f'printf "%s" {q(job_id)}:done > "$DONE_MARKER".tmp && mv "$DONE_MARKER".tmp "$DONE_MARKER"')
    # 装完 live 即成功 → 写终态 SUCCESS + 复位 active,【都在持锁内】(LOCKED=1 保护),最后才释锁。
    lines.append('_p "SUCCESS"')  # 终态先落(progress tail 判完成),持锁时写
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


def list_image_versions(query_params=None):
    """#337(原#217 GET /snapshots,改名避免与 /images 列镜像文件混淆)— GET /list_image_versions:
    列快照表条目的元数据(snapshot_time + label + file_count + status),供 console 让运维选不同
    时间点去 pull。按 snapshot_time 倒序(最新在前)。不回 files 大 JSON(那是 pull 时才逐文件读);
    表未配置 → 503 fail-loud。

    面绝不该列出已下架版本(pull 也会拒);Image Snapshot 面板传 true 看全量,这样"某 host 槽位
    仍引用、但快照记录被误软删"的版本仍会出现(带 deleted 标记),不会因过滤而在 UI 里凭空消失
    (那会导致 live 版本没有徽标——本次修复的 bug)。每条带 `status`,前端据此标记 + 过滤。"""
    if version_snapshots_table is None:
        return _err(503, "NOT_CONFIGURED", "VERSION_SNAPSHOTS_TABLE not configured")
    show_deleted = str((query_params or {}).get("show_deleted", "")).lower() == "true"
    # 运维选不到那些版本去 pull/回滚)。删除保护扫描已翻页,目录查询同样必须翻。
    items = []
    _kw = {}
    while True:
        _resp_scan = version_snapshots_table.scan(**_kw)
        items.extend(_resp_scan.get("Items") or [])
        _lek = _resp_scan.get("LastEvaluatedKey")
        if not _lek:
            break
        _kw["ExclusiveStartKey"] = _lek
    out = [
        {
            "snapshot_time": it.get("snapshot_time"),
            "label": it.get("label", ""),
            "file_count": int(it.get("file_count", 0)),
            "status": it.get("status") or "active",
        }
        for it in items
        if it.get("snapshot_time")
        and (show_deleted or it.get("status") != "deleted")
    ]
    out.sort(key=lambda s: s["snapshot_time"], reverse=True)  # 最新在前
    return _resp(200, out)


def _snapshot_still_referenced(snapshot_time):
    """#394 —— 删快照记录前的保护:该版本是否仍被任何 host 槽位或租户固定引用。

    返回 (referenced: bool, reason: str)。检查两处:
      · host 记录的 image_slots(live/canary/previous_live)—— 删了记录但 host 仍指向它,
        该 host rebuild/新建租户时会照 snapshot_time 去 pull,快照记录没了就拉不回(no-data-loss)。
      · 租户记录的 image_snapshot_time(canary 租户固定的版本)—— 删了记录那些租户 restart
        解析不到自己的版本目录。
    只读扫描,不改任何东西。任一命中即拒删(fail-closed)。

    只读第一页会漏掉后续页的引用 → 把仍被引用的版本误判"无人用"而软删 → host 丢失/恢复时
    拉不回在运行的版本(no-data-loss)。删除保护必须 fail-closed:宁可多扫几页,不可漏判。
    """
    # 1) host 槽位引用。Host slots.json 是权威真值；DDB mirror 只有带新鲜同步时间才可
    # 用于 destructive delete。host-agent 每轮心跳持续覆盖 mirror，写失败/agent 旧版/停机都会
    # 让时间戳老化并 fail-closed，而不是把 stale mirror 当成“无引用”。旧扁平 host 的
    # snapshot_time 也直接纳入保护。
    # codex NB1 —— 删除是破坏性且不可逆(软删后引用方拉不回)。引用扫描必须【强一致读】:
    # 默认最终一致 Scan 可能漏掉刚提交的引用(host 槽刚翻、租户刚固定)→ 误判无引用而软删在用
    # 版本(no-data-loss)。base table Scan 支持 ConsistentRead=True(GSI 不支持,此处扫主表)。
    kwargs = {
        "ProjectionExpression": (
            "instance_id, #st, snapshot_time, image_slots, image_slots_synced_at_epoch"
        ),
        "ExpressionAttributeNames": {"#st": "status"},
        "ConsistentRead": True,
    }
    mirror_now = int(time.time())
    while True:
        resp = hosts_table.scan(**kwargs)
        for h in resp.get("Items") or []:
            host_id = str(h.get("instance_id") or "")
            if not host_id or host_id.startswith("__") or h.get("status") == "deleted":
                continue
            if h.get("snapshot_time") == snapshot_time:
                return True, f"host {host_id} legacy live snapshot_time still points at it"
            slots = h.get("image_slots") or {}
            # Even a stale mirror that explicitly references the target is sufficient to block.
            # Freshness is only needed before concluding that the target is absent.
            for key in ("live", "canary", "previous_live"):
                if slots.get(key) == snapshot_time:
                    return True, f"host {host_id} slot '{key}' still points at it"
            synced_at = int(h.get("image_slots_synced_at_epoch") or 0)
            age = mirror_now - synced_at if synced_at else None
            if not synced_at or age < 0 or age > _IMAGE_SLOTS_MIRROR_MAX_AGE_S:
                return True, (
                    f"host {host_id} slot mirror is missing/stale"
                    f" (age={age!r}s); refusing destructive delete until host-agent reconciles"
                )
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    # 2) 租户固定引用(canary 租户)—— 翻页到底。同样强一致读(codex NB1):漏掉刚固定
    # 该版本的租户 = 删掉它在用的底盘。
    kwargs = {
        "FilterExpression": "image_snapshot_time = :s AND #st <> :deleted",
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {":s": snapshot_time, ":deleted": "deleted"},
        "ProjectionExpression": "id",
        "ConsistentRead": True,
    }
    pinned_ids = []
    while True:
        resp = tenants_table.scan(**kwargs)
        pinned_ids.extend(t["id"] for t in (resp.get("Items") or []))
        lek = resp.get("LastEvaluatedKey")
        if not lek or len(pinned_ids) >= 10:
            break
        kwargs["ExclusiveStartKey"] = lek
    if pinned_ids:
        return True, f"tenants still pin it: {pinned_ids[:10]}"
    # 那个 worker 会把已下架版本装进 live/canary(no-data-loss)。任何非终态 Job 命中即拒删。
    # codex B2 —— 再加 SUCCEEDED grace:刚提交成功但 mirror 回写失败的窗口内(≤ mirror 最大
    # 陈旧时间),mirror 尚未反映新指针,slots 扫描看不到 → 用 Job 兜底,等下轮心跳同步真值。
    active = image_jobs.active_jobs_for_snapshot(
        snapshot_time, success_grace_s=_IMAGE_SLOTS_MIRROR_MAX_AGE_S
    )
    if active:
        desc = [f"{j.get('job_id')}({j.get('state')}→{j.get('instance_id')})" for j in active[:5]]
        return True, f"in-flight pull(s) targeting it: {desc}"
    return False, ""


def delete_image_snapshot(body):
    """POST /delete-image-snapshot — 【软删】一条镜像快照记录(#394)。

    与 create-image-snapshot 对称:body `{snapshot_time}`(不用 path 带冒号的 ISO 时间,
    避开 path segment 编码坑,且与 create/pull 的参数风格一致)。
    不物理删除 DDB 记录,而是打 deleted 标记(status=deleted + deleted_at),对齐租户
    status=deleted 的软删范式:可审计、可恢复、不丢历史。list_image_versions 会过滤掉
    已软删的条目,pull-image 也拒用(见各自过滤)。同样不动 S3 镜像文件。
    删前 fail-closed 保护:该版本仍被任何 host 槽位或租户固定引用则拒删
    (409 IMAGE_VERSION_IN_USE),否则标了 deleted 会让引用方 pull/restart 拉不回。
    幂等:记录不存在 → 404;已是 deleted → 200(重复软删收敛,不报错)。operator+。"""
    # body 解析:与 create_image_snapshot 同款(容忍字符串/dict,标量/数组→400)。
    if isinstance(body, str):
        try:
            body = json.loads(body) if body.strip() else {}
        except (ValueError, TypeError):
            return _err(400, "VALIDATION", "body must be valid JSON")
    body = body or {}
    if not isinstance(body, dict):
        return _err(400, "VALIDATION", "body must be a JSON object")
    snapshot_time = (body.get("snapshot_time") or "").strip()
    if not snapshot_time:
        return _err(400, "VALIDATION", "snapshot_time required")
    if not _SNAPSHOT_TIME_RE.match(snapshot_time):
        return _err(400, "VALIDATION",
                    f"snapshot_time must be ISO8601 UTC (YYYY-MM-DDTHH:MM:SSZ); got {snapshot_time!r}")
    if version_snapshots_table is None:
        return _err(503, "NOT_CONFIGURED", "VERSION_SNAPSHOTS_TABLE not configured")
    existing = version_snapshots_table.get_item(
        Key={"snapshot_time": snapshot_time}, ConsistentRead=True
    ).get("Item")
    if not existing:
        return _err(404, "NOT_FOUND", f"snapshot {snapshot_time} not found")
    if existing.get("status") == "deleted":
        return _resp(200, {
            "message": "snapshot record already deleted",
            "snapshot_time": snapshot_time,
            "label": existing.get("label", ""),
            "status": "deleted",
        })
    ccf = version_snapshots_table.meta.client.exceptions.ConditionalCheckFailedException
    if existing.get("status") == "deleting":
        # Lambda hard-kill 可能发生在 ACTIVE→DELETING 之后。短窗口内拒绝并发删除；超过
        # stale 阈值则以 deletion_started_at 条件接管，先恢复 ACTIVE 再重新走完整保护流程。
        started = existing.get("deletion_started_at")
        try:
            age = time.time() - datetime.fromisoformat(
                str(started or "").replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            age = _DELETE_GATE_STALE_S + 1
        if age <= _DELETE_GATE_STALE_S:
            return _err(409, "DELETE_IN_PROGRESS",
                        f"snapshot {snapshot_time} deletion is already in progress; retry shortly")
        try:
            version_snapshots_table.update_item(
                Key={"snapshot_time": snapshot_time},
                UpdateExpression="SET #s = :active REMOVE deletion_started_at, deletion_owner",
                ConditionExpression="#s = :deleting AND deletion_started_at = :started",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":active": "active", ":deleting": "deleting", ":started": started
                },
            )
        except ccf:
            return _err(409, "DELETE_IN_PROGRESS",
                        f"snapshot {snapshot_time} deletion ownership changed; retry shortly")
        existing = {**existing, "status": "active"}
    # 删除安全依赖 Job 事务作为 Pull admission 的线性化点；未部署 Job 表时无法关闭
    # scan→delete 间插入新 Pull 的窗口，因此必须 fail-closed，不能退化为空引用集合。
    if not image_jobs.is_enabled():
        return _err(503, "JOB_RECORD_UNAVAILABLE",
                    "IMAGE_JOBS_TABLE is required for safe snapshot deletion")

    original_had_status = "status" in existing
    original_status = existing.get("status", "active")
    # deleted / 引用存在回滚)都条件在【本 token】上。否则仅凭 #s=deleting 做条件:A 置 DELETING
    # 后卡死→B 超时接管(恢复 active→自己再置 DELETING+重扫)→A 复活时它的 finalize 条件
    # #s=deleting 又成立(B 刚置的),A 会拿【自己的陈旧扫描结果】把版本 finalize 成 deleted,
    # 绕过 B 的引用检查(no-data-loss 缺口)。token 让 A 的写在 owner 不符时失败。
    delete_owner = uuid.uuid4().hex
    # 先以条件写 ACTIVE→DELETING，阻止新的 guarded Job transaction 入队；然后再扫描引用。
    # 与 Pull 的 ConditionCheck+Put 组合后，两种并发顺序都安全：先入队就会被下面扫描发现，
    # 先 DELETING 就会让 Pull 事务失败。
    try:
        version_snapshots_table.update_item(
            Key={"snapshot_time": snapshot_time},
            UpdateExpression=(
                "SET #s = :deleting, deletion_started_at = :t, deletion_owner = :me"
            ),
            ConditionExpression=(
                "attribute_exists(snapshot_time) AND "
                "(attribute_not_exists(#s) OR #s = :active)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":deleting": "deleting", ":active": "active",
                ":t": _now(), ":me": delete_owner
            },
        )
    except ccf:
        current = version_snapshots_table.get_item(
            Key={"snapshot_time": snapshot_time}, ConsistentRead=True
        ).get("Item") or {}
        if current.get("status") == "deleted":
            return _resp(200, {
                "message": "snapshot record already deleted",
                "snapshot_time": snapshot_time,
                "label": current.get("label", existing.get("label", "")),
                "status": "deleted",
            })
        return _err(409, "DELETE_IN_PROGRESS",
                    f"snapshot {snapshot_time} is not active (status={current.get('status')!r})")

    reference_check_error = None
    try:
        referenced, reason = _snapshot_still_referenced(snapshot_time)
    except Exception as e:  # noqa: BLE001
        # 扫描任一表失败都必须先撤销 DELETING；否则一次 DDB 抖动会把版本永久卡在
        # deleting，后续 Pull 和 Delete 都无法推进。
        referenced, reason = True, "reference check unavailable"
        reference_check_error = e
    if referenced:
        # 引用存在时恢复原状态。旧快照没有 status 字段，必须 REMOVE 而非写 active，保持
        # 兼容数据形状；回滚也带 deleting 条件，绝不覆盖并发状态。
        try:
            if original_had_status:
                version_snapshots_table.update_item(
                    Key={"snapshot_time": snapshot_time},
                    UpdateExpression="SET #s = :old REMOVE deletion_started_at, deletion_owner",
                    ConditionExpression="#s = :deleting AND deletion_owner = :me",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":old": original_status, ":deleting": "deleting",
                        ":me": delete_owner
                    },
                )
            else:
                version_snapshots_table.update_item(
                    Key={"snapshot_time": snapshot_time},
                    UpdateExpression="REMOVE #s, deletion_started_at, deletion_owner",
                    ConditionExpression="#s = :deleting AND deletion_owner = :me",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":deleting": "deleting", ":me": delete_owner
                    },
                )
        except Exception as e:  # noqa: BLE001
            return _err(503, "DELETE_ROLLBACK_FAILED",
                        f"snapshot deletion was blocked but ACTIVE state could not be restored: {e}")
        if reference_check_error is not None:
            return _err(503, "DELETE_REFERENCE_CHECK_FAILED",
                        f"could not verify snapshot references; deletion gate was rolled back: "
                        f"{reference_check_error}")
        return _err(409, "IMAGE_VERSION_IN_USE",
                    f"snapshot {snapshot_time} is still in use ({reason}); "
                    f"repoint/rebuild or delete the referencing hosts/tenants first")

    try:
        version_snapshots_table.update_item(
            Key={"snapshot_time": snapshot_time},
            UpdateExpression=(
                "SET #s = :d, deleted_at = :t REMOVE deletion_started_at, deletion_owner"
            ),
            ConditionExpression="#s = :deleting AND deletion_owner = :me",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":d": "deleted", ":deleting": "deleting", ":t": _now(),
                ":me": delete_owner
            },
        )
    except ccf:
        return _err(409, "DELETE_STATE_CHANGED",
                    f"snapshot {snapshot_time} deletion state changed concurrently; retry")
    return _resp(200, {
        "message": "snapshot record marked deleted (soft delete)",
        "snapshot_time": snapshot_time,
        "label": existing.get("label", ""),
        "status": "deleted",
        # 明说是软删 + 没删 S3 盘,避免运维误以为记录/空间已物理回收。
        "note": "soft delete: DDB record marked status=deleted (not removed); "
                "image files under deployment/ were not removed",
    })


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
        "status": "active",
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


def _pull_by_snapshot(instance_id, snapshot_time, slot=None, idempotency_key=None):
    """#217 V2 — read the snapshot from DDB, SSM-fetch host files by exact VersionId."""
    # 格式先行:非法 snapshot_time → 400(别拿去查 DB 走成 404 误导调用方)。
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
        Key={"snapshot_time": snapshot_time}, ConsistentRead=True
    ).get("Item")
    if not item:
        return _err(404, "NOT_FOUND", f"snapshot {snapshot_time} not found")
    if item.get("status") == "deleted":
        return _err(404, "NOT_FOUND", f"snapshot {snapshot_time} has been deleted")
    try:
        files = json.loads(item.get("files", "[]"))
    except (ValueError, TypeError):
        files = []
    # 不灌 microVM host。选择失败(无 manifest/点名盘缺失)→ fail-loud,不装。
    host_files, sel_err, _ = _select_pull_files(bucket, files)
    if sel_err:
        return _err(409, "CONFLICT", f"snapshot {snapshot_time}: {sel_err}")

    # 校验,镜像滞后最坏是漏判后落到幂等 pull,无害):目标版本已是该 host 的 live → 没有可验证
    # 的东西(在同一镜像上建"金丝雀"租户是无意义操作)→ 409 CANARY_EQUALS_LIVE,提示 pull 一个
    # 【不同】的候选版本。
    #
    # 【为何不做"canary 已就位→already_staged 短路"】:DDB 镜像只记指针,不保证盘上那份
    #   versions/<snap>/ 是【完整】的(可能上次 pull 崩在 commit 中途,无 .complete 标记)。若凭
    #   镜像 canary==目标就短路,会对一个【半装】的 canary 谎报"已就位",跳过本该自愈的重装。
    #   故【不】短路——放行到正常 pull:pull 自己的快路径会检查 .complete,完整则秒级翻指针
    #   (等价你要的 no-op,但【经过盘上验证】),不完整则重下自愈。真值判据永远在盘上,不在镜像。
    if slot == image_slots.SLOT_CANARY:
        _mirror = (hosts_table.get_item(
            Key={"instance_id": instance_id}, ConsistentRead=True
        ).get("Item") or {}).get("image_slots") or {}
        if _mirror.get("live") == snapshot_time:
            return _err(
                409, "CANARY_EQUALS_LIVE",
                f"snapshot {snapshot_time} is already live on host {instance_id}; nothing to "
                f"validate — pull a different candidate to the canary slot",
            )

    # job_id(202),绝不再抢 lease / 再置 upgrading / 再起一次真 pull(否则前一任务结束后会创建
    # 第二个真实 pull,且重试可能吃 409)。key 缺失 → 跳过,退化成旧"每次真跑"语义。
    if idempotency_key:
        prior = image_jobs.find_by_idempotency_key(instance_id, idempotency_key)
        if prior:
            # bug:拿装 A-live 的 key 去请求 B-canary,不能返回旧 A-live job 让它误以为受理了)。
            # 比较规范化指纹:(snapshot_time, slot)。一致才重放原 job。
            prior_slot = prior.get("target_slot", "live")
            prior_snap = prior.get("requested_snapshot_time")
            if prior_snap != snapshot_time or prior_slot != (slot or "live"):
                return _err(
                    409, "IDEMPOTENCY_KEY_REUSED",
                    f"Idempotency-Key already used for pull of {prior_snap!r}→{prior_slot}; "
                    f"this request is {snapshot_time!r}→{slot or 'live'}. Use a fresh key.",
                )
            return _resp(202, {
                "message": "pull-image already accepted (idempotent replay)",
                "instance_id": instance_id,
                "snapshot_time": prior_snap or snapshot_time,
                "slot": prior_slot,
                "job_id": prior.get("job_id"),
                "replayed": True,
            })

    # _set_host_upgrading 的【同一条原子 UpdateItem】里连 pull_command_id 一起写(见该函数注释:
    # 消除 CAS 与写 owner 分两步的窗口)。pull_image_progress 据 pull_command_id tail 进度文件。
    job_id = f"pull-{uuid.uuid4().hex[:16]}"
    #  · image lease 是"同一 host 同时只跑一个镜像操作"的统一互斥(ADR §4.8 规则 1):
    #    抢到它,promote/cleanup/reclaim 全被 409 挡(它们也走同一把 lease)。原先只有 canary
    #    抢 lease、live pull 只靠 status upgrading 门 → live pull 与 promote/cleanup/reclaim
    #    可并发写同一 slots.json / 把半装 live 当孤儿删(P1-2)。现在 live 也抢 lease 堵死。
    #  · status:仅 live pull 额外置 upgrading(维护门:pull 期间不接新租户);canary【绝不】改
    #    status(否则该 host 停接 live 租户,违背"存量零影响"),互斥完全靠 lease。
    lease_epoch, lease_err = image_lease.acquire(instance_id, job_id, "pull-image")
    if lease_err:
        return _err(409, "IMAGE_OPERATION_IN_PROGRESS", lease_err)
    if slot == image_slots.SLOT_CANARY:
        cur = hosts_table.get_item(
            Key={"instance_id": instance_id}, ConsistentRead=True
        ).get("Item") or {}
        prev_status = cur.get("status", "active")
    else:
        # live:CAS active/idle→upgrading + 原子写 owner。失败 → 还掉刚抢的 lease 再返回
        # (否则该 host 镜像操作被占死到 lease 自然过期)。
        prev_status, err = _set_host_upgrading(instance_id, job_id)
        if err:
            image_lease.release(instance_id, job_id)
            return err

    # 真正的入口,没抢到就不该留 Job 记录(否则 409 也会攒一堆 QUEUED 垃圾)。
    #  · 表未配置(is_enabled=False):Job 功能整体关闭,progress 回退 /tmp,返 202 无妨(旧行为);
    #  · 表已启用但写失败(fresh uuid 不可能撞 dup → 只能是 DDB 超时/限流/权限):此时若照常
    #    dispatch,progress 精确查会 404 JOB_NOT_FOUND、canary 又无 /tmp 回退 → 从 202 起就不可观测,
    #    违背持久化 Job 契约。故【释放刚抢的准入闸 + 返回可重试错误】,不做无记录的盲跑。
    created = image_jobs.create(
        job_id, instance_id, slot or "live", snapshot_time,
        idempotency_key=idempotency_key,
        snapshot_table=version_snapshots_table,
    )
    if image_jobs.is_enabled() and not created:
        if slot != image_slots.SLOT_CANARY:
            _reset_host_status(instance_id, prev_status)
        image_lease.release(instance_id, job_id)
        return _err(
            503, "JOB_RECORD_UNAVAILABLE",
            "could not persist pull job record (control-plane store unavailable); "
            "admission released, retry shortly",
        )

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
                                       "job_id": job_id,
                                       "slot": slot}}
            ).encode("utf-8"),
        )
    except Exception as e:  # 自调用都没发出去 → 落失败终态并收尾,别卡 QUEUED/upgrading/lease
        _record_pull_error(
            instance_id,
            f"pull-image worker dispatch failed: {e}",
            job_id,
        )
        # 自然过期,期间 promote/cleanup/reclaim 全被 409 挡)。live 额外复位 status。
        if slot != image_slots.SLOT_CANARY:
            _reset_host_status(instance_id, prev_status)
        image_lease.release(instance_id, job_id)
        return _err(500, "DISPATCH_FAILED", f"failed to dispatch pull-image worker: {e}")
    return _resp(
        202,
        {"message": "pull-image started (async; poll pull-image-progress)",
         "instance_id": instance_id, "snapshot_time": snapshot_time,
         "slot": slot or "live",
         "status": prev_status if slot == image_slots.SLOT_CANARY else "upgrading",
         "job_id": job_id},
    )


def _run_pull_pipeline(instance_id, snapshot_time, prev_status, job_id, slot=None):
    """#394 —— 薄壳:pull 无论走哪条早退路径都必须归还 image lease。

    为什么用 try/finally 包壳而不是在每个 return 前加 release:内层管线有 8+ 条早退路径
    (未配置/快照不存在/选盘失败/拼脚本异常/下发失败/fence 失败/脚本失败/成功),漏掉任一条
    都会把该 host 的镜像操作占死到 lease 自然过期(期间 promote/cleanup/reclaim 全被 409 挡)。

    在此归还。释放是 owner-conditional(release 内部条件写校验 owner==job_id),超时重投的旧
    worker 不会误释放接管者的 lease。live 的 status 复位另在 _finalize_success/失败路径处理,
    与 lease 释放正交(lease 管镜像操作互斥,status 管是否接新租户)。
    """
    try:
        return _run_pull_pipeline_impl(
            instance_id, snapshot_time, prev_status, job_id, slot
        )
    finally:
        try:
            image_lease.release(instance_id, job_id)
        except Exception as e:
            # Lease cleanup is best-effort after the pipeline has already committed its
            # terminal Job state. Let the bounded lease expire instead of replacing a
            # successful result with an exception that the handler would mark FAILED.
            print(
                f"[pull] WARN release image lease failed for {instance_id} "
                f"(job={job_id}): {e}"
            )


def _run_pull_pipeline_impl(instance_id, snapshot_time, prev_status, job_id, slot=None):
    """#309 V1 — 异步 worker:stage + 校验 + copy/unzip 装 live 的长链(数分钟)。#333 去 backup。
    由 pull_image 经 InvocationType=Event 自调用触发({"_pull_image_async": {...}}),
    在无客户端等待的 fire-and-forget 调用里跑满(可达 Lambda 900s)。host 已被 pull_image
    CAS 置 upgrading。金丝雀已移除(owner 2026-07-17),V1 失败不自动 restore(留 V2)。
    job_id 由 pull_image(sync)生成并存进 host DDB 项(pull_command_id),脚本据它把进度写
    /tmp/<job_id>.txt,pull_image_progress tail 之。幂等:重放装同版无害(固定名覆盖同内容)。"""
    # 正常 return、不触发 handler 兜底、进度文件又不存在 → progress 永报 InProgress),再复位回 prev
    # (live 未碰,安全)。所有下发【前】的早退路径都走它,不漏记错误。
    def _fail_before_dispatch(reason):
        _record_pull_error(instance_id, reason, job_id)
        # 别的操作置为 upgrading 的 host 写回 active(凭空解除维护门)。canary 的收尾是还
        # lease(由外层 finally 保证),status 一字不动。
        if slot != image_slots.SLOT_CANARY:
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
        host_files, sel_err, rootfs_version = _select_pull_files(bucket, files)
        if sel_err:
            _fail_before_dispatch(f"snapshot {snapshot_time}: {sel_err}")
            return {"statusCode": 409, "body": f"snapshot {snapshot_time}: {sel_err}"}
        hosts_table_name = os.environ.get("HOSTS_TABLE", "")
        # 用 host 记录里的扁平 live 版本(host.snapshot_time,pull-image 成功时写的)迁进
        # 版本目录。只在 canary + host 已有 image_slots.live 为空 + 有扁平 snapshot_time 时传;
        # 否则传 None(self-heal 段不生成)。读 host 记录:强一致,拿最新状态。
        flat_live_snap = None
        if slot == image_slots.SLOT_CANARY:
            _hrec = hosts_table.get_item(
                Key={"instance_id": instance_id}, ConsistentRead=True
            ).get("Item") or {}
            _slots_now = _hrec.get("image_slots") or {}
            if not _slots_now.get("live"):  # live 未解析 → 需 self-heal
                flat_live_snap = (_hrec.get("snapshot_time") or "").strip() or None
        install_cmd = _snapshot_pull_script(
            bucket, region, host_files, snapshot_time,
            hosts_table_name, instance_id, prev_status, job_id, rootfs_version,
            slot=slot, flat_live_snapshot_time=flat_live_snap,
        )
    except Exception as e:
        _fail_before_dispatch(f"pre-dispatch error: {e}")
        return _resp(500, {"error": str(e)})

    cmd_id, ok, tail = _ssm_wait(instance_id, install_cmd, timeout=900)
    if cmd_id is None:
        # 下发都没成功 → 脚本从未跑,live 未碰,安全复位回 prev(别卡 upgrading)+ 记错误供 progress。
        _fail_before_dispatch("pull-image SSM dispatch failed")
        return _resp(500, {"error": "pull-image SSM dispatch failed"})
    if not ok:
        reason = (tail or "")[-400:]
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
        # 无法判定所有权(可能就是唯一合法 worker)。它发生在 LOCKED=1 【之前】(live 未碰),故按
        # 【真失败】处理:job-conditional 记错误 + 复位回 prev(别卡 upgrading、progress 别永 InProgress)。
        if "OWNERSHIP_CHECK_FAILED" in (tail or ""):
            _record_pull_error(instance_id, reason, job_id)
            if slot != image_slots.SLOT_CANARY:  # #394 canary 未改 status,不复位(同 _fail_before_dispatch)
                _reset_host_status(instance_id, prev_status, job_id)  # live 未碰,安全复位
            return _resp(502, {"error": "pull-image fence DDB read failed (live untouched, reset)",
                               "command_id": cmd_id, "detail": reason})
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
    # host.snapshot_time 改成本次版本。而 host.snapshot_time 的语义是"这台 host 的 live 版本",
    # canary 装好并不改变 live —— 写了就等于谎报该 host 已在跑候选版本(存量 live 租户其实
    # 还在旧版)。canary 的成功事实由 slots.json 的 canary 指针 + 持久化 Job result 表达。
    if slot == image_slots.SLOT_CANARY:
        image_jobs.record_transition(
            job_id, "SUCCEEDED", phase="done", progress_percent=100,
            result={"snapshot_time": snapshot_time, "slot": image_slots.SLOT_CANARY},
        )
        # 真值仍是 host 上的 slots.json(ADR §4.2);这里只是控制面读得到的一份投影,
        # 故 best-effort:写失败不影响 pull 结果(create-tenant 拿不到会 CANARY_NOT_READY
        # 让调用方重试,不会误起 live —— fail-closed 方向)。
        # self-heal:host 侧刚把扁平 live 迁进版本目录并解析了 slots.live;把控制面镜像的
        # live 也一并回填(否则镜像 live 仍 null → promote 出 previous_live=null / 前端 undefined)。
        _mirror_canary_slot(instance_id, snapshot_time,
                            live_snapshot_time=flat_live_snap)
        return _resp(
            200,
            {"message": "pull-image installed to canary slot (live untouched)",
             "snapshot_time": snapshot_time, "instance_id": instance_id,
             "slot": image_slots.SLOT_CANARY, "install_command_id": cmd_id},
        )
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
    at-least-once/超时重投的【旧 worker】失败后,新 pull 已写入新 job,旧 Lambda 会把旧错误写到
    新 job 的槽 → pull_image_progress 的 DDB 终态优先逻辑据此把新任务误报 Failed。非 owner → CCF 跳过。

    持久化 Job 行是【按 job_id 主键】的,天生 owner-safe(每个 job 只有自己那条)。canary pull
    从不写 pull_command_id,故 host 行 CCF 必然失败——但那【不该】连累 Job 终态。原来 CCF 后
    直接 return,导致 canary 失败永远停在 QUEUED(host 重启/进度文件丢后 progress 永报
    InProgress)。改为:host 行写失败只跳过 host 行,Job FAILED 转移【始终】执行。"""
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
        # 非 owner(含 canary 从不写 pull_command_id 的正常情形):跳过 host 行写,但 Job 终态
        # 仍要落(Job 按 job_id 主键,不会污染别的 job)。live 的旧 worker 场景同样安全:它写的
        # 是自己那条 job 的 FAILED,新 job 是另一条 job_id。
        print(f"[pull] record error {instance_id} host-row skipped (not owner / canary); job still marked FAILED (job={job_id})")
    except Exception as e:
        print(f"[pull] WARN record error {instance_id} failed: {e}")
    # 重启 /tmp 丢了仍能判终态"的来源(ADR §7)。旁路 fail-open,不抛。
    image_jobs.record_transition(
        job_id, "FAILED", phase="failed", error={"reason": (reason or "")[:400]}
    )


def _finalize_success(instance_id, snapshot_time, prev_status, job_id, rootfs_version=""):
    """装 live 成功 —— host status→prev + 写 snapshot_time(仅成功才记版本,不谎报),
    清 last_pull_error(本轮无错)。**保留 pull_command_id**:progress 据它 tail 进度文件才能
    读到末行 SUCCESS → 返回 Completed(codex review:删了 pull_command_id → progress 拿不到
    job_id → 永远 InProgress/no-job,观察不到成功)。下一轮 pull 的 _set_host_upgrading 覆盖它。
    当前 owner 且 host 仍在本轮 upgrading 才能更新 Host 投影。防两类:① at-least-once/超时重投
    的【旧 worker】finalize 掉新任务(脚本侧 flock 挡不住 Lambda 侧 DDB 写);② pull_command_id
    成功后【保留】,若 host 已被移到 draining/deleted,延迟 worker 光凭 owner 匹配会把它错误
    复位回 active(round12)。加 #s = :upg 关死:非 upgrading 一律 CCF 跳过 Host 更新。
    但 SSM 已返回成功时,该 Job 自身的 SUCCEEDED 终态仍必须写入:正常路径中 host 脚本会先执行
    _reset_ok 把 status 复位,使 Lambda 的幂等兜底 CAS 触发 CCF。若因此提前 return,成功 Job
    会永久停在 QUEUED。
    Lambda 兜底也必须写 rootfs_version,否则 status/snapshot 恢复了、版本字段仍停旧值(原 bug 重现)。
    故成功且版本号非空时,这条兜底 update 也补 rootfs_version(与脚本侧同值,幂等)。"""
    ccf = hosts_table.meta.client.exceptions.ConditionalCheckFailedException
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
        # draining/deleted),以及正常的 host 脚本已先 _reset_ok → 都不再写 Host 行。
        # 此处只保护 Host 投影；SSM 成功已证明本 Job 完成，下面仍写它自己的终态。
        print(
            f"[pull] host finalize {instance_id} skipped: already finalized or no longer "
            f"current owner (job={job_id}); recording successful job"
        )
    except Exception as e:
        print(f"[pull] WARN finalize {instance_id} failed: {e}")
    # (ADR §4.4 "pull 成功后 result 提供版本信息")。本步 target_slot 恒 live。
    image_jobs.record_transition(
        job_id, "SUCCEEDED", phase="done", progress_percent=100,
        result={"snapshot_time": snapshot_time, "slot": "live"},
    )


def host_image_slots(instance_id):
    """GET /hosts/{id}/image-slots — 读【该 EC2 磁盘上真实】的镜像状态(#394 debug 视图)。

    与 GET /hosts 的 image_slots(DDB 镜像,可能滞后)不同:这个 SSM 进 host 直接读
    /data/firecracker-assets/slots.json 真值 + ls versions/ 已装的版本目录,返回盘上真相。
    用途:一眼看出镜像是否与 DDB 镜像漂移、host 上到底装了哪些版本。只读,viewer 可读。

    返回:
      {instance_id, source:"host-disk", slots:{live,canary,previous_live,generation},
       installed_versions:[<snapshot_time>...], flat_layout:bool, mirror:{...DDB 镜像...}}
    slots.json 不存在(老扁平 host)→ slots 全 null + flat_layout=true。
    SSM 读失败 → 503(host 不可达/离线),不谎报空。"""
    if not instance_id:
        return _err(400, "VALIDATION", "missing instance_id")
    item = hosts_table.get_item(Key={"instance_id": instance_id}).get("Item")
    if not item:
        return _err(404, "NOT_FOUND", f"host {instance_id} not found")
    q = shlex.quote
    slots_f = q(image_slots.SLOTS_FILE)
    vers_d = q(image_slots.VERSIONS_DIR)
    # 一次 SSM 取两样,用前缀分隔:slots.json 内容 + versions/ 下的目录名(每行一个)。
    script = (
        f'echo "__SLOTS__$(cat {slots_f} 2>/dev/null || echo __NONE__)"; '
        f'echo "__VERS__"; ls -1 {vers_d} 2>/dev/null || true'
    )
    cmd_id, ok, out = _ssm_wait(instance_id, script, timeout=60)
    if not ok:
        return _err(503, "DEPENDENCY_UNAVAILABLE",
                    f"could not read image state from host {instance_id} (SSM failed/offline)")
    # 解析:__SLOTS__<json|__NONE__> 换行 __VERS__ 换行 <dir per line>
    raw = out or ""
    slots_line = ""
    versions = []
    in_vers = False
    for ln in raw.splitlines():
        if ln.startswith("__SLOTS__"):
            slots_line = ln[len("__SLOTS__"):]
        elif ln.strip() == "__VERS__":
            in_vers = True
        elif in_vers and ln.strip():
            versions.append(ln.strip())
    flat = False
    try:
        slots = image_slots.normalize(None if "__NONE__" in slots_line or not slots_line.strip()
                                      else slots_line)
        if "__NONE__" in slots_line or not slots_line.strip():
            flat = True
    except (ValueError, json.JSONDecodeError):  # 损坏 slots.json → fail-loud 到 500
        return _err(500, "SLOTS_CORRUPT",
                    f"host {instance_id} slots.json is unparseable")
    return _resp(200, {
        "instance_id": instance_id,
        "source": "host-disk",
        "slots": slots,
        "installed_versions": sorted(versions, reverse=True),
        "flat_layout": flat,
        # 一并回 DDB 镜像,方便调用方直接比对是否漂移。
        "mirror": item.get("image_slots") or None,
        "command_id": cmd_id,
    })


def pull_image(instance_id, query_params, headers=None):
    """#309 V1 — POST /hosts/{id}/pull-image?snapshot_time=<ISO>. 照快照按精确 VersionId
    拉 deployment/rootfs/(镜像三盘 + manifest.json),校验 etag 后 copy+unzip 装到 live 原位置
    (launch-vm 直接读的地方)。只作用一台 host。异步跑,立即回 202 + job_id;进度走
    pull_image_progress。金丝雀已移除,失败只报错不自动 restore(留 V2)。

    pull(与 promote/cleanup 幂等语义一致,兑现文档承诺)。"""
    if not instance_id:
        return _err(400, "VALIDATION", "missing instance_id")
    snapshot_time = ((query_params or {}).get("snapshot_time") or "").strip()
    if not snapshot_time:
        return _err(
            400,
            "VALIDATION",
            "snapshot_time query parameter required; use a snapshot_time from "
            "GET /list_image_versions",
        )
    # #524 —— 在 API 边界规范化缺省值，确保 Job、异步 payload 与安装脚本看到同一个 live。
    slot = ((query_params or {}).get("slot") or "").strip() or image_slots.SLOT_LIVE
    if not image_slots.is_valid_slot(slot):
        return _err(
            400, "VALIDATION",
            f"slot must be 'live' or 'canary'; got {slot!r}",
        )
    idem_key = _idempotency_key_from_headers(headers)
    return _pull_by_snapshot(instance_id, snapshot_time, slot, idem_key)


def _idempotency_key_from_headers(headers):
    """取 Idempotency-Key 头(大小写不敏感;缺失/空 → None,退化成"每次都真跑"的旧语义)。"""
    for key, value in (headers or {}).items():
        if key.lower() == "idempotency-key":
            return (value or "").strip() or None
    return None


# 更新用的,不碰镜像盘。目标只能落 init-host.sh 装脚本的两处 live 根(_script_live_dest):
# · /opt/openclaw/ —— 常驻 host-agent 的 .py service;· /home/ubuntu/ —— .sh + lib/*。
# 镜像盘目录 /data/firecracker-assets/ 不在此列(那是 pull_image 的活,不给手动 copy 覆盖)。
_COPY_FILE_ALLOWED_ROOTS = ("/opt/openclaw/", "/home/ubuntu/")


def _validate_copy_target(target):
    """#309 — 校验 copy-file 的目标 EC2 路径:必须落在 _COPY_FILE_ALLOWED_ROOTS 任一根下的
    绝对路径,禁 .. 穿越。返回 (ok, err_msg)。纯函数、易测。
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
    # /home/ubuntu)都属 ubuntu:ubuntu,host-agent(以 ubuntu 跑)要读改这些文件。故 cp 后
    # chown ubuntu:ubuntu(与 pull-image _install_lines 对 .sh 的 chown 一致,补齐这条路径)。
    # 是纯数据。绝不把用户原文直接插进双引号 echo —— 否则含 $()/反引号的值会在 root SSM 里被执行。
    # 允许根的真实路径(host 侧规范化父目录后,必须仍落在这些根下)。空格分隔喂给 POSIX for。
    allowed_roots_sh = " ".join(q(r.rstrip("/")) for r in _COPY_FILE_ALLOWED_ROOTS)
    script = "\n".join([
        # 不支持 bash 数组。全程用 POSIX 语法(for/case,无数组)。
        "set -eu",
        f"SRC={q(s3_uri)}",
        f"DST={q(target)}",
        f"ALLOWED_ROOTS={q(allowed_roots_sh)}",
        # TOCTOU:host 上 ubuntu(host-agent 身份)可在检查后换软链,借 root SSM 越权。彻底封需
        # openat2(RESOLVE_NO_SYMLINKS)/O_NOFOLLOW helper 或只写 root-owned 根。不触及三条不可退底线,
        # 且需 host ubuntu 已陷才可利用(租户 microVM 不可达),按当前阶段排后为 follow-up。
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


def _progress_from_job(base, job):
    """#394 step1 —— 持久化 Job 已是终态时,直接由 Job 组出 progress 响应(不探 host)。

    ProcessingJobStatus 与既有语义一一对齐,老客户端读法不变:
      SUCCEEDED → Completed(+ ExitCode 0);FAILED / RECOVERY_REQUIRED → Failed
      (+ ErrorCode/FailureReason)。RECOVERY_REQUIRED 对外仍报 Failed(老字段只有三态),
      细分靠新增的 state 字段区分 —— 不给老客户端造第四种它不认识的值。
    """
    state = job.get("state")
    err = job.get("error") or {}
    out = {**base, "last_status": job.get("phase")}
    if state == "SUCCEEDED":
        out["ProcessingJobStatus"] = "Completed"
        out["ExitCode"] = 0
        return out
    out["ProcessingJobStatus"] = "Failed"
    out["ErrorCode"] = err.get("code")
    out["FailureReason"] = err.get("reason") or base.get("last_pull_error")
    return out


def _resolve_progress_job(instance_id, requested_job_id, host_item):
    """#394 step1(ADR §4.4)—— 定位本次 progress 要查的 job。

    返回 (job_id, job_item, err_response):
      · 传了 job_id → 按 id 精确查持久化 Job;查不到 / 不属于该 host → 404 JOB_NOT_FOUND
        (不能默默回退成"该 host 最近那次",否则调用方以为查的是自己那个 job);
      · 没传 job_id(兼容窗口)→ 优先持久化 Job 里该 host 最近一条,其次退回 host 记录的
        pull_command_id(未部署 Job 表的环境行为完全不变)。
    Job 表未配置时 job_item 为 None,调用方照旧走 /tmp 进度文件路径。
    """
    if requested_job_id:
        job = image_jobs.get(requested_job_id)
        if job is None:
            # 表未配置时无法证伪 job 归属 → 不谎报 404,按传入 id 照旧 tail 进度文件。
            if image_jobs.is_enabled():
                return None, None, _err(
                    404, "JOB_NOT_FOUND",
                    f"pull job {requested_job_id} not found for host {instance_id}",
                )
            return requested_job_id, None, None
        if job.get("instance_id") != instance_id:
            return None, None, _err(
                404, "JOB_NOT_FOUND",
                f"pull job {requested_job_id} not found for host {instance_id}",
            )
        return requested_job_id, job, None
    latest = image_jobs.latest_for_host(instance_id)
    if latest:
        return latest.get("job_id"), latest, None
    return host_item.get("pull_command_id"), None, None


def pull_image_progress(instance_id, query_params=None):
    """#309 — GET /hosts/{id}/pull-image-progress:读 host DDB 项拿本轮 pull 的 job_id
    (pull_command_id),SSM 取进度文件的末行(判三态)+ 最近 ERROR 行(失败原因)。返回
    SageMaker ProcessingJob 风格 JSON(owner 2026-07-20):
      ProcessingJobStatus:'Completed'|'Failed'|'InProgress'(末行 SUCCESS/FAIL/其它翻译而来)
      Completed → 带 ExitCode:0
      Failed    → 带 ErrorCode(见 _PULL_ERROR_CODES:DOWNLOAD_FAILED/ETAG_MISMATCH/
                  MANIFEST_MISMATCH/UNZIP_FAILED/INSTALL_MV_FAILED/OWNERSHIP_CHECK_FAILED/UNKNOWN)
                  + FailureReason(详情)
      last_status:进度文件最后一行原文(带时间戳+做了什么,供 UI 展示细节)
    无 job_id → 从没 pull 过(state='NONE',ProcessingJobStatus=null,last_status=None)。

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
    item = hosts_table.get_item(
        Key={"instance_id": instance_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return _err(404, "NOT_FOUND", f"host {instance_id} not found")
    requested_job_id = ((query_params or {}).get("job_id") or "").strip()
    job_id, job, job_err = _resolve_progress_job(instance_id, requested_job_id, item)
    if job_err:
        return job_err
    base = {
        "instance_id": instance_id,
        "host_status": item.get("status"),  # host DDB 态(active/idle/upgrading),别与 pull status 混
        "job_id": job_id,
        "snapshot_time": item.get("snapshot_time"),
        "last_pull_error": item.get("last_pull_error"),
    }
    if job:
        # 新契约附加字段(ADR §4.4):既有 ProcessingJobStatus 保留,另给 state/phase/result/error。
        base.update({
            "state": job.get("state"),
            "phase": job.get("phase"),
            "target_slot": job.get("target_slot"),
            "requested_snapshot_time": job.get("requested_snapshot_time"),
            "result": job.get("result"),
            "error": job.get("error"),
        })
    if not job_id:
        return _resp(
            200,
            {
                **base,
                "snapshot_time": None,
                "last_pull_error": None,
                "state": "NONE",
                "phase": None,
                "target_slot": None,
                "requested_snapshot_time": None,
                "result": None,
                "error": None,
                "ProcessingJobStatus": None,
                "last_status": None,
                "message": "no pull-image job for this host",
            },
        )
    # 也能给出正确终态(ADR §7 "Host 重启导致 /tmp 丢失"),同时省一次 SSM 往返。
    if job and job.get("state") in image_jobs.TERMINAL_STATES:
        return _resp(200, _progress_from_job(base, job))
    q = shlex.quote
    jf = f"/tmp/{q(str(job_id))}.txt"
    # 一次 SSM 取三样:① 文件在不在(__EXISTS__)② 末行(判三态)③ 最近一条 ERROR:<CODE> 行。
    # 都会让 tail 回空;单看空行分不清是"没建"还是"在建"。加一行 [ -f ] 探测,Lambda 据此
    # 在响应里透出 progress_file_missing,但【不因此报错】(fresh/in-flight job 文件本就可能暂缺)。
    script = (
        f'if [ -f {jf} ]; then echo "__EXISTS__yes"; else echo "__EXISTS__no"; fi; '
        f'echo "__LAST__$(tail -n 1 {jf} 2>/dev/null)"; '
        f'echo "__ERR__$(grep "ERROR:" {jf} 2>/dev/null | tail -n 1)"'
    )
    cmd_id, ok, tail = _ssm_wait(instance_id, script, timeout=60)
    last_line, err_line = _parse_progress_output(tail if ok else "")
    # 文件缺失判定:SSM 读成功且明确回 __EXISTS__no → 文件不在;SSM 读失败(host 不可达/重启)
    # 也视作"进度不可用"。两者都进 progress_file_missing,交由下方 reconciler 用 host 真值对账。
    file_missing = (not ok) or ("__EXISTS__no" in (tail or ""))
    job_status = _pull_status_from_line(last_line)
    # 判不出终态(永远 InProgress),但 Lambda 侧 _record_pull_error 把失败写进了 DDB last_pull_error。
    # 故 DDB last_pull_error 有值 → 覆盖为 Failed(以持久化终态为准,不让 worker 早退永卡 InProgress)。
    ddb_err = item.get("last_pull_error")
    if job_status != "Completed" and ddb_err:
        job_status = "Failed"
    # live 以 host.status + snapshot_time 为证据；canary 以 lease 已结束且 slots mirror
    # 在 Job 入队后完成过同步为证据。后者由 pull commit 和 host-agent 心跳共同维护，避免
    # 把 canary 正在执行期间的旧指针误判为失败。所有推断终态必须先成功写回 Job；写失败
    # 返回可重试 503，绝不再输出 ProcessingJobStatus 与 state/phase 互相矛盾的 200。
    if job and job.get("state") not in image_jobs.TERMINAL_STATES:
        req_snap = job.get("requested_snapshot_time")
        target_slot = job.get("target_slot", "live")
        terminal_state = terminal_phase = None
        terminal_result = terminal_error = None

        if job_status == "Completed":
            terminal_state, terminal_phase = "SUCCEEDED", "done"
            terminal_result = {"snapshot_time": req_snap, "slot": target_slot}
        elif job_status == "Failed":
            code, reason = _parse_error_line(err_line)
            terminal_state, terminal_phase = "FAILED", "failed"
            terminal_error = {"code": code, "reason": reason or ddb_err or last_line}
        elif (not ok) or job_status == "InProgress":
            if target_slot == "live":
                host_snap = item.get("snapshot_time")
                host_status = item.get("status")
                if host_status and host_status != "upgrading":
                    if host_snap and req_snap and host_snap == req_snap:
                        terminal_state, terminal_phase = "SUCCEEDED", "done"
                        terminal_result = {"snapshot_time": req_snap, "slot": "live"}
                    else:
                        terminal_state, terminal_phase = "RECOVERY_REQUIRED", "recovery"
                        terminal_error = {
                            "code": "RECOVERY_REQUIRED",
                            "reason": "progress lost and host truth does not confirm this "
                                      "live version; manual recovery required",
                        }
            elif target_slot == image_slots.SLOT_CANARY:
                lease = image_lease.read(instance_id)
                still_running = (
                    bool(lease)
                    and lease.get("active_image_operation_id") == job_id
                    and image_lease.is_held(lease)
                )
                # Re-read slot truth after the lease read. Both lease and mirror live on the
                # same Host item, so a strong read here observes any mirror write ordered before
                # release. If commit mirror failed, host-agent must sync after release/expiry
                # before absence can be treated as proof of failure.
                truth = hosts_table.get_item(
                    Key={"instance_id": instance_id}, ConsistentRead=True
                ).get("Item") or item
                synced_at = int(truth.get("image_slots_synced_at_epoch") or 0)
                try:
                    created_at = int(datetime.fromisoformat(
                        str(job.get("created_at") or "").replace("Z", "+00:00")
                    ).timestamp())
                except (TypeError, ValueError):
                    created_at = 0
                mirror_canary = (truth.get("image_slots") or {}).get("canary")
                mirror_is_post_admission = synced_at > 0 and synced_at >= created_at
                released_at = 0
                if lease and lease.get("image_lease_released_operation_id") == job_id:
                    released_at = int(lease.get("image_lease_released_at_epoch") or 0)
                elif (lease and lease.get("active_image_operation_id") == job_id
                      and not image_lease.is_held(lease)):
                    released_at = int(lease.get("image_lease_until") or 0)
                # codex NB4 —— 必须【严格】晚于 release:秒级时间戳下,一个在 release 同一秒
                # 落库的【release 前】旧心跳若用 >= 会被当成 post-release 证据 → 误判 RECOVERY。
                # 用 > 让同秒心跳不作数(留给下一轮心跳),宁可暂不终结也不误报恢复。
                mirror_is_post_end = released_at > 0 and synced_at > released_at
                if not still_running and mirror_canary == req_snap and mirror_is_post_admission:
                    terminal_state, terminal_phase = "SUCCEEDED", "done"
                    terminal_result = {"snapshot_time": req_snap, "slot": "canary"}
                elif not still_running and mirror_is_post_end:
                    terminal_state, terminal_phase = "RECOVERY_REQUIRED", "recovery"
                    terminal_error = {
                        "code": "RECOVERY_REQUIRED",
                        "reason": "progress lost after canary lease ended and fresh Host "
                                  "slot truth does not confirm the requested version; "
                                  "manual recovery required",
                    }

        if terminal_state:
            persisted = image_jobs.record_transition(
                job_id, terminal_state, phase=terminal_phase,
                progress_percent=100 if terminal_state == "SUCCEEDED" else None,
                result=terminal_result, error=terminal_error,
            )
            if not persisted:
                return _err(
                    503, "JOB_RECORD_UNAVAILABLE",
                    f"reconciled pull job {job_id} to {terminal_state} but could not persist "
                    "the terminal state; retry progress shortly",
                )
            job = {**job, "state": terminal_state, "phase": terminal_phase,
                   "result": terminal_result, "error": terminal_error}
            base.update({"state": terminal_state, "phase": terminal_phase,
                         "result": terminal_result, "error": terminal_error})
            out = _progress_from_job(base, job)
            out.update({"last_status": last_line, "command_id": cmd_id,
                        "progress_file_missing": file_missing})
            return _resp(200, out)

    # Legacy/no-Job path or a genuinely running Job.
    out = {**base, "ProcessingJobStatus": job_status,
           "last_status": last_line, "command_id": cmd_id,
           "progress_file_missing": file_missing}
    if job_status == "Completed":
        out["ExitCode"] = 0
    elif job_status == "Failed":
        code, reason = _parse_error_line(err_line)
        out["ErrorCode"] = code
        out["FailureReason"] = reason or ddb_err or last_line
    return _resp(200, out)


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
