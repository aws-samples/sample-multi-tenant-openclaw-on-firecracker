# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure, stateless helpers for the api handler (T3-4 Phase 2).

Everything here is a pure function of its arguments — no boto3 clients, no DDB
tables, no module-global state — so it moves out of the facade with zero
late-binding concern. The facade re-exports these names (`from domains.common
import *`) so `api._resp`, `api._validate_name`, etc. keep resolving for the
existing tests and intra-handler callers.
"""

import hashlib
import json
import re
import time

# ── Tenant name + tags validation ──
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
_TAG_MAX_KEY_LEN = 50
_TAG_MAX_VALUE_LEN = 100
_TAG_MAX_COUNT = 20

# ── TTL (#28 / issue #15) ──
_TTL_MAX_HOURS = 8760  # 1 year
_TTL_VALID_ON_EXPIRY = ("stop", "delete")

# ── Schedule (#30 / issue #11) ── validation only; the scaler owns execution.
_SCHED_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,x-api-key,Authorization",
        },
        "body": json.dumps(body, default=str),
    }


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _gen_id(name):
    """Generate tenant id: name-xxxx (4 char hash)."""
    raw = f"{name}{time.time()}"
    short = hashlib.sha256(raw.encode()).hexdigest()[:4]
    return f"{name}-{short}"


def _validate_name(name):
    """Tenant name: lowercase DNS-label. Drives the tenant id, which gets
    embedded in URLs, Firecracker socket paths, and ALB rule conditions —
    all of which choke on whitespace or special chars."""
    if not isinstance(name, str) or not name:
        return "name is required"
    if len(name) > 32:
        return "name exceeds 32 characters"
    if not _NAME_RE.match(name):
        return ("name must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ "
                "(lowercase letters, digits, hyphens; cannot start/end with hyphen)")
    return None


def _validate_tags(tags):
    """Return None if valid, else an error message string."""
    if tags is None:
        return None  # absent → treated as {}
    if not isinstance(tags, dict):
        return "tags must be an object (key/value map)"
    if len(tags) > _TAG_MAX_COUNT:
        return f"too many tags (max {_TAG_MAX_COUNT})"
    for k, v in tags.items():
        if not isinstance(k, str) or not k:
            return "tag key must be a non-empty string"
        if not isinstance(v, str):
            return f"tag value for '{k}' must be a string"
        if ":" in k:
            return f"tag key '{k}' must not contain ':' (reserved for query syntax)"
        if ":" in v:
            return f"tag value '{v}' must not contain ':' (reserved for query syntax)"
        if len(k) > _TAG_MAX_KEY_LEN:
            return f"tag key '{k}' exceeds {_TAG_MAX_KEY_LEN} characters"
        if len(v) > _TAG_MAX_VALUE_LEN:
            return f"tag value for '{k}' exceeds {_TAG_MAX_VALUE_LEN} characters"
    return None


def _collect_tag_filters(query_params, multi_query_params):
    """Return list of (key, value) pairs from ?tag=k:v occurrences.

    API Gateway delivers repeated query params via multiValueQueryStringParameters.
    For single-value calls only queryStringParameters is populated.
    """
    raw = []
    if multi_query_params and "tag" in multi_query_params:
        raw = list(multi_query_params["tag"] or [])
    elif query_params and "tag" in query_params:
        raw = [query_params["tag"]]
    pairs = []
    for r in raw:
        if not r or ":" not in r:
            # Malformed filter — keep it so it matches nothing (defensive)
            pairs.append((None, None))
            continue
        k, v = r.split(":", 1)
        pairs.append((k, v))
    return pairs


def _matches_all_tags(item, filters):
    """Item must have every (k, v) pair to match (AND semantics)."""
    item_tags = item.get("tags") or {}
    for k, v in filters:
        if k is None or item_tags.get(k) != v:
            return False
    return True


def _parse_ttl(ttl_hours_raw, on_expiry_raw):
    """Validate and compute TTL fields.

    Returns (fields_dict, error_message). fields_dict is empty when no TTL is
    requested; otherwise contains {ttl_hours, on_expiry, expires_at}.
    """
    if ttl_hours_raw is None:
        if on_expiry_raw is not None:
            return {}, "on_expiry requires ttl_hours"
        return {}, None
    try:
        if isinstance(ttl_hours_raw, bool):
            raise TypeError
        ttl_hours = int(ttl_hours_raw)
    except (TypeError, ValueError):
        return {}, "ttl_hours must be a positive integer"
    if ttl_hours <= 0:
        return {}, "ttl_hours must be a positive integer"
    if ttl_hours > _TTL_MAX_HOURS:
        return {}, f"ttl_hours must be <= {_TTL_MAX_HOURS} (1 year)"
    on_expiry = on_expiry_raw or "stop"
    if on_expiry not in _TTL_VALID_ON_EXPIRY:
        return {}, (f"on_expiry must be one of {sorted(_TTL_VALID_ON_EXPIRY)}; "
                    f"got {on_expiry!r}")
    from datetime import datetime, timedelta, timezone
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    return {"ttl_hours": ttl_hours, "on_expiry": on_expiry, "expires_at": expires_at}, None


def _parse_schedule(raw):
    """Validate a {start, stop, timezone, days} schedule. Returns (dict, err)."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "schedule must be an object"
    start = raw.get("start"); stop = raw.get("stop")
    if not start:
        return None, "schedule.start required"
    if not stop:
        return None, "schedule.stop required"
    from datetime import datetime as _dt
    if not re.match(r"^\d{2}:\d{2}$", str(start)) or not re.match(r"^\d{2}:\d{2}$", str(stop)):
        return None, "schedule.start/stop must be HH:MM"
    # Strict parse: rejects 08:60 etc.
    try:
        _dt.strptime(start, "%H:%M")
        _dt.strptime(stop, "%H:%M")
    except ValueError:
        return None, "schedule.start/stop must be a valid HH:MM time"
    if start == stop:
        return None, "schedule.start must differ from schedule.stop"
    tz = raw.get("timezone", "UTC")
    try:
        from zoneinfo import ZoneInfo  # noqa
        ZoneInfo(tz)
    except Exception:
        return None, f"unknown timezone: {tz}"
    days = raw.get("days") or list(_SCHED_DAYS)
    if not isinstance(days, list) or any(d not in _SCHED_DAYS for d in days):
        return None, f"schedule.days must be a subset of {list(_SCHED_DAYS)}"
    return {"start": start, "stop": stop, "timezone": tz, "days": days}, None
