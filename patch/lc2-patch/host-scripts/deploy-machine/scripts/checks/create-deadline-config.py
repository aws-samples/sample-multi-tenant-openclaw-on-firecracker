#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#562 G13 —— 创建死线的配置基线校验器。可复核、fail-closed、带真实端点断言。

## 它为什么必须存在

180s 的业务契约不是一个常量,是【一组配置的联立结果】。三段预算
`180 = 攒批 2 + 排队 50 + 执行 128` 里的执行段由 SSM `executionTimeout` 决定,而那个值是

    executionTimeout = min( ceil(batch / slots) × per_vm + 120 , visibility - 60 )

`batch` 是 ESM 的 `BatchSize`、`slots` 是 `DISPATCH_HOST_LAUNCH_CONCURRENCY`,
两个【互不相邻、可被独立修改】的配置项。谁单独调一个,契约就静默失效 ——
而失效的表现不是报错,是「客户收到 failed,我方却给它起了一台没人认领的 VM」。
这种事必须由一个能在 CI 与部署前跑的校验器拦住,不能靠人记住。

## 本次落地时实测到的违反(2026-08-21,apse1)

    ESM BatchSize = 500,DISPATCH_HOST_LAUNCH_CONCURRENCY = 30
    → ceil(500/30) = 17 轮 → 17×8 + 120 = 256s 执行段
    → 单执行段就 256s > 180s 总死线

也就是说:**在写这个校验器之前,线上配置在数学上不可能满足 3 分钟契约**,而没有任何
现有检查会说一句话。这就是 G13 的价值 —— 它把一个只能靠人算的联立约束变成一条会红的门。

## 设计取舍(逐条对应 issue 对 G13 的要求)

- **fail-closed**:读不到配置、拿不到线上值、算不出数 —— 一律【非零退出】。绝不「拿不到就跳过」:
  那会让这个门在最需要它的时候(凭证过期、region 写错、资源被删)安静放行。
- **语义检查而非键数比对**:不数键、不做 diff。每条断言都写成「这个值必须落在什么区间、
  为什么是这个方向」。键数比对对「值被改坏但键还在」完全无感,而那正是本类事故的形态。
- **方向性区间**:每条约束都注明偏大/偏小各自的后果,而不是只给一个 magic number。
- **真实端点断言**:`--live` 会去查 ESM / SQS / Lambda env 的【当前真值】。仓里的 config.yml
  只是意图,部署漂移才是事故来源(#458 刚证过一次:S3 上的脚本比仓里少 87 行)。
- **反向验证**:`--selfcheck` 把每条约束逐个改坏,断言校验器【真的会红】。一个恒绿的校验器
  和没有校验器是同一件事。

用法:
    python3 scripts/checks/create-deadline-config.py                 # 只查仓内配置
    python3 scripts/checks/create-deadline-config.py --live          # 加查线上真值
    python3 scripts/checks/create-deadline-config.py --selfcheck     # 反向验证本校验器
退出码:0=全部达标;1=有违反;2=无法完成检查(fail-closed)。
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent.parent
_REGION = "ap-southeast-1"
"""默认区域。**#564 G8 让它可被 `--region` 覆盖**(默认值不变,存量调用行为一致)。

理由:G8 明文「发布前必须跑一次 `--live`:仓里对不代表线上对(#458 证过一次)」。而这个
常量写死之后 `--live` 只能查 ap-southeast-1 —— 在任何其它环境上跑,它会拿另一个区域的
真值来判当前环境达标,那是**一个看起来像结论的假象**(与 `_aws` 里那条 `--region` 注释
记的是同一类事故)。"""

_FUNCTION = "openclaw-api"
_BACKUP_FUNCTION = "openclaw-backup"
"""#564 G6 ② —— 通道 C(api_fn 自调用)与通道 D(backup_fn 异步 invoke)的失败出口就挂在
这两个函数上。G8 表格第 4 行要查的正是「它们的异步失败有没有出口」。"""
_QUEUE_NAME = "openclaw-dispatch"
_LIFECYCLE_QUEUE_NAME = "openclaw-lifecycle.fifo"
"""#564 —— 通道 B 的队列。它的 `visibility × maxReceiveCount` 与死线的比值是 G8 要求
**记录而非约束**的那一项(见 `_check_lifecycle_dlq_ratio`)。"""

# ── 死线三段预算:从产品代码里【读】,不在这里重抄一遍 ───────────────────────
#
# 重抄一份就等于给同一个口径造了第二个真相源 —— 那正是本校验器要防的失败模式,
# 在校验器自己身上犯就格外可笑。import 真模块,它改了这里自动跟着改。
sys.path.insert(0, str(_REPO / "deploy" / "lambda" / "api"))
try:
    import core.create_deadline as cdl  # noqa: E402
except Exception as e:  # noqa: BLE001 —— fail-closed:读不到口径就不能声称检查过
    print(f"FATAL 读不到死线口径模块 core/create_deadline.py: {type(e).__name__}: {e}")
    sys.exit(2)


class Violation(Exception):
    """一条明确的不达标。与「无法完成检查」区分:后者退 2,这个退 1。"""


def _fatal(msg: str) -> None:
    print(f"FATAL {msg}")
    sys.exit(2)


# ══════════════════════════════════════════════════════════════════════════
# 配置读取
# ══════════════════════════════════════════════════════════════════════════


def _load_repo_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """读仓内 config.yml。读不到 = fail-closed 退 2。"""
    p = path or (_REPO / "config.yml")
    if not p.exists():
        _fatal(f"配置文件不存在: {p}")
    try:
        import yaml
    except ImportError:
        _fatal("需要 pyyaml 才能解析 config.yml(fail-closed:装不上就不放行)")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        _fatal(f"config.yml 解析失败: {type(e).__name__}: {e}")
    if not isinstance(data, dict):
        _fatal(f"config.yml 顶层不是映射: {type(data).__name__}")
    return data


def _aws(args: List[str]) -> Any:
    """跑一条 aws CLI 取 JSON。任何失败 = fail-closed。

    显式传 `--region`:本机环境里 `AWS_REGION=us-east-1`,而 CLI 认它【优先于】
    命令内联的 AWS_DEFAULT_REGION。不写 --region 会静默查错区域,然后报「资源不存在」
    —— 那是一个看起来像结论的假象(2026-08-21 实际踩过)。
    """
    cmd = ["aws", *args, "--region", _REGION, "--output", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        _fatal(f"aws 调用失败 {' '.join(args[:2])}: {type(e).__name__}: {e}")
    if r.returncode != 0:
        _fatal(f"aws {' '.join(args[:2])} 退出 {r.returncode}: {r.stderr.strip()[:300]}")
    try:
        return json.loads(r.stdout or "null")
    except json.JSONDecodeError as e:
        _fatal(f"aws {' '.join(args[:2])} 输出不是 JSON: {e}")


def _load_live() -> Dict[str, Any]:
    """查线上真值:ESM(batch/窗口/并发/失败上报)、队列(visibility)、Lambda env(slots/per-vm)。"""
    esms = _aws(["lambda", "list-event-source-mappings", "--function-name", _FUNCTION])
    disp = [
        m for m in (esms or {}).get("EventSourceMappings", [])
        if m.get("EventSourceArn", "").endswith(f":{_QUEUE_NAME}")
    ]
    if len(disp) != 1:
        _fatal(
            f"期望恰好一个指向 {_QUEUE_NAME} 的 ESM,实到 {len(disp)} 个 —— "
            "两个 ESM 会让同一批消息被消费两次,批大小口径也无从判定"
        )
    esm = disp[0]

    acct = _aws(["sts", "get-caller-identity"])["Account"]
    qurl = f"https://sqs.{_REGION}.amazonaws.com/{acct}/{_QUEUE_NAME}"
    qattr = _aws([
        "sqs", "get-queue-attributes", "--queue-url", qurl,
        "--attribute-names", "All",
    ])["Attributes"]

    _api_cfg = _aws([
        "lambda", "get-function-configuration", "--function-name", _FUNCTION,
    ])
    env = _api_cfg.get("Environment", {}).get("Variables", {})

    # #564 G6 ② —— 通道 C/D 的异步失败出口(`DeadLetterConfig.TargetArn`)。
    # 这一项此前是「报告未实现」,G6 落地后改成真检查(见 `_check_async_failure_exits`)。
    _async_dlq = {
        _FUNCTION: (_api_cfg.get("DeadLetterConfig") or {}).get("TargetArn"),
        _BACKUP_FUNCTION: (
            _aws([
                "lambda", "get-function-configuration",
                "--function-name", _BACKUP_FUNCTION,
            ]).get("DeadLetterConfig") or {}
        ).get("TargetArn"),
    }

    # #564 G8 —— 通道 B(lifecycle FIFO)的队列属性。取不到不 fatal:那条检查的口径是
    # 「记录实际比值」而非硬约束,而且存量部署可能压根没建这个队列(CREATE_VIA_QUEUE=false)。
    _lc_vis, _lc_mrc = 0, 0
    try:
        _lc_url = f"https://sqs.{_REGION}.amazonaws.com/{acct}/{_LIFECYCLE_QUEUE_NAME}"
        _lc_attr = _aws([
            "sqs", "get-queue-attributes", "--queue-url", _lc_url,
            "--attribute-names", "All",
        ])["Attributes"]
        _lc_vis = int(_lc_attr.get("VisibilityTimeout") or 0)
        _lc_mrc = int(
            (json.loads(_lc_attr.get("RedrivePolicy") or "{}") or {}).get(
                "maxReceiveCount"
            )
            or 0
        )
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — 见上方理由,缺队列不是本校验器要判死的事
        pass

    return {
        "batch_size": int(esm.get("BatchSize") or 0),
        "batching_window_sec": int(esm.get("MaximumBatchingWindowInSeconds") or 0),
        "esm_max_concurrency": int(
            (esm.get("ScalingConfig") or {}).get("MaximumConcurrency") or 0
        ),
        "report_batch_item_failures": "ReportBatchItemFailures"
        in (esm.get("FunctionResponseTypes") or []),
        "esm_state": esm.get("State"),
        "visibility_sec": int(qattr.get("VisibilityTimeout") or 0),
        "redrive": json.loads(qattr.get("RedrivePolicy") or "{}"),
        "slots": int(env.get("DISPATCH_HOST_LAUNCH_CONCURRENCY") or 0),
        "per_vm_sec": int(env.get("DISPATCH_PER_VM_BUDGET_SEC") or 8),
        "dispatch_mode": env.get("DISPATCH_MODE", ""),
        "retry_budget": int(env.get("DISPATCH_RETRY_BUDGET") or 0),
        # #564 G8 / G5 —— 七档死线的线上真值(漂移复检的右手边)+ 通道 B 队列参数。
        "deadline_env": {
            a: env.get(cdl.env_name_for(a)) for a in cdl.DEADLINE_ACTIONS
        },
        "lifecycle_visibility_sec": _lc_vis,
        "lifecycle_max_receive_count": _lc_mrc,
        "async_dlq": _async_dlq,
    }


def _derive_from_repo(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """从仓内 config.yml 推出同一组量,让「意图」与「线上」可以逐条对。

    slots 不在 config.yml 里(它是 CDK 写进 Lambda env 的),仓内视角只能取代码默认值。
    这一点必须说清楚:仓内检查【不能】替代 --live,它只能拦住「意图本身就写错了」。
    """
    d = cfg.get("dispatch") or {}
    return {
        "batch_size": int(d.get("max_batch_size") or 0),
        "batching_window_sec": int(d.get("batching_window_seconds") or 0),
        "esm_max_concurrency": int(d.get("esm_max_concurrency") or 0),
        "max_receive_count": int(d.get("dlq_max_receive_count") or 0),
        "dispatch_enabled": bool(d.get("enabled")),
        "dispatch_mode": str(d.get("mode") or ""),
    }


# ══════════════════════════════════════════════════════════════════════════
# 约束(每条都带方向与后果,不是 magic number)
# ══════════════════════════════════════════════════════════════════════════


def _check_batch_fits_one_round(v: Dict[str, Any], where: str) -> List[str]:
    """【硬约束】BatchSize ≤ slots —— 保证任何一批都只跑一轮,执行段可证为 128s。

    偏大的后果(这是真出过的那一侧):batch 一超过 slots 就跳一整轮,
    `ceil(batch/slots)×per_vm` 阶梯式变大。batch=500/slots=30 → 17 轮 → 256s 执行段,
    单这一段就超过 180s 总死线 → SSM 还在起 VM 而租户已被判死 → 孤儿 VM(占容量、计费、
    没人认领,且 status 已是 failed 不在任何 creating 自愈扫描面里)。
    偏小的后果:批变多、每批开销摊薄得少,吞吐下降。但 ESM 并发(MaximumConcurrency)
    可以补回来 —— 30×10/2s ≈ 150 TPS 的消费能力,远高于目标 20 TPS。所以【宁可偏小】。
    """
    out = []
    batch, slots = v.get("batch_size", 0), v.get("slots", 0)
    if batch <= 0 or slots <= 0:
        return [f"[{where}] batch({batch})/slots({slots}) 取不到正值,无法判定执行段"]
    if batch > slots:
        rounds = math.ceil(batch / slots)
        est = rounds * v.get("per_vm_sec", 8) + 120
        out.append(
            f"[{where}] BatchSize={batch} > DISPATCH_HOST_LAUNCH_CONCURRENCY={slots}:"
            f" 一批要跑 {rounds} 轮 → 执行段 {est}s > 预算 {cdl.EXEC_BUDGET_SEC}s"
            f"(总死线只有 {cdl.DEADLINE_TOTAL_SEC}s)。后果是 SSM 在租户已被判死后仍在起 VM"
            f" → 孤儿 VM。修法:把 dispatch.max_batch_size 降到 ≤ {slots}"
        )
    return out


def _check_exec_budget(v: Dict[str, Any], where: str) -> List[str]:
    """执行段(按真实参数算)必须装进预算,且 visibility 要留得下它。"""
    out = []
    batch = v.get("batch_size", 0)
    slots = v.get("slots", 0)
    per_vm = v.get("per_vm_sec", 8)
    vis = v.get("visibility_sec", 0)
    if not all((batch, slots, vis)):
        return [f"[{where}] 缺 batch/slots/visibility,无法核算执行段(fail-closed)"]
    real = cdl.exec_budget_for(batch, slots, per_vm, vis)
    if real > cdl.EXEC_BUDGET_SEC:
        out.append(
            f"[{where}] 实际执行段 {real}s > 预算 {cdl.EXEC_BUDGET_SEC}s"
            f"(batch={batch} slots={slots} per_vm={per_vm} visibility={vis})"
        )
    # visibility 是否把 SSM 超时【压小】了 —— 即 min() 的 `visibility - 60` 那一支赢了。
    #
    # 判据必须拿【未被 cap 的估算】去比,不能拿 exec_budget_for 的返回值比:那个返回值已经
    # 是 min(估算, visibility-60),所以 `visibility - 60 < 返回值` 在数学上【永不成立】——
    # 我第一版就是这么写的,反向验证(--selfcheck)当场指出它是个摆设。留这段注释是因为
    # 这类「用被钳制后的值去检查钳制」的写法看起来非常合理,很容易再犯一次。
    uncapped = math.ceil(batch / slots) * per_vm + 120
    cap = vis - 60
    if uncapped > cap:
        out.append(
            f"[{where}] VisibilityTimeout={vis}s 太短:本批需要 {uncapped}s 的 SSM 超时,"
            f"但 visibility 只允许 {cap}s(公式 min(…, visibility-60) 的后一支赢了)。"
            f"后果是 launch 被 SSM 提前掐断,或消息在 SSM 未完成时重新可见 → 同一租户被消费两次。"
            f"需要 VisibilityTimeout ≥ {uncapped + 60}s"
        )
    return out


def _check_batching_window(v: Dict[str, Any], where: str) -> List[str]:
    """攒批窗口必须等于死线口径里的 2s(客户决策:不要攒那么久)。

    偏大:直接吃掉排队预算 —— 窗口每多 1s,等容量的 50s 就少 1s。
    偏小:批更碎、SSM 调用次数上升,更容易撞 SSM 限流(#523 处置过那一类)。
    所以这条是【相等】约束,不是区间。
    """
    got = v.get("batching_window_sec")
    if got != cdl.BATCH_WINDOW_SEC:
        return [
            f"[{where}] 攒批窗口 {got}s ≠ 死线口径里的 {cdl.BATCH_WINDOW_SEC}s:"
            f"每多 1s 就从「等容量」的 {cdl.QUEUE_BUDGET_SEC}s 预算里扣掉 1s"
        ]
    return []


def _check_report_batch_item_failures(v: Dict[str, Any], where: str) -> List[str]:
    """ESM 必须开 ReportBatchItemFailures。

    不开的后果:消费者返回的 batchItemFailures 被忽略,整批要么全删要么全重投。
    「过期消息 ack 删除、失败消息退避重试」这两件事就都做不到 —— G7 与 G10 一起失效,
    而且是静默失效(代码照样返回那个字段,只是没人看)。
    """
    if not v.get("report_batch_item_failures"):
        return [
            f"[{where}] ESM 未开 ReportBatchItemFailures:消费者返回的 batchItemFailures"
            "会被忽略 → 整批全删或全重投,G7(过期 ack)与 G10(DLQ 语义)同时失效"
        ]
    return []


def _check_dlq(v: Dict[str, Any], where: str) -> List[str]:
    """必须配 DLQ 且 maxReceiveCount 有界。

    没有 DLQ:毒消息无限重投,占满消费能力(#141 处置过的 stuck 形态)。
    maxReceiveCount 过大:一条坏消息要烧很久才进 DLQ,期间挤占正常创建的消费槽。
    """
    out = []
    rd = v.get("redrive") or {}
    if not rd.get("deadLetterTargetArn"):
        out.append(f"[{where}] 主队列没有配 DLQ:毒消息会无限重投占满消费能力")
    mrc = int(rd.get("maxReceiveCount") or 0)
    if not (1 <= mrc <= 5):
        out.append(
            f"[{where}] maxReceiveCount={mrc} 不在 [1,5]:过大则一条坏消息长期"
            "挤占消费槽,过小(0/缺失)则等于没有重试"
        )
    return out


def _check_scaler_cannot_save_this_request(cfg: Dict[str, Any]) -> List[str]:
    """扩容节拍必须【慢于】死线 —— 这不是缺陷,是必须被承认的前提。

    形态第 4 条据此成立:「判定只看当前已就绪机队容量,不需要任何预测模型」。
    若有人把 scaler.interval_minutes 调到远小于死线(比如 10 秒),那条推理的前提就变了,
    「等一会儿可能有容量」重新成立,判死策略需要重新论证 —— 所以这里要求它保持
    ≥ 死线,并把理由写在报错里,而不是让人以为调快 scaler 就能提升成功率。
    """
    iv = int(((cfg.get("scaler") or {}).get("interval_minutes")) or 0)
    if iv <= 0:
        return ["[repo] scaler.interval_minutes 取不到正值"]
    if iv * 60 < cdl.DEADLINE_TOTAL_SEC:
        return [
            f"[repo] scaler.interval_minutes={iv}(={iv * 60}s)已快于死线"
            f" {cdl.DEADLINE_TOTAL_SEC}s:形态第 4 条「不需要预测模型、等下去没有意义」"
            "的前提随之改变,判死策略必须重新论证后再放行"
        ]
    return []


def _check_budget_selfconsistent() -> List[str]:
    """三段预算必须自洽(直接调产品代码那条断言,不重抄)。"""
    try:
        cdl.assert_budget_consistent()
    except Exception as e:  # noqa: BLE001
        return [f"[code] 死线三段预算不自洽: {e}"]
    return []


# ══════════════════════════════════════════════════════════════════════════
# #564 G8 —— 七档 per-action 死线的约束(承 #562 G13)
# ══════════════════════════════════════════════════════════════════════════


def _check_per_action_deadline_floor() -> List[str]:
    """每档死线不得小于该操作单次最坏执行 —— G8 表格第 1 行,唯一一条防往小调的约束。

    「偏小 → 判死后动作还在跑 → 孤儿资源」。直接调产品代码那条断言,不在这里重抄算术
    (与 `_check_budget_selfconsistent` 同款做法)。
    """
    try:
        cdl.assert_deadline_config_sane()
    except Exception as e:  # noqa: BLE001
        return [f"[code] per-action 死线配置不安全: {e}"]
    return []


def _check_exec_budget_covers_the_real_ssm_timeout() -> List[str]:
    """#565 G1 —— **预算不能只是纸面。**

    预算表写「执行段 120s」而代码还给 `_ssm_run` 传 300s:三段之和照样等于死线、
    `assert_all_budgets_consistent()` 照样绿,而线上照旧「判死了、SSM 还在跑」→ 孤儿资源。
    所以必须有一条检查把**预算表**与**代码里真实传的那个 timeout** 绑在一起。

    判据:扫 `tenant_service.py` 与 `core/ssm_dispatch.py` 里每个 `_ssm_run(...)` 的
    `timeout=` 实参,凡是**字面整数**且大于八档里最大的执行段的,都报告出来 —— 那说明有一条
    生命周期路径上的等待上界超过了任何一档的预算。走 **AST**:这两个文件的注释里逐字出现过
    `timeout=300`(本轮解释预算的注释自己就写了它),文本匹配必然假红。

    **为什么是"报告"而不是"判错"**:`_ssm_run` 还有一批与死线词汇表无关的调用方
    (`reset`/`resize`/探针),它们没有客户死线,大 timeout 是正当的。判错会把它们一起打红,
    而那不是本约束要管的事。但它们占的是**同一个 consumer 槽**,所以一条 300s 的 `reset`
    会挤占词汇表内各档的排队段 —— 这条耦合必须被看见,故打印而不静默。

    #604 —— **`start` 从这批"无死线调用方"里毕业了**:它进了死线词汇表(七档变八档),
    `_ssm_run` 那条也从字面 `300` 换成 `exec_step_sec(start, "launch-vm")`=60,所以它不再
    出现在下面的可疑清单里(本条检查的输出因此从 5 处降到 4 处 —— 那正是这次改动落地的
    一个旁证)。上面那段举例随之改用 `reset`,它仍传字面 300。
    """
    # 判据取词汇表里**最小**的执行段,不是最大的。取最大(backup 的 300)会让这条检查几乎
    # 无牙:`reset` 恰好也传 300,`300 > 300` 为假,一条都报不出来。
    # 取最小才对得上要看见的那件事:**通道 B 的消费槽是共享的** —— 一条 300s 的 `reset` 占住
    # 一个槽 300 秒,而 restart 的排队段只有 105s,期间任何排在它后面的 restart 都必然超死线。
    #
    # **档位名必须跟着数据算,不能写死**(#604 抓的):原文把标签硬写成「= restart」,而
    # `start` 进词汇表后最小值变成它的 60s,于是那行报告会指着 60 说"这是 restart 的" ——
    # 一个把读者引向错误档位的输出,比不打印更糟。
    _by_exec = {a: cdl.exec_sec(a) for a in cdl.DEADLINE_ACTIONS}
    smallest_action = min(_by_exec, key=lambda a: (_by_exec[a], a))
    smallest = _by_exec[smallest_action]
    suspicious = []
    for rel in (
        "deploy/lambda/api/services/tenant_service.py",
        "deploy/lambda/api/core/ssm_dispatch.py",
        "deploy/lambda/backup/handler.py",
    ):
        path = _REPO / rel
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError) as e:
            return [f"[repo] 读不到/解析不了 {rel}: {type(e).__name__}: {e}"]
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "_ssm_run":
                continue
            for kw in n.keywords:
                if kw.arg != "timeout":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(
                    kw.value.value, int
                ):
                    if kw.value.value > smallest:
                        suspicious.append(f"{rel}:{n.lineno} timeout={kw.value.value}")
    if suspicious:
        print(
            f"  ! 有 {len(suspicious)} 处 `_ssm_run` 的字面 timeout 超过死线词汇表里"
            f"最小的执行段({smallest}s = {smallest_action})。若其中任何一处在词汇表路径上,"
            f"那一档的预算就是纸面的;若在非死线动作上(reset/resize/探针),它们仍占同一个"
            f"通道 B 消费槽 —— 一条 300s 的 reset 能把 restart 的 105s 排队段整段吃掉"
            f"(#565 G4/G5 的耦合): "
            + ", ".join(suspicious)
        )
    return []


def _check_deadline_config_parity(cfg: Dict[str, Any]) -> List[str]:
    """`config.yml` 的 `lifecycle.deadline_sec` 与代码里那份客户表格值必须**同源**。

    G5 第 1 条:两边不同源时,「客户手改线上 env」与「我方 `cdk deploy`」会互相覆盖,
    而覆盖是静默的。这里只判**仓内两个来源**是否一致(线上漂移由 `--live` 那条查):
      · config 少写某档 → **不判错**,只打印。#630 起 `lambdas.py` 无条件注入八档
        (config 写了用 config 的值,没写用 `create_deadline.default_deadline_sec_for()`
        的权威默认),并同步建齐八个 SSM 参数。所以「没写」= 用默认值,不等于「没注入」,
        「改 env 即可」对没写的那几档同样成立;
      · config 写了但值不是正整数 → **判错**。CDK 会把它 `int()` 后注入,
        `True`/`180.5`/`"abc"`/`-1` 这几种要么在 synth 期炸、要么静默改成别的数字,
        要么让 `assert_deadline_config_sane()` 在 Lambda **导入期** raise → 全路由 502
        (#630 的原始故障形态);
      · config 与代码默认不等 → **不报错**。config 是权威(客户可以调),代码默认只是
        config 没写时 CDK 注入的那个数。这里把差异**打印出来**,让部署者确认那是有意调的。

    历史:本函数原来把「缺段/缺档」判成违反,理由是「那几档不会被注入 env」。#630 在
    apse1 真机上证明那句话的因果反了 —— 缺 env 不是「回落到默认」而是导入期 raise,
    修法是让 CDK 补齐而不是让每份 config 各抄一遍默认表。CDK 补齐之后,原来那条违反
    连同它的文案都成了假话,故改为打印。
    """
    out: List[str] = []
    dl = (cfg.get("lifecycle") or {}).get("deadline_sec") or {}
    missing = [a for a in cdl.DEADLINE_ACTIONS if a not in dl]
    if missing:
        print(
            f"  · config.yml 的 lifecycle.deadline_sec 未写: {', '.join(missing)}"
            f" —— CDK(#630 起)按 create_deadline 的权威默认把这几档注入 env 并建 SSM 参数,"
            f"「改 env 即可」仍成立;只有想改**默认值本身**才需要在 config 里写。"
        )
    for act in cdl.DEADLINE_ACTIONS:
        if act not in dl:
            continue
        val = dl[act]
        # bool 是 int 的子类,`int(True)` == 1 —— 不排掉它,`create: true` 会静默变成 1s。
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            out.append(
                f"[repo] lifecycle.deadline_sec.{act}={val!r} 不是正整数 —— CDK 会把它"
                f"注入 LIFECYCLE_DEADLINE_SEC_{act.upper()},非整数在 synth 期就炸,"
                f"小数被静默截断,≤0 让 assert_deadline_config_sane() 在 Lambda 导入期"
                f"raise → 每条路由 502(#630)"
            )
    return out


def _report_deadline_floors_not_covered() -> List[str]:
    """把「哪几档还没有下界守护」显式说出来 —— 不静默,但**不判错**。

    G8 表格第 1 行要的是 `deadline ≥ 最坏执行`,而目前只有 create 有权威的最坏执行值
    (128s,来自 SSM executionTimeout 的算术)。另外六个的预算分解 issue 自己划给了
    **#565**(「本 issue 出机制,#565 出达标与契约」)。

    为什么必须打印而不是跳过:一个只覆盖 1/7 的检查如果安静地过了,读者会以为七档都验过。
    已知它不会是小数 —— suspend/restore/rebuild/delete/backup 都含一次同步备份,而备份侧
    真实墙钟上界约 305s(#565 G1-a 实测),**305 > 180**。也就是说客户给那三档的 180s
    在当前实现下装不下最坏一次备份;判定与达标归 #565,这里只如实报告。
    """
    uncovered = cdl.deadline_actions_without_worst_exec()
    if not uncovered:
        return []
    print(
        f"  ! per-action 死线下界:{len(cdl.DEADLINE_ACTIONS) - len(uncovered)}/"
        f"{len(cdl.DEADLINE_ACTIONS)} 档有权威的「单次最坏执行」值可比。"
        f"未覆盖: {', '.join(uncovered)}(预算分解归 #565,**不要当成已验证**)"
    )
    return []


def _check_lifecycle_dlq_ratio(live_v: Dict[str, Any]) -> List[str]:
    """记录通道 B 的 `visibility × maxReceiveCount` 与死线的实际比值 —— G8 表格第 3 行。

    原文口径是「**记录实际比值,不假装能压进死线**」,所以这条**只报告、不判错**:
    要把进 DLQ 压进 180s 就得把 visibility 降到 ≤180s,而 AWS 硬约束
    `visibility ≥ Lambda timeout`,consumer timeout 是 900s —— 那个 900s 又是 #422 从
    360s 提上来的(360s 会把合法 suspend 硬杀在中途卡成 `suspending`)。这条链是死结。
    误把 DLQ 当终态保证的后果见 G6 那三条理由。
    """
    vis = int(live_v.get("lifecycle_visibility_sec") or 0)
    mrc = int(live_v.get("lifecycle_max_receive_count") or 0)
    if not vis or not mrc:
        return [
            "[live] 取不到 lifecycle 队列的 visibility / maxReceiveCount —— "
            "无法记录进 DLQ 的实际时机,G8 第 3 行这一项判不了"
        ]
    worst = vis * mrc
    for act in ("suspend", "delete"):
        dl_sec = cdl.deadline_sec_for(act)
        print(
            f"  ! 通道 B 进 DLQ 最坏时机 = visibility {vis}s × maxReceiveCount {mrc}"
            f" = {worst}s ≈ {worst // 60} 分钟,是 {act} 死线({dl_sec}s)的"
            f" {worst / dl_sec:.0f} 倍 —— DLQ 只能兜底告警,兑现死线靠消费侧同步判死"
        )
    return []


_ASYNC_DLQ_FNS = ("api_fn", "backup_fn")
"""#564 G6 ② —— IaC 里必须给异步 DLQ 的两个函数变量名(通道 C 与通道 D 的承载者)。"""


def _check_async_failure_exits_iac() -> List[str]:
    """G8 表格第 4 行(通道 C/D 的异步失败出口)—— **仓内**判据,不需要凭据。

    这条检查此前是一句「本轮未实现」的报告,理由是补出口归 G6。**G6 已在 !819 落地**
    (`api_fn` 与 `backup_fn` 都配了 `dead_letter_queue_enabled=True`,真机已验两个
    `DeadLetterConfig.TargetArn` 都在),所以那句报告从那一刻起就是在对外撒谎 —— 校验器
    自己成了「文档承诺了代码没兑现」的反面:代码兑现了,校验器还在说没有。改成真检查。

    判据走 AST 而不是文本:`dead_letter_queue_enabled=True` 在本文件里出现三次
    (第三次是 `audit_archive_fn`),文本计数分不清是哪个函数配上了,而"数对了但配错了
    函数"正是这条要防的形态。AST 能把关键字**绑定到具体那个赋值**上。
    """
    src = _REPO / "deploy" / "stacks" / "lambdas.py"
    try:
        tree = ast.parse(src.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as e:
        return [f"[repo] 读不到/解析不了 {src.name}: {type(e).__name__}: {e}"]
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name) or tgt.id not in _ASYNC_DLQ_FNS:
                continue
            found[tgt.id] = any(
                kw.arg == "dead_letter_queue_enabled"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.value.keywords
            )
    out = []
    for name in _ASYNC_DLQ_FNS:
        if name not in found:
            out.append(
                f"[repo] {src.name} 里找不到 `{name} = <Call>(...)` —— "
                "通道 C/D 的异步失败出口无从判定"
            )
        elif not found[name]:
            out.append(
                f"[repo] {name} 没有 `dead_letter_queue_enabled=True`:异步失败无声消失。"
                "#532 真机证据:两个租户 running→deleting 后永久卡住,消息耗尽重投进 DLQ "
                "再无人接管,客户侧看到一个永不终结的 deleting"
            )
    return out


def _check_async_failure_exits(v: Dict[str, Any], where: str) -> List[str]:
    """G8 表格第 4 行的**线上**judgment:两个函数的 `DeadLetterConfig` 必须真的在。

    与上面那条仓内检查成对:仓里写了不代表部署上去了(#458 证过一次部署漂移)。
    """
    out = []
    for fn, arn in (v.get("async_dlq") or {}).items():
        if not arn:
            out.append(
                f"[{where}] {fn} 没有 DeadLetterConfig:通道 C/D 的异步失败无声消失"
            )
        else:
            print(f"  · {fn} 异步 DLQ = {arn}")
    return out


# ══════════════════════════════════════════════════════════════════════════
# 编排
# ══════════════════════════════════════════════════════════════════════════

_REPO_CHECKS = (_check_batching_window,)
_LIVE_CHECKS = (
    _check_batch_fits_one_round,
    _check_exec_budget,
    _check_batching_window,
    _check_report_batch_item_failures,
    _check_dlq,
    _check_async_failure_exits,
)


def run(live: bool, cfg_path: Optional[Path] = None) -> Tuple[List[str], Dict[str, Any]]:
    """跑全部检查,返回 (违反列表, 实测值)。"""
    cfg = _load_repo_config(cfg_path)
    repo_v = _derive_from_repo(cfg)
    problems: List[str] = []
    problems += _check_budget_selfconsistent()
    problems += _check_scaler_cannot_save_this_request(cfg)
    # #564 G8 —— 七档 per-action 死线。前两条判错,后两条只报告(理由见各自 docstring)。
    problems += _check_per_action_deadline_floor()
    problems += _check_exec_budget_covers_the_real_ssm_timeout()
    problems += _check_deadline_config_parity(cfg)
    problems += _report_deadline_floors_not_covered()
    problems += _check_async_failure_exits_iac()
    for chk in _REPO_CHECKS:
        problems += chk(repo_v, "repo")

    # 仓内视角也能查一次 batch ≤ slots:slots 取代码默认值(30)。这条【不能】替代 --live,
    # 但能在没有凭证的 CI 里就把「意图本身写错」拦住 —— 而本次实测到的违反正属于这一类。
    repo_with_default_slots = dict(repo_v)
    repo_with_default_slots.setdefault("slots", 30)
    repo_with_default_slots.setdefault("per_vm_sec", 8)
    problems += _check_batch_fits_one_round(repo_with_default_slots, "repo")

    observed: Dict[str, Any] = {"repo": repo_v}
    if live:
        live_v = _load_live()
        observed["live"] = live_v
        for chk in _LIVE_CHECKS:
            problems += chk(live_v, "live")
        for key in ("batch_size", "batching_window_sec", "esm_max_concurrency"):
            if repo_v.get(key) and live_v.get(key) != repo_v.get(key):
                problems.append(
                    f"[drift] {key}: config.yml={repo_v[key]} 但线上={live_v.get(key)}"
                    " —— 仓里的意图没有真的部署上去(或线上被手改过)"
                )
        problems += _check_lifecycle_dlq_ratio(live_v)
        # #564 G5 第 1 条 —— 七档死线的漂移复检。
        #
        # 这条**刻意只报告不判错**,而上面那三个量是判错的。区别在于:客户被明确许可
        # 「改 Lambda env 即可修改每个 lifecycle 配置」,所以线上与 config 不等**是预期的
        # 正常状态**(客户刚调过)。判错会让每一次客户合法调参都把我方 CI/部署门打红。
        # 它要防的是另一件事:调完没人回填 config,下一次 `cdk deploy` 静默覆盖回去。
        # 所以口径是"把差异摊在部署者眼前",而不是"拦住部署"。
        _live_dl = live_v.get("deadline_env") or {}
        _cfg_dl = (cfg.get("lifecycle") or {}).get("deadline_sec") or {}
        for _act in cdl.DEADLINE_ACTIONS:
            _want = _cfg_dl.get(_act)
            _got = _live_dl.get(_act)
            if _got is None:
                print(
                    f"  ! [deadline-drift] {_act}: 线上 Lambda 没有 "
                    f"{cdl.env_name_for(_act)} —— 运行时走代码默认值,"
                    "「改 env 即可」对这一档不成立(下一次 cdk deploy 会补上)"
                )
            elif _want is not None and str(_want) != str(_got):
                print(
                    f"  ! [deadline-drift] {_act}: config.yml={_want} 但线上={_got}"
                    " —— 若这是客户临时调参,**必须回填 config.yml**,否则下一次"
                    " cdk deploy 会静默覆盖回去(G5 第 1 条)"
                )
    return problems, observed


def _selfcheck() -> int:
    """反向验证:逐条把约束改坏,校验器必须真的报出【那一条】。

    没有这一步,一个恒绿的校验器会被当成「配置达标」的证据 —— 那比没有校验器更糟。
    """
    cases = [
        (
            "batch > slots(执行段跳轮)",
            {"batch_size": 500, "slots": 30, "per_vm_sec": 8, "visibility_sec": 960},
            _check_batch_fits_one_round,
        ),
        (
            "执行段超预算",
            {"batch_size": 60, "slots": 30, "per_vm_sec": 8, "visibility_sec": 960},
            _check_exec_budget,
        ),
        (
            "visibility 短于执行段+60",
            {"batch_size": 30, "slots": 30, "per_vm_sec": 8, "visibility_sec": 150},
            _check_exec_budget,
        ),
        (
            "攒批窗口被抬大",
            {"batching_window_sec": 20},
            _check_batching_window,
        ),
        (
            "ESM 没开 ReportBatchItemFailures",
            {"report_batch_item_failures": False},
            _check_report_batch_item_failures,
        ),
        (
            "没配 DLQ",
            {"redrive": {}},
            _check_dlq,
        ),
        (
            "maxReceiveCount 过大",
            {"redrive": {"deadLetterTargetArn": "arn:x", "maxReceiveCount": 99}},
            _check_dlq,
        ),
    ]
    bad = 0
    for name, broken, chk in cases:
        got = chk(broken, "selfcheck")
        if not got:
            print(f"  ✗ {name}: 改坏了却没报 —— 这条约束是摆设")
            bad += 1
        else:
            print(f"  ✓ {name}: 报出 {len(got)} 条")

    # ── #564 G8 新增两条的反向验证 ─────────────────────────────────────────
    # 它们的签名与上面那批不同(一个接 config dict、一个读 env),所以单独跑,
    # 不硬塞进 cases 的 (dict, fn) 形态。
    _full_dl = {a: cdl._DEFAULT_DEADLINE_SEC[a] for a in cdl.DEADLINE_ACTIONS}
    # 值本身写坏 → 必须报。#630 起 CDK 会把 config 里的值注入 env,所以这几种坏值不再是
    # 「纸面配置」而是能把 Lambda 打成导入期 raise / 静默换数字的真实输入。
    for name, broken_cfg in (
        (
            "config 的死线不是整数(suspend='abc')",
            {"lifecycle": {"deadline_sec": dict(_full_dl, suspend="abc")}},
        ),
        (
            "config 的死线是负数(suspend=-1)",
            {"lifecycle": {"deadline_sec": dict(_full_dl, suspend=-1)}},
        ),
        (
            "config 的死线是 0(suspend=0)",
            {"lifecycle": {"deadline_sec": dict(_full_dl, suspend=0)}},
        ),
        (
            "config 的死线是小数(suspend=180.5,会被静默截断成 180)",
            {"lifecycle": {"deadline_sec": dict(_full_dl, suspend=180.5)}},
        ),
        (
            "config 的死线是 bool(suspend=True,int(True)==1)",
            {"lifecycle": {"deadline_sec": dict(_full_dl, suspend=True)}},
        ),
    ):
        got = _check_deadline_config_parity(broken_cfg)
        if not got:
            print(f"  ✗ {name}: 改坏了却没报 —— 这条约束是摆设")
            bad += 1
        else:
            print(f"  ✓ {name}: 报出 {len(got)} 条")

    # 正向三种都不该报:八档齐全、整段缺、只写一档。
    # 后两种是 #630 修完之后的正确判词 —— CDK 会用权威默认补齐并建齐 SSM 参数,
    # 判成违反等于让 mechanical-gate 拒绝一份 CDK 已经处理好的 config(而且理由是假的)。
    for name, ok_cfg in (
        ("八档齐全的 config", {"lifecycle": {"deadline_sec": _full_dl}}),
        ("整段缺 lifecycle.deadline_sec(CDK 补齐八档)", {}),
        (
            "只写了 create 的 config(其余七档 CDK 补默认)",
            {"lifecycle": {"deadline_sec": {"create": 180}}},
        ),
    ):
        if _check_deadline_config_parity(ok_cfg):
            print(f"  ✗ {name} 被误报 —— #630 之后这不是违反")
            bad += 1
        else:
            print(f"  ✓ {name} 不报(不是恒红)")

    # per-action 死线下界:把 create 调到 100(< 最坏执行 128)必须报。
    # 这条改的是 env 而不是 dict —— 因为下界约束的输入本来就是 env(客户改的那个地方)。
    _saved_env = os.environ.get("LIFECYCLE_DEADLINE_SEC_CREATE")
    os.environ["LIFECYCLE_DEADLINE_SEC_CREATE"] = "100"
    try:
        got = _check_per_action_deadline_floor()
        if not got:
            print(
                "  ✗ create 死线调到 100s(< 最坏执行 128s)却没报 —— "
                "唯一一条防往小调的约束是摆设,而往小调的后果是孤儿 VM"
            )
            bad += 1
        else:
            print(f"  ✓ create 死线往小调破下界: 报出 {len(got)} 条")
        # 正向:往大调(300s)是安全方向,不该报。
        os.environ["LIFECYCLE_DEADLINE_SEC_CREATE"] = "300"
        if _check_per_action_deadline_floor():
            print("  ✗ create 死线往大调(300s)被误报 —— 往大是安全方向")
            bad += 1
        else:
            print("  ✓ create 死线往大调不报(方向正确)")
    finally:
        if _saved_env is None:
            os.environ.pop("LIFECYCLE_DEADLINE_SEC_CREATE", None)
        else:
            os.environ["LIFECYCLE_DEADLINE_SEC_CREATE"] = _saved_env

    # 正向:达标配置必须【全绿】。只有反向会红不够 —— 一个恒红的校验器同样没用。
    ok_v = {
        "batch_size": 30, "slots": 30, "per_vm_sec": 8, "visibility_sec": 960,
        "batching_window_sec": cdl.BATCH_WINDOW_SEC,
        "report_batch_item_failures": True,
        "redrive": {"deadLetterTargetArn": "arn:x", "maxReceiveCount": 3},
    }
    for chk in _LIVE_CHECKS:
        got = chk(ok_v, "selfcheck-ok")
        if got:
            print(f"  ✗ 达标配置被误报: {got}")
            bad += 1
    if not bad:
        print("  ✓ 达标配置全绿(校验器不是恒红)")

    print(f"\n{'✓ selfcheck 全过' if not bad else f'✗ selfcheck 有 {bad} 条问题'}")
    return 1 if bad else 0


def main() -> int:
    global _REGION  # #564 G8 —— `--region` 覆盖默认区域(声明必须先于任何读取)
    ap = argparse.ArgumentParser(
        description="#562 G13 创建死线配置基线校验(#564 G8 扩到七档 per-action 死线)"
    )
    ap.add_argument("--live", action="store_true", help="同时查线上 ESM/SQS/Lambda 真值")
    ap.add_argument("--selfcheck", action="store_true", help="反向验证本校验器")
    ap.add_argument("--config", type=Path, default=None, help="指定 config.yml(测试用)")
    ap.add_argument(
        "--region",
        default=_REGION,
        help=f"--live 查哪个区域(默认 {_REGION});G8 要求发布前对【目标环境】跑一次",
    )
    args = ap.parse_args()

    # #564 G8 —— 区域可覆盖。写死之后 `--live` 只能查一个区域,在别的环境上跑等于拿
    # 另一个环境的真值判当前环境达标 —— 那是个看起来像结论的假象。
    _REGION = args.region

    if args.selfcheck:
        return _selfcheck()

    problems, observed = run(args.live, args.config)
    b = cdl.budget_breakdown()
    print(
        f"死线口径: 总 {b['total_sec']}s = 攒批 {b['batch_window_sec']}"
        f" + 排队 {b['queue_budget_sec']} + 执行 {b['exec_budget_sec']}"
    )
    for scope, vals in observed.items():
        print(f"[{scope}] {json.dumps(vals, ensure_ascii=False, sort_keys=True)}")
    if not args.live:
        print(
            "注意:未加 --live,只核了仓内意图。部署漂移(线上被手改 / 改了没部署)"
            "查不出来 —— #458 刚证过 S3 上的脚本比仓里少 87 行。发布前必须跑一次 --live。"
        )
    if problems:
        print(f"\n✗ {len(problems)} 条不达标:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\n✓ 创建死线配置基线全部达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
