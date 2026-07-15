# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/fleet_service — 批量租户操作 + fleet-power(全 host 启停)+ per-user fleet 管理。

handler-split #132 阶段2 —— 从 handler.py 逐字机械搬迁,函数体零逻辑改动。
含:_execute_batch/_enqueue_batch_job/run_batch_job/_resolve_filter(批量 job)、
fleet_power(每 host 一条聚合 SSM 启停全 VM)、_authorize_user_scope/_query_user_tenants
(per-user fleet 授权+GSI 查询)。

依赖方向:services → core(clients/utils/auth/audit) + services.tenant_service
(_execute_batch 调 delete_tenant/tenant_action)。services→services 横向,门允许
(只禁 routes/consumers/router)。

死结解法(scheduling/audit/tenant_service 验证过):测试重绑的 clients 符号
(hosts_table/tenants_table/batch_jobs_table/ssm/常量)走 clients.X 属性访问;
被调依赖函数走对应模块属性(auth.X/utils.X/audit.X/tenant_service.X)。
"""

import json
import os
import time

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr, Key

import core.clients as clients
from core.clients import API_KEY_OWNER, GSI_TENANT_USER
from core.utils import _resp, _now, _gen_id, _decode_next_token, _encode_next_token
import core.auth as auth
import core.audit as audit
import services.tenant_service as tenant_service


def _authorize_user_scope(tenant_user_id, event):
    """Decide whether the caller may manage the fleet of `tenant_user_id`.

    Returns None when allowed, else a 403/400 _resp. Policy (reuses the same
    identity layer as every other route):
      • admin / api-key caller       → allowed (external backend / trusted automation;
                                        this is also the identity every caller resolves
                                        to when RBAC is disabled → single-tenant plane)
      • federated user, own id       → allowed (a user manages only their own nodes)
      • otherwise                    → denied (no cross-user fleet access)
    #60 — key the cross-user guard off identity, not the RBAC_ENABLED flag, so
    disabling RBAC can never silently open one user's fleet to another.
    """
    if not tenant_user_id:
        return _resp(400, {"error": "tenant_user_id required"})
    ident = auth._get_caller_identity(event or {})
    if ident.get("is_admin"):
        return None
    caller_user = ident.get("tenant_user_id")
    if caller_user and caller_user == tenant_user_id:
        return None
    return _resp(403, {"error": "forbidden: not authorized for this user's fleet"})


def _query_user_tenants(
    tenant_user_id, limit=None, next_token=None, platform_scope=None
):
    """GSI-backed query for one user's tenants (indexed, never a full scan).

    Returns (items, next_token). Soft-deleted tenants are filtered out. Paginates
    via the gsi_tenant_user index; the cursor is opaque to callers.

    #108 IDOR fix: platform_scope(caller 的 platform 命名空间,None=未限定 admin)非空时
    只保留同 platform 的 tenant——与 list_tenants(handler.py:272)、_resolve_filter
    (fleet_service:456)同款 scope-first 结果过滤。否则 platform-scoped API key(解析成
    is_admin=True)能读任意其它 platform 用户的 fleet(_authorize_user_scope 的 is_admin
    分支不看 scope 就放行,这里在结果层补上隔离)。
    """
    kwargs = {
        "IndexName": GSI_TENANT_USER,
        "KeyConditionExpression": Key("tenant_user_id").eq(tenant_user_id),
        # exclude soft-deleted so the fleet view matches list_tenants semantics
        "FilterExpression": Attr("status").ne("deleted"),
    }
    if limit:
        kwargs["Limit"] = limit
    start_key = _decode_next_token(next_token)
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    out = clients.tenants_table.query(**kwargs)
    items = out.get("Items", []) or []
    if platform_scope is not None:
        items = [it for it in items if it.get("platform_id") == platform_scope]
    return items, _encode_next_token(out.get("LastEvaluatedKey"))


# ───────────── Fleet power: start/stop EVERY VM within 1 minute ─────────────
#
# GOAL: the control plane consumes 380 (×N hosts) openclaw start/stop within 1
# minute. The per-tenant path (batch_tenants → tenant_action → one SSM per VM)
# can't: SSM single-instance concurrency caps at ~5-10, so 380 commands serialize
# and 40 concurrent already TimedOut 11 (measured on 795). The fix is HOST-LEVEL
# fan-out: send ONE SSM command per host (start-all-vms.sh / stop-all-vms.sh),
# and each host starts/stops all its local VMs in bounded parallel. SSM
# concurrency then equals the number of HOSTS (single/low-double digits), not the
# number of VMs. A single send_command also takes a LIST of InstanceIds, so all
# hosts are dispatched in one API call — wall-clock ≈ slowest single host's local
# fan-out (stop is sub-second per VM; start boots FC), not a serial sum.
_FLEET_VALID_ACTIONS = {"start", "stop"}
# Host-local bounded parallelism (passed as the script's arg). Start is heavier
# (mount + skills cp + jq + FC boot). MEASURED (us-east-1 r8g.metal-24xl,
# 380 VMs, 2026-07-01): start wall-clock is FLAT ~50s across parallel 96/160/256
# — bottleneck is per-VM FC cold-boot, not fan-out width — so 96 (= vCPU count)
# is the sweet spot, higher doesn't help. Stop is sub-second/VM so it keeps 128.
_FLEET_START_PARALLEL = int(os.environ.get("FLEET_START_PARALLEL", "96"))
_FLEET_STOP_PARALLEL = int(os.environ.get("FLEET_STOP_PARALLEL", "128"))


def fleet_power(body=None, event=None):
    """POST /hosts/fleet-power — start or stop EVERY microVM across all active
    hosts, via one host-local fan-out SSM command per host.

    Body: {"action": "start"|"stop"}

    Admin-only: powering the whole fleet up/down is the highest-blast-radius
    control-plane op (affects every tenant on every host), so it requires admin
    even though RBAC already gated this route at operator (defense in depth, same
    pattern as the destructive paths at handler.py:1166/4255).

    Fire-and-forget: returns 202 + the per-host SSM CommandIds immediately. The
    host-agent reconcile loop + GET /hosts reflect the resulting state; we don't
    block the 29s API-GW window waiting for 380 VMs to settle.
    """
    # Admin gate (#60 companion) — same decoupling as `_assert_owner_or_admin`:
    # gate on identity only, not `RBAC_ENABLED`. Pre-#60 companion this read
    # `if RBAC_ENABLED and not ident.get("is_admin")`, so flipping RBAC off would
    # let any authenticated non-admin Cognito user POST /hosts/fleet-power and
    # stop EVERY microVM on every host (whole-fleet availability wipe — highest
    # blast radius in the control plane). When RBAC is off, `auth._get_caller_identity`
    # resolves the no-Bearer/api-key path to `is_admin=True`, so this gate still
    # short-circuits open for the trusted API-key single-tenant plane; genuine
    # non-admin Cognito callers stay locked out regardless of the flag.
    ident = auth._get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _resp(
            403,
            {"error": "forbidden: fleet-power requires admin", "required": "admin"},
        )
    body = json.loads(body) if isinstance(body, str) else (body or {})
    action = body.get("action")
    # isinstance guard first: a non-str action (list/dict from a malformed body)
    # would raise `unhashable type` on `in <set>` → uncaught 500 leaking internals.
    # Short-circuit to a clean 400 (边界输入严校验).
    if not isinstance(action, str) or action not in _FLEET_VALID_ACTIONS:
        return _resp(
            400, {"error": f"action must be one of {sorted(_FLEET_VALID_ACTIONS)}"}
        )

    # All hosts that can hold VMs (active or idle). Strong read so a host that
    # JUST registered isn't missed (control-plane consistency, Phase 6).
    hosts = clients.hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    ).get("Items", [])
    host_ids = [h["instance_id"] for h in hosts if h.get("instance_id")]
    if not host_ids:
        return _resp(200, {"action": action, "hosts": 0, "message": "no active hosts"})

    if action == "start":
        script = f"/home/ubuntu/start-all-vms.sh {_FLEET_START_PARALLEL}"
        # Per-host budget: 380 VMs × FC boot, bounded at _FLEET_START_PARALLEL.
        # 300s SSM execution timeout keeps a slow host from wedging the command.
        timeout = int(os.environ.get("FLEET_START_TIMEOUT", "300"))
    else:
        script = f"/home/ubuntu/stop-all-vms.sh {_FLEET_STOP_PARALLEL}"
        timeout = int(os.environ.get("FLEET_STOP_TIMEOUT", "120"))

    # ONE send_command for ALL hosts (SSM fans out to every InstanceId). This is
    # the crux: 1 API call, concurrency = host count, each host parallel-local.
    command_id = None
    try:
        wrapped = f"export HOME=/home/ubuntu && cd /home/ubuntu && {script}"
        resp = clients.ssm.send_command(
            InstanceIds=host_ids,
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
            # SSM's own concurrency control across the host list — start them all
            # at once (MaxConcurrency=100%); MaxErrors high so one bad host
            # doesn't abort the rest of the fleet.
            MaxConcurrency="100%",
            MaxErrors="100%",
        )
        command_id = resp["Command"]["CommandId"]
    except Exception as e:
        print(f"fleet-power SSM send error: {e}")
        return _resp(502, {"error": f"failed to dispatch fleet-power: {e}"})

    # DDB status reconciliation (loop 2026-07-01, 真机+代码抓出的一致性缺口):
    # fleet_power 只发 SSM 停/起 fc 进程 + 写/清 .stopped,不碰 tenant 表;而
    # host-agent 探测遇到 .stopped 的 VM 直接 continue(host-agent.py:262-264)不
    # 更新 DDB。结果 fleet-power stop 后租户 status 永远停在 running(console/
    # GET /tenants 显示假状态),start 后也不会从别的状态被纠正。这里在派发 SSM
    # 成功后批量把受影响 host 上所有非 deleted 租户的 status 对齐到目标态
    # (stop→stopped / start→running),让控制面状态与实际一致。best-effort:
    # 不因个别 update 失败而让整个 fleet-power 报错(SSM 已派发,状态最终会由
    # host-agent 对 running 的 VM 纠正;stopped 态靠这里写入)。
    # Only reconcile the STEADY-STATE pair: stop flips running→stopped, start
    # flips stopped→running. Transitional states (creating/pending/migrating/
    # paused/reset) are owned by their own flows and must NOT be clobbered —
    # e.g. a tenant mid-`creating` (VM still booting) caught by a concurrent
    # fleet-power stop must not be forced to `stopped`, or host-agent's
    # creating→running promotion races a bogus stopped write. So we gate on the
    # exact source state and add a ConditionExpression so the write only lands
    # if the row is STILL in that source state at update time (loses safely to a
    # concurrent promotion/lifecycle transition instead of overwriting it).
    if action == "stop":
        target_status, source_status = "stopped", "running"
    else:
        target_status, source_status = "running", "stopped"
    reconciled = 0
    try:
        _host_id_set = set(host_ids)
        _scan_kwargs = {
            "FilterExpression": "#s = :src",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":src": source_status},
        }
        _start_key = None
        while True:
            if _start_key:
                _scan_kwargs["ExclusiveStartKey"] = _start_key
            _out = clients.tenants_table.scan(**_scan_kwargs)
            for _t in _out.get("Items", []):
                # Only the steady source state on an affected host (re-checked in
                # Python so tests don't depend on the mock honoring the filter).
                if _t.get("status") != source_status:
                    continue
                if _t.get("host_id") not in _host_id_set:
                    continue
                try:
                    clients.tenants_table.update_item(
                        Key={"id": _t["id"]},
                        UpdateExpression="SET #s = :s, updated_at = :t",
                        # CAS: only flip if still in the source state — a
                        # concurrent promotion/stop that already moved it wins.
                        ConditionExpression="#s = :src",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={
                            ":s": target_status,
                            ":src": source_status,
                            ":t": _now(),
                        },
                    )
                    reconciled += 1
                except ClientError as _ce:
                    if (
                        _ce.response["Error"]["Code"]
                        != "ConditionalCheckFailedException"
                    ):
                        print(f"fleet-power status reconcile {_t.get('id')}: {_ce}")
                except Exception as _e:  # noqa: BLE001
                    print(f"fleet-power status reconcile {_t.get('id')}: {_e}")
            _start_key = _out.get("LastEvaluatedKey")
            if not _start_key:
                break
    except Exception as e:  # noqa: BLE001
        print(f"fleet-power status reconcile scan failed (non-fatal): {e}")

    audit._publish_event(
        f"fleet.{action}",
        "fleet",
        {"hosts": len(host_ids), "command_id": command_id, "reconciled": reconciled},
    )
    return _resp(
        202,
        {
            "action": action,
            "hosts": len(host_ids),
            "command_id": command_id,
            "reconciled": reconciled,
            "status": "dispatched",
            "message": (
                f"fan-out {action} dispatched to {len(host_ids)} host(s); "
                "each host powers its VMs in bounded parallel"
            ),
        },
    )


def _execute_batch(action, target_ids, event):
    """Run one action over a list of tenant ids; return (succeeded, failed).

    Shared by the synchronous batch path and the async worker so both enforce
    the SAME per-id ownership (#80, via the threaded event) and failure
    isolation. delete routes to delete_tenant; everything else to tenant_action.
    """
    succeeded, failed = [], []
    for tid in target_ids:
        try:
            tenant = clients.tenants_table.get_item(
                Key={"id": tid}, ConsistentRead=True
            ).get("Item")
            if not tenant:
                failed.append({"id": tid, "error": "tenant not found"})
                continue
            if action == "delete":
                # #263 — 批删削峰不在这里做:delete_tenant 入口已有入队短路(队列开+
                # 非 consumer 重放 → 入队返 202)。同步批删与 async worker(run_batch_job
                # 传 _caller_identity_memo,非 _consumer_ident)都命中它,逐 tid 秒入队,
                # consumer 受控并发消费。202<400 计入 succeeded,{succeeded,failed} 结构不变。
                # 空 query 保持批删现状语义(软删保盘);真删盘走单删 ?keep_data=false。
                result = tenant_service.delete_tenant(tid, {}, event)
            else:
                result = tenant_service.tenant_action(tid, action, None, event)
            if result.get("statusCode", 500) >= 400:
                err = json.loads(result.get("body", "{}")).get("error", "unknown error")
                failed.append({"id": tid, "error": err})
            else:
                succeeded.append({"id": tid, "action": action})
        except Exception as e:
            failed.append({"id": tid, "error": str(e)})
    return succeeded, failed


def _enqueue_batch_job(action, target_ids, event):
    """Record an async batch job and self-invoke the worker. Returns 202 + job_id.

    Idempotent by job_id (a re-submit with the same id is a no-op create). The
    caller's identity is captured into the job so the worker enforces the same
    ownership the synchronous path would (#56 — scale-out doesn't bypass RBAC).
    """
    job_id = _gen_id("batch")
    ident = auth._get_caller_identity(event or {})
    now = _now()
    # TTL: keep finished job rows for 7 days then auto-expire.
    expires_ttl = int(time.time()) + 7 * 24 * 3600
    item = {
        "job_id": job_id,
        "action": action,
        "ids": target_ids,
        "total": len(target_ids),
        "done": 0,
        "succeeded": [],
        "failed": [],
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "expires_ttl": expires_ttl,
        # capture the actor so the worker enforces the same ownership scope
        "actor_owner_id": ident.get("owner_id"),
        "actor_is_admin": bool(ident.get("is_admin")),
        "actor_tenant_user_id": ident.get("tenant_user_id"),
        # #108 — carry the platform scope so the async worker enforces the SAME
        # per-platform namespace as the synchronous path. Without this a scoped
        # key's `async:true` batch replays with scope=None + is_admin=True and
        # deletes/stops ANY platform's tenants (cross-platform IDOR + privilege
        # escalation). DDB drops None attributes → absent = unscoped, unchanged.
        "actor_platform_scope": ident.get("platform_scope"),
    }
    # idempotent create: don't clobber an existing job with the same id
    try:
        clients.batch_jobs_table.put_item(
            Item=item, ConditionExpression="attribute_not_exists(job_id)"
        )
    except Exception:
        pass  # already exists → fall through to returning the id
    # self-invoke the worker asynchronously (Event = fire-and-forget)
    try:
        lambda_client = boto3.client("lambda")
        lambda_client.invoke(
            FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            InvocationType="Event",
            Payload=json.dumps({"_batch_job": job_id}).encode("utf-8"),
        )
    except Exception as e:
        clients.batch_jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "dispatch_failed", ":t": _now()},
        )
        return _resp(
            500, {"error": f"failed to dispatch worker: {e}", "job_id": job_id}
        )
    return _resp(202, {"job_id": job_id, "status": "queued", "total": len(target_ids)})


def run_batch_job(job_id):
    """Async worker: execute a queued batch job in chunks, updating progress.

    Invoked via self-invoke ({"_batch_job": job_id}). Reconstructs the actor's
    identity from the job record so per-id ownership is enforced exactly like the
    synchronous path. Writes progress incrementally so GET /batch/jobs/{id} can
    report it; idempotent-ish (re-running a finished job just re-confirms).
    """
    if clients.batch_jobs_table is None:
        return {"statusCode": 503, "body": "batch jobs not configured"}
    job = clients.batch_jobs_table.get_item(Key={"job_id": job_id}).get("Item")
    if not job:
        return {"statusCode": 404, "body": "job not found"}
    if job.get("status") in ("done", "running"):
        return {"statusCode": 200, "body": f"job {job_id} already {job['status']}"}
    action = job["action"]
    target_ids = list(job.get("ids", []))
    # Rebuild a minimal event carrying the original actor so _execute_batch's
    # ownership checks (delete_tenant / tenant_action via event) see the same
    # identity. Memoize it so no token re-verify is attempted.
    synthetic_event = {
        "_caller_identity_memo": {
            "owner_id": job.get("actor_owner_id"),
            "role": "admin" if job.get("actor_is_admin") else "operator",
            "is_admin": bool(job.get("actor_is_admin")),
            "api_key_only": job.get("actor_owner_id") == API_KEY_OWNER,
            "tenant_user_id": job.get("actor_tenant_user_id"),
            # #108 — restore the platform scope so the worker's per-tenant
            # _assert_owner_or_admin enforces the same namespace as the sync path.
            "platform_scope": job.get("actor_platform_scope"),
        }
    }
    clients.batch_jobs_table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "running", ":t": _now()},
    )
    succeeded, failed = [], []
    CHUNK = 25  # flush progress every CHUNK ids so the status endpoint is live
    for i in range(0, len(target_ids), CHUNK):
        chunk = target_ids[i : i + CHUNK]
        s, f = _execute_batch(action, chunk, synthetic_event)
        succeeded.extend(s)
        failed.extend(f)
        clients.batch_jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET done = :d, succeeded = :s, failed = :f, updated_at = :t",
            ExpressionAttributeValues={
                ":d": len(succeeded) + len(failed),
                ":s": succeeded,
                ":f": failed,
                ":t": _now(),
            },
        )
    clients.batch_jobs_table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "done", ":t": _now()},
    )
    return {"statusCode": 200, "body": f"job {job_id} done"}


def _resolve_filter(flt, event=None):
    """Convert filter dict → list of matching tenant ids (excludes soft-deleted)."""
    items = (
        clients.tenants_table.scan(
            FilterExpression="#s <> :d",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": "deleted"},
        ).get("Items", [])
        or []
    )
    items = [it for it in items if it.get("status") != "deleted"]
    # issue #80 — owner scoping for non-admin batch callers.
    # #60 — key off identity, not RBAC_ENABLED (RBAC off → API_KEY_OWNER admin →
    # is_admin skips the filter; a real non-admin stays scoped regardless).
    # #108 — platform-scoped keys resolve is_admin=True but must NOT match the
    # whole fleet by filter (a scoped key would otherwise enumerate every
    # platform's tenant ids — they surface in the batch failed[] list — and, on
    # the async path, act on them). Filter to the caller's platform first, mirror
    # of list_tenants. Checked before the is_admin branch so scope wins.
    ident = auth._get_caller_identity(event or {})
    scope = ident.get("platform_scope")
    if scope is not None:
        items = [it for it in items if it.get("platform_id") == scope]
    elif not ident["is_admin"]:
        owner = ident["owner_id"]
        items = [it for it in items if owner and it.get("owner_id") == owner]
    tag_expr = flt.get("tag", "")
    if tag_expr and ":" in tag_expr:
        k, v = tag_expr.split(":", 1)
        items = [it for it in items if (it.get("tags") or {}).get(k) == v]
    elif tag_expr:
        items = []
    return [it["id"] for it in items]
