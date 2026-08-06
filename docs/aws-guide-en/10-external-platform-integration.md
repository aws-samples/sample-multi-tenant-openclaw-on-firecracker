# External Platform Integration

This chapter is for customers integrating ClawPool into a SaaS product, trading
platform, or enterprise portal. The current path is:

- The customer platform owns end-user identity, login, and sessions.
- The customer backend calls the ClawPool control plane to manage tenants.
- The browser connects only to the customer platform `/gw/ws`.
- The customer backend holds each tenant's gateway token and Ed25519 device
  private key and connects to the microVM.

The earlier design that federated the customer IdP into ClawPool Cognito and
exchanged hub tokens is retired.

## 1. Responsibility Boundary

| Responsibility | Customer platform | ClawPool |
| --- | --- | --- |
| End-user registration, login, session | Owns | Never receives user passwords |
| Purchase, quota, refund, deactivation | Owns | Receives lifecycle calls |
| User-to-tenant mapping | Creates and stores | Stores `owner_id`, `tenant_user_id`, `platform_id` |
| Browser real-time entry | `/gw/ws` | No hub-token exchange |
| Second-hop authentication | Holds and decrypts tenant credentials | Verifies gateway token and Ed25519 device |
| microVM, scheduling, image, backup | No | Owns |

## 2. Control Plane Identity

Every control plane request carries `x-api-key`, but the API key is only an API
Gateway usage-plan identifier, not standalone authentication. Writes also need
one of these deployment contracts:

1. A valid operator/admin Cognito Bearer token.
2. An explicitly trusted private deployment with
   `console_auth.default_no_jwt_role=operator|admin`, protected by IAM SigV4, a
   private API resource policy, or a Lambda authorizer.

Use a platform-scoped API key/authorizer in production so a caller is limited to
its own `platform_id`. Never expose an API key or control plane Bearer token to
the browser.

## 3. User Attribution

The customer platform maintains:

- `owner_id`: a UUID used by the control plane owner gate.
- `tenant_user_id`: the stable customer-platform user id; prefix it with the
  platform name.
- `platform_id`: the platform namespace.

`tenant_user_id` is attribution and query data, not an authorization credential.
The backend must derive all three values from a verified platform session and
must not trust browser-supplied owner or platform values.

## 4. Activation

```text
Browser -> customer backend: activate assistant
customer backend:
  1. verify platform session
  2. run purchase/quota checks
  3. POST /tenants with control-plane credentials
  4. poll GET /tenants/{id}
  5. return readiness only
```

Example:

```json
{
  "name": "assistant-user-123",
  "client_token": "market:user-123",
  "owner_id": "11111111-2222-3333-4444-555555555555",
  "tenant_user_id": "market:user-123",
  "platform_id": "market"
}
```

With `client_token`, the ID has shape `t-<16hex>`. `creating`, `pending`, and
`queued` are not completion. Poll until `status=running`; require
`app_health=up` when application readiness is needed.

## 5. Chat

```text
Browser
  -> wss://<customer-platform>/gw/ws?token=<platform-session-token>
  -> customer backend
  -> /ws/{tenant_id}
  -> CloudFront -> ALB -> OpenResty -> host DNAT
  -> microVM gateway :18789
```

The customer backend:

1. Verifies the platform session and selects a tenant from server-side data.
2. Calls `GET /tenants/{id}` or `/credentials` for gateway/device ciphertext.
3. Decrypts in process with the correct KMS context or recipient envelope.
4. Completes Ed25519 `connect.challenge` signing and gateway-token auth.
5. Maps `chat.send` and `reply_delta` / `reply` / `reply_error`.

The browser never receives ClawPool credentials. The retired `/hub/token`,
`/hub/ws`, `/channel-token`, and hub file endpoints do not exist.

## 6. Per-User Operations

- `GET /users/{tenant_user_id}/tenants`: list the user's tenants.
- `GET /users/{tenant_user_id}/summary`: summarize by state.
- `POST /users/{tenant_user_id}/action`: batch start/stop.
- `POST /external/authz`: in external-authorization mode, write HMAC-signed
  grant/revoke updates.

Owner, platform-scope, and RBAC checks still apply.

## 7. Security Requirements

- API keys, control plane Bearers, gateway tokens, and device private keys stay
  in backend processes.
- Never forward `GET /tenants/{id}` or `/credentials` responses to a browser.
- KMS decrypt must use the exact `tenant_id` or `owner_id` context used at
  encryption time.
- Keep `client_token` stable so retries cannot create duplicates.
- File support requires platform-side tenant authorization, MIME/size checks,
  and Amazon S3 presigning.
- Cross-Region calls need explicit timeouts, retries, and idempotency. Do not
  treat a network timeout as proof that creation failed.

## 8. Reference Implementation

`engineering/backend/` demonstrates platform JWTs, `/gw/ws`, the control-plane
client, and the device handshake. `console/marketplace-demo/` is a historical
test bed and can still contain retired Cognito/hub scenarios; it is not the
current contract. Before launch, use Chapter 9, OpenAPI, Chapter 13, and the
target-environment regression suite.
