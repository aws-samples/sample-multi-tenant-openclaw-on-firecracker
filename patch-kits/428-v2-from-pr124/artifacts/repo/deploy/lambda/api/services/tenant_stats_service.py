"""Read the latest complete tenant statistics snapshot."""

from datetime import datetime, timezone
from decimal import Decimal

from core.auth import _get_caller_identity
from core.clients import tenant_stats_table
from core.utils import _err, _resp


def _normalize_decimal_numbers(value):
    """Convert DynamoDB Decimal values into JSON-native numbers recursively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {
            key: _normalize_decimal_numbers(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_decimal_numbers(item) for item in value]
    return value


def get_tenant_stats(event):
    ident = _get_caller_identity(event or {})
    if not ident.get("is_admin") or ident.get("platform_scope") is not None:
        return _err(403, "FORBIDDEN", "admin access required")
    if tenant_stats_table is None:
        return _err(503, "UNAVAILABLE", "tenant statistics are not configured")
    item = tenant_stats_table.get_item(
        Key={"id": "current"}, ConsistentRead=True
    ).get("Item")
    if not item:
        return _err(503, "UNAVAILABLE", "tenant statistics snapshot is unavailable")
    item.pop("id", None)
    try:
        as_of = datetime.fromisoformat(item["data_as_of"].replace("Z", "+00:00"))
        item["snapshot_stale"] = (
            datetime.now(timezone.utc) - as_of
        ).total_seconds() > 90
    except (KeyError, TypeError, ValueError):
        item["snapshot_stale"] = True
    return _resp(200, _normalize_decimal_numbers(item))
