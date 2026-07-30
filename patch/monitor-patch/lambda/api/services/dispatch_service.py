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
    normalize_spec,
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
        # #315 receiptHandle 供 unplaced 短退避:对容量不够的消息 ChangeMessageVisibility
        # 缩短可见性(960→15s),不必等默认 visibility 才重投。SQS 事件每条 record 原生带。
        rh = rec.get("receiptHandle")
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
def _snapshot_hosts(now_epoch: int, gate_inflight: bool = True) -> List[Dict[str, Any]]:
    """扫 active/idle host,算 free_slots + inflight_ok + simulated。ConsistentRead 强一致
    (与 core.scheduling._find_host 同款,防跨实例装满同一 host)。

    #315 SPLIT_BY_MODE:gate_inflight 控制是否按"host 有未过期在途命令"算 inflight_ok。
    - push 模式(gate_inflight=True):保留旧逻辑——host 有未过期 inflight → inflight_ok=False,
      binpack 跳过它(host 级串行,poller 靠 inflight 标量追踪 SSM 终态需要这个串行)。
    - ddb 模式(gate_inflight=False):inflight_ok 恒 True,不因在途命令挡装箱(host-agent 每 5s
      从 assignment 表兜底,允许一台 host 并发多批,可扩 1000 host;容量安全由 slot 级 CAS 保证)。
    """
    hosts = clients.hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    ).get("Items", [])

    # #340(codex review)— 磁盘新鲜度基准用【扫描此刻】的时间,不用 invocation 起点 now_epoch。
    # 长批次认领可能已耗时 > TTL,此时刚上报的满盘记录(ts≈real-now)相对 now_epoch 会落进"离谱
    # 未来"被误判 fail-open。用 scan 时刻做基准,ts 与它的差恒是真实新鲜度。inflight 判定仍用
    # now_epoch(那是它与 SendCommand 时序的既有语义,不动)。
    disk_now = int(time.time())
    ttl = clients.DISPATCH_INFLIGHT_TTL_SEC
    out = []
    for h in hosts:
        total_vcpu = int(h.get("total_vcpu", 0) or 0)
        used_vcpu = int(h.get("used_vcpu", 0) or 0)
        allocatable = int(total_vcpu * float(clients.CPU_OVERCOMMIT_RATIO or 1.0))
        # #330 — mem 维度可分配上限(total_mem_mb × MEM_OVERCOMMIT),CAS vcpu+mem 双闸,
        # 与同步 create 路径(handler.py:954-960)一致,防大内存租户超卖 OOM。
        # ★缺 total_mem_mb(旧 host DDB item 没写这字段)→ allocatable_mem=0 当【未知】哨兵,
        # 下游 mem 闸跳过(回落纯 vcpu 闸),绝不因字段缺失误拒整台 host(codex review 指出)。
        total_mem = int(h.get("total_mem_mb", 0) or 0)
        allocatable_mem = int(total_mem * float(clients.MEM_OVERCOMMIT_RATIO or 1.0))
        used_mem = int(h.get("used_mem_mb", 0) or 0)
        # #330 — 装箱按【真实剩余资源】双预算(free_vcpu/free_mem),不再折算成 VM_DEFAULT 名额
        # (旧 free_slots=剩余vcpu//2 把 1c:2G 租户的可装数腰斩到 282,达不到 380)。mem_known=缺
        # total_mem_mb 的老 host 内存容量未知 → 装箱侧 fail-safe 不调度(不 fail-open 当无限内存)。
        free_vcpu = max(0, allocatable - used_vcpu)
        mem_known = total_mem > 0
        free_mem = max(0, allocatable_mem - used_mem)
        # #340 — 磁盘软门:host-agent 每 poll 用 statvfs('/data') 写 avail_disk_mb +
        # disk_check_ts_epoch。剩余低于水位就不接新租户(防 /data 满 → mkdir No space →
        # requires_intervention)。fail-open:字段缺失(旧 host 从没上报)或上报陈旧
        # (host-agent 挂了/漏报,读数不可信)→ disk_ok=True 退回旧行为,绝不用过期读数误杀。
        disk_ok = _host_disk_ok(h, disk_now)
        if gate_inflight:
            inflight_ts = int(h.get("dispatch_inflight_ts_epoch", 0) or 0)
            inflight_ok = (not h.get("dispatch_inflight")) or (
                inflight_ts and (now_epoch - inflight_ts) > ttl
            )
        else:
            inflight_ok = True  # ddb:不设门,并发多批
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
                "raw": h,
            }
        )
    return out


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
    # 逐 host fail-open,绝不让一台 host 的畸形值抛异常炸掉整批装箱(codex score #2)。
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


def _try_reserve_host(
    instance_id: str,
    n: int,
    command_id: str,
    now_epoch: int,
    allocatable_vcpu: int,
    sum_vcpu: int,
    sum_mem: int,
    allocatable_mem: int,
    write_inflight: bool = True,
) -> Optional[int]:
    """单 host 单条 UpdateItem: next_vm_num+=n / used_vcpu+=Σ真实vcpu / used_mem_mb+=Σ真实mem
    (+ push 模式打 inflight)。CAS 双闸(used_vcpu<=cap_v 且 used_mem_mb<=cap_m)。

    #330 修:预留按【本批各租户真实 vcpu/mem 之和】(sum_vcpu/sum_mem),不再 n×VM_DEFAULT
    ——旧版记 n×(2/4096) 与租户声明规格无关,导致 ①账本高估(1c:2G 记成 2c/4G,r8g 有效容量
    从 564 腰斩到 282,达不到 380 目标)②与 reaper 按真实值释放不对称→双向漂移 ③mem 无闸→大内存
    租户超卖 OOM。释放侧 _rollback_host 同步按真实和扣(对称)。参照同步 create 路径(handler.py:954)。

    返回该批第一个 vm_num(reserved base);容量不够 → None。

    #315 SPLIT_BY_MODE(codex 判):按 dispatch 模式分道——
    - **push 模式**(write_inflight=True):保留旧行为——写 inflight 标量 + CAS 含 host 级
      inflight 排他门(一台 host 同一时刻一条在途命令)。push 无 assignment 表 + 无 host-agent
      reconciler,poller 是 promote/回滚唯一驱动,必须靠 inflight 标量 + 串行门追踪 SSM 终态。
    - **ddb 模式**(write_inflight=False):【不写 inflight 标量】+ CAS 只留 slot 级容量闸
      (used_vcpu <= cap_v),允许一台 host 并发多条在途命令。ddb 有 host-agent 每 5s 从
      assignment 表(desired-state 真相源)自愈 promote/重投,poller 冗余、不启用;不写标量 →
      无 last-write-wins 竞争 → 无 poller 误清/误回滚容量(codex 上轮 3 个 Error 的根)。
      这也是 1000 host 稳定启动的前提:控制面零集中扫描,host-agent 分布式自治才扩得上去。
    容量安全两模式都由 slot 级 CAS(used_vcpu <= :cap_v)保证,与 inflight 无关。
    """
    ccf = clients.hosts_table.meta.client.exceptions.ConditionalCheckFailedException
    # #330 — 预留量 = 本批各租户【真实】vcpu/mem 之和(调用方从 batch 的 params 求和传入),
    # 不再 n×VM_DEFAULT。cap_v/cap_m = 允许上限 - 本批增量;任一维负 = 装不下,直接拒。
    dv = int(sum_vcpu)
    dm = int(sum_mem)
    cap_v = int(allocatable_vcpu) - dv
    # ★mem 闸【恒开】(codex review Error3:allocatable_mem<=0 若跳过闸 = 把未知容量当无限 →
    # 大内存租户仍可超卖 OOM)。装箱侧 mem_known=False 的 host 已 fail-safe 排除,正常不会到这;
    # 万一到了(allocatable_mem<=0),cap_m<0 → 拒(fail-safe,绝不把未知当无限内存放行)。
    cap_m = int(allocatable_mem) - dm
    if cap_v < 0 or cap_m < 0:
        return None
    set_expr = (
        "next_vm_num = if_not_exists(next_vm_num, :zero) + :n, "
        "used_vcpu = if_not_exists(used_vcpu, :zero) + :dv, "
        "used_mem_mb = if_not_exists(used_mem_mb, :zero) + :dm, "
        "vm_count = if_not_exists(vm_count, :zero) + :n"
    )
    vals = {
        ":n": n,
        ":dv": dv,
        ":dm": dm,
        ":zero": 0,
        ":cap_v": cap_v,
        ":cap_m": cap_m,
    }
    # CAS 双闸恒开:vcpu + mem(#330 防大内存租户超卖 OOM;mem 未知的 host 已在装箱侧排除)。
    mem_clause = " AND used_mem_mb <= :cap_m"
    if write_inflight:
        # push 模式:写 inflight 标量 + 走 host 级排他门(旧行为保留,加 mem 闸)。
        set_expr += (
            ", dispatch_inflight = :cid, dispatch_inflight_ts = :now, "
            "dispatch_inflight_ts_epoch = :now_epoch"
        )
        cond = (
            "used_vcpu <= :cap_v" + mem_clause + " "
            "AND (attribute_not_exists(dispatch_inflight) "
            "OR dispatch_inflight_ts_epoch < :expired)"
        )
        vals.update(
            {
                ":cid": command_id,
                ":now": _now(),
                ":now_epoch": now_epoch,
                ":expired": now_epoch - clients.DISPATCH_INFLIGHT_TTL_SEC,
            }
        )
    else:
        # ddb 模式:vcpu 闸 + (已知时)mem 闸,不写 inflight 标量。
        cond = "used_vcpu <= :cap_v" + mem_clause
    update_expr = "SET " + set_expr
    if write_inflight:
        # #315(codex review7 P1)—— 写新 dispatch_inflight(command_id)时,必须原子清掉旧
        # dispatch_ssm_cid,否则出现"新 command B + 上一命令 A 的 SSM CID(SA)"矛盾组合:
        # host 短暂停在 B/SA,poller 把 SA 的 SSM 终态错误应用到 B 的租户(误 promote/误回滚 B 容量),
        # 且 review5 加的 CAS(dispatch_inflight=:cid)校的正是 B、照样通过。SendCommand 成功后
        # _record_ssm_cid 会写回 B 自己的 CID;在那之前 ssm_cid 缺失 → poller 走 missing_ssm_cid
        # 分支(still_running,等下轮),不会误清。ddb 模式不写 inflight 也无 ssm_cid,不需要。
        update_expr += " REMOVE dispatch_ssm_cid"
    try:
        resp = clients.hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=update_expr,
            ConditionExpression=cond,
            ExpressionAttributeValues=vals,
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


def _rollback_host(instance_id: str, n: int, sum_vcpu: int, sum_mem: int) -> None:
    """回滚 CAS:used_vcpu-Σ真实vcpu / used_mem_mb-Σ真实mem / vm_count-N。next_vm_num 不倒退。

    #330 修:释放量与 _try_reserve_host 的预留量【对称】(都用本批真实 vcpu/mem 之和),
    不再 n×VM_DEFAULT——否则预留真实、回滚默认(或反之)会双向漂移账本。与 scheduling._release_slot
    同款 best-effort;REMOVE dispatch_inflight 一并做。

    ⚠️ 幂等边界(codex review):本函数【非幂等】,只能由【本次 CAS 新鲜获胜】的调用方调一次。
    调用契约:dispatch_batch 里 base=_try_reserve_host() 返回非 None(赢下 CAS)后,该批在同一
    invocation 内 5 个失败分支【互斥且 continue】,最多回滚一次;重投走认领闸(dispatch_claim)+
    新一轮 CAS,看到 slot 已占用会退出,不会拿旧金额二次回滚。used_mem_mb>=:dm/used_vcpu>=:dv
    下溢条件是最后防线。若未来放开"重试接管已有 slot",必须改成可消费的 reservation_id token
    (预留时原子写、回滚时条件校验+删除+扣资源一次完成),command_id 不够——见后续 issue。
    """
    dv = int(sum_vcpu)
    dm = int(sum_mem)
    try:
        clients.hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "SET used_vcpu = used_vcpu - :dv, "
                "used_mem_mb = used_mem_mb - :dm, "
                "vm_count = vm_count - :n "
                "REMOVE dispatch_inflight, dispatch_inflight_ts, dispatch_inflight_ts_epoch, dispatch_ssm_cid"
            ),
            # #330 — cond 加 used_mem_mb >= :dm(与 vcpu 对称),防回滚把 mem 账本扣成负数
            # (codex review:原只护 vcpu/vm_count,mem 可被扣负)。
            ConditionExpression=(
                "used_vcpu >= :dv AND used_mem_mb >= :dm AND vm_count >= :n"
            ),
            ExpressionAttributeValues={
                ":dv": dv,
                ":dm": dm,
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
                    # #199 — restore 意图透传 host(缺则 launch 建空白盘=数据丢失)。
                    # encode 只在非空时写 r;空 → 普通建盘,行逐字节不变。
                    "restore_backup_key": params.get("restore_backup_key"),
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
    """SSM executionTimeout = ceil(batch × per-vm-budget / 有效并发) + 120s 余量,
    并 ≤ visibility_timeout - 60s(防假超时→回滚活 VM→账本分叉)。

    #331/#327:有效并发 = host 级槽闸数(DISPATCH_HOST_LAUNCH_CONCURRENCY,~30)不是装箱密度
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
    # #331/#327 — 传给 launch-vm.sh 的 MAX_PARALLEL(3rd arg,in-process jobs 上限)对齐 host 级
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
    # #331/#327 — 传给 launch-vm.sh 的 MAX_PARALLEL(3rd arg,in-process jobs 上限)对齐 host 级
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
    mode = clients.DISPATCH_MODE.lower()
    push_mode = mode == "push"
    ddb_mode = mode == "ddb"
    # #315 SPLIT_BY_MODE(codex review4 #3:精确按 mode==ddb 判,不把"非 push"一律当 ddb——
    # pull/误配也会误关 inflight 门):【仅 ddb】不设 inflight 门(host-agent 兜底,可扩 1000 host);
    # push 和 pull(及任何非 ddb)都【保留】旧 inflight 门 + 写标量(逐字旧行为,poller 追踪需要)。
    gate_inflight = not ddb_mode
    hosts = _snapshot_hosts(now_epoch, gate_inflight=gate_inflight)
    # push/ddb 都发真 SSM,simulated host 收不到命令 → 装箱跳过它们(压测用);
    # pull 二期由 host-agent 轮询,simulated host 可参与。
    dispatches_ssm = mode in ("push", "ddb")
    # #330(codex Error5)—— 在【认领后、装箱前】把每个 winner 的 params 里的 vcpu/mem 规范化一次
    # (唯一入口):normalize_spec 校验信任边界外的值(非 dict/负/inf/非数字 → fail-safe 回落 VM_DEFAULT)。
    # ★只覆盖 vcpu/mem_mb,【原 params 其余字段全保留】——chat_ep / restore_backup_key(#199 空盘=
    # 数据丢失)/ gateway_token_ct / device_paired_b64(#188 冷注入)等仍要透传给 manifest/assignment,
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
    result = pack(
        pending,
        hosts,
        per_host_cap=clients.DISPATCH_MAX_PARALLEL,
        skip_simulated=dispatches_ssm,
        default_vcpu=dvm,
        default_mem=dmm,
    )

    # msg_id 反查:tenant_id → msg_id(唯一,认领已保证)
    tid_to_msg = {w["tenant_id"]: w["msg_id"] for w in winners}
    # #315 tenant_id → receiptHandle(供 unplaced 短退避 ChangeMessageVisibility)
    tid_to_rh = {w["tenant_id"]: w.get("receipt_handle") for w in winners}
    alloc_by_host = {h["instance_id"]: h["allocatable_vcpu"] for h in hosts}
    # #330 — mem 维度可分配上限(CAS mem 闸用),与 alloc_by_host(vcpu)对称。
    alloc_mem_by_host = {h["instance_id"]: h.get("allocatable_mem", 0) for h in hosts}
    failures: List[str] = []
    # #315(codex final MR P1#3):容量类失败(unplaced 装箱没位子 + CAS loser 装箱给了位子
    # 但 reserve 时被并发抢输)的租户 tid,稍后对其【原消息】缩短 visibility(960→15s)快速
    # 重投——高并发接近满容量时 CAS loser 是最常见的溢出类型,漏了它容量竞争输家仍卡 960s。
    # 只缩容量类(非 SSM/manifest/写库失败):那些是基础设施故障,按默认 visibility 重投即可。
    capacity_retry_tids: set = set()
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
        # #330 — 本批各租户【真实且已校验】vcpu/mem 之和,用 binpack.normalize_spec(装箱同一入口)
        # 取数 → 装箱与 CAS 口径必然一致;非法/非正/非数字 params 在此统一 fail-safe 回落 VM_DEFAULT
        # (codex Error5:SQS 消息体是信任边界外,负数/非数字直接进账本算术会腐蚀 used_* 或炸批)。
        dvm = int(clients.VM_DEFAULT_VCPU or 2)
        dmm = int(clients.VM_DEFAULT_MEM or 4096)
        specs = [normalize_spec(t.get("params"), dvm, dmm) for t in batch]
        sum_vcpu = sum(v for v, _ in specs)
        sum_mem = sum(m for _, m in specs)
        # #315 SPLIT_BY_MODE:push 模式写 inflight 标量 + 走 host 级排他门(poller 靠它);
        # ddb 模式不写标量、只 slot CAS,允许一台 host 并发多批(host-agent 兜底,可扩 1000 host)。
        base = _try_reserve_host(
            instance_id,
            n,
            command_id,
            now_epoch,
            alloc_by_host.get(instance_id, 0),
            sum_vcpu,
            sum_mem,
            alloc_mem_by_host.get(instance_id, 0),
            write_inflight=gate_inflight,
        )
        if base is None:
            # CAS 输(容量不够;push 模式还含 inflight 未过期)→ 该批全 unplaced 走重试
            # #315(codex final MR P1#3):CAS loser 是容量类失败,纳入短退避快速重投(15s),
            # 不然高并发满容量时最常见的这类输家仍卡 960s。
            for t in batch:
                _fail(t["tenant_id"], "host CAS lost (capacity/inflight)", instance_id)
                capacity_retry_tids.add(t["tenant_id"])
            continue

        # #139:CAS 赢了 = 放置已定,先回写 host_id/vm_num/guest_ip 再分发
        # (顺序重要:先落账再发命令,SSM 再快也查得到 VM 在哪台)。
        _backfill_placement(batch, instance_id, base)

        if push_mode:
            try:
                part_count = _put_manifest_parts(command_id, instance_id, batch, base)
            except Exception as e:  # noqa: BLE001
                _rollback_host(instance_id, n, sum_vcpu, sum_mem)
                for t in batch:
                    _fail(t["tenant_id"], f"manifest write failed: {e}", instance_id)
                continue
            sent = _send_ssm_manifest(instance_id, command_id, part_count, n)
            if sent is None:
                ssm_consec_fail += 1
                _rollback_host(instance_id, n, sum_vcpu, sum_mem)
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
                _rollback_host(instance_id, n, sum_vcpu, sum_mem)
                for t in batch:
                    _fail(t["tenant_id"], "assignments write failed", instance_id)
                continue
            sent = _send_ssm_from_ddb(instance_id, command_id, n)
            if sent is None:
                ssm_consec_fail += 1
                _rollback_host(instance_id, n, sum_vcpu, sum_mem)
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
                _rollback_host(instance_id, n, sum_vcpu, sum_mem)
                for t in batch:
                    _fail(t["tenant_id"], "assignments write failed", instance_id)

    # 4) unplaced(容量不够、装箱阶段跳过的 host)→ 走【原消息】重投,不发新消息。
    # #315 容量溢出体感根治(codex 认可的极简路,替代曾陷入 send/write 原子性泥潭的"发新
    # 消息"状态机):unplaced 就是普通失败(进 batchItemFailures + 下面 _release_claims 释放
    # claim/计预算,与 CAS/SSM 失败同一条久经考验的路)。缩短原消息 visibility 挪到释放 claim
    # 【之后】、只缩成功释放的(见 #6 段),不在此立即缩——codex simple review P1:先缩后释放
    # 会在释放慢于 15s/失败时丢消息。
    for t in result.unplaced:
        _fail(t["tenant_id"], "unplaced: no host capacity this round")
        capacity_retry_tids.add(t["tenant_id"])

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
    released = _release_claims(
        [msg_to_tid[m] for m in dedup if m in msg_to_tid], command_id
    )
    # #315 极简方案:只对【容量类失败(unplaced+CAS loser)且成功释放 claim 且仍需重投】的租户
    # 缩短原消息 visibility(960→15s),让容量溢出快速重投而非等 48min。顺序在释放【之后】+ 只缩
    # 释放成功的 → 消除 codex simple review P1(先缩后释放会在释放慢/失败时 15s 内撞新鲜 claim
    # 丢消息)。释放失败/被接管/已终态的不缩,保留队列默认 960s(或到 maxReceiveCount 进 DLQ)兜底,
    # 不丢。#315 codex final MR P1#3:capacity_retry_tids 含 CAS loser,不再只 unplaced。
    # #315 codex MR recheck 阻断#1:【仅 ddb 模式】缩 visibility。push 模式(CDK 默认)的 unplaced
    # 多是 host 级 inflight 串行的正常等待(合法 SSM 可跑 840s),15s 就催会误伤——push 走原 960s。
    if ddb_mode:
        capacity_rh = [
            tid_to_rh[tid]
            for tid in capacity_retry_tids
            if tid in released and tid_to_rh.get(tid)
        ]
        _shorten_visibility_best_effort(capacity_rh)
    # #141 — batch-level fail-loud summary: 一眼看清本次 invoke 收敛多少/回退多少,
    # 不用 grep per-tenant 行。dedup 非空 = 有租户回队列重投(超预算才最终进 DLQ)。
    # #256 — 全链路可观测:打出本批 won 的**全部** tenant_id(不截断、不丢),让"按任意租户
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
    return {"batchItemFailures": [{"itemIdentifier": m} for m in dedup]}


def _release_claims(tenant_ids: List[str], claim_id: str) -> set:
    """失败重试路径释放认领标记(条件:claim 是我打的),让重投消息能重新认领。
    返回【成功释放且仍需重投(未达预算终态)】的 tenant_id 集合——#315 极简方案据此
    只对这些的原消息缩短 visibility(codex simple review P1:缩 visibility 必须在释放
    claim【之后】、且只对释放成功的做,否则释放慢于 15s/释放失败时消息 15s 回可见撞新鲜
    claim 被静默 ack 删 → 丢消息;释放失败的保留队列默认 960s visibility(或到 maxReceiveCount
    进 DLQ,由 DLQ 告警/redrive 恢复)兜底重投,不静默丢)。

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
    released_needs_retry = set()
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
                _mark_retry_exhausted(tid)  # 已终态,不缩 visibility(不进返回集)
            else:
                released_needs_retry.add(
                    tid
                )  # 释放成功且还要重投 → 可安全缩 visibility
        except ccf:
            pass  # claim 已被别的实例接管/已释放 → 不动、不缩 visibility(交持有者)
        except Exception as e:  # noqa: BLE001
            # 释放失败 → 不进返回集,原消息保留队列默认 960s visibility(或进 DLQ 兜底)重投(不缩到 15s,
            # 否则 15s 回可见时 claim 还在会被静默 ack 删 → 丢消息,codex simple review P1)。
            print(f"[dispatch] release claim {tid} failed (non-fatal): {e}")
    return released_needs_retry


def _shorten_visibility_best_effort(receipt_handles):
    """#315 容量溢出短退避:把 unplaced 消息的可见性从队列默认 960s 缩到 15s,让 SQS 快速
    重投(而非等 48min),receiveCount 自然递增到 maxReceiveCount 进 DLQ。best-effort:失败
    (receiptHandle 过期/权限/限流)不影响正确性——消息仍按默认 visibility 兜底重投,只是慢。
    不发新消息,故无 send/write 原子性问题(#318 曾陷入的泥潭)。"""
    if not receipt_handles or not clients.DISPATCH_QUEUE_URL:
        return
    delay = clients.DISPATCH_UNPLACED_DELAY_BASE_SEC
    for rh in receipt_handles:
        try:
            _sqs().change_message_visibility(
                QueueUrl=clients.DISPATCH_QUEUE_URL,
                ReceiptHandle=rh,
                VisibilityTimeout=delay,
            )
        except Exception as e:  # noqa: BLE001
            # 缩短失败纯属优化未生效(消息仍会按默认 visibility 重投),不炸 invocation。
            print(f"[dispatch] shorten visibility non-fatal: {e}")


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
