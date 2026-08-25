# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""deadline_config — 死线值的**运行时载体**:SSM Parameter Store。

## 为什么需要它(#564 G5)

客户明文要求「改 Lambda env 即可修改每个 lifecycle 配置」,而**真机实测证明那做不到**:
流量走 `live` 别名 → 已发布版本,而**已发布版本的 env 是冻结的**(实测:改 `$LATEST` 的
`DEFAULT_NO_JWT_ROLE`,等 75s,请求仍按旧版本的值判权)。所以「改 env」这条路是一个
**看不见的失败** —— 运维以为改了,线上跑的是另一个数。

|              | 客户手改后        | 下次 cdk deploy |
| ------------ | ---------------- | --------------- |
| Lambda env   | **完全不生效**    | 覆盖回 config    |
| SSM Parameter| **立即生效**      | 仍覆盖 → 靠漂移复检兜 |

仓内已有一个**逐字同款的先例**:`dispatch_infra.py` 的 `/openclaw/dispatch/config`(andon 急停)
——CDK 建参数并给默认值(防首启读空被憋死),运维用 `aws ssm put-parameter --overwrite`
直接改、不等 stack update。它的理由("急停不该等部署")与本模块的("改死线不该等部署")同源。

## 为什么单独一层,不写进 create_deadline.py

`create_deadline.py` 是死线**口径**的单一真相(权威默认值、下界校验、七档子集),而 G1 要求
它保持**纯函数 + 零 boto3** —— 它的消费者里有 **CDK synth 期**与 `scripts/checks/*`,那两处
碰不了 boto3。所以分层:

    core/deadline_config.py  ← 本模块。有 boto3,读 SSM,带进程内缓存
    core/create_deadline.py  ← 不变。权威默认值 + 值校验(parse_deadline_sec)+ 七档子集

**值的解析与校验只有一份**,在 `create_deadline.parse_deadline_sec()` —— env 与 SSM 两个载体
共用它,否则会出现「env 路径拒了、SSM 路径放行」的静默分叉(#430 那条教训:同一公式散在
8 处 = 改一处漏七处)。

## 配额算术(必须写下来,别把这一步变成第二个节流源)

`#573` 刚为「SendCommand 被节流毒 DLQ」打过补丁,同一类事故不该由这一步再引入一次。
**AWS 官方文档(Managing Parameter Store throughput)**:`GetParameter` / `GetParameters` /
`GetParametersByPath` 的默认配额是 **40 TPS,三个 API 共享**(higher throughput 下
by-path 只到 100 TPS,而 GetParameter 能到 10,000 —— 这个差别在下面的选型里有用)。

本模块的稳态压力:

    缓存 TTL 60s,只在冷启动或过期时读一次
    api reserved concurrency 上界按 50 个执行环境算
    → 50 环境 × (60s/60s) = 50 次/分钟 ≈ **0.8 TPS**

同池里已有的邻居:`dispatch_service._check_andon()` 免缓存单读,**约 0.5 TPS**(它是急停开关,
必须每次读最新,所以刻意不缓存)。两者合计 **≈1.3 TPS « 40 TPS**,余量约 30 倍。

**选 `GetParametersByPath` 而不是 7 次 `GetParameter`**:一次调用把七档全取回来 = **1 个
transaction 而不是 7 个**,配额压力直接省 7 倍(逐个读会是 5.6 TPS,余量掉到 7 倍)。
仓内先例:`dispatch_poller.py` 已经这么用。

**去掉缓存会立刻破这个算术** —— 那时每次请求读一次参数,20 TPS 压测下直接撞 40 TPS 池。
所以下面有一条 `assert_cache_is_sane()` 把「缓存必须存在且 TTL > 0」钉住。
"""

from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from core import clients  # 运行时取 clients.ssm,不 from-import(测试要能注入 mock)
import core.create_deadline as create_deadline

PARAM_PREFIX = create_deadline.PARAM_PREFIX
"""前缀与参数名的**权威在 `create_deadline`**(它零 boto3,CDK synth 期能 import)。

这里只做个别名,方便本模块内部书写 —— 不要在这里另定一份字符串:`lambdas.py` 建参数时读的是
`create_deadline.param_name_for()`,两处各写一份就会出现「CDK 建了 A、运行时读 B」的静默失效。
"""

CACHE_TTL_SEC = 60
"""进程内缓存的有效期。

60s 的取法:客户要的是「改完不用重新部署就生效」,不是「毫秒级生效」——一分钟内生效完全
满足那个诉求,而它把配额压力压到 0.8 TPS(见模块 docstring 的算术)。
**这个值不能设成 0 或负数**:那等于取消缓存,会把每次请求都变成一次 SSM 读。
"""

_MAX_PAGES = 5
"""翻页上限。`MaxResults=10` × 5 页 = 50 个参数,而这个前缀下应该只有七档。

**为什么必须有上限**:翻页由响应里的 `NextToken` 驱动,而那是**外部输入**。`while True` +
「token 为真就继续」在 CI 上实撞过一次 OOM(EXIT 137):既有测试把 `core.clients` 整体
换成 `MagicMock` 时,`resp.get("NextToken")` 返回的是 Mock 而不是 `None` —— **Mock 恒为真**,
于是无限翻页,而 Mock 又把每一次调用记进 `mock_calls`,内存无界增长直到被 SIGKILL。
所以下面同时收两条:token 必须是**非空 str**(修的就是这个真值判定),再加这个页数上限
兜住"服务端真的一直给 token"的情形。
"""

_cache: Dict[str, int] = {}
_cache_at: Optional[float] = None
"""上次**成功读到**的时刻(`time.monotonic()`);`None` = 从未成功读过。

**新鲜度判据只能看这个,不能看 `bool(_cache)`**(Codex 独立复审抓出的真缺口)。差别在一个
**默认就会发生**的场景:参数一个都没建时(config 里没写 `lifecycle.deadline_sec`,CDK 就不建),
一次成功的读返回的是**空字典** —— 拿 `bool(_cache)` 当"读过了"就永远为假,于是每次调用都回源,
而 `all_effective_deadline_sec()` 会遍历七档 → **一个请求 7 次 SSM 调用**。那让模块 docstring
里那套 0.8 TPS 的算术彻底失效,也就等于把同池的 andon 急停一起拖进节流。
"成功读到但结果为空"是一个**有效**的缓存状态,必须被记住。
"""


param_name_for = create_deadline.param_name_for
"""参数名的权威在 `create_deadline`(见上面 `PARAM_PREFIX` 的说明)。这里只再导出一次,
让本模块的调用方不必知道它在哪一层 —— 但**实现只有一份**。"""


def assert_cache_is_sane() -> None:
    """缓存必须存在且 TTL > 0 —— 去掉它就会把每次请求变成一次 SSM 读。

    为什么值得一条断言:模块 docstring 里那套配额算术**整个建立在「有缓存」这个前提上**。
    有人为了"让改配置立刻生效"把 TTL 调成 0,读起来像个无害的调参,实际是把稳态从
    0.8 TPS 抬到与请求量同阶 —— 20 TPS 压测就撞满 40 TPS 的共享池,而**同池里还有 andon
    急停**,那条一被节流就等于急停失灵。所以这条约束要能被机械检出,而不是只写在注释里。
    """
    if not isinstance(CACHE_TTL_SEC, int) or CACHE_TTL_SEC <= 0:
        raise ValueError(
            f"CACHE_TTL_SEC={CACHE_TTL_SEC!r} 必须是正整数 —— "
            "取消缓存会让每次请求读一次 SSM,把稳态从 0.8 TPS 抬到与请求量同阶,"
            "撞满与 andon 急停共享的 40 TPS 池"
        )


def _fetch_all() -> Dict[str, int]:
    """`GetParametersByPath` 取回七档,**翻完所有页**。只解析、不吞非法值。

    非法值(非整数/非正/低于该操作的最坏执行下界)由下面两步 raise —— 那是刻意的:
    配置手误必须炸,静默回落默认会让改配置的人以为生效了(G5 的核心要求)。

    **必须翻页**(Codex 独立复审指出):`GetParametersByPath` 是分页 API。当前七档 <
    `MaxResults`,所以今天不会分页;但一旦前缀下多出参数(加第八档、或有人建了个同前缀的),
    只取第一页就会**静默漏读**其中几档 → 那几档悄悄回落 env/默认,而运维在参数里明明改了。
    "静默"正是这个模块存在要消灭的东西,所以不靠"反正现在装得下"。
    """
    out: Dict[str, int] = {}
    token = None
    for _ in range(_MAX_PAGES):
        kwargs = {"Path": PARAM_PREFIX, "Recursive": False, "MaxResults": 10}
        if token:
            kwargs["NextToken"] = token
        resp = clients.ssm.get_parameters_by_path(**kwargs)
        for p in resp.get("Parameters") or []:
            name = str(p.get("Name") or "")
            if not name.startswith(PARAM_PREFIX):
                continue
            action = name[len(PARAM_PREFIX):]
            if action not in create_deadline.DEADLINE_ACTIONS:
                # 前缀下有个不认识的参数:不猜、不炸,跳过并留给 G8 校验器报告。
                # 炸的话一个手误建错名的参数就能让整条热路径 500。
                continue
            out[action] = _parse_and_check_lower_bound(action, p.get("Value"))
        token = resp.get("NextToken")
        # 必须是**非空字符串**才继续翻。只判真值不够 —— 见 `_MAX_PAGES` 的注释:
        # 测试里 `clients` 被整体 MagicMock 掉时 `resp.get()` 返回 Mock 而非 None,
        # Mock 恒为真会把这里变成无限循环(CI 实撞 OOM / EXIT 137)。
        if not isinstance(token, str) or not token:
            return out
    print(
        f"deadline_config: {PARAM_PREFIX} 下的参数超过 {_MAX_PAGES} 页(应只有七档),"
        f"只读了前 {_MAX_PAGES} 页;没读到的档位会回落 env/默认,"
        "在 GET /system/info 里显示为 env-or-default"
    )
    return out


def _parse_and_check_lower_bound(action: str, raw) -> int:
    """解析 + **下界校验**。后者是 env 路径没有的一步,但 SSM 路径必须做。

    为什么(Codex 独立复审抓出的真缺口):`create_deadline.parse_deadline_sec()` 只保证
    正整数,而「死线不得小于该操作的单次最坏执行」是另一条不变量 —— env 路径靠
    `assert_deadline_config_sane()` 在**导入期**跑一次兜住,而 **SSM 的值是运行时才来的,
    导入期那次校验碰不到它**。缺了这一步,`put-parameter create=1` 会被照单接受,后果不是
    "变慢"而是 **判死之后 SSM 还在起 VM → 没人认的孤儿 VM(占容量且计费)** ——
    `create_deadline` 的模块注释把这条列为「唯一一个往小调必须被挡住」的约束。

    只有 create 有权威下界(128s,来自 `dispatch_service._derive_exec_timeout(30)` 的算术);
    另外六档的 `worst_exec_sec_for()` 返 None = 未落地,**跳过而不是当 0** —— 填一个算不出
    来源的数就等于把"未验证"伪装成"已验证",而这个数偏小的后果正是孤儿 VM。
    """
    val = create_deadline.parse_deadline_sec(action, raw, PARAM_PREFIX + action)
    worst = create_deadline.worst_exec_sec_for(action)
    if worst is not None and val < worst:
        raise ValueError(
            f"死线 {PARAM_PREFIX + action}={val} < 单次最坏执行 {worst}s —— "
            "判死之后 host 侧 SSM 还在跑,会留下没人认的 VM(占容量且计费)。"
            f"往大调是安全方向;要往小调必须先压低 {action} 的最坏执行"
        )
    return val


def effective_deadline_sec(action: str) -> Tuple[int, str]:
    """该操作**实际生效**的死线秒数 + 它的来源(给日志/证据自证用)。

    三级分流(比一刀切 fail-closed 更对,理由逐条写在下面):

    | 情形                     | 处置                  | 为什么 |
    | ------------------------ | -------------------- | ------ |
    | 缓存新鲜                  | 用缓存                | 这一步就是配额算术的前提 |
    | SSM 读到                  | 用它 + 刷缓存          | 客户改完一分钟内生效 |
    | 读到但**非法**            | **raise**            | 配置手误必须炸(G5) |
    | 读失败 + 有旧缓存(即使过期)| 用旧缓存              | 那是"上次读到的真值",比默认更接近现实 |
    | 读失败 + 无缓存            | 回落 env/默认 + 打日志 | 瞬时故障不该把一次合法请求变 500 |
    | 前缀下没有这一档           | 回落 env/默认          | 允许只托管部分档位,不强制七档齐全 |

    回落走的是 `create_deadline.deadline_sec_for()`,**不是**直接取 `_DEFAULT_DEADLINE_SEC`
    —— 那样 `LIFECYCLE_DEADLINE_SEC_*` env 仍作第二层兜底(G5 要求它保留一个周期),
    且 env 非法时照旧 raise。
    """
    global _cache_at
    key = str(action).strip().lower()
    param_name_for(key)  # 未知 action 在这里就 raise

    now = time.monotonic()
    # 判据是"上次成功读的时刻",**不是** `bool(_cache)` —— 见 `_cache_at` 的说明:
    # 参数一个都没建时一次成功的读返回空字典,那是有效的缓存状态,必须被记住。
    if _cache_at is not None and (now - _cache_at) < CACHE_TTL_SEC:
        if key in _cache:
            return _cache[key], "ssm"
        return create_deadline.deadline_sec_for(key), "env-or-default"

    try:
        fresh = _fetch_all()
    except ValueError:
        # 非法值:**不吞**。这一条与下面的 except 顺序有关系 —— ValueError 必须先被
        # 重新抛出,否则会被"读失败"那一支当成瞬时故障吞掉,配置手误就静默生效了。
        raise
    except Exception as e:  # noqa: BLE001 — SSM 瞬时故障不该把合法请求变 500
        # 同上:判"读过没"看 `_cache_at`,不看 `bool(_cache)`。
        if _cache_at is not None:
            print(
                f"deadline_config: SSM 读失败({type(e).__name__}),用上次读到的缓存 "
                f"(已过期 {int(now - _cache_at)}s)"
            )
            if key in _cache:
                return _cache[key], "ssm-stale"
            return create_deadline.deadline_sec_for(key), "env-or-default"
        print(
            f"deadline_config: SSM 读失败({type(e).__name__})且无缓存,"
            f"回落 env/默认。{key} 的生效值将来自 "
            f"{create_deadline.env_name_for(key)} 或代码默认"
        )
        return create_deadline.deadline_sec_for(key), "env-or-default"

    _cache.clear()
    _cache.update(fresh)
    _cache_at = now
    if key in _cache:
        return _cache[key], "ssm"
    return create_deadline.deadline_sec_for(key), "env-or-default"


def deadline_epoch_for(action: str, accepted_epoch: int) -> Tuple[int, str]:
    """从受理时刻算出该操作的**死线绝对时间戳** + 秒数的来源。#564 G2 的唯一生产入口。

    计时起点 = **API 受理时刻**,与 #562 一致:上游业务侧在死线之上各留 30s 缓冲
    (180 档→210s、600 档→630s),而缓冲只有在两边起点对齐时才是真缓冲。

    **为什么要有这个函数,而不是让四个调用点各写两行**:`accepted + effective_deadline_sec()`
    这个公式会出现在四处(create、delete、通用 lifecycle、rebuild 自调用),而 #430 的教训
    就是"同一公式散在 8 处 = 改一处漏七处"。更要紧的是它必须**明确压过**
    `create_deadline.deadline_at_for()` —— 那个是零 boto3 的 env/默认原语,在 G5 之后
    **不再是生产口径**(它看不见 SSM 参数)。生产路径用错那个,后果不是算错几秒,而是
    「运维改了参数、`/system/info` 报 source=ssm,而真实死线仍按 env 走」—— G5 要消灭的
    那个"看不见的失败"原封不动地回来,只是换了六个操作。

    返回绝对 epoch 而不是剩余秒数:理由见 `create_deadline` 模块 docstring —— 消息在队列里
    待多久不可知,剩余秒数一旦写进消息体就立刻开始说谎。
    """
    sec, source = effective_deadline_sec(action)
    return int(accepted_epoch) + sec, source


def all_effective_deadline_sec() -> Dict[str, Tuple[int, str]]:
    """七档的生效值与来源 —— 给 G8 校验器、真机证据、以及日志自证用。

    刻意返回 `(值, 来源)` 而不只是值:验收第 4 条要「读取值要在日志或响应里可验证」,
    而"这个 180 是来自 SSM 还是回落的默认"恰恰是那条验收要区分的东西。

    来源只有三个值,**刻意不区分"缓存命中"**:`ssm`(参数里的值,不管这次是否走了缓存)、
    `ssm-stale`(SSM 读失败,用的是上次读到的旧值)、`env-or-default`(参数里没有这一档,
    回落到 env 或代码默认)。"这次是不是缓存命中"对读它的人没有决策价值 —— 缓存里的值和
    刚读到的值一样可信;真正需要区分的是**"这是个过期的旧值"**。
    """
    return {a: effective_deadline_sec(a) for a in create_deadline.DEADLINE_ACTIONS}


def invalidate_cache() -> None:
    """丢掉缓存,下次读强制回源。给测试与运维排查用(不在热路径上调)。"""
    global _cache_at
    _cache.clear()
    _cache_at = None   # None = 从未成功读过(见 _cache_at 的说明)


def cache_state() -> Tuple[int, Optional[int]]:
    """(缓存里的档数, 缓存年龄秒;从未读过则 None)—— 排查用,不参与判定。"""
    if _cache_at is None:
        return len(_cache), None
    return len(_cache), int(time.monotonic() - _cache_at)


assert_cache_is_sane()
