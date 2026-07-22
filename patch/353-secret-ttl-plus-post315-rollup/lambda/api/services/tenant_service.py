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

import core.clients as clients
import core.utils as utils
import core.auth as auth
import core.scheduling as scheduling
import core.vkey as vkey
import core.ssm_dispatch as ssm_dispatch

# #187 转型:core.legacy_alb 全模块下线(数据面两级路由不再用 per-tenant ALB rule/TG)。
import core.skills as skills
import core.audit as audit
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
)


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

    2026.2.26(协议 v3)配对门只看 `roles` 含请求角色 + publicKey 匹配,`tokens`
    可留空对象——比 6.11 简单(6.11 要 tokens[role] 预铸活跃 token)。故这里只放
    deviceId/publicKey(raw base64url,非 PEM)/role/roles/scopes/tokens:{}/时间戳。

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
    # role 固定 operator(2.26 配对门只校 roles 含请求角色;scopes 给 admin 全权,
    # 无人值守部署需 approve/pairing/管理,与 CLI call.ts:272 对齐)。
    paired = {
        device_id: {
            "deviceId": device_id,
            "publicKey": device["public_key"],
            "role": "operator",
            "roles": ["operator"],
            "scopes": scopes,
            "tokens": {},
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


def _phys_tap_occupied(host_id, phys_num, exclude_id=None):
    """#208 — target host 上物理 tap-vm{phys_num} 是否已被别的租户占用?

    "物理占用"= 某租户当前活在 host_id 上、且它 microVM 实际挂的 tap-vm 号 == phys_num。
    物理 tap 号的权威是 **phys_vm_num**(创建时写,迁移不改;host-agent 从 vm.json 回填
    历史/迁移前建的租户)。老租户可能还没回填 phys_vm_num → 回退到 vm_num:对**从未迁移**
    的租户 vm_num == 物理 tap 号,判定正确;迁移过的租户在回填前是残余盲区(见 MR 描述
    "已知残余"),host-agent 一个 tick 内即回填补齐。

    覆盖两个来源(与原 #208 双 scan 同构,只把 key 从 vm_num 换成物理 tap 号):
      ① 已驻留 host_id 的租户(host_id=:h, 非 deleted)
      ② 正在迁入 host_id 的租户(migration_target=:h, status=migrating)——它一旦 restore
         成功就会在本 host 挂 tap-vm{它的 phys_vm_num},必须提前算进占用。
    任一命中即占用。fail-closed:scan 异常时当作"已占"(宁可让调用方重试/换号,不放行撞号)。
    exclude_id:排除租户自身(重入/自迁移场景不算撞自己)。
    """
    try:
        n = int(phys_num)
    except (TypeError, ValueError):
        return True  # 号非法 → fail-closed
    try:
        for expr, extra in (
            ("host_id = :h AND #s <> :d", {":d": "deleted"}),
            ("migration_target = :h AND #s = :mig", {":mig": "migrating"}),
        ):
            vals = {":h": host_id}
            vals.update(extra)
            # scan(FilterExpression) **分页**:每页最多扫 1MB 就返回 + LastEvaluatedKey。
            # 命中的迁入租户可能落在后页,不翻页会漏判 → fail-open 重开这个安全洞。故必须
            # 循环 ExclusiveStartKey 翻完(找到即短路返回 True,不必扫全表)。
            start_key = None
            while True:
                kw = {
                    "FilterExpression": expr,
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": vals,
                    # 物理 tap 号 = phys_vm_num,回退 vm_num(未回填的非迁移租户二者相等)。
                    # ProjectionExpression 只取判定所需字段,少读带宽。
                    "ProjectionExpression": "id, vm_num, phys_vm_num",
                    "ConsistentRead": True,
                }
                if start_key:
                    kw["ExclusiveStartKey"] = start_key
                resp = clients.tenants_table.scan(**kw)
                for it in resp.get("Items", []):
                    if exclude_id and it.get("id") == exclude_id:
                        continue
                    phys = it.get("phys_vm_num", it.get("vm_num"))
                    try:
                        if int(phys) == n:
                            return True
                    except (TypeError, ValueError):
                        continue
                start_key = resp.get("LastEvaluatedKey")
                if not start_key:
                    break
        return False
    except Exception as e:  # noqa: BLE001 — fail-closed,不能因 scan 抖动放行撞号
        print(
            f"_phys_tap_occupied({host_id},{phys_num}) scan failed → fail-closed: {e}"
        )
        return True


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
    # #106 — purchase/order semantics (all additive/optional; see _validate_purchase).
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

    # Issue #9 — quota check (no-op when env vars unset).
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
    client_token = (body.get("client_token") or "").strip()
    # #95 adversarial C-006: .isascii() passes control chars (\n \t \x00), and
    # .strip() only trims the edges, so an embedded control char used to slip
    # through and land in the SSM command / log line (injection / log-poisoning).
    # An idempotency key is a printable token — require ASCII 33-126 (no control
    # chars, no spaces). Length 4-128 (C-003 short / C-005 over-128 rejected too).
    if client_token and not _CLIENT_TOKEN_RE.match(client_token):
        return utils._err(
            400,
            "VALIDATION",
            "client_token must be 4-128 printable ASCII chars (no spaces/control chars)",
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

    # Issue #12 — clone_from is mutually exclusive with restore_from
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
        # issue #80 — can't clone a tenant you don't own (IDOR).
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

    # Issue #10 — validate tags up-front (fail fast before any side effects)
    tags_err = utils._validate_tags(body.get("tags"))
    if tags_err:
        return utils._resp(400, {"error": tags_err})
    tags = body.get("tags") or {}

    # Issue #15 — optional TTL fields
    ttl_fields, ttl_err = utils._parse_ttl(body.get("ttl_hours"), body.get("on_expiry"))
    if ttl_err:
        return utils._resp(400, {"error": ttl_err})

    # Issue #11 — optional `schedule` field; validated then persisted.
    sched, sched_err = utils._parse_schedule(body.get("schedule"))
    if sched_err:
        return utils._resp(400, {"error": sched_err})

    restore_backup_key = ""
    if restore_from:
        src_id = restore_from.get("tenant_id")
        if not src_id:
            return utils._resp(400, {"error": "restore_from.tenant_id required"})
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
            },
        )

    # ── Phase 2: shed-load a 380-create burst onto the FIFO queue ──
    # All CHEAP validation above ran synchronously (so the caller still gets an
    # immediate 400 on a bad request). Now, if create-via-queue is enabled and
    # this is NOT a consumer replay, enqueue the create and return 202 — the
    # consumer drains the queue at its reserved-concurrency rate, so a burst of
    # 380 POST /tenants no longer fans out 380 synchronous SSM calls and trips the
    # SSM single-instance concurrency wall (795: 40 concurrent → 11 TimedOut).
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
        # #309 — upgrading host 一律挡放置(canary 豁免已删,no-cross-tenant 无例外)。
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
        """Atomically claim a vm_num + capacity on host h. Returns the claimed
        vm_num, or None if h no longer has capacity / lost the CAS race."""
        expected = int(h.get("next_vm_num", 1))
        cap_v = int(int(h["total_vcpu"]) * clients.CPU_OVERCOMMIT_RATIO) - vcpu
        cap_m = int(int(h["total_mem_mb"]) * clients.MEM_OVERCOMMIT_RATIO) - mem_mb
        # 普通租户落到 host → 顺手把 host 标 active(有租户即活跃)。
        # #309 — 金丝雀预留槽分支(_is_canary,原不写 #s 以免谎报 upgrading host active)
        # 已随金丝雀移除:upgrading host 现在根本到不了 _reserve_slot(在
        # _get_specific_host_with_capacity 就被挡),故只剩这一条正常路径。
        _set_expr = (
            "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
            "vm_count = vm_count + :one, next_vm_num = next_vm_num + :one, "
            "#s = :a REMOVE idle_since"
        )
        _names = {"#s": "status"}
        _vals = {
            ":v": vcpu,
            ":m": mem_mb,
            ":one": 1,
            ":a": "active",
            ":expected": expected,
            ":cap_v": cap_v,
            ":cap_m": cap_m,
        }
        try:
            _kwargs = dict(
                Key={"instance_id": h["instance_id"]},
                UpdateExpression=_set_expr,
                ConditionExpression=(
                    "next_vm_num = :expected AND used_vcpu <= :cap_v "
                    "AND used_mem_mb <= :cap_m"
                ),
                ExpressionAttributeValues=_vals,
                ReturnValues="UPDATED_NEW",
            )
            if _names:
                _kwargs["ExpressionAttributeNames"] = _names
            r = clients.hosts_table.update_item(**_kwargs)
            # next_vm_num was incremented; the slot we claimed is the pre-increment value.
            return int(r["Attributes"]["next_vm_num"]) - 1
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    vm_num = None
    for attempt in range(8):
        claimed = _reserve_slot(host)
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
        time.sleep(0.05 * (attempt + 1))
        host = scheduling._find_host(vcpu, mem_mb)
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
        scheduling._release_slot(host["instance_id"], vcpu, mem_mb)
        claimed = _reserve_slot(host)
        if claimed is None:
            inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
            return utils._resp(
                503,
                {"error": "no free vm slot on host (phys tap contended); retry"},
            )
        vm_num = claimed
    else:
        # 连续 64 个号都撞物理 tap —— 极端异常(host 上迁入租户密集),fail-closed。
        scheduling._release_slot(host["instance_id"], vcpu, mem_mb)
        inflight_dedup.release_inflight_lock(owner_id, tenant_user_id)
        return utils._resp(
            503,
            {"error": "unable to find a free physical vm slot on host; retry"},
        )

    guest_ip = auth._guest_ip(vm_num)
    host_port = clients.VM_PORT_BASE + vm_num - 1

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
        "rootfs_version": host.get("rootfs_version", ""),
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
    if clone_from:
        item["clone_from"] = clone_from
    # Persist optional TTL fields on the running path too (#48 follow-up).
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
    try:
        clients.tenants_table.put_item(
            Item=item, ConditionExpression="attribute_not_exists(id)"
        )
    except clients.tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        scheduling._release_slot(host["instance_id"], vcpu, mem_mb)
        return utils._err(
            409,
            "CONFLICT",
            f"tenant '{tenant_id}' already exists",
            extra={"id": tenant_id},
        )
    except Exception:
        scheduling._release_slot(host["instance_id"], vcpu, mem_mb)
        raise

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
            scheduling._release_slot(host["instance_id"], vcpu, mem_mb)
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :s, updated_at = :t",
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
            clients.hosts_table.update_item(
                Key={"instance_id": host["instance_id"]},
                UpdateExpression="SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
                ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1},
            )
            clients.tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :s, updated_at = :t",
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
        scheduling._release_slot(host["instance_id"], vcpu, mem_mb)
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :s, updated_at = :t",
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


def delete_tenant(tenant_id, query_params, event=None):
    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return utils._resp(404, {"error": "tenant not found"})
    # issue #80 — IDOR: only the owner (or admin / api-key) may delete.
    denied = auth._assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied
    if item.get("status") == "deleted":
        return utils._resp(200, {"id": tenant_id, "status": "deleted"})

    # #263 — delete 削峰:队列开启且非 consumer 重放时,入队即返 202,由 consumer
    # 受控并发消费(避免短时间批删把单 host SSM CommandWorkersLimit=5 打爆,饿死
    # launch-vm/start/stop;单删同步 backup+4~5 条阻塞 SSM ~15~35s 还会撞 API GW 29s)。
    # keep_data/skip_backup 放进 extra 让 consumer 透传(否则恒软删,盘悄悄没删)。
    # 与 tenant_action 的入队守卫同款:队列没配 → enqueue 返 False,回退下方同步路径
    # (向后兼容);_consumer_ident 存在说明本次已是 consumer 重放,不再二次入队(防
    # 无限入队)。字段 {id,status} 与同步路径一致,status 值 "queued" 与 tenant_action 对齐。
    if clients.LIFECYCLE_QUEUE_URL and not (event or {}).get("_consumer_ident"):
        _extra = {
            "keep_data": query_params.get("keep_data"),
            "skip_backup": query_params.get("skip_backup"),
        }
        if lifecycle_dispatch.enqueue_lifecycle(
            "delete", tenant_id, event, extra=_extra
        ):
            return utils._resp(202, {"id": tenant_id, "status": "queued"})

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
    try:
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :deleting, updated_at = :t",
            ConditionExpression="#s <> :deleted AND #s <> :deleting",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":deleting": "deleting",
                ":deleted": "deleted",
                ":t": utils._now(),
            },
        )
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
        if not is_consumer_replay:
            # 同步路径并发双删的输家:幂等 200,不碰副作用/账本(#107)。
            return utils._resp(200, {"id": tenant_id, "status": "deleting"})
        # consumer 重投卡在 deleting 的删除:status 已是 deleting,不再翻转,直接往下
        # 重试副作用(stop-vm/rm 幂等)。#107 账本回退由下方 `used_vcpu >= :v` guard
        # 防重复扣穿(第一次已扣过则 CCF → skip),不会二次扣账本。
        prev_status = cur.get("status", prev_status)

    keep_data = query_params.get("keep_data", "true").lower() == "true"
    host_id = item.get("host_id")
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
                UpdateExpression="SET #s = :prev, updated_at = :t",
                ConditionExpression="#s = :deleting",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":prev": prev_status,
                    ":deleting": "deleting",
                    ":t": utils._now(),
                },
            )
        except Exception:
            # Best-effort restore; if another op already moved it, leave as-is.
            pass

    if host_id and not keep_data and not skip_backup:
        try:
            lambda_client = boto3.client("lambda")
            resp = lambda_client.invoke(
                FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
                InvocationType="RequestResponse",  # SYNC: data safe in S3 before rm
                # pre_delete=True:让 backup 绕过"只备 running"守卫——delete CAS 已把
                # status 翻成 "deleting"(先于本调用),不带这个信号 backup 会 no-op 拒掉,
                # 删前备份形同虚设、盘照删(CRITICAL 数据丢失,本修复的根因)。
                Payload=json.dumps({"tenant_id": tenant_id, "pre_delete": True}).encode(
                    "utf-8"
                ),
            )
            # 必须解析备份 Lambda 的**业务结果**(Payload.success),不能只看 invoke 层
            # StatusCode——backup 对失败场景是正常 return {"success":False}(非抛异常),
            # RequestResponse 的 StatusCode 仍 200 且无 FunctionError,只看它会误判成功、
            # 越过 fail-closed 继续 rm -rf 而 S3 无备份(CRITICAL 数据丢失,本修复的根因)。
            invoke_ok = (
                resp.get("StatusCode", 500) == 200 and "FunctionError" not in resp
            )
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
                _abort_restore_status()
                return utils._resp(
                    502,
                    {
                        "error": "pre-delete backup failed; aborting destroy to avoid "
                        "irreversible data loss. Retry, or pass ?skip_backup=true to "
                        "delete without a backup.",
                        "backup_error": payload_err,
                    },
                )
        except Exception as e:
            _abort_restore_status()
            return utils._resp(
                502,
                {
                    "error": f"pre-delete backup error ({e}); aborting destroy. Retry, "
                    "or ?skip_backup=true to force.",
                },
            )

    if host_id:
        # Stop VM via SSM.
        # #268 — stop-vm 是关键副作用,不是 best-effort:失败=VM 孤儿(fc 进程还活着
        # 占内存/vCPU)+ 若继续标 deleted 则账本回退但 VM 没停(容量统计失真)。真机实测
        # (新加坡 795,#263 削峰测试):create 的 launch-vm 挤爆单 host SSM
        # CommandWorkersLimit=5,delete 的 stop-vm 排队 >30s → _ssm_run 返 False,旧代码
        # 忽略返回值照常标 deleted → 236 残留目录 + 27 孤儿 fc。这里 fail-loud:stop-vm
        # 失败则回滚 status 到删除前(复用 _abort_restore_status,CAS 保证只回滚自己翻的
        # deleting)+ 返 502。delete 走队列(#263)时 consumer 收 5xx 会重投,重投时租户又
        # 是 running→CAS 重新翻 deleting→重试 stop(幂等:已停的 VM 再停无害)。best-effort
        # 的 iptables/route(下方)失败仍容忍,有 host-agent orphan-reap 兜底。
        vm_num = int(item.get("vm_num", 1))
        if not ssm_dispatch._ssm_run(
            host_id, f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        ):
            _abort_restore_status()
            return utils._resp(
                502,
                {
                    "error": "stop-vm failed (SSM timeout/error); aborting delete to "
                    "avoid a stranded VM + disk leak. Retry — delete via the lifecycle "
                    "queue re-drives automatically.",
                    "id": tenant_id,
                },
            )
        # Remove vm.json so host-agent won't try to recover. Only after stop
        # succeeded — else we'd delete the recovery marker while the VM is still
        # running (worse: host-agent won't recover, VM stays orphaned untracked).
        # #268 — vm.json rm 失败也 fail-loud:留着它 host-agent 会 recover 已"删"的租户
        # (幽灵复活),同样回滚重投。
        if not ssm_dispatch._ssm_run(
            host_id, f"rm -f /data/firecracker-vms/{tenant_id}/vm.json"
        ):
            _abort_restore_status()
            return utils._resp(
                502,
                {
                    "error": "rm vm.json failed (SSM timeout/error); aborting delete so "
                    "host-agent won't recover a half-deleted tenant. Retry.",
                    "id": tenant_id,
                },
            )

    # #187 转型:legacy_alb rule 已删,数据面走两级路由,无需再摘 per-tenant ALB rule。

    if host_id:
        # 销毁租户 → 回收数据面路由(design decision:delete 可移除路由,stop 保留)。
        # DNAT 规则删除的 argv 必须与 host-agent route_ops.dnat_rule_args 加规则时
        # 完全一致(无 `-i <iface>` 前缀)——旧代码带 `-i` 前缀,与 route_ops 无 `-i`
        # 的规则不匹配,`iptables -D` 删不掉,DNAT 规则永久泄漏在 PREROUTING 链里,
        # 累计租户越多链越长(线性匹配退化的真正元凶)。修成对齐 argv 后 delete
        # 真回收端口段槽位,单 host 规则数回落到活跃租户量级。
        _hp = item.get("host_port", 0)
        _gip = item.get("guest_ip", "")
        if _hp and _gip:
            ssm_dispatch._ssm_run(
                host_id,
                f"sudo iptables --wait 3 -t nat -D PREROUTING -p tcp --dport {_hp} "
                f"-j DNAT --to-destination {_gip}:{clients.VM_PORT_BASE} "
                f"2>/dev/null || true",
            )
        # #134 修:delete 显式清 Redis route:{tenant} 键(contract §8 要求,原实现漏了 →
        # edge 仍缓存指向已删 VM 的 host:port,DNAT 已摘 → 502)。控制面无 Redis 客户端,
        # 经 SSM 调 host 上 route_ops.py CLI(host 在 VPC 内、有 ENGINE_REDIS_ENDPOINT)。
        # best-effort:host-agent 的 orphan-reap 也会补删(双保险)。
        ssm_dispatch._ssm_run(
            host_id,
            f"set -a; . /etc/environment 2>/dev/null; . /etc/platform.env 2>/dev/null; set +a; "
            f"python3 /opt/openclaw/route_ops.py del-route {tenant_id} 2>/dev/null || true",
        )

        if not keep_data:
            # #268 — rm -rf 数据盘是关键(no-data-loss:盘泄漏累积撑爆 host 容量)。
            # 到这一步 VM 已停(stop-vm 成功),盘没删则留 deleting 中间态 + 502 fail-loud,
            # 不推进到 deleted(避免"标 deleted 但盘还在"的静默泄漏)。delete 走队列时
            # consumer 收 502 重投,重投时 CAS 撞 deleting → 上方 CCF 分支放行 consumer 重投
            # 继续重试(VM 已停,补删盘幂等);账本回退在本步之后(:2020),此刻未扣,重投
            # 成功那次才扣一次(guard 防负)。
            # #321 — 先写平级 tombstone 再 rm -rf(单条命令原子下发)。tombstone 是"控制面
            # 已判定该数据盘应销毁(keep_data=false)"的 host 侧持久信号:若 rm -rf 被 SSM
            # 中断/超时漏删,tombstone 仍在(放【VM 目录外】的平级路径 .purge-<tid>,不会被
            # rm -rf <tid> 连带删掉,codex 复审:标记在被删目录内会随半删消失),host-agent
            # 的 _gc_orphan_vm_dirs 下轮据此补删。keep_data=true 的软删走不到这里 → 无
            # tombstone → GC 绝不碰其盘(no-data-loss)。
            # 命令安全(codex 复审):① `&&` 非 `;`——tombstone touch 失败就整条失败、走 #268
            # 重投,杜绝"盘没写 tombstone 却已 rm"导致 GC 漏兜底;② tenant_id 经 shlex.quote 进
            # root shell 防注入(纵深防御:tenant_id 虽已过 registry 正则,但可源自 body 的
            # _assigned_tenant_id;正常 tid quote 后原样不变,不影响下游/测试子串匹配)。
            _root = "/data/firecracker-vms"
            _q_tomb = shlex.quote(f"{_root}/.purge-{tenant_id}")
            _q_vmd = shlex.quote(f"{_root}/{tenant_id}")
            if not ssm_dispatch._ssm_run(
                host_id, f"touch {_q_tomb} && rm -rf {_q_vmd}"
            ):
                print(
                    f"delete_tenant #268: rm -rf data disk FAILED for {tenant_id} on "
                    f"host {host_id} (SSM timeout/error) — VM stopped but disk leaked; "
                    f"keeping status=deleting for retry, NOT marking deleted."
                )
                return utils._resp(
                    502,
                    {
                        "error": "data disk rm failed (SSM timeout/error); VM is stopped "
                        "but disk not reclaimed. Kept status=deleting for re-drive.",
                        "id": tenant_id,
                    },
                )

        # Update host counters.
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
    if item.get("litellm_vkey") and not vkey_revoked:
        update_expr = (
            "SET #s = :s, updated_at = :t, vkey_revoke_failed = :vf "
            "REMOVE cognito_channel_password"
        )
        expr_vals = {":s": "deleted", ":t": utils._now(), ":vf": True}
    else:
        update_expr = (
            "SET #s = :s, updated_at = :t REMOVE litellm_vkey, cognito_channel_password"
        )
        expr_vals = {":s": "deleted", ":t": utils._now()}
    clients.tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=expr_vals,
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


def _reserve_migration_slot(target, vcpu, mem_mb, attempts=8):
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
    for attempt in range(attempts):
        expected = int(h.get("next_vm_num", 1))
        cap_v = int(int(h["total_vcpu"]) * clients.CPU_OVERCOMMIT_RATIO) - vcpu
        cap_m = int(int(h["total_mem_mb"]) * clients.MEM_OVERCOMMIT_RATIO) - mem_mb
        try:
            r = clients.hosts_table.update_item(
                Key={"instance_id": instance_id},
                UpdateExpression=(
                    "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
                    "vm_count = vm_count + :one, next_vm_num = next_vm_num + :one"
                ),
                ConditionExpression=(
                    "next_vm_num = :expected AND used_vcpu <= :cap_v "
                    "AND used_mem_mb <= :cap_m"
                ),
                ExpressionAttributeValues={
                    ":v": vcpu,
                    ":m": mem_mb,
                    ":one": 1,
                    ":expected": expected,
                    ":cap_v": cap_v,
                    ":cap_m": cap_m,
                },
                ReturnValues="UPDATED_NEW",
            )
            return int(r["Attributes"]["next_vm_num"]) - 1
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
            h = fresh
    return None


def tenant_action(tenant_id, action, body=None, event=None):
    item = clients.tenants_table.get_item(
        Key={"id": tenant_id}, ConsistentRead=True
    ).get("Item")
    if not item:
        return utils._resp(404, {"error": "tenant not found"})
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
    }
    if action in _mutating_actions and item.get("status") in ("deleted", "deleting"):
        return utils._resp(
            409,
            {"error": f"tenant is {item['status']}; cannot {action}", "id": tenant_id},
        )

    # 控制面重构阶段1 — 产端入队:纯 lifecycle 动作(start/stop/restart/pause/resume)
    # 只是经 SSM 下发、无特殊同步返回值,队列开启时入 SQS 由 consumer 受控并发消费
    # (削峰 + 限流阀,治 1000/s 雪崩),立即返 202。resize/backup/migrate/access 等
    # 有同步返回语义的不入队,保持原同步路径。开关关 → 全走同步(向后兼容)。
    # 防重入:consumer 重放时 event 带 _consumer_ident,不再二次入队。
    _async_actions = {"start", "stop", "restart", "pause", "resume", "reset", "rebuild"}
    if (
        action in _async_actions
        and clients.LIFECYCLE_QUEUE_URL
        and not (event or {}).get("_consumer_ident")
    ):
        if lifecycle_dispatch.enqueue_lifecycle(action, tenant_id, event):
            return utils._resp(
                202,
                {"id": tenant_id, "action": action, "status": "queued"},
            )

    # ── Issue #16: live VM resize (hot-add vCPU) ──
    if action == "resize":
        return tenant_resize(tenant_id, body)

    # ── Issue #22: resize-disk (offline grow of data.ext4) ──
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
        if not ssm_dispatch._ssm_run(
            host_id,
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

        # Capacity check — same allocatable formula as _find_host().
        vcpu = int(item.get("vcpu", 0))
        mem_mb = int(item.get("mem_mb", 0))
        allocatable_vcpu = int(int(target["total_vcpu"]) * clients.CPU_OVERCOMMIT_RATIO)
        free_vcpu = allocatable_vcpu - int(target.get("used_vcpu", 0))
        allocatable_mem = int(
            int(target["total_mem_mb"]) * clients.MEM_OVERCOMMIT_RATIO
        )
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
        target_vm_num = _reserve_migration_slot(target, vcpu, mem_mb)
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

        snap_cmd = ssm_dispatch._ssm_send(
            source_host_id,
            f"/home/ubuntu/migrate-vm.sh snapshot {tenant_id} {vm_num} "
            f"s3://{bucket}/{snap_prefix}",
            timeout=600,  # snapshot + multi-GB disk upload to S3
        )
        if not snap_cmd:
            # #172 — SSM submit 失败,啥都没起。但上面已 CAS 占了 target 的 slot,且这里
            # 在写 status=migrating **之前**就 bail,sweep 只扫 migrating 租户 → 永远看不到
            # 它、_rollback_migration 不会触发 → target host 的 used_vcpu/used_mem_mb/vm_count
            # 永久泄漏(2026-07-01 SSM ThrottlingException 在 380 突发下的失败模式)。必须
            # 在 502 前释放预留,镜像 create 路径的 _release_slot-on-failure(本文件 :901/960)。
            scheduling._release_slot(target_host_id, vcpu, mem_mb)
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
        clients.tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET #s = :s, migration_target = :tgt, "
                "migration_target_vm_num = :tvn, migration_source = :src, "
                "migration_snap_cmd = :scmd, migration_phase = :ph, "
                "migration_started_at = :st, migration_snapshot_uri = :uri, "
                "updated_at = :t"
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
            },
        )

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
            (
                f"sudo iptables --wait 3 -t nat -A PREROUTING "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{clients.VM_PORT_BASE}"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = f"{stop_cmd} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        ssm_dispatch._ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "stop":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        # Remove DNAT rule
        dnat_del = (
            (
                f"sudo iptables --wait 3 -t nat -D PREROUTING "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{clients.VM_PORT_BASE} 2>/dev/null || true"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = stop_cmd
        if dnat_del:
            full_cmd += f" && {dnat_del}"
        ssm_dispatch._ssm_run(item["host_id"], full_cmd)
        new_status = "stopped"
    elif action == "start":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        # #41 — 通过 helper 生成带全部 11 位参数的命令,穿透 chat_endpoint_enabled
        # 到 launch-vm 幂等段;老版本只填 4 位、CHAT_EP_ENABLED 恒空致唤醒漂移。
        launch_cmd = ssm_dispatch._launch_vm_wake_cmd(tenant_id, item)
        dnat_cmd = (
            (
                f"sudo iptables --wait 3 -t nat -A PREROUTING "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{clients.VM_PORT_BASE}"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = launch_cmd
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        ssm_dispatch._ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "reset":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        # Stop, delete overlay (force fresh layer), then launch
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        reset_cmd = f"rm -f /data/firecracker-vms/{tenant_id}/overlay.ext4"
        # #41 — 通过 helper 生成带全部 11 位参数的命令,穿透 chat_endpoint_enabled
        # 到 launch-vm 幂等段;老版本只填 4 位、CHAT_EP_ENABLED 恒空致唤醒漂移。
        launch_cmd = ssm_dispatch._launch_vm_wake_cmd(tenant_id, item)
        dnat_cmd = (
            (
                f"sudo iptables --wait 3 -t nat -A PREROUTING "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{clients.VM_PORT_BASE}"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = f"{stop_cmd} && {reset_cmd} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        ssm_dispatch._ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "rebuild":
        # Phase 4 — in-place UPGRADE this tenant's VM to the host's CURRENT rootfs
        # (the version refresh_rootfs just staged in /data/firecracker-assets).
        # The host's golden image is read-only and the per-VM overlay is the only
        # writable rootfs layer, so dropping the overlay + relaunching boots the VM
        # on the NEW rootfs. The user's data.ext4 (the data disk, separate from the
        # overlay) is PRESERVED — identity/skills/config/channel_secret/vkey live
        # there and survive. This is how a rolling upgrade lands: refresh_rootfs to
        # stage the new image on hosts → rebuild each tenant to adopt it. Same
        # mechanism as reset, but the intent is "upgrade", and we record the new
        # rootfs_version on the tenant so GET /tenants shows the post-upgrade drift.
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        # Drop the overlay so the refreshed read-only rootfs layer takes effect;
        # data.ext4 (user data) is untouched.
        drop_overlay = f"rm -f /data/firecracker-vms/{tenant_id}/overlay.ext4"
        # #41 — 通过 helper 生成带全部 11 位参数的命令,穿透 chat_endpoint_enabled
        # 到 launch-vm 幂等段;老版本只填 4 位、CHAT_EP_ENABLED 恒空致唤醒漂移。
        launch_cmd = ssm_dispatch._launch_vm_wake_cmd(tenant_id, item)
        dnat_cmd = (
            (
                f"sudo iptables --wait 3 -t nat -A PREROUTING "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{clients.VM_PORT_BASE}"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = f"{stop_cmd} && {drop_overlay} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        # #304 — 升级采用校验:relaunch 后确认新 FC 打开的 rootfs FD **不是
        # (deleted)** 旧 inode。refresh_rootfs 用 mv 原子换 rootfs,若旧 FC 没被
        # stop-vm 真杀掉(cmdline 不匹配/race),新旧并存时老进程抱 (deleted) 旧
        # inode → VM 跑旧代码,而下面却把 rootfs_version 标成新的 = 谎报漂移。
        # 扫该租户 fc.sock 进程的 fd,指向 openclaw-rootfs.ext4 (deleted) 就 exit 1
        # → _ssm_run 返回 False → _rebuild_verified=False → 下面不谎报版本。
        _sock = f"/data/firecracker-vms/{tenant_id}/fc.sock"
        verify_cmd = (
            f"_fpid=$(pgrep -f 'api-sock {_sock}' | head -1); "
            "[ -n \"$_fpid\" ] || { echo 'rebuild-verify: no firecracker after relaunch' >&2; exit 1; }; "
            "if ls -l /proc/$_fpid/fd 2>/dev/null | grep -q 'openclaw-rootfs.ext4 (deleted)'; then "
            "echo 'rebuild-verify: FC still on DELETED old rootfs inode — upgrade did NOT take' >&2; exit 1; fi; "
            "echo rebuild-verify-ok"
        )
        full_cmd += f" && {verify_cmd}"
        _rebuild_verified = ssm_dispatch._ssm_run(
            item["host_id"], full_cmd, timeout=300
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
    if _stamp_rootfs:
        host = clients.hosts_table.get_item(
            Key={"instance_id": item["host_id"]}, ConsistentRead=True
        ).get("Item", {})
        update_expr += ", rootfs_version = :rv"
        expr_values[":rv"] = host.get("rootfs_version", "")

    clients.tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=expr_values,
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
    event_name = _action_to_event.get(action, f"tenant.{new_status}")
    audit._publish_event(
        event_name, tenant_id, {"action": action, "status": new_status}
    )
    return utils._resp(200, {"id": tenant_id, "status": new_status})


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
    allocatable = int(int(host["total_vcpu"]) * clients.CPU_OVERCOMMIT_RATIO)
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
