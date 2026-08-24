# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""poller_heartbeat — #432:让「poller 没在跑」这件事**能被发现**。

## 为什么不是「加第二个定时器」

issue 把 poller 写成「单点定时器,挂了或超时会停摆」。但 `EventBridge rate(1 minute)`
**不是一个会崩的单实例** —— 它是 AWS 托管的调度器,自身就是 HA。逐条对真实失效模式:

| 失效模式 | 加第二个定时器能治吗 | 真正的治法 |
| --- | --- | --- |
| 规则被误禁用 / 目标权限断 | ❌ 第二条规则会被同一次操作或 IaC 变更一起禁掉 | **能发现它没跑** |
| Lambda 每一拍都报错(静默) | ❌ 第二个定时器调的是同一个坏函数 | 错误告警 + **陈旧告警** |
| Lambda 被限流 / 并发耗尽 | ❌ 同一个函数,第二个触发一样被限流 | 保留并发 + 告警 |
| 单次调用处理不完 | ⚠️ 只治一半 | 分批 + 上限(#562 已有 `MAX_ENFORCE_PER_RUN`)+ 告警 |
| EventBridge 区域性故障 | ✅ 但必须是**另一个服务**的触发源 | 冗余触发源(需先证幂等) |

**五分之四的失效模式,靠加定时器都治不了;而它们共同的前提是"没人知道它没跑"。**
今天 poller 一个指标都不发 —— 也就是说无论上面哪种发生,都要等到有人发现租户卡住才知道。
所以这个模块做的是最缺的那件事:**每拍发一个心跳,让"没跑"可被告警**。

## 为什么这件事在 #562 之后变得更重

#562 把死线执行者挂在了同一个 poller 上。在那之前,ddb 模式下 `poll_inflight` 第一行就空转
返回,poller 停摆几乎无害。现在它是**死线承诺的唯一兜底**:poller 停 10 分钟,那 10 分钟里
所有过死线的租户都留在 `creating`/`pending` —— 「180s 内必进终态」这个对外承诺**静默失效**,
而客户看到的仍然只是「还在创建中」。这正是本 issue 要消灭的那个态,只是成因换成了 poller 本身。

## 指标口径

- `PollerHeartbeat`(Count,值恒为 1)—— 每次成功跑完发一次。**告警看的是它"缺席"**
  (`treat_missing_data=BREACHING`),不是它的数值。这一点是刻意的:数值告警在"函数根本没被
  调用"时永远不会触发,因为那时连数据点都没有。
- `PollerRunSeconds`(Seconds)—— 单次耗时。它不是用来看性能的,是用来发现「快撞满 1 分钟
  节拍」的:一旦单次耗时接近 60s,下一拍就会与上一拍重叠,那时幂等性才真正被考验。
- `PollerErrors`(Count)—— 本次里被 fail-safe 吞掉、没有向上抛的错误数。#562 的死线执行者
  刻意不让单个租户的失败中断整轮(`errors` 计数),那些错误在日志里但没人看 —— 发成指标才有人看。

## 幂等:为什么双触发是安全的(冗余触发源的前置条件)

如果将来要加冗余触发源,前提是同一拍跑两次不产生第二次副作用。现状逐条:

1. **死线判死**(`deadline_executor._fence_failed`)—— 条件写双锚
   `status = :expected AND create_deadline = :dl`。第二次跑时该行已是 `failed`,
   条件不成立 → CCF → 计为 `raced` 而**不是** error,不重复判死、不重复计数。
2. **判死触发扩容**(`_scale_out_for_deaths`)—— 一轮只做一次决策,且 `_scale_out()` 自身按
   `desired - registered` 判在途容量(#341):第一拍抬了 desired 之后,第二拍看到 `in_flight > 0`
   就跳过,不叠加。
3. **promote / 回滚**(`poll_inflight`)—— 条件写锁 `status = creating AND dispatch_claim = :cid`,
   第二次跑同样 CCF no-op。ddb 模式下它本就空转返回。

结论:**三条副作用路径都是条件写 + 幂等锚**,双触发安全。本模块自己只发指标,无副作用。
`tests/test_432_poller_heartbeat_adversarial.py` 里有一条用例真的把整轮跑两遍,断言第二遍
不产生额外的判死写入 —— 那是这段论证的可执行版本,不是口头承诺。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import core.clients as clients

NAMESPACE = "OpenClaw/Dispatch"
METRIC_HEARTBEAT = "PollerHeartbeat"
METRIC_RUN_SECONDS = "PollerRunSeconds"
METRIC_ERRORS = "PollerErrors"


def _cw():
    """CloudWatch 客户端走 clients 属性访问(与 dispatch_service._cw 同款,便于测试重绑)。

    **可能是 None**:`core/clients.py` 里写的是
    `cloudwatch = boto3.client("cloudwatch") if DISPATCH_QUEUE_URL else None` ——
    即 dispatch 没配队列时它压根没建。调用方必须显式判 None,不能靠 except 兜:
    靠 except 兜会把「本来就没有 dispatch,无需心跳」和「CloudWatch 真的调不通」
    混成同一条日志,而这两者的处置完全相反(前者不该管,后者要查)。
    """
    return clients.cloudwatch


def emit(run_seconds: float, errors: int = 0) -> Dict[str, Any]:
    """发一次 poller 心跳。返回是否发成功(供调用方放进返回值,便于人工排查)。

    **发送失败绝不向上抛。** 理由:心跳是可观测性,不是业务。让它的失败阻断 poller
    等于「为了知道有没有跑,反而让它跑不了」—— 那比没有心跳更糟。失败时打日志并在返回值里
    标出来;真正兜底的是陈旧告警本身(发不出去 = 数据点缺席 = 告警会响),
    也就是说这条路径的失败**不会**变成静默失效。
    """
    out: Dict[str, Any] = {"emitted": False}
    cw = _cw()
    if cw is None:
        # dispatch 未配置(DISPATCH_QUEUE_URL 空)→ 根本没有 poller 要监控。
        # 这不是失败,所以【不打 error 也不算进错误数】,只如实标出 skipped 的原因 ——
        # 否则运维会去查一个不存在的问题。
        out["skipped"] = "no cloudwatch client (DISPATCH_QUEUE_URL unset)"
        return out
    try:
        cw.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[
                {"MetricName": METRIC_HEARTBEAT, "Value": 1.0, "Unit": "Count"},
                {
                    "MetricName": METRIC_RUN_SECONDS,
                    "Value": float(run_seconds),
                    "Unit": "Seconds",
                },
                {
                    "MetricName": METRIC_ERRORS,
                    "Value": float(int(errors)),
                    "Unit": "Count",
                },
            ],
        )
        out["emitted"] = True
    except Exception as e:  # noqa: BLE001 —— 见 docstring:可观测性失败不许阻断业务
        msg = f"{type(e).__name__}: {e}"
        print(f"[#432] poller heartbeat emit failed (non-fatal): {msg}")
        out["error"] = msg
    return out


def errors_in(poll_stats: Optional[Dict[str, Any]],
              deadline_stats: Optional[Dict[str, Any]]) -> int:
    """从两半的统计里数出「被 fail-safe 吞掉、没有向上抛」的错误数。

    为什么要专门数它:#562 的死线执行者刻意让单个租户的写失败不中断整轮(计进 `errors`),
    `poll_inflight` 也有同类处置。那些错误只在日志里,而**没有人在看日志**。
    发成指标之后,「poller 每拍都跑但每拍都在吞错」这种状态才有可能被发现 ——
    否则它长得和「一切正常」完全一样(心跳照发、返回值形状正常)。

    `deadline_stats` 里那个顶层 `error` 键是 handler 在死线执行者整体抛异常时塞的
    (见 handler 的 poller 分支),它也要算一次。
    """
    n = 0
    for stats in (poll_stats, deadline_stats):
        if not isinstance(stats, dict):
            continue
        v = stats.get("errors")
        if isinstance(v, (int, float)):
            n += int(v)
        if stats.get("error"):  # 顶层整体失败
            n += 1
    return n


class timer:  # noqa: N801 —— 刻意小写:当上下文管理器用,读起来像 `with timer() as t:`
    """量一次 poller 的墙钟耗时。

    不用 `time.time()` 而用 `time.monotonic()`:前者会被 NTP 校时拨动,拨一下就可能量出
    负数或跳变的耗时,而这个数是要拿去和 60s 节拍比的 —— 量错了就会误判「有没有重叠」。
    """

    def __init__(self) -> None:
        self.seconds = 0.0
        self._t0 = 0.0

    def __enter__(self) -> "timer":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *_exc) -> bool:
        self.seconds = time.monotonic() - self._t0
        return False  # 不吞异常
