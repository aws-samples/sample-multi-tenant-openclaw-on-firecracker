# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""consumers — SQS event 入口薄壳层。

层间契约(import-layers 单向向下):
    consumers → services → core → core.clients + core.utils

本层只解析 event、路由到对应 service,不做业务逻辑、不直调 boto3。
所有 SQS batch item failure 报告在 service 里组装,consumer 只透传。
"""

from . import dispatch  # re-export

__all__ = ["dispatch"]
