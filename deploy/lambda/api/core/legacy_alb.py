# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/legacy_alb — 旧架构 per-tenant ALB target-group/rule 管理(handler-split #132 T1.6)。

从 handler.py 机械搬迁,函数体逐字不变:_get_listener_arn / _ensure_host_tg /
_add_alb_rule / _add_alb_rule_impl / _repoint_alb_rule_to_tg / _remove_alb_rule /
_remove_host_tg。含 #0-B 保留的 _repoint_alb_rule_to_tg(当前无调用点但逐字保留,不删)。
共享 elbv2 client + ALB_LISTENER_ARN/ENABLE_PER_TENANT_ALB_RULE/VPC_ID 从 core.clients import。
按 design.md 层间契约:core 域只依赖 core.clients/core.utils。facade:handler.py re-export。
"""

import random
import time

from botocore.exceptions import ClientError

from core.clients import (
    ALB_LISTENER_ARN,
    ENABLE_PER_TENANT_ALB_RULE,
    VPC_ID,
    elbv2,
)

def _get_listener_arn():
    """Get ALB listener ARN for path-based routing rules."""
    return ALB_LISTENER_ARN

def _ensure_host_tg(instance_id, private_ip):
    """Create or return target group ARN for a host."""
    tg_name = f"oc-{instance_id[-8:]}"
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        return resp["TargetGroups"][0]["TargetGroupArn"]
    except Exception:
        pass
    resp = elbv2.create_target_group(
        Name=tg_name,
        Protocol="HTTP",
        Port=80,
        VpcId=VPC_ID,
        TargetType="ip",
        HealthCheckPath="/health",
        HealthCheckIntervalSeconds=10,
        HealthyThresholdCount=2,
    )
    tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
    elbv2.register_targets(
        TargetGroupArn=tg_arn, Targets=[{"Id": private_ip, "Port": 80}]
    )
    return tg_arn

def _add_alb_rule(tenant_id, tg_arn):
    """Add ALB listener rule for /vm/{tenant_id}*.

    旧架构遗留 + 默认禁用:C 端现走 channel→WS hub,不需要 per-tenant ALB rule。
    ALB listener rule 硬上限(默认 100)在 ~100 租户撞 TooManyRules 致 create 雪崩
    (2026-06-29 压测实锤)。默认 ENABLE_PER_TENANT_ALB_RULE=false 直接跳过——
    既解除容量被 ALB rule 上限卡死的炸弹,又保留老式 /vm 直连部署显式开的能力。

    Concurrency-safe priority allocation. The old code read the in-use
    priorities once and picked the lowest free slot; when several Lambdas"""
    if not ENABLE_PER_TENANT_ALB_RULE:
        return  # channel 架构默认路径:不加 per-tenant ALB rule(见上方开关注释)
    _add_alb_rule_impl(tenant_id, tg_arn)

def _add_alb_rule_impl(tenant_id, tg_arn):
    """实际加 ALB rule(仅 ENABLE_PER_TENANT_ALB_RULE=true 时走到)。
    ran at the same time they all computed the SAME priority and all but one
    got `PriorityInUseException` (500). We now (a) pick a RANDOM free slot to
    cut collision odds and (b) retry on PriorityInUse by re-reading the live
    rule set, so concurrent creates converge instead of failing.
    """
    arn = _get_listener_arn()
    if not arn:
        return

    last_err = None
    for attempt in range(10):
        rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
        # Idempotent: rule for this tenant already exists.
        if any(
            f"/vm/{tenant_id}" in v
            for r in rules
            for c in r.get("Conditions", [])
            for v in c.get("Values", [])
        ):
            return
        used = {int(r["Priority"]) for r in rules if r["Priority"] != "default"}
        free = [i for i in range(1, 1000) if i not in used]
        if not free:
            raise RuntimeError("ALB listener has no free rule priority (limit reached)")
        # Random free slot — two concurrent callers are unlikely to collide.
        priority = random.choice(free[: max(50, len(free) // 2)])
        try:
            elbv2.create_rule(
                ListenerArn=arn,
                Priority=priority,
                Conditions=[
                    {
                        "Field": "path-pattern",
                        "Values": [f"/vm/{tenant_id}", f"/vm/{tenant_id}/*"],
                    }
                ],
                Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
            )
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            last_err = e
            # Lost the race for this priority — re-read and retry with another.
            if code in ("PriorityInUse", "PriorityInUseException"):
                time.sleep(0.1 * (attempt + 1))
                continue
            raise
    raise last_err

def _repoint_alb_rule_to_tg(tenant_id, tg_arn):
    """1.3.1: Repoint /vm/<tenant_id>* to a different target group.

    Used by migrate (cross-host live migration) and AZ failover. If no rule
    exists yet for this tenant, creates one. If one exists pointing at the
    old host's TG, modifies it in place to point at the new TG. Without
    this, traffic keeps hitting the dead/old host after a host change.
    """
    arn = _get_listener_arn()
    if not arn:
        return
    rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
    rule_arn = None
    for r in rules:
        for c in r.get("Conditions", []):
            if c.get("Field") == "path-pattern" and any(
                f"/vm/{tenant_id}" in v for v in c.get("Values", [])
            ):
                rule_arn = r["RuleArn"]
                break
        if rule_arn:
            break
    if rule_arn:
        elbv2.modify_rule(
            RuleArn=rule_arn,
            Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )
    else:
        # No existing rule — fall back to creating it.
        _add_alb_rule(tenant_id, tg_arn)

def _remove_alb_rule(tenant_id):
    """Remove ALB listener rule for a tenant."""
    arn = _get_listener_arn()
    if not arn:
        return
    rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
    for r in rules:
        for c in r.get("Conditions", []):
            if c.get("Field") == "path-pattern" and f"/vm/{tenant_id}" in c.get(
                "Values", []
            ):
                elbv2.delete_rule(RuleArn=r["RuleArn"])
                return

def _remove_host_tg(instance_id):
    """Delete target group for a host."""
    tg_name = f"oc-{instance_id[-8:]}"
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
        arn = _get_listener_arn()
        if arn:
            rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
            for r in rules:
                for a in r.get("Actions", []):
                    if a.get("TargetGroupArn") == tg_arn:
                        elbv2.delete_rule(RuleArn=r["RuleArn"])
        elbv2.delete_target_group(TargetGroupArn=tg_arn)
    except Exception:
        pass
