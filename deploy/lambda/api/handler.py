# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# #655 —— 必须在任何第一方 import 之前。见 core/runtime_report_fix.py:
# awslambdaric 的 post_init_error 用 latin-1 编 body,导入期中文异常会把上报通道打死,
# 调用方只剩裸 Runtime.ExitError。这里把它的 to_json 换成 ensure_ascii=True。
from core import runtime_report_fix as _runtime_report_fix  # noqa: E402
_RUNTIME_REPORT_FIX = _runtime_report_fix.apply()

import json
import hashlib
import os
import time
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError  # process_pending CAS 认领(#9 跨租户串修复)

from core.event_shape import unsupported_event_response as _unsupported_event_response
import core.ddb_scan as ddb_scan  # #432 —— Scan 必须翻页
from core.logging import logger, inject_trace_root, reset_invocation_keys
logger.info("runtime_report_fix", extra={"status": _RUNTIME_REPORT_FIX})
from services.tenant_query_service import (
    QUERY_FIELDS as _TENANT_QUERY_FIELDS,
    # #601 —— 响应字节预算的来源。见下面 `_RESPONSE_BYTE_BUDGET` 的说明:本路径取它的一半。
    _RESPONSE_ITEM_BUDGET as _GSI_ITEM_BUDGET,
    list_tenants_by_condition,
)
from services.tenant_stats_service import get_tenant_stats


def _resolve_proxy_route(method, path, route_keys):
    """#298 — 私有 API 是 `{proxy+}` 代理,event["resource"] 恒为 `/{proxy+}`,与 handler 按
    resource 模板分发的 routes 对不上(除 /ping 外全 404)。这里把具体 path(如 /tenants/abc/stop)
    按已注册的路由模板(routes.keys(),唯一真相源,不另维护清单免漂移)反解成 (resource_template,
    path_params)。段数相等 + 逐段(字面量相等 或 {param} 占位)匹配;字面量段更多者优先(/tenants/self
    胜 /tenants/{id}),杜绝跨模板误路由。无匹配返 (None, {})。EDGE API 走显式资源不进这里。
    """
    segs = [s for s in path.split("/") if s]
    best = None  # (resource_template, params, literal_count)
    for m, tmpl in route_keys:
        if m != method:
            continue
        tsegs = [s for s in tmpl.split("/") if s]
        if len(tsegs) != len(segs):
            continue
        params = {}
        literal = 0
        ok = True
        for a, b in zip(tsegs, segs):
            if a.startswith("{") and a.endswith("}"):
                params[a[1:-1]] = b
            elif a == b:
                literal += 1
            else:
                ok = False
                break
        if ok and (best is None or literal > best[2]):
            best = (tmpl, params, literal)
    return (best[0], best[1]) if best else (None, {})


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
# token(GET /tenants/{id}/credentials 拿密文,调用方自解)。旧 chat_sign 路由 + claw-
# channel HMAC + hub relay 全部下线。参见 SPEC/11-ENGINE-TRANSFORM/02-DEV-PLAN.md G/D
# 与 04-API-SPEC.md 一、二节。


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

    # #517 stage 4 — drift-gated rolling worker. Handle before HTTP routing.
    if event.get("_rolling_job"):
        return run_rolling_job(event["_rolling_job"])

    # PRD #54 — async batch worker: self-invoked with {"_batch_job": job_id}.
    # Not an HTTP request (no httpMethod) — handle before route dispatch.
    if event.get("_batch_job"):
        return run_batch_job(event["_batch_job"])

    # Rebuild worker: the HTTP request has already validated and fenced the
    # operation, then self-invoked this Lambda with InvocationType=Event.
    # Re-enter the one rebuild business flow with the same op_id and actor.
    _async_rebuild = event.get("_async_rebuild")
    if _async_rebuild:
        worker_body = _async_rebuild.get("body") or None
        reapply_binding = (
            worker_body.get("_config_reapply")
            if isinstance(worker_body, dict)
            else None
        )
        worker_event = {
            "_consumer_ident": _async_rebuild.get("_ident") or {},
            "_op_id": _async_rebuild.get("_op_id"),
            "_fence_epoch": _async_rebuild.get("_fence_epoch"),
        }
        # ── #564 G3(通道 C)—— 执行前判过期,与通道 B 同一条口径 ─────────────────
        # 位置:上面只是组一个 dict,零副作用;下一行的 tenant_action 才是第一个动作。
        # 异步 Lambda 调用对未处理异常会**用同一 payload 重投**(见下方 :166 的注释),
        # 所以没有这道闸时,一次卡死的 rebuild 会带着一个早就过期的死线反复重跑。
        # 缺死线字段(升级期的在飞 payload)→ `is_expired` 返 False → 照旧执行。
        _rb_dl = _async_rebuild.get(_create_deadline.MSG_DEADLINE_KEY)
        if _create_deadline.is_expired(_rb_dl, int(time.time())):
            _rb_tid = _async_rebuild.get("tenant_id")
            # 先围栏再返回:`fence_expired_tenant` 走 rebuild 那条分支(锚 `rebuild_phase`、
            # 写 `rebuild_phase/rebuild_status=failed` + `rebuild_fail_reason`),与每分钟
            # 一拍的扫描共用同一份归因与写法。
            # 传 payload 里的死线与 op_id 做双锚:异步 Lambda 对未处理异常会**用同一 payload
            # 重投**,一条陈旧重投带着早就过期的死线到达时,行上可能已经是另一次 rebuild ——
            # 不锚就会把那次活操作判死(Codex 独立复审第 1 轮抓出)。
            _rb_outcome = _dl_executor.fence_expired_tenant(
                _rb_tid,
                _create_deadline.ACTION_REBUILD,
                _rb_dl,
                observed_op_id=_async_rebuild.get("_op_id"),
            )
            # 围栏之后必须放掉租约,否则这个租户 1800s 内做不了任何生命周期操作,而它
            # 已经是终态了 —— 那会把"超时"变成"卡死更久"。用 payload 里的 fence_epoch
            # 做条件,放的是自己那一把。
            _tenant_service.finalize_async_rebuild_failure(
                _rb_tid,
                _async_rebuild.get("_op_id"),
                _async_rebuild.get("_fence_epoch"),
                f"deadline exceeded before the rebuild worker started "
                f"(deadline={_rb_dl})",
            )
            # 围栏结果打出来但**不**据此让本次调用失败:终态性由上面那次
            # `finalize_async_rebuild_failure` 兜住(它写 rebuild_phase/rebuild_status=failed
            # 并在 finally 里放围栏),围栏这一步失败只会少一个**封闭取值**的
            # `rebuild_fail_reason`,不会让租户留在在飞态。让本次调用失败反而更糟:异步重投
            # 会带着同一个过期 payload 反复走这条路。
            print(
                f"[#564] async rebuild {_rb_tid} 已过死线 {_rb_dl},不执行;"
                f"已围成终态(fence={_rb_outcome})并放掉租约"
            )
            return {"statusCode": 200, "body": '{"status":"deadline_exceeded"}'}
        # #429 —— 重应用模板时把成功收尾推迟到 worker 里做。放在过期检查【之后】:
        # 上面那支已过期的会直接 return、不进执行,也就不需要这个标志位。
        if isinstance(reapply_binding, dict):
            worker_event["_defer_async_rebuild_success_finalize"] = True
        result = tenant_action(
            _async_rebuild.get("tenant_id"),
            "rebuild",
            worker_body,
            worker_event,
        )
        code = result.get("statusCode", 500) if isinstance(result, dict) else 500
        try:
            result_body = json.loads((result or {}).get("body") or "{}")
        except Exception:  # noqa: BLE001
            result_body = {}
        if code < 500 or result_body.get("code") == "REPIN_BACKUP_FAILED":
            if code >= 400:
                _tenant_service.finalize_async_rebuild_failure(
                    _async_rebuild.get("tenant_id"),
                    _async_rebuild.get("_op_id"),
                    _async_rebuild.get("_fence_epoch"),
                    result_body.get("error") or f"worker returned HTTP {code}",
                )
            elif code < 300 and isinstance(reapply_binding, dict):
                if result_body.get("config_reapply") == "already_applied":
                    _tenant_service.finalize_async_rebuild_already_applied(
                        _async_rebuild.get("tenant_id"),
                        _async_rebuild.get("_op_id"),
                        _async_rebuild.get("_fence_epoch"),
                    )
                else:
                    _tenant_service.finalize_async_rebuild_success(
                        _async_rebuild.get("tenant_id"),
                        _async_rebuild.get("_op_id"),
                        _async_rebuild.get("_fence_epoch"),
                        reapply_binding,
                    )
            return result
        if result_body.get("rebuild_status") == "unconfirmed":
            # Host work may have happened. The rebuild branch already stamped
            # unconfirmed and the health-check reconciler owns convergence.
            return result
        if code >= 500:
            # Async Lambda invocations retry unhandled errors with the same
            # payload. The operation-stable op_id, lifecycle fence, and host
            # ledger make that retry resume the same rebuild.
            raise RuntimeError(
                f"async rebuild worker returned retryable status {code}"
            )
        return result

    # #309 — async pull-image worker: self-invoked with
    # {"_pull_image_async": {instance_id, snapshot_time, prev_status, job_id}}. pull_image
    # 已 CAS 置 upgrading + 回 202;这里在无客户端等待的 fire-and-forget 调用里跑
    # stage + 校验 + 备份 + copy/unzip 装 live 的数分钟长链(超 APIGW 29s,故必须异步)。
    _pia = event.get("_pull_image_async")
    if _pia:
        # #333 — 异步 Lambda 调用(InvocationType=Event)对【函数抛错】自动重试 2 次(AWS 官方:
        # invocation-retries.html)→ 重试 = 同 job 第二个 worker。并发由 host 侧 flock + status/owner
        # fence 兜住(见 _snapshot_pull_script);这里【吞掉所有异常、永远正常 return】只是【额外】
        # 减少重投噪声,不是并发防线本身。失败信息由 _run_pull_pipeline 内部写进度文件/last_pull_error
        # 透出。意外异常(_run_pull_pipeline 未捕获的)在此兜底【只记错误、不复位 status】。
        try:
            return _run_pull_pipeline(
                _pia["instance_id"],
                _pia["snapshot_time"],
                _pia.get("prev_status"),
                _pia.get("job_id"),
                # #394 — 目标槽位(live/canary);旧 payload 无此键 → None = 兼容扁平路径。
                _pia.get("slot"),
            )
        except Exception as e:
            print(f"[pull] worker unexpected error (swallowed to prevent async retry): {e}")
            # #333(codex round7)绝不在此复位 active:异常可能发生在 SSM 已下发/phase2 已动 live 之后,
            # 复位 active = 谎报让租户落到半写坏的 live(踩 no-data-loss/no-cross-tenant)。status 由
            # 脚本 trap 按阶段自决(phase1 复位 prev / phase2 留 upgrading);_run_pull_pipeline 的
            # "SSM 未下发"路径已在内部显式复位。这里只 job-conditional 记错误供 progress 透出,保持
            # 脚本设定的 status(通常 upgrading,待运维/下一轮 pull 收敛)。
            _host_service._record_pull_error(
                _pia["instance_id"], f"worker unexpected error: {e}", _pia.get("job_id"))
            return {"statusCode": 500, "body": "pull worker error (logged; not retried)"}

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
        from services.deadline_executor import enforce_deadlines as _deadlines  # noqa: E402

        # #562 G6 —— 独立死线执行者,与 poll_inflight【并列】而不是塞进它里面:
        #
        # ① 为什么不塞进 poll_inflight:那个函数在 ddb 模式(客户生产样例 config-sg-prod.yaml:185 用的就是它)
        #    第一行就空转返回(见其 docstring:#315 判定 ddb 下 poller 动租户状态/容量会错扣
        #    被并发命令占用的槽位)。塞进去等于在生产上永不执行 —— 最坏的一种「实现了但没生效」。
        # ② 为什么不新建 Lambda:G6 要的是「与 SQS 消费者解耦,消费者故障时死线仍被执行」,而
        #    EventBridge rate(1 minute) 这个触发源本身就与 SQS 消费者无关 —— 消费者挂了它照跑,
        #    这正是 G6 要的性质。issue 把「拆独立消费者 Lambda」明确列为【单独一个 MR】,
        #    本轮不混做(铁律 #2)。
        # ③ 为什么它的异常不阻断 poll_inflight:两者职责独立,死线执行失败不该让 push 模式的
        #    promote/回滚也停摆。这里 catch 是因为【本层能处理】——处理方式是记录后继续跑另一半,
        #    不是吞掉:错误既进日志也进返回值,指标上看得见。
        from services import poller_heartbeat as _hb  # noqa: E402

        # #432 —— 心跳必须【包住整轮】,而不是只包某一半。
        # 判据是「这一拍到底跑完没有」:只包一半的话,另一半卡住/抛异常时心跳照发,
        # 陈旧告警就永远不响 —— 那正是本 issue 要消灭的「没人知道它没跑」。
        with _hb.timer() as _t:
            try:
                _dl_stats = _deadlines()
            except Exception as e:  # noqa: BLE001 —— 见 ③:fail-loud 到日志+指标,不阻断 poller
                print(f"[#562] deadline executor failed: {type(e).__name__}: {e}")
                _dl_stats = {"error": f"{type(e).__name__}: {e}"}
            _poll_stats = _poll()
        # 心跳在【两半都跑完之后】发。发送失败不抛(可观测性不许阻断业务),而且发不出去
        # 本身就等于数据点缺席 → 陈旧告警会响,所以这条路径的失败不会变成静默失效。
        _hb_stats = _hb.emit(
            _t.seconds, errors=_hb.errors_in(_poll_stats, _dl_stats)
        )
        return {**_poll_stats, "deadlines": _dl_stats, "heartbeat": _hb_stats}

    # #438 —— 凭据回收对账(EventBridge rate)。消费删租户时打下的 `vkey_revoke_failed`
    # 标记,重试撤销 LiteLLM vkey。落在 **api Lambda** 而不是 health_check:实测只有
    # `openclaw-api` 的 env 带 `LITELLM_MASTER_KEY_SECRET`,放在 health_check 里它连
    # master key 都读不到,是一个注定空转的 reconciler。
    # 与 dispatch.poller 并列成独立 source(而不是塞进 poll_inflight):poll_inflight 在
    # ddb 模式第一行就空转返回,塞进去等于在生产上永不执行 —— #562 G6 已踩过这一条。
    if event.get("source") == "credential.reconciler":
        from services.credential_reconciler import (  # noqa: E402
            reconcile_credentials as _reconcile,
        )

        return _reconcile()

    # #532 —— 卡在 `deleting` 的删除对账(EventBridge rate)。消费「host 侧删除失败后保留的
    # `delete_retryable=true` + claim 已过期」这组行,按落库的 `delete_intent` 重新入队,
    # 让那条完整的删除路径再跑一次。进了 DLQ 的消息不会自己回主队列,所以没有这一拍,租户就
    # 永久停在 `deleting`(issue 真机实例:根因修好后两个租户仍卡着)。
    # 落在 **api Lambda** 而不是 health_check:只有它带 `LIFECYCLE_QUEUE_URL` env 并拿到
    # 该队列的 send 权限(`deploy/stacks/lambdas.py` 的 grant_send_messages),放 health_check
    # 里它连消息都发不出去 —— 与上面 credential.reconciler 同一条理由。
    # 与 dispatch.poller / credential.reconciler 并列成独立 source(不塞进 poll_inflight:
    # 那条在 ddb 模式第一行就空转返回,塞进去等于在生产上永不执行,#562 G6 已踩过)。
    if event.get("source") == "delete.reconciler":
        from services.delete_reconciler import (  # noqa: E402
            reconcile_deletes as _reconcile_deletes,
        )

        return _reconcile_deletes()

    # #515 #21 — 到这里说明既不是 SQS、也不是 poller、也不是上面那些自调用形状。原来直接下标
    # `event["httpMethod"]`,于是任何**直接 invoke**(没有 API Gateway 信封)都确定性抛
    # KeyError,函数根本走不到路由 —— restorepatch 的存活探针发 `{"path":"/ping"}` 正是这条,
    # 而 kit 把随之置位的 FunctionError 误分类成「private API 上的 404 body 属预期」,verify 报
    # 11 pass / 0 fail,「函数能否执行」从未被验证。判据放在 core/event_shape.py(不碰 boto3,
    # 可单测),用返回而不是抛错:返回本身就是「函数执行到了」的证据。
    _unsupported = _unsupported_event_response(event)
    if _unsupported is not None:
        return _unsupported

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
        ("GET", "/tenants-stats"): lambda: get_tenant_stats(event),
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
        ("POST", "/users/{tenant_user_id}/upgrade"): lambda: tenant_action(
            path_params["tenant_user_id"], "upgrade", event.get("body"), event
        ),
        # Go-live A1: external backend pushes the authoritative user↔tenant mapping.
        # Auth is HMAC (verified inside external_authz), NOT Cognito/RBAC — so it
        # must bypass the Cognito role gate (added to the RBAC skip list below).
        ("POST", "/external/authz"): lambda: external_authz(event.get("body"), event),
        # #187 转型:POST /chat/sign 下线,前端改经 /ws/{tenant_id} 直连 gateway。
        ("GET", "/hosts"): lambda: list_hosts(
            event.get("queryStringParameters") or {}
        ),
        ("POST", "/hosts"): lambda: register_host(event.get("body")),
        ("POST", "/hosts/refresh-rootfs"): refresh_rootfs,
        # #217 V2 — 照 DDB 快照按精确 VersionId 拉 host 相关文件(镜像+脚本),校验 etag
        # 后装到 live 原位置(launch-vm/service 直接读)。?snapshot_time=<ISO>,只作用一台
        # host。Admin op。旧 ?version=/version-verdict 已废弃(统一快照模型)。
        ("POST", "/hosts/{instance_id}/pull-image"): lambda: pull_image(
            path_params["instance_id"], event.get("queryStringParameters") or {},
            event.get("headers") or {},
        ),
        # #309 — GET pull-image 长任务进度:tail host 上 /tmp/<job_id>.txt 最后一行当状态。
        # #394 step1 — 透传 query:?job_id=<id> 精确查持久化 Job(不传=兼容取该 host 最近一条)。
        ("GET", "/hosts/{instance_id}/pull-image-progress"): lambda: pull_image_progress(
            path_params["instance_id"], event.get("queryStringParameters") or {}
        ),
        # #394 step5 — 同步槽位操作(admin-only,只改 host 上 slots.json 一个小文件,不搬盘)。
        # promote:canary 槽升为 live(带 expected snapshot+generation 的 CAS,防"验证 A 提升 B")。
        ("POST", "/hosts/{instance_id}/promote-canary"): lambda: _image_slot_op(
            path_params["instance_id"], event, "promote-canary"
        ),
        # #394 —— 无独立 rollback:回滚 = pull 老版到 live(本地已完整则快路径秒级翻指针)。
        # #394 — GET the host's REAL on-disk image state (slots.json + versions/ dir),
        # the authoritative counterpart to the possibly-stale DDB image_slots mirror. viewer.
        # #394 —— 无 DELETE image-slots/canary(cleanup-canary 已移除,精简 API):放弃未提升的
        # canary 无需显式清指针——下次 pull canary 覆盖该槽,promote 成功也会清空它。
        ("GET", "/hosts/{instance_id}/image-slots"): lambda: host_image_slots(
            path_params["instance_id"]
        ),
        # #394 — 回收该 host 上无人引用的版本目录(手动 prune;保留 live/canary/prev + 租户固定引用)。
        ("POST", "/hosts/{instance_id}/reclaim-images"): lambda: _image_slot_op(
            path_params["instance_id"], event, "reclaim-images"
        ),
        # #309 — 把单个文件从 S3 copy 到 EC2 指定位置(目标限 firecracker 资产目录白名单)。
        ("POST", "/hosts/{instance_id}/copy-file-from-s3"): lambda: copy_file_from_s3(
            path_params["instance_id"], event.get("body")
        ),
        # Fleet power: start/stop EVERY VM across all hosts via host-local fan-out
        # (1-minute fleet power goal). Admin-only (gated inside fleet_power).
        ("POST", "/hosts/fleet-power"): lambda: fleet_power(event.get("body"), event),
        # #566 拆分② — fleet guest 出网防火墙运维:一次改全部(或指定)host 的
        # OPENCLAW-EGRESS default-deny 链。Admin-only(gated inside fleet_egress)。
        ("POST", "/hosts/egress"): lambda: fleet_egress(event.get("body"), event),
        ("GET", "/hosts/egress"): lambda: fleet_egress_status(event),
        ("GET", "/hosts/egress/revisions"): lambda: fleet_egress_revisions(event),
        ("DELETE", "/hosts/egress/revisions"): lambda: fleet_egress_revisions_delete(
            event.get("body"), event
        ),
        ("GET", "/hosts/egress/chain"): lambda: fleet_egress_chain(event),
        ("POST", "/hosts/egress/rollback"): lambda: fleet_egress_rollback(
            event.get("body"), event
        ),
        # #668 —— 只读 dry-run:逐条回显 allow 的判定与当前阈值,绝不写期望态、
        # 不发 SSM、不落 revision。Admin-only(gated inside the service)。
        ("POST", "/hosts/egress/allow/validate"): lambda: fleet_egress_allow_validate(
            event.get("body"), event
        ),
        ("POST", "/hosts/rolling-upgrade"): lambda: submit_rolling_upgrade(
            event.get("body"), event
        ),
        ("GET", "/hosts/rolling-jobs/{job_id}"): lambda: get_rolling_job(
            path_params["job_id"], event
        ),
        ("GET", "/hosts/rootfs-version"): rootfs_version,
        ("GET", "/hosts/rootfs-drift"): rootfs_drift,
        # 10h-goal #19 — golden-image inventory. Per-tenant data snapshot is served
        # via GET /tenants/{id}/{action} with action=data (tenant_get_action).
        ("GET", "/images"): lambda: list_images(
            event.get("queryStringParameters") or {}
        ),
        # #217 V2 — list version snapshots (time+label+count) so the console can
        # let an operator pick which snapshot_time to pull onto a host.
        ("GET", "/list_image_versions"): lambda: list_image_versions(
            event.get("queryStringParameters") or {}
        ),
        # #376 — take a version snapshot of the assets bucket (equivalent to
        # scripts/snapshot-version.sh): scan deployment/, record every current
        # object's {path, s3_version_id, etag} into the snapshots table. Operator+.
        ("POST", "/create-image-snapshot"): lambda: create_image_snapshot(
            event.get("body")
        ),
        # #394 — soft-delete ONE snapshot record by snapshot_time (body {snapshot_time},
        # symmetric with /create-image-snapshot; avoids colons-in-path). Refuses (409
        # IMAGE_VERSION_IN_USE) if any host slot or tenant still pins it. Metadata only
        # — marks status=deleted, does not remove S3 image files. Operator+.
        ("POST", "/delete-image-snapshot"): lambda: delete_image_snapshot(
            event.get("body")
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
        ("POST", "/hosts/{instance_id}/taint"): lambda: _taint_host_route(
            path_params["instance_id"], event.get("body"), event
        ),
        ("DELETE", "/hosts/{instance_id}/taint"): lambda: _untaint_host_route(
            path_params["instance_id"], event
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
        # #389 v2 块5 — bootstrap 版本切换(admin-only,handler 内 identity 门)。只在【已存在】
        # 的 S3 bootstrap 版本间切换(传 sha256,不传脚本内容);切 host/edge 两套 LT+ASG。
        ("GET", "/bootstrap/versions"): lambda: _bootstrap_versions(event),
        ("POST", "/bootstrap/promote"): lambda: _bootstrap_promote(event),
    }

    # #298 — 私有 API 用 `{proxy+}` 代理时 resource 恒为 `/{proxy+}`,不匹配任何真实模板。
    # 用 event["path"](已剥 stage 前缀的资源路径)按 routes.keys() 反解成真实 resource 模板 +
    # path_params,并原地更新(routes 里的 lambda 闭包引用同一个 path_params 字典,看得到更新)。
    # 只在 proxy 占位符时介入;EDGE API 的显式 resource 原样不动,零行为变化。
    if resource == "/{proxy+}":
        _resolved, _pp = _resolve_proxy_route(
            method, event.get("path", ""), routes.keys()
        )
        if _resolved is not None:
            resource = _resolved
            path_params.update(_pp)

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
    except Exception:
        import traceback

        # #609 —— 兜底 except 不再回显 str(e)。这里接的是**未预期**异常,原文常带内部
        # 坐标(真机上出现过 botocore 的 "The table does not have the specified index:
        # gsi_tenant_user",把表结构告诉了调用方)。原文进 CloudWatch,调用方只拿到一个
        # 稳定的错误码;可预期的失败应该在各自的 handler 里转成带语义的 4xx/503。
        traceback.print_exc()
        return _err(500, "INTERNAL", "internal error")


# ========== Tenant Operations ==========


# Fields that are server-side secrets / credentials and MUST NEVER reach an API
# response (the chat UI calls GET /tenants with a Cognito Bearer; any field here
# would otherwise be handed to the browser). channel_secret is the HMAC key the
# hub verifies channel registration against — leaking it lets any logged-in user
# forge their node's channel registration (IDOR / credential leak). litellm_vkey
# is the per-tenant LLM billing key. Strip them from every outbound tenant record.


#: #601 补页扫描的每批扫描条数下限与页数上限。批 = max(limit * 4, 下限),让高软删率的
#: 表不必为 limit=2 这种小页发几百次 scan。
_SCAN_BATCH_MIN = 200
_SCAN_MAX_PAGES = 10

#: 单次请求的补页扫描**时间**预算(秒)。页数上限只限调用次数、不限耗时 —— 慢 Scan 或 SDK
#: 重试(botocore 的指数退避)时,10 次串行调用照样能吃掉 API-GW 的 30s,那时客户连续页的
#: token 都拿不到(Codex 独立复审指出)。用 `time.monotonic()` 而不是 `time.time()`:后者
#: 会被 NTP 校正拖动。
_SCAN_TIME_BUDGET_SEC = 10.0

#: 本路径的响应字节预算,取 GSI 分页路径(`tenant_query_service._RESPONSE_ITEM_BUDGET`)的
#: **一半**。
#:
#: 为什么不直接用那个数:`_resp` 把 body 序列化成 JSON 字符串之后,**Lambda runtime 还会把
#: 整个响应对象再序列化一次** —— body 里的每个 `"` 都变成 `\"`,转义密集的数据最坏翻倍。
#: 4.8 MB 的内层 JSON 能撑到约 9.6 MB 外层,越过 Lambda 同步响应的 6 MiB 硬限(Codex 独立
#: 复审指出)。本路径会把多批 scan 聚合进一个响应,放大了这个风险,所以取一半:最坏翻倍后
#: 约 4.8 MB,仍在 6 MiB 之内。派生而不是另写一个数,那边调整时这边跟着走。
_RESPONSE_BYTE_BUDGET = _GSI_ITEM_BUDGET // 2


def _validate_tenant_list_filters(query_params):
    """#106 的 ?platform_id / ?purchase_status 格式校验。返回 err 或 None。

    #601 —— 从过滤流程里提到补页循环【之前】做一次:它只看 query、不看数据,放在循环里
    每批重复校验一遍是白做,而且会让"非法参数"的 400 取决于扫到了几条数据。
    """
    qp = query_params or {}
    pid_filter = qp.get("platform_id")
    if pid_filter is not None and not _PLATFORM_ID_RE.match(pid_filter):
        return _err(
            400, "VALIDATION", "platform_id must be 1-128 chars [a-zA-Z0-9._-]"
        )
    ps_filter = qp.get("purchase_status")
    if ps_filter is not None and ps_filter not in (
        _PURCHASE_PENDING,
        _PURCHASE_PROVISIONED,
    ):
        return _err(
            400,
            "VALIDATION",
            f"purchase_status filter must be one of "
            f"['{_PURCHASE_PENDING}', '{_PURCHASE_PROVISIONED}']",
        )
    return None


def _apply_tenant_list_filters(items, ident, query_params, multi_query_params):
    """把 scan 出来的原始行过滤成"对本调用方可见"的行。纯函数,无 I/O。

    #601 —— 抽成函数是补页的前提:这些过滤全部发生在 scan 之后(DynamoDB 只帮我们挡了
    status=deleted 这一条,而且是在 Limit 之后),所以补页循环必须能对【每一批】施加同一套
    过滤,否则"凑满 limit 条"数的是未过滤的行。校验类 400 由
    `_validate_tenant_list_filters` 在循环外先做。
    """
    # issue #80 — owner scoping: a non-admin Cognito user sees only the tenants
    # they own. Admins and the API-key caller see everything. Records without
    # an owner_id (legacy / API-key-created) stay hidden from non-admins.
    # #60 — key off identity, not RBAC_ENABLED: when RBAC is off every caller is
    # the API_KEY_OWNER admin and is_admin skips the filter anyway, so scoping
    # can never be silently disabled by flipping the global flag.
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
    # created ("按 platform_id + owner 筛租户"), or filter by purchase stage.
    qp = query_params or {}
    pid_filter = qp.get("platform_id")
    if pid_filter is not None:
        items = [it for it in items if it.get("platform_id") == pid_filter]
    ps_filter = qp.get("purchase_status")
    if ps_filter is not None:
        items = [it for it in items if it.get("purchase_status") == ps_filter]

    # Strip server-side secrets (channel_secret / litellm_vkey) before returning —
    # the chat UI calls this with a Cognito Bearer; secrets must stay server-side.
    return [_redact_tenant(it) for it in items]


def _scan_tenant_page(
    scan_kwargs, limit, start_key, ident, query_params, multi_query_params
):
    """#601 —— 补页扫描,直到凑满 limit 条【可见】行或确认扫到表尾。返回 (items, next_key)。

    为什么必须补页:DynamoDB Scan 的 `Limit` 限的是【扫描条数】,`FilterExpression` 在数据
    读出之后才应用(AWS 文档:filter 不影响 ScannedCount、不省读容量),所以 `Limit=N` 从来
    不等于"返回 N 条"。真机实测 openclaw-tenants 669 行里 667 行会被丢掉(563 软删 + 104
    无 status/无 host_id),过滤率 99.7%:`Limit=2` 的首页实测 `ScannedCount=2 Count=0` 且
    带有效 `LastEvaluatedKey`。旧实现把这一页原样返回,于是客户拿到"0 条 + 有效 token",
    按 token 无限翻页(要凑到那 2 条存活租户需连翻约 335 页)——分页接口事实不可用。

    `next_key` 的语义同时收紧为【确实还有下一页】:判据是多凑一条(> limit),游标停在第
    limit 条自己的主键,而不是 DynamoDB 的 `LastEvaluatedKey` —— 后者已经扫过那些"扫到但
    没返回"的行,拿它当游标会把它们跳过去(漏数据)。因此"返回 0 条却带 token"这个组合在
    结构上不再可能:不足 limit 条只发生在扫到表尾或撞页数上限。
    """
    batch = max(int(limit) * 4, _SCAN_BATCH_MIN)
    items, key, pages, encoded = [], start_key, 0, 0
    byte_truncated = False
    out_of_budget = False
    deadline = time.monotonic() + _SCAN_TIME_BUDGET_SEC
    while True:
        kwargs = dict(scan_kwargs, Limit=batch)
        if key:
            kwargs["ExclusiveStartKey"] = key
        out = tenants_table.scan(**kwargs)
        for item in _apply_tenant_list_filters(
            out.get("Items") or [], ident, query_params, multi_query_params
        ):
            # 字节预算与 GSI 分页路径同源(见文件头的 import 说明):`limit` 上限是 1000,
            # 1000 条完整租户行可以轻松越过 Lambda 同步响应的 6 MiB 硬限 → 502。
            # `items and` 保证至少返回一条,不会因为单行超预算而返回空页。
            size = len(
                json.dumps(item, separators=(",", ":"), default=str).encode("utf-8")
            )
            if items and encoded + size > _RESPONSE_BYTE_BUDGET:
                byte_truncated = True
                break
            items.append(item)
            encoded += size
        key = out.get("LastEvaluatedKey")
        pages += 1
        # 次数与耗时**两个**护栏:前者防单请求发出无限多次 scan,后者防 10 次慢 scan
        # (SDK 重试退避)吃掉 API-GW 的 30s。
        out_of_budget = pages >= _SCAN_MAX_PAGES or time.monotonic() >= deadline
        if byte_truncated or len(items) > limit or not key or out_of_budget:
            break
    if byte_truncated or len(items) > limit:
        # 两种截断合并处理:多凑的那条(或超预算的那条)只用来证明"还有下一页",不返回;
        # 游标停在**最后一条已返回的行**。用 LastEvaluatedKey 会跳过这些已扫但未返回的行。
        keep = items[:limit]
        return keep, {"id": keep[-1]["id"]}, False
    if key and out_of_budget:
        # 扫描预算(次数或耗时)耗尽而 DynamoDB 还有更多。游标是 LastEvaluatedKey(已扫过的都被过滤掉了,
        # 没有"漏返回"的行)且必然已推进,所以客户翻页有进展、不会原地打转。
        #
        # 第三个返回值是 `True`:这一档**必须让调用方显式标注**。否则在极端稀疏的表上
        # 仍会出现"0 条 + 有 token",而客户无法区分它与"真的没有租户"—— 那正是本 issue
        # 的核心伤害,只是从"必然发生"退化成"极端情况下发生"仍然不够(Codex 独立复审指出)。
        return items, key, True
    return items, None, False


def list_tenants(query_params=None, multi_query_params=None, event=None):
    query_params = query_params or {}
    if any(field in query_params for field in _TENANT_QUERY_FIELDS):
        return list_tenants_by_condition(query_params, event or {})
    # PRD #53 — optional pagination. Backward compatible: no ?limit → scan to the
    # end and return a bare array (legacy shape small deployments rely on). With
    # ?limit=N → one page of ≤N rows + an opaque next_token, wrapped in an object
    # so a 100k-row table never blows the 30s API-GW timeout or the client.
    paginate = bool(query_params.get("limit")) or bool(
        query_params.get("next_token")
    )
    scan_kwargs = {
        "FilterExpression": "#s <> :d",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":d": "deleted"},
    }
    # 校验先于扫描:非法 ?platform_id/?purchase_status 是 400,不该先花掉一轮 scan。
    err = _validate_tenant_list_filters(query_params)
    if err is not None:
        return err
    ident = _get_caller_identity(event or {})
    if paginate:
        limit, err = _parse_limit(query_params)
        if err is not None:
            return err
        start_key, err = _parse_next_token((query_params or {}).get("next_token"))
        if err is not None:
            return err
        items, next_key, budget_exhausted = _scan_tenant_page(
            scan_kwargs, limit, start_key, ident, query_params, multi_query_params
        )
        body = {
            "tenants": items,
            "next_token": _encode_next_token(next_key),
            "count": len(items),
        }
        if budget_exhausted:
            # #601 —— 单次请求的扫描预算耗尽,匹配行可能在已扫范围之后。**必须显式标注**:
            # 否则这一档的"少于 limit 条(可能 0 条)+ 有 token"与"真的只有这么多租户"在
            # 响应上完全一样,而"分不清哪种"正是本 issue 的核心伤害。带上这个字段,客户就
            # 知道"继续翻是有意义的",而不是把空页当成结论。
            body["scan_budget_exhausted"] = True
        return _resp(200, body)
    items = _apply_tenant_list_filters(
        ddb_scan.scan_all(tenants_table, **scan_kwargs),
        ident,
        query_params,
        multi_query_params,
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
    # the status-poll response once the tenant is `running` (INTERFACE-CONTRACT
    # §5, design decision 二次纠正). Poll semantics: control-plane callers loop
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
    # #422 — 纵深防御:viewer 级自助创建的是「自己的全新节点」,绝不该从任意备份恢复/克隆
    # 他人数据。create_tenant 的 restore_from 分支现已补属主校验(no-cross-tenant),但自助
    # 路径根本不该触达恢复语义 → 在入口显式剥掉,把 IDOR 面从 viewer 入口彻底断开(双保险)。
    body.pop("restore_from", None)
    body.pop("clone_from", None)
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
    tenant data — only the platform→IdP routing (SPEC/02 §2.7). Mirrors aws-samples/
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
            # #564 G5 — 七档生命周期死线的**生效值与来源**。
            #
            # 为什么放在这个端点:它的职责就是 config snapshot(见本函数 docstring),而验收
            # 第 4 条要的是「改配置后死线值随之变化,**读取值要在日志或响应里可验证**」——
            # 没有一个可读处,那条就无从验证,而且"建了参数没人读"正是 G1 在 env 名上专门
            # 防的形态。
            #
            # `source` 是这里的关键,不是冗余:`ssm` = 参数里的值;`ssm-stale` = SSM 读失败、
            # 用的是上次读到的旧值;`env-or-default` = 参数里没有这一档,回落 env 或代码默认。
            # 运维改完参数刷这个端点,看的就是 source 有没有变成 `ssm`、值有没有跟着变。
            "lifecycle": _lifecycle_deadline_snapshot(),
            # #579 Bug3 —— 部署能力/身份,供客户与部署门检测「控制面版本漂移致高影响
            # 字段被静默忽略」。本 build 含 #429 reapply 实现(services.tenant_service
            # ._reapply_requested / _prepare_config_reapply),故 config_reapply=true;
            # 旧部署(如 apse1 deployed version 91)不含本 build → 此块缺失或 config_reapply
            # 非 true,客户据此判定不该发 reapply 字段,而非靠试调用返回 done 后再猜是否真应用。
            # function_version 来自 Lambda runtime,供 deployment gate 核 live alias 与 bb 期望。
            "capabilities": {
                "config_reapply": True,
                "function_version": os.environ.get("AWS_LAMBDA_FUNCTION_VERSION", "")
                or None,
                "deploy_sha": os.environ.get("DEPLOY_SHA", "") or None,
            },
        },
    )


def _lifecycle_deadline_snapshot():
    """七档死线的生效值 + 来源;顺带打一行日志(验收既可看响应也可看日志)。

    **fail-soft**:这个端点是只读诊断面,不能因为死线配置读不出来就整个 500 —— 那会把一次
    "看看配置"变成一次故障。非法参数值该炸的地方是**真正用死线的那条路**(写租户/入队),
    不是这里;所以这里把异常收成一个可见的 `error` 字段。
    """
    try:
        snap = _deadline_config.all_effective_deadline_sec()
    except Exception as e:  # noqa: BLE001 — 见 docstring:诊断面不因配置问题变 500
        logger.warning(f"system_info: 死线快照读取失败({type(e).__name__}): {e}")
        return {"deadline_sec": None, "error": f"{type(e).__name__}: {e}"}
    logger.info(
        "system_info lifecycle deadline snapshot: "
        + ", ".join(f"{a}={v}({src})" for a, (v, src) in sorted(snap.items()))
    )
    return {
        "deadline_sec": {a: {"value": v, "source": src} for a, (v, src) in snap.items()},
        "param_prefix": _deadline_config.PARAM_PREFIX,
    }


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
    # #432 —— 必须翻页。openclaw-tenants 实测 6790 行 / 2.83MB,【已超 1MB 近三倍】,
    # 所以这一处现在就在漏:落在后页的 pending 租户永远轮不到 promote。
    # 而 #562 之后它们会在死线时被判 failed(capacity_unavailable)—— 客户看到的是
    # 「没容量」,真实原因却是「我们没看见你」。
    pending = ddb_scan.scan_all(
        tenants_table,
        FilterExpression="#s = :p",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":p": "pending"},
    )

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
        # #491(review2)—— 不再「试号 → 撞了归还 → 再试」。改成:先算出第一个未被物理占用的
        # 号,再用【跳号 CAS】一次认领它。这样同时消掉三个洞:没有"试几次放弃"的上限
        # (被占号段可能有几百个,任何上限都会误报无容量);不需要回滚(本路径无
        # capacity_reservation_id 令牌,归还做不到幂等可确认);不依赖事件重试换号
        # (process_pending 只由 HostReady 触发,没有保证会来的下一 tick)。
        _occ_fail = 0
        for _attempt in range(8):
            cand = _find_host(vcpu, mem_mb)
            if not cand:
                break
            expected = int(cand.get("next_vm_num", 1))
            target, _occ = _scheduling.next_free_phys_num(
                cand["instance_id"], expected, exclude_ids={tenant["id"]}
            )
            if _occ is None:
                # 占用集合读失败 = 未知 → fail-closed,不认领。
                # #491(review3)—— 先在【本次调用内】重试:process_pending 只由 HostReady
                # 事件触发,没有保证会来的下一次,一次 DDB 抖动就放弃会让租户搁置。
                # 8 次都失败才放弃该租户,并在下面打 WARN(可观测,不是静默丢失)。
                _occ_fail += 1
                print(
                    f"[pending] PHYS OCCUPANCY READ FAILED tenant={tenant['id']} "
                    f"host={cand['instance_id']} attempt={_attempt + 1}/8 — retrying"
                )
                continue
            _occ_fail = 0  # 本轮读成功
            if target is None:
                print(
                    f"[pending] NO FREE PHYS SLOT host={cand['instance_id']} "
                    f"from={expected} — try another host"
                )
                continue
            if target != expected:
                print(
                    f"[pending] SKIP OCCUPIED tenant={tenant['id']} "
                    f"host={cand['instance_id']} expected={expected} target={target}"
                )
            # #430 — per-family ratio,与 scheduling._find_host 同口径。用全局 ratio
            # 会"选得中、订不上"(见 tenant_service.py:_reserve_slot 注释)。
            _cpu_r, _mem_r = _host_profile.ratios(
                cand,
                (CPU_OVERCOMMIT_RATIO, MEM_OVERCOMMIT_RATIO),
                _clients.OVERCOMMIT_BY_FAMILY,
            )
            cap_v = _capacity.allocatable(int(cand["total_vcpu"]), _cpu_r) - vcpu
            cap_m = _capacity.allocatable(int(cand["total_mem_mb"]), _mem_r) - mem_mb
            try:
                r = hosts_table.update_item(
                    Key={"instance_id": cand["instance_id"]},
                    # #491 跳号:next_vm_num 直接推到 target+1(绝对值),不是 +1。
                    # 条件仍是 next_vm_num = :expected —— 并发者改过就 CCF 重来,
                    # 被跳过的号不会被别人同时认领。
                    UpdateExpression=(
                        "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
                        "vm_count = vm_count + :one, next_vm_num = :next_after, "
                        "#ps = :tid, #s = :active REMOVE idle_since"
                    ),
                    ConditionExpression=(
                        "next_vm_num = :expected AND used_vcpu <= :cap_v "
                        "AND used_mem_mb <= :cap_m AND attribute_not_exists(#ps)"
                    ),
                    ExpressionAttributeNames={
                        "#s": "status",
                        "#ps": _scheduling.phys_slot_attr(target),
                    },
                    ExpressionAttributeValues={
                        ":v": vcpu,
                        ":m": mem_mb,
                        ":one": 1,
                        ":active": "active",
                        ":tid": tenant["id"],
                        ":expected": expected,
                        ":next_after": target + 1,
                        ":cap_v": cap_v,
                        ":cap_m": cap_m,
                    },
                    ReturnValues="UPDATED_NEW",
                )
                # 取号:优先用 CAS 返回值(与本函数原有契约一致),取不到回退 target。
                # 两者恒等(CAS 成功 ⇒ next_vm_num == target+1);优先返回值是为了不改变
                # 对外语义,回退是为了不依赖 DDB 一定回传 Attributes。
                try:
                    vm_num = int(r["Attributes"]["next_vm_num"]) - 1
                except (KeyError, TypeError, ValueError):
                    vm_num = target
                host = cand
                break
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    # 输了 CAS(容量满/next_vm_num 变了/物理号被并发原子占了)→ 重选 host 重算
                    continue
                raise
        if host is None or vm_num is None:
            if _occ_fail:
                # 占用始终读不出来:只跳过该租户,不停掉其余 pending(那是「无容量」的语义)。
                # WARN 级日志便于巡检/人工重触发 —— 真正的持久重调度要把 pending 分配接到
                # 队列上,属发号器设计变更,拆后续 issue。
                print(
                    f"[pending] WARN tenant={tenant['id']} left pending: physical "
                    f"occupancy unknown after {_occ_fail} attempts "
                    f"— needs retrigger or reconcile"
                )
                continue
            break  # 无容量或持续竞争 → 停,剩余 pending 下次事件再处理

        guest_ip = _guest_ip(vm_num)
        host_port = VM_PORT_BASE + vm_num - 1
        now = _now()

        # Update pending tenant with host assignment (host slot 已 CAS 占好)
        rootfs_version = host.get("rootfs_version", "")
        update_expression = "SET #s = :s, host_id = :h, vm_num = :n, guest_ip = :g, host_port = :p, rootfs_version = :rv, creation_started_at = :t, updated_at = :t"
        values = {
            ":s": "creating",
            ":h": host["instance_id"],
            ":n": vm_num,
            ":g": guest_ip,
            ":p": host_port,
            ":rv": rootfs_version,
            ":t": now,
        }
        if rootfs_version and len(rootfs_version.encode("utf-8")) <= 256:
            update_expression += ", q_rootfs_version = :qrv"
            values[":qrv"] = rootfs_version
        else:
            update_expression += " REMOVE q_rootfs_version"
        # #595 —— CAS 到 pending:scan 与本写之间租户可能被【带外删除】(scaler TTL / host 终止,
        # 不经 lifecycle fence)。原来是零条件写,会把 deleted 覆盖成 creating、回写 q_rootfs_version、
        # 并起一台 VM(删除后仍起 = host 上无主 microVM + 容量账本泄漏 + 复活进 gsi_rootfs_version)。
        # 条件落空 = 已非 pending → 释放刚 CAS 占好的 host slot 并跳过 launch,绝不覆盖新状态。
        values[":pending"] = "pending"
        try:
            tenants_table.update_item(
                Key={"id": tenant["id"]},
                UpdateExpression=update_expression,
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
            )
        except tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
            # 租户在放置窗口内被并发删除/认领 → 归还占号,跳过本租户(不 launch、不覆盖 deleted)。
            _release_slot(
                host["instance_id"], vcpu, mem_mb,
                phys_num=vm_num, tenant_id=tenant["id"],
            )
            print(
                f"[pending] tenant {tenant['id']} 已非 pending(并发删除/认领)"
                "— 释放 slot,跳过 launch"
            )
            continue
        except Exception:
            # codex #595 复审:非 CCF 的写失败(限流/网络/校验)必须沿用原语义 —— 释放刚占的
            # 容量/ps_* 再抛出。拆 try 后若只接 CCF,这条路径会泄漏占号(原来 update+launch 同一
            # try 的 generic except 覆盖了它)。
            _release_slot(
                host["instance_id"], vcpu, mem_mb,
                phys_num=vm_num, tenant_id=tenant["id"],
            )
            raise
        try:
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
        except Exception:
            # 占号必须和容量分开归还:存量 host 没有 ps_*,不能让容量 floor guard 依赖它;
            # 反过来容量回滚失败也不能连带泄漏仍属于本租户的物理号。
            _release_slot(
                host["instance_id"],
                vcpu,
                mem_mb,
                phys_num=vm_num,
                tenant_id=tenant["id"],
            )
            raise
        # #187 转型:数据面走两级路由(ALB LOR → OpenResty edge → Redis 查表 → host
        # DNAT → microVM:18789);per-tenant ALB rule/TG 死路径已下线。
        #
        # #509 —— 用完即清 restore_backup_key。它是「本次放置要从哪个备份恢复」的一次性指令,
        # 不是租户的常驻属性;上面的 _launch_vm 已经把它按位置参传下去了,这次恢复不再需要它。
        # 留着的危害是实的:launch-vm.sh:602-617 在位置参为空时会自己从 DDB 读这个字段,于是该租户
        # 之后任何一次 launch(重启/换机)都会按【那个旧备份】重铺盘 → 丢掉此后写入的数据。
        # 清空是安全的:消费方本就有回落(tenant_service.py:3297
        # `item.get("restore_backup_key") or _resolve_backup(tenant_id)`),真需要时会从 S3 取最新的。
        # 与既有 suspend/restore 的收尾同款(tenant_service.py:3396 也是 restore 成功后 REMOVE 掉)。
        if tenant.get("restore_backup_key"):
            try:
                tenants_table.update_item(
                    Key={"id": tenant["id"]},
                    UpdateExpression="REMOVE restore_backup_key",
                )
            except Exception as e:  # noqa: BLE001 — 清不掉只是留个陈旧字段,不能反过来让放置失败
                print(f"process_pending: failed to clear restore_backup_key for "
                      f"{tenant['id']}: {e}")
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
    try:
        items, new_token = _query_user_tenants(
            tenant_user_id, limit=limit, next_token=next_token, platform_scope=_scope
        )
    except _TenantUserIndexUnavailable:
        # #609 —— 索引未部署是可预期状态,不是故障:结构化 503,不回显索引名/表结构。
        return _err(
            503, "UNAVAILABLE", "per-user fleet index is not active"
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
        try:
            items, next_token = _query_user_tenants(
                tenant_user_id,
                limit=_USER_PAGE_MAX,
                next_token=next_token,
                platform_scope=_scope,
            )
        except _TenantUserIndexUnavailable:
            # #609 —— 同 list_user_tenants:索引未部署 → 结构化 503。放在循环里是因为
            # 第一页就会抛,后续页不可能走到;写在这里比在循环外包一层更贴调用点。
            return _err(
                503, "UNAVAILABLE", "per-user fleet index is not active"
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
        try:
            items, next_token = _query_user_tenants(
                tenant_user_id,
                limit=_USER_PAGE_MAX,
                next_token=next_token,
                platform_scope=_scope,
            )
        except _TenantUserIndexUnavailable:
            # #609 —— 第三个走同一 GSI 的入口。索引未部署时同样只能是结构化 503;
            # fail closed 在这里尤其重要:此时一个 VM 都还没启停,不存在部分执行。
            return _err(
                503, "UNAVAILABLE", "per-user fleet index is not active"
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
    # #469 P5 —— 202(仅入队)不再混进 succeeded,单列 enqueued。队列开启时
    # delete/stop/start 在 _execute_batch 里返 202,旧代码按 "<400 即成功" 上报,
    # 而 consumer 之后可能 5 次重投进 DLQ —— 调用方看到 succeeded 会以为做完了。
    # 这是 issue #469 评论(2026-08-12)点名的"谎报"根因之一,也直接违反验收第 6 条。
    succeeded, failed, enqueued = _execute_batch(action, target_ids, event)
    return _resp(
        200, {"succeeded": succeeded, "failed": failed, "enqueued": enqueued}
    )


# ───────────── Fleet power: start/stop EVERY VM within 1 minute ─────────────
#
# GOAL: the control plane consumes 380 (×N hosts) openclaw start/stop within 1
# minute. The per-tenant path (batch_tenants → tenant_action → one SSM per VM)
# can't: SSM single-instance concurrency caps at ~5-10, so 380 commands serialize
# and 40 concurrent already TimedOut 11 (measured). The fix is HOST-LEVEL
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


def _receive_count(rec):
    """SQS 的 `ApproximateReceiveCount`。**它是字符串**,而且可能整个缺失。

    形态与 `dispatch_service` 那处(#522 P1-2)逐字相同:`rec["attributes"]` 里的值是
    字符串;缺失/不可解析时返 **0** 而不是 1 —— 0 会让下面「已是最后一次投递」的判定
    **不成立**,也就是"看不出来就不回写终态"。方向刻意保守:误判"是最后一次"会把一个
    仍会被重投并可能成功的操作提前标成失败(而 `system_error` 的已发布语义是"重试无益"),
    那比晚一点回写更糟。
    """
    try:
        return int((rec.get("attributes") or {}).get("ApproximateReceiveCount") or 0)
    except (TypeError, ValueError):
        return 0


#: 队列默认可见性(`lambdas.py:337` `visibility_timeout=Duration.seconds(960)`)。它既是
#: "什么都不做"时一条未 ack 消息的重投间隔,也是本退避必须对齐的**总预算基准**。
_LIFECYCLE_DEFAULT_VISIBILITY_SEC = 960

#: #604 —— 头几次投递的短退避,让 per-tenant FIFO 组头尽快让开。**只缩短,永不加长。**
#:
#: 收益在这里:`MessageGroupId=tenant_id`,组头没 ack 就不投同租户后面的消息,所以一条占着
#: 960s 的消息 = 该租户 16 分钟内任何生命周期操作都撞 409(#604 的现象)。三次覆盖了 flock
#: 持锁的常见量级(launch-vm.sh:705 到 :2570 DONE:prepare disks / tap / firecracker boot)。
#:
#: **为什么不"补足总窗口"**(Codex 独立复审三轮各否掉一个版本,记在这里免得再走回去):
#:   · v1 写死 `(30,60,120,240,480)` 并声称"累计 930s ≈ 960s 所以预算不缩水" —— 那是把
#:     **单次等待**当成了**总窗口**。`maxReceiveCount=5` 有 4 次等待,原总窗口 4×960=3840s,
#:     前四项只有 450s,缩了 8.5 倍。
#:   · v2 改成尾项补足(`…,3630`)确实把总窗口补回了 3840s,但**它让那一次的组头阻塞变成
#:     1 小时**,比本 issue 要修的 960s 更糟 —— 而 `launch-vm.sh` 持锁时会等 host launch
#:     slot,这条路径真的可达。
#:   · v3(即本版之前)只缩头部、之后回落默认,总窗口 1170s。但它建立在"死线兜底才是终态的
#:     真正保证者"这个前提上,而当时那个前提**对 start 不成立**:`DEADLINE_ACTIONS` 那时是
#:     七档(create/suspend/restore/restart/rebuild/backup/delete),**没有 start**,也没有
#:     `ACTION_START` 常量。一条 `action=start` 的消息没有死线、没有兜底,把它的重试窗口
#:     从 3840s 缩到 1170s 就是实打实地提高"已受理的 start 提前进 DLQ 而永久丢失"的概率。
#:
#: 所以判据再收一层:**只对有终态兜底的 action 缩短**(`_deadline_backstopped_action`)。
#: 这治好了 #604 现象的大部分 —— 真机那条卡 960s 的消息 action 就是 `restart`(见
#: `[#564] deadline-enforced ... action=restart`),而 restart/rebuild/delete/suspend/restore
#: 都在词汇表里。
#:
#: **#604 后续项已落地(2026-08-25):`start` 补进死线词汇表,七档变八档。** 上面那条"start
#: 保持原样"的取舍是**当时**的正确判断,但它同时意味着短退避这半边在实践中永远不触发 ——
#: 唯一会单独跑 launch、因而能拿到 `rc=75` 的动作恰恰就是 `start`(`restart` 走
#: `stop-vm && sleep 2 && launch-vm`,撞锁时先死在 stop 上,`stop-vm.sh:207` 是 exit 1
#: 不是 75)。所以真机上 `start` 路径的组头阻塞一点没改善。现在 `start` 有 180s 死线
#: (`ACTION_START`,预算 0+60+120)与 `deadline_executor` 的终态兜底,本判据对它**自动**
#: 成立 —— 这里不需要任何改动,因为判据读的是 `DEADLINE_ACTIONS` 本身而不是抄一份清单。
_LIFECYCLE_RETRY_BACKOFF_SEC = (30, 60, 120)


def _deadline_backstopped_action(action):
    """这个 action **这一类**是否有 #564 死线兜底。只是必要条件,见下面那个函数。"""
    return str(action or "") in set(_create_deadline.DEADLINE_ACTIONS)


def _lifecycle_shortening_is_safe(action, msg):
    """缩短**这条消息**的重投退避是否安全。

    判据不是「action 属于死线词汇表」而是「**这条消息**真带一个会在缩短后的总窗口内到点的死线」——
    Codex 独立复审第 5 轮指出前者不够(它是我上一版的判据):

      · action 只说明**这类操作**有兜底,不保证**手上这条消息**带得上。升级期在飞的老消息
        没有死线字段,consumer 对它走 `is_expired` 的 fail-safe(返 False、不丢弃),于是
        #564 的兜底对它根本不生效;
      · 死线是可配的(`deadline_config`)。配得比缩短后的总窗口还长时,兜底会**晚于** DLQ
        到点 —— 那等于没有兜底。

    两种情况下缩短窗口都是在没有兜底的前提下提高「已受理的操作永久丢失」的概率,所以一律
    不缩、退回队列默认的 960s。方向与本文件其它 fail-safe 一致:看不出来就按没有兜底算。
    """
    if not _deadline_backstopped_action(action):
        return False
    try:
        deadline = int((msg or {}).get(_create_deadline.MSG_DEADLINE_KEY))
    except (TypeError, ValueError):
        return False
    # 缩短后的总窗口 = 头部之和 + 之后回落的那一次默认可见性。
    window = sum(_LIFECYCLE_RETRY_BACKOFF_SEC) + _LIFECYCLE_DEFAULT_VISIBILITY_SEC
    return deadline - int(time.time()) <= window


def _lifecycle_retry_backoff_sec(rec):
    """本次重投前等多久。返回 `None` = **不动可见性**,用队列默认的 960s。

    两处刻意的 `None`:
      · **最后一次投递**(`receiveCount >= LIFECYCLE_MAX_RECEIVE_COUNT`)不改可见性 ——
        这一次失败后消息就进 DLQ,改它只会推迟 DLQ 的发现,而"DLQ 非空 = 100% 是 bug"
        是运维赖以判障的信号。
      · **超出头部长度**之后回落默认,把剩余的重试预算留给队列自己的节奏。

    `_receive_count` 缺失/不可解析时返 0,这里当第一次、取最短退避。方向与
    `_terminal_before_dlq` 相反是刻意的:那里"看不出来就不回写终态"(误判代价是把还会
    成功的操作提前判死),这里"看不出来就早点重投"(误判代价只是多消费一次,消费幂等)。
    """
    n = _receive_count(rec)
    if n >= _clients.LIFECYCLE_MAX_RECEIVE_COUNT:
        return None
    if n < 1:
        n = 1
    if n > len(_LIFECYCLE_RETRY_BACKOFF_SEC):
        return None
    return _LIFECYCLE_RETRY_BACKOFF_SEC[n - 1]


def _shorten_lifecycle_visibility_best_effort(rec, result, action, msg):
    """#604 —— 良性 flock-skip 的留队列重投不必等满队列默认的 960s。

    判据严格:必须是响应 body 里**显式声明**的 `LAUNCH_IN_PROGRESS`
    (`core.ssm_dispatch.LAUNCH_IN_PROGRESS_CODE`)。其余 5xx 的重投节奏一个字不动 ——
    它们可能是真失败,早重投只会更快耗尽 DLQ 预算,而 DLQ 非空是「100% 是 bug」的告警信号。

    best-effort:改可见性失败不影响正确性(消息仍按默认 960s 兜底重投,只是慢),所以异常
    只打日志、绝不炸 invocation —— 与 `dispatch_service` 的两处同款先例
    (`_shorten_visibility_best_effort` / `_deadline_aware_visibility_best_effort`)一致。
    不发新消息,故没有 send/write 原子性问题。
    """
    if not isinstance(result, dict):
        return
    if not _lifecycle_shortening_is_safe(action, msg):
        # 没有可依赖的死线兜底(action 不在死线词汇表 / 这条消息没带死线 / 死线晚于缩短后的
        # 总窗口):重试窗口是这条操作唯一的收敛机会,不缩。
        return
    try:
        body = json.loads(result.get("body") or "{}")
    except (TypeError, ValueError):
        return
    if (
        not isinstance(body, dict)
        or body.get("code") != _ssm_dispatch.LAUNCH_IN_PROGRESS_CODE
    ):
        return
    rh = rec.get("receiptHandle")
    if not rh or not sqs or not LIFECYCLE_QUEUE_URL:
        return
    delay = _lifecycle_retry_backoff_sec(rec)
    if delay is None:
        # 最后一次投递 / 已超出头部:不动可见性,用队列默认的 960s。见该函数的 docstring。
        return
    try:
        sqs.change_message_visibility(
            QueueUrl=LIFECYCLE_QUEUE_URL,
            ReceiptHandle=rh,
            VisibilityTimeout=delay,
        )
        print(
            f"[#604] flock-skip {rec.get('messageId')} 重投退避 {delay}s"
            f"(原队列默认 960s),不再占住 FIFO 组头"
        )
    except Exception as e:  # noqa: BLE001 —— 见 docstring:失败只是退化,不炸 invocation
        print(f"[#604] shorten lifecycle visibility non-fatal: {e}")


def _terminal_before_dlq(rec, action, tenant_id, msg, why):
    """#564 G6 —— **消息即将进 DLQ 之前**把租户回写成终态 + 落机器可读原因。

    判据是 `ApproximateReceiveCount >= LIFECYCLE_MAX_RECEIVE_COUNT`:到这一次,本次失败
    之后 SQS 就把消息转进 DLQ,**不会再有下一次消费**。所以这是最后一个能写终态的时机。

    **为什么不建 DLQ 消费者**(考虑过,放弃了):客户表格明文「DLQ 只负责兜底告警」,而
    #562 §3.1 把「DLQ 非空 = 100% 是 bug」当运维判据。建一个消费者会把 DLQ 变成**正常
    失败通道**,那条最有用的告警信号就废了。在进 DLQ 之前写,两件事都保住:租户有终态,
    DLQ 仍然只在真出缺陷时非空。

    **不写的后果**(#532 的真机证据,ap-southeast-1 2026-08-18):两个租户 running→deleting
    之后永久卡住,SSM 回执 `[oc:delete] FATAL 拉取 delete-vm.sh 失败`,那两条消息耗尽重投
    (`ReceiveCount=6`)进了 DLQ,**再无人接管**。客户侧看到的就是一个永不终结的 `deleting`。

    归因用 `system_error`(由 `fence_delivery_exhausted` 给定):它的已发布语义是"出现即
    缺陷、报障、重试无益",与「DLQ 非空 = 100% 是 bug」逐字对齐。**不用**
    `deadline_exceeded_in_flight` —— 投递耗尽时死线可能压根没到,拿它描述会撒谎。

    `delete` 仍走它自己的例外(`_fence_failed` 里那一支):只写
    `delete_fail_reason` + `delete_reported_failed_at`,**不动 `status`** —— 600s 只约束
    答复,删除不得丢弃。消息照旧进 DLQ 当告警,但删除链条上的状态锚没被破坏。

    「回写前先读租户当前状态再决定」这条要求是**结构性**满足的:`_fence_failed` 的 CAS 锚
    住它读到的那个中间态,租户若已被别的路径推成终态就什么都不写(#532:盲目重写一个已
    failed 的租户会制造孤儿 VM)。
    """
    rc = _receive_count(rec)
    _max = _clients.LIFECYCLE_MAX_RECEIVE_COUNT
    if rc < _max:
        return  # 还会有下一次消费,现在写终态就是提前判死
    if action not in _create_deadline.DEADLINE_ACTIONS:
        # start/stop/pause/resume/reset 不在死线词汇表里,没有 `<action>_fail_reason`
        # 字段可落 —— 对它们调 `fail_reason_attr()` 会 raise。它们进 DLQ 仍有告警。
        print(
            f"[lifecycle-consumer] #564 {action} {tenant_id} 投递耗尽"
            f"(rc={rc}/{_max}),但该动作不在死线词汇表里,只靠 DLQ 告警"
        )
        return
    outcome = _dl_executor.fence_delivery_exhausted(
        tenant_id,
        action,
        msg.get(_create_deadline.MSG_DEADLINE_KEY),
        observed_op_id=msg.get("_op_id"),
    )
    print(
        f"[lifecycle-consumer] #564 {action} {tenant_id} 投递耗尽(rc={rc}/{_max}),"
        f"进 DLQ 前已回写终态(fence={outcome});最后一次失败: {why}"
    )


def _release_lifecycle_lease_if_mine(tenant_id, op_id):
    """#564 G3 —— 丢弃一条过期消息之后,放掉它入队时占的生命周期租约。

    **不放会把"超时"变成"卡死更久"**(Codex 独立复审第 1 轮抓出的真缺陷):
    `_FENCED_LIFECYCLE_ACTIONS`(rebuild/migrate/reset/delete/restart/suspend)在**入队时**
    就取了租约,原本由 consumer 执行完那次操作的正常路径放掉。我加的丢弃分支跳过了执行,
    于是没人放 —— 租户被锁在"做不了任何生命周期操作"的状态里直到租约自然过期(**1800s**),
    而它明明已经是终态了。客户看到的是"操作没了、而且我还改不了它"。

    **为什么读一次再放,而不是把 fence_epoch 塞进消息体**:`lifecycle_fence.release()` 的条件
    是 `op_id` 与 `epoch` **双锚**,所以拿读到的 epoch 去放是安全的 —— 期间若换了别的操作,
    双锚不成立、CCF、返 False、什么都不动。而读一次能同时覆盖**升级期那批发出时还没有
    fence_epoch 字段的在飞消息**;改消息格式则要维护两条路径。

    只在 `active_lifecycle_op_id` 确实是本条消息的 `_op_id` 时才放 —— 否则那把租约属于
    另一次操作,放掉它就是替别人解锁。
    """
    if not tenant_id or not op_id:
        return
    try:
        cur = _lifecycle_fence.read(tenant_id)
        if (cur or {}).get("active_lifecycle_op_id") != op_id:
            return
        _lifecycle_fence.release(tenant_id, op_id, cur.get("lifecycle_fence_epoch"))
    except Exception as e:  # noqa: BLE001 —— 放锁失败不该让一条已判过期的消息回队列绕圈:
        # 租约本身有 1800s 自然过期兜底,而重投会让同一条过期消息反复走这条路。
        print(f"[#564] release-lease {tenant_id}/{op_id} 失败(不阻断): {e}")


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
            # #413 P0 — 透传 producer 生成的 per-call op_id(lifecycle_dispatch:_op_id)。
            # SQS 重投同一条消息 body 不变 → 同一 op_id,给下游一个贯穿"同一逻辑操作所有
            # 重投"的稳定标识(rebuild 失败事件/日志据此归并;fence 完整版的 claim/check
            # 也挂这个钩)。缺失(老消息/非队列路径)时下游自行兜底,不阻断。
            if msg.get("_op_id"):
                ev["_op_id"] = msg["_op_id"]
            # ── #564 G3 —— 消费前判过期,在【任何动作之前】 ──────────────────────
            # 位置的正当性:上面全是纯解析(json.loads + dict.get + 组 ev),零副作用,
            # 所以在这里放弃是"一步未动"。往下一行就开始真实动作了。
            _dl = msg.get(_create_deadline.MSG_DEADLINE_KEY)
            if _dl is not None:
                # create 重放时**继承**这个死线而不是重算 —— 走 event 而不是 body,
                # 因为 body 是客户可控的 POST 内容(见 tenant_service 那段说明)。
                ev["_deadline_epoch"] = _dl
            # 缺死线字段(升级期的在飞消息)→ `is_expired` 返 False → 不丢弃,走正常链路。
            # 那是刻意的 fail-safe:不许因为缺字段就丢掉客户已受理的操作。
            if _create_deadline.is_expired(_dl, int(time.time())):
                if action == "delete":
                    # **delete 是例外**(客户 2026-08-21):600s 只约束【给上层的答复】,
                    # 而**删除不得丢弃**。到点只回报失败(由 deadline_executor 落
                    # `delete_reported_failed_at`,它不动 status),这里**继续执行删除** ——
                    # 丢掉这条消息等于让盘和 VM 就地搁浅。
                    # 回报是 best-effort:删除本身继续走,不因回报失败而中止;真漏了的话
                    # 每分钟一拍的行扫描还会补报一次。租约由删除的正常路径放。
                    print(
                        f"[lifecycle-consumer] #564 delete {tid} 已过死线 {_dl},"
                        "按契约【继续执行】,只回报失败"
                    )
                    _dl_executor.fence_expired_tenant(tid, "delete", _dl)
                else:
                    # 其余动作:不执行、置终态、放围栏租约、ack 删除。**顺序不能变。**
                    _outcome = "raced"
                    if action in _create_deadline.DEADLINE_ACTIONS:
                        _outcome = _dl_executor.fence_expired_tenant(
                            tid, action, _dl, observed_op_id=msg.get("_op_id")
                        )
                    if _outcome == "error":
                        # **围栏没成功就绝不 ack**(Codex 独立复审第 1 轮抓出的真缺陷):
                        # 那会让消息永久消失【而且】租户还卡在中间态 —— 「过期即终态」这条
                        # 承诺直接落空,而且是静默落空。留队列重投;若持续失败最终进 DLQ,
                        # 而「DLQ 非空 = 100% 是 bug」正是这种情形该发出的信号。
                        print(
                            f"[lifecycle-consumer] #564 {action} {tid} 已过死线但围栏失败,"
                            "不 ack,留队列重投"
                        )
                        failures.append({"itemIdentifier": mid})
                        continue
                    # 围栏落地(或确认这行已不是我那次)之后才放租约、才 ack。
                    # ack 而不是进 batchItemFailures —— 业务判死是一次【成功处理】,
                    # 进 DLQ 会污染「DLQ 非空 = 100% 是 bug」这条语义(与通道 A 同一条口径)。
                    _release_lifecycle_lease_if_mine(tid, msg.get("_op_id"))
                    print(
                        f"[lifecycle-consumer] #564 dropped expired {action} for {tid}"
                        f" (deadline={_dl}, late_by={int(time.time()) - int(_dl)}s,"
                        f" fence={_outcome});租户已按死线处置(围栏成终态或仅记录排队死,fence 结果见上),租约已放,消息 ack"
                    )
                    continue
            # ── #565:还没过期,但**剩余时间已装不下执行段** ──────────────────────
            # 上面那档拦的是「已经过期」。这一档拦的是「还有 20 秒,而这个动作最坏要跑
            # 120 秒」—— 不拦就是**白干**:host 侧真的去起 VM / 做备份,跑到一半死线到点,
            # 死线执行者把租户判 failed,而那条 SSM 命令**没有任何机制能撤回**,它仍会
            # 跑完并留下副作用(#565 G1-a 记的「上层失败、底层成功」就是这个形状)。
            #
            # 复用 create 侧那个已被评审过的原语(`core/create_deadline.doomed_by_deadline`,
            # 形态第 4 条),第三个参数就是执行段 —— `exec_sec(action)` 是 #565 G1 落的
            # 三段预算里的执行段,每档各自的最坏值,不是一个共用常量。
            #
            # **位置必须在这里**:与过期闸同一段(往下一行就开始真实动作),所以放弃时是
            # 「一步未动」。处置也刻意与过期闸**逐字同一套**(围栏 → 放租约 → ack),
            # 免得两档的失败面貌不一致让运维分不清。
            #
            # **delete 同样例外**,理由与上面那档一样(客户 2026-08-21:600s 只约束答复、
            # 删除不得丢弃)。判据是「delete 一律不拒」,所以这里连判都不判 —— 省一次
            # 计算不是目的,把「delete 不会走进任何拒绝分支」写成结构性事实才是。
            _doomed_action_ok = (
                action != "delete"
                and action in _create_deadline.DEADLINE_ACTIONS
            )
            if _doomed_action_ok and _create_deadline.doomed_by_deadline(
                _dl, int(time.time()), _create_deadline.exec_sec(action)
            ):
                _outcome = _dl_executor.fence_expired_tenant(
                    tid, action, _dl, observed_op_id=msg.get("_op_id")
                )
                if _outcome == "error":
                    # 与过期闸同款:围栏没成功就绝不 ack(否则消息消失而租户卡中间态)。
                    print(
                        f"[lifecycle-consumer] #565 {action} {tid} 剩余装不下执行段但围栏失败,"
                        "不 ack,留队列重投"
                    )
                    failures.append({"itemIdentifier": mid})
                    continue
                _release_lifecycle_lease_if_mine(tid, msg.get("_op_id"))
                print(
                    f"[lifecycle-consumer] #565 refused doomed {action} for {tid}"
                    f" (deadline={_dl}, remaining="
                    f"{_create_deadline.remaining_sec(_dl, int(time.time()))}s,"
                    f" exec_budget={_create_deadline.exec_sec(action)}s,"
                    f" fence={_outcome});一步未动,租户已围成终态、租约已放,消息 ack"
                )
                continue
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
                # #263 — 透传 keep_data/skip_backup:producer 入队时把原始 query 值放进
                # extra,这里重建成 query_params。恒传空 {} 会让 keep_data 默认 "true"
                # (tenant_service 默认软删),该删的盘悄悄没删(no-data-loss 反向)。
                # None(调用方没传该 query)不放进 dict,让 delete_tenant 的默认值生效。
                # force 同样要重建(#469 P2,codex 独立复审第十八轮):delete_tenant 的
                # 中间态准入检查跑在入队【之前】,重放时缺 force 就被挡成 409,而 409 是
                # 4xx → 下面明确不重投 → 消息消费掉、删除永不发生。P2 的强制删除出口
                # 会在队列开启时整条失效,而调用方只看到 202。
                _q = {
                    k: str(extra[k])
                    for k in ("keep_data", "skip_backup", "force")
                    if extra.get(k) is not None
                }
                result = delete_tenant(tid, _q, ev)
            else:
                result = tenant_action(tid, action, extra or None, ev)
            code = result.get("statusCode", 500) if isinstance(result, dict) else 200
            if code >= 500:
                # 5xx(SSM throttle / 容量争用)→ 留队列退避重试
                _terminal_before_dlq(rec, action, tid, msg, f"HTTP {code}")
                # #604 —— 良性的 flock-skip 不该等满 960s:那会占住 per-tenant FIFO 组头,
                # 把同租户后续操作全堵掉 16 分钟。只缩这一种,判据是 body 里的 code。
                _shorten_lifecycle_visibility_best_effort(rec, result, action, msg)
                failures.append({"itemIdentifier": mid})
            # 4xx(owner/参数错)不重试:消息消费掉,避免毒消息无限重投
        except Exception as e:  # noqa: BLE001
            print(f"[lifecycle-consumer] msg {mid} error: {type(e).__name__}: {e}")
            # 异常这一支同样可能是**最后一次**投递 —— 漏了它,毒消息那一类(每次都抛)
            # 就正好绕过整道门:它从来走不到上面那个 5xx 分支。
            try:
                _terminal_before_dlq(
                    rec, action, tid, msg, f"{type(e).__name__}: {e}"
                )
            except Exception as e2:  # noqa: BLE001 —— 回写失败不该盖掉原始失败
                print(f"[lifecycle-consumer] #564 terminal-before-dlq 失败: {e2}")
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
            # #469 P5(codex 独立复审抓出的桥面缺口):job 记录里已经写了 enqueued
            # (fleet_service.run_batch_job),但这个查询端点没返回它 —— 而它是调用方
            # 唯一的进度来源。漏掉的后果不是少个字段,而是"仅入队"与"真做完"在对外
            # 视图里仍然分不开:succeeded 不含它们、failed 也不含,调用方只能看到
            # total 与 done 对不上却无从知道差在哪。P5 的"不谎报"到这里才算闭合。
            "enqueued": job.get("enqueued", []),
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
from core import capacity as _capacity  # noqa: E402
from core import clients as _clients  # noqa: E402
from core import host_profile as _host_profile  # noqa: E402

# #564 G5 — 七档死线的生效值(SSM Parameter → 缓存 → 回落 env/默认)。放这里而不是底部:
# 底部那批是"阶段化搬迁解依赖环"的产物,而本模块只依赖 core.*、不反向依赖 handler,无环。
from core import deadline_config as _deadline_config  # noqa: E402

# #564 G3 — 消费前判过期要用的两样:死线字段/键名与 `is_expired` 的 fail-safe 口径,
# 以及把过期操作立刻围成终态的那一个入口(与每分钟一拍的扫描共用同一份归因与写法)。
from core import create_deadline as _create_deadline  # noqa: E402
from core import lifecycle_fence as _lifecycle_fence  # noqa: E402
from services import deadline_executor as _dl_executor  # noqa: E402

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
taint_host = _host_service.taint_host
untaint_host = _host_service.untaint_host


def _taint_authz_denied(ident):
    """#539 followup —— taint 授权门:api-key(is_admin) 或 Cognito operator+ 放行,
    viewer / 未验证 token 返 403。taint 路由已从前置 _rbac_check skip(见 auth._RBAC_SKIP),
    这是唯一的授权门 —— 与 registry/bootstrap 的 handler 内 identity-based 门同款,只是把 admin
    放宽到 operator(会议定 taint 为 operator+ 运维动作)。返回 None=放行,否则 403 响应。

    #108 —— platform-scoped API key 只能碰自己 platform 的 tenant,不是整个机队的 blanket
    admin(auth._get_caller_identity 明载"NOT a blanket admin over the whole fleet even if
    role/api-key would otherwise say so")。taint 作用于 host(跨 platform 的基础设施),故 scoped
    key 一律无权,即使其 api-key 路径 is_admin=True。此检查放在 is_admin 放行【之前】,与
    _assert_owner_or_admin 的 scope-before-admin 顺序一致(否则 scoped key 会绕过隔离标污任意 host)。"""
    if ident.get("platform_scope") is not None:
        return _resp(403, {"error": "forbidden: platform-scoped key cannot taint fleet hosts"})
    role = ident.get("role", "viewer")
    if ident.get("is_admin") or _auth._role_satisfies(role, "operator"):
        return None
    return _resp(403, {"error": "forbidden", "rbac": {"role": role, "required": "operator"}})


def _taint_host_route(instance_id, body, event):
    """POST /hosts/{instance_id}/taint —— 授权后调 service。owner_id 作 tainted_by 审计。"""
    ident = _get_caller_identity(event or {})
    denied = _taint_authz_denied(ident)
    if denied:
        return denied
    return taint_host(instance_id, body, ident.get("owner_id") or "")


def _untaint_host_route(instance_id, event):
    """DELETE /hosts/{instance_id}/taint —— 授权门同 _taint_host_route。"""
    denied = _taint_authz_denied(_get_caller_identity(event or {}))
    if denied:
        return denied
    return untaint_host(instance_id)
cleanup_terminated_host = _host_service.cleanup_terminated_host
rootfs_version = _host_service.rootfs_version
_run_pull_pipeline = _host_service._run_pull_pipeline  # #217 fix(504) async pull worker
rootfs_drift = _host_service.rootfs_drift
_get_manifest = _host_service._get_manifest
list_images = _host_service.list_images
list_image_versions = _host_service.list_image_versions  # #337(原#217 /snapshots)— 列镜像版本快照供 console 选
create_image_snapshot = _host_service.create_image_snapshot  # #376 — 打版本快照(等价 snapshot-version.sh)
delete_image_snapshot = _host_service.delete_image_snapshot  # #394 — 删一条快照记录(引用保护)
refresh_rootfs = _host_service.refresh_rootfs
pull_image = _host_service.pull_image  # #217 V2 — snapshot pull → install live
pull_image_progress = _host_service.pull_image_progress  # #309 — tail /tmp/<job_id>.txt
host_image_slots = _host_service.host_image_slots  # #394 — 真机实读 host 磁盘镜像状态
copy_file_from_s3 = _host_service.copy_file_from_s3  # #309 — single file S3 → EC2 (allowlist target)

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
# #609 —— gsi_tenant_user 未部署时 _query_user_tenants 抛这个,两个 per-user fleet
# 端点转成结构化 503 而不是让它冒到兜底 except 变成 500 + DDB 原文回显。
_TenantUserIndexUnavailable = _fleet_service.TenantUserIndexUnavailable
fleet_power = _fleet_service.fleet_power

# #566 拆分② — fleet guest 出网防火墙运维 API(POST /hosts/egress)。
from services import egress_admin_service as _egress_admin_service  # noqa: E402

fleet_egress = _egress_admin_service.fleet_egress
fleet_egress_status = _egress_admin_service.fleet_egress_status
fleet_egress_revisions = _egress_admin_service.fleet_egress_revisions
fleet_egress_revisions_delete = _egress_admin_service.fleet_egress_revisions_delete
fleet_egress_chain = _egress_admin_service.fleet_egress_chain
fleet_egress_rollback = _egress_admin_service.fleet_egress_rollback
fleet_egress_allow_validate = _egress_admin_service.fleet_egress_allow_validate
_execute_batch = _fleet_service._execute_batch
_enqueue_batch_job = _fleet_service._enqueue_batch_job
run_batch_job = _fleet_service.run_batch_job
_resolve_filter = _fleet_service._resolve_filter
_FLEET_VALID_ACTIONS = _fleet_service._FLEET_VALID_ACTIONS
_FLEET_START_PARALLEL = _fleet_service._FLEET_START_PARALLEL
_FLEET_STOP_PARALLEL = _fleet_service._FLEET_STOP_PARALLEL

# #517 stage 4 — bounded, drift-gated rolling identity/image adoption.
from services import rolling_upgrade_service as _rolling_upgrade_service  # noqa: E402

submit_rolling_upgrade = _rolling_upgrade_service.submit_rolling_upgrade
run_rolling_job = _rolling_upgrade_service.run_rolling_job
get_rolling_job = _rolling_upgrade_service.get_rolling_job

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


def _image_slot_op(instance_id, event, op):
    """#394 step5 —— promote-canary / reclaim-images 的统一入口(admin-only)。

    这些都改变一台 host 的 live 镜像指向(或回收版本),属不可逆镜像变更 → admin 门
    (ADR §8:api-key 只承担 usage-plan,不单独授予管理员变更能力)。admin 判定与
    fleet_power/registry 同款 identity-based(本 stack 无自定义 authorizer)。
    回滚不在此列:回滚 = pull 老版到 live(pull-image,operator 权限,本地已完整则秒级翻指针)。
    cleanup-canary 已移除(精简 API):放弃 canary 靠下次 pull 覆盖 / promote 清空。
    """
    if not _get_caller_identity(event or {}).get("is_admin"):
        return _err(403, "ACCESS_DENIED", f"{op} requires admin role")
    from services import image_slot_service

    ssm_wait = _host_service._ssm_wait  # 复用既有 SSM 同步等待(含轮询/超时语义)
    headers = event.get("headers") or {}
    if op == "promote-canary":
        return image_slot_service.promote_canary(
            instance_id, event.get("body"), ssm_wait, headers
        )
    return image_slot_service.reclaim_images(instance_id, headers, ssm_wait)


def _bootstrap_versions(event):
    """GET /bootstrap/versions — 列 host+edge 可切换的 bootstrap 版本(admin-only)。

    admin 门与 fleet_power/registry 同款 identity-based(本 stack 无自定义 authorizer,
    requestContext.authorizer.role 永远为空)。这里刻意用 is_admin 而非 role:api-key 路径
    在 core/auth.py 里 role 解析成 viewer 但 is_admin=True(受信自动化全权)—— 未来读者别把
    这行"当 bug 修成 role==admin",那会锁死持 key 的运维脚本。
    """
    if not _get_caller_identity(event or {}).get("is_admin"):
        return _err(403, "ACCESS_DENIED", "bootstrap version listing requires admin role")
    from services import bootstrap_version_service

    return bootstrap_version_service.list_versions()


def _bootstrap_promote(event):
    """POST /bootstrap/promote — 切到某个已存在的 bootstrap 版本(admin-only,见上门说明)。

    改的是整机队【下次开机】读哪个 init 脚本(host/edge 各一套 LT+ASG),属高权限、可回退但
    影响面广的变更 → admin。body {fleet, target_sha, expected_current_sha}。
    """
    ident = _get_caller_identity(event or {})
    if not ident.get("is_admin"):
        return _err(403, "ACCESS_DENIED", "bootstrap promote requires admin role")
    import json as _json

    from services import bootstrap_version_service

    body = event.get("body")
    if isinstance(body, str):
        try:
            body = _json.loads(body) if body else {}
        except (ValueError, TypeError):
            return _err(400, "VALIDATION", "body must be a JSON object")
    if body is None:
        body = {}
    # 合法 JSON 但非对象(数组/标量/true)→ body.get 会抛 → 先挡成 400,不 500(同
    # create_image_snapshot 的 _parse_body 纪律)。
    if not isinstance(body, dict):
        return _err(400, "VALIDATION", "body must be a JSON object")
    fleet_raw = body.get("fleet")
    # fleet 非字符串(如 fleet:1)会让 .strip() 抛 → 先判类型挡成 400,不 500。
    if fleet_raw is not None and not isinstance(fleet_raw, str):
        return _err(400, "VALIDATION", "fleet must be a string")
    fleet = (fleet_raw or "").strip()
    return bootstrap_version_service.promote(fleet, body, ident)


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
            "envelope_hint": "enc:v1:<alg_code>:<key_id>:<hybrid_flag>:<base64(ciphertext)>",
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
    import json as _json

    # #615 —— admin 门之后原先零校验:一个 `{"__invalid__": true}` 就把平台级收件密钥
    # 关掉(usw2 真机实测返 200 并真的置 enabled=false)。这里补前置校验并 fail closed:
    # 任何被拒的请求都不许已经动过 durable state。
    #
    # 兼容性(API-DESIGN-REVIEW 仓库不变量「新增字段必须可选、省略时保持旧行为」):
    # key_id 是**可选**的。仓内唯一调用方 deploy/console-bff/web/js/app.rsa.js 的
    # disableRsaKey() 不传 body;core/auth.py 的 _RBAC_SKIP 注释又把"持 key 的运维
    # 脚本"列为预期调用方,仓外很可能有同样不传 body 的客户脚本。把 key_id 做成必填
    # 会当场弄坏这两类调用方,所以空 body 必须继续成功。
    raw = event.get("body") if event else None
    if isinstance(raw, str):
        raw = raw.strip()
        if raw:
            try:
                body = _json.loads(raw)
            except (ValueError, TypeError):
                return _err(400, "VALIDATION", "body must be valid JSON")
        else:
            body = {}
    else:
        body = raw if raw is not None else {}
    # 合法 JSON 标量/数组("5"、"[1]")也能过 json.loads,但 body.get 会抛 → 先挡成 400
    # (与 host_service.create_image_snapshot 同款)。
    if not isinstance(body, dict):
        return _err(400, "VALIDATION", "body must be a JSON object")
    unknown = sorted(set(body) - {"key_id"})
    if unknown:
        return _err(
            400,
            "VALIDATION",
            "unknown field(s) in body: %s; only 'key_id' is accepted"
            % ", ".join(unknown),
        )

    from services import recipient_key_service as _rk

    # 传了 key_id 就必须指向当前那把。校验**不在这里做** —— 它作为条件写的一部分下沉到
    # disable_current,由 DynamoDB 在同一次 update_item 里判。原因(#615 独立 review 指出):
    # 在 handler 里先 get_current_key() 比对、再调 disable_current(),中间隔着一次网络往返,
    # 而 disable_current 自己还会重新解析 current —— 期间若有人 register 了新 key(轮换),
    # 那次写就落在**新**那把上而调用方拿到 200,客户以为禁的是旧 key,实际把刚上线的新 key
    # 关了,全平台凭据获取随即中断。检查与动作必须原子。
    requested = body.get("key_id")
    if requested is not None and (not isinstance(requested, str) or not requested):
        return _err(400, "VALIDATION", "body.key_id must be a non-empty string")

    try:
        result = _rk.disable_current(expected_key_id=requested)
    except _rk.RecipientKeyChanged:
        return _err(
            409,
            "CONFLICT",
            "key_id does not match the current recipient key; "
            "re-read GET /recipient-key and retry",
        )
    if requested is not None and result is None:
        # 传了 key_id 却没有 current key:无从匹配,不能当成"匹配成功"放行。
        return _err(
            409,
            "CONFLICT",
            "no current recipient key to disable; "
            "re-read GET /recipient-key and retry",
        )
    return _resp(200, {"disabled": True, "key": result})
