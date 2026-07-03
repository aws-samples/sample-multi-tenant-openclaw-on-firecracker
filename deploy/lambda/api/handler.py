# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import base64
import json
import hashlib
import os
import random
import secrets
import re
import shlex
import time
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr, Key

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
asg_client = boto3.client("autoscaling")
sns = boto3.client("sns")
ddb = boto3.resource("dynamodb")

# ── Per-tenant LiteLLM billing (task #15) ───────────────────────────────
# Each tenant gets its OWN LiteLLM virtual key (vkey) so spend/budget/rate
# limits split per tenant↔Cognito-sub instead of one shared key. The vkey is
# minted here (control plane) — the LiteLLM master key lives in Secrets Manager
# and NEVER reaches the host/guest (zero-credential-guest constraint). The vkey
# is stored on the tenant record; launch-vm.sh injects it into the per-VM
# openclaw.json (PoC verified: /key/generate metadata + per-key spend split).
import urllib.request

LITELLM_MASTER_KEY_SECRET = os.environ.get("LITELLM_MASTER_KEY_SECRET", "")
# base_url: env 优先;空则运行时从 SSM /openclaw/litellm-host 读(CDK 自起 LiteLLM
# 时把 EC2 私网 IP:4000 写在这里,synth 时拿不到 IP,故不能靠 env 硬编码)。
LITELLM_HOST_SSM = os.environ.get("LITELLM_HOST_SSM", "/openclaw/litellm-host")
TENANT_DEFAULT_BUDGET = float(os.environ.get("TENANT_DEFAULT_BUDGET", "0") or 0)
TENANT_DEFAULT_RPM = int(os.environ.get("TENANT_DEFAULT_RPM", "0") or 0)
_secrets_client = None
_litellm_master_key_cache = None
_litellm_base_url_cache = None


def _get_litellm_base_url():
    """Resolve the LiteLLM base URL: explicit env wins; otherwise read the
    CDK-self-hosted gateway address from SSM (/openclaw/litellm-host, written at
    EC2 boot with the runtime private IP). Cached. Returns "" if neither set."""
    global _litellm_base_url_cache
    if _litellm_base_url_cache is not None:
        return _litellm_base_url_cache
    env_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    if env_url:
        _litellm_base_url_cache = env_url
        return env_url
    try:
        val = ssm.get_parameter(Name=LITELLM_HOST_SSM)["Parameter"]["Value"].strip()
        _litellm_base_url_cache = val
    except Exception as e:
        print(f"litellm: cannot read base url from SSM {LITELLM_HOST_SSM}: {e}")
        _litellm_base_url_cache = ""
    return _litellm_base_url_cache


def _get_litellm_master_key():
    """Read the LiteLLM master key from Secrets Manager (cached). Returns None
    if billing isn't configured — callers then skip vkey minting (backward
    compatible: tenants fall back to the shared key baked in the image).
    The secret may be a bare string or JSON {"master_key": "..."} (CDK's
    self-hosted LiteLLM stores JSON) — both are handled."""
    global _secrets_client, _litellm_master_key_cache
    if not (LITELLM_MASTER_KEY_SECRET and _get_litellm_base_url()):
        return None
    if _litellm_master_key_cache is not None:
        return _litellm_master_key_cache
    try:
        if _secrets_client is None:
            _secrets_client = boto3.client("secretsmanager")
        raw = _secrets_client.get_secret_value(SecretId=LITELLM_MASTER_KEY_SECRET).get(
            "SecretString", ""
        )
        mk = raw
        if raw and raw.lstrip().startswith("{"):
            try:
                mk = json.loads(raw).get("master_key", "") or ""
            except Exception:
                mk = ""
        _litellm_master_key_cache = mk or None
    except Exception as e:
        print(f"litellm: cannot read master key secret: {e}")
        _litellm_master_key_cache = None
    return _litellm_master_key_cache


def _mint_tenant_vkey(tenant_id, owner_sub):
    """Mint a per-tenant LiteLLM vkey with tenant/sub metadata + budget/rpm.
    Returns the vkey string, or None if billing unconfigured / call fails
    (non-fatal — tenant still launches on the shared key)."""
    mk = _get_litellm_master_key()
    if not mk:
        return None
    payload = {
        "key_alias": f"tenant-{tenant_id}",
        "metadata": {"tenant_id": tenant_id, "cognito_sub": owner_sub or ""},
    }
    if TENANT_DEFAULT_BUDGET > 0:
        payload["max_budget"] = TENANT_DEFAULT_BUDGET
    if TENANT_DEFAULT_RPM > 0:
        payload["rpm_limit"] = TENANT_DEFAULT_RPM
    try:
        req = urllib.request.Request(
            _get_litellm_base_url().rstrip("/") + "/key/generate",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + mk,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("key")
    except Exception as e:
        print(f"litellm: vkey mint failed for {tenant_id}: {e}")
        return None


def _revoke_tenant_vkey(vkey):
    """Go-live C: reclaim a tenant's LiteLLM vkey on delete (POST /key/delete).
    Without this the per-tenant key lingers in LiteLLM after the tenant is gone —
    a credential + budget leak that accumulates over churn. Non-fatal (delete
    proceeds even if revoke fails; we log) and best-effort. Returns True on
    confirmed revoke, False otherwise."""
    if not vkey:
        return False
    mk = _get_litellm_master_key()
    if not mk:
        return False
    try:
        req = urllib.request.Request(
            _get_litellm_base_url().rstrip("/") + "/key/delete",
            data=json.dumps({"keys": [vkey]}).encode(),
            headers={
                "Authorization": "Bearer " + mk,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
            return True
    except Exception as e:
        # don't leak the key value into logs (mask)
        print(f"litellm: vkey revoke failed for [REDACTED vkey]: {e}")
        return False


def _provision_channel_machine_user(tenant_id):
    """WI-002 — provision the per-tenant Cognito machine-user the in-VM channel
    signs in with (USER_PASSWORD_AUTH) to obtain an access token for the hub.

    username == tenant_id (one user per tenant; access token's username claim is
    the unforgeable tenant identity). Returns {region, clientId, username,
    password} on success, or None when the channel Cognito plane is unconfigured
    (COGNITO_CHANNEL_CLIENT_ID empty) OR provisioning fails — in which case the
    caller falls back to the legacy HMAC channel_secret (graceful rollout).

    The password is a per-tenant secret minted here (control plane) and handed to
    launch-vm.sh for cold-injection to the read-only disk — same blast-radius
    class as channel_secret, never baked into the image, never sent to the
    browser. We do NOT log the password.
    """
    if not (COGNITO_CHANNEL_CLIENT_ID and COGNITO_USER_POOL_ID):
        return None
    # 32 url-safe bytes → comfortably satisfies any Cognito password policy.
    password = secrets.token_urlsafe(32)
    try:
        idp = _cognito_idp_client()
        idp.admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=tenant_id,
            MessageAction="SUPPRESS",  # machine user — no welcome email/SMS
        )
        idp.admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=tenant_id,
            Password=password,
            Permanent=True,
        )
        return {
            "region": COGNITO_REGION,
            "clientId": COGNITO_CHANNEL_CLIENT_ID,
            "username": tenant_id,
            "password": password,
        }
    except Exception as e:
        # Non-fatal: tenant still launches on the HMAC path. Don't leak password.
        print(f"cognito: machine-user provision failed for {tenant_id}: {e}")
        return None


def _delete_channel_machine_user(tenant_id):
    """WI-002 — delete the per-tenant Cognito machine-user on tenant delete.
    Mirror of _provision_channel_machine_user. Best-effort + non-fatal (delete
    proceeds even if this fails; we log). No-op when the plane is unconfigured."""
    if not (COGNITO_CHANNEL_CLIENT_ID and COGNITO_USER_POOL_ID):
        return False
    try:
        _cognito_idp_client().admin_delete_user(
            UserPoolId=COGNITO_USER_POOL_ID, Username=tenant_id
        )
        return True
    except _cognito_idp_client().exceptions.UserNotFoundException:
        return True  # already gone — idempotent
    except Exception as e:
        print(f"cognito: machine-user delete failed for {tenant_id}: {e}")
        return False


def _cognito_creds_from_tenant(tenant):
    """WI-002 — rebuild the channel Cognito creds dict from a stored tenant
    record (wake/rebuild path). The password was persisted at create time; the
    rest is derived from current env. Returns None when the plane is unconfigured
    or no password was stored (legacy tenant) → caller uses HMAC channel_secret."""
    if not (COGNITO_CHANNEL_CLIENT_ID and COGNITO_USER_POOL_ID):
        return None
    pw = tenant.get("cognito_channel_password")
    if not pw:
        return None
    return {
        "region": COGNITO_REGION,
        "clientId": COGNITO_CHANNEL_CLIENT_ID,
        "username": tenant["id"],
        "password": pw,
    }


tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])
# PRD #50-58 — control-plane scale-out GSIs on the tenants table (defined in
# deploy/stack.py). gsi_owner partitions by owner_id (Cognito sub) for "my
# nodes"; gsi_tenant_user partitions by tenant_user_id for the external backend's
# per-user fleet management. Names must match the CDK index_name exactly.
GSI_OWNER = "gsi_owner"
GSI_TENANT_USER = "gsi_tenant_user"
# 1.4.0 (#62) — per-tenant / per-group skill scoping. Optional table:
# legacy deployments without GROUPS_TABLE simply skip the group-resolution
# branch in _resolve_effective_skills() and continue with broadcast behavior.
groups_table = (
    ddb.Table(os.environ["GROUPS_TABLE"]) if os.environ.get("GROUPS_TABLE") else None
)
# Issue #17 — optional audit log; absent in legacy deployments
audit_table = (
    ddb.Table(os.environ["AUDIT_TABLE"]) if os.environ.get("AUDIT_TABLE") else None
)
# PRD #54 — optional async batch-job ledger; absent → batch stays synchronous
batch_jobs_table = (
    ddb.Table(os.environ["BATCH_JOBS_TABLE"])
    if os.environ.get("BATCH_JOBS_TABLE")
    else None
)
# #97 档A — optional external-platform → Cognito-IdP map (SPEC/02 §2.7). Absent →
# federation not configured; /tenantmatch returns 404 (front-end falls back to
# passing identity_provider explicitly). Partition key: platform_id (S).
tenant_idp_table = (
    ddb.Table(os.environ["TENANT_IDP_TABLE"])
    if os.environ.get("TENANT_IDP_TABLE")
    else None
)
AUDIT_TTL_DAYS = int(os.environ.get("AUDIT_TTL_DAYS", "90"))

# Issue #13 — optional SNS topic for tenant lifecycle events.
# Empty string disables publishing (no-op).
NOTIFICATIONS_TOPIC_ARN = os.environ.get("NOTIFICATIONS_TOPIC_ARN", "")

# Per-host limits (from config.yml via env)
HOST_RESERVED_VCPU = int(os.environ.get("HOST_RESERVED_VCPU", 1))
HOST_RESERVED_MEM = int(os.environ.get("HOST_RESERVED_MEM", 2048))
CPU_OVERCOMMIT_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", 1.0))
MEM_OVERCOMMIT_RATIO = float(os.environ.get("MEM_OVERCOMMIT_RATIO", 1.0))
VM_DEFAULT_VCPU = int(os.environ.get("VM_DEFAULT_VCPU", 2))
VM_DEFAULT_MEM = int(os.environ.get("VM_DEFAULT_MEM", 4096))
VM_DATA_DISK_MB = int(os.environ.get("VM_DATA_DISK_MB", 2048))
VM_PORT_BASE = int(os.environ.get("VM_PORT_BASE", 18789))
VM_SUBNET_PREFIX = os.environ.get("VM_SUBNET_PREFIX", "172.16")


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


ASG_NAME = os.environ.get("ASG_NAME", "openclaw-hosts-asg")
ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")
VPC_ID = os.environ.get("VPC_ID", "")
# per-tenant ALB rule(/vm/{tenant}*→gateway 直连)是**旧架构遗留**:C 端现在走
# openclaw channel→WS hub(浏览器只连 hub,channel 主动拨出注册),不再需要给每个
# 租户在 dashboard ALB 加一条 listener rule。而 ALB listener rule 有硬上限(默认 100),
# 每租户一条 rule 在 ~100 租户时撞 `TooManyRules` 致后续 create 全失败(2026-06-29
# 持续压测实锤:50并发 round3 起雪崩,根因 ALB rule 撞 100 非控制面/容量)。
# 故默认 OFF——channel 架构下不加 per-tenant rule,容量不再被 ALB rule 上限卡死。
# 仅当某部署仍依赖 /vm/{tenant} 经 ALB 直连 gateway(老式 dashboard 直连)才显式开。
ENABLE_PER_TENANT_ALB_RULE = (
    os.environ.get("ENABLE_PER_TENANT_ALB_RULE", "false").lower() == "true"
)
elbv2 = boto3.client("elbv2")

# 控制面重构阶段1 — SQS lifecycle 队列(削峰)。LIFECYCLE_QUEUE_URL 配了即启用
# 异步入队路径:create/start/stop/delete 写 DDB desired-state + 入队 + 立即返 202,
# 不再同步等 SSM(治 p99 飙升 + 持续负载雪崩)。空 = 保持同步路径(向后兼容)。
LIFECYCLE_QUEUE_URL = os.environ.get("LIFECYCLE_QUEUE_URL", "")
sqs = boto3.client("sqs") if LIFECYCLE_QUEUE_URL else None
# Phase 2 — route POST /tenants through the FIFO queue too (not just start/stop).
# Default OFF so the create path is unchanged until a deployment opts in (and the
# queue is actually deployed). When ON + queue present, a create-burst is shed
# onto SQS and drained at the consumer's reserved-concurrency rate.
CREATE_VIA_QUEUE = os.environ.get("CREATE_VIA_QUEUE", "false").lower() == "true"
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


# Issue #16 / #9 — quota ceilings (0 = unlimited; ENABLED=false → no checks)
QUOTAS_ENABLED = os.environ.get("QUOTAS_ENABLED", "false").lower() == "true"
QUOTAS_MAX_VCPU = int(os.environ.get("QUOTAS_MAX_VCPU", "0") or "0")
QUOTAS_MAX_MEM_MB = int(os.environ.get("QUOTAS_MAX_MEM_MB", "0") or "0")
QUOTAS_MAX_DATA_DISK_MB = int(os.environ.get("QUOTAS_MAX_DATA_DISK_MB", "0") or "0")
# Self-service: max openclaw nodes a single Cognito user may self-provision via
# POST /tenants/self (anti-abuse). Default 1 (one node per user); 0 = unlimited.
SELF_MAX_NODES_PER_USER = int(os.environ.get("SELF_MAX_NODES_PER_USER", "1") or "0")

# Firecracker can't snapshot a VM with an active balloon device, so live
# migrate is unavailable while balloon is on (issue #72). Reject early.
BALLOON_ENABLED = os.environ.get("BALLOON_ENABLED", "false").lower() == "true"

# ── 1.5.0 security hardening: Cognito JWT signature verification ──
# COGNITO_USER_POOL_ID is injected by CDK from the genuine, stack-owned pool
# (deploy/stack.py add_environment). Empty when console_auth is disabled — in
# which case signature verification is impossible and every Bearer token fails
# safe to `viewer`. AWS_REGION is provided by the Lambda runtime.
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_REGION = os.environ.get("AWS_REGION", "") or os.environ.get(
    "AWS_DEFAULT_REGION", ""
)
# ── WI-002: end-to-end Cognito for the channel plane ──
# The app client (public, USER_PASSWORD_AUTH) the per-tenant machine-user signs
# in with. Injected by CDK from the stack-owned pool. Empty = channel Cognito
# DISABLED → create_tenant keeps minting the legacy HMAC channel_secret only
# (graceful rollout: nothing changes until the stack provisions this client).
COGNITO_CHANNEL_CLIENT_ID = os.environ.get("COGNITO_CHANNEL_CLIENT_ID", "")
# Lazily-built cognito-idp client (admin user provisioning). Only used when the
# channel Cognito plane is enabled; avoids a client init on the legacy path.
_cognito_idp = None


def _cognito_idp_client():
    global _cognito_idp
    if _cognito_idp is None:
        _cognito_idp = boto3.client("cognito-idp")
    return _cognito_idp


# Fall-back role for requests with NO Bearer token (API-key-only path).
# "viewer" = least privilege (fail-safe). Trusted automation that needs write
# access must present a Cognito id_token.
DEFAULT_NO_JWT_ROLE = os.environ.get("DEFAULT_NO_JWT_ROLE", "viewer")
# RBAC role-gating is its own switch, independent of console_auth (Cognito login).
# SECURITY (go-live A2): default is now ON — owner/role checks are enforced by
# default so a production deploy is least-privilege out of the box. A demo/dev
# deploy that genuinely wants the old open behavior must set RBAC_ENABLED=false
# EXPLICITLY (config console_auth.rbac_enabled=false). Absent/unset → enforced.
RBAC_ENABLED = os.environ.get("RBAC_ENABLED", "true").lower() == "true"

# ── Go-live A1: external authorization (tenant↔user mapping write-authority外置) ──
# When EXTERNAL_AUTHZ is on, the "who may use which tenant" mapping is NOT derived
# by us (we stop implicitly owning a tenant by whoever's Cognito sub created it);
# instead the external backend is the WRITE AUTHORITY and pushes grants/revokes via
# the HMAC-signed POST /external/authz endpoint. Our DynamoDB then only CACHES that
# authoritative mapping (authorized_users), and a user's access is exactly what the
# external backend authorized — never something we minted. Default OFF (current
# behavior: creator owns the tenant). EXTERNAL_AUTHZ_SECRET is the shared HMAC key
# the external backend signs with (Secrets Manager-backed env; never logged).
EXTERNAL_AUTHZ = os.environ.get("EXTERNAL_AUTHZ", "false").lower() == "true"
EXTERNAL_AUTHZ_SECRET = os.environ.get("EXTERNAL_AUTHZ_SECRET", "")
# clock-skew window for the signed request timestamp (seconds)
EXTERNAL_AUTHZ_TS_WINDOW = int(os.environ.get("EXTERNAL_AUTHZ_TS_WINDOW", "300"))

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
    ("GET", "/agentcore/status"),
    ("GET", "/agentcore/tools"),
    ("GET", "/system/info"),
    ("GET", "/audit-log"),
    # 1.4.0 (#62) — listing groups is read-only.
    ("GET", "/groups"),
    # 1.4.1 (#63) — read individual SKILL.md content (editor / viewer)
    ("GET", "/skills/{name}"),
    # claw-channel: any VERIFIED Cognito user may sign a message for a tenant
    # they own. The route floor is "viewer" (just needs a real token); the actual
    # authorization is the JWT verification + owner check inside chat_sign().
    ("POST", "/chat/sign"),
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
API_KEY_OWNER = "api-key"


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
            }

    if isinstance(event, dict):
        event["_caller_identity_memo"] = ident
    return ident


def _assert_owner_or_admin(item, event):
    """Return None if the caller may operate on `item`, else a 403 response.

    • Admins and the API-key caller bypass the owner check (full access).
    • Otherwise the caller's owner_id must equal the record's owner_id.
    • Records with no owner_id (legacy / API-key-created) are admin/api-key
      only — a non-admin Cognito user is denied rather than allowed.

    Ownership is only enforced when RBAC_ENABLED; with role-gating off the API
    behaves as the pre-#80 single-tenant control plane (no per-route checks).
    """
    if not RBAC_ENABLED:
        return None
    ident = _get_caller_identity(event)
    if ident["is_admin"]:
        return None
    if ident["owner_id"] is None:
        # Untrusted token — RBAC layer already denied, but be defensive.
        return _resp(403, {"error": "forbidden"})
    owner = (item or {}).get("owner_id")
    if owner and owner == ident["owner_id"]:
        return None
    return _resp(403, {"error": "forbidden: not the owner of this tenant"})


def _resolve_effective_skills(tenant_item):
    """Compute the set of skill names a tenant should receive at launch time.

    1.4.0 (#62): per-tenant / per-group skill distribution.

    Returns:
        None  → tenant has no per-tenant or group scope set; launch-vm.sh
                 falls back to broadcast (legacy v1.3.x behavior).
        list  → sorted unique list of skill names from
                 (tenant.skills) ∪ (groups[tenant.group].skills).
                 If the union is empty, also returns None to avoid lock-out
                 (an explicit empty-list scope would otherwise prevent any
                 skill from reaching the VM, which is rarely intended).

    Unknown groups are silently dropped from the union — the operator gets
    a warning at /groups admin time, not at every launch.
    """
    if not tenant_item:
        return None
    tenant_skills = tenant_item.get("skills") or []
    group_name = (tenant_item.get("group") or "").strip()
    # No scoping configured → broadcast.
    if not tenant_skills and not group_name:
        return None

    effective = set(s for s in tenant_skills if s)
    if group_name and groups_table is not None:
        try:
            grp = groups_table.get_item(Key={"name": group_name}).get("Item") or {}
            for s in grp.get("skills") or []:
                if s:
                    effective.add(s)
        except Exception:
            # Group lookup failure is non-fatal — proceed with tenant.skills only.
            pass

    return sorted(effective) if effective else None


# ============================================================
# Groups CRUD (1.4.0 / #62)
# ============================================================


def list_groups():
    """GET /groups — list all groups."""
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured (1.3.x deployment?)"})
    try:
        resp = groups_table.scan()
        return _resp(200, {"groups": resp.get("Items", [])})
    except Exception as e:
        return _resp(500, {"error": str(e)})


def create_group(body_str):
    """POST /groups — create a new group with optional initial skills.

    Body: {"name": "team-sre", "skills": ["a", "b"], "description": "..."}
    """
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured (1.3.x deployment?)"})
    try:
        body = json.loads(body_str or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON"})
    name = (body.get("name") or "").strip()
    if not name:
        return _resp(400, {"error": "name is required"})
    # Reuse tenant DNS-label rules — group names show up in audit logs and
    # potentially in DNS-related artifacts later, so keep them safe.
    if not _NAME_RE.match(name):
        return _resp(
            400, {"error": "name must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"}
        )
    skills = body.get("skills") or []
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        return _resp(400, {"error": "skills must be a list of strings"})
    description = (body.get("description") or "").strip()
    from datetime import datetime, timezone

    item = {
        "name": name,
        "skills": sorted(set(s for s in skills if s)),
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        groups_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(#n)",
            ExpressionAttributeNames={"#n": "name"},
        )
    except groups_table.meta.client.exceptions.ConditionalCheckFailedException:
        return _resp(409, {"error": f"group '{name}' already exists"})
    except Exception as e:
        return _resp(500, {"error": str(e)})
    return _resp(201, item)


def add_skill_to_group(name, body_str):
    """POST /groups/{name}/skills — append a skill to a group's list (idempotent)."""
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured"})
    try:
        body = json.loads(body_str or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON"})
    skill = (body.get("skill") or "").strip()
    if not skill:
        return _resp(400, {"error": "skill is required"})
    try:
        existing = groups_table.get_item(Key={"name": name}).get("Item")
        if not existing:
            return _resp(404, {"error": f"group '{name}' not found"})
        cur = set(existing.get("skills") or [])
        cur.add(skill)
        groups_table.update_item(
            Key={"name": name},
            UpdateExpression="SET skills = :s",
            ExpressionAttributeValues={":s": sorted(cur)},
        )
        return _resp(200, {"name": name, "skills": sorted(cur)})
    except Exception as e:
        return _resp(500, {"error": str(e)})


def remove_skill_from_group(name, skill):
    """DELETE /groups/{name}/skills/{skill} — remove a skill from a group's list."""
    if groups_table is None:
        return _resp(503, {"error": "groups table not configured"})
    try:
        existing = groups_table.get_item(Key={"name": name}).get("Item")
        if not existing:
            return _resp(404, {"error": f"group '{name}' not found"})
        cur = set(existing.get("skills") or [])
        cur.discard(skill)
        groups_table.update_item(
            Key={"name": name},
            UpdateExpression="SET skills = :s",
            ExpressionAttributeValues={":s": sorted(cur)},
        )
        return _resp(200, {"name": name, "skills": sorted(cur)})
    except Exception as e:
        return _resp(500, {"error": str(e)})


# ========== Skills CRUD (1.4.1 #63 — Console skills management) ==========
#
# Read/write SKILL.md content directly via API so the operator console
# can offer in-browser edit/upload/delete without requiring an AWS
# credentials shell. GET /skills (list) stays in the dedicated skills
# Lambda; the per-name CRUD lives here so we reuse the existing
# RBAC + audit-log infrastructure.

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_SKILL_MAX_BYTES = 256 * 1024  # 256 KiB — generous, an SKILL.md should be tiny

# issue #59 (WI-E/M-1) — config_template is caller-controlled and flows into an
# SSM root shell command; its ONLY legitimate use is as an S3 path slug
# (launch-vm.sh: s3://$ASSETS_BUCKET/templates/openclaw/${CONFIG_TEMPLATE}/openclaw.json),
# so it must be a plain DNS-label. Reject anything with shell metacharacters,
# whitespace, or path separators at the edge (defense in depth still quotes it
# in _launch_vm). Empty == "no custom template" and is validated separately.
_CONFIG_TEMPLATE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")

# #93 idempotency key / #95 adversarial C-003/C-005/C-006 — client_token is a
# caller-supplied idempotency key that flows into an SSM command and log lines.
# Restrict to 4-128 printable ASCII (codepoints 33-126): no spaces, no control
# chars (\n \t \x00), no non-ASCII. .isascii() alone lets control chars through.
_CLIENT_TOKEN_RE = re.compile(r"^[\x21-\x7e]{4,128}$")


def read_skill(name):
    """GET /skills/{name} — return the SKILL.md content for the editor.

    Returns 404 if the skill does not exist (no SKILL.md under the
    s3://${ASSETS_BUCKET}/skills/{name}/ prefix). Body is text/plain
    in the JSON `content` field so the console can drop it straight
    into a textarea.
    """
    if not _SKILL_NAME_RE.match(name or ""):
        return _resp(400, {"error": "invalid skill name"})
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    key = f"skills/{name}/SKILL.md"
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        content = obj["Body"].read().decode("utf-8", errors="replace")
        return _resp(
            200,
            {
                "name": name,
                "content": content,
                "size": obj.get("ContentLength", len(content)),
                "last_modified": obj.get("LastModified").isoformat()
                if obj.get("LastModified")
                else None,
            },
        )
    except s3.exceptions.NoSuchKey:
        return _resp(404, {"error": f"skill '{name}' not found"})
    except Exception as e:
        # Some SDKs throw a generic ClientError with code "NoSuchKey"
        msg = str(e)
        if "NoSuchKey" in msg or "404" in msg:
            return _resp(404, {"error": f"skill '{name}' not found"})
        return _resp(500, {"error": msg})


def update_skill(name, body_str):
    """PUT /skills/{name} — create or replace the skill's SKILL.md.

    Body: {"content": "<markdown>"}. The content must:
      - be valid UTF-8
      - be ≤ _SKILL_MAX_BYTES
      - contain at least one top-level "# Title" line

    The S3 cron sync on each host picks the new file up within 5 min,
    after which new VMs will receive it at launch.
    """
    if not _SKILL_NAME_RE.match(name or ""):
        return _resp(
            400, {"error": "invalid skill name (lowercase letters, digits, hyphens)"}
        )
    try:
        body = json.loads(body_str or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON body"})
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return _resp(400, {"error": "missing or empty 'content' field"})
    if len(content.encode("utf-8")) > _SKILL_MAX_BYTES:
        return _resp(400, {"error": f"content exceeds {_SKILL_MAX_BYTES} bytes"})
    # Require a top-level Markdown heading so empty stubs don't end up published.
    has_h1 = any(
        ln.lstrip().startswith("# ") and ln.lstrip()[2:].strip()
        for ln in content.splitlines()
    )
    if not has_h1:
        return _resp(
            400,
            {"error": "SKILL.md must contain at least one top-level '# Title' line"},
        )
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    key = f"skills/{name}/SKILL.md"
    try:
        # Detect whether this is a create or replace for a more useful response.
        try:
            s3.head_object(Bucket=bucket, Key=key)
            existed = True
        except Exception:
            existed = False
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        return _resp(
            200 if existed else 201,
            {
                "name": name,
                "size": len(content.encode("utf-8")),
                "created": not existed,
            },
        )
    except Exception as e:
        return _resp(500, {"error": str(e)})


def delete_skill(name):
    """DELETE /skills/{name} — remove the entire skills/{name}/ prefix.

    Removes SKILL.md plus any auxiliary files the operator may have
    uploaded under the skill's prefix (images, sub-docs, etc.).
    Idempotent: 404 if the skill never existed, 200 once it's gone.
    """
    if not _SKILL_NAME_RE.match(name or ""):
        return _resp(400, {"error": "invalid skill name"})
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    prefix = f"skills/{name}/"
    try:
        # List & batch-delete (S3 has no recursive delete)
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append({"Key": obj["Key"]})
        if not keys:
            return _resp(404, {"error": f"skill '{name}' not found"})
        # delete_objects max 1000 per call — way more than we'd ever have
        # in a single skill prefix, but loop defensively anyway.
        for i in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i : i + 1000]})
        return _resp(200, {"name": name, "deleted": len(keys)})
    except Exception as e:
        return _resp(500, {"error": str(e)})


# Routes authenticated by their OWN mechanism (not Cognito/RBAC) — skip the role
# gate. /external/authz verifies an HMAC signature inside its handler (go-live A1).
# /external/authz is HMAC-authed inside its handler; /tenantmatch is a pre-login
# read-only IdP-routing lookup (#97 档A) — the browser calls it BEFORE any Cognito
# login to learn which upstream IdP to federate to, so it can't require a JWT. It
# leaks no tenant data (only platform_id→idp_provider_name), and the API-key gate
# at API Gateway still fronts it.
_RBAC_SKIP = {("POST", "/external/authz"), ("GET", "/tenantmatch")}


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


# ════════════════════════════════════════════════════════════
# C-end channel signing endpoint (claw-channel)
# ════════════════════════════════════════════════════════════
#
# The mini-app does NOT talk to the bare gateway /v1/chat/completions endpoint
# anymore (that path needed device-auth-off + CORS=* + token-in-browser). Instead
# it asks this endpoint to SIGN its message, then posts the signed envelope to the
# per-VM claw-channel webhook (CloudFront -> ALB -> nginx /chat/{tenant}/inbound).
#
# Trust model (server-signs-for-channel pattern):
#   • Caller proves identity with a Cognito id_token (Bearer). We VERIFY its RS256
#     signature and take the `sub` from the verified claims — never from the body.
#   • The per-tenant HMAC secret lives only on the VM's data disk and is mirrored
#     into the tenant's DDB record by host-agent (same SSH+jq path it already uses
#     for gateway_token). It is read here server-side and NEVER sent to the browser.
#   • Signature = HMAC-SHA256(random + ts + body, secret) — byte-for-byte the
#     scheme claw-channel/index.js:verifySignature expects, with a ±300s window.
#   • body is the EXACT JSON string the client must POST to the webhook, with the
#     server-derived `sub` baked in, so the client cannot forge another user's id.
def chat_sign(body_str, event):
    """POST /chat/sign — verify Cognito JWT, sign a {sub,text} envelope for the
    tenant's claw-channel webhook. Returns the body + signature headers to POST.
    """
    import hmac

    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return _resp(401, {"error": "cognito id_token required"})
    claims = _verify_and_decode(auth[len("Bearer ") :].strip())
    if claims is None:
        return _resp(401, {"error": "invalid or untrusted token"})
    sub = (claims.get("sub") or "").strip()
    if not sub:
        return _resp(401, {"error": "token has no sub"})

    try:
        req = json.loads(body_str or "{}")
    except (ValueError, TypeError):
        return _resp(400, {"error": "invalid json"})
    tenant_id = str(req.get("tenant_id") or "").strip()
    text = str(req.get("text") or "").strip()
    if not tenant_id or not text:
        return _resp(400, {"error": "tenant_id and text required"})
    if len(text) > 8000:
        return _resp(413, {"error": "text too long"})

    item = tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        return _resp(404, {"error": "tenant not found"})
    # issue #80 — a non-admin Cognito user may only message a tenant they own.
    denied = _assert_owner_or_admin(item, event)
    if denied is not None:
        return denied
    secret = (item.get("channel_secret") or "").strip()
    if not secret:
        # host-agent mirrors the per-VM secret into DDB once the VM is healthy.
        return _resp(
            409, {"error": "channel secret not provisioned yet; retry shortly"}
        )

    # Canonical envelope the webhook will receive. sub is server-derived (T1).
    envelope = json.dumps({"sub": sub, "text": text}, separators=(",", ":"))
    nonce = os.urandom(16).hex()
    ts = str(int(time.time()))
    signature = hmac.new(
        secret.encode(),
        (nonce + ts + envelope).encode(),
        hashlib.sha256,
    ).hexdigest()
    return _resp(
        200,
        {
            "path": f"/chat/{tenant_id}/inbound",
            "body": envelope,
            "headers": {
                "x-claw-signature": signature,
                "x-claw-random": nonce,
                "x-claw-timestamp": ts,
            },
        },
    )


def lambda_handler(event, context):
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
        return _consume_lifecycle_sqs(event["Records"])

    method = event["httpMethod"]
    resource = event["resource"]
    path_params = event.get("pathParameters") or {}

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
        # claw-channel: sign a C-end message envelope for the per-VM webhook.
        ("POST", "/chat/sign"): lambda: chat_sign(event.get("body"), event),
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
        # #97 档A — external-platform → Cognito upstream IdP routing lookup.
        ("GET", "/tenantmatch"): lambda: tenant_match(
            event.get("queryStringParameters") or {}
        ),
        ("GET", "/audit-log"): lambda: _list_audit_log(
            event.get("queryStringParameters") or {}
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
        # 1.4.1 (#63) — Console skills CRUD
        ("GET", "/skills/{name}"): lambda: read_skill(path_params["name"]),
        ("PUT", "/skills/{name}"): lambda: update_skill(
            path_params["name"], event.get("body")
        ),
        ("DELETE", "/skills/{name}"): lambda: delete_skill(path_params["name"]),
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
_TENANT_SECRET_FIELDS = (
    "channel_secret",
    "litellm_vkey",
    "cognito_channel_password",  # WI-002 — machine-user password, never to browser
    "gateway_token",  # #100 — per-tenant bearer protecting the gateway control UI;
    # GET /tenants was leaking it in plaintext, letting one x-api-key harvest EVERY
    # tenant's gateway_token (credential batch-exposure). Server-side only (see :1092).
)


def _redact_tenant(item):
    """Return a shallow copy of a tenant record with secret fields removed.
    Defensive: callers pass DDB items straight to _resp, so this is the single
    choke point that keeps credentials server-side."""
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k not in _TENANT_SECRET_FIELDS}


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
    if RBAC_ENABLED:
        ident = _get_caller_identity(event or {})
        if not ident["is_admin"]:
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
    return _resp(200, _redact_tenant(item))


def create_tenant(body=None, event=None):
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body

    # issue #80 — stamp the creator's identity so future per-tenant routes can
    # enforce owner==caller. Cognito `sub` for logged-in users, API_KEY_OWNER
    # for the API-key path. None only if a Bearer token was present but failed
    # verification — RBAC would already have rejected such a write.
    owner_id = _get_caller_identity(event or {})["owner_id"]
    # task #13/#14 — capture the external stable id for OIDC-federated callers so
    # the tenant is attributable to the external user (identity chain). None for
    # native Cognito / API-key callers, in which case the field is not stored.
    tenant_user_id = _get_caller_identity(event or {}).get("tenant_user_id")
    # Go-live A1: when authority is external, do NOT derive ownership from whoever
    # created the tenant — access is granted exclusively by the external backend
    # via the signed /external/authz endpoint (written to authorized_users). We
    # park owner_id at the API_KEY_OWNER sentinel so no Cognito sub implicitly owns
    # it; with SHARED_TENANT_ACCESS off on the hub, that means "nobody until the
    # external backend grants".
    if EXTERNAL_AUTHZ:
        owner_id = API_KEY_OWNER

    name = body.get("name", "")
    name_err = _validate_name(name)
    if name_err:
        return _resp(400, {"error": name_err})

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

    vcpu, _e = _pos_int("vcpu", VM_DEFAULT_VCPU)
    if _e:
        return _err(400, "VALIDATION", _e)
    mem_mb, _e = _pos_int("mem_mb", VM_DEFAULT_MEM)
    if _e:
        return _err(400, "VALIDATION", _e)
    data_disk_mb, _e = _pos_int("data_disk_mb", VM_DATA_DISK_MB)
    if _e:
        return _err(400, "VALIDATION", _e)

    # Issue #9 — quota check (no-op when env vars unset).
    quota_err = _check_quota(vcpu, mem_mb, data_disk_mb)
    if quota_err:
        return _resp(400, {"error": quota_err})

    config_template = body.get("config_template", "")
    # issue #59 (WI-E/M-1) — reject injection at the edge. Unvalidated,
    # config_template reaches an SSM root shell on a shared host, the strongest
    # cross-tenant escape in the security review. Empty is the common "no custom
    # template" case; any non-empty value must be a bare DNS-label S3 slug.
    if config_template and not _CONFIG_TEMPLATE_RE.match(config_template):
        return _resp(
            400,
            {
                "error": "config_template must match ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"
            },
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
        return _err(
            400,
            "VALIDATION",
            "image_id must match ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$",
        )
    security, sec_err = _validate_security(body.get("security"))
    if sec_err:
        return _err(400, "VALIDATION", sec_err)
    client_token = (body.get("client_token") or "").strip()
    # #95 adversarial C-006: .isascii() passes control chars (\n \t \x00), and
    # .strip() only trims the edges, so an embedded control char used to slip
    # through and land in the SSM command / log line (injection / log-poisoning).
    # An idempotency key is a printable token — require ASCII 33-126 (no control
    # chars, no spaces). Length 4-128 (C-003 short / C-005 over-128 rejected too).
    if client_token and not _CLIENT_TOKEN_RE.match(client_token):
        return _err(
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
            return _resp(400, {"error": "skills must be a list of strings"})
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
        return _err(
            400,
            "VALIDATION",
            "chat_endpoint_enabled must be a JSON boolean (true/false)",
        )
    chat_endpoint_enabled = cee_raw

    group_in = (body.get("group") or "").strip()
    if group_in and not _NAME_RE.match(group_in):
        return _resp(
            400, {"error": "group must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"}
        )
    if group_in and groups_table is not None:
        # Soft-validate: warn at create time if the group doesn't exist yet,
        # rather than silently allowing a typo to drop tenant from any scope.
        try:
            grp_chk = groups_table.get_item(Key={"name": group_in}).get("Item")
            if not grp_chk:
                return _resp(
                    404,
                    {
                        "error": f"group '{group_in}' not found — create it first via POST /groups"
                    },
                )
        except Exception:
            pass

    # Issue #12 — clone_from is mutually exclusive with restore_from
    if clone_from and restore_from:
        return _resp(
            400, {"error": "clone_from and restore_from are mutually exclusive"}
        )

    # Resolve clone source: must exist + be running. Forces same-host scheduling.
    clone_src = None
    if clone_from:
        clone_src = tenants_table.get_item(
            Key={"id": clone_from}, ConsistentRead=True
        ).get("Item")
        if not clone_src:
            return _resp(404, {"error": f"clone source not found: {clone_from}"})
        # issue #80 — can't clone a tenant you don't own (IDOR).
        denied = _assert_owner_or_admin(clone_src, event or {})
        if denied is not None:
            return denied
        if clone_src.get("status") != "running":
            return _resp(
                400,
                {
                    "error": f"clone source must be running (current: {clone_src.get('status')})"
                },
            )

    # Issue #10 — validate tags up-front (fail fast before any side effects)
    tags_err = _validate_tags(body.get("tags"))
    if tags_err:
        return _resp(400, {"error": tags_err})
    tags = body.get("tags") or {}

    # Issue #15 — optional TTL fields
    ttl_fields, ttl_err = _parse_ttl(body.get("ttl_hours"), body.get("on_expiry"))
    if ttl_err:
        return _resp(400, {"error": ttl_err})

    # Issue #11 — optional `schedule` field; validated then persisted.
    sched, sched_err = _parse_schedule(body.get("schedule"))
    if sched_err:
        return _resp(400, {"error": sched_err})

    restore_backup_key = ""
    if restore_from:
        src_id = restore_from.get("tenant_id")
        if not src_id:
            return _resp(400, {"error": "restore_from.tenant_id required"})
        ts = restore_from.get("timestamp")
        restore_backup_key = _resolve_backup(src_id, ts)
        if not restore_backup_key:
            if ts:
                return _resp(404, {"error": f"backup not found: {src_id}/{ts}"})
            return _resp(404, {"error": f"no backups found for tenant_id={src_id}"})

    # On a consumer replay the id was already assigned at enqueue time; reuse it
    # so the consumer materializes exactly the id the caller was handed in its 202
    # (no second _gen_id → no orphaned/duplicate tenant). Fresh sync path mints a
    # new id as before.
    tenant_id = body.get("_assigned_tenant_id") or _gen_id(name, client_token, owner_id)
    now = _now()

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
        CREATE_VIA_QUEUE
        and LIFECYCLE_QUEUE_URL
        and not (event or {}).get("_consumer_ident")
    ):
        queued_body = dict(body)
        queued_body["_assigned_tenant_id"] = tenant_id
        queued_body["_enqueued_at"] = now
        if enqueue_lifecycle("create", tenant_id, event, extra=queued_body):
            return _resp(
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
    tenant_vkey = _mint_tenant_vkey(tenant_id, owner_id)

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

    # WI-002 — provision the per-tenant Cognito machine-user (end-to-end Cognito
    # for the channel plane). Returns None when the plane is unconfigured or the
    # call fails → we keep channel_secret only (legacy HMAC). When it succeeds we
    # ALSO keep channel_secret so a mixed fleet (old image VMs) still works during
    # the rolling rebuild — the in-VM channel prefers Cognito when present.
    cognito_creds = _provision_channel_machine_user(tenant_id)

    # Find host with capacity. The scheduler is normally automatic, but
    # operators occasionally need to pin a tenant to a specific host (e.g.
    # to drain a host before terminating it, or to keep two related VMs on
    # the same hardware). Three modes, in priority order:
    #   1. clone_from → must land on the source's host (local `cp` only)
    #   2. preferred_host_id (admin/operator) → land there or fail
    #   3. default → first host with capacity
    preferred_host_id = (body.get("preferred_host_id") or "").strip()
    if clone_src:
        host = _get_specific_host_with_capacity(clone_src["host_id"], vcpu, mem_mb)
        if not host:
            return _resp(
                400,
                {
                    "error": f"clone source's host {clone_src['host_id']} lacks "
                    f"capacity for clone (vcpu={vcpu}, mem_mb={mem_mb})"
                },
            )
    elif preferred_host_id:
        host = _get_specific_host_with_capacity(preferred_host_id, vcpu, mem_mb)
        if not host:
            # Distinguish "host doesn't exist" from "host full" so the
            # console can render the right message.
            existing = hosts_table.get_item(
                Key={"instance_id": preferred_host_id}, ConsistentRead=True
            ).get("Item")
            if not existing or existing.get("status") in ("deleted", "draining"):
                return _resp(
                    404,
                    {
                        "error": f"preferred_host_id {preferred_host_id} not found or draining"
                    },
                )
            return _resp(
                400,
                {
                    "error": f"preferred_host_id {preferred_host_id} lacks capacity "
                    f"(vcpu={vcpu}, mem_mb={mem_mb})"
                },
            )
    else:
        host = _find_host(vcpu, mem_mb)
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
        if tenant_user_id:  # task #13/#14 — external user attribution
            item["tenant_user_id"] = tenant_user_id
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
            tenants_table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(id)"
            )
        except tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
            return _err(
                409,
                "CONFLICT",
                f"tenant '{tenant_id}' already exists",
                extra={"id": tenant_id},
            )
        _scale_out()
        _publish_event(
            "tenant.created",
            tenant_id,
            {
                "name": name,
                "vcpu": vcpu,
                "mem_mb": mem_mb,
                "status": "pending",
            },
        )
        return _resp(
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
        cap_v = int(int(h["total_vcpu"]) * CPU_OVERCOMMIT_RATIO) - vcpu
        cap_m = int(int(h["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO) - mem_mb
        try:
            r = hosts_table.update_item(
                Key={"instance_id": h["instance_id"]},
                UpdateExpression=(
                    "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
                    "vm_count = vm_count + :one, next_vm_num = next_vm_num + :one, "
                    "#s = :a REMOVE idle_since"
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
                    ":a": "active",
                    ":expected": expected,
                    ":cap_v": cap_v,
                    ":cap_m": cap_m,
                },
                ReturnValues="UPDATED_NEW",
            )
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
            return _resp(
                409,
                {"error": "host slot contended or filled during allocation; retry"},
            )
        time.sleep(0.05 * (attempt + 1))
        host = _find_host(vcpu, mem_mb)
        if not host:
            return _resp(503, {"error": "no host capacity (contended out)"})
    if vm_num is None:
        return _resp(503, {"error": "slot allocation contended out after retries"})

    guest_ip = _guest_ip(vm_num)
    host_port = VM_PORT_BASE + vm_num - 1

    item = {
        "id": tenant_id,
        "name": name,
        "host_id": host["instance_id"],
        "vm_num": vm_num,
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
    if tenant_user_id:  # task #13/#14 — external user attribution
        item["tenant_user_id"] = tenant_user_id
    if tenant_vkey:  # task #15 — per-tenant LiteLLM billing key
        item["litellm_vkey"] = tenant_vkey
    if skills_in is not None:
        item["skills"] = skills_in
    if group_in:
        item["group"] = group_in
    if chat_endpoint_enabled:  # per-tenant chatCompletions switch (default off)
        item["chat_endpoint_enabled"] = True
    if cognito_creds:  # WI-002 — persist the machine-user password for restart/wake
        # Stored like channel_secret (server-side secret, stripped from API
        # responses). Re-injected by launch-vm.sh on wake/restart so the channel
        # always has the same creds the Cognito user pool expects.
        item["cognito_channel_password"] = cognito_creds["password"]
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
        tenants_table.put_item(
            Item=item, ConditionExpression="attribute_not_exists(id)"
        )
    except tenants_table.meta.client.exceptions.ConditionalCheckFailedException:
        _release_slot(host["instance_id"], vcpu, mem_mb)
        return _err(
            409,
            "CONFLICT",
            f"tenant '{tenant_id}' already exists",
            extra={"id": tenant_id},
        )
    except Exception:
        _release_slot(host["instance_id"], vcpu, mem_mb)
        raise

    # Issue #12 — for clones, snapshot source disks before launching the new VM.
    # clone-data.sh: pause src → cp --sparse data.ext4 + overlay.ext4 → resume src.
    if clone_src:
        src_vm_num = int(clone_src.get("vm_num", 1))
        clone_cmd = (
            f"/home/ubuntu/clone-data.sh {clone_from} {src_vm_num} {tenant_id} {vm_num}"
        )
        if not _ssm_run(host["instance_id"], clone_cmd, timeout=180):
            # Roll back: undo the host counter increment + delete tenant row
            hosts_table.update_item(
                Key={"instance_id": host["instance_id"]},
                UpdateExpression="SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
                ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1},
            )
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :s, updated_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "deleted", ":t": _now()},
            )
            return _resp(502, {"error": "clone-data.sh failed; tenant rolled back"})

    launch_cmd_id = _launch_vm(
        host["instance_id"],
        tenant_id,
        vm_num,
        vcpu,
        mem_mb,
        guest_ip,
        host_port,
        config_template,
        restore_backup_key,
        scoped_skills=_resolve_effective_skills(item),
        litellm_vkey=tenant_vkey or "",  # task #15 per-tenant billing key
        channel_secret=channel_secret,  # mint-up-front (kills hub handshake race)
        chat_endpoint_enabled=chat_endpoint_enabled,  # per-tenant chatCompletions
        cognito_creds=cognito_creds,  # WI-002 end-to-end Cognito (None → HMAC)
    )
    # loop 2026-07-01 bugfix: launch-vm's SSM SendCommand can be throttled when
    # many creates fan out concurrently (create_via_queue consumer × 10). It was
    # fire-and-forget, so a throttled launch left the tenant stuck in `creating`
    # forever with a leaked capacity slot (host账本 used_vcpu > 实际 fc). Now: if
    # submission failed, roll the reservation + tenant back and return 502 so the
    # caller retries — and the SQS consumer re-queues the create with backoff
    # (see _consume_lifecycle_sqs: code>=500 → batchItemFailures → SQS redrive).
    if launch_cmd_id is None:
        _release_slot(host["instance_id"], vcpu, mem_mb)
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "deleted", ":t": _now()},
        )
        return _resp(
            502,
            {
                "error": "launch-vm SSM dispatch failed (throttled?); "
                "tenant rolled back, retry",
                "id": tenant_id,
            },
        )

    # ALB path-based routing
    tg_arn = _ensure_host_tg(host["instance_id"], host["private_ip"])
    _add_alb_rule(tenant_id, tg_arn)

    _publish_event(
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

    return _resp(
        201,
        {
            "id": tenant_id,
            "host_id": host["instance_id"],
            "guest_ip": guest_ip,
            "host_port": host_port,
            "status": "creating",
        },
    )


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
    if not body.get("name"):
        # short, DNS-safe, unique-ish per user; create_tenant appends a hash.
        body["name"] = f"u-{str(sub)[:8].lower().replace('_', '-')}"
    return create_tenant(body, event)


def delete_tenant(tenant_id, query_params, event=None):
    item = tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        return _resp(404, {"error": "tenant not found"})
    # issue #80 — IDOR: only the owner (or admin / api-key) may delete.
    denied = _assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied
    if item.get("status") == "deleted":
        return _resp(200, {"id": tenant_id, "status": "deleted"})

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
    if host_id and not keep_data and not skip_backup:
        try:
            lambda_client = boto3.client("lambda")
            resp = lambda_client.invoke(
                FunctionName=os.environ.get("BACKUP_FUNCTION", "openclaw-backup"),
                InvocationType="RequestResponse",  # SYNC: data safe in S3 before rm
                Payload=json.dumps({"tenant_id": tenant_id}).encode("utf-8"),
            )
            ok = resp.get("StatusCode", 500) == 200 and "FunctionError" not in resp
            if not ok:
                return _resp(
                    502,
                    {
                        "error": "pre-delete backup failed; aborting destroy to avoid "
                        "irreversible data loss. Retry, or pass ?skip_backup=true to "
                        "delete without a backup.",
                    },
                )
        except Exception as e:
            return _resp(
                502,
                {
                    "error": f"pre-delete backup error ({e}); aborting destroy. Retry, "
                    "or ?skip_backup=true to force.",
                },
            )

    if host_id:
        # Stop VM via SSM
        vm_num = int(item.get("vm_num", 1))
        _ssm_run(host_id, f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}")
        # Remove vm.json so host-agent won't try to recover
        _ssm_run(host_id, f"rm -f /data/firecracker-vms/{tenant_id}/vm.json")

    # Remove ALB rule
    _remove_alb_rule(tenant_id)

    if host_id:
        # Remove DNAT rule (best effort)
        _ssm_run(
            host_id,
            f"sudo iptables -t nat -D PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) -p tcp --dport {item.get('host_port', 0)} -j DNAT --to-destination {item.get('guest_ip', '')}:{VM_PORT_BASE} 2>/dev/null || true",
        )

        if not keep_data:
            _ssm_run(host_id, f"rm -rf /data/firecracker-vms/{tenant_id}")

        # Update host counters
        host_resp = hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
            ExpressionAttributeValues={
                ":v": item["vcpu"],
                ":m": item["mem_mb"],
                ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
        # Record idle_since when host becomes empty (defensive — mocks may
        # omit Attributes; treat as still-busy and skip).
        attrs = host_resp.get("Attributes") if isinstance(host_resp, dict) else None
        if attrs and int(attrs.get("vm_count", 0)) == 0:
            hosts_table.update_item(
                Key={"instance_id": host_id},
                UpdateExpression="SET idle_since = :t",
                ExpressionAttributeValues={":t": _now()},
            )

    # Go-live C: reclaim the per-tenant LiteLLM vkey so it doesn't linger in
    # LiteLLM after the tenant is gone (credential + budget leak over churn).
    # Best-effort — delete proceeds regardless; we record whether it was revoked.
    vkey_revoked = _revoke_tenant_vkey(item.get("litellm_vkey"))

    # WI-002 — delete the per-tenant Cognito machine-user (mirror of provision at
    # create). Best-effort; delete proceeds regardless. Also drop the stored
    # password from DDB so no secret lingers on the deleted record.
    cognito_user_deleted = _delete_channel_machine_user(tenant_id)

    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET #s = :s, updated_at = :t REMOVE litellm_vkey, cognito_channel_password",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "deleted", ":t": _now()},
    )
    _publish_event(
        "tenant.deleted",
        tenant_id,
        {
            "keep_data": keep_data,
            "vkey_revoked": vkey_revoked,
            "cognito_user_deleted": cognito_user_deleted,
        },
    )
    return _resp(200, {"id": tenant_id, "status": "deleted"})


def tenant_action(tenant_id, action, body=None, event=None):
    item = tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        return _resp(404, {"error": "tenant not found"})
    # issue #80 — IDOR: gate every action (start/stop/restart/migrate/resize/
    # backup/…) on ownership. Checked once here so all branches are covered.
    denied = _assert_owner_or_admin(item, event or {})
    if denied is not None:
        return denied

    # 控制面重构阶段1 — 产端入队:纯 lifecycle 动作(start/stop/restart/pause/resume)
    # 只是经 SSM 下发、无特殊同步返回值,队列开启时入 SQS 由 consumer 受控并发消费
    # (削峰 + 限流阀,治 1000/s 雪崩),立即返 202。resize/backup/migrate/access 等
    # 有同步返回语义的不入队,保持原同步路径。开关关 → 全走同步(向后兼容)。
    # 防重入:consumer 重放时 event 带 _consumer_ident,不再二次入队。
    _async_actions = {"start", "stop", "restart", "pause", "resume", "reset", "rebuild"}
    if (
        action in _async_actions
        and LIFECYCLE_QUEUE_URL
        and not (event or {}).get("_consumer_ident")
    ):
        if enqueue_lifecycle(action, tenant_id, event):
            return _resp(
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
            return _resp(400, {"error": "missing or invalid new_size_mb"})
        current = int(item.get("data_disk_mb", VM_DATA_DISK_MB))
        if new_size <= current:
            return _resp(
                400,
                {
                    "error": f"new_size_mb must be larger (current {current}MB); shrink not supported"
                },
            )
        if new_size > 1024 * 1024:
            return _resp(400, {"error": "new_size_mb exceeds 1 TiB ceiling"})
        host_id = item.get("host_id")
        vm_num = int(item.get("vm_num", 1))
        if not host_id:
            return _resp(400, {"error": "tenant has no host (still pending?)"})
        # Issue #64-class fix: resize-disk.sh was never deployed (same defect
        # as migrate-vm.sh) AND this path was fire-and-forget — it flipped
        # data_disk_mb in DDB before the host had even run the script, so DDB
        # claimed the new size whether or not the ext4 grow actually happened.
        # Now: run synchronously, and only persist the new size on Success.
        if not _ssm_run(
            host_id,
            f"/home/ubuntu/resize-disk.sh {tenant_id} {vm_num} {new_size}",
            timeout=120,
        ):
            return _resp(
                502,
                {
                    "error": "resize-disk.sh failed on host; size unchanged",
                    "id": tenant_id,
                    "data_disk_mb": current,
                },
            )
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET data_disk_mb = :s, updated_at = :t",
            ExpressionAttributeValues={":s": new_size, ":t": _now()},
        )
        return _resp(
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
        if BALLOON_ENABLED:
            return _resp(
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
            return _resp(400, {"error": "missing target_host_id"})
        source_host_id = item.get("host_id")
        if target_host_id == source_host_id:
            return _resp(400, {"error": "target_host_id must be different from source"})
        target = hosts_table.get_item(
            Key={"instance_id": target_host_id}, ConsistentRead=True
        ).get("Item")
        if not target:
            return _resp(404, {"error": f"target host {target_host_id} not found"})
        if target.get("status") in ("draining", "deleted"):
            return _resp(
                409, {"error": f"target host {target_host_id} is {target['status']}"}
            )

        # Capacity check — same allocatable formula as _find_host().
        vcpu = int(item.get("vcpu", 0))
        mem_mb = int(item.get("mem_mb", 0))
        allocatable_vcpu = int(int(target["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
        free_vcpu = allocatable_vcpu - int(target.get("used_vcpu", 0))
        allocatable_mem = int(int(target["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO)
        free_mem = allocatable_mem - int(target.get("used_mem_mb", 0))
        if free_vcpu < vcpu or free_mem < mem_mb:
            return _resp(
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
        bucket = os.environ.get("ASSETS_BUCKET", "")
        snap_prefix = f"migrations/{tenant_id}"
        target_vm_num = int(target.get("next_vm_num", 1))
        now = _now()

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

        snap_cmd = _ssm_send(
            source_host_id,
            f"/home/ubuntu/migrate-vm.sh snapshot {tenant_id} {vm_num} "
            f"s3://{bucket}/{snap_prefix}",
            timeout=600,  # snapshot + multi-GB disk upload to S3
        )
        if not snap_cmd:
            # Couldn't even submit the SSM command — nothing started, DDB clean.
            return _resp(
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
        tenants_table.update_item(
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
        return _resp(
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
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        launch_cmd = f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}"
        # Re-add DNAT after restart
        dnat_cmd = (
            (
                f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = f"{stop_cmd} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        _ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "stop":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        # Remove DNAT rule
        dnat_del = (
            (
                f"sudo iptables -t nat -D PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE} 2>/dev/null || true"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = stop_cmd
        if dnat_del:
            full_cmd += f" && {dnat_del}"
        _ssm_run(item["host_id"], full_cmd)
        new_status = "stopped"
    elif action == "start":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        launch_cmd = f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}"
        dnat_cmd = (
            (
                f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = launch_cmd
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        _ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "reset":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        # Stop, delete overlay (force fresh layer), then launch
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        reset_cmd = f"rm -f /data/firecracker-vms/{tenant_id}/overlay.ext4"
        launch_cmd = f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}"
        dnat_cmd = (
            (
                f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = f"{stop_cmd} && {reset_cmd} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        _ssm_run(item["host_id"], full_cmd, timeout=300)
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
        launch_cmd = f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}"
        dnat_cmd = (
            (
                f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
                f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
            )
            if guest_ip and host_port
            else ""
        )
        full_cmd = f"{stop_cmd} && {drop_overlay} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        _ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "pause":
        vm_dir = f"/data/firecracker-vms/{tenant_id}"
        _ssm_run(
            item["host_id"],
            f"curl -s --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/vm "
            f'-H "Content-Type: application/json" -d \'{{"state":"Paused"}}\'',
        )
        new_status = "paused"
    elif action == "resume":
        vm_dir = f"/data/firecracker-vms/{tenant_id}"
        _ssm_run(
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
        _publish_event("tenant.backup_started", tenant_id, {})
        return _resp(202, {"id": tenant_id, "action": "backup", "status": "started"})
    elif action == "access":
        # Explicit tenant authorization (P0): owner/admin grants or revokes
        # another Cognito sub access to this tenant. Gated by _assert_owner_or_admin
        # above (only owner/admin can manage grants — least privilege). The grant
        # list lives in the tenant record's `authorized_users` map and the hub
        # consults it for /token + /files + WS. Audited via _audit_write (caller).
        return tenant_access_grant(tenant_id, item, body)
    else:
        return _resp(400, {"error": f"unknown action: {action}"})

    update_expr = "SET #s = :s, updated_at = :t"
    expr_values = {":s": new_status, ":t": _now()}
    if action in ("reset", "rebuild"):
        # Record the host's current rootfs_version on the tenant: after a rebuild
        # the VM runs whatever version the host has staged, so GET /tenants must
        # reflect that (drift visibility — who's been upgraded, who's stale).
        host = hosts_table.get_item(
            Key={"instance_id": item["host_id"]}, ConsistentRead=True
        ).get("Item", {})
        update_expr += ", rootfs_version = :rv"
        expr_values[":rv"] = host.get("rootfs_version", "")

    tenants_table.update_item(
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
    _publish_event(event_name, tenant_id, {"action": action, "status": new_status})
    return _resp(200, {"id": tenant_id, "status": new_status})


def list_backups(tenant_id):
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{tenant_id}/")
    backups = []
    for obj in sorted(resp.get("Contents", []), key=lambda o: o["Key"], reverse=True):
        name = obj["Key"].rsplit("/", 1)[-1]
        backups.append(
            {
                "key": obj["Key"],
                "timestamp": name.replace(".gz", ""),
                "size_mb": round(obj["Size"] / 1048576, 1),
            }
        )
    return _resp(200, {"tenant_id": tenant_id, "backups": backups})


def list_all_backups():
    """List all backups across all tenants, left-joined with tenants table to mark orphans."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")

    # Build tenant_id → (name, exists) map from DDB (include soft-deleted for name resolution)
    tenants = tenants_table.scan().get("Items", [])
    tenant_info = {
        t["id"]: {"name": t.get("name", ""), "exists": t.get("status") != "deleted"}
        for t in tenants
    }

    # Paginate S3 list to avoid missing objects when > 1000 backups exist
    paginator = s3.get_paginator("list_objects_v2")
    backups = []
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            # Expect: {prefix}/{tenant_id}/{timestamp}.gz
            if len(parts) < 3 or not parts[-1].endswith(".gz"):
                continue
            src_tenant_id = parts[-2]
            timestamp = parts[-1][:-3]  # strip ".gz"
            info = tenant_info.get(src_tenant_id, {"name": None, "exists": False})
            backups.append(
                {
                    "tenant_id": src_tenant_id,
                    "tenant_name": info["name"],
                    "tenant_exists": info["exists"],
                    "timestamp": timestamp,
                    "size_bytes": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )

    backups.sort(key=lambda b: b["last_modified"], reverse=True)
    return _resp(200, backups)


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
        return _resp(400, {"error": "missing principal (cognito sub)"})
    if op not in ("grant", "revoke"):
        return _resp(400, {"error": "op must be grant or revoke"})
    owner = item.get("owner_id")
    if principal == owner:
        return _resp(400, {"error": "owner access is implicit and cannot be modified"})
    current = item.get("authorized_users")
    if not isinstance(current, dict):
        current = {}
    if op == "revoke":
        current.pop(principal, None)
    else:
        role = str(payload.get("role", "member")).strip() or "member"
        grant = {"role": role, "granted_at": _now()}
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
        "tenant.access_changed", tenant_id, {"principal": principal, "op": op}
    )
    return _resp(200, {"id": tenant_id, "authorized_users": current})


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


# ========== Host Operations ==========


def list_hosts():
    items = hosts_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", [])
    # Filter out synthetic records (e.g. __az_failover_state__ used by the
    # health_check Lambda to remember per-AZ cooldown — added in 1.3.0).
    # Anything starting with "__" is reserved for internal bookkeeping and
    # must not appear in user-facing host lists.
    items = [h for h in items if not str(h.get("instance_id", "")).startswith("__")]
    for item in items:
        item["cpu_overcommit_ratio"] = CPU_OVERCOMMIT_RATIO
        item["mem_overcommit_ratio"] = MEM_OVERCOMMIT_RATIO
    return _resp(200, items)


# Same _sizes / _mem_ratio fallback as deploy/stack.py (kept in sync
# manually because both are intentionally tiny constant tables — adding
# a shared module just to dedupe two dicts isn't worth the import cost
# in cold-start). When EC2 describe_instance_types() works this table
# is unused; it only triggers if the API call fails.
_SIZE_TO_VCPU = {
    "medium": 1,
    "large": 2,
    "xlarge": 4,
    "2xlarge": 8,
    "4xlarge": 16,
    "8xlarge": 32,
    "12xlarge": 48,
    "16xlarge": 64,
    "24xlarge": 96,
}
_FAMILY_LETTER_TO_MEM_PER_VCPU = {"c": 2048, "m": 4096, "r": 8192}


def _resolve_instance_memory_mb(ec2_client, instance_type):
    """Return the advertised RAM (MiB) for an EC2 instance type.

    Tries the authoritative AWS API first (describe_instance_types →
    MemoryInfo.SizeInMiB), falling back to a static lookup table when
    the API call fails (permission, throttling, malformed instance_type).
    The fallback keeps register_host() functional in environments that
    haven't granted ec2:DescribeInstanceTypes, but we log loudly so the
    operator notices.
    """
    if instance_type:
        try:
            resp = ec2_client.describe_instance_types(InstanceTypes=[instance_type])
            return int(resp["InstanceTypes"][0]["MemoryInfo"]["SizeInMiB"])
        except Exception as exc:
            print(
                f"register_host: ec2.describe_instance_types({instance_type}) "
                f"failed: {exc}; falling back to static lookup"
            )
    # Fallback: parse e.g. "m8i.xlarge" → family=m, size=xlarge → 4 * 4096 = 16384 MiB
    try:
        family, size = instance_type.split(".")
        vcpu = _SIZE_TO_VCPU[size]
        return vcpu * _FAMILY_LETTER_TO_MEM_PER_VCPU[family[0]]
    except (ValueError, KeyError, IndexError):
        # Last-ditch sane default. Logged so the operator notices.
        print(
            f"register_host: unable to parse instance_type={instance_type!r}; "
            f"defaulting mem_total to 16384 MiB. Add the type to "
            f"_SIZE_TO_VCPU or grant ec2:DescribeInstanceTypes."
        )
        return 16384


def register_host(body):
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body
    instance_id = body.get("instance_id")
    if not instance_id:
        return _resp(400, {"error": "missing instance_id"})

    # Fetch instance info
    ec2 = boto3.client("ec2")
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    private_ip = inst["PrivateIpAddress"]
    instance_type = inst.get("InstanceType", "")
    # Capture the AZ so the console can group/filter hosts and tenants by AZ
    # without an extra describe_instances call. Falls back to "" rather than
    # failing if Placement is missing (would be unusual but defensive).
    az = (inst.get("Placement") or {}).get("AvailabilityZone", "")
    vcpu_total = inst["CpuOptions"]["CoreCount"] * inst["CpuOptions"]["ThreadsPerCore"]

    # Resolve memory from the instance type via the EC2 API rather than
    # hard-coding 16384 (which silently wrote wrong values for any host
    # larger than xlarge — see register_host TODO removed in 1.2.4).
    # describe_instance_types returns SizeInMiB which IS exactly the
    # advertised RAM; we fall back to a heuristic only if the API errors.
    mem_total = _resolve_instance_memory_mb(ec2, instance_type)

    hosts_table.put_item(
        Item={
            "instance_id": instance_id,
            "private_ip": private_ip,
            "az": az,
            "total_vcpu": vcpu_total - HOST_RESERVED_VCPU,
            "total_mem_mb": mem_total - HOST_RESERVED_MEM,
            "used_vcpu": 0,
            "used_mem_mb": 0,
            "vm_count": 0,
            "next_vm_num": 1,
            "status": "active",
            "idle_since": _now(),
        }
    )
    return _resp(201, {"instance_id": instance_id, "status": "active", "az": az})


def deregister_host(instance_id):
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "draining"},
    )
    # Terminate via ASG API to trigger termination lifecycle hook
    try:
        asg_client.terminate_instance_in_auto_scaling_group(
            InstanceId=instance_id,
            ShouldDecrementDesiredCapacity=False,
        )
    except Exception as e:
        print(f"Failed to terminate {instance_id}: {e}")
    return _resp(200, {"instance_id": instance_id, "status": "draining"})


def cleanup_terminated_host(event):
    """Called by termination lifecycle hook — cleanup DynamoDB then complete hook."""
    detail = event["detail"]
    instance_id = detail["EC2InstanceId"]
    print(f"cleanup_terminated_host: {instance_id}")

    # Delete all tenants on this host
    tenants = tenants_table.scan(
        FilterExpression="host_id = :h AND #s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":h": instance_id, ":d": "deleted"},
    ).get("Items", [])
    for t in tenants:
        _remove_alb_rule(t["id"])
        tenants_table.update_item(
            Key={"id": t["id"]},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "deleted", ":t": _now()},
        )

    # Remove host target group
    _remove_host_tg(instance_id)

    # Delete host
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "deleted", ":t": _now()},
    )
    print(f"cleaned up host {instance_id}, {len(tenants)} tenants deleted")

    # Complete lifecycle hook
    try:
        asg_client.complete_lifecycle_action(
            LifecycleHookName=detail["LifecycleHookName"],
            AutoScalingGroupName=detail["AutoScalingGroupName"],
            LifecycleActionResult="CONTINUE",
            InstanceId=instance_id,
        )
    except Exception as e:
        print(f"complete_lifecycle_action failed: {e}")


def rootfs_version():
    manifest = _get_manifest()
    return _resp(200, {"version": manifest.get("version", "unknown")})


def rootfs_drift():
    """GET /hosts/rootfs-drift — which tenants are NOT on the current rootfs.

    Phase 4: the rolling-upgrade companion to refresh_rootfs + the `rebuild`
    action. refresh_rootfs stages the new image on hosts; `rebuild` adopts it
    per-tenant; this endpoint shows WHO still needs rebuilding (their
    rootfs_version != the manifest's current version), so an operator can drive
    a rolling upgrade to completion instead of guessing. Pure read.
    """
    manifest = _get_manifest()
    current = manifest.get("version", "unknown")
    # Page the tenants table; only non-deleted tenants matter for upgrade drift.
    stale, up_to_date, unknown = [], 0, 0
    scan_kwargs = {
        "FilterExpression": "#s <> :d",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":d": "deleted"},
    }
    start_key = None
    while True:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        out = tenants_table.scan(**scan_kwargs)
        for t in out.get("Items", []):
            v = t.get("rootfs_version", "")
            if not v:
                unknown += 1
            elif v == current:
                up_to_date += 1
            else:
                stale.append(
                    {"id": t["id"], "rootfs_version": v, "host_id": t.get("host_id")}
                )
        start_key = out.get("LastEvaluatedKey")
        if not start_key:
            break
    return _resp(
        200,
        {
            "current_version": current,
            "up_to_date": up_to_date,
            "unknown": unknown,
            "stale_count": len(stale),
            "stale": stale,
        },
    )


def agentcore_status():
    enabled = os.environ.get("AGENTCORE_ENABLED", "false") == "true"
    gateway_url = os.environ.get("AGENTCORE_GATEWAY_URL", "")
    return _resp(
        200,
        {
            "enabled": enabled,
            "gateway_url": gateway_url if enabled else None,
        },
    )


# ════════════════════════════════════════════════════════════
# AgentCore tools listing (for console display)
# ════════════════════════════════════════════════════════════
#
# When AgentCore Gateway is enabled, three Lambda-backed MCP tools are
# registered (see deploy/stack.py — tools=hello/system_info/timestamp).
# The console wants to surface this list so operators can see what tools
# their VMs get for free without having to read the CDK code. The list is
# static (defined at deploy time), so the response is hard-coded here
# rather than calling out to bedrock-agentcore at request time — which
# would cost a control-plane API call per page load.
#
# If AgentCore is disabled, we return an empty list with a hint so the
# console can render an "AgentCore not enabled" placeholder.

_AGENTCORE_BUILTIN_TOOLS = [
    {
        "name": "hello",
        "description": "Say hello — test tool for verifying AgentCore Gateway connectivity",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name to greet"}},
        },
    },
    {
        "name": "system_info",
        "description": "Get Lambda runtime system information",
        "input_schema": {"type": "object"},
    },
    {
        "name": "timestamp",
        "description": "Get current UTC timestamp",
        "input_schema": {
            "type": "object",
            "properties": {"format": {"type": "string", "description": "iso or unix"}},
        },
    },
]


def agentcore_tools():
    """GET /agentcore/tools — list MCP tools registered with the Gateway.

    Today this is a static list (the tools are defined declaratively in
    stack.py at deploy time). A future PR can replace this with a live
    `bedrock-agentcore.list_targets()` call when the Gateway grows
    user-defined tools.
    """
    enabled = os.environ.get("AGENTCORE_ENABLED", "false") == "true"
    if not enabled:
        return _resp(200, {"enabled": False, "tools": []})
    return _resp(200, {"enabled": True, "tools": _AGENTCORE_BUILTIN_TOOLS})


# ════════════════════════════════════════════════════════════
# System info — feature flags / config snapshot for the console
# ════════════════════════════════════════════════════════════
#
# The console's Settings tab wants to surface "is multi-AZ on?",
# "is metrics on?", "is WAF on?" etc. without parsing config.yml.
# We expose the relevant env-derived flags here so the UI can render
# accurate state without an out-of-band copy of config.yml.


_PLATFORM_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")


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
            "metrics": {
                "enabled": bool(os.environ.get("AMP_REMOTE_WRITE_URL")),
                "amp_remote_write_url": os.environ.get("AMP_REMOTE_WRITE_URL", "")
                or None,
                "grafana_url": os.environ.get("GRAFANA_WORKSPACE_URL", "") or None,
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


def _get_manifest():
    """Read manifest.json from S3, return dict."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("ROOTFS_PREFIX", "rootfs")
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{prefix}/manifest.json")
        return json.loads(obj["Body"].read().decode())
    except Exception:
        return {}


def list_images(query_params=None):
    """GET /images — list golden-image artifacts in S3 + the live manifest (10h
    -goal #19: 查看黄金镜像内容). Read-only: enumerates the rootfs prefix (rootfs
    / data-template / kernel / golden-image.sha256 per version) with size + last
    modified, and reports which version manifest.json currently points at (the
    one new hosts boot). Does NOT download/expose image bytes — just the
    inventory + integrity-baseline presence, so an operator can see what's baked
    and which version is live without SSHing a host."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("ROOTFS_PREFIX", "rootfs")
    if not bucket:
        return _resp(503, {"error": "ASSETS_BUCKET not configured"})
    manifest = _get_manifest()
    live_version = manifest.get("version", "unknown")
    artifacts = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                name = key.rsplit("/", 1)[-1]
                if not name:
                    continue
                # classify by filename so the UI can group rootfs/data/kernel/hash
                lname = name.lower()
                if "data-template" in lname:
                    kind = "data-template"
                elif "rootfs" in lname or "openclaw-rootfs" in lname:
                    kind = "rootfs"
                elif "vmlinux" in lname or "kernel" in lname:
                    kind = "kernel"
                elif "sha256" in lname:
                    kind = "integrity-baseline"
                elif "manifest" in lname:
                    kind = "manifest"
                else:
                    kind = "other"
                artifacts.append(
                    {
                        "name": name,
                        "kind": kind,
                        "size_bytes": obj.get("Size", 0),
                        "last_modified": obj.get("LastModified").isoformat()
                        if obj.get("LastModified")
                        else None,
                        "is_backup": ".bak" in lname,
                    }
                )
    except Exception as e:
        return _resp(500, {"error": f"list images failed: {e}"})
    artifacts.sort(key=lambda a: (a["kind"], a["name"]))
    return _resp(
        200,
        {
            "live_version": live_version,
            "manifest": manifest,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
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


def refresh_rootfs():
    """Download rootfs + data template per manifest.json to all active/idle hosts."""
    manifest = _get_manifest()
    if not manifest:
        return _resp(500, {"error": "manifest.json not found"})

    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("ROOTFS_PREFIX", "rootfs")
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    version = manifest["version"]

    hosts = hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
    ).get("Items", [])

    if not hosts:
        return _resp(200, {"message": "no active hosts", "updated": 0})

    ids = [h["instance_id"] for h in hosts]
    assets = "/data/firecracker-assets"
    # Decompress to .tmp then rename — `pigz -dc src > dst` truncates dst at
    # redirect time, so a mid-pipe failure leaves a 0-byte rootfs that boots
    # silently into a kernel panic (issue surfaced 2026-05-22 on a v3.5 push).
    script = f"""
set -eu
ASSETS={assets}
BUCKET={bucket}
PREFIX={prefix}
REGION={region}
ROOTFS_GZ={manifest["rootfs"]}
DATA_GZ={manifest["data_template"]}
IMMUTABLE_GZ={manifest.get("immutable", "")}
aws s3 cp "s3://$BUCKET/$PREFIX/manifest.json" "$ASSETS/manifest.json" --region "$REGION"
aws s3 cp "s3://$BUCKET/$PREFIX/$ROOTFS_GZ" "$ASSETS/rootfs.gz" --region "$REGION"
aws s3 cp "s3://$BUCKET/$PREFIX/$DATA_GZ" "$ASSETS/data.gz" --region "$REGION"
pigz -dc "$ASSETS/rootfs.gz" > "$ASSETS/openclaw-rootfs.ext4.tmp"
[ -s "$ASSETS/openclaw-rootfs.ext4.tmp" ]
mv "$ASSETS/openclaw-rootfs.ext4.tmp" "$ASSETS/openclaw-rootfs.ext4"
rm -f "$ASSETS/rootfs.gz"
pigz -dc "$ASSETS/data.gz" > "$ASSETS/openclaw-data-template.ext4.tmp"
[ -s "$ASSETS/openclaw-data-template.ext4.tmp" ]
mv "$ASSETS/openclaw-data-template.ext4.tmp" "$ASSETS/openclaw-data-template.ext4"
rm -f "$ASSETS/data.gz"
fallocate --dig-holes "$ASSETS/openclaw-data-template.ext4"
# Immutable authority disk (identity + ops skills, read-only). MUST be refreshed
# too — new skills + the routing AGENTS.md live ONLY here, so skipping it means a
# rolling rebuild silently ships stale skills. Same .tmp→mv anti-truncation guard.
if [ -n "$IMMUTABLE_GZ" ]; then
  aws s3 cp "s3://$BUCKET/$PREFIX/$IMMUTABLE_GZ" "$ASSETS/immutable.gz" --region "$REGION"
  pigz -dc "$ASSETS/immutable.gz" > "$ASSETS/openclaw-immutable.ext4.tmp"
  [ -s "$ASSETS/openclaw-immutable.ext4.tmp" ]
  mv "$ASSETS/openclaw-immutable.ext4.tmp" "$ASSETS/openclaw-immutable.ext4"
  rm -f "$ASSETS/immutable.gz"
fi
""".strip()
    try:
        ssm.send_command(
            InstanceIds=ids,
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script], "executionTimeout": ["600"]},
        )
    except Exception as e:
        return _resp(500, {"error": str(e)})

    # Mark version as in-flight; host-agent confirms after files are on disk.
    for host_id in ids:
        hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="SET rootfs_version = :v",
            ExpressionAttributeValues={":v": version},
        )

    return _resp(200, {"message": "refresh started", "version": version, "hosts": ids})


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
        host = _find_host(vcpu, mem_mb)
        if not host:
            break

        vm_num = int(host.get("next_vm_num", 1))
        guest_ip = _guest_ip(vm_num)
        host_port = VM_PORT_BASE + vm_num - 1
        now = _now()

        # Update pending tenant with host assignment
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

        hosts_table.update_item(
            Key={"instance_id": host["instance_id"]},
            UpdateExpression="SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, vm_count = vm_count + :one, next_vm_num = :next",
            ExpressionAttributeValues={
                ":v": vcpu,
                ":m": mem_mb,
                ":one": 1,
                ":next": vm_num + 1,
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
            # WI-002 — rebuild Cognito creds from stored password (None → HMAC)
            cognito_creds=_cognito_creds_from_tenant(tenant),
        )
        tg_arn = _ensure_host_tg(host["instance_id"], host["private_ip"])
        _add_alb_rule(tenant["id"], tg_arn)
        assigned += 1

    return {
        "statusCode": 200,
        "body": f"assigned {assigned}/{len(pending)} pending tenants",
    }


def _scale_out():
    """Increment ASG desired capacity by 1 (capped at max)."""
    try:
        resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
        group = resp["AutoScalingGroups"][0]
        desired = group["DesiredCapacity"]
        max_size = group["MaxSize"]
        if desired < max_size:
            asg_client.set_desired_capacity(
                AutoScalingGroupName=ASG_NAME,
                DesiredCapacity=desired + 1,
            )
            print(f"ASG scaled out: {desired} → {desired + 1}")
        else:
            print(f"ASG at max capacity ({max_size}), cannot scale out")
    except Exception as e:
        print(f"Scale out error: {e}")


# ========== Helpers ==========


def _release_slot(instance_id, vcpu, mem_mb):
    """Roll back a capacity reservation made by the create/clone CAS when a
    later step (put_item / launch) fails. Decrements used_vcpu / used_mem_mb /
    vm_count but deliberately does NOT decrement next_vm_num — vm_num is a
    monotonic counter, and rewinding it could hand a just-freed number to a
    concurrent allocation that already claimed the next slot. Leaving a gap in
    the numbering is harmless; reusing a number is not. Best-effort; never
    raises (rollback failure must not mask the original error)."""
    try:
        hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, "
                "vm_count = vm_count - :one"
            ),
            ConditionExpression="used_vcpu >= :v AND used_mem_mb >= :m AND vm_count >= :one",
            ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1},
        )
    except Exception as e:
        print(f"_release_slot {instance_id} (non-fatal): {e}")


def _find_host(vcpu_needed, mem_needed):
    """Find an active or idle host with enough free resources.

    Spreads load across the warm pool (least-loaded / max-free-vcpu first)
    instead of packing onto whichever host the DynamoDB scan returns first.
    The old "return first fit" behaviour funneled every tenant onto the same
    host until it was overcommitted, leaving the rest of the pool idle.
    """
    # Phase 6: strong read. Under a 380-create burst the spread ranking must see
    # each host's freshest used_* (which sibling creates just incremented via the
    # CAS), or every caller ranks the same stale "least-loaded" host and they all
    # pile onto it — exactly the PriorityInUse / "all packed on one host" failure
    # mode. _reserve_slot's CAS still prevents oversell; this makes the spread
    # correct instead of merely safe.
    hosts = hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    ).get("Items", [])

    # Rank by the host's *tightest* remaining resource, not vCPU alone.
    # Ranking on free_vcpu only mis-orders hosts when vCPU is loose but memory
    # is tight (observed live: a host showed free_vcpu=32 yet free_mem was
    # negative under aggressive MEM_OVERCOMMIT — vCPU-only ranking would still
    # rank it "best"). The hard capacity gate (free_* >= needed) keeps such a
    # host from being chosen, but the spread is wrong whenever one dimension is
    # near-full. Score each host by min(free_vcpu_ratio, free_mem_ratio) so we
    # spread toward the host with the most balanced headroom.
    best = None
    best_score = -1.0
    for h in hosts:
        allocatable_vcpu = int(int(h["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
        free_vcpu = allocatable_vcpu - int(h["used_vcpu"])
        allocatable_mem = int(int(h["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO)
        free_mem = allocatable_mem - int(h["used_mem_mb"])
        # Hard gate unchanged: must actually fit on both dimensions.
        if free_vcpu >= vcpu_needed and free_mem >= mem_needed:
            vcpu_ratio = free_vcpu / allocatable_vcpu if allocatable_vcpu else 0
            mem_ratio = free_mem / allocatable_mem if allocatable_mem else 0
            score = min(vcpu_ratio, mem_ratio)  # tightest dimension wins
            if score > best_score:
                best = h
                best_score = score
    return best


def _get_specific_host_with_capacity(instance_id, vcpu_needed, mem_needed):
    """Issue #12 — locate a specific host (used for same-host clone) and
    confirm it has capacity. Returns the host item or None."""
    # Phase 6: strong read so the capacity gate for a pinned/clone host sees the
    # freshest used_* a concurrent create may have just reserved.
    hosts = hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
        ConsistentRead=True,
    ).get("Items", [])
    for h in hosts:
        if h["instance_id"] != instance_id:
            continue
        allocatable_vcpu = int(int(h["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
        free_vcpu = allocatable_vcpu - int(h["used_vcpu"])
        allocatable_mem = int(int(h["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO)
        free_mem = allocatable_mem - int(h["used_mem_mb"])
        if free_vcpu >= vcpu_needed and free_mem >= mem_needed:
            return h
        return None  # found host but no capacity
    return None


def _gen_id(name, client_token="", owner_id=""):
    """Generate tenant id: name-xxxx (4 char hash).

    #93 / api-design-review C1 — idempotency. Without a client_token the hash seed
    is time.time() (a fresh id every call, legacy behavior preserved). WITH a
    client_token the seed is deterministic → the SAME id every time, so a retried
    POST with the same token collides on the conditional put (attribute_not_exists
    (id)) and returns 409 instead of opening a second VM (EC2 ClientToken
    semantics). client_token is optional (C3) so SDK auto-generation isn't forced.

    #95 adversarial C-002 (idempotency-key-as-decoration): on the idempotent path
    the WHOLE id — prefix included — must be name-independent. If `name` appeared
    anywhere in the id (even as a cosmetic prefix), the same client_token with a
    different name would produce a different primary key and slip past
    attribute_not_exists(id) → a double-open, exactly the retry the idempotency
    key exists to stop. So a token create returns `t-<hash(owner,token)>` with no
    name in it (the human-readable name still lives in the `name` field; the id is
    an opaque route/dir key downstream). owner_id is folded in (NUL-separated to
    avoid boundary ambiguity) so one owner's token can't collide with — or probe
    the existence of — another's (C-011: no cross-owner 409 oracle). 16 hex chars
    (64 bits) keeps global collisions across all owners' tokens negligible now
    that the name no longer namespaces the id."""
    if client_token:
        digest = hashlib.sha256(f"{owner_id}\x00{client_token}".encode()).hexdigest()
        return f"t-{digest[:16]}"  # name-independent idempotency key
    seed = f"{name}{time.time()}"  # legacy: fresh id per call
    return f"{name}-{hashlib.sha256(seed.encode()).hexdigest()[:4]}"


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
_ENCRYPTION_TYPES = ("none", "platform_managed", "tenant_cmk")
_ARN_RE = re.compile(r"^arn:aws[a-z\-]*:[a-z0-9\-]+:[a-z0-9\-]*:\d{0,12}:.+")


def _validate_security(sec):
    """Validate + normalize the optional `security` map. Returns (clean_map, err).
    Absent/empty → ({}, None): a tenant created without `security` behaves exactly
    as before this field existed (api-design-review A2: missing == old world =
    unencrypted/legacy). Enforces the invariant so DDB never holds a contradictory
    state (encrypted=false but a stray key, or encrypted=true with no key)."""
    if sec is None:
        return {}, None
    if not isinstance(sec, dict):
        return None, "security must be an object"
    enc = bool(sec.get("storage_encrypted", False))
    etype = sec.get("encryption_type", "none" if not enc else "platform_managed")
    if etype not in _ENCRYPTION_TYPES:
        return (
            None,
            f"security.encryption_type must be one of {list(_ENCRYPTION_TYPES)}",
        )
    kms = (sec.get("kms_key_arn") or "").strip()
    cert = (sec.get("cert_arn") or "").strip()
    sref = (sec.get("secret_ref") or "").strip()
    for label, val in (("kms_key_arn", kms), ("cert_arn", cert), ("secret_ref", sref)):
        if val and not _ARN_RE.match(val):
            return (
                None,
                f"security.{label} must be a full ARN (arn:aws:...), not a bare id",
            )
    # Invariant (see skill): encrypted=false ⇒ no key; tenant_cmk ⇒ key required.
    if not enc and kms:
        return None, "security.kms_key_arn set but storage_encrypted is false"
    if etype == "tenant_cmk" and not kms:
        return None, "security.encryption_type=tenant_cmk requires kms_key_arn (BYOK)"
    clean = {"storage_encrypted": enc, "encryption_type": etype}
    if kms:
        clean["kms_key_arn"] = kms
    if cert:
        clean["cert_arn"] = cert
    if sref:
        clean["secret_ref"] = sref
    return clean, None


def _resolve_backup(src_tenant_id, timestamp=None):
    """Return the S3 key of a backup, or empty string if not found.
    If timestamp is given, look up that exact backup. Otherwise return the most recent.
    """
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("BACKUP_PREFIX", "backups")
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{src_tenant_id}/")
    objs = resp.get("Contents", [])
    if not objs:
        return ""
    if timestamp:
        key = f"{prefix}/{src_tenant_id}/{timestamp}.gz"
        return key if any(o["Key"] == key for o in objs) else ""
    # Latest = highest LastModified
    return max(objs, key=lambda o: o["LastModified"])["Key"]


## ── ALB path-based routing ──


def _get_listener_arn():
    """Get ALB listener ARN for path-based routing rules."""
    return ALB_LISTENER_ARN


def _ensure_host_tg(instance_id, private_ip):
    """Create or return target group ARN for a host."""
    tg_name = f"oc-{instance_id[-8:]}"
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        return resp["TargetGroups"][0]["TargetGroupArn"]
    except Exception:
        pass
    resp = elbv2.create_target_group(
        Name=tg_name,
        Protocol="HTTP",
        Port=80,
        VpcId=VPC_ID,
        TargetType="ip",
        HealthCheckPath="/health",
        HealthCheckIntervalSeconds=10,
        HealthyThresholdCount=2,
    )
    tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
    elbv2.register_targets(
        TargetGroupArn=tg_arn, Targets=[{"Id": private_ip, "Port": 80}]
    )
    return tg_arn


def _add_alb_rule(tenant_id, tg_arn):
    """Add ALB listener rule for /vm/{tenant_id}*.

    旧架构遗留 + 默认禁用:C 端现走 channel→WS hub,不需要 per-tenant ALB rule。
    ALB listener rule 硬上限(默认 100)在 ~100 租户撞 TooManyRules 致 create 雪崩
    (2026-06-29 压测实锤)。默认 ENABLE_PER_TENANT_ALB_RULE=false 直接跳过——
    既解除容量被 ALB rule 上限卡死的炸弹,又保留老式 /vm 直连部署显式开的能力。

    Concurrency-safe priority allocation. The old code read the in-use
    priorities once and picked the lowest free slot; when several Lambdas"""
    if not ENABLE_PER_TENANT_ALB_RULE:
        return  # channel 架构默认路径:不加 per-tenant ALB rule(见上方开关注释)
    _add_alb_rule_impl(tenant_id, tg_arn)


def _add_alb_rule_impl(tenant_id, tg_arn):
    """实际加 ALB rule(仅 ENABLE_PER_TENANT_ALB_RULE=true 时走到)。
    ran at the same time they all computed the SAME priority and all but one
    got `PriorityInUseException` (500). We now (a) pick a RANDOM free slot to
    cut collision odds and (b) retry on PriorityInUse by re-reading the live
    rule set, so concurrent creates converge instead of failing.
    """
    arn = _get_listener_arn()
    if not arn:
        return

    last_err = None
    for attempt in range(10):
        rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
        # Idempotent: rule for this tenant already exists.
        if any(
            f"/vm/{tenant_id}" in v
            for r in rules
            for c in r.get("Conditions", [])
            for v in c.get("Values", [])
        ):
            return
        used = {int(r["Priority"]) for r in rules if r["Priority"] != "default"}
        free = [i for i in range(1, 1000) if i not in used]
        if not free:
            raise RuntimeError("ALB listener has no free rule priority (limit reached)")
        # Random free slot — two concurrent callers are unlikely to collide.
        priority = random.choice(free[: max(50, len(free) // 2)])
        try:
            elbv2.create_rule(
                ListenerArn=arn,
                Priority=priority,
                Conditions=[
                    {
                        "Field": "path-pattern",
                        "Values": [f"/vm/{tenant_id}", f"/vm/{tenant_id}/*"],
                    }
                ],
                Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
            )
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            last_err = e
            # Lost the race for this priority — re-read and retry with another.
            if code in ("PriorityInUse", "PriorityInUseException"):
                time.sleep(0.1 * (attempt + 1))
                continue
            raise
    raise last_err


def _repoint_alb_rule_to_tg(tenant_id, tg_arn):
    """1.3.1: Repoint /vm/<tenant_id>* to a different target group.

    Used by migrate (cross-host live migration) and AZ failover. If no rule
    exists yet for this tenant, creates one. If one exists pointing at the
    old host's TG, modifies it in place to point at the new TG. Without
    this, traffic keeps hitting the dead/old host after a host change.
    """
    arn = _get_listener_arn()
    if not arn:
        return
    rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
    rule_arn = None
    for r in rules:
        for c in r.get("Conditions", []):
            if c.get("Field") == "path-pattern" and any(
                f"/vm/{tenant_id}" in v for v in c.get("Values", [])
            ):
                rule_arn = r["RuleArn"]
                break
        if rule_arn:
            break
    if rule_arn:
        elbv2.modify_rule(
            RuleArn=rule_arn,
            Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )
    else:
        # No existing rule — fall back to creating it.
        _add_alb_rule(tenant_id, tg_arn)


def _remove_alb_rule(tenant_id):
    """Remove ALB listener rule for a tenant."""
    arn = _get_listener_arn()
    if not arn:
        return
    rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
    for r in rules:
        for c in r.get("Conditions", []):
            if c.get("Field") == "path-pattern" and f"/vm/{tenant_id}" in c.get(
                "Values", []
            ):
                elbv2.delete_rule(RuleArn=r["RuleArn"])
                return


def _remove_host_tg(instance_id):
    """Delete target group for a host."""
    tg_name = f"oc-{instance_id[-8:]}"
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
        arn = _get_listener_arn()
        if arn:
            rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
            for r in rules:
                for a in r.get("Actions", []):
                    if a.get("TargetGroupArn") == tg_arn:
                        elbv2.delete_rule(RuleArn=r["RuleArn"])
        elbv2.delete_target_group(TargetGroupArn=tg_arn)
    except Exception:
        pass


def _launch_vm(
    instance_id,
    tenant_id,
    vm_num,
    vcpu,
    mem_mb,
    guest_ip,
    host_port,
    config_template="",
    restore_backup_key="",
    scoped_skills=None,
    litellm_vkey="",
    channel_secret="",
    chat_endpoint_enabled=False,
    cognito_creds=None,
):
    """Fire-and-forget: launch VM + set up DNAT.

    If restore_backup_key is non-empty, launch-vm.sh will restore data.ext4 from that S3 key instead of using the template.

    1.4.0 (#62): scoped_skills is None or [] for the legacy "broadcast"
    behavior, or a list of skill names for per-tenant scoping. Passed as
    a comma-separated string to launch-vm.sh as the 7th positional arg
    (empty string == broadcast, comma-list == only those subdirs cp'd).
    """

    # issue #59 (WI-E/M-1) — every caller/external-influenced string below is
    # interpolated into an SSM AWS-RunShellScript command that runs as ROOT on a
    # shared host. Shell-quote each so a value can never break out of its
    # positional argument (defense in depth behind create_tenant's input regex).
    # Empty → the literal "" placeholder launch-vm.sh already special-cases
    # (launch-vm.sh:75) so positional alignment is preserved.
    def _q(val):
        return shlex.quote(val) if val else '""'

    # When restore is used but no template, still need a placeholder in arg 5 so positional args align.
    tpl_arg = _q(config_template)
    # Placeholder for restore_backup_key (arg 6) so arg 7 always lines up.
    restore_arg = _q(restore_backup_key)
    # 1.4.0: 7th positional arg — comma-separated skill list (or empty for broadcast).
    skills_arg = _q(",".join(scoped_skills)) if scoped_skills else '""'
    # task #15: 8th positional arg — per-tenant LiteLLM vkey (empty → shared key).
    vkey_arg = _q(litellm_vkey)
    # 9th positional arg — control-plane-minted channel_secret (hub HMAC). Empty
    # → launch-vm.sh falls back to generating its own (legacy; reintroduces the
    # host-agent read-back race). Non-empty (normal path) → DDB & guest share it.
    csecret_arg = _q(channel_secret)
    # 10th positional arg — per-tenant chatCompletions switch. Default off ("0")
    # keeps launch-vm.sh deleting the endpoint (secure default; see CLAUDE.md
    # "chatCompletions 为什么不能全局默认开"). Only tenants with
    # chat_endpoint_enabled=true in DDB get "1" → enabled:true injected.
    chat_ep_arg = "1" if chat_endpoint_enabled else "0"
    # WI-002: 11th positional arg — base64(JSON) of the per-tenant Cognito
    # machine-user creds (end-to-end Cognito). base64 dodges SSM quote-hell (the
    # JSON has braces/quotes). Empty placeholder ("") when the channel Cognito
    # plane is unconfigured → launch-vm.sh keeps the HMAC channel_secret path.
    if cognito_creds:
        cognito_arg = base64.b64encode(
            json.dumps(cognito_creds, separators=(",", ":")).encode()
        ).decode()
    else:
        cognito_arg = '""'
    cmd = (
        f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} {tpl_arg} {restore_arg} {skills_arg} {vkey_arg} {csecret_arg} {chat_ep_arg} {cognito_arg} && "
        f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
        f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
    )
    # Return the SSM CommandId (or None if submission failed — notably an SSM
    # SendCommand ThrottlingException under concurrent consumer fan-out, loop
    # 2026-07-01 real-machine bug). Callers on the create path check this: a
    # None means launch-vm never ran, so the tenant must be rolled back and the
    # create retried, not left stuck in `creating` with a leaked capacity slot.
    return _ssm_send(instance_id, cmd, timeout=300)


def _ssm_send(instance_id, command, timeout=120):
    """Fire-and-forget SSM command. Returns the CommandId (str) so callers can
    later poll get_command_invocation for completion, or None if submission
    failed. Existing call sites that ignore the return value are unaffected;
    the async migrate path (issue #64) stores the CommandId in DynamoDB so the
    health_check sweep can advance the migration out-of-band."""
    try:
        wrapped = f"export HOME=/home/ubuntu && cd /home/ubuntu && {command}"
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        return resp["Command"]["CommandId"]
    except Exception as e:
        print(f"SSM send error: {e}")
        return None


def _ssm_run(instance_id, command, timeout=30):
    """Execute command on host via SSM Run Command. Returns True on success."""
    try:
        # SSM runs as root; set HOME so ~ resolves to /home/ubuntu
        wrapped = f"export HOME=/home/ubuntu && cd /home/ubuntu && {command}"
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [wrapped], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        cmd_id = resp["Command"]["CommandId"]
        time.sleep(3)  # Wait for invocation to register
        for _ in range(timeout // 2):
            try:
                result = ssm.get_command_invocation(
                    CommandId=cmd_id,
                    InstanceId=instance_id,
                )
                status = result["Status"]
                if status == "Success":
                    return True
                if status in ("Failed", "TimedOut", "Cancelled"):
                    print(
                        f"SSM failed: {status} - {result.get('StandardErrorContent', '')}"
                    )
                    return False
            except ssm.exceptions.InvocationDoesNotExist:
                pass
            time.sleep(2)
        print(f"SSM timeout waiting for command {cmd_id}")
        return False
    except Exception as e:
        print(f"SSM error: {e}")
        return False


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ── Tag helpers (issue #10) ──

# Limits chosen to keep DynamoDB items small and avoid colon-conflict with the
# `?tag=k:v` query syntax. AWS resource tags use the same 50/256 model; we cap
# values at 100 chars (more than enough for typical labels) to be conservative.
_TAG_MAX_KEY_LEN = 50
_TAG_MAX_VALUE_LEN = 100
_TAG_MAX_COUNT = 20


_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")


def _validate_name(name):
    """Tenant name: lowercase DNS-label. Drives the tenant id, which gets
    embedded in URLs, Firecracker socket paths, and ALB rule conditions —
    all of which choke on whitespace or special chars."""
    if not isinstance(name, str) or not name:
        return "name is required"
    if len(name) > 32:
        return "name exceeds 32 characters"
    if not _NAME_RE.match(name):
        return (
            "name must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ "
            "(lowercase letters, digits, hyphens; cannot start/end with hyphen)"
        )
    return None


def _validate_tags(tags):
    """Return None if valid, else an error message string."""
    if tags is None:
        return None  # absent → treated as {}
    if not isinstance(tags, dict):
        return "tags must be an object (key/value map)"
    if len(tags) > _TAG_MAX_COUNT:
        return f"too many tags (max {_TAG_MAX_COUNT})"
    for k, v in tags.items():
        if not isinstance(k, str) or not k:
            return "tag key must be a non-empty string"
        if not isinstance(v, str):
            return f"tag value for '{k}' must be a string"
        if ":" in k:
            return f"tag key '{k}' must not contain ':' (reserved for query syntax)"
        if ":" in v:
            return f"tag value '{v}' must not contain ':' (reserved for query syntax)"
        if len(k) > _TAG_MAX_KEY_LEN:
            return f"tag key '{k}' exceeds {_TAG_MAX_KEY_LEN} characters"
        if len(v) > _TAG_MAX_VALUE_LEN:
            return f"tag value for '{k}' exceeds {_TAG_MAX_VALUE_LEN} characters"
    return None


def _collect_tag_filters(query_params, multi_query_params):
    """Return list of (key, value) pairs from ?tag=k:v occurrences.

    API Gateway delivers repeated query params via multiValueQueryStringParameters.
    For single-value calls only queryStringParameters is populated.
    """
    raw = []
    if multi_query_params and "tag" in multi_query_params:
        raw = list(multi_query_params["tag"] or [])
    elif query_params and "tag" in query_params:
        raw = [query_params["tag"]]
    pairs = []
    for r in raw:
        if not r or ":" not in r:
            # Malformed filter — keep it so it matches nothing (defensive)
            pairs.append((None, None))
            continue
        k, v = r.split(":", 1)
        pairs.append((k, v))
    return pairs


def _matches_all_tags(item, filters):
    """Item must have every (k, v) pair to match (AND semantics)."""
    item_tags = item.get("tags") or {}
    for k, v in filters:
        if k is None or item_tags.get(k) != v:
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Helpers restored after the v1.0.0-milestone-q2-2026 cross-PR merge.
# Issue #48 tracks the rationale: each helper was added by an early PR but
# lost when later PRs auto-resolved merge conflicts with `-X theirs`.
# Sources noted alongside each block. — fix/post-merge-regression
# ═══════════════════════════════════════════════════════════════════════════

# ----- TTL (#28 / issue #15, original 47158d2) -----
_TTL_MAX_HOURS = 8760  # 1 year
_TTL_VALID_ON_EXPIRY = ("stop", "delete")


def _parse_ttl(ttl_hours_raw, on_expiry_raw):
    """Validate and compute TTL fields.

    Returns (fields_dict, error_message). fields_dict is empty when no TTL is
    requested; otherwise contains {ttl_hours, on_expiry, expires_at}.
    """
    if ttl_hours_raw is None:
        if on_expiry_raw is not None:
            return {}, "on_expiry requires ttl_hours"
        return {}, None
    try:
        if isinstance(ttl_hours_raw, bool):
            raise TypeError
        ttl_hours = int(ttl_hours_raw)
    except (TypeError, ValueError):
        return {}, "ttl_hours must be a positive integer"
    if ttl_hours <= 0:
        return {}, "ttl_hours must be a positive integer"
    if ttl_hours > _TTL_MAX_HOURS:
        return {}, f"ttl_hours must be <= {_TTL_MAX_HOURS} (1 year)"
    on_expiry = on_expiry_raw or "stop"
    if on_expiry not in _TTL_VALID_ON_EXPIRY:
        return {}, (
            f"on_expiry must be one of {sorted(_TTL_VALID_ON_EXPIRY)}; "
            f"got {on_expiry!r}"
        )
    from datetime import datetime, timedelta, timezone

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    return {
        "ttl_hours": ttl_hours,
        "on_expiry": on_expiry,
        "expires_at": expires_at,
    }, None


# ----- Schedule (#30 / issue #11, original af9434b). Validation only — the
# scaler's _schedule_should_run lives in deploy/lambda/scaler/handler.py.
_SCHED_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_schedule(raw):
    """Validate a {start, stop, timezone, days} schedule. Returns (dict, err)."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "schedule must be an object"
    start = raw.get("start")
    stop = raw.get("stop")
    if not start:
        return None, "schedule.start required"
    if not stop:
        return None, "schedule.stop required"
    import re
    from datetime import datetime as _dt

    if not re.match(r"^\d{2}:\d{2}$", str(start)) or not re.match(
        r"^\d{2}:\d{2}$", str(stop)
    ):
        return None, "schedule.start/stop must be HH:MM"
    # Strict parse: rejects 08:60 etc.
    try:
        _dt.strptime(start, "%H:%M")
        _dt.strptime(stop, "%H:%M")
    except ValueError:
        return None, "schedule.start/stop must be a valid HH:MM time"
    if start == stop:
        return None, "schedule.start must differ from schedule.stop"
    tz = raw.get("timezone", "UTC")
    try:
        from zoneinfo import ZoneInfo  # noqa

        ZoneInfo(tz)
    except Exception:
        return None, f"unknown timezone: {tz}"
    days = raw.get("days") or list(_SCHED_DAYS)
    if not isinstance(days, list) or any(d not in _SCHED_DAYS for d in days):
        return None, f"schedule.days must be a subset of {list(_SCHED_DAYS)}"
    return {"start": start, "stop": stop, "timezone": tz, "days": days}, None


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


def _list_audit_log(query_params):
    """GET /audit-log — return recent audit entries, newest first.

    Optional query params:
        limit  — int (default 50, max 500)
        since  — ISO-8601 timestamp; only entries >= this are returned
    """
    if audit_table is None:
        return _resp(200, [])
    qp = query_params or {}
    try:
        limit = min(int(qp.get("limit", 50)), 500)
    except (TypeError, ValueError):
        limit = 50
    since = qp.get("since")
    from boto3.dynamodb.conditions import Key

    key_cond = Key("pk").eq("audit")
    if since:
        key_cond = key_cond & Key("ts").gte(since)
    try:
        items = audit_table.query(
            KeyConditionExpression=key_cond,
            ScanIndexForward=False,  # newest first
            Limit=limit,
        ).get("Items", [])
    except Exception as e:
        print(f"audit query failed: {e}")
        items = []
    return _resp(200, items[:limit])


# ----- Quota (#34 / issue #9, original 79000fa) -----
# QUOTAS_ENABLED / QUOTAS_MAX_* are defined at the top of the module
# (default disabled, matches README "enabled: false default — no checks").
# The post-merge regression repair (#48) accidentally re-declared them with
# a different default; that re-declaration has been removed.


def _check_quota(vcpu, mem_mb, data_disk_mb):
    """Return None if within quota, else an error string."""
    if not QUOTAS_ENABLED:
        return None
    if QUOTAS_MAX_VCPU and vcpu > QUOTAS_MAX_VCPU:
        return f"vcpu={vcpu} exceeds quota (max {QUOTAS_MAX_VCPU})"
    if QUOTAS_MAX_MEM_MB and mem_mb > QUOTAS_MAX_MEM_MB:
        return f"mem_mb={mem_mb} exceeds quota (max {QUOTAS_MAX_MEM_MB})"
    if QUOTAS_MAX_DATA_DISK_MB and data_disk_mb > QUOTAS_MAX_DATA_DISK_MB:
        return (
            f"data_disk_mb={data_disk_mb} exceeds quota (max {QUOTAS_MAX_DATA_DISK_MB})"
        )
    return None


# ----- SNS lifecycle notifications (#33 / issue #13, original 1f1bffa) -----
# NOTIFICATIONS_TOPIC_ARN and the sns client are defined at the top of the
# module. The post-merge regression repair (#48) accidentally re-bound them;
# that duplication has been removed.


def _publish_event(event_name, tenant_id, details):
    """Publish a tenant lifecycle event to SNS. No-op when topic not set.

    Best-effort: SNS publish failures are logged but do not break the
    underlying API operation.
    """
    if not NOTIFICATIONS_TOPIC_ARN:
        return
    try:
        msg = {
            "event": event_name,
            "tenant_id": tenant_id,
            "timestamp": _now(),
            "details": details or {},
        }
        sns.publish(
            TopicArn=NOTIFICATIONS_TOPIC_ARN,
            Subject=f"OpenClaw: {event_name} ({tenant_id})",
            Message=json.dumps(msg, default=str),
            MessageAttributes={
                "event": {"DataType": "String", "StringValue": event_name},
                "tenant_id": {"DataType": "String", "StringValue": tenant_id},
            },
        )
    except Exception as e:
        print(f"SNS publish failed (operation succeeded): {e}")


# ===== Control-plane scale-out: per-user fleet management (PRD #50-58) =====
# Manage thousands of openclaw microVMs by the tenant user that owns them,
# without a k8s control plane and without full-table scans. The tenant record
# already carries the user association (tenant_user_id / owner_id, written at
# create_tenant); these GSIs make "all nodes of a user" an indexed query.

_USER_PAGE_DEFAULT = 100  # default page size for paginated listings
_USER_PAGE_MAX = 1000  # hard cap so one call can't pull the whole table
_USER_ACTION_VALID = {"start", "stop"}  # per-user bulk lifecycle actions


def _encode_next_token(last_evaluated_key):
    """Opaque pagination cursor: base64(JSON(LastEvaluatedKey)). None → omitted."""
    if not last_evaluated_key:
        return None
    raw = json.dumps(last_evaluated_key, sort_keys=True, default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_next_token(token):
    """Decode a pagination cursor back into an ExclusiveStartKey. Bad token → None."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        key = json.loads(raw)
        return key if isinstance(key, dict) else None
    except Exception:
        return None


def _parse_limit(query_params):
    """Resolve a page-size limit from ?limit=. Returns (limit, err).

    Distinguishes "absent" (→ default, backward compatible) from "present but
    malformed" (→ 400 VALIDATION). #95 adversarial D-01/03/04/05: a garbage,
    negative, zero, or fractional ?limit= used to be silently clamped to a valid
    value, which hides a client bug and violates AWS API standard E1/E2 (a bad
    client input must fail loud with a structured code, not be silently coerced).
    A positive integer over the ceiling is still valid — clamp it down (D-02).
    """
    raw = (query_params or {}).get("limit")
    if raw is None or raw == "":
        return _USER_PAGE_DEFAULT, None
    # int("1.5")/int("abc") raise ValueError → reject as non-integer.
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, _err(400, "VALIDATION", "limit must be a positive integer")
    if n < 1:
        return None, _err(400, "VALIDATION", "limit must be a positive integer (>= 1)")
    return min(_USER_PAGE_MAX, n), None  # over-ceiling is valid, just capped


def _parse_next_token(token):
    """Validate a client-supplied pagination cursor. Returns (start_key, err).

    Absent → (None, None) (first page). Present but not a valid opaque cursor
    (bad base64, non-JSON, or a JSON value that isn't an object) → 400 VALIDATION.
    #95 adversarial D-06/07/10/11: a tampered/garbage next_token used to silently
    reset to page 1, which traps a paging client in an infinite first-page loop
    and masks cursor corruption. AWS API standard D3: an opaque token must be
    rejected, not reinterpreted. A structurally valid but foreign cursor (D-08/09)
    still decodes here and is defended downstream by the owner_id filter.
    """
    if not token:
        return None, None
    key = _decode_next_token(token)
    if not isinstance(key, dict) or not key:
        return None, _err(400, "VALIDATION", "next_token is invalid or expired")
    return key, None


def _authorize_user_scope(tenant_user_id, event):
    """Decide whether the caller may manage the fleet of `tenant_user_id`.

    Returns None when allowed, else a 403/400 _resp. Policy (reuses the same
    identity layer as every other route):
      • RBAC disabled                → allowed (single-tenant control plane)
      • admin / api-key caller       → allowed (external backend / trusted automation)
      • federated user, own id       → allowed (a user manages only their own nodes)
      • otherwise                    → denied (no cross-user fleet access)
    """
    if not tenant_user_id:
        return _resp(400, {"error": "tenant_user_id required"})
    if not RBAC_ENABLED:
        return None
    ident = _get_caller_identity(event or {})
    if ident.get("is_admin"):
        return None
    caller_user = ident.get("tenant_user_id")
    if caller_user and caller_user == tenant_user_id:
        return None
    return _resp(403, {"error": "forbidden: not authorized for this user's fleet"})


def _query_user_tenants(tenant_user_id, limit=None, next_token=None):
    """GSI-backed query for one user's tenants (indexed, never a full scan).

    Returns (items, next_token). Soft-deleted tenants are filtered out. Paginates
    via the gsi_tenant_user index; the cursor is opaque to callers.
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
    out = tenants_table.query(**kwargs)
    items = out.get("Items", []) or []
    return items, _encode_next_token(out.get("LastEvaluatedKey"))


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
    items, new_token = _query_user_tenants(
        tenant_user_id, limit=limit, next_token=next_token
    )
    for it in items:
        it.setdefault("tags", {})
    return _resp(200, {"tenants": items, "next_token": new_token, "count": len(items)})


def user_summary(tenant_user_id, event=None):
    """GET /users/{tenant_user_id}/summary — node count + per-status buckets (#57).

    Read-only roll-up for a backend dashboard / reconciliation. Pages through the
    GSI internally (projection is small enough; we only keep status) so the count
    is exact even past one page, but bounds the work to avoid an unbounded loop.
    """
    denied = _authorize_user_scope(tenant_user_id, event)
    if denied is not None:
        return denied
    by_status = {}
    total = 0
    next_token = None
    pages = 0
    while True:
        items, next_token = _query_user_tenants(
            tenant_user_id, limit=_USER_PAGE_MAX, next_token=next_token
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
    target_ids, next_token, pages = [], None, 0
    while True:
        items, next_token = _query_user_tenants(
            tenant_user_id, limit=_USER_PAGE_MAX, next_token=next_token
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
    body = json.loads(body) if isinstance(body, str) else body
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
    # Admin gate (RBAC already required operator+ for this non-viewer route).
    ident = _get_caller_identity(event or {})
    if RBAC_ENABLED and not ident.get("is_admin"):
        return _resp(
            403,
            {"error": "forbidden: fleet-power requires admin", "required": "admin"},
        )
    body = json.loads(body) if isinstance(body, str) else (body or {})
    action = body.get("action")
    if action not in _FLEET_VALID_ACTIONS:
        return _resp(
            400, {"error": f"action must be one of {sorted(_FLEET_VALID_ACTIONS)}"}
        )

    # All hosts that can hold VMs (active or idle). Strong read so a host that
    # JUST registered isn't missed (control-plane consistency, Phase 6).
    hosts = hosts_table.scan(
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
        resp = ssm.send_command(
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
            _out = tenants_table.scan(**_scan_kwargs)
            for _t in _out.get("Items", []):
                # Only the steady source state on an affected host (re-checked in
                # Python so tests don't depend on the mock honoring the filter).
                if _t.get("status") != source_status:
                    continue
                if _t.get("host_id") not in _host_id_set:
                    continue
                try:
                    tenants_table.update_item(
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

    _publish_event(
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


# ───────────── 控制面重构阶段1:SQS lifecycle 队列(削峰 + consumer) ─────────────


def enqueue_lifecycle(action, tenant_id, event, extra=None):
    """把一个 lifecycle 操作入 SQS,供 consumer 受控并发消费。返回 True=已入队。

    LIFECYCLE_QUEUE_URL 未配 → 返 False,调用方回退同步路径(向后兼容)。
    幂等:MessageDeduplicationId/_idem 用 tenant_id+action,SQS FIFO 或 consumer 侧去重。
    捎带调用者身份(#56:异步消费不绕过 RBAC),与 _enqueue_batch_job 同款。
    """
    if not sqs or not LIFECYCLE_QUEUE_URL:
        return False
    ident = _get_caller_identity(event or {})
    msg = {
        "action": action,
        "tenant_id": tenant_id,
        "extra": extra or {},
        # 重放调用者身份,consumer 据此重建最小 event 做 owner 检查
        "_ident": {
            "owner_id": ident.get("owner_id"),
            "is_admin": ident.get("is_admin"),
        },
        "_idem": f"{tenant_id}:{action}",
    }
    kwargs = {"QueueUrl": LIFECYCLE_QUEUE_URL, "MessageBody": json.dumps(msg)}
    # FIFO 队列(.fifo 结尾)需要 group/dedup id;标准队列忽略这俩
    if LIFECYCLE_QUEUE_URL.endswith(".fifo"):
        kwargs["MessageGroupId"] = tenant_id  # per-tenant 有序
        kwargs["MessageDeduplicationId"] = msg["_idem"]
    sqs.send_message(**kwargs)
    return True


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


def _execute_batch(action, target_ids, event):
    """Run one action over a list of tenant ids; return (succeeded, failed).

    Shared by the synchronous batch path and the async worker so both enforce
    the SAME per-id ownership (#80, via the threaded event) and failure
    isolation. delete routes to delete_tenant; everything else to tenant_action.
    """
    succeeded, failed = [], []
    for tid in target_ids:
        try:
            tenant = tenants_table.get_item(Key={"id": tid}, ConsistentRead=True).get(
                "Item"
            )
            if not tenant:
                failed.append({"id": tid, "error": "tenant not found"})
                continue
            if action == "delete":
                result = delete_tenant(tid, {}, event)
            else:
                result = tenant_action(tid, action, None, event)
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
    ident = _get_caller_identity(event or {})
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
    }
    # idempotent create: don't clobber an existing job with the same id
    try:
        batch_jobs_table.put_item(
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
        batch_jobs_table.update_item(
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
    if batch_jobs_table is None:
        return {"statusCode": 503, "body": "batch jobs not configured"}
    job = batch_jobs_table.get_item(Key={"job_id": job_id}).get("Item")
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
        }
    }
    batch_jobs_table.update_item(
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
        batch_jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET done = :d, succeeded = :s, failed = :f, updated_at = :t",
            ExpressionAttributeValues={
                ":d": len(succeeded) + len(failed),
                ":s": succeeded,
                ":f": failed,
                ":t": _now(),
            },
        )
    batch_jobs_table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "done", ":t": _now()},
    )
    return {"statusCode": 200, "body": f"job {job_id} done"}


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


def _resolve_filter(flt, event=None):
    """Convert filter dict → list of matching tenant ids (excludes soft-deleted)."""
    items = (
        tenants_table.scan(
            FilterExpression="#s <> :d",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": "deleted"},
        ).get("Items", [])
        or []
    )
    items = [it for it in items if it.get("status") != "deleted"]
    # issue #80 — owner scoping for non-admin batch callers.
    if RBAC_ENABLED:
        ident = _get_caller_identity(event or {})
        if not ident["is_admin"]:
            owner = ident["owner_id"]
            items = [it for it in items if owner and it.get("owner_id") == owner]
    tag_expr = flt.get("tag", "")
    if tag_expr and ":" in tag_expr:
        k, v = tag_expr.split(":", 1)
        items = [it for it in items if (it.get("tags") or {}).get(k) == v]
    elif tag_expr:
        items = []
    return [it["id"] for it in items]


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,x-api-key,Authorization",
        },
        "body": json.dumps(body, default=str),
    }


# #93 / api-design-review E1+E2 — structured error code. AWS Exceptions standard:
# clients MUST be able to distinguish errors in code without parsing the free-text
# message. `_err` attaches a stable machine-readable `code` alongside the existing
# `error` string. ADDITIVE + backward-compatible: old callers that read only
# `error` still work; the message text stays free to change, `code` is the contract.
# Prefer `_err(4xx, "CODE", "text")` over `_resp(4xx, {"error": "text"})` for new
# error returns; existing _resp error sites can migrate opportunistically.
def _err(code, error_code, message, extra=None):
    body = {"error": message, "code": error_code}
    if extra:
        body.update(extra)
    return _resp(code, body)


# ────────────────────────────────────────────────────────────
# Live VM resize (#35 / issue #16, original b3d48cf)
# ────────────────────────────────────────────────────────────


def tenant_resize(tenant_id, body):
    """POST /tenants/{id}/resize — hot-add vCPU on a running tenant."""
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body
    new_vcpu = body.get("vcpu")
    new_mem = body.get("mem_mb")
    if new_vcpu is None and new_mem is None:
        return _resp(400, {"error": "specify vcpu (memory live-resize not supported)"})
    if new_mem is not None:
        return _resp(
            400,
            {
                "error": "memory live-resize is not supported; "
                "stop the tenant, recreate with new mem_mb, then start"
            },
        )
    try:
        new_vcpu = int(new_vcpu)
    except (TypeError, ValueError):
        return _resp(400, {"error": "vcpu must be an integer"})
    item = tenants_table.get_item(Key={"id": tenant_id}, ConsistentRead=True).get(
        "Item"
    )
    if not item:
        return _resp(404, {"error": "tenant not found"})
    if item.get("status") != "running":
        return _resp(
            400, {"error": f"tenant must be running (current: {item.get('status')})"}
        )
    current_vcpu = int(item.get("vcpu", 0))
    if new_vcpu <= current_vcpu:
        return _resp(
            400,
            {
                "error": f"vcpu must be greater than current ({current_vcpu}); "
                "Firecracker cannot shrink — restart to decrease"
            },
        )
    quota_err = _check_quota(
        new_vcpu, int(item.get("mem_mb", 0)), int(item.get("data_disk_mb", 0))
    )
    if quota_err:
        return _resp(400, {"error": quota_err})
    host_id = item.get("host_id", "")
    if not host_id:
        return _resp(400, {"error": "tenant has no host assigned"})
    host = hosts_table.get_item(Key={"instance_id": host_id}, ConsistentRead=True).get(
        "Item"
    )
    if not host:
        return _resp(400, {"error": f"host {host_id} not found"})
    delta = new_vcpu - current_vcpu
    allocatable = int(int(host["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
    free = allocatable - int(host["used_vcpu"])
    if delta > free:
        return _resp(
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
    if not _ssm_run(host_id, cmd, timeout=30):
        return _resp(
            502, {"error": "Firecracker machine-config PATCH failed; tenant unchanged"}
        )
    now = _now()
    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET vcpu = :v, updated_at = :t",
        ExpressionAttributeValues={":v": new_vcpu, ":t": now},
    )
    hosts_table.update_item(
        Key={"instance_id": host_id},
        UpdateExpression="SET used_vcpu = used_vcpu + :v",
        ExpressionAttributeValues={":v": delta},
    )
    return _resp(
        200,
        {
            "id": tenant_id,
            "vcpu": new_vcpu,
            "mem_mb": int(item["mem_mb"]),
            "delta": delta,
        },
    )
