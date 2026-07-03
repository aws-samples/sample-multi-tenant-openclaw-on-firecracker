// claw-hub — self-hosted WS gateway hub for the claw-channel. Topology:
//
//   frontend (chat mini-app)  --wss-->  [ claw-hub ]  <--wss(outbound)--  VM claw-channel
//                                            |
//                              token issuance + per-tenant multiplexing
//
// Contract:
//   - token = HMAC-SHA256("{appId}:{timestamp}", Buffer.from(appSecret,"hex"))
//   - wire frame = {messageId, senderId, senderType:"USER"|"BOT", receiverId,
//                   receiverType, parts:[{kind:"TEXT",text,isDone}], operationType,
//                   threadId, chatType}
//   - frontend identity = Cognito sub (server-verified, never client-asserted)
//
// Two auth planes (orthogonal — frontend user-token vs channel app-token):
//   (a) FRONTEND token: issued by POST /token after verifying a Cognito id_token.
//       Bound to the Cognito sub. The browser never sees any channel secret.
//   (b) CHANNEL token: issued by POST /channel-token after verifying the VM's
//       appId/appSecret HMAC. The VM's channel uses it to register outbound.
//
// The hub routes a frontend message to the channel whose tenant the frontend is
// authorized for, and routes the channel's reply back to that frontend only.
// Cross-tenant is impossible: a frontend token carries one tenant; a channel
// registers one tenant; the hub matches them and never bridges across.

import { createServer } from "node:http";
import { createHmac, timingSafeEqual, randomBytes } from "node:crypto";
import { WebSocketServer } from "ws";
// Go-live B1: cross-Pod routing on EKS (degrade-safe — no Redis = local-only,
// i.e. today's single-process behavior unchanged). See cluster-routing.mjs.
import {
  initClusterRouting,
  clusterEnabled,
  registerChannel as crRegisterChannel,
  registerFrontend as crRegisterFrontend,
  unregisterChannel as crUnregisterChannel,
  unregisterFrontend as crUnregisterFrontend,
  forwardToChannel as crForwardToChannel,
  forwardToFrontend as crForwardToFrontend,
} from "./cluster-routing.mjs";

const PORT = Number(process.env.CLAW_HUB_PORT || 8790);
const TS_WINDOW_SEC = 300;
const TOKEN_TTL_SEC = 300;

// #14 channel 注册 HMAC 防重放:时间窗(±300s)只挡"迟到/超前"的请求,挡不住窗口内
// 重放——攻击者截获一条合法 {appId,ts,signature} 可在 300s 内重复提交劫持注册。加一个
// 一次性 nonce:每个签名(按 tenant:appId:ts:sig 唯一)只接受一次,窗口内再现即拒。
// 存活 TS_WINDOW_SEC 后自动过期回收(过了时间窗的旧签名本就会被时间窗挡,无需再记)。
// 说明:本地内存 store — 单 Pod 完整防重放;EKS 多 Pod 下同 Pod 防住,跨 Pod 完整防重放
// 需共享 store(Redis/ElastiCache,与连接路由同源),留作后续(注释标注,不假装已覆盖)。
const _usedChannelSigs = new Map(); // key = tenant:appId:ts:sig → expiresAtMs
function _channelSigSeenBefore(key, nowMs) {
  // 惰性清理:每次校验顺带回收已过期条目,避免 store 无界增长(无需定时器)。
  if (_usedChannelSigs.size > 0) {
    for (const [k, exp] of _usedChannelSigs) {
      if (exp <= nowMs) _usedChannelSigs.delete(k);
    }
  }
  if (_usedChannelSigs.has(key)) return true; // 重放
  _usedChannelSigs.set(key, nowMs + TS_WINDOW_SEC * 1000);
  return false;
}

// ── Media (image) config ──────────────────
// The hub is the ONLY component holding S3 credentials (instance role). VMs are
// zero-credential golden images, so the channel can never touch S3 directly; it
// asks the hub for a presigned URL. This keeps the S3 blast-radius at one place.
const ASSETS_BUCKET = process.env.OC_ASSETS_BUCKET || process.env.ASSETS_BUCKET || "";
const MEDIA_PREFIX = "media"; // s3 key = media/{tenant}/{uuid}.{ext}
const MEDIA_MAX_BYTES = 20 * 1024 * 1024; // 20MB, matches outbound-media OUTBOUND_MEDIA_MAX_BYTES
const MEDIA_URL_TTL_SEC = 300; // presigned URL lifetime
// MIME whitelist — kept in sync with the channel's ALLOWED_OUTBOUND_MIMES so a
// round-trip (browser upload → agent → reply image) only ever moves allowed types.
const MEDIA_ALLOWED_MIMES = new Set([
  "image/png", "image/jpeg", "image/gif", "image/webp",
  "application/pdf",
  "text/plain", "text/csv",
]);
const MEDIA_EXT_BY_MIME = {
  "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
  "application/pdf": "pdf", "text/plain": "txt", "text/csv": "csv",
};
let _s3 = null; // lazy S3 client + presigner
async function getS3() {
  if (_s3) return _s3;
  const { S3Client, PutObjectCommand, GetObjectCommand, HeadObjectCommand } = await import("@aws-sdk/client-s3");
  const { getSignedUrl } = await import("@aws-sdk/s3-request-presigner");
  _s3 = {
    client: new S3Client({ region: AWS_REGION }),
    PutObjectCommand, GetObjectCommand, HeadObjectCommand, getSignedUrl,
  };
  return _s3;
}

// Per-tenant channel app secret. Two sources, in order:
//   1. env CLAW_HUB_APP_SECRETS (JSON {tenant:secretHex}) — for local tests.
//   2. DynamoDB tenants table `channel_secret` — production. launch-vm generates
//      the per-VM secret, host-agent mirrors it into the tenant record, the hub
//      reads it here. Nothing hard-coded; the secret never leaves AWS+the VM.
let APP_SECRETS = {};
try {
  APP_SECRETS = JSON.parse(process.env.CLAW_HUB_APP_SECRETS || "{}");
} catch {
  APP_SECRETS = {};
}
const TENANTS_TABLE = process.env.TENANTS_TABLE || "";
const AWS_REGION = process.env.AWS_REGION || process.env.OC_REGION || "ap-southeast-1";
// SECURITY (go-live A2): shared/legacy tenant access (owner_id ∈ {"api-key",""})
// is a demo convenience that lets ANY logged-in user reach a control-plane-created
// or pre-authz node. For go-live this must be OFF so a node with no explicit owner
// is reachable by NOBODY (except admins via the control plane). Default OFF
// (secure); a demo that genuinely needs the old shared behavior sets
// CLAW_HUB_SHARED_TENANT_ACCESS=true EXPLICITLY.
const SHARED_TENANT_ACCESS =
  String(process.env.CLAW_HUB_SHARED_TENANT_ACCESS || "false").toLowerCase() === "true";
const _secretCache = new Map(); // tenant -> {secret, at}
const SECRET_TTL_MS = 60000;
let _ddb = null;
// Explicit tenant authorization layer (P0). Returns the tenant's access record:
//   { owner, authorizedUsers }
// where owner = owner_id (creator's Cognito sub, or "api-key" sentinel for
// shared/control-plane nodes, or "" for legacy records with no owner_id) and
// authorizedUsers = { <sub>: { role, expireAt? }, ... } — the explicit, auditable
// grant list a tenant owner can extend to other Cognito subs (default: only the
// owner has access). This is the single source of truth all hub auth points
// (/token, /files, WS) consult so authorization is explicit + least-privilege,
// not the old implicit owner_id===sub equality. Mirrors the control plane's
// owner_id model (deploy/lambda/api/handler.py) and adds delegation.
// Used by /token to stop a logged-in user minting a frontend token bound to a
// tenant they are NOT authorized for (the hub previously trusted body.tenant_id
// outright — the cross-tenant token-mint HIGH).
const _ownerCache = new Map();
async function getTenantAccess(tenantId) {
  if (!TENANTS_TABLE) return null;
  const cached = _ownerCache.get(tenantId);
  if (cached && Date.now() - cached.at < SECRET_TTL_MS) return cached.access;
  try {
    if (!_ddb) {
      const { DynamoDBClient } = await import("@aws-sdk/client-dynamodb");
      const { DynamoDBDocumentClient, GetCommand } = await import("@aws-sdk/lib-dynamodb");
      _ddb = { doc: DynamoDBDocumentClient.from(new DynamoDBClient({ region: AWS_REGION })), GetCommand };
    }
    const out = await _ddb.doc.send(
      new _ddb.GetCommand({
        TableName: TENANTS_TABLE,
        Key: { id: tenantId },
        ProjectionExpression: "owner_id, authorized_users",
      }),
    );
    // undefined owner_id = no record / legacy → "" (shared/legacy policy).
    const access = {
      owner: out?.Item?.owner_id ?? "",
      authorizedUsers:
        out?.Item?.authorized_users && typeof out.Item.authorized_users === "object"
          ? out.Item.authorized_users
          : {},
    };
    _ownerCache.set(tenantId, { access, at: Date.now() });
    return access;
  } catch {
    return null; // DDB error → fail closed at the call site
  }
}

// Decide whether a Cognito `sub` may access `tenantId`, and with what role.
// Returns { allowed: boolean, role: string, reason: string }. Policy (least
// privilege, explicit grants, demo-preserving):
//   • owner_id === sub                 → allowed, role "owner"
//   • sub ∈ authorized_users (unexpired)→ allowed, role from grant
//   • owner_id ∈ {"api-key",""}        → allowed, role "shared" (shared/legacy
//                                         nodes: control-plane-created or pre-
//                                         authz records — preserves the demo)
//   • DDB error (access===null)        → denied (fail closed)
//   • otherwise (another user's private)→ denied
async function authorizeSubForTenant(sub, tenantId) {
  const access = await getTenantAccess(tenantId);
  if (access === null) return { allowed: false, role: null, reason: "ddb-error" };
  if (access.owner && access.owner === sub) return { allowed: true, role: "owner", reason: "owner" };
  const grant = access.authorizedUsers?.[sub];
  if (grant) {
    const exp = Number(grant.expireAt || 0);
    if (!exp || Math.floor(Date.now() / 1000) <= exp) {
      return { allowed: true, role: String(grant.role || "member"), reason: "granted" };
    }
    return { allowed: false, role: null, reason: "grant-expired" };
  }
  if (access.owner === "" || access.owner === "api-key") {
    // shared/legacy nodes: only reachable when the demo flag is explicitly on.
    // Go-live default (flag off) → a node with no explicit owner is denied to
    // every frontend user; manage it via the control plane (admin) instead.
    if (SHARED_TENANT_ACCESS) {
      return { allowed: true, role: "shared", reason: "shared-or-legacy" };
    }
    return { allowed: false, role: null, reason: "shared-access-disabled" };
  }
  return { allowed: false, role: null, reason: "not-authorized" };
}

async function getTenantSecret(tenantId) {
  if (APP_SECRETS[tenantId]) return APP_SECRETS[tenantId];
  if (!TENANTS_TABLE) return null;
  const cached = _secretCache.get(tenantId);
  if (cached && Date.now() - cached.at < SECRET_TTL_MS) return cached.secret;
  try {
    if (!_ddb) {
      const { DynamoDBClient } = await import("@aws-sdk/client-dynamodb");
      const { DynamoDBDocumentClient, GetCommand } = await import("@aws-sdk/lib-dynamodb");
      _ddb = { doc: DynamoDBDocumentClient.from(new DynamoDBClient({ region: AWS_REGION })), GetCommand };
    }
    const out = await _ddb.doc.send(
      new _ddb.GetCommand({ TableName: TENANTS_TABLE, Key: { id: tenantId }, ProjectionExpression: "channel_secret" }),
    );
    const secret = out?.Item?.channel_secret || null;
    if (secret) _secretCache.set(tenantId, { secret, at: Date.now() });
    return secret;
  } catch (e) {
    // 不静默吞:读 secret 失败(SDK 缺失/权限/网络)直接拒掉 channel 注册,
    // 但必须把原因 log 出来——这个 catch 曾静默吞掉「镜像缺 @aws-sdk/client-dynamodb」
    // 整整骗过多轮排查(channel 注册全 401)。fail 要响,不要哑。
    console.error(`[getTenantSecret] DDB read failed for ${tenantId}: ${e.name}: ${e.message}`);
    return null;
  }
}

// HMAC key the hub uses to sign/verify its OWN short-lived session tokens.
// Go-live B1 (EKS multi-Pod): a token signed by Pod A must verify on Pod B, so
// every replica MUST share the SAME key (from Secrets Manager via env). A random
// per-process key is only acceptable for a single-process (metal) hub. When
// CLAW_HUB_CLUSTERED=true we FAIL CLOSED if no shared key is provided, rather
// than silently minting tokens nobody else can verify (which would drop every
// cross-Pod reconnect). Single-process mode keeps the random fallback.
const HUB_CLUSTERED =
  String(process.env.CLAW_HUB_CLUSTERED || "false").toLowerCase() === "true";
const HUB_TOKEN_KEY = (() => {
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
const COGNITO_REGION = process.env.COGNITO_REGION || "ap-southeast-1";
const COGNITO_USER_POOL_ID = process.env.COGNITO_USER_POOL_ID || "";
const COGNITO_CLIENT_ID = process.env.COGNITO_CLIENT_ID || "";
// CHANNEL plane (WI-002): the app client used by the per-tenant machine-user
// (USER_PASSWORD_AUTH flow). access tokens minted for THIS client are accepted
// on /channel-token. Empty = channel Cognito path disabled (HMAC-only, legacy).
const COGNITO_CHANNEL_CLIENT_ID = process.env.COGNITO_CHANNEL_CLIENT_ID || "";

// ---- lazy JWKS verify (PyJWT-equivalent in JS via jose, optional) ----
// One JWKS set per user pool covers BOTH id and access tokens (same signing keys).
let _jwks = null;
function _getJwks() {
  if (!_jwks) {
    const url = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}/.well-known/jwks.json`;
    // createRemoteJWKSet is resolved lazily inside the async verifiers via import("jose").
    _jwks = { url, set: null };
  }
  return _jwks;
}

// ---- #98 档B: 多 issuer id_token 验证(按需,默认关)----
// 仅当客户坚持不经 Cognito 联邦、直接用自己 IdP 的 JWT 打 hub 时开启(ADR §3 档B)。
// 配置 EXTERNAL_ISSUERS = JSON 数组 [{issuer,jwksUrl,audience,platform_id}]:按 token 的
// `iss` 选对应 issuer 配置验签(每 issuer 独立 JWKS 缓存),命中才验、未知 issuer fail-closed 拒
// (TB-004 allowlist 语义)。默认为空 → 完全不改变现有单 Cognito 路径(向后兼容)。
// 代价:hub 维护多 JWKS 缓存 + 多信任链,运维需保证 allowlist 只列可信 IdP。
function parseExternalIssuers(raw) {
  if (!raw || !String(raw).trim()) return [];
  let arr;
  try {
    arr = JSON.parse(raw);
  } catch {
    // 配置坏了 fail-closed:不 silently 退化成"无外部 issuer",而是明确拒(返回 sentinel)。
    return { error: "EXTERNAL_ISSUERS is not valid JSON" };
  }
  if (!Array.isArray(arr)) return { error: "EXTERNAL_ISSUERS must be a JSON array" };
  const out = [];
  for (const e of arr) {
    // 每条必须齐全:issuer + jwksUrl(https) + platform_id;audience 可选。
    // ⚠️ 运维注意:不配 audience = 接受该 IdP 签发的**任意 aud** 的 token(无法防同一 IdP
    // 签给其它 app 的 token 被拿来打 hub)。强烈建议配 audience 把信任收窄到本平台的 app;
    // 兜底仍在 /token 的 authorizeSubForTenant 显式租户授权门(aud 不参与租户归属)。
    if (!e || typeof e !== "object") continue;
    const issuer = String(e.issuer || "").trim();
    const jwksUrl = String(e.jwksUrl || "").trim();
    const platformId = String(e.platform_id || "").trim();
    const audience = e.audience ? String(e.audience).trim() : "";
    if (!issuer || !/^https:\/\//.test(jwksUrl) || !platformId) continue; // 不合格条目丢弃(不 fail 整个 allowlist)
    out.push({ issuer, jwksUrl, platformId, audience });
  }
  return out;
}
const _externalIssuersParsed = parseExternalIssuers(process.env.EXTERNAL_ISSUERS || "");
// per-issuer JWKS 缓存: issuer → { set }
const _externalJwks = new Map();

// 不验签、只解 JWT payload 拿 `iss`(用于选 issuer 配置)。base64url 解码 payload 段。
// 这只用来路由到正确的验证器;真正的信任由随后的 jwtVerify(签名+issuer+aud)建立。
function _peekIssuer(token) {
  try {
    const parts = String(token).split(".");
    if (parts.length !== 3) return null;
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString());
    const iss = payload && typeof payload.iss === "string" ? payload.iss : null;
    return iss || null;
  } catch {
    return null;
  }
}

// 按 token 的 iss 在 allowlist 里找配置并验签。未配/未知 issuer/坏配置 → null(fail-closed)。
// 成功返回 { ...payload, platform_id }。
async function verifyExternalIdToken(token) {
  if (!token) return null;
  if (!Array.isArray(_externalIssuersParsed)) return null; // 坏配置 → 一律拒(fail-closed)
  if (_externalIssuersParsed.length === 0) return null; // 档B 未开
  const iss = _peekIssuer(token);
  if (!iss) return null;
  const cfg = _externalIssuersParsed.find((e) => e.issuer === iss);
  if (!cfg) return null; // 未知 issuer:不在 allowlist → fail-closed 拒
  try {
    const { jwtVerify, createRemoteJWKSet } = await import("jose");
    let entry = _externalJwks.get(cfg.issuer);
    if (!entry) {
      entry = { set: createRemoteJWKSet(new URL(cfg.jwksUrl)) };
      _externalJwks.set(cfg.issuer, entry);
    }
    const opts = { issuer: cfg.issuer };
    if (cfg.audience) opts.audience = cfg.audience;
    const { payload } = await jwtVerify(token, entry.set, opts);
    // server 盖戳 platform_id(来自可信 allowlist 配置,不信 token 自报),供下游区分来源。
    return { ...payload, platform_id: cfg.platformId };
  } catch {
    return null; // 验签/aud/issuer 任一失败 → 拒
  }
}

// 统一 id_token 验证入口:先走现有 Cognito 单 issuer(默认路径,向后兼容),失败再试
// 档B 多 issuer allowlist(仅当 EXTERNAL_ISSUERS 配了才有效)。两条都 fail-closed。
async function verifyIdToken(token) {
  const cognito = await verifyCognitoIdToken(token);
  if (cognito && cognito.sub) return cognito;
  return await verifyExternalIdToken(token);
}

async function verifyCognitoIdToken(token) {
  if (!token || !COGNITO_USER_POOL_ID) return null;
  try {
    const { jwtVerify, createRemoteJWKSet } = await import("jose");
    const j = _getJwks();
    if (!j.set) j.set = createRemoteJWKSet(new URL(j.url));
    const issuer = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}`;
    const { payload } = await jwtVerify(token, j.set, { issuer });
    // id token: token_use must be "id"; audience claim is `aud`.
    if (payload.token_use && payload.token_use !== "id") return null;
    if (COGNITO_CLIENT_ID) {
      const aud = payload.aud || payload.client_id;
      if (aud && aud !== COGNITO_CLIENT_ID) return null;
    }
    return payload; // verified claims
  } catch {
    return null; // any failure → untrusted
  }
}

// ---- channel plane: verify a Cognito ACCESS token (WI-002) ----
// The VM's per-tenant machine-user signs in (USER_PASSWORD_AUTH) and presents
// its access token. We verify the SAME way the AWS-official `aws-jwt-verify`
// library does for access tokens: issuer + signature (JWKS) + token_use=access
// + client_id match. The tenant identity is the `username` claim, minted by
// Cognito and unforgeable — the VM cannot claim to be another tenant.
// Returns { tenant } on success, or null. (We only surface what the caller needs.)
async function verifyCognitoAccessToken(token) {
  if (!token || !COGNITO_USER_POOL_ID || !COGNITO_CHANNEL_CLIENT_ID) return null;
  try {
    const { jwtVerify, createRemoteJWKSet } = await import("jose");
    const j = _getJwks();
    if (!j.set) j.set = createRemoteJWKSet(new URL(j.url));
    const issuer = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}`;
    const { payload } = await jwtVerify(token, j.set, { issuer });
    // access token invariants (mirror aws-jwt-verify defaults for tokenUse:"access"):
    if (payload.token_use !== "access") return null;
    // access tokens carry the app client in `client_id` (NOT `aud`).
    if (payload.client_id !== COGNITO_CHANNEL_CLIENT_ID) return null;
    // tenant identity = the machine-user's username (one user per tenant).
    const tenant = String(payload.username || payload["cognito:username"] || "").trim();
    if (!tenant) return null;
    return { tenant };
  } catch {
    return null; // any failure → untrusted
  }
}

// ---- hub session token: HMAC over a compact claims string, with a TTL ----
function issueHubToken(claims) {
  const body = Buffer.from(JSON.stringify(claims)).toString("base64url");
  const sig = createHmac("sha256", HUB_TOKEN_KEY).update(body).digest("base64url");
  return `${body}.${sig}`;
}
function verifyHubToken(tok) {
  if (!tok || typeof tok !== "string" || !tok.includes(".")) return null;
  const [body, sig] = tok.split(".");
  const expected = createHmac("sha256", HUB_TOKEN_KEY).update(body).digest("base64url");
  if (sig.length !== expected.length) return null;
  try {
    if (!timingSafeEqual(Buffer.from(sig), Buffer.from(expected))) return null;
  } catch {
    return null;
  }
  let claims;
  try {
    claims = JSON.parse(Buffer.from(body, "base64url").toString());
  } catch {
    return null;
  }
  if (!claims.exp || Math.floor(Date.now() / 1000) > claims.exp) return null;
  return claims;
}

// ---- media auth: accept a hub token via Bearer header or ?token= query ----
// Returns the verified claims (with .tenant) or null. Both frontend and channel
// tokens are accepted — each is bound to exactly one tenant, which scopes S3 keys.
function mediaAuth(req) {
  const auth = req.headers["authorization"] || "";
  let tok = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!tok) {
    try {
      tok = new URL(req.url, "http://x").searchParams.get("token") || "";
    } catch {
      tok = "";
    }
  }
  return verifyHubToken(tok);
}

// ---- channel registration HMAC ----
async function verifyChannelSignature(tenantId, appId, ts, signature) {
  const secret = await getTenantSecret(tenantId);
  if (!secret || !appId || !ts || !signature) return false;
  const tsNum = Number(ts);
  if (!Number.isFinite(tsNum)) return false;
  if (Math.abs(Math.floor(Date.now() / 1000) - tsNum) > TS_WINDOW_SEC) return false;
  const expected = createHmac("sha256", Buffer.from(secret, "hex"))
    .update(`${appId}:${ts}`)
    .digest("hex");
  if (signature.length !== expected.length) return false;
  let sigOk = false;
  try {
    sigOk = timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
  } catch {
    return false;
  }
  if (!sigOk) return false;
  // #14 只在签名已验真后才记 nonce:① 防重放(窗口内同签名第二次即拒)② 避免用错误
  // 签名+随机 ts 撑爆 store(错签在上面就被拒,不占 nonce)。key 含 tenant 防跨租户串。
  // 已知毛刺(legacy HMAC 路径,将退役):签名输入 `{appId}:{ts}` 的 ts 是秒粒度,合法
  // channel 若在同一 wall-clock 秒内二次 fetchHubToken(如断连即重连撞同秒)会得到字节
  // 相同的签名、被 nonce 当重放拒一次;下一秒重试(ts+1)签名不同即自愈。非安全问题,
  // 主路径 Cognito access token 不受影响。彻底消除需客户端签名带 per-request 随机 nonce。
  const nowMs = Date.now();
  const nonceKey = `${tenantId}:${appId}:${ts}:${signature}`;
  if (_channelSigSeenBefore(nonceKey, nowMs)) return false; // 重放请求被拒
  return true;
}

// ---- routing tables ----
// channels: tenantId -> ws (the VM's outbound channel connection)
const channels = new Map();
// frontends: sub -> Set<ws> (a user may have multiple tabs); each ws carries .tenantId
const frontends = new Map();

function safeSend(ws, obj) {
  try {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  } catch {
    /* never throw on send */
  }
}

// ── B1 cross-Pod delivery helpers ──────────────────────────────────────────
// deliverToChannel: send `frame` to the tenant's channel. Local hit → send here;
// local miss + clustered → forward to the Pod that holds it (returns true if
// delivered locally or forwarded, false only if truly nowhere/agent offline).
// Returns a Promise<boolean>.
async function deliverToChannel(tenant, frame) {
  const ch = channels.get(tenant);
  if (ch) {
    safeSend(ch, frame);
    return true;
  }
  if (clusterEnabled()) {
    const r = await crForwardToChannel(tenant, frame);
    return r === "remote"; // remote Pod will deliver; local miss already handled
  }
  return false; // single-process + no local channel = agent offline
}
// deliverToFrontend: send `frame` to all of sub's tabs ON THIS Pod for `tenant`;
// local miss + clustered → forward to the owner Pod. Returns Promise<boolean>.
async function deliverToFrontend(sub, tenant, frame) {
  const set = frontends.get(sub);
  let deliveredLocal = false;
  if (set) {
    for (const fws of set) {
      if (fws._tenant === tenant) {
        safeSend(fws, frame);
        deliveredLocal = true;
      }
    }
  }
  if (deliveredLocal) return true;
  if (clusterEnabled()) {
    const r = await crForwardToFrontend(sub, tenant, frame);
    return r === "remote";
  }
  return false;
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let d = "";
    let n = 0;
    req.on("data", (c) => {
      n += c.length;
      if (n > 1_000_000) {
        reject(new Error("body too large"));
        req.destroy();
        return;
      }
      d += c;
    });
    req.on("end", () => resolve(d));
    req.on("error", reject);
  });
}

// ---- HTTP: token issuance ----
const httpServer = createServer(async (req, res) => {
  // CloudFront 的 /hub/* behavior 直转到 EKS ALB 时带 `/hub` 前缀(不像 metal nginx
  // 那样 strip)。这里统一剥掉开头的 /hub,让根路径路由(/healthz、/token、/ws)
  // 在 metal 直连和 CloudFront→EKS 两条链路下都匹配。
  let _rawUrl = req.url || "";
  if (_rawUrl === "/hub" || _rawUrl.startsWith("/hub/")) {
    _rawUrl = _rawUrl.slice(4) || "/";
    req.url = _rawUrl;
  }
  const url = _rawUrl.split("?")[0];
  const cors = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,x-api-key",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
  };
  if (req.method === "OPTIONS") {
    res.writeHead(204, cors);
    res.end();
    return;
  }
  if (url === "/healthz") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("ok");
    return;
  }

  // FRONTEND token: verify Cognito JWT, bind to sub + the requested tenant.
  if (url === "/token" && req.method === "POST") {
    try {
      const auth = req.headers["authorization"] || "";
      if (!auth.startsWith("Bearer ")) {
        res.writeHead(401, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "cognito id_token required" }));
        return;
      }
      // #98 档B: 统一入口——先 Cognito 单 issuer(默认),失败再试多 issuer allowlist
      // (仅当 EXTERNAL_ISSUERS 配了才有效;未知 issuer fail-closed)。默认行为不变。
      const claims = await verifyIdToken(auth.slice(7).trim());
      if (!claims || !claims.sub) {
        res.writeHead(401, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "invalid token" }));
        return;
      }
      const body = JSON.parse((await readJsonBody(req)) || "{}");
      const tenantId = String(body.tenant_id || "").trim();
      if (!tenantId) {
        res.writeHead(400, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "tenant_id required" }));
        return;
      }
      // SECURITY (explicit tenant authorization, P0): a valid Cognito login
      // alone must NOT let a user bind a frontend token to a tenant they are not
      // authorized for. Consult the explicit authz layer (owner / granted /
      // shared). DDB error → fail closed (403). The granted role is embedded in
      // the issued token so downstream points (/files, WS) enforce least privilege.
      const authz = await authorizeSubForTenant(claims.sub, tenantId);
      if (!authz.allowed) {
        res.writeHead(403, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "forbidden: not authorized for this tenant" }));
        return;
      }
      const token = issueHubToken({
        role: "frontend",
        sub: claims.sub,
        tenant: tenantId,
        access: authz.role, // owner | <granted role> | shared
        exp: Math.floor(Date.now() / 1000) + TOKEN_TTL_SEC,
      });
      res.writeHead(200, { ...cors, "Content-Type": "application/json" });
      res.end(JSON.stringify({ token, expires_in: TOKEN_TTL_SEC }));
    } catch (e) {
      res.writeHead(500, { ...cors, "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "internal" }));
    }
    return;
  }

  // CHANNEL token: the VM proves its tenant identity, hub mints a short hub token.
  // Two accepted proofs, gracefully coexisting for rollout (WI-002):
  //   (1) Cognito access token (NEW, end-to-end Cognito): Authorization: Bearer <jwt>.
  //       The per-tenant machine-user signed in (USER_PASSWORD_AUTH); tenant = the
  //       unforgeable `username` claim. Preferred — same trust root as the frontend.
  //   (2) HMAC signature (LEGACY): {tenant_id, appId, timestamp, signature} in body.
  //       Kept so VMs baked before the Cognito image can still connect during the
  //       rolling rebuild. Remove once all VMs are on the Cognito image.
  if (url === "/channel-token" && req.method === "POST") {
    try {
      // (1) Cognito access token path — try first if a Bearer token is present.
      const authz = req.headers["authorization"] || "";
      if (authz.startsWith("Bearer ")) {
        const verified = await verifyCognitoAccessToken(authz.slice(7).trim());
        if (!verified) {
          res.writeHead(401, { ...cors, "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "invalid channel access token" }));
          return;
        }
        const token = issueHubToken({
          role: "channel",
          tenant: verified.tenant,
          exp: Math.floor(Date.now() / 1000) + TOKEN_TTL_SEC,
        });
        res.writeHead(200, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ token, expires_in: TOKEN_TTL_SEC }));
        return;
      }
      // (2) Legacy HMAC path.
      const body = JSON.parse((await readJsonBody(req)) || "{}");
      const { tenant_id: tenantId, appId, timestamp, signature } = body;
      if (!(await verifyChannelSignature(tenantId, appId, timestamp, signature))) {
        res.writeHead(401, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "bad channel signature" }));
        return;
      }
      const token = issueHubToken({
        role: "channel",
        tenant: tenantId,
        exp: Math.floor(Date.now() / 1000) + TOKEN_TTL_SEC,
      });
      res.writeHead(200, { ...cors, "Content-Type": "application/json" });
      res.end(JSON.stringify({ token, expires_in: TOKEN_TTL_SEC }));
    } catch {
      res.writeHead(500, { ...cors, "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "internal" }));
    }
    return;
  }

  // ── MEDIA: presigned upload URL ──
  // Auth: any valid hub token (frontend OR channel). The S3 key is namespaced
  // by the token's tenant so a tenant can only ever write under its own prefix.
  if (url === "/files/upload-url" && req.method === "POST") {
    try {
      if (!ASSETS_BUCKET) {
        res.writeHead(503, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "media not configured" }));
        return;
      }
      const claims = mediaAuth(req);
      if (!claims) {
        res.writeHead(401, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "unauthorized" }));
        return;
      }
      const body = JSON.parse((await readJsonBody(req)) || "{}");
      const mime = String(body.mimeType || body.contentType || "").toLowerCase();
      if (!MEDIA_ALLOWED_MIMES.has(mime)) {
        res.writeHead(415, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: `unsupported mimeType: ${mime || "(none)"}` }));
        return;
      }
      const declaredSize = Number(body.size || 0);
      if (declaredSize && declaredSize > MEDIA_MAX_BYTES) {
        res.writeHead(413, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: `file exceeds ${MEDIA_MAX_BYTES / (1024 * 1024)}MB cap` }));
        return;
      }
      // fileKey = media/{tenant}/{uuid}.{ext} — tenant-namespaced, opaque uuid.
      const ext = MEDIA_EXT_BY_MIME[mime] || "bin";
      const fileKey = `${MEDIA_PREFIX}/${claims.tenant}/${randomBytes(16).toString("hex")}.${ext}`;
      const s3 = await getS3();
      // ContentLengthRange would need a POST policy; for a PUT presign we bound
      // via ContentType pinning + the channel/front-end size check. The S3 object
      // is private (no public ACL); only the hub can presign reads.
      const putUrl = await s3.getSignedUrl(
        s3.client,
        new s3.PutObjectCommand({ Bucket: ASSETS_BUCKET, Key: fileKey, ContentType: mime }),
        { expiresIn: MEDIA_URL_TTL_SEC },
      );
      res.writeHead(200, { ...cors, "Content-Type": "application/json" });
      res.end(JSON.stringify({ code: "000000", success: true, data: { fileKey, url: putUrl } }));
    } catch (e) {
      res.writeHead(500, { ...cors, "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "internal" }));
    }
    return;
  }

  // ── MEDIA: presigned download URL ──
  // IDOR guard: the token's tenant MUST match the fileKey's tenant segment, so
  // tenant A can never resolve a URL for tenant B's object. Scan-ready is stubbed
  // to "immediately ready" (HeadObject 200) — a real ClamAV/GuardDuty hook lands here.
  if (url === "/files/download-url" && req.method === "GET") {
    try {
      if (!ASSETS_BUCKET) {
        res.writeHead(503, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "media not configured" }));
        return;
      }
      const claims = mediaAuth(req);
      if (!claims) {
        res.writeHead(401, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "unauthorized" }));
        return;
      }
      const fileKey = new URL(req.url, "http://x").searchParams.get("fileKey") || "";
      const seg = fileKey.split("/"); // [media, tenant, uuid.ext]
      if (seg.length !== 3 || seg[0] !== MEDIA_PREFIX || seg[1] !== claims.tenant) {
        // wrong shape OR cross-tenant access attempt → 404 (don't leak existence)
        res.writeHead(404, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "not found" }));
        return;
      }
      const s3 = await getS3();
      // scan-ready check: object must exist. (Stub: existence == ready.)
      try {
        await s3.client.send(new s3.HeadObjectCommand({ Bucket: ASSETS_BUCKET, Key: fileKey }));
      } catch {
        res.writeHead(202, { ...cors, "Content-Type": "application/json" });
        res.end(JSON.stringify({ code: "pending", success: false, message: "not ready" }));
        return;
      }
      const getUrl = await s3.getSignedUrl(
        s3.client,
        new s3.GetObjectCommand({ Bucket: ASSETS_BUCKET, Key: fileKey }),
        { expiresIn: MEDIA_URL_TTL_SEC },
      );
      res.writeHead(200, { ...cors, "Content-Type": "application/json" });
      res.end(JSON.stringify({ code: "000000", success: true, data: { url: getUrl } }));
    } catch (e) {
      res.writeHead(500, { ...cors, "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "internal" }));
    }
    return;
  }

  res.writeHead(404);
  res.end();
});

// ---- WS: token in ?token= query; role decides routing table ----
const wss = new WebSocketServer({ server: httpServer, maxPayload: 1_000_000 });
wss.on("connection", (ws, req) => {
  const q = new URL(req.url, "http://x").searchParams;
  const claims = verifyHubToken(q.get("token"));
  if (!claims) {
    safeSend(ws, { type: "error", error: "unauthorized" });
    ws.close(1008, "unauthorized");
    return;
  }
  ws._role = claims.role;
  ws._tenant = claims.tenant;
  ws._sub = claims.sub;

  if (claims.role === "channel") {
    channels.set(claims.tenant, ws);
    crRegisterChannel(claims.tenant); // B1: record "this Pod holds tenant's channel"
    safeSend(ws, { type: "registered", tenant: claims.tenant });
    ws.on("close", () => {
      if (channels.get(claims.tenant) === ws) {
        channels.delete(claims.tenant);
        crUnregisterChannel(claims.tenant);
      }
    });
    ws.on("message", (raw) => routeChannelToFrontend(claims.tenant, raw));
    return;
  }

  // frontend
  if (!frontends.has(claims.sub)) frontends.set(claims.sub, new Set());
  frontends.get(claims.sub).add(ws);
  crRegisterFrontend(claims.sub); // B1: record "this Pod holds sub's tabs"
  safeSend(ws, { type: "ready", tenant: claims.tenant });
  // Keepalive: the FIRST agent reply can take tens of seconds (gateway cold
  // start ~4.7s + LLM first token), during which the frontend WS is idle. An
  // idle WS gets cut by CloudFront / intermediate proxies (idle timeout) before
  // the reply streams back → the browser saw "中枢连接断开" mid-conversation.
  // Send a protocol-level PING every 25s so the connection is never idle; the
  // browser auto-replies PONG (no frontend code change), and we drop the socket
  // only if a PING goes unanswered for two cycles (dead peer, not slow agent).
  let _fePongMissed = 0;
  const _fePing = setInterval(() => {
    if (ws.readyState !== 1) return;
    if (_fePongMissed >= 2) {
      try { ws.terminate(); } catch { /* already gone */ }
      return;
    }
    _fePongMissed++;
    try { ws.ping(); } catch { /* send race; close handler cleans up */ }
  }, 25000);
  ws.on("pong", () => { _fePongMissed = 0; });
  ws.on("close", () => {
    clearInterval(_fePing);
    const set = frontends.get(claims.sub);
    if (set) {
      set.delete(ws);
      if (set.size === 0) {
        frontends.delete(claims.sub);
        crUnregisterFrontend(claims.sub);
      }
    }
  });
  ws.on("message", (raw) => routeFrontendToChannel(ws, raw));
});

// frontend → channel: stamp the server-verified sub, forward to the tenant's channel.
function routeFrontendToChannel(ws, raw) {
  let msg;
  try {
    msg = JSON.parse(raw.toString());
  } catch {
    return safeSend(ws, { type: "error", error: "bad json" });
  }
  // 会话历史回看 (PRD #40-43): a history_request asks the channel for the past
  // turns of THIS user's own thread. We stamp the server-verified sub (T1, never
  // client-asserted) as senderId so the channel reads only the session that
  // sub+threadId resolves to (structural IDOR guard). threadId is sanitized like
  // the message path. The channel answers with a history_reply we route back.
  if (msg.type === "history_request") {
    const hThread = String(msg.threadId || "").trim().slice(0, 80);
    const threadId = hThread && /^[A-Za-z0-9._:-]+$/.test(hThread) ? hThread : ws._sub;
    const reqId = String(msg.requestId || "").trim().slice(0, 80) || randomBytes(8).toString("hex");
    const limit = Number(msg.limit) || 0;
    // B1: deliver to the tenant's channel local-or-cross-Pod; offline → tell user
    deliverToChannel(ws._tenant, {
      type: "history_request",
      requestId: reqId,
      senderId: ws._sub, // server-verified sub
      senderType: "USER",
      receiverId: ws._tenant,
      receiverType: "BOT",
      threadId,
      limit: limit > 0 ? Math.min(limit, 1000) : undefined,
      chatType: "dm",
    }).then((ok) => {
      if (!ok)
        safeSend(ws, { type: "history_reply", requestId: reqId, messages: [], error: "agent offline" });
    });
    return;
  }
  const text = String(msg.text || "").slice(0, 8000).trim();
  // Inbound 看图: the user may attach uploaded images. Forward only the safe
  // descriptor (fileKey + mimeType) as FILE parts — never a raw URL. The channel
  // resolves the real presigned URL via /files/download-url, which re-checks
  // tenant ownership (IDOR guard). Here we additionally require each fileKey's
  // tenant segment to match this frontend's tenant (the fileKey shape is
  // media/{tenant}/{uuid}.{ext}) so one user can't smuggle another tenant's key.
  const rawAtts = Array.isArray(msg.attachments) ? msg.attachments : [];
  const fileParts = [];
  for (const a of rawAtts.slice(0, 4)) {
    const fileKey = String(a?.fileKey || "");
    const seg = fileKey.split("/"); // [media, tenant, uuid.ext]
    if (seg.length !== 3 || seg[0] !== "media" || seg[1] !== ws._tenant) {
      continue; // foreign / malformed key — drop silently
    }
    fileParts.push({
      kind: "FILE",
      file: {
        uri: "",
        name: String(a?.name || "").slice(0, 200),
        mimeType: String(a?.mimeType || "").slice(0, 80),
      },
      metadata: { fileKey, type: String(a?.type || "image").slice(0, 20) },
      isDone: true,
    });
  }
  // A turn is valid if it has text OR at least one image; reject the empty turn.
  if (!text && fileParts.length === 0)
    return safeSend(ws, { type: "error", error: "empty message" });
  // 多会话 threadId: the client may pass a threadId so one user
  // can hold several isolated conversations. Sanitize it (the channel embeds it
  // into the agent session key as sub:t:threadId for isolation). Absent → the
  // sub itself (single thread per user, backward compatible). senderId is always
  // the server-verified sub (T1), never client-asserted.
  const clientThread = String(msg.threadId || "").trim().slice(0, 80);
  const threadId = clientThread && /^[A-Za-z0-9._:-]+$/.test(clientThread)
    ? clientThread
    : ws._sub;
  // 并发修复: pass the frontend's correlation id (clientMessageId) through to the
  // channel so its reply / reply_delta / error frames carry it back — the
  // frontend matches replies to the exact request bubble instead of FIFO order
  // (which broke under concurrent sends). Sanitize length; opaque to the hub.
  const clientMessageId = String(msg.clientMessageId || "").trim().slice(0, 80);
  const parts = [];
  if (text) parts.push({ kind: "TEXT", text, isDone: true });
  for (const fp of fileParts) parts.push(fp);
  const frame = {
    messageId: randomBytes(8).toString("hex"),
    clientMessageId: clientMessageId || undefined,
    senderId: ws._sub,
    senderType: "USER",
    receiverId: ws._tenant,
    receiverType: "BOT",
    parts,
    operationType: "msg_create",
    threadId,
    chatType: "dm",
  };
  // B1: deliver to the tenant's channel — local Pod or cross-Pod via Redis.
  deliverToChannel(ws._tenant, frame).then((ok) => {
    if (!ok) safeSend(ws, { type: "error", error: "agent offline" });
  });
}

// channel → frontend: deliver the BOT reply only to the originating user's tabs.
function routeChannelToFrontend(tenant, raw) {
  let msg;
  try {
    msg = JSON.parse(raw.toString());
  } catch {
    return;
  }
  const targetSub = String(msg.receiverId || msg.threadId || "").trim();
  if (!targetSub) return;
  // B1: build the SINGLE outbound frame, then deliver to the user's tabs whether
  // they're on THIS Pod or another (deliverToFrontend forwards cross-Pod). The
  // tenant scope (fws._tenant === tenant) is enforced inside deliverToFrontend
  // for local tabs and re-checked on the receiving Pod for forwarded frames.
  const cmid = String(msg.clientMessageId || "").trim() || undefined;
  let out;
  // 会话历史回看 (PRD #40-43): history_reply carries past turns from the VM transcript.
  if (msg.type === "history_reply") {
    out = {
      type: "history_reply",
      requestId: String(msg.requestId || "") || undefined,
      threadId: String(msg.threadId || "").trim() || undefined,
      messages: Array.isArray(msg.messages) ? msg.messages : [],
      error: msg.error ? String(msg.error).slice(0, 120) : undefined,
    };
  } else {
    const parts = msg.parts || [];
    const text = parts.map((p) => p?.text || "").join("");
    const thread = String(msg.threadId || "").trim() || undefined;
    if (msg.type === "reply_delta" || msg.operationType === "msg_update") {
      // 流式输出: cumulative in-progress text; frontend REPLACES the bubble.
      out = { type: "reply_delta", text, threadId: thread, clientMessageId: cmid };
    } else if (msg.type === "reply_error") {
      // agent turn failed; resolve the pending request so the bubble stops spinning.
      out = { type: "reply_error", text: text || "", threadId: thread, clientMessageId: cmid };
    } else {
      // Final reply. Extract FILE parts (agent 出图) as safe descriptors only —
      // fileKey + mimeType + type, never a raw URL (browser resolves via the
      // hub's /files/download-url, which re-checks tenant ownership / IDOR).
      const files = parts
        .filter((p) => p?.kind === "FILE" && p?.metadata?.fileKey)
        .map((p) => ({
          fileKey: p.metadata.fileKey,
          mimeType: p.file?.mimeType || "",
          name: p.file?.name || "",
          type: p.metadata.type || "image",
        }));
      out = { type: "reply", text, files, threadId: thread, clientMessageId: cmid, raw: msg };
    }
  }
  deliverToFrontend(targetSub, tenant, out);
}

httpServer.listen(PORT, "0.0.0.0", () => {
  console.log(
    `[claw-hub] listening on 0.0.0.0:${PORT} (token + wss multiplex)` +
      (HUB_CLUSTERED ? " [clustered]" : ""),
  );
});

// Go-live B1: init cross-Pod routing. The inbox callback fires when ANOTHER Pod
// forwarded a frame for a channel/frontend that THIS Pod holds locally — we
// deliver it to the local connection (this is the receiving end of the Redis
// pub/sub). Degrade-safe: no Redis → init returns false and this never fires.
initClusterRouting((env) => {
  try {
    if (!env || !env.frame) return;
    if (env.kind === "to_channel") {
      const ch = channels.get(env.target);
      if (ch) safeSend(ch, env.frame);
    } else if (env.kind === "to_frontend") {
      const tenant = env.frame._tenant;
      const set = frontends.get(env.target);
      if (set) {
        const { _tenant, ...clean } = env.frame; // strip internal routing field
        for (const fws of set) if (fws._tenant === tenant) safeSend(fws, clean);
      }
    }
  } catch {
    /* never throw on inbox delivery */
  }
}).catch((e) => console.error(`[claw-hub] cluster routing init error: ${e.message}`));

// Go-live B1 (EKS graceful drain): on SIGTERM (Pod rolling / scale-in), stop
// accepting new connections and let in-flight ones finish, so a rolling restart
// doesn't hard-drop live chats. K8s sends SIGTERM then waits terminationGrace
// before SIGKILL; channels/frontends reconnect to a healthy Pod meanwhile (the
// channel already has reconnect+reregister, and the frontend rejects pending on
// close). We close the listener + notify peers, then exit.
let _shuttingDown = false;
function gracefulShutdown(signal) {
  if (_shuttingDown) return;
  _shuttingDown = true;
  console.log(`[claw-hub] ${signal} received — draining (stop accepting, close peers)`);
  try {
    httpServer.close(() => console.log("[claw-hub] http server closed"));
  } catch {
    /* ignore */
  }
  // tell connected frontends/channels to reconnect elsewhere
  for (const set of frontends.values()) {
    for (const ws of set) safeSend(ws, { type: "draining" });
  }
  for (const ws of channels.values()) safeSend(ws, { type: "draining" });
  // give in-flight sends a short grace, then exit so K8s can replace the Pod
  setTimeout(() => process.exit(0), 3000);
}
process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
process.on("SIGINT", () => gracefulShutdown("SIGINT"));
