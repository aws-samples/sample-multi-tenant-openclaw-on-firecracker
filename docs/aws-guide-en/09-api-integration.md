# Control Plane API Integration

This section is for integrators who want to embed the platform into their own systems (a customer backend, an operations console, or automation scripts). The control plane endpoint examples below have been verified against a live deployment (a direct `curl` against the environment; responses are real machine output with credentials redacted).

> This chapter defines only the control plane REST API. Real-time chat uses the
> platform backend `/gw/ws` and two-tier routing. There is no current
> `{HUB}/hub/*` integration contract; see Chapter 13.

> Throughout this document, `{BASE}` refers to the control plane API Gateway address (of the form `https://<api-id>.execute-api.<region>.amazonaws.com/v1`, supplied after deployment by `OC_DEFAULT_API_URL` in `console/config.js`). Use your own deployment for the real account ID / domain / credentials; this document uses placeholders.

> **Machine-readable contract**: the field-level, per-parameter contract for every endpoint below lives in `../aws-guide/openapi.yaml` (OpenAPI 3.1, 38 paths). Browse it interactively with `swagger.html` in that same directory: `cd docs/aws-guide && python3 -m http.server`, then open `swagger.html`. Where this prose and `openapi.yaml` disagree, treat `openapi.yaml` as the field-level source of truth.

---

## 1. Authentication Model (the three-part authorization model)

The control plane applies three layers:

1. **Usage-plan identity (`x-api-key`)**. Every current API Gateway method requires this header; a missing or invalid value returns `403`. AWS explicitly states that API keys must not be used as authentication or authorization, and usage-plan throttles and quotas are best effort.
2. **Identity (`Authorization: Bearer <id_token>`)**. When Amazon Cognito is enabled, Lambda verifies RS256, issuer, expiration, and the configured client id through JWKS, then derives the caller and `cognito:groups` role.
3. **Authorization in Lambda**. RBAC controls viewer/operator/admin route access; owner and platform-scope checks constrain resources. An invalid Bearer token never degrades into the trusted API-key-only identity.

The default configuration maps API-key-only requests to `viewer`, so they can call read routes only. A deployment that permits trusted internal automation to write with only `x-api-key` must explicitly set `console_auth.default_no_jwt_role` to `operator` or `admin` and add a real boundary such as private networking, IAM, or a Lambda authorizer.

AWS reference: [API Gateway API key and usage-plan best practices](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-api-usage-plans.html).

### 1.1 RBAC Roles and Route Permissions (config-gated)

RBAC is controlled by the environment variable `RBAC_ENABLED` (on by default). Roles come in three levels `viewer < operator < admin`, derived from the Cognito `cognito:groups` claim (highest level taken). Authorization is checked after the route matches, so an unknown path still returns `404` rather than `403`. Fail-safe: with no Bearer token, the caller falls to `DEFAULT_NO_JWT_ROLE` (default `viewer`); if the token fails validation → `403`.

- **Skip RBAC (self-authenticating)**: `POST /external/authz` (HMAC signature), `GET /tenantmatch` (pre-login IdP routing lookup).
- **viewer suffices**: all read-only `GET` routes plus `POST /tenants/self` (self-service registration).
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

**`POST {BASE}/tenants`** — create a tenant (spins up an independent openclaw microVM). Requires `x-api-key` and an operator-or-higher RBAC role; with the default `viewer` fallback, an operator/admin Bearer token is also required.

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
- `platform_id` (optional, regex `^[a-zA-Z0-9._-]{1,128}$`) — marks the owning external platform when a platform backend creates a tenant on a user's behalf (`tenant_service.py:252`)
- `order_id` / `plan_tier` / `purchase_status` — the #106 purchase-semantics trio, all validated by `_validate_purchase` (`tenant_service.py:94`): `order_id` (printable ASCII 1–128), `plan_tier` (enum `free|standard|pro|enterprise`), `purchase_status` (on create only omitted or explicit `pending` is accepted; `provisioned` is rejected — flip pending→provisioned via `POST /tenants/{id}/provision`)

> **Note** `owner_id` is set automatically from the caller's identity (Cognito `sub` or `API_KEY_OWNER`); passing `owner`/`owner_id` in the body is ignored. `tenant_user_id` (the stable id of an externally federated user) comes from the JWT's `custom:tenant_user_id` claim and is likewise not taken from the body — for external platform integration it is injected via Pre-Token-Generation (see the External Platform Integration chapter).

**Body field-level contract** (for the per-field machine-readable version see `openapi.yaml` `createTenant`):

| field                              | type           | required | default                                | constraint / enum                             | meaning                                                        | sensitive |
| ---------------------------------- | -------------- | -------- | -------------------------------------- | --------------------------------------------- | -------------------------------------------------------------- | --------- |
| `name`                             | string         | yes      | —                                      | `^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`        | tenant identifier                                              | no        |
| `vcpu` / `mem_mb` / `data_disk_mb` | integer        | no       | host default                           | positive integer (else `400 VALIDATION`)      | sizing                                                         | no        |
| `image_id`                         | string         | no       | `v2`                                   | —                                             | golden image version                                           | no        |
| `config_template`                  | string         | no       | —                                      | DNS-label regex                               | config template name                                           | no        |
| `security`                         | object         | no       | —                                      | see §3.10 invariants                          | nested encryption config                                       | no        |
| `client_token`                     | string         | no       | —                                      | `^[\x21-\x7e]{4,128}$`                        | idempotency key                                                | no        |
| `chat_endpoint_enabled`            | boolean        | no       | `false`                                | JSON boolean only (string → `400`)            | opens the OpenAI-compatible chat endpoint                      | no        |
| `skills`                           | string[]       | no       | —                                      | —                                             | per-tenant skill scope                                         | no        |
| `group`                            | string         | no       | —                                      | same regex as `name`                          | inherits group-level skills                                    | no        |
| `tags`                             | Map            | no       | —                                      | —                                             | free-form key/value tags                                       | no        |
| `ttl_hours` + `on_expiry`          | integer + enum | no       | —                                      | `on_expiry` = `delete` \| `stop`              | time-to-live and expiry action                                 | no        |
| `schedule`                         | object         | no       | —                                      | —                                             | scheduled stop/start                                           | no        |
| `restore_from` / `clone_from`      | string         | no       | —                                      | —                                             | derive from a backup / existing tenant                         | no        |
| `preferred_host_id`                | string         | no       | —                                      | —                                             | placement hint                                                 | no        |
| `platform_id`                      | string         | no       | —                                      | `^[a-zA-Z0-9._-]{1,128}$`                     | owning external platform (attribution)                         | no        |
| `order_id`                         | string         | no       | —                                      | printable ASCII 1–128                         | external order number (billing/reconciliation anchor)          | no        |
| `plan_tier`                        | string         | no       | —                                      | `free` \| `standard` \| `pro` \| `enterprise` | plan tier (controlled enum)                                    | no        |
| `purchase_status`                  | string         | no       | `pending` if any purchase field is set | on create: omit or `pending` only             | purchase state machine; `provisioned` set only via `provision` | no        |
| `owner`/`owner_id`                 | —              | —        | —                                      | ignored if supplied                           | set server-side from the caller's identity                     | —         |

> **Note (cross-platform gating, #108)** A platform-scoped API key can only create tenants inside its own platform namespace. If the body's `platform_id` names a different platform than the key's scope, the create is rejected with `403 FORBIDDEN` (`tenant_service.py:271`).

The return code depends on whether the deployment has the create throttling queue enabled (`CREATE_VIA_QUEUE`, the `scaler.create_via_queue` in `config.yml`, **off by default**):

- **Default (synchronous)**: with host capacity available, returns `201 {"id":"acme-xxxx","status":"creating",...}`; with no capacity, returns `201 {"id":"...","status":"pending"}` (automatically triggers scale-out, handled in the background).
- **With the throttling queue enabled (asynchronous)**: the request is enqueued into Amazon SQS for throttling, returning `202 {"id":"t-<16hex>","status":"queued","message":"create accepted; provisioning asynchronously"}`. Enabling it is recommended when creating tenants at scale (see the SSM concurrency note in the deployment planning chapter).

With `client_token`, the id is determined by `(owner, client_token)` and has the fixed shape `t-` plus 16 hexadecimal characters; replaying the same key returns `409 CONFLICT`. Only a create without `client_token` uses `<name>-<4hex>`. In both modes, **poll `GET /tenants/{id}` until `status:running`**. The live `tests/api-regress` suite locks this distinction.

> **Known bug (#160) — when creating via the SQS throttle queue (`DISPATCH_QUEUE_URL` enabled), some fields are not injected into the VM.** Verified live (2026-07-07): the dispatch branch only forwards `vcpu`/`mem_mb`/`owner_id`/`chat_ep`/`image` in the consumer message params (`tenant_service.py:440-451`) — **`skills`/`group`/`schedule`/`ttl_hours`/`chat_endpoint_enabled` are all dropped**. `platform_id`/`tags` are persisted (visible on GET), but `skills` never reaches the VM (`effective_skills` stays `*` broadcast) and `chat_endpoint_enabled` is lost (read as `chat_ep`). **The synchronous path (default, queue off) is unaffected — every field in the table above takes effect.** If your deployment enables the throttle queue and relies on these fields, mind this gap until #160 is fixed.

**`POST {BASE}/tenants/self`** — self-service registration (create your own node under the logged-in user's identity). Requires `Authorization: Bearer <Cognito id_token>` + `x-api-key` (RBAC viewer+). `owner_id` is forced to the caller's validated `sub` (the body cannot designate an owner for someone else), gated by the per-user node cap `SELF_MAX_NODES_PER_USER` (default 1, 0=unlimited); exceeding the cap returns `409`. When `EXTERNAL_AUTHZ` is enabled, this endpoint is rejected outright (the authorization decision is handed to the external backend). This is the entry point for users of an external SaaS platform to create their own openclaw (for the federation approach see the External Platform Integration chapter).

**`GET {BASE}/tenants`** — the tenant list (RBAC viewer+; a non-admin only sees the tenants they own).

- No parameters: a bare array (full set). Each entry is the tenant's DDB record projected with server-side credential fields removed, containing `id/name/status/owner_id/host_port/guest_ip/vm_health/app_health` and similar (`vm_health`/`app_health` are written by the health_check Lambda and may not yet appear on a freshly created tenant before its first health check).
- Pagination: `GET {BASE}/tenants?limit=5` → `{"tenants":[…],"next_token":"<opaque>","count":<count on this page>}`.
- Boundaries (verified on a live machine): `?limit=-1` → `400 {"code":"VALIDATION","error":"limit must be a positive integer (>= 1)"}`; `?next_token=garbage` → `400 {"code":"VALIDATION","error":"next_token is invalid or expired"}`.
- Optional `?tag=key:value` to filter by tag (can be repeated, AND semantics).
  > **Important** List responses strip tenant credentials. An integrator **must not pass the response through verbatim to an untrusted frontend**; expose only display fields.

**`GET {BASE}/tenants/{id}`** — one tenant (owner/admin-gated). The base record is redacted. When the tenant is `running`, the response adds the gateway-token KMS ciphertext and a `device` object (device id, public key, private-key KMS ciphertext, and scopes) for a trusted platform backend to decrypt with the required encryption contexts and complete the WSS device handshake. These are ciphertext, not public frontend fields. `GET /tenants/{id}/credentials` rewraps the same credentials into the recipient-key `asymmetric-v1` envelope.

**`POST {BASE}/tenants/{id}/{action}`** — lifecycle actions. Most use RBAC operator+ and owner gating; `rebuild` is administrator-only. Supported `action` values:

| action                          | semantics                                                                                                                                                              | returns                                                                                                 |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `start`                         | wake (~3.7s)                                                                                                                                                           | 202 async (enqueued when LIFECYCLE_QUEUE is configured)                                                 |
| `stop`                          | hibernate (~6.0s)                                                                                                                                                      | 202 async                                                                                               |
| `restart` / `reset`             | restart / reset                                                                                                                                                         | 202 async                                                                                               |
| `rebuild`                       | rebuild with `image_channel`; defaults to `live` and requires a successful backup first                                                                                | 200 synchronous; 503 when adoption cannot be confirmed                                                  |
| `pause` / `resume`              | pause / resume                                                                                                                                                         | 202 async                                                                                               |
| `backup`                        | back up to Amazon S3 (~6.6s)                                                                                                                                           | 202, asynchronously invokes the backup Lambda                                                           |
| `resize`                        | hot-change vCPU (requires body `vcpu`)                                                                                                                                 | 200 `{old_vcpu,new_vcpu}`                                                                               |
| `resize-disk`                   | offline-expand the data disk (requires body `new_size_mb`)                                                                                                             | 200                                                                                                     |
| `migrate`                       | migrate to a target host (requires body `target_host_id`)                                                                                                              | 202 `{status:"migrating",snapshot_uri,poll}`                                                            |
| `access`                        | read/write the authorization ledger (body `principal`+`op:grant\|revoke`+optional `role`)                                                                              | 200                                                                                                     |
| `provision`                     | second stage of the #106 two-stage purchase flow: flip `purchase_status` pending→provisioned so the tenant becomes business-usable (`tenant_service.py:1579`); no body | 200 `{id,purchase_status:"provisioned"}` (idempotent; `400` if the tenant has no purchase to provision) |

For an already-deleted tenant there is a state guard against revival. An unknown action returns `400`.

> **Note** `purchase_status` is orthogonal to the VM lifecycle `status`: `status` tracks "is the VM alive", `provision` moves the commercial state from `pending` (order recorded, VM not yet business-usable) to `provisioned`. The flip is a CAS conditional update, so a repeat `provision` returns `200` idempotently without side effects (`openapi.yaml` `tenantAction`, `title: provision`).

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

Image snapshot and canary-slot endpoints:

- **`POST {BASE}/create-image-snapshot`** accepts `{"label":"<1-128 A-Za-z0-9._->"}`. An empty label is not derived by the control plane; it returns `400 VALIDATION`. Any derivation or deduplication belongs to the upstream caller.
- **`GET {BASE}/list_image_versions`** / **`POST {BASE}/delete-image-snapshot`** list or delete version snapshots.
- **`POST {BASE}/hosts/{id}/pull-image?snapshot_time=<ISO>&slot=live|canary`** pulls into the live or canary slot.
- **`GET {BASE}/hosts/{id}/pull-image-progress`** / **`GET {BASE}/hosts/{id}/image-slots`** return the async job and the host's actual slot state.
- **`POST {BASE}/hosts/{id}/promote-canary`** / **`POST {BASE}/hosts/{id}/reclaim-images`** promote a validated canary or reclaim unreferenced versions. See `openapi.yaml` and the [pull-image API](../api/pull-image-api.md) for state and conflict details.

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

**`GET {BASE}/skills`** — the skill catalog (viewer+, served by a standalone Lambda `deploy/lambda/skills/handler.py:34`). Returns `{"skills":[{id,name,description},...]}` enumerated from the S3 `skills/` prefix. An optional `?tenant=<id>` narrows the list to that tenant's effective skill set (per-tenant + group-resolved); without it the full broadcast catalog is returned, which the operator console's "Skills library" view uses. (`openapi.yaml` `listSkills`.)

**`GET {BASE}/skills/{name}`** — read a skill's `SKILL.md` content (viewer+): `{name,content,size,last_modified}`, `404` if it does not exist.
**`PUT {BASE}/skills/{name}`** — write/create a skill (operator+): body `{content}`, must be UTF-8, ≤256KB, and contain at least one top-level `# heading`; returns `200` (already existed) or `201` (newly created); over the limit `413`, non-conforming format `400`.
**`DELETE {BASE}/skills/{name}`** — delete a skill (operator+): returns `{name,deleted:<number of files deleted>}`.

> **Note** A skill name is limited to lowercase letters + digits + hyphens; an illegal name returns `400`. Skill changes land in the image artifact layer and take effect on the next image rebuild / refresh-rootfs, not by hot-patching a running VM (in keeping with the architectural rule "change the image and rebuild, do not hot-patch a live VM").

**Config templates** — CRUD over the OpenClaw config templates under the S3 `templates/openclaw/` prefix, served by a standalone Lambda. These routes currently require only API Gateway `x-api-key`; they do not pass through Cognito RBAC or the API Lambda audit path. Because an API key is not authentication, put template writes behind trusted networking or an additional authorizer rather than exposing them directly to the public internet.

- **`GET {BASE}/templates`** — the template list `{"templates":[{name,size,modified},...]}`.
- **`GET {BASE}/templates/{name}`** — one parsed `openclaw.json` object, or `404`.
- **`PUT {BASE}/templates/{name}`** — create/update; invalid JSON returns `400`, and `default` is write-protected (`403`).
- **`DELETE {BASE}/templates/{name}`** — delete; `default` is protected (`403`).

### 3.7 AgentCore (read-only, config-gated)

**`GET {BASE}/agentcore/status`** (viewer+) — the AgentCore gateway enablement status `{enabled, gateway_url}`.
**`GET {BASE}/agentcore/tools`** (viewer+) — the MCP tool list registered to the AgentCore gateway `{enabled, tools:[{name,description,input_schema}]}`. When not enabled, `enabled:false`.

### 3.8 Federated IdP Routing (pre-login lookup)

**`GET {BASE}/tenantmatch?platform_id=<id>`** — an external platform → Cognito upstream IdP routing lookup (**skips RBAC**, no identity before login). Given a platform identifier it returns `{platform_id, idp_provider_name, issuer_url}`, and the frontend uses this to redirect the user straight to the corresponding upstream IdP. `platform_id` regex `[a-zA-Z0-9._-]{1,128}`, illegal `400`; IdP federation not configured (`TENANT_IDP_TABLE` unset) `404 NOT_CONFIGURED`; platform not registered `404 NOT_FOUND`; DynamoDB failure `502`. **A pre-login lookup that leaks no tenant data, doing only platform → IdP routing.**

> **Known gap — currently unreachable** The handler, DDB table, Lambda environment, and IAM read grant exist, but `deploy/stacks/lambdas.py` does not add a `tenantmatch` API Gateway resource. API Gateway therefore rejects the path before Lambda. Do not depend on this documented-but-unreachable operation.

### 3.9 Authorization Integration (external backend)

**`POST {BASE}/external/authz`** — an external backend pushes "user ↔ tenant" authorization mappings (**skips RBAC, self-authenticated with an HMAC signature**, in effect when `EXTERNAL_AUTHZ` is enabled). The mapping is written into `authorized_users`; the platform WebSocket gateway consults the same authorization fact before selecting a tenant for a user.

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

## 4. Data Plane: Platform WebSocket Gateway

Real-time chat is not a control plane API subresource. The browser connects only
to the customer's platform endpoint:
`wss://<platform>/gw/ws?token=<platform-session-token>`. The platform backend
validates its own session token, chooses the tenant from server-side ownership
data, and connects as an OpenClaw WebSocket client to `/ws/{tenant_id}`. The
second hop traverses Amazon CloudFront, an Application Load Balancer, the
OpenResty edge, host DNAT, and the microVM on port `18789`.

The platform backend obtains KMS ciphertext for the gateway token and device
private key from the control plane, decrypts it in process with the required
encryption context, and completes `connect.challenge` → Ed25519 signature →
`hello-ok`. The browser never receives `x-api-key`, a gateway token, or a device
private key. The retired `/hub/token`, `/hub/ws`, `/channel-token`,
`/chat/sign`, and hub file-presign endpoints are not current contracts. See
Chapter 13 and `engineering/backend/lib/gw-ws.mjs` for frames, close codes, and
retry behavior.

A customer platform that needs file upload or download must implement its own
tenant authorization and Amazon S3 presigning path. A presigned URL is a bearer
capability and can expire earlier than requested when its signing credentials
expire; do not call the retired hub endpoints. See
[Amazon S3 presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html).

---

## 5. Quick Start: Running End to End (from the customer's perspective)

```bash
export KEY="<your x-api-key>"
export TOKEN="<operator or admin Cognito id_token>"
export BASE="https://<api-id>.execute-api.<region>.amazonaws.com/v1"

# 1) Confirm the environment
curl -s -H "x-api-key: $KEY" "$BASE/system/info" | python3 -m json.tool

# 2) Create a tenant (with an idempotency key)
curl -s -X POST -H "x-api-key: $KEY" -H "Authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"name":"quickstart","client_token":"qs-0001"}' "$BASE/tenants"
# → With client_token, id is t-<16hex>
# → Default synchronous: 201 {"id":"t-...","status":"creating"}
# → With the throttling queue: 202 {"id":"t-...","status":"queued"}

# 3) Poll until running
curl -s -H "x-api-key: $KEY" "$BASE/tenants/t-<16hex>"
# → until {"status":"running","vm_health":"up","app_health":"up"}

# 4) Chat uses the customer platform /gw/ws; the browser sends only its platform session token
#    wss://<platform>/gw/ws?token=<platform-session-token>
```

Steps 1–3 are control-plane calls. With the default configuration, writes need an operator/admin Bearer token; omit it only when the deployment explicitly grants operator/admin to the API-key-only path. Chapter 13 covers the platform backend in step 4.

---

## 6. Appendix: Endpoint Quick Reference

| endpoint                                               | method         | auth / RBAC                | purpose                                                                                          |
| ------------------------------------------------------ | -------------- | -------------------------- | ------------------------------------------------------------------------------------------------ |
| `/system/info`                                         | GET            | api-key · viewer           | system capability snapshot                                                                       |
| `/hosts` `/hosts/rootfs-version` `/hosts/rootfs-drift` | GET            | api-key · viewer           | host and image reconciliation                                                                    |
| `/hosts`                                               | POST           | operator                   | register a host                                                                                  |
| `/hosts/{instance_id}`                                 | DELETE         | operator                   | take a host offline (draining)                                                                   |
| `/hosts/refresh-rootfs`                                | POST           | operator                   | push the latest image to hosts                                                                   |
| `/hosts/fleet-power`                                   | POST           | **admin**                  | fleet-wide power on/off                                                                          |
| `/images`                                              | GET            | viewer                     | image artifact list                                                                              |
| `/tenants`                                             | GET            | viewer (own only)          | tenant list (bare-array / paginated dual shape)                                                  |
| `/tenants`                                             | POST           | operator                   | create a tenant                                                                                  |
| `/tenants/self`                                        | POST           | Bearer · viewer            | self-service registration (user identity)                                                        |
| `/tenants/{id}`                                        | GET            | owner-gated                | single tenant detail                                                                             |
| `/tenants/{id}/{action}`                               | GET            | owner-gated                | backups / data / access (read-only)                                                              |
| `/tenants/{id}/{action}`                               | POST           | operator + owner; rebuild admin-only | start/stop/restart/pause/resume/reset/rebuild/backup/resize/resize-disk/migrate/access/provision |
| `/tenants/{id}`                                        | DELETE         | operator + owner           | deregister (keep_data optional)                                                                  |
| `/batch/tenants` `/batch/jobs/{id}`                    | POST/GET       | operator / viewer          | batch and job progress                                                                           |
| `/users/{uid}/tenants` `/summary` `/action`            | GET/POST       | viewer (own only)          | manage a fleet by platform user                                                                  |
| `/backups` `/audit-log`                                | GET            | viewer                     | backups and audit                                                                                |
| `/groups` `/groups/{name}/skills`                      | GET/POST       | viewer / operator          | groups and group-level skill                                                                     |
| `/groups/{name}/skills/{skill}`                        | DELETE         | operator                   | remove a group-level skill                                                                       |
| `/skills`                                              | GET            | viewer                     | skill catalog list (optional `?tenant=`)                                                         |
| `/skills/{name}`                                       | GET/PUT/DELETE | viewer / operator          | skill content CRUD                                                                               |
| `/templates` `/templates/{name}`                       | GET/PUT/DELETE | viewer / operator          | config-template list + CRUD (`default` immutable)                                                |
| `/agentcore/status` `/agentcore/tools`                 | GET            | viewer                     | AgentCore gateway status/tools (config-gated)                                                    |
| `/tenantmatch`                                         | GET            | api-key; no RBAC           | documented-but-unreachable; gateway resource is not wired                                        |
| `/external/authz`                                      | POST           | HMAC                       | external authorization push (config-gated, off by default)                                       |
| `/hosts/{id}/pull-image` `/promote-canary`             | POST           | operator / admin           | image pull and canary promotion                                                                   |
| `/hosts/{id}/image-slots` `/pull-image-progress`       | GET            | viewer                     | host slot state and pull progress                                                                 |
| `/hosts/{id}/reclaim-images`                           | POST           | admin                      | reclaim unreferenced image versions                                                               |

> Verification sources: control-plane routes and RBAC come from `deploy/lambda/api/handler.py`, `deploy/lambda/api/core/auth.py`, and `deploy/stacks/lambdas.py`; field contracts come from `openapi.yaml`; live behavior is locked by `tests/api-regress/`. Data-plane behavior comes from `engineering/backend/lib/gw-ws.mjs`, not the retired hub.
