# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""services/tenant_service — 租户 CRUD + 生命周期动作 + resize/access/backup 解析。

handler-split #132 阶段2 —— 从 handler.py 逐字机械搬迁这 8 个函数,函数体零逻辑改动:
  _validate_purchase · _redact_tenant · create_tenant · tenant_access_grant ·
  delete_tenant · tenant_action · _resolve_backup · tenant_resize
唯一的字面改动是把测试会重绑的 clients 符号、以及测试 patch 在核心模块上的依赖函数
改成属性访问(`clients.X` / `scheduling.X` / …),见下方死结说明。

依赖方向(import-layers 合法):services → core(clients/utils/auth/scheduling/vkey/
ssm_dispatch/legacy_alb/skills/audit)+ services.lifecycle_dispatch(横向 services 允许)。
不反向 import handler / routes / consumers / router。

**属性访问解死结(scheduling/audit/lifecycle 域已验证的跨模块串染)**:
测试用 `spec_from_file_location` 反复 exec handler,再对 handler 模块(`api`)重绑符号
注入 fixture:① clients 值符号(`api.tenants_table = mock`、
`patch.object(api, "LIFECYCLE_QUEUE_URL", ...)`、`api.CPU_OVERCOMMIT_RATIO = 1.0`)——
若本模块 `from core.clients import tenants_table` 做值绑定,会持有原始对象、看不到
测试重绑 → 测试红。故本模块所有 clients 符号一律走 `import core.clients as clients` +
函数体内 `clients.tenants_table`,测试 patch `clients.X`(规范源)即全局生效。
② 依赖函数(`_find_host`/`_launch_vm`/`_ssm_run`/`_get_caller_identity`/…):这 8 个
函数原本在 handler 名字空间读裸名,测试 `patch.object(api, "_find_host", ...)` 才生效;
搬到本模块后调用点在这里,若 `from core.scheduling import _find_host` 做值绑定,patch
`api._find_host`(handler facade 的别名)看不到。故依赖函数也走模块属性访问
`scheduling._find_host(...)`,测试改 patch 对应核心模块(`api._scheduling` 等,与 handler
facade 指向同一模块对象)即可全局生效。
"""

import base64
import json
import os
import re
import shlex
import time
import secrets

import boto3
from botocore.exceptions import ClientError

import core.capacity as capacity
import core.clients as clients
import core.create_deadline as create_deadline  # #562 — 创建死线的唯一口径(纯函数)
import core.deadline_config as deadline_config  # #564 G5 — 死线的运行时载体(SSM Parameter)
import core.host_profile as host_profile
import core.host_taint as host_taint  # #540 — 放置侧读污点,判定复用写侧纯函数
import core.utils as utils
import core.auth as auth
import core.scheduling as scheduling
import core.vkey as vkey
import core.ssm_dispatch as ssm_dispatch

import core.skills as skills
import core.audit as audit
import core.image_channel as image_channel_mod  # #394 — image_channel 准入 + 版本固定
import core.image_ops as image_ops
import core.lifecycle_fence as lifecycle_fence
import services.action_idem as action_idem  # #456 — client_token 幂等(ADR §5.1)
import services.lifecycle_dispatch as lifecycle_dispatch
import services.registry_service as registry_service
import services.inflight_dedup as inflight_dedup
import services.name_dedup as name_dedup

# tenant-credential-contract — envelope 是 stdlib-only 叶子,纯函数值绑定安全
# (测试不 patch 它,不受本文件头部「属性访问解死结」约束)。
from core.envelope import (
    _validate_injected_parameters_v2,
    looks_encrypted,
    resolve_scheme,
)

# ── #475 必修1 —— 槽位认领失败的两种原因 ─────────────────────────────────────
# 分辨它们是承重的:调用方只在 _CLAIM_FULL 时把 host 拉出候选池。把"抢输槽位号"也当成
# "这台满了",等于把一台仍有余量的 host 从池子里删掉 —— 池里只剩一台有空间时就是立刻
# 503。模块级常量而非局部字面量,是为了让测试能引用同一个符号,不靠字符串巧合对齐。
_CLAIM_CONTENDED = "contended"
_CLAIM_FULL = "full"
# 无法归因(ALL_OLD 没捎回 Item,或值不可解析)。不能并进 CONTENDED —— 见
# _classify_claim_failure 的说明:那会让调用方既不换机也不刷新,空转掉整个重试预算。
_CLAIM_UNKNOWN = "unknown"


def _classify_claim_failure(err_response, cap_v, cap_m):
    """CCF 响应 → _CLAIM_FULL / _CLAIM_CONTENDED / _CLAIM_UNKNOWN。

    条件表达式混了四个子条件(next_vm_num 竞态 / vCPU 满 / 内存满 / 物理槽被占),而 CCF
    不说是哪个失败。带 ReturnValuesOnConditionCheckFailure=ALL_OLD 时 DDB 会把该 item 的
    旧值放进异常响应,据此判定(AWS 文档 WorkingWithItems「Returning the item attributes
    of a failed conditional write」)。

    走 boto3 **resource** 层时 err_response["Item"] 是【未反序列化】的类型化 JSON
    ({'N': '5'}),不是 python 值 —— moto 5.2.2 与真 DDB 一致(实测),故直接读 "N"。

    【为什么"读不到旧值"要单独成一档,而不是并进 CONTENDED】
    第三轮评审抓到的:并进 CONTENDED 会让调用方既不换机也不刷新(没有 Item 就没有新的
    next_vm_num),于是用同一个 stale expected 空转 8 次 —— 即便别的 host 有空间也 503。
    那正是本 issue 要修的故障形态在"无 Item"这条路上重现。典型触发是 host 行在读与 CAS
    之间被删掉(实例终止/回收),ALL_OLD 无东西可回。所以它必须是 UNKNOWN:调用方拿它去做
    一次强一致读再定夺,而不是盲目原地重试。
    """
    old = (err_response or {}).get("Item") or {}

    def _num(name):
        try:
            return int(old[name]["N"])
        except (KeyError, TypeError, ValueError):
            return None

    used_v, used_m = _num("used_vcpu"), _num("used_mem_mb")
    # 先归因能归的:任一可读字段已证明装不下 → FULL(能判就判,不多付一次读)。
    if (used_v is not None and used_v > cap_v) or (used_m is not None and used_m > cap_m):
        return _CLAIM_FULL
    # 【任一】字段读不出来就是 UNKNOWN,不能因为另一个"还有余量"就判 CONTENDED。
    # 依据是 DDB 语义:条件里 used_vcpu <= :cap_v AND used_mem_mb <= :cap_m 对【缺失
    # 属性】求值为假,缺一个就整条 AND 为假 —— 实测确认(moto:半行缺 used_vcpu 时该条件
    # 抛 CCF,而存在的 used_mem_mb 照常比较)。所以缺任一用量字段的 host,它的 CAS 会
    # 【永久失败】,原地重试注定耗尽预算。这种半行是真实存在的形态:#445 记录过 host-agent
    # 心跳的无条件 update_item 会建出只有心跳字段的行。第四轮评审抓到这条。
    if used_v is None or used_m is None:
        return _CLAIM_UNKNOWN
    return _CLAIM_CONTENDED


def _fresh_host_state(err_response):
    """从 CCF 捎回的旧值里取出【赢家写完后的当前状态】,供重试刷新本地 host 字典。

    而里面 `expected = h["next_vm_num"]` 读的是同一个 stale 字典,于是 CAS 条件
    `next_vm_num = :expected` 必然再次不满足 —— 8 次重试确定性全废。真机实测(apse1
    2026-08-14,6 路并发单 host):只改分类时 6 路里 4 路拿到
    "slot allocation contended out after retries",一次都没成功过。

    数据是白来的:ALL_OLD 已经把当前 item 放进异常响应,不必再读一次 DDB。
    """
    old = (err_response or {}).get("Item") or {}
    fresh = {}
    for name in ("next_vm_num", "used_vcpu", "used_mem_mb", "vm_count"):
        try:
            fresh[name] = int(old[name]["N"])
        except (KeyError, TypeError, ValueError):
            continue
    return fresh

# ── tenant 域私有常量(逐字搬自 handler.py 顶部;仅本域使用)──────────────────
# issue #59 (WI-E/M-1) — config_template is caller-controlled and flows into an
# SSM root shell command; its ONLY legitimate use is as an S3 path slug
# (launch-vm.sh: s3://$ASSETS_BUCKET/templates/openclaw/${CONFIG_TEMPLATE}/openclaw.json),
# so it must be a plain DNS-label. Reject anything with shell metacharacters,
# whitespace, or path separators at the edge (defense in depth still quotes it
# in _launch_vm). Empty == "no custom template" and is validated separately.
_CONFIG_TEMPLATE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
# tenant-credential-contract Task 3.5 — registry 按 config_template 分区,注入路径
# 复用同一 DNS-label 约束(别名而非新 regex:同一约束一个来源,防两处漂移)。
_DNS_LABEL_RE = _CONFIG_TEMPLATE_RE

# #93 idempotency key / #95 adversarial C-003/C-005/C-006 — client_token is a
# caller-supplied idempotency key that flows into an SSM command and log lines.
# Restrict to 4-128 printable ASCII (codepoints 33-126): no spaces, no control
# chars (\n \t \x00), no non-ASCII. .isascii() alone lets control chars through.
_CLIENT_TOKEN_RE = re.compile(r"^[\x21-\x7e]{4,128}$")
# #429 setpath uses a key array, so hyphenated and numeric segments are safe.
# Dots remain separators; empty/control-character/path-like segments are rejected.
_INJECTION_TARGET_RE = re.compile(
    r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z"
)
_HARDEN_CONFIG_TARGETS = (
    "gateway.controlUi.dangerouslyDisableDeviceAuth",
    "gateway.controlUi.enabled",
    "gateway.controlUi.allowedOrigins",
    "gateway.http.endpoints.chatCompletions",
    "models.providers.litellm.baseUrl",
    "models.providers.litellm.apiKey",
)
_DELETE_CLAIM_TTL_SECONDS = 900
# ⑭ codex 独立复审第九轮 —— suspend 必须进这个集合。
#
# 在此之前 suspend 【从不取 fence】,于是两件事同时成立而互相矛盾:
#   · suspend 的两个破坏性步骤(stop-vm、rm -rf)裸跑,没有 host_guard;
#   · 而 #469 的 reaper 会在 1200s 后把卡住的 suspending 回滚成活跃态。
# 于是一个只是"慢"的 suspend 可以在回滚之后才落地它的删盘 → 「row=running / 无 VM /
# 无盘」,#268 禁止的谎报形态。
#
# 我第七、八轮试图用"等 active_lifecycle_until 过期再回滚"来挡这条,但那道门当时是
# **空转的** —— 那个字段只由本集合里的动作写,suspend 不在里面,所以对一个 suspend
# 卡死的租户它通常根本不存在(我的注释却声称它守着 suspend 的窗口,那是错的)。
# 这是本轮之前我在这条路径上第五处被推翻的书面判断。
#
# 把 suspend 纳入 fence 后两件事一起成立:
#   · 租户行上真的有 suspend 自己的 active_lifecycle_until,reaper 那道门于此才有意义;
#   · 破坏性命令带上 host_guard,而 host_guard 同时校验 owner + epoch + **租约未过期**
#     (lifecycle_fence.py:264)。所以 reaper 只在租约过期后回滚 ⟹ 任何延迟落地的
#     suspend 命令必然撞 LIFECYCLE_FENCE_EXPIRED、exit 79,一步都不做。两者组合才闭合。
#
# 副作用是有意的:suspend 与其它生命周期动作之间从此互斥(并发时 409
# LIFECYCLE_IN_FLIGHT)。suspend 本来就是生命周期动作,这正是该集合的语义。
# 释放路径已有:外层 tenant_action 无条件调 _release_lifecycle_ctx,且只在 5xx 时保留
# 租约 —— 恰好是这里要的(失败留租约防重投撞车,成功立刻放手)。
#
# **restore 不加**(有意):它的破坏性动作是在新 host 起 VM,而 reaper 对 restoring 一律
# mark_stuck、从不回滚,所以本轮这条竞态对它不成立。没有已证实的缺陷就不动它。
_FENCED_LIFECYCLE_ACTIONS = frozenset(
    {"rebuild", "migrate", "reset", "delete", "restart", "suspend"}
)

# #501 — 健康位只由 health_check sweep 写(health_check/handler.py 的 vm_health/app_health
# update),而 sweep 跳过终态租户(`status not in ("deleted", "suspended")`)。于是删除后这三个
# 字段冻在删除前的值,DDB 里留下 status=deleted + vm_health=up + app_health=up 的行;软删无 TTL,
# 行永不消失,任何按健康位判「租户是否在役」的监控/巡检/排障都会把已删租户当成健康在役租户
# (客户排障实测已发生)。每条把 status 写成 deleted 的路径都必须一并 REMOVE 这三个字段——
# DDB 的 REMOVE 对不存在的属性是 no-op,所以 create 回滚路径带上它同样安全。
_STALE_HEALTH_FIELDS = "vm_health, app_health, last_health_check"
# #593 —— 软删必须一并 REMOVE q_rootfs_version(gsi_rootfs_version 的投影键)。删除只翻 status、
# 不清 q_rootfs_version 的话,软删租户永久留在该 GSI 分区(软删无 TTL)。GET /tenants?rootfs_version=
# 的 Limit 加在过滤 deleted 之前,分区被死行前置时小 limit 首页可能全是 deleted → 返回空列表,而
# tenant-stats 只统计非 deleted 仍显示有实例(两接口对不上)。q_rootfs_version 是纯查询投影键
# (展示仍用 rootfs_version),软删后删它可安全,且与 gsi_status 天然自洁(status 翻 deleted 即移出
# 其它状态分区)对齐。REMOVE 不存在的属性是 no-op,对未设该键的行零影响。
_STALE_ON_DELETE_FIELDS = f"{_STALE_HEALTH_FIELDS}, q_rootfs_version"
# create 回滚路径(mint token / clone-data / launch-vm 提交失败)共用的终态写法。
_ROLLBACK_DELETED_EXPR = f"SET #s = :s, updated_at = :t REMOVE {_STALE_ON_DELETE_FIELDS}"

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
_ORDER_ID_RE = re.compile(r"^[\x21-\x7e]{1,128}\Z")
_PLAN_TIERS = ("free", "standard", "pro", "enterprise")
_PURCHASE_PENDING = "pending"
_PURCHASE_PROVISIONED = "provisioned"

_TENANT_SECRET_FIELDS = (
    "channel_secret",
    "litellm_vkey",
    "cognito_channel_password",  # WI-002 — machine-user password, never to browser
    "gateway_token",  # #100 — per-tenant bearer protecting the gateway control UI;
    # GET /tenants was leaking it in plaintext, letting one x-api-key harvest EVERY
    # tenant's gateway_token (credential batch-exposure). Server-side only (see :1092).
    "injected_credentials",  # #118/#116 — platform-injected credential ciphertext
    # blobs; ciphertext (not plaintext), but still never echoed to any GET caller.
    "frozen_injection_plan",  # tenant-credential-contract Task 4.1 — frozen plan
    # entries carry value_ref (ciphertext or plaintext values); never echoed.
    "device_paired_b64",  # #415 — paired.json 自 7.1(v4)起在 tokens.operator 预铸
    # 一枚活 bearer token(免 approve 用)。该字段 #312 存进 tenants 表长期留存,
    # 若不脱敏会随 GET /tenants(list/detail 都过 _redact_tenant)原样回给持
    # x-api-key 的调用方 → 一把 key 批量收割每租户的 device token(与 #100 gateway_token
    # 同类批量泄漏)。launch-vm 直接查 DDB 重注入,不走 _redact_tenant,故脱敏不影响冷注入。
    "rebuild_ssm_command_id",  # ADR-rebuild-idempotency-sync-contract §5.4b —
    # 本次 rebuild 的 SSM CommandId,供服务端事后回查 SSM 执行记录做对账(§5.4a 路 1)。
    # 对客户无用(他们无权调 SSM),但可用于关联运维操作,没必要外露。健康巡检直接读
    # DDB 表、不经 _redact_tenant,故脱敏不影响对账读取。
)


def _normalize_client_token(raw):
    """Return the canonical token plus a validation error, if any."""
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return "", (
            "client_token must be a string of 4-128 printable ASCII chars "
            "(no spaces/control chars)"
        )
    token = raw.strip()
    if token and not _CLIENT_TOKEN_RE.fullmatch(token):
        return "", (
            "client_token must be 4-128 printable ASCII chars "
            "(no spaces/control chars)"
        )
    return token, None


def _extract_reapply_request(action, body):
    """Return canonical #429 fields only for rebuild/v1 upgrade actions."""
    if action not in ("rebuild", "upgrade") or not isinstance(body, dict):
        return None
    if action == "upgrade":
        return {
            "config_template": body.get("configTemplate"),
            "config_template_version": body.get("configTemplateVersion"),
            "force_reapply": body.get("forceReapply"),
        }
    return {
        "config_template": body.get("config_template"),
        "config_template_version": body.get("config_template_version"),
        "force_reapply": body.get("force_reapply"),
    }


def _normalize_v1_upgrade_body(body):
    """Map the legacy v1 camelCase upgrade body onto rebuild's snake_case body."""
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except Exception:
            return body
        if not isinstance(parsed, dict):
            return body
        body = parsed
    if not isinstance(body, dict):
        return body
    normalized = dict(body)
    aliases = {
        "imageChannel": "image_channel",
        "requestId": "client_token",
        "configTemplate": "config_template",
        "configTemplateVersion": "config_template_version",
        "forceReapply": "force_reapply",
    }
    for legacy, canonical in aliases.items():
        if legacy in normalized and canonical not in normalized:
            normalized[canonical] = normalized[legacy]
        normalized.pop(legacy, None)
    return normalized


def _reapply_requested(request):
    if not request:
        return False
    return bool(
        request.get("force_reapply") is True
        or request.get("config_template") is not None
        or request.get("config_template_version") is not None
    )


def _reapply_injection_targets(item):
    plan = item.get("frozen_injection_plan") or {}
    targets = list(_HARDEN_CONFIG_TARGETS)
    if isinstance(plan, dict):
        for entry in plan.values():
            if isinstance(entry, dict) and entry.get("param_class") == "config":
                targets.append(entry.get("injection_target"))
    return targets


def _validate_reapply_targets(item):
    invalid = []
    for target in _reapply_injection_targets(item):
        if not isinstance(target, str) or not _INJECTION_TARGET_RE.fullmatch(target):
            invalid.append(target)
    return invalid


def _validate_frozen_injection_contract(item, registry_entries):
    """Reject a snapshot that cannot describe this tenant's frozen values."""
    plan = item.get("frozen_injection_plan") or {}
    if not isinstance(plan, dict) or not isinstance(registry_entries, dict):
        raise LookupError("frozen injection contract is not an object")
    mismatches = []
    for field, frozen in plan.items():
        selected = registry_entries.get(field)
        if not isinstance(frozen, dict) or not isinstance(selected, dict):
            mismatches.append(f"{field}: missing")
            continue
        for key in ("param_class", "injection_target"):
            if (selected.get(key) or "") != (frozen.get(key) or ""):
                mismatches.append(f"{field}.{key}")
        if bool(selected.get("sensitive")) != bool(frozen.get("sensitive")):
            mismatches.append(f"{field}.sensitive")
        if (selected.get("empty_fallback") or "") != (
            frozen.get("empty_fallback") or ""
        ):
            mismatches.append(f"{field}.empty_fallback")
    if mismatches:
        raise LookupError(
            "selected registry snapshot changes the tenant's frozen injection "
            "contract: " + ", ".join(sorted(set(mismatches)))
        )


def _resolved_reapply_snapshot(resolved):
    return str(
        resolved.get("target_host_snapshot")
        or resolved.get("target_snap")
        or ""
    )


def _resolve_target_openclaw_version(item, resolved):
    """Read the selected host image's manifest without mutating host or tenant."""
    target_snapshot = (
        resolved.get("target_host_snapshot")
        or resolved.get("target_snap")
        or ""
    )
    if target_snapshot:
        manifest = (
            f"/data/firecracker-assets/versions/{target_snapshot}/manifest.json"
        )
    else:
        manifest = "/data/firecracker-assets/manifest.json"
    captured = {"stdout": "", "stderr": ""}
    command = (
        f"jq -er '.openclaw_version' {shlex.quote(manifest)} "
        "2>/dev/null"
    )
    ok = ssm_dispatch._ssm_run(
        item["host_id"],
        command,
        timeout=30,
        on_output=lambda stdout, stderr: captured.update(
            {"stdout": stdout, "stderr": stderr}
        ),
    )
    version = (captured["stdout"] or "").strip().splitlines()
    if not ok or not version:
        raise LookupError(
            "target image manifest does not expose openclaw_version"
        )
    return version[-1].strip()


def _prepare_config_reapply(item, request, resolved):
    """#429 control-plane fast pre-screen; this function performs no writes."""
    if not _reapply_requested(request):
        return None
    force = request.get("force_reapply")
    if force is not None and not isinstance(force, bool):
        raise ValueError("force_reapply must be a boolean")
    template = request.get("config_template")
    if template is None or template == "":
        template = item.get("config_template") or registry_service.DEFAULT_TEMPLATE
    if not isinstance(template, str) or not _CONFIG_TEMPLATE_RE.fullmatch(template):
        raise ValueError(
            "config_template must match "
            "^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"
        )
    requested_version = request.get("config_template_version")
    registry_version, entries, _metadata = registry_service.load_snapshot(
        template, requested_version
    )
    _validate_frozen_injection_contract(item, entries)
    target_openclaw_version = _resolve_target_openclaw_version(item, resolved)
    try:
        body, body_binding = registry_service.load_template_body(template)
    except ValueError as exc:
        raise LookupError(str(exc)) from exc
    forbidden = registry_service.forbidden_paths_for_version(
        target_openclaw_version
    )
    if forbidden is None:
        raise LookupError(
            f"target openclaw version {target_openclaw_version!r} "
            "has no denylist classification"
        )
    hits = (
        registry_service.find_forbidden_paths(body, forbidden)
        if body is not None
        else []
    )
    invalid_targets = _validate_reapply_targets(item)
    if hits or invalid_targets:
        detail = []
        if hits:
            detail.append("forbidden keys: " + ", ".join(sorted(hits)))
        if invalid_targets:
            detail.append(
                "unsafe injection_target: "
                + ", ".join(repr(value) for value in invalid_targets)
            )
        raise LookupError("; ".join(detail))
    return {
        "config_template": template,
        "registry_version": registry_version,
        "target_openclaw_version": target_openclaw_version,
        "target_image_snapshot_time": _resolved_reapply_snapshot(resolved),
        **body_binding,
    }


def _reapply_target_matches(item, binding):
    if not binding:
        return False
    return all(
        (
            item.get("config_template") or registry_service.DEFAULT_TEMPLATE
            if field == "config_template"
            else (
                int(item.get(item_field))
                if field == "registry_version" and item.get(item_field) is not None
                else (item.get(item_field) or "")
            )
        )
        == binding.get(field)
        for field, item_field in (
            ("config_template", "config_template"),
            ("registry_version", "config_reapply_registry_version"),
            ("body_version_id", "config_reapply_body_version_id"),
            ("body_sha256", "config_reapply_body_sha256"),
            (
                "target_image_snapshot_time",
                "config_reapply_image_snapshot_time",
            ),
        )
    )


def _reapply_already_applied(item, binding, resolved):
    if not _reapply_target_matches(item, binding):
        return False
    if binding.get("target_image_snapshot_time", "") != (
        _resolved_reapply_snapshot(resolved)
    ):
        return False
    current_channel = item.get("image_channel") or image_channel_mod.DEFAULT_CHANNEL
    current_snapshot = (item.get("image_snapshot_time") or "").strip() or None
    return (
        current_channel == resolved["channel"]
        and current_snapshot == resolved.get("target_snap")
    )


def _reapply_already_applied_response(tenant_id, item, binding):
    return utils._resp(
        200,
        {
            "id": tenant_id,
            "status": item.get("status") or "running",
            "rebuild_status": _REBUILD_STATUS_DONE,
            "config_reapply": "already_applied",
            "registry_version": binding["registry_version"],
            "body_version_id": binding.get("body_version_id", ""),
            "body_sha256": binding.get("body_sha256", ""),
        },
    )


def _should_stamp_reapply(rebuild_verified, binding):
    return bool(rebuild_verified and isinstance(binding, dict) and binding)


def _reapply_stamp_values(binding):
    return {
        ":cfg_tpl": binding["config_template"],
        ":cfg_reg": int(binding["registry_version"]),
        ":cfg_vid": binding.get("body_version_id", ""),
        ":cfg_sha": binding.get("body_sha256", ""),
        ":cfg_img": binding.get("target_image_snapshot_time", ""),
    }


def _reapply_env_prefix(binding):
    raw = json.dumps(binding, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"OC_REAPPLY_CONFIG=1 OC_REAPPLY_BINDING_B64={shlex.quote(encoded)} "


def _run_reapply_host_probe(item, binding, resolved):
    """Run schema-only assembly/validation while the current VM is still running."""
    target_snapshot = (
        resolved.get("target_host_snapshot")
        or resolved.get("target_snap")
        or "__legacy_flat__"
    )
    heal = ssm_dispatch.host_script_self_heal(
        ("launch-vm.sh",),
        "oc:reapply-probe",
    )
    lib_heal = (
        "([ -r /home/ubuntu/lib/harden-config.sh ] && "
        "grep -q '^oc_assemble_config()' /home/ubuntu/lib/harden-config.sh) || "
        "(. /etc/platform.env && mkdir -p /home/ubuntu/lib && "
        "aws s3 cp "
        "\"s3://${ASSETS_BUCKET}/deployment/scripts/lib/harden-config.sh\" "
        "/home/ubuntu/lib/harden-config.sh "
        "--region \"${OC_REGION:-ap-northeast-1}\" --quiet && "
        "grep -q '^oc_assemble_config()' /home/ubuntu/lib/harden-config.sh)"
    )
    command = (
        f"{heal} && {lib_heal} && {_reapply_env_prefix(binding)}"
        f"/home/ubuntu/launch-vm.sh --pre-rebuild-probe "
        f"{shlex.quote(str(item['id']))} "
        f"{shlex.quote(str(item.get('vm_num', 1)))} "
        f"{shlex.quote(str(target_snapshot))}"
    )
    captured = {"stdout": "", "stderr": "", "rc": None}
    ok = ssm_dispatch._ssm_run(
        item["host_id"],
        command,
        timeout=180,
        on_result=lambda _status, rc: captured.__setitem__("rc", rc),
        on_output=lambda stdout, stderr: captured.update(
            {"stdout": stdout, "stderr": stderr}
        ),
    )
    incompatible = any(
        '"state":"INCOMPATIBLE"' in text or "openclaw.json 不兼容" in text
        for text in (captured["stdout"], captured["stderr"])
    )
    return bool(ok), incompatible, captured


# 令牌释放三态(codex review #3:delete 若把瞬时失败当已释放照样标 deleted,令牌永久搁浅
# ——deleted 租户不被 reaper 兜底 → 容量永漏。三态让 delete 在 retry 时留 deleting 返 5xx 重投)。
_REL_CONSUMED = "consumed"  # 本次扣了账本、清了令牌
_REL_ALREADY = "already"    # 令牌已不在(别人消费/从没有)或下溢守卫触发 → 安全幂等
_REL_RETRY = "retry"        # 瞬时失败(冲突/throttle/网络)→ 令牌可能仍在,必须重投再释放


def _classify_release_cancel(e, tenant_id, tag):
    """令牌化释放事务的失败 → 三态。**TransactItems 顺序契约:[0]=host 账本项、
    [1]=tenant 令牌项**;调用方必须按这个次序组装事务,否则本判定会读错位次。

    suspend/restore 的令牌化释放必须把【状态提交】并进 tenant 那一项(DDB 事务不允许对
    同一 key 出现两个操作),所以它们复用不了整个事务函数,只能复用这段判定。而这段
    判定的优先级阶梯是四轮 codex 评审收敛的结果(尤其"token-gone 优先于 host 下溢"),
    抄一份必然漂移,且漂移方向恰好是【把瞬时失败误判成已释放】→ 令牌搁浅 → 容量永漏。
    """
    retryable = {"TransactionConflict", "ThrottlingError",
                 "ProvisionedThroughputExceeded", "RequestLimitExceeded"}
    if not isinstance(e, ClientError):
        print(f"{tag}: token release {tenant_id} error (retry): {e}")
        return _REL_RETRY  # 未知错误保守当可重试:宁重投也不搁浅令牌
    # 按【位次】判(codex review2 #2):TransactItems[0]=host 扣减,[1]=tenant 令牌消费。
    # 仅 tenant 项(idx1)条件失败才算 already(令牌已被别人消费);host 项(idx0)下溢
    # 或缺 reasons/可重试因 → retry(不当已释放,否则搁浅令牌 / delete 误 finalize)。
    code = e.response["Error"]["Code"]
    if code == "TransactionCanceledException":
        reasons = e.response.get("CancellationReasons", []) or []

        def _code_at(idx):
            return reasons[idx].get("Code", "") if idx < len(reasons) else ""

        host_code, tenant_code = _code_at(0), _code_at(1)
        # 优先级(codex review4 #2):瞬时 > 令牌已消费 > host 下溢 > 缺细节。token 项(idx1)
        # CCF 优先判 ALREADY——最后一张预留双重释放时 host 下溢与 token-gone 会同时失败,
        # token-gone 说明别人已成功扣账本,本次安全幂等,绝不能因 host 下溢误报 retry 让 delete
        # 卡 deleting/进 DLQ。
        if host_code in retryable or tenant_code in retryable:
            print(f"{tag}: release {tenant_id} retryable cancel "
                  f"{[host_code, tenant_code]}")
            return _REL_RETRY
        if tenant_code == "ConditionalCheckFailed":
            return _REL_ALREADY  # 令牌已被别人消费/从没有 → 安全幂等
        if host_code == "ConditionalCheckFailed":
            print(f"{tag}: release {tenant_id} host underflow — retry+alarm")
            return _REL_RETRY
        print(f"{tag}: release {tenant_id} cancel w/o reasons — retry")
        return _REL_RETRY
    if code in retryable:
        print(f"{tag}: release {tenant_id} retryable error {code}")
        return _REL_RETRY
    print(f"{tag}: token release {tenant_id} error (retry): {e}")
    return _REL_RETRY  # 未知错误保守当可重试:宁重投也不搁浅令牌


def _release_capacity_reservation(tenant_id, host_id, reservation_id, vcpu, mem_mb):
    """#412 —— 令牌化释放 dispatch 预留:扣 host 账本 + 清租户令牌/放置,一个
    TransactWriteItems,条件 tenants.capacity_reservation_id=:rid。与 dispatch_service.
    _release_reservation / reaper 同款互斥锚:谁先消费令牌谁扣一次账本,其余幂等 no-op
    (codex #3 防 ABA 双扣)。

    返回三态(codex review #3):_REL_CONSUMED / _REL_ALREADY(安全,可继续标 deleted)/
    _REL_RETRY(令牌可能仍在,delete 必须留 deleting 返 5xx 重投,绝不 finalize)。

    独立实现(不 import dispatch_service):delete 是 no-data-loss 关键路径,自包含避免跨
    service 依赖;事务写法与本文件 canary put(:734)同源(原生值,不预 TypeSerializer)。"""
    txn_items = [
        {
            "Update": {
                "TableName": clients.hosts_table.table_name,
                "Key": {"instance_id": host_id},
                "UpdateExpression": (
                    "SET used_vcpu = used_vcpu - :v, "
                    "used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one"
                ),
                "ConditionExpression": (
                    "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                ),
                "ExpressionAttributeValues": {":v": vcpu, ":m": mem_mb, ":one": 1},
            }
        },
        {
            "Update": {
                "TableName": clients.tenants_table.table_name,
                "Key": {"id": tenant_id},
                "UpdateExpression": (
                    "REMOVE capacity_reservation_id, dispatch_settle, host_id, "
                    "vm_num, guest_ip, host_port"
                ),
                "ConditionExpression": "capacity_reservation_id = :rid",
                "ExpressionAttributeValues": {":rid": reservation_id},
            }
        },
    ]
    try:
        clients.hosts_table.meta.client.transact_write_items(TransactItems=txn_items)
        return _REL_CONSUMED
    except Exception as e:  # noqa: BLE001 — 判定(含"未知错误保守当 retry")在分类器里
        return _classify_release_cancel(e, tenant_id, "delete_tenant #412")


def _maybe_mark_idle(host_id):
    """令牌释放后若 host 空了则打 idle_since(与旧扣减路径的 idle 逻辑等价;令牌事务不
    返回 host 属性,单独强一致读一次 vm_count)。best-effort。"""
    try:
        h = clients.hosts_table.get_item(
            Key={"instance_id": host_id}, ConsistentRead=True
        ).get("Item")
        if h and int(h.get("vm_count", 0) or 0) == 0:
            clients.hosts_table.update_item(
                Key={"instance_id": host_id},
                UpdateExpression="SET idle_since = :t",
                ExpressionAttributeValues={":t": utils._now()},
            )
    except Exception as e:  # noqa: BLE001
        print(f"delete_tenant #412: mark idle {host_id} non-fatal: {e}")


def _validate_purchase(body):
    """校验 create body 里的购买字段,返回 (purchase_fields_dict, err_str)。

    三个字段全 optional。任一存在即进入「下单」语义:purchase_status 记为 pending
    (除非显式传 pending,不接受 create 直接塞 provisioned——开通只能走 provision
    动作的服务端状态机,防止调用方一步到位跳过开通闸)。都不传则返回空 dict,
    调用方一个购买字段都不写,行为与 #106 前完全一致。
    """
    fields = {}
    order_id = body.get("order_id")
    if order_id is not None:
        if not isinstance(order_id, str) or not _ORDER_ID_RE.match(order_id):
            return (
                None,
                "order_id must be 1-128 printable ASCII chars (no spaces/control chars)",
            )
        fields["order_id"] = order_id
    plan_tier = body.get("plan_tier")
    if plan_tier is not None:
        if not isinstance(plan_tier, str) or plan_tier not in _PLAN_TIERS:
            return None, f"plan_tier must be one of {list(_PLAN_TIERS)}"
        fields["plan_tier"] = plan_tier
    ps = body.get("purchase_status")
    if ps is not None:
        # create 只接受省略或显式 pending;provisioned/其它值一律拒(开通走 provision 动作)。
        if ps != _PURCHASE_PENDING:
            return None, (
                f"purchase_status on create must be omitted or '{_PURCHASE_PENDING}' "
                f"(use POST /tenants/{{id}}/provision to move pending→provisioned)"
            )
    # 任一购买字段存在 → 这是一笔下单,记 purchase_status=pending。
    if fields or ps is not None:
        fields["purchase_status"] = _PURCHASE_PENDING
    return fields, None


def _redact_tenant(item):
    """Return a shallow copy of a tenant record with secret fields removed.
    Defensive: callers pass DDB items straight to _resp, so this is the single
    choke point that keeps credentials server-side."""
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k not in _TENANT_SECRET_FIELDS}


def _dnat_rule_values(host_port, guest_ip):
    """Return shell-quoted values shared by the exact DNAT commands."""
    port = shlex.quote(str(int(host_port)))
    destination = shlex.quote(f"{guest_ip}:{clients.VM_PORT_BASE}")
    return port, destination


def _dnat_add_idempotent_cmd(host_port, guest_ip):
    port, destination = _dnat_rule_values(host_port, guest_ip)
    check = (
        f"sudo iptables --wait 3 -t nat -C PREROUTING -p tcp --dport {port} "
        f"-j DNAT --to-destination {destination}"
    )
    add = (
        f"sudo iptables --wait 3 -t nat -A PREROUTING -p tcp --dport {port} "
        f"-j DNAT --to-destination {destination}"
    )
    return f"({check} 2>/dev/null || {add})"


def _dnat_remove_all_cmd(host_port, guest_ip):
    port, destination = _dnat_rule_values(host_port, guest_ip)
    check = (
        f"sudo iptables --wait 3 -t nat -C PREROUTING -p tcp --dport {port} "
        f"-j DNAT --to-destination {destination}"
    )
    delete = (
        f"sudo iptables --wait 3 -t nat -D PREROUTING -p tcp --dport {port} "
        f"-j DNAT --to-destination {destination}"
    )
    return f"while {check} 2>/dev/null; do {delete} || exit $?; done"


def _route_cleanup_requires_host(item):
    """Return whether persisted route state cannot be cleaned without a host."""
    if item.get("host_id"):
        return False
    return any(
        item.get(field) not in (None, "", 0)
        for field in ("host_port", "guest_ip", "host_private_ip")
    )


# ── #187 P1 — pre-mint gateway token + reveal (11-ENGINE-TRANSFORM D 段)──────
# 建租户时预铸 32 字节随机 token,KMS 信封绑 tenant_id 加密,密文落 openclaw-tenant-secrets
# 表(#353 起无 TTL,长存)。SSM 命令把密文按位置 12 传给 launch-vm.sh;host
# 用同一 tenant_id ctx 解密后写入 openclaw.json 的 `.gateway.auth.token`。
#
# **API 侧不解密**(design decision · INTERFACE-CONTRACT §5):控制平面调用方
# 建租户后本就在 loop 里轮询状态,一旦查到 running 就绪、就在 GET /tenants/{id} 的
# 响应里带回 gateway_token **密文原样**(base64 信封密文);GET /tenants/{id}/token
# 保留作等价别名,同样只返密文。**调用方自己拿 KMS Decrypt**(它有 kms:Decrypt +
# 知道 EncryptionContext={"tenant_id":<id>})。好处:token 全程不以明文过线、不进
# CloudTrail、不进 Lambda 日志。API Lambda 不需要 kms:Decrypt 权限。
#
# 不进 tenants 表(独立表隔离)——避免 _redact_tenant 需要新增字段。
import core.kms_envelope as kms_envelope  # noqa: E402  (关键路径依赖显式在这里 import,不与顶层 import 混)

# #353 — 密文无 TTL,随租户生命周期长存(方向 A,design decision)。设计是 GET /tenants/{id}
# 一站返全(status/token/device/vkey),调用方(如 JDWS 平台网关)按需反复取。密文长存
# 让 rebuild/recover/restore 在租户创建 1-2 年后仍能回读原始 token/device 身份 —— 过期
# 读空会走 openssl 回退产生不一致 token → JDWS 连不上(#290/#312 recover 路径读此表)。
# 历史:旧 900s(15min)→ 30 天 → 现无 TTL;表 TTL 属性 + expires_at/device_expires_at
# 软过期检查全部移除。密文本身 KMS 加密(EncryptionContext 锁 tenant_id)+ 表在私网。
_GATEWAY_TOKEN_BYTES = 32  # 32 字节 → 43 char base64url(比 hex 短、URL 安全)

# #10 WSS 直连丝滑授权 —— 设备身份三件套(Ed25519)。控制面创建租户时铸一对
# ed25519 keypair:公钥 + deviceId(=SHA256(公钥 raw 32B) hex,与 OpenClaw
# device-identity.ts:143 deriveDeviceIdFromPublicKey 一致)冷注入镜像的
# devices/paired.json(gateway 侧"已批准名单",免界面 approve);私钥 PEM 走
# KMS 信封加密(EncryptionContext=owner_id,同 gateway_token 机制)存 tenant_secrets,
# 由控制面 GET /tenants/{id} 折进就绪响应返回,调用方本地解密后签 WSS 握手帧。
# scope 预授权: 无人值守部署需 admin 全权(approve/pairing/管理均需),
# 与 openclaw CLI 连接时写死的 scopes 对齐(gateway/call.ts:272)。
_DEVICE_SCOPES_DEFAULT = ["operator.admin", "operator.approvals", "operator.pairing"]


def mint_gateway_token(tenant_id):
    """Mint a per-tenant gateway token, envelope-encrypt with tenant_id EC, and
    persist the ciphertext to the tenant_secrets table with a 15-min TTL. Returns
    the base64 ciphertext (str) so the caller can pass it to _launch_vm (which
    hands it to launch-vm.sh position 12; the host decrypts with the same EC).

    Fail-loud on every branch — no half-minted state:
      • CLAWPOOL_CMK_ARN unset → the feature is off (upstream forgot to enable
        security.clawpool_cmk_enabled). RuntimeError, don't silently no-op.
      • TENANT_SECRETS_TABLE unset → stack out-of-date (feature deployed
        without the table). RuntimeError.
      • KMS GenerateRandom / encrypt / DDB put_item raise → propagate; the
        caller (create_tenant) rolls back the tenant put like the vkey/launch
        failure paths do.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not clients.CLAWPOOL_CMK_ARN:
        raise RuntimeError(
            "gateway token mint requires CLAWPOOL_CMK_ARN "
            "(security.clawpool_cmk_enabled=true in config)"
        )
    if clients.tenant_secrets_table is None:
        raise RuntimeError(
            "gateway token mint requires TENANT_SECRETS_TABLE "
            "(openclaw-tenant-secrets DDB table not deployed)"
        )
    # KMS GenerateRandom is a FIPS-validated CSPRNG on the KMS HSM — stronger than
    # secrets.token_bytes for a token that grants control-plane authority. Its
    # cost is one KMS call per create, negligible next to the encrypt/put below.
    rnd = clients.kms.generate_random(NumberOfBytes=_GATEWAY_TOKEN_BYTES)["Plaintext"]
    # base64url without padding → URL-safe + shorter than hex; the host writes it
    # as-is into openclaw.json .gateway.auth.token (opaque bearer token to gateway).
    import base64 as _b64

    token_plaintext = _b64.urlsafe_b64encode(rnd).rstrip(b"=").decode()
    ciphertext = kms_envelope.encrypt_with_tenant(
        token_plaintext, tenant_id, clients.CLAWPOOL_CMK_ARN
    )
    # #10 fix(对称):用 update_item SET merge,不用 put_item。gateway token 与 device
    # 身份共用同一 tenant_secrets 行(主键 tenant_id),无论谁先写,put_item 整条替换都会
    # 抹掉对方的字段。两侧都改 update_item 才真共存(reviewer 抓 device 侧覆盖 gateway,
    # 反序测试又暴露 gateway 侧同样会覆盖 device)。
    # #353 — 不再写 expires_at(表 TTL 属性已移除,密文随租户生命周期长存,方向 A)。
    clients.tenant_secrets_table.update_item(
        Key={"tenant_id": tenant_id},
        UpdateExpression="SET gateway_token_ct = :ct, created_at = :ca",
        ExpressionAttributeValues={
            ":ct": ciphertext,
            ":ca": utils._now(),
        },
    )
    return ciphertext


def _derive_device_id(public_raw: bytes) -> str:
    """deviceId = SHA256(公钥 raw 32B) hex —— 与 OpenClaw device-identity.ts:148
    deriveDeviceIdFromPublicKey 一致(gateway 握手时反推校验 id==derive(publicKey))。"""
    import hashlib

    return hashlib.sha256(public_raw).hexdigest()


def mint_device_identity(tenant_id, owner_id, scopes=None):
    """#10 — 铸一对 ed25519 设备身份,私钥 KMS 信封加密(EncryptionContext=owner_id,
    同 injected_credentials/vkey 机制)存 tenant_secrets,返回冷注入 + 返回给调用方所需的
    四元组。fail-loud 与 mint_gateway_token 一致(半铸态即回滚)。

    返回 dict:
      device_id      — SHA256(公钥) hex,注入 paired.json + 明文返回调用方
      public_key     — 公钥 raw 32B 的 base64url,注入 paired.json + 明文返回
      private_key_ct — 私钥 PKCS8 PEM 的 KMS 密文(base64),存 DDB + 返回调用方(本地解密签名)
      scopes         — 预授权 scope(default-deny 读写两档)

    owner_id 必须存在(EncryptionContext 绑定);api-key 建租户但 owner_id 未定(停在
    sentinel)时不铸(返回 None)——设备身份属某个 end user,无 owner 无从绑。
    """
    if not owner_id or owner_id == clients.API_KEY_OWNER:
        return None
    if not clients.CLAWPOOL_CMK_ARN:
        raise RuntimeError(
            "device identity mint requires CLAWPOOL_CMK_ARN "
            "(security.clawpool_cmk_enabled=true in config)"
        )
    if clients.tenant_secrets_table is None:
        raise RuntimeError(
            "device identity mint requires TENANT_SECRETS_TABLE "
            "(openclaw-tenant-secrets DDB table not deployed)"
        )
    import base64 as _b64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    device_id = _derive_device_id(pub_raw)
    public_key_b64u = _b64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode()
    scopes = list(scopes) if scopes else list(_DEVICE_SCOPES_DEFAULT)
    private_key_ct = kms_envelope.encrypt(priv_pem, owner_id, clients.CLAWPOOL_CMK_ARN)
    # #10 fix(reviewer CONFIRMED): 用 update_item SET merge,不用 put_item。
    # gateway token 和 device 身份共用同一 tenant_secrets 行(主键 tenant_id),
    # put_item 是整条替换 → 会把先写的 gateway_token_ct 抹掉。update_item 只加/改
    # device_* 字段,gateway token 字段原样保留(反之亦然),两侧共存。
    # #353 — 不再写 device_expires_at(表 TTL 属性已移除,device 密文随租户生命周期长存)。
    clients.tenant_secrets_table.update_item(
        Key={"tenant_id": tenant_id},
        UpdateExpression=(
            "SET device_id = :did, device_public_key = :pk, "
            "device_private_key_ct = :ct, device_scopes = :sc, "
            "device_created_at = :dc"
        ),
        ExpressionAttributeValues={
            ":did": device_id,
            ":pk": public_key_b64u,
            ":ct": private_key_ct,
            ":sc": scopes,
            ":dc": utils._now(),
        },
    )
    return {
        "device_id": device_id,
        "public_key": public_key_b64u,
        "private_key_ct": private_key_ct,
        "scopes": scopes,
    }


def build_paired_json_b64(device):
    """#188 — 把 mint_device_identity 的返回 dict 组装成 launch-vm 冷注入用的
    paired.json base64。paired.json 是 gateway 侧"已批准设备名单",首连命中即免人工
    approve(INJECTION-SPEC-2026.2.26.md,真机验证)。

    - 7.1(协议 v4)配对门 `listEffectivePairedDeviceRoles = 活跃tokens的role ∩ 批准role`,
      空 `tokens:{}` → effective roles 为空 → operator 被判 role-upgrade → 远程连接
      被拒(NOT_PAIRED,us-west-2 真机远程拓扑实测)。故必须在 tokens 里放一枚带
      role 的 entry,该 role 才进 effective 集合、免 approve 放行。
    - 2.26(协议 v3)配对门只比 publicKey + roles、不读 tokens,故多出的 tokens.operator
      字段对 2.26 无害(真机 2.26 gateway 实测仍 CONNECTED)。→ 同一份产物兼容两版。
    - token 值用随机 32B:客户端走 shared-gateway-token 认证(connect 帧 auth.token
      出示 gateway token 即 authOk,server 跳过 verifyDeviceToken 的明文比对),故 token
      仅需存在、让 listActiveTokenRoles 能从中派生出 operator role,不必是 server 铸的值
      (真机 7.1 远程连接实测:随机 token entry → CONNECTED)。
    - 覆盖语义(#415 codex review):launch-vm.sh 只在 NEW_DATA=true(首次/restore/
      rebuild 重建数据盘)时整文件写 paired.json,唤醒不重注(one-time,见 launch-vm.sh
      NEW_DATA 块内注释)。故本预铸 token 不会覆盖运行中 VM 磁盘上的 token;且当前
      shared-token 认证路径下 server 不轮换 device token(verifyDeviceToken 被跳过),
      不存在"用预铸值覆盖已轮换运行时 token"的回退。若未来切到 device-token 认证并启用
      轮换,需改为"设备记录已存在则保留磁盘 tokens、仅首次缺失才注入",此处留提示。

    device 为 None(owner 未知 / CMK 关 → mint 返 None)→ 返回 "",launch-vm 侧
    读空跳过写盘(feature-off 字节兼容)。返回单 device 的 paired.json 全量对象的
    base64(launch-vm base64 -d 后直接当 paired.json 内容)。
    """
    if not device or not device.get("device_id") or not device.get("public_key"):
        return ""
    import base64 as _b64

    device_id = device["device_id"]
    scopes = list(device.get("scopes") or _DEVICE_SCOPES_DEFAULT)
    now_ms = int(time.time() * 1000)
    # 预铸 operator role token(随机 32B,base64url 无填充,与 openclaw
    # generatePairingToken() 同格式)。7.1 免 approve 依赖此 entry。
    pairing_token = _b64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    # role 固定 operator(scopes 给 admin 全权,无人值守部署需 approve/pairing/管理,
    # 与 CLI call.ts:272 对齐)。
    paired = {
        device_id: {
            "deviceId": device_id,
            "publicKey": device["public_key"],
            "role": "operator",
            "roles": ["operator"],
            "scopes": scopes,
            "tokens": {
                "operator": {
                    "token": pairing_token,
                    "role": "operator",
                    "scopes": scopes,
                    "createdAtMs": now_ms,
                    "lastUsedAtMs": now_ms,
                }
            },
            "createdAtMs": now_ms,
            "approvedAtMs": now_ms,
        }
    }
    return _b64.b64encode(json.dumps(paired).encode()).decode()


def persist_device_paired_b64(tenant_id, device_paired_b64):
    """#314(codex review 缺陷2 修复):把 device_paired_b64 长期存 tenants 表(无 TTL),
    带一次重试 + fail-loud。dispatch 与 sync 两条创建路径共用,避免重复内联。

    device_paired_b64 是 launch-vm restart/镜像更新自愈重注入 paired.json 的唯一长期
    来源(tenant_secrets 不存该组装字段)。写失败若静默吞 → 该租户 data 盘丢失后无源重建
    → NOT_PAIRED 无告警。这里 DDB 瞬时失败重试一次(消化大部分),仍失败则 print ERROR
    醒目告警(不阻塞 create——paired 是增强;且 launch-vm 侧对读到空会按需 backfill 兜底)。
    返回 True/False 供调用方按需处理。
    """
    if not device_paired_b64:
        return False
    for _attempt in (1, 2):
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET device_paired_b64 = :dpb",
                ExpressionAttributeValues={":dpb": device_paired_b64},
            )
            return True
        except Exception as e:  # noqa: BLE001 — 重试一次;仍失败 fail-loud 不吞
            if _attempt == 2:
                print(
                    f"[#314][ERROR] persist device_paired_b64 to tenants table FAILED "
                    f"after retry for {tenant_id}: {type(e).__name__}: {e} — "
                    f"该租户 data 盘丢失后将无源重建 paired.json(NOT_PAIRED),"
                    f"依赖 launch-vm 侧 backfill 兜底或运维 reconciliation"
                )
    return False


def read_gateway_token_ct(tenant_id):
    """Helper for GET /tenants/{id} (handler.get_tenant): if a ciphertext row
    exists, return the base64 ciphertext string; else return None. Silent (no
    error responses) — the caller decides how to shape the response and whether
    to include the field.

    Returns None on: feature-off / no row. Never raises.

    life so rebuild/recover/restore months or years later can still read back
    the original token (an expired read → openssl fallback → token mismatch →
    JDWS can't connect). The `expires_at` field is no longer written/read; the
    table's DynamoDB TTL attribute is removed (storage.py).
    """
    if clients.tenant_secrets_table is None:
        return None
    try:
        row = clients.tenant_secrets_table.get_item(
            Key={"tenant_id": tenant_id}, ConsistentRead=True
        ).get("Item")
    except Exception:
        # Fold-into-poll must not break the tenant read; on a transient error
        # the field is simply omitted and the caller re-polls GET /tenants/{id}.
        return None
    if not row:
        return None
    return row.get("gateway_token_ct")


def read_device_identity(tenant_id):
    """#10 — Helper for GET /tenants/{id}: 返回 WSS 设备三件套(供调用方本地签握手),
    TTL 窗口内有 device 行才返回,否则 None。Silent(不抛)——与 read_gateway_token_ct
    同款 fold-into-poll 语义。

    返回 dict {device_id, public_key(明文), private_key(KMS 密文), scopes} 或 None。

    让 1-2 年后 rebuild/recover 仍能回读原始设备身份,不因 TTL 过期读空 →
    paired.json 无源重注入 → NOT_PAIRED / token 不一致连不上。
    """
    if clients.tenant_secrets_table is None:
        return None
    try:
        row = clients.tenant_secrets_table.get_item(
            Key={"tenant_id": tenant_id}, ConsistentRead=True
        ).get("Item")
    except Exception:
        return None
    if not row or not row.get("device_id"):
        return None
    return {
        "device_id": row["device_id"],
        "public_key": row["device_public_key"],
        "private_key": row["device_private_key_ct"],
        "scopes": row.get("device_scopes", []),
    }


def _cleanup_gateway_token_secret(tenant_id):
    """Best-effort remove the secrets-table row. #353 (方案 B) — called on BOTH
    the delete_tenant happy path (terminal cleanup: a deleted tenant can't rebuild,
    so drop its ciphertext to shrink the host-compromise blast radius to active
    tenants only) AND the create-failure / rollback paths (clear a half-written row
    for a tenant that never came up). Active tenants keep their ciphertext forever
    (no TTL) so rebuild/recover years later works — only terminal/failed rows are
    dropped. Failures are non-fatal — with no TTL sweep there's no auto-cleanup
    fallback, but a leftover row is inert (KMS ciphertext, private-subnet table)
    and reconciliation can clean it."""
    if clients.tenant_secrets_table is None:
        return
    try:
        clients.tenant_secrets_table.delete_item(Key={"tenant_id": tenant_id})
    except Exception as e:  # noqa: BLE001 — best-effort cleanup of a half-write orphan
        # #353 — 保持 best-effort(不阻塞删除,避免 DDB 瞬时抖动把租户卡在 deleting/
        # 删除路径,违反幂等收敛)。但 TTL 移除后没有自动清扫兜底,失败 = 该 tenant_id
        # 的密文行残留,需 reconciliation 清。故升级为醒目 ERROR(原为普通 print),让
        # 巡检能发现残留凭据。tenant_id 是删除态租户的 key,残留行 KMS 加密 + 私网表。
        print(
            f"[#353][ERROR] tenant_secrets delete_item FAILED for {tenant_id}: "
            f"{type(e).__name__}: {e} — no TTL sweep fallback, ciphertext row "
            f"REMAINS; reconciliation must clean it"
        )


def _attribution_override(body, event):
    """#143 — resolve the create-on-behalf attribution override from the body.

    The api-key path is an external platform's backend (trusted automation, no
    per-user Bearer), so it may state WHICH end user the tenant belongs to:
      • body.owner_id — the end user's Cognito sub (UUID; every owner check and
        the chat UI compare owner_id == sub, so any other shape could never
        match a real caller). Without it the record parks at the API_KEY_OWNER
        sentinel and the end user can never see their node (evidence
        Q2-OWNER-E2E-2026-07-06).
      • body.tenant_user_id — the platform's OWN stable user id (NOT a Cognito
        sub — attributes end users that never touch our Cognito, #13/#14).

    Security contract:
      • Bearer callers NEVER get the override: owner derives from the verified
        token, otherwise user A could create/claim a node in B's name. Reject
        loud (403), don't silently strip — a misconfigured client must notice.
        create_tenant_self additionally strips these fields (defense in depth).
      • Under EXTERNAL_AUTHZ authority is the signed /external/authz endpoint,
        not the creator — owner_id override is refused loud (403) instead of
        being silently parked at the sentinel.
      • Values are validated at the edge (anti injection / log poisoning).
      • The SQS replay path re-runs this with _consumer_ident (api_key_only
        carried) and the body snapshot carries the override → same result.

    Returns (owner_id | None, tenant_user_id | None, error_resp | None).
    """
    body_owner_id = body.get("owner_id")
    body_tenant_user_id = body.get("tenant_user_id")
    if body_owner_id is None and body_tenant_user_id is None:
        return None, None, None
    if not auth._get_caller_identity(event or {}).get("api_key_only"):
        return (
            None,
            None,
            utils._err(
                403,
                "FORBIDDEN",
                "owner_id/tenant_user_id in body is allowed only for the "
                "api-key (create-on-behalf) path",
            ),
        )
    if body_owner_id is not None:
        if clients.EXTERNAL_AUTHZ:
            return (
                None,
                None,
                utils._err(
                    403,
                    "FORBIDDEN",
                    "tenant authority is external: owner_id cannot be set at "
                    "create — grant access via POST /external/authz",
                ),
            )
        if not isinstance(body_owner_id, str) or not utils._COGNITO_SUB_RE.match(
            body_owner_id
        ):
            return (
                None,
                None,
                utils._err(400, "VALIDATION", "owner_id must be a Cognito sub (UUID)"),
            )
    if body_tenant_user_id is not None:
        if not isinstance(
            body_tenant_user_id, str
        ) or not utils._TENANT_USER_ID_RE.match(body_tenant_user_id):
            return (
                None,
                None,
                utils._err(
                    400,
                    "VALIDATION",
                    "tenant_user_id must be 1-128 printable ASCII chars "
                    "(no spaces/control chars)",
                ),
            )
    return body_owner_id, body_tenant_user_id, None


def _resolve_injection_plan(
    validated_items, scheme, registry_entries, registry_version
):
    """把校验过的 injected items 冻结成 per-field 注入计划(Task 2.4)。

    validated_items 已过 _validate_injected_parameters_v2(每个 field 必在 registry 里),
    这里只做决策快照:复制 registry 的注入坐标 + 按 enc:v1: 前缀定 mode,值原样存
    value_ref(本层绝不解密——guest 零凭据基线)。plan 随租户落 DDB 后,registry 再
    发布新快照也不影响已建租户(冻结语义);scheme 由调用方传入已归一化值,当前
    plan 消费方(launch 冷注入)从 enc:v1: 信封自描述头取解密参数,故不落 entry。
    返回 (frozen_injection_plan_dict, registry_version)。
    """
    del scheme  # 契约签名保留;见 docstring
    plan = {}
    for field, value in validated_items.items():
        entry = registry_entries[field]
        plan_entry = {
            "param_class": entry.get("param_class"),
            "injection_target": entry.get("injection_target"),
            "sensitive": bool(entry.get("sensitive")),
            "mode": "encrypted" if looks_encrypted(value) else "plaintext",
            "value_ref": value,
        }
        if entry.get("empty_fallback"):
            plan_entry["empty_fallback"] = entry["empty_fallback"]
        plan[field] = plan_entry
    return plan, registry_version


def _probe_stuck_tenant_facts(host_id, tenant_id):
    """#469 P2(codex 独立复审第六轮)—— 强制删除【当场复探】host 侧两个事实。

    返回 (ok, {"vm_dir": bool, "fc_alive": bool});ok=False 表示探不到,调用方必须拒绝
    而不是猜(与 health_check 的 _probe_host_tenant_state / _confirm_vm_stopped 同一条
    原则:时序不替代正确性)。

    为什么不能直接用租户行里 reaper 留下的 lifecycle_stuck_vm_dir / _fc_alive:那两个
    字段是 reaper **第一次**判定时刻的快照,而且 reaper 对已标记的租户【不再复探】
    (health_check :1127 `if t.get("lifecycle_stuck_at"): marked += 1; continue`,那是为了
    不让已标记者烧光每轮 10 个的探测预算)。于是标记可能是几小时前的,而现场早就变了。
    两个方向都会出事,且都踩账本红线:

      · vm_dir 记的是 True、现场其实已经删了:一次只是"慢"的 suspend 在被标记之后继续
        跑完了 rm -rf(:3571)和 _release_slot(:3584),却崩在最后那次终态 CAS 之前 ——
        账本【已扣】而 status 仍 suspending、标记仍说盘在。强制删除据此走普通 delete →
        【再扣一次】。_release_slot 只有下溢守卫、没有"这个租户扣过没"的互斥锚
        (core/scheduling.py),host 上还有别的租户占容量时那一扣会成功并吃掉别人的额度。
      · vm_dir 记的是 False、fc_alive 记的是 False,而现场 FC 其实还活着(或反之):
        见下方快路径里的说明 —— 会把物理槽位发给一个活着的孤儿 VM。

    所以分工:reaper 的标记是**准入凭据**(证明"它确实卡住了,不是正在正常执行"),
    而**走哪条删除路径由当场复探决定**。这两件事此前被合成了一个判据。

    命令与解析【与 reaper 同源】(health_check:_probe_host_tenant_state):fc_alive 的判据
    是匹配 `--api-sock <VM_DIR>/fc.sock` 而不是拿 tenant_id 去 pgrep 整条命令行 ——
    后者会被别的租户的路径子串命中(t-1 命中 t-10)= 跨租户误判。
    tenant_id 经 shlex.quote 进 root shell(纵深防御)。
    """
    if not host_id:
        return False, {}
    _q_tid = shlex.quote(str(tenant_id))
    cmd = (
        f'_d=/data/firecracker-vms/{_q_tid}; '
        f'if [ -d "$_d" ]; then echo VMDIR=yes; else echo VMDIR=no; fi; '
        f'_n=0; for _p in /proc/[0-9]*; do '
        # comm 而不是 exe 的 basename(codex 第十轮):二进制被替换/删除后
        # `readlink /proc/<pid>/exe` 返回 `... (deleted)`,basename 判据漏判 —— 而滚动
        # 升级换镜像正是这个场景。漏判 fc_alive 会让强制删除以为"VM 已停"而放行。
        # comm 恒为进程名、不带后缀,截断到 15 字符("firecracker" 11 字符,安全)。
        # 与 stop-vm.sh 的 _oc_is_firecracker 同一判据。
        f'  [ "$(cat "$_p/comm" 2>/dev/null)" = firecracker ] || continue; '
        f'  tr "\\0" " " < "$_p/cmdline" 2>/dev/null '
        f'    | grep -q -- "--api-sock $_d/fc.sock" && _n=$((_n+1)); '
        f'done; echo FC=$_n'
    )
    _captured = {}

    def _grab(stdout, _stderr):
        _captured["out"] = stdout or ""

    ok = ssm_dispatch._ssm_run(host_id, cmd, timeout=30, on_output=_grab)
    if not ok:
        return False, {}
    text = _captured.get("out", "")
    if "VMDIR=" not in text or "FC=" not in text:
        # 命令跑了但输出不完整(被截断/污染)→ 视作探不到,不猜(同 reaper)。
        return False, {}
    fc_alive = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("FC="):
            try:
                fc_alive = int(line[3:]) > 0
            except ValueError:
                return False, {}
            break
    else:
        return False, {}
    return True, {"vm_dir": "VMDIR=yes" in text, "fc_alive": fc_alive}


def _phys_tap_occupied(host_id, phys_num, exclude_id=None):
    """#491 —— 实现已搬到 core.scheduling.phys_tap_occupied,供队列 dispatch 路径共用。

    本名保留:同步 create(:1868)、另一放置路径(:3203)、migrate 目标(:4428)三处调用点
    与既有测试(tests/test_208_tap_collision_adversarial.py)都按这个名字引用,
    连带改名会把「机械搬迁」和「行为变更」混进同一个 diff。
    """
    return scheduling.phys_tap_occupied(host_id, phys_num, exclude_id=exclude_id)


def _persist_tenant_record(item, tenant_id):
    """把租户记录落库。成功返回 None;可预期冲突返回错误响应;意外异常向上抛(调用方回滚 slot)。

    租户,冲突回 409 CONFLICT。

    快照【线性化】:否则 delete 扫描完租户表(即使强一致,也只是时间点)之后、deleting→deleted
    之前这条 put 才落库 → 版本被删但租户已固定它 → restart 拉不回(no-data-loss)。故 canary 走
    TransactWriteItems:租户 Put + 快照 status==active 的 ConditionCheck 同一事务。delete 先把 status
    条件写成 deleting 就让这条事务失败;反之这条先成事务就让 delete 的引用扫描看见该租户。
    传【原生值】(不预 TypeSerializer;resource client 已挂序列化 transform,预序列化会二次包裹 →
    Type mismatch,与 image_jobs.create 同源教训)。live 租户不固定版本 → 走普通条件 put。
    """
    ccf = clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
    pin = item.get("image_snapshot_time")
    try:
        if pin and clients.version_snapshots_table is not None:
            clients.tenants_table.meta.client.transact_write_items(TransactItems=[
                {"ConditionCheck": {
                    "TableName": clients.version_snapshots_table.name,
                    "Key": {"snapshot_time": pin},
                    "ConditionExpression": (
                        "attribute_exists(snapshot_time) AND "
                        "(attribute_not_exists(#s) OR #s = :active)"
                    ),
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":active": "active"},
                }},
                {"Put": {
                    "TableName": clients.tenants_table.name,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(id)",
                }},
            ])
        else:
            clients.tenants_table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(id)"
            )
        return None
    except (ccf, ClientError) as e:
        # 事务取消原因在 CancellationReasons(顺序同 TransactItems):第 0 项 = 快照 ConditionCheck,
        # 第 1 项 = 租户 Put。快照那项 ConditionalCheckFailed → 版本正被删/已删,不能再固定它。
        reasons = getattr(e, "response", {}).get("CancellationReasons") or []
        snap_gone = bool(pin) and bool(reasons) and (
            (reasons[0] or {}).get("Code") == "ConditionalCheckFailed"
        )
        if snap_gone:
            return utils._err(
                409, "IMAGE_VERSION_SNAPSHOT_UNAVAILABLE",
                f"image snapshot {pin} is being deleted or no longer active; "
                f"cannot pin it to a new canary tenant",
                extra={"snapshot_time": pin},
            )
        if isinstance(e, ccf) or reasons:
            # #562 G9 —— 409 必须带【当前 status】,否则客户端拿到它做不了任何决策。
            # 墓碑方案(形态第 6 条:failed 是墓碑,同 client_token 重试继续 409)下有两种
            # 完全不同的 409,而旧响应体把它们混成一个:
            #   · status ∈ {creating, running, ...} → 「在跑,别重试」
            #   · status == failed                 → 「墓碑,换一个 client_token 再来」
            # 客户端分不清就只有两种坏选择:傻等一个永不复活的墓碑,或对着在跑的租户狂重试。
            # 顺带带上 create_fail_reason(G4 的机器可读归因),让重试决策不用再查一次 API。
            return utils._err(
                409, "CONFLICT", f"tenant '{tenant_id}' already exists",
                extra=_conflict_extra(tenant_id),
            )
        raise


def _conflict_extra(tenant_id):
    """#562 G9 —— 创建撞 409 时,把【当前 status】与失败归因一起给客户端。

    为什么这是必须的:形态第 6 条选了墓碑(`failed` 永久保留、同 client_token 重试继续 409),
    于是 409 有两种语义完全相反的情形,而旧响应体只给 `{"error", "id"}`,把它们混成一个:
      · status ∈ {creating, running, stopped, ...} → 「这个租户在,别重试」
      · status == failed                          → 「这是墓碑,换 client_token 重试」
    分不清就只剩两种坏选择:对着永不复活的墓碑傻等,或对着在跑的租户狂重试。

    **fail-open 是刻意的**:查不到 / DDB 抖动 → 只回 {"id"},不抛。这个函数在 409 的
    返回路径上,让它把一个正确的 409 变成 500 就是纯粹的倒退 —— 客户端拿 500 会重试,
    而 409 本来是在告诉它「别重试」。所以宁可少给一个字段,不能把状态码搞错。
    """
    extra = {"id": tenant_id}
    try:
        item = (
            clients.tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True)
            .get("Item")
            or {}
        )
    except Exception as e:  # noqa: BLE001 — 见 docstring:不许把 409 变 500
        print(f"[#562] _conflict_extra get_item failed (non-fatal): {type(e).__name__}: {e}")
        return extra
    status = item.get("status")
    if status:
        extra["status"] = status
    reason = item.get(create_deadline.ATTR_FAIL_REASON)
    if reason:
        extra[create_deadline.ATTR_FAIL_REASON] = reason
    # 墓碑显式标出来:客户端不必自己维护「哪些 status 算终态」的表(那张表会漂)。
    extra["retriable_with_new_token"] = status == "failed"
    return extra


def _initial_immutable_version(host, pinned_image_snapshot_time=None):
    """Return the immutable coordinate for the disk a new tenant will attach."""
    return pinned_image_snapshot_time or host.get("immutable_version", "")


def create_tenant(body=None, event=None):
    # #309 — the _canary_host param + its "land on an upgrading host" exemption were
    # removed with the canary (owner 2026-07-17). An upgrading host now blocks ALL
    # tenant placement with NO exception (no-cross-tenant: a tenant must never land on
    # a host mid image-swap). See scheduling._get_specific_host_with_capacity.
    if body is None:
        return utils._resp(400, {"error": "missing body"})
    # 坏 JSON → 400 不 500;合法但非对象(list/数字)→ 400(否则下面 body.get / 各处
    # 取值抛 AttributeError → 顶层 catch 500 泄内部错)。边界输入严校验。
    try:
        body = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return utils._resp(400, {"error": "invalid json"})
    if not isinstance(body, dict):
        return utils._resp(400, {"error": "body must be a JSON object"})

    # issue #80 — stamp the creator's identity so future per-tenant routes can
    # enforce owner==caller. Cognito `sub` for logged-in users, API_KEY_OWNER
    # for the API-key path. None only if a Bearer token was present but failed
    # verification — RBAC would already have rejected such a write.
    owner_id = auth._get_caller_identity(event or {})["owner_id"]
    # task #13/#14 — capture the external stable id for OIDC-federated callers so
    # the tenant is attributable to the external user (identity chain). None for
    # native Cognito / API-key callers, in which case the field is not stored.
    tenant_user_id = auth._get_caller_identity(event or {}).get("tenant_user_id")
    # #143 — create-on-behalf attribution override (路 A), api-key callers only.
    # See _attribution_override for the security contract. Loud errors (403/400)
    # propagate; a returned value replaces the identity-derived default.
    _ovr_owner, _ovr_tuid, _ovr_err = _attribution_override(body, event)
    if _ovr_err is not None:
        return _ovr_err
    if _ovr_owner is not None:
        owner_id = _ovr_owner
    if _ovr_tuid is not None:
        tenant_user_id = _ovr_tuid
    # #106 — external-platform attribution. Precedence:
    #   1. body.platform_id — explicit, used by the "交易平台后端代开" path where the
    #      platform's server (API-key auth, no per-user Cognito token) creates a
    #      tenant on behalf of one of its users and states which platform it is.
    #   2. custom:platform_id claim — for the federated-user path (pretokengen
    #      injected it, resolved once in _get_caller_identity — no extra verify).
    # Body override is validated (caller-controlled input → _PLATFORM_ID_RE); the
    # claim value is already Cognito-controlled. Stored only when present, so
    # native / non-platform tenants keep byte-identical records to before #106.
    platform_id = auth._get_caller_identity(event or {}).get("platform_id")
    body_platform_id = body.get("platform_id")
    if body_platform_id is not None:
        if not isinstance(body_platform_id, str) or not utils._PLATFORM_ID_RE.match(
            body_platform_id
        ):
            return utils._err(
                400,
                "VALIDATION",
                "platform_id must be 1-128 chars [a-zA-Z0-9._-]",
            )
        platform_id = body_platform_id
    # #108 — a platform-scoped API key can ONLY create tenants inside its own
    # platform namespace. The authorizer-injected scope wins over both body and
    # claim: if the body tried to name a DIFFERENT platform, that's a
    # cross-platform create attempt → 403 (not a silent re-pin). If body/claim
    # agree or are absent, we pin platform_id to the scope so the record always
    # lands in the caller's namespace (and is later visible via ?platform_id).
    _scope = auth._get_caller_identity(event or {}).get("platform_scope")
    if _scope is not None:
        if platform_id is not None and platform_id != _scope:
            return utils._err(
                403,
                "FORBIDDEN",
                "platform-scoped key cannot create a tenant for another platform",
            )
        platform_id = _scope
    purchase_fields, purchase_err = _validate_purchase(body)
    if purchase_err:
        return utils._err(400, "VALIDATION", purchase_err)
    # Go-live A1: when authority is external, do NOT derive ownership from whoever
    # created the tenant — access is granted exclusively by the external backend
    # via the signed /external/authz endpoint (written to authorized_users). We
    # park owner_id at the API_KEY_OWNER sentinel so no Cognito sub implicitly owns
    # it; with SHARED_TENANT_ACCESS off on the hub, that means "nobody until the
    # external backend grants".
    if clients.EXTERNAL_AUTHZ:
        owner_id = clients.API_KEY_OWNER

    name = body.get("name", "")
    name_err = utils._validate_name(name)
    if name_err:
        return utils._resp(400, {"error": name_err})

    # #95 对抗测试暴露(ADV-J-003/J-004):裸 int() 对非数字抛 ValueError→500 泄内部报错
    # (违反 E2),负数/0 绕过配额击穿容量账本(违反 I1)。改为类型+正整数校验,返 400 code。
    def _pos_int(field, default):
        try:
            v = int(body.get(field, default))
        except (ValueError, TypeError):
            return None, f"{field} must be a positive integer"
        if v <= 0:
            return None, f"{field} must be a positive integer (got {v})"
        return v, None

    vcpu, _e = _pos_int("vcpu", clients.VM_DEFAULT_VCPU)
    if _e:
        return utils._err(400, "VALIDATION", _e)
    mem_mb, _e = _pos_int("mem_mb", clients.VM_DEFAULT_MEM)
    if _e:
        return utils._err(400, "VALIDATION", _e)
    data_disk_mb, _e = _pos_int("data_disk_mb", clients.VM_DATA_DISK_MB)
    if _e:
        return utils._err(400, "VALIDATION", _e)

    quota_err = scheduling._check_quota(vcpu, mem_mb, data_disk_mb)
    if quota_err:
        return utils._resp(400, {"error": quota_err})

    config_template = body.get("config_template", "")
    # issue #59 (WI-E/M-1) — reject injection at the edge. Unvalidated,
    # config_template reaches an SSM root shell on a shared host, the strongest
    # cross-tenant escape in the security review. Empty is the common "no custom
    # template" case; any non-empty value must be a bare DNS-label S3 slug.
    if config_template and not _CONFIG_TEMPLATE_RE.match(config_template):
        return utils._resp(
            400,
            {
                "error": "config_template must match ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"
            },
        )
    # #228 — hoist config_template existence check out of the v2 injection
    # branch. Before this fix, `POST /tenants {"config_template":"missing"}`
    # (no injected_parameters) was silently accepted at the API; launch-vm.sh
    # then hit S3 404 pulling templates/openclaw/missing/openclaw.json, wrote
    # an empty/default config, and gateway failed to come up — tenant ended at
    # status=running app_health=down (silent data-plane failure with a 202 on
    # the control plane). Fail-loud 400 at the edge instead. Empty template =
    # shipped "default" (auto-seeded by load_current_snapshot), loaded lazily
    # in the v2 branch below to preserve legacy-path behaviour (no registry
    # access when neither config_template nor injection params passed).
    _reg_version = None
    _reg_entries = None
    if config_template:
        try:
            _reg_version, _reg_entries = registry_service.load_current_snapshot(
                config_template
            )
        except LookupError:
            return utils._err(
                400,
                "VALIDATION",
                f"config_template '{config_template}' not found in registry; "
                "create it via console (Templates → New) first, or use default.",
            )
    restore_from = body.get("restore_from")
    clone_from = body.get("clone_from")

    # #93 — control-plane standardization. All ADDITIVE + optional: a caller that
    # omits every one of these gets byte-identical behavior to before #93
    # (api-design-review A1/A2). image_id: which golden rootfs version this tenant
    # was created against — records it for later rolling-upgrade tracking; phase-1
    # default "v2". security: per-tenant encryption/cert config (see
    # _validate_security). client_token: optional idempotency key (C1/C3) — NOT
    # required, so SDK auto-generation isn't disturbed.
    image_id = (body.get("image_id") or "v2").strip()
    if not _CONFIG_TEMPLATE_RE.match(image_id):
        return utils._err(
            400,
            "VALIDATION",
            "image_id must match ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$",
        )
    security, sec_err = utils._validate_security(body.get("security"))
    if sec_err:
        return utils._err(400, "VALIDATION", sec_err)
    # tenant-credential-contract Task 3.5 — normalize FIRST, then route:
    # body 带新字段 `injected_parameters` → registry 驱动的 v2 校验 + frozen plan;
    # 只带旧 `injected_credentials`(或都不带)→ 原有 #118 路径逐字不变(兼容:旧
    # 调用方不需要 registry 表存在)。归一化只做形态翻译,校验各归各路 fail-closed。
    frozen_injection_plan = None
    registry_version = None
    injected_credentials = None
    _frozen_scheme = (
        None  # #149 — resolved scheme (kms-cmk/asymmetric-v1) 落库供 host 分派
    )
    _injected_params = utils._normalize_injected_parameters(body)
    # #149 — v2(registry+frozen plan)路径:新 injected_parameters 或目标态别名
    # env_injected_credentials 触发;旧 injected_credentials 仍走下面的 legacy 分支。
    if (
        body.get("injected_parameters") is not None
        or body.get("env_injected_credentials") is not None
    ):
        # #228 — reuse the snapshot loaded above when config_template was
        # explicit; only load "default" lazily here (empty template case)
        # so no-injection legacy calls stay registry-free. Same fail-closed
        # 400 semantics (LookupError → VALIDATION).
        if _reg_entries is not None:
            registry_version = _reg_version
        else:
            try:
                registry_version, _reg_entries = registry_service.load_current_snapshot(
                    "default"
                )
            except LookupError as e:
                return utils._err(400, "VALIDATION", str(e))
        _validated_items, ip_err = _validate_injected_parameters_v2(
            _injected_params, clients.CLAWPOOL_CMK_ARN, _reg_entries
        )
        if ip_err:
            return utils._err(400, "VALIDATION", ip_err)
        # R12.1 — 注入值绑定 owner_id(解密 AAD/EncryptionContext);与旧路径同一
        # 拒绝语义(EXTERNAL_AUTHZ 下 owner_id 被停在哨兵值,同样不支持)。
        if not owner_id or owner_id == clients.API_KEY_OWNER:
            return utils._err(
                400,
                "VALIDATION",
                "injected_parameters requires an owner_id (the decryption-context "
                "binding); pass body.owner_id (create-on-behalf). Not supported "
                "under EXTERNAL_AUTHZ (owner_id is externalized).",
            )
        _frozen_scheme = resolve_scheme(_injected_params.get("scheme"))
        frozen_injection_plan, registry_version = _resolve_injection_plan(
            _validated_items,
            _frozen_scheme,
            _reg_entries,
            registry_version,
        )
    else:
        # #118/#116 — optional platform-injected credentials (in-transit KMS
        # ciphertext). The API only validates + relays the ciphertext; the host
        # decrypts at VM launch (guest zero-credential baseline). Absent →
        # unchanged behavior.
        injected_credentials, ic_err = utils._validate_injected_credentials(
            body.get("injected_credentials"), clients.CLAWPOOL_CMK_ARN
        )
        if ic_err:
            return utils._err(400, "VALIDATION", ic_err)
        # #118 — the injected ciphertext is bound to owner_id in its KMS
        # EncryptionContext (the upstream registration center encrypts the userkey
        # under the platform user's owner_id, before any tenant exists). So a
        # tenant that carries injected_credentials MUST have a concrete owner_id —
        # that's the value the host decrypts against. Reject loudly at create
        # instead of letting the host fail-closed at launch (better UX + clear
        # cause). owner_id here is the platform-supplied value (create-on-behalf).
        # NOTE: under EXTERNAL_AUTHZ, owner_id was parked at the API_KEY_OWNER
        # sentinel above (authority externalized) — injection needs the real
        # owner_id as its EC, so it is incompatible with EXTERNAL_AUTHZ for now
        # (a dedicated cred_owner_id field would decouple them; deferred until
        # that mode is actually used).
        if injected_credentials:
            if not owner_id or owner_id == clients.API_KEY_OWNER:
                return utils._err(
                    400,
                    "VALIDATION",
                    "injected_credentials requires an owner_id (the KMS EncryptionContext "
                    "binding); pass body.owner_id (create-on-behalf). Not supported under "
                    "EXTERNAL_AUTHZ (owner_id is externalized).",
                )
    client_token, client_token_error = _normalize_client_token(
        body.get("client_token")
    )
    if client_token_error:
        return utils._err(
            400,
            "VALIDATION",
            client_token_error,
        )

    # 1.4.0 (#62) — per-tenant skill list and optional group membership.
    # Validate up-front so we don't half-create a tenant with a malformed scope.
    skills_in = body.get("skills")
    if skills_in is not None:
        if not isinstance(skills_in, list) or not all(
            isinstance(s, str) for s in skills_in
        ):
            return utils._resp(400, {"error": "skills must be a list of strings"})
        skills_in = sorted(set(s.strip() for s in skills_in if s and s.strip()))
    # per-tenant chatCompletions switch (default off = secure default). Only
    # tenants explicitly created with chat_endpoint_enabled=true get the
    # OpenAI-compatible HTTP endpoint; launch-vm.sh injects enabled:true for them
    # and deletes the endpoint for everyone else. See CLAUDE.md security decision.
    # #95 adversarial C-017: bool("false") / bool("0") are both True, so a JSON
    # string used to silently OPEN this deviceAuth-bypassing endpoint. This is a
    # secure-default switch — accept only a real JSON boolean; anything else
    # (string, number, null) fails loud rather than defaulting to "on".
    cee_raw = body.get("chat_endpoint_enabled", False)
    if not isinstance(cee_raw, bool):
        return utils._err(
            400,
            "VALIDATION",
            "chat_endpoint_enabled must be a JSON boolean (true/false)",
        )
    chat_endpoint_enabled = cee_raw

    group_in = (body.get("group") or "").strip()
    if group_in and not utils._NAME_RE.match(group_in):
        return utils._resp(
            400, {"error": "group must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"}
        )
    if group_in and clients.groups_table is not None:
        # Soft-validate: warn at create time if the group doesn't exist yet,
        # rather than silently allowing a typo to drop tenant from any scope.
        try:
            grp_chk = clients.groups_table.get_item(Key={"name": group_in}).get("Item")
            if not grp_chk:
                return utils._resp(
                    404,
                    {
                        "error": f"group '{group_in}' not found — create it first via POST /groups"
                    },
                )
        except Exception:
            pass

    if clone_from and restore_from:
        return utils._resp(
            400, {"error": "clone_from and restore_from are mutually exclusive"}
        )

    # Resolve clone source: must exist + be running. Forces same-host scheduling.
    clone_src = None
    if clone_from:
        clone_src = clients.tenants_table.get_item(
            Key={"id": clone_from}, ConsistentRead=True
        ).get("Item")
        if not clone_src:
            return utils._resp(404, {"error": f"clone source not found: {clone_from}"})
        denied = auth._assert_owner_or_admin(clone_src, event or {})
        if denied is not None:
            return denied
        if clone_src.get("status") != "running":
            return utils._resp(
                400,
                {
                    "error": f"clone source must be running (current: {clone_src.get('status')})"
                },
            )

    tags_err = utils._validate_tags(body.get("tags"))
    if tags_err:
        return utils._resp(400, {"error": tags_err})
    tags = body.get("tags") or {}

    ttl_fields, ttl_err = utils._parse_ttl(body.get("ttl_hours"), body.get("on_expiry"))
    if ttl_err:
        return utils._resp(400, {"error": ttl_err})

    sched, sched_err = utils._parse_schedule(body.get("schedule"))
    if sched_err:
        return utils._resp(400, {"error": sched_err})

    restore_backup_key = ""
    if restore_from:
        src_id = restore_from.get("tenant_id")
        if not src_id:
            return utils._resp(400, {"error": "restore_from.tenant_id required"})
        # #422 — IDOR: 恢复他人备份前必须校验属主(与 clone_from :1106 对称)。缺此校验时,
        # 任意登录用户经 viewer 级 POST /tenants/self 传 restore_from.tenant_id=<他人 id>,
        # 即可把他人备份还原到自己名下 = 跨租户数据读取(code-forensics CONFIRMED)。
        # 校验对象是备份源租户的 DDB 记录(owner_id)。**源记录可能已被彻底删除而 S3 备份仍在**
        # (合法灾备场景,见 test_restore_when_source_tenant_deleted)——此时用空 item 走同一
        # 校验:_assert_owner_or_admin 对无 owner_id 的 item 只放行 admin/api-key(灾备本就是
        # 运维操作),普通 viewer 被拒。既挡 IDOR(记录在→校 owner;记录不在→要 admin),又不
        # 破坏"源已删仍可恢复"契约。owner_id 为 None 的空 item 传入是该函数明确支持的路径。
        restore_src = clients.tenants_table.get_item(
            Key={"id": src_id}, ConsistentRead=True
        ).get("Item") or {}
        denied = auth._assert_owner_or_admin(restore_src, event or {})
        if denied is not None:
            return denied
        ts = restore_from.get("timestamp")
        restore_backup_key = _resolve_backup(src_id, ts)
        if not restore_backup_key:
            if ts:
                return utils._resp(404, {"error": f"backup not found: {src_id}/{ts}"})
            return utils._resp(
                404, {"error": f"no backups found for tenant_id={src_id}"}
            )

    # On a consumer replay the id was already assigned at enqueue time; reuse it
    # so the consumer materializes exactly the id the caller was handed in its 202
    # (no second _gen_id → no orphaned/duplicate tenant). Fresh sync path mints a
    # new id as before.
    # #321(codex 复审):_assigned_tenant_id 只在 consumer 重放(带 _consumer_ident,内部信号)
    # 时才认——否则外部 POST 传任意 _assigned_tenant_id 会成为 id → 进下游 SSM/rm shell。外部
    # 首次调用一律用 _gen_id(受控字符集),杜绝注入源。
    _replay_id = (
        body.get("_assigned_tenant_id")
        if (event or {}).get("_consumer_ident")
        else None
    )
    tenant_id = _replay_id or utils._gen_id(name, client_token, owner_id)
    now = utils._now()
    # #562 —— 受理时刻的 epoch 与由它算出的创建死线。【只算一次】,占位租户行与 dispatch
    # 消息体共用同一个值:两处若各算一次,受理与入队之间的耗时(铸 token/device 身份、
    # 写 secrets)会让两个死线差出几百毫秒到数秒,于是「消费者按消息体判过期」与
    # 「死线执行者按租户行判过期」会对同一个租户给出不同结论 —— 那正是本 issue 要消灭的
    # 「不确定」态。now 是 ISO 串(给人看),这个是 epoch(给判定用)。
    _accepted_epoch = int(time.time())
    # #564 G5 —— 死线秒数走**运行时载体**(SSM Parameter → 缓存 → 回落 env/默认),
    # 而不是 `create_deadline.deadline_at()`(那条只看 env/默认)。
    #
    # **为什么这一行是 G5 的成立条件**(Codex 独立复审抓出来的):没有它,参数改了也不会影响
    # 任何真实死线,而 `GET /system/info` 却把参数值报成 "effective" —— 运维会看到一次
    # **成功但毫无作用**的变更,那正是本门要消灭的「看不见的失败」,只是换了个方向。
    # 计时起点仍是受理时刻(与 #562 一致),口径不变;变的只是"秒数从哪来"。
    #
    # raise 的口径与 env 版一致:参数值非法 → 炸(配置手误必须炸);SSM 读不到/读失败 →
    # 回落 env/默认、不炸(瞬时故障不该把一次合法创建变 500)。
    # #564 G2 —— consumer 重放时**继承**首次受理算出的死线,不重算。
    #
    # 修的是一个真实的不一致:create-via-queue 的 202(:1829)把受理时刻算出的死线
    # **承诺给了客户**,而 consumer 重放时本函数会重新执行 `int(time.time())`,于是写进
    # 租户行、被 `deadline_executor` 用来判死的是一个**更晚**的死线,差值正好是排队时长。
    # 客户被告知 T1、系统按 T2 执行,而 #562 的预算表(`QUEUE_BUDGET_SEC = 180-2-128 = 50s`)
    # 本来就是为「把排队时长算在 180s 之内」留的 —— 重算等于把那 50s 预算变成无限。
    #
    # 走 `event` 而不是 body:body 是**客户可控**的 POST 内容(`MSG_DEADLINE_KEY` 是
    # `"deadline"`,没有下划线前缀,客户塞一个就能自己指定死线);`event["_deadline_epoch"]`
    # 由 consumer 从**消息顶层**构造,与 `_op_id` 同一条不可伪造的路。
    _inherited_dl = (event or {}).get("_deadline_epoch")
    if _inherited_dl is not None:
        _create_deadline_epoch = int(_inherited_dl)
        print(
            f"create_deadline: 继承首次受理的死线 {_create_deadline_epoch},"
            f"距它还剩 {_create_deadline_epoch - _accepted_epoch}s"
        )
    else:
        _dl_sec, _dl_src = deadline_config.effective_deadline_sec(
            create_deadline.ACTION_CREATE
        )
        _create_deadline_epoch = _accepted_epoch + _dl_sec
        if _dl_src != "ssm":
            # 只在**没走参数**时记一行:那意味着运维以为改了参数其实没生效,或者压根没托管这一档。
            # 走了参数是常态,不值得每次创建都刷日志。
            print(
                f"create_deadline: {_dl_sec}s from {_dl_src} "
                f"(参数 {deadline_config.param_name_for(create_deadline.ACTION_CREATE)} 未生效)"
            )

    # ── R16.2 在途去重:同 owner_id+tenant_user_id 只允许一个在途创建 ──
    # 仅首次 API 调用做(consumer 回放已有占位,跳过)。
    _name_scope = name_dedup.dedup_scope(owner_id, tenant_user_id, platform_id)
    if not (event or {}).get("_consumer_ident"):
        _lock_err = inflight_dedup.acquire_inflight_lock(owner_id, tenant_user_id)
        if _lock_err is not None:
            return _lock_err[0]
        # 占位成功,绑定 tenant_id 供后续 409 响应告知调用方
        inflight_dedup.bind_tenant_id(owner_id, tenant_user_id, tenant_id)
        # #240 — owner 作用域活跃 name 去重(治客户 api-key+只传 name 双开)。与
        # inflight 锁互补:inflight 只守在途窗口(成功即释放),name 锁跟随租户活跃
        # 生命周期(创建成功不释放,delete 终态才释放)→ 防"第一次已成功、第二次
        # 晚到"重放。同作用域同 name 已有活跃租户 → 409 NAME_EXISTS。失败要 release
        # inflight 锁(它成功也释放,语义不同),name 锁靠僵尸自愈兜底,不必逐点 release。
        _name_err = name_dedup.acquire_name_lock(_name_scope, name, tenant_id)
        if _name_err is not None:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return _name_err[0]

    # #217/#309 — pinned 放置(preferred_host_id / clone 同 host)不走 dispatch 队列。
    # dispatch → binpack 是"不指定 host 的批量创建"优化:binpack 无 pinned 概念、且
    # dispatch msg 不带 preferred_host_id → 走它会把指定 host 丢掉(unplaced)。pinned
    # 走下方同步路径。(#309:_canary_host 分支已随金丝雀移除。)
    _pinned_placement = bool(
        (body.get("preferred_host_id") or "").strip() or clone_from
    )
    # #394 —— canary 准入【前置到入队之前】:image_channel=canary 但目标 host 没有 READY 的
    # canary 槽(或与 expected 不符)时,必须【同步】返回 409,不能先回 202 queued 再在消费者
    # 重放时静默失败——那样调用方拿到"provisioning asynchronously"却永远等不到租户(实测:
    # 无 canary 槽建 canary 租户曾误返 202,租户随后凭空消失)。fail-closed,绝不回落 live。
    # 只在原始请求上做(消费者重放带 _consumer_ident,重放时下游 1578 那道校验仍会再兜一次)。
    if not (event or {}).get("_consumer_ident"):
        _pre_ch, _pre_ch_err = image_channel_mod.normalize_channel(body.get("image_channel"))
        if _pre_ch_err:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(400, {"error": _pre_ch_err, "code": "VALIDATION"})
        if _pre_ch == "canary":
            _pre_hid = (body.get("preferred_host_id") or "").strip()
            if not _pre_hid:
                inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
                return utils._resp(400, {
                    "error": "image_channel=canary requires preferred_host_id",
                    "code": "VALIDATION"})
            _pre_host = clients.hosts_table.get_item(
                Key={"instance_id": _pre_hid}, ConsistentRead=True
            ).get("Item")
            if not _pre_host:
                inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
                return utils._resp(404, {
                    "error": f"preferred_host_id {_pre_hid} not found", "code": "NOT_FOUND"})
            _pre_snap, _pre_code, _pre_msg = image_channel_mod.resolve_pinned_version(
                _pre_ch, _pre_host.get("image_slots") or {},
                body.get("expected_image_snapshot_time"),
                body.get("expected_image_generation"),
            )
            if _pre_code:
                inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
                return utils._resp(
                    400 if _pre_code == "VALIDATION" else 409,
                    {"error": _pre_msg, "code": _pre_code})
    # ── [hackathon] Dispatch (SQS 标准队列 + 装箱消费,SPEC/sqs-dispatch) ──
    # DISPATCH_QUEUE_URL 非空 且 非 consumer 回放 且 非 pinned → 走装箱路径,优先于旧
    # CREATE_VIA_QUEUE FIFO(SPEC 开关优先级矩阵)。tenants 条件写占位保证幂等:
    # 相同 tenant_id 二次投递 conditional check fail → 返 409(而非重复开 VM)。
    if (
        clients.DISPATCH_QUEUE_URL
        and not (event or {}).get("_consumer_ident")
        and not _pinned_placement
    ):
        # 占位字段集对照同步路径 item(本函数下方 ~L721)保持同宽——#139/#140 真机
        # 抓出窄字段集的两类后果:丢 tags → batch by filter 0 命中;缺
        # channel_secret → hub 握手竞态回归(同步路径特意 mint-up-front)。
        # host_id/vm_num/guest_ip 此刻还不存在(消费端装箱才定),由
        # dispatch_service 装箱 CAS 成功后回写(_backfill_placement)。
        try:
            item = {
                "id": tenant_id,
                "status": "creating",
                # #200 — 删死字段 desired_status:全仓仅此一处写(create dispatch 路径,
                # 写死 running),stop/start/pause 不更新、无任何读点/自愈/对账消费它,
                # 是语义错的死字段。删除比给每个生命周期动作补写更小(YAGNI:没人读的
                # 字段不该维护)。若将来要做 desired-state 对账,再作为独立 feature 全链设计。
                "dispatch_retries": 0,
                "created_at": now,
                "updated_at": now,
                "creation_started_at": now,
                # #562 —— 创建死线的【绝对】时间戳(epoch 秒)。三个消费者读它:
                #   ① dispatch_service 消费前检查过期(过期即丢弃,不起 VM);
                #   ② dispatch_service 判「注定超不过死线」→ 判死 + 触发扩容;
                #   ③ dispatch_poller 的独立死线执行者兜底扫「creating 且已过死线」——
                #      消费者挂掉时死线承诺必须仍兑现,而那恰恰是最需要它兑现的时候(G6)。
                # 用绝对值而非「剩余秒数」:消息会在队列里躺任意久、会被重投多次,相对量每经
                # 一跳都要重算,错一次整条链路口径就不一致。这里算一次,全链只读。
                # creation_started_at 是 ISO 串(给人看),这个是 epoch(给判定用),两者并存。
                create_deadline.ATTR_DEADLINE: _create_deadline_epoch,
                "name": name,
                "vcpu": int(vcpu),
                "mem_mb": int(mem_mb),
                "owner_id": owner_id,
                "health_failures": 0,
                "config_template": config_template,
                "restore_backup_key": restore_backup_key,
                "tags": tags,
                "image_id": image_id,
                "channel_secret": secrets.token_hex(32),
            }
            if owner_id:
                item["uuid"] = owner_id  # #93 同步路径同款 principal 记录
            if security:
                item["security"] = security
            if (
                injected_credentials
            ):  # #118/#116 — 与同步路径同宽,dispatch 消费重建时 host 自取解密
                item["injected_credentials"] = injected_credentials
            if frozen_injection_plan:  # tenant-credential-contract Task 3.5
                item["frozen_injection_plan"] = frozen_injection_plan
                item["registry_version"] = registry_version
                if (
                    _frozen_scheme
                ):  # #149 — host launch-vm.sh:506 读 .Item.scheme 分派解密
                    item["scheme"] = _frozen_scheme
            if tenant_user_id:  # #143 — 占位与同步路径同宽(丢字段=Q2 真机现形)
                item["tenant_user_id"] = tenant_user_id
            if platform_id:
                item["platform_id"] = platform_id
            # #526 — 与同步路径(:2137)/pending 路径(:1751)同宽:必须把 chat_endpoint_enabled
            # 落库。此前 dispatch 占位行漏写 → wake/restore/restart/reset/rebuild/health_check/
            # scaler 用 item.get("chat_endpoint_enabled", False) 反读一律拿 False → 传 launch-vm.sh
            # 第 10 位 "0" → harden-config.sh del(chatCompletions) → guest 数据面永久 404。
            # 首启走 dispatch 消息 params.chat_ep(#160)开了端点,但没落库 → 任何后续 wake 退回 0
            # 删端点,stop/start 亦不自愈。secure default 不变:只在 True 时写字段。
            if chat_endpoint_enabled:  # per-tenant chatCompletions switch (default off)
                item["chat_endpoint_enabled"] = True
            clients.tenants_table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(id)",
            )
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                # #562 G9 —— 与 _persist_tenant_record 的 409 同款:带当前 status 与归因,
                # 客户端才能区分「在跑别重试」与「墓碑换 token」。共用一个 helper,
                # 不在两处各拼一遍(两处漂了就等于没有这个契约)。
                return utils._resp(
                    409, {"error": "tenant already exists", **_conflict_extra(tenant_id)}
                )
            raise

        # #188 — dispatch push 路径的 device/gateway_token 冷注入根因修复。
        # 同步路径在 _launch_vm 前铸 device+token 并冷注入(:1228/:1254);dispatch
        # 路径此前只 put 占位 + send_message,把铸造留给了从不发生的同步分支 → 队列
        # 消息不带密文 → launch-vm 第 12/13 位参空 → wss 免 approve 在生产配置
        # (dispatch.enabled=true mode=push)100% 失效。这里在 send_message 之前补铸,
        # 把密文塞进 msg.params 一路穿透到 host 冷注入(dispatch_service manifest g/d 字段)。
        # fail-open:铸造失败不阻塞已 accepted 的异步 create(占位已落、202 已定),
        # launch-vm 回退到 in-VM openssl gateway token(pre-#187 行为),租户照常起来,
        # 只是这一台不具备 reveal/免 approve 能力——比 500 掉一个已接受的异步请求更好。
        gateway_token_ct = None
        if clients.CLAWPOOL_CMK_ARN and clients.tenant_secrets_table is not None:
            try:
                gateway_token_ct = mint_gateway_token(tenant_id)
            except Exception as e:  # noqa: BLE001 — fail-open,不阻塞异步 create
                print(
                    f"[#188] dispatch mint_gateway_token failed (non-fatal): "
                    f"{type(e).__name__}: {e}"
                )
                _cleanup_gateway_token_secret(tenant_id)  # 清半写残留
                gateway_token_ct = None
        device_paired_b64 = ""
        if (
            owner_id
            and clients.CLAWPOOL_CMK_ARN
            and clients.tenant_secrets_table is not None
        ):
            try:
                # owner==API_KEY_OWNER / 空 → mint_device_identity 返 None → paired 空。
                _device = mint_device_identity(tenant_id, owner_id)
                device_paired_b64 = build_paired_json_b64(_device)
                # #312 — device_paired_b64 是 paired.json(deviceId+publicKey+roles+
                # scopes,**无私钥**,纯公开信息)。除了随 dispatch manifest 一次性下发,
                # 还长期存进 tenants 表(无 TTL)。根因:device 私钥密文在 tenant_secrets
                # 表有 15min TTL,几小时后 restart/recovery(镜像更新自愈)时 #290 DDB
                # fallback 拿到空 → launch-vm 无法重注入 paired.json → 网关读到空盘配对
                # → 前端 NOT_PAIRED(新加坡真机复现 + message-handler.ts:786 getPairedDevice
                # → isPaired=false)。paired.json 本身无私钥,长期留存不泄密,让 launch-vm
                # 每次(重)启动都能从 tenants 表幂等重建 approved backend 条目。
                # #314 — 带重试 + fail-loud 的持久化(dispatch 路径)。
                persist_device_paired_b64(tenant_id, device_paired_b64)
            except Exception as e:  # noqa: BLE001 — 可选增强,失败不回滚
                print(
                    f"[#188] dispatch mint_device_identity failed (non-fatal): "
                    f"{type(e).__name__}: {e}"
                )
                device_paired_b64 = ""

        _sqs_dispatch = getattr(clients, "sqs", None) or boto3.client("sqs")
        msg = {
            "v": 1,
            "action": "create",
            "tenant_id": tenant_id,
            "request_token": (body.get("client_token") or "") or f"req-{tenant_id}",
            # #562 —— 死线随消息走(G7)。消费者【消费前】先看这个字段,过期即丢弃、
            # 不发起任何 SSM。为什么必须在消息体里而不是只查租户行:消费者停 10 分钟后恢复
            # 会一次性领到一大批早已被判死的消息,若每条都回查 DDB 才知道过不过期,那一波
            # 回查本身就是限流风险;而丢弃是安全的 —— 独立死线执行者(G6)已经把它们判死了。
            # 与占位租户行上的 ATTR_DEADLINE 是【同一个值】(见上方 _create_deadline_epoch
            # 的注释:只算一次),所以「按消息判」与「按租户行判」永远同结论。
            create_deadline.MSG_DEADLINE_KEY: _create_deadline_epoch,
            "params": {
                "vcpu": int(vcpu),
                "mem_mb": int(mem_mb),
                "owner_id": owner_id,
                # #160 — 用已校验的 chat_endpoint_enabled 变量(:608 从对外字段名
                # chat_endpoint_enabled 校验而来),不是从 body 再取错 key chat_ep
                # (客户端从不传 chat_ep,那样恒 False → dispatch 路径静默丢开关)。
                # consumer(dispatch_service:399/557)读 params["chat_ep"],故此处 key
                # 仍叫 chat_ep(队列内部字段名),只修取值来源。
                "chat_ep": chat_endpoint_enabled,
                "image": config_template or "default",
                # #199 — restore 意图透传给 consumer → manifest/assignments → launch。
                # 现状 dispatch 路径丢 restore_backup_key,restore-create 走队列时 host
                # 拿不到 key → 静默用空白模板盘(数据丢失级)。空 = 非 restore 普通建,
                # 下游 launch RESTORE_KEY 为空走建盘;非空 = restore,launch 必须用它,
                # 缺则 fail-loud 拒起(见 launch-vm 空 key 守卫)。
                "restore_backup_key": restore_backup_key,
                # #188 — 预铸密文/配对元数据透传给 consumer → manifest/assignments →
                # host 冷注入。空值(feature-off / owner 未知)不影响下游:manifest
                # encode 只在非空时写 g/d,launch-vm fail-open 跳过。
                "gateway_token_ct": gateway_token_ct,
                "device_paired_b64": device_paired_b64,
            },
        }
        try:
            _sqs_dispatch.send_message(
                QueueUrl=clients.DISPATCH_QUEUE_URL, MessageBody=json.dumps(msg)
            )
        except Exception as e:  # noqa: BLE001
            print(f"[dispatch] send_message failed: {e}")
            # 失败:回滚占位(best-effort),返 5xx 让客户端重试
            try:
                clients.tenants_table.delete_item(Key={"id": tenant_id})
            except Exception:  # noqa: BLE001
                pass
            # #353 — 回滚也清 secrets 行:此前已 mint gateway token(:1136)+ device
            # 身份(:1152)。TTL 移除后没有自动清扫兜底,不清 = 无主凭据永久残留。
            _cleanup_gateway_token_secret(tenant_id)
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(503, {"error": "dispatch enqueue failed; retry"})
        # 在途窗口关闭:租户已落库(creating)+ 已入 dispatch 队列,"创建在途"结束,
        # 释放占位锁(设计=in-flight 去重,非"一人一VM";后续去重交给"租户已存在")。
        # 不释放会让同 owner+tenant_user_id 的第二次合法创建被 409 挡到 30min TTL。
        inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
        return utils._resp(
            202,
            {
                "id": tenant_id,
                "status": "queued",
                "message": "create accepted; dispatching",
                # #562 —— 死线必须出现在【每一条】202 上,不能只在部分路径上。
                # 真机 invoke 抓出来的:队列路径(生产默认)的 202 原来不带这个字段,
                # 而契约文档说「响应体带死线」。调用方【无从知道】自己走了哪条内部路径,
                # 所以一个只在部分路径出现的字段等于不可用 —— 客户端要么得容忍它缺失
                # (那它就没有约束力),要么会在缺失时崩。故四条 202 出口全部带上。
                create_deadline.ATTR_DEADLINE: _create_deadline_epoch,
            },
        )

    # ── Phase 2: shed-load a 380-create burst onto the FIFO queue ──
    # All CHEAP validation above ran synchronously (so the caller still gets an
    # immediate 400 on a bad request). Now, if create-via-queue is enabled and
    # this is NOT a consumer replay, enqueue the create and return 202 — the
    # consumer drains the queue at its reserved-concurrency rate, so a burst of
    # 380 POST /tenants no longer fans out 380 synchronous SSM calls and trips the
    # SSM single-instance concurrency wall (measured: 40 concurrent → 11 TimedOut).
    # Parallelism is preserved: MessageGroupId = tenant_id means every create is
    # its own FIFO group and they consume concurrently (only same-tenant ops
    # serialize). We stamp the already-generated tenant_id into the queued body so
    # the consumer creates exactly THIS id (no second _gen_id → no ghost tenant)
    # and the caller can poll GET /tenants/{id} immediately. enqueued_at lets the
    # consumer emit a queue-wait latency metric (the 1-minute SLA guard).
    if (
        clients.CREATE_VIA_QUEUE
        and clients.LIFECYCLE_QUEUE_URL
        and not (event or {}).get("_consumer_ident")
    ):
        queued_body = dict(body)
        queued_body["_assigned_tenant_id"] = tenant_id
        queued_body["_enqueued_at"] = now
        # #108 — stamp the pinned platform_id into the queued snapshot so the
        # tenant lands in the caller's namespace even if scope resolution on
        # replay ever changed. (enqueue_lifecycle also carries platform_scope in
        # _ident, which re-pins on replay; this makes the body self-consistent.)
        if platform_id:
            queued_body["platform_id"] = platform_id
        if lifecycle_dispatch.enqueue_lifecycle(
            "create",
            tenant_id,
            event,
            extra=queued_body,
            # #564 G2 —— 把受理时刻算出的死线带进消息,consumer 用它做两件事:
            # ① 消费前判过期(G3);② 重放 `create_tenant` 时**继承**它而不是重算
            # (见 :1499 那段:重算会让客户被承诺的死线与实际执行的不一致)。
            # 与上面两条 202 里返给客户的、以及占位租户行上的 `create_deadline`
            # 是【同一个值】。
            deadline_epoch=_create_deadline_epoch,
        ):
            # 在途窗口关闭:已入 FIFO 队列,释放占位锁(同上;见 inflight_dedup docstring)。
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                202,
                {
                    "id": tenant_id,
                    "status": "queued",
                    "message": "create accepted; provisioning asynchronously",
                    # #562 —— 同上:死线必须出现在每一条 202 上(见上一处的完整说明)。
                    create_deadline.ATTR_DEADLINE: _create_deadline_epoch,
                },
            )

    # task #15 — mint this tenant's own LiteLLM vkey (spend/budget split per
    # tenant↔sub). None if billing unconfigured → falls back to the image's
    # shared key (backward compatible). Stored on the record; launch-vm.sh
    # injects it into the per-VM openclaw.json.
    tenant_vkey = vkey._mint_tenant_vkey(tenant_id, owner_id)

    # channel_secret — the per-tenant HMAC secret the in-VM claw-channel signs
    # its hub registration with (the hub verifies against this same value, read
    # from this DDB record). We MINT IT HERE (control plane, before the VM
    # exists) and pass it to launch-vm.sh, instead of letting launch-vm.sh
    # `openssl rand` its own and relying on host-agent to SSH-read-back + mirror
    # it into DDB afterwards. That read-back path had a startup RACE: the VM's
    # channel dialed the hub within seconds of boot (token-fail / 401) while DDB
    # still had no channel_secret (host-agent's 15s poll hadn't mirrored it yet);
    # the channel exhausted its retry budget (~30s) and gave up permanently →
    # "agent offline" forever. Minting up-front makes DDB authoritative and
    # populated BEFORE the VM boots, so the hub verifies the very first attempt.
    # 64 hex chars == openssl rand -hex 32 (the format launch-vm.sh used).
    channel_secret = secrets.token_hex(32)

    # #187 P5 — Cognito 渠道机器用户(WI-002)随 channel/hub 一起下线;数据面走
    # 两级路由到 microVM:18789 gateway,鉴权改走 gateway 原生 token。

    # Find host with capacity. The scheduler is normally automatic, but
    # operators occasionally need to pin a tenant to a specific host (e.g.
    # to drain a host before terminating it, or to keep two related VMs on
    # the same hardware). Three modes, in priority order:
    #   1. clone_from → must land on the source's host (local `cp` only)
    #   2. preferred_host_id (admin/operator) → land there or fail
    #   3. default → first host with capacity
    preferred_host_id = (body.get("preferred_host_id") or "").strip()
    # #394 —— image_channel 准入(ADR §4.3)。缺省 live = 既有行为字节不变。
    # canary 必须显式 pin 到装过 canary 槽的那台 host(canary 槽不是全 fleet 都有)。
    image_channel, _ch_err = image_channel_mod.normalize_channel(body.get("image_channel"))
    if _ch_err:
        inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
        return utils._resp(400, {"error": _ch_err, "code": "VALIDATION"})
    if image_channel_mod.requires_pinned_host(image_channel) and not preferred_host_id:
        inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
        return utils._resp(400, {
            "error": "image_channel=canary requires preferred_host_id (the canary slot "
                     "exists only on the host you pulled it to)",
            "code": "VALIDATION",
        })
    if clone_src:
        host = scheduling._get_specific_host_with_capacity(
            clone_src["host_id"], vcpu, mem_mb
        )
        if not host:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                400,
                {
                    "error": f"clone source's host {clone_src['host_id']} lacks "
                    f"capacity for clone (vcpu={vcpu}, mem_mb={mem_mb})"
                },
            )
    elif preferred_host_id:
        host = scheduling._get_specific_host_with_capacity(
            preferred_host_id, vcpu, mem_mb
        )
        if not host:
            # Distinguish "host doesn't exist" from "host full" so the
            # console can render the right message.
            existing = clients.hosts_table.get_item(
                Key={"instance_id": preferred_host_id}, ConsistentRead=True
            ).get("Item")
            if not existing or existing.get("status") in ("deleted", "draining"):
                inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
                return utils._resp(
                    404,
                    {
                        "error": f"preferred_host_id {preferred_host_id} not found or draining"
                    },
                )
            # #540 — 污点(cordon)机器被显式指定:409,与 draining 的 404、容量不足的 400
            # 三者分开。分开的理由是三种情况运维要做的事完全不同 —— 404 是打错了 id 或机器
            # 正在下线;400 是这台真的满了,换台或等腾空;409 是这台被【刻意】标了不收新租户,
            # 你要么换台,要么先取消污点。混成一个码会让人对着满载去排查,方向就错了。
            # 复用上面那次诊断性 get_item,不额外读。
            if host_taint.is_tainted(existing):
                inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
                return utils._resp(
                    409,
                    {
                        "error": f"preferred_host_id {preferred_host_id} is tainted "
                        "(cordoned): it accepts no new tenants. Pick another host, "
                        "or remove the taint first.",
                        "code": "HOST_TAINTED",
                    },
                )
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                400,
                {
                    "error": f"preferred_host_id {preferred_host_id} lacks capacity "
                    f"(vcpu={vcpu}, mem_mb={mem_mb})"
                },
            )
    else:
        host = scheduling._find_host(vcpu, mem_mb)
    if not host:
        # No capacity — save as pending and scale out.
        # Persist config_template and restore_backup_key so process_pending() can apply them.
        item = {
            "id": tenant_id,
            "name": name,
            "vcpu": vcpu,
            "mem_mb": mem_mb,
            "status": "pending",
            "health_failures": 0,
            "config_template": config_template,
            "restore_backup_key": restore_backup_key,
            "tags": tags,
            "created_at": now,
            "updated_at": now,
            # #562 —— pending 路径也要落死线。这条路径最需要它:它进来就是【没有 host】,
            # 靠 scale-out + process_poller promote,是「等容量」的典型;不落死线,
            # 独立死线执行者(G6)扫不到它,租户就会永久停在 pending —— 而 pending 既不是
            # running 也不是 failed,业务拿它做不了任何决策,正是本 issue 要消灭的非终态。
            create_deadline.ATTR_DEADLINE: _create_deadline_epoch,
        }
        if owner_id:  # issue #80 — record ownership for IDOR enforcement
            item["owner_id"] = owner_id
            item["uuid"] = owner_id  # #93 — stable Cognito-sub principal (= owner_id);
            # NOT the primary key (id stays name-xxxx so one user can own many tenants)
        item["image_id"] = image_id  # #93 — golden rootfs version at creation
        if security:  # #93 — per-tenant encryption/security config (optional)
            item["security"] = security
        if injected_credentials:  # #118/#116 — platform-injected credential ciphertext
            item["injected_credentials"] = injected_credentials
        if frozen_injection_plan:  # tenant-credential-contract Task 3.5
            item["frozen_injection_plan"] = frozen_injection_plan
            item["registry_version"] = registry_version
            if _frozen_scheme:  # #149 — host launch-vm.sh:506 读 .Item.scheme 分派解密
                item["scheme"] = _frozen_scheme
        if tenant_user_id:  # task #13/#14 — external user attribution
            item["tenant_user_id"] = tenant_user_id
        if platform_id:  # #106 — external-platform attribution (筛租户用)
            item["platform_id"] = platform_id
        item.update(purchase_fields)  # #106 — order_id/plan_tier/purchase_status
        if tenant_vkey:  # task #15 — per-tenant LiteLLM billing key
            item["litellm_vkey"] = tenant_vkey
        if skills_in is not None:
            item["skills"] = skills_in
        if group_in:
            item["group"] = group_in
        if chat_endpoint_enabled:  # per-tenant chatCompletions switch (default off)
            item["chat_endpoint_enabled"] = True
        item.update(ttl_fields)
        if sched:
            item["schedule"] = sched
        # #93 / api-design-review C2/J2 — conditional put prevents a same-id replay
        # (retry, double-submit, duplicate queue consume) from overwriting an
        # existing tenant. Same id already present → 409 ConflictException (C4),
        # not a silent clobber. This is the durable idempotency layer; SQS FIFO
        # dedup (C6) only sheds load, it doesn't guarantee exactly-once.
        try:
            clients.tenants_table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(id)"
            )
        except (
            clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
        ):
            # #562 G9 —— 第三处 409(pending 路径)。三处共用 _conflict_extra:
            # 少改一处就等于客户端在那条路径上仍分不清「在跑」与「墓碑」。
            return utils._err(
                409,
                "CONFLICT",
                f"tenant '{tenant_id}' already exists",
                extra=_conflict_extra(tenant_id),
            )
        # #562 —— 这一行是 _scale_out() 在全仓的【唯一】调用点(G15 核实过)。
        # 形态第 3 条要把本路径的 201 统一成 202,顺手改动这一段时【绝不能把它删掉】:
        # 删了之后连现存这一条扩容触发都没有,而 dispatch 队列路径(生产默认 DISPATCH_MODE=ddb)
        # 本来就不触发扩容。#562 在 dispatch 判死路径【另外】补了触发(G14),两处并存不是重复:
        # 这一条管「同步路径无 host」,那一条管「队列路径判死」。
        scheduling._scale_out()
        audit._publish_event(
            "tenant.created",
            tenant_id,
            {
                "name": name,
                "vcpu": vcpu,
                "mem_mb": mem_mb,
                "status": "pending",
            },
        )
        # 在途窗口关闭:租户已落库(pending)+ 已触发 scale-out,后续 process_pending
        # (consumer replay,不 acquire)在 HostReady 时 promote。释放占位锁(同其它成功
        # 路径;不释放会 409 挡同 owner+user 的第二次合法创建到 30min TTL)。
        inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
        # #562 形态第 3 条 —— 合法请求【永远 202】。这条路径原本返 201 `pending`,
        # 而 201 的语义是「已创建」:客户端拿到 201 会以为 VM 已经在了,实际上机队还没有
        # host、要等 scale-out + process_pending 才 promote。202「已受理」才是实情。
        # 三条创建出口(queued / pending / 同步 creating)现在口径一致:合法即 202,
        # 前置校验失败仍同步 400/409(形态第 3 条的括号部分)。
        # 死线一并给出去:客户端据它决定「什么时候可以认定这次创建没戏了」——
        # 不暴露排队位次(那会把调度算法绑进对外契约),只暴露绝对死线时间戳。
        return utils._resp(
            202,
            {
                "id": tenant_id,
                "status": "pending",
                "message": "create accepted; scaling out, VM will be created when host is ready",
                create_deadline.ATTR_DEADLINE: _create_deadline_epoch,
            },
        )

    # Allocate vm_num + reserve capacity ATOMICALLY (GITHUB-scheduler-bugs P0).
    #
    # The old code read host["next_vm_num"], computed guest_ip/host_port from it,
    # then did an unconditional `SET next_vm_num = :next` (absolute assignment).
    # Under concurrent create_tenant calls, _find_host deterministically returns
    # the same least-loaded host, every caller reads the SAME next_vm_num, and
    # they all land on the same vm_num / guest_ip / host_port — the real root of
    # the observed PriorityInUse / "all packed on one host" failures, plus the
    # absolute assignment silently dropped concurrent increments (capacity ledger
    # drift). Fix: a compare-and-swap that atomically (a) confirms next_vm_num is
    # unchanged since we read it, (b) re-checks capacity at write time so we never
    # oversell, and (c) increments next_vm_num / used_* in one conditional update.
    # On contention (ConditionalCheckFailedException) we re-pick a host and retry.
    def _reserve_slot(h):
        """Atomically claim a vm_num + capacity on host h.

        Returns `(vm_num, None)` on success, else `(None, reason)` where reason is
        one of `_CLAIM_CONTENDED` / `_CLAIM_FULL`.

        (见下方 tried_hosts 的注释,那是 2026-08-11 实测事故的修复),但在"只是抢输了
        槽位号"时把它拉黑,等于把一台【仍有余量】的 host 从池子里删掉。池里只剩一台有
        空间时,拉黑它就没有下一台 → 8 次重试只用掉 1 次就直接 503。而"池子接近装满"
        正是 #430 追求的稳态,所以这不是异常路径,是日常。
        """
        expected = int(h.get("next_vm_num", 1))
        target, occupied = scheduling.next_free_phys_num(
            h["instance_id"], expected, exclude_ids={tenant_id}
        )
        if occupied is None or target is None:
            # 本 host 上找不到空闲物理号 —— 这是"这台装不下了",不是抢输,走 host 轮换。
            return None, _CLAIM_FULL
        # #430 — 必须与 scheduling._find_host 同口径(per-family ratio)。用全局 ratio
        # 会让"选得中、订不上":_find_host 按 per-family 判定还有容量并选中该 host,
        # CAS 却按更小的全局 allocatable 恒拒 → 8 次重试全废在同一批 host 上 → 503
        # "slot allocation contended out",而低优先级 host 明明空着(apse1 2026-08-11 实撞:
        # 三台 r8g used_mem=768000,per-family cap_m=784910 放行 / 全局 cap_m=767970 拒)。
        _cpu_r, _mem_r = host_profile.ratios(
            h,
            (clients.CPU_OVERCOMMIT_RATIO, clients.MEM_OVERCOMMIT_RATIO),
            clients.OVERCOMMIT_BY_FAMILY,
        )
        cap_v = capacity.allocatable(int(h["total_vcpu"]), _cpu_r) - vcpu
        cap_m = capacity.allocatable(int(h["total_mem_mb"]), _mem_r) - mem_mb
        # 普通租户落到 host → 顺手把 host 标 active(有租户即活跃)。
        # #309 — 金丝雀预留槽分支(_is_canary,原不写 #s 以免谎报 upgrading host active)
        # 已随金丝雀移除:upgrading host 现在根本到不了 _reserve_slot(在
        # _get_specific_host_with_capacity 就被挡),故只剩这一条正常路径。
        _set_expr = (
            "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
            "vm_count = vm_count + :one, next_vm_num = :next_after, #ps = :tid, "
            "#s = :a REMOVE idle_since"
        )
        _names = {"#s": "status", "#ps": scheduling.phys_slot_attr(target)}
        _vals = {
            ":v": vcpu,
            ":m": mem_mb,
            ":one": 1,
            ":a": "active",
            ":tid": tenant_id,
            ":expected": expected,
            ":next_after": target + 1,
            ":cap_v": cap_v,
            ":cap_m": cap_m,
            **host_taint.NOT_TAINTED_VALUES,  # #540
        }
        try:
            _kwargs = dict(
                Key={"instance_id": h["instance_id"]},
                UpdateExpression=_set_expr,
                # #540 — 污点原子门。选点(_find_host)与认领之间有窗口:运维正好在这时标记。
                # 用条件写解决而不是认领前再读一次 —— 二次读只是把窗口缩小,不消除它,而且多
                # 一次强一致读。条件写是零额外读的原子判定。
                ConditionExpression=(
                    "next_vm_num = :expected AND used_vcpu <= :cap_v "
                    "AND used_mem_mb <= :cap_m AND attribute_not_exists(#ps) "
                    "AND " + host_taint.NOT_TAINTED_CONDITION
                ),
                ExpressionAttributeValues=_vals,
                ReturnValues="UPDATED_NEW",
                # #475 必修1 —— CCF 本身不说是哪个子条件失败,而这里的条件表达式混了四个
                # (next_vm_num 竞态 / vCPU 满 / 内存满 / 物理槽被占)。ALL_OLD 让 DDB 在
                # 条件失败时把该 item 的旧值捎回异常里,免去一次额外读(AWS 文档
                # WorkingWithItems「Returning the item attributes of a failed
                # conditional write」)。原子取值,无 TOCTOU。
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            if _names:
                _kwargs["ExpressionAttributeNames"] = _names
            r = clients.hosts_table.update_item(**_kwargs)
            # CAS 返回的 next_vm_num - 1 与预先算出的 target 恒等；缺 Attributes 时回退 target。
            try:
                return int(r["Attributes"]["next_vm_num"]) - 1, None
            except (KeyError, TypeError, ValueError):
                return target, None
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                # 非 CCF(限流 / 权限 / DDB 故障)照旧原样重抛 —— 但抛之前要释放 inflight 锁。
                # 这个漏锁是【先存在】的(本 issue 之前就在),不是本次引入;修在这里是因为它与
                # 上面那个读失败是同一个缺陷、只隔三行,且无法单独测(要复用同一套 harness)。
                inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
                raise
            # 用赢家写完后的当前状态刷新本地 host 字典 —— 否则下一轮 expected 还是旧的
            # next_vm_num,CAS 必然再次失败(见 _fresh_host_state 的真机实测记录)。
            # cap_v / cap_m 是按【本次请求规格】算的余量,所以"满"= 满足不了这一笔。
            _fresh = _fresh_host_state(e.response)
            why = _classify_claim_failure(e.response, cap_v, cap_m)
            if why == _CLAIM_CONTENDED and "next_vm_num" not in _fresh:
                # 判了抢输,却没能从 ALL_OLD 里取到可解析的 next_vm_num —— 原地重试仍会用
                # 旧值,CAS 注定再次失败。降级到 UNKNOWN 走强一致读定夺,而不是拿这台耗预算。
                # (第五轮评审指的是强一致读那一侧的同类洞;这一侧是顺着它查出来的。)
                why = _CLAIM_UNKNOWN
            if why != _CLAIM_UNKNOWN:
                h.update(_fresh)
                return None, why
            # 无法归因 —— 原地重试注定失败(没有新的 next_vm_num),盲目换机又会把可能仍有
            # 余量的 host 拉出池子。所以做【一次】强一致读定夺,这条路很少走,读一次不心疼。
            # 读失败【不吞】。限流 / 权限不足 / DDB 故障不是"这台满了" —— 把它伪装成换机
            # 会把系统性故障说成容量不足:表级限流时每台 host 都"满",客户拿到 503
            # "no host capacity",而日志里看不到真因。既误导排查,也违反"只 catch 你能处理
            # 的、绝不静默吞异常"。让它抛出去,handler.py 顶层 except 会打 traceback 并回
            # 500,客户可重试而真因留在日志里。第七轮评审抓到这条 —— 与第五轮吞掉 int()
            # 失败是同一类违规。
            # 唯一要收尾的是 inflight 锁:其余失败路径都先释放再返回,直接抛会把它漏掉
            # (有 30min TTL 兜底不至死锁,但该用户 30 分钟内建不了新租户)。所以先释放再抛。
            try:
                _cur = (
                    clients.hosts_table.get_item(
                        Key={"instance_id": h["instance_id"]}, ConsistentRead=True
                    ).get("Item")
                    or {}
                )
            except Exception:
                # 捕 Exception 而不只是 ClientError:传输层错误(EndpointConnectionError /
                # ConnectTimeoutError / ReadTimeoutError)是 BotoCoreError 子类,【不是】
                # ClientError,只捕后者会让它们绕过这里、把 inflight 锁漏掉 30 分钟 ——
                # 正是本次要避免的那个后果(第八轮评审抓到)。
                # 不捕 BaseException:SystemExit / KeyboardInterrupt 在 Lambda 里不会走
                # 正常栈展开(超时是直接杀进程),捕它买不到真实收益,只会让意图更含糊。
                # 这里只做"释放锁"这一件能处理的事,异常【原样重抛】,不改类型也不吞。
                inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
                raise
            if not _cur:
                # host 行已不在(实例被终止/回收)。它永远不会成功,必须换机。
                return None, _CLAIM_FULL
            # 三个 CAS 必需字段必须【原子地】全部可解析。存在性不等于可用:
            #   缺字段     → 条件对缺失属性恒假(已实测,见测试里的 DDB 语义那条)
            #   值非数值   → int() 抛错;把它吞掉再原地重试,等于拿一台必败的 host 耗完
            #                预算,还可能在下一轮 CAS 上撞 DynamoDB 类型错误
            # 两种都让这台永远认领不到,所以一律当不可用换机。原子性是关键 —— 逐字段
            # try/except 会留下"改了一半"的本地状态,而且吞异常本身违反项目铁律
            # (第五轮评审抓到的正是那个被吞掉的 int() 失败)。
            # 半行是真实形态:见 #445 里心跳的无条件 update_item 建出的只有心跳字段的行。
            try:
                _parsed = {
                    _k: int(_cur[_k])
                    for _k in ("next_vm_num", "used_vcpu", "used_mem_mb")
                }
            except (KeyError, TypeError, ValueError):
                return None, _CLAIM_FULL
            # 只刷 CAS 参与的三个字段。vm_count 由 CAS 自增、本地值不参与判定,不必带上 ——
            # 少刷一个字段就少一处需要吞异常的地方。
            h.update(_parsed)
            return None, _CLAIM_CONTENDED

    vm_num = None
    # #430 — hosts this caller already lost the CAS on. Without it every retry
    # re-picks the SAME host: _find_host ranks by (affinity_tier, balance), and a
    # host that is full-but-not-yet-reflected still ranks best, so all 8 attempts
    # burn on it and the caller 503s while a lower-tier host sits empty (observed
    # apse1 2026-08-11: three r8g at 97.6% returned 117× "slot allocation
    # contended out" with the m8g completely idle). Feeding the loser into
    # `exclude` makes the next pick walk down the priority order instead.
    tried_hosts = set()
    for attempt in range(8):
        claimed, why = _reserve_slot(host)
        if claimed is not None:
            vm_num = claimed
            break
        # Lost the CAS race or host filled up — clones must stay on their source
        # host, so they can't re-pick and just fail; others re-pick a host.
        if clone_src or preferred_host_id:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                409,
                {"error": "host slot contended or filled during allocation; retry"},
            )
        # #475 必修1 —— 退避必须带抖动。所有输家是被同一个赢家同时踢出来的,固定退避
        # 让它们下一轮仍然对齐、继续互撞;抖动把重试打散开。真机实测(apse1 6 路并发):
        # 无抖动时输家整批同时重试,连撞到重试预算用尽。
        time.sleep(0.05 * (attempt + 1) * (0.5 + secrets.randbelow(1000) / 1000.0))
        # 只有"这台真装不下了"才把它拉出候选池并换机;纯粹抢输槽位号时
        # 留在本 host 上重试(它仍有余量,退避后赢家已写完)。此前两者不分,导致池子接近
        # 装满时一次抢输就把最后一台有空间的 host 拉黑 → 立刻 503,8 次重试只用掉 1 次。
        if why != _CLAIM_FULL:
            continue
        tried_hosts.add(host["instance_id"])
        # 换机重挑也要防漏锁。_find_host 内部【没有】任何 except(实测:0 处),所以 DDB
        # 限流 / 权限错误会从这里抛出去,绕过下面所有显式 release —— 与第七/八轮那两条是
        # 同一族缺陷,只是站点不同。主动补上,不等评审再指一次。
        # 说明范围:create_tenant 整体上"任何异常都会把 inflight 锁留到 30min TTL"这个性质
        # 是【先存在】的,彻底解决要给整个函数加一层 finally 归属,属独立 issue;这里只收
        # 槽位认领这一段里可确证的逃逸点。
        try:
            host = scheduling._find_host(vcpu, mem_mb, exclude=tried_hosts)
        except Exception:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            raise
        if not host:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(503, {"error": "no host capacity (contended out)"})
    if vm_num is None:
        inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
        return utils._resp(
            503, {"error": "slot allocation contended out after retries"}
        )

    # #208 姊妹洞 — create 路径也要防物理 tap 撞号(no-cross-tenant 不可退底线)。
    # _reserve_slot 的 next_vm_num 是**单调 DDB 记账序号**,对"迁入本 host 的租户物理占
    # 用哪个 tap"一无所知:迁移 restore 后 VM 挂 tap-vm{原始 launch vm_num}(migrate-vm.sh
    # 从 snapshot 的 vm.json 读 SRC_VM_NUM),但其 DDB vm_num 已被翻成 target 槽位号 → 物理
    # tap 号与 DDB vm_num 分叉。若单调序号后来又发到某迁入租户的物理 tap 号,新 VM 的
    # launch-vm.sh 会 `ip link del`+`kill -KILL` 抢占那条已在用的 tap(launch-vm.sh:707-719)
    # → 杀掉迁入租户 FC + 劫持其网络。故认领 slot 后按**物理 tap**(phys_vm_num,见下)复检本
    # host 是否已被占;占了就丢弃这个号继续单调认领下一个(_reserve_slot 已保证 next_vm_num
    # 只增不退,重试拿到的必是更大的号,不会死循环)。
    for _skip in range(64):
        if not _phys_tap_occupied(host["instance_id"], vm_num, exclude_id=tenant_id):
            break
        # 该号的物理 tap 已被某迁入租户占用 —— 归还容量记账、丢弃此号,认领下一个。
        scheduling._release_slot(
            host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
        )
        claimed, _why = _reserve_slot(host)
        if claimed is None:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                503,
                {"error": "no free vm slot on host (phys tap contended); retry"},
            )
        vm_num = claimed
    else:
        # 连续 64 个号都撞物理 tap —— 极端异常(host 上迁入租户密集),fail-closed。
        scheduling._release_slot(
            host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
        )
        inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
        return utils._resp(
            503,
            {"error": "unable to find a free physical vm slot on host; retry"},
        )

    guest_ip = auth._guest_ip(vm_num)
    host_port = clients.VM_PORT_BASE + vm_num - 1

    # #394 —— canary 租户:把 channel 解析成【具体不可变版本】并固定到租户记录(ADR §4.3)。
    # 只存 channel 不行 —— promote 清空 canary 指针后该租户 restart 会解析不到 / 解析到别的
    # 候选版本 = 版本漂移,验证结论作废。expected_* 是调用方从 pull Job result 读到的值,
    # 在此做 TOCTOU 校验(期间被并发 pull 换掉即拒绝),不回落 live。
    # 槽位真值读 host 记录上的 image_slots 镜像(控制面副本);canary 装成功时由 pull 落。
    pinned_image_snapshot_time = None
    if image_channel == "canary":
        _slots = host.get("image_slots") or {}
        pinned_image_snapshot_time, _pin_code, _pin_msg = (
            image_channel_mod.resolve_pinned_version(
                image_channel, _slots,
                body.get("expected_image_snapshot_time"),
                body.get("expected_image_generation"),
            )
        )
        if _pin_code:
            scheduling._release_slot(
                host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
            )
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                400 if _pin_code == "VALIDATION" else 409,
                {"error": _pin_msg, "code": _pin_code},
            )

    item = {
        "id": tenant_id,
        "name": name,
        "host_id": host["instance_id"],
        "vm_num": vm_num,
        # #208 — phys_vm_num:租户 microVM 物理挂的 tap-vm{N} 号,= launch 时的 vm_num,
        # **迁移永不改写**(migrate finalize 只翻 vm_num=target 槽,phys_vm_num 恒等原始
        # launch 号,与 snapshot vm.json 里 migrate-vm.sh:182 读的 SRC_VM_NUM 一致)。这是
        # "物理 tap 归属"的唯一持久权威字段;撞号检查(create + migrate)一律键在它上,不再
        # 键在会随迁移分叉的 vm_num 上。创建时二者相等。
        "phys_vm_num": vm_num,
        "guest_ip": guest_ip,
        "host_port": host_port,
        "vcpu": vcpu,
        "mem_mb": mem_mb,
        "status": "creating",
        "health_failures": 0,
        # #562 —— 同步路径也要落死线。它虽然已经装箱、已下发 launch,但 status 仍是
        # creating:host 侧起 VM 失败或卡住时,没有死线就没人把它推到终态(现有自愈只覆盖
        # 部分形态)。三条创建出口都落同一个字段,死线执行者才有单一扫描判据。
        create_deadline.ATTR_DEADLINE: _create_deadline_epoch,
        "rootfs_version": host.get("rootfs_version", ""),
        # #517 阶段1 —— 新租户继承 host 当前的 immutable_version(只读身份盘版本坐标)。
        # Canary 租户实际从固定的 versions/<snapshot>/ 挂盘,必须记录 candidate 坐标;
        # live 租户才继承 host 当前坐标。否则 canary 一出生就被误标成 live。
        "immutable_version": _initial_immutable_version(
            host, pinned_image_snapshot_time
        ),
        "config_template": config_template,
        "restore_backup_key": restore_backup_key,
        "tags": tags,
        "creation_started_at": now,
        "created_at": now,
        "updated_at": now,
        # Authoritative HMAC secret for the hub↔channel handshake, present in DDB
        # BEFORE the VM boots (kills the host-agent read-back race; see above).
        "channel_secret": channel_secret,
    }
    if item["rootfs_version"] and len(item["rootfs_version"].encode("utf-8")) <= 256:
        item["q_rootfs_version"] = item["rootfs_version"]
    if owner_id:  # issue #80 — record ownership for IDOR enforcement
        item["owner_id"] = owner_id
        item["uuid"] = owner_id  # #93 — stable Cognito-sub principal (= owner_id);
        # NOT the primary key (id stays name-xxxx so one user can own many tenants)
    item["image_id"] = image_id  # #93 — golden rootfs version at creation
    # #394 —— 记 channel(审计/展示)+ canary 的固定版本(launch-vm 起盘的权威来源)。
    # live 租户【不】写 image_snapshot_time:它每次启动解析当前 live 指针(既有产品语义:
    # 运行中不受指针变化影响,restart 时拿当前 live)。
    item["image_channel"] = image_channel
    if pinned_image_snapshot_time:
        item["image_snapshot_time"] = pinned_image_snapshot_time
    if security:  # #93 — per-tenant encryption/security config (optional)
        item["security"] = security
    if injected_credentials:  # #118/#116 — platform-injected credential ciphertext
        item["injected_credentials"] = injected_credentials
    if frozen_injection_plan:  # tenant-credential-contract Task 3.5
        item["frozen_injection_plan"] = frozen_injection_plan
        item["registry_version"] = registry_version
        if _frozen_scheme:  # #149 — host launch-vm.sh:506 读 .Item.scheme 分派解密
            item["scheme"] = _frozen_scheme
    if tenant_user_id:  # task #13/#14 — external user attribution
        item["tenant_user_id"] = tenant_user_id
    if platform_id:  # #106 — external-platform attribution (筛租户用)
        item["platform_id"] = platform_id
    item.update(purchase_fields)  # #106 — order_id/plan_tier/purchase_status
    if tenant_vkey:  # task #15 — per-tenant LiteLLM billing key
        item["litellm_vkey"] = tenant_vkey
    if skills_in is not None:
        item["skills"] = skills_in
    if group_in:
        item["group"] = group_in
    if chat_endpoint_enabled:  # per-tenant chatCompletions switch (default off)
        item["chat_endpoint_enabled"] = True
    if clone_from:
        item["clone_from"] = clone_from
    item.update(ttl_fields)
    if sched:
        item["schedule"] = sched
    # Capacity + vm_num already reserved atomically above; just record the tenant.
    # If this put fails we roll the reservation back so the ledger stays honest.
    # #93 / api-design-review C2/J2 — conditional put: a same-id replay (retry,
    # double-submit, duplicate queue consume) must not overwrite an existing
    # tenant. On conflict we release the slot THIS attempt just reserved (the
    # original tenant keeps its own) and return 409 — avoids both a silent clobber
    # and a capacity leak. Any other failure rolls back + re-raises as before.
    #
    # #394 codex NB2 —— canary 租户固定了具体版本(image_snapshot_time):它的持久化必须与
    # 全局删快照【线性化】。否则:delete 扫描完租户表(即使强一致,那也只是时间点)之后、
    # deleting→deleted 之前,这条 put 才落库 → 该版本被删,但租户已固定它 → restart 拉不回
    # (no-data-loss)。故 canary 走 TransactWriteItems:租户 Put(attribute_not_exists(id))
    # + 快照 status==active 的 ConditionCheck 同一事务;delete 先把 status 条件写成 deleting
    # 就会让这条事务失败,反之这条先成事务就让 delete 的引用扫描看见租户。传【原生值】
    # (不预序列化;resource client 已挂序列化 transform,预序列化会二次包裹 → Type mismatch)。
    _persist_err = _persist_tenant_record(item, tenant_id)
    if _persist_err is not None:
        scheduling._release_slot(
            host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
        )
        return _persist_err

    # #187 P1 — pre-mint gateway token when feature is on (CMK + secrets table
    # both deployed). Enables control-plane reveal (GET /tenants/{id}/token) and
    # SSM-position-12 injection to launch-vm.sh (host decrypts + writes
    # openclaw.json .gateway.auth.token). Feature OFF → gateway_token_ct stays
    # None and launch-vm keeps `openssl rand`-ing its own gateway token in-VM
    # (existing pre-#187 behavior, byte-identical). Placed AFTER the successful
    # tenant put (never mint for a 409-conflict path), BEFORE _launch_vm (so the
    # ciphertext can be threaded to launch-vm position 12). Failure to mint after
    # the feature was enabled follows the launch-vm rollback path: mark tenant
    # deleted + release the slot + best-effort clean up the secrets row (may not
    # exist), then 502 so caller retries.
    gateway_token_ct = None
    if clients.CLAWPOOL_CMK_ARN and clients.tenant_secrets_table is not None:
        try:
            gateway_token_ct = mint_gateway_token(tenant_id)
        except Exception as e:  # noqa: BLE001 — rollback path, then propagate as 502
            print(f"[#187] mint_gateway_token failed: {type(e).__name__}: {e}")
            _cleanup_gateway_token_secret(tenant_id)  # in case partial write landed
            scheduling._release_slot(
                host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
            )
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=_ROLLBACK_DELETED_EXPR,
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "deleted", ":t": utils._now()},
            )
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                502,
                {
                    "error": "gateway token mint failed; tenant rolled back, retry",
                    "id": tenant_id,
                },
            )

    # #10 — WSS 设备身份三件套(可选增强,不阻塞租户创建)。owner_id 已知(非 sentinel)
    # 且 CMK 开时铸:公钥 + deviceId 供 launch-vm 冷注入 paired.json,私钥密文存 DDB
    # 供 GET fold-in。铸失败不回滚租户(gateway token 已够本期用,device 是丝滑授权
    # 增强)——best-effort 告警,租户照常创建。
    device_paired_b64 = ""
    if (
        owner_id
        and clients.CLAWPOOL_CMK_ARN
        and clients.tenant_secrets_table is not None
    ):
        try:
            # 铸设备三件套写进 tenant_secrets 供 GET fold-in;返回的公钥+deviceId 再
            # 组装成 paired.json base64 传给 launch-vm 冷注入(#188 免人工 approve)。
            _device = mint_device_identity(tenant_id, owner_id)
            device_paired_b64 = build_paired_json_b64(_device)
            # #312 — 同步/pinned 创建路径(canary 灰度 / preferred_host_id / clone)与
            # dispatch 路径(:1125)同样必须把 device_paired_b64 长期存 tenants 表(无 TTL)。
            # 否则这类租户某次 4 参 restart 若盘上 paired.json 恰空,三级回落全空
            # (位置参空 → tenant_secrets 无此组装字段 → tenants 表也没有)→ NOT_PAIRED
            # 无源重建。device_paired_b64 无私钥,长期留存不泄密。
            # #314 — 带重试 + fail-loud 的持久化(sync/pinned 路径,与 dispatch 共用 helper)。
            persist_device_paired_b64(tenant_id, device_paired_b64)
        except Exception as e:  # noqa: BLE001 — 可选增强,失败不回滚租户
            print(
                f"[#10] mint_device_identity failed (non-fatal): {type(e).__name__}: {e}"
            )
            device_paired_b64 = ""  # mint 失败 → 空参,launch-vm fail-open 跳过冷注入

    # Issue #12 — for clones, snapshot source disks before launching the new VM.
    # clone-data.sh: pause src → cp --sparse data.ext4 + overlay.ext4 → resume src.
    if clone_src:
        src_vm_num = int(clone_src.get("vm_num", 1))
        clone_cmd = (
            f"/home/ubuntu/clone-data.sh {clone_from} {src_vm_num} {tenant_id} {vm_num}"
        )
        if not ssm_dispatch._ssm_run(host["instance_id"], clone_cmd, timeout=180):
            # Roll back: undo the host counter increment + delete tenant row
            scheduling._release_slot(
                host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
            )
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=_ROLLBACK_DELETED_EXPR,
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "deleted", ":t": utils._now()},
            )
            # #187 — same reasoning as the launch-vm rollback: a token never
            # injected must not remain revealable for its 15-min window.
            _cleanup_gateway_token_secret(tenant_id)
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                502, {"error": "clone-data.sh failed; tenant rolled back"}
            )

    launch_cmd_id = ssm_dispatch._launch_vm(
        host["instance_id"],
        tenant_id,
        vm_num,
        vcpu,
        mem_mb,
        guest_ip,
        host_port,
        config_template,
        restore_backup_key,
        scoped_skills=skills._resolve_effective_skills(item),
        litellm_vkey=tenant_vkey or "",  # task #15 per-tenant billing key
        channel_secret=channel_secret,  # mint-up-front (kills hub handshake race)
        chat_endpoint_enabled=chat_endpoint_enabled,  # per-tenant chatCompletions
        gateway_token_ct=gateway_token_ct,  # #187 P1 — SPEC/11-ENGINE-TRANSFORM D
        device_paired_b64=device_paired_b64,  # #188 — paired.json 冷注入(免 approve)
    )
    # loop 2026-07-01 bugfix: launch-vm's SSM SendCommand can be throttled when
    # many creates fan out concurrently (create_via_queue consumer × 10). It was
    # fire-and-forget, so a throttled launch left the tenant stuck in `creating`
    # forever with a leaked capacity slot (host账本 used_vcpu > 实际 fc). Now: if
    # submission failed, roll the reservation + tenant back and return 502 so the
    # caller retries — and the SQS consumer re-queues the create with backoff
    # (see _consume_lifecycle_sqs: code>=500 → batchItemFailures → SQS redrive).
    if launch_cmd_id is None:
        scheduling._release_slot(
            host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
        )
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=_ROLLBACK_DELETED_EXPR,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "deleted", ":t": utils._now()},
        )
        # #187 — a token that never made it into the VM is a token that will
        # never be redeemed; release the secrets row so its 15-min reveal window
        # isn't handed to a caller whose tenant is being rolled back.
        _cleanup_gateway_token_secret(tenant_id)
        inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
        return utils._resp(
            502,
            {
                "error": "launch-vm SSM dispatch failed (throttled?); "
                "tenant rolled back, retry",
                "id": tenant_id,
            },
        )

    # #187 转型:per-tenant ALB rule/TG 已下线,数据面走 ALB LOR → OpenResty edge
    # → Redis 查表 → host DNAT → microVM:18789。

    audit._publish_event(
        "tenant.created",
        tenant_id,
        {
            "name": name,
            "vcpu": vcpu,
            "mem_mb": mem_mb,
            "host_id": host["instance_id"],
            "guest_ip": guest_ip,
        },
    )

    # 在途窗口关闭:租户已落库(creating)+ launch 已下发,"创建在途"结束,释放占位锁。
    # (设计=in-flight 去重,见 inflight_dedup docstring "终态时删除占位";不释放会让同
    # owner+tenant_user_id 第二次合法创建被 409 挡到 30min TTL。)
    inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)

    # #562 形态第 3 条 —— 第三条出口(同步路径)也统一 202。
    # 这条路径已经下发了 launch,但租户 status 仍是 `creating` 而不是 `running`:VM 起没起来、
    # gateway 通没通,都要等 host 侧完成。201「已创建」对它同样是过度承诺 —— 客户端据 201
    # 直接去连数据面会撞上还没起来的 gateway。202「已受理 + 给你死线」才是实情。
    # 与另两条出口口径一致后,客户端只需要一条规则:合法请求恒 202,拿死线轮询终态。
    # host_id/guest_ip/host_port 仍然给 —— 它们此刻已经确定,不给等于让客户端多查一次。
    return utils._resp(
        202,
        {
            "id": tenant_id,
            "host_id": host["instance_id"],
            "guest_ip": guest_ip,
            "host_port": host_port,
            "status": "creating",
            create_deadline.ATTR_DEADLINE: _create_deadline_epoch,
        },
    )


def _force_backup_sync(tenant_id, ssm_budget_sec=None):
    """同步强制备份租户数据盘到 S3,fail-closed。返回 (ok: bool, err_msg: str|None)。

    #565 G1 —— `ssm_budget_sec` 是**本次备份在 backup 侧允许等多久的墙钟预算**,由调用方
    按自己所在的死线档给定,**不是本函数的常量**:同一次备份在 delete(600s 档)那里有 90s,
    在 rebuild(180s 档)那里只有 55s —— 因为 rebuild 备份完还有一整个 relaunch 要做,而死线
    是同一个 180s。缺省(None)则由 backup 侧回落它自己的默认 300s。

    在任何可能毁掉 data.ext4 的不可逆操作【之前】调用:delete(rm -rf 数据盘)、
    rebuild 降级(切回可能读不了新 schema 的旧 rootfs)。ok=False 时调用方必须
    中止破坏性操作(铁律#4 no-data-loss),各自决定回滚动作(delete 回滚 status、
    rebuild 拒绝换版),故本函数只做"备份+双层校验",不含任何 abort 副作用。

    双层校验缺一不可:
      · invoke 层——StatusCode==200 且无 FunctionError(排除 Lambda 平台错);
      · 业务层——Payload.success is True(backup 对失败场景正常 return
        {"success":False} 不抛异常,RequestResponse 的 StatusCode 仍 200,只看它
        会误判成功、越过 fail-closed 继续 rm -rf 而 S3 无备份 = CRITICAL 数据丢失)。
    pre_delete=True 让 backup 绕过"只备 running"守卫——调用点 status 常已翻成
    deleting/其它非 running 态(先于本调用),不带这个信号 backup 会 no-op 拒掉。
    """
    try:
        # #565 G1-a —— 必须显式给 Config。裸 client 吃 botocore 默认 read_timeout=60,
        # 而 backup 侧真实上界 ~305s → 超过 60s 的备份会在 backup 侧仍在预算内时被这里
        # 掐掉,而它继续跑完并写 S3 → 下面的 except 按 fail-closed 判失败 → 上层看到失败、
        # 底层其实备成功了。取值与"不重试"的理由见 core/clients.BACKUP_SYNC_INVOKE_CONFIG。
        lambda_client = boto3.client(
            "lambda", config=clients.BACKUP_SYNC_INVOKE_CONFIG
        )
        resp = lambda_client.invoke(
            FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
            InvocationType="RequestResponse",  # SYNC: data safe in S3 before rm
            Payload=json.dumps(
                {
                    "tenant_id": tenant_id,
                    "pre_delete": True,
                    # #565 G1 —— 预算随事件过去(backup Lambda import 不到口径模块)。
                    "ssm_budget_sec": ssm_budget_sec,
                }
            ).encode("utf-8"),
        )
        invoke_ok = resp.get("StatusCode", 500) == 200 and "FunctionError" not in resp
        if not invoke_ok:
            return False, "backup invoke failed (StatusCode/FunctionError)"
        try:
            raw = resp["Payload"].read()
            result = json.loads(raw) if raw else {}
        except Exception as pe:  # noqa: BLE001 — 解析失败即 fail-closed
            return False, f"backup response parse error: {pe}"
        if result.get("success") is True:
            return True, None
        return False, (result.get("error") or "backup reported failure")
    except Exception as e:  # noqa: BLE001 — invoke 异常即 fail-closed
        return False, f"backup error ({e})"


def delete_tenant(tenant_id, query_params, event=None):
    """Delete wrapper that releases only the lifecycle lease it acquired."""
    ctx = {}
    try:
        response = _delete_tenant_inner(tenant_id, query_params, event, ctx)
        code = int((response or {}).get("statusCode") or 0)
        if code >= 500 and not ctx.get("release_lifecycle_fence_on_error"):
            ctx["hold_lifecycle_fence"] = True
        return response
    except Exception:
        if not ctx.get("release_lifecycle_fence_on_error"):
            ctx["hold_lifecycle_fence"] = True
        raise
    finally:
        _release_lifecycle_ctx(tenant_id, ctx)


def _delete_tenant_inner(tenant_id, query_params, event=None, _lifecycle_ctx=None):
    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return utils._resp(404, {"error": "tenant not found"})
    denied = auth._assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied
    if item.get("status") == "deleted":
        return utils._resp(200, {"id": tenant_id, "status": "deleted"})

    # #422 — suspended 租户的 VM 已停、本地盘已删、host slot 已在 suspend 时释放。若走下方
    # 常规 delete:①CAS suspended→deleting 后会【二次】扣 slot(used_vcpu -= vcpu),而该 slot
    # 早已释放 → 账本扣穿/低估 → 过度调度;②stop-vm/rm 对已不存在的 VM 是无效副作用。故走
    # 【确认回收】专路。落实 ADR "suspended 删除必经确认回收"。
    # codex round2 #2:只接【稳定 suspended】;中间态 suspending/restoring 是在途操作(与并发
    # suspend/restore launch 竞争),此刻删除会与那些操作抢状态、可能留孤儿 VM(delete 先翻
    # deleted、restore 又起 VM)→ 返 409 让调用方等在途操作收敛(达稳定 suspended 或回 running)。
    # #469 P2 —— 卡死租户的强制出口。
    #
    # 上面这条 409 本身是对的(在途 suspend/restore 与 delete 抢状态会留孤儿 VM),但它与
    # P1 组合出一个死局:一个因 Lambda 被杀而永久卡在 suspending/restoring 的租户,
    # 【连删除都做不到】—— issue 原话「连删除这条兜底出口都是关着的」,只能人工改 DDB。
    #
    # 放行条件是【两个】,缺一不可:
    #   ① 调用方显式带 ?force=true —— 表明这是有意识的强制操作,不是普通删除误入;
    #   ② 该租户已被 health_check 的中间态巡检标记 lifecycle_stuck_at —— 即
    #      **由 reaper 按 host 侧事实判定过它真的卡死了**,不是调用方自称卡死。
    # 只有 ② 而没有 ① → 仍走正常等待(卡死不等于必须马上删);只有 ① 而没有 ② → 拒绝,
    # 否则这个开关就成了"随时打断一个正在合法执行的 suspend"的后门(那会造出孤儿 VM,
    # 正是上面 409 要防的)。
    # ③ 只放行 `suspending`(codex 独立复审第四轮)。`restoring` 【不】放行:
    #    restore 的破坏性动作是「在新 host 上预留 slot + 起 VM」(:3508 `_reserve_slot_on`),
    #    而在 restore 定型之前,租户行里的 host_id / vm_num 仍是【旧的】。此时走普通
    #    delete 会:
    #      · 按旧行去扣【旧 host】的账本 —— 而那个 slot 在 suspend 时已经释放过 → 扣穿,
    #        而 `_release_slot` 只有下溢守卫、没有「这个租户扣过没」的互斥锚(:123),
    #        host 上还有别的租户占容量时那一扣会成功并吃掉别人的额度;
    #      · 按旧 host_id/vm_num 下发 stop/rm → 停错 VM 或对已不存在的 VM 做无效副作用;
    #      · 新 host 上那份预留【没人释放】→ 泄漏,且该物理槽位可能被下一个租户复用
    #        (跨租户槽位复用,no-cross-tenant 方向)。
    #    要安全放行 restoring,前置是「目标 host/slot 与一个幂等的 reservation 令牌已落库」
    #    (#412 的 capacity_reservation_id 就是那种锚),属独立子项。在那之前拒绝,
    #    并在错误里给出明确出路 —— 而不是提供一条会腐蚀容量账本的假出口。
    _force = query_params.get("force", "false").lower() == "true"
    _stuck_at = item.get("lifecycle_stuck_at")
    _forcible = item.get("status") == "suspending"
    if item.get("status") in ("suspending", "restoring") and not (
        _force and _stuck_at and _forcible
    ):
        if not _forcible:
            _hint = (
                "restoring cannot be force-deleted: the row still points at the OLD "
                "host/vm while restore may already hold a reservation on the NEW host, "
                "so a normal delete would double-decrement the old host's ledger, stop "
                "the wrong VM and leak the new reservation. Wait for it to settle "
                "(running or suspended), then delete"
            )
        elif not _stuck_at:
            _hint = "wait for it to settle (suspended or running) before delete"
        else:
            _hint = "tenant is marked stuck; retry with ?force=true to delete it"
        return utils._resp(
            409,
            {
                "error": f"tenant is {item['status']} (hibernate/restore in flight); {_hint}",
                "id": tenant_id,
                **({"lifecycle_stuck_at": _stuck_at} if _stuck_at else {}),
            },
        )

    lifecycle_op_id = (event or {}).get("_op_id") or secrets.token_hex(16)
    event = dict(event or {})
    event["_op_id"] = lifecycle_op_id
    lifecycle_epoch, fence_reason = lifecycle_fence.acquire(
        tenant_id, lifecycle_op_id, "delete"
    )
    if lifecycle_epoch is None:
        return utils._resp(
            409,
            {
                "error": fence_reason,
                "code": "LIFECYCLE_IN_FLIGHT",
                "id": tenant_id,
                "op_id": lifecycle_op_id,
            },
        )
    if _lifecycle_ctx is not None:
        _lifecycle_ctx.update(
            {
                "lifecycle_op_id": lifecycle_op_id,
                "lifecycle_fence_epoch": lifecycle_epoch,
                "hold_lifecycle_fence": False,
            }
        )
    delete_host_guard = lifecycle_fence.host_guard(
        tenant_id, lifecycle_op_id, lifecycle_epoch
    )
    delete_condition, delete_condition_values = lifecycle_fence.condition(
        lifecycle_op_id, lifecycle_epoch
    )

    # #469 P2 —— 走"不扣账本的一步 CAS 到 deleted"这条专路的两类租户:
    #   · 稳定 suspended:slot 已在 suspend 时释放(既有语义,见下方注释);
    #   · 强制删除一个卡在 suspending 且【盘已被回收】的租户:reaper 探到
    #     lifecycle_stuck_vm_dir=False,说明 suspend 已跑过 rm -rf(tenant_service:3134),
    #     而账本扣减就在其后一行(:3149 _release_slot)—— 于是账本【可能已扣】。
    #     scheduling._release_slot(scheduling.py:123)只有下溢守卫、没有"这个租户扣过没"
    #     的互斥锚,所以再走普通 delete 扣一次,若 host 上还有别的租户占着容量,那一扣会
    #     【成功】并吃掉别人的额度 —— 正是下面 codex round4 #5 说的"扣穿其他租户容量"。
    #     故这类必须走同一条不扣账本的路。代价:若账本实际还没扣,这个 slot 会泄漏;
    #     而扣穿别人的容量不可接受,两害取轻,与既有取舍一致。
    #
    #     ⚠ 这里原先写的是"需靠对账回收(可恢复)"。codex 独立复审第五轮质疑了这句,
    #     实查后【它是一句没有兑现的承诺】,现更正:全仓**没有**容量账本对账器 ——
    #     20 处写 used_vcpu/used_mem_mb/vm_count 全是"增量预留"或"单点定额扣减",
    #     没有一处做重算/比对/写回;5 条 EventBridge 定时 Lambda 逐个排除;
    #     tenant_stats 已按 host 汇总出了所需的那个数(handler.py 的 per_host_counts),
    #     但它连 hosts 表句柄都没有,算完只写快照、从不比对。本文件 :234 与 :3203 两处
    #     既有注释也早已写明同一件事:"deleted 租户不被 reaper 兜底 → 容量永漏"。
    #
    #     值得记下的不对称:health_check 的 reap_orphan_phys_slots 【是】一个真对账器
    #     (扫租户表建在役集合 → 扫 host 行比对 → 无主就写回),而且它明确把 deleted
    #     租户当不在役、回收其 ps_<n>。也就是说同一个 host 行上,一个已 deleted 租户的
    #     **物理槽位会被对账回收,而它的 vcpu/mem/vm_count 不会** —— (B)类对账的模式
    #     在本仓库存在,只是没有铺到容量三元组上。
    #
    #     为什么不在本次补上:通用补扣的前置正是 _release_slot 的幂等锚(#412 的
    #     capacity_reservation_id 那种一次性令牌)。没有它,任何"发现少扣就补一刀"的
    #     对账器本身就会变成第二个扣穿源。这属独立高危子项,已记入 UNRESOLVED_GAPS。
    #     本次只把承诺改成事实:这个泄漏目前**要人工重算**,没有自动兜底。
    #
    #     为什么仍然放行删除(而不是像 codex 建议的"账本状态未知就不许报删除成功"):
    #     拒绝会让租户行和那个并发槽【一起】卡住,而这正是 #469 P2/P4 要消灭的东西
    #     (P4 原话:卡死租户占着永不释放的槽,数量只增不减)。可人工重算的容量泄漏
    #     比"连删都删不掉"轻,方向与本 issue 一致。
    #
    # 反过来,卡在 suspending 但【盘还在】(vm_dir=True)、或卡在 restoring 的租户,账本
    # 状态是明确的(前者未扣、后者 restore 已在新 host 预留),走下面的普通 delete 路径
    # 才正确 —— 它会停 VM、删盘、扣账本,一步不少。
    #
    # ⑦ codex 独立复审第六轮 —— 路径选择改用【当场复探】,不再信 reaper 留下的快照。
    # reaper 对已标记的租户不再复探(health_check :1127,为了不烧光每轮探测预算),所以
    # lifecycle_stuck_vm_dir 可能是几小时前的事实。两个方向都会踩账本红线,详见
    # _probe_stuck_tenant_facts 的 docstring。分工改为:reaper 的标记是**准入凭据**
    # (证明它确实卡住、不是正在正常执行),**走哪条路由当场复探决定**。
    # 探不到就拒绝(502),不猜 —— 与 reaper 的"确认不了就什么都不做"同一条原则。
    _stuck_disk_gone = False
    _fresh_fc_alive = None
    if item.get("status") == "suspending" and item.get("lifecycle_stuck_at"):
        _pok, _pf = _probe_stuck_tenant_facts(item.get("host_id"), tenant_id)
        if not _pok:
            _mark_fail_reason(tenant_id, "delete", create_deadline.REASON_HOST_UNREACHABLE)
            # ㉓ 只读失败必须【放掉围栏】(codex 独立复审第十九轮)。
            #
            # delete_tenant 的 wrapper(:2519)见 code>=500 就 hold_lifecycle_fence=True。
            # 而这条返回是 502,于是围栏被扣住整个租期(LIFECYCLE_FENCE_LEASE_SECONDS=1800,
            # 30 分钟)—— 而我的文案写着 "Nothing was changed; retry."。**照文案去 retry
            # 会撞 LIFECYCLE_IN_FLIGHT,半小时内根本进不来。**
            #
            # 这比"文案不准"更糟:这条路正是 P2 给卡死租户留的唯一出口,一次瞬时 SSM 抖动
            # 就把运维锁在门外 30 分钟,而租户本来就已经卡死了。
            #
            # 放掉是安全的,判据是【此刻还没有任何破坏性命令下发】—— 探测是纯只读的
            # (get-item + 一条读 /proc 与 ls 的 SSM)。下面那条 stop-vm 确认失败的 502
            # 刻意【不】放掉:那时命令已经下发且结果未知,放掉会让 restore 与在途 stop 交错。
            if _lifecycle_ctx is not None:
                _lifecycle_ctx["release_lifecycle_fence_on_error"] = True
            return utils._resp(
                502,
                {
                    "error": "cannot force-delete: host-side state could not be probed "
                    "right now (host unreachable / SSM failing). The route depends on "
                    "whether the disk was already reclaimed — the reaper's stored probe "
                    "may be hours stale, and guessing either way corrupts the capacity "
                    "ledger. Nothing was changed; retry.",
                    "id": tenant_id,
                },
            )
        _stuck_disk_gone = _pf["vm_dir"] is False
        _fresh_fc_alive = _pf["fc_alive"]
    # #679(Codex 实现评审第二轮)—— `suspend_failed` 的 delete 必须走这条【无账本、无
    # host 副作用】的专路,不能掉进下面的普通删除路径:它的 host 已被 EC2 权威确认亡了
    # (原语 B 的前置),普通路径的 stop-vm/rm SSM 会对死 host 反复 InvalidInstanceId →
    # 502 循环,而 delete 是无备份档租户的**唯一出口**,出口不能卡死。专路的每一步对
    # suspend_failed 都成立:无 VM/盘(host 亡)→ 不需要副作用序列;账本不扣(令牌已在
    # 原语 B 消费,host 亡时 hosts 行已消失);release_phys_slot 对缺行 CCF 是 best-effort。
    if (
        item.get("status") in ("suspended", "suspend_failed")
        or _stuck_disk_gone
    ):
        # ⑥ codex 独立复审第五轮 —— 盘没了【不等于】VM 停了,而这条路会释放物理槽位。
        #
        # `_stuck_disk_gone` 只看 vm_dir=False,把 lifecycle_stuck_fc_alive 完全忽略了。
        # reaper 的判定矩阵对 `suspending + 盘没了` 这一格,fc_alive 写的是 `*` ——
        # 两种都放行,因为它确实两种都可能:Linux 上 rm -rf 掉目录【不会】杀死持有那些
        # 文件描述符的 Firecracker 进程。于是有一格真实现场是「盘已删 + FC 还活着」。
        #
        # 这条路对那一格是错的,而且错法比"少扫一次"严重:
        #   · 它一路走到底 CAS 成 deleted,全程【不下发任何 host 侧清理】——
        #     依据是"suspended 租户没有 VM/盘的破坏性副作用序列",这对真 suspended
        #     成立,但对这一格不成立:VM 还在跑;
        #   · 然后 `release_phys_slot` 把 ps_<n> 放回去(第三轮补的,本身没错);
        #   · 而 `phys_tap_occupied`(core/scheduling.py:195)是【扫租户表】判占用、
        #     且明确排除 deleted 行 —— 行一翻 deleted,这个号在"发号器 ps_<n>"和
        #     "撞号复检"两套机制里【同时】变空闲;
        #   · 下一个租户于是被排到 n 上,launch-vm.sh 走 `ip link del` + `kill -KILL`
        #     去抢 tap —— 那正是 #208 记着的"跨租户劫持"形态,只是这次先到者是孤儿。
        #   在被复用之前,那个孤儿 guest 还带着自己的 DNAT(host_port)在跑,而租户已被
        #   宣告 deleted:"deleted 却还能从 host 端口连上去"本身就不该发生。
        #
        # 修法:**CAS 之前**同步 stop 并要回执,确认不了就 502、一个字段都不改。
        # 与 health_check:475 `_confirm_vm_stopped`(#412 blocker-B)同一条原则 ——
        # 「释放前先做权威停机确认,确认不了就不释放,时序不替代正确性」。
        #
        # 为什么放在 CAS 【之前】而不是之后:之后就已经宣告 deleted 了,stop 失败时既
        # 收不回那句话,也只能靠"跳过 release_phys_slot"半兜底。之前失败则什么都没动,
        # stop-vm.sh 幂等,重试完全安全。
        # 为什么 CAS 前 stop 不会误杀一个合法活着的 VM:进这条分支要求 status 仍是
        # suspending 且已被 reaper 按 host 事实标记为「盘已删」——这个状态下不存在合法
        # 活 VM。若并发的 suspend 恰好跑完(→suspended),那 VM 本就该停,stop 是 no-op
        # success,随后 CAS 会因 `#s = :cur` 而 CCF 出局返 409。若之后又被 restore 拉起,
        # 它拿的是【新】vm_num(冷恢复重分配,:3610),而这里 stop 的是旧号,打不到它。
        # 用 `is not False` 而不是 `is True`:探不到/未探(理论上到不了这里,因为探不到
        # 已在上面 502 了)一律当"可能活着"处理 —— fail-closed。
        #
        # 第六轮起这里用的是【当场复探】的 _fresh_fc_alive,不再是租户行里 reaper 留下的
        # lifecycle_stuck_fc_alive 快照 —— 后者可能是几小时前的(reaper 对已标记者不复探)。
        if _stuck_disk_gone and _fresh_fc_alive is not False:
            _sg_host = item.get("host_id")
            _sg_vm = item.get("vm_num")
            if not _sg_host or _sg_vm is None:
                # #565 G3(Codex 独立复审第四轮)—— 这条**不是** host_unreachable。
                # 条件是「租户行上缺 host_id / vm_num」= 数据不完整,重试一万次也不会让
                # 缺失的字段出现;下面那句注释自己写着「已经需要人工介入」「运维修好行数据
                # 后想立刻重删」。而 host_unreachable 的契约指引是「值得,隔 1–2 分钟重试」——
                # 自动化调用方照它做就是无限重试一个永远不会自愈的状态。
                # 上面那条(`_probe_stuck_tenant_facts` 探不到)才是真的 host_unreachable:
                # 那是 SSM/探测失败,重试有意义。两者不能共用一个值。
                _mark_fail_reason(tenant_id, "delete", create_deadline.REASON_SYSTEM)
                # 与上面那条探测失败同类:【纯只读,没有任何命令下发】→ 放掉围栏。
                # 否则这个已经需要人工介入的租户会再被自己的围栏锁住 30 分钟,
                # 运维修好行数据后想立刻重删也进不来。
                if _lifecycle_ctx is not None:
                    _lifecycle_ctx["release_lifecycle_fence_on_error"] = True
                return utils._resp(
                    502,
                    {
                        "error": "cannot force-delete: the tenant may still have a live "
                        "VM (disk gone but Firecracker reported alive) and its "
                        "host_id/vm_num is missing, so the VM cannot be stopped. "
                        "Releasing the slot now would let the next tenant land on a "
                        "live VM. Escalate.",
                        "id": tenant_id,
                    },
                )
            # 自愈同 restore 回滚那处(:3745):stop-vm.sh 缺失/过期会静默无效,
            # 而这里的返回值是【判据】,不能让一个跑不动的脚本冒充"已确认停机"。
            #
            # 新鲜度哨兵必须覆盖 orphan 复验和 live guest flush。只认旧
            # OC_STOP_ORPHAN_NO_VMDIR 会让强删使用一份仍可强杀未刷盘 guest 的脚本。
            _sg_heal = ssm_dispatch.host_script_self_heal(
                ("stop-vm.sh",),
                "oc:force-delete",
                freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED"),
            )
            _sg_q_tid = shlex.quote(str(tenant_id))
            _sg_q_vm = shlex.quote(str(int(_sg_vm)))
            # 60s:比 health_check 那条纯确认用的 STOP_CONFIRM_TIMEOUT=20 宽裕(它有
            # 每轮预算要防饿死,这里是单次同步请求),又远小于普通 delete 的 300
            # (那条含 backup)。留余量是因为前置的 self-heal 可能要从 S3 拉脚本。
            if not ssm_dispatch._ssm_run(
                _sg_host,
                f"{_sg_heal} && /home/ubuntu/stop-vm.sh {_sg_q_tid} {_sg_q_vm}",
                timeout=60,
            ):
                _mark_fail_reason(tenant_id, "delete", create_deadline.REASON_HOST_UNREACHABLE)
                return utils._resp(
                    502,
                    {
                        # 文案必须说实话:这条【刻意扣住围栏】(与上面两条只读失败相反)。
                        # stop-vm 已经下发且结果未知,而它没带 host_guard —— 此刻放掉围栏
                        # 会让一次 restore 与在途的 stop 交错。所以立刻重试会撞
                        # LIFECYCLE_IN_FLIGHT,要等租约到期。之前这里写"retry",是在承诺
                        # 一件代码不允许的事(与 ㉒ ㉓ 同一个病)。
                        "error": "cannot force-delete: stop-vm was NOT confirmed, so the "
                        "VM may still be running while its disk is already gone. "
                        "Declaring the tenant deleted would free its physical slot "
                        "(ps_<n>) and the next tenant could be placed on top of the live "
                        "VM. The tenant row was not changed. NOTE: the lifecycle lease is "
                        "deliberately held here (an unconfirmed stop-vm may still land, "
                        "and it carries no fence guard), so a retry returns "
                        "LIFECYCLE_IN_FLIGHT until the lease expires; stop-vm itself is "
                        "idempotent, so retrying after that is safe.",
                        "id": tenant_id,
                    },
                )
        _keep = query_params.get("keep_data", "true").lower() == "true"
        # suspend_failed 行的备份在 suspend_backup_key(原语 B 刻意保留,finalize 没跑到
        # rename 那步);真 suspended 行在 restore_backup_key。两者互斥,or 链取到即用。
        _bk = item.get("restore_backup_key") or item.get("suspend_backup_key") or ""
        # codex round3 #1 + round4 #5 — 【一步 CAS suspended→deleted,不经共享 deleting 中间态】。
        # 抢闸:CAS 抢下唯一赢家,才做删 S3 备份/撤 vkey(否则并发 restore 先赢 suspended→
        # restoring、本删除删掉其唯一备份 = 数据丢失)。**直接翻终态 deleted 而非 deleting**:
        # suspended 租户无 VM/盘的破坏性副作用序列(suspend 时已删),不需要 deleting 保护窗口;
        # 若经 deleting 且中途崩溃,consumer 重投会走【普通 delete 路径】对已释放的 slot 二次扣账
        # (codex round4 #5,扣穿其他租户容量)。一步到 deleted 彻底关掉该窗口:重投读到 deleted
        # 直接幂等返回,永不进普通删除路径。破坏性清理(S3/vkey)放 CAS 之后 best-effort——崩溃则
        # 泄漏可被对账清理(可恢复),而二次扣 slot 破坏账本正确性(不可接受),两害取轻。
        _vkey = item.get("litellm_vkey")
        # 两侧合并:bb 侧的 _STALE_HEALTH_FIELDS(#52 —— 终态不留陈旧健康字段)
        # + 本批次的 lifecycle_stuck_*(#469 P2 —— 卡死标记随终态一并清掉,不留残留属性)。
        _remove = (
            "restore_backup_key, suspend_backup_key, suspended_at, suspended_from, "
            f"cognito_channel_password, {_STALE_ON_DELETE_FIELDS}"
            ", lifecycle_stuck_at, lifecycle_stuck_reason, lifecycle_stuck_vm_dir"
            ", lifecycle_stuck_fc_alive, updated_at_stuck_seen, lifecycle_prev_status"
            ", lifecycle_rollback_deferred_until, lifecycle_probe_attempted_at"
        )
        _set = "SET #s = :d, updated_at = :t"
        # CAS 的 :cur 必须是【本次读到的那个状态】,不能写死 "suspended" —— 强制删除走
        # 这条路时当前状态是 suspending,写死会让条件永远 CCF、force 出口形同不存在。
        _cur_status = item.get("status")
        _vals = {":d": "deleted", ":cur": _cur_status, ":t": utils._now()}
        # vkey 撤销在 CAS 前做(幂等,失败则保留字段+flag);其余破坏性清理在 CAS 赢后做。
        _vkey_revoked = vkey._revoke_tenant_vkey(_vkey) if _vkey else True
        if _vkey and not _vkey_revoked:
            _set += ", vkey_revoke_failed = :vf"
            _vals[":vf"] = True
        else:
            _remove = "litellm_vkey, " + _remove
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=f"{_set} REMOVE {_remove}",
                ConditionExpression=f"#s = :cur AND {delete_condition}",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    **_vals,
                    **delete_condition_values,
                },
            )
        except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
            cur2 = (clients.tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get("Item") or {})
            if cur2.get("status") == "deleted":
                return utils._resp(200, {"id": tenant_id, "status": "deleted"})
            _mark_fail_reason(tenant_id, "delete", create_deadline.REASON_PREEMPTED)
            return utils._resp(
                409,
                {
                    "error": f"tenant is now {cur2.get('status')}; "
                    f"{_cur_status}-delete lost the race",
                    "id": tenant_id,
                },
            )
        # CAS 赢(已 deleted 终态),破坏性清理 best-effort(崩溃泄漏可对账,不影响 slot 账本)。
        #
        # #469 P2(codex 独立复审第三轮)—— 释放【物理槽位】ps_<n>。
        # 上面那段注释论证的是【容量账本】不能在这条路上扣(_release_slot 不幂等,
        # 二次扣会吃掉别人的额度),那个结论不变。但物理槽位是**另一套机制**,
        # 复审指出的这一半是真缺口:这条路此前完全没释放它,租户行一旦翻 deleted、
        # 恢复元数据被 REMOVE,ps_<n> 就永久挂在一个已不存在的 owner 上 —— 该号再也
        # 分不出去,而 reap_orphan_phys_slots 靠"owner 已不在役"回收,已 deleted 的行
        # 仍在表里、它判不出孤儿。
        #
        # 为什么这里可以放心释放,而容量账本不行:release_phys_slot 带
        # `#ps = :tid` 的 owner 条件(core/scheduling.py:239)—— **它是幂等的**,
        # 号已被别人接手就返 False 不动。这与 _release_slot 只有下溢守卫、没有
        # "本租户扣过没"互斥锚的情况完全不同。
        # 判据同 :2935 的普通 delete 路径:phys_vm_num 优先,回落 vm_num
        # (存量租户没有 phys_vm_num;迁移后 phys_vm_num 恒等原始 launch 号)。
        _host_id = item.get("host_id")
        _phys = item.get("phys_vm_num", item.get("vm_num"))
        if _host_id and _phys is not None:
            try:
                scheduling.release_phys_slot(_host_id, _phys, tenant_id)
            except Exception as e:  # noqa: BLE001 — best-effort,对账会重试孤儿
                print(f"force-delete {tenant_id}: release_phys_slot failed: {e}")
        if not _keep and _bk:
            try:
                _bucket = os.environ.get("BACKUP_BUCKET") or os.environ.get("ASSETS_BUCKET", "")
                if _bucket:
                    clients.s3.delete_object(Bucket=_bucket, Key=_bk)
            except Exception as e:  # noqa: BLE001 — best-effort,不阻断终态
                print(f"delete suspended #{tenant_id}: S3 backup rm best-effort failed: {e}")
        _cleanup_gateway_token_secret(tenant_id)
        inflight_dedup.release_inflight_lock(item.get("owner_id"), item.get("tenant_user_id"))
        name_dedup.release_name_lock(
            name_dedup.dedup_scope(
                item.get("owner_id", ""), item.get("tenant_user_id", ""), item.get("platform_id", "")
            ),
            item.get("name", ""),
            tenant_id,
        )
        # ⑳ 把"这次删除没动容量账本"记进审计事件(codex 第五轮提出、第十三轮重提)。
        #
        # 两轮的建议都是"拒绝这条快路径直到有幂等令牌",两轮都【维持驳回】——理由见上方
        # :2664:拒绝会让租户行和那个并发槽一起卡住,那正是 #469 P4 要消灭的东西。
        # 但驳回一个建议不等于它指出的后果不存在:强制删除一个盘已删的卡死租户时,
        # 账本【是否已扣】确实不可知,不扣就可能泄漏一份 vcpu/mem/vm_count,而全仓没有
        # 对账器会发现它(第五轮实查确认)。
        #
        # 所以至少把它变【可见】:审计事件带上 ledger_release_skipped,并写清为什么。
        # 于是"哪些租户可能留下了未回收的容量"是一次审计表查询,而不是靠人回忆;
        # 也可以据此对账/告警。这比"注释里写清楚"前进一步 —— 注释只有读代码的人看得到。
        _ledger_unknown = bool(_stuck_disk_gone)
        audit._publish_event(
            "tenant.deleted", tenant_id,
            {
                "from_suspended": True,
                # #679 —— 这条专路现在也收 suspend_failed(host 亡档),from_suspended
                # 保留是为了老消费者;真实来源状态用它区分(审计不谎报)。
                "from_status": _cur_status,
                "kept_backup": _keep,
                "vkey_revoked": _vkey_revoked,
                # True 仅出现在"强制删除一个盘已被回收的卡死租户"这一格。稳定 suspended
                # 的正常删除不带它(那种情形 slot 在 suspend 时已确定释放过)。
                "ledger_release_skipped": _ledger_unknown,
                **(
                    {
                        "ledger_note": (
                            "capacity was NOT decremented: suspend had already passed "
                            "rm -rf, so whether _release_slot ran is unknowable and "
                            "double-decrementing would eat another tenant's quota "
                            "(_release_slot has no per-tenant idempotency anchor). "
                            "This may leak one tenant's vcpu/mem/vm_count on this host; "
                            "there is no reconciler — recompute manually. See #469 "
                            "UNRESOLVED_GAPS."
                        ),
                        "host_id": item.get("host_id", ""),
                        "vcpu": item.get("vcpu", 0),
                        "mem_mb": item.get("mem_mb", 0),
                    }
                    if _ledger_unknown
                    else {}
                ),
            },
        )
        return utils._resp(200, {"id": tenant_id, "status": "deleted"})

    # #263 — delete 削峰:队列开启且非 consumer 重放时,入队即返 202,由 consumer
    # 受控并发消费(避免短时间批删把单 host SSM CommandWorkersLimit=5 打爆,饿死
    # launch-vm/start/stop;单删同步 backup+4~5 条阻塞 SSM ~15~35s 还会撞 API GW 29s)。
    # keep_data/skip_backup 放进 extra 让 consumer 透传(否则恒软删,盘悄悄没删)。
    # 与 tenant_action 的入队守卫同款:队列没配 → enqueue 返 False,回退下方同步路径
    # (向后兼容);_consumer_ident 存在说明本次已是 consumer 重放,不再二次入队(防
    # 无限入队)。字段 {id,status} 与同步路径一致,status 值 "queued" 与 tenant_action 对齐。
    if (
        clients.LIFECYCLE_QUEUE_URL
        and "_consumer_ident" not in (event or {})
    ):
        _extra = {
            "keep_data": query_params.get("keep_data"),
            "skip_backup": query_params.get("skip_backup"),
            # ⑳ force 也必须透传(codex 独立复审第十八轮)。
            #
            # 上面 :2576 那道准入检查在【入队之前】跑,所以带 ?force=true 的首次调用能过、
            # 入队返 202;而 consumer 重放时若拿不到 force,同一道检查就把它挡成 409。
            # 409 是 4xx → consumer 明确【不重投】(见 handler.py 那句"4xx 不重试") →
            # 消息被消费掉,删除永远不发生。
            # 于是 P2 承诺的"卡死租户可强制删除"在 lifecycle 队列开启时【整条失效】,
            # 而调用方看到的是 202 —— 正是本 issue 零节 S1/S2 那句"接口返回成功、
            # 实际操作没有发生,且调用方无从察觉"。
            # (盘已删那半走 :2920 的同步 200 快路径,不经队列,所以不受影响;
            #  受影响的是盘还在、需要走普通 delete 的那半。)
            "force": query_params.get("force"),
        }
        # #564 G2 —— 受理时刻算 delete 的绝对死线,同一个值进消息体与租户行。
        # 上面 :3105 的 `_consumer_ident not in event` 保证本块只在 API 请求路径跑,
        # 所以这就是受理时刻;放到 consumer 里算会让死线随每次重投往后挪。
        _accepted_epoch = int(time.time())
        _dl_epoch, _dl_src = deadline_config.deadline_epoch_for(
            create_deadline.ACTION_DELETE, _accepted_epoch
        )
        if _dl_src != "ssm":
            print(
                f"delete_deadline: {_dl_epoch - _accepted_epoch}s "
                f"from {_dl_src} for {tenant_id}"
            )
        # 顺序:先落行、再发消息。行上没有 `delete_deadline`,`deadline_executor` 就扫不到
        # 这一行,这次删除永远不会被判死。
        try:
            _write_action_deadline(tenant_id, create_deadline.ACTION_DELETE, _dl_epoch)
        except Exception as exc:  # noqa: BLE001
            # **发送前的失败,不能走下面那条 `ENQUEUE_STATE_UNKNOWN`**(Codex 独立复审第 1 轮)。
            # 那条刻意扣住租约,因为 SQS 可能已经收下消息、盲目重试会起第二次删除;而这里
            # 消息一条都没发。塞进那条路的后果是答复叫客户重试、而重试全撞 409,直到 1800s
            # 租约过期。照仓内既有惯例用 `release_lifecycle_fence_on_error`(见 :2694-2699
            # 的 wrapper:5xx 默认扣住,除非设了这个键)让包装器把租约放掉。
            if _lifecycle_ctx is not None:
                _lifecycle_ctx["release_lifecycle_fence_on_error"] = True
            print(f"delete deadline write failed for {tenant_id}: {exc}")
            return utils._resp(
                503,
                {
                    "error": "could not record the delete deadline before dispatch; "
                    "nothing was started - safe to retry",
                    "code": "ENQUEUE_ANCHOR_FAILED",
                    "id": tenant_id,
                    "op_id": lifecycle_op_id,
                },
            )
        try:
            enqueued_op_id = lifecycle_dispatch.enqueue_lifecycle(
                "delete",
                tenant_id,
                event,
                extra=_extra,
                operation_id=lifecycle_op_id,
                deadline_epoch=_dl_epoch,
            )
        except Exception as exc:  # noqa: BLE001
            # SQS may have accepted the message before the acknowledgement was
            # lost. Retain the lease so a blind retry cannot start a new delete.
            if _lifecycle_ctx is not None:
                _lifecycle_ctx["hold_lifecycle_fence"] = True
            print(f"delete enqueue UNKNOWN state for {tenant_id}: {exc}")
            return utils._resp(
                503,
                {
                    "error": "the queue may have accepted this delete; poll the "
                    "tenant before retrying",
                    "code": "ENQUEUE_STATE_UNKNOWN",
                    "id": tenant_id,
                    "op_id": lifecycle_op_id,
                },
            )
        if enqueued_op_id:
            if _lifecycle_ctx is not None:
                _lifecycle_ctx["hold_lifecycle_fence"] = True
            return utils._resp(
                202,
                # #565 G6 —— 带上刚算定的绝对死线(`:3442` 算、`:3455` 落行、`:3480` 进
                # 消息体,三处同一个值)。delete 恒在七档里,所以这条无条件带。
                _with_action_deadline(
                    {
                        "id": tenant_id,
                        "status": "queued",
                        "op_id": enqueued_op_id,
                    },
                    create_deadline.ACTION_DELETE,
                    deadline_epoch=_dl_epoch,
                ),
            )

    # #107 — 并发双删扣穿 host 账本修复(与 create 的原子 CAS 对称)。
    # delete 的 host 计数回退(used_vcpu/vm_count -= …,下方 line ~2210)无
    # ConditionExpression,两个并发 DELETE 同一 tenant 会各扣一次 → 账本被扣穿变负,
    # 调度容量判断失真。这里用一次 CAS 把 status pending/running/… → "deleting" 当
    # 并发闸:ConditionExpression 保证**只有一个调用赢得这次翻转**,赢家继续执行
    # 全部副作用(SSM stop / rm / ALB / 计数回退)且账本只被扣一次;输家(CCF)立即
    # 幂等返回 200,不碰计数。用中间态 "deleting" 而非直接 "deleted",是为了:① 副作用
    # 期间 status 已非 running,list/调度不再把它算作活跃;② 万一副作用中途失败,记录
    # 停在 "deleting" 可被巡检发现重试,而不是过早标 "deleted" 把半清理的租户藏起来。
    prev_status = item.get("status")
    claim_expires_at = int(time.time()) + _DELETE_CLAIM_TTL_SECONDS
    # #532 —— 把【本次删除的意图】与 deleting 一起原子落库。
    #
    # 为什么必须落:`keep_data`/`skip_backup`/`force` 只活在同步请求的 query,或入队时放进
    # 消息 `extra` 的那份拷贝(:3121)。消息一旦进 DLQ,这三个值就**只存在于那条 DLQ 消息里** ——
    # 靠扫 DDB 触发的收敛者(#532 的 delete_reconciler)读不到,只能落回默认值,于是一次
    # `?keep_data=false` 的**硬删会静默变成软删**:该销毁的数据盘留下来,租户却照常进 deleted
    # → 磁盘泄漏。方向确实是"留盘"这一侧(可事后补删,不是丢数据),但它是一次与调用方请求
    # 不一致的**静默降级**,必须消掉,而不是靠"反正方向安全"带过。
    #
    # 存成一个 **map** 而不是三个字段:消费侧 handler.py:1905 只把【调用方真传过的】key 放进
    # 重建的 query,让 delete_tenant 的默认值对缺失键生效 ⇒ 「没传」与「传了 false」是两种
    # 不同语义。map 把 `extra` 的形状原样存下来,收敛者取出即可回喂,**零二次解析**;三个独立
    # 字段则要靠 attribute_not_exists 逐个区分「缺失」与「false」,多三处可错点。
    #
    # 与 CAS 写在**同一条 update** ⇒ 零额外写,且不存在"翻了 deleting 但意图还没落"的窗口。
    # 形态抄同路径现成的 predelete_backup_at(:3470);#241 的
    # ADR-delete-fanout-manifest-contract §5 已把"删除意图落库"这条升级路径预先论证过。
    #
    # 只在**首次**翻 deleting 时写(本 CAS 条件含 `#s <> :deleting`)⇒ 重投/收敛者的
    # 二次进入不会覆盖原始意图。
    _delete_intent = {
        k: str((query_params or {})[k])
        for k in ("keep_data", "skip_backup", "force")
        if (query_params or {}).get(k) is not None
    }
    # #412 — 拿 CAS 后的【新鲜】属性(ReturnValues=ALL_NEW),不再用 CAS 前的陈旧 item
    # 判 host_id/capacity_reservation_id(codex #1:陈旧快照会漏掉 reserve-won-then-delete
    # straddle 场景里刚写上的 host_id/令牌 → delete 跳过扣减 → 泄漏)。deleting CAS 一旦赢,
    # dispatch 的 reserve 事务(条件 status=creating)必不能再提交,故此刻属性是权威终值。
    fresh = dict(item)
    try:
        _cas_resp = clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET #s = :deleting, updated_at = :t, "
                "delete_retryable = :false, delete_prev_status = :prev, "
                "delete_claim_expires_at_epoch = :claim_exp, "
                "delete_intent = :intent"
            ),
            ConditionExpression=(
                f"#s <> :deleted AND #s <> :deleting AND {delete_condition}"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":deleting": "deleting",
                ":deleted": "deleted",
                ":false": False,
                ":prev": prev_status,
                ":claim_exp": claim_expires_at,
                ":intent": _delete_intent,  # #532 见上方说明
                ":t": utils._now(),
                **delete_condition_values,
            },
            ReturnValues="ALL_NEW",
        )
        if isinstance(_cas_resp, dict) and _cas_resp.get("Attributes"):
            # #412(codex review4 #1)—— 用 ALL_NEW 的【完整 post-image】,【不】与陈旧 item 合并。
            # 合并会把并发 promote 刚 REMOVE 掉的 capacity_reservation_id 从旧 item 里"复活",
            # 令 delete 误走令牌释放(而该租户已 running、令牌已清),token 释放条件失败 →
            # 走不到旧的按 item.vcpu 扣减 → 账本永久泄漏。ALL_NEW 是 CAS 之后的真值,直接用。
            fresh = _cas_resp["Attributes"]
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        # CAS 撞 deleting/deleted。区分两种:
        # ① status 已 deleted → 真幂等,删完了,返 200。
        # ② status 还是 deleting → 删除未完成(上次副作用失败留下,#268)。若这是 consumer
        #    队列重投(_consumer_ident,FIFO MessageGroupId=tenant_id 保证同租户串行消费,
        #    不存在并发双删),应**继续重试剩余副作用**(stop-vm/rm 幂等),而不是返 200 把
        #    半清理的租户永久卡 deleting(旧行为 → #268 盘泄漏 + 孤儿永不收敛)。同步并发
        #    请求(无 _consumer_ident)仍返 200 防 #107 双删扣穿——那条路没有重投机制。
        cur = (
            clients.tenants_table.get_item(
                Key={"id": tenant_id}, ConsistentRead=True
            ).get("Item")
            or {}
        )
        if cur.get("status") == "deleted":
            return utils._resp(200, {"id": tenant_id, "status": "deleted"})
        is_consumer_replay = bool((event or {}).get("_consumer_ident"))
        now_epoch = int(time.time())
        claim_expired = (
            int(cur.get("delete_claim_expires_at_epoch", 0) or 0)
            <= now_epoch
        )
        if (
            not is_consumer_replay
            and cur.get("delete_retryable") is not True
            and not claim_expired
        ):
            # Another synchronous request still owns an unexpired delete claim.
            # Return idempotently regardless of reservation-token state; retry
            # becomes eligible when the owner marks retryable or the claim expires.
            # #565 G6 —— 这次删除**是别人发起的**,本地没有那个死线值,从 `cur` 这行读
            # (`:3590` 的 ConsistentRead 新鲜行)。调用方分不清自己拿到的是这条 200 还是
            # 下面那条 202,所以两条都得带 —— 否则字段"有时在有时不在",等于不可用。
            return utils._resp(
                200,
                _with_action_deadline(
                    {"id": tenant_id, "status": "deleting"},
                    create_deadline.ACTION_DELETE,
                    row=cur,
                ),
            )
        if not is_consumer_replay and (
            cur.get("delete_retryable") is True or claim_expired
        ):
            try:
                next_claim_exp = now_epoch + _DELETE_CLAIM_TTL_SECONDS
                clients.tenants_table.update_item(
                    Key={"id": tenant_id},
                    UpdateExpression=(
                        "SET delete_retryable = :false, updated_at = :t, "
                        "delete_claim_expires_at_epoch = :claim_exp"
                    ),
                    ConditionExpression=(
                        "#s = :deleting AND (delete_retryable = :true OR "
                        "attribute_not_exists(delete_claim_expires_at_epoch) OR "
                        "delete_claim_expires_at_epoch <= :now) AND "
                        f"{delete_condition}"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":deleting": "deleting",
                        ":true": True,
                        ":false": False,
                        ":now": now_epoch,
                        ":claim_exp": next_claim_exp,
                        ":t": utils._now(),
                        **delete_condition_values,
                    },
                )
                cur["delete_retryable"] = False
                cur["delete_claim_expires_at_epoch"] = next_claim_exp
            except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
                # #565 G6 —— 与上面那条 200 同源:抢 claim 失败说明删除归别人,死线只在行上。
                return utils._resp(
                    202,
                    _with_action_deadline(
                        {"id": tenant_id, "status": "deleting"},
                        create_deadline.ACTION_DELETE,
                        row=cur,
                    ),
                )
            print(
                f"delete_tenant #425: {tenant_id} claimed retryable/expired delete"
            )
        # consumer 重投卡在 deleting 的删除:status 已是 deleting,不再翻转,直接往下
        # 重试副作用(stop-vm/rm 幂等)。#107 账本回退由下方 `used_vcpu >= :v` guard
        # 防重复扣穿(第一次已扣过则 CCF → skip),不会二次扣账本。
        prev_status = cur.get("delete_prev_status", prev_status)
        if cur:
            fresh = cur  # #412 — 重投路径同样用新鲜属性判 host_id/令牌

    keep_data = query_params.get("keep_data", "true").lower() == "true"
    host_id = fresh.get("host_id")
    # Go-live B2: snapshot-before-destroy. Deleting with keep_data=false does an
    # irreversible `rm -rf` of the tenant's data disk. Per the project rule
    # "不可逆操作前先保护", we first take a SYNCHRONOUS backup to S3 so the data
    # is recoverable, and fail closed (abort the delete) if the backup fails —
    # unless the caller explicitly opts out with ?skip_backup=true (e.g. the
    # tenant truly has no data worth keeping). keep_data=true deletes (soft) keep
    # the disk on the host anyway, so no pre-backup is needed there.
    skip_backup = query_params.get("skip_backup", "false").lower() == "true"

    # #107 — if we abort AFTER winning the CAS gate (status already "deleting")
    # but BEFORE doing any destructive side effect (backup failed → we bail to
    # protect data, 铁律 #4), roll status back to its pre-delete value so the
    # tenant isn't stranded in "deleting" (invisible to list, un-actionable). No
    # capacity counter was touched yet at this point, so status is all we restore.
    def _abort_restore_status():
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                # #412 blocker-A(codex round2 #1)—— 回滚到活跃态时【一并 REMOVE predelete_backup_at】。
                # 该标记只对"本次删除尝试"有效;回滚 = 放弃本次删除,租户重新可用、之后可能写【新】
                # 数据。若把标记留在活跃租户上,下一次 delete 会凭陈旧标记跳过 backup → rm 掉新写的
                # 未备份数据(铁律#4)。故回滚必清标记,让下次 delete 重新走 backup。
                UpdateExpression=(
                    "SET #s = :prev, updated_at = :t "
                    "REMOVE predelete_backup_at, delete_retryable, "
                    # #532 —— 回滚到活跃态必须一并清 delete_intent。留着它,下一次删除若在
                    # 写自己的意图【之前】崩溃(理论上不会:意图与 deleting CAS 同一条 update),
                    # 或运维直接读这一行做判断时,会看到一次【已被放弃的】删除的意图。
                    # 与 #412 blocker-A(回滚必 REMOVE predelete_backup_at)同一条教训。
                    "delete_prev_status, delete_claim_expires_at_epoch, delete_intent, "
                    # #532 —— reconciler 的重投计数与 exhausted 标记也必须清。
                    # **不清 `delete_redrive_exhausted` 是一条真缺陷**:软删行可被回收复用
                    # (#529 reclaim-soft-deleted),而扫描判据把带该标记的行排除出候选集
                    # ⇒ 一个陈旧标记会让那个租户【以后永远不被自动收敛】。
                    # 与 #412 blocker-A(回滚必 REMOVE predelete_backup_at)同一条教训。
                    "delete_redrive_attempts, delete_redrive_exhausted, "
                    # #565 G2 —— `delete_reported_failed_at` 同属"只对本次删除尝试有效"那一类,
                    # **漏清它是上面同一条教训的第四个实例**。它是死线围栏的 per-attempt 幂等锚
                    # (`deadline_executor._fence_failed` 的 delete 档条件里写着
                    # `attribute_not_exists(delete_reported_failed_at)`),留在一个已回滚成活跃态
                    # 的租户上,后果是**该租户此后每一次 delete 超死线都被静默成 `raced`、永远
                    # 不再回报失败** —— 而 G2 的整个要求就是「已回报失败」与「仍在删除」必须是
                    # 两个可读的事实。陈旧锚把第一个事实永久钉死在"已报过",两个事实都失效。
                    #
                    # **只清这一个,不清 `delete_fail_reason` / `delete_fail_at`**:那两个是
                    # 「最近一次该操作的失败」历史记录,客户面契约
                    # `lifecycle-deadline-contract.md` 已逐字承诺它们「永不自动清除」,
                    # 清它们是破坏性变更;而本字段是内部幂等闸门,契约里没有它,生产代码里
                    # 也只有围栏一个读者。判据是「终态留历史 / 回归在役清 per-attempt 状态」。
                    "delete_reported_failed_at"
                ),
                ConditionExpression=f"#s = :deleting AND {delete_condition}",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":prev": prev_status,
                    ":deleting": "deleting",
                    ":t": utils._now(),
                    **delete_condition_values,
                },
            )
        except Exception:
            # Best-effort restore; if another op already moved it, leave as-is.
            pass

    def _mark_delete_retryable():
        """Allow one caller to resume a failed post-stop delete."""
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET delete_retryable = :true, updated_at = :t",
            ConditionExpression=f"#s = :deleting AND {delete_condition}",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":deleting": "deleting",
                ":true": True,
                ":t": utils._now(),
                **delete_condition_values,
            },
        )

    if _route_cleanup_requires_host(fresh):
        _mark_fail_reason(tenant_id, "delete", create_deadline.REASON_ROUTE_CLEANUP_BLOCKED)
        _mark_delete_retryable()
        return utils._resp(
            502,
            {
                "error": "route cleanup blocked: tenant has persisted route state "
                "but no host_id. Tenant remains deleting; restore host_id or "
                "clean the exact DNAT/Redis route after identifying its host, "
                "then retry.",
                "id": tenant_id,
                "requires_intervention": True,
            },
        )

    # #412 blocker-A(codex review CHANGES_NEEDED #1)——delete replay 幂等 + no-data-loss。
    # 要同时关住的两个方向:
    #  · 首次 delete 干净翻 running→deleting 后,若后续步骤遇瞬时错误留 deleting,consumer
    #    replay 会再进来:此时 rm 数据盘可能已跑、盘已没,再跑 pre-delete backup 必失败
    #    (backup 打已删目录)→ 若 fail-closed 就永远到不了令牌释放 → 容量永久搁浅。
    #  · 反向也【绝不能】仅凭"状态是 deleting"就跳过 backup:CAS 翻 deleting 先于 backup,
    #    一个在 backup【之前】崩溃的首次 delete 也留 deleting、盘仍在且从未备份——盲跳过
    #    会 rm 未备份数据(铁律#4 数据丢失)。
    # #412 当时的解法是"backup 成功后写 predelete_backup_at,仅当标记存在才跳过"。
    # 该解法依赖 marker 写在 stop-vm【之后】(marker ⇒ VM 已停 ⇒ 备份后无新写入),而 #469
    # 的原子化消除了那个中间点,故判据已改为下面的"盘是否还在",见 #469 那段说明。
    # ── #469(codex 独立复审 blocker #1)—— 重投【不】凭 marker 跳过备份 ──────────
    # 原判据是"marker 存在 ⇒ 跳过备份"。原来 marker 写在 stop-vm 成功【之后】,故它
    # 蕴含"VM 已停 ⇒ 不可能有晚于备份的新写入",跳过是安全的。原子化把 stop 与 rm -rf
    # 合进同一条 SSM,那个中间点没了(见下方 marker 段的说明),marker 只能提前到破坏性
    # 动作【之前】写 —— 此时 VM 仍在跑。于是出现一个真实的丢数据窗口:
    #   marker 落库 → VM 继续接受并 ack 新写入 → Lambda 崩溃 → 重投凭 marker 跳过备份
    #   → 删盘。备份里没有这批已 ack 的增量,数据丢失(铁律#4)。
    # 修正:重投【照常重跑】备份(同一 tenant 覆盖同一 S3 key,幂等),只在备份失败且
    # 失败原因是【盘已经不在】时才放行继续删。盘不在 = 上一次尝试的 rm -rf 已跑完 =
    # 已无数据可丢,此时若也 fail-closed 就成死局(令牌永不释放、容量永久搁浅)。
    # 这样两个方向都关住:盘还在 → 一定重新备份(不丢增量);盘已没 → 放行(不死锁)。
    # marker 从"跳过备份的凭据"降级为"仅供审计/排障的时间戳",不再参与控制流。
    #
    # ── 还有一个【上游既有】的窗口,本轮一并关掉(codex 独立复审)──────────────
    # backup-data.sh 压缩完会把 VM 【resume】(:79 与 trap cleanup:36),而控制面随后还要
    # 写 marker、发 SSM,这几秒里 VM 在跑、可以 ack 新写入 —— 那批写入进不了刚才那份
    # 备份,却会随 rm -rf 一起消失。原子化前也是这样(上游注释即写明"backup-data.sh 在
    # 压缩后会恢复 VM"),不是本改动引入,但既然 delete 收尾已经变成 host 侧一条脚本,
    # 就有了干净的关法:让脚本在【停机之后】再补一次【静止盘】备份(参数 7)。
    # 此刻盘不可能再被写,那份产物就是删盘前的最终状态。S3 key 按时间戳命名 = 追加而非
    # 覆盖,前一份仍可用。只有真做了 pre-delete backup 的这条路径才要求它(下面这个标志)。
    _want_quiesced_backup = False
    if host_id and not keep_data and not skip_backup:
        # 强制备份 fail-closed(共用 _force_backup_sync,与 rebuild 降级同一份经验证的
        # 双层校验)。失败 → 回滚 status(_abort_restore_status,CAS 只回滚自己翻的
        # deleting)+ 502,绝不 rm 未备份数据(铁律#4)。
        # #565 G1 —— delete 是 600s 档,执行段 210s 里给备份 90s(余下 120s 给 host 删除)。
        _ok, _err = _force_backup_sync(
            tenant_id,
            ssm_budget_sec=create_deadline.exec_step_sec(
                create_deadline.ACTION_DELETE, "backup"
            ),
        )
        # 哨兵由 backup-data.sh 在 `[ ! -f data.ext4 ]` 分支打出,经 backup Lambda 的
        # result["error"] = <SSM 输出> 原样透传上来。用固定串而非 grep "not found":
        # 后者太脆(路径本身、其它步骤的报错都可能含该词),误判会跳过一次【本该做】的
        # 备份然后删盘。
        _source_absent = bool(_err) and "OC_BACKUP_SOURCE_ABSENT" in _err
        if not _ok and not _source_absent:
            _mark_fail_reason(tenant_id, "delete", create_deadline.REASON_BACKUP_FAILED)
            _abort_restore_status()
            # #547 兄弟路径 —— 这条 fail-closed 早退必须放掉【自己】取得的租约。
            #
            # 判据与 ㉓(:2801)同一条:【此刻还没有任何破坏性命令下发】。本支只做了一次
            # 同步 invoke backup Lambda(读盘 + 写 S3),没有 stop-vm、没有 rm、没有改路由,
            # 且 _abort_restore_status() 已把 status 从 deleting 回滚掉。
            #
            # 不放的后果与 #547 在 rebuild 侧修掉的那条【逐字相同】—— 两者是同一个
            # _force_backup_sync 的两个调用者:wrapper(:2626)见 code>=500 就
            # hold_lifecycle_fence=True,租约被扣住整个 LIFECYCLE_FENCE_LEASE_SECONDS
            # (1800s),而下面的文案写着 "Retry" —— 照文案 retry 会撞
            # 409 LIFECYCLE_IN_FLIGHT,半小时内根本进不来(:2795 记的"承诺一件代码不
            # 允许的事")。放掉之后那句 Retry 才是实话,不必再改文案。
            if _lifecycle_ctx is not None:
                _lifecycle_ctx["release_lifecycle_fence_on_error"] = True
            return utils._resp(
                502,
                {
                    "error": "pre-delete backup failed; aborting destroy to avoid "
                    "irreversible data loss. Retry, or pass ?skip_backup=true to "
                    "delete without a backup.",
                    "backup_error": _err,
                },
            )
        if _source_absent:
            # 盘已不在:上一次尝试已过 rm -rf。继续走完剩余收尾(路由/账本/状态),
            # 让租户能收敛到 deleted、令牌得以释放。脚本每步对已完成都幂等。
            # 也【不必】再要求静止盘补备份:没有盘可备,脚本那侧同样会跳过。
            print(
                f"delete_tenant #469: data disk already absent for {tenant_id} "
                f"(a prior attempt completed rm -rf); skipping pre-delete backup "
                f"and continuing cleanup so the capacity token is not stranded. "
                f"backup_error={_err}"
            )
        else:
            # 备份真做成了 ⇒ 盘还在、且 backup-data.sh 已把 VM resume 回去。要求脚本在
            # 停机后补一份静止盘备份,把这段 resume 窗口里的写入收进去(见上方说明)。
            _want_quiesced_backup = True
        # ── #469 — marker 现在只是【审计时间戳】,不再参与控制流 ─────────────────
        # 历史:原来 marker 写在 "stop-vm 成功之后、rm -rf 之前",蕴含"VM 已停 ⇒ 备份后
        # 无新写入",故可当"重投跳过备份"的凭据。原子化把 stop 与 rm -rf 合进同一条 SSM,
        # 那个中间点消失;marker 只能提前到破坏性动作之前写,于是它【不再】蕴含 VM 已停,
        # 拿它当跳过凭据会丢掉"marker 落库后 VM 又 ack 的增量"(codex blocker #1)。
        # 现在跳过备份的判据改成上面那条"备份失败且 OC_BACKUP_SOURCE_ABSENT",marker
        # 退化为排障用的时间戳:记录本次尝试何时完成了 pre-delete backup。
        #
        # 仍然保留它 + 保留写失败即中止,有两个理由:
        #   · _abort_restore_status() 会一并 REMOVE 它(:2549),回滚后不留陈旧痕迹;
        #   · 写不进通常意味着 fence 已易主或 DDB 异常,此时不该继续做破坏性动作。
        #
        # ★ 但【盘已经不在】的重投路径【不写】marker(codex 独立复审):
        #   ① 语义上没意义 —— marker 记的是"本次尝试何时完成了 pre-delete backup",
        #      而这条路径根本没做备份(盘都没了);
        #   ② 更要紧的是失败处理会出错:写失败原本走 _abort_restore_status() 回滚到
        #      running/stopped 等【活跃态】,前提是"盘与备份都完好"。盘已被上一次尝试
        #      删掉时这个前提不成立 —— 回滚等于谎报"已毁租户存活"(#268 明确禁止),
        #      而且租户从 deleting 变回活跃后,容量令牌与账本也不再收敛。
        #   故这条路径直接跳过 marker;它本来就只差"把剩余收尾做完 + 释放令牌"。
        if not _source_absent:
            marker_ts = utils._now()
            try:
                clients.tenants_table.update_item(
                    Key={"id": tenant_id},
                    UpdateExpression="SET predelete_backup_at = :t",
                    # 与本函数其它状态写一致带上 #413 P1 的 fence 条件:被抢占的操作
                    # (租约易主/epoch 前进)不得写 marker,否则它会让【新】操作误判
                    # "备份已做"而跳过 backup 直接删盘。
                    ConditionExpression=f"#s = :deleting AND {delete_condition}",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":t": marker_ts,
                        ":deleting": "deleting",
                        **delete_condition_values,
                    },
                )
                fresh["predelete_backup_at"] = marker_ts
            except Exception as e:  # noqa: BLE001
                _mark_fail_reason(tenant_id, "delete", create_deadline.REASON_BACKUP_FAILED)
                # marker 写不进 ⇒ 不敢做破坏性动作。此处盘【确实还在】(_source_absent
                # 为假 ⇒ 备份刚刚成功 ⇒ 盘存在),故回滚到删除前状态是安全的:
                # 盘与备份都完好,重投会重跑 backup 再试落 marker。
                _abort_restore_status()
                return utils._resp(
                    502,
                    {
                        "error": "pre-delete backup marker persistence failed; aborting "
                        "destroy while the disk is still intact. Retry.",
                        "detail": str(e),
                        "id": tenant_id,
                    },
                )
    if host_id:
        # ── #469 R2/S2 — host 侧【原子】收尾:1 条 SSM 取代原来的 4 条 ──────────
        # 原来控制面逐条下发 stop-vm / rm vm.json / route_ops delete-route /
        # touch+rm -rf 共 4 条阻塞 SSM。两个后果,均真机实证(2026-08-12 us-west-2,
        # QPS20×10s 派发 200 个 DELETE):
        #  ① 速率放大 4×:800 次 SendCommand / 27s ≈ 30 次/秒 → SSM 服务端
        #     `ThrottlingException: Rate exceeded`(日志 89 次、已 reached max retries: 4),
        #     HTTP 502 占 89.5%。把 host 的 CommandWorkersLimit 20→50 【无改善】
        #     (89.5%→89.0%,throttle 反增到 99)——限的是控制面 API 提交速率,不是 host
        #     执行并发。合并为 1 条把速率降到 1/4。
        #  ② `deleting` 中间态:4 条里后 2 条落在【不可回滚区】(VM 已停,回滚成 running
        #     会谎报已毁租户存活),任一条被限流打挂即卡住——实测 46/200 = 23% 卡 deleting。
        #     合并后控制面只有【一个】判定点,"stop 成功但 rm 失败"对控制面不再可见。
        # 同款"控制面一条、host 本地 fan-out"已在本仓验证(start-all-vms.sh /
        # stop-all-vms.sh),内部标杆亦然(Lambda MicroManager,见 ADR-batch-delete-throttle §2.1)。
        #
        # #412(codex review #2)—— vm_num/host_port/guest_ip 都用【fresh】(CAS 后 ALL_NEW),
        # 不用 CAS 前的陈旧 item:reserve/delete 竞态下 host_id 从 fresh 拿了、vm_num 却用陈旧值,
        # 会 stop/摘 DNAT 到【别的租户】的 tap-vm<n>(no-cross-tenant 违规)。三者同源一致。
        # #268 fail-loud 语义原样保留,只是判定点从 4 个收敛成 1 个:
        #   rc≠0 → 不推进 deleted。VM 是否已停无法从单个 rc 区分,故统一用
        #   _mark_delete_retryable()【留 deleting】而不是 _abort_restore_status() 回 running:
        #   回 running 的前提是"VM 确实还在跑",而原子脚本失败时 VM 可能已停(①成功②失败),
        #   此时回 running 会谎报已停租户存活;留 deleting 则重投幂等补做剩余步骤(每步都
        #   对已完成无害)。保守取舍:宁可多一个可重投的中间态,不要一个谎报的活跃态。
        vm_num = int(fresh.get("vm_num", 1))
        _hp = int(fresh.get("host_port", 0) or 0)
        _gip = fresh.get("guest_ip", "")
        _legacy_hp = clients.VM_PORT_BASE + vm_num - 1
        # #469 — 原来的 4 条 SSM 合并成这一条 delete-vm.sh(见上方说明)。
        # ★ #413 P1 的 lifecycle fence 语义【原样保留】:仍在 host 副作用【前后】各夹一次
        #   delete_host_guard。合并后从"4 条 × 前后 2 次 = 8 次 guard"降到 2 次,但保护
        #   的窗口不变——前置 guard 确认本次操作仍持租约(否则 exit 79 被抢占 / exit 78
        #   读不到 fence 时 fail-closed),后置 guard 确认整个破坏性序列期间没被抢占。
        #   guard 里含 aws dynamodb get-item,是 shell 片段而非独立 SSM,不增加
        #   SendCommand 次数,故不抵消本改动的限流收益。
        # ── 部署顺序无关的自愈式装载(codex 独立复审)─────────────────────────────
        # 问题:控制面一上线就【无条件】调 /home/ubuntu/delete-vm.sh,而这是本次【新增】
        # 的脚本。init-host.sh 的硬失败拉取只覆盖【新起】的 host —— 已经在跑的 host 不会
        # 重跑 init,于是"Lambda 先部署、host 脚本还没同步"这个窗口里,每次删除都 exit 127
        # (或撞上旧版 stop-vm.sh 不认 guest flush 契约而强杀未刷盘 guest)。
        # 光在文档里写"必须成对部署"不算保护 —— 那是把正确性寄托在人工步骤上。
        #
        # 解法:命令串前置一段自愈 —— 两个脚本【任一】缺失或过期就从 S3 的 deployment/
        # scripts/ 重新拉,与 init-host.sh 同一来源、同一路径。host 本来就有该桶读权限
        # (init-host.sh 就是这么装的),故不需要新 IAM。
        # · 判据不只看"文件存在",还看 stop-vm.sh 是否认得 guest flush 契约。
        # · 拉取失败即 `exit 1`,让整条命令非零 → 控制面走 _mark_delete_retryable()
        #   留 deleting 重投,绝不在"脚本可能过期"的状态下动手删盘。
        # #520 C2:这段实现搬到 core.ssm_dispatch.host_script_self_heal,因为 suspend /
        # restore / reset / rebuild 有同一个病(那几条路径此前完全没有兜底),重复四份
        # 会各自漂移。行为不变:同样两个脚本成对装、同样以 guest flush 哨兵作
        # "存在但过期"的判据、同样任何一步失败即 exit 1。
        # 其余理由(为什么桶名由 host 自己从 /etc/platform.env 读、为什么不许 `|| true`)
        # 都写在那个函数的 docstring 里。
        _self_heal = ssm_dispatch.host_script_self_heal(
            ("delete-vm.sh", "stop-vm.sh"),
            "oc:delete",
            freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED"),
        )
        # 第 7 个参数 = 停机后补一份静止盘备份(仅当本次真做了 pre-delete backup;
        # 见上方 `_want_quiesced_backup` 的说明)。脚本侧默认 false,少传即行为不变。
        _del_cmd = (
            f"{_self_heal} && {delete_host_guard} && /home/ubuntu/delete-vm.sh "
            f"{shlex.quote(tenant_id)} {vm_num} {_hp} {shlex.quote(_gip)} "
            f"{_legacy_hp} {'true' if keep_data else 'false'} "
            f"{'true' if _want_quiesced_backup else 'false'} "
            f"&& {delete_host_guard}"
        )
        # #565 G1 —— delete 是 600s 档,执行段 210s = 同步备份 90 + host 删除 120。
        # 原值 300 与备份的 300 相加正好等于 600s 死线 → 受理段与排队段**零余量**,
        # 队列里只要有一条在等,delete 就必然超死线。
        if not ssm_dispatch._ssm_run(
            host_id,
            _del_cmd,
            timeout=create_deadline.exec_step_sec(
                create_deadline.ACTION_DELETE, "host-delete"
            ),
        ):
            _mark_fail_reason(tenant_id, "delete", create_deadline.REASON_HOST_UNREACHABLE)
            _mark_delete_retryable()
            print(
                f"delete_tenant #469: atomic delete-vm.sh FAILED for {tenant_id} on "
                f"host {host_id} (SSM timeout/throttle or script rc!=0) — keeping "
                f"status=deleting for retry, NOT marking deleted."
            )
            return utils._resp(
                502,
                {
                    "error": "host-side delete failed (SSM timeout/throttle or script "
                    "error); tenant kept in deleting for safe re-drive. Retry — delete "
                    "via the lifecycle queue re-drives automatically.",
                    "id": tenant_id,
                },
            )
        # ── 以下四段原为独立 SSM,已合并进 delete-vm.sh(见上方 #469 说明)──────
        #  · stop-vm.sh                          → delete-vm.sh ①
        #  · rm -f <vmdir>/vm.json               → delete-vm.sh ②(仍在 stop 成功之后,
        #    顺序不变:先删 vm.json 再 stop 会让 host-agent 不 recover 而 VM 仍跑 = 孤儿)
        #  · route_ops.py delete-route           → delete-vm.sh ③
        #  · touch .purge-<tid> && rm -rf <vmdir> → delete-vm.sh ④(#321 双门 tombstone
        #    原样保留在脚本内;keep_data=true 不写 tombstone,GC 绝不碰其盘)
        # 每步的 fail-loud 与幂等语义在脚本里逐条保留;控制面这里只判一个总 rc。

        # Update host counters.
        # #412 — dispatch 预留的租户带 capacity_reservation_id 令牌:此时账本增量与
        # host_id 是【一个事务】原子落库的(_reserve_batch_txn),释放也必须【令牌化】——
        # 扣 host + 删令牌一个事务、条件 capacity_reservation_id=:rid,与 reaper/poller/批回滚
        # 共用同一互斥锚,谁先消费谁扣一次,其余幂等 no-op(codex #3 防 ABA 双扣)。令牌缺失
        # (同步 create 租户,reserve 时即写 host_id、无令牌)→ 走下方 #107 的原始扣减路径。
        rid = fresh.get("capacity_reservation_id")
        if rid:
            _rel = _release_capacity_reservation(
                tenant_id, host_id, rid, int(fresh.get("vcpu", 0)), int(fresh.get("mem_mb", 0))
            )
            print(
                f"delete_tenant #412: token release tenant={tenant_id} host={host_id} "
                f"rid={rid} result={_rel}"
            )
            if _rel == _REL_RETRY:
                # #412(codex review #3)—— 释放瞬时失败,令牌可能仍占容量。绝不能继续标
                # deleted:deleted 租户不被 reaper 兜底 → 令牌永久搁浅、容量永漏。【留 deleting】
                # (不 _abort_restore_status 回 running——VM 已停、盘已删,回 running 会谎报已毁
                # 租户存活)返 502,队列消费者/调用方重投;重投时仍 deleting → CCF 分支放行重试
                # 副作用(stop/rm 幂等)+ 重跑本释放(令牌仍在 → 幂等消费一次,已消费则 already)。
                # #565 G3(Codex 独立复审第二轮)—— **不能用 system_error**:它的已发布语义是
                # 「不值得重试,报障」,而这条路上面那段注释明写「返 502,队列消费者/调用方
                # 重投」。照 system_error 行事的调用方会不重投 → 租户永久停在 deleting、
                # 令牌永久搁浅,正是这条出口 fail-closed 想避免的结局。
                _mark_fail_reason(
                    tenant_id, "delete", create_deadline.REASON_CAPACITY_RELEASE_PENDING
                )
                return utils._resp(
                    502,
                    {
                        "error": "capacity reservation release failed (transient); kept "
                        "status=deleting for re-drive to avoid stranding the reservation.",
                        "id": tenant_id,
                    },
                )
            _maybe_mark_idle(host_id)
        else:
            # #107 defense-in-depth: guard the decrement with `>= :v` so it can NEVER
            # drive the ledger negative even if the status CAS gate is somehow bypassed
            # (e.g. a concurrent lifecycle action reversed "deleting" back to active and
            # a second DELETE won the gate again). The gate makes the double-decrement
            # rare; this guard makes an over-decrement structurally impossible. On a
            # guard violation we fail LOUD (log the anomaly) instead of silently
            # subtracting into negative — the tenant is still marked deleted (its VM is
            # gone), we just refuse to corrupt the capacity ledger.
            host_resp = None
            try:
                host_resp = clients.hosts_table.update_item(
                    Key={"instance_id": host_id},
                    UpdateExpression="SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
                    ConditionExpression="used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one",
                    ExpressionAttributeValues={
                        ":v": item["vcpu"],
                        ":m": item["mem_mb"],
                        ":one": 1,
                    },
                    ReturnValues="ALL_NEW",
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    # Ledger would go negative → a prior decrement already ran for this
                    # tenant (double-delete slipped the gate). Skip the subtract; do NOT
                    # corrupt the ledger. Loud so it's caught, not swallowed.
                    print(
                        f"delete_tenant #107: refused ledger under-run on host {host_id} "
                        f"for tenant {tenant_id} (vcpu={item['vcpu']}, mem={item['mem_mb']}) "
                        f"— counters already reclaimed by a prior delete; skipping."
                    )
                else:
                    raise
            # Record idle_since when host becomes empty (defensive — mocks may
            # omit Attributes; treat as still-busy and skip).
            attrs = host_resp.get("Attributes") if isinstance(host_resp, dict) else None
            if attrs and int(attrs.get("vm_count", 0)) == 0:
                clients.hosts_table.update_item(
                    Key={"instance_id": host_id},
                    UpdateExpression="SET idle_since = :t",
                    ExpressionAttributeValues={":t": utils._now()},
                )
        # 容量令牌只有 consumed/already 才走到这里；RETRY 已提前返回并保留占号。
        phys_num = fresh.get("phys_vm_num", fresh.get("vm_num"))
        if phys_num is not None:
            scheduling.release_phys_slot(host_id, phys_num, tenant_id)

    # Go-live C: reclaim the per-tenant LiteLLM vkey so it doesn't linger in
    # LiteLLM after the tenant is gone (credential + budget leak over churn).
    # Best-effort — delete proceeds regardless; we record whether it was revoked.
    vkey_revoked = vkey._revoke_tenant_vkey(item.get("litellm_vkey"))

    # #187 P5 — Cognito 渠道机器用户 helper 已删;老记录里若还残留
    # cognito_channel_password 字段,由下面的 UpdateExpression 幂等 REMOVE 清除。

    # #107 — collapse deleting → deleted (the winner's final step). We already
    # hold the concurrency gate (status is "deleting", set by our CAS above), so
    # this is an unconditional finalize; no second race is possible.
    # #166 — only drop litellm_vkey once revoke is confirmed. If the tenant had a
    # per-tenant vkey but revoke failed (e.g. LiteLLM transient outage), keep the
    # field and flag it so a reconciler can retry — dropping it here would orphan
    # a live key in LiteLLM (credential + budget leak with no way to find it).
    # #469 P2 —— 终态一并清掉卡死标记。这条【普通 delete】路径也会被强制删除走到
    # (卡死的 restoring、以及卡死但盘还在的 suspending 都路由到这里),不清则标记随
    # deleted 行残留;同 id 若被重建/或对账工具读到,会以为它还卡着。
    # 我第一版只在 suspended/force 专路清了,是 test_p2_force_marked_restoring_* 的
    # 断言把这半边漏洞抓出来的。REMOVE 不存在的属性是 no-op,对普通删除零影响。
    _stuck_removes = (
        ", lifecycle_stuck_at, lifecycle_stuck_reason, lifecycle_stuck_vm_dir"
        ", lifecycle_stuck_fc_alive, updated_at_stuck_seen, lifecycle_prev_status"
            ", lifecycle_rollback_deferred_until, lifecycle_probe_attempted_at"
    )
    if item.get("litellm_vkey") and not vkey_revoked:
        update_expr = (
            "SET #s = :s, updated_at = :t, vkey_revoke_failed = :vf "
            "REMOVE cognito_channel_password, delete_retryable, "
            # #532 —— delete_intent 与相邻这三个删除阶段字段同生命周期,终态一并清。
            "delete_intent, delete_redrive_attempts, delete_redrive_exhausted, "
            # #565 G2 —— `delete_reported_failed_at` 同属这一族,终态一并清。**它的已发布
            # 含义与 `status=deleting` 成对**(客户面契约:「你会读到 `status = deleting`
            # **且** 该字段有值 —— 含义是我方已认定这次删除超时、但删除仍在进行」),所以
            # 不变量是「字段在 ⟹ 有一次在飞的删除被回报过失败」。留在一行 `deleted` 上就得
            # 给它编第三种含义,而 G2 要的「成功后再纠正一次状态」在这里已经由
            # `status: deleting → deleted` 兑现 —— 清掉陈旧锚正是那次纠正的另一半。
            "delete_reported_failed_at, "
            f"delete_prev_status, delete_claim_expires_at_epoch, {_STALE_ON_DELETE_FIELDS}"
            + _stuck_removes
        )
        expr_vals = {":s": "deleted", ":t": utils._now(), ":vf": True}
    else:
        update_expr = (
            "SET #s = :s, updated_at = :t "
            "REMOVE litellm_vkey, cognito_channel_password, "
            # #532 —— 同上:与相邻三个删除阶段字段同生命周期,终态一并清。
            "delete_intent, delete_redrive_attempts, delete_redrive_exhausted, "
            # #565 G2 —— 同上一支的理由(那里写全了):字段的已发布含义与 `status=deleting`
            # 成对,终态清掉陈旧锚 = G2「成功后再纠正一次状态」的另一半。
            "delete_reported_failed_at, "
            "delete_retryable, delete_prev_status, "
            f"delete_claim_expires_at_epoch, {_STALE_ON_DELETE_FIELDS}"
            + _stuck_removes
        )
        expr_vals = {":s": "deleted", ":t": utils._now()}
    clients.tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression=update_expr,
        ConditionExpression=delete_condition,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            **expr_vals,
            **delete_condition_values,
        },
    )
    # #353 — 删租户【仍】清 gateway-token/device 密文行(方案 B,design decision)。TTL 已移除
    # (密文不再自动过期,活跃租户的凭据永久留存,让 1-2 年后 rebuild/recover 拿回原 token),
    # 但删租户属【终态】清理:已 deleted 的租户不能 rebuild(_async_actions 不含已删态),
    # 所以删除时主动 delete_item 清密文,把"任一 host 失陷可解密的范围"收窄到活跃租户,
    # 不留所有历史租户密文永久可解密的暴露面(HostRole grant_read_data 整张表 +
    # clawpool_cmk grant_decrypt 无 EncryptionContext 条件,compute.py:64/51)。既满足
    # rebuild 需求(只发生在活跃租户),又收敛暴露面(codex review Error3 权衡)。
    _cleanup_gateway_token_secret(tenant_id)
    # R16.2 — 终态释放在途占位(best-effort,TTL 兜底)
    inflight_dedup.release_inflight_lock(
        item.get("owner_id"), item.get("tenant_user_id")
    )
    # #240 — 终态释放 name 去重占位,让同作用域同 name 可再次创建。作用域用同一
    # 优先级(tenant_user_id > platform_id > owner_id),条件写限定占位仍指向本租户
    # (软删后立即重建同名的竞态下不误删新占位);漏删也由僵尸自愈兜底。
    name_dedup.release_name_lock(
        name_dedup.dedup_scope(
            item.get("owner_id", ""),
            item.get("tenant_user_id", ""),
            item.get("platform_id", ""),
        ),
        item.get("name", ""),
        tenant_id,
    )
    audit._publish_event(
        "tenant.deleted",
        tenant_id,
        {
            "keep_data": keep_data,
            "vkey_revoked": vkey_revoked,
        },
    )
    return utils._resp(200, {"id": tenant_id, "status": "deleted"})


def _cas_status(tenant_id, from_status, to_status, clear_stuck=False, stash_prev=False,
                stash_token=None):
    """#422 — CAS 翻租户 status(并发闸)。仅当当前 status == from_status 时翻到
    to_status,返回 True;CCF(已被别的 op 改)返回 False。suspend/restore 用它抢唯一
    赢家(抄 delete :2050 的 CAS 形态,但提成小工具供 suspend+restore 复用,避免各写一遍
    闭包)。status 是 DDB 保留字,必须用 ExpressionAttributeNames 占位。

    保持原行为:抢中间态那次调用绝不能清(那时还没标记,清了也无意义;更重要的是语义上
    "进入中间态"不该碰卡死历史)。回滚 = 租户回到活跃/稳定态,此后可能再写新数据、再发起
    新的 suspend —— 留着陈旧标记会让 P2 的 ?force=true 对一个健康租户放行。
    与 #412 blocker-A(_abort_restore_status 回滚必 REMOVE predelete_backup_at)同一条教训。

    lifecycle_prev_status,供【卡死巡检】回滚时知道该回哪个态。
    为什么必须记:`suspended_from` 只在 suspend 走到终态时才写(:3228),而卡在
    `suspending` 的租户【根本没到那一步】,该字段不存在。reaper 若回退到写死的
    "running",会把一个原本 `stopped` 的租户(suspend 允许 running/stopped 两种入口,
    见 :3071)错误地标成 `running` —— 那是「无 VM 却 running」的谎报,正是 #268 禁止的
    形态,而且它还会让后续 start/stop 的语义全错。
    与 CAS 写在【同一条 update】里 → 原子:抢到中间态的那一刻这个字段就在,不存在
    "翻了态但还没记下来源"的窗口。

    update。抄的正是 stash_prev 的形态与理由:令牌必须与"进中间态"原子落库,否则存在
    "翻了态但令牌还没写"的窗口,而收尾事务的条件是 `attr = :rid` —— 窗口内崩溃会让收尾
    永远条件失败、租户永久卡中间态。attr 只来自本文件的字面量(调用方从不透传外部输入),
    故直接拼进表达式;value 走占位符。

    回滚(clear_stuck=True)一并 REMOVE 令牌字段、suspend 的阶段快照与 restore 的临时坐标:
    租户回到稳定态后可能再发起一次新的 suspend/restore,留着上一轮的令牌会让新一轮的
    `attribute_not_exists` 前置条件恒失败,或让一次迟到的释放匹配上新一轮的放置(ABA)。
    与 #412 blocker-A(回滚必 REMOVE predelete_backup_at)同一条教训。
    **前提**:调用方只在"这一轮已经不占任何容量"时才带 clear_stuck 回滚 —— 释放返
    `_REL_RETRY`(令牌可能仍在、账本可能已加)时必须保持中间态,清掉令牌会把增量变成无主。"""
    _update = "SET #s = :to, updated_at = :t"
    _vals = {":to": to_status, ":from": from_status, ":t": utils._now()}
    if stash_prev:
        _update += ", lifecycle_prev_status = :lps"
        _vals[":lps"] = from_status
    if stash_token:
        _tok_attr, _tok_val = stash_token
        _update += f", {_tok_attr} = :tok"
        _vals[":tok"] = _tok_val
    if clear_stuck:
        _update += (
            " REMOVE lifecycle_stuck_at, lifecycle_stuck_reason, "
            "lifecycle_stuck_vm_dir, lifecycle_stuck_fc_alive, updated_at_stuck_seen, "
                "lifecycle_rollback_deferred_until, lifecycle_probe_attempted_at, "
            "lifecycle_prev_status, suspend_release_id, suspend_backup_key, "
            "restore_reservation_id, restore_host_id, restore_vm_num, "
            "restore_guest_ip, restore_host_port"
        )
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=_update,
            ConditionExpression="#s = :from",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=_vals,
        )
        return True
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _mark_fail_reason(tenant_id, action, reason):
    """#565 G3 —— 把机器可读的失败原因落到租户行 `<action>_fail_reason`。

    **为什么需要它**:同步失败时租户行上**零痕迹**。以 suspend 为例,它的 `_rollback()`
    只做 `_cas_status(tenant_id, "suspending", prev_status)` —— 状态回到 running/stopped,
    而失败原因随那个 502 响应一起消失。客户没接住那次响应(请求超时、网络断、客户端崩)就
    **永远不知道发生过什么**,轮询只看到一个 running 的租户。

    这与 #562 立的标准直接冲突:「失败必须是终态**且可归因**」——「还在创建」「不确定」
    业务拿它做不了决策。同步 5xx 只在客户接住时可归因,接不住就等于没给答复。

    **三条设计判断**:

    1. **取值先断言,再写库**(`assert_reason_valid`)。取值集合是对外契约、发布后不可改;
       一个拼错的值漏出去就再也收不回来,所以让它在**开发期**炸,而不是变成客户读到的垃圾值。
    2. **写失败只 print 不抛**。本函数全部在**已经失败的路径**上被调用,它是可观测性增强 ——
       绝不能让一次 DDB 抖动把本该返 502 的请求变成 500(那会把"备份失败"这种可归因结果换成
       "不知道为什么",正是 G3 要消灭的东西)。
    3. **不动 status,也不碰 `updated_at`**。状态机归各操作自己的 CAS/回滚。delete 那条
       尤其重要:它的 600s 只约束【给上层的答复】而实际删除不得丢弃(客户 2026-08-21),
       所以落原因**不等于**停止删除。

       `updated_at` **绝不能顺手写**(Codex 独立复审抓出的真缺陷,第一版写了)——
       `health_check._reap_stuck_lifecycle` 拿它当「进入该中间态的时刻」算 elapsed
       (那处注释明写:「updated_at 由 _cas_status 在翻中间态时写,所以它就是进入该中间态
       的时刻,无需新增字段」)。缺陷链:suspend A 赢 CAS 写 updated_at=T0 → 并发的
       suspend B 输 CAS,在这里落 preempted 顺带写 updated_at=T1 → 巡检从 T1 起算,
       **A 的卡死被推迟 (T1-T0) 才发现**;B 只要在超时窗口内反复重试,A 就永远不被收。
       失败时刻已经由 `<action>_fail_at` 记着,写 updated_at 不多给任何信息、只会破坏
       另一个字段的既有语义。**引入新写入前必须查清它碰的每个字段有谁在消费。**

    **语义是「最近一次该操作的失败原因」,不代表当前状态,也不会被自动清除。**
    当前状态永远只看 `status`。伴随字段 `<action>_fail_at` 让轮询方能判断这条记录是不是
    自己那次请求留下的(它比自己的发起时刻早 = 是旧记录)。

    为什么**不**在成功路径 `REMOVE`(考虑过,放弃了):那需要在五个操作各自"本次操作真正
    开始"的位置插清除,而 delete 根本没有这样一个位置 —— 它的 CAS 一赢就已经是 `deleted`
    终态了,那时清任何字段都没有读者。加时间戳是同一次写里的一个字段(零额外 DDB 写),
    信息还更多:"失败过、发生在何时"比"字段消失了"更能支撑重试决策。

    **一条已知边界(Codex 独立复审第三轮指出,本 issue 范围内不修):这是无条件写,没有
    per-attempt 世代号。** 调用点的顺序已经保证「落原因 → 改状态 → 放围栏」(见
    `TestMarkOrdering`),所以同一次请求内部不会乱序。但跨请求仍有一个窗口:某次尝试在 host
    侧卡很久 → 兜底巡检先把中间态回滚 → 新的同类请求开始 → 老那次才醒过来写原因,于是
    `<action>_fail_at` 比新请求的起始时刻还新。
    修它需要「受理即发一个操作句柄、失败写条件在该句柄上、stale 的 CCF 静默丢弃」——那是
    #564 G7 手动备份操作句柄要建的同一套机制,牵动每个操作的受理路径,属于新设计而不是本
    issue 的「落原因」。**不用 status 做条件**的原因也记在这里:有一批出口(delete 的 host
    探测失败那几条)发生在进入 `deleting` **之前**,拿中间态当条件会让它们的原因直接写不进去
    —— 那是比归属不精确更糟的回归。
    契约文档 §1.1 的提示框把这条边界如实写给了客户,并明确「判断某一次请求的成败只看
    `status`」。
    """
    create_deadline.assert_reason_valid(action, reason)
    attr = create_deadline.fail_reason_attr(action)
    at_attr = create_deadline.fail_at_attr(action)
    # #565 —— **这里刻意【不】加「`preempted` 不许覆盖 `deadline_exceeded_in_flight`」
    # 的条件写。** 病灶是真的,但那个修法在没有世代号的前提下不成立;记在这里防止重犯。
    #
    # 病灶(真实,用户 2026-08-24 那次 300 个 suspend 的压测里有 3 个是这形状):死线围栏先
    # 到点、把租户判成终态并落 `deadline_exceeded_in_flight`;随后那次操作自己的 CAS
    # (例如 suspend 备份完成后 `SET suspend_backup_key` 锚在 `status=suspending`)撞上已被
    # 翻掉的状态而失败,于是走"抢占"分支,把原因**改写成 `preempted`**。两个取值的对外契约
    # 不同(前者「可立刻重试」、后者「连续出现要报障」,见客户面契约 §1.2),降级之后上层会
    # 立刻重投进同一个堵死的队列再冤死一次。
    #
    # **为什么条件写解决不了它**(Codex 独立复审 2026-08-25 指出,实测判据在契约第 53 行):
    # 条件只能锚在 `<action>_fail_reason` 自身的值上,而那个字段**跨请求持久、永不清除**
    # (契约原文:「记的是最近一次该操作的失败……也不会被自动清除」)。于是同一条件也会拒绝
    # **三天后一次真抢占**的正确写入 —— 陈旧值永久胜出,契约里"最近一次"那句话对所有重复
    # 操作过的租户都变成假的。**那比它要修的 1.2% 方向性误记更糟**:前者是最新一次记错,
    # 后者是永远记着一个旧的。
    #
    # 正确修法要 per-attempt 世代号 —— 与上面判断 4 逐字同一件事(受理即发操作句柄、失败写
    # 条件锚在该句柄、stale 的 CCF 静默丢弃),即 #564 G7 手动备份那套机制推广到每个操作。
    # 契约 §3.2 已把它列为未兑现项(「失败原因精确归属到某一次尝试 → 归后续项」)。
    # **所以维持无条件写,不引入一个新的、未声明的谎言。**
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=f"SET {attr} = :r, {at_attr} = :t",
            # **必须带 attribute_exists(id)**(Codex 独立复审第五轮抓出的真缺陷)。
            # DDB 的 update_item 是 **upsert**:key 不存在就【新建】一行。而租户行是真的会被
            # 删掉的 —— create 入队失败的回滚路径(:1742)调 delete_item。于是这条竞态存在:
            #   T0 create 落库 creating → T1 入队失败,delete_item 真删行
            #   T2 一个在飞的操作失败,在这里 upsert → **凭空造出一行只有 id +
            #      <action>_fail_reason + <action>_fail_at 的畸形租户**(无 status、无 owner_id)
            # 那正是 #339 整套 CAS 要防的「删除后被复活」的一个新变种,而且更难查:卡死巡检
            # 按 status 过滤,压根扫不到它,但 GET /tenants 会把它列给客户。
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeValues={":r": reason, ":t": utils._now()},
        )
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        # 行已经不在了 —— 这不是"写失败",是**正确地放弃**:原因写给谁看?
        # 与上面判断 2 同一条精神(可观测性增强绝不反噬),所以也不抛。
        print(f"_mark_fail_reason: {tenant_id} 行已不存在,跳过落 {attr}={reason}")
    except Exception as e:  # noqa: BLE001 — 见上方判断 2:绝不升级为 500
        print(f"_mark_fail_reason: {tenant_id} {attr}={reason} 写入失败(不阻断): {e}")


def _write_action_deadline(tenant_id, action, deadline_epoch):
    """#564 G2 —— 把该操作的**绝对死线**落到租户行 `<action>_deadline`。写失败**必须抛**。

    与 `_mark_fail_reason` 的失败策略**刻意相反**,这不是不一致而是两者的位置不同:
      · `_mark_fail_reason` 在**已经失败**的路径上,是可观测性增强 —— 写失败只 print,
        绝不能把一个本该 502 的请求变成 500。
      · 本函数在**受理**路径上。死线没落到行上 = `deadline_executor` 扫不到这一行
        (它的 filter 第一条就是 `attribute_exists(<action>_deadline)`)= 这次操作**永远
        不会被判死**,卡在中间态直到有人手工干预。那正是本 issue 存在的全部理由。
        所以宁可 5xx 让客户重试,也不受理一个无法被超时的操作 —— fail-closed。

    **不写 `updated_at`**:理由与 `_mark_fail_reason` 逐字相同(见那边判断 3)——
    `health_check._reap_stuck_lifecycle` 拿它当"进入该中间态的时刻"算 elapsed,
    在别处顺手写会推迟另一条链的卡死发现。死线是绝对时间戳,不需要也不该借那个字段。

    **`attribute_exists(id)`**:同款理由 —— DDB 的 `update_item` 是 upsert,租户行真的会被
    删掉(create 入队失败的回滚路径调 `delete_item`),无条件写会凭空造出一行只有 id +
    死线字段的畸形租户,而按 status 过滤的巡检扫不到它、`GET /tenants` 却会列给客户。
    行已不存在时 **不抛**:那说明这次操作的对象没了,不是"死线写不进去",继续走原路径由
    下游的存在性检查给出正确答复。
    """
    attr = create_deadline.deadline_attr(action)
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            # #679(Codex 实现评审 #1)—— 这里**不得** REMOVE `deadline_enforced_at`:
            # 本函数被所有 async action 的受理共用,对一个 suspending+已判死的租户发 restore,
            # 受理会先走到这里 —— 无条件清会抹掉 suspend 的补偿标记,租户错过新矩阵、退回
            # 20 分钟的旧 stuck 路径。陈旧 enforced 对新一轮的误伤由 `deadline_enforced_for`
            # 轮次锚解决(围栏写、刹车比对),不靠受理清。
            UpdateExpression=f"SET {attr} = :d",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeValues={":d": int(deadline_epoch)},
        )
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        print(f"_write_action_deadline: {tenant_id} 行已不存在,跳过落 {attr}")


def _deadline_brake_engaged(tenant_id, action, observed_deadline):
    """#679 —— 执行链的**显式刹车**:围栏判死后,执行者不得再发出任何 host 副作用。

    背景:围栏的 suspend 在飞档已退为记录者(不再翻 `status=failed`),于是执行链原有的
    **隐式刹车**(后续锚 `status=suspending` 的条件写因 failed 而 CCF)不复存在 ——
    没有这道显式检查,判死之后执行者会把整条链跑完(consumer Lambda 允许跑 900s,
    `executionTimeout` 只约束每条命令发出之后,不约束最后一条何时发出),reaper 的归位
    会被迟到的 stop/rm 打脸,造出「行说 running、VM 已停」的谎报(#268)。

    真值表(强一致读,~毫秒级;for = 行上 `deadline_enforced_for` —— 围栏判死时记下的
    「判的是哪一轮」轮次锚;match = 行上 `<action>_deadline` 与执行者手里的相等):

      | 判据 | 决策 | 含义 |
      | for == 我的 deadline | 停 | 本轮被围栏判死 |
      | 行 deadline != 我的 | 停 | 行已属新一轮,我是陈旧执行者 |
      | enforced 在但 for != 我的 | 放行 | **上一轮的残留**(执行者与围栏赛跑、执行者赢下收尾后留下的记录)—— 对本轮无效。Codex 实现评审 #1:受理路径不能无条件清它(会误清并发操作的补偿标记),所以靠这个轮次锚辨认 |
      | 其余(dl 匹配、无本轮判死) | 放行 | 正常态 |

    `observed_deadline` 为 None(升级期老消息没带死线)按「否」处理 —— 分不清轮次就
    保守停(fail-closed)。**读失败一律当被判死(fail-closed)**:fail-open 会在 DDB 抖动
    的那一刻重新打开上面描述的整个窗口;误刹的代价只是本次操作留在中间态,由重投/补偿
    收敛 —— 与本仓「宁可慢,不说谎」同一条原则。

    返回 True = 刹车(调用方就地停手,不清理不回滚,全部交 reaper);False = 放行。
    """
    try:
        row = clients.tenants_table.get_item(
            Key={"id": tenant_id}, ConsistentRead=True
        ).get("Item")
    except Exception as e:  # noqa: BLE001 — 见 docstring:读失败 fail-closed
        print(f"_deadline_brake_engaged: {tenant_id} 读行失败,按判死处置(fail-closed): {e}")
        return True
    if not row:
        # 行没了(create 入队失败的回滚路径真的会删行)—— 对着不存在的租户继续发命令毫无意义。
        return True
    row_dl = row.get(create_deadline.deadline_attr(action))
    _enforced_for = row.get("deadline_enforced_for")
    if _enforced_for is not None and observed_deadline is not None:
        try:
            if int(_enforced_for) == int(observed_deadline):
                return True
        except (TypeError, ValueError):
            return True
    elif row.get("deadline_enforced_at") is not None and observed_deadline is None:
        # 老消息(无死线)撞上判死记录:分不清轮次,保守停(fail-closed)。
        return True
    if row_dl is None and observed_deadline is None:
        # 无死线的老链路(升级期消息 / 从未写过死线的行):这类操作永远不会被围栏判死,
        # 刹车对它没有意义 —— 放行,行为与 #679 之前逐字一致。
        return False
    try:
        return int(row_dl) != int(observed_deadline)
    except (TypeError, ValueError):
        return True


def _with_action_deadline(body, action, deadline_epoch=None, row=None):
    """#565 G6 —— 给一条「操作在飞」的响应体补上该操作的**绝对死线**,原地改并返回 `body`。

    **为什么每条在飞出口都要带,而不是只带受理那条**:调用方**无从知道**自己走了哪条
    内部路径 —— 同一个 `POST /tenants/{id}/suspend` 可能返 202(入队),也可能返 200
    (CAS 输给并发调用、租户已在 `suspending`);同一个 `DELETE /tenants/{id}` 有一条受理
    202、一条「别人已拥有这次删除」202、还有一条同义的 200。**只在部分路径出现的字段
    等于不可用** —— 这不是推演,是 #562 真机 invoke 抓出来的同一个缺陷(原文见
    `create_tenant` 队列路径那条 202 上的注释)。而客户面契约
    `engineering/customer-requirements/lifecycle-deadline-contract.md` 已经写着
    「同一次操作在响应体、消息体、租户行上的死线是同一个值」—— 不补齐这几条出口,
    那句话就是假的。

    取值优先级**刻意**是「受理路径刚算出的值 → 行上的值」:
      · `deadline_epoch` 是本次受理算定、同时写进消息体与租户行的那一个值,同源无歧义;
      · 幂等返回路径上这次操作**是别人发起的**,本地没有那个值,只能从行上读。
    两个都没有时**不加字段**,而不是加一个 `None` —— 契约口径是「字段在即可信」,塞 null
    会让调用方写出 `if "suspend_deadline" in resp` 却拿到一个不能做算术的值。

    `action` 不在八档词汇表里时(stop/pause/resume/reset —— 它们没有客户承诺的
    死线)**原样返回**:`deadline_attr()` 对它们会 raise,在这里挡住,免得每个调用点各写
    一遍 `if action in DEADLINE_ACTIONS`。行上的值坏掉时也只是不加字段 —— 与
    `create_deadline.is_expired` 对非数值的处置同一口径,一次手工改库不该把一条本来
    正常的幂等返回变成 500。
    """
    if action not in create_deadline.DEADLINE_ACTIONS:
        return body
    attr = create_deadline.deadline_attr(action)
    raw = deadline_epoch if deadline_epoch is not None else (row or {}).get(attr)
    if raw is None:
        return body
    try:
        body[attr] = int(raw)
    except (TypeError, ValueError):
        print(f"_with_action_deadline: {action} 死线值不可解析,响应体不带该字段: {raw!r}")
    return body


def _suspend_finalize_txn(tenant_id, host_id, release_id, vcpu, mem_mb,
                          backup_key, prev_status):
    """#438 —— suspend 收尾:扣 host 账本 + 翻 suspended + 消费一次性令牌,**一个
    TransactWriteItems**。返回 `_release_capacity_reservation` 的同款三态。

    核心不变量:**令牌还在 ⟺ 账本还没扣**(两者在同一事务里,全有或全无)。于是"账本
    动过没有"这个事实在崩溃后从 DDB 读得出来 —— 这正是 health_check/handler.py 的 #469
    P1 说明逐字指出的、suspend 路径缺失的互斥锚(`scheduling._release_slot` 不幂等,只有
    下溢守卫、无锚;#412 之所以能安全补扣是因为有 capacity_reservation_id)。

    bb 基线的形态是两次独立写:`scheduling._release_slot`(best-effort、两个 except 只
    print)后【无条件】翻 suspended。所以"释放确失败仍 finalize、容量永久泄漏"是代码结构
    决定的,不是竞态 —— 合成一个事务正是把那条结构性泄漏路径消掉。

    TransactItems 顺序必须是 [0]=host 账本、[1]=tenant 令牌+状态,`_classify_release_cancel`
    按位次判三态。事务不接受对同一 key 的两个操作,所以状态提交只能并进 tenant 那一项。
    """
    now = utils._now()
    txn_items = [
        {
            "Update": {
                "TableName": clients.hosts_table.table_name,
                "Key": {"instance_id": host_id},
                "UpdateExpression": (
                    "SET used_vcpu = used_vcpu - :v, "
                    "used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one"
                ),
                "ConditionExpression": (
                    "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                ),
                "ExpressionAttributeValues": {":v": vcpu, ":m": mem_mb, ":one": 1},
            }
        },
        {
            "Update": {
                "TableName": clients.tenants_table.table_name,
                "Key": {"id": tenant_id},
                # 终态字段与 bb 基线的 finalize update 逐字一致(status/restore_backup_key/
                # suspended_at/suspended_from/updated_at + 清卡死标记),只是多消费令牌、
                # 多清阶段快照 suspend_backup_key。
                # 刻意【不】REMOVE host_id/vm_num:bb 的 suspended 租户保留旧坐标,改它要
                # 扫遍所有读 suspended 租户坐标的调用方,属独立取舍,不混进本次修复。
                "UpdateExpression": (
                    "SET #s = :suspended, restore_backup_key = :bk, suspended_at = :t, "
                    "suspended_from = :prev, updated_at = :t "
                    "REMOVE suspend_release_id, suspend_backup_key, "
                    "lifecycle_stuck_at, lifecycle_stuck_reason, "
                    "lifecycle_stuck_vm_dir, lifecycle_stuck_fc_alive, "
                    "updated_at_stuck_seen, lifecycle_rollback_deferred_until, "
                    "lifecycle_probe_attempted_at, lifecycle_prev_status"
                ),
                "ConditionExpression": "#s = :suspending AND suspend_release_id = :rid",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":suspended": "suspended",
                    ":suspending": "suspending",
                    ":bk": backup_key,
                    ":prev": prev_status,
                    ":t": now,
                    ":rid": release_id,
                },
            }
        },
    ]
    try:
        clients.hosts_table.meta.client.transact_write_items(TransactItems=txn_items)
        return _REL_CONSUMED
    except Exception as e:  # noqa: BLE001 — 判定(含未知错误保守当 retry)在分类器里
        return _classify_release_cancel(e, tenant_id, "suspend #438")


def _tenant_suspend(tenant_id, item, host_guard="", _lifecycle_ctx=None):
    """#422 — 休眠一个 running/stopped 租户:无条件同步备份数据盘到 S3(fail-closed)→
    停 VM → 释放 host slot → 保留 DDB 记录与 tenant_id,翻 status=suspended,写
    restore_backup_key 供 restore 冷恢复。释放 slot 供新用户调度(休眠的核心目的)。

    fail-closed 语义(no-data-loss 铁律,对齐 delete keep_data=false 的删前备份门):
    备份未确认成功 → 回滚状态、不停 VM、不释放 slot、不删盘、502。只有备份成功才推进破坏性
    步骤。stop-vm 失败 → 回滚 + 502(VM 未停不能标 suspended,否则账本失真)。

    _lifecycle_ctx(#547 兄弟路径)—— `_tenant_action_inner` 的那份 ctx,与
    `_delete_tenant_inner` 同款 kwarg。suspend 自 #469(⑭)起进了
    `_FENCED_LIFECYCLE_ACTIONS`,于是 `tenant_action` 的 5xx wrapper(:5114)对本函数的
    【每一个】502 都 hold_lifecycle_fence=True → 租约扣 1800s。本函数的 502 分两类,
    只有前一类该放掉,分界线是 `stop-vm`(第一个破坏性命令):在它【之前】的失败可证明
    host 侧一步未动,在它【之后】的失败结果未知、必须继续扣住(否则 restore 会与在途
    stop 交错)。
    """
    prev_status = item.get("status")
    # #438 重投续做 —— 阶段持久化的【消费处】。
    #
    # 一次 suspend 若崩在「已过备份门、令牌与备份 key 都已落库」之后,status 停在
    # suspending。此前入口只认 running/stopped,于是重投一律 409、消息被 ack,租户永久卡在
    # suspending:VM 已停、盘已删、数据在 S3,却既不能 restore(它只认 suspended)也不能
    # 再 suspend。那不是容量泄漏(账本仍算着它、行也还在),而是**永久中间态** ——
    # HIGH-RISK-CHANGES 第 3 条不变量,也正是本 issue 验收第一条要求的「崩在任意中间点,
    # 重投/reaper 能收敛到稳定态」。
    #
    # 只落库不消费等于「只建桥墩不铺桥面」:`suspend_backup_key` 存在的唯一意义就是让
    # 后来者能把剩下的步骤做完,所以消费处必须与它同一个 MR 落地。
    #
    # 续做的前提逐条都是【库里读出来的事实】,缺任何一条就不续做(fail-closed,退回原
    # 409 让人工介入,绝不猜):
    #   · status 就是 suspending(我们要续的正是这一次);
    #   · suspend_release_id 在 —— 令牌在 ⟺ 账本还没扣(同事务契约),所以收尾仍是安全的;
    #   · suspend_backup_key 在 —— 盘可能已删,绝不能重跑备份(那会失败或产出错对象);
    #   · lifecycle_prev_status 是 running/stopped —— 收尾要把它写进 suspended_from,
    #     猜一个会造出「无 VM 却 running」的谎报(#469 记过这条)。
    if (
        prev_status == "suspending"
        and item.get("suspend_release_id")
        and item.get("suspend_backup_key")
        and item.get("lifecycle_prev_status") in ("running", "stopped")
        and item.get("host_id")
    ):
        print(
            f"suspend #438: resuming interrupted suspend for {tenant_id} "
            f"(rid={item['suspend_release_id']})"
        )
        # rollback=None:上一次已过备份门,host 侧动到哪一步【不可知】,回滚成活跃态可能
        # 谎报一个已停/已毁的租户还活着(#268)。按本文件"歧义时留中间态"的原则不回滚。
        # 破坏性步骤本身幂等(stop-vm 对已停的 VM、rm -rf 对已不存在的目录都是 no-op),
        # 所以重跑它们是安全的,也是原注释承诺的 "kept status=suspending for re-drive"。
        return _suspend_finish(
            tenant_id,
            item,
            host_guard,
            item["suspend_release_id"],
            item["suspend_backup_key"],
            item["lifecycle_prev_status"],
            None,
        )
    if prev_status not in ("running", "stopped"):
        return utils._resp(
            409,
            {
                "error": f"can only suspend a running/stopped tenant (current: {prev_status})"
                + (
                    # #679 —— suspend_failed 没有 VM(host 已亡),重试 suspend 无物可挂;
                    # 按备份分档给出唯一有意义的下一步,免得客户端对着 409 猜。
                    (
                        " This tenant's host died mid-suspend; restore it (data rolls "
                        "back to the backup taken then) or delete it."
                        if item.get("suspend_backup_key")
                        else " This tenant's host died before its suspend backup "
                        "completed; data is not recoverable — delete it."
                    )
                    if prev_status == "suspend_failed"
                    else ""
                ),
                "id": tenant_id,
            },
        )
    host_id = item.get("host_id")
    if not host_id:
        return utils._resp(
            400, {"error": "tenant has no host (still pending?)", "id": tenant_id}
        )

    # #438 —— 本次 suspend 的一次性容量释放令牌,与下面那次 CAS 写在同一条 update。
    # 每次 suspend 新生成:迟到的旧释放匹配不上新令牌(#412 `_reservation_id` 防 ABA 的
    # 同一条理由 —— host_id 会被复用,随机令牌不会)。
    release_id = secrets.token_hex(16)
    # 并发闸:CAS prev → suspending,单赢家。输家(并发 suspend/其他 op 已改)幂等/409。
    if not _cas_status(tenant_id, prev_status, "suspending", stash_prev=True,
                       stash_token=("suspend_release_id", release_id)):
        cur = (
            clients.tenants_table.get_item(
                Key={"id": tenant_id}, ConsistentRead=True
            ).get("Item")
            or {}
        )
        cur_s = cur.get("status")
        if cur_s in ("suspending", "suspended"):
            # #565 G6 —— `suspending` 是**在飞**态:那次 suspend 归赢家,死线在它写的行上。
            # `suspended` 已是终态、行上那个死线属于已完成的那次,`_with_action_deadline`
            # 照样带出来 —— 调用方靠 `status` 判在飞与否,靠死线判"该等到几点",两者不冲突。
            return utils._resp(
                200,
                _with_action_deadline(
                    {"id": tenant_id, "status": cur_s},
                    create_deadline.ACTION_SUSPEND,
                    row=cur,
                ),
            )
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_PREEMPTED)
        return utils._resp(
            409,
            {"error": f"tenant is {cur_s}; suspend lost the race", "id": tenant_id},
        )

    def _rollback():
        # 回滚 suspending → prev(抄 _abort_restore_status 的 CAS 形态,只回滚自己翻的态)。
        _cas_status(tenant_id, "suspending", prev_status, clear_stuck=True)

    def _release_fence_no_host_work():
        """#547 兄弟路径 —— 本支可证明【一步破坏性命令都没下发】,放掉自己的租约。

        只允许在 `stop-vm`(下方 `_stop_ok, _stop_rc = ssm_dispatch._ssm_run`,:3985)
        【之前】的失败分支调用。在它之后调用是错的:命令已下发且结果可能未知,放掉围栏
        会让一次 restore/start 与在途 stop 交错(:2803 与 ㉘ 都记过这条)。
        """
        if _lifecycle_ctx is not None:
            _lifecycle_ctx["release_lifecycle_fence_on_error"] = True

    # 1) 无条件同步备份(复用 delete 备份门的 invoke+双层解析形态,去掉 keep_data 触发条件)。
    try:
        # #565 G1-a —— 与 _force_backup_sync 同一份 Config(理由见
        # core/clients.BACKUP_SYNC_INVOKE_CONFIG)。本函数是那条链的内联副本,
        # 60s 截断在这里的后果是 suspend 白白回滚一次本已成功的备份。
        lambda_client = boto3.client(
            "lambda", config=clients.BACKUP_SYNC_INVOKE_CONFIG
        )
        resp = lambda_client.invoke(
            FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
            InvocationType="RequestResponse",  # SYNC:数据落 S3 才继续破坏性步骤
            # pre_delete=True:绕过 backup "只备 running" 守卫(CAS 已把 status 翻成
            # suspending,与 delete 翻 deleting 同源;不带这个信号 backup 会 no-op 拒掉)。
            Payload=json.dumps(
                {
                    "tenant_id": tenant_id,
                    "pre_delete": True,
                    # #565 G1 —— suspend 是 180s 档,执行段 120s 里给备份 90s(stop-vm 拿 30s)。
                    "ssm_budget_sec": create_deadline.exec_step_sec(
                        create_deadline.ACTION_SUSPEND, "backup"
                    ),
                }
            ).encode("utf-8"),
        )
        invoke_ok = resp.get("StatusCode", 500) == 200 and "FunctionError" not in resp
        payload_ok = False
        # 必须先初始化:invoke 失败或 JSON 解析抛异常时下面两条路径都会读 result
        # (取 vm_left_paused / 取 backup_key),而那两种情形下它原本【从未被赋值】——
        # 读一个未绑定的名字会抛 NameError,被外层 except 吞成"backup error"并照旧回滚,
        # 于是 ㉘ 那道守卫在最需要它的失败路径上反而不生效。
        result = {}
        payload_err = "unparseable backup response"
        if invoke_ok:
            try:
                raw = resp["Payload"].read()
                result = json.loads(raw) if raw else {}
                payload_ok = result.get("success") is True
                if not payload_ok:
                    payload_err = result.get("error") or "backup reported failure"
            except Exception as pe:  # noqa: BLE001 — 解析失败即 fail-closed
                payload_err = f"backup response parse error: {pe}"
        else:
            payload_err = "backup invoke failed (StatusCode/FunctionError)"
        if not payload_ok:
            # ㉘ 备份失败时也要先问一句「VM 被留在 Paused 了吗」(#469,第二十三轮那条判据的
            # 第四个面 —— 应用该判据、把所有失败分支数一遍时自己找到的)。
            #
            # 备份本身不删任何东西,所以这一支的默认动作(回滚成 running/stopped)在绝大多数
            # 失败下是对的。但有一种失败【恰恰是 resume 没成功】:那时 VM 是 Paused 的,
            # 回滚成 running 就是 #268 的谎报 —— 租户看着活着,实际冻着。
            #
            # 而 **reaper 救不了这一种**:它的 fc_alive 是进程存活检查,一个 Paused 的
            # Firecracker 进程照样活着,所以它会得出同样的错误结论。这个事实只有
            # backup-data.sh 知道,故由它打稳定哨兵 OC_BACKUP_VM_LEFT_PAUSED,
            # backup Lambda 转成 vm_left_paused 回传(通道复用它已经在读的那份 stdout)。
            #
            # #547 兄弟路径 —— 这一支【刻意不】调 _release_fence_no_host_work():
            # 它是本函数四个"stop-vm 之前"的 502 里唯一一个 host 侧【真的动过且没回滚】
            # 的(VM 被留在 Paused),而且它有意保留 status=suspending 让 stuck-lifecycle
            # 告警看得见。扣住围栏正是要挡住一次 restore/start 撞上一台冻住的 VM。
            if isinstance(result, dict) and result.get("vm_left_paused"):
                _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_BACKUP_FAILED)
                return utils._resp(
                    502,
                    {
                        "error": "suspend backup failed AND left the VM PAUSED (the host "
                        "script exhausted its bounded resume retries). Rolling back to an "
                        "active status would report a frozen tenant as live, and the reaper "
                        "cannot detect this — its liveness probe only checks that the "
                        "Firecracker process exists, and a paused one does. status=suspending "
                        "is kept so it stays visible to the stuck-lifecycle alarm; resume it "
                        "on the host (see the OC_BACKUP_VM_LEFT_PAUSED log line for the exact "
                        "curl) or force-delete it, then retry.",
                        "backup_error": payload_err,
                        "vm_left_paused": True,
                        "id": tenant_id,
                    },
                )
            _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_BACKUP_FAILED)
            _rollback()
            # 备份【确定】失败(backup Lambda 明确返 success!=True)且已回滚:host 侧
            # 一步未动,文案又写着 "Retry" —— 必须放掉围栏,否则那句 Retry 是谎话。
            _release_fence_no_host_work()
            return utils._resp(
                502,
                {
                    "error": "suspend backup failed; aborting to avoid data loss. Retry.",
                    "backup_error": payload_err,
                    "id": tenant_id,
                },
            )
        backup_key = result.get("backup_key") or result.get("key") or ""
    except Exception as e:  # noqa: BLE001
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_BACKUP_FAILED)
        _rollback()
        # 同上:stop-vm 之前,host 侧无破坏性动作,放掉围栏让调用方真能重试。
        #
        # 注:这一支也是 ReadTimeout 的落点(裸 boto3.client("lambda") 吃 botocore
        # 默认 read_timeout=60,而 backup Lambda timeout=900)。那种情况下 host 上的
        # backup 脚本【可能仍在跑】,于是 _rollback() 的活跃态是乐观的。但那是
        # 「模糊超时被当成确定失败」的问题,归 #565 G1-a(它要给这三处同步 invoke 显式
        # 传 Config),不在本 issue 范围。围栏该不该放与它无关:本操作返回后不再写任何
        # 东西,而 host 侧的并发由 backup-data.sh 自己持的 per-tenant flock 挡
        # (oc-launch-<tid>.lock),从来不是靠这把围栏挡的。
        _release_fence_no_host_work()
        return utils._resp(
            502,
            {"error": f"suspend backup error ({e}); aborting.", "id": tenant_id},
        )

    # #422 FINDING-5 修复(apse1 真机实测 + codex 三轮独立评审):必须在【停 VM / 删盘之前】
    # 确认拿到【本次】备份的可用 S3 key,否则删盘后才发现无备份 → restore 无从恢复 →
    # 数据永久丢失(no-data-loss 违规)。根治在 backup Lambda 侧:backup-data.sh 把真实 S3
    # key echo 到 stdout,backup Lambda 解析并回传 result["backup_key"](见
    # deploy/lambda/backup/handler.py backup_tenant),故上方 result.get("backup_key") 即
    # 【本次】产物的权威 key。
    # 【绝不用 _resolve_backup 兜底】(codex finding):它取 tenant 的 latest 历史对象,若本次
    # backup 伪成功却没生成对象、而该租户有旧备份,会拿【旧 key】→ 删当前盘 → restore 到旧
    # 数据 → 新增量永久丢失。既然 backup 已回传本次真实 key,无 key 只能说明备份产物确实不存在
    # (backup 侧已对"报成功但无 key"做了 fail-closed),这里直接 fail-closed,不回退旧备份。
    if not backup_key:
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_BACKUP_FAILED)
        _rollback()
        # 同上:仍在 stop-vm 之前,已回滚,文案写着 "Retry" —— 放掉围栏。
        _release_fence_no_host_work()
        return utils._resp(
            502,
            {
                "error": "suspend aborted: backup reported success but returned no "
                "backup_key for this run; refusing to stop/delete the VM without a "
                "fresh restorable backup (avoid data loss). Retry.",
                "id": tenant_id,
            },
        )

    # #438 阶段持久化 —— 把【本次】备份的 S3 key 落库,位置必须在这里:backup_key 已
    # fail-closed 证明非空,而一步破坏性动作都还没下发。
    #
    # 为什么必须落库:收尾要把它写进 restore_backup_key(restore 唯一的恢复入口),而在
    # bb 上这个值只存在于本次 Lambda 的【内存】里。崩在 stop-vm/rm -rf 与 finalize 之间时,
    # 盘已删、数据在 S3,但没有任何人知道对象的 key —— 后续收敛者(重投/reaper)无从写
    # restore_backup_key,租户要么永久卡 suspending,要么被收敛成一个查不到备份的
    # suspended(而 restore 的 `_resolve_backup` 兜底会取【历史 latest】,那正是 #422
    # FINDING-5 判定为"新增量永久丢失"的路径)。形态抄同路径现成的 predelete_backup_at。
    #
    # 写失败 → 回滚 + 502,**一步破坏性动作都不做**(fail-closed,与上面三个备份失败分支
    # 同款:此刻 host 侧一步未动,所以放掉围栏、文案写 Retry 是诚实的)。
    # 刻意【不写 updated_at】:它是 reaper 用来算"进中间态多久"的时钟
    # (health_check/handler.py 读 t["updated_at"] 判 elapsed,并用它做 `updated_at = :seen`
    # 围栏),在这里 restamp 会静默把卡死超时往后推一整轮。
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET suspend_backup_key = :bk",
            ConditionExpression="#s = :suspending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":bk": backup_key,
                ":suspending": "suspending",
            },
        )
    except Exception as e:  # noqa: BLE001 — CCF(状态被别的 op 改走)也走这里
        # 两种失败合成一支处理:CCF 时 _rollback() 自身条件在 #s = :suspending,已被改走
        # 则它是 no-op(不会踩别人的状态);瞬时失败时回滚生效。502 让队列重投 ——
        # 重投读到 running/stopped 就干净重跑,读到别人写的态就在入口 409/200 收敛。
        # #565 G3 接线:落原因必须是本分支的**第一件事**(前面任何一条抛异常,承诺的原因就
        # 永远落不上,调用方拿到的是未分类异常而不是可归因的 502)。所以它排在 print 之前。
        # 处置可以合并,**归因不能**:CCF 的含义是"状态被别的 op 改走"= 被抢占;其余异常
        # 才是系统故障。`REASON_SYSTEM` 的文档明写「不许用它兜底『不知道为什么』」,
        # 所以这里分开判,不一律 system_error。
        _mark_fail_reason(
            tenant_id,
            "suspend",
            create_deadline.REASON_PREEMPTED
            if isinstance(
                e,
                clients.tenants_table.meta.client.exceptions
                .ConditionalCheckFailedException,
            )
            else create_deadline.REASON_SYSTEM,
        )
        print(f"suspend #438: persist backup_key for {tenant_id} failed: {e}")
        _rollback()
        _release_fence_no_host_work()
        return utils._resp(
            502,
            {
                "error": "suspend aborted: could not persist this run's backup key "
                "before the destructive steps; nothing was stopped or deleted. Retry.",
                "id": tenant_id,
            },
        )

    return _suspend_finish(
        tenant_id, item, host_guard, release_id, backup_key, prev_status, _rollback
    )


def _suspend_finish(tenant_id, item, host_guard, release_id, backup_key,
                    prev_status, rollback):
    """#438 —— suspend 的后半段:停 VM → 删本地盘 → 归还占号 → 令牌化收尾。

    从 `_tenant_suspend` **机械搬迁**出来(函数体逐字不变,只把它原先从闭包里读的
    `host_id` / `release_id` / `backup_key` / `prev_status` / `_rollback` 变成参数)。
    搬迁的唯一理由:`_tenant_suspend` 顶部的【重投续做支】必须跑完全同一段破坏性步骤 +
    收尾,抄一份必然漂移 —— 这一段里 `host_guard` 的前后夹、rc=89 专属码、子 shell 语义
    是踩过十几轮评审收敛的,复制是它最危险的处置方式。

    `rollback` 是可调用或 None。None 表示**这一支绝不回滚**:重投续做时无法证明 host
    侧一步未动(上一次已过备份门),按本文件"歧义时留中间态"的原则保持 suspending。
    """
    host_id = item.get("host_id")

    def _rollback():
        if rollback is not None:
            rollback()

    # #679 —— 显式刹车:发 stop-vm(本链第一个改变 VM 生死的副作用)之前,确认本轮没被
    # 围栏判死。围栏的 suspend 在飞档已退为记录者(不翻 failed),原先靠 failed 撞 CCF 的
    # 隐式刹车不复存在,这一读就是它的等价替换。判死 → 就地停手:**不回滚不清理**
    # (VM 生死未知,回滚可能谎报 #268),保持 suspending 交 reaper 的补偿矩阵归位。
    # 502 而不是 4xx:consumer 只对 >=500 留消息重投;重投续做支会再过一次这道刹车,
    # 幂等地继续等待归位,不会空转副作用。
    # 位置在 `_suspend_finish` 开头 = 两个调用方(主路径 / 重投续做支)都被覆盖。
    #
    # **TOCTOU 窗口的时序论证**(Codex 实现评审 #2 质疑,裁决为不成立,论证留档):
    # 本读之后、围栏恰好写下判死之前发出的 stop,正是补偿矩阵静默期定义里的「判死那一刻
    # 已在飞的最后一条命令」:stop ≤50s、其后的 rm ≤30s(都被 executionTimeout 强杀),
    # 收尾事务是 DDB 条件写(与矩阵归位单赢家)。150s 静默期 > 50+30+余量,矩阵在
    # enforced+150s 后看到的必然是落定后的终局 —— 窗口被静默期覆盖,无需原子仲裁。
    # 唯一逃逸形态「执行者赢下收尾 → suspended + 判死记录残留」由 `deadline_enforced_for`
    # 轮次锚化解(残留对新一轮自动无效)。
    if _deadline_brake_engaged(
        tenant_id, "suspend", item.get(create_deadline.deadline_attr("suspend"))
    ):
        return utils._resp(
            502,
            {
                "error": "this suspend attempt was fenced by its deadline before the "
                "destructive step; leaving status=suspending untouched for the "
                "reaper compensation matrix to settle (no rollback here — the VM's "
                "true state is the reaper's to probe, not this worker's to guess).",
                "id": tenant_id,
            },
        )

    # 2) 停 VM(关键副作用,fail-loud;抄 delete #268 语义:失败回滚+502,不释放 slot)。
    # #520 C2 + #494:前置 S3 自愈 —— 既有 host 上的 stop-vm.sh 可能缺失或是不认
    # guest flush 契约的旧版。这里的失败路径与 stop-vm 本身
    # 失败一致(回滚 + 502 + 不释放 slot),所以自愈失败 exit 1 即可,语义不变。
    vm_num = int(item.get("vm_num", 1))
    _suspend_heal = ssm_dispatch.host_script_self_heal(
        ("stop-vm.sh",), "oc:suspend", freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED")
    )
    # guard 夹在破坏性动作【前后】各一次,与 delete 主路径同款(:3152/:3158 那对):
    # 前置确认本次操作仍持租约(被抢占 exit 79 / 读不到 fence exit 78 都 fail-closed),
    # 后置确认整个动作期间没被抢占。host_guard 为空串时退化成原行为(未走 fence 的调用方)。
    #
    # ⑱ 后置 guard 必须有【专属退出码】(codex 独立复审第十一轮)。
    #
    # 我第九轮加上后置 guard 时引入了一条新的谎报路径:破坏性动作【成功】、而后置 guard
    # 失败(被抢占)时,整条命令 rc≠0 → 下面按"stop-vm 失败"处理 → `_rollback()` 把状态
    # 回成 running/stopped —— 而 VM 确实已经停了、盘可能已经删了。那正是 #268 禁止的
    # 「row 说活着、实际没有」的谎报,而且是我为了加固而【新造】出来的。
    #
    # 难点:前置与后置 guard 都 exit 78/79,单看 rc 分不出"动作没跑"与"动作跑完了但之后
    # 被抢占"。所以给后置 guard 套一层 `|| exit 89`(89 全仓未占用,已 grep 确认),
    # 于是三种情形可区分:
    #   rc 78/79 → 前置就被挡住,破坏性动作【从未执行】→ 回滚是安全的;
    #   rc 89    → 动作已执行、之后被抢占 → **绝不回滚**,保留 suspending 交给 reaper /
    #              SQS 重投对账(与 delete 路径"歧义时留中间态"同一条原则:
    #              回 running 的前提是"VM 确实还在跑",而这里恰恰不成立);
    #   其它 rc  → 动作自身失败(stop-vm 的 rc),回滚安全。
    #
    # ⑲ 后置 guard 必须跑在【子 shell】里(codex 独立复审第十七轮)。
    #
    # 上面 ⑱ 那道 `|| exit 89` 此前【一直是空转的】,而且是我自己第十轮的修复造成的:
    # 第十轮为了修 `&&` 结合律,把 host_guard 的返回值包成 `{ body; }`(组命令)。
    # 组命令在【当前 shell】里执行,所以 body 里的 `exit 79` 终结的是整个 shell ——
    # 外层的 `|| exit 89` 根本轮不到。真 shell 实测:
    #     bash -c 'true && { { exit 79; } || exit 89; }'   → rc=79(不是 89)
    #     bash -c 'true && { ( { exit 79; } ) || exit 89; }' → rc=89
    # 于是"动作已执行"这个信息丢了,rc=79 落进下面"前置就被挡住 → 回滚安全"的分支,
    # ⑱ 想关掉的那条谎报路径原封不动地还在,只是多了一段声称它已关闭的注释。
    #
    # 修法是把后置 guard 放进 `( )`:子 shell 里 `exit` 只终结子 shell,其退出码交给 `||`。
    # 刻意【不改】host_guard 的源头返回值 —— 那个 `{ }` 是第十轮修 `&&` 结合律用的,
    # 6 个调用点都靠它;这里只有后置这一处需要"让 exit 可被捕获"的语义。
    # 外层 `{ }` 也必须留:去掉就变成 `(cmd && (guard)) || exit 89`,cmd 自身失败时
    # 也会报成 89 —— 那是把结合律的坑换了个方向再踩一次。
    #
    # ⚠ 这条为什么被吞了六轮:原测试断言的是【源码里有 `|| exit 89` 这个字符串】。
    # 字符串在,行为不在。源码级字符串断言证明不了 shell 语义,`{ }` 与 `( )` 的差别对它
    # 完全不可见。故本轮补的是【真 shell 行为测试】(见 test_422 的 four_rc_cases)。
    _sg_pre = f"{host_guard} && " if host_guard else ""
    _sg_post = f" && {{ ( {host_guard} ) || exit 89; }}" if host_guard else ""
    # 后置 guard 被抢占的专属码。取个名字而不是在两处判断里各写一个 89 —— 本函数里
    # 两处破坏性动作(stop-vm / rm -rf)都要认它,写死两遍必然漂移。
    # 作用域够用就放函数内:它只被这两处消费,且与上面那行 `_sg_post` 的拼装同源。
    _SG_POST_PREEMPTED = 89
    # #565 G1 —— suspend 的执行段 120s 拆成「同步备份 90 + stop-vm 30」。这一步取那个 30。
    # 它与 `_ssm_run` 的默认值恰好同值,但**取值来源不同**:默认值是"没人指定时的兜底",
    # 而这里是"预算表分给这一步的额度"。写成显式取值,预算表一改它就跟着改;靠默认值同值
    # 只是巧合,下一个改默认值的人不会知道 suspend 的死线依赖它。
    _stop_ok, _stop_rc = ssm_dispatch._ssm_run(
        host_id,
        f"{_suspend_heal} && {_sg_pre}OC_STOP_FLUSH_MODE=require "
        f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        f"{_sg_post}",
        timeout=create_deadline.exec_step_sec(
            create_deadline.ACTION_SUSPEND, "stop-vm"
        ),
        want_rc=True,
    )
    if not _stop_ok and _stop_rc == _SG_POST_PREEMPTED:
        # 动作已执行、之后被抢占:VM 可能已停。回滚会谎报"还在跑"(#268),故留 suspending。
        # 返 502 而不是 4xx:队列 consumer 只有 code>=500 才把消息留队列重投
        # (handler.py:1651),而这条现场需要重投或 reaper 来收敛。
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_PREEMPTED)
        return utils._resp(
            502,
            {
                "error": "suspend was preempted by another lifecycle operation AFTER "
                "stop-vm already ran; the VM may already be stopped, so rolling back to "
                "an active status would misreport a live tenant. Kept status=suspending "
                "for the reaper / redelivery to reconcile.",
                "id": tenant_id,
            },
        )
    if not _stop_ok:
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_HOST_UNREACHABLE)
        # ⚠ 这里【故意保持 bb 基线原样】(失败即回滚),不做"只在能证明未执行时才回滚"。
        #
        # codex 第二十三轮在这里抓出一个真问题:「SSM 报失败」不等于「脚本没跑」——
        # rc is None 表示压根没拿到 invocation 结果(超时/传输异常),脚本可能已经跑完只是
        # 回执丢了;其它 rc 表示脚本确实跑了并返回非零,它可能已经杀掉 FC 只是后续某步失败。
        # 两种情形下回滚成 running/stopped 都可能谎报,而且回成活跃态后它就从 reaper 的
        # 视野里消失了(reaper 只扫中间态)。
        #
        # **但这段失败处置属于 #422 的既有逻辑,不属于 #469** —— bb 基线(aa18bd8f)上就是
        # `if not _ssm_run(...): _rollback()`,本批次只在它前面加了 host_guard 与 rc==89 的
        # 分支(那两样是本批次自己引入的,所以留着)。改动既有失败语义会改变现网行为
        # (原本立刻回滚的租户变成要等一轮 reaper),那是独立的取舍,该由排期的人决定。
        # 已单独记录,不混进这个 P0 + 命中 IaC 安全红线的分支。
        #
        # 判据不是"缺陷有多严重",而是"这段代码是不是本批次引入或改动的"。
        _rollback()
        return utils._resp(
            502,
            {
                "error": "stop-vm failed (SSM timeout/error); suspend aborted, slot not "
                "released. Retry.",
                "id": tenant_id,
            },
        )
    # best-effort 摘 DNAT(与 stop 动作一致:失败不阻断,orphan-reap 兜底)。
    _hp = item.get("host_port", 0)
    _gip = item.get("guest_ip", "")
    if _hp and _gip:
        ssm_dispatch._ssm_run(host_id, f"({_dnat_remove_all_cmd(_hp, _gip)} || true)")

    # 3) 删本地 VM 目录回收 host 磁盘(休眠的核心目的之一是腾磁盘,不只是账本 slot)。
    # #422 codex-blocker:stop-vm.sh 只杀 FC 进程、保留 data.ext4/VM 目录 → 只停不删=磁盘
    # 没回收,且 restore 若回同一 host 会撞残留 data.ext4 绕过 S3 恢复。此刻数据已确认在 S3
    # (备份 payload_ok=True),删本地盘不丢数据。fail-loud(抄 delete #268):rm 失败=磁盘泄漏,
    # 回滚状态待重投(status 仍 suspending,VM 已停,重投补删幂等),不推进 suspended。
    # tenant_id 经 shlex.quote 进 root shell 防注入(纵深:虽已过 registry 正则)。
    _q_vmd = shlex.quote(f"/data/firecracker-vms/{tenant_id}")
    _rm_ok, _rm_rc = ssm_dispatch._ssm_run(
        host_id, f"{_sg_pre}rm -rf {_q_vmd}{_sg_post}", want_rc=True
    )
    if not _rm_ok and _rm_rc == _SG_POST_PREEMPTED:
        # 盘可能已经删了。这条比 stop-vm 那条更不能回滚 —— 回滚成活跃态而盘已毁,
        # 就是 #268 说的"谎报已毁租户存活"。留 suspending 让重投补做剩余步骤(幂等)。
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_PREEMPTED)
        return utils._resp(
            502,
            {
                "error": "suspend was preempted by another lifecycle operation AFTER the "
                "disk reclaim ran; the local disk may already be gone (data is safe in "
                "S3), so rolling back to an active status would misreport a destroyed "
                "tenant as live. Kept status=suspending for re-drive.",
                "id": tenant_id,
            },
        )
    if not _rm_ok:
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_HOST_UNREACHABLE)
        # ⚠ 同上,这里也【故意保持 bb 基线原样】(失败即回滚)。
        #
        # codex 第十九轮指出:走到这一行时 stop-vm 已经成功、VM 确实停了,而 rm 的 SSM 失败
        # 不代表 rm 没跑,盘可能已删或删了一半 —— 回滚成活跃态是 #268 的谎报,严重那支是
        # "谎报一个已毁租户存活"。而且基线那段文案写着 "Kept status=suspending for re-drive"
        # 却在回滚,**文案与行为本来就打对台**。
        #
        # 但这同样是 #422 的既有失败语义(基线上就是 `_rollback()` + 那句文案),不属于 #469。
        # 本批次在这里只加了 rc==89 那一支(后置 guard 是本批次引入的),那一支留着。
        # 已单独记录:包括"文案与行为矛盾"这一点也该在那个 issue 里一起改。
        _rollback()
        return utils._resp(
            502,
            {
                "error": "suspend disk reclaim (rm -rf) failed (SSM timeout/error); VM "
                "stopped and backup safe in S3, but local disk not reclaimed. Kept "
                "status=suspending for re-drive.",
                "id": tenant_id,
            },
        )

    # 4) 归还物理占号(ps_<n>)。它【不】进下面那个事务,与 scheduling._release_slot 里
    # "占号先还、且独立于容量释放的结果"的既有分工一致:它有自己的 owner 条件
    # (ps_<n> = tenant_id),是一个独立的幂等单元;并进事务会让占号的归还被账本项的
    # 下溢守卫连带取消 —— 号还被占着时 reaper 也救不了(owner 仍在役)。
    # `is not None` 守卫保持与旧 _release_slot 内部那道守卫逐字等价(存量行可能没有 ps_*)。
    _phys_num = item.get("phys_vm_num", vm_num)
    if _phys_num is not None:
        scheduling.release_phys_slot(host_id, _phys_num, tenant_id)

    # 5) 终态 = 【一个事务】:扣 host 账本 + 翻 suspended + 消费令牌(见
    # _suspend_finalize_txn)。backup_key 在停 VM/删盘之前已 fail-closed 保证非空
    # (#422 FINDING-5),此处直接写入,不会是空值。
    _rel = _suspend_finalize_txn(
        tenant_id, host_id, release_id,
        int(item.get("vcpu", 0)), int(item.get("mem_mb", 0)),
        backup_key, prev_status,
    )
    print(
        f"suspend #438: finalize tenant={tenant_id} host={host_id} "
        f"rid={release_id} result={_rel}"
    )
    if _rel == _REL_RETRY:
        # 令牌仍在 ⟹ 账本一定【没扣】(两者同事务,全有或全无)。所以保持 suspending 返
        # 502 是安全且必须的:租户仍名义上占着自己的 slot(不是泄漏,是尚未归还),队列
        # 重投 / reaper 可以凭令牌把它收敛掉。绝不 finalize —— 那正是 bb 基线把"释放确
        # 失败"固化成容量永久泄漏的那一步。
        #
        # #565 G3 接线 —— ⚠️ **归因值是已知的近似,不是精确匹配**,如实记在这里:
        # 语义上最准的是 `capacity_release_pending`(「破坏性动作已完成、只剩容量账本没
        # 收敛、值得且必须重试」——与本出口逐字相符),但那个值的**已发布客户契约**写着
        # 「只有 delete 有」,`REASONS_FOR` 也没把它放进 suspend 的封闭子集,
        # `assert_reason_valid` 会直接拒。扩子集要改一份**面向客户的**契约文档 +
        # 一条专门钉它 delete-only 的用例,那属于 #565/#564 的契约决策,不该由本卡单方面改。
        # 退而用 `system_error` 的**代价被本卡自己的设计兜住了**:它给客户的建议是"报障、
        # 不必重试",而本出口保留了令牌 ⇒ 队列重投**和** reaper 的 finalize_suspend 都能
        # 自行收敛,不依赖客户重试。所以这里的不精确只会多一张工单,不会像 delete 那条
        # (#565 记过的原缺陷)造成租户永久卡住 + 容量搁浅。
        # **已上报请 #565 owner 决定是否把该值扩进 suspend 子集。**
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_SYSTEM)
        return utils._resp(
            502,
            {
                "error": "suspend could not atomically release capacity (transient); "
                "kept status=suspending with the release token intact so a re-drive "
                "reconciles it. The VM is stopped and the backup is safe in S3. Retry.",
                "id": tenant_id,
            },
        )
    if _rel == _REL_ALREADY:
        # 令牌已不在:要么本次 suspend 的收尾已被另一个写手(重投)完成,要么 status 被别的
        # op 改走。强一致读一次判幂等,不猜(与 delete 路径"歧义时读一次"同款)。
        cur = (
            clients.tenants_table.get_item(
                Key={"id": tenant_id}, ConsistentRead=True
            ).get("Item")
            or {}
        )
        if cur.get("status") == "suspended":
            return utils._resp(200, {"id": tenant_id, "status": "suspended"})
        # rebase 冲突解析(#565 G3 × #438):#565 在【本卡替换掉的那个 CCF except】里落了
        # `preempted`。语义上它对应的正是这里 —— 令牌没了且状态不是 suspended,说明被别的
        # op 抢占了。所以把 #565 的调用点搬到这个等价出口,而不是二选一丢掉一边。
        _mark_fail_reason(tenant_id, "suspend", create_deadline.REASON_PREEMPTED)
        return utils._resp(
            409,
            {
                "error": "suspend finalize lost the release token (status changed "
                "mid-suspend); VM stopped and backup safe in S3 — inspect tenant state.",
                "id": tenant_id,
            },
        )
    audit._publish_event(
        "tenant.suspended", tenant_id, {"backup_key": backup_key, "from": prev_status}
    )
    return utils._resp(200, {"id": tenant_id, "status": "suspended"})


def _restore_endpoint(vm_num):
    """本次 restore 的 (guest_ip, host_port)。**唯一派生处。**

    (供 reaper 转正时【照抄】而不是自己再算一遍),`_tenant_restore` 把它传给 launch-vm。
    两处各写一遍必然漂移,而 guest_ip 漂了就是跨租户网络串号 —— `auth._guest_ip` 的
    docstring 明写它是 /30 编址的 single source of truth,不能再多出第二个口径。"""
    return auth._guest_ip(vm_num), clients.VM_PORT_BASE + vm_num - 1


def _reserve_slot_on(host, vcpu, mem_mb, tenant_id, reservation_id):
    """#422 — 在 host 上原子认领 vm_num + 容量。返回认领的 vm_num,或 None(容量不足/
    输 CAS 竞争)。与 create 路径的内层 `_reserve_slot`(:1642)、migrate 的
    `_reserve_migration_slot`(:2408)同款 CAS,为 restore 路径抽出模块级版本(本仓既有模式:
    同款 CAS 按路径各持一份,避免跨函数闭包依赖)。next_vm_num 单调只增。

    (`restore_reservation_id` 令牌 + `restore_host_id`/`restore_vm_num` 目标坐标)原子落库。

    为什么必须同事务:bb 上目标 host/vm_num 直到最后那次 finalize 才写进租户行,窗口内行里
    的 `host_id` 仍指 suspend 之前那台【旧】host(suspend 收尾刻意不清坐标)。于是崩在窗口
    里时,事后收敛者面对的是 status=restoring、行里坐标指 A、真实预留在 B,而 **B 从未落库**
    —— 既不能回滚(不知道该扣哪台的账本)、不能推进、也不能删(按旧行去删会扣穿旧 host、
    停错 VM、泄漏新 host 的预留)。这正是 health_check/handler.py 的 #469 说明逐字记下的
    「卡死 restoring 没有自动出口」的根因。令牌与账本增量同事务 ⟹ 令牌在 ⟺ 增量已落。

    `reservation_id` 由调用方铸(它负责在撞号/回滚时释放),每次认领一枚新的:迟到的旧释放
    匹配不上新令牌(防 ABA,同 #412 `_reservation_id`)。

    TransactItems 顺序 [0]=host、[1]=tenant 是全文件契约(`_classify_release_cancel` 与下面
    的 `_fresh_host_state` 都按位次读)。"""
    # #430 — 物理内存门必须覆盖【每一个】放置入口,不只 _find_host。restore 路径
    # 指定 host 直接预留,跳过它就等于在水位保护上开了个洞:账本说有余量,而该 host
    # 自报的实测 MemAvailable 已在水位以下。needed_mb 做预测准入(放置后仍须高于水位)。
    if not capacity.mem_ok(
        host,
        clients.MEM_SAFETY_FLOOR_RATIO,
        clients.MEM_CHECK_TTL_SEC,
        int(time.time()),
        needed_mb=mem_mb,
    ):
        return None
    expected = int(host.get("next_vm_num", 1))
    target, occupied = scheduling.next_free_phys_num(
        host["instance_id"], expected, exclude_ids={tenant_id}
    )
    if occupied is None or target is None:
        return None
    _cpu_r, _mem_r = host_profile.ratios(
        host,
        (clients.CPU_OVERCOMMIT_RATIO, clients.MEM_OVERCOMMIT_RATIO),
        clients.OVERCOMMIT_BY_FAMILY,
    )
    cap_v = capacity.allocatable(int(host["total_vcpu"]), _cpu_r) - vcpu
    cap_m = capacity.allocatable(int(host["total_mem_mb"]), _mem_r) - mem_mb
    txn_items = [
        {
            "Update": {
                "TableName": clients.hosts_table.table_name,
                "Key": {"instance_id": host["instance_id"]},
                "UpdateExpression": (
                    "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
                    "vm_count = vm_count + :one, next_vm_num = :next_after, #ps = :tid, "
                    "#s = :a REMOVE idle_since"
                ),
                "ConditionExpression": (
                    "next_vm_num = :expected AND used_vcpu <= :cap_v "
                    "AND used_mem_mb <= :cap_m AND attribute_not_exists(#ps) "
                    # #540 — 污点原子门,与 create 的 _reserve_slot 同款(同一个窗口:
                    # _restore_reserve_slot 选点后、认领前,运维可能正好标记这台)。
                    "AND " + host_taint.NOT_TAINTED_CONDITION
                ),
                "ExpressionAttributeNames": {
                    "#s": "status",
                    "#ps": scheduling.phys_slot_attr(target),
                },
                "ExpressionAttributeValues": {
                    ":v": vcpu, ":m": mem_mb, ":one": 1, ":a": "active",
                    ":tid": tenant_id, ":expected": expected,
                    ":next_after": target + 1, ":cap_v": cap_v, ":cap_m": cap_m,
                    **host_taint.NOT_TAINTED_VALUES,  # #540
                },
                # CCF 时把该 host 的旧值捎回(#475):事务里它落在
                # CancellationReasons[0]["Item"],形状与单表 CAS 的 err_response["Item"]
                # 一致,故 _fresh_host_state 逐字复用。
                "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
            }
        },
        {
            "Update": {
                "TableName": clients.tenants_table.table_name,
                "Key": {"id": tenant_id},
                # #438 —— 连 (guest_ip, host_port) 一起落库。它们由 vm_num 派生,本可让
                # 收敛者自己算;但那等于把 `auth._guest_ip` 的 /30 编址口径复制到
                # health_check(它独立打包、不能 import api/core),而那个口径漂了就是
                # 跨租户网络串号。落库让 reaper 的转正变成【照抄持久化值】,零派生逻辑。
                "UpdateExpression": (
                    "SET restore_reservation_id = :rid, restore_host_id = :rh, "
                    "restore_vm_num = :rvn, restore_guest_ip = :rip, "
                    "restore_host_port = :rport"
                ),
                # `attribute_not_exists(restore_reservation_id)` 防重投对同一租户压第二份
                # 预留(与 dispatch `_reserve_batch_txn` 的同名守卫同源):一个租户行只能记住
                # 一枚令牌,第二份预留就会变成无主增量。撞号循环靠"先释放再认领"腾出该属性。
                "ConditionExpression": (
                    "#s = :restoring AND attribute_not_exists(restore_reservation_id)"
                ),
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":rid": reservation_id,
                    ":rh": host["instance_id"],
                    ":rvn": target,
                    ":rip": _restore_endpoint(target)[0],
                    ":rport": _restore_endpoint(target)[1],
                    ":restoring": "restoring",
                },
            }
        },
    ]
    try:
        clients.hosts_table.meta.client.transact_write_items(TransactItems=txn_items)
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            _reasons = e.response.get("CancellationReasons", []) or []
            # 只有 host 项(idx0)带 ALL_OLD,租户项失败时这里拿到空 dict → host 字典不变,
            # 与"抢输"同款 None 契约(调用方换机重试,最终 fail-closed 503)。
            host.update(_fresh_host_state(_reasons[0] if _reasons else {}))
            return None
        raise
    # #446 —— restore 的物理 tap 撞号循环会把当前号归还后继续认领下一个,所以每次
    # 认领后都必须把 caller 持有的 host 字典推进到最新 next_vm_num。否则成功认领已
    # 推进 DDB、本地 expected 却停在旧值,下一轮必然 CCF；而释放刻意不回退
    # next_vm_num,这个 stale 值永远不会自行恢复。与 create 路径 #475 的刷新先例同款。
    # 事务不支持 ReturnValues=UPDATED_NEW,所以不再读回值 —— 但事务写进 DDB 的就是
    # `:next_after = target + 1`,本地赋同一个表达式【按构造恒等】于库里的值,比旧的
    # "读回来再解析、解析不了才 fallback" 更强(少一条可失败的路径)。
    host["next_vm_num"] = target + 1
    return target


def _release_restore_reservation(tenant_id, host_id, reservation_id, vcpu, mem_mb,
                                 phys_num):
    """#438 —— 令牌化释放 restore 预留:扣 host 账本 + 清租户 `restore_*` 坐标与令牌,
    一个 TransactWriteItems,条件 `restore_reservation_id = :rid`。返回三态。

    与 `_release_capacity_reservation` 同款互斥锚,只是令牌字段与被清坐标不同:谁先消费
    令牌谁扣一次账本,其余幂等 no-op。**刻意不带 status 守卫** —— 本函数既在保持
    `restoring` 时调用(撞号重认领),也在回滚前调用,而 status 条件失败会被分类器读成
    "令牌已被别人消费"(ALREADY),那是把"还占着容量"误报成"已释放",正好是要防的方向。

    `ps_<n>` 与 suspend 收尾同理走独立的 `release_phys_slot`(自带 owner 条件,是独立的
    幂等单元;并进事务会被账本项的下溢守卫连带取消 → 号泄漏且 reaper 救不了)。"""
    txn_items = [
        {
            "Update": {
                "TableName": clients.hosts_table.table_name,
                "Key": {"instance_id": host_id},
                "UpdateExpression": (
                    "SET used_vcpu = used_vcpu - :v, "
                    "used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one"
                ),
                "ConditionExpression": (
                    "used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one"
                ),
                "ExpressionAttributeValues": {":v": vcpu, ":m": mem_mb, ":one": 1},
            }
        },
        {
            "Update": {
                "TableName": clients.tenants_table.table_name,
                "Key": {"id": tenant_id},
                "UpdateExpression": (
                    "REMOVE restore_reservation_id, restore_host_id, restore_vm_num, "
                    "restore_guest_ip, restore_host_port"
                ),
                "ConditionExpression": "restore_reservation_id = :rid",
                "ExpressionAttributeValues": {":rid": reservation_id},
            }
        },
    ]
    try:
        clients.hosts_table.meta.client.transact_write_items(TransactItems=txn_items)
        _rel = _REL_CONSUMED
    except Exception as e:  # noqa: BLE001 — 判定(含未知错误保守当 retry)在分类器里
        _rel = _classify_release_cancel(e, tenant_id, "restore #438")
    if _rel != _REL_RETRY and phys_num is not None:
        scheduling.release_phys_slot(host_id, phys_num, tenant_id)
    return _rel


def _reclaim_stale_restore_reservation(tenant_id, item):
    """#659 —— failed 重入的前置清场。返回 None = 干净(或无需清场);返回 resp = 保持
    restoring 原样返回,重投时幂等重跑。

    上一次被死线围栏判死的 restore 可能留下两样东西:
      · **仍占账本的预留令牌**(`restore_reservation_id`)—— 围栏刻意保留令牌(#412 防双扣
        语义),而孤儿令牌 reaper 只扫 `capacity_reservation_id`、deadline_executor 不释放、
        reaper 矩阵只处理 `status=restoring` —— 三个"别人会管"都不成立,它就是永久泄漏。
      · **一台没人记账的 VM**(围栏 vs finalize 竞态):launch 已成功、finalize 的 CAS
        (`#s=restoring AND rid`)输给先到的围栏,CCF 分支只释放预留、不停 VM。

    为什么这一步是**硬前提**而不是加固:`_reserve_slot_on` 的租户项条件是
    `attribute_not_exists(restore_reservation_id)` —— 残留令牌不清,重入的新预留必然 CCF。

    顺序照 #520 C2(**释放 slot 之前必须把 VM 停掉**):先 stop-vm 再释放。反过来的话,
    释放让号可被复用,而那台竞态 VM 还活着 —— 下一个租户可能被排进同一个物理号(串号)。

    stop-vm 的墙钟额度取 `exec_step_sec(suspend, "stop-vm")`(#626 实测对齐的 50s),
    不写字面量。代价如实说明:重入路径的执行段最坏是 50s(清场)+ 120s(launch),超出
    restore 档 120s 的名义执行段 —— 死线可能在中途到点、被再次判死回 failed。**收敛性
    仍然成立**:清场是幂等的,已清掉的部分下次重入直接跳过,每次重试都有净进展。
    """
    rid = item.get("restore_reservation_id")
    if not rid:
        return None
    rh = (item.get("restore_host_id") or "").strip()
    rvn = item.get("restore_vm_num")
    if not rh or rvn is None:
        # 令牌与坐标由 `_reserve_slot_on` 的同一个事务写入,按构造同在。缺坐标 = 行被手改
        # 过或出现了未知写者 —— fail-closed 不猜:停错 VM(串号方向)比多等一轮糟得多。
        # 归因 system_error(已发布语义:出现即缺陷、报障)—— 这确实是缺陷,不是可自愈的
        # 瞬时态,所以刻意用 502 而不是 503 的"等重投"口吻。
        _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_SYSTEM)
        return utils._resp(
            502,
            {
                "error": "stale restore reservation token exists but its coordinates "
                "are missing; refusing to guess which VM to stop. Inspect the tenant "
                "row (this state is unreachable by construction).",
                "id": tenant_id,
            },
        )
    host_row = clients.hosts_table.get_item(Key={"instance_id": rh}).get("Item")
    if host_row is not None:
        # host 还在 → 那台竞态 VM 可能还活着,必须**确认停掉**才能释放(#520 C2)。
        # 与 launch 失败清理块同款自愈前缀;stop-vm.sh 本身幂等(VM 不在即 no-op)。
        _heal = ssm_dispatch.host_script_self_heal(
            ("stop-vm.sh",),
            "oc:restore",
            freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED"),
        )
        _stopped = ssm_dispatch._ssm_run(
            rh,
            f"{_heal} && /home/ubuntu/stop-vm.sh {tenant_id} {rvn}",
            timeout=create_deadline.exec_step_sec(
                create_deadline.ACTION_SUSPEND, "stop-vm"
            ),
        )
        if not _stopped:
            # 与 launch 失败路径同一归因(host_unreachable):SSM 到不了或脚本失败。
            # 503 保持 restoring 让消息重投 —— 本函数幂等,重投时从头再试。
            _mark_fail_reason(
                tenant_id, "restore", create_deadline.REASON_HOST_UNREACHABLE
            )
            return utils._resp(
                503,
                {
                    "error": "could not confirm the stale restore target VM is "
                    "stopped; kept restoring (with the old reservation) for "
                    "re-drive — releasing before stopping could reassign the "
                    "physical slot under a live VM.",
                    "id": tenant_id,
                },
            )
    # host 行已不存在 → 实例已 terminate,VM 不可能活着,直接释放。
    # (释放事务里 host 项的下溢守卫对不存在的行条件不成立 → 分类器判 ALREADY,安全幂等。)
    _rel = _release_restore_reservation(
        tenant_id, rh, rid, int(item.get("vcpu", 0)), int(item.get("mem_mb", 0)), rvn
    )
    if _rel == _REL_RETRY:
        # 瞬时失败,令牌可能仍在 → 绝不往下走预留(一租户行只记得住一枚令牌)。归因取
        # capacity,与 `_restore_reserve_slot` 撞号循环里同形态的 RETRY 出口同一口径
        # (「这一轮没能拿到可用槽位」);host_unreachable 不对(这是 DDB 侧的瞬时失败)。
        _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_CAPACITY)
        return utils._resp(
            503,
            {
                "error": "releasing the stale restore reservation hit a transient "
                "failure; kept restoring (token retained) for re-drive — releasing "
                "again is idempotent.",
                "id": tenant_id,
            },
        )
    return None


def _restore_reserve_slot(vcpu, mem_mb, tenant_id):
    """#422 — restore 冷恢复重取 host slot(全新 launch,与 create 同款:找 host → 认领
    vm_num → 物理 tap 撞号复检)。冷恢复的 tap 绑新 vm_num(launch-vm.sh:661
    TAP=tap-vm{VM_NUM}),故 phys_vm_num 重分配为新 vm_num,不沿用原值(原值那台 host 的
    slot 休眠时已释放,物理 tap 也已回收)。

    第三位在失败时【非 None 就意味着"这份预留仍占着容量"】,调用方据此**必须保持
    restoring、绝不回滚**:回滚会清掉令牌而账本仍是加过的 → 那就是本 issue 要修的永久
    容量泄漏,只是换了个触发点。"""
    host = scheduling._find_host(vcpu, mem_mb)
    if not host:
        _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_CAPACITY)
        return None, utils._resp(
            503, {"error": "no host capacity for restore", "id": tenant_id}
        ), None
    vm_num = None
    rid = None
    for attempt in range(8):
        _rid = secrets.token_hex(16)
        claimed = _reserve_slot_on(host, vcpu, mem_mb, tenant_id, _rid)
        if claimed is not None:
            vm_num, rid = claimed, _rid
            break
        time.sleep(0.05 * (attempt + 1))
        host = scheduling._find_host(vcpu, mem_mb)
        if not host:
            _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_CAPACITY)
            return None, utils._resp(
                503, {"error": "no host capacity (contended)", "id": tenant_id}
            ), None
    if vm_num is None:
        _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_CAPACITY)
        return None, utils._resp(
            503, {"error": "slot allocation contended out", "id": tenant_id}
        ), None
    # 物理 tap 撞号复检(no-cross-tenant,与 create :1722 同款):占了就丢号认领下一个。
    for _skip in range(64):
        if not _phys_tap_occupied(host["instance_id"], vm_num, exclude_id=tenant_id):
            break
        _rel = _release_restore_reservation(
            tenant_id, host["instance_id"], rid, vcpu, mem_mb, vm_num
        )
        if _rel == _REL_RETRY:
            # #438 —— **绝不能继续认领下一个号**。租户行只有一个 `restore_reservation_id`
            # 字段;这份预留可能仍在,再认领一份就等于同一租户同时持两份预留而只有一枚
            # 令牌记得住 → 另一份永久无主(容量泄漏)。返 503 让消息重投:令牌仍在,重投
            # 时释放是幂等的。调用方看到第三位非 None 就保持 restoring 不回滚。
            # #565 G3 接线:与本函数其余出口同档(capacity)—— 走到这里意味着这一轮没能
            # 拿到可用槽位。刻意**不用** capacity_release_pending:那个值的已发布契约写着
            # 「只有 delete 会出」,且不在 restore 的封闭子集里(assert_reason_valid 会拒)。
            _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_CAPACITY)
            return None, utils._resp(
                503,
                {
                    "error": "restore reservation release hit a transient failure while "
                    "skipping an occupied physical tap; kept the reservation and "
                    "status=restoring for re-drive (releasing again is idempotent).",
                    "id": tenant_id,
                },
            ), rid
        _rid = secrets.token_hex(16)
        claimed = _reserve_slot_on(host, vcpu, mem_mb, tenant_id, _rid)
        if claimed is None:
            _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_CAPACITY)
            return None, utils._resp(
                503, {"error": "no free vm slot (phys tap contended)", "id": tenant_id}
            ), None
        vm_num, rid = claimed, _rid
    else:
        # #565 G3(Codex 第八轮)—— 释放写的是 **hosts_table**,而落原因写 tenants_table:
        # 两张表**可以独立被节流**,所以"清理和落原因同为 DDB 写、先后无差别"这条推理在这里
        # 不成立。tenants 表被节流时先落原因(默认 read_timeout 60s × 4 次,最坏 ~240s)会把
        # 剩余运行时间耗光 → 预留永久搁浅。用 try/finally:清理先跑,原因照样保证落盘。
        # #438 —— 释放改走令牌化事务;try/finally 的结构与理由原样保留。
        try:
            _rel = _release_restore_reservation(
                tenant_id, host["instance_id"], rid, vcpu, mem_mb, vm_num
            )
        finally:
            _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_CAPACITY)
        return None, utils._resp(
            503, {"error": "unable to find free physical vm slot", "id": tenant_id}
        ), (rid if _rel == _REL_RETRY else None)
    return host, vm_num, rid


def _tenant_restore(tenant_id, item):
    """#422 — 恢复一个 suspended 租户到【同一 tenant_id】:从 S3 冷恢复数据盘、重取
    host+vm_num+slot、挂回原记录,status suspended→restoring→running。冷恢复(会话/上下文
    不续,只还原数据盘);tenant_id 不变(会议硬要求,维护生命周期链路)。

    fail-closed:launch(带 RESTORE_KEY)失败 → 回滚 restoring→suspended、释放刚取的 slot、
    不删 S3 备份、502。只有 launch 成功才翻 running。restore_backup_key 从 suspend 时写入的
    租户记录读(_tenant_suspend 落库)。

    #659 —— `failed` 是**受控**合法入口,仅限「从休眠链路掉进 failed」的行。
    此前 failed 是吸收态:死线围栏把超死线的 restore 判成 `status=failed`(排队超时时打中的
    甚至是健康的 `suspended`,围栏常规档没有状态白名单),而本函数只认 suspended → 再 restore
    永远 409;suspend 只认 running/stopped;start/restart 撞 #624 墓碑门;rebuild 走
    `_launch_vm_wake_cmd` 不带 RESTORE_KEY = 起空盘。数据在 S3(围栏只 SET 不 REMOVE,
    `restore_backup_key` 还在行上)但常规 API 永远取不回。放行后客户重发一次 restore 即自愈,
    与契约里 `deadline_exceeded_in_flight` 的「可重试报障」语义对齐。

    **守卫 `suspended_at`,一个都不能少**:它区分「failed 但没有 VM」(可以重入)与
    「failed 但 VM 可能还活着」(重入 = 起第二份 = 同租户双活)。逐条论证:
      · restore 链路掉进 failed 的行必带它 —— suspend 收尾写入,只有 restore finalize 与
        reaper promote(同一 REMOVE 清单)会清,围栏不清;
      · suspend 中途被判死(VM 活着)的行**没有**它 —— 那次 suspend 没走到 finalize;
      · create 失败的 failed 没有它,且下面 `backup_missing` 那道门双保险。"""
    _entry = item.get("status")
    # #679 —— `suspend_failed`(host 亡档)有备份的那一半允许 restore:数据在 S3 的
    # **备份时刻**快照里(客户端契约明写这是回退,其后写入不在其中),restore 是它唯一的
    # 数据取回通道 —— 不放行就重演 #659 修过的吸收态。判据锚 `suspend_backup_key`
    # (那次 suspend 的阶段快照,与 CAS 进 suspending 同轮落库、回滚会清,有轮次语义),
    # **不是** `suspended_at`(它没有 —— 没走到 finalize)。
    _restorable = _entry == "suspended" or (
        _entry == "failed" and item.get("suspended_at")
    ) or (
        _entry == "suspend_failed" and item.get("suspend_backup_key")
    )
    if not _restorable:
        if _entry == "failed":
            # failed 而无 suspended_at:要么从没休眠成功过(create/suspend 链路的失败,VM 或
            # 其残骸可能还在 host 上),要么行被手改过。指向 rebuild 是 #624 的既有口径。
            _hint = " To recover a failed tenant that was never suspended, rebuild it."
        elif _entry == "suspend_failed":
            # 无备份档:host 死在备份完成之前,S3 里没有本轮快照,数据不可恢复 ——
            # 契约明写唯一合法操作是 delete,这里把话说死,免得客户端无限重试抱幻想。
            _hint = (
                " This tenant's host died before its suspend backup completed; "
                "data is not recoverable — delete it."
            )
        else:
            _hint = ""
        return utils._resp(
            409,
            {
                "error": "can only restore a suspended tenant (or a failed one that "
                f"was suspended before); current: {_entry}.{_hint}",
                "id": tenant_id,
            },
        )
    # #679 —— `suspend_failed` 入口的 key **只认** `suspend_backup_key`,不落
    # `_resolve_backup` 兜底:那个兜底按约定取 latest 系统定时备份,拿一份不知道多旧的
    # 快照冒充"那次 suspend 的备份"是静默数据回退的放大版。准入已锚过该字段存在,
    # 这里直接取;其余入口维持既有取值链。
    if _entry == "suspend_failed":
        backup_key = item.get("suspend_backup_key") or ""
    else:
        backup_key = item.get("restore_backup_key") or _resolve_backup(tenant_id) or ""
    # #679 —— 失败回滚**从哪来回哪去**。`suspend_failed` 入口的 restore 失败若按既有
    # 字面量回滚成 `suspended`,等于把「数据只到备份时刻」洗白成「数据完好到挂起时刻」——
    # 正是 #679 裁定禁止的语义提升(另一个观察者看到 suspended 会误读数据新鲜度)。
    # `failed`(#659 档)洗成 suspended 则是**语义等价**(它有 suspended_at,备份就是
    # 挂起时刻的),维持既有行为。
    _rollback_to = "suspend_failed" if _entry == "suspend_failed" else "suspended"
    if not backup_key:
        _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_BACKUP_MISSING)
        return utils._resp(
            409,
            {
                "error": "no backup found for this tenant; cannot restore (data may be "
                "unrecoverable — do NOT create a blank tenant).",
                "id": tenant_id,
            },
        )

    # 并发闸:CAS <入口态> → restoring,单赢家(与并发 restore/delete 互斥)。
    # #659 —— from 用**实际读到的入口态**(suspended 或 failed),不写死 "suspended":
    # failed 重入走同一道闸,两个并发重入同样只有一个赢家。失败回滚(T3/T4 与 launch 失败
    # 路径)仍是既有字面量 "suspended" —— 那正是要的:重入一旦走到回滚,failed 就被洗回
    # suspended,下次重试连本档守卫都不再需要。
    if not _cas_status(tenant_id, _entry, "restoring", stash_prev=True):
        cur = (
            clients.tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get("Item")
            or {}
        )
        cur_s = cur.get("status")
        if cur_s in ("restoring", "running"):
            # #565 G6 —— 与 suspend 那条幂等 200 同源(`restoring` 在飞、`running` 已终态)。
            return utils._resp(
                200,
                _with_action_deadline(
                    {"id": tenant_id, "status": cur_s},
                    create_deadline.ACTION_RESTORE,
                    row=cur,
                ),
            )
        _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_PREEMPTED)
        return utils._resp(
            409, {"error": f"tenant is {cur_s}; restore lost the race", "id": tenant_id}
        )

    # #659 —— failed 重入的前置清场(**硬前提,不是加固**):上一次被围栏判死的 restore 可能
    # 留下仍占账本的令牌与一台没人记账的 VM。不清则下面 `_reserve_slot_on` 的租户项条件
    # `attribute_not_exists(restore_reservation_id)` 必然 CCF,重入压根走不通。
    # 位置在 CAS 赢之后:此刻本次操作独占 restoring,清场与旧的迟到释放者(带旧 rid)互斥于
    # 令牌锚,幂等。返回非 None = 清不动,保持 restoring 原样返回(重投时幂等重跑)。
    _stale = _reclaim_stale_restore_reservation(tenant_id, item)
    if _stale is not None:
        return _stale

    vcpu = int(item.get("vcpu", 0))
    mem_mb = int(item.get("mem_mb", 0))
    host, vm_num_or_resp, reservation_id = _restore_reserve_slot(vcpu, mem_mb, tenant_id)
    if host is None:
        # #438 —— 第三位非 None ⟹ 那份预留仍占着容量(释放遇瞬时失败)。此时【绝不回滚】:
        # 回滚会 REMOVE 令牌而账本仍是加过的 → 无主增量 = 永久容量泄漏。保持 restoring
        # 让消息重投,重投时凭令牌幂等释放。为 None 时才是"什么都没占",可以干净回滚。
        if reservation_id is None:
            _cas_status(tenant_id, "restoring", _rollback_to, clear_stuck=True)
        return vm_num_or_resp
    vm_num = vm_num_or_resp
    # #438 —— 与 `_reserve_slot_on` 落库的 restore_guest_ip/restore_host_port 同一个派生处,
    # 所以「传给 launch-vm 的」与「reaper 转正时照抄的」按构造相同。
    guest_ip, host_port = _restore_endpoint(vm_num)

    # 冷恢复 launch(带 RESTORE_KEY,launch-vm.sh 从 S3 下载/解密/解压/e2fsck 还原 data.ext4)。
    # #422 codex-blocker:sync=True 同步等 launch-vm.sh 真跑完返 bool —— 不能用 fire-and-forget
    # 的 CommandId 就翻 running(那只证明"提交了",VM 可能没起=假成功、VM 缺失、slot 泄漏)。
    # launch-vm.sh 内部已做:RESTORE_KEY 下载/解密/解压 + e2fsck + status 白名单(已含 restoring)
    # + 起 FC + DNAT;rc=0 才 True。失败(SSM 超时/launch-vm rc≠0)→ 回滚 suspended + 释放 slot,
    # 备份完好可重试(no-data-loss)。
    # #422 codex-blocker:sync=True 同步等 launch-vm.sh 真跑完,返 (ok, rc) —— 不能用
    # fire-and-forget 的 CommandId 就翻 running(那只证明"提交了",VM 可能没起=假成功)。
    # launch-vm.sh 内部:RESTORE_KEY 下载/解密/解压 + e2fsck + status 白名单(含 restoring)
    # + 起 FC + DNAT;rc=0 才 ok。
    launched, launch_rc = ssm_dispatch._launch_vm(
        host["instance_id"], tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port,
        config_template=item.get("config_template", ""),
        restore_backup_key=backup_key,
        # codex round4 #5(no-cross-tenant):必须传租户 effective skills。漏传→空值→launch-vm
        # 走广播分支 cp 全部共享 skills → 受限租户恢复后越权拿到未授权 skills。与 create :1957 同。
        scoped_skills=skills._resolve_effective_skills(item),
        chat_endpoint_enabled=bool(item.get("chat_endpoint_enabled", False)),
        sync=True,
        # #565 G1 —— restore 的执行段(180s 档里的 120s)。从预算表取,不写字面量。
        sync_timeout=create_deadline.exec_step_sec(
            create_deadline.ACTION_RESTORE, "launch-vm"
        ),
    )
    # 真机-bug 修复(flock 竞争):launch-vm.sh 抢不到同租户 per-tenant flock 时 exit 75
    # (launch-vm.sh:395,skip 专用哨兵)——意味着【另一次同租户 launch 正在把 VM 拉起】
    # (SQS FIFO 重投 / 并发)。此时【绝不能回滚】:回滚会释放 slot、翻 suspended,而那次在跑的
    # launch 随后 DONE → VM 活着但账本已回收+状态 suspended = 状态震荡 + slot 泄漏(真机实测:
    # restoring↔suspended↔running 反复横跳)。
    # 处理:保持 restoring(不回滚不释放 slot)+ 返 503。为什么 503 不是 202 —— consumer
    # (handler.py:1651)只有 code>=500 才把消息留队列重投,4xx/2xx 一律 ack。若返 2xx,这条
    # 消息被 ack 消费掉;万一持锁的那次 launch 最终没能把状态翻 running(它自己也失败),就无人
    # 再推进→永久卡 restoring。返 503 让本消息留队列:可见性超时后重投,那时持锁者大概率已跑完
    # (成功→本次重投读到 running 幂等返回;失败→本次重投自己拿到锁重试),收敛有保证。
    if not launched and launch_rc == 75:
        _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_PREEMPTED)
        return utils._resp(
            503,
            {
                "id": tenant_id,
                "status": "restoring",
                # #604 —— **这里刻意不发 `LAUNCH_IN_PROGRESS`**,即 restore 保持队列默认的
                # 960s 重投,不参与短退避。
                #
                # 理由(Codex 独立复审第 6 轮):短退避会把「等到状态收敛后再重投」变成
                # 「状态还没收敛就重投」,而 restore 与 restart 在这一点上不同 ——
                #   · restart 失败时**不动 status**(仍是 running),重投时准入照旧通过;
                #   · restore 走 `_cas_status(restoring ← suspended)`,重投时 status 已经是
                #     `restoring`,那次 CAS 不会成立。它之后的路径没有被证明可恢复,而这条
                #     路上挂着已预留的容量(`restore_reservation_id`)—— 一旦重投被当成
                #     4xx 消费掉,恢复请求就永久消失、预留容量搁浅。
                # 在没有为 restore 做出「同一次恢复可安全重投」的证明之前,宁可让它慢
                # (960s,与改动前一致)也不引入丢消息的可能。留作 #604 的后续项。
                "error": "restore launch is already in progress on the host (flock held by "
                "a concurrent/redelivered launch); kept restoring, will reconverge on retry",
            },
        )
    if not launched:
        # 真失败(rc≠0 且≠75,或 SSM 超时 rc=None):launch-vm 可能已起 VM(超时≠没起)。
        # 直接释放 slot 会留孤儿 VM。回滚前先 stop-vm 清目标(幂等),再释放 slot、回 suspended。
        # #520 C2:这条清理是"释放 slot 之前必须把 VM 停掉",stop-vm.sh 缺失/过期会让它
        # 静默无效(本调用的返回值本来就不看)→ 孤儿 VM 留在 host 上而 slot 已释放,
        # 下一个租户可能被排到同一个物理号。故同样前置 S3 自愈。
        #
        # #565 G3(Codex 独立复审第七轮)—— **这一处、也只有这一处用 try/finally**,让落原因
        # 排在清理【之后】而又保证一定执行。理由是量化过的:
        #   · `ddb = boto3.resource("dynamodb")` 没配 Config → boto3 默认 read_timeout 60s
        #     × 4 次尝试,一次 DDB 写最坏能吃掉 ~240s。DDB 不可达时先落原因就可能把 Lambda
        #     的剩余时间耗光,而**下面这个 stop-vm 是唯一不依赖 DDB、能救回孤儿 VM 的动作**。
        #   · 其余接线的清理全是 DDB 写 —— DDB 挂了它们和落原因一样做不成,先后无实质差别。
        # finally 让两个目标同时成立:安全关键的清理先跑,而清理抛异常时原因照样落盘。
        #
        # #438 —— 结构原样保留,只把 `_release_slot` 换成令牌化释放,并加一条 RETRY 早返回。
        # 早返回落在 try 里 ⇒ finally 照样执行 ⇒ **那条 502 出口也是可归因的**(原因仍是
        # host_unreachable:走到这里的根因是 launch 失败,释放失败是次生的)。
        try:
            _restore_heal = ssm_dispatch.host_script_self_heal(
                ("stop-vm.sh",),
                "oc:restore",
                freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED"),
            )
            ssm_dispatch._ssm_run(
                host["instance_id"],
                f"{_restore_heal} && /home/ubuntu/stop-vm.sh {tenant_id} {vm_num}",
            )
            # 令牌化释放:扣账本与清 restore_* 坐标/令牌一个事务、条件 rid 匹配,幂等。
            _rel = _release_restore_reservation(
                tenant_id, host["instance_id"], reservation_id, vcpu, mem_mb, vm_num
            )
            # 释放遇瞬时失败 ⟹ 令牌可能仍在、账本可能仍是加过的。**绝不回滚**:回滚会清掉
            # 令牌而增量还在 → 无主增量 = 永久容量泄漏(与 delete 路径 RETRY 留 deleting
            # 同一条原则)。此时保持 restoring,由下面那条 502 让消息重投(释放幂等)。
            if _rel != _REL_RETRY:
                _cas_status(tenant_id, "restoring", _rollback_to, clear_stuck=True)
        finally:
            _mark_fail_reason(
                tenant_id, "restore", create_deadline.REASON_HOST_UNREACHABLE
            )
        # #565 G3 —— 这条 return 刻意放在 try/finally **之后**,而且是**一条**而不是两条
        # 嵌在 `if` 里。原因是覆盖检查的判据:它按「同 block 内、return 之前有一条 mark」找,
        # 且对 try/finally 只在 `return 落在 Try 之后`时才回溯 `finalbody`。
        #   · 写在 try 体内 → 运行时照样触发 finally,但静态判成未接线;
        #   · 嵌在 `if` 里 → return 所在 block 是 If.body,回溯不到那个 Try;
        #   · 在 return 前再补一条 mark → 与 finally 重复写(#565 明确要避免)。
        # 所以收敛成一条 return、只让**文案**随 _rel 变。两种情形本来都是 502,
        # 回滚与否已在上面的 try 体内按 _rel 处置过,故运行时语义不变。
        return utils._resp(
            502,
            {
                "error": (
                    f"restore launch failed (rc={launch_rc}) and releasing the "
                    "capacity reservation hit a transient failure; target VM stopped "
                    "and the S3 backup is intact, but status is kept restoring with "
                    "the reservation token so a re-drive reconciles it instead of "
                    "stranding the capacity. Retry."
                    if _rel == _REL_RETRY
                    else f"restore launch failed (rc={launch_rc}); target VM stopped, "
                    "rolled back to suspended, slot released, S3 backup intact. Retry."
                ),
                "id": tenant_id,
            },
        )

    # 挂回原 tenant_id:更新 host/vm_num/phys_vm_num(冷恢复重分配)/guest_ip/host_port,翻 running。
    # CAS 条件 status=restoring(单赢家持有)。清 suspended_at/restore_backup_key/suspended_from。
    # #571 — 收尾一并把 app_health 置 down + 刷新 last_health_check：restore 不重置健康位
    # 会让「status=running && app_health=up」在数据面就绪前就为真（host-agent 下一 tick
    # 才对账路由/探活），客户端据此发首请求落空需刷新。置 down 让就绪信号诚实且不依赖
    # poll 时序；host-agent 下一 tick 探到 gateway 应答即写回 up（其 gate 用当轮探测值，
    # 不受此 down 影响）。
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            # #469 P1 —— 同 suspend 收尾:一并清卡死标记。一个跑得慢但最终成功的 restore
            # 可能已被中间态巡检标记过;不清则健康的 running 租户永久带着 lifecycle_stuck_at,
            # 让 P2 的 ?force=true 对它放行 → 误删。REMOVE 不存在的属性是 no-op。
            # #438 —— 收尾把临时坐标转正成永久坐标并消费令牌,**不碰账本**:容量在预留时
            # 已经加过,finalize 只是"转正"(与 dispatch_poller 的 promote / host-agent 的
            # mark-running 同款,它们也只 REMOVE 令牌、不动 used_*)。
            # 写进 host_id/vm_num 的值与 restore_host_id/restore_vm_num 同源 —— 二者由
            # `_reserve_slot_on` 的同一个事务写下,而下面的条件把这一行钉在【那次】预留上,
            # 所以内存值与库里的临时坐标按构造相等。
            UpdateExpression=(
                "SET #s = :running, host_id = :h, vm_num = :vn, phys_vm_num = :vn, "
                "guest_ip = :gip, host_port = :hp, updated_at = :t, "
                "app_health = :down, last_health_check = :t "
                # #679(Codex 实现评审第二轮)—— suspend_backup_key 必须随翻 running 一并
                # 清掉:从 suspend_failed 入口 restore 成功后若残留,下一轮 suspend 失败时
                # 原语 B 会把这份【上一轮】的陈旧快照当成本轮备份 → 客户端 restore 恢复到
                # 更老的数据,静默回退。REMOVE 不存在的属性是 no-op,真 suspended 入口不受影响。
                "REMOVE suspended_at, restore_backup_key, suspend_backup_key, "
                "suspended_from, "
                "restore_reservation_id, restore_host_id, restore_vm_num, "
                "restore_guest_ip, restore_host_port, "
                "lifecycle_stuck_at, lifecycle_stuck_reason, lifecycle_stuck_vm_dir, "
                "lifecycle_stuck_fc_alive, updated_at_stuck_seen, lifecycle_prev_status, "
                "lifecycle_rollback_deferred_until, lifecycle_probe_attempted_at"
            ),
            # 令牌条件是本步的核心:没有它,一次迟到的 finalize 可以把租户翻成 running 并
            # 写上【别的一轮】预留的坐标(那一轮的账本增量随后被谁释放都算错)。
            ConditionExpression="#s = :restoring AND restore_reservation_id = :rid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":down": "down",
                ":restoring": "restoring",
                ":rid": reservation_id,
                ":h": host["instance_id"],
                ":vn": vm_num,
                ":gip": guest_ip,
                ":hp": host_port,
                ":t": utils._now(),
            },
        )
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        # 状态被改走或令牌已被别人消费。释放本次预留(幂等:令牌已不在则 ALREADY no-op),
        # 状态不动 —— 它已经不属于我们了。
        # 同上(#565 G3,Codex 第八轮):释放写 hosts_table,落原因写 tenants_table,
        # 两表可独立被节流 → 清理先跑,finally 兜住原因一定落盘。
        try:
            _rel = _release_restore_reservation(
                tenant_id, host["instance_id"], reservation_id, vcpu, mem_mb, vm_num
            )
        finally:
            _mark_fail_reason(tenant_id, "restore", create_deadline.REASON_PREEMPTED)
        return utils._resp(
            409 if _rel != _REL_RETRY else 502,
            {
                "error": "restore finalize lost CAS (status changed mid-restore); "
                "inspect tenant state."
                + (
                    " Releasing this run's capacity reservation hit a transient failure; "
                    "the token is kept so a re-drive reconciles it."
                    if _rel == _REL_RETRY
                    else ""
                ),
                "id": tenant_id,
            },
        )
    audit._publish_event(
        "tenant.restored", tenant_id, {"host_id": host["instance_id"], "vm_num": vm_num}
    )
    return utils._resp(
        200, {"id": tenant_id, "status": "running", "host_id": host["instance_id"]}
    )


def _reserve_migration_slot(target, vcpu, mem_mb, attempts=8, tenant_id=None):
    """#50/#172 — atomically claim a vm_num + capacity on a migration TARGET host.

    与 create 路径 _reserve_slot 同款 CAS,为 migrate 路径抽出。修复前 migrate 用
    `target_vm_num = int(target.get("next_vm_num"))` 裸 get + 无条件写租户记录,不抢占
    target 的 slot、不递增 next_vm_num:两个并发迁移到同一 target host 读到同一
    next_vm_num → 分到同一 vm_num → guest_ip/host_port 重叠 → 跨租户网络/端口串(危害轴①)。
    此 CAS(a)确认 next_vm_num 自读取未变(b)写时复检容量不超卖(c)一次条件写自增
    next_vm_num/used_*。CCF(竞争)则重读 target 重试。返回认领的(自增前)vm_num,或
    None(target 无容量/持续输 CAS/已 draining/deleted)。

    容量记账发生在这里(认领即递增 target used_*),所以 health_check sweep 的
    restore-success 路径**只能减 SOURCE 计数**,绝不能再递增 target 或绝对赋值
    next_vm_num(否则重复计账 + 覆盖并发 create 的递增,#172 defect B)。
    """
    instance_id = target["instance_id"]
    h = target
    # #430 — 物理内存软门必须覆盖【全部】放置入口,迁移是第三个。前两个(_find_host、
    # _get_specific_host_with_capacity 的 pinned/clone 路径)已补;漏掉这里等于留了一条
    # 绕过水位保护的路:账本说还有位置,而 target 自报的实测 MemAvailable 已在水位之下,
    # 迁入的租户会把它推得更低(#352 的形态)。带 needed_mb 做预测准入 —— 门要保证
    # "迁入之后"仍高于水位,而不是"迁入之前"。
    # 放在重试循环【外】:三段 fail-open(门关/无信号/陈旧)与是否输 CAS 无关,且
    # 循环内重读的 fresh item 会在下面同步刷新判定。
    if not capacity.mem_ok(
        h,
        clients.MEM_SAFETY_FLOOR_RATIO,
        clients.MEM_CHECK_TTL_SEC,
        int(time.time()),
        needed_mb=mem_mb,
    ):
        return None
    for attempt in range(attempts):
        expected = int(h.get("next_vm_num", 1))
        claimed_target, occupied = scheduling.next_free_phys_num(
            instance_id,
            expected,
            exclude_ids={tenant_id} if tenant_id else None,
        )
        if occupied is None or claimed_target is None:
            return None
        _cpu_r, _mem_r = host_profile.ratios(
            h,
            (clients.CPU_OVERCOMMIT_RATIO, clients.MEM_OVERCOMMIT_RATIO),
            clients.OVERCOMMIT_BY_FAMILY,
        )
        cap_v = capacity.allocatable(int(h["total_vcpu"]), _cpu_r) - vcpu
        cap_m = capacity.allocatable(int(h["total_mem_mb"]), _mem_r) - mem_mb
        try:
            r = clients.hosts_table.update_item(
                Key={"instance_id": instance_id},
                UpdateExpression=(
                    "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
                    "vm_count = vm_count + :one, next_vm_num = :next_after, #ps = :tid"
                ),
                ConditionExpression=(
                    "next_vm_num = :expected AND used_vcpu <= :cap_v "
                    "AND used_mem_mb <= :cap_m AND attribute_not_exists(#ps) "
                    # #540 — 污点原子门。上面 target 那次显式检查是【提前给 409】,
                    # 这里才是原子的:检查与认领之间运维仍可能标记这台。
                    "AND " + host_taint.NOT_TAINTED_CONDITION
                ),
                ExpressionAttributeNames={
                    "#ps": scheduling.phys_slot_attr(claimed_target)
                },
                ExpressionAttributeValues={
                    ":v": vcpu,
                    ":m": mem_mb,
                    ":one": 1,
                    ":tid": tenant_id or "unknown",
                    ":expected": expected,
                    ":next_after": claimed_target + 1,
                    ":cap_v": cap_v,
                    ":cap_m": cap_m,
                    **host_taint.NOT_TAINTED_VALUES,  # #540
                },
                ReturnValues="UPDATED_NEW",
            )
            try:
                return int(r["Attributes"]["next_vm_num"]) - 1
            except (KeyError, TypeError, ValueError):
                return claimed_target
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # 输了 CAS 或容量变了 — 重读 target 重试(退避)。
            time.sleep(0.05 * (attempt + 1))
            fresh = clients.hosts_table.get_item(
                Key={"instance_id": instance_id}, ConsistentRead=True
            ).get("Item")
            if not fresh or fresh.get("status") in ("draining", "deleted"):
                return None
            # #430 — 重读后复检物理门:重试期间 target 的实测 MemAvailable 可能已跌破水位
            # (并发 create/迁移在往它塞租户)。只在循环外判一次会让后续重试绕过门。
            if not capacity.mem_ok(
                fresh,
                clients.MEM_SAFETY_FLOOR_RATIO,
                clients.MEM_CHECK_TTL_SEC,
                int(time.time()),
                needed_mb=mem_mb,
            ):
                return None
            h = fresh
    return None


def _rebuild_repin_resolve(item, repin_body):
    """解析 rebuild 目标 channel(纯校验,不落库、不备份)。返回
    {"channel": str, "target_snap": str|None} 或 utils._resp(...) 错误响应。

    缺省 channel 是 live。live/canary 都必须在当前 host 有对应槽位；任一槽位缺失都
    fail-closed，且必须发生在强制备份、租户落库和 VM 变更之前。
    """
    channel, ch_err = image_channel_mod.normalize_channel(repin_body.get("image_channel"))
    if ch_err:
        return utils._resp(400, {"error": ch_err, "code": "VALIDATION"})
    # ADR §5.6 —— host_id 闸对 live 与 canary **对称**。此前 live 在解析出 channel 后就直接
    # return,绕过了这道检查:一个没落在任何 host 上的租户(host_id 空,如 stopped/异常态)走
    # live 换版会被放行,接着上层无条件跑 stop + rm overlay + launch —— 而 item["host_id"] 是
    # 空串,SSM 拿空 InstanceId 必然报错,可落库已把 image_channel 改成了 live。客户看到的是
    # 一次莫名失败而不是清晰的 409。两条路径的前置条件本来相同(都得在某台 host 上执行),
    # 守卫只有一边有,属实现遗漏而非设计意图。
    _hid = (item.get("host_id") or "").strip()
    if not _hid:
        # 两个 code 分开:canary 还额外要求该 host 有 READY 的 canary 槽,错误语义更窄,
        # 且客户已在用 CANARY_NOT_READY 做分支,不改它。
        if channel == "live":
            return utils._resp(
                409,
                {
                    "error": "tenant is not placed on a host; re-pin needs a placed "
                    "tenant (start the tenant first)",
                    "code": "REPIN_NO_HOST",
                    "id": item.get("id"),
                },
            )
        # canary:以该 host image_slots 的 canary 槽为准做 snapshot_time CAS(与 create 同源)。
        # 拿空 key 调 get_item 会让 DynamoDB 抛 ParamValidation → 未捕获 500,故先挡掉。
        return utils._resp(
            409,
            {
                "error": "tenant is not placed on a host; canary re-pin needs a host "
                "with a canary slot (start the tenant first, or use image_channel=live)",
                "code": "CANARY_NOT_READY",
                "id": item.get("id"),
            },
        )
    host = clients.hosts_table.get_item(
        Key={"instance_id": _hid}, ConsistentRead=True
    ).get("Item")
    if not host:
        return utils._resp(
            404,
            {"error": f"tenant host {item.get('host_id')!r} not found", "code": "NOT_FOUND"},
        )
    host_slots = host.get("image_slots") or {}
    if channel == "live" and not host_slots.get("live"):
        return utils._resp(
            409,
            {
                "error": "target host has no READY live image version; pull or promote "
                "a live image before rebuilding",
                "code": "NO_LIVE_VERSION",
                "id": item.get("id"),
            },
        )
    if channel == "live":
        return {
            "channel": "live",
            "target_snap": None,
            "target_host_snapshot": host_slots.get("live") or "",
        }
    target_snap, code, msg = image_channel_mod.resolve_pinned_version(
        channel, host_slots,
        repin_body.get("expected_image_snapshot_time"),
        repin_body.get("expected_image_generation"),
    )
    if code:
        return utils._resp(400 if code == "VALIDATION" else 409, {"error": msg, "code": code})
    return {
        "channel": "canary",
        "target_snap": target_snap,
        "target_host_snapshot": target_snap,
    }


def _rebuild_repin_apply(
    tenant_id,
    item,
    channel,
    target_snap,
    lifecycle_op_id=None,
    lifecycle_fence_epoch=None,
):
    """#416 — 落库换版目标(破坏性 relaunch 之前):换版前强制备份 fail-closed,再写租户
    image_channel/image_snapshot_time。返回 None(成功)或 utils._resp(...) 错误响应。

    幂等(SQS 重投):目标 == 当前已固定 → 跳过【DDB 写】(no-op 写),但【仍强制备份】——因为
    上层无条件走破坏性 relaunch(drop overlay),relaunch 会重新解析并采用当时的目标版本,
    可能与 overlay 建立时的 rootfs 不同(尤其 live→live:host live 指针可能已移到别的版本)。
    codex(review2)#1 — 短路不能跳过备份,否则丢 overlay 前无兜底(违反"任何换版都先备份")。
    """
    # 换版前强制备份 fail-closed(任一方向、含 no-op 写路径都备:上层无条件 relaunch drop
    # overlay,换到不同 rootfs 读不了新 data.ext4 是最高数据风险)。失败即拒绝,不 relaunch。
    #
    # ADR §5.6 —— 备份守卫不再拿 host_id 的真值性当条件。原写法 `if item.get("host_id"):`
    # 把"没有 host"**静默当成不需要备份**然后继续往下落库+relaunch:一个 host_id 为空的租户
    # 会跳过这道 no-data-loss 兜底。备份是否需要,取决于"这次要不要动 overlay"(答案恒为要),
    # 不取决于我们此刻是否恰好知道它在哪台机器上。host_id 为空本身就是不该继续的状态,
    # 由调用方 _rebuild_repin_resolve 的 409 闸负责挡下(两条路径现已对称);这里保留一层
    # 防御式断言,避免将来有人绕过 resolve 直接调本函数就静默失去备份。
    if not (item.get("host_id") or "").strip():
        return utils._resp(
            409,
            {
                "error": "tenant is not placed on a host; re-pin needs a placed tenant "
                "(refusing to skip the mandatory pre-repin backup)",
                "code": "REPIN_NO_HOST",
                "id": tenant_id,
            },
        )
    # #565 G1 —— rebuild 是 180s 档,执行段 120s 里只能给备份 55s(余下 65s 给 rebuild-vm)。
    # 比 delete 那档的 90s 小,理由见 `_EXEC_STEPS[ACTION_REBUILD]`:同一个 180s 里备份之后
    # 还有一整个 relaunch。**这是七档里最紧的一步**,大盘租户可能在这里失败而在 suspend 成功。
    ok, err = _force_backup_sync(
        tenant_id,
        ssm_budget_sec=create_deadline.exec_step_sec(
            create_deadline.ACTION_REBUILD, "backup"
        ),
    )
    if not ok:
        _mark_fail_reason(tenant_id, "rebuild", create_deadline.REASON_BACKUP_FAILED)
        # #547 — fail-closed 早退必须先放掉【自己】取得的生命周期租约。
        # 这条路径一步都没执行(真机复现:rebuild_phase/rebuild_status 全空、host 上无
        # rebuild-vm.sh 的 SSM 记录),却把租约留在租户记录上,于是 delete/restart/reset/
        # migrate 全被 409 LIFECYCLE_IN_FLIGHT 挡住约 30 分钟 —— 包括删不掉,只能等租约自然过期。
        # 紧随其后的 renew_owned 失败分支【绝不能】这样做:那时租约已经属于别人,
        # 释放它等于把别人的锁抢掉。
        if lifecycle_op_id is not None and lifecycle_fence_epoch is not None:
            lifecycle_fence.release(tenant_id, lifecycle_op_id, lifecycle_fence_epoch)
        return utils._resp(
            502,
            {
                "error": "pre-repin backup failed; aborting rebuild re-pin to avoid "
                "irreversible data loss on downgrade. Retry once the backup succeeds.",
                "backup_error": err,
                "code": "REPIN_BACKUP_FAILED",
            },
        )
    if (
        lifecycle_op_id is not None
        and lifecycle_fence_epoch is not None
        and not lifecycle_fence.renew_owned(
            tenant_id, lifecycle_op_id, lifecycle_fence_epoch
        )
    ):
        _mark_fail_reason(tenant_id, "rebuild", create_deadline.REASON_PREEMPTED)
        return utils._resp(
            409,
            {
                "error": "rebuild was superseded while the mandatory backup ran",
                "code": "LIFECYCLE_SUPERSEDED",
                "id": tenant_id,
            },
        )

    cur_channel = item.get("image_channel") or image_channel_mod.DEFAULT_CHANNEL
    cur_snap = (item.get("image_snapshot_time") or "").strip() or None
    if channel == cur_channel and target_snap == cur_snap:
        return None  # 已固定到目标 → 跳过冗余 DDB 写(备份已做,上层继续 relaunch)

    # 只动本租户自己的两个属性(no-cross-tenant)。
    update_kwargs = {}
    if lifecycle_op_id is not None and lifecycle_fence_epoch is not None:
        condition, values = lifecycle_fence.condition(
            lifecycle_op_id, lifecycle_fence_epoch
        )
        update_kwargs["ConditionExpression"] = condition
        update_kwargs["ExpressionAttributeValues"] = values
    # #565 G3(Codex 独立复审第六轮)—— 这两条写都带围栏条件,而围栏在上面 renew_owned()
    # 之后**仍可能被抢走**(租约到期后别人 acquire 到新的 op_id/epoch)。此前 CCF 直接逃逸 →
    # 上层变成一个**未归因的 500**,而 rebuild 在契约里声明了 preempted。
    # 处置与 _tenant_action_inner 的 CCF 分支一致:落原因后**原样上抛**,不改既有响应行为
    # (围栏的处置语义归 #413/#547,不在本 issue 范围)。落原因排在 raise 之前 —— 顺序规则见
    # _mark_fail_reason 的 docstring 与 TestMarkOrdering。
    try:
        if channel == "live":
            values = {
                ":c": "live",
                ":t": utils._now(),
                **update_kwargs.pop("ExpressionAttributeValues", {}),
            }
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=(
                    "SET image_channel = :c, updated_at = :t REMOVE image_snapshot_time"
                ),
                ExpressionAttributeValues=values,
                **update_kwargs,
            )
        else:
            values = {
                ":c": "canary",
                ":s": target_snap,
                ":t": utils._now(),
                **update_kwargs.pop("ExpressionAttributeValues", {}),
            }
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=(
                    "SET image_channel = :c, image_snapshot_time = :s, updated_at = :t"
                ),
                ExpressionAttributeValues=values,
                **update_kwargs,
            )
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        _mark_fail_reason(tenant_id, "rebuild", create_deadline.REASON_PREEMPTED)
        raise
    return None


# ADR-rebuild-idempotency-sync-contract §5.3/§5.4 — rebuild 进度与终态字段。
# 为什么用独立字段而不是给 `status` 加一个 "rebuilding" 值:`status` 有四个既有消费方,
# 其中 scaler 的 _REFRESH_SKIP_STATUS(scaler/handler.py)不含 rebuilding → 升级中的租户
# 会被判为版本落后而触发跨 host 迁移,与正在跑的 rebuild 撞车;且 `status` 是 gsi_status
# 的查询键(升级中的租户会从客户 ?status=running 的结果里静默消失),还是客户已在用的契约
# 字段(新增枚举值属破坏性变更)。这几个字段是 ADDITIVE 的,现有消费方为零。
# 注:host 侧 launch-vm.sh 的可起态白名单其实已含 "rebuilding"(控制面从未写过该值),
# 但那只解决四个消费方里的一个,故仍不走扩 status 的路子。
_REBUILD_PHASE_QUEUED = "queued"  # 兼容历史异步 rebuild 记录
_REBUILD_PHASE_RUNNING = "running"  # consumer 已下发 SSM
_REBUILD_PHASE_VERIFYING = "verifying"  # 回执已收,正在验采用
# 非终态集合:处于其中任一阶段就说明上一次 rebuild 还在飞,不该再放第二次进来(见
# tenant_action 的 REBUILD_IN_FLIGHT 闸)。
# ── #564 G7 —— 手动备份的相位。**这里是这套取值的权威定义** ──────────────────
#
# 为什么不放 `core/create_deadline.py`:那里管的是"死线口径"(默认值/字段名/取值子集),
# 而相位是**操作状态机**的一部分,与 `_REBUILD_PHASE_*` 是同一类东西,理应放在同一处。
#
# `deploy/lambda/backup/handler.py` 要写这几个值,但它是**另一个 Lambda**(asset 只含
# 它自己一个文件),import 不到本模块 —— 所以那边只能写字面值,一致性由
# `tests/test_564_g6g7_dlq_backup.py` 的一条断言逐值比对(与 rebuild 相位那条同款理由)。
_BACKUP_PHASE_QUEUED = "queued"      # 已受理、已派发,worker 还没开始
_BACKUP_PHASE_RUNNING = "running"    # worker 已开始备份
_BACKUP_PHASE_SUCCEEDED = "succeeded"
_BACKUP_PHASE_FAILED = "failed"
# 非终态集合:处于其中任一相位就说明这次备份还在飞。死线执行者按它扫
# (`deadline_executor._BACKUP_INFLIGHT_PHASES` 必须与它逐值一致)。
_BACKUP_INFLIGHT_PHASES = frozenset(
    {_BACKUP_PHASE_QUEUED, _BACKUP_PHASE_RUNNING}
)

_REBUILD_INFLIGHT_PHASES = frozenset(
    {_REBUILD_PHASE_QUEUED, _REBUILD_PHASE_RUNNING, _REBUILD_PHASE_VERIFYING}
)
# in-flight 闸的过期时长。**这个兜底是必需的,不是可选优化**:若上一次 rebuild 的执行崩在
# 中途、没能写终态,一个没有超时的闸会把该租户永久锁死在"无法 rebuild"。默认 30 分钟远大于
# 一次 rebuild 的正常收敛时间(真机 15s ~ 数分钟),且到那时 §5.4a 的对账也已把上一次收成
# done/failed 了。与 health_check 的 REBUILD_UNCONFIRMED_TIMEOUT_SECONDS 同语义、同默认值。
_REBUILD_INFLIGHT_TIMEOUT_SECONDS = int(
    os.environ.get("REBUILD_INFLIGHT_TIMEOUT_SECONDS", "1800")
)


def _rebuild_inflight_is_stale(started_at):
    """上一次 rebuild 的 in-flight 标记是否已过期(可以放行新的一次)。

    读不懂时间戳、或根本没有起始时间 → 一律当【已过期】返回 True。这是刻意选的宽松方向:
    闸的目的是防并发,不是防重试;一个读不出时间的坏记录若让闸永久生效,租户就再也 rebuild
    不了(而它本可能只是个历史遗留字段)。宁可放行让 flock/对账兜住,也不锁死。
    """
    if not started_at:
        return True
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:  # noqa: BLE001
        return True
    return elapsed >= _REBUILD_INFLIGHT_TIMEOUT_SECONDS


# #523 判据 4 —— rebuild 采用证据的版本维度。
# firecracker_version 取自 host 上 /proc/<pid>/exe --version 的输出(正在跑的那个二进制,
# 不是磁盘上那份声称),形如 `v1.15.1`,允许 upstream 的构建后缀(`v1.15.1-dirty`)。
# guest_kernel_sha256 是 VM 真正引导的那个 vmlinux 的内容摘要 —— 用摘要而不用 marker 里的
# 名字,因为名字只是 provision 的声称、在 #389v2 之前的老 host 上根本没有,且抓不到
# "同名不同字节"(CI 桶重发过同名对象)这一档。
_FC_VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._-]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _parse_host_rebuild_result(
    stdout,
    tenant_id,
    op_id,
    attempt_id,
    fence_epoch,
    host_id,
    vm_num,
):
    """Return only an identity-bound host result from rebuild-vm.sh.

    verify 路径原本零业务日志:一次真机上,部署在 S3 的旧 rebuild-vm.sh 不发
    firecracker_version/guest_kernel_sha256,parser 恒拒 → 每次 rebuild 都静默落
    unconfirmed,却查不到任何"为什么"——只能临时加诊断码才定位到 fc_version 分支。
    这些 `rebuild-adopt-reject` 行让 host 脚本与 parser 的契约漂移在 CloudWatch 里
    直接可见(host 脚本太旧/字段缺失/身份不符/inode 不合法),不必再临时插桩。
    """
    _tag = f"rebuild-adopt-reject op={op_id} tenant={tenant_id}"
    result = None
    for line in reversed((stdout or "").splitlines()):
        try:
            candidate = json.loads(line)
        except Exception:
            continue
        if isinstance(candidate, dict) and candidate.get("state"):
            result = candidate
            break
    if not result:
        print(f"{_tag} reason=no_identity_bound_state_json stdout_len={len(stdout or '')}")
        return None
    expected = {
        "tenant_id": tenant_id,
        "op_id": op_id,
        "attempt_id": attempt_id,
        "host_id": host_id,
        "vm_num": int(vm_num),
        "fence_epoch": int(fence_epoch),
    }
    _mismatch = [k for k, v in expected.items() if result.get(k) != v]
    if _mismatch:
        print(f"{_tag} reason=identity_mismatch fields={_mismatch}")
        return None
    if result.get("state") != "SUCCEEDED":
        return result
    inode = re.compile(r"^[0-9]+:[0-9]+$")
    required_inodes = (
        "target_rootfs_dev_inode",
        "firecracker_exe_dev_inode",
        "overlay_dev_inode",
        "overlay_fd_dev_inode",
    )
    _bad_inodes = [k for k in required_inodes if not inode.fullmatch(str(result.get(k) or ""))]
    if _bad_inodes:
        print(f"{_tag} reason=bad_or_missing_inode fields={_bad_inodes}")
        return None
    if result["overlay_dev_inode"] != result["overlay_fd_dev_inode"]:
        print(f"{_tag} reason=overlay_inode_mismatch")
        return None
    if not str(result.get("firecracker_start_ticks") or "").isdigit():
        print(f"{_tag} reason=firecracker_start_ticks_not_digit")
        return None
    # #523 判据 4 —— 版本维度。上面那组 inode 是 per-host 身份,跨 host 不可比,所以
    # "两台 host 跑着两个不同的 FC / 两个不同的 guest kernel" 在旧证据里完全不可见:
    # 证据齐全、照样 PASS。这两个字段让混版在 rebuild 时可见,并随
    # image_ops.record_result(result=...) 一起进账本,运维比两台 host 即可看出分叉。
    #
    # 与其余字段同样【必填】:本函数的既有契约是"证据不全 = 不确认",给版本字段开
    # optional 就等于允许一台不报版本的 host 冒充证据齐全。在役老 host 上那份没有这两个
    # 字段的 rebuild-vm.sh 由同一条 SSM 命令里的 host_script_self_heal 先换掉
    # (freshness sentinel = guest_kernel_sha256),所以这不是"上线即打挂在役机队"。
    if not _FC_VERSION_RE.fullmatch(str(result.get("firecracker_version") or "")):
        # 最常见的契约漂移:host 侧 rebuild-vm.sh 太旧、不发 #523 的 freshness sentinel
        # (firecracker_version)。真机根因即此:S3 旧脚本 → 每次 rebuild 恒拒 → unconfirmed。
        print(
            f"{_tag} reason=firecracker_version_missing_or_invalid"
            f" value={result.get('firecracker_version')!r}"
            " (host rebuild-vm.sh may predate #523 — check deployed script version)"
        )
        return None
    if not _SHA256_RE.fullmatch(str(result.get("guest_kernel_sha256") or "")):
        print(
            f"{_tag} reason=guest_kernel_sha256_missing_or_invalid"
            f" value={result.get('guest_kernel_sha256')!r}"
            " (host rebuild-vm.sh may predate #523 — check deployed script version)"
        )
        return None
    tombstone = str(result.get("tombstone_dev_inode") or "")
    if tombstone and tombstone == result["overlay_dev_inode"]:
        print(f"{_tag} reason=tombstone_equals_overlay_inode")
        return None
    return result


def _parse_host_reset_result(
    stdout,
    tenant_id,
    op_id,
    attempt_id,
    fence_epoch,
    host_id,
    vm_num,
):
    """Return only identity-bound fresh-overlay evidence from reset-vm.sh."""
    result = None
    for line in reversed((stdout or "").splitlines()):
        try:
            candidate = json.loads(line)
        except Exception:
            continue
        if isinstance(candidate, dict) and candidate.get("state"):
            result = candidate
            break
    if not result:
        return None
    expected = {
        "tenant_id": tenant_id,
        "op_id": op_id,
        "attempt_id": attempt_id,
        "host_id": host_id,
        "vm_num": int(vm_num),
        "fence_epoch": int(fence_epoch),
    }
    if any(result.get(key) != value for key, value in expected.items()):
        return None
    if result.get("state") != "SUCCEEDED":
        return result
    inode = re.compile(r"^[0-9]+:[0-9]+$")
    required_inodes = (
        "firecracker_exe_dev_inode",
        "overlay_dev_inode",
        "overlay_fd_dev_inode",
    )
    if any(not inode.fullmatch(str(result.get(key) or "")) for key in required_inodes):
        return None
    if result["overlay_dev_inode"] != result["overlay_fd_dev_inode"]:
        return None
    if not str(result.get("firecracker_start_ticks") or "").isdigit():
        return None
    tombstone = str(result.get("tombstone_dev_inode") or "")
    if tombstone and tombstone == result["overlay_dev_inode"]:
        return None
    return result


# 终态三值。`unconfirmed` 是新增值,与 core/image_ops 的 STATE_UNKNOWN 同源同理:把
# "没能确认" 从 "确认失败" 里摘出来。见 _REBUILD_STATUS_UNCONFIRMED 注释。
_REBUILD_STATUS_DONE = "done"
# `failed` = 【确认】的失败(host 真跑了并给出非零退出码等),客户可安全重试。本轮没有
# 写入点:_ssm_run 返裸 bool,把"确认失败"和"没拿到回执"折叠成同一个 False,分不清就
# 只能报 unconfirmed(见下方写入处的说明)。值留在这里是因为它属于对外契约的一部分
# (openapi 已声明三值),由 §5.4a 的对账 MR 在能确认失败时写入。
_REBUILD_STATUS_FAILED = "failed"
_REBUILD_STATUS_UNCONFIRMED = "unconfirmed"


# 一次 rebuild 操作【专属】的字段。开新操作时必须整组原子重置 —— 见
# _stamp_rebuild_progress(new_operation=True) 的说明。
_REBUILD_OP_SCOPED_FIELDS = (
    "rebuild_op_id",
    "rebuild_phase",
    "rebuild_started_at",
    "rebuild_status",
    "rebuild_failed_reason",
    "rebuild_target_snapshot_time",
    "rebuild_ssm_command_id",
    "rebuild_lifecycle_fence_epoch",
    "rebuild_source_host_id",
    "rebuild_source_vm_num",
)


def _stamp_rebuild_progress(
    tenant_id,
    op_id=None,
    phase=None,
    started_at=None,
    target_snapshot_time=None,
    ssm_command_id=None,
    new_operation=False,
    fail_loud=False,
    fence_epoch=None,
    source_host_id=None,
    source_vm_num=None,
):
    """把 rebuild 的进度锚点写进本租户记录(ADR §5.3)。

    只写传进来的字段(None = 不碰),故同一函数可在链路各跳增量更新而不互相擦除。
    GET /tenants/{id} 返回整条记录、只剔 _TENANT_SECRET_FIELDS,所以字段落库即可被
    轮询读到,**GET 侧零改动**。

    new_operation=True —— 开启一次【新】操作:除本次传入的字段外,把上一轮遗留的
    operation-scoped 字段(见 _REBUILD_OP_SCOPED_FIELDS)在**同一次 update 里** REMOVE 掉。
    这不是洁癖,不清会直接坏掉轮询契约:上一轮成功后记录里留着 rebuild_status=done、
    上一轮的 CommandId 和 target。若只 SET 新的 op_id/phase/started_at,新操作会短暂地
    「新 op_id + 旧 done」并存 —— 客户匹配到自己的 op_id 后立刻读到 done,误判成功;更糟的是
    事后对账会拿【上一轮的 CommandId】去问 SSM,得到 Success 后把【这一轮】判成 done。
    单次 update_item 天然原子,故「清旧 + 立新」之间不存在可被读到的中间态。

    fail_loud=True —— 写失败时抛出而不是吞掉。用于「这次写入本身承载契约」的场合:
    入队后返回 202 之前必须已落 queued 锚点,否则客户拿到 op_id 却在记录里找不到它,
    轮询无从下手(而 202 已经承诺了可轮询)。默认 False 保持链路中段各跳的 best-effort
    语义 —— 那些跳拿不到进度远好于让一次本可成功的 rebuild 因为写监控字段而失败。

    只动 Key={"id": tenant_id} 的这一条记录(no-cross-tenant)。
    """
    sets, vals = [], {}
    for attr, value in (
        ("rebuild_op_id", op_id),
        ("rebuild_phase", phase),
        ("rebuild_started_at", started_at),
        ("rebuild_target_snapshot_time", target_snapshot_time),
        ("rebuild_ssm_command_id", ssm_command_id),
        ("rebuild_lifecycle_fence_epoch", fence_epoch),
        ("rebuild_source_host_id", source_host_id),
        ("rebuild_source_vm_num", source_vm_num),
    ):
        if value is None:
            continue
        placeholder = f":{attr}"
        sets.append(f"{attr} = {placeholder}")
        vals[placeholder] = value
    removes = []
    if new_operation:
        # 本次要 SET 的不能同时出现在 REMOVE 里(DynamoDB 会拒绝同一属性既 SET 又 REMOVE)。
        _setting = {a for a, v in (
            ("rebuild_op_id", op_id),
            ("rebuild_phase", phase),
            ("rebuild_started_at", started_at),
            ("rebuild_target_snapshot_time", target_snapshot_time),
            ("rebuild_ssm_command_id", ssm_command_id),
            ("rebuild_lifecycle_fence_epoch", fence_epoch),
            ("rebuild_source_host_id", source_host_id),
            ("rebuild_source_vm_num", source_vm_num),
        ) if v is not None}
        removes = [a for a in _REBUILD_OP_SCOPED_FIELDS if a not in _setting]
    if not sets and not removes:
        return
    if sets:
        sets.append("updated_at = :_rbp_t")
        vals[":_rbp_t"] = utils._now()
    expr = ("SET " + ", ".join(sets)) if sets else ""
    if removes:
        expr += (" " if expr else "") + "REMOVE " + ", ".join(removes)
    try:
        kwargs = {
            "Key": {"id": tenant_id},
            "UpdateExpression": expr,
        }
        if vals:
            kwargs["ExpressionAttributeValues"] = vals
        if op_id is not None and fence_epoch is not None:
            cond, fence_vals = lifecycle_fence.condition(op_id, fence_epoch)
            kwargs["ConditionExpression"] = cond
            kwargs.setdefault("ExpressionAttributeValues", {}).update(fence_vals)
        clients.tenants_table.update_item(**kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"rebuild progress stamp failed for {tenant_id}: {e}")
        if fail_loud:
            raise


def _release_lifecycle_ctx(tenant_id, ctx):
    if not ctx or ctx.get("hold_lifecycle_fence"):
        return
    op_id = ctx.get("lifecycle_op_id")
    epoch = ctx.get("lifecycle_fence_epoch")
    if op_id is None or epoch is None:
        return
    lifecycle_fence.release(tenant_id, op_id, epoch)
    ctx.pop("lifecycle_op_id", None)
    ctx.pop("lifecycle_fence_epoch", None)


def finalize_async_rebuild_failure(
    tenant_id,
    op_id,
    fence_epoch,
    reason,
):
    """Close an admitted rebuild that failed before destructive host work."""
    if not tenant_id or not op_id or fence_epoch is None:
        print("async rebuild terminal stamp skipped: incomplete worker identity")
        return
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET rebuild_phase = :failed, rebuild_status = :failed, "
                "rebuild_failed_reason = :reason, updated_at = :t"
            ),
            ConditionExpression=(
                "rebuild_op_id = :op AND rebuild_lifecycle_fence_epoch = :epoch"
            ),
            ExpressionAttributeValues={
                ":failed": _REBUILD_STATUS_FAILED,
                ":reason": str(reason)[:1000],
                ":t": utils._now(),
                ":op": op_id,
                ":epoch": int(fence_epoch),
            },
        )
    except Exception as e:  # noqa: BLE001
        print(f"async rebuild terminal stamp skipped for {tenant_id}/{op_id}: {e}")
    finally:
        lifecycle_fence.release(tenant_id, op_id, fence_epoch)


def finalize_async_rebuild_success(
    tenant_id,
    op_id,
    fence_epoch,
    reapply_binding,
):
    """Persist one verified reapply result, then release its exact lifecycle fence."""
    if not tenant_id or not op_id or fence_epoch is None:
        raise ValueError("async rebuild success finalization requires full identity")
    stamp_values = _reapply_stamp_values(reapply_binding)
    update_expression = (
        "SET config_template = :cfg_tpl, "
        "config_reapply_registry_version = :cfg_reg, "
        "config_reapply_body_version_id = :cfg_vid, "
        "config_reapply_body_sha256 = :cfg_sha"
    )
    if "target_image_snapshot_time" in reapply_binding:
        update_expression += (
            ", config_reapply_image_snapshot_time = :cfg_img"
        )
    else:
        stamp_values.pop(":cfg_img", None)
    update_expression += ", updated_at = :t"
    clients.tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression=update_expression,
        ConditionExpression=(
            "rebuild_op_id = :op AND rebuild_status = :done AND "
            "rebuild_lifecycle_fence_epoch = :epoch AND "
            "active_lifecycle_op_id = :op AND lifecycle_fence_epoch = :epoch"
        ),
        ExpressionAttributeValues={
            **stamp_values,
            ":t": utils._now(),
            ":op": op_id,
            ":done": _REBUILD_STATUS_DONE,
            ":epoch": int(fence_epoch),
        },
    )
    if not lifecycle_fence.release(tenant_id, op_id, fence_epoch):
        raise RuntimeError(
            f"async rebuild success fence release lost ownership for {tenant_id}/{op_id}"
        )


def finalize_async_rebuild_already_applied(
    tenant_id,
    op_id,
    fence_epoch,
):
    """Close a queued legacy worker retry that discovers the target is applied."""
    if not tenant_id or not op_id or fence_epoch is None:
        raise ValueError("async rebuild no-op finalization requires full identity")
    clients.tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression=(
            "SET rebuild_phase = :done, rebuild_status = :done, updated_at = :t "
            "REMOVE rebuild_failed_reason"
        ),
        ConditionExpression=(
            "rebuild_op_id = :op AND rebuild_lifecycle_fence_epoch = :epoch AND "
            "active_lifecycle_op_id = :op AND lifecycle_fence_epoch = :epoch"
        ),
        ExpressionAttributeValues={
            ":t": utils._now(),
            ":op": op_id,
            ":done": _REBUILD_STATUS_DONE,
            ":epoch": int(fence_epoch),
        },
    )
    if not lifecycle_fence.release(tenant_id, op_id, fence_epoch):
        raise RuntimeError(
            f"async rebuild no-op fence release lost ownership for {tenant_id}/{op_id}"
        )


def tenant_action(tenant_id, action, body=None, event=None):
    """POST /tenants/{id}/{action} 的入口。

    return 退出(tenant_action 内部有 36 个 return)的结果统一写成 result。
    为什么用包装而不是在每个 return 前加 finish():36 处逐一插入,漏一处就会让那条 idem
    记录永久停在 IN_PROGRESS —— 该客户带同一 token 的后续请求会被 409 挡死,再也发不出这个
    操作。包装还能兜住异常路径(内层抛异常时落 UNKNOWN 而不是留 IN_PROGRESS)。

    是否登记幂等由内层决定(它解析 body 才知道有没有 client_token),故内层把用到的
    (owner, token) 通过 _idem_ctx 回传给这层。
    """
    if action == "upgrade":
        action = "rebuild"
        body = _normalize_v1_upgrade_body(body)
    _ctx = {}
    if (
        action in action_idem.IDEMPOTENT_ACTIONS
        and "_consumer_ident" in (event or {})
        and isinstance(body, dict)
    ):
        # Restore the queue-owned intent before any tenant lookup or ownership
        # guard. Early 404/4xx returns and lookup exceptions must also finalize
        # the record instead of leaving it IN_PROGRESS forever.
        _queued_token = body.get("client_token")
        if isinstance(_queued_token, str) and _queued_token.strip():
            _ctx["token"] = _queued_token.strip()
            _ctx["owner"] = str(
                ((event or {}).get("_consumer_ident") or {}).get("owner_id") or ""
            )
    try:
        resp = _tenant_action_inner(tenant_id, action, body, event, _ctx)
    except Exception:
        # 内层抛异常 = 结果未知(SSM 可能已下发)。落 UNKNOWN 而非 FAILED:image_ops 已
        # 论证过,落 FAILED 会让同 token 的重试被永久挡死,违背"重试可对账"。
        if _ctx.get("token"):
            action_idem.finish(
                tenant_id, action, _ctx["owner"], _ctx["token"],
                action_idem.STATE_UNKNOWN,
                {"error": "action raised before completing", "id": tenant_id},
            )
        if not _ctx.get("release_lifecycle_fence_on_error"):
            _ctx["hold_lifecycle_fence"] = True
        _release_lifecycle_ctx(tenant_id, _ctx)
        raise
    if _ctx.get("token") and not _ctx.get("defer_finish"):
        _code = int((resp or {}).get("statusCode") or 0)
        try:
            _body = json.loads((resp or {}).get("body") or "{}")
        except Exception:  # noqa: BLE001
            _body = {}
        # LIFECYCLE_IN_FLIGHT is a pre-dispatch conflict: this operation did not
        # reach the host or queue and the caller is expected to retry later.
        # Do not poison that retry by storing the token as terminal FAILED.
        if _body.get("code") == "LIFECYCLE_IN_FLIGHT":
            _state = action_idem.STATE_NOT_STARTED
        # 2xx = 确定成功;4xx = 确定失败(客户端错误,重试同样会失败,可安全记 FAILED);
        # 5xx = **结果未知**(SSM 可能已下发) → UNKNOWN,允许同 token 重放去对账。
        elif 200 <= _code < 300:
            _state = action_idem.STATE_SUCCEEDED
        elif 400 <= _code < 500:
            _state = action_idem.STATE_FAILED
        elif _body.get("code") == "ENQUEUE_ANCHOR_FAILED":
            # The lifecycle fence was not persisted, so no queue message or
            # host command could have been sent. Keep this distinct from the
            # genuinely ambiguous post-send failure state.
            _state = action_idem.STATE_NOT_STARTED
        else:
            _state = action_idem.STATE_UNKNOWN
        action_idem.finish(
            tenant_id, action, _ctx["owner"], _ctx["token"], _state, _body
        )
    if (
        action == "rebuild"
        and (event or {}).get("_defer_async_rebuild_success_finalize")
        and 200 <= int((resp or {}).get("statusCode") or 0) < 300
    ):
        _ctx["hold_lifecycle_fence"] = True
    if (
        int((resp or {}).get("statusCode") or 0) >= 500
        and not _ctx.get("release_lifecycle_fence_on_error")
    ):
        _ctx["hold_lifecycle_fence"] = True
    _release_lifecycle_ctx(tenant_id, _ctx)
    return resp


def _tenant_action_inner(tenant_id, action, body=None, event=None, _idem_ctx=None):
    # Rebuild drops the VM's writable rootfs overlay and can change its image
    # channel. It is an administrator-only operation. Check before tenant lookup
    # so an unauthorized caller cannot probe whether a tenant id exists.
    if action == "rebuild" and not auth._get_caller_identity(event or {}).get(
        "is_admin"
    ):
        return utils._resp(
            403,
            {"error": "rebuild requires admin role", "code": "ACCESS_DENIED"},
        )

    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return utils._resp(404, {"error": "tenant not found"})
    # #339 目标① —— 本次操作【决策时看到的】status,供终态回写做 CAS(见下方 :6706 附近)。
    #
    # 为什么必须在这里快照、而不是到写点再读 `item.get("status")`:下方 #321 的入口门
    # (:5157)是 TOCTOU 的 T,终态回写才是 U。两者之间隔着最长 300s 的 SSM 往返
    # (`_ssm_run(..., timeout=300)`),入口门对那段窗口无能为力。快照钉在门之前,于是
    # 「我据以放行的那个状态」与「我回写时要求的那个状态」是同一个值 —— 中途被任何人
    # 改过,CAS 就必须失败。到写点再读会随 item 的任何重读(rebuild 分支 :6252 就重读了)
    # 一起漂,那样的"条件"只是把无条件覆盖包了一层壳。
    _entry_status = item.get("status")
    # issue #80 — IDOR: gate every action (start/stop/restart/migrate/resize/
    # backup/…) on ownership. Checked once here so all branches are covered.
    denied = auth._assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied

    # #321 — 已删/删除中租户拒绝【一切改状态/动 VM 的动作】。既是正确性(删了的租户不该能
    # start/stop/migrate…),也堵住磁盘 GC 的 TOCTOU(codex 复审):GC 强一致读到 deleted 后到
    # rm 之间,若被 start/restart 拉起、或 stop/pause/migrate 把状态改回 stopped/migrating
    # 复活租户,会误删新盘或破坏 GC 判据。只读/无害动作(access 等)不在此列,不受影响。
    _mutating_actions = {
        "start",
        "stop",
        "restart",
        "pause",
        "resume",
        "reset",
        "rebuild",
        "migrate",
        "resize",
        "resize-disk",
        # #422 — 休眠/恢复是改状态+动 VM 的动作,继承对 deleted/deleting 的 409 终态闸
        # (已删/删除中的租户不该能 suspend/restore)。
        "suspend",
        "restore",
    }
    if action in _mutating_actions and item.get("status") in ("deleted", "deleting"):
        return utils._resp(
            409,
            {"error": f"tenant is {item['status']}; cannot {action}", "id": tenant_id},
        )

    # #624 —— failed 是墓碑,requires_intervention 明确不会自行 reset;start/restart
    # 在这两种状态上从受理时就注定失败。API 路径在入队前拒绝,consumer 路径则覆盖
    # 「受理后、真正执行前状态恶化」的窗口并落 fail_reason。这里必须返 4xx:consumer
    # 会 ack 4xx,只有 >=500 才留在 batchItemFailures;用 409 可避免注定失败的消息占住
    # per-tenant FIFO 组头、继续阻塞客户唯一的 stop/delete/rebuild 出路。
    # #679 —— `suspend_failed`(host 亡档的新终态)同属"从受理时就注定失败":它没有 VM
    # (host 已亡),start/restart 走底部通用块只会造出「无 VM 却 running」的谎报。
    # 指引按备份分档:有备份 → restore(接受回退到备份时刻)或 delete;无备份 → 仅 delete。
    if action in ("start", "restart") and item.get("status") in (
        "failed",
        "requires_intervention",
        "suspend_failed",
    ):
        _mark_fail_reason(
            tenant_id, action, create_deadline.REASON_TENANT_NOT_STARTABLE
        )
        _status = item.get("status")
        if _status == "failed":
            _guidance = "rebuild the tenant with a different client_token"
        elif _status == "suspend_failed":
            _guidance = (
                "restore it (data rolls back to the backup taken during the failed "
                "suspend) or delete it"
                if item.get("suspend_backup_key")
                else "delete it (the host died before a backup completed; data is "
                "not recoverable)"
            )
        else:
            _guidance = "stop the tenant first to return it to stopped, then start it"
        return utils._resp(
            409,
            {
                "error": f"tenant is {_status}; cannot {action}. To recover, {_guidance}.",
                "code": "TENANT_NOT_STARTABLE",
                "id": tenant_id,
                "status": _status,
            },
        )

    # #422 codex round3 #3 — 休眠态(suspending/suspended/restoring)下,常规动 VM 的动作
    # (start/stop/restart/pause/resume/reset/rebuild/migrate/resize/resize-disk)必须拒绝:
    # suspended 租户本地无 VM(已删)、host_id 可能指向已释放的旧 slot,对它 restart/pause 会
    # 走底部通用块无条件覆盖 status(留"无 VM 却 running"或抢占别人 slot 的活 VM=未记账孤儿)。
    # 仅 suspend/restore 两个动作能作用于休眠态(它们自带精确前置校验:suspend 要 running/
    # stopped,restore 要 suspended),故从本闸排除。suspend 对 suspended 幂等、restore 对
    # running 幂等都在各自 helper 内处理。
    # #456 —— 并发/连发 rebuild 闸。真机 2026-08-12:对同一租户连发两次 rebuild,第二次跑到
    # launch-vm 时第一次仍持 per-tenant flock,launch-vm.sh exit 75 让位,而命令链
    # `stop && rm overlay && sleep && launch && verify` 已经在 `&& launch` 之前把 **overlay
    # 删掉了** —— 破坏已经发生,verify 根本没跑到。
    #
    # P2 的 host transaction 现已让 same-op retry 不会重复 tombstone；此处仍拒绝不同 op
    # 并发，避免两个独立 rebuild 依次提交各自的破坏点。
    #
    # 判据用 rebuild_phase 的非终态(queued/running/verifying)。**必须带超时兜底**:若上一次
    # rebuild 的进程崩在中途、没能写终态,没有超时的闸会把这个租户永久锁死在"无法 rebuild"。
    # 超时值复用 §5.5 的 unconfirmed 兜底常量语义(默认 30 分钟远大于一次 rebuild 的正常收敛
    # 时间),超时后放行:此时 §5.4a 的对账也已经把上一次收成 done/failed 了。
    if action == "rebuild":
        _inflight_phase = (item.get("rebuild_phase") or "").strip()
        if _inflight_phase in _REBUILD_INFLIGHT_PHASES:
            _incoming_op = (event or {}).get("_op_id")
            _same_op = bool(
                _incoming_op and _incoming_op == item.get("rebuild_op_id")
            )
            if (
                not _same_op
                and not _rebuild_inflight_is_stale(item.get("rebuild_started_at"))
            ):
                return utils._resp(
                    409,
                    {
                        "error": "a rebuild is already in flight for this tenant "
                        f"(phase={_inflight_phase}). Concurrent rebuilds are refused "
                        "because each one drops the per-VM overlay — running two would "
                        "discard writes made in between. Poll GET /tenants/{id} until "
                        "rebuild_status is done/failed, then retry if needed.",
                        "code": "REBUILD_IN_FLIGHT",
                        "id": tenant_id,
                        "rebuild_phase": _inflight_phase,
                        "rebuild_op_id": item.get("rebuild_op_id"),
                    },
                )
        _incoming_op = (event or {}).get("_op_id")
        _source_host = (item.get("rebuild_source_host_id") or "").strip()
        _source_vm = item.get("rebuild_source_vm_num")
        _source_identity_changed = (
            bool(_source_host)
            and _source_host != (item.get("host_id") or "").strip()
        ) or (
            _source_vm is not None
            and int(_source_vm) != int(item.get("vm_num", 1))
        )
        if (
            _incoming_op
            and _incoming_op == item.get("rebuild_op_id")
            and _source_identity_changed
        ):
            return utils._resp(
                409,
                {
                    "error": "the tenant moved to another host or VM slot after this "
                    "rebuild started; the old operation is superseded and will not "
                    "run there",
                    "code": "LIFECYCLE_SUPERSEDED",
                    "id": tenant_id,
                    "op_id": _incoming_op,
                },
            )

    _hibernate_states = ("suspending", "suspended", "restoring")
    if (
        action in _mutating_actions
        and action not in ("suspend", "restore")
        and item.get("status") in _hibernate_states
    ):
        return utils._resp(
            409,
            {
                "error": f"tenant is {item['status']} (hibernated/in-flight); only "
                f"restore/suspend apply — cannot {action}",
                "id": tenant_id,
            },
        )

    # #547 建议项 —— rebuild 一直缺一道 status 前置条件。
    #
    # 真机复现(#547 issue 正文):对一个还在 `creating`(冷恢复未把 data.ext4 落盘)的租户
    # 发 rebuild → 强制前置备份找不到源盘 → 按 no-data-loss 铁律拒绝继续 → 502。那个 502
    # 本身是对的,但整条路压根不该被受理:在它之前 rebuild 已经取了生命周期租约、并发出一次
    # 注定失败的强制备份 SSM。对照 suspend 有 running/stopped 前置、restore 有 suspended
    # 前置,而 rebuild 只有上方那道 REBUILD_IN_FLIGHT 闸 —— 它只拦"同租户已有 rebuild
    # 在飞",不看 status。
    #
    # **只补 `creating`**:issue 原文写的是「creating/restoring 直接 409」,但 `restoring`
    # 已被上面 `_hibernate_states` 那道闸拦住(:5243),再列一遍是死代码。
    #
    # 位置在**任何 lifecycle_fence.acquire 之前**,所以连租约都不取 —— 这正是 issue 说的
    # 「连那次无意义的强制备份 SSM 都省了」。
    if action == "rebuild" and item.get("status") == "creating":
        return utils._resp(
            409,
            {
                "error": "tenant is still creating; its data disk may not be on the host "
                "yet, so the mandatory pre-repin backup would fail and abort the rebuild. "
                "Wait until status=running, then rebuild.",
                "code": "REBUILD_TENANT_NOT_READY",
                "id": tenant_id,
                "status": "creating",
            },
        )

    # Destructive actions accept an optional object body. Rebuild additionally
    # reads image selection fields; reset reads client_token only.
    _rebuild_body = {}
    if action in action_idem.IDEMPOTENT_ACTIONS:
        _raw = body
        if isinstance(_raw, str):
            _raw_stripped = _raw.strip()
            if _raw_stripped:
                try:
                    _parsed = json.loads(_raw_stripped)
                except Exception:
                    return utils._resp(
                        400,
                        {
                            "error": f"{action} body is not valid JSON",
                            "code": "VALIDATION",
                        },
                    )
                if not isinstance(_parsed, dict):
                    return utils._resp(
                        400,
                        {
                            "error": f"{action} body must be a JSON object",
                            "code": "VALIDATION",
                        },
                    )
                _rebuild_body = _parsed
        elif isinstance(_raw, dict):
            _rebuild_body = _raw
        elif _raw is not None:
            return utils._resp(
                400,
                {
                    "error": f"{action} body must be a JSON object",
                    "code": "VALIDATION",
                },
            )
    # image_channel 必须是字符串(非 string 如 123 会让 .strip() 抛 → 500);非法即 400。
    _ic = _rebuild_body.get("image_channel")
    if action == "rebuild" and _ic is not None and not isinstance(_ic, str):
        return utils._resp(
            400, {"error": "image_channel must be a string", "code": "VALIDATION"}
        )
    # #429 fast pre-screen.  This block is before client-token intent writes,
    # lifecycle fencing, progress stamps, backup, and destructive host commands.
    _reapply_binding = None
    _reapply_request = None
    _rebuild_verified = False
    _reapply_admission_resolved = None
    if action == "rebuild":
        _trusted_binding = (
            _rebuild_body.get("_config_reapply")
            if "_consumer_ident" in (event or {})
            else None
        )
        if isinstance(_trusted_binding, dict):
            _reapply_binding = dict(_trusted_binding)
        else:
            _reapply_request = _extract_reapply_request(action, _rebuild_body)
            if _reapply_requested(_reapply_request):
                _reapply_admission_resolved = _rebuild_repin_resolve(
                    item, _rebuild_body
                )
                if not (
                    isinstance(_reapply_admission_resolved, dict)
                    and "channel" in _reapply_admission_resolved
                ):
                    return _reapply_admission_resolved
                try:
                    _reapply_binding = _prepare_config_reapply(
                        item,
                        _reapply_request,
                        _reapply_admission_resolved,
                    )
                except ValueError as exc:
                    return utils._resp(
                        400,
                        {"error": str(exc), "code": "VALIDATION"},
                    )
                except LookupError as exc:
                    return utils._resp(
                        400,
                        {
                            "error": f"openclaw.json 不兼容: {exc}",
                            "code": "OPENCLAW_CONFIG_INCOMPATIBLE",
                        },
                    )
    # #456 / ADR §5.1 —— client_token 幂等。客户接口表声明 rebuild 的 client_token「供上游
    # 做幂等」,而控制面此前全文零命中该字段:承诺了却没实现。
    #
    # 为什么 REBUILD_IN_FLIGHT 闸不够:那道闸只拦「上一次还在飞」。而客户重试的典型时机是
    # 上一次**已经收敛**之后 —— 响应丢了(API GW 29s 断开/客户端超时),服务端其实早已 done。
    # 那时闸放行,于是又跑一遍 stop && rm overlay && launch,抹掉两次之间落盘的写入。
    # token 幂等补的正是这一格:同一 token 得到同一答案,绝不重跑破坏性步骤。
    #
    # 只对 IDEMPOTENT_ACTIONS(rebuild/reset)启用:start/stop/restart 的 host 脚本本身幂等,
    # 重复执行是安全 no-op,给它们加记录只增加写放大与失败面。
    # consumer replay(带 _consumer_ident)不走这道闸:它是同一操作的继续执行,不是新请求;
    # 让它再撞一次自己的 intent 会把重投挡死。
    _idem_token = ""
    _idem_op_id = None
    # 判【键是否存在】而不是取值真假:consumer 传的是 `{"_consumer_ident": ident}`,而
    # ident 来自 `msg.get("_ident") or {}`(handler.py:1626)—— api-key 路径下它就是**空
    # dict**,取值是 falsy。用真假判断会把 SQS 重投误判成"新的客户请求",于是重投去撞自己
    # 那条 intent、被 409 挡死 → 操作永远完不成。仓库其他地方的 `not ...get(...)` 都有同一
    # 隐患,只是它们的后果是"多入一次队"(既有幂等能兜),而这里的后果是"操作卡死"。
    if action in action_idem.IDEMPOTENT_ACTIONS:
        _raw_idem_token = _rebuild_body.get("client_token")
        if "_consumer_ident" in (event or {}):
            # The producer already validated this trusted queue field. Preserve
            # compatibility with old queued messages that predate validation.
            _idem_token = (
                _raw_idem_token.strip()
                if isinstance(_raw_idem_token, str)
                else ""
            )
        else:
            _idem_token, _token_error = _normalize_client_token(_raw_idem_token)
            if _token_error:
                return utils._err(400, "VALIDATION", _token_error)
    _idem_owner = ""
    if _idem_token:
        _idem_owner = str(
            (auth._get_caller_identity(event or {}) or {}).get("owner_id") or ""
        )
        _consumer_replay = "_consumer_ident" in (event or {})
        if _consumer_replay:
            _idem_op_id = (event or {}).get("_op_id") or action_idem.derive_op_id(
                _idem_owner, tenant_id, action, _idem_token
            )
            _idem_existing = None
        else:
            _idem_op_id, _idem_existing = action_idem.begin(
                tenant_id, action, _idem_owner, _idem_token
            )
        # The consumer owns finalization for queued work. It skips begin(), but
        # still restores this context from the signed queue envelope.
        if _idem_existing is None and _idem_ctx is not None:
            _idem_ctx["owner"] = _idem_owner
            _idem_ctx["token"] = _idem_token
        if _idem_existing is not None:
            _kind, _payload = action_idem.replay_decision(_idem_existing)
            if _kind == "return":
                # 有确定答案 → 原样返回。这是幂等的核心:同一 token 得到同一答案,
                # 而不是"第二次调用看到的新世界"里的另一个答案。
                print(
                    f"action_idem replay {tenant_id}/{action}: returning stored result"
                )
                return utils._resp(200, _payload or {"id": tenant_id})
            if _kind == "poll":
                # 仍在跑 → 409 + op_id,让调用方轮询而不是重试。
                return utils._resp(
                    409,
                    {
                        "error": "an operation with this client_token is already in "
                        "progress; poll GET /tenants/{id} instead of retrying — a "
                        "duplicate rebuild drops the per-VM overlay again and discards "
                        "writes made in between",
                        "code": "IDEMPOTENT_OPERATION_IN_PROGRESS",
                        "id": tenant_id,
                        "op_id": _payload,
                    },
                )
            # _kind == "rerun"(UNKNOWN):可能已执行也可能没 —— 放行去做对账。
            # 绝不在这里返回"失败":image_ops 已论证过,把未知落成失败会让同 token 重试
            # 被永久挡死,违背"重试可对账"。放行后仍受下方 REBUILD_IN_FLIGHT 闸约束。
            _retry_state = (_idem_existing or {}).get("state")
            if not action_idem.claim_rerun(
                tenant_id,
                action,
                _idem_owner,
                _idem_token,
                _retry_state,
            ):
                return utils._resp(
                    409,
                    {
                        "error": "another retry already resumed this operation; "
                        "wait for its terminal result",
                        "code": "IDEMPOTENT_OPERATION_IN_PROGRESS",
                        "id": tenant_id,
                        "op_id": _payload,
                    },
                )
            if _idem_ctx is not None:
                _idem_ctx["owner"] = _idem_owner
                _idem_ctx["token"] = _idem_token
            print(
                f"action_idem replay {tenant_id}/{action}: "
                f"{(_idem_existing or {}).get('state')} → re-running"
            )

    _lifecycle_op_id = (event or {}).get("_op_id")
    if action in _FENCED_LIFECYCLE_ACTIONS:
        _lifecycle_op_id = _lifecycle_op_id or _idem_op_id or secrets.token_hex(16)
        event = dict(event or {})
        event["_op_id"] = _lifecycle_op_id

    # Rebuild has one business flow for both live and canary. The HTTP request
    # performs only admission, fencing, and pollable progress anchoring, then
    # asynchronously self-invokes this Lambda. The worker re-enters the single
    # rebuild branch below with the same op_id and caller identity.
    if action == "rebuild" and "_consumer_ident" not in (event or {}):
        try:
            _dispatch_fence_epoch, _fence_reason = lifecycle_fence.acquire(
                tenant_id, _lifecycle_op_id, action
            )
        except Exception as e:  # noqa: BLE001
            print(f"rebuild fence anchor failed for {tenant_id}: {e}")
            return utils._resp(
                503,
                {
                    "error": "could not fence the rebuild before dispatch; "
                    "nothing was started - safe to retry",
                    "code": "ENQUEUE_ANCHOR_FAILED",
                    "id": tenant_id,
                },
            )
        if _dispatch_fence_epoch is None:
            return utils._resp(
                409,
                {
                    "error": _fence_reason,
                    "code": "LIFECYCLE_IN_FLIGHT",
                    "id": tenant_id,
                    "op_id": _lifecycle_op_id,
                },
            )
        if _idem_ctx is not None:
            _idem_ctx.update(
                {
                    "lifecycle_op_id": _lifecycle_op_id,
                    "lifecycle_fence_epoch": _dispatch_fence_epoch,
                    "hold_lifecycle_fence": True,
                }
            )

        # The first read may have raced with a migration that released its
        # fence just before this rebuild acquired it. Re-read under our fence
        # so validation and the source identity anchor describe one placement.
        try:
            item = clients.tenants_table.get_item(
                Key={"id": tenant_id}, ConsistentRead=True
            ).get("Item")
            _resolved = (
                _rebuild_repin_resolve(item, _rebuild_body) if item else None
            )
        except Exception as e:  # noqa: BLE001
            lifecycle_fence.release(
                tenant_id, _lifecycle_op_id, _dispatch_fence_epoch
            )
            if _idem_ctx is not None:
                _idem_ctx["hold_lifecycle_fence"] = False
                _idem_ctx["release_lifecycle_fence_on_error"] = True
            print(f"rebuild admission failed before invoke for {tenant_id}: {e}")
            return utils._resp(
                503,
                {
                    "error": "could not validate the rebuild before dispatch; "
                    "nothing was started - safe to retry",
                    "code": "ENQUEUE_ANCHOR_FAILED",
                    "id": tenant_id,
                },
            )
        if not item:
            lifecycle_fence.release(
                tenant_id, _lifecycle_op_id, _dispatch_fence_epoch
            )
            if _idem_ctx is not None:
                _idem_ctx["hold_lifecycle_fence"] = False
            return utils._resp(404, {"error": "tenant not found"})
        if not (isinstance(_resolved, dict) and "channel" in _resolved):
            lifecycle_fence.release(
                tenant_id, _lifecycle_op_id, _dispatch_fence_epoch
            )
            if _idem_ctx is not None:
                _idem_ctx["hold_lifecycle_fence"] = False
            return _resolved

        if _reapply_binding:
            # The host live/canary slot can move between the pre-screen and
            # fence acquisition. Rebind under the fence so validation, no-op
            # detection, worker payload, and the eventual stamp name the same
            # exact image snapshot.
            if (
                _reapply_binding.get("target_image_snapshot_time", "")
                != _resolved_reapply_snapshot(_resolved)
            ):
                try:
                    _reapply_binding = _prepare_config_reapply(
                        item,
                        _reapply_request
                        or {
                            "config_template": _reapply_binding.get(
                                "config_template"
                            ),
                            "config_template_version": _reapply_binding.get(
                                "registry_version"
                            ),
                            "force_reapply": True,
                        },
                        _resolved,
                    )
                except ValueError as exc:
                    if _idem_ctx is not None:
                        _idem_ctx["hold_lifecycle_fence"] = False
                    return utils._resp(
                        400, {"error": str(exc), "code": "VALIDATION"}
                    )
                except LookupError as exc:
                    if _idem_ctx is not None:
                        _idem_ctx["hold_lifecycle_fence"] = False
                    return utils._resp(
                        400,
                        {
                            "error": f"openclaw.json 不兼容: {exc}",
                            "code": "OPENCLAW_CONFIG_INCOMPATIBLE",
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    if _idem_ctx is not None:
                        _idem_ctx["hold_lifecycle_fence"] = False
                        _idem_ctx["release_lifecycle_fence_on_error"] = True
                    print(
                        f"rebuild reapply rebind failed before invoke for "
                        f"{tenant_id}: {exc}"
                    )
                    return utils._resp(
                        503,
                        {
                            "error": "could not revalidate the config reapply "
                            "target before dispatch; nothing was started - safe "
                            "to retry",
                            "code": "ENQUEUE_ANCHOR_FAILED",
                            "id": tenant_id,
                        },
                    )
            if _reapply_already_applied(item, _reapply_binding, _resolved):
                if _idem_ctx is not None:
                    _idem_ctx["hold_lifecycle_fence"] = False
                return _reapply_already_applied_response(
                    tenant_id, item, _reapply_binding
                )

        # #564 G2(通道 C)—— rebuild 走 Lambda 自调用而不是 SQS,但死线口径必须同一份:
        # 受理时刻算出绝对 epoch,写进【租户行】与【事件 payload】。这里就是受理时刻 ——
        # 本块只在 `"_consumer_ident" not in event` 时进(见 :5651 的条件),worker 重入时
        # 走的是另一条路,不会重算。
        _accepted_epoch = int(time.time())
        _dl_epoch, _dl_src = deadline_config.deadline_epoch_for(
            create_deadline.ACTION_REBUILD, _accepted_epoch
        )
        if _dl_src != "ssm":
            print(
                f"rebuild_deadline: {_dl_epoch - _accepted_epoch}s "
                f"from {_dl_src} for {tenant_id}"
            )
        try:
            _stamp_rebuild_progress(
                tenant_id,
                op_id=_lifecycle_op_id,
                phase=_REBUILD_PHASE_QUEUED,
                started_at=utils._now(),
                target_snapshot_time=_resolved.get("target_snap") or None,
                new_operation=True,
                fail_loud=True,
                fence_epoch=_dispatch_fence_epoch,
                source_host_id=item.get("host_id"),
                source_vm_num=int(item.get("vm_num", 1)),
            )
            # 与 SQS 那两条通道同一条顺序纪律:死线必须在**发起之前**落到行上,否则
            # `deadline_executor` 扫不到这一行(它的 filter 第一条是
            # `attribute_exists(rebuild_deadline)`),这次 rebuild 永远不会被判死。
            # 放在这个 try 里面是刻意的:写失败会走下面那条既有分支 —— 放掉围栏 + 503
            # 「什么都没开始,可安全重试」,那正是这一步失败时该给的答复。
            _write_action_deadline(
                tenant_id, create_deadline.ACTION_REBUILD, _dl_epoch
            )
        except Exception as e:  # noqa: BLE001
            lifecycle_fence.release(
                tenant_id, _lifecycle_op_id, _dispatch_fence_epoch
            )
            if _idem_ctx is not None:
                _idem_ctx["hold_lifecycle_fence"] = False
                _idem_ctx["release_lifecycle_fence_on_error"] = True
            print(f"rebuild dispatch aborted before invoke for {tenant_id}: {e}")
            return utils._resp(
                503,
                {
                    "error": "could not record the rebuild before dispatch; "
                    "nothing was started - safe to retry",
                    "code": "ENQUEUE_ANCHOR_FAILED",
                    "id": tenant_id,
                },
            )

        ident = auth._get_caller_identity(event or {})
        worker_body = dict(_rebuild_body)
        if _reapply_binding:
            # Trusted producer-owned evidence; caller-supplied copies are ignored.
            worker_body["_config_reapply"] = _reapply_binding
        if _resolved["channel"] == "canary" and _resolved.get("target_snap"):
            # Freeze the canary selected during admission. A promotion or pull
            # between HTTP 202 and worker start must fail the existing expected-
            # snapshot guard, not silently rebuild onto a different candidate.
            worker_body.setdefault(
                "expected_image_snapshot_time", _resolved["target_snap"]
            )
        worker_payload = {
            "_async_rebuild": {
                "tenant_id": tenant_id,
                "body": worker_body,
                "_op_id": _lifecycle_op_id,
                "_fence_epoch": _dispatch_fence_epoch,
                # #564 G2/G3 —— 与租户行 `rebuild_deadline` 是【同一个值】(上面算的
                # `_dl_epoch`)。键名复用 `MSG_DEADLINE_KEY`,与 SQS 两条通道同一个口径:
                # 消费侧那段判过期的代码不必按通道分叉。
                create_deadline.MSG_DEADLINE_KEY: _dl_epoch,
                "_ident": {
                    "owner_id": ident.get("owner_id"),
                    "is_admin": ident.get("is_admin"),
                    "platform_scope": ident.get("platform_scope"),
                    "tenant_user_id": ident.get("tenant_user_id"),
                },
            }
        }
        try:
            boto3.client("lambda").invoke(
                FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
                InvocationType="Event",
                Payload=json.dumps(worker_payload).encode("utf-8"),
            )
        except Exception as e:  # noqa: BLE001
            # A transport timeout can happen after Lambda accepted the event.
            # Keep the fence and queued anchor so callers can poll this exact
            # operation instead of starting a second destructive rebuild.
            print(f"rebuild async dispatch state unknown for {tenant_id}: {e}")
            return utils._resp(
                503,
                {
                    "error": "the async worker did not acknowledge the rebuild, but "
                    "it may have been accepted. Do NOT blindly retry; poll this "
                    "tenant's rebuild_phase/rebuild_status first.",
                    "code": "ENQUEUE_STATE_UNKNOWN",
                    "id": tenant_id,
                    "op_id": _lifecycle_op_id,
                },
            )
        if _idem_token and _idem_ctx is not None:
            _idem_ctx["defer_finish"] = True
        return utils._resp(
            202,
            # #565 G6 —— rebuild 走通道 C(Lambda 自调用),同样在七档里。`_dl_epoch` 是上面
            # 算定、已写进租户行与 worker payload 的那一个值,三处同源。
            _with_action_deadline(
                {
                    "id": tenant_id,
                    "action": "rebuild",
                    "status": "queued",
                    "op_id": _lifecycle_op_id,
                    **(
                        {
                            # #579 Bug3 —— accepted 响应显式回 reapply_requested=true + 冻结坐标,
                            # 让调用方确认服务端【识别并受理】了 reapply(而非旧部署静默忽略字段后
                            # 只回普通 rebuild 的 done)。旧部署不含本块 → 调用方据缺失判定被忽略。
                            "reapply_requested": True,
                            "registry_version": _reapply_binding["registry_version"],
                            "body_version_id": _reapply_binding.get(
                                "body_version_id", ""
                            ),
                            "body_sha256": _reapply_binding.get("body_sha256", ""),
                        }
                        if _reapply_binding
                        else {}
                    ),
                },
                create_deadline.ACTION_REBUILD,
                deadline_epoch=_dl_epoch,
            ),
        )

    # 控制面重构阶段1 — 产端入队:纯 lifecycle 动作(start/stop/restart/pause/resume)
    # 只是经 SSM 下发、无特殊同步返回值,队列开启时入 SQS 由 consumer 受控并发消费
    # (削峰 + 限流阀,治 1000/s 雪崩),立即返 202。resize/backup/migrate/access 等
    # 有同步返回语义的不入队,保持原同步路径。开关关 → 全走同步(向后兼容)。
    # 防重入:consumer 重放时 event 带 _consumer_ident,不再二次入队。
    # #422 — suspend/restore 加入 _async_actions:两者含同步备份/冷恢复(下载解密解压
    # e2fsck),真机耗时可达 20-30s+(会议压测),撞 API GW 29s 同步硬超时。队列开启时入队
    # 由 consumer 异步执行(无 29s 限制),立即返 202+轮询(会议"走现有 Job 轮询链");队列
    # 未开则回退同步(小部署可接受)。consumer 重放带 _consumer_ident,不再二次入队。
    _async_actions = {
        "start",
        "stop",
        "restart",
        "pause",
        "resume",
        "reset",
        "suspend",
        "restore",
    }
    if (
        action in _async_actions
        and clients.LIFECYCLE_QUEUE_URL
        and "_consumer_ident" not in (event or {})
    ):
        _queue_fence_epoch = None
        if action in _FENCED_LIFECYCLE_ACTIONS:
            try:
                _queue_fence_epoch, _fence_reason = lifecycle_fence.acquire(
                    tenant_id, _lifecycle_op_id, action
                )
            except Exception as e:  # noqa: BLE001
                print(f"lifecycle fence anchor failed for {tenant_id}: {e}")
                return utils._resp(
                    503,
                    {
                        "error": "could not fence the operation before queueing it; "
                        "nothing was queued - safe to retry",
                        "code": "ENQUEUE_ANCHOR_FAILED",
                        "id": tenant_id,
                    },
                )
            if _queue_fence_epoch is None:
                return utils._resp(
                    409,
                    {
                        "error": _fence_reason,
                        "code": "LIFECYCLE_IN_FLIGHT",
                        "id": tenant_id,
                    },
                )
            if _idem_ctx is not None:
                _idem_ctx.update(
                    {
                        "lifecycle_op_id": _lifecycle_op_id,
                        "lifecycle_fence_epoch": _queue_fence_epoch,
                        "hold_lifecycle_fence": True,
                    }
                )
        # #564 G2 —— 受理时刻算绝对死线,同一个值写进【消息体】与【租户行】。
        # 计时起点在这里而不是 consumer 里:上面 :5838 那个 `_consumer_ident not in event`
        # 的闸保证本块只在 API 请求路径跑,所以这就是真正的受理时刻;放到 consumer 里算
        # 会让死线随每次重投往后挪,超时机制等于不存在。
        #
        # **只对死线词汇表里的动作算**:`_async_actions` 有八个,而 `DEADLINE_ACTIONS`
        # 也有八档,其中 start/restart/suspend/restore 属于本集合;stop/pause/resume/reset
        # 仍不在其中,对它们调 `deadline_epoch_for()` 会 raise。
        _accepted_epoch = int(time.time())
        _dl_epoch = None
        if action in create_deadline.DEADLINE_ACTIONS:
            _dl_epoch, _dl_src = deadline_config.deadline_epoch_for(
                action, _accepted_epoch
            )
            if _dl_src != "ssm":
                # 只在【没走参数】时打日志:走参数是常态,每次操作都刷一行没有信息量。
                # 反过来"本该由参数托管却回落了"是运维要知道的事(G5 验收第 4 条)。
                print(
                    f"{action}_deadline: {_dl_epoch - _accepted_epoch}s "
                    f"from {_dl_src} for {tenant_id}"
                )
        # 死线必须在消息发出【之前】落到行上,顺序不能反:消息一发出 consumer 就可能取走并
        # 推进状态,而 `deadline_executor` 的 filter 第一条是
        # `attribute_exists(<action>_deadline)` —— 行上没有它,这次操作永远不会被判死。
        if _dl_epoch is not None:
            try:
                _write_action_deadline(tenant_id, action, _dl_epoch)
            except Exception as e:  # noqa: BLE001
                # **这是发送前的失败,不能落到下面那条 `ENQUEUE_STATE_UNKNOWN`**
                # (Codex 独立复审第 1 轮抓出的真缺陷)。那条是为**发送后的不确定**设计的:
                # SQS 可能已经收下消息,所以它刻意**扣住租约**,防盲目重试起第二次操作。
                # 而这里消息一条都没发出、host 侧一步未动。把它塞进那条路的后果是:答复叫
                # 客户重试,而每次重试都撞 409 `LIFECYCLE_IN_FLIGHT`,直到 1800s 租约自然
                # 过期 —— 一次写库抖动把租户锁死半小时,比它想解决的问题更糟。
                # 正确处置:放掉刚取的那把租约,给一个"什么都没开始、可安全重试"的 503
                # (与 rebuild 那条同款语义,复用同一个 code)。
                if _queue_fence_epoch is not None:
                    lifecycle_fence.release(
                        tenant_id, _lifecycle_op_id, _queue_fence_epoch
                    )
                    if _idem_ctx is not None:
                        _idem_ctx["hold_lifecycle_fence"] = False
                        _idem_ctx["release_lifecycle_fence_on_error"] = True
                print(f"{action} deadline write failed for {tenant_id}: {e}")
                return utils._resp(
                    503,
                    {
                        "error": "could not record the deadline before dispatch; "
                        "nothing was started - safe to retry",
                        "code": "ENQUEUE_ANCHOR_FAILED",
                        "id": tenant_id,
                        "op_id": _lifecycle_op_id,
                    },
                )
        try:
            _enq_op_id = lifecycle_dispatch.enqueue_lifecycle(
                action,
                tenant_id,
                event,
                extra=(
                    _rebuild_body
                    if action in action_idem.IDEMPOTENT_ACTIONS
                    else None
                ),
                operation_id=_lifecycle_op_id,
                deadline_epoch=_dl_epoch,
            )
        except Exception as e:  # noqa: BLE001
            print(f"lifecycle enqueue failed for {tenant_id}/{action}: {e}")
            return utils._resp(
                503,
                {
                    "error": "the lifecycle queue did not acknowledge the request; "
                    "check tenant state before retrying",
                    "code": "ENQUEUE_STATE_UNKNOWN",
                    "id": tenant_id,
                    "op_id": _lifecycle_op_id,
                },
            )
        if _enq_op_id:
            if _idem_token and _idem_ctx is not None:
                # 202 means accepted, not completed. The queue consumer keeps
                # the intent IN_PROGRESS and writes the real terminal result.
                _idem_ctx["defer_finish"] = True
            return utils._resp(
                202,
                # #565 G6 —— 这条出口被 `_async_actions` 的**八个**动作共用,八档死线词汇表
                # 里有 start/restart/suspend/restore 四个;stop/pause/resume/reset 的
                # `_dl_epoch` 恒为 `None`(见上面那段注释),`_with_action_deadline` 按 action
                # 自行挡掉,所以这里不用再写一遍 `if action in DEADLINE_ACTIONS`。
                _with_action_deadline(
                    {
                        "id": tenant_id,
                        "action": action,
                        "status": "queued",
                        "op_id": _enq_op_id,
                    },
                    action,
                    deadline_epoch=_dl_epoch,
                ),
            )
        if _queue_fence_epoch is not None:
            lifecycle_fence.release(tenant_id, _lifecycle_op_id, _queue_fence_epoch)
            if _idem_ctx is not None:
                _idem_ctx["hold_lifecycle_fence"] = False

    _lifecycle_fence_epoch = None
    _lifecycle_host_guard = ""
    if action in _FENCED_LIFECYCLE_ACTIONS:
        _expected_fence_epoch = (
            (event or {}).get("_fence_epoch")
            if action == "rebuild" and "_consumer_ident" in (event or {})
            else None
        )
        if _expected_fence_epoch is not None:
            _expected_fence_epoch = int(_expected_fence_epoch)
            if lifecycle_fence.renew_owned(
                tenant_id, _lifecycle_op_id, _expected_fence_epoch
            ):
                _lifecycle_fence_epoch, _fence_reason = (
                    _expected_fence_epoch,
                    None,
                )
            else:
                _lifecycle_fence_epoch, _fence_reason = (
                    None,
                    "the async rebuild no longer owns its admitted lifecycle fence",
                )
        else:
            _lifecycle_fence_epoch, _fence_reason = lifecycle_fence.acquire(
                tenant_id, _lifecycle_op_id, action
            )
        if _lifecycle_fence_epoch is None:
            return utils._resp(
                409,
                {
                    "error": _fence_reason,
                    "code": "LIFECYCLE_IN_FLIGHT",
                    "id": tenant_id,
                    "op_id": _lifecycle_op_id,
                },
            )
        if _idem_ctx is not None:
            _idem_ctx.update(
                {
                    "lifecycle_op_id": _lifecycle_op_id,
                    "lifecycle_fence_epoch": _lifecycle_fence_epoch,
                    "hold_lifecycle_fence": False,
                }
            )
        _lifecycle_host_guard = lifecycle_fence.host_guard(
            tenant_id, _lifecycle_op_id, _lifecycle_fence_epoch
        )

    if action == "resize":
        return tenant_resize(tenant_id, body)

    if action == "resize-disk":
        try:
            payload = json.loads(body) if isinstance(body, str) else (body or {})
        except Exception:
            payload = {}
        new_size = payload.get("new_size_mb")
        if not isinstance(new_size, int):
            return utils._resp(400, {"error": "missing or invalid new_size_mb"})
        current = int(item.get("data_disk_mb", clients.VM_DATA_DISK_MB))
        if new_size <= current:
            return utils._resp(
                400,
                {
                    "error": f"new_size_mb must be larger (current {current}MB); shrink not supported"
                },
            )
        if new_size > 1024 * 1024:
            return utils._resp(400, {"error": "new_size_mb exceeds 1 TiB ceiling"})
        host_id = item.get("host_id")
        vm_num = int(item.get("vm_num", 1))
        if not host_id:
            return utils._resp(400, {"error": "tenant has no host (still pending?)"})
        # Issue #64-class fix: resize-disk.sh was never deployed (same defect
        # as migrate-vm.sh) AND this path was fire-and-forget — it flipped
        # data_disk_mb in DDB before the host had even run the script, so DDB
        # claimed the new size whether or not the ext4 grow actually happened.
        # Now: run synchronously, and only persist the new size on Success.
        _resize_heal = ssm_dispatch.host_script_self_heal(
            ("resize-disk.sh",), "oc:resize"
        )
        if not ssm_dispatch._ssm_run(
            host_id,
            f"{_resize_heal} && "
            f"/home/ubuntu/resize-disk.sh {tenant_id} {vm_num} {new_size}",
            timeout=120,
        ):
            return utils._resp(
                502,
                {
                    "error": "resize-disk.sh failed on host; size unchanged",
                    "id": tenant_id,
                    "data_disk_mb": current,
                },
            )
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET data_disk_mb = :s, updated_at = :t",
            ExpressionAttributeValues={":s": new_size, ":t": utils._now()},
        )
        return utils._resp(
            200,
            {
                "id": tenant_id,
                "status": "running",
                "old_size_mb": current,
                "new_size_mb": new_size,
            },
        )

    if action == "migrate":
        # Live migration via Firecracker snapshot/restore (issue #20).
        # Body shape: {"target_host_id": "i-...."}
        # Firecracker can't snapshot a VM with an active balloon device, live migrate is unavailable while balloon is on (issue #72).
        # Reject up front instead of failing ~minutes later in the snapshot step.
        if clients.BALLOON_ENABLED:
            return utils._resp(
                409,
                {
                    "error": "Live migration isn't available while memory overcommit (balloon) is on. To move this tenant, back it up, recreate it on the target host, then restore the backup — no data is lost.",
                    "reason": "balloon_enabled",
                },
            )
        try:
            payload = json.loads(body) if isinstance(body, str) else (body or {})
        except Exception:
            payload = {}
        target_host_id = payload.get("target_host_id")
        if not target_host_id:
            return utils._resp(400, {"error": "missing target_host_id"})
        source_host_id = item.get("host_id")
        if target_host_id == source_host_id:
            return utils._resp(
                400, {"error": "target_host_id must be different from source"}
            )
        target = clients.hosts_table.get_item(
            Key={"instance_id": target_host_id}, ConsistentRead=True
        ).get("Item")
        if not target:
            return utils._resp(
                404, {"error": f"target host {target_host_id} not found"}
            )
        if target.get("status") in ("draining", "deleted"):
            return utils._resp(
                409, {"error": f"target host {target_host_id} is {target['status']}"}
            )
        # #540 — 显式 target 撞污点(cordon)机器一律 409,与上面 draining/deleted 并列。
        # 【不提供 force / allow_tainted】:同 pinned create,依据是 #309 已为同一件事否掉过
        # allow_upgrading 豁免。把租户搬【上】一台正在腾空的机器,方向本身就是反的。
        if host_taint.is_tainted(target):
            return utils._resp(
                409,
                {
                    "error": f"target host {target_host_id} is tainted (cordoned): "
                    "it accepts no new tenants. Migrating onto a host being drained "
                    "is the wrong direction.",
                    "code": "HOST_TAINTED",
                },
            )

        # Capacity check — same allocatable formula as _find_host().
        vcpu = int(item.get("vcpu", 0))
        mem_mb = int(item.get("mem_mb", 0))
        _cpu_r, _mem_r = host_profile.ratios(
            target,
            (clients.CPU_OVERCOMMIT_RATIO, clients.MEM_OVERCOMMIT_RATIO),
            clients.OVERCOMMIT_BY_FAMILY,
        )
        allocatable_vcpu = capacity.allocatable(int(target["total_vcpu"]), _cpu_r)
        free_vcpu = allocatable_vcpu - int(target.get("used_vcpu", 0))
        allocatable_mem = capacity.allocatable(int(target["total_mem_mb"]), _mem_r)
        free_mem = allocatable_mem - int(target.get("used_mem_mb", 0))
        if free_vcpu < vcpu or free_mem < mem_mb:
            return utils._resp(
                409,
                {
                    "error": (
                        f"target host has insufficient capacity "
                        f"(free vcpu={free_vcpu}, free mem={free_mem}MB; "
                        f"need vcpu={vcpu}, mem={mem_mb}MB)"
                    )
                },
            )

        vm_num = int(item.get("vm_num", 1))
        # #208 — tap 冲突检查(no-cross-tenant 不可退底线)。
        # Firecracker snapshot 烤死 tap-vm{原始 launch vm_num},restore 后 VM 挂这个
        # tap(migrate-vm.sh:182 从 snapshot 的 vm.json 读 SRC_VM_NUM)。若 target host 上
        # 已有同名物理 tap(另一租户物理占同一号)→ 两 VM 共享一个 tap → 跨租户网络互通。
        #
        # 【修复 #208 二次迁移盲区】原实现把冲突键在 DDB vm_num 上,但迁移 finalize 会把
        # vm_num 翻成 target 槽位号(health_check/handler.py),使 DDB vm_num 与物理 tap 号
        # 分叉:一个已迁入 target 的租户(物理 tap=原始 launch 号,DDB vm_num=别的槽)对
        # "键在 vm_num"的 scan 完全隐形 → 第二个原始号相同的租户迁入同一 host 时放行 →
        # 两 VM 挂同一 tap-vm{X} → 真跨租户 L2。修:改键在**物理 tap 号 phys_vm_num**
        # (本租户迁移后 restore 实际挂的号 = 它自己的 phys_vm_num,恒等原始 launch 号),
        # 用 _phys_tap_occupied 统一覆盖"已驻留 + 迁入中"两来源,fail-closed。
        src_phys = int(item.get("phys_vm_num", vm_num))
        if _phys_tap_occupied(target_host_id, src_phys, exclude_id=tenant_id):
            return utils._resp(
                409,
                {
                    "error": (
                        f"tap collision: target host already has a tenant on "
                        f"tap-vm{src_phys} (this tenant's physical tap); snapshot "
                        f"restore would share the same tap device → cross-tenant "
                        f"network access. Pick a different target host."
                    )
                },
            )
        bucket = os.environ.get("ASSETS_BUCKET", "")
        snap_prefix = f"migrations/{tenant_id}"
        # #172 — CAS 原子占 target host 的 vm_num + 容量(替代裸 get,防两个并发迁移到
        # 同一 target 抢同一 vm_num → guest_ip/host_port 串)。占不到(target 无容量/持续
        # 输 CAS)→ fail migrate,不落 migrating 态(否则租户卡 migrating 且没抢到 slot)。
        target_vm_num = _reserve_migration_slot(
            target, vcpu, mem_mb, tenant_id=tenant_id
        )
        if target_vm_num is None:
            return utils._resp(
                503,
                {
                    "error": "migration target has no capacity (lost CAS / full); "
                    "retry later or pick another target",
                },
            )
        now = utils._now()

        # ── Issue #64 — ASYNC, fail-safe migration ──
        # A correct migration runs snapshot(source)+restore(target), which now
        # ship multi-GB disk images and take *minutes*. API Gateway caps a
        # synchronous integration at 29s, so we cannot block here (an earlier
        # synchronous version returned "Endpoint request timed out" and could
        # leave the tenant stuck in `migrating`). Instead:
        #   1. Do all the cheap validation synchronously (done above).
        #   2. Fire-and-forget the snapshot via _ssm_send (returns a CommandId).
        #   3. Record migrating + the async context in DDB and return 202.
        # The health_check Lambda's 5-min sweep (_advance_migration) polls the
        # SSM CommandId, triggers restore when snapshot succeeds, verifies the
        # dashboard through the public path, and only then flips host_id /
        # counters / routing → running. Any failure (or a 15-min watchdog)
        # rolls status back to running with host_id untouched, so the tenant
        # is never left pointing at a host with no VM. The source of truth is
        # only mutated after the whole move is proven — same fail-safe contract
        # as before, just driven out-of-band instead of in the request path.

        _migrate_heal = ssm_dispatch.host_script_self_heal(
            ("migrate-vm.sh",), "oc:migrate"
        )
        snap_cmd = ssm_dispatch._ssm_send(
            source_host_id,
            f"{_lifecycle_host_guard} && "
            f"{_migrate_heal} && "
            f"/home/ubuntu/migrate-vm.sh snapshot {shlex.quote(tenant_id)} {vm_num} "
            f"{shlex.quote(f's3://{bucket}/{snap_prefix}')} && "
            f"{_lifecycle_host_guard}",
            timeout=600,  # snapshot + multi-GB disk upload to S3
        )
        if not snap_cmd:
            # #172 — SSM submit 失败,啥都没起。但上面已 CAS 占了 target 的 slot,且这里
            # 在写 status=migrating **之前**就 bail,sweep 只扫 migrating 租户 → 永远看不到
            # 它、_rollback_migration 不会触发 → target host 的 used_vcpu/used_mem_mb/vm_count
            # 永久泄漏(2026-07-01 SSM ThrottlingException 在 380 突发下的失败模式)。必须
            # 在 502 前释放预留,镜像 create 路径的 _release_slot-on-failure(本文件 :901/960)。
            scheduling._release_slot(
                target_host_id, vcpu, mem_mb, target_vm_num, tenant_id
            )
            return utils._resp(
                502,
                {
                    "error": "failed to start migration (SSM submit)",
                    "id": tenant_id,
                    "source_host_id": source_host_id,
                    "target_host_id": target_host_id,
                },
            )

        # Mark migrating + stash everything the sweep needs to finish the move.
        # No host_id / counter / routing change happens here — only after the
        # sweep proves snapshot+restore+dashboard all succeeded.
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=(
                    "SET #s = :s, migration_target = :tgt, "
                    "migration_target_vm_num = :tvn, migration_source = :src, "
                    "migration_snap_cmd = :scmd, migration_phase = :ph, "
                    "migration_started_at = :st, migration_snapshot_uri = :uri, "
                    "updated_at = :t, migration_lifecycle_op_id = :lf_op, "
                    "migration_lifecycle_fence_epoch = :lf_epoch"
                ),
                ConditionExpression=(
                    "active_lifecycle_op_id = :lf_op AND "
                    "lifecycle_fence_epoch = :lf_epoch"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "migrating",
                    ":tgt": target_host_id,
                    ":tvn": target_vm_num,
                    ":src": source_host_id,
                    ":scmd": snap_cmd,
                    ":ph": "snapshot",
                    ":st": now,
                    ":uri": f"s3://{bucket}/{snap_prefix}",
                    ":t": now,
                    ":lf_op": _lifecycle_op_id,
                    ":lf_epoch": _lifecycle_fence_epoch,
                },
            )
        except Exception as exc:  # noqa: BLE001
            # The target reservation is not discoverable by the migration sweep
            # until this context write succeeds. Never leak it when the fence
            # was superseded between SSM submission and persistence.
            scheduling._release_slot(
                target_host_id, vcpu, mem_mb, target_vm_num, tenant_id
            )
            if lifecycle_fence._is_ccf(exc):
                return utils._resp(
                    409,
                    {
                        "error": "migration was superseded before its context "
                        "could be persisted",
                        "code": "LIFECYCLE_SUPERSEDED",
                        "id": tenant_id,
                    },
                )
            raise
        if _idem_ctx is not None:
            _idem_ctx["hold_lifecycle_fence"] = True

        # 202 Accepted: the move is in flight. Clients poll GET /tenants/{id}
        # until status is `running` (success) or back to its prior value with
        # migration_failed set (failure).
        return utils._resp(
            202,
            {
                "id": tenant_id,
                "status": "migrating",
                "source_host_id": source_host_id,
                "target_host_id": target_host_id,
                "snapshot_uri": f"s3://{bucket}/{snap_prefix}",
                "poll": f"/tenants/{tenant_id}",
            },
        )

    if action == "restart":
        # #305 语义边界:restart = 软重启,**保留 overlay.ext4**(per-VM 写时复制层)。
        # overlay 是叠在共享只读 rootfs 之上的,旧 overlay 是针对旧 rootfs 建的 →
        # 若镜像升级后用 restart 想让新 rootfs 生效,只会得到"半新半旧"(未被 overlay
        # COW 覆盖的块才是新的)。**镜像升级必须走 rebuild(丢 overlay + 采用校验),
        # 不要用 restart**。restart 只用于"不换代码的软重启"(保留运行态数据盘 + overlay)。
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        # #41 — 通过 helper 生成带全部 11 位参数的命令,穿透 chat_endpoint_enabled
        # 到 launch-vm 幂等段;老版本只填 4 位、CHAT_EP_ENABLED 恒空致唤醒漂移。
        launch_cmd = ssm_dispatch._launch_vm_wake_cmd(tenant_id, item)
        # Re-add DNAT after restart
        dnat_cmd = (
            _dnat_add_idempotent_cmd(host_port, guest_ip)
            if guest_ip and host_port
            else ""
        )
        # restart 同时用 stop-vm.sh 与(经 wake helper 的) launch-vm.sh,两者都要在位。
        _restart_heal = ssm_dispatch.host_script_self_heal(
            ("stop-vm.sh", "launch-vm.sh"),
            "oc:restart",
            freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED"),
        )
        full_cmd = (
            f"{_restart_heal} && "
            f"{_lifecycle_host_guard} && {stop_cmd} && sleep 2 && "
            f"{_lifecycle_host_guard} && {launch_cmd}"
        )
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        full_cmd += f" && {_lifecycle_host_guard}"
        # #565 G3(Codex 独立复审第八轮)—— **要看 rc,不能一律记成 host_unreachable。**
        # 这条组命令前后都夹着 `_lifecycle_host_guard`,而那个 guard 的退出码在本仓已有成熟语义
        # (论证见 :4067-4082):
        #   · **79** = 租约被抢占(LIFECYCLE_FENCE_EXPIRED)→ 被别的操作抢先
        #   · **75** = launch-vm.sh 的 flock 争用(并发/重投的 launch 正持锁)→ 同样是被抢先
        #   · **78** = host **读不到** DDB 里的 fence → 那是**控制面故障,不是被抢占**
        # 只有 75/79 归 preempted。契约给 preempted 的指引是「**可立刻重试**」,而 78 若也归它,
        # 就会在一次 DDB 故障里指示所有调用方立刻重试 —— **放大控制面故障、还盖住真实原因**
        # (第九轮 Codex 纠正的;我第八轮把 78 一起塞进去了)。78 保持 host_unreachable:
        # 它的指引是「隔 1–2 分钟」,正好是等控制面恢复该做的事。
        # 判据与第四轮那条同款:**分类依据是"重试有没有用、该多快重试",不是"错在哪一层"。**
        # #565 G1 —— 执行段从预算表取(restart 是 180s 档,执行段 120s)。原值 300 超死线 120s:
        # 到点判 failed 而这条组命令还能合法再跑 120s 并把 VM 拉起来。
        _restart_ok, _restart_rc = ssm_dispatch._ssm_run(
            item["host_id"],
            full_cmd,
            timeout=create_deadline.exec_step_sec(
                create_deadline.ACTION_RESTART, "stop+launch"
            ),
            want_rc=True,
        )
        if not _restart_ok:
            _mark_fail_reason(
                tenant_id,
                "restart",
                create_deadline.REASON_PREEMPTED
                if _restart_rc in (75, 79)
                else create_deadline.REASON_HOST_UNREACHABLE,
            )
            # #604 —— rc==75 是 launch-vm.sh:705 的 flock-skip(良性、短暂:另一次同租户
            # launch 正持锁把 VM 拉起)。补一个机器可读 code,consumer 据此把**这条消息**的
            # 重投退避从队列默认 960s 缩到秒级 —— 否则它占住 per-tenant FIFO 组头 16 分钟,
            # 同租户后续 rebuild/delete 全撞 409。**三次真机复现命中的正是这条 restart 路径**
            # (`active_lifecycle_action=restart` + `[oc:launch] ... flock held — skip`),
            # 少了它整个修复对真实流量不可达(Codex 独立复审第 4 轮抓出)。
            #
            # rc==79 不带:那是 fence 被别的操作抢占、对方已经接管并会推进到终态,不是
            # 「等一会儿锁就放开」。给它短退避只会让一条注定失败的消息更快耗尽 DLQ 预算。
            #
            # 写成 `**(... if ... else {})` 而不是先建变量再改:#565 G3 的接线覆盖统计
            # (`test_565_g3_fail_reason_contract._resp_body_text`)用 AST 认 `_resp` 的第二个
            # 实参**必须是 dict 字面量**,换成变量引用会让这处出口数不出来 —— 覆盖数从 34 掉到
            # 33,而那道门正是防「失败出口悄悄失去可归因原因」的。
            return utils._resp(
                502,
                {
                    "error": "restart was not confirmed on the host; the same "
                    "operation will be retried",
                    "id": tenant_id,
                    **(
                        {"code": ssm_dispatch.LAUNCH_IN_PROGRESS_CODE}
                        if _restart_rc == ssm_dispatch.RC_FLOCK_SKIP
                        else {}
                    ),
                },
            )
        new_status = "running"
    elif action == "stop":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        # #548 — stop 绝不回收端口/DNAT。这里【曾经】把一条 iptables -D PREROUTING 删除循环
        # 拼在 stop-vm.sh 后面,那正好打破了整个端口模型赖以成立的不变量
        # (route_ops.py:438 release_port_and_dnat 的 R2.3:"only DELETE reclaims, STOP never does"):
        #   ① stop 删掉 DNAT → ② 端口位图由活规则重建(rebuild_bitmap_from_iptables)于是认为该端口空闲
        #   → ③ 下一个租户 promote 拿到同一端口 → ④ 停机租户 DDB 里 host_port 没变
        #   → ⑤ 它再 start 时 _dnat_add_idempotent_cmd 把规则加回来 → 同 dport 两条 DNAT 并存。
        # PREROUTING 按首条匹配,于是一个租户公布的端点会把流量投进另一个租户的 VM;
        # 而 route_ops.list_dnat_rules() 返回 dict 会把重复端口静默折叠(且报的是后一条,
        # 与内核生效的首条相反),连冲突都看不见。真机已复现(#548)。
        # 端口/DNAT 的回收只在 delete 路径做;suspend 另有自己的摘除(它同时释放 slot)。
        _stop_heal = ssm_dispatch.host_script_self_heal(
            ("stop-vm.sh",),
            "oc:stop",
            freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED"),
        )
        full_cmd = f"{_stop_heal} && {stop_cmd}"
        if not ssm_dispatch._ssm_run(item["host_id"], full_cmd):
            return utils._resp(
                502,
                {
                    "error": "stop-vm failed (SSM timeout/error); tenant status was not changed"
                },
            )
        new_status = "stopped"
    elif action == "start":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        # #41 — 通过 helper 生成带全部 11 位参数的命令,穿透 chat_endpoint_enabled
        # 到 launch-vm 幂等段;老版本只填 4 位、CHAT_EP_ENABLED 恒空致唤醒漂移。
        launch_cmd = ssm_dispatch._launch_vm_wake_cmd(tenant_id, item)
        dnat_cmd = (
            _dnat_add_idempotent_cmd(host_port, guest_ip)
            if guest_ip and host_port
            else ""
        )
        _start_heal = ssm_dispatch.host_script_self_heal(
            ("launch-vm.sh",), "oc:start"
        )
        full_cmd = f"{_start_heal} && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        # #604 —— 必须拿到 rc,不能只看 _ssm_run 的 bool。用 on_result 回调而不是
        # want_rc=True:后者把返回类型从 bool 变成 tuple,会波及所有既有调用方与测试桩
        # (reset 分支 :7517 用的也是这个回调形态,与它保持一致)。
        _start_rc = {"v": None}
        if not ssm_dispatch._ssm_run(
            item["host_id"],
            full_cmd,
            timeout=create_deadline.exec_step_sec(
                create_deadline.ACTION_START, "launch-vm"
            ),
            on_result=lambda _st, rc: _start_rc.__setitem__("v", rc),
        ):
            # #604 —— rc==75 是 launch-vm.sh:705 的 flock-skip 哨兵:另一次【同租户】launch
            # 正持锁把 VM 拉起(SQS 重投 / 并发)。这是**良性**结果,不是 start 失败。
            #
            # 旧代码只看 bool,把它和"SSM 真超时""launch 真失败"一起报成 502。后果是一条
            # 完整的连锁(三次真机复现,时间戳全部落在 960s 上):consumer 对 `code >= 500`
            # 的处置是进 batchItemFailures = 【不 ack】(handler.py:2150),而 lifecycle.fifo
            # 的 VisibilityTimeout=960 且 MessageGroupId=tenant_id,于是这条消息占住 FIFO
            # 组头 16 分钟,**同租户后续所有生命周期操作被组头阻塞**、rebuild/delete 一律撞
            # 409 LIFECYCLE_IN_FLIGHT,最后由 #564 死线兜底判死。
            #
            # 与 restore 路径(:5293)同口径:状态一个字不动,返 503 让消息留队列重投 ——
            # 返 2xx 会让消息被 ack 掉,万一持锁那次自己也失败就无人再推进。真正把 16 分钟
            # 压掉的是 consumer 侧按 code 做的短退避重投(handler.py
            # `_shorten_lifecycle_visibility_best_effort`),它认下面这个 code。
            #
            if _start_rc["v"] == ssm_dispatch.RC_FLOCK_SKIP:
                _mark_fail_reason(
                    tenant_id, "start", create_deadline.REASON_PREEMPTED
                )
                return utils._resp(
                    503,
                    {
                        "id": tenant_id,
                        "status": item.get("status"),
                        "code": ssm_dispatch.LAUNCH_IN_PROGRESS_CODE,
                        "error": "start skipped: another launch of this tenant holds the "
                        "per-tenant flock on the host; status unchanged, will reconverge "
                        "on retry",
                    },
                )
            _mark_fail_reason(
                tenant_id, "start", create_deadline.REASON_HOST_UNREACHABLE
            )
            return utils._resp(
                502,
                {
                    "error": "start-vm failed (SSM timeout/error); tenant status was not changed"
                },
            )
        new_status = "running"
    elif action == "reset":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        launch_cmd = ssm_dispatch._launch_vm_wake_cmd(tenant_id, item)
        dnat_cmd = (
            _dnat_add_idempotent_cmd(host_port, guest_ip)
            if guest_ip and host_port
            else ""
        )
        _reset_attempt_id = secrets.token_hex(16)
        q = shlex.quote
        reset_cmd = (
            f"/home/ubuntu/reset-vm.sh {q(str(tenant_id))} {q(str(vm_num))} "
            f"{q(str(_lifecycle_op_id))} {q(str(_lifecycle_fence_epoch))} "
            f"{q(_reset_attempt_id)} {q(str(item['host_id']))} -- {launch_cmd}"
        )
        # #520 C2:reset-vm.sh 是后加的 host 侧事务脚本,既有 host(开机时那版
        # init-host.sh 还没有拉它的那几行)上根本不存在 → 无条件调用 exit 127。
        # 自愈段放在 fence host_guard 之【前】:守卫本身不依赖这些脚本,而脚本缺失时
        # 先跑守卫只是把 127 往后挪一步;失败即整条非零,与 reset 自身失败同路径。
        # 成对自愈 stop-vm.sh:reset-vm.sh 自己会调它(deploy/userdata/reset-vm.sh:228),
        # 只装顶层脚本的话,旧 host 上那个依赖仍可能缺失或过期(独立评审指出)。
        _reset_heal = ssm_dispatch.host_script_self_heal(
            ("reset-vm.sh", "stop-vm.sh", "launch-vm.sh"),
            "oc:reset",
            freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED"),
        )
        full_cmd = f"{_reset_heal} && {_lifecycle_host_guard} && {reset_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        full_cmd += f" && {_lifecycle_host_guard}"

        _reset_rc = {"v": None}
        _reset_output = {"stdout": "", "stderr": ""}
        _reset_ssm_ok = ssm_dispatch._ssm_run(
            item["host_id"],
            full_cmd,
            timeout=300,
            on_result=lambda _st, rc: _reset_rc.__setitem__("v", rc),
            on_output=lambda stdout, stderr: _reset_output.update(
                {"stdout": stdout, "stderr": stderr}
            ),
        )
        _reset_host_result = _parse_host_reset_result(
            _reset_output["stdout"],
            tenant_id,
            _lifecycle_op_id,
            _reset_attempt_id,
            _lifecycle_fence_epoch,
            item["host_id"],
            vm_num,
        )
        if (
            _reset_rc["v"] == 79
            or (
                _reset_host_result
                and _reset_host_result.get("state") == "SUPERSEDED"
            )
        ):
            return utils._resp(
                409,
                {
                    "error": "the reset lost its lifecycle fence or source host",
                    "code": "LIFECYCLE_SUPERSEDED",
                    "id": tenant_id,
                    "op_id": _lifecycle_op_id,
                },
            )
        _reset_verified = bool(
            _reset_ssm_ok
            and _reset_host_result
            and _reset_host_result.get("state") == "SUCCEEDED"
        )
        if not _reset_verified:
            return utils._resp(
                502,
                {
                    "error": "reset was not confirmed on the host; the same "
                    "operation will be retried with its op-scoped evidence",
                    "code": "RESET_RETRY_PENDING",
                    "id": tenant_id,
                    "op_id": _lifecycle_op_id,
                },
            )
        new_status = "running"
    elif action == "rebuild":
        # All rebuild workers use one channel flow. Missing image_channel is
        # live; both channels are resolved before the mandatory backup and before
        # any tenant/VM mutation.
        _resolved = _rebuild_repin_resolve(item, _rebuild_body)
        if not (isinstance(_resolved, dict) and "channel" in _resolved):
            return _resolved
        _repin_channel = _resolved["channel"]
        _repin_snapshot = _resolved.get("target_snap")
        if _reapply_binding:
            if (
                "target_image_snapshot_time" in _reapply_binding
                and _reapply_binding["target_image_snapshot_time"]
                != _resolved_reapply_snapshot(_resolved)
            ):
                if _idem_ctx is not None:
                    _idem_ctx["release_lifecycle_fence_on_error"] = True
                return utils._resp(
                    409,
                    {
                        "error": "the host image slot changed after config "
                        "reapply admission; no tenant data was modified",
                        "code": "REAPPLY_TARGET_CHANGED",
                        "id": tenant_id,
                    },
                )
            _reapply_binding.setdefault(
                "target_image_snapshot_time",
                _resolved_reapply_snapshot(_resolved),
            )
            if _reapply_already_applied(item, _reapply_binding, _resolved):
                return _reapply_already_applied_response(
                    tenant_id, item, _reapply_binding
                )
            _probe_ok, _probe_incompatible, _probe_result = (
                _run_reapply_host_probe(item, _reapply_binding, _resolved)
            )
            if not _probe_ok:
                if _idem_ctx is not None:
                    _idem_ctx["release_lifecycle_fence_on_error"] = True
                if _probe_incompatible:
                    return utils._resp(
                        409,
                        {
                            "error": "openclaw.json 不兼容",
                            "code": "OPENCLAW_CONFIG_INCOMPATIBLE",
                            "id": tenant_id,
                        },
                    )
                return utils._resp(
                    503,
                    {
                        "error": "openclaw.json compatibility probe failed before "
                        "the destructive rebuild; tenant was not modified",
                        "code": "REAPPLY_PROBE_FAILED",
                        "id": tenant_id,
                        "probe_rc": _probe_result.get("rc"),
                    },
                )
        _applied = _rebuild_repin_apply(
            tenant_id,
            item,
            _repin_channel,
            _repin_snapshot,
            _lifecycle_op_id,
            _lifecycle_fence_epoch,
        )
        if _applied is not None:
            return _applied
        # Re-read after channel persistence so launch uses the selected version.
        item = clients.tenants_table.get_item(
            Key={"id": tenant_id}, ConsistentRead=True
        ).get("Item") or item
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        launch_cmd = ssm_dispatch._launch_vm_wake_cmd(tenant_id, item)
        dnat_cmd = (
            _dnat_add_idempotent_cmd(host_port, guest_ip)
            if guest_ip and host_port
            else ""
        )
        # Queue consumers carry the operation-stable id in _op_id. Synchronous
        # rebuilds still own the lifecycle fence under _lifecycle_op_id, so the
        # host transaction and ledger must use that same identity rather than
        # serializing a missing event id as the literal string "None".
        _rb_op_id = (event or {}).get("_op_id") or _lifecycle_op_id
        _rb_same_operation = bool(
            _rb_op_id and item.get("rebuild_op_id") == _rb_op_id
        )
        _stamp_rebuild_progress(
            tenant_id,
            op_id=_rb_op_id,
            phase=_REBUILD_PHASE_RUNNING,
            started_at=None if _rb_same_operation else utils._now(),
            target_snapshot_time=_repin_snapshot or None,
            new_operation=not _rb_same_operation,
            fence_epoch=_lifecycle_fence_epoch,
            source_host_id=None if _rb_same_operation else item.get("host_id"),
            source_vm_num=None if _rb_same_operation else vm_num,
        )

        _rb_attempt_id = secrets.token_hex(16)
        _rb_ledger_enabled = getattr(clients, "image_jobs_table", None) is not None
        if _rb_ledger_enabled:
            existing = image_ops.get(_rb_op_id)
            if existing is None:
                image_ops.record_intent(
                    _rb_op_id,
                    tenant_id,
                    "tenant_rebuild",
                    {
                        "host_id": item["host_id"],
                        "vm_num": vm_num,
                        "fence_epoch": _lifecycle_fence_epoch,
                        "target_snapshot_time": _repin_snapshot or "",
                    },
                )
                existing = image_ops.get(_rb_op_id)
            if existing and (
                existing.get("instance_id") != tenant_id
                or existing.get("operation") != "tenant_rebuild"
            ):
                return utils._resp(
                    409,
                    {
                        "error": "operation id belongs to a different rebuild",
                        "code": "OPERATION_ID_REUSED",
                        "id": tenant_id,
                    },
                )
            if not image_ops.claim_attempt(_rb_op_id, _rb_attempt_id):
                return utils._resp(
                    503,
                    {
                        "error": "another attempt still owns this rebuild; retry later",
                        "code": "REBUILD_ATTEMPT_BUSY",
                        "id": tenant_id,
                        "op_id": _rb_op_id,
                    },
                )

        q = shlex.quote
        rebuild_cmd = (
            f"/home/ubuntu/rebuild-vm.sh {q(str(tenant_id))} {q(str(vm_num))} "
            f"{q(str(_rb_op_id))} {q(str(_lifecycle_fence_epoch))} "
            f"{q(_rb_attempt_id)} {q(str(item['host_id']))} -- {launch_cmd}"
        )
        _reapply_command_env = (
            _reapply_env_prefix(_reapply_binding) if _reapply_binding else ""
        )
        # #520 C2:同 reset —— rebuild-vm.sh 在既有 host 上可能不存在。客户 apse1 打
        # restorepatch 时正是靠人工 scp 补装到在役 3 台才让 rebuild 可用,那是把正确性
        # 寄托在人工步骤上;这里改成控制面自己兜底。同 reset 成对自愈 stop-vm.sh:
        # rebuild-vm.sh 自己会调它(deploy/userdata/rebuild-vm.sh:284),只装顶层脚本的话
        # 旧 host 上那个依赖仍可能缺失或过期(独立评审指出)。
        _rebuild_heal = ssm_dispatch.host_script_self_heal(
            ("rebuild-vm.sh", "stop-vm.sh", "launch-vm.sh"),
            "oc:rebuild",
            freshness=("stop-vm.sh", "OC_STOP_GUEST_FLUSH_REQUIRED"),
        )
        # #523 判据 4 —— 第二个"存在但过期"判据。控制面现在【要求】采用证据带
        # firecracker_version / guest_kernel_sha256(见 _parse_host_rebuild_result),而在役
        # host 上那份 rebuild-vm.sh 是它开机时装的旧版,不会写这两个字段 → 每次 rebuild 都
        # 会被判成 unconfirmed。这不是可以用文档"必须成对部署"糊过去的,那是把正确性寄托在
        # 人工步骤上(host_script_self_heal 的 docstring 写的就是这条)。
        # 为什么是第二段而不是把 freshness 改成收多对:helper 的 freshness 是单对语义,
        # 另外十个调用点都按单对写。为了 rebuild 一条路径去改共用 helper 的签名会波及
        # suspend / restore / reset / delete / restart / stop / start / resize / migrate ——
        # 违反最小正确改动。两段各自独立判定、串在同一条 SSM 命令里,幸福路径的代价是
        # 一次 `[ -x ]` 加一次 grep。
        _rebuild_version_heal = ssm_dispatch.host_script_self_heal(
            ("rebuild-vm.sh",),
            "oc:rebuild",
            freshness=("rebuild-vm.sh", "guest_kernel_sha256"),
        )
        full_cmd = (
            f"{_rebuild_heal} && {_rebuild_version_heal} && "
            f"{_lifecycle_host_guard} && {_reapply_command_env}{rebuild_cmd}"
        )
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        full_cmd += f" && {_lifecycle_host_guard}"

        _rb_rc = {"v": None}
        _rb_output = {"stdout": "", "stderr": ""}

        def _record_rebuild_command(command_id):
            _stamp_rebuild_progress(
                tenant_id,
                op_id=_rb_op_id,
                ssm_command_id=command_id,
                phase=_REBUILD_PHASE_VERIFYING,
                fence_epoch=_lifecycle_fence_epoch,
            )
            if _rb_ledger_enabled:
                image_ops.record_command(_rb_op_id, _rb_attempt_id, command_id)

        _rb_ssm_ok = ssm_dispatch._ssm_run(
            item["host_id"],
            full_cmd,
            # #565 G1 —— rebuild 是 180s 档,执行段 120s。原值 300 超死线 120s,而 rebuild
            # 正是唯一已知会常态化产出「不确定」这种非终态的操作(客户把它纳入 180s 档的理由)。
            timeout=create_deadline.exec_step_sec(
                create_deadline.ACTION_REBUILD, "rebuild-vm"
            ),
            on_command_id=_record_rebuild_command,
            on_result=lambda _st, rc: _rb_rc.__setitem__("v", rc),
            on_output=lambda stdout, stderr: _rb_output.update(
                {"stdout": stdout, "stderr": stderr}
            ),
        )
        _rb_host_result = _parse_host_rebuild_result(
            _rb_output["stdout"],
            tenant_id,
            _rb_op_id,
            _rb_attempt_id,
            _lifecycle_fence_epoch,
            item["host_id"],
            vm_num,
        )
        _rebuild_superseded = bool(
            _rb_rc["v"] == 79
            or (
                _rb_host_result
                and _rb_host_result.get("state") == "SUPERSEDED"
            )
        )
        _rebuild_verified = bool(
            _rb_ssm_ok
            and _rb_host_result
            and _rb_host_result.get("state") == "SUCCEEDED"
        )
        _rb_evidence_snapshot = (
            (_rb_host_result or {}).get("target_snapshot_time") or ""
        )
        if _rb_ledger_enabled:
            if _rebuild_verified:
                image_ops.record_result(
                    _rb_op_id,
                    True,
                    result=_rb_host_result,
                    attempt_id=_rb_attempt_id,
                )
            elif _rebuild_superseded:
                image_ops.record_result(
                    _rb_op_id,
                    False,
                    result=_rb_host_result,
                    state=image_ops.STATE_SUPERSEDED,
                    attempt_id=_rb_attempt_id,
                )
            else:
                image_ops.record_result(
                    _rb_op_id,
                    False,
                    error={
                        "rc": _rb_rc["v"],
                        "stderr": _rb_output["stderr"][-1000:],
                    },
                    state=image_ops.STATE_UNKNOWN,
                    attempt_id=_rb_attempt_id,
                )
        new_status = "running"
    elif action == "pause":
        vm_dir = f"/data/firecracker-vms/{tenant_id}"
        ssm_dispatch._ssm_run(
            item["host_id"],
            f"curl -s --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/vm "
            f'-H "Content-Type: application/json" -d \'{{"state":"Paused"}}\'',
        )
        new_status = "paused"
    elif action == "resume":
        vm_dir = f"/data/firecracker-vms/{tenant_id}"
        ssm_dispatch._ssm_run(
            item["host_id"],
            f"curl -s --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/vm "
            f'-H "Content-Type: application/json" -d \'{{"state":"Resumed"}}\'',
        )
        new_status = "running"
    elif action == "suspend":
        # #422 — 休眠:释放 host slot 供新用户调度,同时保住数据可恢复。冷恢复语义
        # (备份数据盘到 S3 + 停 VM + 释放 slot,保留 DDB 记录与 tenant_id;恢复走 restore)。
        # 独立同步分支(不落底部通用 update):有 fail-closed 备份+回滚语义,与 backup/migrate 同类。
        # host_guard 必须传下去(codex 第九轮):suspend 的 stop-vm 与 rm -rf 是裸跑的,
        # 而 reaper 会在 1200s 后回滚卡住的 suspending —— 延迟落地的命令会在回滚之后
        # 才删盘。guard 同时校验 owner+epoch+租约未过期,配上 reaper「租约过期才回滚」
        # 就闭合了。见 _FENCED_LIFECYCLE_ACTIONS 上方的说明。
        # _lifecycle_ctx 必须传下去(#547 兄弟路径):suspend 的 "stop-vm 之前" 失败分支
        # 要靠它把 release_lifecycle_fence_on_error 写回本 ctx,否则 `tenant_action` 的
        # 5xx wrapper(:5114)对那些 502 一律扣住租约 1800s,而它们的文案写着 Retry。
        return _tenant_suspend(
            tenant_id,
            item,
            host_guard=_lifecycle_host_guard,
            _lifecycle_ctx=_idem_ctx,
        )
    elif action == "restore":
        # #422 — 恢复:唤醒一个 suspended 租户到【同一 tenant_id】,从 S3 冷恢复数据盘、
        # 重取 host+vm_num+slot、挂回原记录。走 create 同款冷恢复链(_resolve_backup +
        # launch-vm RESTORE_KEY),但不新建租户。
        return _tenant_restore(tenant_id, item)
    elif action == "backup":
        # ── #564 G7 —— 网关手动备份的**可轮询句柄**与状态字段 ────────────────────
        #
        # 在这之前这条出口返 `202 {"status":"started"}` 就结束了:**没有 op_id、没有任何
        # 状态字段**。客户拿着那个 202 无从知道备份成不成功 —— 而客户口径给它的死线是 600s。
        # 那正是 issue 零节那句「接口返回成功、实际操作没有发生,且调用方无从察觉」。
        #
        # 字段形状(与 #565 G6 同一套,两个 issue 明文要求"别各出一套"):
        #   backup_op_id     本次操作的句柄,回在 202 里,后续轮询靠它对上"是哪一次"
        #   backup_phase     queued → running → succeeded / failed
        #   backup_deadline  绝对死线 epoch(G2 的口径),由死线执行者扫
        #   backup_fail_reason / backup_fail_at   封闭取值 + 时刻(#565 G3 已建好字段)
        #
        # **只用一个 `backup_phase`,不学 rebuild 的两字段**:rebuild 需要
        # `rebuild_status` 额外表达"确认了没"(回执可能丢),而备份要么写进了 S3 要么没有,
        # 没有那个第三态,所以一个字段就够。
        _bk_op_id = secrets.token_hex(16)
        _accepted_epoch = int(time.time())
        _bk_dl, _bk_dl_src = deadline_config.deadline_epoch_for(
            create_deadline.ACTION_BACKUP, _accepted_epoch
        )
        if _bk_dl_src != "ssm":
            print(
                f"backup_deadline: {_bk_dl - _accepted_epoch}s "
                f"from {_bk_dl_src} for {tenant_id}"
            )
        # **契约性写入必须在派发之前落库** —— 与 `enqueue_lifecycle` 的 `before_send` 同一条
        # 纪律,而且这里更硬:派发一出去,backup Lambda 可能立刻把 phase 推到 `running`,
        # 生产者随后再写就会把它**倒退回 `queued`**(进度倒退);更糟的是若那次写入失败,
        # 客户已经拿到 202 和句柄,却在记录里找不到这个 op —— 202 承诺了可轮询。
        # 写失败即 5xx、**不派发**:宁可让调用方重试,也不发出一次无法被轮询的备份。
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression=(
                    "SET backup_op_id = :op, backup_phase = :ph, "
                    f"{create_deadline.deadline_attr(create_deadline.ACTION_BACKUP)}"
                    " = :dl, backup_started_at = :t "
                    # 上一次备份的失败痕迹在新一次受理时清掉:留着会让轮询方把旧原因
                    # 读成本次的结果。时刻字段一并清,两者必须同时存在或同时不存在。
                    f"REMOVE {create_deadline.fail_reason_attr(create_deadline.ACTION_BACKUP)}, "
                    f"{create_deadline.fail_at_attr(create_deadline.ACTION_BACKUP)}"
                ),
                # `attribute_exists(id)` 防 upsert 造畸形行(理由见 `_write_action_deadline`)。
                ConditionExpression="attribute_exists(id)",
                ExpressionAttributeValues={
                    ":op": _bk_op_id,
                    ":ph": _BACKUP_PHASE_QUEUED,
                    ":dl": _bk_dl,
                    ":t": utils._now(),
                },
            )
        except Exception as e:  # noqa: BLE001
            print(f"backup handle anchor failed for {tenant_id}: {e}")
            return utils._resp(
                503,
                {
                    "error": "could not record the backup operation before dispatch; "
                    "nothing was started - safe to retry",
                    "code": "ENQUEUE_ANCHOR_FAILED",
                    "id": tenant_id,
                    "op_id": _bk_op_id,
                },
            )
        # Async invoke Backup Lambda with single tenant
        lambda_client = boto3.client("lambda")
        lambda_client.invoke(
            FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
            InvocationType="Event",  # async, returns immediately
            # `backup_op_id` 是**手动备份的判别符**:backup Lambda 只在收到它时才写
            # phase。删前备份与 suspend 备份走同一个函数但不带它 —— 它们的失败由各自的
            # fail-closed 路径处置,而**系统定时备份的错峰语义不许被顺手改**(客户表格明文
            # 只要"网关手动备份")。
            Payload=json.dumps(
                {
                    "tenant_id": tenant_id,
                    "backup_op_id": _bk_op_id,
                    # #565 G1 —— 手动备份走通道 D:**异步**,不受调用侧 read_timeout 约束,
                    # 600s 死线下执行段给足 300s。这是四种调用方式里预算最大的一个,
                    # 也是唯一对大盘友好的那条路。
                    "ssm_budget_sec": create_deadline.exec_step_sec(
                        create_deadline.ACTION_BACKUP, "backup"
                    ),
                }
            ).encode(),
        )
        audit._publish_event(
            "tenant.backup_started", tenant_id, {"backup_op_id": _bk_op_id}
        )
        return utils._resp(
            202,
            {
                "id": tenant_id,
                "action": "backup",
                # `status` 保持 "started" 不动 —— 已发布字段,客户可能在读。新字段是加法。
                "status": "started",
                "backup_op_id": _bk_op_id,
                "backup_phase": _BACKUP_PHASE_QUEUED,
                create_deadline.deadline_attr(
                    create_deadline.ACTION_BACKUP
                ): _bk_dl,
            },
        )
    elif action == "access":
        # Explicit tenant authorization (P0): owner/admin grants or revokes
        # another Cognito sub access to this tenant. Gated by _assert_owner_or_admin
        # above (only owner/admin can manage grants — least privilege). The grant
        # list lives in the tenant record's `authorized_users` map and the hub
        # consults it for /token + /files + WS. Audited via _audit_write (caller).
        return tenant_access_grant(tenant_id, item, body)
    elif action == "provision":
        # #106 — 两段式下单/开通状态机的第二段:pending → provisioned。
        # 这是「业务开通」(把一笔已下单的租户标记为业务可用),与 VM 生命周期
        # status(creating/running)正交,所以走独立字段 purchase_status、独立
        # 分支直接返回(不落到底部那套改 status 的通用更新)。
        # 幂等 + 防错序:CAS 条件更新 purchase_status = pending 才翻 provisioned。
        #   • 记录本来就没有购买语义(从没下过单)→ 400,不允许凭空开通。
        #   • 已经是 provisioned → 200 幂等返回(重复 provision 不报错、不副作用)。
        #   • 处于 pending → 原子翻成 provisioned(ConditionExpression 防并发双开)。
        cur = item.get("purchase_status")
        if cur is None:
            return utils._err(
                400,
                "VALIDATION",
                "tenant has no purchase to provision (create it with an order first)",
                extra={"id": tenant_id},
            )
        if cur == _PURCHASE_PROVISIONED:
            return utils._resp(
                200,
                {
                    "id": tenant_id,
                    "purchase_status": _PURCHASE_PROVISIONED,
                    "message": "already provisioned",
                },
            )
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET purchase_status = :prov, provisioned_at = :t, updated_at = :t",
                ConditionExpression="purchase_status = :pend",
                ExpressionAttributeValues={
                    ":prov": _PURCHASE_PROVISIONED,
                    ":pend": _PURCHASE_PENDING,
                    ":t": utils._now(),
                },
            )
        except (
            clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException
        ):
            # Lost the CAS race (a concurrent provision already flipped it) — the
            # end state is provisioned either way, so report success idempotently.
            return utils._resp(
                200,
                {
                    "id": tenant_id,
                    "purchase_status": _PURCHASE_PROVISIONED,
                    "message": "already provisioned",
                },
            )
        audit._publish_event("tenant.provisioned", tenant_id, {})
        return utils._resp(
            200,
            {
                "id": tenant_id,
                "purchase_status": _PURCHASE_PROVISIONED,
                "provisioned": True,
            },
        )
    else:
        return utils._resp(400, {"error": f"unknown action: {action}"})

    if action == "rebuild" and locals().get("_rebuild_superseded", False):
        return utils._resp(
            409,
            {
                "error": "the rebuild lost its lifecycle fence or source host; "
                "its host result was discarded",
                "code": "LIFECYCLE_SUPERSEDED",
                "id": tenant_id,
                "op_id": (event or {}).get("_op_id"),
            },
        )

    update_expr = "SET #s = :s, updated_at = :t"
    expr_values = {":s": new_status, ":t": utils._now()}
    # #304 — 只在校验通过后才标 rootfs_version,不谎报漂移。
    #   · reset:非升级语义(丢 overlay 回出厂),照旧无条件标 host 当前版本。
    #   · rebuild:是升级采用,只有 relaunch 校验(FC 不在 deleted 旧 inode 上)
    #     通过(_rebuild_verified 为真)才标新版本;校验失败 → 不标,GET /tenants
    #     仍显示旧版本 = 如实反映"这台没升成",而不是谎报新。
    _stamp_rootfs = action == "reset" or (
        action == "rebuild" and locals().get("_rebuild_verified", False)
    )
    # codex#6 — 多个可选片段(rootfs 投影键 + rebuild_status 标记)可能同时要 REMOVE。
    # DynamoDB UpdateExpression 只允许一个 REMOVE 子句,拼两个(", REMOVE ... REMOVE ...")
    # 是非法语法 → update 抛错、消息进 DLQ。统一收集 SET/REMOVE 片段,各出一个子句。
    _remove_attrs: list[str] = []
    if _stamp_rootfs:
        # #416 — 版本记录写【本次实际采用】的版本,而非无脑写 host.rootfs_version。
        # canary 换版后租户跑的是 canary 槽的候选版本,host.rootfs_version 是该 host 的
        # live 版本;写 live 会把跑 candidate 的租户在 GET /hosts/rootfs-drift 误算成
        # up_to_date(谎报)。换版解析出的 _repin_snapshot 才是真值；live channel
        # 不固定 snapshot，采用后仍写 host.rootfs_version。
        _resolved_ver = (
            locals().get("_rb_evidence_snapshot")
            or locals().get("_repin_snapshot")
            or None
        )
        if _resolved_ver:
            # #534 —— _resolved_ver 是本次采用的 snapshot_time;反解成版本 label 再写,让
            # rootfs_version 统一为 label 坐标(host 侧本就是 label)。精确快照仍活在
            # image_snapshot_time,不动。查不到 label → 原样透传(fail-safe,不谎报)。
            from core.version_labels import label_for_snapshot
            _stamp_ver = label_for_snapshot(_resolved_ver)
        else:
            host = clients.hosts_table.get_item(
                Key={"instance_id": item["host_id"]}, ConsistentRead=True
            ).get("Item", {})
            _stamp_ver = host.get("rootfs_version", "")
        update_expr += ", rootfs_version = :rv"
        expr_values[":rv"] = _stamp_ver
        if expr_values[":rv"] and len(expr_values[":rv"].encode("utf-8")) <= 256:
            update_expr += ", q_rootfs_version = :qrv"
            expr_values[":qrv"] = expr_values[":rv"]
        else:
            _remove_attrs.append("q_rootfs_version")

    # #517 immutable_version 盖戳(F3 + codex 交叉审 C4 后的最终口径)。
    #   背景:launch-vm.sh:801-816 —— 钉版/canary 租户从 versions/<snapshot>/ 取只读盘(与 rootfs
    #   同快照目录、同源 manifest.version);未钉版(live channel)租户则解析 live、重挂 host 当前
    #   只读盘。故正确盖戳分两类:
    #   · 采用事件(reset / 校验通过 rebuild):取 _stamp_rootfs 解出的 _resolved_ver(canary 候选/
    #     rebuild 真实快照),回落 host.immutable_version。
    #   · 唤醒事件(restart / start)且【未钉版】:重挂 host 当前只读盘 → 取 host.immutable_version
    #     使坐标收敛(C4:issue §6 的「restart 后 md 翻新」正是这条;F3 只让钉版租户别被误标 live,
    #     不是让 live 租户永不收敛)。【钉版/canary 租户 restart/start 不盖】——它们重挂的是自己
    #     钉的旧快照,盖 host live 会谎报(F3)。resume 排除(解冻非重挂,仍持旧 inode)。
    #   取值为空即照「非空才写」不覆盖(不谎报;旧 host / 无 immutable 盘的快照同此)。
    _is_pinned = bool(item.get("image_snapshot_time"))
    _stamp_immutable = _stamp_rootfs or (
        action in ("restart", "start") and not _is_pinned
    )
    if _stamp_immutable:
        _resolved_ver_i = locals().get("_resolved_ver") or None
        if _resolved_ver_i:
            # #534 —— 同 rootfs:采用的 snapshot_time 反解成 label 再写,统一 immutable_version 坐标。
            from core.version_labels import label_for_snapshot
            _imm_ver = label_for_snapshot(_resolved_ver_i)
        else:
            # 未钉版唤醒 / 无 resolved 的 reset:取 host 当前 immutable_version。
            # _stamp_rootfs 的 else 分支可能已按同 key 强一致读过 host;复用避免二次读。
            _imm_host = locals().get("host")
            if _imm_host is None:
                _imm_host = clients.hosts_table.get_item(
                    Key={"instance_id": item["host_id"]}, ConsistentRead=True
                ).get("Item", {})
            _imm_ver = _imm_host.get("immutable_version", "")
        if _imm_ver:
            update_expr += ", immutable_version = :iv"
            expr_values[":iv"] = _imm_ver

    # #411/6.4 — rebuild 采用校验的结果必须【可见】,不再静默停在旧版本。真机(新加坡
    # 长轮询 x3)坐实约 1/3 的 rebuild:VM 真重启了(FC pid 变),但 _ssm_run 在 300s 内
    # 没拿到 Success 回执(SSM lag / consumer 180s 先超时)→ _rebuild_verified=False →
    # 上面不标 rootfs_version,而 API 仍返 200-running → 客户看不出"没升成",误判卡住。
    # 校验未过就标 rebuild_status(+reason),校验过则标终态。这不改 SSM/consumer 时序
    # 本身(那两条对齐在下方 CDK/consumer 处)。
    #
    # ADR-rebuild-idempotency-sync-contract §5.4 — 三值化:把"没能确认"从"确认失败"里
    # 摘出来。#411 那版把校验未通过一律标 `failed`,而它的 reason 原文自述是
    # "adoption not verified within timeout" = **我没能确认成功**,不是"失败了"。
    # 而那约 1/3 的案例里多数真机其实已经升级成功,只是 SSM 回执丢了。客户读到
    # `failed` → 按常理重试 → 又跑一遍 stop && rm overlay && launch → 抹掉两次之间
    # 落盘的写入。**字段值本身在引导客户做危险操作**,故改名即止血。
    #   done        = 确认成功(采用校验通过)
    #   failed      = 确认失败,可安全重试(本轮无写入点,见下方 else 分支说明)
    #   unconfirmed = 本 attempt 暂时不知道；调用方先查状态，并以同 client_token 重试
    # `unconfirmed` 与 core/image_ops 的 STATE_UNKNOWN 同源同理:那里已论证过"落
    # FAILED 会让同 key 重试被挡死,违背重试可对账"。
    if action == "rebuild":
        _evidence_target = locals().get("_rb_evidence_snapshot") or ""
        if _evidence_target:
            update_expr += ", rebuild_target_snapshot_time = :rb_target"
            expr_values[":rb_target"] = _evidence_target
        if locals().get("_rebuild_verified", False):
            # 确认成功:显式标 done(而非 REMOVE 掉标记)。REMOVE 会让"这次成功了"与
            # "从没 rebuild 过"在客户看来完全一样 —— 轮询方无法判断自己那次是否已收敛,
            # 只能靠 rootfs_version 侧信道猜。留下 done + 本次 op_id 才是可轮询的终态。
            update_expr += ", rebuild_status = :rbs"
            expr_values[":rbs"] = _REBUILD_STATUS_DONE
            update_expr += ", rebuild_phase = :rbp"
            expr_values[":rbp"] = _REBUILD_STATUS_DONE
            _remove_attrs.append("rebuild_failed_reason")
        else:
            # 这里恒标 unconfirmed 而不是 failed：回执丢失、host 失败或 evidence 不完整
            # 都只说明当前 attempt 没能确认。调用方必须保留 client_token，让幂等层
            # 对账同一操作；不同 op 仍会被 lifecycle owner/epoch 闸拒绝。
            update_expr += ", rebuild_status = :rbs, rebuild_failed_reason = :rbr"
            update_expr += ", rebuild_phase = :rbp"
            expr_values[":rbs"] = _REBUILD_STATUS_UNCONFIRMED
            expr_values[":rbp"] = _REBUILD_STATUS_UNCONFIRMED
            expr_values[":rbr"] = (
                "host adoption evidence was not confirmed for this attempt. The "
                "caller must check tenant status before retrying and reuse the same "
                "client_token so the operation can be reconciled safely."
            )
        # 本次操作标识：客户可与 tenant 记录里的 rebuild_op_id 对照。
        _rb_final_op_id = locals().get("_rb_op_id")
        if _rb_final_op_id:
            update_expr += ", rebuild_op_id = :rbo"
            expr_values[":rbo"] = _rb_final_op_id
        if _should_stamp_reapply(_rebuild_verified, _reapply_binding):
            update_expr += (
                ", config_template = :cfg_tpl"
                ", config_reapply_registry_version = :cfg_reg"
                ", config_reapply_body_version_id = :cfg_vid"
                ", config_reapply_body_sha256 = :cfg_sha"
                ", config_reapply_image_snapshot_time = :cfg_img"
            )
            expr_values.update(_reapply_stamp_values(_reapply_binding))

    if _remove_attrs:
        update_expr += " REMOVE " + ", ".join(_remove_attrs)
    _final_update = {
        "Key": {"id": tenant_id},
        "UpdateExpression": update_expr,
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": expr_values,
    }
    if action in _FENCED_LIFECYCLE_ACTIONS:
        _cond, _fence_vals = lifecycle_fence.condition(
            _lifecycle_op_id, _lifecycle_fence_epoch
        )
        # #595 —— fence 条件只证"围栏还归本 op_id/epoch",不看 status。带外删除(scaler TTL /
        # host 终止)不经 lifecycle fence、不抬 epoch,故 in-flight 的 rebuild/reset 完成写(会写
        # running/rootfs_version/q_rootfs_version)撞上带外删除时 fence 仍成立 → 把已删租户复活
        # (status=running + 重进 gsi_rootfs_version)。追加 `#s <> :deleted`:任何路径删掉即拒绝
        # 终态回写。API DELETE 走 fence(抬 epoch)本已被上面 fence 条件挡住,这条专补带外删除缺口。
        _final_update["ConditionExpression"] = f"({_cond}) AND #s <> :deleted"
        _final_update["ExpressionAttributeValues"].update(_fence_vals)
        _final_update["ExpressionAttributeValues"][":deleted"] = "deleted"
    else:
        # #339 目标①「所有终态/生命周期状态回写加 ConditionExpression(预期旧状态 CAS),
        # 不再无条件覆盖」—— 这里是 tenant_action 唯一的终态回写点,而条件此前**只对
        # fenced action 注入**。非 fenced 的 `stop/start/pause/resume` 走到这里是零条件写。
        #
        # 为什么围栏条件不能替代这一条:fenced action 的条件是
        # `lifecycle_fence.condition()` 派生的「围栏还归我这个 op_id / epoch」,与租户
        # status 无关;而这四个动作压根不取围栏(不在 `_FENCED_LIFECYCLE_ACTIONS` 里),
        # 于是既没有围栏、也没有状态条件,是完全裸的覆盖写。
        #
        # 现实链(issue #339「删除后被复活」的那一条):
        #   T0 start 入口 ConsistentRead 读到 stopped,过 #321 的 deleted/deleting 闸
        #   T1 start 发 launch-vm SSM,最长等 300s
        #   T2 期间 DELETE 到达 → CAS 翻 status=deleting → delete-vm.sh 排队
        #   T3 start 的 SSM 回来,零条件写 `status=running`  ← 已删租户被复活
        # 加 CAS 后 T3 打 CCF、status 保持 deleting,delete 继续收敛;host 侧那个被
        # start 起来的 VM 由 delete-vm.sh 的 stop+rm 清掉 —— 收敛方向正确。
        _final_update["ConditionExpression"] = "#s = :expected_prev"
        _final_update["ExpressionAttributeValues"][":expected_prev"] = _entry_status
    try:
        clients.tenants_table.update_item(
            **_final_update
        )
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        # #565 G3(Codex 独立复审抓出的缺口)—— 无论下面走 raise 还是 409,这一条都是
        # 「本次操作执行期间被别的操作抢先」,正是 preempted 的定义。restart 此前**只有**
        # host_unreachable 一个出口,于是它在契约里声明的 preempted **没有任何产出点** ——
        # 而契约文档明写「只声明代码里真有出口的值,偏大是撒谎」。
        # 放在分支判断【之前】:raise 那一支照旧上抛(围栏处置不在本 issue 范围),这里只补
        # 一条可观测记录,不改任何调用方契约。
        #
        # **闸必须查「该 action 的子集里有没有 preempted」,不能只查「action 有没有契约」。**
        # 这里的 `action` 是**运行时变量**(start/stop/restart/reset/backup/migrate…),而
        # `_mark_fail_reason` 的取值断言是刻意 raise 的(硬编码调用点写错要在开发期炸)。
        # 两者相撞:`backup` 有契约但它的子集**不含** preempted(手动备份不与别的操作抢状态),
        # 只查前者就会让 assert 抛出 ValueError,把一次 CCF 变成 500 —— 正好是 G3 要消灭的
        # 「不知道为什么」。migrate/reset 压根没有契约,同一道闸一并挡住。
        if create_deadline.REASON_PREEMPTED in create_deadline.REASONS_FOR.get(action, ()):
            _mark_fail_reason(tenant_id, action, create_deadline.REASON_PREEMPTED)
        if action in _FENCED_LIFECYCLE_ACTIONS:
            # 围栏条件失败的处置不在 #339 范围内(围栏有自己的一整套 owner/epoch/租约
            # 语义与调用方契约),保持原行为:继续上抛。
            raise
        # #339 —— CAS 失败 = 本次操作决策后,租户状态被别的操作改了。**绝不覆盖**。
        # 409 与同族的 LIFECYCLE_IN_FLIGHT/LIFECYCLE_SUPERSEDED 一致:调用方重读状态
        # 后自行决定是否重发。host 侧本次动作可能已生效(SSM 已跑),但那是【可收敛】的:
        # 抢赢的那个操作(delete/suspend/rebuild…)自己会把 VM 带到它要的形态。
        return utils._resp(
            409,
            {
                "error": (
                    f"tenant state changed while {action} was running "
                    f"(expected {_entry_status!r}); the write was refused so the "
                    "concurrent operation can converge. Re-read the tenant, then "
                    "decide whether to retry."
                ),
                "code": "TENANT_STATE_CHANGED",
                "id": tenant_id,
                "expected_status": _entry_status,
            },
        )
    # Issue #13 — publish lifecycle event for the action.
    # Map action verbs to lifecycle event names so consumers can filter.
    _action_to_event = {
        "stop": "tenant.stopped",
        "start": "tenant.started",
        "restart": "tenant.restarted",
        "pause": "tenant.paused",
        "resume": "tenant.resumed",
        "reset": "tenant.reset",
        "rebuild": "tenant.rebuilt",
    }
    # codex#8 — rebuild 未采用校验时,绝不发 tenant.rebuilt 成功事件(否则谎报一次成功)。
    # ADR §5.4 — 事件名同步改成 tenant.rebuild_unconfirmed:与上面 rebuild_status=
    # unconfirmed 一致。旧名 tenant.rebuild_failed 断言了"失败",而我们只知道"没确认";
    # 下游告警按 failed 处理会把多数其实已升级成功的租户误报成故障。
    _rebuild_unverified = action == "rebuild" and not locals().get(
        "_rebuild_verified", False
    )
    if _rebuild_unverified:
        event_name = "tenant.rebuild_unconfirmed"
    else:
        event_name = _action_to_event.get(action, f"tenant.{new_status}")
    audit._publish_event(
        event_name, tenant_id, {"action": action, "status": new_status}
    )
    # A synchronous 503 reports missing adoption evidence. Callers inspect tenant
    # state and reuse client_token; source-host and epoch checks reject stale work.
    _response_code = (
        503
        if action == "rebuild" and not locals().get("_rebuild_verified", False)
        else 200
    )
    return utils._resp(
        _response_code,
        {
            "id": tenant_id,
            "status": new_status,
            **(
                {
                    # 与上面落库的值取同一来源,避免响应体和 DDB 说两套话。
                    "rebuild_status": expr_values.get(":rbs", _REBUILD_STATUS_DONE),
                    "op_id": locals().get("_rb_op_id"),
                    **(
                        {
                            "code": "REBUILD_RETRY_PENDING",
                            "error": "host evidence not confirmed; check tenant status "
                            "before retrying and reuse the same client_token",
                        }
                        if _response_code == 503
                        else {}
                    ),
                }
                if action == "rebuild"
                else {}
            ),
            **(
                {
                    "registry_version": _reapply_binding["registry_version"],
                    "body_version_id": _reapply_binding.get(
                        "body_version_id", ""
                    ),
                    "body_sha256": _reapply_binding.get("body_sha256", ""),
                }
                if action == "rebuild"
                and _should_stamp_reapply(_rebuild_verified, _reapply_binding)
                else {}
            ),
        },
    )


def tenant_access_grant(tenant_id, item, body):
    """Grant or revoke a Cognito sub's access to a tenant (explicit authz, P0).

    Body: { "principal": "<cognito-sub>", "op": "grant"|"revoke",
            "role": "member"|"viewer"|... (grant only), "expire_at": <epoch?> }
    The owner is implicit (owner_id) and cannot be revoked here. Writes the
    tenant record's `authorized_users` map: { sub: {role, granted_at, expire_at?} }.
    Caller already passed _assert_owner_or_admin, so only owner/admin reach here.
    """
    try:
        payload = json.loads(body) if isinstance(body, str) else (body or {})
    except Exception:
        payload = {}
    principal = str(payload.get("principal", "")).strip()
    op = str(payload.get("op", "grant")).strip().lower()
    if not principal:
        return utils._resp(400, {"error": "missing principal (cognito sub)"})
    if op not in ("grant", "revoke"):
        return utils._resp(400, {"error": "op must be grant or revoke"})
    owner = item.get("owner_id")
    if principal == owner:
        return utils._resp(
            400, {"error": "owner access is implicit and cannot be modified"}
        )
    current = item.get("authorized_users")
    if not isinstance(current, dict):
        current = {}
    if op == "revoke":
        current.pop(principal, None)
    else:
        role = str(payload.get("role", "member")).strip() or "member"
        grant = {"role": role, "granted_at": utils._now()}
        exp = payload.get("expire_at")
        if isinstance(exp, (int, float)) and exp > 0:
            grant["expire_at"] = int(exp)
        current[principal] = grant
    clients.tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET authorized_users = :a, updated_at = :t",
        ExpressionAttributeValues={":a": current, ":t": utils._now()},
    )
    audit._publish_event(
        "tenant.access_changed", tenant_id, {"principal": principal, "op": op}
    )
    return utils._resp(200, {"id": tenant_id, "authorized_users": current})


def _resolve_backup(src_tenant_id, timestamp=None):
    """Return the S3 key of a backup, or empty string if not found.
    If timestamp is given, look up that exact backup. Otherwise return the most recent.

    数据):
      • bucket: backups 写在 BACKUP_BUCKET(WORM+CMK 专用桶,见 backup-data.sh:16
        `${BACKUP_BUCKET:-${ASSETS_BUCKET}}`),但这里原读 ASSETS_BUCKET → 永远 list
        空。回退 ASSETS_BUCKET 兼容未注入 BACKUP_BUCKET 的旧部署。
      • suffix: backup-data.sh 加密模式产出 `<ts>.gz.enc`(+`.gz.key` 辅助对象),
        非加密模式产出 `<ts>.gz`;原代码死拼 `<ts>.gz` → 加密备份匹配不到。改为
        后缀无关的前缀匹配:认 `<ts>.gz` 或 `<ts>.gz.enc`,排除 `.key`(那是数据
        密钥不是数据本体)。返回的 key 交给 host launch-vm.sh restore 时会原样下载。
    """
    bucket = os.environ.get("BACKUP_BUCKET") or os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")
    # ㉛ 必须【翻完整个前缀】(codex 独立复审第二十五轮)。
    #
    # 原来只取第一页(list_objects_v2 上限 1000 个 key)。两个后果,第二个更隐蔽:
    #   · S3 按【字典序】返回,而对象名是 ISO 时间戳 → **最新的备份排在最后**。
    #     一个租户攒够 1000 个 key(约 500 次备份;R7 是 24h 一次,且备份桶 Object Lock
    #     COMPLIANCE 让它们删不掉)之后,第一页里全是最旧的,"选最新"就恒选不到真正的最新;
    #   · 配对判据会【误杀】:`.enc` 在第一页、它的 `.key` 落到第二页时,_decryptable 判它
    #     是孤儿并跳过 —— 于是一个完全可用的恢复点被当成不可解,而这正是那道过滤要防的事
    #     的反面(它本该只挡真孤儿)。
    #
    # ⚠ 分页循环就是本分支开局修掉的那个 `_ssm_ping_map` 死循环形态:`resp.get(...)` 在
    # MagicMock 上恒返回真值 → while 永不退出、吃光内存。所以这里【硬上限】而不是只靠
    # IsTruncated 为假退出;超限 fail-loud(打日志 + 用已取到的部分继续,而不是静默截断)。
    _MAX_PAGES = 50  # 50 × 1000 = 5 万个 key,远超任何真实租户的备份数
    _all = []
    _tok = None
    for _page in range(_MAX_PAGES):
        _kw = {"Bucket": bucket, "Prefix": f"{prefix}/{src_tenant_id}/"}
        if _tok:
            _kw["ContinuationToken"] = _tok
        resp = clients.s3.list_objects_v2(**_kw)
        _all.extend(resp.get("Contents") or [])
        if not resp.get("IsTruncated"):
            break
        _tok = resp.get("NextContinuationToken")
        if not _tok:
            break
    else:
        print(
            f"_resolve_backup {src_tenant_id}: stopped after {_MAX_PAGES} list pages "
            f"({len(_all)} keys); selection may miss newer backups — investigate the "
            "backup retention/lifecycle policy for this prefix"
        )
    # 只认数据对象(.gz / .gz.enc),排除 .key(envelope 数据密钥,非数据本体)。
    objs = [o for o in _all if not o["Key"].endswith(".key")]
    # ⑪ codex 独立复审第七轮 —— 加密备份必须有【配对的 .key】才算可选。
    #
    # `.enc` 与 `.key` 是两次独立上传。backup-data.sh 本轮已改成"先传 .key、最后传 .enc"
    # (让 .enc 成为完整发布的完成标记),但那只保证【今后】不再产生孤儿 .enc;
    # 改之前那个顺序留下的孤儿(.key 上传失败而 .enc 已落地)还在桶里,而 Object Lock
    # 让它删不掉。选到一个解不开的 .enc 会把更早那个【可用】恢复点盖住,恢复时才发现
    # 解不开 —— 那时已经无路可退,属 no-data-loss。
    # 故解析侧也过一道:候选是 .enc 的,必须存在同名 .key。非加密的 .gz 不受影响
    # (它没有也不需要 .key)。
    _key_objs = {o["Key"] for o in _all if o["Key"].endswith(".key")}

    def _decryptable(o):
        k = o["Key"]
        if not k.endswith(".enc"):
            return True
        _paired = k[: -len(".enc")] + ".key"
        if _paired in _key_objs:
            return True
        print(
            f"_resolve_backup {src_tenant_id}: skipping {k} — no matching .key "
            f"({_paired}); an undecryptable .enc must not shadow an older usable backup"
        )
        return False

    objs = [o for o in objs if _decryptable(o)]
    if not objs:
        return ""
    if timestamp:
        base = f"{prefix}/{src_tenant_id}/{timestamp}.gz"
        # 旧格式:精确匹配 <ts>.gz 或加密态 <ts>.gz.enc(不含 .key)。
        match = [o for o in objs if o["Key"] == base or o["Key"] == f"{base}.enc"]
        if match:
            return match[0]["Key"]
        # 新格式(codex 复审补):R7 给 key 加了 run id 防同秒两次备份撞 key ——
        # backup-data.sh:50 现在写的是 `<ts>-<pid>-<ns>.gz`。上面的精确匹配对它恒不命中,
        # 于是【按 timestamp 恢复新备份必 404】,含控制台走的那条路。这里补上前缀匹配,
        # 同一秒可能有多份(这正是加 run id 的原因),取 LastModified 最新的那份。
        _pfx = f"{prefix}/{src_tenant_id}/{timestamp}-"
        run_id_match = [
            o for o in objs
            if o["Key"].startswith(_pfx)
            and (o["Key"].endswith(".gz") or o["Key"].endswith(".gz.enc"))
        ]
        if run_id_match:
            return max(run_id_match, key=lambda o: o["LastModified"])["Key"]
        return ""
    # Latest = highest LastModified
    return max(objs, key=lambda o: o["LastModified"])["Key"]


def tenant_resize(tenant_id, body):
    """POST /tenants/{id}/resize — hot-add vCPU on a running tenant."""
    if body is None:
        return utils._resp(400, {"error": "missing body"})
    try:
        body = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return utils._resp(400, {"error": "invalid json"})
    if not isinstance(body, dict):
        return utils._resp(400, {"error": "body must be a JSON object"})
    new_vcpu = body.get("vcpu")
    new_mem = body.get("mem_mb")
    if new_vcpu is None and new_mem is None:
        return utils._resp(
            400, {"error": "specify vcpu (memory live-resize not supported)"}
        )
    if new_mem is not None:
        return utils._resp(
            400,
            {
                "error": "memory live-resize is not supported; "
                "stop the tenant, recreate with new mem_mb, then start"
            },
        )
    try:
        new_vcpu = int(new_vcpu)
    except (TypeError, ValueError):
        return utils._resp(400, {"error": "vcpu must be an integer"})
    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return utils._resp(404, {"error": "tenant not found"})
    if item.get("status") != "running":
        return utils._resp(
            400, {"error": f"tenant must be running (current: {item.get('status')})"}
        )
    current_vcpu = int(item.get("vcpu", 0))
    if new_vcpu <= current_vcpu:
        return utils._resp(
            400,
            {
                "error": f"vcpu must be greater than current ({current_vcpu}); "
                "Firecracker cannot shrink — restart to decrease"
            },
        )
    quota_err = scheduling._check_quota(
        new_vcpu, int(item.get("mem_mb", 0)), int(item.get("data_disk_mb", 0))
    )
    if quota_err:
        return utils._resp(400, {"error": quota_err})
    host_id = item.get("host_id", "")
    if not host_id:
        return utils._resp(400, {"error": "tenant has no host assigned"})
    host = clients.hosts_table.get_item(
        Key={"instance_id": host_id}, ConsistentRead=True
    ).get("Item")
    if not host:
        return utils._resp(400, {"error": f"host {host_id} not found"})
    delta = new_vcpu - current_vcpu
    _cpu_r, _ = host_profile.ratios(
        host,
        (clients.CPU_OVERCOMMIT_RATIO, clients.MEM_OVERCOMMIT_RATIO),
        clients.OVERCOMMIT_BY_FAMILY,
    )
    allocatable = capacity.allocatable(int(host["total_vcpu"]), _cpu_r)
    free = allocatable - int(host["used_vcpu"])
    if delta > free:
        return utils._resp(
            400,
            {
                "error": f"insufficient host capacity: need {delta} more vCPU, "
                f"host has {free} free (allocatable={allocatable}, used={host['used_vcpu']})"
            },
        )
    vm_dir = f"/data/firecracker-vms/{tenant_id}"
    cmd = (
        f"curl -sf --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/machine-config "
        f'-H "Content-Type: application/json" '
        f'-d \'{{"vcpu_count":{new_vcpu},"mem_size_mib":{int(item["mem_mb"])}}}\''
    )
    if not ssm_dispatch._ssm_run(host_id, cmd, timeout=30):
        return utils._resp(
            502, {"error": "Firecracker machine-config PATCH failed; tenant unchanged"}
        )
    now = utils._now()
    clients.tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET vcpu = :v, updated_at = :t",
        ExpressionAttributeValues={":v": new_vcpu, ":t": now},
    )
    clients.hosts_table.update_item(
        Key={"instance_id": host_id},
        UpdateExpression="SET used_vcpu = used_vcpu + :v",
        ExpressionAttributeValues={":v": delta},
    )
    return utils._resp(
        200,
        {
            "id": tenant_id,
            "vcpu": new_vcpu,
            "mem_mb": int(item["mem_mb"]),
            "delta": delta,
        },
    )


def _rewrap_for_recipient(kms_plaintext, pem, field, key_id):
    """KMS 明文(bytes|str)→ recipient 公钥 OAEP 加密的 enc:v1: 信封。纯本地 RSA。"""
    from core.envelope import encrypt_outbound

    plain = (
        kms_plaintext.decode() if isinstance(kms_plaintext, bytes) else kms_plaintext
    )
    return encrypt_outbound(plain, pem, field, key_id)


def get_tenant_credentials(tenant_id, event=None):
    """GET /tenants/{id}/credentials — 出站凭据交付(asymmetric-v1,无 legacy 回落)。

    recipient key 由平台首调自动生成(ensure_bootstrap_key:公钥 DDB / 私钥
    Secrets Manager,运维线下交给调用方),调用方无需注册。流程:KMS 解密存储密文 →
    recipient 公钥重新 OAEP 加密 → 返回;调用方本地私钥解密。解密/重加密任一步失败
    返 502 fail-loud,绝不回落成调用方解不开的 KMS 密文(旧 legacy flat 形态已删,
    它只在平台自己持有 kms:Decrypt 的 bootstrap 调试场景有意义)。
    非 running → 404;owner==caller/admin 门与 get_tenant 同款(#80 IDOR)。
    """
    from services.recipient_key_service import ensure_bootstrap_key
    from core import kms_envelope

    item = clients.tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return utils._err(404, "NOT_FOUND", f"tenant {tenant_id} not found")
    denied = auth._assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied
    if item.get("status") != "running":
        return utils._err(
            404,
            "NOT_FOUND",
            f"tenant {tenant_id} is not running (status={item.get('status')})",
        )

    # 读 gateway token + device identity(TTL 窗口外/未铸 → None,响应字段留空)
    gw_ct = read_gateway_token_ct(tenant_id)
    device = read_device_identity(tenant_id)

    try:
        recipient_key = ensure_bootstrap_key()
    except Exception as e:  # noqa: BLE001 — 首启生成失败必须可见,不降级
        return utils._err(
            502,
            "RECIPIENT_KEY_UNAVAILABLE",
            f"recipient key bootstrap failed: {type(e).__name__}",
        )
    if not recipient_key.get("enabled", False):
        # key 被显式 disable(轮换中):fail-closed,让运维登记新 key,绝不回落 KMS 密文
        return utils._err(
            409,
            "RECIPIENT_KEY_DISABLED",
            "current recipient key is disabled; register a new key "
            "(POST /recipient-key) before fetching credentials",
        )
    pem = recipient_key["public_key_pem"]
    key_id = recipient_key["key_id"]

    gw_enc = ""
    dev_privkey_enc = ""
    try:
        if gw_ct:
            gw_enc = _rewrap_for_recipient(
                kms_envelope.decrypt_with_tenant(gw_ct, tenant_id),
                pem,
                "gateway.token",
                key_id,
            )
        if device and device.get("private_key"):
            dev_privkey_enc = _rewrap_for_recipient(
                kms_envelope.decrypt(device["private_key"], item.get("owner_id", "")),
                pem,
                "device.private_key",
                key_id,
            )
    except Exception as e:  # noqa: BLE001 — fail-loud,绝不静默回落 legacy
        return utils._err(
            502,
            "CREDENTIAL_REWRAP_FAILED",
            f"credential re-wrap failed: {type(e).__name__}",
        )

    creds = {
        "claw_credentials": {
            "enc": {
                "scheme": "asymmetric-v1",
                "recipient_key_id": key_id,
                "algorithm": "RSA_4096_OAEP_SHA256",
                "key_version": recipient_key["version"],
            },
            "gateway": {"token": gw_enc},
            "device": {
                "id": device["device_id"] if device else "",
                "public_key": device["public_key"] if device else "",
                "private_key": dev_privkey_enc,
                "scopes": device.get("scopes", list(_DEVICE_SCOPES_DEFAULT))
                if device
                else list(_DEVICE_SCOPES_DEFAULT),
            },
        }
    }
    return utils._resp(200, creds)
