"""Scale-safe, single-condition tenant queries."""

import json
import os
import re

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.auth import _get_caller_identity
from core.clients import tenants_table
from core.pagination import decode_cursor, encode_cursor
from core.utils import _TENANT_USER_ID_RE, _err, _resp
from services.tenant_service import _redact_tenant


QUERY_FIELDS = ("user_id", "host_id", "status", "rootfs_version")
INDEXES = {
    "user_id": ("gsi_tenant_user", "tenant_user_id"),
    "host_id": ("gsi_host", "host_id"),
    "status": ("gsi_status", "status"),
    "rootfs_version": ("gsi_rootfs_version", "q_rootfs_version"),
}
_HOST_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}\Z")
_STATUS_RE = re.compile(r"^[a-z_]{1,64}\Z")
# 与 activename#/inflight# 同款前缀隔离)。不补的话 GET /tenants 会把这些内部记录当成业务
# 租户返给调用方 —— 既是脏数据,也泄漏别人的 op_id/action 历史。
_INTERNAL_PREFIXES = ("activename#", "inflight#", "idem#", "__")
_RESPONSE_ITEM_BUDGET = 4_800_000


def is_tenant_record(item):
    item_id = str(item.get("id", ""))
    return bool(item_id) and not item_id.startswith(_INTERNAL_PREFIXES)


def _validate(field, value):
    if not isinstance(value, str) or not value:
        return False
    if field == "user_id":
        return bool(_TENANT_USER_ID_RE.match(value))
    if field == "host_id":
        return bool(_HOST_ID_RE.match(value))
    if field == "status":
        return bool(_STATUS_RE.match(value))
    try:
        return len(value.encode("utf-8")) <= 256
    except UnicodeEncodeError:
        return False


def _limit(query):
    raw = query.get("limit", "100")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 1000 else None


def list_tenants_by_condition(query, event):
    if os.environ.get("TENANT_QUERY_ENABLED", "false").lower() != "true":
        return _err(503, "UNAVAILABLE", "tenant query indexes are not active")
    present = [field for field in QUERY_FIELDS if field in query]
    conflicting_legacy = any(
        field in query for field in ("tag", "platform_id", "purchase_status")
    )
    if len(present) != 1 or conflicting_legacy:
        return _err(
            400,
            "VALIDATION",
            "exactly one of user_id, host_id, status, rootfs_version is allowed",
        )

    ident = _get_caller_identity(event or {})
    if not ident.get("is_admin") or ident.get("platform_scope") is not None:
        return _err(403, "FORBIDDEN", "admin access required")

    field = present[0]
    value = query[field]
    if not _validate(field, value):
        return _err(400, "VALIDATION", f"{field} is invalid")
    limit = _limit(query)
    if limit is None:
        return _err(400, "VALIDATION", "limit must be an integer between 1 and 1000")

    condition = {
        "route": "/tenants",
        "field": field,
        "value": value,
        "scope": "admin",
    }
    try:
        start_key = decode_cursor(query.get("next_token"), condition)
    except ValueError as exc:
        return _err(400, "VALIDATION", str(exc))

    index_name, attribute = INDEXES[field]
    kwargs = {
        "IndexName": index_name,
        "KeyConditionExpression": Key(attribute).eq(value),
        "Limit": limit,
    }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    try:
        out = tenants_table.query(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "ResourceNotFoundException",
            "ValidationException",
        }:
            return _err(503, "UNAVAILABLE", "tenant query index is not active")
        raise
    items = []
    encoded_size = 0
    truncated_key = None
    budget_truncated = False
    for raw_item in out.get("Items", []):
        if not is_tenant_record(raw_item) or (
            not (field == "status" and value == "deleted")
            and raw_item.get("status") == "deleted"
        ):
            continue
        # GET /tenants path (_redact_tenant = full row minus the secret blacklist),
        # not a narrow allowlist. The allowlist silently dropped non-secret
        # operational fields callers depend on (app_health, metrics, vm_health,
        # ...). _redact_tenant is the shared secret choke point, so parity here
        # never leaks a credential.
        item = _redact_tenant(raw_item)
        item.setdefault("tags", {})
        item_size = len(
            json.dumps(item, separators=(",", ":"), default=str).encode("utf-8")
        )
        if items and encoded_size + item_size > _RESPONSE_ITEM_BUDGET:
            budget_truncated = True
            break
        items.append(item)
        encoded_size += item_size
        truncated_key = {"id": raw_item["id"], attribute: raw_item.get(attribute, value)}
    next_key = out.get("LastEvaluatedKey")
    if budget_truncated and truncated_key:
        next_key = truncated_key
    return _resp(
        200,
        {
            "tenants": items,
            "next_token": encode_cursor(next_key, condition),
            "count": len(items),
        },
    )
