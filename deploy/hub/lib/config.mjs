// lib/config.mjs — 全部 env 常量唯一定义点(#136 拆分,js-split 门3:单例/配置唯一定义)。
// 只 import node 内建;任何 lib 模块都可 import 本文件,本文件不 import 其它 lib(叶子)。
// 常量语义与原 server.mjs 逐字一致,只搬家不改值。

import { randomBytes } from "node:crypto";

export const PORT = Number(process.env.CLAW_HUB_PORT || 8790);
export const TS_WINDOW_SEC = 300;
export const TOKEN_TTL_SEC = 300;

// ── Media (image) config ──────────────────
// The hub is the ONLY component holding S3 credentials (instance role). VMs are
// zero-credential golden images, so the channel can never touch S3 directly; it
// asks the hub for a presigned URL. This keeps the S3 blast-radius at one place.
export const ASSETS_BUCKET = process.env.OC_ASSETS_BUCKET || process.env.ASSETS_BUCKET || "";
export const MEDIA_PREFIX = "media"; // s3 key = media/{tenant}/{uuid}.{ext}
export const MEDIA_MAX_BYTES = 20 * 1024 * 1024; // 20MB, matches outbound-media OUTBOUND_MEDIA_MAX_BYTES
export const MEDIA_URL_TTL_SEC = 300; // presigned URL lifetime
// MIME whitelist — kept in sync with the channel's ALLOWED_OUTBOUND_MIMES so a
// round-trip (browser upload → agent → reply image) only ever moves allowed types.
export const MEDIA_ALLOWED_MIMES = new Set([
  "image/png", "image/jpeg", "image/gif", "image/webp",
  "application/pdf",
  "text/plain", "text/csv",
]);
export const MEDIA_EXT_BY_MIME = {
  "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
  "application/pdf": "pdf", "text/plain": "txt", "text/csv": "csv",
};

// Per-tenant channel app secret. Two sources, in order:
//   1. env CLAW_HUB_APP_SECRETS (JSON {tenant:secretHex}) — for local tests.
//   2. DynamoDB tenants table `channel_secret` — production. launch-vm generates
//      the per-VM secret, host-agent mirrors it into the tenant record, the hub
//      reads it here. Nothing hard-coded; the secret never leaves AWS+the VM.
export let APP_SECRETS = {};
try {
  APP_SECRETS = JSON.parse(process.env.CLAW_HUB_APP_SECRETS || "{}");
} catch {
  APP_SECRETS = {};
}
export const TENANTS_TABLE = process.env.TENANTS_TABLE || "";
export const AWS_REGION = process.env.AWS_REGION || process.env.OC_REGION || "ap-southeast-1";
// SECURITY (go-live A2): shared/legacy tenant access (owner_id ∈ {"api-key",""})
// is a demo convenience that lets ANY logged-in user reach a control-plane-created
// or pre-authz node. For go-live this must be OFF so a node with no explicit owner
// is reachable by NOBODY (except admins via the control plane). Default OFF
// (secure); a demo that genuinely needs the old shared behavior sets
// CLAW_HUB_SHARED_TENANT_ACCESS=true EXPLICITLY.
export const SHARED_TENANT_ACCESS =
  String(process.env.CLAW_HUB_SHARED_TENANT_ACCESS || "false").toLowerCase() === "true";
export const SECRET_TTL_MS = 60000;

// HMAC key the hub uses to sign/verify its OWN short-lived session tokens.
// Go-live B1 (EKS multi-Pod): a token signed by Pod A must verify on Pod B, so
// every replica MUST share the SAME key (from Secrets Manager via env). A random
// per-process key is only acceptable for a single-process (metal) hub. When
// CLAW_HUB_CLUSTERED=true we FAIL CLOSED if no shared key is provided, rather
// than silently minting tokens nobody else can verify (which would drop every
// cross-Pod reconnect). Single-process mode keeps the random fallback.
export const HUB_CLUSTERED =
  String(process.env.CLAW_HUB_CLUSTERED || "false").toLowerCase() === "true";
export const HUB_TOKEN_KEY = (() => {
  const k = process.env.CLAW_HUB_TOKEN_KEY || "";
  if (k) return k;
  if (HUB_CLUSTERED) {
    console.error(
      "[claw-hub] FATAL: CLAW_HUB_CLUSTERED=true but CLAW_HUB_TOKEN_KEY is unset — " +
        "multi-Pod replicas need a shared HMAC key (Secrets Manager). Refusing to start with a per-Pod random key.",
    );
    process.exit(1);
  }
  return randomBytes(32).toString("hex"); // single-process only
})();

// Cognito verification config (same trust chain as the control-plane Lambda).
export const COGNITO_REGION = process.env.COGNITO_REGION || "ap-southeast-1";
export const COGNITO_USER_POOL_ID = process.env.COGNITO_USER_POOL_ID || "";
export const COGNITO_CLIENT_ID = process.env.COGNITO_CLIENT_ID || "";
// CHANNEL plane (WI-002): the app client used by the per-tenant machine-user
// (USER_PASSWORD_AUTH flow). access tokens minted for THIS client are accepted
// on /channel-token. Empty = channel Cognito path disabled (HMAC-only, legacy).
export const COGNITO_CHANNEL_CLIENT_ID = process.env.COGNITO_CHANNEL_CLIENT_ID || "";
