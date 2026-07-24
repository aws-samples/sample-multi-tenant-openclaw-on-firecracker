# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AZ-failover worker Lambda (T3-2).

The health_check detector enqueues one SQS message per affected tenant during
an AZ outage (see handler._enqueue_failover). This worker consumes ONE message
at a time (batch_size=1) and runs the launch/verify/repoint/flip pipeline for
that single tenant, so N tenants recover in PARALLEL across N concurrent worker
invocations instead of serially inside the detector's 180s budget.

It lives in the SAME asset directory as handler.py so it can ``import handler``
and reuse the entire failover pipeline (_execute_failover, the verify gates, the
ALB repoint, the DDB flip, audit) with zero duplication — CDK ships the whole
directory; the two Lambdas just point at different handler strings.

Idempotency contract (queue visibility_timeout 900s > this Lambda's 600s
timeout):
  * A worker re-claims a tenant with ConditionExpression status IN
    (failover_queued, failover_recovering). A redelivered message therefore
    proves the previous attempt died (its visibility timeout elapsed) and is
    safe to retry.
  * The success path flips status to running, so a crash-after-flip redelivery
    fails the re-claim and is a clean no-op (the message is deleted).
  * Handled failures set failover_failed / failover_failed_partial and RETURN
    normally (message deleted) — we never blind-retry a non-idempotent
    launch-vm.sh. Only unhandled exceptions redeliver.
"""

import json

import handler as hc


def _claim(tenant_id, now_iso):
    """Flip failover_queued/failover_recovering → failover_recovering for this
    worker. Returns True if we hold the claim, False if the tenant is in any
    other state (already recovered by a prior attempt, or watchdog-reaped)."""
    try:
        hc.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :r, failover_at = :t",
            ConditionExpression="#s = :q OR #s = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":r": "failover_recovering", ":q": "failover_queued",
                ":t": now_iso,
            },
        )
        return True
    except hc.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _process_record(body, now):
    job = json.loads(body)
    tenant_id = job["tenant_id"]

    tenant = hc.tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not tenant:
        print(f"worker: tenant {tenant_id} no longer exists — dropping job")
        return

    if not _claim(tenant_id, now.isoformat()):
        print(f"worker: {tenant_id} not in a claimable failover state — no-op")
        return

    # Load the target host fresh so private_ip / counters are current at
    # execution time (the detector's snapshot may be minutes old).
    target_host_id = job["target_host_id"]
    target_host = hc.hosts_table.get_item(
        Key={"instance_id": target_host_id}).get("Item")
    if not target_host:
        print(f"worker: target host {target_host_id} vanished — failing {tenant_id}")
        hc.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :f, failover_error = :e",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":f": "failover_failed",
                ":e": f"target host {target_host_id} not found at execution",
            },
        )
        hc._emit_audit("AZ_FAILOVER_TENANT_FAILED",
                       {"tenant_id": tenant_id, "error": "target host vanished",
                        "status_set": "failover_failed"})
        return

    # The detector already claimed the tenant, reserved the vm_num, and found
    # the backup — run steps 3-8 directly. _execute_failover never raises; it
    # sets failover_failed/partial + audit on failure and returns False.
    hc._execute_failover(
        tenant, target_host,
        source_az=job.get("source_az", ""),
        backup_key=job["backup_key"],
        target_vm_num=int(job["target_vm_num"]),
        now=now,
    )


def lambda_handler(event, context):
    """SQS event source, batch_size=1. Processes each record; an unhandled
    exception lets SQS redeliver (safe per the idempotency contract)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for record in (event or {}).get("Records", []):
        _process_record(record["body"], now)
    return {"processed": len((event or {}).get("Records", []))}
