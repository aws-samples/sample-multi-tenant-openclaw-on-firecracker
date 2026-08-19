# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#534 — snapshot_time → 版本 label 反解。

背景:`rootfs_version` / `immutable_version` 被重载 —— host 侧存版本 label(`v1.x`,来自
manifest.version),但钉版/canary 租户侧的 `_stamp_rootfs` 把【采用的 snapshot_time】(ISO
串)直接写了进去,导致同字段两种取值域(#534)。本模块把 snapshot_time 反解回它建快照时记的
label(`openclaw-version-snapshots.label`),让 `rootfs_version`/`immutable_version` 统一成
version label 坐标。

设计口径(#534 决定 A1):label 是建快照时运营可控的自由文本、可重复,直接采用不做规范化。
fail-safe:查不到 / 表未配置 / 出错 / 已经是 label(非 snapshot_time 键)→ 原样返回入参
(绝不丢信息、绝不谎报)。故对"已是 label"的输入是幂等透传。

注意:精确快照身份仍活在 `image_snapshot_time` / `image_slots` / host `snapshot_time`(launch /
pull-image / rebuild / reclaim / delete-snapshot 的操作真值)——本模块只碰展示/drift/查询坐标,
不碰那些。
"""


def label_for_snapshot(snapshot_time):
    """把一个 snapshot_time 反解成它的版本 label;非 snapshot_time(已是 label)/查不到 → 原样返回。

    version-snapshots 表按 snapshot_time 为主键。一个已经是 label 的入参(如 `v1.2`)不是该表的
    键 → get_item 落空 → 原样透传(幂等)。表未配置 / 读失败同样透传(fail-safe,不谎报)。
    """
    if not snapshot_time or not isinstance(snapshot_time, str):
        return snapshot_time
    # 惰性 import 避免 core.clients ↔ services 的潜在环;core.clients 只依赖 boto3。
    from core.clients import version_snapshots_table
    if version_snapshots_table is None:
        return snapshot_time
    try:
        item = version_snapshots_table.get_item(
            Key={"snapshot_time": snapshot_time}
        ).get("Item")
    except Exception:
        return snapshot_time
    if not item:
        return snapshot_time
    label = item.get("label")
    # ≤256B 守卫:与 q_rootfs_version 的 GSI 键上限一致,超长 label 不用(退回原值)。
    if isinstance(label, str) and label and len(label.encode("utf-8")) <= 256:
        return label
    return snapshot_time
