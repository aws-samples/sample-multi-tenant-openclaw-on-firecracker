# Developer Guide

This chapter is for control plane API and real-time chat integrators. The
field-level REST contract is [`openapi.yaml`](../aws-guide/openapi.yaml). The
data-plane contract is
[`13-data-plane-redesign.md`](13-data-plane-redesign.md) and
`engineering/backend/lib/gw-ws.mjs`.

## Authentication And Authorization

### Control Plane

All current control plane methods require an Amazon API Gateway `x-api-key`.
The value identifies a usage-plan client and enables quota and throttle
accounting. It is not a standalone authentication or authorization mechanism.
AWS documentation explicitly warns against using API keys for that purpose,
and usage-plan quotas and throttles are best effort.

When Amazon Cognito is enabled, a caller can also send
`Authorization: Bearer <id_token>`. Lambda verifies RS256, issuer, expiration,
and the configured client id through the JSON Web Key Set (JWKS), then derives
the caller identity and highest `cognito:groups` role.

Roles are ordered `viewer < operator < admin`:

- `viewer` can call read routes and `POST /tenants/self`.
- `operator` can call ordinary write routes.
- `admin` can perform fleet-wide and explicitly admin-only operations.

`console_auth.default_no_jwt_role` controls the role of API-key-only requests.
The repository default is `viewer`, so the default path is read-only. A trusted
internal deployment that permits writes with only `x-api-key` must explicitly
select `operator` or `admin` and add a real boundary such as private networking,
IAM authorization, or a Lambda authorizer.

Resource authorization is separate from the role gate:

- A regular Cognito user can access only tenants where `owner_id == sub`.
- An admin and an unscoped trusted API-key path can operate across owners.
- A platform-scoped API key can access only tenants with the same `platform_id`.
- An invalid Bearer token has no owner identity and returns `403`; it never
  degrades into the trusted API-key-only identity.

### Data Plane

The browser carries only the customer's platform session token to
`wss://<platform>/gw/ws?token=<platform-session-token>`. The platform backend
authenticates the user, selects a tenant from server-side records, and connects
to the OpenClaw gateway with tenant credentials:

| Credential | Purpose | Storage and decryption boundary |
| --- | --- | --- |
| Gateway token | OpenClaw gateway bearer authentication | KMS ciphertext in `openclaw-tenant-secrets`; the platform backend decrypts with the `tenant_id` encryption context |
| Ed25519 device private key | Signs `connect.challenge` | KMS ciphertext decrypted with the `owner_id` encryption context; the public key is cold-injected into `paired.json` |
| Platform session token | Browser to platform `/gw/ws` | Issued and verified by the customer platform; not sent to the ClawPool control plane |

The browser never receives `x-api-key`, a gateway token, or a device private
key. The retired `claw-hub`, `claw-channel`, `/hub/token`, `/hub/ws`,
`/channel-token`, `/chat/sign`, and hub file-presign endpoints are not
compatibility paths.

## Control Plane REST API

### Common Conventions

- Write requests use `Content-Type: application/json`.
- Structured errors normally use `{"error":"...","code":"..."}`; branch on
  `code`, not message text.
- `client_token` is a 4–128 printable-ASCII idempotency key.
- Legacy lists can return a bare array without `limit`; pagination returns an
  envelope when `limit` or `next_token` is supplied.
- A `202` response means accepted, not completed. Poll the resource or job.
- Server redaction is mandatory, but integrators must still avoid forwarding
  control plane responses verbatim to an untrusted frontend.

### Create And Query Tenants

`POST /tenants` requires operator or higher. `name` is the only universal
required field; OpenAPI defines CPU, memory, disk, skills, scheduling,
attribution, purchase semantics, restore, and security options.

The ID shape depends on the idempotency input:

- With `client_token`: `t-<16hex>`, stably derived from owner and token.
- Without `client_token`: `<name>-<4hex>`.

`creating`, `pending`, and `queued` mean that work entered the lifecycle. The
completion condition is `GET /tenants/{id}` reaching `status=running`; when
application readiness matters, also require `app_health=up`.

`GET /tenants` supports owner/platform scope, tags, and cursor pagination and
redacts server credentials. `GET /tenants/{id}` first redacts the base record;
for a `running` tenant it then adds the gateway-token KMS ciphertext plus device
id, public key, private-key KMS ciphertext, and scopes behind the owner/admin
gate. `GET /tenants/{id}/credentials` can rewrap the same credentials in the
recipient-key `asymmetric-v1` envelope. Neither response belongs in a browser.

### Lifecycle And Delete

`POST /tenants/{id}/{action}` supports the start, stop, restart, pause, resume,
reset, rebuild, backup, resize, resize-disk, migrate, access, and provision
operations listed in OpenAPI. Poll asynchronous actions to the target state.

`DELETE /tenants/{id}` is idempotent. `keep_data=false` enters the data-removal
path. `skip_backup=true` skips the pre-delete backup and must be treated as an
explicit data-risk choice.

### Image Lifecycle

The image API supports live/canary slots and CAS promotion:

- `POST /create-image-snapshot` requires a valid non-empty `label`; the control
  plane does not derive an empty label.
- `POST /hosts/{id}/pull-image?slot=canary` stages a candidate.
- `GET /hosts/{id}/pull-image-progress` and
  `GET /hosts/{id}/image-slots` return job and host slot state.
- `POST /hosts/{id}/promote-canary` promotes the verified candidate.
- `POST /hosts/{id}/reclaim-images` removes unreferenced versions.

See [`../api/pull-image-api.md`](../api/pull-image-api.md) for conflicts,
idempotency, and rollback.

### External Platforms And Authorization

When `EXTERNAL_AUTHZ=true`, `POST /external/authz` accepts HMAC-signed
grant/revoke updates and writes `authorized_users`. The signature covers
`timestamp.raw_body` and is constrained by a replay window.

`GET /tenantmatch` has a handler, table, and IAM grant, but no API Gateway
resource. It is documented-but-unreachable and must not be an integration
dependency.

## Real-Time Chat Integration

### Request Path

```text
Browser
  -> wss /gw/ws (platform session token)
  -> platform backend (tenant authorization)
  -> /ws/{tenant_id} (gateway token + Ed25519 device handshake)
  -> CloudFront -> ALB -> OpenResty edge -> host DNAT
  -> microVM OpenClaw gateway :18789
```

The platform gateway must:

1. Verify the platform session token and never trust a browser-supplied tenant.
2. Select a running tenant from server-side owner/platform authorization.
3. Obtain and decrypt KMS ciphertext in process with the correct contexts.
4. Answer `connect.challenge` with an Ed25519 signature and gateway token.
5. Map frontend `{text, clientMessageId, threadId}` to OpenClaw `chat.send`.
6. Map upstream events to `reply_delta`, `reply`, or `reply_error`.

The reference gateway first sends `gw_status:connecting`. A provisioning tenant
receives `gw_status:provisioning` and close 4409; no tenant closes 4404;
credential or handshake failure closes 4502; upstream close uses 4503. A
successful handshake sends `gw_status:ready` and a compatibility `ready` frame.

`threadId` accepts only `[A-Za-z0-9._:-]{1,80}`. `clientMessageId` is the
upstream idempotency and reply-correlation key. Empty `text` returns `gw_error`.

### Version Boundary

The current `bb` golden image is pinned to OpenClaw `2026.7.1-2` (protocol v4,
#476). Keep the `-2` suffix verbatim: under semver it is a prerelease of
`2026.7.1`, so a bare `2026.7.1` resolves to a different published build. The
platform gateway advertises a protocol **range**, `GW_PROTOCOL_MIN`..
`GW_PROTOCOL_MAX` (default `3..4`), so new v4 tenants and existing `2026.2.26`
(v3) tenants coexist during the transition. An OpenClaw upgrade must jointly
verify the `build-rootfs.sh` pin, the `OPENCLAW_NODE_MIN` engine floor, the
template schema gate, the `paired.json` shape, and the protocol range, then run
a remote-topology device handshake.

## Capability Boundaries

- The two-tier data plane is gated by `edge.enabled` and `redis.enabled`, both
  off by default.
- The `/chat/sign` API Gateway resource remains, but the Lambda route is gone;
  it is not a valid contract.
- No compatibility layer replaces the retired hub media-presign API. File
  support requires platform-side tenant authorization, MIME/size checks, and
  Amazon S3 presigning.
- `chat_endpoint_enabled` is a per-tenant direct HTTP switch, off by default;
  it is not an alternative authentication path for `/gw/ws`.
- AgentCore, AWS WAF, GuardDuty, Wazuh, managed metrics, external authorization,
  and several operational features are configuration-gated. Read
  `/system/info` and verify the deployed environment before depending on them.

## Verification

The static contract is `docs/aws-guide/openapi.yaml`. The control plane customer
regression entry point is `tests/api-regress/oc-regress.sh`; it creates, polls,
stops, starts, rebuilds, and deletes a test tenant and must run only in an
authorized test environment. Data-plane reference tests are
`engineering/backend/test/gw-ws-device.test.mjs` and
`engineering/backend/test/e2e-isolation.mjs`.
