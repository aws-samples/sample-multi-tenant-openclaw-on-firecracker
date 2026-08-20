# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""host_taint — host cordon 标记的纯判定与公开视图，零 boto3。

（`decimal` 是标准库；DDB resource 层把 N 型读成 `Decimal`，本模块要在序列化前收敛它。）

#539:污点与多方可写的生命周期 status 正交，只表达 NoSchedule 运维意图。字段名与
判定集中在这里，避免写 API、列表响应和后续 #540 调度排除各自解释一套脏值语义。
"""

from decimal import Decimal

ATTR_IS_TAINTED = "is_tainted"
ATTR_TAINTED_AT = "tainted_at"
ATTR_TAINTED_REASON = "tainted_reason"
ATTR_TAINTED_BY = "tainted_by"

TAINT_ATTRS = (
    ATTR_IS_TAINTED,
    ATTR_TAINTED_AT,
    ATTR_TAINTED_REASON,
    ATTR_TAINTED_BY,
)


def is_tainted(host) -> bool:
    """仅真实 DDB BOOL true 表示污点，其余输入全部 fail-open。

    必须用 ``is True``，不能用 ``bool(...)``：字符串 ``"false"`` 也是真值，若把非法
    写入的脏值当污点，会误挡整台机器。污点只由标记 API 写、正常值必为 DDB BOOL；
    遇到非法外部写入时选择不误杀机队，与本仓 disk_ok/mem_ok 缺信号 fail-open 一致。
    污点是放置策略，不是跨租户安全边界。
    """
    return isinstance(host, dict) and host.get(ATTR_IS_TAINTED) is True


def _epoch_or_none(value):
    """#546 —— 把 DDB 读回的 tainted_at 收敛成 JSON number,拿不准就返 None(省略该键)。

    只接受【非 bool 的整数】与【整值 Decimal】。刻意不用裸 int():
    int() 会把 Decimal("...9") 静默截断、把数字字符串和 True 也照收 —— 那等于拿一个【看似合法
    但编造】的审计时间戳回给调用方(True → 1 → 1970-01-01),比"没有这个字段"更糟:前者会被当成
    真实标记时刻写进运维记录,后者只是缺失、可查可补。审计字段宁缺毋伪。
    bool 要单独挡:它是 int 子类,isinstance(True, int) 为真。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        # 非有限(NaN/Inf)或带小数部分 → 视为 schema 损坏,不猜。
        return int(value) if value.is_finite() and value == value.to_integral_value() else None
    return None


def public_view(host) -> dict:
    """返回可合并进 host API 响应的规范化污点视图；tainted_by 只留审计侧。"""
    view = {ATTR_IS_TAINTED: is_tainted(host)}
    if not view[ATTR_IS_TAINTED]:
        return view
    stamp = _epoch_or_none(host.get(ATTR_TAINTED_AT))
    if stamp is not None:
        view[ATTR_TAINTED_AT] = stamp
    if ATTR_TAINTED_REASON in host:
        view[ATTR_TAINTED_REASON] = host[ATTR_TAINTED_REASON]
    return view
