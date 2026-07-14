# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import hashlib
import os
import time
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError  # process_pending CAS 认领(#9 跨租户串修复)

from core.logging import logger, inject_trace_root, reset_invocation_keys


# ============================================================
# Groups CRUD (1.4.0 / #62)
# ============================================================


# ========== Skills CRUD (1.4.1 #63 — Console skills management) ==========
#
# Read/write SKILL.md content directly via API so the operator console
# can offer in-browser edit/upload/delete without requiring an AWS
# credentials shell. GET /skills (list) stays in the dedicated skills
# Lambda; the per-name CRUD lives here so we reuse the existing
# RBAC + audit-log infrastructure.


# issue #59 (WI-E/M-1) — config_template is caller-controlled and flows into an
# SSM root shell command; its ONLY legitimate use is as an S3 path slug
# (launch-vm.sh: s3://$ASSETS_BUCKET/templates/openclaw/${CONFIG_TEMPLATE}/openclaw.json),
# so it must be a plain DNS-label. Reject anything with shell metacharacters,
# whitespace, or path separators at the edge (defense in depth still quotes it
# in _launch_vm). Empty == "no custom template" and is validated separately.

# #93 idempotency key / #95 adversarial C-003/C-005/C-006 — client_token is a
# caller-supplied idempotency key that flows into an SSM command and log lines.
# Restrict to 4-128 printable ASCII (codepoints 33-126): no spaces, no control
# chars (\n \t \x00), no non-ASCII. .isascii() alone lets control chars through.

# ── #106 下单/购买语义(商业闭环)──────────────────────────────────────────
# 业务场景:用户在外部平台页面「下单购买一个 claw」。租户记录带三个购买维度字段
# (全部 ADDITIVE + optional,不带 = 与 #106 前字节一致的行为,严格向后兼容):
#   • order_id      外部平台订单号(计费/对账锚,#66/#68 spend 端点按它归集)。
#   • plan_tier     套餐档(free/standard/pro/enterprise 之一,受控枚举防脏数据)。
#   • purchase_status  两段式状态机:pending(下单意向已记,VM 未开通)→ provisioned
#                   (已开通,业务可用)。对齐 AWS SaaS Factory 的下单→provisioning
#                   状态机。注意这与 tenant.status(creating/running/stopped 生命周期)
#                   正交:status 是「VM 活着没」,purchase_status 是「这笔生意到哪步」。
# order_id 走 client_token 同款可打印 ASCII 校验(当前只落 DDB + 可能进 CloudWatch 日志行,
# 若未来进 SSM 命令拼接则此校验已就位;纵深防御,防注入/日志投毒);plan_tier 受控枚举;
# purchase_status 由服务端状态机管,不接受 create 直接塞任意值(只允许省略→默认 pending,或显式 pending)。
# \Z 而非 $:Python 的 $ 在 re.match 下也匹配「末尾换行符之前」,`"ord\n"` 会被 $ 放行
# (尾换行绕过校验,进日志/命令行做投毒)。\Z 只匹配字符串绝对末尾,堵掉这个注入面。


# #187 转型:C 端聊天不再走 claw-channel + hub 中枢签名路径。
# 前端直接 POST /ws/{tenant_id}/v1/chat/completions,SSE 流式,鉴权用租户 gateway
# token(GET /tenants/{id}/token 拿密文,调用方自解)。旧 chat_sign 路由 + claw-
# channel HMAC + hub relay 全部下线。参见 the dev plan G/D
# 与 the API spec.md 一、二节。


def lambda_handler(event, context):
    # #209: structured logging. Deliberately NOT using
    # @logger.inject_lambda_context — that decorator hard-derefs
    # context.function_name and crashes when context is None (137 test call
    # sites pass None, and it adds no correlation value we need). We manually
    # inject trace_root + the API GW request id, which is what cross-source
    # log correlation actually depends on.
    # Clear any per-invocation keys left over from a prior warm invocation
    # FIRST — the Logger is a reused singleton and append_keys is persistent,
    # so without this a warm container leaks tenant A's id onto a later
    # request that has no tenant path id (no-cross-tenant at the log layer).
    reset_invocation_keys()
    inject_trace_root()
    _rid = (event.get("requestContext") or {}).get("requestId")
    if _rid:
        logger.append_keys(request_id=_rid)
    # EventBridge: new host InService → process pending tenants
    if event.get("source") == "aws.autoscaling":
        detail_type = event.get("detail-type", "")
        if "terminate" in detail_type.lower():
            return cleanup_terminated_host(event)
        return process_pending()

    # PRD #54 — async batch worker: self-invoked with {"_batch_job": job_id}.
    # Not an HTTP request (no httpMethod) — handle before route dispatch.
    if event.get("_batch_job"):
        return run_batch_job(event["_batch_job"])

    # 控制面重构阶段1 — SQS lifecycle consumer。lifecycle 写操作(create/start/
    # stop/delete)入 SQS,本 Lambda 作为 consumer 被 SQS 触发(event.Records,
    # eventSource=aws:sqs)按受控并发(reserved concurrency)消费,削峰 + 限流阀 +
    # DLQ。把"1000/s 瞬时"摊成持续速率,治同步直驱 SSM 的雪崩(见 DESIGN-控制面重构)。
    # 报告 batchItemFailures:失败的消息留在队列退避重试,成功的不重复。
    if (
        isinstance(event.get("Records"), list)
        and event["Records"]
        and (event["Records"][0].get("eventSource") == "aws:sqs")
    ):
        # [hackathon] SQS dispatch consumer:eventSourceARN 含 "openclaw-dispatch"
        # 路由到装箱消费者;否则走既有 lifecycle FIFO consumer(向后兼容)。
        first_arn = event["Records"][0].get("eventSourceARN", "") or ""
        if "openclaw-dispatch" in first_arn:
            from consumers.dispatch import handle as _dispatch_consume  # noqa: E402

            return _dispatch_consume(event)
        return _consume_lifecycle_sqs(event["Records"])

    # [hackathon] EventBridge Poller — dispatch.poller source triggers the
    # in-flight SSM command polling loop (rate(1 minute)).
    if event.get("source") == "dispatch.poller":
        from services.dispatch_poller import poll_inflight as _poll  # noqa: E402

        return _poll()

    method = event["httpMethod"]
    resource = event["resource"]
    path_params = event.get("pathParameters") or {}

    # #209: structured logging — attach tenant_id to all subsequent log lines
    if path_params.get("id"):
        from core.logging import inject_tenant_id

        inject_tenant_id(path_params["id"])

    routes = {
        # issue #80 — `event` is threaded into per-tenant routes so they can
        # resolve the caller's owner identity and enforce owner==caller.
        ("GET", "/tenants"): lambda: list_tenants(
            event.get("queryStringParameters") or {},
            event.get("multiValueQueryStringParameters") or {},
            event,
        ),
        ("POST", "/tenants"): lambda: create_tenant(event.get("body"), event),
        # self-service: a logged-in user provisions their OWN node (viewer-level,
        # owner forced to caller, per-user cap). See create_tenant_self.
        ("POST", "/tenants/self"): lambda: create_tenant_self(event.get("body"), event),
        ("GET", "/tenants/{id}"): lambda: get_tenant(path_params["id"], event),
        ("DELETE", "/tenants/{id}"): lambda: delete_tenant(
            path_params["id"], event.get("queryStringParameters") or {}, event
        ),
        ("POST", "/tenants/{id}/{action}"): lambda: tenant_action(
            path_params["id"], path_params["action"], event.get("body"), event
        ),
        ("GET", "/tenants/{id}/{action}"): lambda: tenant_get_action(
            path_params["id"], path_params["action"], event
        ),
        ("GET", "/backups"): list_all_backups,
        ("POST", "/batch/tenants"): lambda: batch_tenants(event.get("body"), event),
        # PRD #54 — async batch job progress
        ("GET", "/batch/jobs/{job_id}"): lambda: get_batch_job(
            path_params["job_id"], event
        ),
        # PRD #50-58 — control-plane scale-out: manage a tenant user's whole fleet
        # of openclaw nodes by tenant_user_id (indexed query, pagination, bulk
        # start/stop) without k8s and without full-table scans.
        ("GET", "/users/{tenant_user_id}/tenants"): lambda: list_user_tenants(
            path_params["tenant_user_id"],
            event.get("queryStringParameters") or {},
            event,
        ),
        ("GET", "/users/{tenant_user_id}/summary"): lambda: user_summary(
            path_params["tenant_user_id"], event
        ),
        ("POST", "/users/{tenant_user_id}/action"): lambda: user_action(
            path_params["tenant_user_id"], event.get("body"), event
        ),
        # Go-live A1: external backend pushes the authoritative user↔tenant mapping.
        # Auth is HMAC (verified inside external_authz), NOT Cognito/RBAC — so it
        # must bypass the Cognito role gate (added to the RBAC skip list below).
        ("POST", "/external/authz"): lambda: external_authz(event.get("body"), event),
        # #187 转型:POST /chat/sign 下线,前端改经 /ws/{tenant_id} 直连 gateway。
        ("GET", "/hosts"): list_hosts,
        ("POST", "/hosts"): lambda: register_host(event.get("body")),
        ("POST", "/hosts/refresh-rootfs"): refresh_rootfs,
        # Fleet power: start/stop EVERY VM across all hosts via host-local fan-out
        # (1-minute fleet power goal). Admin-only (gated inside fleet_power).
        ("POST", "/hosts/fleet-power"): lambda: fleet_power(event.get("body"), event),
        ("GET", "/hosts/rootfs-version"): rootfs_version,
        ("GET", "/hosts/rootfs-drift"): rootfs_drift,
        # 10h-goal #19 — golden-image inventory. Per-tenant data snapshot is served
        # via GET /tenants/{id}/{action} with action=data (tenant_get_action).
        ("GET", "/images"): lambda: list_images(
            event.get("queryStringParameters") or {}
        ),
        ("GET", "/agentcore/status"): agentcore_status,
        ("GET", "/agentcore/tools"): agentcore_tools,
        ("GET", "/system/info"): system_info,
        # R10.2 — 只读队列深度(主队列 + DLQ 的 ApproximateNumberOfMessages),
        # 供 console SQS 面板 + DLQ 非零告警。只 get_queue_attributes,不 receive。
        ("GET", "/system/queues"): system_queues,
        # #97 档A — external-platform → Cognito upstream IdP routing lookup.
        ("GET", "/tenantmatch"): lambda: tenant_match(
            event.get("queryStringParameters") or {}
        ),
        ("GET", "/audit-log"): lambda: _list_audit_log(
            event.get("queryStringParameters") or {}, event
        ),
        ("DELETE", "/hosts/{instance_id}"): lambda: deregister_host(
            path_params["instance_id"]
        ),
        # 1.4.0 (#62) — per-tenant / per-group skill scoping
        ("GET", "/groups"): list_groups,
        ("POST", "/groups"): lambda: create_group(event.get("body")),
        ("POST", "/groups/{name}/skills"): lambda: add_skill_to_group(
            path_params["name"], event.get("body")
        ),
        ("DELETE", "/groups/{name}/skills/{skill}"): lambda: remove_skill_from_group(
            path_params["name"], path_params["skill"]
        ),
        # P4-③ (#187) — edge admin read-only endpoints (operator+)
        ("GET", "/admin/edge/instances"): list_edge_instances,
        ("GET", "/admin/edge/metrics"): list_edge_metrics,
        # 1.4.1 (#63) — Console skills CRUD
        ("GET", "/skills/{name}"): lambda: read_skill(path_params["name"]),
        ("PUT", "/skills/{name}"): lambda: update_skill(
            path_params["name"], event.get("body")
        ),
        ("DELETE", "/skills/{name}"): lambda: delete_skill(path_params["name"]),
        # Task 7.3 — tenant-credential-contract 新路由
        ("GET", "/tenants/{id}/credentials"): lambda: _get_tenant_credentials(
            path_params["id"], event
        ),
        ("GET", "/registry/{config_template}"): lambda: _get_registry(
            path_params["config_template"], event
        ),
        ("POST", "/registry/{config_template}"): lambda: _publish_registry(
            path_params["config_template"], event
        ),
        ("POST", "/registry/{config_template}/rollback"): lambda: _rollback_registry(
            path_params["config_template"], event
        ),
        ("GET", "/recipient-key"): lambda: _get_recipient_key(event),
        ("POST", "/recipient-key"): lambda: _register_recipient_key(event),
        ("POST", "/recipient-key/disable"): lambda: _disable_recipient_key(event),
        ("GET", "/clawpool-rsa-public-key"): lambda: _get_clawpool_rsa_public_key(),
    }

    handler = routes.get((method, resource))
    if not handler:
        return _resp(404, {"error": "not found"})
    # RBAC enforcement — checked AFTER routing so unknown paths still 404.
    forbidden = _rbac_check(event, method, resource)
    if forbidden is not None:
        return forbidden
    try:
        result = handler() if callable(handler) else handler
        # Issue #17 — audit-log mutating operations after they run so the
        # response_status is captured. GET requests skip auditing to avoid
        # noise; the audit-log route itself is read-only.
        if method in ("POST", "PUT", "DELETE"):
            _audit_write(method, resource, path_params, event, result)
        return result
    except Exception as e:
        import traceback

        traceback.print_exc()
        return _resp(500, {"error": str(e)})


# ========== Tenant Operations ==========


# Fields that are server-side secrets / credentials and MUST NEVER reach an API
# response (the chat UI calls GET /tenants with a Cognito Bearer; any field here
# would otherwise be handed to the browser). channel_secret is the HMAC key the
# hub verifies channel registration against — leaking it lets any logged-in user
# forge their node's channel registration (IDOR / credential leak). litellm_vkey
# is the per-tenant LLM billing key. Strip them from every outbound tenant record.


def list_tenants(query_params=None, multi_query_params=None, event=None):
    # PRD #53 — optional pagination. Backward compatible: no ?limit → scan to the
    # end and return a bare array (legacy shape small deployments rely on). With
    # ?limit=N → one page of ≤N rows + an opaque next_token, wrapped in an object
    # so a 100k-row table never blows the 30s API-GW timeout or the client.
    paginate = bool((query_params or {}).get("limit")) or bool(
        (query_params or {}).get("next_token")
    )
    scan_kwargs = {
        "FilterExpression": "#s <> :d",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":d": "deleted"},
    }
    if paginate:
        limit, err = _parse_limit(query_params)
        if err is not None:
            return err
        start_key, err = _parse_next_token((query_params or {}).get("next_token"))
        if err is not None:
            return err
        scan_kwargs["Limit"] = limit
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        out = tenants_table.scan(**scan_kwargs)
        items = out.get("Items", []) or []
        next_token = _encode_next_token(out.get("LastEvaluatedKey"))
    else:
        items = tenants_table.scan(**scan_kwargs).get("Items", [])
        next_token = None

    # issue #80 — owner scoping: a non-admin Cognito user sees only the tenants
    # they own. Admins and the API-key caller see everything. Records without
    # an owner_id (legacy / API-key-created) stay hidden from non-admins.
    # #60 — key off identity, not RBAC_ENABLED: when RBAC is off every caller is
    # the API_KEY_OWNER admin and is_admin skips the filter anyway, so scoping
    # can never be silently disabled by flipping the global flag.
    ident = _get_caller_identity(event or {})
    # #108 — a platform-scoped API key sees ONLY its own platform's tenants,
    # even though the key path resolves is_admin. Checked first so a scoped
    # key never enumerates the whole fleet (god-key list IDOR).
    scope = ident.get("platform_scope")
    if scope is not None:
        items = [it for it in items if it.get("platform_id") == scope]
    elif not ident["is_admin"]:
        owner = ident["owner_id"]
        items = [it for it in items if owner and it.get("owner_id") == owner]

    # Drop malformed/ghost rows: records with no status or no host assignment are
    # half-written failures or legacy debris (they render as blank "-" rows in the
    # console and pollute the list). A real tenant always has a status and a host_id.
    # The scan's "#s <> deleted" filter can't catch rows that have NO status
    # attribute at all (DynamoDB excludes them inconsistently), so enforce here.
    items = [
        it
        for it in items
        if it.get("status") and it.get("status") != "deleted" and it.get("host_id")
    ]

    # Ensure every record exposes a tags field so the console can render it
    for it in items:
        it.setdefault("tags", {})

    # Issue #10 — optional ?tag=key:value filter (AND across multiple)
    tag_filters = _collect_tag_filters(query_params, multi_query_params)
    if tag_filters:
        items = [it for it in items if _matches_all_tags(it, tag_filters)]

    # #106 — optional ?platform_id / ?purchase_status filters (exact match, AND
    # with owner scoping + tag filters). Lets a platform list only the tenants it
    # created ("按 platform_id + owner 筛租户"), or filter by purchase stage. Both
    # are validated so a bad query param is a 400, not a silent empty result.
    qp = query_params or {}
    pid_filter = qp.get("platform_id")
    if pid_filter is not None:
        if not _PLATFORM_ID_RE.match(pid_filter):
            return _err(
                400, "VALIDATION", "platform_id must be 1-128 chars [a-zA-Z0-9._-]"
            )
        items = [it for it in items if it.get("platform_id") == pid_filter]
    ps_filter = qp.get("purchase_status")
    if ps_filter is not None:
        if ps_filter not in (_PURCHASE_PENDING, _PURCHASE_PROVISIONED):
            return _err(
                400,
                "VALIDATION",
                f"purchase_status filter must be one of "
                f"['{_PURCHASE_PENDING}', '{_PURCHASE_PROVISIONED}']",
            )
        items = [it for it in items if it.get("purchase_status") == ps_filter]

    # Strip server-side secrets (channel_secret / litellm_vkey) before returning —
    # the chat UI calls this with a Cognito Bearer; secrets must stay server-side.
    items = [_redact_tenant(it) for it in items]

    if paginate:
        return _resp(
            200, {"tenants": items, "next_token": next_token, "count": len(items)}
        )
    return _resp(200, items)


def get_tenant(tenant_id, event=None):
    item = tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        return _resp(404, {"error": "tenant not found"})
    # issue #80 — IDOR: only the owner (or admin / api-key) may read the record.
    denied = _assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied
    item.setdefault("tags", {})
    # 1.4.0 (#62) — surface the resolved effective skill set to the caller.
    # None means "broadcast all" (legacy behavior); a list means scoping is
    # active and only those skills will be injected at next launch.
    eff = _resolve_effective_skills(item)
    item["effective_skills"] = eff if eff is not None else "*"
    # Strip server-side secrets before returning (see _redact_tenant).
    body = _redact_tenant(item)
    # #187 P1 — fold the KMS **ciphertext** of the pre-minted gateway token into
    # the status-poll response once the tenant is `running` (the data-plane contract
    # §5, per the data-plane contract review). Poll semantics: control-plane callers loop
    # GET /tenants/{id}; on `creating` they keep polling; on `running` they read
    # `gateway_token` (base64 ciphertext) out of this same response and decrypt
    # it locally with EncryptionContext={"tenant_id":<id>}. API Lambda does NOT
    # decrypt. This single tenant-details response is the sole way to fetch the
    # token ciphertext — the caller polls until `running`, then reads it here.
    if item.get("status") == "running":
        ct = _tenant_service.read_gateway_token_ct(tenant_id)
        if ct is not None:
            body["gateway_token"] = ct
        # #10 — fold WSS 设备三件套进就绪响应:device_id/public_key 明文,
        # private_key 是 KMS 密文(EncryptionContext=owner_id,调用方本地解密后签
        # WSS 握手帧),scopes 预授权档。paired.json 已冷注入镜像 → gateway
        # getPairedDevice 命中免界面 approve。None 时不加(feature off / 未铸 / 过窗)。
        device = _tenant_service.read_device_identity(tenant_id)
        if device is not None:
            body["device"] = device
    return _resp(200, body)


def _count_owner_tenants(owner_id):
    """Count a Cognito user's own non-deleted nodes via the gsi_owner index
    (no full-table scan). Used by self-service to enforce the per-user cap."""
    try:
        # Phase 6 note: GSI queries CANNOT use ConsistentRead (DynamoDB hard
        # limit — global secondary indexes are eventually consistent only). So
        # this per-user count can lag a just-created node by milliseconds; the
        # per-user cap tolerates that (worst case lets one extra node through a
        # tight race, re-checked on the next call). Do NOT add ConsistentRead
        # here — it raises ValidationException on an index query.
        out = tenants_table.query(
            IndexName=GSI_OWNER,
            KeyConditionExpression=Key("owner_id").eq(owner_id),
            FilterExpression=Attr("status").ne("deleted"),
            Select="COUNT",
        )
        return int(out.get("Count", 0))
    except Exception as e:
        # fail closed for a cap check: if we can't count, assume at-limit so we
        # don't let a user spin unlimited nodes during a DDB hiccup. LOG the real
        # cause — a silent except here once masked a missing gsi_owner index + a
        # missing index IAM permission, making every self-provision wrongly 409.
        # Never swallow this quietly again.
        print(
            f"[self-provision] _count_owner_tenants FAILED for owner={owner_id}: "
            f"{type(e).__name__}: {e} — failing closed (treat as at-limit). "
            f"Check gsi_owner index status + Lambda role dynamodb:Query on "
            f"table/openclaw-tenants/index/*."
        )
        return SELF_MAX_NODES_PER_USER if SELF_MAX_NODES_PER_USER else 0


def create_tenant_self(body=None, event=None):
    """POST /tenants/self — let a logged-in user provision their OWN openclaw
    node (self-service registration). Differs from POST /tenants (operator+):
      • ANY verified Cognito user may call it (viewer-level) — but ONLY for
        themselves: owner_id is forced to the caller's verified sub, the body
        cannot set owner/owner_id for someone else.
      • A per-user node cap (SELF_MAX_NODES_PER_USER, default 1) blocks abuse.
    Then it delegates to create_tenant so all the host-scheduling, vkey mint,
    skill scoping, etc. are identical. Returns create_tenant's 201/4xx.
    """
    ident = _get_caller_identity(event or {})
    sub = ident.get("owner_id")
    # must be a real, verified Cognito user (not the api-key automation path,
    # not an unverified token) — self-service is for end users provisioning
    # their own node.
    if not sub or ident.get("api_key_only") or sub == API_KEY_OWNER:
        return _resp(401, {"error": "self-service requires a logged-in user"})
    # When authority is external (external backend grants), self-provisioning by
    # the end user is not the model — the external backend decides who gets a node.
    # Refuse clearly.
    if EXTERNAL_AUTHZ:
        return _resp(
            403,
            {
                "error": "self-service disabled: tenant authority is external (externally granted)"
            },
        )
    # per-user cap (anti-abuse). 0 = unlimited.
    if SELF_MAX_NODES_PER_USER:
        n = _count_owner_tenants(sub)
        if n >= SELF_MAX_NODES_PER_USER:
            return _resp(
                409,
                {
                    "error": f"node limit reached ({n}/{SELF_MAX_NODES_PER_USER}); "
                    "delete an existing node or contact an admin to raise the limit.",
                },
            )
    # Build the create body: force a safe per-user default name if none given,
    # and never let the caller smuggle owner fields (create_tenant derives owner
    # from the verified identity anyway, but we strip defensively).
    body = json.loads(body) if isinstance(body, str) else (body or {})
    body.pop("owner_id", None)
    body.pop("owner", None)
    # #143 — the attribution override is api-key-only; create_tenant now rejects
    # a Bearer body carrying it (403), so strip it here like owner_id (a
    # self-service user's identity comes from the verified token, not the body).
    body.pop("tenant_user_id", None)
    if not body.get("name"):
        # short, DNS-safe, unique-ish per user; create_tenant appends a hash.
        body["name"] = f"u-{str(sub)[:8].lower().replace('_', '-')}"
    return create_tenant(body, event)


def external_authz(body_str, event):
    """POST /external/authz — the external backend writes the AUTHORITATIVE
    user↔tenant mapping (go-live A1). Authority is the HMAC signature (the external
    backend's shared secret), NOT a Cognito owner — so the external backend, not us,
    decides who may use which node. We just persist its decision into the tenant's
    authorized_users (our DDB = cache of the external backend's authority).

    Auth: header `x-claw-authz-signature` = HMAC-SHA256(secret, f"{timestamp}.{raw_body}"),
          header `x-claw-authz-timestamp` = unix seconds (±EXTERNAL_AUTHZ_TS_WINDOW).
    Body: { "tenant_id", "tenant_user_id"|"principal", "op": "grant"|"revoke",
            "role"?, "expire_at"? }. `principal` is the Cognito sub the hub will
    match; for federated users it's the sub mapped from tenant_user_id.
    """
    import hmac

    if not EXTERNAL_AUTHZ:
        return _resp(404, {"error": "external authz disabled"})
    if not EXTERNAL_AUTHZ_SECRET:
        return _resp(503, {"error": "external authz secret not configured"})
    headers = event.get("headers") or {}
    sig = (
        headers.get("x-claw-authz-signature")
        or headers.get("X-Claw-Authz-Signature")
        or ""
    ).strip()
    ts = (
        headers.get("x-claw-authz-timestamp")
        or headers.get("X-Claw-Authz-Timestamp")
        or ""
    ).strip()
    if not sig or not ts:
        return _resp(401, {"error": "missing signature/timestamp"})
    # timestamp window (replay protection)
    try:
        ts_num = int(ts)
    except (TypeError, ValueError):
        return _resp(401, {"error": "bad timestamp"})
    if abs(int(time.time()) - ts_num) > EXTERNAL_AUTHZ_TS_WINDOW:
        return _resp(401, {"error": "timestamp outside window"})
    raw = body_str or ""
    expected = hmac.new(
        EXTERNAL_AUTHZ_SECRET.encode("utf-8"),
        f"{ts}.{raw}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return _resp(401, {"error": "bad signature"})

    try:
        payload = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return _resp(400, {"error": "invalid json"})
    tenant_id = str(payload.get("tenant_id") or "").strip()
    principal = str(
        payload.get("principal") or payload.get("tenant_user_id") or ""
    ).strip()
    op = str(payload.get("op") or "grant").strip().lower()
    if not tenant_id or not principal:
        return _resp(
            400, {"error": "tenant_id and principal (or tenant_user_id) required"}
        )
    if op not in ("grant", "revoke"):
        return _resp(400, {"error": "op must be grant or revoke"})
    item = tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        return _resp(404, {"error": "tenant not found"})
    # Authority is the HMAC (external backend), so we DON'T require a Cognito owner here —
    # write the grant/revoke directly into authorized_users (the same map the hub
    # and control plane consult). This is the externalized write-authority.
    current = item.get("authorized_users")
    if not isinstance(current, dict):
        current = {}
    if op == "revoke":
        current.pop(principal, None)
    else:
        role = str(payload.get("role", "member")).strip() or "member"
        grant = {"role": role, "granted_at": _now(), "granted_by": "external-authz"}
        exp = payload.get("expire_at")
        if isinstance(exp, (int, float)) and exp > 0:
            grant["expire_at"] = int(exp)
        current[principal] = grant
    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET authorized_users = :a, updated_at = :t",
        ExpressionAttributeValues={":a": current, ":t": _now()},
    )
    _publish_event(
        "tenant.external_authz", tenant_id, {"principal": principal, "op": op}
    )
    return _resp(200, {"id": tenant_id, "op": op, "principal": principal})


def tenant_get_action(tenant_id, action, event=None):
    # issue #80 — IDOR: this exposes a tenant's backup list; gate on ownership.
    item = tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        return _resp(404, {"error": "tenant not found"})
    denied = _assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied
    if action == "backups":
        return list_backups(tenant_id)
    if action == "data":
        # 10h-goal #19 — per-tenant data snapshot (metadata only, zero-credential).
        return get_tenant_data(tenant_id, event)
    if action == "access":
        # List the explicit grant list (owner is implicit, shown for clarity).
        au = item.get("authorized_users")
        return _resp(
            200,
            {
                "id": tenant_id,
                "owner_id": item.get("owner_id"),
                "authorized_users": au if isinstance(au, dict) else {},
            },
        )
    return _resp(400, {"error": f"unknown GET action: {action}"})


# ========== Host Operations — moved to services/host_service.py (#132 T1.7) ==========


# ════════════════════════════════════════════════════════════
# System info — feature flags / config snapshot for the console
# ════════════════════════════════════════════════════════════
#
# The console's Settings tab wants to surface "is multi-AZ on?",
# "is metrics on?", "is WAF on?" etc. without parsing config.yml.
# We expose the relevant env-derived flags here so the UI can render
# accurate state without an out-of-band copy of config.yml.


def tenant_match(query_params=None):
    """GET /tenantmatch?platform_id=<id> — external-platform → Cognito IdP routing (#97 档A).

    Pre-login lookup: the browser calls this BEFORE any Cognito login to learn which
    upstream IdP (Cognito provider name) to federate to for a given external platform,
    then does federatedSignIn(customProvider=<idp_provider_name>). Read-only, leaks no
    tenant data — only the platform→IdP routing (internal design spec §2.7). Mirrors aws-samples/
    amazon-cognito-example-for-multi-tenant TenantAPI.ts:13-22 (there keyed by email
    domain; here by explicit platform_id).

    Returns 200 {platform_id, idp_provider_name} | 400 VALIDATION (bad/missing param)
    | 404 (federation not configured, or platform not registered → front-end falls
    back to passing identity_provider explicitly).
    """
    qp = query_params or {}
    platform_id = (qp.get("platform_id") or "").strip()
    if not platform_id:
        return _err(400, "VALIDATION", "platform_id query param required")
    if not _PLATFORM_ID_RE.match(platform_id):
        return _err(400, "VALIDATION", "platform_id must be 1-128 chars [a-zA-Z0-9._-]")
    if tenant_idp_table is None:
        return _err(404, "NOT_CONFIGURED", "external IdP federation not configured")
    try:
        item = tenant_idp_table.get_item(Key={"platform_id": platform_id}).get("Item")
    except Exception as e:  # fail-loud on real errors, don't pretend not-found
        return _err(502, "UPSTREAM", f"idp map lookup failed: {type(e).__name__}")
    if not item or not item.get("idp_provider_name"):
        return _err(404, "NOT_FOUND", f"no IdP registered for platform '{platform_id}'")
    # Return only routing fields (no secrets); issuer_url is public OIDC metadata.
    return _resp(
        200,
        {
            "platform_id": platform_id,
            "idp_provider_name": item["idp_provider_name"],
            "issuer_url": item.get("issuer_url", ""),
        },
    )


def system_info():
    """GET /system/info — feature flags + config snapshot for the console.

    Returns the subset of stack config the console needs to render
    Settings → Infrastructure: which optional features are enabled, and
    where to find their associated AWS resources (Grafana URL, SNS topic
    ARN, etc.). Values come from env vars wired in stack.py.
    """
    return _resp(
        200,
        {
            "version": os.environ.get("PROJECT_VERSION", "dev"),
            "region": os.environ.get("AWS_REGION", ""),
            "agentcore": {
                "enabled": os.environ.get("AGENTCORE_ENABLED", "false") == "true",
                "gateway_url": os.environ.get("AGENTCORE_GATEWAY_URL", "") or None,
            },
            # #234 — enabled reflects the config switch (METRICS_ENABLED), not
            # just the AMP path; backend tells the console which stack is live
            # (managed AMP/AMG vs self-hosted Prometheus/Grafana). grafana_url
            # is filled for whichever backend is deployed.
            "metrics": {
                "enabled": os.environ.get("METRICS_ENABLED", "false") == "true",
                "backend": os.environ.get("METRICS_BACKEND", "self-hosted"),
                "amp_remote_write_url": os.environ.get("AMP_REMOTE_WRITE_URL", "")
                or None,
                "grafana_url": os.environ.get("GRAFANA_WORKSPACE_URL", "")
                or os.environ.get("SELF_HOSTED_GRAFANA_URL", "")
                or None,
            },
            "multi_az": {
                "enabled": os.environ.get("MULTI_AZ_ENABLED", "false") == "true",
                "az_count": int(os.environ.get("MULTI_AZ_COUNT", "1") or "1"),
            },
            "waf": {"enabled": os.environ.get("WAF_ENABLED", "false") == "true"},
            "cognito": {
                # 1.2.9 fix: was checking COGNITO_USER_POOL_ID which is only
                # populated when console_auth.user_pool_id is *explicitly* set
                # in config.yml. The auto-created pool path leaves that env
                # empty even though Cognito IS deployed and the user is
                # actively logged in via OAuth — read CONSOLE_AUTH_ENABLED
                # (driven by config.yml console_auth.enabled) instead.
                "enabled": os.environ.get("CONSOLE_AUTH_ENABLED", "false") == "true",
                "user_pool_id": os.environ.get("COGNITO_USER_POOL_ID", "") or None,
                # 1.5.4: RBAC is an independent switch from login — a deployment can require Cognito login without enforcing per-route role checks.
                "rbac_enabled": RBAC_ENABLED,
            },
            "notifications": {
                "enabled": bool(NOTIFICATIONS_TOPIC_ARN),
                "topic_arn": NOTIFICATIONS_TOPIC_ARN or None,
            },
            "quotas": {
                "enabled": QUOTAS_ENABLED,
                "max_vcpu_per_tenant": QUOTAS_MAX_VCPU,
                "max_mem_mb_per_tenant": QUOTAS_MAX_MEM_MB,
                "max_data_disk_mb": QUOTAS_MAX_DATA_DISK_MB,
            },
            "host_config": {
                "cpu_overcommit_ratio": CPU_OVERCOMMIT_RATIO,
                "mem_overcommit_ratio": MEM_OVERCOMMIT_RATIO,
                "vm_default_vcpu": VM_DEFAULT_VCPU,
                "vm_default_mem_mb": VM_DEFAULT_MEM,
            },
        },
    )


def _queue_depth(url):
    """一个队列的即时深度:{visible, not_visible}。只 get_queue_attributes(只读计数,
    不 receive、不碰消息体,R13.3 不泄租户数据)。url 空或出错 → None(fail-soft,不 500)。"""
    if not url or sqs is None:
        return None
    try:
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        ).get("Attributes", {})
        return {
            "visible": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "not_visible": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        }
    except Exception as e:  # noqa: BLE001 — 只读观测端点,单队列失败返 null 不拖垮整体
        print(f"queue depth read failed for {url}: {e}")
        return None


def system_queues():
    """GET /system/queues — 主队列 + DLQ 的即时深度(R10.2)。console SQS 面板 + DLQ
    非零告警用。纯只读:只 get_queue_attributes 取计数,绝不 receive_message(不暴露消息
    体/租户数据,R13.3)。每个队列 fail-soft 返 null,一个读不到不影响其它。DLQ 深度 >0
    时前端标告警色(有租户创建/生命周期动作进死信,需人工看)。"""
    dispatch_dlq = _queue_depth(DISPATCH_DLQ_URL)
    lifecycle_dlq = _queue_depth(LIFECYCLE_DLQ_URL)
    dlq_total = (dispatch_dlq or {}).get("visible", 0) + (lifecycle_dlq or {}).get(
        "visible", 0
    )
    return _resp(
        200,
        {
            "dispatch": {
                "main": _queue_depth(DISPATCH_QUEUE_URL),
                "dlq": dispatch_dlq,
            },
            "lifecycle": {
                "main": _queue_depth(LIFECYCLE_QUEUE_URL),
                "dlq": lifecycle_dlq,
            },
            # 汇总告警位:DLQ 有任何可见消息即 true(前端标红,不必逐队列算)
            "dlq_alarm": dlq_total > 0,
        },
    )


def get_tenant_data(tenant_id, event=None):
    """GET /tenants/{id}/data — a tenant's own data snapshot for the console (10h
    -goal #19: 查看 openclaw 数据). Returns the control-plane's view of the
    tenant: lifecycle status, host/guest placement, resource spec, skill scope,
    schedule/TTL, billing-vkey presence (boolean, never the value), and backup
    count. IDOR-guarded (owner/admin only). Zero-credential: reads the DDB record
    + counts S3 backups; it does NOT pull guest secrets or sensitive file
    contents — operators view metadata, the agent's private data stays in-VM."""
    item = tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        return _resp(404, {"error": "tenant not found"})
    denied = _assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied
    # count backups for this tenant (S3 list, read-only)
    backup_count = 0
    bucket = os.environ.get("ASSETS_BUCKET", "")
    bprefix = os.environ.get("BACKUP_PREFIX", "backups")
    if bucket:
        try:
            out = s3.list_objects_v2(Bucket=bucket, Prefix=f"{bprefix}/{tenant_id}/")
            backup_count = out.get("KeyCount", 0)
        except Exception:
            backup_count = -1  # unknown
    eff = _resolve_effective_skills(item)
    return _resp(
        200,
        {
            "id": tenant_id,
            "status": item.get("status"),
            "host_id": item.get("host_id"),
            "guest_ip": item.get("guest_ip"),
            "vm_num": item.get("vm_num"),
            "vcpu": item.get("vcpu"),
            "mem_mb": item.get("mem_mb"),
            "data_disk_mb": item.get("data_disk_mb"),
            "rootfs_version": item.get("rootfs_version"),
            "effective_skills": eff if eff is not None else "*",
            "group": item.get("group"),
            "schedule": item.get("schedule"),
            "ttl_hours": item.get("ttl_hours"),
            "expires_at": item.get("expires_at"),
            "owner_id": item.get("owner_id"),
            "tenant_user_id": item.get("tenant_user_id"),
            # presence only — NEVER the value (zero-credential surface)
            "has_billing_vkey": bool(item.get("litellm_vkey")),
            "backup_count": backup_count,
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "tags": item.get("tags", {}),
        },
    )


# ========== Pending Tenant Processing ==========


def process_pending():
    """Called when a new host becomes InService. Assign pending tenants to available hosts."""
    pending = tenants_table.scan(
        FilterExpression="#s = :p",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":p": "pending"},
    ).get("Items", [])

    if not pending:
        return {"statusCode": 200, "body": "no pending tenants"}

    pending.sort(key=lambda x: x.get("created_at", ""))

    assigned = 0
    for tenant in pending:
        vcpu = int(tenant["vcpu"])
        mem_mb = int(tenant["mem_mb"])

        # Allocate vm_num + reserve capacity ATOMICALLY via CAS — same fix as
        # create_tenant._reserve_slot (tenant_service.py, GITHUB-scheduler-bugs P0).
        # 反模式(修复前,并发跨租户串数据 CRITICAL):读 host["next_vm_num"] → 算
        # guest_ip/host_port → 无条件 `SET next_vm_num = :next`。两个并发 api_fn 实例
        # (EventBridge HostReady 触发,api_fn 无 reserved concurrency)都选同一 least-loaded
        # host、读到同一 next_vm_num → 两个不同租户落同一 vm_num/guest_ip/host_port →
        # 同 tap/同 DNAT 端口/同 /30 guest_ip,C 端流量跨租户互串;且绝对写吞掉并发增量致
        # 账本漂移。修:CAS 原子(a)确认 next_vm_num 自读取未变(b)写时复检容量不超卖
        # (c)一次条件写自增 next_vm_num/used_*;竞争(CCF)则重选 host 重试。
        vm_num = None
        host = None
        for _attempt in range(8):
            cand = _find_host(vcpu, mem_mb)
            if not cand:
                break
            expected = int(cand.get("next_vm_num", 1))
            cap_v = int(int(cand["total_vcpu"]) * CPU_OVERCOMMIT_RATIO) - vcpu
            cap_m = int(int(cand["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO) - mem_mb
            try:
                r = hosts_table.update_item(
                    Key={"instance_id": cand["instance_id"]},
                    UpdateExpression=(
                        "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
                        "vm_count = vm_count + :one, next_vm_num = next_vm_num + :one, "
                        "#s = :active REMOVE idle_since"
                    ),
                    ConditionExpression=(
                        "next_vm_num = :expected AND used_vcpu <= :cap_v "
                        "AND used_mem_mb <= :cap_m"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":v": vcpu,
                        ":m": mem_mb,
                        ":one": 1,
                        ":active": "active",
                        ":expected": expected,
                        ":cap_v": cap_v,
                        ":cap_m": cap_m,
                    },
                    ReturnValues="UPDATED_NEW",
                )
                # 认领成功:claimed vm_num 是自增前的值。
                vm_num = int(r["Attributes"]["next_vm_num"]) - 1
                host = cand
                break
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    continue  # 输了 CAS(容量满/next_vm_num 变了)→ 重选 host 重试
                raise
        if host is None or vm_num is None:
            break  # 无容量或持续竞争 → 停,剩余 pending 下次 tick 再处理

        guest_ip = _guest_ip(vm_num)
        host_port = VM_PORT_BASE + vm_num - 1
        now = _now()

        # Update pending tenant with host assignment (host slot 已 CAS 占好)
        tenants_table.update_item(
            Key={"id": tenant["id"]},
            UpdateExpression="SET #s = :s, host_id = :h, vm_num = :n, guest_ip = :g, host_port = :p, rootfs_version = :rv, creation_started_at = :t, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "creating",
                ":h": host["instance_id"],
                ":n": vm_num,
                ":g": guest_ip,
                ":p": host_port,
                ":rv": host.get("rootfs_version", ""),
                ":t": now,
            },
        )

        _launch_vm(
            host["instance_id"],
            tenant["id"],
            vm_num,
            vcpu,
            mem_mb,
            guest_ip,
            host_port,
            tenant.get("config_template", ""),
            tenant.get("restore_backup_key", ""),
            scoped_skills=_resolve_effective_skills(tenant),
            litellm_vkey=tenant.get("litellm_vkey", ""),  # task #15
            # mint-up-front secret persisted at create time (kills handshake race)
            channel_secret=tenant.get("channel_secret", ""),
        )
        # #187 转型:数据面走两级路由(ALB LOR → OpenResty edge → Redis 查表 → host
        # DNAT → microVM:18789);per-tenant ALB rule/TG 死路径已下线。
        assigned += 1

    return {
        "statusCode": 200,
        "body": f"assigned {assigned}/{len(pending)} pending tenants",
    }


# #93 / api-design-review F4 — per-tenant encryption/security config.
# Named nested Map (S3 ServerSideEncryptionConfiguration pattern), NOT a flat
# `env` blob: `env` is AWS-reserved for environment variables; these fields have
# inter-dependencies (a KMS key only makes sense once encryption is on), so they
# belong in one cohesive object. ARNs, not bare ids (a bare KMS id/alias resolves
# to the wrong key cross-account); XxxArn suffix per IAM convention. secret_ref
# holds a Secrets Manager ARN (a reference), never the secret VALUE. All five are
# references/config, not secrets — safe to store plaintext and echo (IAM: an ARN
# is "not considered secret"). Only put a secret's *content* into
# _TENANT_SECRET_FIELDS, and never store content here.


## ── ALB path-based routing ──


# ── Tag helpers (issue #10) ──

# Limits chosen to keep DynamoDB items small and avoid colon-conflict with the
# `?tag=k:v` query syntax. AWS resource tags use the same 50/256 model; we cap
# values at 100 chars (more than enough for typical labels) to be conservative.


# ═══════════════════════════════════════════════════════════════════════════
# Helpers restored after the v1.0.0-milestone-q2-2026 cross-PR merge.
# Issue #48 tracks the rationale: each helper was added by an early PR but
# lost when later PRs auto-resolved merge conflicts with `-X theirs`.
# Sources noted alongside each block. — fix/post-merge-regression
# ═══════════════════════════════════════════════════════════════════════════

# ----- TTL (#28 / issue #15, original 47158d2) -----


# ----- Schedule (#30 / issue #11, original af9434b). Validation only — the
# scaler's _schedule_should_run lives in deploy/lambda/scaler/handler.py.


# ----- Audit log (#32 / issue #17, original 96d7496) -----
# audit_table is defined above (top of module). No re-binding needed here —
# the post-merge regression repair (#48) accidentally re-declared it; the
# top-of-module definition is authoritative.


def _audit_write(method, resource, path_params, event, result):
    """Best-effort audit-log writer; failures must NEVER break the API."""
    if audit_table is None:
        return
    try:
        import uuid, time as _t

        path_params = path_params or {}
        resource_id = path_params.get("id") or path_params.get("instance_id") or ""
        api_key_id = (event.get("requestContext") or {}).get("identity", {}).get(
            "apiKeyId"
        ) or (event.get("headers") or {}).get("x-api-key", "")[:32]
        # Issue #80 follow-up — record the *actor* (Cognito sub + role), not just
        # the api_key_id. Without this, a Bearer-token (Cognito user) mutation is
        # untraceable to a specific person: api_key_id is empty on that path. The
        # owner_id is the stable principal RBAC already authorizes on; logging it
        # closes the "who did it" gap for incident review.
        ident = _get_caller_identity(event)
        actor_owner_id = ident.get("owner_id") or ""
        actor_role = ident.get("role") or ""
        # Auto-prune via DynamoDB TTL: 90-day retention.
        expires_ttl = int(_t.time()) + 90 * 86400
        audit_table.put_item(
            Item={
                "pk": "audit",
                "id": str(uuid.uuid4()),
                "ts": _now(),
                "operation": f"{method} {resource}",
                "resource_id": resource_id,
                "api_key_id": api_key_id,
                "actor_owner_id": actor_owner_id,
                "actor_role": actor_role,
                "response_status": result.get("statusCode")
                if isinstance(result, dict)
                else None,
                "expires_ttl": expires_ttl,
            }
        )
    except Exception as e:
        print(f"audit_write failed: {e}")


GSI_AUDIT_OWNER = "gsi_audit_owner"


def _list_audit_log(query_params, event=None):
    """GET /audit-log — return recent audit entries, newest first.

    Optional query params:
        limit  — int (default 50, max 500)
        since  — ISO-8601 timestamp; only entries >= this are returned
        owner  — verified admin (Bearer + cognito:groups=admin) may target
                 another owner's trail; any other caller has this ignored and
                 is clamped to their own owner_id (never echoes somebody
                 else's trail even when passed in the query string).

    Ownership scoping — layered decision (safety on top of speed):

    Trust tier (#61 · read-side IDOR fix; kept as the top guard):
      • verified Cognito admin (Bearer, RS256-verified, is_admin AND NOT
        api_key_only) → may see everyone (owner_scope=None) or target one
        owner via `?owner=…`
      • api-key admin (no-Bearer path with is_admin=True but api_key_only=True)
        — the single `openclaw-admin-key` is sed-baked into the C-end chat
        page (setup.sh → window.OC_API_KEY) and served to every end user, so
        this identity is effectively public. Clamped down to its own
        API_KEY_OWNER records, NOT the whole partition. (#61 HIGH follow-up.)
      • non-admin with a resolvable owner_id → clamped to self
      • unauthenticated / owner_id=None (RBAC on) → 403 fail-closed

    Speed path (#32 · GSI acceleration; grafted onto the trust tier):
      When `owner_scope` is resolved (either verified-admin's explicit
      `?owner=…` or the clamped self value), we query gsi_audit_owner
      (partition=actor_owner_id, sort=ts) so per-tenant reads don't scan the
      whole pk="audit" partition. The GSI is orthogonal to owner enforcement
      — it never widens what a caller may see, only speeds up the scoped
      lookup. Missing-GSI deploys (worm_archive_enabled=false) fall back to
      pk-partition + in-app owner filter.
    """
    if audit_table is None:
        return _resp(200, [])
    qp = query_params or {}
    try:
        # Clamp into [1, 500]: DynamoDB rejects Limit <= 0, and a negative/zero
        # limit from a hostile query string must not reach boto3 (or, worse,
        # skip the paginator's `len(items) < limit` guard on the owner path).
        limit = max(1, min(int(qp.get("limit", 50)), 500))
    except (TypeError, ValueError):
        limit = 50
    since = qp.get("since")
    requested_owner = qp.get("owner") or None
    from boto3.dynamodb.conditions import Key, Attr

    # ---- trust tier (#61): resolve owner_scope BEFORE consulting `?owner=` ---
    ident = _get_caller_identity(event or {})
    if ident["is_admin"] and not ident.get("api_key_only"):
        # verified Cognito admin: may target any owner, or (default) no filter.
        owner_scope = requested_owner  # None → full partition
    elif ident["owner_id"] is None:
        # Untrusted / unverified token — deny rather than leak the partition.
        return _resp(403, {"error": "forbidden"})
    else:
        # non-admin (incl. api_key_only=True): silently clamp to own owner_id;
        # `?owner=` from the caller is discarded so we NEVER echo somebody
        # else's trail even when it was in the query string.
        owner_scope = ident["owner_id"]

    # ---- speed path (#32): GSI-direct query when owner_scope is known ----
    items = []
    try:
        if owner_scope is not None:
            # GSI query by actor_owner_id (per-tenant fast path). Owner is
            # enforced at the KEY level, so DynamoDB cannot return foreign
            # rows even if the caller reached this via `?owner=X` — the trust
            # tier above already gated who is allowed to name a foreign X.
            key_cond = Key("actor_owner_id").eq(owner_scope)
            if since:
                key_cond = key_cond & Key("ts").gte(since)
            items = audit_table.query(
                IndexName=GSI_AUDIT_OWNER,
                KeyConditionExpression=key_cond,
                ScanIndexForward=False,  # newest first
                Limit=limit,
            ).get("Items", [])
        else:
            # verified admin, no owner filter: scan the pk="audit" partition.
            key_cond = Key("pk").eq("audit")
            if since:
                key_cond = key_cond & Key("ts").gte(since)
            items = audit_table.query(
                KeyConditionExpression=key_cond,
                ScanIndexForward=False,
                Limit=limit,
            ).get("Items", [])
    except Exception as e:
        # A missing GSI (worm_archive_enabled=false deploy) falls back to the
        # pk-partition query + application-level owner filter — the owner_scope
        # decision from the trust tier above is what enforces "no foreign rows",
        # so this fallback is safe even without the index.
        msg = str(e)
        if "gsi_audit_owner" in msg or "index" in msg.lower():
            try:
                key_cond = Key("pk").eq("audit")
                if since:
                    key_cond = key_cond & Key("ts").gte(since)
                # Owner-scoped fallback: DynamoDB applies FilterExpression AFTER
                # Limit, so single-page may return < limit of the caller's rows.
                # Paginate up to 20 pages (matches #61's pattern) to fill limit.
                query_kwargs = {
                    "KeyConditionExpression": key_cond,
                    "ScanIndexForward": False,  # newest first
                    "Limit": limit if owner_scope is None else 500,
                }
                if owner_scope is not None:
                    query_kwargs["FilterExpression"] = Attr("actor_owner_id").eq(
                        owner_scope
                    )
                items = []
                pages = 0
                last_key = None
                while len(items) < limit and pages < 20:
                    if last_key:
                        query_kwargs["ExclusiveStartKey"] = last_key
                    resp = audit_table.query(**query_kwargs)
                    page_items = resp.get("Items", [])
                    # Belt-and-suspenders: DynamoDB SHOULD apply
                    # FilterExpression server-side, but keep an in-app filter
                    # too so a broken engine / mocked test / older-boto3 can
                    # never leak foreign rows through this fallback path.
                    if owner_scope is not None:
                        page_items = [
                            i
                            for i in page_items
                            if i.get("actor_owner_id") == owner_scope
                        ]
                    items.extend(page_items)
                    last_key = resp.get("LastEvaluatedKey")
                    pages += 1
                    if not last_key:
                        break
            except Exception as e2:
                print(f"audit fallback query failed: {e2}")
                items = []
        else:
            print(f"audit query failed: {e}")
            items = []
    return _resp(200, items[:limit])


# ----- Quota (#34 / issue #9, original 79000fa) -----
# QUOTAS_ENABLED / QUOTAS_MAX_* are defined at the top of the module
# (default disabled, matches README "enabled: false default — no checks").
# The post-merge regression repair (#48) accidentally re-declared them with
# a different default; that re-declaration has been removed.


# ----- SNS lifecycle notifications (#33 / issue #13, original 1f1bffa) -----
# _publish_event 已搬进 core/audit.py(#132 handler-split);facade 别名见文件底部。
# NOTIFICATIONS_TOPIC_ARN / sns 仍在 core/clients.py 定义。


# ===== Control-plane scale-out: per-user fleet management (PRD #50-58) =====
# Manage thousands of openclaw microVMs by the tenant user that owns them,
# without a k8s control plane and without full-table scans. The tenant record
# already carries the user association (tenant_user_id / owner_id, written at
# create_tenant); these GSIs make "all nodes of a user" an indexed query.

_USER_ACTION_VALID = {"start", "stop"}  # per-user bulk lifecycle actions


def list_user_tenants(tenant_user_id, query_params=None, event=None):
    """GET /users/{tenant_user_id}/tenants — indexed, paginated fleet listing (#50/#51/#53)."""
    denied = _authorize_user_scope(tenant_user_id, event)
    if denied is not None:
        return denied
    limit, err = _parse_limit(query_params)
    if err is not None:
        return err
    next_token = (query_params or {}).get("next_token")
    _, err = _parse_next_token(next_token)  # reject tampered/garbage cursor loud
    if err is not None:
        return err
    # #108 IDOR fix — platform-scoped key 只看自己 platform 的 fleet(_authorize_user_scope
    # 的 is_admin 分支不看 scope 就放行,这里在查询结果层补隔离)。
    _scope = _get_caller_identity(event or {}).get("platform_scope")
    items, new_token = _query_user_tenants(
        tenant_user_id, limit=limit, next_token=next_token, platform_scope=_scope
    )
    # Strip server-side secrets before returning — gsi_tenant_user is
    # ProjectionType.ALL so items carry channel_secret / litellm_vkey /
    # gateway_token / cognito_channel_password. GET /tenants (:398) and
    # get_tenant (:424) both redact; this per-user fleet route was the #100
    # (gateway_token leak) sibling that got missed — one x-api-key / scoped
    # caller could batch-harvest every credential of the nodes it lists.
    redacted = [_redact_tenant(it) for it in items]
    for it in redacted:
        it.setdefault("tags", {})
    return _resp(
        200, {"tenants": redacted, "next_token": new_token, "count": len(redacted)}
    )


def user_summary(tenant_user_id, event=None):
    """GET /users/{tenant_user_id}/summary — node count + per-status buckets (#57).

    Read-only roll-up for a backend dashboard / reconciliation. Pages through the
    GSI internally (projection is small enough; we only keep status) so the count
    is exact even past one page, but bounds the work to avoid an unbounded loop.
    """
    denied = _authorize_user_scope(tenant_user_id, event)
    if denied is not None:
        return denied
    _scope = _get_caller_identity(event or {}).get("platform_scope")  # #108 IDOR fix
    by_status = {}
    total = 0
    next_token = None
    pages = 0
    while True:
        items, next_token = _query_user_tenants(
            tenant_user_id,
            limit=_USER_PAGE_MAX,
            next_token=next_token,
            platform_scope=_scope,
        )
        for it in items:
            st = it.get("status", "unknown")
            by_status[st] = by_status.get(st, 0) + 1
            total += 1
        pages += 1
        # safety bound: 1000/page × 50 pages = 50k nodes per user is far beyond
        # any real case; stop rather than loop unboundedly on a pathological set.
        if not next_token or pages >= 50:
            break
    return _resp(
        200,
        {
            "tenant_user_id": tenant_user_id,
            "total": total,
            "by_status": by_status,
            "truncated": bool(next_token),
        },
    )


def user_action(tenant_user_id, body=None, event=None):
    """POST /users/{tenant_user_id}/action {action:start|stop} — bulk lifecycle (#52/#56).

    Applies one lifecycle action to EVERY node the user owns. The target set comes
    from the GSI (not a client-supplied id list), so the backend says "stop this
    user" and we resolve their nodes. Reuses tenant_action per node (same RBAC +
    SSM + audit + event path); failures are isolated into a failed[] list.
    """
    denied = _authorize_user_scope(tenant_user_id, event)
    if denied is not None:
        return denied
    body = json.loads(body) if isinstance(body, str) else (body or {})
    action = body.get("action")
    if action not in _USER_ACTION_VALID:
        return _resp(
            400, {"error": f"action must be one of {sorted(_USER_ACTION_VALID)}"}
        )
    # Resolve the full fleet (page through the GSI; bounded like user_summary).
    _scope = _get_caller_identity(event or {}).get("platform_scope")  # #108 IDOR fix
    target_ids, next_token, pages = [], None, 0
    while True:
        items, next_token = _query_user_tenants(
            tenant_user_id,
            limit=_USER_PAGE_MAX,
            next_token=next_token,
            platform_scope=_scope,
        )
        target_ids.extend(it["id"] for it in items if it.get("id"))
        pages += 1
        if not next_token or pages >= 50:
            break
    succeeded, failed = [], []
    for tid in target_ids:
        try:
            result = tenant_action(tid, action, None, event)
            if result.get("statusCode", 500) >= 400:
                err = json.loads(result.get("body", "{}")).get("error", "unknown error")
                failed.append({"id": tid, "error": err})
            else:
                succeeded.append({"id": tid, "action": action})
        except Exception as e:
            failed.append({"id": tid, "error": str(e)})
    return _resp(
        200,
        {
            "tenant_user_id": tenant_user_id,
            "action": action,
            "succeeded": succeeded,
            "failed": failed,
            "truncated": bool(next_token),
        },
    )


# ----- Batch tenant operations (#29 / issue #23, original d05e107) -----
_BATCH_VALID_ACTIONS = {"stop", "start", "delete", "backup"}
_BATCH_VALID_FILTER_KEYS = {"tag"}
_BATCH_MAX_IDS = 100


def batch_tenants(body=None, event=None):
    """POST /batch/tenants — apply one action to many tenants in a single call."""
    if body is None:
        return _resp(400, {"error": "missing body"})
    # 坏 JSON → 400 不 500(裸 json.loads 会抛 ValueError 冒泡顶层 500 泄内部错);
    # 合法但非对象(list/数字)→ 400(否则下面 body.get 抛 AttributeError 同样 500)。
    try:
        body = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return _resp(400, {"error": "invalid json"})
    if not isinstance(body, dict):
        return _resp(400, {"error": "body must be a JSON object"})
    action = body.get("action")
    if action not in _BATCH_VALID_ACTIONS:
        return _resp(
            400, {"error": f"action must be one of {sorted(_BATCH_VALID_ACTIONS)}"}
        )
    ids = body.get("ids")
    flt = body.get("filter")
    if ids is not None and flt is not None:
        return _resp(400, {"error": "specify exactly one of 'ids' or 'filter'"})
    if ids is None and flt is None:
        return _resp(400, {"error": "specify exactly one of 'ids' or 'filter'"})
    if ids is not None:
        if not isinstance(ids, list):
            return _resp(400, {"error": "ids must be an array"})
        # PRD #54 — the >_BATCH_MAX_IDS ceiling is no longer a hard reject here;
        # large lists route to the async job path below (or 400 with a hint if
        # the async ledger isn't deployed). Hard upper bound to bound a single
        # request's memory/cost.
        if len(ids) > 100_000:
            return _resp(400, {"error": "too many ids (max 100000 per request)"})
        target_ids = list(ids)
    else:
        if not isinstance(flt, dict):
            return _resp(400, {"error": "filter must be an object"})
        unknown = set(flt.keys()) - _BATCH_VALID_FILTER_KEYS
        if unknown:
            return _resp(400, {"error": f"unknown filter key(s): {sorted(unknown)}"})
        # issue #80 — scope filter resolution to the caller so a non-admin's
        # batch never even sees tenants they don't own.
        target_ids = _resolve_filter(flt, event)

    # PRD #54 — async mode. A large batch (>_BATCH_MAX_IDS) or an explicit
    # `async:true` is recorded as a job and run by a self-invoked worker, so the
    # client gets 202 + job_id instead of a synchronous call that would exceed
    # the 30s API-GW timeout. Small synchronous batches keep the old immediate
    # behavior so existing callers are untouched.
    want_async = bool(body.get("async")) or len(target_ids) > _BATCH_MAX_IDS
    if want_async:
        if batch_jobs_table is None:
            # async requested but the ledger isn't deployed → fail loudly rather
            # than silently truncating to a sync batch.
            if len(target_ids) > _BATCH_MAX_IDS:
                return _resp(
                    503,
                    {"error": "async batch not configured (BATCH_JOBS_TABLE absent)"},
                )
        else:
            return _enqueue_batch_job(action, target_ids, event)

    if len(target_ids) > _BATCH_MAX_IDS:
        return _resp(
            400, {"error": f"too many ids (max {_BATCH_MAX_IDS}); use async:true"}
        )
    succeeded, failed = _execute_batch(action, target_ids, event)
    return _resp(200, {"succeeded": succeeded, "failed": failed})


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
# Host-local bounded parallelism (passed as the script's arg). Start is heavier
# (mount + skills cp + jq + FC boot). MEASURED (us-east-1 r8g.metal-24xl,
# 380 VMs, 2026-07-01): start wall-clock is FLAT ~50s across parallel 96/160/256
# — bottleneck is per-VM FC cold-boot, not fan-out width — so 96 (= vCPU count)
# is the sweet spot, higher doesn't help. Stop is sub-second/VM so it keeps 128.


# ───────────── 控制面重构阶段1:SQS lifecycle 队列(削峰 + consumer) ─────────────


# enqueue_lifecycle 已搬进 services/lifecycle_dispatch.py(#132 阶段3 解依赖环);
# facade 别名见文件底部。放 services 层是为断开 tenant_service→consumers 反向依赖环。


def _consume_lifecycle_sqs(records):
    """SQS consumer:逐条消费 lifecycle 消息,复用现有 create/tenant_action/delete。

    返回 {"batchItemFailures": [...]} — 失败的消息留队列退避重试(maxReceiveCount
    后进 DLQ),成功的不重复。consumer 的 reserved concurrency 是限流阀(削峰)。
    """
    failures = []
    for rec in records:
        mid = rec.get("messageId")
        try:
            msg = json.loads(rec.get("body") or "{}")
            action = msg.get("action")
            tid = msg.get("tenant_id")
            extra = msg.get("extra") or {}
            # 重建最小 event 让下游 owner 检查(#56/#80)生效
            ident = msg.get("_ident") or {}
            ev = {"_consumer_ident": ident}
            if action == "create":
                # create:extra 带 create_tenant 所需 body(name/vcpu/owner 等)
                result = create_tenant(extra, ev)
                # Emit end-to-end create latency (enqueue → provisioned) so the
                # 1-minute SLA is measured, not assumed. Best-effort; parse-safe.
                try:
                    enq = (extra or {}).get("_enqueued_at")
                    if enq:
                        from datetime import datetime, timezone

                        enq_dt = datetime.fromisoformat(enq)
                        waited = (
                            datetime.now(timezone.utc).timestamp() - enq_dt.timestamp()
                        )
                        if waited >= 0:
                            _emit_create_latency(waited)
                except Exception:  # noqa: BLE001
                    pass
            elif action == "delete":
                result = delete_tenant(tid, {}, ev)
            else:
                result = tenant_action(tid, action, extra or None, ev)
            code = result.get("statusCode", 500) if isinstance(result, dict) else 200
            if code >= 500:
                # 5xx(SSM throttle / 容量争用)→ 留队列退避重试
                failures.append({"itemIdentifier": mid})
            # 4xx(owner/参数错)不重试:消息消费掉,避免毒消息无限重投
        except Exception as e:  # noqa: BLE001
            print(f"[lifecycle-consumer] msg {mid} error: {type(e).__name__}: {e}")
            failures.append({"itemIdentifier": mid})
    return {"batchItemFailures": failures}


def get_batch_job(job_id, event=None):
    """GET /batch/jobs/{job_id} — async batch progress (#54)."""
    if batch_jobs_table is None:
        return _resp(503, {"error": "batch jobs not configured"})
    job = batch_jobs_table.get_item(Key={"job_id": job_id}).get("Item")
    if not job:
        return _resp(404, {"error": "job not found"})
    # don't echo the raw ids list (can be huge); report progress + results
    return _resp(
        200,
        {
            "job_id": job["job_id"],
            "action": job.get("action"),
            "status": job.get("status"),
            "total": job.get("total", 0),
            "done": job.get("done", 0),
            "succeeded": job.get("succeeded", []),
            "failed": job.get("failed", []),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        },
    )


# #93 / api-design-review E1+E2 — structured error code. AWS Exceptions standard:
# clients MUST be able to distinguish errors in code without parsing the free-text
# message. `_err` attaches a stable machine-readable `code` alongside the existing
# `error` string. ADDITIVE + backward-compatible: old callers that read only
# `error` still work; the message text stays free to change, `code` is the contract.
# Prefer `_err(4xx, "CODE", "text")` over `_resp(4xx, {"error": "text"})` for new
# error returns; existing _resp error sites can migrate opportunistically.


# ────────────────────────────────────────────────────────────
# Live VM resize (#35 / issue #16, original b3d48cf)
# ────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# handler-split #132 Phase1 — core 分层包 facade。
# 上面这些符号已搬入 core/*.py(逐字不变)。此处 re-export 保持:
#   ① 旧 import 路径(handler.<symbol>)全程有效
#   ② 测试 patch.object(handler, "<symbol>") 仍命中(留在 handler 的调用方读 handler 全局)
# 新代码应 from core.<domain> import <symbol>。facade 在测试全迁移后删。
import os as _os_split, sys as _sys_split

_here = _os_split.path.dirname(_os_split.path.abspath(__file__))
if _here not in _sys_split.path:
    _sys_split.path.insert(0, _here)

# 测试用 importlib.util.spec_from_file_location 反复重新 exec handler.py(每个
# 测试文件/reload 一套自己的 boto3 mock)。core.clients 在模块级建 boto3 client +
# DDB 表句柄,若 sys.modules 里残留上一次 exec 的 core.*,facade 会 alias 到旧 mock
# → 跨文件污染。故每次 exec handler 先 evict core.* 子模块,让 core 在当前 exec 的
# boto3 mock 下重新初始化(生产只 import 一次,首次无可 evict,无副作用)。
for _m in [
    _k
    for _k in _sys_split.modules
    if _k in ("core", "services", "routes", "consumers")
    or _k.startswith("core.")
    or _k.startswith("services.")
    or _k.startswith("routes.")
    or _k.startswith("consumers.")
]:
    _sys_split.modules.pop(_m, None)

# T1.0 — core/clients:共享 boto3 client / DDB 表句柄 / env 常量 / 条件建 sqs。
from core import clients as _clients  # noqa: E402

ssm = _clients.ssm
s3 = _clients.s3
asg_client = _clients.asg_client
sns = _clients.sns
ddb = _clients.ddb
elbv2 = _clients.elbv2
tenants_table = _clients.tenants_table
hosts_table = _clients.hosts_table
GSI_OWNER = _clients.GSI_OWNER
GSI_TENANT_USER = _clients.GSI_TENANT_USER
groups_table = _clients.groups_table
audit_table = _clients.audit_table
batch_jobs_table = _clients.batch_jobs_table
tenant_idp_table = _clients.tenant_idp_table
AUDIT_TTL_DAYS = _clients.AUDIT_TTL_DAYS
NOTIFICATIONS_TOPIC_ARN = _clients.NOTIFICATIONS_TOPIC_ARN
HOST_RESERVED_VCPU = _clients.HOST_RESERVED_VCPU
HOST_RESERVED_MEM = _clients.HOST_RESERVED_MEM
CPU_OVERCOMMIT_RATIO = _clients.CPU_OVERCOMMIT_RATIO
MEM_OVERCOMMIT_RATIO = _clients.MEM_OVERCOMMIT_RATIO
VM_DEFAULT_VCPU = _clients.VM_DEFAULT_VCPU
VM_DEFAULT_MEM = _clients.VM_DEFAULT_MEM
VM_DATA_DISK_MB = _clients.VM_DATA_DISK_MB
VM_PORT_BASE = _clients.VM_PORT_BASE
VM_SUBNET_PREFIX = _clients.VM_SUBNET_PREFIX
ASG_NAME = _clients.ASG_NAME
ALB_LISTENER_ARN = _clients.ALB_LISTENER_ARN
VPC_ID = _clients.VPC_ID
# #187 转型:ENABLE_PER_TENANT_ALB_RULE + legacy_alb 已下线;per-tenant ALB rule/TG 全删。
LIFECYCLE_QUEUE_URL = _clients.LIFECYCLE_QUEUE_URL
sqs = _clients.sqs
CREATE_VIA_QUEUE = _clients.CREATE_VIA_QUEUE
QUOTAS_ENABLED = _clients.QUOTAS_ENABLED
QUOTAS_MAX_VCPU = _clients.QUOTAS_MAX_VCPU
QUOTAS_MAX_MEM_MB = _clients.QUOTAS_MAX_MEM_MB
QUOTAS_MAX_DATA_DISK_MB = _clients.QUOTAS_MAX_DATA_DISK_MB
SELF_MAX_NODES_PER_USER = _clients.SELF_MAX_NODES_PER_USER
BALLOON_ENABLED = _clients.BALLOON_ENABLED
COGNITO_USER_POOL_ID = _clients.COGNITO_USER_POOL_ID
COGNITO_CLIENT_ID = _clients.COGNITO_CLIENT_ID
COGNITO_REGION = _clients.COGNITO_REGION
COGNITO_CHANNEL_CLIENT_ID = _clients.COGNITO_CHANNEL_CLIENT_ID
DEFAULT_NO_JWT_ROLE = _clients.DEFAULT_NO_JWT_ROLE
RBAC_ENABLED = _clients.RBAC_ENABLED
EXTERNAL_AUTHZ = _clients.EXTERNAL_AUTHZ
EXTERNAL_AUTHZ_SECRET = _clients.EXTERNAL_AUTHZ_SECRET
EXTERNAL_AUTHZ_TS_WINDOW = _clients.EXTERNAL_AUTHZ_TS_WINDOW
API_KEY_OWNER = _clients.API_KEY_OWNER
# [hackathon] SQS dispatch env re-export (SPEC/specs/sqs-dispatch/interfaces.md)
DISPATCH_QUEUE_URL = _clients.DISPATCH_QUEUE_URL
DISPATCH_MODE = _clients.DISPATCH_MODE
DISPATCH_RETRY_BUDGET = _clients.DISPATCH_RETRY_BUDGET
# R10.2 — DLQ URLs(只读队列深度用;CDK 注入,未配则空,端点 fail-soft 返 null)
DISPATCH_DLQ_URL = os.environ.get("DISPATCH_DLQ_URL", "")
LIFECYCLE_DLQ_URL = os.environ.get("LIFECYCLE_DLQ_URL", "")

from core import utils as _utils  # noqa: E402

_ENCRYPTION_TYPES = _utils._ENCRYPTION_TYPES
_ARN_RE = _utils._ARN_RE
_TAG_MAX_KEY_LEN = _utils._TAG_MAX_KEY_LEN
_TAG_MAX_VALUE_LEN = _utils._TAG_MAX_VALUE_LEN
_TAG_MAX_COUNT = _utils._TAG_MAX_COUNT
_NAME_RE = _utils._NAME_RE
_PLATFORM_ID_RE = _utils._PLATFORM_ID_RE
_TTL_MAX_HOURS = _utils._TTL_MAX_HOURS
_TTL_VALID_ON_EXPIRY = _utils._TTL_VALID_ON_EXPIRY
_SCHED_DAYS = _utils._SCHED_DAYS
_USER_PAGE_DEFAULT = _utils._USER_PAGE_DEFAULT
_USER_PAGE_MAX = _utils._USER_PAGE_MAX
_gen_id = _utils._gen_id
_validate_security = _utils._validate_security
_validate_injected_credentials = _utils._validate_injected_credentials
_now = _utils._now
_validate_name = _utils._validate_name
_validate_tags = _utils._validate_tags
_collect_tag_filters = _utils._collect_tag_filters
_matches_all_tags = _utils._matches_all_tags
_parse_ttl = _utils._parse_ttl
_parse_schedule = _utils._parse_schedule
_encode_next_token = _utils._encode_next_token
_decode_next_token = _utils._decode_next_token
_parse_limit = _utils._parse_limit
_parse_next_token = _utils._parse_next_token
_resp = _utils._resp
_err = _utils._err

# T1.2 — core/vkey:per-tenant LiteLLM 计费虚拟密钥(常量/缓存/4 函数)。
from core import vkey as _vkey  # noqa: E402

LITELLM_MASTER_KEY_SECRET = _vkey.LITELLM_MASTER_KEY_SECRET
LITELLM_HOST_SSM = _vkey.LITELLM_HOST_SSM
TENANT_DEFAULT_BUDGET = _vkey.TENANT_DEFAULT_BUDGET
TENANT_DEFAULT_RPM = _vkey.TENANT_DEFAULT_RPM
_get_litellm_base_url = _vkey._get_litellm_base_url
_get_litellm_master_key = _vkey._get_litellm_master_key
_mint_tenant_vkey = _vkey._mint_tenant_vkey
_revoke_tenant_vkey = _vkey._revoke_tenant_vkey

# T1.3 — core/ssm_dispatch:VM 生命周期 SSM 下发(4 函数;_ssm_run 被 14 处调)。
from core import ssm_dispatch as _ssm_dispatch  # noqa: E402

_launch_vm_wake_cmd = _ssm_dispatch._launch_vm_wake_cmd
_launch_vm = _ssm_dispatch._launch_vm
_ssm_send = _ssm_dispatch._ssm_send
_ssm_run = _ssm_dispatch._ssm_run

# T1.4 — core/skills:每租户/每组 skill 分发解析(1 函数)。
from core import skills as _skills  # noqa: E402

_resolve_effective_skills = _skills._resolve_effective_skills

# #187 转型:T1.6 core/legacy_alb 全模块下线(数据面改两级路由,per-tenant ALB
# rule/TG 死代码彻底不用;handler / tenant_service / host_service 调用点已删)。

# T1.5 — core/scheduling:host 选择/容量预留回滚/ASG 扩容/配额检查(5 函数)。
# 调用点全在 handler 内(create_tenant 等),facade 别名即在 handler.__globals__,
# 测试 patch.object(api, "_find_host"/...) 精确命中,无需改调用点。
from core import scheduling as _scheduling  # noqa: E402

_scale_out = _scheduling._scale_out
_release_slot = _scheduling._release_slot
_find_host = _scheduling._find_host
_get_specific_host_with_capacity = _scheduling._get_specific_host_with_capacity
_check_quota = _scheduling._check_quota

# T1.6 — core/audit:SNS 生命周期事件发布(1 函数;9 处调用点全在 handler 内)。
# _audit_write/_list_audit_log 暂留(横向依赖 auth / 归 routes,待评审拍板)。
from core import audit as _audit  # noqa: E402

_publish_event = _audit._publish_event

# T1.7 — services/host_service:host 注册/注销/清理 + rootfs 镜像清单/刷新/漂移(10 函数)。
from services import host_service as _host_service  # noqa: E402

list_hosts = _host_service.list_hosts
_SIZE_TO_VCPU = _host_service._SIZE_TO_VCPU
_FAMILY_LETTER_TO_MEM_PER_VCPU = _host_service._FAMILY_LETTER_TO_MEM_PER_VCPU
_resolve_instance_memory_mb = _host_service._resolve_instance_memory_mb
register_host = _host_service.register_host
deregister_host = _host_service.deregister_host
cleanup_terminated_host = _host_service.cleanup_terminated_host
rootfs_version = _host_service.rootfs_version
rootfs_drift = _host_service.rootfs_drift
_get_manifest = _host_service._get_manifest
list_images = _host_service.list_images
refresh_rootfs = _host_service.refresh_rootfs

# T1.8 — services/console_info:控制台只读端点(备份清单 + AgentCore 工具清单,4 函数)。
from services import console_info as _console_info  # noqa: E402

list_backups = _console_info.list_backups
list_all_backups = _console_info.list_all_backups
agentcore_status = _console_info.agentcore_status
agentcore_tools = _console_info.agentcore_tools
_AGENTCORE_BUILTIN_TOOLS = _console_info._AGENTCORE_BUILTIN_TOOLS

# 阶段3 解依赖环 — services/lifecycle_dispatch:enqueue_lifecycle 入 SQS(1 函数)。
# 放 services 层断开 tenant_service→consumers 反向依赖(见该文件头注释)。
from services import lifecycle_dispatch as _lifecycle_dispatch  # noqa: E402

enqueue_lifecycle = _lifecycle_dispatch.enqueue_lifecycle

# 阶段2 — services/tenant_service:租户 CRUD + 生命周期动作 + resize/access/backup(8 函数)。
# 逐字机械搬迁,行为零改动。调用点(routes/router/consumers)读 handler 全局裸名 → facade
# 别名仍命中,现有 patch.object(handler,"create_tenant"/…) 无需改。被搬走的域私有常量
# (_PURCHASE_PENDING/_PURCHASE_PROVISIONED 仍被留在 handler 的 list_tenants 用;其余仅域内用)
# 也在此 re-export,旧的 handler.<const> 引用路径全程有效。
from services import tenant_service as _tenant_service  # noqa: E402

create_tenant = _tenant_service.create_tenant
delete_tenant = _tenant_service.delete_tenant
tenant_action = _tenant_service.tenant_action
tenant_access_grant = _tenant_service.tenant_access_grant
tenant_resize = _tenant_service.tenant_resize
# #187 P1 — gateway-token mint helper (tests) + read helper used by get_tenant
# to fold the ciphertext into the GET /tenants/{id} poll response.
mint_gateway_token = _tenant_service.mint_gateway_token
read_gateway_token_ct = _tenant_service.read_gateway_token_ct
_resolve_backup = _tenant_service._resolve_backup
_validate_purchase = _tenant_service._validate_purchase
_redact_tenant = _tenant_service._redact_tenant
_CONFIG_TEMPLATE_RE = _tenant_service._CONFIG_TEMPLATE_RE
_CLIENT_TOKEN_RE = _tenant_service._CLIENT_TOKEN_RE
_ORDER_ID_RE = _tenant_service._ORDER_ID_RE
_PLAN_TIERS = _tenant_service._PLAN_TIERS
_PURCHASE_PENDING = _tenant_service._PURCHASE_PENDING
_PURCHASE_PROVISIONED = _tenant_service._PURCHASE_PROVISIONED
_TENANT_SECRET_FIELDS = _tenant_service._TENANT_SECRET_FIELDS

# 阶段2 — services/fleet_service:批量 job + fleet-power + per-user fleet(7 函数 + 3 FLEET 常量)。
# 依赖 tenant_service(_execute_batch 调 delete_tenant/tenant_action),放其后。
from services import fleet_service as _fleet_service  # noqa: E402

# P4-③ (#187) — edge admin read-only endpoints (list_edge_instances / list_edge_metrics)
from services import edge_admin as _edge_admin  # noqa: E402

list_edge_instances = _edge_admin.list_edge_instances
list_edge_metrics = _edge_admin.list_edge_metrics

_authorize_user_scope = _fleet_service._authorize_user_scope
_query_user_tenants = _fleet_service._query_user_tenants
fleet_power = _fleet_service.fleet_power
_execute_batch = _fleet_service._execute_batch
_enqueue_batch_job = _fleet_service._enqueue_batch_job
run_batch_job = _fleet_service.run_batch_job
_resolve_filter = _fleet_service._resolve_filter
_FLEET_VALID_ACTIONS = _fleet_service._FLEET_VALID_ACTIONS
_FLEET_START_PARALLEL = _fleet_service._FLEET_START_PARALLEL
_FLEET_STOP_PARALLEL = _fleet_service._FLEET_STOP_PARALLEL

# T1.1 — core/auth:身份验证 / RBAC / IDOR 所有权(13 函数)。
# auth 域试点(design.md):被 handler 域函数调用的 auth 符号(_get_caller_identity 等),
# 其调用方在 handler 名字空间读裸名 → facade 别名仍命中,现有 patch(handler,"X") 无需改;
# 只有 auth 内部互调(如 _assert_owner_or_admin 调 _get_caller_identity)那条路,测试若
# 同时 patch 了该符号又走内部函数,才需改 patch(api._auth,"X")。
# #187 P5 — Cognito 渠道机器用户 helper 随 channel/hub 一起下线,facade 同步移除。
from core import auth as _auth  # noqa: E402

_guest_ip = _auth._guest_ip
_emit_create_latency = _auth._emit_create_latency
_get_jwks_client = _auth._get_jwks_client
_verify_and_decode = _auth._verify_and_decode
_tenant_user_id_from_claims = _auth._tenant_user_id_from_claims
_platform_id_from_claims = _auth._platform_id_from_claims
_role_from_claims = _auth._role_from_claims
_get_user_role = _auth._get_user_role
_role_satisfies = _auth._role_satisfies
_get_caller_identity = _auth._get_caller_identity
_assert_owner_or_admin = _auth._assert_owner_or_admin
_rbac_check = _auth._rbac_check
_ROLE_RANK = _auth._ROLE_RANK
_VIEWER_OK = _auth._VIEWER_OK
_RBAC_SKIP = _auth._RBAC_SKIP

# handler-split #132 — routes/skills_groups facade
from routes import skills_groups as _skills_groups  # noqa: E402

_SKILL_NAME_RE = _skills_groups._SKILL_NAME_RE
_SKILL_MAX_BYTES = _skills_groups._SKILL_MAX_BYTES
list_groups = _skills_groups.list_groups
create_group = _skills_groups.create_group
add_skill_to_group = _skills_groups.add_skill_to_group
remove_skill_from_group = _skills_groups.remove_skill_from_group
read_skill = _skills_groups.read_skill
update_skill = _skills_groups.update_skill
delete_skill = _skills_groups.delete_skill


# ── Task 7.3: tenant-credential-contract 路由 handler ──────────────────────────


def _get_tenant_credentials(tenant_id, event):
    """GET /tenants/{id}/credentials — 出站凭据子资源(R7.1);event 透传做 #80 owner 门"""
    from services.tenant_service import get_tenant_credentials

    return get_tenant_credentials(tenant_id, event)


def _get_registry(config_template, event):
    """GET /registry/{config_template} — 读当前 registry 快照(admin-only)"""
    # admin gate — identity-based like fleet_power (works for api-key admin path;
    # requestContext.authorizer.role is never populated in this stack, no custom authorizer)
    if not _get_caller_identity(event or {}).get("is_admin"):
        return _err(403, "ACCESS_DENIED", "registry management requires admin role")
    from services.registry_service import load_current_snapshot

    try:
        version, entries = load_current_snapshot(config_template)
    except LookupError as e:
        return _err(404, "NOT_FOUND", str(e))
    return _resp(
        200,
        {"config_template": config_template, "version": version, "entries": entries},
    )


def _publish_registry(config_template, event):
    """POST /registry/{config_template} — 发布新快照(admin-only)"""
    # admin gate — identity-based like fleet_power (works for api-key admin path;
    # requestContext.authorizer.role is never populated in this stack, no custom authorizer)
    if not _get_caller_identity(event or {}).get("is_admin"):
        return _err(403, "ACCESS_DENIED", "registry publish requires admin role")
    import json as _json

    body = event.get("body")
    if isinstance(body, str):
        body = _json.loads(body)
    if not body or not isinstance(body.get("entries"), dict):
        return _err(400, "VALIDATION", "body.entries must be an object")
    from services.registry_service import publish_snapshot

    new_version = publish_snapshot(config_template, body["entries"])
    return _resp(200, {"config_template": config_template, "version": new_version})


def _rollback_registry(config_template, event):
    """POST /registry/{config_template}/rollback — 回滚到指定版本(admin-only)"""
    # admin gate — identity-based like fleet_power (works for api-key admin path;
    # requestContext.authorizer.role is never populated in this stack, no custom authorizer)
    if not _get_caller_identity(event or {}).get("is_admin"):
        return _err(403, "ACCESS_DENIED", "registry rollback requires admin role")
    import json as _json

    body = event.get("body")
    if isinstance(body, str):
        body = _json.loads(body)
    version = (body or {}).get("version")
    if not isinstance(version, int):
        return _err(400, "VALIDATION", "body.version must be an integer")
    from services.registry_service import rollback

    try:
        rollback(config_template, version)
    except Exception as e:
        return _err(400, "VALIDATION", f"rollback failed: {e}")
    return _resp(200, {"config_template": config_template, "rolled_back_to": version})


def _get_clawpool_rsa_public_key():
    """GET /clawpool-rsa-public-key — #149 asymmetric-v1 入站用。

    返回 ClawPool RSA CMK 的公钥(PEM),供外部调用方本地 OAEP-SHA256 加密 env 凭据,
    再以 enc:v1: 信封发 POST /tenants(env_injected_credentials)。私钥不出 KMS,只有
    host 在 VM launch 时 kms:Decrypt。此端点只读公钥(kms:GetPublicKey),不涉私钥。
    """
    import base64
    from core import clients

    arn = clients.CLAWPOOL_RSA_CMK_ARN
    if not arn:
        return _err(
            404,
            "NOT_FOUND",
            "asymmetric-v1 not enabled (no RSA CMK; security.clawpool_cmk_enabled off)",
        )
    try:
        resp = clients.kms.get_public_key(KeyId=arn)
    except Exception as e:  # noqa: BLE001
        return _err(502, "UPSTREAM", f"kms:GetPublicKey failed: {e}")
    der_b64 = base64.b64encode(resp["PublicKey"]).decode()
    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        + "\n".join(der_b64[i : i + 64] for i in range(0, len(der_b64), 64))
        + "\n-----END PUBLIC KEY-----\n"
    )
    return _resp(
        200,
        {
            "key_id": arn,
            "key_spec": resp.get("KeySpec", "RSA_4096"),
            "algorithm": "RSAES_OAEP_SHA_256",
            "public_key_pem": pem,
            "envelope_hint": "enc:v1:1:RSA_4096_OAEP_SHA_256:<key_id>:0:<base64(ciphertext)>",
        },
    )


def _get_recipient_key(event):
    """GET /recipient-key — 读当前 enabled 公钥元数据"""
    from services.recipient_key_service import get_current_key

    key = get_current_key()
    if not key:
        return _resp(200, {"recipient_key": None})
    return _resp(200, {"recipient_key": key})


def _register_recipient_key(event):
    """POST /recipient-key — 登记/替换平台级 recipient 公钥(admin-only)"""
    # admin gate — identity-based like fleet_power (works for api-key admin path;
    # requestContext.authorizer.role is never populated in this stack, no custom authorizer)
    if not _get_caller_identity(event or {}).get("is_admin"):
        return _err(
            403, "ACCESS_DENIED", "recipient key registration requires admin role"
        )
    import json as _json

    body = event.get("body")
    if isinstance(body, str):
        body = _json.loads(body)
    pem = (body or {}).get("public_key_pem", "")
    if not pem:
        return _err(400, "VALIDATION", "body.public_key_pem is required")
    source = (body or {}).get("source", "caller")
    from services.recipient_key_service import register_key

    try:
        meta = register_key(pem, source=source)
    except ValueError as e:
        return _err(400, "VALIDATION", str(e))
    return _resp(200, meta)


def _disable_recipient_key(event):
    """POST /recipient-key/disable — 禁用当前 recipient key(admin-only)"""
    # admin gate — identity-based like fleet_power (works for api-key admin path;
    # requestContext.authorizer.role is never populated in this stack, no custom authorizer)
    if not _get_caller_identity(event or {}).get("is_admin"):
        return _err(403, "ACCESS_DENIED", "recipient key disable requires admin role")
    from services.recipient_key_service import disable_current

    result = disable_current()
    return _resp(200, {"disabled": True, "key": result})
