# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/utils — 纯叶子工具层(handler-split #132 Phase1)。

从 handler.py 机械搬迁,函数体逐字不变。只依赖 stdlib(re/json/base64/time/hashlib + 函数内 datetime/zoneinfo)。
按 design.md 层间契约:本层不 import 仓内任何东西(最底叶子)。
facade:handler.py re-export 全部符号,旧 patch/调用路径全程有效。
"""

import base64
import hashlib
import json
import re
import time


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


# #118/#116 — credential injection (in-transit encrypted). Distinct from
# `security` (that is at-rest storage config). Env var names per POSIX + the
# dotenv the host writes to the read-only creds disk (see launch-vm.sh).
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# #118 安全评审 MEDIUM 修复:一条注入凭据最终落成 guest 的 dotenv NAME=value 行,
# OpenClaw 原生 dotenv loader 灌进 process.env,agent 的 exec 子进程继承。有些 env
# NAME 会在解释器/shell 启动早期执行代码 —— 在 in-guest 护栏(sentinel/acl-guard)加载
# 前就跑,等于预护栏 RCE。cred-inject.sh 的 value 侧已花力气拒控制字符防同一 NODE_OPTIONS
# 威胁(注释白纸黑字),name 侧必须对称设防:拒这些危险名(前缀 LD_ 覆盖 LD_PRELOAD/
# LD_LIBRARY_PATH 等一族)。判据:这个 name 会不会让进程在业务代码前执行任意代码。
_DANGEROUS_ENV_NAMES = frozenset(
    {
        "NODE_OPTIONS",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "PYTHONHOME",
        "PERL5OPT",
        "RUBYOPT",
        "GIT_SSH_COMMAND",
        "IFS",
        "PATH",
    }
)
_DANGEROUS_ENV_PREFIXES = ("LD_",)  # LD_PRELOAD / LD_LIBRARY_PATH / LD_AUDIT ...
_MAX_INJECTED_ITEMS = 32
_MAX_CIPHERTEXT_LEN = 8192  # base64 of a KMS blob for a small secret; generous ceiling
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _validate_injected_credentials(raw, configured_cmk_arn=""):
    """Validate + normalize the optional `injected_credentials` map. Returns
    (clean | None, err). Absent → (None, None): unchanged behavior.

    `configured_cmk_arn` is the stack's ClawPool CMK ARN (clients.CLAWPOOL_CMK_ARN),
    passed in because this leaf module can't import clients. It is the real feature
    gate:
      • empty → the credential-injection feature is OFF (config gate off / no CMK
        deployed) → any injection attempt is REFUSED (not silently stored against a
        key the host can't decrypt).
      • non-empty → the caller's kms_key_arn MUST equal it, so a tenant can't get
        the host to kms:Decrypt against an arbitrary attacker-controlled key.

    Contract (see DESIGN-credential-kms-injection-e2e):
      • kms_encrypted MUST be true — the platform encrypts each value with the
        ClawPool CMK *before* the API Gateway; we never accept plaintext creds.
      • kms_key_arn is a full ARN and must match the stack's ClawPool CMK.
      • items is a non-empty list (≤ _MAX_INJECTED_ITEMS) of
        {name: ENV_VAR, ciphertext: base64}. name must be a POSIX env var name
        (anti-injection: it lands in a dotenv line NAME=value on the host).
      • ciphertext is base64 within a size ceiling. We do NOT decrypt here
        (guest zero-credential baseline: the API has no kms:Decrypt) — only the
        host decrypts at VM launch.
    The returned map is stored verbatim on the tenant record (still ciphertext).
    """
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "injected_credentials must be an object"
    if not configured_cmk_arn:
        return (
            None,
            "injected_credentials not supported: credential-injection CMK is not "
            "configured (security.clawpool_cmk_enabled is off)",
        )
    if raw.get("kms_encrypted") is not True:
        return (
            None,
            "injected_credentials.kms_encrypted must be true (plaintext injection is refused)",
        )
    key_arn = (raw.get("kms_key_arn") or "").strip()
    if not key_arn or not _ARN_RE.match(key_arn):
        return None, "injected_credentials.kms_key_arn must be a full KMS key ARN"
    if key_arn != configured_cmk_arn:
        return (
            None,
            "injected_credentials.kms_key_arn must match the stack's ClawPool CMK "
            "(host can only decrypt against that key)",
        )
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        return None, "injected_credentials.items must be a non-empty array"
    if len(items) > _MAX_INJECTED_ITEMS:
        return None, f"injected_credentials.items exceeds {_MAX_INJECTED_ITEMS} entries"
    clean_items = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            return None, "each injected_credentials item must be an object"
        name = it.get("name")
        ct = it.get("ciphertext")
        if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
            return None, "item.name must match ^[A-Z_][A-Z0-9_]*$ (POSIX env var name)"
        # #118 安全评审 MEDIUM:拒会在业务代码/护栏加载前执行代码的危险 env 名
        # (NODE_OPTIONS/LD_PRELOAD/BASH_ENV 等),否则等于 guest 预护栏 RCE 面。
        if name in _DANGEROUS_ENV_NAMES or any(
            name.startswith(p) for p in _DANGEROUS_ENV_PREFIXES
        ):
            return (
                None,
                f"item.name '{name}' is a disallowed env var that could execute "
                "code before in-guest guards load (NODE_OPTIONS/LD_*/BASH_ENV/...)",
            )
        if name in seen:
            return None, f"duplicate injected credential name: {name}"
        seen.add(name)
        if not isinstance(ct, str) or not ct:
            return None, f"item.ciphertext for {name} must be a non-empty base64 string"
        if len(ct) > _MAX_CIPHERTEXT_LEN or not _B64_RE.match(ct):
            return (
                None,
                f"item.ciphertext for {name} must be base64 within {_MAX_CIPHERTEXT_LEN} chars",
            )
        clean_items.append({"name": name, "ciphertext": ct})
    return {"kms_encrypted": True, "kms_key_arn": key_arn, "items": clean_items}, None


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_TAG_MAX_KEY_LEN = 50
_TAG_MAX_VALUE_LEN = 100
_TAG_MAX_COUNT = 20
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

# #106/#108 — external-platform id validator. Shared by core.auth
# (_platform_id_from_claims) and the tenant CRUD/query paths (list_tenants
# ?platform_id filter, create_tenant body override, tenant_access_grant).
# Kept here in core.utils (with the other validation regexes) so it has a
# single home usable by both the auth leaf and the tenant service (#132 T1.1).
# \Z 而非 $:$ 在 re.match 下也匹配「末尾换行前」,`"plat\n"` 会被 $ 放行(尾换行绕过
# 校验)。\Z 只匹配绝对末尾。#106 起 platform_id 除 /tenantmatch(#97)外还落租户记录 +
# 供 ?platform_id 筛选,尾换行绕过会让「能落库却筛不回」,故收紧为 \Z(纯加固,合法值行为不变)。
_PLATFORM_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,128}\Z")

# #143 — validators for the api-key create-on-behalf attribution override.
# owner_id must be a Cognito sub: a user-pool sub is always a UUID (native and
# federated alike), and every owner check downstream compares owner_id == sub,
# so anything else could never match a caller — reject at the edge. \Z (not $)
# so a trailing newline can't ride through into logs / DDB.
_COGNITO_SUB_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
# tenant_user_id is the EXTERNAL platform's user id (not a Cognito sub — the
# whole point is attributing users who may never touch our Cognito), so shape
# is platform-defined; constrain to printable ASCII, no spaces/control chars
# (anti injection / log poisoning — same family as _CLIENT_TOKEN_RE/_ORDER_ID_RE).
_TENANT_USER_ID_RE = re.compile(r"^[\x21-\x7e]{1,128}\Z")


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


_USER_PAGE_DEFAULT = 100  # default page size for paginated listings
_USER_PAGE_MAX = 1000  # hard cap so one call can't pull the whole table


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


def _err(code, error_code, message, extra=None):
    body = {"error": message, "code": error_code}
    if extra:
        body.update(extra)
    return _resp(code, body)
