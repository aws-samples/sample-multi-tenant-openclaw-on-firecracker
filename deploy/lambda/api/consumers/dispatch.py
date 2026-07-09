# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SQS dispatch consumer — SPEC/specs/sqs-dispatch/interfaces.md。

Lambda EventSourceMapping 触发本 handler,event = {"Records": [SQS record,...]}。
薄壳:签名/权限校验放 Lambda 层(execution role scoped to dispatch queue),我们
只把 Records 转给 services.dispatch_service.dispatch_batch。
"""

from __future__ import annotations

from typing import Any, Dict, List

from services.dispatch_service import dispatch_batch


def handle(event: Dict[str, Any]) -> Dict[str, Any]:
    """SQS consumer 入口。返回 batchItemFailures 报告,失败留队列退避重试。"""
    records: List[Dict[str, Any]] = event.get("Records") or []
    if not records:
        return {"batchItemFailures": []}
    return dispatch_batch(records)
