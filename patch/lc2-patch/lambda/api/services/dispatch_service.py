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

from concurrent.futures import ThreadPoolExecutor
import json
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from botocore.config import Config as _BotoConfig
from botocore.exceptions import ClientError

import core.capacity as capacity
import core.clients as clients
import core.ddb_scan as ddb_scan  # #432 —— Scan 必须翻页
import core.dispatch.binpack as dispatch_binpack
import core.create_deadline as create_deadline  # #562 — 死线口径,与 tenant_service 同一份
import core.host_profile as host_profile
import core.host_taint as host_taint  # #540 — 队列路径读污点,判定复用写侧纯函数
# #491 — 物理 tap 占用守卫。core.scheduling 只依赖 core.*(不 import services),
# 故此处无循环导入风险。
import core.scheduling as scheduling
# #562 形态第 4 条 —— 复用死线执行者的【同一份】围栏与扩容实现,不在这里另写一份。
# 变异 M7 已经证过一次「同一判定写两份就会漂」的代价(归因在两处各判一次,结论能互相矛盾)。
# 分层允许 services→services(import-layers.sh:25 只禁 routes/consumers/router)。
import services.deadline_executor as deadline_executor
from core.dispatch import (
    MANIFEST_PART_MAX_BYTES,
    encode_manifest_line,
    normalize_spec,
    pack,
    split_manifest_parts,
)
from core.utils import _now


_dispatch_monotonic = time.monotonic


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
        # 缩短可见性(960→15s),不必等默认 visibility 才重投。SQS 事件每条 record 原生带。
        rh = rec.get("receiptHandle")
        # #522 P1-2 —— SQS 原生把 ApproximateReceiveCount 放在 record.attributes(字符串)。
        # 供升级宽限的收敛 backstop:到最后一次投递(rc >= maxReceive,下次即 DLQ)仍无处可放 →
        # 直接标 requires_intervention,杜绝 no-budget 宽限把消息静默送进 DLQ 却让租户卡 creating。
        try:
            rc = int((rec.get("attributes") or {}).get("ApproximateReceiveCount", 1) or 1)
        except (TypeError, ValueError):
            rc = 1
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
                "receipt_handle": rh,
                "receive_count": rc,
                "action": msg.get("action"),
                "tenant_id": msg.get("tenant_id"),
                "request_token": msg.get("request_token"),
                "params": msg.get("params") or {},
                # #562 G7 —— 死线随消息走。这里只【带出来】,不在解析阶段判定:解析是纯函数
                # (被多处以脱包方式加载测试),判定要读时钟,分开才好测。
                # 老消息(本改动部署【之前】入队的)没有这个字段 → None → 下游按「未知」处理
                # = 不丢弃、走正常链路,由死线执行者兜底。这是升级期的必然形态:队列里会同时
                # 有带死线与不带死线的消息,不许因为缺字段就丢掉客户已受理的创建。
                "deadline": msg.get(create_deadline.MSG_DEADLINE_KEY),
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
    if not records:
        return [], []
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    # stale claim 回收(真机洪峰抓出的死锁):消费中途非 ccf 异常炸批时,claim 已打上
    # 但工作没做完,消息回队列重投,若只认 attribute_not_exists 会永远被挡 → 租户
    # 永久卡 creating。照 Powertools INPROGRESS 超时释放同款语义:超过阈值的旧
    # claim 视为死锁残留,允许接管。ISO8601 字符串可直接字典序比较。
    stale_before = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - clients.DISPATCH_CLAIM_STALE_SEC),
    )

    def _claim_one(rec):
        if rec.get("invalid"):
            # 坏消息直接 drop,不重试(4xx-like:毒消息无限重投是事故)
            return "ack_drop", rec["msg_id"]
        tid = rec.get("tenant_id")
        if not tid or rec.get("action") != "create":
            return "ack_drop", rec["msg_id"]
        try:
            # #671 —— 这里只调用 Table 的无状态 generated action,不触发 resource load/
            # metadata 变更；I/O 立即委托给共享的 thread-safe meta.client。表句柄在 invocation
            # 内固定,每个 worker 只持有自己的参数/响应,因此可安全并发且保留现有测试注入面。
            _claim_resp = clients.tenants_table.update_item(
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
                # reservation_id】(上一次 dispatch 装箱过、释放遇 RETRY 没清掉),赢得认领后
                # 记下那张陈旧令牌,装箱前先【结算掉】它(见 dispatch_batch)。否则新一轮 reserve
                # 的 attribute_not_exists(capacity_reservation_id) 恒失败 → 白烧 retry 预算 →
                # 最终误判 failed/requires_intervention(令牌自己占着自己,活锁)。
                ReturnValues="ALL_OLD",
            )
            old = (_claim_resp or {}).get("Attributes") or {}
            if old.get("capacity_reservation_id"):
                rec["stale_reservation"] = {
                    "rid": old["capacity_reservation_id"],
                    "host_id": old.get("host_id"),
                    "vcpu": int(old.get("vcpu", 0) or 0),
                    "mem_mb": int(old.get("mem_mb", 0) or 0),
                }
            return "winner", rec
        except ccf:
            # 别的实例赢了 / 状态不再 creating / 超预算 → 静默 ack drop。
            # 超预算的租户顺手转 requires_intervention(best-effort,幂等:条件写
            # 只在还是 creating 且 retries 超限时生效,防止误标已 running 的)。
            _mark_over_budget_best_effort(tid)
            return "ack_drop", rec["msg_id"]

    # #671 —— batch 默认最多 30,并发上限固定 8 防止认领闸把 DynamoDB 打成限流。
    # futures 按原输入序取结果,显式保住 winners/ack_drop 的顺序语义。
    with ThreadPoolExecutor(max_workers=min(8, len(records))) as pool:
        outcomes = list(pool.map(_claim_one, records))

    winners: List[Dict[str, Any]] = []
    ack_drop: List[str] = []
    for kind, value in outcomes:
        if kind == "winner":
            winners.append(value)
        else:
            ack_drop.append(value)
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
def _snapshot_hosts(now_epoch: int, gate_inflight: bool = True) -> List[Dict[str, Any]]:
    """扫 active/idle host,算 free_slots + inflight_ok + simulated。ConsistentRead 强一致
    (与 core.scheduling._find_host 同款,防跨实例装满同一 host)。

    - push 模式(gate_inflight=True):保留旧逻辑——host 有未过期 inflight → inflight_ok=False,
      binpack 跳过它(host 级串行,poller 靠 inflight 标量追踪 SSM 终态需要这个串行)。
    - ddb 模式(gate_inflight=False):inflight_ok 恒 True,不因在途命令挡装箱(host-agent 每 5s
      从 assignment 表兜底,允许一台 host 并发多批,可扩 1000 host;容量安全由 slot 级 CAS 保证)。
    """
    # 隐藏一部分机队:装箱把它们当不存在 → unplaced → 而 #562 之后 unplaced 会在死线时
    # 被判 failed(capacity_unavailable)。也就是说客户拿到「容量不足」,而那些机器正空着。
    # 判据不是「命中数小」而是「全表字节数」:filter 在 1MB 读之后才过滤,openclaw-hosts
    # 又累积 deleted 死行(实测 39 行里 33 行 deleted)。
    hosts = ddb_scan.scan_all(
        clients.hosts_table,
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    )

    # 长批次认领可能已耗时 > TTL,此时刚上报的满盘记录(ts≈real-now)相对 now_epoch 会落进"离谱
    # 未来"被误判 fail-open。用 scan 时刻做基准,ts 与它的差恒是真实新鲜度。inflight 判定仍用
    # now_epoch(那是它与 SendCommand 时序的既有语义,不动)。
    disk_now = int(time.time())
    ttl = clients.DISPATCH_INFLIGHT_TTL_SEC
    out = []
    for h in hosts:
        total_vcpu = int(h.get("total_vcpu", 0) or 0)
        used_vcpu = int(h.get("used_vcpu", 0) or 0)
        # 但机制就位:每类机型可分别设。理想比 = 物理供给(GB/vCPU) ÷ 租户需求(GB/vCPU)。
        cpu_ratio, mem_ratio = host_profile.ratios(
            h,
            (clients.CPU_OVERCOMMIT_RATIO, clients.MEM_OVERCOMMIT_RATIO),
            clients.OVERCOMMIT_BY_FAMILY,
        )
        allocatable = capacity.allocatable(total_vcpu, cpu_ratio)
        # 与同步 create 路径(handler.py:954-960)一致,防大内存租户超卖 OOM。
        # ★缺 total_mem_mb(旧 host DDB item 没写这字段)→ allocatable_mem=0 当【未知】哨兵,
        # 下游 mem 闸跳过(回落纯 vcpu 闸),绝不因字段缺失误拒整台 host(codex review 指出)。
        total_mem = int(h.get("total_mem_mb", 0) or 0)
        allocatable_mem = capacity.allocatable(total_mem, mem_ratio)
        used_mem = int(h.get("used_mem_mb", 0) or 0)
        # (旧 free_slots=剩余vcpu//2 把 1c:2G 租户的可装数腰斩到 282,达不到 380)。mem_known=缺
        # total_mem_mb 的老 host 内存容量未知 → 装箱侧 fail-safe 不调度(不 fail-open 当无限内存)。
        free_vcpu = max(0, allocatable - used_vcpu)
        mem_known = total_mem > 0
        free_mem = max(0, allocatable_mem - used_mem)
        # disk_check_ts_epoch。剩余低于水位就不接新租户(防 /data 满 → mkdir No space →
        # requires_intervention)。fail-open:字段缺失(旧 host 从没上报)或上报陈旧
        # (host-agent 挂了/漏报,读数不可信)→ disk_ok=True 退回旧行为,绝不用过期读数误杀。
        disk_ok = _host_disk_ok(h, disk_now)
        # 占用可能超出声明(balloon 是 best-effort)。这里用 host 自报的实测 MemAvailable
        # 兜底,与磁盘门同款三段逻辑(门关/无信号/陈旧 一律 fail-open,只在新鲜确认
        # 不足时阻断),基准同样用 disk_now(扫描此刻)而非 now_epoch。
        mem_ok = capacity.mem_ok(
            h, clients.MEM_SAFETY_FLOOR_RATIO, clients.MEM_CHECK_TTL_SEC, disk_now
        )
        # 放【整批】:逐个问都通过,累加起来照样跌破水位(同步 create 一次一个,传
        # needed_mb 就够;批量不行)。所以把【水位之上的实测余量】并进 free_mem 预算 ——
        # binpack 已按每租户 mem 逐个扣减这个预算(binpack.py:148/163),批内累加就被水位
        # 自然夹住,且 binpack 保持零依赖(不必知道水位这回事)。
        # 两个预算取小:声明维(账本不超卖)与物理维(实测不跌破水位)都不能破。
        headroom = capacity.mem_headroom_mb(
            h, clients.MEM_SAFETY_FLOOR_RATIO, clients.MEM_CHECK_TTL_SEC, disk_now
        )
        if headroom is not None:
            free_mem = min(free_mem, headroom)
        if gate_inflight:
            inflight_ts = int(h.get("dispatch_inflight_ts_epoch", 0) or 0)
            inflight_ok = (not h.get("dispatch_inflight")) or (
                inflight_ts and (now_epoch - inflight_ts) > ttl
            )
        else:
            inflight_ok = True  # ddb:不设门,并发多批
        # #540 — 污点(cordon)软门。与 disk_ok / mem_ok / inflight_ok 同款:在这里算好一个
        # 扁平布尔传给 binpack,binpack 保持零依赖纯函数(它被 importlib 脱包加载,不能
        # import core.host_taint,也不该读私有 "raw")。
        # 这里【不 fail-open】,与磁盘/内存两个软门相反:那两个门的信号来自 host 自报,
        # 字段缺失或陈旧就意味着"读数不可信",误杀整台机器的代价大于放行。污点不一样 ——
        # 它是运维在控制面显式写下的意图,`is_tainted` 只在写侧写 true、取消用 REMOVE,
        # 不存在"陈旧读数"这回事;字段不存在就是没被标,判 taint_ok=True 本身就是正确答案。
        taint_ok = not host_taint.is_tainted(h)
        # #549 — 心跳陈旧闸(与选点侧 _find_host 同口径):last_seen 超阈值的 host 不接新租户。
        # 基准用 disk_now(扫描此刻),与 disk/mem 软门一致。缺信号/未来时间戳 fail-open。
        seen_ok = capacity.seen_fresh(h, clients.HOST_SEEN_STALE_SEC, disk_now)
        out.append(
            {
                "instance_id": h["instance_id"],
                "free_vcpu": free_vcpu,
                "free_mem": free_mem,
                "mem_known": mem_known,
                "allocatable_vcpu": allocatable,
                "allocatable_mem": allocatable_mem,
                "simulated": bool(h.get("simulated", False)),
                "inflight_ok": bool(inflight_ok),
                "disk_ok": bool(disk_ok),
                "mem_ok": bool(mem_ok),
                "taint_ok": bool(taint_ok),  # #540
                "seen_ok": bool(seen_ok),  # #549
                # (零 boto3、被 tests/test_dispatch_binpack.py 用 importlib 脱包加载),
                # 所以它不能 import clients/host_profile、也不该读私有 "raw" —— tier
                # 在这里算好传下去。
                "affinity_tier": host_profile.affinity_tier(h, clients.FAMILY_ORDER),
                "raw": h,
            }
        )
    return out


def _iso_to_epoch(s: Any) -> Optional[int]:
    """容忍 'Z' 与 '+00:00' 两种 ISO8601 UTC 渲染(host.upgrading_at 来自 utils._now() 的
    isoformat = 带微秒 +00:00;历史记录可能是 …Z)。解析失败回 None(调用方按 fail-safe 处理)。"""
    if not s:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def _fleet_has_fresh_upgrade(grace_sec: int, now_epoch: int) -> bool:
    """#522 P1-2 —— fleet 是否有 host 正处在【新鲜】的 upgrading 窗口(upgrading_at 距今
    < grace_sec)。有 → dispatch 把本轮 unplaced 视作瞬态(host 升级完会回 active/idle),
    按 no-budget 重投处理(记 reserve_retry_tids:claim 保留、不计 dispatch_retries、不缩
    visibility),避免升级窗口内新建租户被误推终态 requires_intervention。无(或升级卡死超
    grace)→ unplaced 仍按容量不足计预算,最终收敛 requires_intervention(fail-loud,卡死
    升级是运维问题)。grace<=0 关此宽限(退回旧行为)。

    为什么 host 在 upgrading 时会导致 unplaced:_snapshot_hosts 只扫 active/idle,升级中的
    host 被排除出候选。全 fleet 都在升级(或活跃 host 都满)时,binpack 无处可放 → unplaced。

    fail-safe:扫描/解析异常一律当【无新鲜升级】(不授予宽限)—— 宁可退回旧的计预算路径,
    也不因异常把新建租户无限 park 在队列里。只在有 unplaced 时才调用(见 dispatch_batch),
    正常无容量压力路径零额外扫描开销。"""
    if grace_sec <= 0:
        return False
    # 分页扫(codex F2):单次 Scan 只读 ≤1MB 原始数据后才 apply FilterExpression;大 fleet 的
    # upgrading host 可能落在后页,单次扫会漏 → 误判无升级 → 租户白烧预算(bug 复现)。故 follow
    # LastEvaluatedKey 直到扫完或【提前命中】首个新鲜升级(命中即返,不必扫全表)。
    start_key = None
    # 页数上限护栏:hosts 表体量有界(~千台,每页 1MB),正常 1-2 页即扫完。上限纯属防御——
    # 真实 DDB 终会返回空 LastEvaluatedKey;万一遇到异常(或测试 mock 恒返同一 key)也不至于死循环。
    _MAX_PAGES = 100
    try:
        for _ in range(_MAX_PAGES):
            kw = {
                "FilterExpression": "#s = :upg",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":upg": "upgrading"},
                "ProjectionExpression": "upgrading_at",
                "ConsistentRead": True,
            }
            if start_key:
                kw["ExclusiveStartKey"] = start_key
            resp = clients.hosts_table.scan(**kw)
            for h in resp.get("Items", []):
                started = _iso_to_epoch(h.get("upgrading_at"))
                # 未来/刚起(delta<0)也算新鲜:安全方向(授予宽限=no-budget,不会误终态)。
                if started is not None and (now_epoch - started) < grace_sec:
                    return True
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                return False
        # 到达页数上限仍没扫完(异常大表 / 异常 key)→ 保守当【无新鲜升级】(退回计预算,收敛)。
        print(f"[dispatch] upgrade-grace scan hit page cap {_MAX_PAGES} → no grace")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] upgrade-grace scan failed → no grace: {e}")
        return False


def _mark_stuck_creating_intervention(tenant_id: str, claim_id: str) -> None:
    """#522 P1-2(codex F1)收敛 backstop —— SQS 投递预算耗尽(下次即 DLQ)仍 unplaced → 标
    requires_intervention。为什么不能只靠 _release_claims 的 `dispatch_retries >= budget`:升级
    宽限走 no-budget 重投(不计 dispatch_retries),receiveCount 与 dispatch_retries 脱钩,到 DLQ
    时 retries 可能 < budget → 那道终态标记打不出 → 消息静默进 DLQ、租户永久卡 creating。故按
    【SQS 投递耗尽】直接收敛,失败要响(loud)而非 silent strand。

    幂等 + 认领归属(codex F2 复审):条件 `#s=creating AND dispatch_claim=本 claim_id`——
    ① 仅 creating 才转,不误标已 running/终态;② **只在仍持本 invocation 的 claim 时转**,防
    本 invocation 已跑成陈旧(claim 过期被新一轮接管、新 invocation 正在放置该租户)时把它误
    推终态、覆盖新认领(与 _release_claims 的 `dispatch_claim=:cid` 同款归属守卫)。"""
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :ri, requires_intervention_ts = :now",
            ConditionExpression="#s = :creating AND dispatch_claim = :cid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ri": "requires_intervention",
                ":creating": "creating",
                ":cid": claim_id,
                ":now": _now(),
            },
        )
        print(
            f"[dispatch] {tenant_id} unplaced through SQS receive budget "
            f"→ requires_intervention"
        )
    except ccf:
        pass  # 已非 creating / claim 已被接管——幂等跳过(交持有者)
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] stuck-creating mark {tenant_id} non-fatal: {e}")


def _host_disk_ok(h: Dict[str, Any], now_epoch: int) -> bool:
    """#340 — host /data 是否还有足够物理余量接新租户(装箱磁盘软门的判据)。

    True = 可接(含所有 fail-open 情形);False = 【新鲜确认】盘将满、跳过该 host。

    只在【新鲜确认盘将满】时才阻断,其余一律 fail-open(退回旧的"只看 vcpu/mem"):
    - 门关闭(DISPATCH_HOST_DISK_MIN_FREE_MB<=0)→ True;
    - 没有磁盘信号(avail_disk_mb 缺失 → 旧 host / host-agent 未升级)→ True(过渡期);
    - 磁盘信号陈旧(now - disk_check_ts_epoch > TTL,或无时间戳)→ True;
    - 新鲜且剩余 < 水位 → False(唯一阻断分支);新鲜且剩余 ≥ 水位 → True。

    ★为什么陈旧要 fail-open(codex score:陈旧阻断会误摘健康 host + 旧字段永久封锁):
    磁盘上报已剥离到【独立线程】(host-agent._disk_report_loop),不再搭 poll 心跳便车,
    所以读数陈旧不再是"VM probe 慢拖累",而是真·agent 没在跑。agent 死是正交问题,交
    health_check(租户全 stale → SSM 重启 agent)+ ASG(实例失联替换)兜底,磁盘门不越权
    用一个可能过期的读数【永久封锁】host(那会让 agent 回滚后残留旧字段的 host 再不可调度)。
    代价权衡:漏挡"曾满但现在 agent 死"的 host 窗口 → 该 host 若真满,派过去 mkdir 失败仍
    走 requires_intervention(退化回本 bug,但仅限"agent 死且盘真满"这个窄交集,且 agent
    一旦被 health_check 拉活、独立线程立刻刷新读数即恢复拦截);误挡健康 host 的代价(整机
    产能损失 + 无法自愈)更大。故对陈旧取 fail-open。TTL<=0 = 不校验新鲜度(有值就按值判)。
    """
    min_free = int(clients.DISPATCH_HOST_DISK_MIN_FREE_MB or 0)
    if min_free <= 0:
        return True  # 门关闭
    if "avail_disk_mb" not in h:
        return True  # 没上报 → fail-open(旧 host / 未升级 / 取不到磁盘)
    # 信任边界外(DDB item 可能被别的写者写脏/半写):任何 coerce 失败都当【不可信信号】
    try:
        ttl = int(clients.DISPATCH_DISK_REPORT_TTL_SEC or 0)
        if ttl > 0:
            ts = int(h.get("disk_check_ts_epoch", 0) or 0)
            # now_epoch 是 invocation 起点;认领 + host scan 期间独立线程可能刚上报,ts 会略大于
            # now_epoch(codex review:那样会被"未来时间戳"误判 fail-open,即使报的是满盘)。故给
            # 时钟漂移容差 = TTL:|now - ts| ≤ TTL 都算【新鲜】按值判;只有离谱的过去(陈旧,agent
            # 死)或离谱的未来(> now+TTL,时钟错乱/伪造)才当不可信 → fail-open。无/坏时间戳同样放行。
            if ts <= 0 or ts > now_epoch + ttl or (now_epoch - ts) > ttl:
                return True
        return int(h.get("avail_disk_mb", 0) or 0) >= min_free
    except (TypeError, ValueError):
        return True  # 畸形数值 → 不可信 → fail-open(单 host,不炸批)


def _reservation_id(command_id: str, tenant_id: str) -> str:
    """本次预留的唯一可消费令牌(#412)。每租户每次装箱唯一 → 释放侧条件写
    `capacity_reservation_id = :rid` 保证【恰好一个写手】消费它、扣一次账本。

    为什么不能用 host_id 当锚(codex #3 ABA):host_id 会被同租户后续重新落到
    【同一台 host】复用 → 迟到的旧释放匹配上新放置 → 误扣。command_id 也不够
    (同命令重投会复用)。command_id:tenant_id 组合每次装箱唯一(command_id 含
    epoch+msgid),且随租户行存活到 delete,是跨 4 条释放路径的互斥锚。"""
    return f"{command_id}:{tenant_id}"


# #661 —— reserve 争用从 4 次线性等待提高到 7 次 full-jitter 指数退避。20ms 起步能覆盖
# 同一批 Lambda 的毫秒级事务交错，单次封顶 500ms 避免尾部一次睡太久；即使随机数每次
# 都取上界，7 次累计也只有 1.62s，并再由 2s 总预算硬夹住，远小于 create 的 128s
# 执行段，不会为了抢账本把真正的 VM 启动预算吃掉。
_RESERVE_STATS = {"attempts": 0}
_RESERVE_MAX_ATTEMPTS = 7
_RESERVE_BACKOFF_BASE_SEC = 0.02
_RESERVE_BACKOFF_MAX_SEC = 0.5
_RESERVE_BACKOFF_TOTAL_BUDGET_SEC = 2.0

# #661 —— reserve_retry_tids 保 claim，不能套 capacity 段“先释放再缩 visibility”的门。
# 争用是毫秒级瞬时态，固定 5s 已足够让同批事务错峰，也明显低于 15s 量级上界；实际
# 等待仍会被剩余死线夹小，死线已到或未知时不安排这条主动重投。
_RESERVE_REQUEUE_WAIT_SEC = 5

# #661 —— 最多换 3 台 host，对齐 RM 的 random-3；连同首次 host 共 4 次 reserve。
# 每次 reserve 的 C 项 full-jitter 硬上限是 2s，所以累计争用预算 = (1 + 3) × 2s = 8s，
# 明显小于 create_deadline.EXEC_BUDGET_SEC=128s，保留至少 120s 给真实 VM 启动段。
_RESERVE_HOST_SWITCH_MAX = 3
_RESERVE_HOST_RETRY_TOTAL_BUDGET_SEC = (
    (1 + _RESERVE_HOST_SWITCH_MAX) * _RESERVE_BACKOFF_TOTAL_BUDGET_SEC
)


def _sleep_reserve_backoff(attempt: int, spent: float, rng=None) -> float:
    """按 full jitter 睡一次，并返回累计墙钟预算；任何路径都不突破总预算。"""
    remaining = max(0.0, _RESERVE_BACKOFF_TOTAL_BUDGET_SEC - spent)
    ceiling = min(
        _RESERVE_BACKOFF_BASE_SEC * (2**attempt),
        _RESERVE_BACKOFF_MAX_SEC,
        remaining,
    )
    if ceiling <= 0:
        return spent
    source = rng or random
    delay = source.uniform(0.0, ceiling)
    if delay > 0:
        time.sleep(delay)
    return spent + delay


def _reserve_batch_txn(
    tenants: List[Dict[str, Any]],
    instance_id: str,
    command_id: str,
    now_epoch: int,
    expected_next: int,
    allocatable_vcpu: int,
    sum_vcpu: int,
    sum_mem: int,
    allocatable_mem: int,
    specs: List[Tuple[int, int]],
    write_inflight: bool = True,
    rootfs_version: str = "",
    immutable_version: str = "",
    occupied_hint=None,
) -> Optional[int]:
    """#412 —— 每 host 一批【一个 TransactWriteItems】:host 账本增量 + 每租户放置写
    + 唯一 capacity_reservation_id,全有或全无。返回该批 base vm_num;取消(容量/乐观锁
    /delete 抢赢/冲突)→ None。

    替代旧的"_try_reserve_host(整批 CAS)+ _backfill_placement(逐租户另写)"两步:两步之间
    delete 把某租户翻 deleting → 该租户 backfill CCF 跳过、host_id 永不写,而 host 账本增量
    已落 → 那份增量【无主】,delete(if host_id)与 reaper(status=creating)都不认领 →
    host 账本慢性高估(#412 真机报告)。合成一个事务后:任一租户 status≠creating → 整批取消
    → host 增量也不生效 → 无无主增量。建立不变量【used_* 为租户 T 增量 ⟺ T 带
    capacity_reservation_id】,释放侧(_release_reservation)据此令牌幂等消费。

    事务项(N≤DISPATCH_MAX_PARALLEL=96,+1 host 项 ≤97 < DDB 100 上限):
    - host 项(Update):next_vm_num/used_vcpu/used_mem_mb/vm_count 增量;条件
      next_vm_num=:expected(乐观锁,同步 create 路径 _reserve_slot 同款)AND 容量双闸;
      push 模式并入 inflight 标量写 + 排他门(语义逐字保留)。
    - 每租户项(Update):SET host_id/vm_num/guest_ip/host_port/capacity_reservation_id;
      条件 status=creating AND dispatch_claim=:cid AND attribute_not_exists(
      capacity_reservation_id)——最后一项防 crash 重投对已预留租户二次增量(codex #2)。

    vm_num:事务不返回 Attributes,故不能读回 next_vm_num。用乐观锁把 base 钉死为
    expected_next(=快照的 next_vm_num);并发另一批改了它 → 事务取消 → 走 CAS-loss 重投。
    """
    from core.auth import _guest_ip  # noqa: PLC0415 — 避免模块级环(auth→clients)

    dv = int(sum_vcpu)
    dm = int(sum_mem)
    cap_v = int(allocatable_vcpu) - dv
    cap_m = int(allocatable_mem) - dm
    if cap_v < 0 or cap_m < 0:
        return None  # fail-safe:任一维负(含 mem 未知 allocatable_mem<=0)→ 拒

    n = len(tenants)
    atomic_claim = n <= scheduling.MAX_ATOMIC_SLOT_CLAIM
    if not atomic_claim:
        # 超阈值时保留既有扫描判定，不生成会撞 DDB 300-operator 上限的条件表达式。
        print(
            f"[dispatch] WARN atomic phys-slot claim disabled host={instance_id} "
            f"cmd={command_id} n={n} limit={scheduling.MAX_ATOMIC_SLOT_CLAIM}; "
            "falling back to scan-only guard"
        )
    batch_ids = {t["tenant_id"] for t in tenants}
    # 直接返 None 当 CAS-loss → 输家白烧 tenant retry 预算、最终误 requires_intervention(明明有
    # 容量)。这里对【纯 next_vm_num 冲突】(host 项 idx0 取消、租户项都没失败)做【有界 reread-retry】
    # (重读 next_vm_num 重算 base 再试,有界指数退避+jitter,避免同批调用同步醒来再次互撞),
    # 【不】计租户预算;真容量不够/delete 抢赢(租户项失败)则照旧返 None 走重投。
    _backoff_spent = 0.0
    # #671 —— hint 只负责挑一个更可能成功的候选 base；no-cross-tenant 的权威仍是
    # 同一事务里的 attribute_not_exists(ps_N)。只有 atomic_claim=True 才有这层原子门,
    # 所以超 120 的 scan-only 回落必须逐圈实时扫描；None 也必须走原路径而非当空集合。
    _use_occupied_hint = atomic_claim and occupied_hint is not None
    for _attempt in range(_RESERVE_MAX_ATTEMPTS):
        _RESERVE_STATS["attempts"] += 1
        _attempt_started = time.monotonic()
        if _use_occupied_hint:
            occupied = occupied_hint
            base = scheduling.first_free_phys_run(expected_next, n, occupied)
        else:
            base, occupied = scheduling.next_free_phys_run(
                instance_id, expected_next, n, exclude_ids=batch_ids
            )
        if occupied is None:
            return _RESERVE_TRANSIENT
        if base is None:
            return None
        r = _reserve_batch_txn_once(
            tenants, instance_id, command_id, now_epoch, expected_next,
            cap_v, cap_m, dv, dm, n, write_inflight, _guest_ip, rootfs_version,
            base=base, atomic_claim=atomic_claim, immutable_version=immutable_version,
        )
        if r == _RESERVE_TXN_CONFLICT:
            # 明确的事务冲突/限流(TransactionConflict/throttle)→ 纯瞬时,重读重算重试。
            fresh_next = _read_next_vm_num(instance_id)
            if fresh_next is None:
                return _RESERVE_TRANSIENT  # 读不到 host → 瞬时,重投不烧预算(review8 #3)
            expected_next = fresh_next
            # #671 —— 预算计真实墙钟而不是只计 sleep:慢 scan/事务本身也会吃掉
            # invocation 时间,若不计会让名义 2s 的 reserve 合法跑到数秒甚至更久。
            _backoff_spent += max(0.0, time.monotonic() - _attempt_started)
            if _backoff_spent >= _RESERVE_BACKOFF_TOTAL_BUDGET_SEC:
                break
            _backoff_spent = _sleep_reserve_backoff(_attempt, _backoff_spent)
            if _backoff_spent >= _RESERVE_BACKOFF_TOTAL_BUDGET_SEC:
                break
            continue
        if r != _RESERVE_HOST_CCF:
            return r  # base vm_num(成功)或 None(真失败:租户项 CCF = delete 抢赢)
        # host 项 CCF:可能是 next_vm_num 乐观锁冲突,【也可能】是容量/inflight 门失败
        # 判别:变了 = 真 next_vm_num 竞争 → 重算重试;没变 = 容量/inflight 失败 → 真失败返 None
        # (别当瞬时空转烧完 4 次再 TRANSIENT,那会让满 host 的批延迟重投)。
        fresh_next = _read_next_vm_num(instance_id)
        if fresh_next is None:
            return _RESERVE_TRANSIENT  # 读不到 host → 瞬时
        if fresh_next == expected_next:
            fresh_base, fresh_occupied = scheduling.next_free_phys_run(
                instance_id, expected_next, n, exclude_ids=batch_ids
            )
            if fresh_occupied is None:
                return _RESERVE_TRANSIENT
            if fresh_base != base:
                # next 未变但 ps_* 被并发者占走；重算同一游标后的新空段再试。
                # 这次结果来自【实时扫描】,可刷新候选 hint；检测本身绝不能用旧 hint,
                # 否则 fresh_base 恒等于 base,会把并发占号永久误判成真容量不足。
                if _use_occupied_hint:
                    occupied_hint = fresh_occupied
                _backoff_spent += max(0.0, time.monotonic() - _attempt_started)
                if _backoff_spent >= _RESERVE_BACKOFF_TOTAL_BUDGET_SEC:
                    break
                _backoff_spent = _sleep_reserve_backoff(_attempt, _backoff_spent)
                if _backoff_spent >= _RESERVE_BACKOFF_TOTAL_BUDGET_SEC:
                    break
                continue
            # next_vm_num 没动 → host CCF 是容量/inflight 门(非乐观锁)→ 真容量类失败。
            return None
        expected_next = fresh_next  # next_vm_num 真被并发批推进了 → 重算 base 重试
        _backoff_spent += max(0.0, time.monotonic() - _attempt_started)
        if _backoff_spent >= _RESERVE_BACKOFF_TOTAL_BUDGET_SEC:
            break
        _backoff_spent = _sleep_reserve_backoff(_attempt, _backoff_spent)
        if _backoff_spent >= _RESERVE_BACKOFF_TOTAL_BUDGET_SEC:
            break
    print(f"[dispatch] reserve next_vm_num conflict exhausted host={instance_id} cmd={command_id}")
    return _RESERVE_TRANSIENT


_RESERVE_HOST_CCF = "__host_ccf__"            # 哨兵:host 项条件失败(next_vm_num 竞争 或 容量/inflight
#                                              门失败——DDB 不区分,调用方重读 next_vm_num 判别)
_RESERVE_TXN_CONFLICT = "__txn_conflict__"    # 哨兵:明确瞬时(TransactionConflict/throttle),重读重试
_RESERVE_TRANSIENT = "__reserve_transient__"  # 哨兵:瞬时高竞争耗尽/读失败,重投不烧预算


def _read_next_vm_num(instance_id: str) -> Optional[int]:
    """强一致读 host 的 next_vm_num(reserve 乐观锁冲突后重算 base 用)。读不到/出错返 None。"""
    try:
        resp = clients.hosts_table.get_item(
            Key={"instance_id": instance_id},
            ConsistentRead=True,
            ProjectionExpression="next_vm_num",
        )
        item = resp.get("Item")
        if not item:
            return None
        return int(item.get("next_vm_num", 1) or 1)
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] reread next_vm_num {instance_id} failed: {e}")
        return None


def _reserve_batch_txn_once(
    tenants, instance_id, command_id, now_epoch, expected_next,
    cap_v, cap_m, dv, dm, n, write_inflight, _guest_ip, rootfs_version="",
    base=None, atomic_claim=True, immutable_version="",
):
    """跑一次 reserve 事务。返回:base vm_num(成功)/ None(真失败:租户项 CCF=delete 抢赢)
    / _RESERVE_TXN_CONFLICT(明确瞬时冲突/限流)/ _RESERVE_HOST_CCF(host 项条件失败,调用方重读
    next_vm_num 判别是乐观锁竞争还是容量/inflight 门失败)。"""
    # 保持旧调用签名兼容；正常入口总会显式传本轮扫描选出的 base。
    base = expected_next if base is None else base
    # ---- host 项 ----
    host_set = (
        "next_vm_num = :next_after, "
        "used_vcpu = if_not_exists(used_vcpu, :zero) + :dv, "
        "used_mem_mb = if_not_exists(used_mem_mb, :zero) + :dm, "
        "vm_count = if_not_exists(vm_count, :zero) + :n"
    )
    host_vals: Dict[str, Any] = {
        ":n": n,
        ":dv": dv,
        ":dm": dm,
        ":zero": 0,
        ":expected": expected_next,
        ":next_after": base + n,
        ":cap_v": cap_v,
        ":cap_m": cap_m,
    }
    host_cond = (
        "next_vm_num = :expected AND used_vcpu <= :cap_v AND used_mem_mb <= :cap_m"
    )
    # #540 — 污点原子门。队列路径的窗口比同步 create 更宽:_snapshot_hosts 扫一次、binpack
    # 装【整批】、然后才逐 host 写,期间运维完全可能标记其中一台。快照里的 taint_ok 是软门
    # (装箱时不选它),这条条件写才是原子的那一层。
    host_cond += " AND " + host_taint.NOT_TAINTED_CONDITION
    host_vals.update(host_taint.NOT_TAINTED_VALUES)
    host_names = {}
    if atomic_claim:
        claim_cond, claim_set, host_names, value_keys = scheduling.slot_claim_clause(
            range(base, base + n)
        )
        host_set += ", " + claim_set
        host_cond += " AND " + claim_cond
        for value_key, tenant in zip(value_keys, tenants):
            host_vals[value_key] = tenant["tenant_id"]
    host_update_expr = "SET " + host_set
    if write_inflight:
        host_update_expr = (
            "SET " + host_set + ", dispatch_inflight = :cid, "
            "dispatch_inflight_ts = :now, dispatch_inflight_ts_epoch = :now_epoch "
            "REMOVE dispatch_ssm_cid"
        )
        host_cond += (
            " AND (attribute_not_exists(dispatch_inflight) "
            "OR dispatch_inflight_ts_epoch < :expired)"
        )
        host_vals.update(
            {
                ":cid": command_id,
                ":now": _now(),
                ":now_epoch": now_epoch,
                ":expired": now_epoch - clients.DISPATCH_INFLIGHT_TTL_SEC,
            }
        )
    host_update = {
        "TableName": clients.hosts_table.table_name,
        "Key": {"instance_id": instance_id},
        "UpdateExpression": host_update_expr,
        "ConditionExpression": host_cond,
        "ExpressionAttributeValues": host_vals,
    }
    if host_names:
        host_update["ExpressionAttributeNames"] = host_names
    txn_items: List[Dict[str, Any]] = [{"Update": host_update}]
    # ---- 每租户项 ----
    now = _now()
    # version 非空 → SET rootfs_version(+ ≤256B 才建查询投影 q_rootfs_version,否则 REMOVE 投影);
    # version 空 → REMOVE 两个陈旧字段(重投可能落到别版本写过的行,不清则 GET 与 GSI 不一致)。
    _rv_set = ""
    _rv_remove: List[str] = []
    if rootfs_version:
        _rv_set = ", rootfs_version = :rv"
        if len(rootfs_version.encode("utf-8")) <= 256:
            _rv_set += ", q_rootfs_version = :rv"
        else:
            _rv_remove = ["q_rootfs_version"]
    else:
        _rv_remove = ["rootfs_version", "q_rootfs_version"]
    # #517 阶段1(F4)—— immutable_version 与 rootfs_version 同范式随 reserve 回写:非空 SET、
    # 空则 REMOVE 陈旧值(重投可能落到别版本写过的行,不清则与真实盘版本不一致)。无 GSI 投影
    # (免 q_immutable_version;阶段2 探测口另议),故不含 ≤256B 投影分支。
    if immutable_version:
        _rv_set += ", immutable_version = :iv"
    else:
        _rv_remove.append("immutable_version")
    for offset, t in enumerate(tenants):
        vm_num = base + offset
        rid = _reservation_id(command_id, t["tenant_id"])
        # tenant_service.py:1866 同款)。否则 dispatch 路径 phys_vm_num 仅靠 host-agent if_not_exists
        # → 跨租户 tap 复用(红线)。plain SET 让 phys 随 vm_num 走(reserve 只作用于 creating 租户)。
        _upd = (
            "SET host_id = :h, vm_num = :vn, phys_vm_num = :vn, "
            "guest_ip = :g, host_port = :p, capacity_reservation_id = :rid, "
            "updated_at = :now" + _rv_set
        )
        if _rv_remove:
            _upd += " REMOVE " + ", ".join(_rv_remove)
        _vals = {
            ":h": instance_id,
            ":vn": vm_num,
            ":g": _guest_ip(vm_num),
            ":p": clients.VM_PORT_BASE + vm_num - 1,
            ":rid": rid,
            ":cid": command_id,
            ":creating": "creating",
            ":now": now,
        }
        if rootfs_version:
            _vals[":rv"] = rootfs_version
        if immutable_version:
            _vals[":iv"] = immutable_version
        txn_items.append(
            {
                "Update": {
                    "TableName": clients.tenants_table.table_name,
                    "Key": {"id": t["tenant_id"]},
                    "UpdateExpression": _upd,
                    "ConditionExpression": (
                        "#s = :creating AND dispatch_claim = :cid "
                        "AND attribute_not_exists(capacity_reservation_id)"
                    ),
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": _vals,
                }
            }
        )
    try:
        clients.hosts_table.meta.client.transact_write_items(TransactItems=txn_items)
        return base  # host 事务占号与租户放置使用同一个 base
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", []) or []
            codes = [r.get("Code") for r in reasons]
            print(
                f"[dispatch] reserve txn cancelled host={instance_id} "
                f"cmd={command_id} reasons={codes}"
            )
            _retryable = {"TransactionConflict", "ThrottlingError",
                          "ProvisionedThroughputExceeded", "RequestLimitExceeded"}
            host_code = codes[0] if len(codes) > 0 else ""
            tenant_codes = codes[1:] if len(codes) > 1 else []
            tenant_failed = any(c and c != "None" for c in tenant_codes)
            if any(c in _retryable for c in codes):
                return _RESERVE_TXN_CONFLICT
            # ② 仅 host 项(idx0)CCF、租户项没失败 → HOST_CCF。调用方重读 next_vm_num 判别到底是
            #    next_vm_num 乐观锁竞争(变了→重试)还是容量/inflight 门失败(没变→真失败)——
            if host_code == "ConditionalCheckFailed" and not tenant_failed:
                return _RESERVE_HOST_CCF
            # ③ 租户项失败(delete 抢赢/被接管)等 → 真失败,当 CAS-loss 返 None。
            return None
        if code in ("TransactionConflict", "ThrottlingError",
                    "ProvisionedThroughputExceeded", "RequestLimitExceeded"):
            return _RESERVE_TXN_CONFLICT  # 顶层瞬时冲突/限流:重读重试,不烧预算
        raise  # 其它错误 fail-loud(IAM/网络/坏参数),别静默当容量不够


# (必须重试)"混为一谈 → delete 会在瞬时失败后照样标 deleted 令牌永久搁浅 / rollback 会在
# throttle 后清 claim 让令牌卡死每次重投)。三态让调用方精确分流。
RELEASE_CONSUMED = "consumed"  # 本次消费了令牌并扣了账本
RELEASE_ALREADY = "already"    # 令牌已不在(别人消费过 / 从没有)或下溢守卫触发 → 安全幂等
RELEASE_RETRY = "retry"        # 瞬时失败(throttle/conflict/网络)→ 令牌可能仍在,必须重试


def _release_reservation(
    tenant_id: str,
    instance_id: str,
    reservation_id: str,
    vcpu: int,
    mem_mb: int,
) -> str:
    """#412 —— 令牌化释放:host 账本扣减 + 清租户 capacity_reservation_id/host_id 放置,
    放进【一个 TransactWriteItems】,条件 tenants.capacity_reservation_id = :rid。

    这是 dispatch 侧 4 条释放路径(批级失败回滚 / delete / reaper / poller)共用的原子原语。
    幂等 + 无双扣的锚(codex #3):唯一 reservation_id。谁先消费该令牌谁扣一次账本,其余
    释放者的 `= :rid` 条件失败 → 事务整体取消(host 扣减也随之作废,DDB all-or-nothing)→
    幂等 no-op。清 host_id 让 delete/reaper 能区分"dispatch 令牌已释放"(host_id 没了→跳过
    旧扣减路径)与"同步 create 租户"(host_id 在、无令牌→走旧扣减)。

    返回三态(codex review #3/#4):
    - RELEASE_CONSUMED:事务成功,本次扣了账本、清了令牌。
    - RELEASE_ALREADY:令牌已不在(别人消费 / 从没有)或下溢守卫触发——安全,不用重试。
    - RELEASE_RETRY:瞬时错误(TransactionConflict/throttle/网络)——令牌可能仍在,调用方
      【不得】就此清 claim/inflight 或标 deleted,必须让消息/删除重投再释放。
    """
    # TransactItems 顺序固定:[0]=host 扣减(下溢守卫),[1]=tenant 令牌消费(capacity_
    txn_items = [
        {
            "Update": {
                "TableName": clients.hosts_table.table_name,
                "Key": {"instance_id": instance_id},
                "UpdateExpression": (
                    "SET used_vcpu = used_vcpu - :v, "
                    "used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one"
                ),
                # 下溢守卫(最后防线):即便令牌校验被绕过也不把账本扣负。
                "ConditionExpression": (
                    "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                ),
                "ExpressionAttributeValues": {":v": int(vcpu), ":m": int(mem_mb), ":one": 1},
            }
        },
        {
            "Update": {
                "TableName": clients.tenants_table.table_name,
                "Key": {"id": tenant_id},
                "UpdateExpression": (
                    "REMOVE capacity_reservation_id, dispatch_settle, host_id, "
                    "vm_num, guest_ip, host_port"
                ),
                # 令牌互斥锚:只有仍持有【这张】令牌的租户行能被消费一次。
                "ConditionExpression": "capacity_reservation_id = :rid",
                "ExpressionAttributeValues": {":rid": reservation_id},
            }
        },
    ]
    try:
        clients.hosts_table.meta.client.transact_write_items(TransactItems=txn_items)
        return RELEASE_CONSUMED
    except ClientError as e:
        return _classify_release_cancel(e, tenant_id)
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] release reservation {tenant_id} error (retry): {e}")
        return RELEASE_RETRY


# CancellationReasons 位次:host 扣减项索引 0,tenant 令牌项索引 1(与 _release_reservation /
# _release_capacity_reservation 的 TransactItems 顺序一致)。
_REL_HOST_IDX = 0
_REL_TENANT_IDX = 1
_REL_RETRYABLE_CODES = {"TransactionConflict", "ThrottlingError",
                        "ProvisionedThroughputExceeded", "RequestLimitExceeded"}


def _classify_release_cancel(e: "ClientError", tenant_id: str) -> str:
    """按【位次】把释放事务的取消/错误分成三态(codex review2 #2):
    - 只有 tenant 项(索引1)的条件失败(令牌已被别人消费)才算 RELEASE_ALREADY(安全);
    - host 项(索引0)下溢守卫触发 = 账本异常,不是"令牌已释放" → RELEASE_RETRY 并告警;
    - 任一项可重试因(冲突/throttle)/ 缺 reasons / 未知错误 → RELEASE_RETRY(宁重试不搁浅)。"""
    code = e.response.get("Error", {}).get("Code", "")
    if code == "TransactionCanceledException":
        reasons = e.response.get("CancellationReasons", []) or []

        def _code_at(idx: int) -> str:
            return reasons[idx].get("Code", "") if idx < len(reasons) else ""

        host_code = _code_at(_REL_HOST_IDX)
        tenant_code = _code_at(_REL_TENANT_IDX)
        # ① 任一项可重试因 → RETRY(瞬时,重投)。
        if host_code in _REL_RETRYABLE_CODES or tenant_code in _REL_RETRYABLE_CODES:
            print(f"[dispatch] release {tenant_id} retryable cancel: {[host_code, tenant_code]}")
            return RELEASE_RETRY
        # ② 令牌项(idx1)CCF 优先判 ALREADY —— 即便 host 项(idx0)也 CCF(最后一张预留双重
        # 释放时账本已被前一次扣到不足,host 下溢与 token-gone 会【同时】失败)。token-gone 说明
        # 别的释放者已成功消费并扣过账本,本次就是安全幂等,绝不能因 host 下溢误报 RETRY 让
        if tenant_code == "ConditionalCheckFailed":
            return RELEASE_ALREADY  # 令牌已被别人消费/从没有 → 安全幂等
        # ③ 仅 host 项(idx0)CCF、令牌项没失败:账本与令牌不一致的真异常 → 告警 + 重试。
        if host_code == "ConditionalCheckFailed":
            print(f"[dispatch] release {tenant_id} host underflow guard tripped — retry+alarm")
            return RELEASE_RETRY
        print(f"[dispatch] release {tenant_id} cancel w/o reasons — retry")
        return RELEASE_RETRY
    if code in _REL_RETRYABLE_CODES:
        print(f"[dispatch] release {tenant_id} retryable error: {code}")
        return RELEASE_RETRY
    print(f"[dispatch] release reservation {tenant_id} error (retry): {e}")
    return RELEASE_RETRY  # 未知错误保守当可重试:宁可重试也不搁浅令牌


def _clear_inflight_scalar(instance_id: str, command_id: str) -> None:
    """push 批级回滚时清 host 的 inflight 标量(命令未真正发出)。带 poller 同款 CAS
    (dispatch_inflight=:cid)防误清并发新命令的标记。best-effort。"""
    try:
        clients.hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "REMOVE dispatch_inflight, dispatch_inflight_ts, "
                "dispatch_inflight_ts_epoch, dispatch_ssm_cid"
            ),
            ConditionExpression="dispatch_inflight = :cid",
            ExpressionAttributeValues={":cid": command_id},
        )
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] clear inflight scalar {instance_id} non-fatal: {e}")


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
                    # encode 只在非空时写 r;空 → 普通建盘,行逐字节不变。
                    "restore_backup_key": params.get("restore_backup_key"),
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
    """SSM executionTimeout = ceil(batch × per-vm-budget / 有效并发) + 120s 余量,
    并 ≤ visibility_timeout - 60s(防假超时→回滚活 VM→账本分叉)。

    DISPATCH_MAX_PARALLEL(96)。VM 经跨进程 flock 槽【排队限速】起:batch 个 VM 分 ceil(batch/slots)
    【轮】跑,每轮并行 slots 个、耗时约 per_vm 秒。故公式 = ceil(batch/slots)×per_vm + 120 余量
    (codex #327:不是 batch×per_vm/slots——后者在不整除时少算一整轮尾巴)。"""
    per_vm = max(1, int(clients.DISPATCH_PER_VM_BUDGET_SEC or 8))
    parallel = max(1, int(clients.DISPATCH_HOST_LAUNCH_CONCURRENCY or 30))
    rounds = -(-batch_size // parallel)  # ceil(batch/slots):要跑几轮
    est = rounds * per_vm + 120
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
        # #671 —— 与认领/释放同款:这里只调用无状态 generated action,worker 不加载或
        # 修改 resource metadata；实际 I/O 委托给共享的 thread-safe meta.client。
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
    # flock 槽数:起的后台 job 数不超过槽数(否则多出的 job 全阻塞在抢槽上,白占内存/句柄)。真正的
    # 跨进程硬闸是 launch-vm.sh 的 OC_HOST_LAUNCH_SLOTS,这里只是让 in-process 并发不虚高。
    parallel = max(1, int(clients.DISPATCH_HOST_LAUNCH_CONCURRENCY or 30))
    exec_timeout = _derive_exec_timeout(batch_size)
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=120,  # invocation delivery
            Parameters={
                "commands": [
                    f"bash /home/ubuntu/launch-vm.sh --manifest {command_id} {part_count} {parallel} "
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
    # flock 槽数:起的后台 job 数不超过槽数(否则多出的 job 全阻塞在抢槽上,白占内存/句柄)。真正的
    # 跨进程硬闸是 launch-vm.sh 的 OC_HOST_LAUNCH_SLOTS,这里只是让 in-process 并发不虚高。
    parallel = max(1, int(clients.DISPATCH_HOST_LAUNCH_CONCURRENCY or 30))
    exec_timeout = _derive_exec_timeout(batch_size)
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=120,
            Parameters={
                "commands": [
                    f"bash /home/ubuntu/launch-vm.sh --from-ddb {command_id} "
                    f"{batch_size} {parallel} {clients.assignments_table.table_name}"
                ],
                "executionTimeout": [str(exec_timeout)],
            },
        )
        return (resp.get("Command") or {}).get("CommandId")
    except Exception as e:  # noqa: BLE001
        print(f"[dispatch] --from-ddb SendCommand {instance_id} failed: {e}")
        return None


def _clear_assignments(
    instance_id: str,
    tenants: List[Dict[str, Any]],
    command_id: str,
) -> None:
    """ddb 载体回滚:删本批刚写的 pending assignments(SSM 叫醒失败时)。
    best-effort:删不掉留 24h TTL 兜底,host 侧 vm.json check 防重复 launch。"""
    if not clients.assignments_table:
        return
    ccf = clients.assignments_table.meta.client.exceptions.ConditionalCheckFailedException
    for t in tenants:
        try:
            clients.assignments_table.delete_item(
                Key={"instance_id": instance_id, "tenant_id": t["tenant_id"]},
                ConditionExpression="command_id = :cid",
                ExpressionAttributeValues={":cid": command_id},
            )
        except ccf:
            pass  # a newer command replaced this row; never delete its assignment
        except Exception as e:  # noqa: BLE001
            print(
                f"[dispatch] clear assignment {instance_id}/{t['tenant_id']} "
                f"non-fatal: {e}"
            )


# ── assignments (pull 模式) ───────────────────────────────────────────
def _write_assignments(
    instance_id: str,
    tenants: List[Dict[str, Any]],
    base_vm_num: int,
    now_epoch: int,
    command_id: str,
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
                    "command_id": command_id,
                    "capacity_reservation_id": _reservation_id(
                        command_id, t["tenant_id"]
                    ),
                    "action": "create",
                    "vm_num": base_vm_num + offset,
                    "vcpu": int(params.get("vcpu", clients.VM_DEFAULT_VCPU)),
                    "mem_mb": int(params.get("mem_mb", clients.VM_DEFAULT_MEM)),
                    "chat_ep": bool(params.get("chat_ep", False)),
                    "status": "pending",
                    "created_ts": _now(),
                    "ttl": ttl_epoch,
                }
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
    # #671 —— warm container 会复用模块级计数,必须在任何早退/andon 判定之前归零。
    scheduling.reset_scan_stats()
    _RESERVE_STATS["attempts"] = 0
    _timing_started = _dispatch_monotonic()
    _timing = {
        "parse": 0.0,
        "claim": 0.0,
        "stale": 0.0,
        "snapshot_hosts": 0.0,
        "pack": 0.0,
        "phys_prefetch": 0.0,
        "reserve": 0.0,
        "phys_check": 0.0,
        "assignments": 0.0,
        "ssm": 0.0,
        "unplaced": 0.0,
        "release_claims": 0.0,
    }
    _timing_host_count = 0
    # #671 —— 光有各段总耗时还不够判 SendCommand 是否在被限流。boto3 的 adaptive retry
    # 吸收 throttle 的形态是【sleep 后重试】:不打日志、不抛异常、纯变慢(#646 记的同一形态,
    # 且 AWS/Usage 在本区没有 SSM 指标,服务端侧看不到调用数)。所以这里记调用次数,
    # 让 ssm/ssm_calls 能算出单次均值 —— 单次远超 200ms 就是 adaptive 在 sleep。
    # reserve_calls 同理:它减去 host 数就是 reserve 事务的 CAS 重试次数。
    _counts = {"ssm_calls": 0, "reserve_calls": 0}

    def _timing_add(stage: str, started: float) -> None:
        _timing[stage] += max(0.0, _dispatch_monotonic() - started)

    def _log_timing() -> None:
        total_ms = int(max(0.0, _dispatch_monotonic() - _timing_started) * 1000)
        values = {key: int(value * 1000) for key, value in _timing.items()}
        scan_stats = scheduling.get_scan_stats()
        print(
            f"[dispatch] TIMING cmd={command_id} msgs={len(records)} "
            f"hosts={_timing_host_count} total={total_ms} "
            f"parse={values['parse']} claim={values['claim']} "
            f"stale={values['stale']} snapshot_hosts={values['snapshot_hosts']} "
            f"pack={values['pack']} phys_prefetch={values['phys_prefetch']} "
            f"reserve={values['reserve']} phys_check={values['phys_check']} "
            f"assignments={values['assignments']} ssm={values['ssm']} "
            f"unplaced={values['unplaced']} "
            f"release_claims={values['release_claims']} "
            f"ssm_calls={_counts['ssm_calls']} "
            f"reserve_calls={_counts['reserve_calls']} "
            f"phys_scan_calls={scan_stats['calls']} "
            f"phys_scan_pages={scan_stats['pages']} "
            f"reserve_attempts={_RESERVE_STATS['attempts']}"
        )

    now_epoch = int(time.time())
    command_id = f"cmd-{now_epoch}-{records[0].get('messageId', 'x')[:8] if records else 'empty'}"

    # 0) andon check(免缓存)
    stop, reason = _check_andon()
    if stop:
        print(f"[dispatch] andon halt: {reason} — all batch retried")
        _log_timing()
        return {
            "batchItemFailures": [
                {"itemIdentifier": r.get("messageId")}
                for r in records
                if r.get("messageId")
            ]
        }

    _stage_started = _dispatch_monotonic()
    parsed = _parse_records(records)
    _timing_add("parse", _stage_started)
    if not parsed:
        _log_timing()
        return {"batchItemFailures": []}

    # 0.5) #562 G7 —— 过期消息不起 VM。在【认领之前】丢弃,所以不占号、不写账本、不发 SSM。
    #
    # 为什么必须有这一步:消费者停 10 分钟再恢复,会一次性领到一大批客户【早已放弃】的创建。
    # 若照常起 VM,后果是最糟的一种组合 —— 客户端收到 failed 并且已经重试过了,而我方悄悄给
    # 它起了一台 VM:占容量、计费、没人认领、也不会被任何自愈流程收走(它 status 已是 failed,
    # 不在 creating 扫描面里)。
    #
    # 丢弃是【安全】的,因为独立死线执行者(G6,dispatch_poller 侧)已经把这些租户判成终态了。
    # 这里 ack 删除而不进 batchItemFailures:业务判死是一次【成功处理】,进 DLQ 会污染
    # 「DLQ 非空 = 100% 是 bug」这条语义(形态第 7 条)。
    #
    # 缺死线字段(老消息)→ is_expired 返 False → 不丢弃,走正常链路。升级期队列里两种消息并存,
    # 不许因为缺字段就丢掉客户已受理的创建。
    _fresh, _expired_ids = [], []
    for _p in parsed:
        if _p.get("invalid"):
            _fresh.append(_p)  # 非法消息的处置归下游既有逻辑,不在这里抢
            continue
        if create_deadline.is_expired(_p.get("deadline"), now_epoch):
            _expired_ids.append(_p.get("msg_id"))
            continue
        _fresh.append(_p)
    if _expired_ids:
        # fail-loud 到日志:这批被丢弃的必须可数、可归因。若这个数长期非零,说明消费能力
        # 或死线预算有问题,而不是"正常丢弃"。
        print(
            f"[dispatch] #562 dropped {len(_expired_ids)} expired message(s) "
            f"(deadline passed, tenants already terminal via deadline executor); "
            f"msg_ids={_expired_ids[:10]}"
        )
    parsed = _fresh
    if not parsed:
        _log_timing()
        return {"batchItemFailures": []}

    # 1) 认领闸
    _stage_started = _dispatch_monotonic()
    winners, _ack_drop = _claim_tenants(parsed, command_id, _now())
    _timing_add("claim", _stage_started)
    # 认领输家(重复/竞争)静默 ack,不进 batchItemFailures
    if not winners:
        _log_timing()
        return {"batchItemFailures": []}

    # 过(写了 capacity_reservation_id + host_id),但那批的释放遇 RETRY 没清掉;消息重投、本次
    # 赢得认领后,若直接重新装箱,reserve 的 attribute_not_exists(capacity_reservation_id) 会恒
    # 失败 → 白烧 retry 预算 → 误判 failed(令牌自占,活锁)。这里先把陈旧令牌释放掉(令牌互斥锚,
    # 幂等:已被 reaper/别人清则 already no-op),把租户还原成干净 creating,再走下面正常装箱。
    _stage_started = _dispatch_monotonic()
    stale_unsettled_msgs: List[str] = []
    _survivors = []
    for w in winners:
        stale = w.get("stale_reservation")
        if stale and stale.get("host_id") and stale.get("rid"):
            r = _release_reservation(
                w["tenant_id"], stale["host_id"], stale["rid"],
                stale["vcpu"], stale["mem_mb"],
            )
            print(f"[dispatch] settled stale reservation tenant={w['tenant_id']} "
                  f"rid={stale['rid']} result={r}")
            if r == RELEASE_RETRY:
                # 装箱,新 reserve 的 attribute_not_exists(capacity_reservation_id) 恒失败白烧预算。
                # 故把该租户【本轮排除出装箱】:进 batchItemFailures 让 SQS 原消息重投,且【不清
                # claim/不计 retry】(claim 留着,下轮重投再结算),靠 reaper/孤儿清扫兜底。
                stale_unsettled_msgs.append(w["msg_id"])
                continue
        _survivors.append(w)
    winners = _survivors
    _timing_add("stale", _stage_started)

    # 2) hosts 快照 + 装箱
    mode = clients.DISPATCH_MODE.lower()
    push_mode = mode == "push"
    ddb_mode = mode == "ddb"
    # pull/误配也会误关 inflight 门):【仅 ddb】不设 inflight 门(host-agent 兜底,可扩 1000 host);
    # push 和 pull(及任何非 ddb)都【保留】旧 inflight 门 + 写标量(逐字旧行为,poller 追踪需要)。
    gate_inflight = not ddb_mode
    _stage_started = _dispatch_monotonic()
    hosts = _snapshot_hosts(now_epoch, gate_inflight=gate_inflight)
    _timing_add("snapshot_hosts", _stage_started)
    # push/ddb 都发真 SSM,simulated host 收不到命令 → 装箱跳过它们(压测用);
    # pull 二期由 host-agent 轮询,simulated host 可参与。
    dispatches_ssm = mode in ("push", "ddb")
    # (唯一入口):normalize_spec 校验信任边界外的值(非 dict/负/inf/非数字 → fail-safe 回落 VM_DEFAULT)。
    # 否则静默空盘/丢配对(codex review 阻断级回归)。之后 pending 的 params 恒是 dict,装箱/CAS 求和/
    # manifest/assignment 全读同一份 → 启动规格与账本一致,且下游 .get("params") 不会遇到非 dict 炸批。
    dvm = int(clients.VM_DEFAULT_VCPU or 2)
    dmm = int(clients.VM_DEFAULT_MEM or 4096)
    pending = []
    for w in winners:
        raw = w.get("params")
        nv, nm = normalize_spec(raw, dvm, dmm)
        merged = {**raw} if isinstance(raw, dict) else {}
        merged["vcpu"], merged["mem_mb"] = nv, nm
        pending.append({"tenant_id": w["tenant_id"], "params": merged})
    # #661 —— 全列表加权顺序必须在 binpack 过完全部资格门后生成；放在编排层会被
    # binpack 的 sort 覆盖，复制资格门又会随演进静默漂移。这里只传开关与 rng，
    # 资格判定、同 tier 加权排序、FFD 租户装箱仍由唯一实现负责。
    # #661 第九轮修正 —— per_host_cap 的绝对上界必须同时受 host launch 槽数约束。
    # 默认 DISPATCH_MAX_PARALLEL=96、slots=30、per_vm=8 时，若单台承接 96 个：
    # ceil(96/30)=4 轮，SSM executionTimeout=4*8+120=152s > create 执行段 128s；
    # 夹到 30 后 ceil(30/30)=1 轮，1*8+120=128s，才与执行段正好贴合。上界一旦
    # 超过 slots，单台就要多跑 launch 轮次，executionTimeout 随轮数线性上涨并突破
    # create 死线；死线执行者判死后 SSM 仍会继续起 VM，最终留下静默且计费的孤儿 VM。
    # #646 R1 要求的 per-host 生命周期闸门若从创建侧现值 30 降到 ≤5，这个 min()
    # 会自动跟随到 5，使 ceil(cap/5)=1 继续贴合执行段；两处口径未统一前不能硬编码。
    _stage_started = _dispatch_monotonic()
    result = pack(
        pending,
        hosts,
        per_host_cap=min(
            clients.DISPATCH_MAX_PARALLEL,
            clients.DISPATCH_HOST_LAUNCH_CONCURRENCY,
        ),
        skip_simulated=dispatches_ssm,
        default_vcpu=dvm,
        default_mem=dmm,
        # (被 tests/test_dispatch_binpack.py 以 importlib 脱包加载)。
        affinity=clients.AFFINITY_ENABLED,
        spread_hosts=True,
        # #661 —— 加权随机只改“谁先吃”，不能约束空机一次吞掉整批。动态目标台数交给
        # binpack 在完整资格门后的 usable 上按 pending 一起计算，避免编排层复制资格门
        # 或把坏 host 算进分母；环境上限只兜底极端扇出，上面的 per_host_cap 仍保持
        # 绝对上界，且取 DISPATCH_MAX_PARALLEL 与 host launch 槽数中更小者。
        spread_max_hosts_per_batch=clients.SPREAD_MAX_HOSTS_PER_BATCH,
        rng=random,
        # #661 —— 两个加权旋钮与同步 create 共用 clients.X 的环境覆盖值；binpack
        # 保持零依赖，只接收显式参数，避免在纯函数模块里反向 import scheduling/clients。
        host_selection_weight_alpha=clients.HOST_SELECTION_WEIGHT_ALPHA,
        host_selection_score_floor=clients.HOST_SELECTION_SCORE_FLOOR,
    )
    _timing_add("pack", _stage_started)
    _timing_host_count = len(result.assignments)

    # #671 —— tenants 表无 host_id GSI(只有 gsi_owner/owner_id),所以撞号门只能 scan,
    # 而 scan 的 1MB 分页数由【过滤前】的全表大小决定。真机实测:表 17208 行 / 20.4 MB
    # 时每套 filter 翻 15 页、matched=0;旧形态每批是 6 host × 2 个 filter × 15 页 = 180 页,
    # 按 Scan 服务端延迟 avg 24ms 算约 4.3s,占 dispatch invocation p50(9–11s)的四成。
    # 这里一次强一致翻页拿回本批所有 host 的 owner/phys 对,循环内再按各自 batch_ids 排除。
    #
    # 【绝不能传整批 exclude_ids】:排除集合是 per-host 的,预取阶段传全批 id 会把其它
    # host 上同名租户的既有占用一起排掉 → fail-open 放行撞号。
    #
    # 【时序前移是安全的,但依赖一个前提,写在这里以免以后被无声破坏】:旧形态在每台
    # host 的 reserve 事务【之后】才 scan,新形态在 pack 之后、reserve 之前预取一次,
    # 所以快照比 reserve 更早。漏判只会发生在"预取之后、reserve 之前有别的批次占了
    # 同一个 base+offset"这种情况,而同一 host 上的号由 reserve 事务的
    # next_vm_num=:expected 乐观锁串行发出,两个并发批次拿不到同一段号。撞号的真实成因
    # 是发号器被回退(init-host 整项覆写 / register_host 无条件 put_item)后把【早已在役】
    # 的老租户的号再发一遍 —— 那些行在预取快照里就存在,照样被抓到。
    # 若哪天 next_vm_num 的 CAS 保证被改掉,这里必须回退成 reserve 后逐台强一致复查。
    #
    # 同理【不把 result.host_order 的候选 host 一起预取】:那看着免费(反正已扫全表),
    # 但换机发生得更晚,用更旧的快照判更晚的 reserve 会实打实地拉长上面那个窗口。
    # 换机是异常路径、频率低,回退单 host 强一致查询更划算。
    _stage_started = _dispatch_monotonic()
    _phys_prefetch = scheduling.phys_occupied_pairs(
        list(result.assignments.keys())
    )
    _timing_add("phys_prefetch", _stage_started)

    # msg_id 反查:tenant_id → msg_id(唯一,认领已保证)
    tid_to_msg = {w["tenant_id"]: w["msg_id"] for w in winners}
    tid_to_rh = {w["tenant_id"]: w.get("receipt_handle") for w in winners}
    # #522 P1-2 tenant_id → SQS ApproximateReceiveCount(升级宽限的收敛 backstop 用)
    recv_by_tid = {w["tenant_id"]: int(w.get("receive_count", 1) or 1) for w in winners}
    # #562 —— tid → 死线。binpack 产出的 unplaced 元素不一定带 deadline 字段,
    # 所以在这里从 winners 建一次映射(与 recv_by_tid 同款),下面判「注定超不过」时查。
    deadline_by_tid = {w["tenant_id"]: w.get("deadline") for w in winners}
    alloc_by_host = {h["instance_id"]: h["allocatable_vcpu"] for h in hosts}
    alloc_mem_by_host = {h["instance_id"]: h.get("allocatable_mem", 0) for h in hosts}
    # 从它派连续 vm_num 段(事务不返回 Attributes,不能读回 → 用乐观锁把 base 钉死)。
    next_vm_by_host = {
        h["instance_id"]: int(h.get("raw", {}).get("next_vm_num", 1) or 1) for h in hosts
    }
    rootfs_by_host = {
        h["instance_id"]: (h.get("raw") or {}).get("rootfs_version", "") for h in hosts
    }
    # #517 阶段1(F4 修正,codex 交叉审)—— 队列化(生产)create 路径同步回写只读身份盘版本坐标
    # immutable_version,与 rootfs_version 对称。此前仅同步 create_tenant 写它、reserve 事务只带
    # rootfs → 队列化创建的租户建户即缺 immutable 坐标(阶段2 rootfs-drift 会误报)。
    immutable_by_host = {
        h["instance_id"]: (h.get("raw") or {}).get("immutable_version", "") for h in hosts
    }
    failures: List[str] = []
    # 但 reserve 时被并发抢输)的租户 tid,稍后对其【原消息】缩短 visibility(960→15s)快速
    # 重投——高并发接近满容量时 CAS loser 是最常见的溢出类型,漏了它容量竞争输家仍卡 960s。
    # 只缩容量类(非 SSM/manifest/写库失败):那些是基础设施故障,按默认 visibility 重投即可。
    capacity_retry_tids: set = set()
    # (否则清 claim 后重投过认领闸撞新鲜 claim 被静默 ack、令牌搁浅逃出 reaper)。保留 claim
    # + inflight,靠 SQS 原消息按默认 visibility 重投再释放,或 reaper 令牌孤儿清扫兜底。
    reserve_retry_tids: set = set()
    ssm_consec_fail = 0

    # 打一条带 tenant_id + host + 原因的日志。修复前这些 append 点静默,突发下
    # 部分租户卡 creating 时 CloudWatch 零错误日志、运维只能靠 DLQ 深度发现
    def _fail(tid: str, reason: str, host: str = "-") -> None:
        print(
            f"[dispatch] FAIL tenant={tid} host={host} cmd={command_id} "
            f"reason={reason} — requeue for retry"
        )
        failures.append(tid_to_msg[tid])

    # 3) 每 host 一批:CAS → 分发
    _ssm_cid_pool = None
    for instance_id, batch in result.assignments.items():
        n = len(batch)
        batch_ids = {t["tenant_id"] for t in batch}
        # 取数 → 装箱与 CAS 口径必然一致;非法/非正/非数字 params 在此统一 fail-safe 回落 VM_DEFAULT
        # (codex Error5:SQS 消息体是信任边界外,负数/非数字直接进账本算术会腐蚀 used_* 或炸批)。
        dvm = int(clients.VM_DEFAULT_VCPU or 2)
        dmm = int(clients.VM_DEFAULT_MEM or 4096)
        specs = [normalize_spec(t.get("params"), dvm, dmm) for t in batch]
        sum_vcpu = sum(v for v, _ in specs)
        sum_mem = sum(m for _, m in specs)
        # 每租户放置写 + 唯一 capacity_reservation_id。取代旧的"_try_reserve_host 整批 CAS
        # #491(R3-1)选择方案(a):空段重算和 ps_* 占号都在 _reserve_batch_txn 内完成,
        # 最终由 _reserve_batch_txn_once 的同一个事务写 host 账本、占号和租户放置。
        # 这样每次 CCF 重读 next_vm_num 后即使换 base,实际 reserve 与占号仍天然同段；
        # 不再保留“先挪号/占号一次、事务重试可能换号”的双写窗口。
        # #661 —— transient 明确保证 host 增量未生效，所以同 invocation 可安全换机；
        # None 可能是真容量不足/delete 抢赢，仍由下面既有分支处理，绝不进入换机。
        # 候选顺序来自首次 binpack 的完整同 tier 加权排序；每次换机重新扫强一致快照，
        # 再让 binpack 唯一实现重过 inflight/磁盘/内存/污点/心跳及整批双资源容量门。
        # 新 host 的 next_vm_num/rootfs/immutable 也全部取新快照；换机不在预取 map 中,
        # reserve 会回退 next_free_phys_run 实时扫描并原子写 ps_*，不能把上一台 host 的
        # 物理号跨租户串过来。
        try:
            _candidate_index = result.host_order.index(instance_id)
        except ValueError:
            _candidate_index = len(result.host_order)
        _candidate_order = result.host_order[_candidate_index + 1 :]
        _tried_hosts = {instance_id}
        _host_switches = 0
        _reserve_retry_started = time.monotonic()
        while True:
            _snap_next = next_vm_by_host.get(instance_id, 1)
            if _phys_prefetch is not None and instance_id in _phys_prefetch:
                # #671 —— 只把【当前 host】预取结果排除本 host 本批 id 后作为候选提示。
                # 缺键(换机)或预取失败都传 None,由 reserve 回退实时扫描,绝不 fail-open。
                _occupied_hint = {
                    phys_num
                    for owner_id, phys_num in _phys_prefetch[instance_id]
                    if owner_id not in batch_ids
                }
            else:
                _occupied_hint = None
            _stage_started = _dispatch_monotonic()
            base = _reserve_batch_txn(
                batch,
                instance_id,
                command_id,
                now_epoch,
                _snap_next,
                alloc_by_host.get(instance_id, 0),
                sum_vcpu,
                sum_mem,
                alloc_mem_by_host.get(instance_id, 0),
                specs,
                write_inflight=gate_inflight,
                rootfs_version=rootfs_by_host.get(instance_id, ""),
                immutable_version=immutable_by_host.get(instance_id, ""),
                occupied_hint=_occupied_hint,
            )
            _timing_add("reserve", _stage_started)
            _counts["reserve_calls"] += 1
            if base != _RESERVE_TRANSIENT:
                break
            if (
                _host_switches >= _RESERVE_HOST_SWITCH_MAX
                or not _candidate_order
            ):
                break
            _retry_elapsed = max(0.0, time.monotonic() - _reserve_retry_started)
            if (
                _retry_elapsed + _RESERVE_BACKOFF_TOTAL_BUDGET_SEC
                > _RESERVE_HOST_RETRY_TOTAL_BUDGET_SEC
            ):
                break
            _stage_started = _dispatch_monotonic()
            _fresh_hosts = _snapshot_hosts(
                now_epoch, gate_inflight=gate_inflight
            )
            _timing_add("snapshot_hosts", _stage_started)
            _next_host = dispatch_binpack.select_host_for_batch(
                batch,
                _fresh_hosts,
                _candidate_order,
                per_host_cap=clients.DISPATCH_MAX_PARALLEL,
                exclude_host_ids=_tried_hosts,
                skip_simulated=dispatches_ssm,
                default_vcpu=dvm,
                default_mem=dmm,
                affinity=clients.AFFINITY_ENABLED,
            )
            if not _next_host:
                break
            _previous_host = instance_id
            instance_id = _next_host["instance_id"]
            _tried_hosts.add(instance_id)
            _host_switches += 1
            alloc_by_host[instance_id] = _next_host.get("allocatable_vcpu", 0)
            alloc_mem_by_host[instance_id] = _next_host.get("allocatable_mem", 0)
            _raw_next_host = _next_host.get("raw") or {}
            next_vm_by_host[instance_id] = int(
                _raw_next_host.get("next_vm_num", 1) or 1
            )
            rootfs_by_host[instance_id] = _raw_next_host.get("rootfs_version", "")
            immutable_by_host[instance_id] = _raw_next_host.get(
                "immutable_version", ""
            )
            print(
                f"[dispatch] #661 reserve transient host switch "
                f"{_previous_host} -> {instance_id} cmd={command_id} "
                f"attempt={_host_switches}/{_RESERVE_HOST_SWITCH_MAX}"
            )
        if base == _RESERVE_TRANSIENT:
            # 未生效。整批进 batchItemFailures 让原消息重投,但【不计 dispatch_retries、不清 claim】
            # (记 reserve_retry_tids,下方 _release_claims 跳过),否则竞争下反复 +retry 会把健康
            # 租户误推进 requires_intervention。claim 留着,重投时新一轮认领/reserve 再试。
            for t in batch:
                _fail(t["tenant_id"], "host reserve transient contention (no-budget retry)", instance_id)
                reserve_retry_tids.add(t["tenant_id"])
            continue
        if base is None:
            # 事务取消(容量不够 / delete 抢赢某租户)→ host 增量未生效,该批全 unplaced 走重试。
            for t in batch:
                _fail(t["tenant_id"], "host reserve txn cancelled (capacity/race)", instance_id)
                capacity_retry_tids.add(t["tenant_id"])
            continue

        # 不再走已删除的 _backfill_placement。批级失败回滚改走 _release_batch(逐租户令牌释放,
        # 幂等、不双扣;再单独清 host inflight)。
        def _release_batch() -> None:
            all_settled = True  # 所有租户都已 consumed/already(令牌确不再占容量)
            for offset, t in enumerate(batch):
                v, m = specs[offset]
                r = _release_reservation(
                    t["tenant_id"],
                    instance_id,
                    _reservation_id(command_id, t["tenant_id"]),
                    v,
                    m,
                )
                if r == RELEASE_RETRY:
                    # 瞬时失败:令牌可能仍占容量。租户已进 batchItemFailures 重投,重投时
                    # reserve 见令牌会取消、reaper 兜底;此刻【不清 inflight】——否则并发新命令
                    # 下方 _release_claims 跳过它,保 claim + inflight,靠原消息重投或 reaper 兜底。
                    all_settled = False
                    reserve_retry_tids.add(t["tenant_id"])
                else:
                    # 只有容量令牌确认 consumed/already 才能还号；RETRY 时旧租户可能仍活着。
                    scheduling.release_phys_slot(
                        instance_id, base + offset, t["tenant_id"]
                    )
            # inflight 是 host 级批状态(非 per-tenant),命令未真正发出。仅当本批令牌全部落定
            # (无 retry 悬空)才清,带 poller 同款 CAS(dispatch_inflight=:cid)防误清并发新命令。
            # push 才有 inflight;有 retry 悬空则留 inflight,靠 TTL 过期或下轮释放清。
            if push_mode and all_settled:
                _clear_inflight_scalar(instance_id, command_id)

        # #491 —— 队列路径撞号守卫(此前零覆盖)。reserve 的 CAS 只保证 next_vm_num 原子
        # 递增,不保证发出的号未被本 host 在役租户物理占用:发号器一旦被回退(init-host
        # 整项覆写、host_service.register_host 无条件 put_item),CAS 会把已在用的号再发一遍
        # 且每次都成功 → launch-vm.sh 随后 `ip link del`+`kill -KILL` 抢占先到者的 tap
        # → 两个 running 租户共用同一 vm_num/guest_ip/DNAT/Redis route = 跨租户劫持。
        # 已真机复现:同一 host、同一回退状态下,同步 create 返 503(它在
        # tenant_service:1868 有这道门),队列 create 返 202 并撞号成功。
        #
        # 为什么整批释放而不是只剔除撞号那一个:offset→vm_num 的映射由 reserve 事务按
        # `base+offset` 写死,_write_assignments/_put_manifest_parts 也按同一 offset 推算,
        # 部分剔除会让剩余租户的号错位。整批 _release_batch + 重投在正确性上等价且必然收敛
        # —— _release_reservation 不回退 next_vm_num,重投时 expected_next 已前移,迟早越过
        # 被占号段(同步路径的 `for _skip in range(64)` 同样靠单调递增收敛)。撞号只在发号器
        # 异常时才走到,不需要为它优化吞吐。
        # #671 预取保留 owner_id,所以 exclusion 留到这里按【本 host 的本批全部租户】应用。
        # 预取时若传全批 id,会把其它 host 上同 id 的既有占用也误删。换机重试后 instance_id
        # 可能不在 result.assignments.keys() 的预取 map 中,必须回退单 host 强一致查询；把缺键
        # 当空集合会 fail-open 放行撞号。
        _stage_started = _dispatch_monotonic()
        if _phys_prefetch is None:
            # 预取整体失败 = 占用未知,整批 fail-closed(下面的 None 分支)。
            occupied_nums = None
        elif instance_id in _phys_prefetch:
            occupied_nums = {
                phys_num
                for owner_id, phys_num in _phys_prefetch[instance_id]
                if owner_id not in batch_ids
            }
        else:
            # 换机重试把 instance_id 换成了 result.host_order 里的另一台,它不在预取集合中。
            # 【必须回退强一致单 host 查询】:把缺键当空集合就是 fail-open 放行撞号。
            occupied_nums = scheduling.phys_occupied_nums(
                instance_id, exclude_ids=batch_ids
            )
        _timing_add("phys_check", _stage_started)
        if occupied_nums is None:
            # 扫描失败 = 占用情况未知 → fail-closed:此刻可能正撞着号,不能放行去起 VM。
            # 归入 reserve_retry_tids(不计 dispatch_retries、不清 claim),与
            # _RESERVE_TRANSIENT 同语义 —— 瞬时读失败不该把健康租户推向
            # requires_intervention。
            print(
                f"[dispatch] PHYS OCCUPANCY UNKNOWN host={instance_id} "
                f"cmd={command_id} — release batch + requeue (fail-closed)"
            )
            _release_batch()
            for t in batch:
                _fail(
                    t["tenant_id"],
                    "phys occupancy scan failed (fail-closed)",
                    instance_id,
                )
                reserve_retry_tids.add(t["tenant_id"])
            continue
        occupied = [
            (t["tenant_id"], base + off)
            for off, t in enumerate(batch)
            if (base + off) in occupied_nums
        ]
        if occupied:
            for tid, vnum in occupied:
                print(
                    f"[dispatch] PHYS TAP OCCUPIED tenant={tid} host={instance_id} "
                    f"vm_num={vnum} cmd={command_id} — release batch + requeue"
                )
            _release_batch()
            for t in batch:
                _fail(
                    t["tenant_id"],
                    "phys tap occupied on host (batch released)",
                    instance_id,
                )
                # #491(codex review)—— 记 reserve_retry_tids 而不是 capacity_retry_tids:
                # 后者会进 _release_claims 并 ADD dispatch_retries(:1424),
                # DISPATCH_RETRY_BUDGET 默认 3,于是连撞三次就把租户推进
                # requires_intervention(:1483 的收敛逻辑)。撞号是**环境异常**(发号器被
                # 回退),不是这个租户的错,不该烧它的预算;而且号段被占多少个是未知的,
                # 用有限预算去换号必然误伤。reserve_retry_tids 保 claim、不计 retry,
                # 靠 SQS 重投继续换号(next_vm_num 单调推进,必然收敛)。
                reserve_retry_tids.add(t["tenant_id"])
            continue

        if push_mode:
            _stage_started = _dispatch_monotonic()
            try:
                part_count = _put_manifest_parts(command_id, instance_id, batch, base)
            except Exception as e:  # noqa: BLE001
                _release_batch()
                for t in batch:
                    _fail(t["tenant_id"], f"manifest write failed: {e}", instance_id)
                continue
            finally:
                _timing_add("assignments", _stage_started)
            _stage_started = _dispatch_monotonic()
            sent = _send_ssm_manifest(instance_id, command_id, part_count, n)
            _timing_add("ssm", _stage_started)
            _counts["ssm_calls"] += 1
            if sent is None:
                ssm_consec_fail += 1
                _release_batch()
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
                if _ssm_cid_pool is None:
                    # #671 —— 每 host 一次独立 UpdateItem,上限 8；提交后继续处理下一台
                    # host,最后统一 wait,让小写入与后续 SendCommand/assignments 重叠。
                    _ssm_cid_pool = ThreadPoolExecutor(
                        max_workers=min(8, max(1, _timing_host_count))
                    )
                _ssm_cid_pool.submit(_record_ssm_cid, instance_id, sent)
        elif mode == "ddb":
            # ddb 载体:先写 assignments(数据),再发 --from-ddb 叫醒(信号)。
            # 写序重要:表里有行,叫醒命令到达时 host 才查得到(同步路径先落账
            _stage_started = _dispatch_monotonic()
            _assignments_ok = _write_assignments(
                instance_id, batch, base, now_epoch, command_id
            )
            _timing_add("assignments", _stage_started)
            if not _assignments_ok:
                _clear_assignments(instance_id, batch, command_id)
                _release_batch()
                for t in batch:
                    _fail(t["tenant_id"], "assignments write failed", instance_id)
                continue
            _stage_started = _dispatch_monotonic()
            sent = _send_ssm_from_ddb(instance_id, command_id, n)
            _timing_add("ssm", _stage_started)
            _counts["ssm_calls"] += 1
            if sent is None:
                ssm_consec_fail += 1
                _clear_assignments(instance_id, batch, command_id)
                _release_batch()
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
                if _ssm_cid_pool is None:
                    _ssm_cid_pool = ThreadPoolExecutor(
                        max_workers=min(8, max(1, _timing_host_count))
                    )
                _ssm_cid_pool.submit(_record_ssm_cid, instance_id, sent)
        else:  # pull(二期 host-agent 轮询,无 SSM 叫醒)
            _stage_started = _dispatch_monotonic()
            ok = _write_assignments(
                instance_id, batch, base, now_epoch, command_id
            )
            _timing_add("assignments", _stage_started)
            if not ok:
                _clear_assignments(instance_id, batch, command_id)
                _release_batch()
                for t in batch:
                    _fail(t["tenant_id"], "assignments write failed", instance_id)

    if _ssm_cid_pool is not None:
        _ssm_cid_pool.shutdown(wait=True)

    # 4) unplaced(容量不够、装箱阶段跳过的 host)→ 走【原消息】重投,不发新消息。
    # 消息"状态机):unplaced 就是普通失败(进 batchItemFailures + 下面 _release_claims 释放
    # claim/计预算,与 CAS/SSM 失败同一条久经考验的路)。缩短原消息 visibility 挪到释放 claim
    # 会在释放慢于 15s/失败时丢消息。
    # #522 P1-2 —— unplaced 归因分流:若 fleet 有 host 正在【新鲜】升级(upgrading_at 距今
    # < DISPATCH_UPGRADE_GRACE_SEC),本轮没位子多半是升级临时把 host 挪出候选(升级完回
    # active/idle),按【no-budget】重投(记 reserve_retry_tids:保 claim、不计 dispatch_retries、
    # 不缩 visibility),避免升级窗口内新建租户被误推终态 requires_intervention 且不自愈。无新鲜
    # 升级 → 仍按容量不足计预算(capacity_retry_tids),真·满容量的新建照旧收敛到 requires_
    # intervention。只在确有 unplaced 时才扫一次(升级卡死超 grace 也自动退回计预算,fail-loud)。
    _stage_started = _dispatch_monotonic()
    _upgrade_grace = (
        _fleet_has_fresh_upgrade(clients.DISPATCH_UPGRADE_GRACE_SEC, now_epoch)
        if result.unplaced
        else False
    )
    _max_recv = clients.DISPATCH_MAX_RECEIVE_COUNT
    _doomed_capacity_deaths = 0
    for t in result.unplaced:
        _tid = t["tenant_id"]
        _rc = recv_by_tid.get(_tid, 1)
        _dl = deadline_by_tid.get(_tid)
        if create_deadline.doomed_by_deadline(_dl, now_epoch):
            # ★#562 形态第 4 条 ——「注定超不过死线」:剩余时间已装不下执行段,现在【同步判死】。
            #
            # 为什么这条必须排在最前面(压过投递预算与升级宽限):没时间了就不该再重投。
            # 重投的两个结局都更差 —— 要么烧完投递预算进 DLQ(污染「DLQ 非空 = 100% 是 bug」),
            # 要么一路等到 1 分钟节拍的兜底执行者才被判死。
            #
            # 【这条缺失被真机压测抓出来】2026-08-21 一次 12 个创建的实测:5 个容量类失败全部
            # 由兜底执行者判死,其中 5 个都晚于 180s 死线 —— 因为本判定当时只有函数、没有调用点。
            # G1 的「180s 内进终态」靠的就是这里的同步判定,兜底只能保证「分钟内必然终态」。
            #
            # 围栏复用 deadline_executor._fence_failed:同一份实现、同样双锚 status+deadline、
            # 同样【保留】容量令牌交给带 stop-confirm 的释放者。此处租户未装箱(unplaced)故无令牌,
            # 但走同一函数保证语义不漂。
            _outcome, _reason = deadline_executor._fence_failed(
                {"id": _tid, "status": "creating",
                 create_deadline.ATTR_DEADLINE: _dl},
                now_epoch,
            )
            # 【不能用 _fail()】—— 它的第 3 行是 `failures.append(tid_to_msg[tid])`,即把消息
            # 塞进 batchItemFailures 重投。对已判死的租户那是最坏组合:租户是 failed 终态,
            # 消息还要被重投到耗尽预算进 DLQ,或者更糟 —— 重投成功后对一个 failed 租户起了 VM。
            # 我第一版就是调了 _fail(),被本文件的
            # test_doomed_tenant_is_failed_synchronously_not_requeued 当场打红。
            # 这里只打日志、不进 failures,消息随本批 ack 删除。
            print(
                f"[dispatch] #562 doomed tenant={_tid} cmd={command_id} "
                f"reason={_reason} outcome={_outcome} deadline={_dl} now={now_epoch} "
                f"— fenced terminal, message ACKed (no requeue)"
            )
            if _outcome == "fenced" and _reason == create_deadline.REASON_CAPACITY:
                _doomed_capacity_deaths += 1
            # 【不进 batchItemFailures】:租户已是终态,消息再没有用途。进 DLQ 会污染那条运维判据;
            # 重投则会对一个已 failed 的租户起 VM —— 那正是「没人认领的孤儿 VM」。
            continue
        if _rc >= _max_recv:
            # ★收敛 backstop(codex F1):到 SQS 最后一次投递(下次即 DLQ)仍无处可放 → 直接标
            # requires_intervention(loud 终态),杜绝 no-budget 宽限把消息静默送进 DLQ 却让租户
            # >~一个 visibility 周期、或真·满容量耗尽全部投递,属运维异常,失败要响。消息仍进
            # batchItemFailures(rc 已达上限 → SQS 转 DLQ),租户已 loud 终态。
            _fail(_tid, "unplaced: exhausted SQS receive budget → requires_intervention")
            _mark_stuck_creating_intervention(_tid, command_id)
        elif _upgrade_grace:
            # 新鲜升级窗口 + 尚有投递余量 → no-budget 重投(保 claim、不计 dispatch_retries、
            # 不缩 visibility),等升级完 host 回 active/idle 再落位。
            _fail(_tid, "unplaced during host upgrade window (no-budget retry)")
            reserve_retry_tids.add(_tid)
        else:
            _fail(_tid, "unplaced: no host capacity this round")
            capacity_retry_tids.add(_tid)

    # #562 G14 —— 判死的那一刻触发扩容,但【一轮只做一次决策】。
    # issue 原文:「判死的同时自动触发扩容。这是必须项:不触发扩容,客户端重试也一样失败,
    # 形成永久失败循环。」同时它也要求「幂等且有上限:1800 个请求同时判死不能触发 1800 次扩容」。
    # 所以这里传【本批的容量类死亡数】给同一个带上限的实现,而不是在上面的循环里逐个调用 ——
    # G15 的结论是「扩容风暴的成因是调用频率,不是 _scale_out 的 fail-safe 本身」。
    if _doomed_capacity_deaths:
        deadline_executor._scale_out_for_deaths(_doomed_capacity_deaths)
    _timing_add("unplaced", _stage_started)

    # 但记入 reserve_retry_tids(下方 _release_claims 跳过它)保 claim 不计 retry,下轮重投再结算。
    for _msg in stale_unsettled_msgs:
        failures.append(_msg)
        _tid = {v: k for k, v in tid_to_msg.items()}.get(_msg)
        if _tid:
            reserve_retry_tids.add(_tid)

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
    # inflight + 不计 dispatch_retries,靠 SQS 原消息按默认 960s 重投再释放(重投时令牌仍在
    # → reserve 见令牌取消/或本 host 命令重跑释放),或 reaper 令牌孤儿清扫兜底。清了 claim 会
    # 让重投撞新鲜认领闸被静默 ack → 令牌搁浅。它们仍在 batchItemFailures(下面 dedup)里,消息
    # 照常重投,只是不动 claim。
    _stage_started = _dispatch_monotonic()
    released = _release_claims(
        [
            msg_to_tid[m]
            for m in dedup
            if m in msg_to_tid and msg_to_tid[m] not in reserve_retry_tids
        ],
        command_id,
    )
    _timing_add("release_claims", _stage_started)
    # 缩短原消息 visibility(960→15s),让容量溢出快速重投而非等 48min。顺序在释放【之后】+ 只缩
    # 释放成功的 → 消除 codex simple review P1(先缩后释放会在释放慢/失败时 15s 内撞新鲜 claim
    # 丢消息)。释放失败/被接管/已终态的不缩,保留队列默认 960s(或到 maxReceiveCount 进 DLQ)兜底,
    # 多是 host 级 inflight 串行的正常等待(合法 SSM 可跑 840s),15s 就催会误伤——push 走原 960s。
    # #562 —— 带死线的容量重投:按死线安排下一次投递(不分模式,理由见
    _dl_pairs, _legacy_rh = [], []
    for _tid in capacity_retry_tids:
        if _tid not in released or not tid_to_rh.get(_tid):
            continue
        _dl = deadline_by_tid.get(_tid)
        _left = create_deadline.remaining_sec(_dl, now_epoch)
        if _left is None:
            _legacy_rh.append(tid_to_rh[_tid])
            continue
        # 目标:下一次投递落在判定窗口【刚打开之后】。窗口在「剩余 < 执行段」时打开,
        # 所以等 (剩余 - 执行段) 秒窗口正好打开,再加 5s 余量确保严格小于。
        # 下限 5s:不要因为算出 0/负数就变成"立刻重投"打转。
        # 上限压在剩余时间之内:超过死线再回来就没意义了(那时执行者已判死,认领闸会静默 ack)。
        _wait = max(5, min(_left, _left - create_deadline.EXEC_BUDGET_SEC + 5))
        _dl_pairs.append((tid_to_rh[_tid], _wait))
    if _dl_pairs:
        print(
            f"[dispatch] #562 deadline-aware requeue: {len(_dl_pairs)} msg(s), "
            f"waits={[w for _, w in _dl_pairs[:10]]}s "
            f"(下一次投递落进判定窗口,保证死线前有一次同步判定)"
        )
        _deadline_aware_visibility_best_effort(_dl_pairs)
    if ddb_mode and _legacy_rh:
        _shorten_visibility_best_effort(_legacy_rh)
    # #661 —— reserve 争用与 capacity 不同：这批租户刻意保留 claim，所以上面的
    # `_tid in released` 门对它们恒不成立；同时争用通常只持续毫秒，若照 capacity 等到
    # 判定窗口才回来，反而会白白丢掉可用于真正执行的时间。这里独立按 5s 小步重投，
    # 只要求 receipt handle 存在，不要求 claim 已释放；死线未知/已到则不再安排无意义重投。
    _reserve_dl_pairs = []
    for _tid in reserve_retry_tids:
        if not tid_to_rh.get(_tid):
            continue
        _dl = deadline_by_tid.get(_tid)
        _left = create_deadline.remaining_sec(_dl, now_epoch)
        if _left is None or _left <= 0:
            continue
        _wait = min(_left, _RESERVE_REQUEUE_WAIT_SEC)
        _reserve_dl_pairs.append((tid_to_rh[_tid], _wait))
    if _reserve_dl_pairs:
        print(
            f"[dispatch] #661 reserve-contention deadline-aware requeue: "
            f"{len(_reserve_dl_pairs)} msg(s), "
            f"waits={[w for _, w in _reserve_dl_pairs[:10]]}s "
            f"(保 claim 的瞬时争用小步重投,且不越过 create 死线)"
        )
        _deadline_aware_visibility_best_effort(_reserve_dl_pairs)
    # 不用 grep per-tenant 行。dedup 非空 = 有租户回队列重投(超预算才最终进 DLQ)。
    # grep 一键追全链路"成立(原来只记 command_id,tid→command_id 关联断在 dispatch 段,追踪
    # 要中转 SSM/assignments)。**分批写入**:每 50 个 tid 一条日志行(带 [i/n] 序号),既不丢
    # 任何 tid,又不把 380/批挤成一条巨长难读/易被下游截断的行。summary 行先出总数。
    _won_tids = [w["tenant_id"] for w in winners]
    print(
        f"[dispatch] batch done cmd={command_id}: "
        f"won={len(winners)} requeued={len(dedup)}"
    )
    _CHUNK = 50
    _total_chunks = (len(_won_tids) + _CHUNK - 1) // _CHUNK
    for _i in range(0, len(_won_tids), _CHUNK):
        _part = _i // _CHUNK + 1
        _chunk = ",".join(_won_tids[_i : _i + _CHUNK])
        print(
            f"[dispatch] tenants cmd={command_id} [{_part}/{_total_chunks}]=[{_chunk}]"
        )
    _log_timing()
    return {"batchItemFailures": [{"itemIdentifier": m} for m in dedup]}


def _release_claims(tenant_ids: List[str], claim_id: str) -> set:
    """失败重试路径释放认领标记(条件:claim 是我打的),让重投消息能重新认领。
    返回【成功释放且仍需重投(未达预算终态)】的 tenant_id 集合——#315 极简方案据此
    只对这些的原消息缩短 visibility(codex simple review P1:缩 visibility 必须在释放
    claim【之后】、且只对释放成功的做,否则释放慢于 15s/释放失败时消息 15s 回可见撞新鲜
    claim 被静默 ack 删 → 丢消息;释放失败的保留队列默认 960s visibility(或到 maxReceiveCount
    进 DLQ,由 DLQ 告警/redrive 恢复)兜底重投,不静默丢)。

    requires_intervention。为什么在这里而非只靠认领闸的 over-budget 分支——
    时序:SQS dlq_max_receive_count=N,消息第 N 次接收失败时 dispatch_retries
    从 N-1 ADD 到 N,此刻 SQS receiveCount=N=maxReceiveCount,消息转 DLQ,
    「第 N+1 次接收」永不发生 → 认领闸的 over-budget(`dispatch_retries > budget`,
    需第 N+1 次认领被拒才触发)永不命中 → 租户永久卡 creating(DoD#1 现象)。
    故在最后一次失败释放(retries 达 budget)时主动转终态,赶在进 DLQ 前;
    条件 `>= budget`,幂等(仅 creating 且达阈值才转,不误标已 running/终态的)。
    ReturnValues 拿新值避免二次读。"""
    if not tenant_ids:
        return set()
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException

    def _release_one(tid):
        try:
            # #671 —— 与认领闸相同,worker 只跑无状态 generated action；共享 client
            # 负责线程安全 I/O。单批并发最多 8,避免释放尾巴反过来制造 DDB throttle。
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
                _mark_retry_exhausted(tid)  # 已终态,不缩 visibility(不进返回集)
                return None
            # 释放成功且还要重投 → 可安全缩 visibility
            return tid
        except ccf:
            return None  # claim 已被别的实例接管/已释放 → 不动、不缩 visibility(交持有者)
        except Exception as e:  # noqa: BLE001
            # 释放失败 → 不进返回集,原消息保留队列默认 960s visibility(或进 DLQ 兜底)重投(不缩到 15s,
            # 否则 15s 回可见时 claim 还在会被静默 ack 删 → 丢消息,codex simple review P1)。
            print(f"[dispatch] release claim {tid} failed (non-fatal): {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(8, len(tenant_ids))) as pool:
        outcomes = pool.map(_release_one, tenant_ids)
        return {tid for tid in outcomes if tid}


def _change_visibility_batch_best_effort(pairs, log_prefix):
    """ChangeMessageVisibilityBatch(每批最多 10 条),失败只降级为默认 visibility。"""
    sqs = _sqs()
    for offset in range(0, len(pairs), 10):
        chunk = pairs[offset : offset + 10]
        entries = [
            {
                "Id": f"v{offset + index}",
                "ReceiptHandle": receipt_handle,
                "VisibilityTimeout": int(seconds),
            }
            for index, (receipt_handle, seconds) in enumerate(chunk)
        ]
        batch_call = getattr(sqs, "change_message_visibility_batch", None)
        if callable(batch_call):
            try:
                response = batch_call(
                    QueueUrl=clients.DISPATCH_QUEUE_URL,
                    Entries=entries,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[dispatch] {log_prefix} non-fatal: {e}")
                continue
            if isinstance(response, dict):
                for failed in response.get("Failed", []):
                    print(
                        f"[dispatch] {log_prefix} non-fatal: "
                        f"id={failed.get('Id')} code={failed.get('Code')} "
                        f"message={failed.get('Message', '')}"
                    )
                continue

        # #671 —— 正式 boto3 SQS client 总会返回 dict；这个窄回落仅兼容测试注入/
        # 旧 wrapper 没实现 Batch 方法的情况,生产热路径只走上面的 10 条一批。
        for entry in entries:
            try:
                sqs.change_message_visibility(
                    QueueUrl=clients.DISPATCH_QUEUE_URL,
                    ReceiptHandle=entry["ReceiptHandle"],
                    VisibilityTimeout=entry["VisibilityTimeout"],
                )
            except Exception as e:  # noqa: BLE001
                print(f"[dispatch] {log_prefix} non-fatal: {e}")


def _deadline_aware_visibility_best_effort(pairs):
    """#562 —— 按【死线】给 unplaced 消息安排下一次投递,让最后一次消费必然落进判定窗口。

    为什么必须有这个:原来的短退避 `_shorten_visibility_best_effort` 被 `if ddb_mode:` 门住
    (#315:push 模式的 unplaced 多是 host 级 inflight 串行的正常等待,15s 就催会误伤)。
    我们这套部署是 **push** 模式,所以容量不足的 unplaced 消息按队列默认 **960s** 重投 ——
    16 分钟后才回来,远在 180s 死线之后。后果:「注定超不过死线」的同步判定【结构上永不触发】,
    每个容量不足的创建都只能等 1 分钟节拍的兜底执行者,必然晚于死线。
    2026-08-21 真机压测(20 个创建 / 14 个容量失败)实测到这个形态:14 个全部 enforced_by_executor
    且全部 late,同步判死日志一条都没有。

    这里不复用那个 15s 常量,而是按剩余时间算:把下一次投递安排到【判定窗口刚打开之后】。
    这样既不"提前放弃"(issue 明确要求「期间可能有 slot 释放,不提前放弃」),又保证在死线之前
    一定有一次消费能做出同步判定(issue 同样明确「但也绝不超过 3 分钟」)。
    因为判据是死线而不是一个拍脑袋的秒数,所以它对 push / ddb 两种模式都成立,不需要模式门。

    best-effort:改可见性失败不影响正确性 —— 消息仍按默认 visibility 兜底重投,只是那时
    只能靠死线执行者判死(晚,但仍是终态)。不发新消息,无 send/write 原子性问题。
    """
    if not pairs or not clients.DISPATCH_QUEUE_URL:
        return
    _change_visibility_batch_best_effort(
        pairs, "#562 deadline-aware visibility"
    )


def _shorten_visibility_best_effort(receipt_handles):
    """#315 容量溢出短退避:把 unplaced 消息的可见性从队列默认 960s 缩到 15s,让 SQS 快速
    重投(而非等 48min),receiveCount 自然递增到 maxReceiveCount 进 DLQ。best-effort:失败
    (receiptHandle 过期/权限/限流)不影响正确性——消息仍按默认 visibility 兜底重投,只是慢。
    不发新消息,故无 send/write 原子性问题(#318 曾陷入的泥潭)。"""
    if not receipt_handles or not clients.DISPATCH_QUEUE_URL:
        return
    delay = clients.DISPATCH_UNPLACED_DELAY_BASE_SEC
    _change_visibility_batch_best_effort(
        [(receipt_handle, delay) for receipt_handle in receipt_handles],
        "shorten visibility",
    )


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
