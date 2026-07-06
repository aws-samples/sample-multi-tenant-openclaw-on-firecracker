# Control Plane API Integration

This section is for integrators who want to embed the platform into their own systems (a customer backend, an operations console, or automation scripts). Every endpoint example below has been verified against a live deployment (a direct `curl` against the environment; responses are real machine output with credentials redacted). Following this section top to bottom lets you run the full path end to end: create a tenant → confirm it is running → send a message over the real-time channel → receive a reply.

> Throughout this document, `{BASE}` refers to the control plane API Gateway address (of the form `https://<api-id>.execute-api.<region>.amazonaws.com/v1`, supplied after deployment by `OC_DEFAULT_API_URL` in `console/config.js`). `{HUB}` refers to the public address of the data plane hub (reverse-proxied via Amazon CloudFront under `/hub/*`). Use your own deployment for the real account ID / domain / credentials; this document uses placeholders.

---

## 1. Authentication Model (the three-part authorization model)

The control plane runs three layers of validation on every request. Understand them before integrating:

1. **API Key (gateway layer, `x-api-key` header)** — validated by the API Gateway usage plan. A missing or wrong key always returns `403 {"message":"Forbidden"}`, and the request never reaches business logic (verified on a live machine: a missing key and a wrong key return the same response, indistinguishable, to prevent enumeration). Every control plane call must carry `x-api-key`.
2. **Amazon Cognito JWT (identity layer, `Authorization: Bearer <id_token>` header)** — for routes that operate "as a specific logged-in user" (such as self-service registration `POST /tenants/self`). A Cognito authorizer runs at the gateway plus JWKS signature verification inside AWS Lambda (RS256, checking issuer + expiry + client_id). Pure backend automation can use only the API Key on the administrator path (`owner_id = API_KEY_OWNER`, full visibility).
3. **RBAC + owner gating (authorization layer, inside Lambda)** — the validated Cognito `sub` becomes the `owner_id` stored on the tenant record; every per-tenant route enforces `owner == caller` (or admin / api-key). Cross-user access by a non-owner returns `403`.

The rule in one line: **use `x-api-key` for pure backend integration; add `Authorization: Bearer <Cognito id_token>` when you need to create/manage each platform user's own openclaw under their identity.**

The data plane (real-time chat) has its own separate token exchange, see §5.

### 1.1 RBAC Roles and Route Permissions (config-gated)

RBAC is controlled by the environment variable `RBAC_ENABLED` (on by default). Roles come in three levels `viewer < operator < admin`, derived from the Cognito `cognito:groups` claim (highest level taken). Authorization is checked after the route matches, so an unknown path still returns `404` rather than `403`. Fail-safe: with no Bearer token, the caller falls to `DEFAULT_NO_JWT_ROLE` (default `viewer`); if the token fails validation → `403`.

- **Skip RBAC (self-authenticating)**: `POST /external/authz` (HMAC signature), `GET /tenantmatch` (pre-login IdP routing lookup).
- **viewer suffices**: all read-only `GET` (list / detail / system info / audit / images / group reads / skill reads) + `POST /tenants/self` (self-service registration) + `POST /chat/sign` (viewer at the route layer, with additional owner gating inside the function).
- **operator and above**: all write operations (create / lifecycle / host management / skill write & delete / group write & delete / batch).
- **admin only**: `POST /hosts/fleet-power` (fleet-wide power on/off; requires operator at the route layer, with an additional admin check inside the function).

per-tenant routes (those with `{id}`) add owner gating on top of RBAC: a non-admin / non-api-key caller can only operate on tenants where `owner_id == their own sub`, otherwise `403` (to prevent IDOR).

---

## 2. Common Conventions

- **Content-Type**: `application/json` for write operations.
- **Error codes (structured)**: client errors return `4xx` + `{"error":"<human-readable message>","code":"<machine-readable code>"}`. Machine-readable codes already shipped: `VALIDATION` (invalid input), `CONFLICT` (idempotency key replay hitting an existing id). Branch on `code`, do not parse the `error` text.
- **Idempotency**: create operations support an optional `client_token` (4–128 printable ASCII characters). Replaying the same `client_token` (same owner) returns the same id, and the second call returns `409 CONFLICT`; no double creation. Omitting it creates a new resource each time (equivalent to EC2 RunInstances without ClientToken).
- **Pagination**: list operations support an optional `?limit=N` (positive integer, upper bound shown in the response). **Without `limit`, a bare array is returned (full set, legacy-compatible shape); with `limit`, a paginated object `{items…, next_token, count}` is returned.** An illegal `limit` (negative / 0 / non-integer) or a tampered `next_token` returns `400 VALIDATION` (it does not silently degrade). To page, pass the previous response's `next_token` back verbatim.
- **Asynchronous writes**: heavy operations (create / lifecycle stop-start / large batches) go through an Amazon SQS throttling queue and return `202` + status `queued` / a job id; you then poll for the result, so nothing blocks against the 30s API Gateway timeout.
- **Numeric fields are strings**: fields such as `vcpu`/`mem_mb`/`vm_count` in `/hosts` and `/tenants` are string-typed (for example `"95"`), so treat them as strings when parsing.

---

## 3. Endpoint Reference (each verified on a live machine)

### 3.1 System and Capacity (read-only)

**`GET {BASE}/system/info`** — a snapshot of system capability switches. Call this first before integrating to confirm the environment's shape.

```bash
curl -s -H "x-api-key: $KEY" "{BASE}/system/info"
```

Live response (excerpt):

```json
{
  "version": "1.5.5",
  "region": "<region>",
  "agentcore": { "enabled": false, "gateway_url": null },
  "metrics": { "enabled": true, "grafana_url": "<placeholder>" },
  "multi_az": { "enabled": true, "az_count": 2 },
  "waf": { "enabled": true },
  "cognito": {
    "enabled": true,
    "user_pool_id": "<placeholder>",
    "rbac_enabled": true
  },
  "notifications": { "enabled": true, "topic_arn": "<placeholder>" },
  "quotas": { "enabled": false },
  "host_config": {
    "cpu_overcommit_ratio": 6.0,
    "mem_overcommit_ratio": 1.5,
    "vm_default_vcpu": 2,
    "vm_default_mem_mb": 4096
  }
}
```

**`GET {BASE}/hosts`** — the host list and their capacity. Returns a bare array (excluding `__*` synthetic records).

```json
[
  {
    "instance_id": "i-xxx",
    "status": "active",
    "total_vcpu": "95",
    "used_vcpu": "8",
    "vm_count": "18",
    "total_mem_mb": "770018",
    "used_mem_mb": "21504",
    "rootfs_version": "v1.0",
    "az": "<az>",
    "private_ip": "<ip>",
    "next_vm_num": "36",
    "last_seen": "<ISO8601>"
  }
]
```

**`GET {BASE}/hosts/rootfs-version`** — returns `{"version":"v1.0"}` (the current live image version, i.e. the image a newly booted host starts with).
**`GET {BASE}/hosts/rootfs-drift`** — a reconciliation of each host's image version `{"current_version","up_to_date","unknown","stale_count","stale":[...]}`, showing which hosts have not yet rolled to the latest image (rolling-upgrade tracking).

The distinction among the three: `/images` (see §3.4) shows which artifacts are baked in Amazon S3 and which version is live; `/hosts/rootfs-version` only returns the live version number; `/hosts/rootfs-drift` shows whether the image version actually running on each host is aligned.

### 3.2 Tenant Lifecycle

**`POST {BASE}/tenants`** — create a tenant (spins up an independent openclaw microVM). Administrator/backend path (`x-api-key`, RBAC operator+).

```bash
curl -s -X POST -H "x-api-key: $KEY" -H "content-type: application/json" \
  -d '{"name":"acme","vcpu":1,"mem_mb":2048,"client_token":"acme-idem-0001"}' \
  "{BASE}/tenants"
```

Input parameters (all optional except `name`):

- `name` (required, identifier regex `^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`)
- `vcpu`/`mem_mb`/`data_disk_mb` (positive integers; a non-integer / negative / 0 → `400 VALIDATION`, no longer a 500)
- `image_id` (default `v2`), `config_template` (DNS-label regex)
- `security` (a nested encryption config Map, see §3.10)
- `client_token` (idempotency key, `^[\x21-\x7e]{4,128}$`)
- `chat_endpoint_enabled` (**JSON boolean**, default false; passing a string returns 400)
- `skills` (string array, per-tenant skill scope), `group` (group name, inherits group-level skills)
- `tags` (Map), `ttl_hours` + `on_expiry` (`delete`/`stop`), `schedule` (scheduled stop/start)
- `restore_from` / `clone_from` (derive from a backup or an existing tenant), `preferred_host_id`

> **Note** `owner_id` is set automatically from the caller's identity (Cognito `sub` or `API_KEY_OWNER`); passing `owner`/`owner_id` in the body is ignored. `tenant_user_id` (the stable id of an externally federated user) comes from the JWT's `custom:tenant_user_id` claim and is likewise not taken from the body — for external platform integration it is injected via Pre-Token-Generation (see the External Platform Integration chapter).

The return code depends on whether the deployment has the create throttling queue enabled (`CREATE_VIA_QUEUE`, the `scaler.create_via_queue` in `config.yml`, **off by default**):

- **Default (synchronous)**: with host capacity available, returns `201 {"id":"acme-xxxx","status":"creating",...}`; with no capacity, returns `201 {"id":"...","status":"pending"}` (automatically triggers scale-out, handled in the background).
- **With the throttling queue enabled (asynchronous)**: the request is enqueued into Amazon SQS for throttling, returning `202 {"id":"acme-xxxx","status":"queued","message":"create accepted; provisioning asynchronously"}`. Enabling it is recommended when creating tenants at scale (see the SSM concurrency note in the deployment planning chapter).

With `client_token`, the id is determined by `(owner, client_token)`, and replaying the same key returns `409 CONFLICT`. In both modes you then **poll `GET /tenants/{id}` until `status:running`**.

**`POST {BASE}/tenants/self`** — self-service registration (create your own node under the logged-in user's identity). Requires `Authorization: Bearer <Cognito id_token>` + `x-api-key` (RBAC viewer+). `owner_id` is forced to the caller's validated `sub` (the body cannot designate an owner for someone else), gated by the per-user node cap `SELF_MAX_NODES_PER_USER` (default 1, 0=unlimited); exceeding the cap returns `409`. When `EXTERNAL_AUTHZ` is enabled, this endpoint is rejected outright (the authorization decision is handed to the external backend). This is the entry point for users of an external SaaS platform to create their own openclaw (for the federation approach see the External Platform Integration chapter).

**`GET {BASE}/tenants`** — the tenant list (RBAC viewer+; a non-admin only sees the tenants they own).

- No parameters: a bare array (full set). Each entry is the tenant's DDB record projected with server-side credential fields removed, containing `id/name/status/owner_id/host_port/guest_ip/vm_health/app_health` and similar (`vm_health`/`app_health` are written by the health_check Lambda and may not yet appear on a freshly created tenant before its first health check).
- Pagination: `GET {BASE}/tenants?limit=5` → `{"tenants":[…],"next_token":"<opaque>","count":<count on this page>}`.
- Boundaries (verified on a live machine): `?limit=-1` → `400 {"code":"VALIDATION","error":"limit must be a positive integer (>= 1)"}`; `?next_token=garbage` → `400 {"code":"VALIDATION","error":"next_token is invalid or expired"}`.
- Optional `?tag=key:value` to filter by tag (can be repeated, AND semantics).
  > **Important** The list response may currently contain tenant-level credential fields; an integrator **must not pass the response through verbatim to an untrusted frontend**; credentials are for server-side use only.

**`GET {BASE}/tenants/{id}`** — a single tenant's detail (owner-gated).

**`POST {BASE}/tenants/{id}/{action}`** — lifecycle actions (RBAC operator+ + owner gating). Supported `action` values:

| action                          | semantics                                                                                 | returns                                                 |
| ------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| `start`                         | wake (~3.7s)                                                                              | 202 async (enqueued when LIFECYCLE_QUEUE is configured) |
| `stop`                          | hibernate (~6.0s)                                                                         | 202 async                                               |
| `restart` / `reset` / `rebuild` | restart / reset / rebuild with a new image                                                | 202 async                                               |
| `pause` / `resume`              | pause / resume                                                                            | 202 async                                               |
| `backup`                        | back up to Amazon S3 (~6.6s)                                                              | 202, asynchronously invokes the backup Lambda           |
| `resize`                        | hot-change vCPU (requires body `vcpu`)                                                    | 200 `{old_vcpu,new_vcpu}`                               |
| `resize-disk`                   | offline-expand the data disk (requires body `new_size_mb`)                                | 200                                                     |
| `migrate`                       | migrate to a target host (requires body `target_host_id`)                                 | 202 `{status:"migrating",snapshot_uri,poll}`            |
| `access`                        | read/write the authorization ledger (body `principal`+`op:grant\|revoke`+optional `role`) | 200                                                     |

For an already-deleted tenant there is a state guard against revival. An unknown action returns `400`.

**`GET {BASE}/tenants/{id}/{action}`** — tenant-scoped read-only (RBAC viewer+ + owner gating). Supported `action` values:

- `backups` — the backup list for this tenant.
- `data` — a tenant data snapshot (**metadata only, zero credentials**: `id/status/host_id/guest_ip/vm_num/vcpu/mem_mb/data_disk_mb/rootfs_version/effective_skills/group/schedule/ttl_hours/expires_at/owner_id/tenant_user_id/has_billing_vkey/backup_count/created_at/updated_at/tags`; `has_billing_vkey` reports only whether a billing key exists, never its value), so operations can see the tenant's state clearly without entering the guest.
- `access` — the explicit authorization ledger `{owner_id, authorized_users:{sub:{role,granted_at,expire_at}}}`.

**`DELETE {BASE}/tenants/{id}`** — deregister (~12.2s, RBAC operator+ + owner gating).

- Default `?keep_data=true`: a soft delete, **retaining the data disk**.
- `?keep_data=false`: delete the data disk, and before deleting **automatically sync a backup to Amazon S3** (unless you also pass `?skip_backup=true` to explicitly skip it).
- Before deleting, the platform conditionally rolls back the host capacity. Live response `200 {"id":"...","status":"deleted"}`. Idempotent for an already-deleted tenant (a repeat delete returns the already-deleted state).

### 3.3 Batch and User Dimension

**`POST {BASE}/batch/tenants`** — apply one action to multiple tenants in batch (RBAC operator+). body: `{action:"start|stop|delete|backup", ids:[...] or filter:{tag:"k:v"}, async?:true}`. `ids` and `filter` are mutually exclusive; `ids` has an upper bound of 100000. Synchronous (≤100 and not async) returns `200 {succeeded,failed}`; over 100 or `async:true` returns `202 {job_id,status:"queued"}`. For a non-admin, the filter only resolves tenants they own.
**`GET {BASE}/batch/jobs/{job_id}`** — query the progress of an asynchronous job (RBAC viewer+): `{job_id,action,status,total,done,succeeded:[...],failed:[{id,error}],created_at,updated_at}`.
**`GET {BASE}/users/{tenant_user_id}/tenants`** — query all nodes owned by a platform user (a GSI index query, not a full-table scan; pagination as in §2).
**`GET {BASE}/users/{tenant_user_id}/summary`** — that user's node count + buckets by status `{total, by_status, truncated}`.
**`POST {BASE}/users/{tenant_user_id}/action`** — batch `start`/`stop` all of that user's nodes, returning `{succeeded:[...],failed:[...],truncated}`.

These three `/users/*` endpoints are the core interfaces for "managing thousands of openclaw associated by platform user": the target set comes from the GSI (not a client-supplied id list), so the backend says "stop this user" and the platform resolves their nodes. Owner gating: a federated user can only manage their own fleet; admin/api-key manages all.

### 3.4 Backups, Audit, Images

**`GET {BASE}/backups`** — the full backup list (across all tenants; a left join against the tenant table marks orphan backups `exists:false`).
**`GET {BASE}/audit-log`** — the audit log (reverse chronological, `?limit=` default 50, upper bound 500, optional `?since=<ISO8601>`). Each entry contains `method/resource/actor/target_id/status_code/ts/error`. All write operations (POST/PUT/DELETE) are audited automatically.

**`GET {BASE}/images`** — the golden image artifact list + the current live version. Read-only: enumerates all artifacts under the S3 `rootfs/` prefix (rootfs / data-template / kernel / integrity baseline / manifest) and reports which version `manifest.json` currently points to. **Returns artifact metadata only (name/size/time/kind); it does not download or expose the image bytes**, so operations can see what was baked and which version is live without SSHing into a host.

```bash
curl -s -H "x-api-key: $KEY" "{BASE}/images"
```

```json
{
  "live_version": "v1.0",
  "manifest": { "version": "v1.0", "...": "verbatim manifest.json content" },
  "artifact_count": 5,
  "artifacts": [
    {
      "name": "openclaw-rootfs-v1.0.erofs",
      "kind": "rootfs",
      "size_bytes": 707788800,
      "last_modified": "2026-06-30T12:00:00+00:00",
      "is_backup": false
    }
  ]
}
```

Fields: `live_version` (the version `manifest.json` points to, `"unknown"` if not obtainable), `manifest` (verbatim content, `{}` if not obtainable), `artifact_count`, `artifacts[]` sorted by `(kind, name)`, each containing `name`, `kind`, `size_bytes`, `last_modified` (ISO8601, `null` if missing), `is_backup` (`true` if the name contains `.bak`). `kind` enum: `rootfs` (the read-only root filesystem disk), `data-template` (the data disk template), `kernel` (`vmlinux`), `integrity-baseline` (`golden-image.sha256`), `manifest`, `other`. Errors: `ASSETS_BUCKET` not configured → `503`; S3 listing failure → `500`.

### 3.5 Host Management (operations)

**`POST {BASE}/hosts`** — register an EC2 instance as a Firecracker host (RBAC operator+). body `{instance_id:"i-..."}`. The platform runs DescribeInstances to obtain vCPU/memory/AZ, accounts for it after deducting `HOST_RESERVED_*`, and returns `201 {instance_id,status:"active",az}`.
**`DELETE {BASE}/hosts/{instance_id}`** — take a host offline (RBAC operator+). It is not deleted directly; the host is marked `status:draining` and an ASG lifecycle hook is triggered, which cleans up all tenants on it (mark-deleted) before terminating the instance. Returns `200 {instance_id,status:"draining"}`.
**`POST {BASE}/hosts/refresh-rootfs`** — from `manifest.json`, push the latest rootfs + data template + read-only disk via SSM to all active/idle hosts (RBAC operator+), asynchronously updating each host's `rootfs_version`. No body. Returns `{message:"refresh started",version,hosts:[...]}`.
**`POST {BASE}/hosts/fleet-power`** — **fleet-wide power on/off**: across all active hosts, via a host-local fan-out, start/stop every microVM on them at once (targeting a 1-minute fleet power on/off). body `{action:"start|stop"}`. **admin only** (operator required at the route layer, with an additional admin check inside the function, defense in depth). Returns `202 {action,hosts,command_id,reconciled,status:"dispatched"}`, and automatically reconciles the state of steady-state tenants (start: stopped→running; stop: running→stopped), without touching transitional states.

### 3.6 Groups and Skills (console operations)

**`GET {BASE}/groups`** — the group list (viewer+): `{groups:[{name,skills:[...],description,created_at}]}`.
**`POST {BASE}/groups`** — create a group (operator+): body `{name (same regex as tenant),skills:[...],description?}`, returns `201`, a duplicate name `409`.
**`POST {BASE}/groups/{name}/skills`** — add a skill to a group (operator+): body `{skill}`, idempotent, returns the updated skill list.
**`DELETE {BASE}/groups/{name}/skills/{skill}`** — remove a skill from a group (operator+), returns the remaining skill list.

**`GET {BASE}/skills/{name}`** — read a skill's `SKILL.md` content (viewer+): `{name,content,size,last_modified}`, `404` if it does not exist.
**`PUT {BASE}/skills/{name}`** — write/create a skill (operator+): body `{content}`, must be UTF-8, ≤256KB, and contain at least one top-level `# heading`; returns `200` (already existed) or `201` (newly created); over the limit `413`, non-conforming format `400`.
**`DELETE {BASE}/skills/{name}`** — delete a skill (operator+): returns `{name,deleted:<number of files deleted>}`.

> **Note** A skill name is limited to lowercase letters + digits + hyphens; an illegal name returns `400`. Skill changes land in the image artifact layer and take effect on the next image rebuild / refresh-rootfs, not by hot-patching a running VM (in keeping with the architectural rule "change the image and rebuild, do not hot-patch a live VM").

### 3.7 AgentCore (read-only, config-gated)

**`GET {BASE}/agentcore/status`** (viewer+) — the AgentCore gateway enablement status `{enabled, gateway_url}`.
**`GET {BASE}/agentcore/tools`** (viewer+) — the MCP tool list registered to the AgentCore gateway `{enabled, tools:[{name,description,input_schema}]}`. When not enabled, `enabled:false`.

### 3.8 Federated IdP Routing (pre-login lookup)

**`GET {BASE}/tenantmatch?platform_id=<id>`** — an external platform → Cognito upstream IdP routing lookup (**skips RBAC**, no identity before login). Given a platform identifier it returns `{platform_id, idp_provider_name, issuer_url}`, and the frontend uses this to redirect the user straight to the corresponding upstream IdP. `platform_id` regex `[a-zA-Z0-9._-]{1,128}`, illegal `400`; IdP federation not configured (`TENANT_IDP_TABLE` unset) `404 NOT_CONFIGURED`; platform not registered `404 NOT_FOUND`; DynamoDB failure `502`. **A pre-login lookup that leaks no tenant data, doing only platform → IdP routing.**

### 3.9 Authorization Integration (external backend)

**`POST {BASE}/external/authz`** — an external backend pushes "user ↔ tenant" authorization mappings (**skips RBAC, self-authenticated with an HMAC signature**, in effect when `EXTERNAL_AUTHZ` is enabled). Used to hand the authorization decision to the customer's own platform: the platform says "user X may access tenant Y", this is written into `authorized_users`, and the data plane hub admits based on it.

- Signature headers: `x-claw-authz-signature` (= HMAC-SHA256(secret, `"{timestamp}.{raw_body}"`)) + `x-claw-authz-timestamp` (unix seconds, ±`EXTERNAL_AUTHZ_TS_WINDOW` default 300s, replay protection).
- body: `{tenant_id, principal, op:"grant"|"revoke", role?, expire_at?}`.
- Returns `200 {id,op,principal}`. Errors: `EXTERNAL_AUTHZ` not enabled `404`; secret not configured `503`; invalid signature/timestamp `401`; missing/invalid input `400`; tenant does not exist `404`.

> **Note** Current live state: on a default deployment where `EXTERNAL_AUTHZ` is not enabled, this route returns `404`. To use it, enable `EXTERNAL_AUTHZ` in the stack + configure `EXTERNAL_AUTHZ_SECRET`. This document marks it as a config-gated capability.

### 3.10 The `security` Nested Encryption Config (optional at creation)

The `security` field of `POST /tenants` is a named sub-object (aligned with the S3 ServerSideEncryptionConfiguration idiom, not `env` — `env` in AWS convention specifically means environment variables):

```json
{
  "security": {
    "storage_encrypted": true,
    "encryption_type": "tenant_cmk",
    "kms_key_arn": "arn:aws:kms:<region>:<acct>:key/<id>",
    "cert_arn": "arn:aws:acm:...",
    "secret_ref": "arn:aws:secretsmanager:...:secret:..."
  }
}
```

Invariants (a violation returns `400 VALIDATION`): `storage_encrypted:false` cannot carry a key; `encryption_type:tenant_cmk` must supply a `kms_key_arn`; referencing an external resource must be a **full ARN** (a bare id/alias resolves to the wrong key across accounts). `secret_ref` stores an AWS Secrets Manager ARN (a reference, not the secret value).

---

## 4. Data Plane: Exchange a Frontend Token + Message Signing (hub)

The data plane connects "browser/frontend ⇄ the user's own openclaw" through the hub (the WS central relay). The frontend does not connect directly to the microVM (the VM opens no inbound port); instead both sides make outbound connections that meet at the hub.

**`POST {HUB}/hub/token`** — the frontend exchanges a Cognito `id_token` for a hub short-lived token.

```bash
curl -s -X POST -H "Authorization: Bearer <cognito_id_token>" \
  -H "content-type: application/json" -d '{"tenant_id":"acme-xxxx"}' "{HUB}/hub/token"
```

On the hub side: JWKS signature verification (`token_use=id` + audience) + `authorizeSubForTenant(sub, tenant_id)` looking up `owner_id`/`authorized_users`. On success it returns `{"token":"<frontend short-lived token>","expires_in":300}`. The short-lived token claim = `{role:"frontend", sub:<validated>, tenant, access, exp:+300s}`, HMAC-signed (the key is shared as multiple replicas via AWS Secrets Manager). A 403 means the sub has no permission to access the tenant.

**`POST {HUB}/channel-token`** — the microVM outbound side (claw-channel) proving it is a given tenant: a Cognito machine-user access token (the `username` claim = the tenant, cannot be forged), exchanged for an equivalent `{role:"channel"}` short-lived token. An integrator usually does not need to call this directly (it is done automatically by the channel inside the VM).

**`POST {BASE}/chat/sign`** — on the control plane side, sign a C-side message envelope, delivered to a per-VM webhook as a fallback side path (RBAC viewer+, with owner/admin gating inside the function). Requires `Authorization: Bearer <Cognito id_token>` + body `{tenant_id, text}` (`text` ≤8000 characters). Returns `{path:"/chat/{tenant_id}/inbound", body:"<signed envelope>", headers:{x-claw-signature, x-claw-random, x-claw-timestamp}}`, signed by the HMAC-derived `channel_secret`. If the tenant's channel key is not yet ready (the VM is still booting), returns `409`. Day-to-day real-time chat goes over the WebSocket in §5 and does not need to call this endpoint directly.

**Files**: `POST {HUB}/files/upload-url` (a MIME allowlist + size) returns an S3 pre-signed PUT; `GET {HUB}/files/download-url?fileKey=` with a tenant-segment guard (the second segment of fileKey must == the caller's tenant, to prevent cross-tenant IDOR) returns a pre-signed GET.

---

## 5. WebSocket Real-Time Chat

1. The frontend obtains a frontend short-lived token per §4.
2. Connect: `wss {HUB}/hub/ws?token=<frontend short-lived token>` (via CloudFront `/hub/*` → ALB → hub). The hub validates the token, stamps `_tenant/_sub`, and registers it in the frontends table. After connecting, the hub sends a protocol-level PING keepalive every 25s (to withstand idle disconnection during the agent's cold start).
3. Send a message (frame shape):

```json
{
  "operationType": "msg_create",
  "parts": [{ "kind": "TEXT", "text": "Hello" }],
  "threadId": "<thread id, regex ^[A-Za-z0-9._:-]+$ length ≤80>",
  "clientMessageId": "<frontend correlation id>"
}
```

The hub sets `senderId` to the **server-validated Cognito sub** (it does not trust the client's self-report, to prevent impersonation) and delivers it to that tenant's channel → openclaw inference inside the microVM. 4. Receive the reply: `type:"reply_delta"` or `operationType:"msg_update"` streaming increments (the frontend replaces the bubble by `clientMessageId`). The hub only delivers to all tabs of that sub whose `_tenant` matches (cross-tenant isolation). 5. History: send `type:"history_request"` → receive `type:"history_reply"` (a messages array).

**Cross-tenant isolation (structural, not trusting the client's self-report)**: ① the frontend short-lived token is bound to a single tenant ② the channel proves its tenant identity via the username claim of the Cognito access token ③ matching happens only when `fws._tenant === frame._tenant` + senderId uses the server-validated sub + authorization looks up `owner_id`/`authorized_users`.

---

## 6. Quick Start: Running End to End (from the customer's perspective)

```bash
export KEY="<your x-api-key>"
export BASE="https://<api-id>.execute-api.<region>.amazonaws.com/v1"

# 1) Confirm the environment
curl -s -H "x-api-key: $KEY" "$BASE/system/info" | python3 -m json.tool

# 2) Create a tenant (with an idempotency key)
curl -s -X POST -H "x-api-key: $KEY" -H "content-type: application/json" \
  -d '{"name":"quickstart","client_token":"qs-0001"}' "$BASE/tenants"
# → Default synchronous: 201 {"id":"quickstart-xxxx","status":"creating"}
# → With the throttling queue: 202 {"id":"quickstart-xxxx","status":"queued"}

# 3) Poll until running
curl -s -H "x-api-key: $KEY" "$BASE/tenants/quickstart-xxxx"
# → until {"status":"running","vm_health":"up","app_health":"up"}

# 4) Frontend exchanges a hub token (browser/frontend, requires a Cognito login to obtain an id_token)
#    POST {HUB}/hub/token  Bearer <id_token> + {"tenant_id":"quickstart-xxxx"}
#    → {"token":"<frontend short-lived token>","expires_in":300}

# 5) Open a wss, send a message, receive a reply
#    wss {HUB}/hub/ws?token=<frontend short-lived token>
#    send {operationType:msg_create, parts:[{kind:TEXT,text:"..."}], threadId, clientMessageId}
#    receive reply_delta streaming reply
```

The control plane steps (1–3) can be run with pure `curl`; the data plane (4–5) needs a Cognito login session + a WS client (refer to the chat UI). The measured end-to-end first reply is about 27s (including the agent's cold start).

---

## 7. Appendix: Endpoint Quick Reference

| endpoint                                               | method         | auth / RBAC                | purpose                                                                                |
| ------------------------------------------------------ | -------------- | -------------------------- | -------------------------------------------------------------------------------------- |
| `/system/info`                                         | GET            | api-key · viewer           | system capability snapshot                                                             |
| `/hosts` `/hosts/rootfs-version` `/hosts/rootfs-drift` | GET            | api-key · viewer           | host and image reconciliation                                                          |
| `/hosts`                                               | POST           | operator                   | register a host                                                                        |
| `/hosts/{instance_id}`                                 | DELETE         | operator                   | take a host offline (draining)                                                         |
| `/hosts/refresh-rootfs`                                | POST           | operator                   | push the latest image to hosts                                                         |
| `/hosts/fleet-power`                                   | POST           | **admin**                  | fleet-wide power on/off                                                                |
| `/images`                                              | GET            | viewer                     | image artifact list                                                                    |
| `/tenants`                                             | GET            | viewer (own only)          | tenant list (bare-array / paginated dual shape)                                        |
| `/tenants`                                             | POST           | operator                   | create a tenant                                                                        |
| `/tenants/self`                                        | POST           | Bearer · viewer            | self-service registration (user identity)                                              |
| `/tenants/{id}`                                        | GET            | owner-gated                | single tenant detail                                                                   |
| `/tenants/{id}/{action}`                               | GET            | owner-gated                | backups / data / access (read-only)                                                    |
| `/tenants/{id}/{action}`                               | POST           | operator + owner           | start/stop/restart/pause/resume/reset/rebuild/backup/resize/resize-disk/migrate/access |
| `/tenants/{id}`                                        | DELETE         | operator + owner           | deregister (keep_data optional)                                                        |
| `/batch/tenants` `/batch/jobs/{id}`                    | POST/GET       | operator / viewer          | batch and job progress                                                                 |
| `/users/{uid}/tenants` `/summary` `/action`            | GET/POST       | viewer (own only)          | manage a fleet by platform user                                                        |
| `/backups` `/audit-log`                                | GET            | viewer                     | backups and audit                                                                      |
| `/groups` `/groups/{name}/skills`                      | GET/POST       | viewer / operator          | groups and group-level skill                                                           |
| `/groups/{name}/skills/{skill}`                        | DELETE         | operator                   | remove a group-level skill                                                             |
| `/skills/{name}`                                       | GET/PUT/DELETE | viewer / operator          | skill content CRUD                                                                     |
| `/agentcore/status` `/agentcore/tools`                 | GET            | viewer                     | AgentCore gateway status/tools (config-gated)                                          |
| `/tenantmatch`                                         | GET            | none (pre-login)           | platform → IdP routing lookup                                                          |
| `/external/authz`                                      | POST           | HMAC                       | external authorization push (config-gated, off by default)                             |
| `/chat/sign`                                           | POST           | Bearer · viewer + owner    | C-side message signing (fallback side path)                                            |
| `{HUB}/hub/token` `/channel-token`                     | POST           | Bearer / Cognito access    | data plane token exchange                                                              |
| `{HUB}/hub/ws`                                         | WSS            | frontend short-lived token | real-time chat                                                                         |
| `{HUB}/files/upload-url` `/download-url`               | POST/GET       | hub token                  | file pre-signing (tenant-segment guard)                                                |

> Verification sources: the control plane routes and RBAC levels come from the route table in `deploy/lambda/api/handler.py` (the `routes` dictionary) + the `_VIEWER_OK`/`_RBAC_SKIP`/`_rbac_check` definitions; endpoint behavior was verified with live `curl` against the deployment environment (evidence in `engineering/00-knowledge-base/evidence/`); the hub/wss parameters come from `SPEC/03-HUB-SPEC.md` + `deploy/hub/server.mjs`. The pagination page-size semantics and the AgentCore tool list follow the actual deployment configuration.
