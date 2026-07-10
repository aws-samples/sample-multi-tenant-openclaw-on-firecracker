# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""services/edge_admin — P4-③ (#187) 边缘管理只读 API。

两个端点(只读,operator+):

- ``list_edge_instances()`` — ``GET /admin/edge/instances``:列 EdgeASG(P2b-iac
  合入,`auto_scaling_group_name="openclaw-edge-asg"`)实例 + ALB target health。
- ``list_edge_metrics()`` — ``GET /admin/edge/metrics``:P4-③ 阶段返聚合骨架 +
  ``metrics_source="stub"``;真 Prometheus 指标待 P6 观测栈接线后从 CloudWatch/AMP
  取(见 progress/p4-edge-console.md §八风险 1)。**不造假数据。**

契约:04-API-SPEC §六;拆解:progress/p4-edge-console.md。
"""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from core.clients import asg_client, elbv2
from core.utils import _now, _err, _resp

EDGE_ASG_NAME = "openclaw-edge-asg"


def _describe_asg():
    """返回 EdgeASG 描述,不存在则抛 EdgeStackNotDeployed。"""
    out = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[EDGE_ASG_NAME])
    groups = out.get("AutoScalingGroups") or []
    if not groups:
        raise EdgeStackNotDeployed(EDGE_ASG_NAME)
    return groups[0]


def _describe_target_health(asg_desc=None):
    """返回 {instance_id: (state, reason)},elbv2 权限缺时返 None(P5 后补 IAM)。

    TG 经 ASG 的 TargetGroupARNs 反查(P7 起 TG 不再显式命名,CFN 自动名,
    换端口 replacement 才不撞 AlreadyExists)。"""
    try:
        _asg = asg_desc if asg_desc is not None else _describe_asg()
        tg_arns = _asg.get("TargetGroupARNs") or []
        if not tg_arns:
            return None
        tg_arn = tg_arns[0]
        th_out = elbv2.describe_target_health(TargetGroupArn=tg_arn)
        by_id = {}
        for entry in th_out.get("TargetHealthDescriptions") or []:
            target = entry.get("Target") or {}
            iid = target.get("Id", "")
            health = entry.get("TargetHealth") or {}
            by_id[iid] = (health.get("State", ""), health.get("Reason", ""))
        return by_id
    except ClientError as exc:
        code = (exc.response.get("Error") or {}).get("Code", "")
        # P5 后追加 IAM 前会 AccessDenied;target group 不存在 → TargetGroupNotFound。
        # 两种情况下 target_health 字段降级为 null(前端标注 "unknown, P5 后接")。
        if code in ("AccessDenied", "AccessDeniedException", "TargetGroupNotFound"):
            return None
        raise


def _describe_private_ips(instance_ids):
    """batch-lookup instance_id → private_ip + launched_at,ec2 权限现有。"""
    if not instance_ids:
        return {}
    ec2 = boto3.client("ec2")
    resp = ec2.describe_instances(InstanceIds=list(instance_ids))
    out = {}
    for r in resp.get("Reservations") or []:
        for i in r.get("Instances") or []:
            iid = i.get("InstanceId", "")
            launch = i.get("LaunchTime")
            out[iid] = {
                "private_ip": i.get("PrivateIpAddress", ""),
                "launched_at": launch.isoformat() if launch else "",
            }
    return out


class EdgeStackNotDeployed(Exception):
    """EdgeASG 未创建(edge.enabled=false 或 stack 未部署 P2b-iac)。"""


def list_edge_instances():
    """``GET /admin/edge/instances`` — 只读列表,operator+。"""
    try:
        group = _describe_asg()
    except EdgeStackNotDeployed:
        return _err(
            500,
            "EDGE_STACK_NOT_DEPLOYED",
            "EdgeASG 'openclaw-edge-asg' not found (edge.enabled=false or stack 未部署)",
        )
    except ClientError as exc:
        code = (exc.response.get("Error") or {}).get("Code", "")
        if code in ("ValidationError", "AutoScalingGroupNotFound"):
            return _err(500, "EDGE_STACK_NOT_DEPLOYED", "EdgeASG not found")
        raise

    asg_instances = group.get("Instances") or []
    instance_ids = [
        i.get("InstanceId", "") for i in asg_instances if i.get("InstanceId")
    ]
    ip_map = _describe_private_ips(instance_ids)
    health_map = _describe_target_health(asg_desc=group)

    notes = []
    if health_map is None:
        notes.append(
            "target_health unavailable (P5 后追加 elbv2:DescribeTarget* IAM 到 api Lambda)"
        )

    items = []
    for i in asg_instances:
        iid = i.get("InstanceId", "")
        ip_info = ip_map.get(iid, {})
        th = (health_map or {}).get(iid)
        items.append(
            {
                "instance_id": iid,
                "availability_zone": i.get("AvailabilityZone", ""),
                "lifecycle_state": i.get("LifecycleState", ""),
                "health_status": i.get("HealthStatus", ""),
                "target_health": th[0] if th else None,
                "target_health_reason": th[1] if th else "",
                "private_ip": ip_info.get("private_ip", ""),
                "launched_at": ip_info.get("launched_at", ""),
            }
        )

    return _resp(
        200,
        {
            "asg_name": EDGE_ASG_NAME,
            "asg": {
                "min_size": group.get("MinSize", 0),
                "max_size": group.get("MaxSize", 0),
                "desired_capacity": group.get("DesiredCapacity", 0),
            },
            "instances": items,
            "generated_at": _now(),
            "notes": notes,
        },
    )


def list_edge_metrics():
    """``GET /admin/edge/metrics`` — P4-③ 阶段返聚合骨架 stub。

    真 Prometheus 指标从 edge OpenResty ``/metrics`` 端点(deploy/edge/nginx.conf
    :164)采集,现阶段该端点只吐 ``edge_up 1`` 占位。P6 观测阶段接线 CloudWatch
    GetMetricData 或 AMP scrape 后本函数改从那读真值;api Lambda 现在不在 VPC 内,
    直接拉 edge 私网 IP 走不通(见 progress/p4-edge-console.md §八风险 1)。
    """
    # 先复用 list_edge_instances 的数据面(ASG + IP 列表),失败直接透传。
    inner = list_edge_instances()
    if inner.get("statusCode") != 200:
        return inner
    import json

    inst_data = json.loads(inner["body"])

    instances = []
    for i in inst_data["instances"]:
        instances.append(
            {
                "instance_id": i["instance_id"],
                "private_ip": i["private_ip"],
                "reachable": None,  # 未接线,P6 后改布尔
                "metrics": None,
            }
        )

    return _resp(
        200,
        {
            "generated_at": _now(),
            "metrics_source": "stub",
            "instances": instances,
            "notes": [
                "P4-③ 阶段 metrics 未接线;edge /metrics 端点(nginx.conf:164)仅吐 edge_up 1 占位。",
                "P6 观测栈接线后本端点改走 CloudWatch GetMetricData / AMP,填 l1/l2/l3/cache_hit_rate/failstatic_active/upstream_latency 等真指标。",
                "字段返 null 而非 0,避免 UI 误显示为已上报的 0 值。",
            ],
        },
    )
