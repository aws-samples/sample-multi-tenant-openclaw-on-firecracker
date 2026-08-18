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
import core.host_profile as host_profile
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
    # 心跳的无条件 update_item 会建出只有心跳字段的行。第四轮评审抓到这条。
    if used_v is None or used_m is None:
        return _CLAIM_UNKNOWN
    return _CLAIM_CONTENDED


def _fresh_host_state(err_response):
    """从 CCF 捎回的旧值里取出【赢家写完后的当前状态】,供重试刷新本地 host 字典。

    #475 必修1 —— 只把"抢输"改成原地重试是【不够】的:重试仍然调 _reserve_slot(host),
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
# SSM root shell command; its ONLY legitimate use is as an S3 path slug
# (launch-vm.sh: s3://$ASSETS_BUCKET/templates/openclaw/${CONFIG_TEMPLATE}/openclaw.json),
# so it must be a plain DNS-label. Reject anything with shell metacharacters,
# whitespace, or path separators at the edge (defense in depth still quotes it
# in _launch_vm). Empty == "no custom template" and is validated separately.
_CONFIG_TEMPLATE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
# tenant-credential-contract Task 3.5 — registry 按 config_template 分区,注入路径
# 复用同一 DNS-label 约束(别名而非新 regex:同一约束一个来源,防两处漂移)。
_DNS_LABEL_RE = _CONFIG_TEMPLATE_RE

# caller-supplied idempotency key that flows into an SSM command and log lines.
# Restrict to 4-128 printable ASCII (codepoints 33-126): no spaces, no control
# chars (\n \t \x00), no non-ASCII. .isascii() alone lets control chars through.
_CLIENT_TOKEN_RE = re.compile(r"^[\x21-\x7e]{4,128}$")
_DELETE_CLAIM_TTL_SECONDS = 900
_FENCED_LIFECYCLE_ACTIONS = frozenset(
    {"rebuild", "migrate", "reset", "delete", "restart"}
)

# #501 — 健康位只由 health_check sweep 写(health_check/handler.py 的 vm_health/app_health
# update),而 sweep 跳过终态租户(`status not in ("deleted", "suspended")`)。于是删除后这三个
# 字段冻在删除前的值,DDB 里留下 status=deleted + vm_health=up + app_health=up 的行;软删无 TTL,
# 行永不消失,任何按健康位判「租户是否在役」的监控/巡检/排障都会把已删租户当成健康在役租户
# (客户排障实测已发生)。每条把 status 写成 deleted 的路径都必须一并 REMOVE 这三个字段——
# DDB 的 REMOVE 对不存在的属性是 no-op,所以 create 回滚路径带上它同样安全。
_STALE_HEALTH_FIELDS = "vm_health, app_health, last_health_check"
# create 回滚路径(mint token / clone-data / launch-vm 提交失败)共用的终态写法。
_ROLLBACK_DELETED_EXPR = f"SET #s = :s, updated_at = :t REMOVE {_STALE_HEALTH_FIELDS}"

# 业务场景:用户在外部平台页面「下单购买一个 claw」。租户记录带三个购买维度字段
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
    # 若不脱敏会随 GET /tenants(list/detail 都过 _redact_tenant)原样回给持
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


# ——deleted 租户不被 reaper 兜底 → 容量永漏。三态让 delete 在 retry 时留 deleting 返 5xx 重投)。
_REL_CONSUMED = "consumed"  # 本次扣了账本、清了令牌
_REL_ALREADY = "already"    # 令牌已不在(别人消费/从没有)或下溢守卫触发 → 安全幂等
_REL_RETRY = "retry"        # 瞬时失败(冲突/throttle/网络)→ 令牌可能仍在,必须重投再释放


def _release_capacity_reservation(tenant_id, host_id, reservation_id, vcpu, mem_mb):
    """#412 —— 令牌化释放 dispatch 预留:扣 host 账本 + 清租户令牌/放置,一个
    TransactWriteItems,条件 tenants.capacity_reservation_id=:rid。与 dispatch_service.
    _release_reservation / reaper 同款互斥锚:谁先消费令牌谁扣一次账本,其余幂等 no-op
    (codex #3 防 ABA 双扣)。

    返回三态(codex review #3):_REL_CONSUMED / _REL_ALREADY(安全,可继续标 deleted)/
    _REL_RETRY(令牌可能仍在,delete 必须留 deleting 返 5xx 重投,绝不 finalize)。

    独立实现(不 import dispatch_service):delete 是 no-data-loss 关键路径,自包含避免跨
    service 依赖;事务写法与本文件 canary put(:734)同源(原生值,不预 TypeSerializer)。"""
    retryable = {"TransactionConflict", "ThrottlingError",
                 "ProvisionedThroughputExceeded", "RequestLimitExceeded"}
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
    except ClientError as e:
        # 仅 tenant 项(idx1)条件失败才算 already(令牌已被别人消费);host 项(idx0)下溢
        # 或缺 reasons/可重试因 → retry(不当已释放,否则搁浅令牌 / delete 误 finalize)。
        code = e.response["Error"]["Code"]
        if code == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", []) or []

            def _code_at(idx):
                return reasons[idx].get("Code", "") if idx < len(reasons) else ""

            host_code, tenant_code = _code_at(0), _code_at(1)
            # CCF 优先判 ALREADY——最后一张预留双重释放时 host 下溢与 token-gone 会同时失败,
            # token-gone 说明别人已成功扣账本,本次安全幂等,绝不能因 host 下溢误报 retry 让 delete
            # 卡 deleting/进 DLQ。
            if host_code in retryable or tenant_code in retryable:
                print(f"delete_tenant #412: release {tenant_id} retryable cancel "
                      f"{[host_code, tenant_code]}")
                return _REL_RETRY
            if tenant_code == "ConditionalCheckFailed":
                return _REL_ALREADY  # 令牌已被别人消费/从没有 → 安全幂等
            if host_code == "ConditionalCheckFailed":
                print(f"delete_tenant #412: release {tenant_id} host underflow — retry+alarm")
                return _REL_RETRY
            print(f"delete_tenant #412: release {tenant_id} cancel w/o reasons — retry")
            return _REL_RETRY
        if code in retryable:
            print(f"delete_tenant #412: release {tenant_id} retryable error {code}")
            return _REL_RETRY
        print(f"delete_tenant #412: token release {tenant_id} error (retry): {e}")
        return _REL_RETRY  # 未知错误保守当可重试:宁重投也不搁浅令牌
    except Exception as e:  # noqa: BLE001
        print(f"delete_tenant #412: token release {tenant_id} error (retry): {e}")
        return _REL_RETRY


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


# 建租户时预铸 32 字节随机 token,KMS 信封绑 tenant_id 加密,密文落 openclaw-tenant-secrets
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

# 一站返全(status/token/device/vkey),调用方(如 JDWS 平台网关)按需反复取。密文长存
# 让 rebuild/recover/restore 在租户创建 1-2 年后仍能回读原始 token/device 身份 —— 过期
# 历史:旧 900s(15min)→ 30 天 → 现无 TTL;表 TTL 属性 + expires_at/device_expires_at
# 软过期检查全部移除。密文本身 KMS 加密(EncryptionContext 锁 tenant_id)+ 表在私网。
_GATEWAY_TOKEN_BYTES = 32  # 32 字节 → 43 char base64url(比 hex 短、URL 安全)

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
    # 身份共用同一 tenant_secrets 行(主键 tenant_id),无论谁先写,put_item 整条替换都会
    # 抹掉对方的字段。两侧都改 update_item 才真共存(reviewer 抓 device 侧覆盖 gateway,
    # 反序测试又暴露 gateway 侧同样会覆盖 device)。
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
    # gateway token 和 device 身份共用同一 tenant_secrets 行(主键 tenant_id),
    # put_item 是整条替换 → 会把先写的 gateway_token_ct 抹掉。update_item 只加/改
    # device_* 字段,gateway token 字段原样保留(反之亦然),两侧共存。
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

    #415(2.26→7.1 升级,真机实测):预铸一枚 `tokens.operator` 活跃 token。
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

    #353 — no TTL expiry check: the ciphertext persists for the tenant's whole
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

    #353 — 去掉 device_expires_at 软过期检查:device 私钥密文随租户生命周期长存,
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


def _phys_tap_occupied(host_id, phys_num, exclude_id=None):
    """#491 —— 实现已搬到 core.scheduling.phys_tap_occupied,供队列 dispatch 路径共用。

    本名保留:同步 create(:1868)、另一放置路径(:3203)、migrate 目标(:4428)三处调用点
    与既有测试(tests/test_208_tap_collision_adversarial.py)都按这个名字引用,
    连带改名会把「机械搬迁」和「行为变更」混进同一个 diff。
    """
    return scheduling.phys_tap_occupied(host_id, phys_num, exclude_id=exclude_id)


def _persist_tenant_record(item, tenant_id):
    """把租户记录落库。成功返回 None;可预期冲突返回错误响应;意外异常向上抛(调用方回滚 slot)。

    #93 —— 条件写 attribute_not_exists(id):同 id 重放(重试/双提交/队列重复消费)不覆盖已存在
    租户,冲突回 409 CONFLICT。

    #394 codex NB2 —— canary 租户固定了具体版本(image_snapshot_time)时,其持久化必须与全局删
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
            return utils._err(
                409, "CONFLICT", f"tenant '{tenant_id}' already exists",
                extra={"id": tenant_id},
            )
        raise


def _initial_immutable_version(host, pinned_image_snapshot_time=None):
    """Return the immutable coordinate for the disk a new tenant will attach."""
    return pinned_image_snapshot_time or host.get("immutable_version", "")


def create_tenant(body=None, event=None):
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

    # enforce owner==caller. Cognito `sub` for logged-in users, API_KEY_OWNER
    # for the API-key path. None only if a Bearer token was present but failed
    # verification — RBAC would already have rejected such a write.
    owner_id = auth._get_caller_identity(event or {})["owner_id"]
    # the tenant is attributable to the external user (identity chain). None for
    # native Cognito / API-key callers, in which case the field is not stored.
    tenant_user_id = auth._get_caller_identity(event or {}).get("tenant_user_id")
    # See _attribution_override for the security contract. Loud errors (403/400)
    # propagate; a returned value replaces the identity-derived default.
    _ovr_owner, _ovr_tuid, _ovr_err = _attribution_override(body, event)
    if _ovr_err is not None:
        return _ovr_err
    if _ovr_owner is not None:
        owner_id = _ovr_owner
    if _ovr_tuid is not None:
        tenant_user_id = _ovr_tuid
    #   1. body.platform_id — explicit, used by the "交易平台后端代开" path where the
    #      platform's server (API-key auth, no per-user Cognito token) creates a
    #      tenant on behalf of one of its users and states which platform it is.
    #   2. custom:platform_id claim — for the federated-user path (pretokengen
    #      injected it, resolved once in _get_caller_identity — no extra verify).
    # Body override is validated (caller-controlled input → _PLATFORM_ID_RE); the
    # claim value is already Cognito-controlled. Stored only when present, so
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
    # 调用方不需要 registry 表存在)。归一化只做形态翻译,校验各归各路 fail-closed。
    frozen_injection_plan = None
    registry_version = None
    injected_credentials = None
    _frozen_scheme = (
        None  # #149 — resolved scheme (kms-cmk/asymmetric-v1) 落库供 host 分派
    )
    _injected_params = utils._normalize_injected_parameters(body)
    # env_injected_credentials 触发;旧 injected_credentials 仍走下面的 legacy 分支。
    if (
        body.get("injected_parameters") is not None
        or body.get("env_injected_credentials") is not None
    ):
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
        # ciphertext). The API only validates + relays the ciphertext; the host
        # decrypts at VM launch (guest zero-credential baseline). Absent →
        # unchanged behavior.
        injected_credentials, ic_err = utils._validate_injected_credentials(
            body.get("injected_credentials"), clients.CLAWPOOL_CMK_ARN
        )
        if ic_err:
            return utils._err(400, "VALIDATION", ic_err)
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
    # 时才认——否则外部 POST 传任意 _assigned_tenant_id 会成为 id → 进下游 SSM/rm shell。外部
    # 首次调用一律用 _gen_id(受控字符集),杜绝注入源。
    _replay_id = (
        body.get("_assigned_tenant_id")
        if (event or {}).get("_consumer_ident")
        else None
    )
    tenant_id = _replay_id or utils._gen_id(name, client_token, owner_id)
    now = utils._now()

    # ── R16.2 在途去重:同 owner_id+tenant_user_id 只允许一个在途创建 ──
    # 仅首次 API 调用做(consumer 回放已有占位,跳过)。
    _name_scope = name_dedup.dedup_scope(owner_id, tenant_user_id, platform_id)
    if not (event or {}).get("_consumer_ident"):
        _lock_err = inflight_dedup.acquire_inflight_lock(owner_id, tenant_user_id)
        if _lock_err is not None:
            return _lock_err[0]
        # 占位成功,绑定 tenant_id 供后续 409 响应告知调用方
        inflight_dedup.bind_tenant_id(owner_id, tenant_user_id, tenant_id)
        # inflight 锁互补:inflight 只守在途窗口(成功即释放),name 锁跟随租户活跃
        # 生命周期(创建成功不释放,delete 终态才释放)→ 防"第一次已成功、第二次
        # 晚到"重放。同作用域同 name 已有活跃租户 → 409 NAME_EXISTS。失败要 release
        # inflight 锁(它成功也释放,语义不同),name 锁靠僵尸自愈兜底,不必逐点 release。
        _name_err = name_dedup.acquire_name_lock(_name_scope, name, tenant_id)
        if _name_err is not None:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return _name_err[0]

    # dispatch → binpack 是"不指定 host 的批量创建"优化:binpack 无 pinned 概念、且
    # dispatch msg 不带 preferred_host_id → 走它会把指定 host 丢掉(unplaced)。pinned
    _pinned_placement = bool(
        (body.get("preferred_host_id") or "").strip() or clone_from
    )
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
        # 抓出窄字段集的两类后果:丢 tags → batch by filter 0 命中;缺
        # channel_secret → hub 握手竞态回归(同步路径特意 mint-up-front)。
        # host_id/vm_num/guest_ip 此刻还不存在(消费端装箱才定),由
        # dispatch_service 装箱 CAS 成功后回写(_backfill_placement)。
        try:
            item = {
                "id": tenant_id,
                "status": "creating",
                # 写死 running),stop/start/pause 不更新、无任何读点/自愈/对账消费它,
                # 是语义错的死字段。删除比给每个生命周期动作补写更小(YAGNI:没人读的
                # 字段不该维护)。若将来要做 desired-state 对账,再作为独立 feature 全链设计。
                "dispatch_retries": 0,
                "created_at": now,
                "updated_at": now,
                "creation_started_at": now,
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
            clients.tenants_table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(id)",
            )
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                return utils._resp(
                    409, {"error": "tenant already exists", "id": tenant_id}
                )
            raise

        # 同步路径在 _launch_vm 前铸 device+token 并冷注入(:1228/:1254);dispatch
        # 路径此前只 put 占位 + send_message,把铸造留给了从不发生的同步分支 → 队列
        # 消息不带密文 → launch-vm 第 12/13 位参空 → wss 免 approve 在生产配置
        # (dispatch.enabled=true mode=push)100% 失效。这里在 send_message 之前补铸,
        # 把密文塞进 msg.params 一路穿透到 host 冷注入(dispatch_service manifest g/d 字段)。
        # fail-open:铸造失败不阻塞已 accepted 的异步 create(占位已落、202 已定),
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
                # scopes,**无私钥**,纯公开信息)。除了随 dispatch manifest 一次性下发,
                # 还长期存进 tenants 表(无 TTL)。根因:device 私钥密文在 tenant_secrets
                # fallback 拿到空 → launch-vm 无法重注入 paired.json → 网关读到空盘配对
                # → 前端 NOT_PAIRED(新加坡真机复现 + message-handler.ts:786 getPairedDevice
                # → isPaired=false)。paired.json 本身无私钥,长期留存不泄密,让 launch-vm
                # 每次(重)启动都能从 tenants 表幂等重建 approved backend 条目。
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
            "params": {
                "vcpu": int(vcpu),
                "mem_mb": int(mem_mb),
                "owner_id": owner_id,
                # chat_endpoint_enabled 校验而来),不是从 body 再取错 key chat_ep
                # (客户端从不传 chat_ep,那样恒 False → dispatch 路径静默丢开关)。
                # consumer(dispatch_service:399/557)读 params["chat_ep"],故此处 key
                # 仍叫 chat_ep(队列内部字段名),只修取值来源。
                "chat_ep": chat_endpoint_enabled,
                "image": config_template or "default",
                # 现状 dispatch 路径丢 restore_backup_key,restore-create 走队列时 host
                # 拿不到 key → 静默用空白模板盘(数据丢失级)。空 = 非 restore 普通建,
                # 下游 launch RESTORE_KEY 为空走建盘;非空 = restore,launch 必须用它,
                # 缺则 fail-loud 拒起(见 launch-vm 空 key 守卫)。
                "restore_backup_key": restore_backup_key,
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
        # tenant lands in the caller's namespace even if scope resolution on
        # replay ever changed. (enqueue_lifecycle also carries platform_scope in
        # _ident, which re-pins on replay; this makes the body self-consistent.)
        if platform_id:
            queued_body["platform_id"] = platform_id
        if lifecycle_dispatch.enqueue_lifecycle(
            "create", tenant_id, event, extra=queued_body
        ):
            # 在途窗口关闭:已入 FIFO 队列,释放占位锁(同上;见 inflight_dedup docstring)。
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                202,
                {
                    "id": tenant_id,
                    "status": "queued",
                    "message": "create accepted; provisioning asynchronously",
                },
            )

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

    # 两级路由到 microVM:18789 gateway,鉴权改走 gateway 原生 token。

    # Find host with capacity. The scheduler is normally automatic, but
    # operators occasionally need to pin a tenant to a specific host (e.g.
    # to drain a host before terminating it, or to keep two related VMs on
    # the same hardware). Three modes, in priority order:
    #   1. clone_from → must land on the source's host (local `cp` only)
    #   2. preferred_host_id (admin/operator) → land there or fail
    #   3. default → first host with capacity
    preferred_host_id = (body.get("preferred_host_id") or "").strip()
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
            return utils._err(
                409,
                "CONFLICT",
                f"tenant '{tenant_id}' already exists",
                extra={"id": tenant_id},
            )
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
        return utils._resp(
            201,
            {
                "id": tenant_id,
                "status": "pending",
                "message": "scaling out, VM will be created when host is ready",
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

        #475 必修1 —— 为什么必须分辨这两种失败:调用方在"真满了"时把 host 拉出候选池
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
        }
        try:
            _kwargs = dict(
                Key={"instance_id": h["instance_id"]},
                UpdateExpression=_set_expr,
                ConditionExpression=(
                    "next_vm_num = :expected AND used_vcpu <= :cap_v "
                    "AND used_mem_mb <= :cap_m AND attribute_not_exists(#ps)"
                ),
                ExpressionAttributeValues=_vals,
                ReturnValues="UPDATED_NEW",
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
    # double-submit, duplicate queue consume) must not overwrite an existing
    # tenant. On conflict we release the slot THIS attempt just reserved (the
    # original tenant keeps its own) and return 409 — avoids both a silent clobber
    # and a capacity leak. Any other failure rolls back + re-raises as before.
    #
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

    # both deployed). Enables control-plane reveal (GET /tenants/{id}/token) and
    # SSM-position-12 injection to launch-vm.sh (host decrypts + writes
    # openclaw.json .gateway.auth.token). Feature OFF → gateway_token_ct stays
    # None and launch-vm keeps `openssl rand`-ing its own gateway token in-VM
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
            _device = mint_device_identity(tenant_id, owner_id)
            device_paired_b64 = build_paired_json_b64(_device)
            # dispatch 路径(:1125)同样必须把 device_paired_b64 长期存 tenants 表(无 TTL)。
            # 否则这类租户某次 4 参 restart 若盘上 paired.json 恰空,三级回落全空
            # (位置参空 → tenant_secrets 无此组装字段 → tenants 表也没有)→ NOT_PAIRED
            # 无源重建。device_paired_b64 无私钥,长期留存不泄密。
            persist_device_paired_b64(tenant_id, device_paired_b64)
        except Exception as e:  # noqa: BLE001 — 可选增强,失败不回滚租户
            print(
                f"[#10] mint_device_identity failed (non-fatal): {type(e).__name__}: {e}"
            )
            device_paired_b64 = ""  # mint 失败 → 空参,launch-vm fail-open 跳过冷注入

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

    return utils._resp(
        201,
        {
            "id": tenant_id,
            "host_id": host["instance_id"],
            "guest_ip": guest_ip,
            "host_port": host_port,
            "status": "creating",
        },
    )


def _force_backup_sync(tenant_id):
    """同步强制备份租户数据盘到 S3,fail-closed。返回 (ok: bool, err_msg: str|None)。

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
        lambda_client = boto3.client("lambda")
        resp = lambda_client.invoke(
            FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
            InvocationType="RequestResponse",  # SYNC: data safe in S3 before rm
            Payload=json.dumps({"tenant_id": tenant_id, "pre_delete": True}).encode(
                "utf-8"
            ),
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

    # 常规 delete:①CAS suspended→deleting 后会【二次】扣 slot(used_vcpu -= vcpu),而该 slot
    # 早已释放 → 账本扣穿/低估 → 过度调度;②stop-vm/rm 对已不存在的 VM 是无效副作用。故走
    # 【确认回收】专路。落实 ADR "suspended 删除必经确认回收"。
    # suspend/restore launch 竞争),此刻删除会与那些操作抢状态、可能留孤儿 VM(delete 先翻
    # deleted、restore 又起 VM)→ 返 409 让调用方等在途操作收敛(达稳定 suspended 或回 running)。
    if item.get("status") in ("suspending", "restoring"):
        return utils._resp(
            409,
            {
                "error": f"tenant is {item['status']} (hibernate/restore in flight); "
                "wait for it to settle (suspended or running) before delete",
                "id": tenant_id,
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

    if item.get("status") == "suspended":
        _keep = query_params.get("keep_data", "true").lower() == "true"
        _bk = item.get("restore_backup_key", "")
        # 抢闸:CAS 抢下唯一赢家,才做删 S3 备份/撤 vkey(否则并发 restore 先赢 suspended→
        # restoring、本删除删掉其唯一备份 = 数据丢失)。**直接翻终态 deleted 而非 deleting**:
        # suspended 租户无 VM/盘的破坏性副作用序列(suspend 时已删),不需要 deleting 保护窗口;
        # 若经 deleting 且中途崩溃,consumer 重投会走【普通 delete 路径】对已释放的 slot 二次扣账
        # 直接幂等返回,永不进普通删除路径。破坏性清理(S3/vkey)放 CAS 之后 best-effort——崩溃则
        # 泄漏可被对账清理(可恢复),而二次扣 slot 破坏账本正确性(不可接受),两害取轻。
        _vkey = item.get("litellm_vkey")
        _remove = (
            "restore_backup_key, suspended_at, suspended_from, "
            f"cognito_channel_password, {_STALE_HEALTH_FIELDS}"
        )
        _set = "SET #s = :d, updated_at = :t"
        _vals = {":d": "deleted", ":cur": "suspended", ":t": utils._now()}
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
            return utils._resp(
                409,
                {"error": f"tenant is now {cur2.get('status')}; suspended-delete lost the race", "id": tenant_id},
            )
        # CAS 赢(已 deleted 终态),破坏性清理 best-effort(崩溃泄漏可对账,不影响 slot 账本)。
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
        audit._publish_event(
            "tenant.deleted", tenant_id,
            {"from_suspended": True, "kept_backup": _keep, "vkey_revoked": _vkey_revoked},
        )
        return utils._resp(200, {"id": tenant_id, "status": "deleted"})

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
        }
        try:
            enqueued_op_id = lifecycle_dispatch.enqueue_lifecycle(
                "delete",
                tenant_id,
                event,
                extra=_extra,
                operation_id=lifecycle_op_id,
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
                {
                    "id": tenant_id,
                    "status": "queued",
                    "op_id": enqueued_op_id,
                },
            )

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
                "delete_claim_expires_at_epoch = :claim_exp"
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
                ":t": utils._now(),
                **delete_condition_values,
            },
            ReturnValues="ALL_NEW",
        )
        if isinstance(_cas_resp, dict) and _cas_resp.get("Attributes"):
            # 合并会把并发 promote 刚 REMOVE 掉的 capacity_reservation_id 从旧 item 里"复活",
            # 令 delete 误走令牌释放(而该租户已 running、令牌已清),token 释放条件失败 →
            # 走不到旧的按 item.vcpu 扣减 → 账本永久泄漏。ALL_NEW 是 CAS 之后的真值,直接用。
            fresh = _cas_resp["Attributes"]
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        # CAS 撞 deleting/deleted。区分两种:
        # ① status 已 deleted → 真幂等,删完了,返 200。
        #    队列重投(_consumer_ident,FIFO MessageGroupId=tenant_id 保证同租户串行消费,
        #    不存在并发双删),应**继续重试剩余副作用**(stop-vm/rm 幂等),而不是返 200 把
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
            return utils._resp(200, {"id": tenant_id, "status": "deleting"})
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
                return utils._resp(202, {"id": tenant_id, "status": "deleting"})
            print(
                f"delete_tenant #425: {tenant_id} claimed retryable/expired delete"
            )
        # consumer 重投卡在 deleting 的删除:status 已是 deleting,不再翻转,直接往下
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

    # but BEFORE doing any destructive side effect (backup failed → we bail to
    # tenant isn't stranded in "deleting" (invisible to list, un-actionable). No
    # capacity counter was touched yet at this point, so status is all we restore.
    def _abort_restore_status():
        try:
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                # 该标记只对"本次删除尝试"有效;回滚 = 放弃本次删除,租户重新可用、之后可能写【新】
                # 数据。若把标记留在活跃租户上,下一次 delete 会凭陈旧标记跳过 backup → rm 掉新写的
                UpdateExpression=(
                    "SET #s = :prev, updated_at = :t "
                    "REMOVE predelete_backup_at, delete_retryable, "
                    "delete_prev_status, delete_claim_expires_at_epoch"
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

    # 要同时关住的两个方向:
    #  · 首次 delete 干净翻 running→deleting 后,若后续步骤遇瞬时错误留 deleting,consumer
    #    replay 会再进来:此时 rm 数据盘可能已跑、盘已没,再跑 pre-delete backup 必失败
    #    (backup 打已删目录)→ 若 fail-closed 就永远到不了令牌释放 → 容量永久搁浅。
    #  · 反向也【绝不能】仅凭"状态是 deleting"就跳过 backup:CAS 翻 deleting 先于 backup,
    #    一个在 backup【之前】崩溃的首次 delete 也留 deleting、盘仍在且从未备份——盲跳过
    # 原判据是"marker 存在 ⇒ 跳过备份"。原来 marker 写在 stop-vm 成功【之后】,故它
    # 蕴含"VM 已停 ⇒ 不可能有晚于备份的新写入",跳过是安全的。原子化把 stop 与 rm -rf
    # 合进同一条 SSM,那个中间点没了(见下方 marker 段的说明),marker 只能提前到破坏性
    # 动作【之前】写 —— 此时 VM 仍在跑。于是出现一个真实的丢数据窗口:
    #   marker 落库 → VM 继续接受并 ack 新写入 → Lambda 崩溃 → 重投凭 marker 跳过备份
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
        _ok, _err = _force_backup_sync(tenant_id)
        # 哨兵由 backup-data.sh 在 `[ ! -f data.ext4 ]` 分支打出,经 backup Lambda 的
        # result["error"] = <SSM 输出> 原样透传上来。用固定串而非 grep "not found":
        # 后者太脆(路径本身、其它步骤的报错都可能含该词),误判会跳过一次【本该做】的
        # 备份然后删盘。
        _source_absent = bool(_err) and "OC_BACKUP_SOURCE_ABSENT" in _err
        if not _ok and not _source_absent:
            _abort_restore_status()
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
        #      而且租户从 deleting 变回活跃后,容量令牌与账本也不再收敛。
        #   故这条路径直接跳过 marker;它本来就只差"把剩余收尾做完 + 释放令牌"。
        if not _source_absent:
            marker_ts = utils._now()
            try:
                clients.tenants_table.update_item(
                    Key={"id": tenant_id},
                    UpdateExpression="SET predelete_backup_at = :t",
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
        # 不用 CAS 前的陈旧 item:reserve/delete 竞态下 host_id 从 fresh 拿了、vm_num 却用陈旧值,
        # 会 stop/摘 DNAT 到【别的租户】的 tap-vm<n>(no-cross-tenant 违规)。三者同源一致。
        #   rc≠0 → 不推进 deleted。VM 是否已停无法从单个 rc 区分,故统一用
        #   _mark_delete_retryable()【留 deleting】而不是 _abort_restore_status() 回 running:
        #   回 running 的前提是"VM 确实还在跑",而原子脚本失败时 VM 可能已停(①成功②失败),
        #   此时回 running 会谎报已停租户存活;留 deleting 则重投幂等补做剩余步骤(每步都
        #   对已完成无害)。保守取舍:宁可多一个可重投的中间态,不要一个谎报的活跃态。
        vm_num = int(fresh.get("vm_num", 1))
        _hp = int(fresh.get("host_port", 0) or 0)
        _gip = fresh.get("guest_ip", "")
        _legacy_hp = clients.VM_PORT_BASE + vm_num - 1
        #   delete_host_guard。合并后从"4 条 × 前后 2 次 = 8 次 guard"降到 2 次,但保护
        #   的窗口不变——前置 guard 确认本次操作仍持租约(否则 exit 79 被抢占 / exit 78
        #   读不到 fence 时 fail-closed),后置 guard 确认整个破坏性序列期间没被抢占。
        #   guard 里含 aws dynamodb get-item,是 shell 片段而非独立 SSM,不增加
        #   SendCommand 次数,故不抵消本改动的限流收益。
        # ── 部署顺序无关的自愈式装载(codex 独立复审)─────────────────────────────
        # 问题:控制面一上线就【无条件】调 /home/ubuntu/delete-vm.sh,而这是本次【新增】
        # 的脚本。init-host.sh 的硬失败拉取只覆盖【新起】的 host —— 已经在跑的 host 不会
        # 重跑 init,于是"Lambda 先部署、host 脚本还没同步"这个窗口里,每次删除都 exit 127
        # (或撞上旧版 stop-vm.sh 不认 OC_LIFECYCLE_LOCK_FD 而 15s 后锁超时 FATAL)。
        # 光在文档里写"必须成对部署"不算保护 —— 那是把正确性寄托在人工步骤上。
        #
        # 解法:命令串前置一段自愈 —— 两个脚本【任一】缺失或过期就从 S3 的 deployment/
        # scripts/ 重新拉,与 init-host.sh 同一来源、同一路径。host 本来就有该桶读权限
        # (init-host.sh 就是这么装的),故不需要新 IAM。
        # · 判据不只看"文件存在",还看 stop-vm.sh 是否认得 OC_LIFECYCLE_LOCK_FD ——
        #   旧版存在但不认,那正是会 15s 锁超时的情况,必须一起换掉。
        # · 拉取失败即 `exit 1`,让整条命令非零 → 控制面走 _mark_delete_retryable()
        #   留 deleting 重投,绝不在"脚本可能过期"的状态下动手删盘。
        # #520 C2:这段实现搬到 core.ssm_dispatch.host_script_self_heal,因为 suspend /
        # restore / reset / rebuild 有同一个病(那几条路径此前完全没有兜底),重复四份
        # 会各自漂移。行为不变:同样两个脚本成对装、同样以 stop-vm.sh 认不认
        # OC_LIFECYCLE_LOCK_FD 作"存在但过期"的判据、同样任何一步失败即 exit 1。
        # 其余理由(为什么桶名由 host 自己从 /etc/platform.env 读、为什么不许 `|| true`)
        # 都写在那个函数的 docstring 里。
        _self_heal = ssm_dispatch.host_script_self_heal(
            ("delete-vm.sh", "stop-vm.sh"),
            "oc:delete",
            freshness=("stop-vm.sh", "OC_LIFECYCLE_LOCK_FD"),
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
        if not ssm_dispatch._ssm_run(host_id, _del_cmd, timeout=300):
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
        #  · stop-vm.sh                          → delete-vm.sh ①
        #  · rm -f <vmdir>/vm.json               → delete-vm.sh ②(仍在 stop 成功之后,
        #    顺序不变:先删 vm.json 再 stop 会让 host-agent 不 recover 而 VM 仍跑 = 孤儿)
        #  · route_ops.py delete-route           → delete-vm.sh ③
        #    原样保留在脚本内;keep_data=true 不写 tombstone,GC 绝不碰其盘)
        # 每步的 fail-loud 与幂等语义在脚本里逐条保留;控制面这里只判一个总 rc。

        # Update host counters.
        # host_id 是【一个事务】原子落库的(_reserve_batch_txn),释放也必须【令牌化】——
        # 扣 host + 删令牌一个事务、条件 capacity_reservation_id=:rid,与 reaper/poller/批回滚
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
                # deleted:deleted 租户不被 reaper 兜底 → 令牌永久搁浅、容量永漏。【留 deleting】
                # (不 _abort_restore_status 回 running——VM 已停、盘已删,回 running 会谎报已毁
                # 租户存活)返 502,队列消费者/调用方重投;重投时仍 deleting → CCF 分支放行重试
                # 副作用(stop/rm 幂等)+ 重跑本释放(令牌仍在 → 幂等消费一次,已消费则 already)。
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

    # cognito_channel_password 字段,由下面的 UpdateExpression 幂等 REMOVE 清除。

    # hold the concurrency gate (status is "deleting", set by our CAS above), so
    # this is an unconditional finalize; no second race is possible.
    # per-tenant vkey but revoke failed (e.g. LiteLLM transient outage), keep the
    # field and flag it so a reconciler can retry — dropping it here would orphan
    # a live key in LiteLLM (credential + budget leak with no way to find it).
    if item.get("litellm_vkey") and not vkey_revoked:
        update_expr = (
            "SET #s = :s, updated_at = :t, vkey_revoke_failed = :vf "
            "REMOVE cognito_channel_password, delete_retryable, "
            f"delete_prev_status, delete_claim_expires_at_epoch, {_STALE_HEALTH_FIELDS}"
        )
        expr_vals = {":s": "deleted", ":t": utils._now(), ":vf": True}
    else:
        update_expr = (
            "SET #s = :s, updated_at = :t "
            "REMOVE litellm_vkey, cognito_channel_password, "
            "delete_retryable, delete_prev_status, "
            f"delete_claim_expires_at_epoch, {_STALE_HEALTH_FIELDS}"
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


def _cas_status(tenant_id, from_status, to_status):
    """#422 — CAS 翻租户 status(并发闸)。仅当当前 status == from_status 时翻到
    to_status,返回 True;CCF(已被别的 op 改)返回 False。suspend/restore 用它抢唯一
    赢家(抄 delete :2050 的 CAS 形态,但提成小工具供 suspend+restore 复用,避免各写一遍
    闭包)。status 是 DDB 保留字,必须用 ExpressionAttributeNames 占位。"""
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :to, updated_at = :t",
            ConditionExpression="#s = :from",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":to": to_status,
                ":from": from_status,
                ":t": utils._now(),
            },
        )
        return True
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _tenant_suspend(tenant_id, item):
    """#422 — 休眠一个 running/stopped 租户:无条件同步备份数据盘到 S3(fail-closed)→
    停 VM → 释放 host slot → 保留 DDB 记录与 tenant_id,翻 status=suspended,写
    restore_backup_key 供 restore 冷恢复。释放 slot 供新用户调度(休眠的核心目的)。

    fail-closed 语义(no-data-loss 铁律,对齐 delete keep_data=false 的删前备份门):
    备份未确认成功 → 回滚状态、不停 VM、不释放 slot、不删盘、502。只有备份成功才推进破坏性
    步骤。stop-vm 失败 → 回滚 + 502(VM 未停不能标 suspended,否则账本失真)。
    """
    prev_status = item.get("status")
    if prev_status not in ("running", "stopped"):
        return utils._resp(
            409,
            {
                "error": f"can only suspend a running/stopped tenant (current: {prev_status})",
                "id": tenant_id,
            },
        )
    host_id = item.get("host_id")
    if not host_id:
        return utils._resp(
            400, {"error": "tenant has no host (still pending?)", "id": tenant_id}
        )

    # 并发闸:CAS prev → suspending,单赢家。输家(并发 suspend/其他 op 已改)幂等/409。
    if not _cas_status(tenant_id, prev_status, "suspending"):
        cur = (
            clients.tenants_table.get_item(
                Key={"id": tenant_id}, ConsistentRead=True
            ).get("Item")
            or {}
        )
        cur_s = cur.get("status")
        if cur_s in ("suspending", "suspended"):
            return utils._resp(200, {"id": tenant_id, "status": cur_s})
        return utils._resp(
            409,
            {"error": f"tenant is {cur_s}; suspend lost the race", "id": tenant_id},
        )

    def _rollback():
        # 回滚 suspending → prev(抄 _abort_restore_status 的 CAS 形态,只回滚自己翻的态)。
        _cas_status(tenant_id, "suspending", prev_status)

    # 1) 无条件同步备份(复用 delete 备份门的 invoke+双层解析形态,去掉 keep_data 触发条件)。
    try:
        lambda_client = boto3.client("lambda")
        resp = lambda_client.invoke(
            FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
            InvocationType="RequestResponse",  # SYNC:数据落 S3 才继续破坏性步骤
            # pre_delete=True:绕过 backup "只备 running" 守卫(CAS 已把 status 翻成
            # suspending,与 delete 翻 deleting 同源;不带这个信号 backup 会 no-op 拒掉)。
            Payload=json.dumps(
                {"tenant_id": tenant_id, "pre_delete": True}
            ).encode("utf-8"),
        )
        invoke_ok = resp.get("StatusCode", 500) == 200 and "FunctionError" not in resp
        payload_ok = False
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
            _rollback()
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
        _rollback()
        return utils._resp(
            502,
            {"error": f"suspend backup error ({e}); aborting.", "id": tenant_id},
        )

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
        _rollback()
        return utils._resp(
            502,
            {
                "error": "suspend aborted: backup reported success but returned no "
                "backup_key for this run; refusing to stop/delete the VM without a "
                "fresh restorable backup (avoid data loss). Retry.",
                "id": tenant_id,
            },
        )

    # #520 C2:前置 S3 自愈 —— 既有 host 上的 stop-vm.sh 可能缺失或是不认
    # OC_LIFECYCLE_LOCK_FD 的旧版(后者会 15s 锁超时)。这里的失败路径与 stop-vm 本身
    # 失败一致(回滚 + 502 + 不释放 slot),所以自愈失败 exit 1 即可,语义不变。
    vm_num = int(item.get("vm_num", 1))
    _suspend_heal = ssm_dispatch.host_script_self_heal(
        ("stop-vm.sh",), "oc:suspend", freshness=("stop-vm.sh", "OC_LIFECYCLE_LOCK_FD")
    )
    if not ssm_dispatch._ssm_run(
        host_id,
        f"{_suspend_heal} && /home/ubuntu/stop-vm.sh {tenant_id} {vm_num}",
    ):
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
    # 没回收,且 restore 若回同一 host 会撞残留 data.ext4 绕过 S3 恢复。此刻数据已确认在 S3
    # 回滚状态待重投(status 仍 suspending,VM 已停,重投补删幂等),不推进 suspended。
    # tenant_id 经 shlex.quote 进 root shell 防注入(纵深:虽已过 registry 正则)。
    _q_vmd = shlex.quote(f"/data/firecracker-vms/{tenant_id}")
    if not ssm_dispatch._ssm_run(host_id, f"rm -rf {_q_vmd}"):
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

    # 4) 释放 host slot(归还容量记账,供新用户调度——休眠的核心目的)。
    # _release_slot 内部带 >= guard 防负、best-effort 不抛;next_vm_num 不回退(restore 重分配)。
    scheduling._release_slot(
        host_id,
        int(item.get("vcpu", 0)),
        int(item.get("mem_mb", 0)),
        item.get("phys_vm_num", vm_num),
        tenant_id,
    )

    # 4) 终态:写 restore_backup_key + suspended_at,翻 suspended。此刻 status 仍是我们翻的
    # suspending(单赢家持有),用 CAS 收尾防中途被改。backup_key 在上方停 VM/删盘之前已
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET #s = :suspended, restore_backup_key = :bk, suspended_at = :t, "
                "suspended_from = :prev, updated_at = :t"
            ),
            ConditionExpression="#s = :suspending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":suspended": "suspended",
                ":suspending": "suspending",
                ":bk": backup_key,
                ":prev": prev_status,
                ":t": utils._now(),
            },
        )
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        # 中途被别的 op 改(极罕见:suspending 是单赢家态)。VM 已停、slot 已释放、备份在 S3,
        # 数据安全;状态非预期则 fail-loud 让运维查,不谎报成功。
        return utils._resp(
            409,
            {
                "error": "suspend finalize lost CAS (status changed mid-suspend); "
                "VM stopped and backup safe in S3 — inspect tenant state.",
                "id": tenant_id,
            },
        )
    audit._publish_event(
        "tenant.suspended", tenant_id, {"backup_key": backup_key, "from": prev_status}
    )
    return utils._resp(200, {"id": tenant_id, "status": "suspended"})


def _reserve_slot_on(host, vcpu, mem_mb, tenant_id):
    """#422 — 在 host 上原子认领 vm_num + 容量(CAS)。返回认领的 vm_num,或 None(容量不足/
    输 CAS 竞争)。与 create 路径的内层 `_reserve_slot`(:1642)、migrate 的
    `_reserve_migration_slot`(:2408)同款 CAS,为 restore 路径抽出模块级版本(本仓既有模式:
    同款 CAS 按路径各持一份,避免跨函数闭包依赖)。next_vm_num 单调只增。"""
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
    # CAS 后都必须把 caller 持有的 host 字典推进到最新 next_vm_num。否则成功认领已
    # 推进 DDB、本地 expected 却停在旧值,下一轮必然 CCF；而 _release_slot 刻意不回退
    try:
        r = clients.hosts_table.update_item(
            Key={"instance_id": host["instance_id"]},
            UpdateExpression=(
                "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
                "vm_count = vm_count + :one, next_vm_num = :next_after, #ps = :tid, "
                "#s = :a REMOVE idle_since"
            ),
            ConditionExpression=(
                "next_vm_num = :expected AND used_vcpu <= :cap_v "
                "AND used_mem_mb <= :cap_m AND attribute_not_exists(#ps)"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#ps": scheduling.phys_slot_attr(target),
            },
            ExpressionAttributeValues={
                ":v": vcpu, ":m": mem_mb, ":one": 1, ":a": "active",
                ":tid": tenant_id, ":expected": expected,
                ":next_after": target + 1, ":cap_v": cap_v, ":cap_m": cap_m,
            },
            ReturnValues="UPDATED_NEW",
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
        try:
            next_after = int(r["Attributes"]["next_vm_num"])
            host["next_vm_num"] = next_after
            return next_after - 1
        except (KeyError, TypeError, ValueError):
            host["next_vm_num"] = target + 1
            return target
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            host.update(_fresh_host_state(e.response))
            return None
        raise


def _restore_reserve_slot(vcpu, mem_mb, tenant_id):
    """#422 — restore 冷恢复重取 host slot(全新 launch,与 create 同款:找 host → CAS 认领
    vm_num → 物理 tap 撞号复检)。返回 (host, vm_num) 或 (None, resp) 失败响应。
    冷恢复的 tap 绑新 vm_num(launch-vm.sh:661 TAP=tap-vm{VM_NUM}),故 phys_vm_num 重分配为
    新 vm_num,不沿用原值(原值那台 host 的 slot 休眠时已释放,物理 tap 也已回收)。"""
    host = scheduling._find_host(vcpu, mem_mb)
    if not host:
        return None, utils._resp(503, {"error": "no host capacity for restore", "id": tenant_id})
    vm_num = None
    for attempt in range(8):
        claimed = _reserve_slot_on(host, vcpu, mem_mb, tenant_id)
        if claimed is not None:
            vm_num = claimed
            break
        time.sleep(0.05 * (attempt + 1))
        host = scheduling._find_host(vcpu, mem_mb)
        if not host:
            return None, utils._resp(503, {"error": "no host capacity (contended)", "id": tenant_id})
    if vm_num is None:
        return None, utils._resp(503, {"error": "slot allocation contended out", "id": tenant_id})
    # 物理 tap 撞号复检(no-cross-tenant,与 create :1722 同款):占了就丢号认领下一个。
    for _skip in range(64):
        if not _phys_tap_occupied(host["instance_id"], vm_num, exclude_id=tenant_id):
            break
        scheduling._release_slot(
            host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
        )
        claimed = _reserve_slot_on(host, vcpu, mem_mb, tenant_id)
        if claimed is None:
            return None, utils._resp(503, {"error": "no free vm slot (phys tap contended)", "id": tenant_id})
        vm_num = claimed
    else:
        scheduling._release_slot(
            host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
        )
        return None, utils._resp(503, {"error": "unable to find free physical vm slot", "id": tenant_id})
    return host, vm_num


def _tenant_restore(tenant_id, item):
    """#422 — 恢复一个 suspended 租户到【同一 tenant_id】:从 S3 冷恢复数据盘、重取
    host+vm_num+slot、挂回原记录,status suspended→restoring→running。冷恢复(会话/上下文
    不续,只还原数据盘);tenant_id 不变(会议硬要求,维护生命周期链路)。

    fail-closed:launch(带 RESTORE_KEY)失败 → 回滚 restoring→suspended、释放刚取的 slot、
    不删 S3 备份、502。只有 launch 成功才翻 running。restore_backup_key 从 suspend 时写入的
    租户记录读(_tenant_suspend 落库)。"""
    if item.get("status") != "suspended":
        return utils._resp(
            409,
            {
                "error": f"can only restore a suspended tenant (current: {item.get('status')})",
                "id": tenant_id,
            },
        )
    backup_key = item.get("restore_backup_key") or _resolve_backup(tenant_id) or ""
    if not backup_key:
        return utils._resp(
            409,
            {
                "error": "no backup found for this tenant; cannot restore (data may be "
                "unrecoverable — do NOT create a blank tenant).",
                "id": tenant_id,
            },
        )

    # 并发闸:CAS suspended → restoring,单赢家(与并发 restore/delete 互斥)。
    if not _cas_status(tenant_id, "suspended", "restoring"):
        cur = (
            clients.tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get("Item")
            or {}
        )
        cur_s = cur.get("status")
        if cur_s in ("restoring", "running"):
            return utils._resp(200, {"id": tenant_id, "status": cur_s})
        return utils._resp(
            409, {"error": f"tenant is {cur_s}; restore lost the race", "id": tenant_id}
        )

    vcpu = int(item.get("vcpu", 0))
    mem_mb = int(item.get("mem_mb", 0))
    host, vm_num_or_resp = _restore_reserve_slot(vcpu, mem_mb, tenant_id)
    if host is None:
        _cas_status(tenant_id, "restoring", "suspended")  # 回滚,slot 没取到无需释放
        return vm_num_or_resp
    vm_num = vm_num_or_resp
    guest_ip = auth._guest_ip(vm_num)
    host_port = clients.VM_PORT_BASE + vm_num - 1

    # 冷恢复 launch(带 RESTORE_KEY,launch-vm.sh 从 S3 下载/解密/解压/e2fsck 还原 data.ext4)。
    # 的 CommandId 就翻 running(那只证明"提交了",VM 可能没起=假成功、VM 缺失、slot 泄漏)。
    # launch-vm.sh 内部已做:RESTORE_KEY 下载/解密/解压 + e2fsck + status 白名单(已含 restoring)
    # + 起 FC + DNAT;rc=0 才 True。失败(SSM 超时/launch-vm rc≠0)→ 回滚 suspended + 释放 slot,
    # 备份完好可重试(no-data-loss)。
    # fire-and-forget 的 CommandId 就翻 running(那只证明"提交了",VM 可能没起=假成功)。
    # launch-vm.sh 内部:RESTORE_KEY 下载/解密/解压 + e2fsck + status 白名单(含 restoring)
    # + 起 FC + DNAT;rc=0 才 ok。
    launched, launch_rc = ssm_dispatch._launch_vm(
        host["instance_id"], tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port,
        config_template=item.get("config_template", ""),
        restore_backup_key=backup_key,
        # 走广播分支 cp 全部共享 skills → 受限租户恢复后越权拿到未授权 skills。与 create :1957 同。
        scoped_skills=skills._resolve_effective_skills(item),
        chat_endpoint_enabled=bool(item.get("chat_endpoint_enabled", False)),
        sync=True,
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
        return utils._resp(
            503,
            {
                "id": tenant_id,
                "status": "restoring",
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
        _restore_heal = ssm_dispatch.host_script_self_heal(
            ("stop-vm.sh",),
            "oc:restore",
            freshness=("stop-vm.sh", "OC_LIFECYCLE_LOCK_FD"),
        )
        ssm_dispatch._ssm_run(
            host["instance_id"],
            f"{_restore_heal} && /home/ubuntu/stop-vm.sh {tenant_id} {vm_num}",
        )
        scheduling._release_slot(
            host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
        )
        _cas_status(tenant_id, "restoring", "suspended")
        return utils._resp(
            502,
            {
                "error": f"restore launch failed (rc={launch_rc}); target VM stopped, "
                "rolled back to suspended, slot released, S3 backup intact. Retry.",
                "id": tenant_id,
            },
        )

    # 挂回原 tenant_id:更新 host/vm_num/phys_vm_num(冷恢复重分配)/guest_ip/host_port,翻 running。
    # CAS 条件 status=restoring(单赢家持有)。清 suspended_at/restore_backup_key/suspended_from。
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET #s = :running, host_id = :h, vm_num = :vn, phys_vm_num = :vn, "
                "guest_ip = :gip, host_port = :hp, updated_at = :t "
                "REMOVE suspended_at, restore_backup_key, suspended_from"
            ),
            ConditionExpression="#s = :restoring",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":restoring": "restoring",
                ":h": host["instance_id"],
                ":vn": vm_num,
                ":gip": guest_ip,
                ":hp": host_port,
                ":t": utils._now(),
            },
        )
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        scheduling._release_slot(
            host["instance_id"], vcpu, mem_mb, vm_num, tenant_id
        )
        return utils._resp(
            409,
            {
                "error": "restore finalize lost CAS (status changed mid-restore); "
                "inspect tenant state.",
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
    # _get_specific_host_with_capacity 的 pinned/clone 路径)已补;漏掉这里等于留了一条
    # 绕过水位保护的路:账本说还有位置,而 target 自报的实测 MemAvailable 已在水位之下,
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
                    "AND used_mem_mb <= :cap_m AND attribute_not_exists(#ps)"
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
        return {"channel": "live", "target_snap": None}
    target_snap, code, msg = image_channel_mod.resolve_pinned_version(
        channel, host_slots,
        repin_body.get("expected_image_snapshot_time"),
        repin_body.get("expected_image_generation"),
    )
    if code:
        return utils._resp(400 if code == "VALIDATION" else 409, {"error": msg, "code": code})
    return {"channel": "canary", "target_snap": target_snap}


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
    ok, err = _force_backup_sync(tenant_id)
    if not ok:
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
    if channel == "live":
        values = {
            ":c": "live",
            ":t": utils._now(),
            **update_kwargs.pop("ExpressionAttributeValues", {}),
        }
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET image_channel = :c, updated_at = :t REMOVE image_snapshot_time",
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
            UpdateExpression="SET image_channel = :c, image_snapshot_time = :s, updated_at = :t",
            ExpressionAttributeValues=values,
            **update_kwargs,
        )
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


def _parse_host_rebuild_result(
    stdout,
    tenant_id,
    op_id,
    attempt_id,
    fence_epoch,
    host_id,
    vm_num,
):
    """Return only an identity-bound host result from rebuild-vm.sh."""
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
        "target_rootfs_dev_inode",
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


def tenant_action(tenant_id, action, body=None, event=None):
    """POST /tenants/{id}/{action} 的入口。

    #456 / ADR §5.1 —— 这层薄包装只负责 client_token 幂等记录的**收尾**:把内层不论从哪个
    return 退出(tenant_action 内部有 36 个 return)的结果统一写成 result。
    为什么用包装而不是在每个 return 前加 finish():36 处逐一插入,漏一处就会让那条 idem
    记录永久停在 IN_PROGRESS —— 该客户带同一 token 的后续请求会被 409 挡死,再也发不出这个
    操作。包装还能兜住异常路径(内层抛异常时落 UNKNOWN 而不是留 IN_PROGRESS)。

    是否登记幂等由内层决定(它解析 body 才知道有没有 client_token),故内层把用到的
    (owner, token) 通过 _idem_ctx 回传给这层。
    """
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
    # backup/…) on ownership. Checked once here so all branches are covered.
    denied = auth._assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied

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
        # (已删/删除中的租户不该能 suspend/restore)。
        "suspend",
        "restore",
    }
    if action in _mutating_actions and item.get("status") in ("deleted", "deleting"):
        return utils._resp(
            409,
            {"error": f"tenant is {item['status']}; cannot {action}", "id": tenant_id},
        )

    # (start/stop/restart/pause/resume/reset/rebuild/migrate/resize/resize-disk)必须拒绝:
    # suspended 租户本地无 VM(已删)、host_id 可能指向已释放的旧 slot,对它 restart/pause 会
    # 走底部通用块无条件覆盖 status(留"无 VM 却 running"或抢占别人 slot 的活 VM=未记账孤儿)。
    # 仅 suspend/restore 两个动作能作用于休眠态(它们自带精确前置校验:suspend 要 running/
    # stopped,restore 要 suspended),故从本闸排除。suspend 对 suspended 幂等、restore 对
    # running 幂等都在各自 helper 内处理。
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
            {
                "id": tenant_id,
                "action": "rebuild",
                "status": "queued",
                "op_id": _lifecycle_op_id,
            },
        )

    # 控制面重构阶段1 — 产端入队:纯 lifecycle 动作(start/stop/restart/pause/resume)
    # 只是经 SSM 下发、无特殊同步返回值,队列开启时入 SQS 由 consumer 受控并发消费
    # (削峰 + 限流阀,治 1000/s 雪崩),立即返 202。resize/backup/migrate/access 等
    # 有同步返回语义的不入队,保持原同步路径。开关关 → 全走同步(向后兼容)。
    # 防重入:consumer 重放时 event 带 _consumer_ident,不再二次入队。
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
                {
                    "id": tenant_id,
                    "action": action,
                    "status": "queued",
                    "op_id": _enq_op_id,
                },
            )
        if _queue_fence_epoch is not None:
            lifecycle_fence.release(tenant_id, _lifecycle_op_id, _queue_fence_epoch)
            if _idem_ctx is not None:
                _idem_ctx["hold_lifecycle_fence"] = False

    _lifecycle_fence_epoch = None
    _lifecycle_host_guard = ""
    if action in _FENCED_LIFECYCLE_ACTIONS:
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
        # Body shape: {"target_host_id": "i-...."}
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
        # Firecracker snapshot 烤死 tap-vm{原始 launch vm_num},restore 后 VM 挂这个
        # tap(migrate-vm.sh:182 从 snapshot 的 vm.json 读 SRC_VM_NUM)。若 target host 上
        # 已有同名物理 tap(另一租户物理占同一号)→ 两 VM 共享一个 tap → 跨租户网络互通。
        #
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
        # overlay 是叠在共享只读 rootfs 之上的,旧 overlay 是针对旧 rootfs 建的 →
        # 若镜像升级后用 restart 想让新 rootfs 生效,只会得到"半新半旧"(未被 overlay
        # COW 覆盖的块才是新的)。**镜像升级必须走 rebuild(丢 overlay + 采用校验),
        # 不要用 restart**。restart 只用于"不换代码的软重启"(保留运行态数据盘 + overlay)。
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
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
            freshness=("stop-vm.sh", "OC_LIFECYCLE_LOCK_FD"),
        )
        full_cmd = (
            f"{_restart_heal} && "
            f"{_lifecycle_host_guard} && {stop_cmd} && sleep 2 && "
            f"{_lifecycle_host_guard} && {launch_cmd}"
        )
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        full_cmd += f" && {_lifecycle_host_guard}"
        if not ssm_dispatch._ssm_run(item["host_id"], full_cmd, timeout=300):
            return utils._resp(
                502,
                {
                    "error": "restart was not confirmed on the host; the same "
                    "operation will be retried",
                    "id": tenant_id,
                },
            )
        new_status = "running"
    elif action == "stop":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        # Remove DNAT rule
        dnat_del = (
            _dnat_remove_all_cmd(host_port, guest_ip)
            if guest_ip and host_port
            else ""
        )
        _stop_heal = ssm_dispatch.host_script_self_heal(
            ("stop-vm.sh",),
            "oc:stop",
            freshness=("stop-vm.sh", "OC_LIFECYCLE_LOCK_FD"),
        )
        full_cmd = f"{_stop_heal} && {stop_cmd}"
        if dnat_del:
            # Keep cleanup best-effort without letting it mask stop-vm failure.
            full_cmd += f" && ({dnat_del} || true)"
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
        if not ssm_dispatch._ssm_run(item["host_id"], full_cmd, timeout=300):
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
            freshness=("stop-vm.sh", "OC_LIFECYCLE_LOCK_FD"),
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
        # #520 C2:同 reset —— rebuild-vm.sh 在既有 host 上可能不存在。客户 apse1 打
        # restorepatch 时正是靠人工 scp 补装到在役 3 台才让 rebuild 可用,那是把正确性
        # 寄托在人工步骤上;这里改成控制面自己兜底。同 reset 成对自愈 stop-vm.sh:
        # rebuild-vm.sh 自己会调它(deploy/userdata/rebuild-vm.sh:284),只装顶层脚本的话
        # 旧 host 上那个依赖仍可能缺失或过期(独立评审指出)。
        _rebuild_heal = ssm_dispatch.host_script_self_heal(
            ("rebuild-vm.sh", "stop-vm.sh", "launch-vm.sh"),
            "oc:rebuild",
            freshness=("stop-vm.sh", "OC_LIFECYCLE_LOCK_FD"),
        )
        full_cmd = f"{_rebuild_heal} && {_lifecycle_host_guard} && {rebuild_cmd}"
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
            timeout=300,
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
        # (备份数据盘到 S3 + 停 VM + 释放 slot,保留 DDB 记录与 tenant_id;恢复走 restore)。
        # 独立同步分支(不落底部通用 update):有 fail-closed 备份+回滚语义,与 backup/migrate 同类。
        return _tenant_suspend(tenant_id, item)
    elif action == "restore":
        # 重取 host+vm_num+slot、挂回原记录。走 create 同款冷恢复链(_resolve_backup +
        # launch-vm RESTORE_KEY),但不新建租户。
        return _tenant_restore(tenant_id, item)
    elif action == "backup":
        # Async invoke Backup Lambda with single tenant
        lambda_client = boto3.client("lambda")
        lambda_client.invoke(
            FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
            InvocationType="Event",  # async, returns immediately
            Payload=json.dumps({"tenant_id": tenant_id}).encode(),
        )
        audit._publish_event("tenant.backup_started", tenant_id, {})
        return utils._resp(
            202, {"id": tenant_id, "action": "backup", "status": "started"}
        )
    elif action == "access":
        # Explicit tenant authorization (P0): owner/admin grants or revokes
        # another Cognito sub access to this tenant. Gated by _assert_owner_or_admin
        # above (only owner/admin can manage grants — least privilege). The grant
        # list lives in the tenant record's `authorized_users` map and the hub
        # consults it for /token + /files + WS. Audited via _audit_write (caller).
        return tenant_access_grant(tenant_id, item, body)
    elif action == "provision":
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
    #   · reset:非升级语义(丢 overlay 回出厂),照旧无条件标 host 当前版本。
    #   · rebuild:是升级采用,只有 relaunch 校验(FC 不在 deleted 旧 inode 上)
    #     通过(_rebuild_verified 为真)才标新版本;校验失败 → 不标,GET /tenants
    #     仍显示旧版本 = 如实反映"这台没升成",而不是谎报新。
    _stamp_rootfs = action == "reset" or (
        action == "rebuild" and locals().get("_rebuild_verified", False)
    )
    # DynamoDB UpdateExpression 只允许一个 REMOVE 子句,拼两个(", REMOVE ... REMOVE ...")
    # 是非法语法 → update 抛错、消息进 DLQ。统一收集 SET/REMOVE 片段,各出一个子句。
    _remove_attrs: list[str] = []
    if _stamp_rootfs:
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
            _stamp_ver = _resolved_ver
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
            _imm_ver = _resolved_ver_i  # 采用事件解析出的真实快照(canary/rebuild)
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

    # 长轮询 x3)坐实约 1/3 的 rebuild:VM 真重启了(FC pid 变),但 _ssm_run 在 300s 内
    # 没拿到 Success 回执(SSM lag / consumer 180s 先超时)→ _rebuild_verified=False →
    # 上面不标 rootfs_version,而 API 仍返 200-running → 客户看不出"没升成",误判卡住。
    # 校验未过就标 rebuild_status(+reason),校验过则标终态。这不改 SSM/consumer 时序
    # 本身(那两条对齐在下方 CDK/consumer 处)。
    #
    # ADR-rebuild-idempotency-sync-contract §5.4 — 三值化:把"没能确认"从"确认失败"里
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
        _final_update["ConditionExpression"] = _cond
        _final_update["ExpressionAttributeValues"].update(_fence_vals)
    clients.tenants_table.update_item(
        **_final_update
    )
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

    #199 fix — 两处桶/后缀 bug 导致备份存在却 resolve 不到(客户 restore/迁移拿不到
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
    resp = clients.s3.list_objects_v2(
        Bucket=bucket, Prefix=f"{prefix}/{src_tenant_id}/"
    )
    # 只认数据对象(.gz / .gz.enc),排除 .key(envelope 数据密钥,非数据本体)。
    objs = [o for o in resp.get("Contents", []) if not o["Key"].endswith(".key")]
    if not objs:
        return ""
    if timestamp:
        base = f"{prefix}/{src_tenant_id}/{timestamp}.gz"
        # 精确匹配 <ts>.gz 或加密态 <ts>.gz.enc(不含 .key)。
        match = [o for o in objs if o["Key"] == base or o["Key"] == f"{base}.enc"]
        return match[0]["Key"] if match else ""
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
