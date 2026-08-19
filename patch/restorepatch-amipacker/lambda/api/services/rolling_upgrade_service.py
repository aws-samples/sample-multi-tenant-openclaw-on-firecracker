# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Drift-gated rolling identity/image upgrade orchestration (#517 stage 4)."""

import hashlib
import json
import os
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError

import core.auth as auth
import core.clients as clients
from core.clients import API_KEY_OWNER
from core.utils import _err, _now, _resp
import services.tenant_service as tenant_service

_ACTIVE_STATUSES = {"queued", "running", "dispatch_unknown", "dispatch_failed"}
_ACTIONS = {"restart", "rebuild"}
_CLIENT_TOKEN_RE = re.compile(r"^[\x21-\x7e]{4,128}$")
_MAX_BATCH_SIZE = int(os.environ.get("ROLLING_UPGRADE_MAX_BATCH_SIZE", "25"))
_MAX_TARGETS = int(os.environ.get("ROLLING_UPGRADE_MAX_TARGETS", "2000"))
_POLL_INTERVAL_SEC = float(os.environ.get("ROLLING_UPGRADE_POLL_INTERVAL_SEC", "5"))
_BATCH_TIMEOUT_SEC = float(os.environ.get("ROLLING_UPGRADE_BATCH_TIMEOUT_SEC", "300"))
_WORKER_BUDGET_SEC = float(os.environ.get("ROLLING_UPGRADE_WORKER_BUDGET_SEC", "600"))
_WORKER_LEASE_SEC = max(
    960, int(os.environ.get("ROLLING_UPGRADE_WORKER_LEASE_SEC", "960"))
)
_SUBMIT_LOCK_SEC = 30
_SUBMIT_LOCK_ID = "__rolling_upgrade_submit_lock__"
_MAX_JOB_BYTES = 350_000  # leave headroom below DynamoDB's 400 KiB item limit
_REQUEST_FIELDS = {
    "scope",
    "action",
    "batch_size",
    "max_batch_failures",
    "target_version",
    "client_token",
}
_POLLABLE_TENANT_CODES = {
    "ENQUEUE_STATE_UNKNOWN",
    "IDEMPOTENT_OPERATION_IN_PROGRESS",
    "LIFECYCLE_IN_FLIGHT",
    "REBUILD_IN_FLIGHT",
}


def _parse_body(body):
    try:
        value = json.loads(body) if isinstance(body, str) else body
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _scan_table(table, **kwargs):
    items = []
    start = None
    while True:
        call = dict(kwargs)
        if start:
            call["ExclusiveStartKey"] = start
        out = table.scan(**call)
        items.extend(out.get("Items", []) or [])
        start = out.get("LastEvaluatedKey")
        if not start:
            return items


def _allowed(item, ident):
    scope = ident.get("platform_scope")
    if scope is not None:
        return item.get("platform_id") == scope
    if ident.get("is_admin"):
        return True
    owner = ident.get("owner_id")
    return bool(owner and item.get("owner_id") == owner)


def _scope_parts(body):
    scope = body.get("scope")
    if scope == "fleet":
        return "fleet", None
    if isinstance(scope, dict) and set(scope) == {"host_id"}:
        return "host_id", scope.get("host_id")
    if isinstance(scope, dict) and set(scope) == {"tenant_ids"}:
        return "tenant_ids", scope.get("tenant_ids")
    return None, None


def _resolve_targets(body, ident):
    kind, value = _scope_parts(body)
    if kind is None:
        return None, _err(
            400,
            "VALIDATION",
            "scope must be 'fleet', {host_id: ...}, or {tenant_ids: [...]}",
        )

    if kind == "tenant_ids":
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(v, str) or not v for v in value)
        ):
            return None, _err(
                400, "VALIDATION", "tenant_ids must be a non-empty string list"
            )
        if len(value) != len(set(value)):
            return None, _err(
                400, "VALIDATION", "tenant_ids must not contain duplicates"
            )
        tenants = []
        for tenant_id in value:
            item = clients.tenants_table.get_item(
                Key={"id": tenant_id}, ConsistentRead=True
            ).get("Item")
            if not item or item.get("status") == "deleted":
                return None, _err(404, "NOT_FOUND", "tenant not found")
            if not _allowed(item, ident):
                return None, _err(404, "NOT_FOUND", "tenant not found")
            tenants.append(item)
    else:
        if kind == "host_id" and (not isinstance(value, str) or not value):
            return None, _err(400, "VALIDATION", "host_id must be a non-empty string")
        tenants = _scan_table(clients.tenants_table, ConsistentRead=True)
        tenants = [
            item
            for item in tenants
            if item.get("id")
            and item.get("status") != "deleted"
            and item.get("vcpu") is not None
            and (kind != "host_id" or item.get("host_id") == value)
            and _allowed(item, ident)
        ]
        tenants.sort(key=lambda item: item["id"])

    if not tenants:
        return None, _err(400, "VALIDATION", "scope resolved to no tenants")
    if len(tenants) > _MAX_TARGETS:
        return None, _err(
            400,
            "VALIDATION",
            f"scope exceeds rolling-upgrade target limit {_MAX_TARGETS}",
        )

    hosts = {}
    targets = {}
    expected = body.get("target_version")
    if expected is not None and (not isinstance(expected, str) or not expected):
        return None, _err(
            400, "VALIDATION", "target_version must be a non-empty string"
        )
    for tenant in tenants:
        host_id = tenant.get("host_id")
        if not host_id:
            return None, _err(
                409,
                "CONFLICT",
                f"tenant has no assigned host: {tenant['id']}",
            )
        if host_id not in hosts:
            hosts[host_id] = clients.hosts_table.get_item(
                Key={"instance_id": host_id}, ConsistentRead=True
            ).get("Item")
        host = hosts[host_id] or {}
        rootfs = host.get("rootfs_version") or ""
        if not rootfs:
            return None, _err(
                409,
                "CONFLICT",
                f"host has no committed rootfs version: {host_id}",
            )
        if expected is not None and rootfs != expected:
            return None, _err(
                409,
                "CONFLICT",
                f"host {host_id} live version is {rootfs}, not {expected}",
                {"host_id": host_id},
            )
        targets[tenant["id"]] = {
            "host_id": host_id,
            "rootfs_version": rootfs,
            "immutable_version": host.get("immutable_version") or "",
        }
    return (kind, [item["id"] for item in tenants], targets), None


def _job_id(client_token, ident):
    seed = "\x00".join(
        [
            str(ident.get("owner_id") or ""),
            str(ident.get("tenant_user_id") or ""),
            str(ident.get("platform_scope") or ""),
            client_token,
        ]
    )
    return "rolling-" + hashlib.sha256(seed.encode()).hexdigest()[:24]


def _tenant_operation_id(job_id, tenant_id, action):
    seed = "\x00".join([job_id, tenant_id, action])
    return "rolling-" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _request_fingerprint(scope, action, batch_size, max_failures, target_version):
    canonical = json.dumps(
        {
            "scope": scope,
            "action": action,
            "batch_size": batch_size,
            "max_batch_failures": max_failures,
            "target_version": target_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _conditional_failed(exc):
    return isinstance(exc, ClientError) and (
        exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    )


def _acquire_submit_lock(owner):
    now = int(time.time())
    clients.batch_jobs_table.put_item(
        Item={
            "job_id": _SUBMIT_LOCK_ID,
            "item_type": "rolling-submit-lock",
            "lock_owner": owner,
            "lock_expires_at": now + _SUBMIT_LOCK_SEC,
            "expires_ttl": now + _SUBMIT_LOCK_SEC,
        },
        ConditionExpression=("attribute_not_exists(job_id) OR lock_expires_at < :now"),
        ExpressionAttributeValues={":now": now},
    )


def _release_submit_lock(owner):
    try:
        clients.batch_jobs_table.delete_item(
            Key={"job_id": _SUBMIT_LOCK_ID},
            ConditionExpression="lock_owner = :owner",
            ExpressionAttributeValues={":owner": owner},
        )
    except Exception as exc:  # lock TTL is the final recovery path
        print(f"[rolling-upgrade] release submit lock failed: {exc}")


def _active_jobs():
    return [
        item
        for item in _scan_table(clients.batch_jobs_table, ConsistentRead=True)
        if item.get("item_type") == "rolling-upgrade"
        and item.get("status") in _ACTIVE_STATUSES
    ]


def _invoke_worker(job_id):
    boto3.client("lambda").invoke(
        FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
        InvocationType="Event",
        Payload=json.dumps({"_rolling_job": job_id}).encode("utf-8"),
    )


def _mark_dispatch_unknown(job_id, expected_status):
    """Record ambiguous invoke state without overwriting a worker that claimed."""
    try:
        clients.batch_jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #s = :unknown, updated_at = :t REMOVE dispatch_error"
            ),
            ConditionExpression=(
                "#s = :expected AND attribute_not_exists(worker_lease_owner)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":unknown": "dispatch_unknown",
                ":expected": expected_status,
                ":t": _now(),
            },
        )
        return True
    except Exception as exc:
        if not _conditional_failed(exc):
            print(
                f"[rolling-upgrade] failed to persist dispatch ambiguity "
                f"for {job_id}: {exc}"
            )
        return False


def submit_rolling_upgrade(body=None, event=None):
    """POST /hosts/rolling-upgrade."""
    if clients.batch_jobs_table is None:
        return _err(503, "NOT_CONFIGURED", "rolling jobs are not configured")
    body = _parse_body(body)
    if body is None:
        return _err(400, "VALIDATION", "body must be a JSON object")
    unknown_fields = sorted(set(body) - _REQUEST_FIELDS)
    if unknown_fields:
        return _err(
            400,
            "VALIDATION",
            f"unsupported request fields: {', '.join(unknown_fields)}",
        )

    ident = auth._get_caller_identity(event or {})
    action = body.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        return _err(400, "VALIDATION", f"action must be one of {sorted(_ACTIONS)}")
    # The existing rebuild action is admin-only. Reject before creating a job
    # that can only fail, while restart remains operator+ with owner checks.
    if action == "rebuild" and not ident.get("is_admin"):
        return _err(403, "ACCESS_DENIED", "rebuild requires admin")

    batch_size = body.get("batch_size", 1)
    max_failures = body.get("max_batch_failures", 0)
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= _MAX_BATCH_SIZE
    ):
        return _err(
            400,
            "VALIDATION",
            f"batch_size must be between 1 and {_MAX_BATCH_SIZE}",
        )
    if (
        isinstance(max_failures, bool)
        or not isinstance(max_failures, int)
        or not 0 <= max_failures < batch_size
    ):
        return _err(
            400,
            "VALIDATION",
            "max_batch_failures must be between 0 and batch_size - 1",
        )
    client_token = body.get("client_token")
    if not isinstance(client_token, str) or not _CLIENT_TOKEN_RE.fullmatch(
        client_token
    ):
        return _err(
            400,
            "VALIDATION",
            "client_token must be 4-128 printable non-space ASCII characters",
        )

    job_id = _job_id(client_token, ident)
    request_fingerprint = _request_fingerprint(
        body.get("scope"),
        action,
        batch_size,
        max_failures,
        body.get("target_version"),
    )
    lock_owner = f"{job_id}:{uuid.uuid4().hex}"
    try:
        _acquire_submit_lock(lock_owner)
    except Exception as exc:
        if _conditional_failed(exc):
            return _err(409, "CONFLICT", "rolling-upgrade submission is busy")
        print(f"[rolling-upgrade] submit lock acquisition failed: {exc}")
        return _err(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "rolling-upgrade submission is temporarily unavailable",
        )

    now = _now()
    try:
        existing = clients.batch_jobs_table.get_item(
            Key={"job_id": job_id}, ConsistentRead=True
        ).get("Item")
        if existing:
            if existing.get("request_fingerprint") != request_fingerprint:
                return _err(
                    409,
                    "CONFLICT",
                    "client_token was already used for a different request",
                    {"job_id": job_id},
                )
            status = existing.get("status")
            lease_expired = int(existing.get("worker_lease_until") or 0) < int(
                time.time()
            )
            # A retry with the same token is also the manual recovery path for
            # a failed initial invoke or a worker that died and left an expired
            # lease. The worker claim still prevents concurrent execution.
            if status in _ACTIVE_STATUSES and lease_expired:
                try:
                    _invoke_worker(job_id)
                except Exception as exc:
                    print(
                        f"[rolling-upgrade] resume dispatch state unknown "
                        f"for {job_id}: {exc}"
                    )
                    if status in {"queued", "dispatch_failed"}:
                        _mark_dispatch_unknown(job_id, status)
                    return _err(
                        503,
                        "DISPATCH_STATE_UNKNOWN",
                        "the rolling worker may have been accepted; retry with the "
                        "same client_token and poll the job",
                        {"job_id": job_id},
                    )
            return _resp(
                202,
                {
                    "job_id": job_id,
                    "status": status,
                    "total": existing.get("total", 0),
                    "idempotent_replay": True,
                },
            )
        try:
            resolved, error = _resolve_targets(body, ident)
        except Exception as exc:
            print(f"[rolling-upgrade] target resolution failed: {exc}")
            return _err(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "rolling-upgrade target resolution is temporarily unavailable",
            )
        if error:
            return error
        scope_kind, target_ids, targets = resolved
        requested = set(target_ids)
        for active in _active_jobs():
            if requested.intersection(active.get("target_ids", [])):
                return _err(
                    409,
                    "CONFLICT",
                    "scope overlaps an active rolling-upgrade job",
                    {"active_job_id": active.get("job_id")},
                )
        item = {
            "job_id": job_id,
            "item_type": "rolling-upgrade",
            "scope_kind": scope_kind,
            "action": action,
            "batch_size": batch_size,
            "max_batch_failures": max_failures,
            "target_ids": target_ids,
            "pending_ids": target_ids,
            "targets": targets,
            "total": len(target_ids),
            "done": 0,
            "succeeded": [],
            "failed": [],
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "expires_ttl": int(time.time()) + 7 * 24 * 3600,
            "actor_owner_id": ident.get("owner_id"),
            "actor_is_admin": bool(ident.get("is_admin")),
            "actor_tenant_user_id": ident.get("tenant_user_id"),
            "actor_platform_scope": ident.get("platform_scope"),
            "request_fingerprint": request_fingerprint,
        }
        if len(json.dumps(item, separators=(",", ":")).encode()) > _MAX_JOB_BYTES:
            return _err(
                400,
                "VALIDATION",
                "rolling-upgrade job state exceeds the DynamoDB item size safety "
                "limit; submit a narrower scope",
            )
        clients.batch_jobs_table.put_item(
            Item=item, ConditionExpression="attribute_not_exists(job_id)"
        )
    except Exception as exc:
        if _conditional_failed(exc):
            return _resp(
                202,
                {
                    "job_id": job_id,
                    "status": "queued",
                    "total": len(target_ids),
                    "idempotent_replay": True,
                },
            )
        print(f"[rolling-upgrade] job creation failed for {job_id}: {exc}")
        return _err(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "rolling-upgrade job creation is temporarily unavailable",
        )
    finally:
        _release_submit_lock(lock_owner)

    try:
        _invoke_worker(job_id)
    except Exception as exc:
        print(f"[rolling-upgrade] initial dispatch state unknown for {job_id}: {exc}")
        _mark_dispatch_unknown(job_id, "queued")
        return _err(
            503,
            "DISPATCH_STATE_UNKNOWN",
            "the rolling worker may have been accepted; retry with the same "
            "client_token and poll the job",
            {"job_id": job_id},
        )
    return _resp(202, {"job_id": job_id, "status": "queued", "total": len(target_ids)})


def _synthetic_event(job):
    return {
        "_caller_identity_memo": {
            "owner_id": job.get("actor_owner_id"),
            "role": "admin" if job.get("actor_is_admin") else "operator",
            "is_admin": bool(job.get("actor_is_admin")),
            "api_key_only": job.get("actor_owner_id") == API_KEY_OWNER,
            "tenant_user_id": job.get("actor_tenant_user_id"),
            "platform_scope": job.get("actor_platform_scope"),
        }
    }


def _tenant(tenant_id):
    return clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")


def _converged(item, target):
    if not item or item.get("status") == "deleted":
        return False
    if item.get("host_id") != target.get("host_id"):
        return False
    if item.get("rootfs_version") != target.get("rootfs_version"):
        return False
    immutable = target.get("immutable_version") or ""
    return not immutable or item.get("immutable_version") == immutable


def _append_unique(results, entry):
    tenant_id = entry["id"]
    if not any(old.get("id") == tenant_id for old in results):
        results.append(entry)


def _persist_progress(job_id, lease_owner, status, pending, succeeded, failed, batch):
    try:
        clients.batch_jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #s = :s, pending_ids = :p, done = :d, succeeded = :ok, "
                "failed = :bad, current_batch = :b, updated_at = :t "
                "REMOVE worker_lease_until, worker_lease_owner"
            ),
            ConditionExpression="#s = :running AND worker_lease_owner = :owner",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":owner": lease_owner,
                ":s": status,
                ":p": pending,
                ":d": len(succeeded) + len(failed),
                ":ok": succeeded,
                ":bad": failed,
                ":b": batch,
                ":t": _now(),
            },
        )
        return True
    except Exception as exc:
        if _conditional_failed(exc):
            return False
        raise


def _set_current_batch(job_id, lease_owner, batch):
    try:
        clients.batch_jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET current_batch = :b, updated_at = :t",
            ConditionExpression="#s = :running AND worker_lease_owner = :owner",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":owner": lease_owner,
                ":b": batch,
                ":t": _now(),
            },
        )
        return True
    except Exception as exc:
        if _conditional_failed(exc):
            return False
        raise


def _claim_worker(job_id, owner, now_epoch):
    try:
        clients.batch_jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #s = :running, worker_lease_owner = :owner, "
                "worker_lease_until = :until, updated_at = :t"
            ),
            ConditionExpression=(
                "#s IN (:queued, :running, :unknown, :legacy_failed) AND "
                "(attribute_not_exists(worker_lease_until) OR worker_lease_until < :now)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":queued": "queued",
                ":running": "running",
                ":unknown": "dispatch_unknown",
                ":legacy_failed": "dispatch_failed",
                ":owner": owner,
                ":until": now_epoch + _WORKER_LEASE_SEC,
                ":now": now_epoch,
                ":t": _now(),
            },
        )
        return True
    except Exception as exc:
        if _conditional_failed(exc):
            return False
        raise


def _continue_job(job_id):
    try:
        _invoke_worker(job_id)
    except Exception as exc:
        print(
            f"[rolling-upgrade] continuation dispatch state unknown for {job_id}: {exc}"
        )
        _mark_dispatch_unknown(job_id, "queued")
        raise


def _wait_for_convergence(tenant_ids, targets, invocation_deadline):
    batch_deadline = min(time.time() + _BATCH_TIMEOUT_SEC, invocation_deadline)
    remaining = list(tenant_ids)
    while True:
        remaining = [
            tenant_id
            for tenant_id in remaining
            if not _converged(_tenant(tenant_id), targets[tenant_id])
        ]
        if not remaining:
            return [], False
        now = time.time()
        if now >= batch_deadline:
            return remaining, now >= invocation_deadline
        time.sleep(min(_POLL_INTERVAL_SEC, max(0, batch_deadline - now)))


def run_rolling_job(job_id):
    """Run one bounded rolling batch and self-invoke for the next batch."""
    if clients.batch_jobs_table is None:
        return {"statusCode": 503, "body": "rolling jobs not configured"}
    job = clients.batch_jobs_table.get_item(
        Key={"job_id": job_id}, ConsistentRead=True
    ).get("Item")
    if not job or job.get("item_type") != "rolling-upgrade":
        return {"statusCode": 404, "body": "rolling job not found"}
    if job.get("status") not in _ACTIVE_STATUSES:
        return {"statusCode": 200, "body": f"job {job_id} already {job.get('status')}"}

    started = time.time()
    lease_owner = uuid.uuid4().hex
    if not _claim_worker(job_id, lease_owner, int(started)):
        return {"statusCode": 200, "body": f"job {job_id} already running"}

    pending = list(job.get("pending_ids", job.get("target_ids", [])))
    targets = job.get("targets", {})
    succeeded = list(job.get("succeeded", []))
    failed = list(job.get("failed", []))

    # A retry can observe tenants converged by the prior invocation after it
    # timed out. Retire those before selecting a batch, without acting again.
    still_pending = []
    for tenant_id in pending:
        if _converged(_tenant(tenant_id), targets.get(tenant_id, {})):
            _append_unique(
                succeeded,
                {"id": tenant_id, "action": job["action"], "converged": True},
            )
        else:
            still_pending.append(tenant_id)
    pending = still_pending
    if not pending:
        if not _persist_progress(
            job_id, lease_owner, "done", [], succeeded, failed, []
        ):
            return {"statusCode": 200, "body": f"job {job_id} worker lease lost"}
        return {"statusCode": 200, "body": f"job {job_id} done"}

    if time.time() - started >= _WORKER_BUDGET_SEC:
        if not _persist_progress(
            job_id, lease_owner, "queued", pending, succeeded, failed, []
        ):
            return {"statusCode": 200, "body": f"job {job_id} worker lease lost"}
        _continue_job(job_id)
        return {"statusCode": 202, "body": f"job {job_id} continued"}

    batch = pending[: int(job.get("batch_size", 1))]
    if not _set_current_batch(job_id, lease_owner, batch):
        return {"statusCode": 200, "body": f"job {job_id} worker lease lost"}

    dispatched = []
    batch_failures = []
    base_event = _synthetic_event(job)
    for tenant_id in batch:
        event = dict(base_event)
        event["_op_id"] = _tenant_operation_id(job_id, tenant_id, job["action"])
        try:
            result = tenant_service.tenant_action(tenant_id, job["action"], None, event)
            code = result.get("statusCode", 500) if isinstance(result, dict) else 500
            try:
                response_body = json.loads(result.get("body") or "{}")
            except (AttributeError, TypeError, ValueError):
                response_body = {}
            error_code = response_body.get("code")
            if code >= 400:
                if error_code in _POLLABLE_TENANT_CODES:
                    dispatched.append(tenant_id)
                else:
                    batch_failures.append(
                        {
                            "id": tenant_id,
                            "error": "tenant action was rejected",
                            "code": error_code or "TENANT_ACTION_FAILED",
                            "http_status": code,
                        }
                    )
            else:
                dispatched.append(tenant_id)
        except Exception as exc:
            print(
                f"[rolling-upgrade] tenant action raised for "
                f"{job_id}/{tenant_id}: {exc}"
            )
            batch_failures.append(
                {
                    "id": tenant_id,
                    "error": "tenant action raised unexpectedly",
                    "code": "TENANT_ACTION_FAILED",
                }
            )

    not_converged, budget_exhausted = _wait_for_convergence(
        dispatched, targets, started + _WORKER_BUDGET_SEC
    )
    if budget_exhausted:
        if not _persist_progress(
            job_id, lease_owner, "queued", pending, succeeded, failed, batch
        ):
            return {"statusCode": 200, "body": f"job {job_id} worker lease lost"}
        _continue_job(job_id)
        return {"statusCode": 202, "body": f"job {job_id} continued"}

    not_converged_set = set(not_converged)
    for tenant_id in dispatched:
        if tenant_id in not_converged_set:
            batch_failures.append(
                {"id": tenant_id, "error": "did not converge before batch timeout"}
            )
        else:
            _append_unique(
                succeeded,
                {"id": tenant_id, "action": job["action"], "converged": True},
            )
    for entry in batch_failures:
        _append_unique(failed, entry)

    pending = [tenant_id for tenant_id in pending if tenant_id not in set(batch)]
    if len(batch_failures) > int(job.get("max_batch_failures", 0)):
        if not _persist_progress(
            job_id, lease_owner, "stopped", pending, succeeded, failed, batch
        ):
            return {"statusCode": 200, "body": f"job {job_id} worker lease lost"}
        return {"statusCode": 200, "body": f"job {job_id} stopped"}

    status = "queued" if pending else "done"
    if not _persist_progress(
        job_id, lease_owner, status, pending, succeeded, failed, batch
    ):
        return {"statusCode": 200, "body": f"job {job_id} worker lease lost"}
    if pending:
        _continue_job(job_id)
        return {"statusCode": 202, "body": f"job {job_id} continued"}
    return {"statusCode": 200, "body": f"job {job_id} done"}


def get_rolling_job(job_id, event=None):
    """GET /hosts/rolling-jobs/{job_id}."""
    if clients.batch_jobs_table is None:
        return _err(503, "NOT_CONFIGURED", "rolling jobs are not configured")
    try:
        job = clients.batch_jobs_table.get_item(
            Key={"job_id": job_id}, ConsistentRead=True
        ).get("Item")
    except Exception as exc:
        print(f"[rolling-upgrade] progress lookup failed for {job_id}: {exc}")
        return _err(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "rolling job progress is temporarily unavailable",
        )
    if not job or job.get("item_type") != "rolling-upgrade":
        return _err(404, "NOT_FOUND", "rolling job not found")
    ident = auth._get_caller_identity(event or {})
    scope = ident.get("platform_scope")
    if scope is not None:
        allowed = scope == job.get("actor_platform_scope")
    elif ident.get("is_admin"):
        allowed = True
    else:
        allowed = bool(
            ident.get("owner_id")
            and ident.get("owner_id") == job.get("actor_owner_id")
            and job.get("actor_platform_scope") is None
        )
    if not allowed:
        return _err(403, "ACCESS_DENIED", "rolling job access denied")
    return _resp(
        200,
        {
            "job_id": job["job_id"],
            "action": job.get("action"),
            "scope": job.get("scope_kind"),
            "status": job.get("status"),
            "total": job.get("total", 0),
            "done": job.get("done", 0),
            "current_batch": job.get("current_batch", []),
            "succeeded": job.get("succeeded", []),
            "failed": job.get("failed", []),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        },
    )
