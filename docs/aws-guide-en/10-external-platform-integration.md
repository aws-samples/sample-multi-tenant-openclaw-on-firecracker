# External Platform Integration Guide (from the customer's perspective)

> **2026-07-08 data-plane redesign notice**: the real-time chat integration flow in this chapter — the federated-token exchange to `{HUB}/hub/token` and the `wss {HUB}/hub/ws` connection — has been **fully superseded** by [Chapter 13 · Data-plane two-tier routing](13-data-plane-redesign.md) (customer OIDC does not support headless/browserless login, so Cognito and the outbound-dial hub were dropped in favor of OpenClaw-native gateway auth). The tenant creation / management (control plane REST API) parts of this chapter remain valid. For the current real-time chat integration, Chapter 13 is authoritative.

This section is for **customers who want to integrate ClawPool into their own platform** (a trading platform, a secondhand marketplace, any SaaS). By the end you will be able to give every one of your platform's free users a dedicated AI assistant (an independent openclaw), entirely through your own account system, with the user never touching any underlying console.

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

> **Note** Tenant ownership takes one of two paths — pick one by how you call, and **do not mix them**. **Path 1 · api-key-only create-on-behalf (recommended, simplest)**: the request carries only `x-api-key` (no user Bearer), and you pass `owner_id` (the user's `sub` in ClawPool's Cognito, a UUID) and `tenant_user_id` (your platform's stable user id) directly in the body; the control plane validates them and persists. **Path 2 · forward the user's `id_token`**: the request carries `x-api-key` plus the user's Bearer `id_token` (federation-issued), and ownership is derived automatically from the token (`sub` → `owner_id`, the `custom:tenant_user_id` claim → `tenant_user_id`); **on this path the body must NOT carry those two fields — if it does, the create returns 403** (the owner may only come from a verified token, preventing a node from being created under someone else's name). `platform_id` may be passed in the body on **either** path (1-128 chars matching `^[a-zA-Z0-9._-]{1,128}$`) to tag the owning platform; the same `platform_id` is also the key the pre-login IdP routing lookup (`GET /tenantmatch`) matches on. See `openapi.yaml` operation `createTenant`, and code `tenant_service.py:139` (the `_attribution_override` two-path gate) and `tenant_service.py:252` (the `platform_id` handling).
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
The user clicks "enter the AI assistant" → jumps to the chat UI under your domain → the chat UI, via your platform back end, obtains the tenant's `gateway_token` (your back end decrypts the KMS ciphertext returned by `revealToken`) → chats over the two-tier edge route to the microVM-native gateway. The current integration is detailed in [Chapter 13 · Data-plane two-tier routing](13-data-plane-redesign.md).

**Step 5 · Manage by user**

- Query all AI assistants of a given user: `GET {CTRL_API}/users/{tenant_user_id}/tenants`
- That user's node summary: `GET {CTRL_API}/users/{tenant_user_id}/summary`
- Batch stop/start (e.g. when the user unsubscribes): `POST {CTRL_API}/users/{tenant_user_id}/action {action:stop}`

## 4. Isolation and Security Guarantees (the promise to your users)

- **Cross-user isolation**: each user's openclaw is an independent microVM (an independent kernel); user A cannot access user B's sessions/data/nodes. The real-time channel has three gates: the token is bound to a single tenant + matching by the server-validated user identity + authorization looks up the server-side ledger (it never trusts the client's self-report).
- **Your users' credentials never leave your platform**: in the federation model, the user password stays only in your IdP; ClawPool receives only the issued JWT and never touches your users' passwords.
- **Trust boundary**: ClawPool's channel machine identity runs on a separate internal Cognito Pool (isolated from your user entry Pool), so your users federating in are never confused with the internal machine identity.
- **Credential custody**: the `x-api-key` used for backend-side creation is a server-side credential; never put it in the frontend, and rotate it regularly.

## 5. Multi-region

Your platform and the ClawPool pool need not be in the same region (federation works across regions). The test bed example puts the e-commerce frontend in a Japan region and the openclaw pool in a Singapore region, validating cross-region integration.

## 6. Reference Implementation

`console/marketplace-demo/` (a development test bed, not shipped with the product) provides a minimal runnable integration reference: a secondhand marketplace SPA (`marketplace.html`) + backend-side creation (`broker/handler.py`) + a federation configuration script (`setup-federation.sh`) + a test matrix (`TESTPLAN.md`). You can connect your own platform following its structure.

> Status: the federated integration described in this guide is ClawPool's Track A approach (`ADR-dataplane-external-saas-auth`). The control plane API (create/query/lifecycle/manage-by-user) is live and available; external IdP federation + Pre-Token-Generation injection of the custom claim is a Track A landing item, subject to the availability scope confirmed by ClawPool operations.
