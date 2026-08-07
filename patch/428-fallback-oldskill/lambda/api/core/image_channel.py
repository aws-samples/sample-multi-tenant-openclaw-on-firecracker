# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#394 step4 — create-tenant 的 image_channel 准入(ADR §4.3)。

契约:`POST /tenants` 新增可选 `image_channel: "live"|"canary"`(缺省 live,向后兼容)。
canary 场景另需 `preferred_host_id` + 调用方从 pull Job result 读到的
`expected_image_snapshot_time` / `expected_image_generation`。

本模块只做一件事:**把 channel 解析成一个具体的不可变版本,并在准入时校验它没被换掉**,
然后由调用方把解析结果 `image_snapshot_time` 固定到租户记录。为什么必须固定具体版本而不是
只存 channel:只存 channel 的话,promote 清空 canary 指针后该租户 restart 会解析不到(或
解析到别的候选版本)= 版本漂移,验证结论作废(ADR §10 拒绝项 1)。

canary 准入【不回落 live】:调用方明确要求候选版本,拿不到就该失败,静默给 live 等于
让人以为验证了新版(ADR §4.3 末句)。
"""

from core import image_slots

# 缺省 channel:不传 = live,保持既有 create-tenant 行为字节不变。
DEFAULT_CHANNEL = image_slots.SLOT_LIVE


def normalize_channel(raw):
    """请求里的 image_channel → 规范值。返回 (channel, err_msg)。

    空/缺省 → live。非法值 → 报错(绝不猜成 live:猜错会让调用方以为在验证候选版本)。
    """
    value = (raw or "").strip() or DEFAULT_CHANNEL
    if not image_slots.is_valid_slot(value):
        return None, (
            f"image_channel must be 'live' or 'canary'; got {value!r}"
        )
    return value, None


def resolve_pinned_version(channel, host_slots, expected_snapshot_time=None,
                           expected_generation=None):
    """把 channel 解析成要固定到租户上的具体版本。返回 (snapshot_time, err_code, err_msg)。

    · live:返回 None —— live 租户【不】固定版本,每次启动解析当前 live 指针(保持既有
      产品语义:运行中不受指针变化影响,restart 时拿当前 live)。
    · canary:必须解析出具体版本,且:
        - host 上确实有 canary(没有 → CANARY_NOT_READY,不回落 live);
        - 调用方给了 expected 时必须一致(TOCTOU:期间被并发 pull 换掉 → CANARY_CHANGED);
        - expected_generation 给了也要一致(同一 snapshot 名下槽位又被动过 → 也算变了)。
    `expected_generation` 只在准入时用;创建成功后 host generation 继续变化不影响该租户
    (它已固定到具体版本目录,ADR §4.3)。
    """
    if channel == image_slots.SLOT_LIVE:
        return None, None, None
    canary = (host_slots or {}).get("canary")
    if not canary:
        return None, "CANARY_NOT_READY", (
            "target host has no READY canary slot; pull the candidate image with "
            "slot=canary first (never falls back to live)"
        )
    if expected_snapshot_time and canary != expected_snapshot_time:
        return None, "CANARY_CHANGED", (
            f"host canary is {canary!r}, not the expected {expected_snapshot_time!r} — "
            f"the candidate was replaced; re-read and retry"
        )
    if expected_generation is not None:
        try:
            want = int(expected_generation)
        except (TypeError, ValueError):
            return None, "VALIDATION", "expected_image_generation must be an integer"
        current = int((host_slots or {}).get("generation") or 0)
        if current != want:
            return None, "CANARY_CHANGED", (
                f"host slot generation is {current}, not the expected {want} — "
                f"the slots changed since you read them; re-read and retry"
            )
    return canary, None, None


def requires_pinned_host(channel):
    """canary 租户必须显式 pin 到 host(canary 槽只存在于运维装过的那台,ADR §4.3)。"""
    return channel == image_slots.SLOT_CANARY
