# External Platform Integration Guide (from the customer's perspective)

This section is for **customers who want to integrate ClawPool into their own platform** (a trading platform, a secondhand marketplace, any SaaS). By the end you will be able to give every one of your platform's free users a dedicated AI assistant (an independent openclaw), entirely through your own account system, with the user never touching any underlying console.

> ⚠️ **Note (superseded): the identity approach in this chapter has changed — use only the "backend create-on-behalf + platform-issued session token" path**
>
> This chapter was originally written against the Amazon Cognito federation ADR (`ADR-dataplane-external-saas-auth`, Track A). That federation approach was marked **SUPERSEDED** on 2026-07-07 (reason: the customer's OIDC does not support page-less login), and identity has converged on OpenClaw's native gateway authentication. The integration path that is **still valid and recommended** today:
>
> - **Tenant activation**: your backend holds the `x-api-key` that ClawPool issued to you and calls `POST /tenants` on the user's behalf (`owner_id` + `tenant_user_id` attribution: see §2), optionally scoping by `platform_id`. This control-plane path is live.
> - **Real-time chat**: the data plane goes through two-tier routing (platform backend WebSocket gateway → edge → microVM gateway). The first hop uses **your platform's self-issued session token** (an HS256 JWT, not a Cognito `id_token`); the second hop uses OpenClaw's native Ed25519 device authentication. It no longer goes through `POST /hub/token` + `wss /hub/ws` (the hub has been removed). See chapter 13, _Data-plane two-tier routing_.
>
> Everything below that describes "federating your IdP into ClawPool's Cognito User Pool", "Pre-Token-Generation claim injection", or "exchanging tokens via `/hub/token`" belongs to the superseded approach and is kept only to explain the evolution — do not build against it.

> Positioning distinction: the previous section, the _Control Plane API Integration_, is a **per-endpoint reference**; this section is an **integration approach and steps** — how your platform should connect, how to thread authentication, and how users use it. The term "the platform / ClawPool" refers to the integratee (which provides the openclaw pool), and "your platform" refers to the integrator (the customer's own platform).

## 1. What It Looks Like After Integration

- Your users log in on **your own platform** with **your own account** (they do not register a new ClawPool account).
- A user "activates the AI assistant" inside your platform → **your backend** spins up a dedicated openclaw microVM for that user.
- The user enters the AI assistant interface (embedded under your domain) → real-time chat.
- Each user's openclaw is isolated from the others; one user cannot see another user's sessions/data.

## 2. Three Integration Decisions (settle these three first)

**① How to connect identity (federation first)**
Register your platform's IdP (OIDC or SAML) as an upstream IdP of ClawPool's single Cognito User Pool. When your users log in they authenticate through your IdP, and Cognito federation issues a JWT that ClawPool trusts. **You do not need to import users into ClawPool, nor do you need a ClawPool account system** — the trust root remains ClawPool's Cognito, but the user identity comes from you. If your platform has no independent IdP, you can also simply use the Cognito app client that ClawPool allocates to you for username/password login (the simplest).

**② Who creates the tenant (your backend creates it on the user's behalf, not user self-service)**
The user clicks "activate the AI assistant" → your frontend calls **your backend** with the user's JWT → your backend, holding the `x-api-key` that ClawPool issued to you, calls `POST /tenants`. This way you can insert your own **purchase / billing / quota** logic before activation, and the user does not create nodes directly. ClawPool provides the self-service registration endpoint `POST /tenants/self`, but **going through backend-side creation is recommended** to preserve your commercial gating.

> **Note** Tenant ownership takes one of two paths — pick one by how you call, and **do not mix them**. **Path 1 · api-key-only create-on-behalf (recommended, simplest)**: the request carries only `x-api-key` (no user Bearer), and you pass `owner_id` (the user's `sub` in ClawPool's Cognito, a UUID) and `tenant_user_id` (your platform's stable user id) directly in the body; the control plane validates them and persists. **Path 2 · forward the user's `id_token`**: the request carries `x-api-key` plus the user's Bearer `id_token` (federation-issued), and ownership is derived automatically from the token (`sub` → `owner_id`, the `custom:tenant_user_id` claim → `tenant_user_id`); **on this path the body must NOT carry those two fields — if it does, the create returns 403** (the owner may only come from a verified token, preventing a node from being created under someone else's name). `platform_id` may be passed in the body on **either** path (optional, matching `^[a-zA-Z0-9._-]{1,128}$`; tags the owning platform when an external platform creates on behalf of its users; see `tenant_service.py:252` / `core/utils.py:110`).
>
> **`tenant_user_id` is a data-attribution label, not an authorization credential**: it does not take part in chat/lifecycle authorization decisions (authorization goes through `owner_id`/`authorized_users`), but once you stamp a `tenant_user_id` onto a tenant, any federated-login user holding the same `custom:tenant_user_id` claim can list that tenant's metadata (no credentials) via `GET /users/{tenant_user_id}/tenants` — stamping the field means you allow that user to see this list. Use a **globally unique** user id (prefix it with your platform, e.g. `yourplatform:12345`); different platforms using the same bare numeric id would otherwise see each other's same-numbered users' node lists.

**③ Where to put the AI assistant interface (embed it in your domain)**
The AI assistant frontend (chat UI) is embedded under your domain, and the user enters seamlessly from your logged-in session (no redirect to an unfamiliar domain). The frontend uses the federation-issued JWT to exchange for a real-time chat token. ClawPool provides a reference implementation (Hosted UI + authorization_code + PKCE + refresh, federation-ready).

## 3. Integration Steps

**Step 1 · Register your platform**
Contact ClawPool operations and provide: your IdP metadata (OIDC issuer + JWKS URL, or SAML metadata), your `platform_id`, and callback URLs. On the ClawPool side: register your IdP as a Cognito upstream provider + configure Pre-Token-Generation to inject `custom:tenant_user_id`/`custom:platform_id` + issue you an `x-api-key` (used for backend-side tenant creation, kept server-side, kept out of the frontend).

**Step 2 · Wire up federated login on the frontend**
Your frontend initiates an `authorization_code + PKCE` login to the Cognito Hosted UI, carrying `identity_provider=<your platform_id>` to jump straight to your IdP (skipping the selector). After logging in, the user obtains an `id_token` containing `custom:tenant_user_id`/`custom:platform_id`.

**Step 3 · Backend creates the tenant on the user's behalf**

```
User clicks activate → your frontend POSTs your backend (Bearer <user id_token>)
Your backend: ① validate the id_token (federation-issued, JWKS signature verification) ② run your purchase/quota gate
             ③ hold x-api-key, call POST {CTRL_API}/tenants (no user Bearer)
                body: {name, client_token:<idempotency key>,
                       owner_id:<the user's Cognito sub>, tenant_user_id:<your platform's user id>,
                       platform_id:<your platform id>}
             ④ poll GET {CTRL_API}/tenants/{id} until status:running
             ⑤ return {tenant_id, status} to the frontend
```

Use `<platform_id>:<user id>` for `client_token` to guarantee idempotent repeat activation for the same user (no double creation).

> **Important** For "manage a fleet by user" (Step 5) and "the user sees their own nodes in the chat UI" to work, the attribution fields must land at create time: on the api-key-only path **pass `owner_id` + `tenant_user_id` explicitly in the body** (as above); on the forward-the-id_token path ownership is derived automatically from the token. Give neither and the tenant lands under the system sentinel — the user's `GET /tenants` will not list it and `GET /users/{tenant_user_id}/tenants` will not find it. The reference implementation `console/marketplace-demo/broker/handler.py` shows the overall flow skeleton; treat this section's convention as authoritative.

**Step 4 · Enter the AI assistant**
The user clicks "enter the AI assistant" → jumps to the chat UI under your domain → your backend calls `GET {CTRL_API}/tenants/{id}` to fetch the tenant's gateway token and device-credential KMS ciphertexts and decrypts them locally → the chat UI opens `wss {your platform backend}/gw/ws?token=` with your platform session token (an HS256 JWT) → your platform backend, acting as the WS client, completes the Ed25519 device handshake with the microVM gateway through the edge → chats. See chapter 13, _Data-plane two-tier routing_, for the full chain.

**Step 5 · Manage by user**

- Query all AI assistants of a given user: `GET {CTRL_API}/users/{tenant_user_id}/tenants`
- That user's node summary: `GET {CTRL_API}/users/{tenant_user_id}/summary`
- Batch stop/start (e.g. when the user unsubscribes): `POST {CTRL_API}/users/{tenant_user_id}/action {action:stop}`

## 4. Isolation and Security Guarantees (the promise to your users)

- **Cross-user isolation**: each user's openclaw is an independent microVM (an independent kernel); user A cannot access user B's sessions/data/nodes. The real-time channel has three gates: the token is bound to a single tenant + matching by the server-validated user identity + authorization looks up the server-side ledger (it never trusts the client's self-report).
- **Your users' credentials never leave your platform**: user passwords stay only in your IdP / your own account system; on the ClawPool side the data plane accepts only your platform session token plus device authentication, and never touches your users' passwords.
- **Data-plane credentials are held server-side**: each tenant's gateway token and device private key are envelope-encrypted with AWS KMS (encryption context bound to `tenant_id`/`owner_id`) and are decrypted and held by your platform backend — never sent down to the browser; the frontend only ever holds your platform session token.
- **Credential custody**: the `x-api-key` used for backend-side creation is a server-side credential; never put it in the frontend, and rotate it regularly.

## 5. Multi-region

Your platform and the ClawPool pool need not be in the same region. The control-plane API is simply called cross-region; the first hop of the data plane lands on your own platform backend, so you decide where it is deployed.

## 6. Reference Implementation

`console/marketplace-demo/` (a development test bed, not shipped with the product) provides a minimal runnable integration reference: a secondhand marketplace SPA (`marketplace.html`) + backend-side creation (`broker/handler.py`) + a federation configuration script (`setup-federation.sh`) + a test matrix (`TESTPLAN.md`). You can connect your own platform following its structure.

> Status: the control-plane API (create/query/lifecycle/manage-by-user) is live and available. The external IdP federation + Pre-Token-Generation custom-claim injection this chapter originally described (`ADR-dataplane-external-saas-auth`, Track A) has been SUPERSEDED and is no longer an integration basis (see the notice at the top of this chapter); data-plane identity is device authentication plus your platform session token.
