# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Audit-log row writer shared across Lambdas (T3-3).

The #71 audit schema was written by three drifting helpers — api
`_audit_write`/`_audit_system`, health_check `_emit_audit`, scaler `_audit`.
Critically, health_check `_emit_audit` HARD-CODED a 90-day TTL instead of
honoring AUDIT_TTL_DAYS, so its rows outlived everything else's when an operator
shortened retention. This is the one authoritative writer; each Lambda passes
its own `audit_table` + `ttl_days` (from AUDIT_TTL_DAYS), so the TTL is now
consistent everywhere.

`put_audit_row` never raises — an audit failure must never break the caller.
"""

import json
import time
import uuid


def put_audit_row(audit_table, *, event, obj, resource_id, actor,
                  actor_role="system", operation=None, response_status=200,
                  detail=None, ttl_days=90, api_key_id=None, ts=None):
    """Write one row in the unified #71 schema. No-op if audit_table is None.

    Fields mirror what the console Logs tab + existing readers/tests expect:
    pk/id/ts, operation (back-compat label), resource_id, api_key_id,
    response_status, expires_ttl (now + ttl_days), event, object, actor,
    actor_role, and a truncated detail.
    """
    if audit_table is None:
        return
    try:
        now = time.time()
        item = {
            "pk": "audit",
            "id": str(uuid.uuid4()),
            "ts": ts or _iso_now(),
            "operation": operation if operation is not None else event,
            "resource_id": resource_id or "",
            "api_key_id": api_key_id if api_key_id is not None else actor,
            "response_status": response_status,
            "expires_ttl": int(now) + int(ttl_days) * 86400,
            "event": event,
            "object": obj,
            "actor": actor,
            "actor_role": actor_role,
        }
        if detail is not None:
            item["detail"] = (detail if isinstance(detail, str)
                              else json.dumps(detail, default=str))[:1000]
        audit_table.put_item(Item=item)
    except Exception as e:
        print(f"audit write failed (non-fatal): {e}")


def _iso_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
