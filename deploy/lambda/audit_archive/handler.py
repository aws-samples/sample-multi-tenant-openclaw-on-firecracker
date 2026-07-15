# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#32 Audit archive Lambda — consumes DDB audit-table Stream, writes each row
to the WORM audit-archive S3 bucket at
    <prefix>/<actor_owner_id>/<yyyy>/<mm>/<dd>/<audit_row_id>.json

Design intent:
- 只归档 INSERT(NEW_IMAGE),忽略 REMOVE/MODIFY —— stack.py 的 event source filter
  已经在 Lambda 服务侧过滤,handler 内再兜底一次防止上游改配置漏拦。
- 幂等:S3 key 里带审计条目的 uuid4 id,同事件重放写同 key,配合 bucket versioning
  不会造成 lost update(WORM 桶版本化 + 新版本创建也在 Object Lock 保护下)。
- fail-loud:反序列化/写桶失败抛异常让 Lambda 服务重试→重试耗尽后进 DLQ,不静默吞
  (对齐铁律 #11 与 the ops guide "别静默吞异常")。
- unmarshal 不引外部依赖(zero deps):DDB Stream 的 NEW_IMAGE 是 attr:{type:val}
  形式,手写一个 stdlib-only 反序列化器,防止将来 lambda-layer 漂移。
"""

import json
import os
import re

import boto3

_s3 = boto3.client("s3")

ARCHIVE_BUCKET = os.environ.get("AUDIT_ARCHIVE_BUCKET", "")
ARCHIVE_PREFIX = os.environ.get("AUDIT_ARCHIVE_PREFIX", "audit-archive").strip("/")
# CMK 只由 S3 桶自身默认加密处理(BucketEncryption=KMS + encryption_key),PutObject
# 不需要显式带 SSE 头。这个 env 保留是为审计溯源与将来跨账号复制加密。
_CMK_KEY_ID = os.environ.get("AUDIT_ARCHIVE_CMK_KEY_ID", "")

# S3 key 安全:S3 允许几乎所有字符,但 owner_id 若混入路径分隔符会破坏分区结构。
# actor_owner_id 名义上是 Cognito sub(UUID 36 字符)或 API_KEY_OWNER 定值,均在
# 白名单范围。任何越界字符按 _ 归一化,防越权用户借 owner_id 构造 ../ 越目录。
_SAFE_KEY = re.compile(r"[^0-9A-Za-z_.-]")


def _sanitize(s: str) -> str:
    if not s:
        return "unknown"
    return _SAFE_KEY.sub("_", s)[:128] or "unknown"


def _unmarshal(image):
    """Unmarshal a DynamoDB NEW_IMAGE dict into a plain Python dict.

    DDB stream event: {"attrName": {"S": "value"}} / {"N": "12"} / {"BOOL": true} /
    {"NULL": true} / {"L": [...]} / {"M": {...}} / {"SS": [...]} / {"NS": [...]}.
    We only need S/N/BOOL/NULL/L/M for audit rows in practice; other types round-trip
    as best-effort str so a schema drift doesn't crash the archiver.
    """
    if not isinstance(image, dict):
        return image
    out = {}
    for k, v in image.items():
        if not isinstance(v, dict):
            out[k] = v
            continue
        # DDB attr descriptor has exactly one type key
        ((t, val),) = v.items()
        if t == "S":
            out[k] = val
        elif t == "N":
            # keep numeric strings as-is; downstream reader decides int vs float
            out[k] = val
        elif t == "BOOL":
            out[k] = bool(val)
        elif t == "NULL":
            out[k] = None
        elif t == "L":
            out[k] = [_unmarshal({"_": item})["_"] for item in val]
        elif t == "M":
            out[k] = _unmarshal(val)
        elif t == "SS" or t == "NS":
            out[k] = list(val)
        else:
            out[k] = str(val)
    return out


def _archive_key(row, ts_iso):
    """Build partitioned S3 key: <prefix>/<owner>/<yyyy>/<mm>/<dd>/<uuid>.json"""
    owner = _sanitize(row.get("actor_owner_id") or "unknown")
    row_id = _sanitize(row.get("id") or "no-id")
    # ts is ISO-8601 like "2026-07-04T05:12:34.567Z"; grab yyyy/mm/dd from it
    date = "unknown-date"
    if (
        isinstance(ts_iso, str)
        and len(ts_iso) >= 10
        and ts_iso[4] == "-"
        and ts_iso[7] == "-"
    ):
        date = ts_iso[:10]
    yyyy, mm, dd = (date.split("-") + ["00", "00"])[:3]
    return f"{ARCHIVE_PREFIX}/{owner}/{yyyy}/{mm}/{dd}/{row_id}.json"


def _archive_one(record):
    event_name = record.get("eventName", "")
    if event_name != "INSERT":
        # Belt-and-suspenders (Lambda-side filter already drops non-INSERT). Skip
        # rather than raising so partial batches don't false-fail.
        return None
    dyn = record.get("dynamodb") or {}
    new_image = dyn.get("NewImage")
    if not new_image:
        # Malformed record; fail-loud so operator sees it, don't silently drop
        raise ValueError(
            f"audit archive: INSERT without NewImage in eventID={record.get('eventID')}"
        )
    row = _unmarshal(new_image)
    key = _archive_key(row, row.get("ts", ""))
    body = json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
    _s3.put_object(
        Bucket=ARCHIVE_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    return key


def lambda_handler(event, context):
    """Entry: DynamoDB Stream event with `Records` list."""
    if not ARCHIVE_BUCKET:
        # config gate off but Lambda somehow bound — refuse loudly, not silently
        raise RuntimeError("AUDIT_ARCHIVE_BUCKET not configured")
    records = event.get("Records", [])
    written = []
    for rec in records:
        key = _archive_one(rec)
        if key:
            written.append(key)
    # Return summary for CloudWatch visibility (not used by DDB stream service)
    return {"archived": len(written), "keys": written[:10]}
