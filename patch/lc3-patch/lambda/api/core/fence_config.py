# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""fence_config — 生命周期租约秒数的**运行时载体**:SSM Parameter Store(#680)。

## 为什么不能是 Lambda env

与 `deadline_config` 同一条真机实测:流量走 `openclaw-api:live` 别名 → **已发布版本**,
而已发布版本的 env 是**冻结快照**;`update-function-configuration` 只改 `$LATEST`,
而 `$LATEST` 不服务流量。所以「改 env 把租约从 1800 调到 120」是一个**看不见的失败**
——运维以为改了,线上跑的是另一个数。

|               | 客户手改后     | 下次 cdk deploy |
| ------------- | ------------- | --------------- |
| Lambda env    | **完全不生效** | 覆盖回 config   |
| SSM Parameter | **立即生效**   | 仍覆盖 → 靠漂移复检兜 |

死线七档(`deadline_config`)与 andon 急停(`dispatch_infra` 的 `/openclaw/dispatch/config`)
都已经走这条路,本模块是第三个同款。

## 为什么是**独立**参数 + 独立缓存,不并进死线那次 `GetParametersByPath`

初版设计想「合并进同一次调用,事务数不增加」。放弃了,因为要做到那件事必须把
`deadline_config._fetch_all()` 的 `Path` 从 `/openclaw/lifecycle/deadline-sec/` 抬到
`/openclaw/lifecycle/` 并打开 `Recursive` —— 那是**改在役热路径的读形状**,而收益只是省
一次调用。配额算术不支持这个交换:

    本模块稳态 = 50 个执行环境 × (60s / CACHE_TTL_SEC 60s) ≈ 0.8 TPS
    deadline_config 同款             ≈ 0.8 TPS
    dispatch_service._check_andon()  ≈ 0.5 TPS(急停必须读最新,刻意不缓存)
    合计 ≈ 2.1 TPS  «  40 TPS(GetParameter/GetParameters/GetParametersByPath 共享配额)

余量约 19 倍。**用 19 倍余量换"不动在役读形状"是正确方向** —— `deadline_config` 的模块
注释已经把「去掉缓存会立刻破掉这套算术」写成硬约束,本模块沿用同一条断言
(`assert_cache_is_sane`)。

## 下界:为什么是 `exec_sec` 而不是实测 lease_hold

租约必须装得下**一次操作合法在飞的最长时间**,否则会出现「执行中途租约过期 →
`host_guard` 判 epoch/owner 不符 → `exit 79` 把自己的破坏性命令串斩断」。对 restart 而言
那意味着 `stop-vm` 已跑、`launch-vm` 被拦,VM 停着而 `status` 还是 `running`;对 delete 更糟
—— 后置 guard 夹在 `delete-vm.sh` **之后**,VM 与数据盘已经删掉才 `exit 79`,租户卡在
`deleting` 且资源不可恢复。

「最长时间」的权威值是 `create_deadline.exec_sec(action)`(各步 `_ssm_run(timeout=…)` 的墙钟
额度之和),不是实测 lease_hold。下界因此取
`max(exec_sec)` over fenced 动作 = **210s**(`delete`),见
`create_deadline.FENCE_LEASE_MIN_SEC`。

`ap-southeast-1` 实测(2026-08-28,#680)只用来**佐证预算没被穿透**,不用来定下界:

| 场景                                      | lease_hold | 该档 exec_sec |
| ----------------------------------------- | ---------- | ------------- |
| `suspend`,空盘 230MB(n=26 全表历史)      | p50 18.44s / max 19.31s | 140 |
| `suspend`,6.3GB 不可压缩数据(两次连测)    | 46.53s / 45.76s | 140 |
| `delete`,`keep_data=true` 软删             | 7.18s | 210 |
| `delete`,`keep_data=false` 硬删 + 6GB 数据 | **69.55s** | 210 |

最后一行是门 5 补测的那格(预备份 27.0s + 删前静止盘备份 36s,两次 6GB)。它同时推翻了初版
的两个说法:实测上界不是 46.53s,而初版下界 60 连这一格都装不下。

**下界的用途是拒绝往下调,不是推荐值。** #680 之后客户带同一 `client_token` 重试立刻拿
`202 + retry_after_sec`,不必等租约过期,所以「配短点让客户早点重做」这个动机基本消失;这个
参数存在的意义是**往上调**(超大盘租户的备份可能吃满 90s 预算)。
"""

from __future__ import annotations

import os
import time
from typing import Optional, Tuple

from core import clients  # 运行时取 clients.ssm,不 from-import(测试要能注入 mock)
import core.create_deadline as create_deadline

PARAM_NAME = create_deadline.FENCE_LEASE_PARAM_NAME
"""参数全名的**权威在 `create_deadline`**(它零 boto3,CDK synth 期能 import)。

这里只做别名。不在这里另写一份字符串:`lambdas.py` 建参数时读的是那一份,两处各写一份
就会出现「CDK 建了 A、运行时读 B」——参数建好了没人读,而读的那个永远
`ParameterNotFound` 一路回落默认,日志上完全看不出来。
"""

ENV_NAME = "LIFECYCLE_FENCE_LEASE_SECONDS"
"""第二层兜底。保留它是为了不破坏既有部署(以及单测/本地),但它**不是**生效途径 ——
见模块 docstring 那张表。"""

DEFAULT_LEASE_SECONDS = create_deadline.FENCE_LEASE_DEFAULT_SEC
"""代码默认(权威定义在 `create_deadline`,那层零 boto3 所以 CDK synth 期也能用)。
#680 前是 1800,即实测 lease_hold 上界(46.53s)的 38.7 倍。

1800 的后果不是"锁得久一点":客户带 requestId 重试时会连吃 409 直到租约自然过期,
而客户方的容忍是 120s。降到 120 之后,「什么时候能安全重做」由租约过期自己回答 ——
过期后的 `acquire` 走新 owner 接管分支并把 `lifecycle_fence_epoch` +1,旧命令的
`host_guard` 随即判 epoch 不符而 `exit 79` 自杀。这正是 EC2 Zonal Host Service 的
saga 设计里那条论证:「永不过期的 lease 加上会崩的 worker = 永久死锁;而仅靠过期也不
安全 —— stalled worker 可能过期后醒来覆盖 corrector,所以需要 fencing epoch」。
"""

MIN_LEASE_SECONDS = create_deadline.FENCE_LEASE_MIN_SEC
"""硬下界(权威定义同上在 `create_deadline`)。低于它一律拒绝并回落。
见模块 docstring 最后一节的实测依据。"""

CACHE_TTL_SEC = 60
"""进程内缓存有效期。取 60 的理由与 `deadline_config.CACHE_TTL_SEC` 相同:客户要的是
「改完不用重新部署就生效」而不是毫秒级生效,而它把稳态压到 0.8 TPS。
**不能设成 0 或负数** —— 那等于取消缓存。"""

_cached: Optional[int] = None
_cached_source: str = ""
_cache_at: Optional[float] = None
"""上次**成功读到**的时刻(`time.monotonic()`);`None` = 从未成功读过。

新鲜度只看这个,不看 `bool(_cached)` —— 与 `deadline_config._cache_at` 同一条教训:
参数没建时一次成功的读结果是"没有这个参数",那是**有效**的缓存状态,必须被记住,
否则每次调用都回源,把上面那套配额算术作废。
"""


def assert_cache_is_sane() -> None:
    """缓存必须存在且 TTL > 0。与 `deadline_config` 同款断言,同一条理由。

    有人为了"让改配置立刻生效"把 TTL 调成 0,读起来像无害调参,实际是把稳态从 0.8 TPS
    抬到与请求量同阶 —— 而同池里还有 andon 急停,那条一被节流就等于急停失灵。
    所以这条约束要能被机械检出,而不是只写在注释里。
    """
    if not isinstance(CACHE_TTL_SEC, int) or CACHE_TTL_SEC <= 0:
        raise ValueError(
            f"CACHE_TTL_SEC={CACHE_TTL_SEC!r} 必须是正整数 —— "
            "取消缓存会让每次请求读一次 SSM,撞满与 andon 急停共享的 40 TPS 池"
        )


def parse_lease_seconds(raw, source: str) -> int:
    """解析 + 下界校验。**唯一**的判定点,env 与 SSM 两个载体共用。

    与 `deadline_config._parse_and_check_lower_bound` 同一分工理由:两个载体各写一份
    校验就会出现「env 路径拒了、SSM 路径放行」的静默分叉(#430 那条「同一公式散在 8 处」
    的教训)。

    非法值一律 raise,由调用方决定是回落还是上抛 —— 本模块不吞:一个手误
    (`put-parameter --value 5`)静默生效的后果是**每次 restart 都在中途被自己的租约
    斩断**,而那种失败看起来像 host 故障,查起来极贵。
    """
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"{source} 的取值 {raw!r} 不是整数;生命周期租约秒数必须是正整数"
        )
    if val < MIN_LEASE_SECONDS:
        raise ValueError(
            f"{source}={val} 低于硬下界 {MIN_LEASE_SECONDS}s"
            f"(= delete 的执行段预算 exec_sec,八个 fenced 动作里最长的那档) —— "
            "配到下界以下会让操作在执行中途被自己的租约过期斩断"
            "(host_guard 判 epoch 不符 → exit 79,stop 完 launch 被拦;"
            "delete 更糟:VM 与数据盘已删,后置 guard 才 exit 79,租户卡在 deleting)"
        )
    return val


def _env_or_default() -> Tuple[int, str]:
    """env 兜底。env 非法时**照旧 raise**(与 SSM 路径同一条判定)。"""
    raw = os.environ.get(ENV_NAME)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LEASE_SECONDS, "default"
    return parse_lease_seconds(raw, ENV_NAME), "env"


def _fetch() -> Tuple[int, str]:
    """`GetParameter` 单读。参数不存在 → 回落 env/默认,不是错误。

    用 `GetParameter` 而不是 `GetParametersByPath`:只有一个参数,by-path 在这里没有
    任何优势,而它多一层"前缀下可能有别的东西"的解析分支(`deadline_config` 为此写了
    整页注释)。
    """
    resp = clients.ssm.get_parameter(Name=PARAM_NAME)
    raw = ((resp or {}).get("Parameter") or {}).get("Value")
    # **先判类型再判值。** 响应是外部输入,而下面 `parse_lease_seconds` 的 `ValueError`
    # 有一个刻意的语义:「配置手误,必须炸」。只有在**确实读到了一个标量**的前提下,那个
    # 语义才成立。
    #
    # 具体踩到的形态(#680 实测,45 个既有测试同时红):仓内大量单测把 `core.clients` 整体
    # 换成 `MagicMock`,于是 `get_parameter(...)` 返回 Mock、层层 `.get()` 也返回 Mock ——
    # `int(str(Mock))` 失败 → `ValueError` → 被当成配置手误上抛 → **每一个碰到租约的测试
    # 都变成 500**。`deadline_config` 侥幸躲过是因为它用 `GetParametersByPath`,Mock 让
    # `for p in Mock` 抛 `TypeError`(不是 ValueError),正好落进它的宽 except 回落分支。
    # 那是巧合而不是设计,所以这里显式判。
    #
    # 抛 `TypeError` 而不是 `ValueError`:调用方按异常类型分流 —— ValueError = 配置手误
    # (上抛),其它 = 读不到(回落默认)。`bool` 要排除,它是 `int` 的子类而 `True` 会被
    # 解析成 1,那是个荒谬的租约值。
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise TypeError(
            f"{PARAM_NAME} 的响应值不是标量(got {type(raw).__name__}) —— "
            "按「读不到」处理并回落默认,而不是按「配错了」上抛"
        )
    return parse_lease_seconds(raw, PARAM_NAME), "ssm"


def effective_lease_seconds() -> Tuple[int, str]:
    """**实际生效**的租约秒数 + 来源(给日志与真机证据自证用)。

    分流与 `deadline_config.effective_deadline_sec` 逐条对齐:

    | 情形                        | 处置                   | 为什么 |
    | --------------------------- | ---------------------- | ------ |
    | 缓存新鲜                     | 用缓存                 | 配额算术的前提 |
    | SSM 读到                     | 用它 + 刷缓存           | 改完一分钟内生效 |
    | 读到但**非法**               | **raise**              | 配置手误必须炸,不能静默 |
    | 参数不存在                   | 回落 env/默认           | 允许不托管这个参数 |
    | 读失败 + 有旧缓存(即使过期)  | 用旧缓存               | 那是上次读到的真值,比默认更接近现实 |
    | 读失败 + 无缓存              | 回落 env/默认 + 打日志   | 瞬时故障不该把合法请求变 500 |
    """
    global _cached, _cached_source, _cache_at
    now = time.monotonic()
    if _cache_at is not None and (now - _cache_at) < CACHE_TTL_SEC:
        if _cached is not None:
            return _cached, _cached_source
        return _env_or_default()

    try:
        val, source = _fetch()
    except ValueError:
        # 非法值**不吞**。这一条必须排在下面那个宽 except 之前,否则配置手误会被当成
        # 瞬时故障吞掉、静默生效 —— 那正是本模块要消灭的东西。
        raise
    except Exception as e:  # noqa: BLE001 — SSM 瞬时故障/参数不存在不该把请求变 500
        name = type(e).__name__
        if name == "ParameterNotFound" or "ParameterNotFound" in str(e):
            # 没托管这个参数是**正常配置**,不是故障:记成"读过了、结果是没有",
            # 这样不会每次请求都回源(见 `_cache_at` 的说明)。
            _cached, _cached_source, _cache_at = None, "", now
            return _env_or_default()
        if _cache_at is not None and _cached is not None:
            print(
                f"fence_config: SSM 读失败({name}),用上次读到的缓存 "
                f"(已过期 {int(now - _cache_at)}s)"
            )
            return _cached, "ssm-stale"
        print(
            f"fence_config: SSM 读失败({name})且无缓存,回落 env/默认;"
            f"生效值将来自 {ENV_NAME} 或代码默认 {DEFAULT_LEASE_SECONDS}"
        )
        return _env_or_default()

    _cached, _cached_source, _cache_at = val, source, now
    return val, source


def invalidate_cache() -> None:
    """丢掉缓存,下次读强制回源。给测试与运维排查用(不在热路径上调)。"""
    global _cached, _cached_source, _cache_at
    _cached, _cached_source, _cache_at = None, "", None


def cache_state() -> Tuple[Optional[int], str, Optional[int]]:
    """(缓存值, 来源, 缓存年龄秒;从未读过则 None)—— 排查用,不参与判定。"""
    if _cache_at is None:
        return _cached, _cached_source, None
    return _cached, _cached_source, int(time.monotonic() - _cache_at)


assert_cache_is_sane()
