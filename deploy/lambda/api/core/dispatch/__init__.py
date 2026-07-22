# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/dispatch — SQS dispatch 装箱+manifest 纯函数域。

依赖单向:consumers → services → core.dispatch(叶子)。本包只准 import stdlib,
零 boto3。所有 AWS I/O 归 services/dispatch_service.py 编排。

契约:SPEC/specs/sqs-dispatch/interfaces.md。
"""

from .binpack import PackResult, normalize_spec, pack
from .manifest import (
    MANIFEST_PART_MAX_BYTES,
    decode_manifest_lines,
    encode_manifest_line,
    split_manifest_parts,
)

__all__ = [
    "PackResult",
    "pack",
    "normalize_spec",
    "MANIFEST_PART_MAX_BYTES",
    "encode_manifest_line",
    "split_manifest_parts",
    "decode_manifest_lines",
]
