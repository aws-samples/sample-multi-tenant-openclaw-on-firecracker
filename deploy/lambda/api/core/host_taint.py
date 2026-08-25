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

# #540 —— 放置侧 CAS 的原子门(治选点与认领之间的 TOCTOU:运维正好在这个窗口标记)。
#
# 为什么【不是】裸的 attribute_not_exists(is_tainted):那会与本模块 is_tainted() 的
# fail-open 语义相反,两边口径不一致就制造"选得中、订不上"——
#   · is_tainted() 只认真实 DDB BOOL true,脏值(例如被非法写入的字符串 "false")一律
#     判为【未标污点】,理由见该函数 docstring:污点是放置策略,不是跨租户安全边界,
#     宁可不误杀整台机器;
#   · 而 attribute_not_exists 只问"属性在不在":脏值时属性【存在】→ 条件失败 → CAS 拒。
# 于是同一台脏值 host 会被 _find_host / binpack 选中、却被 CAS 一直拒绝,每次 create
# 白烧一次重试直到耗尽预算。本仓对这个形状已有明确告诫(tenant_service.py 里
# "必须与 scheduling._find_host 同口径…会让『选得中、订不上』")。
#
# 所以条件表达式镜像 is_tainted() 的判据:属性不存在 或 值不等于 BOOL true → 放行。
# DDB 的 <> 跨类型比较视为不相等,故字符串 "false" 这类脏值也会放行,与读侧一致。
#
# 片段由 ATTR_IS_TAINTED 拼出而不是在四处 CAS 各硬写一遍:属性名一旦改名,硬写的字符串
# 会【静默失去保护】(条件恒真),而这里会跟着改。
NOT_TAINTED_VALUE_KEY = ":oc_taint_true"
NOT_TAINTED_CONDITION = (
    f"(attribute_not_exists({ATTR_IS_TAINTED}) "
    f"OR {ATTR_IS_TAINTED} <> {NOT_TAINTED_VALUE_KEY})"
)
#: 上面条件用到的表达式值。调用方 update(...) 进自己的 ExpressionAttributeValues。
NOT_TAINTED_VALUES = {NOT_TAINTED_VALUE_KEY: True}


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
