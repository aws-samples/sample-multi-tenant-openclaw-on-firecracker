# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""host_taint — host cordon 标记的纯判定与公开视图，零 boto3。

#539:污点与多方可写的生命周期 status 正交，只表达 NoSchedule 运维意图。字段名与
判定集中在这里，避免写 API、列表响应和后续 #540 调度排除各自解释一套脏值语义。
"""

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


def public_view(host) -> dict:
    """返回可合并进 host API 响应的规范化污点视图；tainted_by 只留审计侧。"""
    view = {ATTR_IS_TAINTED: is_tainted(host)}
    if not view[ATTR_IS_TAINTED]:
        return view
    for attr in (ATTR_TAINTED_AT, ATTR_TAINTED_REASON):
        if attr in host:
            view[attr] = host[attr]
    return view
