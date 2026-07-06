# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""dispatch_poller — EventBridge rate(1 minute) 触发,巡检 dispatch_inflight 的 host。

- Success:批内租户逐条条件写 status=running + 清 inflight token + DeleteParameter
  (manifest 前缀白名单)。
- Failed/TimedOut:回滚该批 CAS(scheduling._release_slot 已在 dispatch_service)+
  tenants.dispatch_retries+1;预算内 SendMessageBatch 重入队,超预算 → tenants.
  status=requires_intervention(不再回队列)。
- 幂等:launch 前 host 侧 check /data/firecracker-vms/<tenant>/vm.json;控制面侧靠
  条件写 status=creating→running(重入不会二次改写)。

boto3 走 dispatch_service 的属性访问(_ssm_adaptive/_cw/_sqs),测试重绑友好。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

import core.clients as clients
from core.utils import _now
from services import dispatch_service as ds


def _list_inflight_hosts() -> List[Dict[str, Any]]:
    """扫 hosts 有 dispatch_inflight 的记录。ConsistentRead 让下游 CAS 用最新值。"""
    return clients.hosts_table.scan(
        FilterExpression="attribute_exists(dispatch_inflight)",
        ConsistentRead=True,
    ).get("Items", [])


def _query_assignments(instance_id: str, command_id: str) -> List[Dict[str, Any]]:
    """从 assignments 表反查该 host+command 下的 tenants(pull 模式或需要租户清单时)。
    push 模式我们只知道 host 上 inflight 命令是 command_id,还得从 tenants 表查
    dispatch_claim=command_id 的所有 id 定位这批租户。"""
    if not clients.assignments_table:
        return []
    resp = clients.assignments_table.query(
        KeyConditionExpression="instance_id = :i",
        ExpressionAttributeValues={":i": instance_id},
    )
    return [
        it
        for it in resp.get("Items", [])
        if it.get("dispatch_claim") == command_id or it.get("command_id") == command_id
    ]


def _query_batch_tenants(command_id: str) -> List[Dict[str, Any]]:
    """按 dispatch_claim 反查一批租户(用于 push 模式清算+回滚)。GSI 未建时退化为 scan。"""
    try:
        resp = clients.tenants_table.scan(
            FilterExpression="dispatch_claim = :c AND #s = :creating",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": command_id, ":creating": "creating"},
            ConsistentRead=True,
        )
        return resp.get("Items", [])
    except Exception as e:  # noqa: BLE001
        print(f"[poller] tenant scan failed: {e}")
        return []


def _get_ssm_status(command_id: str, instance_id: str) -> Tuple[str, Dict[str, Any]]:
    """GetCommandInvocation → (状态, stdout 解析出的 v2 报告 dict)。查不到当 InProgress。

    v2 报告(launch-all stdout JSON)带 tenants.launched/skipped/failed 三个 id 清单
    ——执行体自报结果,是唯一不会被控制面清掉的租户关联(dispatch_claim 在退避重试
    路径会被 _release_claims 主动清空,靠它反查会把整批 VM 已起的租户永久卡在
    creating,真机 e2ev2-probe 2026-07-05 抓出)。老版脚本 stdout 无 tenants 字段
    → 返回空 dict,调用方降级走 claim 反查(向后兼容)。"""
    ssm = ds._ssm_adaptive()
    try:
        resp = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        status = resp.get("Status", "InProgress")
        report: Dict[str, Any] = {}
        if status in ("Success", "Failed"):
            out = (resp.get("StandardOutputContent") or "").strip()
            # stdout 末行是 JSON 契约(log 走 stderr 不污染);从最后一行往前找
            # 首个能整行解析成 dict 的(不能 rfind("{")——嵌套 JSON 的内层 '{'
            # 会截出残片,单测抓过)。
            for line in reversed(out.splitlines()):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    report = parsed
                    break
        return status, report
    except Exception as e:  # noqa: BLE001
        # InvocationDoesNotExist / 未到 → 下轮再看
        print(f"[poller] GetCommandInvocation {command_id}/{instance_id}: {e}")
        return "InProgress", {}


def _mark_running(tenant_id: str) -> bool:
    """条件写 tenants: status creating→running,清 dispatch_claim/inflight 标记。"""
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET #s = :run, running_ts = :now "
                "REMOVE dispatch_claim, dispatch_claim_ts"
            ),
            ConditionExpression="#s = :creating",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":run": "running",
                ":creating": "creating",
                ":now": _now(),
            },
        )
        return True
    except ccf:
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[poller] mark_running {tenant_id} error: {e}")
        return False


def _bump_retry(tenant_id: str) -> int:
    """dispatch_retries+=1,返回新值。超预算调用方转 requires_intervention。"""
    try:
        resp = clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET dispatch_retries = if_not_exists(dispatch_retries, :z) + :one "
                "REMOVE dispatch_claim, dispatch_claim_ts"
            ),
            ExpressionAttributeValues={":one": 1, ":z": 0},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["dispatch_retries"])
    except Exception as e:  # noqa: BLE001
        print(f"[poller] bump_retry {tenant_id} error: {e}")
        return clients.DISPATCH_RETRY_BUDGET + 1  # 保守当已耗尽


def _mark_requires_intervention(tenant_id: str) -> None:
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :ri, requires_intervention_ts = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":ri": "requires_intervention", ":now": _now()},
        )
    except Exception as e:  # noqa: BLE001
        print(f"[poller] requires_intervention {tenant_id} error: {e}")


def _requeue(tenant_id: str, params: Dict[str, Any]) -> None:
    """SendMessageBatch 单条重入队(预算内)。用 dispatch_service._sqs()。"""
    if not clients.DISPATCH_QUEUE_URL:
        print(f"[poller] no DISPATCH_QUEUE_URL — cannot requeue {tenant_id}")
        return
    msg = {
        "v": 1,
        "action": "create",
        "tenant_id": tenant_id,
        "request_token": f"retry-{int(time.time())}",
        "params": params or {},
    }
    try:
        ds._sqs().send_message(
            QueueUrl=clients.DISPATCH_QUEUE_URL, MessageBody=json.dumps(msg)
        )
    except Exception as e:  # noqa: BLE001
        print(f"[poller] requeue {tenant_id} failed: {e}")


def _params_from_tenant(item: Dict[str, Any]) -> Dict[str, Any]:
    """从 tenants item 还原重投所需的 params(vcpu/mem_mb/owner_id/chat_ep)。"""
    return {
        "vcpu": int(item.get("vcpu") or clients.VM_DEFAULT_VCPU),
        "mem_mb": int(item.get("mem_mb") or clients.VM_DEFAULT_MEM),
        "owner_id": item.get("owner_id") or "",
        "chat_ep": bool(item.get("chat_ep", False)),
    }


def poll_inflight() -> Dict[str, Any]:
    """扫在途命令,推进终态或回滚。返回统计 dict(供指标/日志)。"""
    hosts = _list_inflight_hosts()
    stats = {"hosts": len(hosts), "success": 0, "failed": 0, "still_running": 0}
    for h in hosts:
        instance_id = h.get("instance_id")
        command_id = h.get("dispatch_inflight")
        if not (instance_id and command_id):
            continue
        # SSM 终态用 dispatch_ssm_cid(SSM 分配的 36 位 UUID)查;dispatch_inflight
        # 是内部批次号(关联租户 dispatch_claim),GetCommandInvocation 不认它。
        # 老记录/写失败没有 ssm_cid → 保持 InProgress,等 stale-TTL 兜底。
        ssm_cid = h.get("dispatch_ssm_cid")
        if not ssm_cid:
            stats["still_running"] += 1
            continue
        status, report = _get_ssm_status(ssm_cid, instance_id)
        if status == "Success":
            # v2 报告优先:launched+skipped 的租户直接按执行体自报标 running
            # (claim 可能已被退避路径清空,反查会漏,见 _get_ssm_status docstring);
            # 老脚本无 tenants 字段 → 降级 claim 反查。
            t_report = (report or {}).get("tenants") or {}
            reported = [
                tid
                for k in ("launched", "skipped")
                for tid in (t_report.get(k) or [])
                if isinstance(tid, str) and tid
            ]
            if reported:
                for tid in reported:
                    _mark_running(tid)
            else:
                batch = _query_batch_tenants(command_id)
                for t in batch:
                    _mark_running(t["id"])
            # 清 host inflight 标记(不回滚容量:VM 真起了)
            try:
                clients.hosts_table.update_item(
                    Key={"instance_id": instance_id},
                    UpdateExpression="REMOVE dispatch_inflight, dispatch_inflight_ts, dispatch_inflight_ts_epoch, dispatch_ssm_cid",
                )
            except Exception as e:  # noqa: BLE001
                print(f"[poller] clear inflight {instance_id}: {e}")
            # DeleteParameter — 手动扫 part 编号(0..8 保守足够,每 part 携百 tenant)
            _cleanup_manifest_best_effort(command_id, instance_id)
            stats["success"] += 1
        elif status in ("Failed", "TimedOut", "Cancelled"):
            batch = _query_batch_tenants(command_id)
            n = len(batch) or int(h.get("vm_count_delta", 0) or 0) or 1
            ds._rollback_host(instance_id, n)
            for t in batch:
                new_retries = _bump_retry(t["id"])
                if new_retries > clients.DISPATCH_RETRY_BUDGET:
                    _mark_requires_intervention(t["id"])
                else:
                    _requeue(t["id"], _params_from_tenant(t))
            _cleanup_manifest_best_effort(command_id, instance_id)
            stats["failed"] += 1
        else:
            stats["still_running"] += 1
    return stats


def _cleanup_manifest_best_effort(command_id: str, instance_id: str) -> None:
    """扫 manifests/<cid>/<iid>/ 前缀下的 part 名清理(GetParametersByPath),精确删除。"""
    prefix = f"{clients.DISPATCH_PARAM_PREFIX}/manifests/{command_id}/{instance_id}"
    # 硬白名单校验:防未来有人改 prefix 变量误伤 config
    if not prefix.startswith(f"{clients.DISPATCH_PARAM_PREFIX}/manifests/"):
        raise RuntimeError(f"refusing delete outside manifests/: {prefix}")
    ssm = ds._ssm_adaptive()
    try:
        resp = ssm.get_parameters_by_path(Path=prefix, Recursive=False, MaxResults=10)
        names = [p["Name"] for p in resp.get("Parameters", [])]
        for name in names:
            if not name.startswith(prefix + "/"):
                continue  # defence-in-depth
            try:
                ssm.delete_parameter(Name=name)
            except Exception as e:  # noqa: BLE001
                print(f"[poller] delete {name} non-fatal: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[poller] list manifests {prefix} non-fatal: {e}")
