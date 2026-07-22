# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/skills — 每租户/每组 skill 分发解析(handler-split #132 Phase1 T1.4)。

从 handler.py 机械搬迁,函数体逐字不变:_resolve_effective_skills。
共享 groups_table 从 core.clients import。按 design.md 层间契约:core 域只依赖
core.clients/core.utils。facade:handler.py re-export,旧 patch/调用路径全程有效。
"""

from core.clients import groups_table

def _resolve_effective_skills(tenant_item):
    """Compute the set of skill names a tenant should receive at launch time.

    1.4.0 (#62): per-tenant / per-group skill distribution.

    Returns:
        None  → tenant has no per-tenant or group scope set; launch-vm.sh
                 falls back to broadcast (legacy v1.3.x behavior).
        list  → sorted unique list of skill names from
                 (tenant.skills) ∪ (groups[tenant.group].skills).
                 If the union is empty, also returns None to avoid lock-out
                 (an explicit empty-list scope would otherwise prevent any
                 skill from reaching the VM, which is rarely intended).

    Unknown groups are silently dropped from the union — the operator gets
    a warning at /groups admin time, not at every launch.
    """
    if not tenant_item:
        return None
    tenant_skills = tenant_item.get("skills") or []
    group_name = (tenant_item.get("group") or "").strip()
    # No scoping configured → broadcast.
    if not tenant_skills and not group_name:
        return None

    effective = set(s for s in tenant_skills if s)
    if group_name and groups_table is not None:
        try:
            grp = groups_table.get_item(Key={"name": group_name}).get("Item") or {}
            for s in grp.get("skills") or []:
                if s:
                    effective.add(s)
        except Exception:
            # Group lookup failure is non-fatal — proceed with tenant.skills only.
            pass

    return sorted(effective) if effective else None
