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
import shlex
import time
from typing import Any, Dict, List, Tuple

import core.clients as clients
import core.ssm_dispatch as ssm_dispatch
from core.dispatch import normalize_spec
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
    """按 dispatch_claim 反查一批租户(用于 push 模式清算+回滚)。GSI 未建时退化为 scan。

    #412(codex review2 #6)—— 【全量翻页】(LastEvaluatedKey)且【读失败向上抛】,不再单页
    + 吞异常返 []:单页会漏掉 >1MB 或 FilterExpression 命中在后页的租户,吞异常返 [] 会让
    失败分支误判"batch 空 → 全落定 → 清 inflight",而残留预留仍占容量卡死。调用方对异常
    的处置(保留 inflight/不推进 retry)比"当空"安全。"""
    items: List[Dict[str, Any]] = []
    kwargs = {
        "FilterExpression": "dispatch_claim = :c AND #s = :creating",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":c": command_id, ":creating": "creating"},
        "ConsistentRead": True,
    }
    resp = clients.tenants_table.scan(**kwargs)
    items.extend(resp.get("Items", []))
    while resp.get("LastEvaluatedKey"):
        resp = clients.tenants_table.scan(
            ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs
        )
        items.extend(resp.get("Items", []))
    return items


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


def _mark_running(tenant_id: str, instance_id: str, command_id: str) -> bool:
    """条件写 tenants: status creating→running,清 dispatch_claim/inflight 标记。

    #412(codex review2 #1)—— 转 running 时【一并 REMOVE capacity_reservation_id】:
    VM 已真起,容量归 running 租户合法持有,后续由正常 delete 路径(按 item.vcpu 扣)回收。

    #412(codex review3 #1)—— fence host_id=:self:promote 与令牌释放【互斥】,防复活已释放租户。

    #412(codex review9 #2)—— ABA 闸【改用 capacity_reservation_id=:rid】(rid=command_id:tenant_id):
    host_id 单独不够——租户被释放后重投【落回同一 host】拿【新】预留(新 command_id、新 rid),本
    (旧 command)poller 迟到 promote 会 host_id=:self 命中 → 把【新预留】的租户按旧命令 promote、
    清掉新令牌 → VM/放置漂移 + 未记账容量。改锚 capacity_reservation_id=本命令的 rid:只 promote
    仍持【本命令那张令牌】的租户;新预留的令牌是新 rid → CCF 跳过。
    为何这次能用 rid 当条件(review5 #2 曾担心 claim 被清):令牌【不】在退避路径被清(只 dispatch_
    claim 被清,见 grep:capacity_reservation_id 仅 release/promote 清)——reported 路径的租户即便
    claim 已清,令牌仍在,故 rid 是稳定锚,不会把'claim 已清但真起了'的租户误挡在 creating。"""
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    rid = ds._reservation_id(command_id, tenant_id)
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET #s = :run, running_ts = :now "
                "REMOVE dispatch_claim, dispatch_claim_ts, capacity_reservation_id"
            ),
            ConditionExpression=(
                "#s = :creating AND host_id = :self "
                "AND capacity_reservation_id = :rid "
                "AND attribute_not_exists(dispatch_settle)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":run": "running",
                ":creating": "creating",
                ":self": instance_id,
                ":rid": rid,
                ":now": _now(),
            },
        )
        return True
    except ccf:
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[poller] mark_running {tenant_id} error: {e}")
        return False


def _bump_retry(tenant_id: str, command_id: str) -> int:
    """dispatch_retries+=1 且清 dispatch_claim,返回新值。超预算调用方转 requires_intervention。

    #412(codex review9 #3)—— fence dispatch_claim=:cid:只清【本命令】打的 claim。否则一个
    迟到的旧 poller 会清掉【新一轮 dispatch 刚打的 claim】,让新预留的活租户被后续误当 stale
    释放。CCF(claim 已被新命令接管)→ 返 -1,调用方跳过本租户的 retry/requeue(不误动新命令)。"""
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        resp = clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET dispatch_retries = if_not_exists(dispatch_retries, :z) + :one "
                "REMOVE dispatch_claim, dispatch_claim_ts"
            ),
            ConditionExpression="#s = :creating AND dispatch_claim = :cid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":one": 1,
                ":z": 0,
                ":cid": command_id,
                ":creating": "creating",
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["dispatch_retries"])
    except ccf:
        # claim 已被新命令接管 → 本租户已归新一轮 dispatch,不该由旧 poller 计 retry/清 claim。
        print(f"[poller] bump_retry {tenant_id} skipped: claim moved to newer command")
        return -1  # 调用方据此跳过 retry/requeue
    except Exception as e:  # noqa: BLE001
        print(f"[poller] bump_retry {tenant_id} error: {e}")
        return clients.DISPATCH_RETRY_BUDGET + 1  # 保守当已耗尽


def _claim_failed_settlement(
    tenant_id: str, instance_id: str, command_id: str, reservation_id: str
) -> bool:
    """Fence promotion before stopping a VM reported failed.

    The launcher can return nonzero after Firecracker has started. A durable
    dispatch_settle claim keeps both poller and host-agent promotion from racing
    the stop/release sequence. Re-entering the same command is idempotent.
    """
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET dispatch_settle = :cid",
            ConditionExpression=(
                "#s = :creating AND host_id = :host "
                "AND dispatch_claim = :cid AND capacity_reservation_id = :rid "
                "AND (attribute_not_exists(dispatch_settle) OR dispatch_settle = :cid)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":creating": "creating",
                ":host": instance_id,
                ":cid": command_id,
                ":rid": reservation_id,
            },
        )
        return True
    except ccf:
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[poller] settle fence {tenant_id} failed: {e}")
        return False


def _stop_failed_tenant(instance_id: str, tenant_id: str, vm_num: int) -> bool:
    tenant_arg = shlex.quote(str(tenant_id))
    vm_arg = shlex.quote(str(int(vm_num)))
    return ssm_dispatch._ssm_run(
        instance_id,
        f"/home/ubuntu/stop-vm.sh {tenant_arg} {vm_arg}",
        timeout=30,
    )


def _mark_requires_intervention(tenant_id: str) -> None:
    # ConditionExpression #s=:creating — 只把**仍在 creating** 的租户标 requires_intervention。
    # 无守卫时(修复前)是唯一漏状态守卫的终态写(三个兄弟 _mark_running/_flag_requires_
    # intervention/_mark_over_budget_best_effort 全带守卫):Failed/TimedOut 分支按 T0 快照
    # 逐条 mark,若 host-agent health reporter 在 T0 后已把该租户 creating→running(VM 实活,
    # SSM-TimedOut-但-launch-成功 的分叉),无条件写会把健康 running 掀回 requires_intervention
    # → 永久卡态(promote 只认 creating→running,不再碰它);覆盖 deleted 记录则复活成僵尸态。
    # 加守卫后:非 creating(已 running/deleted/stopped)→ CCF → no-op,不掀翻。
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :ri, requires_intervention_ts = :now",
            ConditionExpression="#s = :creating",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ri": "requires_intervention",
                ":creating": "creating",
                ":now": _now(),
            },
        )
    except ccf:
        # 租户已被并发改成 running/deleted/stopped → 不该掀回 requires_intervention,no-op。
        print(f"[poller] requires_intervention {tenant_id} skipped: no longer creating")
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
    """扫在途命令,推进终态或回滚。返回统计 dict(供指标/日志)。

    #315 SPLIT_BY_MODE(codex 判):**ddb 模式直接空转返回**。ddb 下 host-agent reconciler
    (每 5s 查 assignment 表)是 promote/重投的主驱动,dispatch 不写 inflight 标量;poller 若在
    ddb 下动容量(_rollback_host 按 batch 盲扣)会错扣被其他并发命令占用的槽位(codex 上轮 Error),
    900s reaper 只能事后修、中间过度装箱。故 ddb 模式 poller 不碰租户状态/assignment/host 容量。
    push 模式(无 assignment 表 + 无 host-agent reconciler)poller 仍是唯一 promote/回滚驱动,照常跑。
    """
    if clients.DISPATCH_MODE.lower() == "ddb":
        # #315(codex review6)—— ddb 模式 poller 纯空转:host-agent reconciler(每 5s 查
        # assignment 表)是 promote/重投的唯一驱动;dispatch 不写 inflight 标量(_try_reserve_host
        # write_inflight=False),_snapshot_hosts(gate_inflight=False)也【忽略】残留 inflight。
        # 故 ddb 稳态下 inflight 标量对调度完全无害,无需 drain。曾在 review4 加过一次性 drain
        # 清残留,但滚动切换期间"旧 push 在 scan 后又写了新 inflight"这类竞态下,CAS 只证 command_id
        # 未变、不证已过期,仍可能误删活跃 inflight(review6 P1)。切换前遗留的在途租户由 health_check
        # 的 900s reaper(卡 creating 兜底)收敛,不靠 poller。
        return {
            "hosts": 0,
            "success": 0,
            "failed": 0,
            "still_running": 0,
            "skipped_ddb": True,
        }
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
                    _mark_running(tid, instance_id, command_id)
            else:
                # 老脚本无 tenants 报告 → claim 反查降级 promote。#412 review2 #6:反查
                # 现在会抛;成功路径 VM 真起了,反查失败就跳过降级 promote(host-agent 5s
                # 自愈仍会 promote),不阻断下方 inflight 清理。
                # #412 review3 #2:一个 command_id 跨多 host,降级 promote 必须【按 host_id 收窄】
                # ——否则会把落在【别台 host】、命令还在跑的租户提前 promote、清其令牌,毁其失败回滚。
                try:
                    for t in _query_batch_tenants(command_id):
                        if t.get("host_id") == instance_id:
                            _mark_running(t["id"], instance_id, command_id)
                except Exception as e:  # noqa: BLE001
                    print(f"[poller] success-path batch scan {instance_id} failed "
                          f"(host-agent will promote): {e}")
            # 清 host inflight 标记(不回滚容量:VM 真起了)。
            # #315 guard(codex A_NEEDS_GUARD):去掉 host 级 inflight 门后,一台 host 可同时有
            # 多条并发在途命令,dispatch_inflight/dispatch_ssm_cid 是标量 last-write-wins。清理
            # 必须带 CAS 条件"当前值仍是本 poller 观测到的那条"(dispatch_inflight=:cid AND
            # dispatch_ssm_cid=:sc),否则本 poller 读到旧命令、处理期间新命令已覆盖标记,无条件
            # REMOVE 会把【新命令】的 inflight/ssm_cid 误清 → 新命令的租户 poller 再也追不到
            # (host-agent 5s 轮询仍会 promote,但 poller 兜底链断)。CCF = 已被新命令覆盖,
            # 本就不该由我清,静默跳过。
            ccf = clients.hosts_table.meta.client.exceptions.ConditionalCheckFailedException
            try:
                clients.hosts_table.update_item(
                    Key={"instance_id": instance_id},
                    UpdateExpression="REMOVE dispatch_inflight, dispatch_inflight_ts, dispatch_inflight_ts_epoch, dispatch_ssm_cid",
                    ConditionExpression="dispatch_inflight = :cid AND dispatch_ssm_cid = :sc",
                    ExpressionAttributeValues={":cid": command_id, ":sc": ssm_cid},
                )
            except ccf:
                # 已被新命令覆盖(并发在途)→ 不是本 poller 该清的标记,跳过。
                print(
                    f"[poller] clear inflight {instance_id} skipped: inflight/ssm_cid moved on"
                )
            except Exception as e:  # noqa: BLE001
                print(f"[poller] clear inflight {instance_id}: {e}")
            # DeleteParameter — 手动扫 part 编号(0..8 保守足够,每 part 携百 tenant)
            _cleanup_manifest_best_effort(command_id, instance_id)
            stats["success"] += 1
        elif status in ("Failed", "TimedOut", "Cancelled"):
            # #412(codex review #1)—— 一个 command_id 可跨多台 host(dispatch_batch 一次
            # invocation 给多台 host 各发一条命令、共用同一 command_id)。本 poller 迭代只处理
            # 【本 host(instance_id)】这条 SSM 命令的终态,故必须把 batch 收窄到 host_id==本 host
            # 的租户——否则会错误释放/重投别台 host 上还在跑的租户,扣错 host 账本、毁其放置。
            try:
                batch = [
                    t for t in _query_batch_tenants(command_id)
                    if t.get("host_id") == instance_id
                ]
            except Exception as e:  # noqa: BLE001
                # #412(codex review2 #6)—— 反查失败:不能当 batch 空(那会误清 inflight、
                # 搁浅残留预留)。保留 inflight + 跳过本 host,下轮 poll 重试反查。
                print(f"[poller] batch scan {instance_id} cmd={command_id} failed, "
                      f"retain inflight, retry next poll: {e}")
                stats["still_running"] += 1
                continue
            # #412(review7 #1 + review8 #1)—— 命令级 Failed/TimedOut/Cancelled ≠ 每个 VM 都没起。
            # 安全规则:**只释放 launch-all v2 报告【明确列为 failed】的租户**;launched/skipped 的
            # promote;其余(报告没提到的 unknown / 老脚本无报告 / TimedOut/Cancelled 根本没解析
            # stdout)一律【既不释放也不 promote,保留 creating + 令牌】,交 _reap_stuck_creating
            # (900s 超时)兜底。为什么不再"非 launched 就整批释放"(review8 #1):TimedOut/Cancelled
            # 的 report 为空 → 那样会把整批(可能含真起了的 VM)全释放 → 欠记账 + 重复 launch(红线)。
            # 宁可让真死的租户多等一个 reaper 周期(容量延迟回收),也绝不释放一个可能还活着的 VM。
            t_report = (report or {}).get("tenants") or {}
            launched_ok = {
                tid
                for k in ("launched", "skipped")
                for tid in (t_report.get(k) or [])
                if isinstance(tid, str) and tid
            }
            failed_reported = {
                tid for tid in (t_report.get("failed") or [])
                if isinstance(tid, str) and tid
            }
            for _tid in launched_ok:
                _mark_running(_tid, instance_id, command_id)  # 真起了 → promote(fence host_id=:self)
            # 只释放【明确 failed】的;unknown/无报告 → 保留,reaper 超时兜底(绝不释放可能活的 VM)。
            dvm = int(clients.VM_DEFAULT_VCPU or 2)
            dmm = int(clients.VM_DEFAULT_MEM or 4096)
            to_release = [t for t in batch if t["id"] in failed_reported]
            unknown = [
                t for t in batch
                if t["id"] not in failed_reported and t["id"] not in launched_ok
            ]
            if unknown:
                print(f"[poller] {instance_id} cmd={command_id}: {len(unknown)} tenant(s) "
                      f"unknown outcome (report incomplete/TimedOut) — retained for reaper")
            settled = []  # 释放已落定(consumed/already)的租户,才可推进 retry-budget/requeue
            # unknown 租户没定论(保留等 reaper)→ 本命令未落定,不清 inflight/manifest,
            # 下轮 poll 继续观察;reaper 900s 把 unknown 翻出 creating 后,批清空 → 自然落定。
            all_settled = not unknown
            for t in to_release:
                v, m = normalize_spec(t, dvm, dmm)
                rid = t.get("capacity_reservation_id") or ds._reservation_id(
                    command_id, t["id"]
                )
                if not _claim_failed_settlement(
                    t["id"], instance_id, command_id, rid
                ):
                    all_settled = False
                    continue
                if not _stop_failed_tenant(
                    instance_id, t["id"], int(t.get("vm_num", 1) or 1)
                ):
                    # Keep dispatch_settle + token + claim. Promotion is fenced,
                    # and the next poll retries the authoritative stop.
                    all_settled = False
                    continue
                if ds._release_reservation(t["id"], instance_id, rid, v, m) == ds.RELEASE_RETRY:
                    # 瞬时失败:令牌可能仍占容量。本轮【不动】该租户的 claim/inflight/retry
                    # (codex review2 #3:_bump_retry 会清 claim → 下轮 poll 反查不到、令牌搁浅、
                    # 最终误 requires_intervention 逃出 reaper 覆盖)。保留 claim+inflight,靠下轮
                    # poll 重新反查同一 command_id 再释放(令牌仍在 → 幂等消费一次)。
                    all_settled = False
                else:
                    settled.append(t)
            # inflight 是 host 级批状态;仅当本 host 令牌全部落定才清(带 CAS 防误清并发新命令)。
            # 有 retry 悬空则留 inflight —— 下轮 poll 仍能反查本 command_id 的残留租户继续释放。
            if all_settled:
                ds._clear_inflight_scalar(instance_id, command_id)
            # 只对释放落定的租户推进 retry-budget / 重投;retry 悬空的留原样等下轮。
            for t in settled:
                new_retries = _bump_retry(t["id"], command_id)
                if new_retries < 0:
                    # #412 review9 #3:claim 已被【新一轮 dispatch】接管 → 本租户已归新命令,
                    # 旧 poller 不再计 retry/重投它(否则会踩新预留)。跳过。
                    continue
                if new_retries > clients.DISPATCH_RETRY_BUDGET:
                    _mark_requires_intervention(t["id"])
                else:
                    _requeue(t["id"], _params_from_tenant(t))
            # manifest 仅在本 host 全部落定后清(未落定说明还要下轮 poll,清早了下轮取不到）。
            if all_settled:
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
