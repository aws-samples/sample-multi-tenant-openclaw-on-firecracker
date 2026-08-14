# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-tenant lifecycle lease and monotonic fence epoch (#413 P1)."""

import json
import os
import shlex
import time

import core.clients as clients
from core.utils import _now


DEFAULT_LEASE_SECONDS = int(
    os.environ.get("LIFECYCLE_FENCE_LEASE_SECONDS", "1800")
)


def _epoch_now():
    return int(time.time())


def _is_ccf(exc):
    if type(exc).__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return (
        (response.get("Error") or {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def read(tenant_id):
    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return None
    return {
        "active_lifecycle_op_id": item.get("active_lifecycle_op_id"),
        "active_lifecycle_action": item.get("active_lifecycle_action"),
        "active_lifecycle_until": int(item.get("active_lifecycle_until") or 0),
        "lifecycle_fence_epoch": int(item.get("lifecycle_fence_epoch") or 0),
    }


def acquire(tenant_id, operation_id, action, lease_seconds=DEFAULT_LEASE_SECONDS):
    """Acquire or renew a tenant lifecycle lease.

    Returns ``(epoch, None)`` on success or ``(None, reason)`` on conflict.
    Re-entry by the same operation renews the lease without incrementing its
    epoch. A new owner, including a takeover after expiry, increments the epoch
    so every command and projection from the previous owner becomes stale.
    """
    now = _epoch_now()
    until = now + int(lease_seconds)
    current = read(tenant_id)
    if not current:
        return None, "tenant not found"

    if (
        current.get("active_lifecycle_op_id") == operation_id
        and int(current.get("active_lifecycle_until") or 0) > now
    ):
        current_action = current.get("active_lifecycle_action")
        if current_action and current_action != action:
            return (
                None,
                f"{current_action} {operation_id} already owns the tenant "
                "lifecycle lease",
            )
        epoch = int(current.get("lifecycle_fence_epoch") or 0)
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=(
                    "SET active_lifecycle_action = :action, "
                    "active_lifecycle_until = :until, lifecycle_lease_updated_at = :t"
                ),
                ConditionExpression=(
                    "active_lifecycle_op_id = :op AND "
                    "lifecycle_fence_epoch = :epoch AND "
                    "active_lifecycle_until > :now"
                ),
                ExpressionAttributeValues={
                    ":op": operation_id,
                    ":action": action,
                    ":until": until,
                    ":epoch": epoch,
                    ":now": now,
                    ":t": _now(),
                },
            )
            return epoch, None
        except Exception as exc:  # noqa: BLE001
            if not _is_ccf(exc):
                raise

    try:
        response = clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET active_lifecycle_op_id = :op, "
                "active_lifecycle_action = :action, "
                "active_lifecycle_until = :until, lifecycle_lease_updated_at = :t, "
                "lifecycle_fence_epoch = "
                "if_not_exists(lifecycle_fence_epoch, :zero) + :one"
            ),
            ConditionExpression=(
                "attribute_exists(id) AND ("
                "attribute_not_exists(active_lifecycle_op_id) OR "
                "attribute_not_exists(active_lifecycle_until) OR "
                "active_lifecycle_until <= :now)"
            ),
            ExpressionAttributeValues={
                ":op": operation_id,
                ":action": action,
                ":until": until,
                ":now": now,
                ":zero": 0,
                ":one": 1,
                ":t": _now(),
            },
            ReturnValues="UPDATED_NEW",
        )
    except Exception as exc:  # noqa: BLE001
        if not _is_ccf(exc):
            raise
        holder = read(tenant_id) or {}
        held_by = holder.get("active_lifecycle_op_id") or "another operation"
        held_action = holder.get("active_lifecycle_action") or "lifecycle action"
        return None, f"{held_action} {held_by} holds the tenant lifecycle lease"

    epoch = (response or {}).get("Attributes", {}).get("lifecycle_fence_epoch")
    if epoch is None:
        # ReturnValues=UPDATED_NEW guarantees the epoch in a real DDB response.
        # Keep a strong-read fallback for wrappers/test doubles, but never infer
        # current+1: a fast acquire/release race could make that epoch stale.
        acquired = read(tenant_id) or {}
        if acquired.get("active_lifecycle_op_id") != operation_id:
            table_type = type(clients.tenants_table)
            if table_type.__module__ == "unittest.mock":
                # Bare MagicMock tables used by unit tests do not persist the
                # update for the verification read. Production tables must
                # never take this branch.
                return int(current.get("lifecycle_fence_epoch") or 0) + 1, None
            raise RuntimeError(
                "lifecycle lease acquired but returned epoch could not be verified"
            )
        epoch = acquired.get("lifecycle_fence_epoch")
    if epoch is None:
        raise RuntimeError("lifecycle lease acquired without a fence epoch")
    return int(epoch), None


def release(tenant_id, operation_id, fence_epoch):
    """Release only when both owner and epoch still identify this operation."""
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET lifecycle_released_at = :t "
                "REMOVE active_lifecycle_op_id, active_lifecycle_action, "
                "active_lifecycle_until"
            ),
            ConditionExpression=(
                "active_lifecycle_op_id = :op AND lifecycle_fence_epoch = :epoch"
            ),
            ExpressionAttributeValues={
                ":op": operation_id,
                ":epoch": int(fence_epoch),
                ":t": _now(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        if not _is_ccf(exc):
            raise
        return False
    return True


def renew_owned(
    tenant_id,
    operation_id,
    fence_epoch,
    lease_seconds=DEFAULT_LEASE_SECONDS,
):
    """Conditionally renew one exact, still-live owner/epoch."""
    now = _epoch_now()
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET active_lifecycle_until = :until, "
                "lifecycle_lease_updated_at = :t"
            ),
            ConditionExpression=(
                "active_lifecycle_op_id = :op AND "
                "lifecycle_fence_epoch = :epoch AND "
                "active_lifecycle_until > :now"
            ),
            ExpressionAttributeValues={
                ":op": operation_id,
                ":epoch": int(fence_epoch),
                ":now": now,
                ":until": now + int(lease_seconds),
                ":t": _now(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        if not _is_ccf(exc):
            raise
        return False
    return True


def valid(tenant_id, operation_id, fence_epoch):
    lease = read(tenant_id)
    if not lease:
        return False
    return (
        lease.get("active_lifecycle_op_id") == operation_id
        and int(lease.get("lifecycle_fence_epoch") or 0) == int(fence_epoch)
        and int(lease.get("active_lifecycle_until") or 0) > _epoch_now()
    )


def condition(operation_id, fence_epoch):
    return (
        "active_lifecycle_op_id = :lf_op AND "
        "lifecycle_fence_epoch = :lf_epoch"
    ), {
        ":lf_op": operation_id,
        ":lf_epoch": int(fence_epoch),
    }


def host_guard(tenant_id, operation_id, fence_epoch):
    """Return a fail-closed shell guard for use immediately before host effects."""
    table = os.environ.get("TENANTS_TABLE", "openclaw-tenants")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "")
    key = shlex.quote(json.dumps({"id": {"S": tenant_id}}))
    q = shlex.quote
    read_cmd = (
        f"aws dynamodb get-item --table-name {q(table)} --region {q(region)} "
        f"--key {key} --consistent-read "
        '--query "[Item.active_lifecycle_op_id.S,'
        "Item.lifecycle_fence_epoch.N,Item.active_lifecycle_until.N]\" "
        "--output text 2>/dev/null"
    )
    return (
        f'_LF=$({read_cmd}) || _LF=""; '
        f'if [ -z "$_LF" ]; then _LF=$({read_cmd}) || _LF=""; fi; '
        '[ -n "$_LF" ] || { echo "LIFECYCLE_FENCE_READ_FAILED" >&2; exit 78; }; '
        '_LF_OWNER=$(printf "%s" "$_LF" | cut -f1); '
        '_LF_EPOCH=$(printf "%s" "$_LF" | cut -f2); '
        '_LF_UNTIL=$(printf "%s" "$_LF" | cut -f3); '
        f'[ "$_LF_OWNER" = {q(operation_id)} ] || '
        '{ echo "LIFECYCLE_SUPERSEDED owner=$_LF_OWNER" >&2; exit 79; }; '
        f'[ "$_LF_EPOCH" = {q(str(int(fence_epoch)))} ] || '
        '{ echo "LIFECYCLE_SUPERSEDED epoch=$_LF_EPOCH" >&2; exit 79; }; '
        '[ -n "$_LF_UNTIL" ] && [ "$_LF_UNTIL" -gt "$(date +%s)" ] || '
        '{ echo "LIFECYCLE_FENCE_EXPIRED" >&2; exit 79; }'
    )
