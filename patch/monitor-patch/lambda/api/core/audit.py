# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/audit — 租户生命周期事件发布(SNS)。

handler-split #132 —— 从 handler.py 逐字搬迁,函数体零改动。
依赖方向:core/audit → core.clients(sns/topic arn) + core.utils(_now),
不反向 import handler,不横向 import 其它 core 域(clients/utils 是叶子层,允许)。
facade:handler.py re-export `_publish_event`,旧调用/patch 路径全程有效。

sns 句柄和 NOTIFICATIONS_TOPIC_ARN 均走属性访问 `clients.X`(不用 from-import):
test_notifications 用 `api.sns = MagicMock()` 重绑 sns、`api.NOTIFICATIONS_TOPIC_ARN
= ""` 重绑 topic 注入 fixture,值绑定会让本模块持有原始对象、看不到重绑
(scheduling 域验证过的跨模块串染死结)。属性访问下测试重绑 `clients.X` 即全局生效。

注:ADR 把 `_audit_write` 也划归本域,但它调 `_get_caller_identity`(core.auth),
是 core 横向依赖、被 import-layers 门禁止。该依赖的裁决(router 传入 identity /
门加 audit→auth 白名单)是未决项,故本轮只搬无横向依赖的 `_publish_event`,
`_audit_write` 暂留 handler,待评审拍板依赖方向后再搬。
"""

import json

import core.clients as clients
from core.utils import _now


def _publish_event(event_name, tenant_id, details):
    """Publish a tenant lifecycle event to SNS. No-op when topic not set.

    Best-effort: SNS publish failures are logged but do not break the
    underlying API operation.
    """
    if not clients.NOTIFICATIONS_TOPIC_ARN:
        return
    try:
        msg = {
            "event": event_name,
            "tenant_id": tenant_id,
            "timestamp": _now(),
            "details": details or {},
        }
        clients.sns.publish(
            TopicArn=clients.NOTIFICATIONS_TOPIC_ARN,
            Subject=f"OpenClaw: {event_name} ({tenant_id})",
            Message=json.dumps(msg, default=str),
            MessageAttributes={
                "event": {"DataType": "String", "StringValue": event_name},
                "tenant_id": {"DataType": "String", "StringValue": tenant_id},
            },
        )
    except Exception as e:
        print(f"SNS publish failed (operation succeeded): {e}")
