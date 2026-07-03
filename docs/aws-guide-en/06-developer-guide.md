# Developer Guide

This section is for developers who need to interact with the solution programmatically, covering the authentication model, the control plane REST API, real-time chat integration, and the authorization model.

The solution exposes two integration surfaces: the control plane REST API (management operations such as tenant registration, lifecycle, backup, and deregistration) and the real-time chat path (a bidirectional WebSocket channel: browser ↔ chat hub `claw-hub` ↔ `claw-channel` inside the tenant microVM). The two integration surfaces share the same trust root, Amazon Cognito, but fall on two mutually orthogonal verification planes.

> **Note**
>
> The solution follows a design discipline that runs through the whole system: identity, credentials, and configuration are cold-injected into the read-only golden image disk before the tenant microVM launches, and no hot-injection channel from host to microVM is opened after launch. Modifying a tenant's identity, skills, or switches is not a runtime API call but a matter of modifying the deployment code and rebuilding the image. The detailed mechanism of this discipline is described in "Architecture Details" and "Plan the Deployment — Security".

---

## Authentication model

This section describes the solution's authentication model, including the single trust root, the three token types, the two verification planes, the three-level role-based access control (RBAC) roles, and the zero-credential constraint.

### Trust root and verification planes

The core of the solution's authentication is Amazon Cognito, the single trust root of the entire platform. Amazon Cognito is AWS's identity platform for web and mobile applications, providing a user directory, an authentication server, and OAuth 2.0 authorization; the solution uses it to issue id_tokens (JSON Web Tokens, JWTs) for tenants and end users, which each component verifies with Cognito's public keys (JWKS, JSON Web Key Set).

For the three token types and two verification planes in the system, trust all traces back to the id_token issued by Amazon Cognito and verified by its public key. The control plane verifies it, and the data plane `claw-hub` also verifies only it; users who sign in through external OpenID Connect (OIDC) federation are also federated into the same Cognito User Pool and then re-issued an id_token by Cognito. The platform itself maintains no independent user-password system and trusts no client-asserted identity; everything is based on the user's unique identifier (`sub`) obtained from Cognito verification. Requests that fail verification are uniformly downgraded to least privilege (read-only viewer) and are not granted high privilege.

Above the trust root are two orthogonal authentication planes:

- **Control plane (REST API)**: uses the Bearer credential of the Amazon Cognito id_token plus RBAC.
- **Chat plane (`claw-hub`)**: internally divided into a chat frontend token and a chat channel token, each verified independently; the issuance of the frontend token is likewise premised on verification of the Cognito id_token.

> **Note**
>
> The bare paths `/token`, `/channel-token`, `/ws`, `/files/*` inside the hub process described in this section are exposed externally through the Amazon CloudFront `/hub/*` prefix as `/hub/token`, `/hub/ws`, and so on. The subsections below use the bare paths to describe the hub contract, and the end-to-end integration sequence uses the external `/hub/*` paths; the two refer to the same endpoint.

### Three token types overview

The table below summarizes the issuer, verifier, carrier, and time to live (TTL) of the three token types in the solution.

| token                          | Issuer                                                          | Verifier                                                                                           | Carrier                                                                                      | TTL                                                                      |
| ------------------------------ | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| control-plane Cognito id_token | Amazon Cognito (exchanged via OAuth2 authorization-code + PKCE) | control-plane Lambda (RS256, verifies issuer, compares audience only if a client id is configured) | `Authorization: Bearer {id_token}` HTTP header                                               | id/access token 60 minutes, refresh_token 7 days (Cognito client config) |
| chat frontend token            | `claw-hub` (HMAC self-signed)                                   | `claw-hub` (recomputes HMAC-SHA256, constant-time comparison)                                      | WebSocket handshake query parameter `?token=` or the Bearer header / `?token=` of `/files/*` | 300 seconds                                                              |
| chat channel token             | `claw-hub` (HMAC self-signed)                                   | `claw-hub` (recomputes HMAC-SHA256, constant-time comparison)                                      | WebSocket handshake query parameter `?token=`                                                | 300 seconds                                                              |

> **Note**
>
> Both the chat frontend token and the chat channel token are HMAC tokens self-signed by `claw-hub`, in the format `base64url(claims).base64url(sig)`, using the hub's signing key for HMAC-SHA256. Both are decoupled from the Cognito id_token, but the issuance of the chat frontend token is premised on Cognito verification passing and the tenant authorization check passing.

### Control-plane Cognito id_token

When calling the control plane REST API, carry the id_token obtained after Cognito sign-in in `Authorization: Bearer {Cognito id_token}`. The control plane uses a standard JWT library to verify the RS256 signature, pulls the public key from Cognito's JWKS endpoint, and verifies the issuer as `https://cognito-idp.<region>.amazonaws.com/<pool_id>`; the audience is not enforced by default (it is only best-effort compared when a client id is configured), and a missing expiration time or missing issuer is treated as invalid. The user's unique identifier (`sub`) obtained from verification serves as the caller's identity, and verification failure is uniformly downgraded to viewer.

The caller can be a native Cognito user or a user who signed in through external OIDC federation. For the latter, after verification the external account's stable id is also extracted from the token and recorded, used only to attribute the tenant back to the external account, and does not participate in verification or authorization decisions (authorization is still based on the Cognito `sub`); native Cognito users have no such field.

> **Important**
>
> A request without a Bearer token is treated as trusted automation, in which case the caller's identity is the built-in "key caller" (api-key), equivalent to admin full privilege. In other words, network-level trusted internal automation (no JWT) receives full privilege. When exposing this API externally, you must ensure that the "no Bearer means full privilege" path is open only to trusted networks (through Amazon API Gateway, with `x-api-key`). Amazon API Gateway is an AWS-managed API gateway that hosts REST/HTTP APIs and connects to a backend; the solution uses it to expose the control plane API externally.

### Three RBAC roles

The RBAC role levels are viewer (read-only) < operator < admin. Routes on the read-only allowlist are reachable by viewer; the rest require operator and above; the role check runs uniformly after a route is matched and before the business logic executes; the owner check runs only when RBAC is enabled. `RBAC_ENABLED` defaults to `true`, that is, per-route role checks and owner checks are enforced by default; disable it explicitly only when a demo or development environment needs to be fully open. Confirm the target environment's value before integrating.

### Chat frontend token

The frontend must obtain a chat frontend token to connect to the hub's WebSocket. The issuance flow is `POST /token`, with the header `Authorization: Bearer {Cognito id_token}` and the body `{tenant_id}`. The hub first remotely pulls Cognito's JWKS to verify RS256 (verifying the issuer, and verifying the audience if a client id is configured), and rejects any failure; on successful verification it takes the user's unique identifier (`sub`), then performs an explicit tenant authorization check (whether the user is the owner or an authorized user of the tenant). Only if authorization passes is the token issued; authorization failure returns 403. The chat frontend token carries four claims:

| claim    | Value                                 | Description                                                                                       |
| -------- | ------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `role`   | `"frontend"`                          | Fixed                                                                                             |
| `sub`    | Cognito sub                           | The server-verified user identity, not the client-asserted one                                    |
| `tenant` | tenant_id                             | The tenant this token is bound to                                                                 |
| `access` | `owner` / `<granted role>` / `shared` | Depends on the authorization record (see "Tenant authorization model and cross-tenant isolation") |
| `exp`    | Unix seconds                          | Issuance time + 300s                                                                              |

The Cognito region, user pool, and client id are all read from environment variables with empty-value fallbacks. In an environment without a configured user pool, verification fails directly and no frontend token can be issued; that is, Amazon Cognito is a precondition dependency of the chat plane.

### Chat channel token

The `claw-channel` inside the tenant microVM uses the chat channel token to register its outbound connection with the hub. The issuance flow is `POST /channel-token`, with the body `{tenant_id, appId, timestamp, signature}`, where `signature = HMAC-SHA256("{appId}:{timestamp}", Buffer.from(appSecret, "hex"))`, a time window of ±300 seconds, and a constant-time comparison to prevent timing attacks. After verification passes, the channel token is issued.

The chat channel token carries only three claims and no `sub`: `role:"channel"`, `tenant:tenantId`, `exp` (TTL 300s). Because the channel is a single-tenant registration on the tenant microVM side, it represents a tenant rather than a particular user. A microVM holds only one tenant's appSecret (cold-injected before launch), so a channel can register only one tenant, and cross-tenant channels require their respective secrets.

### WebSocket handshake and token verification

The hub verifies a self-signed token by recomputing the HMAC, comparing in constant time, checking `exp` expiration, and parsing the claims. During the WebSocket handshake the token goes in the query parameter `GET /ws?token={hub token}`, with `maxPayload` limited to 1MB. Verification failure closes the connection with WebSocket close code 1008. After a successful handshake, connections fall into different routing tables by role: a channel connection is registered by tenant and returns `{type:"registered"}`; a frontend connection is registered by user (multiple tabs of the same user are grouped together) and returns `{type:"ready"}`.

### Zero-credential constraint and secret management

The tenant microVM side is a zero-credential golden image: it has no AWS access key or Amazon Simple Storage Service (Amazon S3) permission, and holds only the current tenant's appSecret (used for the channel HMAC signature). When it needs to read or write Amazon S3 files, the channel must request a presigned URL from the hub, which is the only component holding the Amazon S3 instance role, keeping the Amazon S3 blast radius in one place.

The appSecret exists in two places: the microVM's local configuration file (the read-only data disk) and the corresponding field in the tenants table. Injection uses a control-plane pre-generation model: the control plane mints a per-tenant unique 32-byte random secret when creating the tenant, writes it into the tenant record, and injects it into the local configuration through the deployment script when launching the microVM; only when the parameter is absent (the compatibility path) does it fall back to the microVM self-generating locally. The hub reads this secret from the tenants table when verifying signatures. Pre-generation ensures the tenant record has a secret before the microVM launches, so the hub can verify the channel's first registration, avoiding a launch race. The whole process has no hard-coding, and the secret does not leave AWS and the microVM.

The same cold-injection also handles the per-tenant LiteLLM billing virtual key (vkey): if there is a dedicated vkey, the microVM's LLM-calling key is overwritten with it (spend, budget, and rate limit split by tenant), otherwise the image's shared key is retained. The vkey, like the appSecret, lands only on each microVM's data disk, not in the read-only golden image, and not in the browser.

> **Important**
>
> Secret rotation follows the "modify the deployment code and rebuild" discipline: the hub's token signing key defaults to a random 32 bytes and can be overridden by an environment variable; the appSecret is not hot-updated after cold injection, and rotation requires rebuilding the image or modifying the launch configuration. All credentials in this guide are placeholdered as `[REDACTED]`.

### Guest and credential-exfiltration interception

A visitor without a Cognito identity (no id_token) cannot call `/token` (a missing Bearer directly returns 401). Even with a legitimate Cognito sign-in, if the user is neither the tenant owner nor on the authorization list, the hub uniformly returns 403. Shared or legacy nodes (old records with no explicit owner) are also denied by default; only when the shared-access switch is explicitly turned on is the shared role admitted (off by default, deny on failure, fail-closed).

Credential-exfiltration interception takes effect before a tool actually executes (not relying on the model's self-restraint): it intercepts reads of cloud access keys and temporary session credentials, and intercepts reads of sensitive files such as command-line history, local environment configuration, and the SSH directory. Both interceptions use a fail-closed policy — even if the judgment logic itself errs, it blocks rather than admits. Combined with the zero-credential constraint (the microVM holds no AWS credentials), the credential-exfiltration surface is minimized. This layer is the L2 tool-execution guardrail; see "Plan the Deployment — Security".

### Frontend sign-in flow

The frontend (a mini-program or web app) obtains the Cognito id_token through OAuth2 authorization-code + PKCE; this is the flow that an integrator implementing a sign-in page must follow, and it is the starting point of all the tokens above.

When not signed in, the frontend generates a PKCE `code_verifier` (random), computes the S256 `code_challenge`, and redirects to the Cognito Hosted UI `GET /oauth2/authorize?response_type=code&scope=openid+email&code_challenge_method=S256&...`; after the callback returns with `?code=`, it uses `code` plus `code_verifier` to call `POST /oauth2/token` (`grant_type=authorization_code`) to exchange for the id_token plus refresh_token, stored in localStorage. The authorization code is one-time; before exchanging, the frontend wipes `?code=` from the address bar and counts exchange failures, to prevent a reload-with-stale-code death loop.

The token lifecycle is split into two layers with different refresh mechanisms. The hub self-signed tokens (used by the frontend and channel to connect the WebSocket, TTL 300s) are actively refreshed with a margin by each of the three parties: the channel side clears the cache 60s before expiration and refreshes the token in advance before reconnecting; the frontend side forces a reconnect for a new hub token when the WebSocket connection exceeds 270s (leaving a 30s margin); the Media presigned URL is also TTL 300s. The Cognito id_token is configured as id/access token 60 minutes, refresh_token 7 days, and the frontend silently exchanges for a new one with the refresh_token near expiration, not kicking back to the sign-in page within 7 days; when exchanging for a hub token (`POST /token`) returns 401, the frontend first silently exchanges for a new id_token with the refresh_token and retries, only clearing the local token and jumping to sign-in if the exchange fails.

> **Note**
>
> External OIDC federated integration is optional and off by default. The Cognito user pool can attach an external OIDC identity provider, letting users of an external account system sign in to the same pool with their existing accounts, controlled by the external identity provider section in the configuration. The external provider's client secret is managed by AWS Secrets Manager (the plaintext does not enter the deployment template). After federated sign-in, the hub still verifies only the id_token issued by Cognito (the trust root is still Cognito), and the external account's stable id is used only for attribution. To turn on external sign-in, an integrator needs to configure the external identity provider section in the target environment and pre-create the corresponding custom attributes on the user pool.

---

## Control plane REST API

This section describes the endpoint contract of the control plane REST API. The control plane is an AWS Lambda function that provides tenant registration, lifecycle, backup, host management, skill and group management, audit logging, and other capabilities, persisted by Amazon DynamoDB. All endpoints go through Amazon API Gateway, require `x-api-key`, and have RBAC enabled by default. The following is organized by domain; each endpoint gives the method and path, RBAC level, purpose, key parameters, and main status codes.

### Tenant registration

#### POST /tenants

**RBAC: operator+.** Registers a tenant and generates a `tenant_id` (`name-xxxx` format). The default synchronous path returns 201: the initial status is `pending` (no host capacity, returns `{id, status:pending}`, auto-triggering scale-out) or `creating` (with capacity, returns `{id, host_id, guest_ip, host_port, status:creating}`). With the create throttling queue enabled (`CREATE_VIA_QUEUE`/`scaler.create_via_queue`, off by default), it switches to asynchronous: enqueued to Amazon SQS to throttle, returns 202 `{id, status:queued}`, then polls `GET /tenants/{id}` until running. Enabling the queue is recommended when creating tenants at scale (see the Systems Manager concurrency note in Troubleshoot).

Key parameters:

- **Required**: `name` (DNS-label, ≤32 characters, regex `^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`).
- **Optional**: `vcpu` (script default 2), `mem_mb` (script default 4096), `data_disk_mb` (code default 2048, the environment variable prevails), `config_template`, `restore_from` (object, `{tenant_id, timestamp?}`, uses the latest backup when omitted), `clone_from` (id, the source must be `running`, forced same-host), `preferred_host_id`, `tags` (dict, ≤20 items, key ≤50 chars, value ≤100 chars, key/value must not contain a colon), `ttl_hours`, `on_expiry`, `schedule`, `skills` (list), `group`. `restore_from` and `clone_from` are mutually exclusive.
- **Optional (idempotency and standard fields)**: `client_token` (4–128 ASCII, the idempotency key; the same `client_token` + the same `name` derive the same `tenant_id`, and a duplicate submission returns 409 due to the conditional write rather than starting a duplicate machine; if not provided, a new id is generated each time), `image_id` (golden image version, DNS-label format, default `v2`), `security` (encryption configuration object, with inter-field dependencies, see below).
- **The `security` object** (optional, per-tenant encryption/certificate configuration, named to align with AWS S3 `ServerSideEncryptionConfiguration`): `storage_encrypted` (boolean), `encryption_type` (`none` | `platform_managed` | `tenant_cmk`, where `tenant_cmk` is customer-managed bring-your-own-key BYOK), `kms_key_arn` / `cert_arn` / `secret_ref` (all full ARNs, not bare ids; `secret_ref` stores an AWS Secrets Manager reference rather than the key content). Validation invariants: unencrypted must not carry a KMS key; `tenant_cmk` must carry a `kms_key_arn`. These are all references/configuration (not secrets), stored in the record and echoable on query.

> **Note**
>
> The `vcpu`/`mem_mb` script defaults (2 vCPU / 4096 MB, `deploy/userdata/launch-vm.sh:34-35`) are consistent with the central configuration file defaults (`config.yml.example:36-37` `default_vcpu: 2` / `default_mem_mb: 4096`). Capacity planning separately estimates steady-state density based on 2 GB of memory per microVM, and the parameters passed in by the caller prevail in practice; see "Use the Solution — Capacity configuration".

Capacity allocation uses CAS (Compare-And-Swap) to atomically claim vm_num and reserve used_vcpu/used_mem_mb, with 8 retries on conflict; a launch failure rolls back the capacity.

Main status codes:

| Status code | Trigger condition                                                                                                       |
| ----------- | ----------------------------------------------------------------------------------------------------------------------- |
| 201         | Registration successful                                                                                                 |
| 400         | Parameter validation failed (the error body carries a stable `code`, such as `VALIDATION`); clone capacity insufficient |
| 409         | Same id already exists (idempotent replay, error body `code:CONFLICT`)                                                  |
| 503         | No capacity / CAS contention failure                                                                                    |

> **Note**
>
> The error response body, besides `error` (human-readable text, may change), carries a stable machine-readable `code` (such as `VALIDATION`, `CONFLICT`), so clients can distinguish errors in code without parsing text. Old clients that read only `error` remain compatible.

> **Note**
>
> On registration, the control plane automatically performs several identity and billing writes (the integrator need not pass them). First, the per-tenant LiteLLM billing vkey: the control plane requests a tenant-dedicated virtual key from LiteLLM `/key/generate`, so that spend, budget, and rate limit split by tenant and Cognito sub; on success it is stored in the Amazon DynamoDB `litellm_vkey` field and injected when launching the microVM. This logic depends on the control plane having a LiteLLM master key configured (stored in AWS Secrets Manager); when the master key or `LITELLM_BASE_URL` is not configured, minting is skipped and the tenant falls back to the image's shared key, so it is not unconditionally in effect. Second, external account attribution: the OIDC-federated caller's `tenant_user_id`, when it has a value, is written into the Amazon DynamoDB `tenant_user_id` field; native Cognito or api-key callers have no such field. Third, standard identifier fields: `uuid` (= the stable principal identifier, taken from the verified Cognito `sub`, that is, `owner_id`; note that the tenant primary key is still `id` = `name-xxxx`, and `uuid` is not the primary key, so one user can own multiple tenants), `created_at`, and `image_id` (the golden image version at creation, default `v2`) are written into the record and echoable on query.

### User self-registration

#### POST /tenants/self

**RBAC: on the viewer allowlist (the real self-only and quota checks are inside the handler).** A signed-in end user provisions a node for themselves (distinct from the operator-level `POST /tenants`), returning 201 or 4xx. Any verified Cognito user can call it, but can only provision for themselves: `owner_id` is forced to the caller's verified `sub`, and `owner` and `owner_id` in the body are stripped; when name is missing, `u-<first 8 of sub>` is auto-generated. After validation it delegates to the internal registration logic, and host scheduling, vkey, and skill scoping are identical to `POST /tenants`.

Key parameters and constraints:

- Must be a real signed-in user; api-key automation, unverified tokens, and `API_KEY_OWNER` all return 401.
- `SELF_MAX_NODES_PER_USER` (default 1, 0 means unlimited) counts non-deleted nodes through the owner index COUNT and returns 409 when the limit is reached; on COUNT failure it fails closed (treated as already at the limit, to prevent being flooded by Amazon DynamoDB jitter).

Main status codes:

| Status code | Trigger condition                                                                                              |
| ----------- | -------------------------------------------------------------------------------------------------------------- |
| 201         | Provisioning successful                                                                                        |
| 401         | api-key automation / unverified token / `API_KEY_OWNER`                                                        |
| 403         | `EXTERNAL_AUTHZ=true` (who can obtain a node is decided by an external backend, not through self-registration) |
| 409         | The `SELF_MAX_NODES_PER_USER` limit reached                                                                    |

> **Note**
>
> This endpoint and the frontend "+ Provision my AI node" entry are in place. Confirm the target environment's `EXTERNAL_AUTHZ` and `SELF_MAX_NODES_PER_USER` values before integrating.

### Tenant state machine and lifecycle operations

The tenant state machine is `pending → creating → running|stopped|paused → (each operation) → deleted`. `pending` means no capacity, auto-triggering an ASG scale-out and waiting for the background `process_pending` (driven by Amazon EventBridge) to handle it; `creating` means a host is allocated, the microVM is starting, and the health check polls until running; `running` means the launch is complete, `channel_secret` is mirrored to Amazon DynamoDB, and chat can be received (the gateway_token is also mirrored at this point, but is only for the administrator control UI and unrelated to the chat path, see the capability boundaries).

#### POST /tenants/{id}/{action}

**RBAC: operator+.** `action ∈ {resize | resize-disk | migrate | restart | stop | start | reset | pause | resume | backup | access}`; on entry it first performs the IDOR (Insecure Direct Object Reference) owner check, returning 403 for a non-owner. `start/stop/restart/pause/resume/reset`, when `LIFECYCLE_QUEUE_URL` is configured, enqueue to Amazon Simple Queue Service (Amazon SQS) to throttle and return 202 queued; without it configured they execute synchronously.

Three special actions:

- **resize**: hot-adds vCPU (adding `mem_mb` is refused, online memory resize is not supported), only for `running`, `new_vcpu > current`, always synchronous, returns 200.
- **resize-disk**: offline data-disk expansion, `new_size_mb` must be > current and ≤1 TiB (no shrinking), landed to the database only on success. Returns 200 / 400 / 502.
- **migrate**: cross-host snapshot migration, receives `{target_host_id}`, always asynchronous, returns 202, triggering a health-check sweep poll (state machine `{status:migrating, migration_phase:snapshot|restore}`, transitioning to running and updating host_id on success, rolling back to the original state on failure). When `BALLOON_ENABLED=true`, it returns 409 (Firecracker does not support a snapshot with balloon; use back up plus rebuild plus restore instead). This path is an edge operations path, and end-to-end real-machine success is to be verified.

Main status codes:

| Status code | Trigger condition                                                                    |
| ----------- | ------------------------------------------------------------------------------------ |
| 200         | resize successful / synchronous lifecycle action successful / resize-disk successful |
| 202         | Throttled enqueue when `LIFECYCLE_QUEUE_URL` is configured / migrate async trigger   |
| 400         | resize-disk parameter illegal (not greater than current or over 1 TiB)               |
| 403         | IDOR owner check failed                                                              |
| 409         | migrate when `BALLOON_ENABLED=true`                                                  |
| 502         | resize-disk backend failure                                                          |

### Delete

#### DELETE /tenants/{id}

**RBAC: operator+.** `DELETE /tenants/{id}?keep_data=(true|false)&skip_backup=(true)` deletes the Application Load Balancer (ALB) rule, stops the microVM, clears the data directory when `keep_data=false`, and returns 200 (idempotent; already-deleted still returns 200). The flow is IDOR owner check → (forced backup before destruction) → stop microVM → clear microVM metadata → remove ALB rule → revoke DNAT → (optionally) delete data directory → update host capacity count → reclaim LiteLLM vkey → soft delete (status:deleted).

> **Important**
>
> Forced backup before destruction is an irreversible protection: when `keep_data=false` (default true) and `?skip_backup=true` is not carried, it first synchronously invokes the backup Lambda to transfer the data disk to Amazon S3; if the backup fails it returns 502 and aborts the deletion (does not delete the data). When confirmed that no data need be retained, pass `?skip_backup=true` to skip; `keep_data=true` (soft delete retaining the disk) does not trigger a backup.

vkey reclamation: on deletion it reclaims the tenant's per-tenant LiteLLM vkey (`POST /key/delete`, best-effort, does not block deletion) and removes `litellm_vkey` from the record, to prevent churn from accumulating orphan keys.

Main status codes:

| Status code | Trigger condition                                         |
| ----------- | --------------------------------------------------------- |
| 200         | Deletion successful (idempotent)                          |
| 403         | IDOR owner check failed                                   |
| 502         | Forced backup before destruction failed, deletion aborted |

### Backup

- **POST /tenants/{id}/backup** (RBAC operator+, grouped under the lifecycle routes): asynchronously invokes the backup Lambda (`InvocationType:Event`), always returns 202 `{status:started}`, and does not change the status.
- **GET /backups** (RBAC viewer): lists all backups of all tenants plus a `tenant_exists` orphan marker, returning `[{tenant_id, tenant_name, tenant_exists, timestamp, size_bytes, last_modified}]`.
- **GET /tenants/{id}/backups** (RBAC viewer, owner-gated): lists a single tenant's backups `[{key, timestamp, size_mb}]`.

On restore, the parsed backup key is passed in when launching the microVM, downloaded from Amazon S3 to overwrite the data disk (rather than using the data_template).

### Query endpoints

#### GET /tenants

**RBAC: viewer.** Lists all tenants; with `RBAC_ENABLED`, a non-admin sees only tenants of their own `owner_id`. Supports `?tag=key:value` multi-value filtering (AND semantics).

Response redaction: before each record is returned, the server-side secrets `channel_secret` (the hub HMAC registration secret) and `litellm_vkey` (the billing key) are projected out and not delivered to the browser. The chat UI calls this endpoint with the Cognito Bearer, and leaking `channel_secret` would let a signed-in user forge that node's channel registration, constituting an IDOR and a credential leak.

Pagination (backward compatible): without `?limit` and without `?next_token`, it returns a bare array `[{...}]` (unchanged for old callers); with `?limit=N` (1-1000, default 100, an illegal value falls back to 100) or `?next_token`, it returns an envelope `{tenants:[...], next_token, count}`. `next_token` is an opaque base64 cursor (an encoding of the Amazon DynamoDB `LastEvaluatedKey`); pass it back verbatim to get the next page, `null` when done, and do not construct it manually.

#### GET /tenants/{id}

**RBAC: viewer, with owner gating inside the handler.** Gets a single tenant, returning `effective_skills` (the resolved skill list, or `'*'` for broadcast), likewise redacting `channel_secret` and `litellm_vkey`.

#### GET /tenants/{id}/access

**RBAC: viewer, owner/admin-gated.** Gets the explicit authorization list and `owner_id`, returning `{id, owner_id, authorized_users}` (see "Tenant authorization model and cross-tenant isolation").

### Batch and authorization

#### POST /batch/tenants

**RBAC: operator+.** Body `{action:(stop|start|delete|backup), exactly one of ids:[...] or filter:{tag:k:v}, async?}` (ids and filter are mutually exclusive). Synchronous and asynchronous semantics: when `ids ≤ 100` and `async:true` is not carried, it synchronously returns 200 plus `{succeeded, failed}`; when `ids > 100` or `async:true` is explicit, it switches to an async job, writes the job record, and immediately returns 202 plus `{job_id, status, total}` (a background worker executes in batches); `ids > 100000` hits the hard limit and returns 400. Async requires the batch jobs table (`BATCH_JOBS_TABLE`) to be deployed, otherwise a large batch returns 503.

Main status codes:

| Status code | Trigger condition                                       |
| ----------- | ------------------------------------------------------- |
| 200         | `ids ≤ 100` and not async, executed synchronously       |
| 202         | `ids > 100` or explicit async, switched to an async job |
| 400         | `ids > 100000` over the hard limit                      |
| 503         | Large async batch but the jobs table not deployed       |

#### GET /batch/jobs/{job_id}

**RBAC: viewer.** Queries async batch progress, returning `{job_id, action, status:(queued|running|done|dispatch_failed), total, done, succeeded:[{id,action}], failed:[{id,error}], created_at, updated_at}` (does not echo the original ids list); returns 503 without the jobs table.

#### POST /tenants/{id}/access

**RBAC: only owner/admin can operate.** Explicit authorization, body `{principal, op:(grant|revoke), role?, expire_at?}`, and on grant persists to the `authorized_users` map (see "Tenant authorization model and cross-tenant isolation").

### Manage a node fleet by external user

This group of endpoints lets an external backend manage all nodes under one external user (`tenant_user_id`), backed by the tenants table's `gsi_tenant_user` index for reverse lookup (a node's `tenant_user_id` is written at creation and exists only for OIDC-federated users; it is a data fact, not an after-the-fact join). The three endpoints share authorization: an admin or api-key can manage any user; an ordinary signed-in session can manage only itself (the requested `tenant_user_id` must equal the caller token's `tenant_user_id`, otherwise 403); with RBAC off, it is admitted.

> **Note**
>
> `gsi_tenant_user` is a config-gated index (not created by default; Amazon DynamoDB can add only one GSI per update, requiring a separate deployment to transition to ACTIVE); when the index is missing it degrades (the query cannot find the index) and does not block core CRUD. Whether the target account's index is already ACTIVE is to be verified.

- **GET /users/{tenant_user_id}/tenants** (RBAC viewer): lists all nodes under the user (index query), with pagination `?limit=N` (1-1000, default 100) plus `?next_token` (opaque cursor), returning `{tenants:[...], next_token, count}`.
- **GET /users/{tenant_user_id}/summary** (RBAC viewer): a read-only summary `{tenant_user_id, total, by_status:{running:N,...}, truncated}`; internally paginates and accumulates, and over the safety limit (1000/page × 50 pages) sets `truncated:true` (does not error, indicating the result may be incomplete).
- **POST /users/{tenant_user_id}/action** (RBAC operator+, not on the viewer allowlist): performs one action in batch on all of the user's nodes, body `{"action":(start|stop)}` (only these two, no delete), with the target set obtained by an index query (no id list need be passed), reusing the lifecycle path per node (with the same IDOR plus audit), returning `{tenant_user_id, action, succeeded:[{id,action}], failed:[{id,error}], truncated}`.

### External authorization write

#### POST /external/authz

**Authorization: does not use Cognito and RBAC; uses HMAC instead.** For an external backend to write "user↔tenant" authorization (grant/revoke) into the platform, as the write-authoritative external entry for the mapping (once enabled, tenant ownership is decided by the external backend, no longer implicitly derived by the creator). Enabled only when `EXTERNAL_AUTHZ=true` (off by default, returns 404 when off).

Authorization details: the header `x-claw-authz-timestamp` is Unix seconds (±`EXTERNAL_AUTHZ_TS_WINDOW`, default 300s, anti-replay); `x-claw-authz-signature` is the hex of `HMAC-SHA256(shared secret, f"{timestamp}.{raw body}")`, compared in constant time; the uppercase `X-Claw-Authz-*` is a backward-compatibility fallback only; the shared secret is stored in AWS Secrets Manager and injected through an AWS CloudFormation dynamic reference, so the plaintext does not enter the template.

Request body: `{tenant_id, "principal"|"tenant_user_id", op:(grant|revoke), role?, expire_at?}`, written into the tenant record's `authorized_users` (the hub and control plane read the same copy).

Main status codes:

| Status code | Trigger condition                                                 |
| ----------- | ----------------------------------------------------------------- |
| 200         | Write successful                                                  |
| 401         | Missing signature / bad timestamp / out of window / bad signature |
| 404         | `EXTERNAL_AUTHZ` not enabled / tenant does not exist              |
| 503         | Shared secret not configured                                      |

### Host management

- **GET /hosts** (RBAC viewer): lists non-deleted hosts, filters internal records, and appends the overcommit ratios.
- **POST /hosts** (RBAC operator+): registers a host `{instance_id}`, obtaining the specification through an Amazon Elastic Compute Cloud (Amazon EC2) describe, returning 400 for a missing `instance_id` and 201 on success. This write endpoint depends on a real Amazon EC2 environment.
- **DELETE /hosts/{instance_id}** (RBAC operator+): marks draining plus an ASG terminate, returns 200.
- **GET /hosts/rootfs-version** (RBAC viewer): reads the version from the Amazon S3 manifest, returning unknown if missing.
- **POST /hosts/refresh-rootfs** (RBAC operator+): pushes the three image shards — rootfs, data, immutable — to all active/idle hosts, marks the version in-flight, does not wait for completion, and does not confirm; returns 500 without a manifest, and 200 `updated:0` without hosts.

Host capacity counting accounts for overcommit (`CPU_OVERCOMMIT_RATIO` / `MEM_OVERCOMMIT_RATIO`), with available capacity = (total × ratio) − used (ratio defaults to 1.0). The guest IP is computed as `block=(vm_num-1)*4`, `IP = SUBNET_PREFIX.{block//256}.{block%256+2}`.

### Skill and group management

- **GET /groups** (RBAC viewer): lists per-group skill sets.
- **POST /groups** (RBAC operator+): creates a group `{name(DNS-label required), skills?, description?}`, returning 400 for a missing/illegal name or a non-list skills, 409 for a duplicate name, and 503 without `GROUPS_TABLE`.
- **POST /groups/{name}/skills** (RBAC operator+): appends a skill (idempotent).
- **DELETE /groups/{name}/skills/{skill}** (RBAC operator+): removes a skill.
- **GET /skills/{name}** (RBAC viewer): reads `skills/{name}/SKILL.md` from Amazon S3, returning 400 for an illegal name, 503 without a bucket, 404 if missing, and 200 on success.
- **PUT /skills/{name}** (RBAC operator+): updates (validates UTF-8, ≤256KiB, at least one top-level `# Title` line), returning 400 for illegal/oversized/no-H1, 201 for new, and 200 for replace.
- **DELETE /skills/{name}** (RBAC operator+): deletes the whole prefix, returning 404 for an empty prefix and 200 `{deleted:N}` on success.

Skill distribution merges `tenant.skills + groups[tenant.group].skills`; when it returns None it broadcasts all skills.

> **Note**
>
> `GET /skills` (list, with optional `?tenant` filter) is a separate skills Lambda, not the control plane api Lambda; do not write PUT/DELETE to `/skills`.

### System and audit

- **GET /system/info** (RBAC viewer): an environment-derived feature snapshot (version, region, agentcore, metrics, multi_az, waf, cognito{rbac_enabled}, notifications, quotas, host_config), read-only.
- **GET /agentcore/status** (RBAC viewer): returns `{enabled, gateway_url}`, config-gated, disabled by default.
- **GET /agentcore/tools** (RBAC viewer): returns the hard-coded static three tools `{hello, system_info, timestamp}` (not a real-time Gateway query; returns `{enabled:false, tools:[]}` when disabled). The whole agentcore feature is config-gated and off by default.
- **GET /audit-log** (RBAC viewer): lists audits (newest-first), with query parameters `limit` (default 50, max 500) and `since` (ISO-8601); returns 200 `[]` without an audit_table. Audit best-effort records all POST/PUT/DELETE, TTL 90 days.

Amazon Simple Notification Service (Amazon SNS) lifecycle events are sent through the internal publish logic, and it is a no-op without a topic (Amazon SNS notifications are a config-gated, off-by-default capability, see "Capability boundaries").

### Quotas and execution constraints

Only when `QUOTAS_ENABLED=true` (default false) does it check whether `vcpu` / `mem_mb` / `data_disk_mb` exceed the per-tenant limit, which is off by default. microVM lifecycle execution is of two kinds: fire-and-forget (returns a CommandId, used by migrate) and synchronous polling (returns a boolean, used by launch/stop/resize). DNAT rules dynamically extract the default NIC to avoid hard-coding. The ALB rule priority randomly selects a free value and retries on conflict.

---

## Real-time chat integration

This section describes how to integrate the real-time chat path. The path uses an outbound-WebSocket hub model: the `claw-channel` inside the tenant microVM actively dials out to `claw-hub`, the browser connects to the same hub through CloudFront, the two are matched at the hub by tenant, and the microVM opens no inbound port.

The chat path is an outbound-WebSocket hub model: the `claw-channel` inside the tenant microVM actively dials out to `claw-hub` and opens no inbound port. Real-time delivery between the browser and the agent all travels over the hub's WebSocket: browser ↔ `claw-hub` ↔ `claw-channel` inside the tenant microVM ↔ agent.

### WebSocket frame structure

The WebSocket wire frame structure is as follows:

```
{
  messageId, clientMessageId, senderId,
  senderType: "USER" | "BOT",
  receiverId, receiverType,
  parts: [{ kind: "TEXT" | "FILE", text, isDone }],
  operationType, threadId, chatType
}
```

On the frontend-to-channel route, the hub uses the server-verified `sub` as `senderId` (does not trust the client assertion) and forwards to that tenant's channel.

### Reply frame types

Reply frames come in three types:

- **reply**: the final reply, `type:"reply"`.
- **reply_delta**: streaming accumulated text, `type:"reply_delta"`, `operationType:"msg_update"`. The channel sends one delta frame every 250ms, the hub forwards it to the frontend, and the frontend locates the bubble for that request by `clientMessageId` and REPLACEs (not appends), with the final `reply` frame finalizing.
- **reply_error**: failure.

### Concurrency and multi-session

Each message is stamped by the client with a `clientMessageId`, which the hub echoes verbatim on each reply/reply_delta/error frame, and the frontend routes to the exact pending request by this id rather than FIFO. Internally the channel uses a per-session serial chain (`Map<sessionKey, Promise>`), with each sessionKey processed in order and different sessions in parallel.

The client may optionally pass `threadId` (regex `^[A-Za-z0-9._:-]+$`, max 80), which the hub validates and forwards, falling back to `sub` when missing (single-thread backward compatibility). On the session-isolation dimension, the channel enforces `dmScope="per-channel-peer"`, `peerId = threadId ? "{cognitoSub}:t:{threadId}" : cognitoSub`, so each sub plus threadId gets an independent session. A malicious threadId containing injection characters is cleaned by the regex and dropped, falling back to `sub`, and an overlong one (>80) is truncated.

### Outbound and inbound image paths

The media path splits into inbound (viewing an image) and outbound (producing an image):

- **Inbound (viewing an image)**: user uploads → the frontend presigns a PUT to Amazon S3 through the hub → sends a FILE frame containing `fileKey` → the hub forwards → the channel calls `/files/download-url` to get a presigned GET → validates MIME and size locally, then writes a temp file → passes to core multimodal.
- **Outbound (producing an image)**: the agent references a local file → the channel validates MIME and size and uploads to Amazon S3 through hub presign → `fileKey` → sends a FILE frame → the hub forwards → the frontend calls `/files/download-url` to get a presigned URL.

Image protection uses a MIME allowlist (image/png, image/jpeg, image/gif, image/webp, and so on), at most 4 images per message; identity files (SOUL.md, AGENTS.md, IDENTITY.md, and so on) are forbidden to upload through outbound media.

> **Note**
>
> The upload size limit (20MB) is a soft constraint: PUT presign cannot use ContentLengthRange and relies on ContentType pinning plus a declared-size soft validation, not an Amazon S3 hard enforcement.

### File endpoints and IDOR protection

Both `/files/upload-url` and `/files/download-url` validate the hub token in the Bearer header or `?token=` (both frontend and channel tokens are accepted). The file's storage path in Amazon S3 is segmented by tenant, and when getting the download link, the tenant segment in the path is validated to match the tenant in the token (preventing unauthorized access to another's file, not even leaking "whether it exists" across tenants). When the frontend sends a file frame, the hub also validates that the file ownership equals the frontend's tenant, otherwise it silently drops it. The cross-origin (CORS) of the Amazon S3 media bucket admits only a single Amazon CloudFront domain, limits methods to GET/PUT, and does not open `*`.

Amazon CloudFront is AWS's global content delivery network that caches and accelerates at the edge; the solution uses it as the sole public entry (Amazon CloudFront → ALB), reverse-proxying the chat UI and the hub. Amazon CloudFront's `/hub/*` behavior is configured as `origin_request_policy=ALL_VIEWER` (forwards all request headers including Upgrade and Sec-WebSocket-\*), `cache_policy=CACHING_DISABLED`, `allowed_methods=ALLOW_ALL`, origin read_timeout=60s (corresponding to the WebSocket long connection).

> **Important**
>
> The readiness check for a media object is currently just a placeholder: an object that can be fetched is considered ready, with no virus or content scanning connected. An integrator should not claim externally that uploaded files have undergone content security scanning; to scan, add it yourself to the path (see "Capability boundaries").

### hub-side HTTP status code quick reference

The table below summarizes the return codes on the hub side (`/token`, `/channel-token`, `/files/*`, WebSocket). The status codes of the control plane REST endpoints are inline with each endpoint.

| Status code | Endpoint / trigger condition                                                              |
| ----------- | ----------------------------------------------------------------------------------------- |
| 401         | `/token`: no `Bearer ` prefix / Cognito verification failed or no sub                     |
| 400         | `/token`: body has no `tenant_id`                                                         |
| 403         | `/token`: tenant authorization check failed (not the tenant's owner/authorized/shared)    |
| 200         | `/token`: returns `{token, expires_in:300}`                                               |
| 401         | `/channel-token`: channel signature validation failed (signature/time window/secret)      |
| 200         | `/channel-token`: returns the channel token                                               |
| 503         | `/files/*`: `ASSETS_BUCKET` not configured                                                |
| 401         | `/files/*`: no valid hub token                                                            |
| 415         | `/files/upload-url`: mimeType not on the allowlist                                        |
| 413         | `/files/upload-url`: declared size exceeds `MEDIA_MAX_BYTES`                              |
| 404         | `/files/download-url`: fileKey shape wrong / cross-tenant (IDOR, does not leak existence) |
| 202         | `/files/download-url`: object not yet ready (HeadObject failed)                           |
| 500         | hub endpoints: uncaught-exception fallback                                                |
| WS 1008     | hub WebSocket handshake: hub token verification failed, sends error frame then closes     |

> **Note**
>
> The 200 response body of the hub-side media endpoints is wrapped as `{code:"000000", success:true, data}`.

### Error handling and timeouts

- An empty message (no text and no image) is rejected by the hub, returning `{type:"error", error:"empty message"}`.
- An offline agent (no channel connection found for the tenant) returns `{type:"error", error:"agent offline"}`.
- A bad JSON frame gets `{type:"error", error:"bad json"}` from the hub, which the channel side try-catches and ignores without crashing the connection.
- When the frontend's exchange for a hub token (`POST /token`) returns 401, it first silently exchanges for a new id_token with the refresh_token and retries once, only clearing the local token and jumping to the Hosted UI to re-sign-in if the exchange fails.
- The frontend automatically backs off and retries on transient faults (Amazon CloudFront jitter, WebSocket reconnect windows, token TTL boundaries), with a 12s total timeout per handshake.
- The channel logs key decisions (token-fail, inbound-media-skip) locally for audit.

### Minimal end-to-end integration sequence

The numbered steps below describe the shortest path for the frontend from sign-in to sending and receiving one message, stitching together authentication and the frame structure (curl illustrative, `{...}` placeholders, credentials `[REDACTED]`).

1. **Obtain the Cognito id_token**: through the Cognito Hosted UI's authorization-code + PKCE (optionally through external OIDC federation), use `code` plus `code_verifier` to call `/oauth2/token` to exchange for the id_token plus refresh_token.
2. **Exchange for a frontend hub token**: `POST /hub/token` (through Amazon CloudFront `/hub/*` → ALB → hub), `Authorization: Bearer {id_token}`, body `{"tenant_id":"{tenant}"}`; the hub returns `{token, expires_in:300}` after verifying JWKS plus the authorization check, and 403 on authorization failure:

   ```
   POST https://<your-distribution>.cloudfront.net/hub/token
   Authorization: Bearer [REDACTED]

   {"tenant_id":"{tenant}"}
   → 200 {"token":"[REDACTED]","expires_in":300}
   ```

3. **Connect the WebSocket**: `GET wss://<your-distribution>.cloudfront.net/hub/ws?token={hub token}`; on success it receives `{type:"ready", tenant}`, and on verification failure it receives an error frame plus close 1008.
4. **Send a message**: send one frame over the WebSocket: `{messageId, clientMessageId:"{uuid}", senderType:"USER", parts:[{kind:"TEXT", text:"hi", isDone:true}], threadId?}`; `clientMessageId` is a client-stamped UUID used for reply reconciliation.
5. **Receive the reply**: on the same WebSocket, receive `reply_delta` (streaming accumulation, REPLACE the current bubble) and the final `reply` frame to finalize, by `clientMessageId`, with failure receiving `reply_error`. Near hub-token expiration (the frontend's 270s threshold) it actively reconnects for a new one, and on id_token expiration the refresh_token silently exchanges for a new one.

Producing and viewing images additionally require the two steps `/files/upload-url` (PUT presign) and `/files/download-url` (GET presign).

---

## Tenant authorization model and cross-tenant isolation

This section describes the solution's tenant authorization model, including the authorization fields, the grant/revoke operations, the hub-side authorization decision, the control-plane RBAC and owner check, and the cross-tenant isolation mechanism.

### owner_id and authorized_users

The authorization fields in the Amazon DynamoDB tenants table are `owner_id` (the creator's identity) and `authorized_users` (the explicit authorization map); in addition, at registration two fields are written on demand: `tenant_user_id` (attribution) and `litellm_vkey` (billing). `owner_id` is written after the caller's identity is obtained at registration, and its value can be a Cognito sub (a user with a verified id_token), `API_KEY_OWNER` (value `api-key`, a keyed call without a Bearer), None (has a Bearer but verification failed), or an empty string (a legacy record). The `authorized_users` structure is `{<sub>:{role, granted_at, expire_at?}, ...}`.

### grant and revoke

`POST /tenants/{id}/access` provides authorization writes:

- **grant**: `principal + op="grant" + role + expire_at` (optional epoch) writes `authorized_users[principal]={role, granted_at, expire_at}`.
- **revoke**: `principal + op="revoke"` removes the principal from `authorized_users`.
- Preconditions: only owner/admin can operate; the owner identity cannot be modified by grant/revoke, and an attempt returns 400.

`GET /tenants/{id}/access` returns `{id, owner_id, authorized_users}`, likewise owner/admin-gated.

### hub-side authorization decision

The hub-side determination of "whether a user can access a tenant" is the single source of truth that all authorization points (`/token`, `/files`, WebSocket) consult uniformly, with the decision order as follows:

1. `owner_id === sub` → allowed, role is `owner`.
2. `sub ∈ authorized_users` and not expired → allowed, role from the grant.
3. `owner_id ∈ {"api-key", ""}` and `CLAW_HUB_SHARED_TENANT_ACCESS=true` → allowed, role is `shared`; when off by default, directly denied (the go-live default).
4. Amazon DynamoDB error → denied (fail-closed).
5. Others (someone else's private tenant) → denied.

The authorization read takes only the two fields owner and authorization list to save read capacity, and a missing owner is treated as a legacy record. The expiration judgment for authorization is done only on the hub side: no expiration time set is treated as long-term valid, and if set, it is compared against the current time. Both authorization and secrets carry an about-60-second local cache to reduce database pressure, at the cost of a maximum visibility delay of about 60 seconds for credential and authorization rotation.

### Control-plane RBAC and owner check

The control-plane owner/admin check: admins and api-key callers bypass, others need `owner_id` to match, in effect only when `RBAC_ENABLED=true`. `GET /tenants`, when RBAC is on, filters so a non-admin Cognito user sees only tenants of their own `owner_id`; per-tenant routes such as `GET /tenants/{id}` and `DELETE /tenants/{id}` all perform this check, returning 403 on failure. The Cognito verification chains on the control-plane and hub sides are consistent, with a single trust root.

### Cross-tenant isolation

Cross-tenant isolation is overlaid at two layers.

On the chat path, cross-tenant is structurally blocked: the frontend token carries only one tenant, the channel registers only one tenant, and when the hub routes it requires the frontend WebSocket's tenant and the channel's tenant to match, otherwise it routes to an empty channel (agent offline). Reply unicast: the channel sends a reply frame with `receiverId=cognitoSub`, and the hub fans out over the WebSocket set of all tabs of that sub.

Network-layer isolation is enforced by the host iptables (Firecracker itself does not filter traffic): the three per-tap DROPs (IMDS, cross-tenant east-west, management-plane ports) are all inserted at the top of the chain to take effect first. 100% cross-tenant packet loss after hardening is the hardened target state (the vulnerable state was 0% packet loss, and a fresh, timestamped bare-metal re-test after hardening is to be verified); an IMDS DROP manifests as a connection timeout or no route, not a 401 (the 401 is the application-layer response of IMDSv2 without a token, a different semantics). On the in-microVM guardrail side, jailbreak interception is the Amazon Bedrock Guardrail at the LLM and LiteLLM layer (not inside the image), and malicious skills and credential reads are intercepted by the in-image sentinel-guard and acl-guard. The full layering of this network and content isolation is described in "Plan the Deployment — Security".

---

## Capability boundaries

This section describes the capability boundaries that must be honestly annotated when integrating the solution. The capabilities below are either historical residue, sample scaffolding, or off-by-default optional switches, and an integrator should not depend on them directly as production-grade.

### chatCompletions endpoint

OpenClaw's native `POST /vm/{tenant}/v1/chat/completions` endpoint is disabled by default in source. This architecture explicitly deletes it: when injecting openclaw.json at microVM launch, it deletes `gateway.http.endpoints.chatCompletions` (the same step also deletes `dangerouslyDisableDeviceAuth` and narrows `allowedOrigins` to a single Amazon CloudFront origin), so no tenant's microVM will inject this endpoint. `gateway_token` still exists in the system, but its purpose is unrelated to the chat path: the host side reads the gateway auth token from the microVM's openclaw.json and writes it into the Amazon DynamoDB tenant record, for an administrator console to assemble an optional `/vm/{tenant}/` control UI link (this control UI is an administrator bypass orthogonal to consumer chat and does not participate in chat). The control plane REST API's own authentication uses the Amazon Cognito id_token (RS256) + `x-api-key` + RBAC, and does not read `gateway_token`. The external chat entry does not pass through the gateway HTTP endpoint at all.

> **Important**
>
> If an integrator needs an external chat endpoint, the correct approach is to make it an on-demand per-tenant switch (injecting `enabled:true` at rebuild only for the tenants that truly need it), not to turn it on globally. This endpoint is a bypass that goes directly to the LLM and skips the control UI device authentication, and turning it on globally is one more external attack surface for every tenant. This per-tenant switch is implemented: a tenant that passes `chat_endpoint_enabled:true` at registration (default false, `deploy/lambda/api/handler.py:1309`) has its DynamoDB record set (`handler.py:1509/1640`) and passed as a parameter to the launch script (`handler.py:1691`, injection decision `handler.py:3431`), and only then is `chatCompletions.enabled:true` injected at launch; tenants not set still have the endpoint explicitly deleted, safe by default.

### POST /chat/sign orphan path

`POST /chat/sign` (RBAC viewer plus owner gating) verifies the RS256 signature of the Cognito id_token, takes the server-derived `sub` from the claims, uses the tenant's `channel_secret` to HMAC-sign the `{sub,text}` envelope, and returns `{path, body, headers}`, with the return headers containing `x-claw-signature`, `x-claw-random`, and `x-claw-timestamp`.

> **Important**
>
> This endpoint has no paired live inbound verifier: `claw-channel` is an outbound-WebSocket model and opens no inbound port, and there is no `x-claw-*` signature validation logic anywhere in the code. Although the launch configuration configures nginx to reverse-proxy `/chat/{tenant}/` to an internal microVM port, the channel process has no webhook server listening on that port. This "sign → through nginx → microVM inbound" is historical design residue, and an integrator should not treat it as a live contract; consumer chat delivery actually all travels over the hub WebSocket. This orphan path still has IDOR plus Cognito verification gating, so even if called by mistake, only owner/admin can sign their own tenant, and it does not constitute a new attack surface. Whether there is still a live caller is to be verified.

### agentcore and media placeholders

- `/agentcore/tools` returns the hard-coded static three tools (`hello`, `system_info`, `timestamp`); it is a stub, not a real-time Gateway query; the whole agentcore feature is config-gated and off by default (returns `{enabled:false, tools:[]}` when disabled). An integrator should not treat these three tools as a real tool list.
- The media-object readiness check is a stub: the readiness determination of `/files/download-url` is that HeadObject 200 is considered ready, with no virus or content scanning connected. To scan, add it yourself to the path.
- Sample skills such as quantitative trading, settlement, and square publishing are samples: the framework is there, but end-to-end depends on external conditions (such as a testnet key, an external settlement backend, a publish placeholder interface) and is not a platform-guaranteed production capability.

### config-gated off-by-default list

The table below lists capabilities that are ready in CDK but off by default and must be explicitly enabled in the target environment. An integrator should not design the integration as "works out of the box", and should confirm whether the target environment actually has them enabled before integrating.

| Switch                                                                                                                                                     | Affected integration surface                                                                                                                                                                                                                              | Default                                                                                                          | How to enable                                                                                                                       |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `gsi_tenant_user` index                                                                                                                                    | The three "manage a node fleet by external user" endpoints `GET /users/{id}/tenants`, `GET /users/{id}/summary`, `POST /users/{id}/action` all depend on this index; when missing they degrade and core CRUD is unaffected                                | Not created by default                                                                                           | Enable in config plus a separate deployment, and after `gsi_owner` is ACTIVE, transition `gsi_tenant_user` to ACTIVE                |
| `chatCompletions` endpoint                                                                                                                                 | Integrators wanting an external chat HTTP endpoint                                                                                                                                                                                                        | Explicitly deleted by default (injected only for tenants that pass `chat_endpoint_enabled:true` at registration) | The per-tenant switch is implemented (`handler.py:1309/1509/1691/3431`); enable per tenant, not globally                            |
| `EXTERNAL_AUTHZ`                                                                                                                                           | Once enabled, `POST /tenants/self` self-registration returns 403; the `POST /external/authz` write-authoritative entry is enabled (returns 404 when off)                                                                                                  | Default false                                                                                                    | env / config `external_authz` section; requires the external party to provide an HMAC shared secret in the database for integration |
| `RBAC_ENABLED`                                                                                                                                             | When off, the control plane performs no per-route role check and owner check (fully open)                                                                                                                                                                 | Default true (enforced by default, the exception on this list that is on by default)                             | Set false explicitly only for a demo/dev that wants to be fully open                                                                |
| `CLAW_HUB_SHARED_TENANT_ACCESS`                                                                                                                            | Once enabled, shared or legacy nodes (owner=api-key or empty) admit the shared role; when off, fail-closed and directly denied                                                                                                                            | Default off                                                                                                      | env explicit true                                                                                                                   |
| `SELF_MAX_NODES_PER_USER`                                                                                                                                  | The per-user node limit for `POST /tenants/self`, returning 409 when the limit is reached                                                                                                                                                                 | Default 1 (0 means unlimited)                                                                                    | env                                                                                                                                 |
| AZ failover / host monitoring / GuardDuty / Amazon SNS notifications / metrics dashboard / WAF / DNS Firewall / balloon overcommit / Amazon SQS throttling | Operations-side capabilities such as external alert delivery, lifecycle throttling, and memory overcommit; when Amazon SNS notifications are off, events are a no-op, and when Amazon SQS is off, lifecycle actions go synchronous rather than 202 queued | All off by default                                                                                               | Each in its own config section / env                                                                                                |

### claw-hub deployment form

What an integrator connects to is the hub WebSocket (through Amazon CloudFront `/hub/*` → ALB → hub). Production currently runs a single-process metal hub (systemd), and the cross-Pod Redis routing (cluster-routing) code is complete and degrade-safe, but the Amazon Elastic Kubernetes Service (Amazon EKS) multi-replica canary has not yet been switched to production (target state / in canary). This is a deliberate progressive switch, and the integration contract (`/hub/token`, `/hub/ws`, frame structure) is consistent under single-process and multi-replica, so integrator code need not distinguish. The default port is `CLAW_HUB_PORT=8790`.

> **Note**
>
> What can be integrated directly as a production contract are the authentication model, the control plane REST API, and the authorization model (the three sections: authentication model, control plane REST API, and tenant authorization model and cross-tenant isolation); the capabilities listed in this section are either not integrated (orphan paths), treated as a sample starting point (scaffolding), or depended on only after confirming the switch (config-gated).
