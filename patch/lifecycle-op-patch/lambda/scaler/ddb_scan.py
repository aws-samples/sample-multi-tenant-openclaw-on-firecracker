# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ddb_scan — DynamoDB Scan 必须翻页。这个模块的存在就是为了让「忘记翻页」不再可能。


DDB 单次 `Scan` 最多返回 **1 MB**,并且 `FilterExpression` 是在那 1 MB 读【之后】才过滤。
所以「匹配集合很小」完全不代表「一页就够」—— 决定页数的是**全表字节数**,不是命中数。

`openclaw-hosts` 实测(2026-08-21,apse1):**39 行 / 15,752 字节 = 403 字节/行**,
其中 **33 行是 `deleted`**(在役只有 5 台)。每台 terminate 的机器都留一行,所以死行只增不减。
按 403 字节/行推,1 MB ≈ **2,601 行**;若死行按当前比例(87%)累积,到那时在役才约 333 台 ——
**远早于**「上千台大机队」。也就是说这不是远期容量问题,是近期正确性问题。

不翻页的后果是**静默看错**,不是报错:
  · 选点/装箱看不见后页的 host → 明明有容量却报 unplaced → 租户拿到「容量不足」而机器空着;
  · AZ 故障判定看错机队 → 误触发全量迁移,或漏掉真故障;
  · 疏散/清理漏掉后页的租户 → 租户被静默留在已终止的机器上。

本仓早就写下过这条纪律 —— `core/scheduling.py::_registered_host_count` 的注释原话:
「a DynamoDB Scan returns at most 1MB per call, and openclaw-hosts accumulates `deleted`
rows over its lifetime … pagination is required even though the match set is small」。
但 22 个 scan 调用点里有 10 个没照做(#432 审计)。文档拦不住,所以抽成函数 + 加机械门。

## 为什么返回 list 而不是生成器

生成器看着更省内存,但会埋一个**静默**的坑:调用方写 `if not hosts:` 时,生成器**恒为真**,
于是「没有可用 host」这条分支再也进不去(`host_service.refresh_rootfs` 就有这种写法)。
返回 list 是这 10 个调用点的原地替换,不改变任何真值语义。要流式处理再另加 `iter_all`。

## 为什么每一页都要重传 kwargs

`FilterExpression` / `ProjectionExpression` / `ConsistentRead` **不会**被 DDB 记住,
第二页不传就变成「无过滤全量返回」。手写循环最容易漏的就是这个 —— 本函数用 `**kwargs`
统一透传,从根上消掉这一类。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 单次调用的页数上限。存在的理由不是性能,是**防呆**:真实 DDB 终会返回空
# LastEvaluatedKey,但测试里的 mock 常常恒返同一个 key(本仓 #522 的注释记过这个坑),
# 那样就是死循环。到上限仍未扫完是异常情况,必须 fail-loud 而不是静默截断 ——
# 静默截断正是本模块要消灭的那个失效模式。
MAX_PAGES = 10_000


class ScanPagesExceeded(RuntimeError):
    """翻页数超过 MAX_PAGES。宁可炸也不静默返回不完整结果(见 MAX_PAGES 的说明)。"""


def scan_all(table, **kwargs) -> List[Dict[str, Any]]:
    """把一次逻辑 Scan 翻完所有页,返回全部 Items。

    `kwargs` 逐页原样透传(`FilterExpression` / `ExpressionAttributeNames` /
    `ExpressionAttributeValues` / `ProjectionExpression` / `ConsistentRead` …)。
    调用方【不要】自己传 `ExclusiveStartKey`,那是本函数管的。

    注意 `Limit` 的语义:DDB 的 `Limit` 限的是【每页】读取的条数,不是总数。传了它仍会翻页,
    只是每页更小。想要「只取前 N 个」请自己在结果上切片,或者别用这个函数。
    """
    if "ExclusiveStartKey" in kwargs:
        raise ValueError(
            "scan_all 自己管翻页,不要传 ExclusiveStartKey"
            "(传了会从中途开始扫,前面的行静默丢失 —— 正是本函数要防的那类错)"
        )
    items: List[Dict[str, Any]] = []
    start_key = None
    for _ in range(MAX_PAGES):
        call = dict(kwargs)
        if start_key is not None:
            call["ExclusiveStartKey"] = start_key
        page = table.scan(**call)
        items.extend(page.get("Items", []))
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return items
    raise ScanPagesExceeded(
        f"Scan 翻页超过 {MAX_PAGES} 页仍未结束(已取 {len(items)} 行)。"
        "真实 DDB 不会这样;通常是测试 mock 恒返同一个 LastEvaluatedKey。"
        "宁可在这里炸,也不返回一个不完整的结果集。"
    )
