# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""dispatch_service — SQS 装箱 → 聚合 SSM (push) / assignments (pull) 编排层。

依赖:core.dispatch (纯函数) + core.clients (boto3 单例)。上游 consumers/dispatch.py
只做 SQS event 解析薄壳。契约 SPEC/specs/sqs-dispatch/interfaces.md。

关键节:
1. 认领闸:tenants 单条 CondUpdate 打上 dispatch_claim,重复入队自然去重。
2. 装箱:core.dispatch.pack(纯函数,零 I/O)。
3. 批量 CAS + 在途 token:每 host 一条 UpdateItem 原子写(next_vm_num/used_vcpu/
   dispatch_inflight),失败 → 该 host 批全 unplaced。
4. 分发:push=写 ParamStore manifest 分片 + SendCommand;pull=BatchWriteItem
   assignments 表(host-agent 拉)。simulated host 在 push 模式跳过。
5. 熔断:单 invocation SSM 连续失败 ≥ DISPATCH_CIRCUIT_THRESHOLD → 整批报失败 +
   put_metric_data DispatchCircuitOpen=1。
6. andon:每次 SendCommand/BatchWriteItem 前免缓存 GetParameter 单读,andon=stop
   fail-closed 停发。

boto3 client 从 core.clients 属性访问(测试 monkeypatch 友好,同 scheduling.py)。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from botocore.config import Config as _BotoConfig

import core.clients as clients
from core.dispatch import (
    MANIFEST_PART_MAX_BYTES,
    encode_manifest_line,
    pack,
    split_manifest_parts,
)
from core.utils import _now


# ── boto3 clients (lazy;测试重绑属性即接管) ────────────────────────────────
# PutParameter 需要 adaptive retry(SPEC 契约):写 SecureString 批量 part 时按
# ParamStore per-account throttle 会返回 ThrottlingException,adaptive 让 boto3
# 自己指数退避 + jitter。SendCommand 也走同 client(SSM API rate limit)。
_SSM_ADAPTIVE = _BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"})


def _ssm_adaptive():
    """The dispatch ParamStore/SendCommand path.

    Prefers an explicitly-provisioned adaptive-retry client at
    ``clients._ssm_dispatch_client``(deployments set this at CDK time via
    Lambda env AWS_RETRY_MODE=adaptive on the whole runtime, or the boot
    sequence caches an explicit boto3.client("ssm", config=...) there).
    Falls back to the ambient ``clients.ssm`` — always a real client in
    production and always the injected mock in tests. Never builds a fresh
    boto3 client at import time or on demand:that both defeats mocking and
    doubles the cold-start cost."""
    return getattr(clients, "_ssm_dispatch_client", None) or clients.ssm


def _cw():
    # cloudwatch is provisioned in core.clients iff DISPATCH_QUEUE_URL was set
    # at cold start;tests inject the mock. Never lazy-build a fresh client here.
    return clients.cloudwatch


def _sqs():
    """Reuse the ambient sqs client (created iff LIFECYCLE_QUEUE_URL OR
    DISPATCH_QUEUE_URL was set in core.clients cold-start). Tests inject a mock
    at ``clients.sqs`` directly."""
    return clients.sqs


# ── andon 急停(未缓存单读) ─────────────────────────────────────────────
def _check_andon() -> Tuple[bool, str]:
    """免缓存单读 /openclaw/dispatch/config → andon 字段。稳态 0.5 TPS,对 40 TPS 池零压。

    读失败 fail-closed(返回 (True, reason))停发。参数由 CDK StringParameter 托管带
    默认值 andon=ok,不会 ParameterNotFound。"""
    key = f"{clients.DISPATCH_PARAM_PREFIX}/config"
    try:
        resp = _ssm_adaptive().get_parameter(Name=key)
        raw = (resp.get("Parameter") or {}).get("Value") or ""
        val = _parse_kv(raw).get("andon", "ok").strip().lower()
        if val == "stop":
            return True, "andon=stop"
        return False, ""
    except Exception as e:  # noqa: BLE001
        return True, f"andon-read-failed: {type(e).__name__}"


def _parse_kv(raw: str) -> Dict[str, str]:
    """极简 k=v 一行/多行解析,不引 yaml。andon=ok 或 andon=stop 单行足够。"""
    out: Dict[str, str] = {}
    for line in raw.replace(";", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ── SQS record parser ─────────────────────────────────────────────────
def _parse_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 SQS Records 列表解析为 [(msg_id, action, tenant_id, params)]。schema v1。"""
    out = []
    for rec in records:
        mid = rec.get("messageId")
        body = rec.get("body") or "{}"
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            out.append({"msg_id": mid, "invalid": True})
            continue
        if int(msg.get("v", 0) or 0) != 1:
            out.append({"msg_id": mid, "invalid": True})
            continue
        out.append(
            {
                "msg_id": mid,
                "action": msg.get("action"),
                "tenant_id": msg.get("tenant_id"),
                "request_token": msg.get("request_token"),
                "params": msg.get("params") or {},
            }
        )
    return out


# ── 认领闸 ────────────────────────────────────────────────────────────
def _claim_tenants(
    records: List[Dict[str, Any]], claim_id: str, now: str
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """对每条 record 的 tenant_id 做 tenants 表 CondUpdate 打认领标记。

    ConditionalCheckFailedException = 该 tenant 已被别的 invocation 认领 或已不是
    creating(重放/竞争)→ 本消息直接 ack 掉(不进 batchItemFailures)。
    其它异常 = 5xx 走批失败重试。返回 (winners, ack_and_drop)。
    """
    winners: List[Dict[str, Any]] = []
    ack_drop: List[str] = []
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    # stale claim 回收(真机洪峰抓出的死锁):消费中途非 ccf 异常炸批时,claim 已打上
    # 但工作没做完,消息回队列重投,若只认 attribute_not_exists 会永远被挡 → 租户
    # 永久卡 creating。照 Powertools INPROGRESS 超时释放同款语义:超过阈值的旧
    # claim 视为死锁残留,允许接管。ISO8601 字符串可直接字典序比较。
    stale_before = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - clients.DISPATCH_CLAIM_STALE_SEC),
    )
    for rec in records:
        if rec.get("invalid"):
            # 坏消息直接 drop,不重试(4xx-like:毒消息无限重投是事故)
            ack_drop.append(rec["msg_id"])
            continue
        tid = rec.get("tenant_id")
        if not tid or rec.get("action") != "create":
            ack_drop.append(rec["msg_id"])
            continue
        try:
            clients.tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression=(
                    "SET dispatch_claim = :cid, dispatch_claim_ts = :now"
                ),
                # 预算硬闸(真机 e2ev2-probe 抓出:retries=5 仍在循环):Poller 的
                # requires_intervention 只覆盖 SSM Failed 分支,visibility 重投
                # 这条路会绕开它,消费端必须自己拒收超预算租户。
                ConditionExpression=(
                    "#s = :creating AND (attribute_not_exists(dispatch_claim) "
                    "OR dispatch_claim_ts < :stale) "
                    "AND (attribute_not_exists(dispatch_retries) "
                    "OR dispatch_retries <= :budget)"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":cid": claim_id,
                    ":now": now,
                    ":creating": "creating",
                    ":stale": stale_before,
                    ":budget": clients.DISPATCH_RETRY_BUDGET,
                },
            )
            winners.append(rec)
        except ccf:
            # 别的实例赢了 / 状态不再 creating / 超预算 → 静默 ack drop。
            # 超预算的租户顺手转 requires_intervention(best-effort,幂等:条件写
            # 只在还是 creating 且 retries 超限时生效,防止误标已 running 的)。
            _mark_over_budget_best_effort(tid)
            ack_drop.append(rec["msg_id"])
    return winners, ack_drop


def _mark_over_budget_best_effort(tenant_id: str) -> None:
    """认领被拒时检查是否超预算,是则转 requires_intervention(不再无限重投)。"""
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :ri, requires_intervention_ts = :now",
            ConditionExpression="#s = :creating AND dispatch_retries > :budget",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ri": "requires_intervention",
                ":creating": "creating",
                ":now": _now(),
                ":budget": clients.DISPATCH_RETRY_BUDGET,
            },
        )
        print(f"[dispatch] {tenant_id} over retry budget → requires_intervention")
    except ccf:
        pass  # 没超预算/已不是 creating——正常竞争路径,静默
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] over-budget mark {tenant_id} non-fatal: {e}")


# ── hosts 快照 + inflight/CAS ─────────────────────────────────────────
def _snapshot_hosts(now_epoch: int) -> List[Dict[str, Any]]:
    """扫 active/idle host,算 free_slots + inflight_ok + simulated。ConsistentRead 强一致
    (与 core.scheduling._find_host 同款,防跨实例装满同一 host)。"""
    hosts = clients.hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    ).get("Items", [])

    ttl = clients.DISPATCH_INFLIGHT_TTL_SEC
    out = []
    for h in hosts:
        total_vcpu = int(h.get("total_vcpu", 0) or 0)
        used_vcpu = int(h.get("used_vcpu", 0) or 0)
        allocatable = int(total_vcpu * float(clients.CPU_OVERCOMMIT_RATIO or 1.0))
        # 一租户默认 1 slot(2 vCPU 单位);free_slots = 剩余 vcpu / VM_DEFAULT_VCPU 保守值
        vcpu_per_vm = max(1, int(clients.VM_DEFAULT_VCPU or 2))
        free_slots = max(0, (allocatable - used_vcpu) // vcpu_per_vm)
        inflight_ts = int(h.get("dispatch_inflight_ts_epoch", 0) or 0)
        inflight_ok = (not h.get("dispatch_inflight")) or (
            inflight_ts and (now_epoch - inflight_ts) > ttl
        )
        out.append(
            {
                "instance_id": h["instance_id"],
                "free_slots": free_slots,
                "allocatable_vcpu": allocatable,
                "simulated": bool(h.get("simulated", False)),
                "inflight_ok": bool(inflight_ok),
                "raw": h,
            }
        )
    return out


def _try_reserve_host(
    instance_id: str,
    n: int,
    command_id: str,
    now_epoch: int,
    allocatable_vcpu: int,
) -> Optional[int]:
    """单 host 单条 UpdateItem: next_vm_num+=n / used_vcpu+=n*vcpu / 打 inflight_ok。

    返回该批第一个 vm_num(reserved base);失败(容量不够或 inflight 未过期)→ None。
    DDB ConditionExpression 不支持算术,照 handler._reserve_slot 范式调用方预计算
    cap(:cap_v = allocatable - dv),条件写成 used_vcpu <= :cap_v ——写前 used_vcpu
    加上本批增量不超 allocatable 的等价式。真机洪峰抓出:`used_vcpu + :dv <=
    total_vcpu * :ratio` 直接 ValidationException(Syntax error token "+")。
    """
    ccf = clients.hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    ttl = clients.DISPATCH_INFLIGHT_TTL_SEC
    vcpu_per_vm = max(1, int(clients.VM_DEFAULT_VCPU or 2))
    mem_per_vm = max(0, int(clients.VM_DEFAULT_MEM or 2048))
    dv = n * vcpu_per_vm
    cap_v = int(allocatable_vcpu) - dv
    if cap_v < 0:
        return None
    try:
        resp = clients.hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "SET next_vm_num = if_not_exists(next_vm_num, :zero) + :n, "
                "used_vcpu = if_not_exists(used_vcpu, :zero) + :dv, "
                "used_mem_mb = if_not_exists(used_mem_mb, :zero) + :dm, "
                "vm_count = if_not_exists(vm_count, :zero) + :n, "
                "dispatch_inflight = :cid, dispatch_inflight_ts = :now, "
                "dispatch_inflight_ts_epoch = :now_epoch"
            ),
            ConditionExpression=(
                "used_vcpu <= :cap_v "
                "AND (attribute_not_exists(dispatch_inflight) "
                "OR dispatch_inflight_ts_epoch < :expired)"
            ),
            ExpressionAttributeValues={
                ":n": n,
                ":dv": dv,
                ":dm": n * mem_per_vm,
                ":zero": 0,
                ":cid": command_id,
                ":now": _now(),
                ":now_epoch": now_epoch,
                ":expired": now_epoch - ttl,
                ":cap_v": cap_v,
            },
            ReturnValues="UPDATED_NEW",
        )
        new_next = int(resp["Attributes"]["next_vm_num"])
        return new_next - n + 1  # base vm_num of this batch
    except ccf:
        return None


def _backfill_placement(
    tenants: List[Dict[str, Any]], instance_id: str, base_vm_num: int
) -> None:
    """装箱 CAS 成功后把放置结果回写 tenants(#139 真机缺陷修复)。

    同步路径的 item 一开始就带 host_id/vm_num/guest_ip/host_port(tenant_service
    ~L750);dispatch 路径这些值在消费端装箱才产生,不回写则 stop/restart/delete
    找不到 VM 在哪台 host——e2e 只验"到 running"没验往返,#139 抓出。guest_ip 用
    auth._guest_ip(与 launch-vm.sh /30 编址的唯一真相源对齐)。条件写限 creating:
    重复投递的输家批(claim 已被接管)不得覆盖赢家的放置。best-effort:单租户
    失败不炸批,Poller/对账兜底。"""
    from core.auth import _guest_ip  # noqa: PLC0415 — 避免模块级环(auth→clients)

    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    for offset, t in enumerate(tenants):
        vm_num = base_vm_num + offset
        try:
            clients.tenants_table.update_item(
                Key={"id": t["tenant_id"]},
                UpdateExpression=(
                    "SET host_id = :h, vm_num = :n, guest_ip = :g, "
                    "host_port = :p, updated_at = :now"
                ),
                ConditionExpression="#s = :creating",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":h": instance_id,
                    ":n": vm_num,
                    ":g": _guest_ip(vm_num),
                    ":p": clients.VM_PORT_BASE + vm_num - 1,
                    ":creating": "creating",
                    ":now": _now(),
                },
            )
        except ccf:
            pass  # 状态已非 creating(赢家已推进)——不覆盖
        except Exception as e:  # noqa: BLE001
            print(f"[dispatch] placement backfill {t['tenant_id']} non-fatal: {e}")


def _rollback_host(instance_id: str, n: int) -> None:
    """回滚 CAS:used_vcpu-N / used_mem_mb-N*vm / vm_count-N。next_vm_num 不倒退。

    与 scheduling._release_slot 同款 best-effort。REMOVE dispatch_inflight 一并做。
    """
    vcpu_per_vm = max(1, int(clients.VM_DEFAULT_VCPU or 2))
    mem_per_vm = max(0, int(clients.VM_DEFAULT_MEM or 2048))
    try:
        clients.hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "SET used_vcpu = used_vcpu - :dv, "
                "used_mem_mb = used_mem_mb - :dm, "
                "vm_count = vm_count - :n "
                "REMOVE dispatch_inflight, dispatch_inflight_ts, dispatch_inflight_ts_epoch, dispatch_ssm_cid"
            ),
            ConditionExpression="used_vcpu >= :dv AND vm_count >= :n",
            ExpressionAttributeValues={
                ":dv": n * vcpu_per_vm,
                ":dm": n * mem_per_vm,
                ":n": n,
            },
        )
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] rollback {instance_id} non-fatal: {e}")


# ── manifest 上传 + SendCommand (push) ───────────────────────────────
def _put_manifest_parts(
    command_id: str,
    instance_id: str,
    tenants: List[Dict[str, Any]],
    base_vm_num: int,
) -> int:
    """写 ParamStore SecureString 分片,返回 part 数量。异常 fail-loud:调用方回滚该 host 批。"""
    lines = []
    for offset, t in enumerate(tenants):
        params = t.get("params") or {}
        lines.append(
            encode_manifest_line(
                {
                    "tenant_id": t["tenant_id"],
                    "vm_num": base_vm_num + offset,
                    "vcpu": params.get("vcpu", clients.VM_DEFAULT_VCPU),
                    "mem_mb": params.get("mem_mb", clients.VM_DEFAULT_MEM),
                    "chat_ep": params.get("chat_ep", False),
                    # #188 — control-plane 预铸的密文/配对元数据透传给 host 冷注入。
                    # 密文按 tenant_id 一一对应(create 时铸;EncryptionContext 绑
                    # token=tenant_id / device=owner_id),encode 只在非空时写 g/d,
                    # 空 → feature-off 行逐字节不变。part 整体又是 SecureString。
                    "gateway_token_ct": params.get("gateway_token_ct"),
                    "device_paired_b64": params.get("device_paired_b64"),
                }
            )
        )
    parts = split_manifest_parts(lines, max_bytes=MANIFEST_PART_MAX_BYTES)
    ssm = _ssm_adaptive()
    prefix = f"{clients.DISPATCH_PARAM_PREFIX}/manifests/{command_id}/{instance_id}"
    for idx, body in enumerate(parts):
        ssm.put_parameter(
            Name=f"{prefix}/part-{idx}",
            Type="SecureString",
            Value=body,
            Overwrite=True,
        )
    return len(parts)


def _delete_manifest_parts(command_id: str, instance_id: str, part_count: int) -> None:
    """Poller 终态清理;只允许 manifests/ 前缀,防连删 config。"""
    ssm = _ssm_adaptive()
    prefix = f"{clients.DISPATCH_PARAM_PREFIX}/manifests/{command_id}/{instance_id}"
    if not prefix.startswith(f"{clients.DISPATCH_PARAM_PREFIX}/manifests/"):
        # defence-in-depth:防未来有人改 prefix 变量误伤 config
        raise RuntimeError(f"refusing delete outside manifests/ prefix: {prefix}")
    for idx in range(part_count):
        try:
            ssm.delete_parameter(Name=f"{prefix}/part-{idx}")
        except Exception as e:  # noqa: BLE001
            print(f"[dispatch] delete manifest part-{idx} non-fatal: {e}")


def _derive_exec_timeout(batch_size: int) -> int:
    """SSM executionTimeout = ceil(batch × per-vm-budget / parallelism) + 120s 余量,
    并 ≤ visibility_timeout - 60s(防假超时→回滚活 VM→账本分叉)。"""
    per_vm = max(1, int(clients.DISPATCH_PER_VM_BUDGET_SEC or 8))
    parallel = max(1, int(clients.DISPATCH_MAX_PARALLEL or 96))
    est = -(-batch_size * per_vm // parallel) + 120  # ceil div
    cap = max(60, int(clients.DISPATCH_VISIBILITY_TIMEOUT_SEC or 900) - 60)
    return min(est, cap)


def _record_ssm_cid(instance_id: str, ssm_cid: str) -> None:
    """SendCommand 成功后把 SSM 分配的 CommandId 记到 host(dispatch_ssm_cid)。

    dispatch_inflight 存的是内部批次号 cmd-<epoch>-<msgid>(23 字符,用于租户
    dispatch_claim 关联);GetCommandInvocation 只认 SSM 的 36 位 UUID——两个 id
    职责不同,Poller 用 dispatch_ssm_cid 查 SSM 终态、用 dispatch_inflight 查租户。
    (真机 e2e 抓出:漏记则 Poller 参数校验永远失败,租户永久卡 creating。)
    best-effort:写失败不回滚命令(命令已在跑),留给 stale-TTL 兜底。
    """
    try:
        clients.hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression="SET dispatch_ssm_cid = :sc",
            ExpressionAttributeValues={":sc": ssm_cid},
        )
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] record ssm_cid {instance_id} non-fatal: {e}")


def _send_ssm_manifest(
    instance_id: str, command_id: str, part_count: int, batch_size: int
) -> Optional[str]:
    """发聚合 SSM 命令。返回 CommandId(SSM 分配的);发送失败返回 None(调用方回滚)。"""
    ssm = _ssm_adaptive()
    parallel = max(1, int(clients.DISPATCH_MAX_PARALLEL or 96))
    exec_timeout = _derive_exec_timeout(batch_size)
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=120,  # invocation delivery
            Parameters={
                "commands": [
                    f"bash /home/ubuntu/launch-all-vms.sh {command_id} {part_count} {parallel} "
                    f"{clients.DISPATCH_PARAM_PREFIX}"
                ],
                "executionTimeout": [str(exec_timeout)],
            },
        )
        return (resp.get("Command") or {}).get("CommandId")
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] SendCommand {instance_id} failed: {e}")
        return None


def _send_ssm_from_ddb(
    instance_id: str, command_id: str, batch_size: int
) -> Optional[str]:
    """ddb 载体:发 --from-ddb 叫醒命令(数据已在 assignments 表,SSM 只传信号)。

    与 push 的区别:命令带 --from-ddb,host 查 openclaw-assignments 自取本批,而不是
    拉 ParamStore 分片——PutParameter 退出热路径。命令仍带 command_id/count/parallel
    (count 只作为 host 侧的期望条数校验用;真实清单从表查)。返回 SSM CommandId,
    发送失败返回 None(调用方回滚 + 清 assignments)。
    """
    ssm = _ssm_adaptive()
    parallel = max(1, int(clients.DISPATCH_MAX_PARALLEL or 96))
    exec_timeout = _derive_exec_timeout(batch_size)
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=120,
            Parameters={
                "commands": [
                    f"bash /home/ubuntu/launch-all-vms.sh --from-ddb {command_id} "
                    f"{batch_size} {parallel} {clients.assignments_table.table_name}"
                ],
                "executionTimeout": [str(exec_timeout)],
            },
        )
        return (resp.get("Command") or {}).get("CommandId")
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] --from-ddb SendCommand {instance_id} failed: {e}")
        return None


def _clear_assignments(instance_id: str, tenants: List[Dict[str, Any]]) -> None:
    """ddb 载体回滚:删本批刚写的 pending assignments(SSM 叫醒失败时)。
    best-effort:删不掉留 24h TTL 兜底,host 侧 vm.json check 防重复 launch。"""
    if not clients.assignments_table:
        return
    try:
        with clients.assignments_table.batch_writer() as bw:
            for t in tenants:
                bw.delete_item(
                    Key={"instance_id": instance_id, "tenant_id": t["tenant_id"]}
                )
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] clear assignments {instance_id} non-fatal: {e}")


# ── assignments (pull 模式) ───────────────────────────────────────────
def _write_assignments(
    instance_id: str,
    tenants: List[Dict[str, Any]],
    base_vm_num: int,
    now_epoch: int,
) -> bool:
    """Pull 模式:写 openclaw-assignments (PK=instance_id, SK=tenant_id) status=pending。
    失败 → 调用方按 host 批失败回滚。"""
    if not clients.assignments_table:
        print("[dispatch] pull mode but ASSIGNMENTS_TABLE unset — refuse")
        return False
    ttl_epoch = now_epoch + 24 * 3600
    try:
        with clients.assignments_table.batch_writer() as bw:
            for offset, t in enumerate(tenants):
                params = t.get("params") or {}
                item = {
                    "instance_id": instance_id,
                    "tenant_id": t["tenant_id"],
                    "action": "create",
                    "vm_num": base_vm_num + offset,
                    "vcpu": int(params.get("vcpu", clients.VM_DEFAULT_VCPU)),
                    "mem_mb": int(params.get("mem_mb", clients.VM_DEFAULT_MEM)),
                    "chat_ep": bool(params.get("chat_ep", False)),
                    "status": "pending",
                    "created_ts": _now(),
                    "ttl": ttl_epoch,
                }
                # #188 — 与 push manifest 对称:密文/配对元数据透传给 host 冷注入,
                # 仅非空写入(空 → 不加字段,feature-off item 逐字节不变)。密文按
                # tenant_id 一一对应,EncryptionContext 绑定,host 用对的 EC 才解得开。
                gw_ct = params.get("gateway_token_ct")
                if gw_ct:
                    item["gateway_token_ct"] = gw_ct
                device_paired = params.get("device_paired_b64")
                if device_paired:
                    item["device_paired_b64"] = device_paired
                bw.put_item(Item=item)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] assignments write {instance_id} failed: {e}")
        return False


# ── 熔断 ───────────────────────────────────────────────────────────────
def _emit_circuit_open() -> None:
    try:
        _cw().put_metric_data(
            Namespace="OpenClaw/Dispatch",
            MetricData=[
                {"MetricName": "DispatchCircuitOpen", "Value": 1.0, "Unit": "Count"}
            ],
        )
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] circuit-open metric emit failed: {e}")


# ── consumer 入口 ─────────────────────────────────────────────────────
def dispatch_batch(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """SQS event 入口。返回 {"batchItemFailures": [{"itemIdentifier": ...}]}。"""
    now_epoch = int(time.time())
    command_id = f"cmd-{now_epoch}-{records[0].get('messageId', 'x')[:8] if records else 'empty'}"

    # 0) andon check(免缓存)
    stop, reason = _check_andon()
    if stop:
        print(f"[dispatch] andon halt: {reason} — all batch retried")
        return {
            "batchItemFailures": [
                {"itemIdentifier": r.get("messageId")}
                for r in records
                if r.get("messageId")
            ]
        }

    parsed = _parse_records(records)
    if not parsed:
        return {"batchItemFailures": []}

    # 1) 认领闸
    winners, _ack_drop = _claim_tenants(parsed, command_id, _now())
    # 认领输家(重复/竞争)静默 ack,不进 batchItemFailures
    if not winners:
        return {"batchItemFailures": []}

    # 2) hosts 快照 + 装箱
    hosts = _snapshot_hosts(now_epoch)
    mode = clients.DISPATCH_MODE.lower()
    push_mode = mode == "push"
    # push/ddb 都发真 SSM,simulated host 收不到命令 → 装箱跳过它们(压测用);
    # pull 二期由 host-agent 轮询,simulated host 可参与。
    dispatches_ssm = mode in ("push", "ddb")
    pending = [{"tenant_id": w["tenant_id"], "params": w["params"]} for w in winners]
    result = pack(
        pending,
        hosts,
        per_host_cap=clients.DISPATCH_MAX_PARALLEL,
        skip_simulated=dispatches_ssm,
    )

    # msg_id 反查:tenant_id → msg_id(唯一,认领已保证)
    tid_to_msg = {w["tenant_id"]: w["msg_id"] for w in winners}
    alloc_by_host = {h["instance_id"]: h["allocatable_vcpu"] for h in hosts}
    failures: List[str] = []
    ssm_consec_fail = 0

    # #141 — fail-loud:每个进 batchItemFailures(→ 重投,超预算才进 DLQ)的租户
    # 打一条带 tenant_id + host + 原因的日志。修复前这些 append 点静默,突发下
    # 部分租户卡 creating 时 CloudWatch 零错误日志、运维只能靠 DLQ 深度发现
    # (issue #141 真机实证)。日志入口收敛到一个 helper,防再有 append 漏记。
    def _fail(tid: str, reason: str, host: str = "-") -> None:
        print(
            f"[dispatch] FAIL tenant={tid} host={host} cmd={command_id} "
            f"reason={reason} — requeue for retry"
        )
        failures.append(tid_to_msg[tid])

    # 3) 每 host 一批:CAS → 分发
    for instance_id, batch in result.assignments.items():
        n = len(batch)
        base = _try_reserve_host(
            instance_id, n, command_id, now_epoch, alloc_by_host.get(instance_id, 0)
        )
        if base is None:
            # CAS 输(容量不够 or inflight 未过期)→ 该批全 unplaced 走重试
            for t in batch:
                _fail(t["tenant_id"], "host CAS lost (capacity/inflight)", instance_id)
            continue

        # #139:CAS 赢了 = 放置已定,先回写 host_id/vm_num/guest_ip 再分发
        # (顺序重要:先落账再发命令,SSM 再快也查得到 VM 在哪台)。
        _backfill_placement(batch, instance_id, base)

        if push_mode:
            try:
                part_count = _put_manifest_parts(command_id, instance_id, batch, base)
            except Exception as e:  # noqa: BLE001
                _rollback_host(instance_id, n)
                for t in batch:
                    _fail(t["tenant_id"], f"manifest write failed: {e}", instance_id)
                continue
            sent = _send_ssm_manifest(instance_id, command_id, part_count, n)
            if sent is None:
                ssm_consec_fail += 1
                _rollback_host(instance_id, n)
                _delete_manifest_parts(command_id, instance_id, part_count)
                for t in batch:
                    _fail(t["tenant_id"], "SSM SendCommand failed", instance_id)
                if ssm_consec_fail >= clients.DISPATCH_CIRCUIT_THRESHOLD:
                    _emit_circuit_open()
                    # 整批未处理的 host 全部报失败退出装箱。每个租户走 _fail
                    # 打 per-tenant 日志(与其它失败路径一致),再补一条熔断汇总。
                    remaining_ids = [
                        (t["tenant_id"], hid)
                        for hid, todo in result.assignments.items()
                        for t in todo
                        if hid != instance_id
                        and tid_to_msg[t["tenant_id"]] not in failures
                    ]
                    print(
                        f"[dispatch] CIRCUIT OPEN cmd={command_id}: "
                        f"{ssm_consec_fail} consecutive SSM failures, aborting "
                        f"{len(remaining_ids)} remaining tenants for retry"
                    )
                    for tid, hid in remaining_ids:
                        _fail(tid, "circuit open: aborted before dispatch", hid)
                    break
            else:
                ssm_consec_fail = 0
                _record_ssm_cid(instance_id, sent)
        elif mode == "ddb":
            # ddb 载体:先写 assignments(数据),再发 --from-ddb 叫醒(信号)。
            # 写序重要:表里有行,叫醒命令到达时 host 才查得到(同步路径先落账
            # 再发命令同款,#139 教训)。写失败或叫醒失败都回滚 host + 清 assignments。
            if not _write_assignments(instance_id, batch, base, now_epoch):
                _rollback_host(instance_id, n)
                for t in batch:
                    _fail(t["tenant_id"], "assignments write failed", instance_id)
                continue
            sent = _send_ssm_from_ddb(instance_id, command_id, n)
            if sent is None:
                ssm_consec_fail += 1
                _rollback_host(instance_id, n)
                _clear_assignments(instance_id, batch)
                for t in batch:
                    _fail(t["tenant_id"], "SSM from-ddb wake failed", instance_id)
                if ssm_consec_fail >= clients.DISPATCH_CIRCUIT_THRESHOLD:
                    _emit_circuit_open()
                    remaining_ids = [
                        (t["tenant_id"], hid)
                        for hid, todo in result.assignments.items()
                        for t in todo
                        if hid != instance_id
                        and tid_to_msg[t["tenant_id"]] not in failures
                    ]
                    print(
                        f"[dispatch] CIRCUIT OPEN cmd={command_id}: "
                        f"{ssm_consec_fail} consecutive SSM failures, aborting "
                        f"{len(remaining_ids)} remaining tenants for retry"
                    )
                    for tid, hid in remaining_ids:
                        _fail(tid, "circuit open: aborted before dispatch", hid)
                    break
            else:
                ssm_consec_fail = 0
                _record_ssm_cid(instance_id, sent)
        else:  # pull(二期 host-agent 轮询,无 SSM 叫醒)
            ok = _write_assignments(instance_id, batch, base, now_epoch)
            if not ok:
                _rollback_host(instance_id, n)
                for t in batch:
                    _fail(t["tenant_id"], "assignments write failed", instance_id)

    # 4) unplaced(容量不够、装箱阶段跳过的 host) → 全报失败重试
    for t in result.unplaced:
        _fail(t["tenant_id"], "unplaced: no host capacity this round")

    # dedup 保序
    seen = set()
    dedup = []
    for m in failures:
        if m and m not in seen:
            seen.add(m)
            dedup.append(m)

    # 5) 报失败回队列的租户必须释放本 invocation 打的 claim(真机洪峰抓出的死锁:
    # 不释放的话,重投回来过认领闸撞"claim 已存在且新鲜"被静默 ack,租户永久卡
    # creating——unplaced 尾巴 ~16% 全灭在这)。条件写限定 claim 归属本 claim_id,
    # 绝不误删并发实例的新 claim;顺带 ADD dispatch_retries 计预算。
    msg_to_tid = {v: k for k, v in tid_to_msg.items()}
    _release_claims([msg_to_tid[m] for m in dedup if m in msg_to_tid], command_id)
    # #141 — batch-level fail-loud summary: 一眼看清本次 invoke 收敛多少/回退多少,
    # 不用 grep per-tenant 行。dedup 非空 = 有租户回队列重投(超预算才最终进 DLQ)。
    print(
        f"[dispatch] batch done cmd={command_id}: "
        f"won={len(winners)} requeued={len(dedup)}"
    )
    return {"batchItemFailures": [{"itemIdentifier": m} for m in dedup]}


def _release_claims(tenant_ids: List[str], claim_id: str) -> None:
    """失败重试路径释放认领标记(条件:claim 是我打的),让重投消息能重新认领。

    #141 收敛主修:ADD dispatch_retries 后,若新值达到重试预算,立刻转
    requires_intervention。为什么在这里而非只靠认领闸的 over-budget 分支——
    时序:SQS dlq_max_receive_count=N,消息第 N 次接收失败时 dispatch_retries
    从 N-1 ADD 到 N,此刻 SQS receiveCount=N=maxReceiveCount,消息转 DLQ,
    「第 N+1 次接收」永不发生 → 认领闸的 over-budget(`dispatch_retries > budget`,
    需第 N+1 次认领被拒才触发)永不命中 → 租户永久卡 creating(DoD#1 现象)。
    故在最后一次失败释放(retries 达 budget)时主动转终态,赶在进 DLQ 前;
    条件 `>= budget`,幂等(仅 creating 且达阈值才转,不误标已 running/终态的)。
    ReturnValues 拿新值避免二次读。"""
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    for tid in tenant_ids:
        try:
            resp = clients.tenants_table.update_item(
                Key={"id": tid},
                UpdateExpression="REMOVE dispatch_claim, dispatch_claim_ts "
                "ADD dispatch_retries :one",
                ConditionExpression="dispatch_claim = :cid",
                ExpressionAttributeValues={":cid": claim_id, ":one": 1},
                ReturnValues="UPDATED_NEW",
            )
            new_retries = int((resp.get("Attributes") or {}).get("dispatch_retries", 0))
            # 不变量(锁死):dispatch 队列 dlq_max_receive_count(dispatch_infra.py,默认 3)
            # 必须 >= DISPATCH_RETRY_BUDGET(clients.py,默认 3)。否则消息在 retries 达到
            # budget 之前(receiveCount < budget)就进 DLQ,`>= budget` 触发不了 → 仍永久
            # stuck。当前两者默认都 3(成立)。改任一配置须同步保持 maxReceive >= budget。
            if new_retries >= clients.DISPATCH_RETRY_BUDGET:
                _mark_retry_exhausted(tid)
        except ccf:
            pass  # claim 已被别的实例接管/已释放,不动
        except Exception as e:  # noqa: BLE001
            print(f"[dispatch] release claim {tid} failed (non-fatal): {e}")


def _mark_retry_exhausted(tenant_id: str) -> None:
    """#141:重试预算耗尽(retries 达 budget)→ 转 requires_intervention。

    在消息进 DLQ 前的最后一次失败释放时调用。条件写限 creating 且 retries 已达
    budget(幂等:重投竞争或已推进到 running 的不误标)。best-effort:失败不炸
    invocation,Poller/对账兜底。"""
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :ri, requires_intervention_ts = :now",
            ConditionExpression="#s = :creating AND dispatch_retries >= :budget",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ri": "requires_intervention",
                ":creating": "creating",
                ":now": _now(),
                ":budget": clients.DISPATCH_RETRY_BUDGET,
            },
        )
        print(
            f"[dispatch] {tenant_id} retry budget exhausted "
            f"(>= {clients.DISPATCH_RETRY_BUDGET}) → requires_intervention"
        )
    except ccf:
        pass  # 未达阈值/已不是 creating——正常路径,静默
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] retry-exhausted mark {tenant_id} non-fatal: {e}")
