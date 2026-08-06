"""core 层 · auth:身份验证 / RBAC / IDOR 所有权。

handler-split #132 T1.1(auth 域试点)—— 从 handler.py 逐字搬迁,行为零改动。
安全域(RBAC 角色门 / owner_id 越权防护 / JWT RS256 验签),
搬后经 test_rbac + test_audit_idor + test_external_authz 全绿确认「不窜数据」红线。

#187 P5 — Cognito 渠道机器用户(WI-002)在数据面去中枢化转型中随 channel/hub 一起下线,
本文件只保留 Cognito id_token 验签用于 console RBAC(gate 默认关,fail-closed 到 viewer)。

依赖方向:core.auth → core.clients(env 常量)+ core.utils(_resp / _PLATFORM_ID_RE),
不反向 import handler。进程级缓存(_JWKS_CLIENT / _cw)跟随本域,
global 声明留在模块内。
"""

import json
import os

import boto3

from core.clients import (
    COGNITO_USER_POOL_ID,
    COGNITO_CLIENT_ID,
    COGNITO_REGION,
    DEFAULT_NO_JWT_ROLE,
    RBAC_ENABLED,
    API_KEY_OWNER,
    VM_SUBNET_PREFIX,
)
from core.utils import _resp, _PLATFORM_ID_RE


def _guest_ip(vm_num):
    """Guest IP for a vm_num — MUST match launch-vm.sh's /30 addressing.

    launch-vm.sh lays out one /30 point-to-point link per VM across the
    SUBNET_PREFIX/16 supernet (host=.+1, guest=.+2). The old single-octet
    scheme (SUBNET_PREFIX.<vm_num>.2) capped a host at 254 VMs; the /30 layout
    supports 480+ on one big host. This helper is the single source of truth on
    the Lambda side so the tenant record's guest_ip (and thus the DNAT rule +
    nginx/ALB routing) agrees with what the VM actually gets on the host.
        block = (vm_num-1)*4 ; o3 = block//256 ; o4 = block%256
        guest = SUBNET_PREFIX.<o3>.<o4+2>
    """
    block = (int(vm_num) - 1) * 4
    return f"{VM_SUBNET_PREFIX}.{block // 256}.{block % 256 + 2}"


# CloudWatch namespace for the create-latency SLA metric (1-minute fleet goal).
_CW_NAMESPACE = os.environ.get("CW_METRICS_NAMESPACE", "OpenClaw/ControlPlane")
_cw = None


def _emit_create_latency(seconds):
    """Best-effort CloudWatch metric: queue-wait + provision latency per create.

    Lets us VERIFY the "380 creates within 1 minute" SLA from real data instead
    of asserting it — if p50/p99 drift past target we raise consumer concurrency.
    Never raises (metrics must not break provisioning).
    """
    global _cw
    try:
        if _cw is None:
            _cw = boto3.client("cloudwatch")
        _cw.put_metric_data(
            Namespace=_CW_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "TenantCreateLatencySeconds",
                    "Value": float(seconds),
                    "Unit": "Seconds",
                }
            ],
        )
    except Exception as e:  # noqa: BLE001
        print(f"[metrics] create-latency emit failed (non-fatal): {e}")


# Lazily-built, module-cached JWKS client. Cognito rotates signing keys
# rarely; PyJWKClient caches fetched keys in-process, so we pay the JWKS
# HTTP fetch at most once per cold container (and on key rotation).
_JWKS_CLIENT = None


def _get_jwks_client():
    """Return a cached PyJWKClient for the configured Cognito pool, or None.

    None means verification is impossible (no pool id, or PyJWT/cryptography
    unavailable) — callers must then fail safe.
    """
    global _JWKS_CLIENT
    if not COGNITO_USER_POOL_ID or not COGNITO_REGION:
        return None
    if _JWKS_CLIENT is not None:
        return _JWKS_CLIENT
    try:
        import jwt  # PyJWT — bundled into the Lambda asset (requirements.txt)

        jwks_url = (
            f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
            f"{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
        )
        _JWKS_CLIENT = jwt.PyJWKClient(jwks_url)
        return _JWKS_CLIENT
    except Exception:
        # Import error or malformed config → cannot verify → fail safe.
        return None


def _verify_and_decode(token):
    """Verify a Cognito id_token's RS256 signature and return its claims.

    Returns the decoded claims dict on success, or None if the token cannot be
    cryptographically trusted (bad/alg:none signature, expired, wrong issuer,
    or verification is unavailable). Callers MUST treat None as untrusted and
    fall back to least privilege — NEVER read claims from an unverified token.
    """
    client = _get_jwks_client()
    if client is None or not token:
        return None
    try:
        import jwt

        signing_key = client.get_signing_key_from_jwt(token)
        issuer = (
            f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
        )
        # verify_aud is OFF: Cognito id_tokens carry `aud`=app client id, but
        # access_tokens use `client_id` and omit `aud`. We accept either token
        # type for RBAC (the signature + issuer are what establish trust). If a
        # client id is configured we still cross-check it below, best-effort.
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            # leeway tolerates small clock skew between the token minter
            # (Cognito) and this verifier. Without it iat/nbf must be <= our
            # wall clock to the microsecond, so a host a few seconds ahead of
            # Cognito rejects freshly minted tokens as ImmatureSignatureError.
            # 30s matches Cognito's own freshness window and stays well under
            # the 5min access-token TTL, so it never widens the replay surface
            # meaningfully. exp is still enforced (require=[exp]).
            leeway=30,
            options={"verify_aud": False, "require": ["exp", "iss"]},
        )
        # Best-effort audience pinning: reject tokens minted for a DIFFERENT
        # Cognito app client when we know our own client id.
        if COGNITO_CLIENT_ID:
            aud = claims.get("aud") or claims.get("client_id")
            if aud and aud != COGNITO_CLIENT_ID:
                return None
        return claims
    except Exception:
        # Any verification failure (signature, expiry, issuer, alg) → untrusted.
        return None


def _tenant_user_id_from_claims(claims):
    """Extract the external stable user id from VERIFIED Cognito claims.

    Identity chain (task #13/#14): a tenant user federates in via OIDC, so
    Cognito stamps the IdP-native subject onto the id_token. We read it in
    priority order:
      1. `custom:tenant_user_id` — the custom attribute populated by the OIDC
         attribute mapping configured in stack.py (the canonical source).
      2. The `identities` claim Cognito puts on federated id_tokens — its
         `userId` field is the IdP-native subject. Fallback when the custom
         attribute mapping is not (yet) wired on the pool.

    Returns the id as a string, or None for native (non-federated) Cognito
    users (who have a `sub` but no external id). Callers store it only when
    present, so the field is simply absent for native / API-key tenants.
    """
    if not claims:
        return None
    ext_user_id = claims.get("custom:tenant_user_id")
    if not ext_user_id:
        ids = claims.get("identities")
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except (ValueError, TypeError):
                ids = None
        if isinstance(ids, list):
            for ent in ids:
                if isinstance(ent, dict) and ent.get("userId"):
                    ext_user_id = ent["userId"]
                    break
    return str(ext_user_id) if ext_user_id else None


def _platform_id_from_claims(claims):
    """从 VERIFIED Cognito claims 提取外部平台标识 custom:platform_id。

    对齐 _tenant_user_id_from_claims 的模式(pretokengen/handler.py 在签 id_token 前
    注入 custom:platform_id)。原生非联邦用户 pretokengen 标 "native";这里把 "native"
    与空值一视同仁返回 None(原生用户不归属任何外部平台,字段不落库)。

    安全:platform_id 只做「数据关联维度」(按平台筛租户),NEVER 用于授权决策
    (SPEC §2.7:授权仍在 hub 查 owner_id/authorized_users,不信 claim 自称)。

    一致性(review Warning):claim 值也过 _PLATFORM_ID_RE。list_tenants 的
    ?platform_id 筛选参数用同一正则校验,若 claim 含正则外字符(如空格)却落了库,
    该租户会永远筛不回来(筛选先 400)。保证「能落库的一定能筛回」——不匹配视为 None
    不落库(不 400,claim 是身份链产物非用户直接输入,静默忽略比拒登录合理)。
    """
    if not claims:
        return None
    pid = claims.get("custom:platform_id")
    if not pid or pid == "native":
        return None
    pid = str(pid)
    if not _PLATFORM_ID_RE.match(pid):
        return None
    return pid


# ════════════════════════════════════════════════════════════
# RBAC (issue #14)
# ════════════════════════════════════════════════════════════
#
# Cognito User Pool Groups carry the role assignment as a
# `cognito:groups` claim on the id_token. The console attaches the
# token as `Authorization: Bearer …`. As of 1.5.0 we VERIFY the JWT's
# RS256 signature against the pool's JWKS before trusting any claim
# (_verify_and_decode). A token that fails verification — forged,
# expired, alg:none, wrong issuer/audience — is treated as untrusted and
# the caller is downgraded to `viewer` (least privilege).
#
# Fail-safe default: a request with NO Bearer token (API-key-only path)
# resolves to DEFAULT_NO_JWT_ROLE (default `viewer`), NOT admin. This
# closes the pre-1.5.0 fail-open hole where missing/forged tokens granted
# full access. Trusted automation must present a real Cognito id_token.

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

# Endpoints that read state are open to viewers; everything else
# requires operator+ by default. Admin-only endpoints can be added here.
_VIEWER_OK = {
    ("GET", "/tenants"),
    ("GET", "/tenants/{id}"),
    ("GET", "/tenants/{id}/{action}"),
    # Task 7.3 — credentials 子资源与 GET /tenants/{id} 同级(那里今天就折 token/device):
    # viewer 过 RBAC 门,owner==caller/admin 检查在 get_tenant_credentials 内(#80 IDOR 模式)。
    ("GET", "/tenants/{id}/credentials"),
    # self-service node provisioning is viewer-level at the RBAC gate; the
    # create_tenant_self handler then enforces self-only + per-user cap.
    ("POST", "/tenants/self"),
    # PRD #50-58 — per-user fleet reads are viewer-level; the per-user-scope
    # check inside the handler still restricts a non-admin to their own fleet.
    ("GET", "/users/{tenant_user_id}/tenants"),
    ("GET", "/users/{tenant_user_id}/summary"),
    # PRD #54 — reading async batch job progress is viewer-level
    ("GET", "/batch/jobs/{job_id}"),
    ("GET", "/backups"),
    ("GET", "/hosts"),
    ("GET", "/hosts/rootfs-version"),
    ("GET", "/hosts/rootfs-drift"),
    ("GET", "/images"),
    # #337(原#217 /snapshots)— 列镜像版本快照(time+label+count),只读,供 console 选。同 /images 级。
    ("GET", "/list_image_versions"),
    # #394 P2-4 —— 这两个是只读镜像视图,契约(doc/OpenAPI)声明 viewer;不列入则默认 operator,
    # 按公开契约实现的 viewer 客户端会吃 403。pull 进度轮询 + host 盘上真实镜像状态,纯读。
    ("GET", "/hosts/{instance_id}/pull-image-progress"),
    ("GET", "/hosts/{instance_id}/image-slots"),
    ("GET", "/agentcore/status"),
    ("GET", "/agentcore/tools"),
    ("GET", "/system/info"),
    ("GET", "/audit-log"),
    # 1.4.0 (#62) — listing groups is read-only.
    ("GET", "/groups"),
    # 1.4.1 (#63) — read individual SKILL.md content (editor / viewer)
    ("GET", "/skills/{name}"),
    # #187 转型:POST /chat/sign 路由已下线,前端直连 /ws/{tenant_id} gateway。
}


def _role_from_claims(claims):
    """Map a verified claims dict → highest-privilege role, else 'viewer'."""
    groups = (claims or {}).get("cognito:groups", []) or []
    if isinstance(groups, str):
        groups = [groups]
    best = None
    for g in groups:
        if g in _ROLE_RANK and (best is None or _ROLE_RANK[g] > _ROLE_RANK[best]):
            best = g
    return best or "viewer"


def _get_user_role(event):
    """Return the caller's role from a SIGNATURE-VERIFIED Cognito id_token.

    Fail-safe (1.5.0):
      • No Bearer token            → DEFAULT_NO_JWT_ROLE (default: viewer).
      • Token present, unverifiable → viewer (forged/expired/alg:none → denied).
      • Token verified             → role from cognito:groups (admin>operator>viewer).

    This replaces the pre-1.5.0 behavior where a missing token meant `admin`
    (fail-open) and claims were trusted without verifying the signature (any
    attacker could forge `{"cognito:groups":["admin"]}`).
    """
    headers = event.get("headers") or {}
    # API Gateway lower-cases header names but real-world clients vary.
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        # No JWT → API key-only path. Fail safe to the configured default.
        return DEFAULT_NO_JWT_ROLE
    token = auth[len("Bearer ") :].strip()
    claims = _verify_and_decode(token)
    if claims is None:
        # Token present but could not be cryptographically trusted → deny.
        return "viewer"
    return _role_from_claims(claims)


def _role_satisfies(actual, required):
    """True iff `actual` has at least the privilege of `required`."""
    return _ROLE_RANK.get(actual, -1) >= _ROLE_RANK.get(required, 99)


# ════════════════════════════════════════════════════════════
# Identity ownership / IDOR hardening (issue #80)
# ════════════════════════════════════════════════════════════
#
# Pre-#80 every authenticated caller could list/read/mutate ANY tenant by id
# (broken-object-level-authorization). We now stamp the creator's identity into
# `owner_id` at create time and enforce owner==caller on every per-tenant route.
#
# Caller identity is derived from the SAME trust chain as RBAC:
#   • Verified Cognito id_token  → owner_id = `sub` (stable, never reused),
#                                   role from cognito:groups. Admin sees all.
#   • No Bearer token (API key)  → owner_id = API_KEY_OWNER, full access. This
#                                   path is reserved for trusted automation /
#                                   customer scripts that hold the API key, so
#                                   it is treated as admin-equivalent for
#                                   ownership (it never gets locked out and can
#                                   operate on api-key-owned + legacy records).
#   • Token present but unverified → owner_id = None, role viewer (denied).
#
# Records created before #80 (or via the API-key path) have no `owner_id`; to
# avoid leaking them to arbitrary Cognito users they are visible/operable only
# by admins and the API-key caller.


def _get_caller_identity(event):
    """Resolve the caller's owner identity + role from the request.

    Returns a dict:
      {
        "owner_id":     stable owner principal (Cognito `sub`, or API_KEY_OWNER),
                        or None when a Bearer token is present but untrusted,
        "role":         "viewer" | "operator" | "admin",
        "is_admin":     True for admin role OR the API-key path (full access),
        "api_key_only": True when no Bearer token was presented.
      }

    Memoized per-request on the event dict: a single request may resolve
    identity more than once (e.g. owner check + audit-log actor stamping), and
    each miss costs an RS256 verify. Caching on `event` keeps it to one verify
    without touching the JWKS client's own key cache.
    """
    if isinstance(event, dict):
        cached = event.get("_caller_identity_memo")
        if cached is not None:
            return cached

    # 控制面重构阶段1 — SQS consumer 重放:入队时(producer 路径)已用真请求验过
    # 调用者身份并捎带进消息(_consumer_ident),consumer 重放时信任它,不必也无法
    # 再验 Bearer(消息里没有原始 token)。这是受信内部路径(消息只能由本账号的
    # api Lambda 用 SQS 发送权限投递),非外部输入。
    if isinstance(event, dict) and event.get("_consumer_ident"):
        ci = event["_consumer_ident"]
        ident = {
            "owner_id": ci.get("owner_id"),
            "role": "admin" if ci.get("is_admin") else "operator",
            "is_admin": bool(ci.get("is_admin")),
            "api_key_only": ci.get("owner_id") in (None, API_KEY_OWNER),
            # #108 — carry the platform scope forward from the original (already
            # authorized) request so async lifecycle replay stays in-namespace.
            "platform_scope": ci.get("platform_scope"),
            # #143 sibling — carry the federated caller's stable id so a
            # CREATE_VIA_QUEUE replay of create_tenant lands tenant_user_id
            # (create_tenant reads it off this identity). None for native /
            # api-key callers (unchanged). enqueue_lifecycle now stamps it.
            "tenant_user_id": ci.get("tenant_user_id"),
        }
        event["_caller_identity_memo"] = ident
        return ident

    headers = (event.get("headers") if isinstance(event, dict) else None) or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        # API-key-only path: no Cognito sub. Trusted automation → full access.
        ident = {
            "owner_id": API_KEY_OWNER,
            "role": DEFAULT_NO_JWT_ROLE,
            "is_admin": True,
            "api_key_only": True,
        }
    else:
        token = auth[len("Bearer ") :].strip()
        claims = _verify_and_decode(token)
        if claims is None:
            # Token present but untrusted (forged/expired/alg:none) → no identity.
            ident = {
                "owner_id": None,
                "role": "viewer",
                "is_admin": False,
                "api_key_only": False,
            }
        else:
            role = _role_from_claims(claims)
            # Prefer the immutable `sub`; fall back to username only if absent.
            owner_id = (
                claims.get("sub")
                or claims.get("cognito:username")
                or claims.get("username")
            )
            ident = {
                "owner_id": owner_id,
                "role": role,
                "is_admin": role == "admin",
                "api_key_only": False,
                # task #13/#14 — external stable id for OIDC-federated users
                # (None for native Cognito users); used to attribute the tenant
                # back to the external user across the identity chain.
                "tenant_user_id": _tenant_user_id_from_claims(claims),
                # #106 — external-platform id from custom:platform_id claim
                # (None for native / API-key callers). Resolved off the same
                # single verify as tenant_user_id (no extra RS256 verify).
                "platform_id": _platform_id_from_claims(claims),
            }

    # #108 — per-platform scoped API key. A REQUEST Lambda authorizer resolves
    # which platform the presented API key belongs to and injects it at
    # requestContext.authorizer.platform_id (the ONLY trusted source — AWS docs:
    # requestContext.authorizer.* is populated by a CUSTOM authorizer; a caller
    # cannot forge it). When present, this caller is "platform-scoped": it may
    # only touch tenants whose platform_id matches, and is NOT a blanket admin
    # over the whole fleet even if role/api-key would otherwise say so. Absent
    # (legacy god admin-key / Cognito admin) → platform_scope None, behavior
    # unchanged (backward compatible during rollout). The SQS consumer replay
    # path carries it forward via _consumer_ident.platform_scope (handled in the
    # early-return branch above), so here we only resolve the live-request source.
    scope = None
    if isinstance(event, dict):
        authz = ((event.get("requestContext") or {}).get("authorizer")) or {}
        pid = authz.get("platform_id")
        # authorizer context values arrive as strings; empty/"native" = no scope.
        if pid and str(pid) not in ("", "native"):
            scope = str(pid)
    ident["platform_scope"] = scope

    if isinstance(event, dict):
        event["_caller_identity_memo"] = ident
    return ident


def _assert_owner_or_admin(item, event):
    """Return None if the caller may operate on `item`, else a 403 response.

    • Admins and the API-key caller bypass the owner check (full access).
    • Otherwise the caller's owner_id must equal the record's owner_id.
    • Records with no owner_id (legacy / API-key-created) are admin/api-key
      only — a non-admin Cognito user is denied rather than allowed.

    Ownership is enforced by the CALLER'S IDENTITY, decoupled from the
    RBAC_ENABLED flag (#60). RBAC_ENABLED gates *role* checks (viewer/operator/
    admin, in `_rbac_check`) — it must NOT double as an owner-check kill switch.
    Pre-#60 the first line was `if not RBAC_ENABLED: return None`, so flipping
    the global flag off silently turned every per-tenant route into a
    cross-tenant IDOR (a viewer could read/mutate any tenant). Now the check
    keys off identity only: when RBAC is disabled every caller resolves to the
    API_KEY_OWNER admin identity (see `_get_caller_identity` no-Bearer path) and
    is_admin short-circuits below, so the pre-#80 single-tenant semantics still
    hold for free — but a genuine non-admin Cognito user stays owner-scoped no
    matter how the flag is set.
    """
    ident = _get_caller_identity(event)
    # #108 — a platform-scoped API key may ONLY touch tenants inside its own
    # platform namespace, checked BEFORE the is_admin bypass (else a scoped key
    # that also resolves is_admin would reach every platform's tenants — the very
    # god-key IDOR this issue closes). Cross-platform / no-platform tenants → 403.
    scope = ident.get("platform_scope")
    if scope is not None:
        if (item or {}).get("platform_id") != scope:
            return _resp(
                403,
                {"error": "forbidden: tenant outside caller's platform scope"},
            )
        return None
    if ident["is_admin"]:
        return None
    if ident["owner_id"] is None:
        # Untrusted token (forged/expired/alg:none) → no identity → deny.
        return _resp(403, {"error": "forbidden"})
    owner = (item or {}).get("owner_id")
    if owner and owner == ident["owner_id"]:
        return None
    return _resp(403, {"error": "forbidden: not the owner of this tenant"})


# Routes authenticated by their OWN mechanism (not Cognito/RBAC) — skip the role
# gate. /external/authz verifies an HMAC signature inside its handler (go-live A1).
# /external/authz is HMAC-authed inside its handler; /tenantmatch is a pre-login
# read-only IdP-routing lookup (#97 档A) — the browser calls it BEFORE any Cognito
# login to learn which upstream IdP to federate to, so it can't require a JWT. It
# leaks no tenant data (only platform_id→idp_provider_name), and the API-key gate
# at API Gateway still fronts it.
# tenant-credential-contract: registry/recipient-key 管理接口的真正 admin 门在
# handler 内(_get_caller_identity().is_admin:api-key=admin 全权、operator/viewer
# Bearer 被拒)。前置 RBAC 门按 role 判会把 api-key(role=viewer 但 is_admin=True)误拦,
# 故这些端点 skip 前置门,让 handler 内的 is_admin 判定当唯一门。
_RBAC_SKIP = {
    ("POST", "/external/authz"),
    ("GET", "/tenantmatch"),
    ("GET", "/tenants-stats"),
    ("GET", "/registry/{config_template}"),
    ("POST", "/registry/{config_template}"),
    ("POST", "/registry/{config_template}/rollback"),
    ("GET", "/recipient-key"),
    ("POST", "/recipient-key"),
    ("POST", "/recipient-key/disable"),
    # #389 v2 块5 — admin 门在 handler 内(identity-based is_admin)。前置 RBAC 门按 role 判定,
    # 而 api-key 路径 role 解析成 viewer(is_admin=True),不 skip 会在门口被 viewer<operator 挡掉,
    # 持 key 的运维脚本永远进不来。故与 registry/recipient-key 同样列入 skip。
    ("GET", "/bootstrap/versions"),
    ("POST", "/bootstrap/promote"),
}


def _rbac_check(event, method, resource):
    """Return None if allowed, else a 403 response."""
    if (method, resource) in _RBAC_SKIP:
        return None  # HMAC-authed route, gated inside its own handler
    if not RBAC_ENABLED:
        return None  # role-gating disabled — all routes open
    role = _get_user_role(event)
    needed = "viewer" if (method, resource) in _VIEWER_OK else "operator"
    if not _role_satisfies(role, needed):
        return _resp(
            403,
            {
                "error": "forbidden",
                "rbac": {"role": role, "required": needed},
            },
        )
    return None
