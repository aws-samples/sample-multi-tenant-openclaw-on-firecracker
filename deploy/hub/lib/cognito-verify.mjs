// lib/cognito-verify.mjs — Cognito JWT 验证(id/access token)+ #98 档B 多 issuer(#136 拆分)。
// 单例状态唯一定义点(js-split 门3):_jwks/_externalJwks 只在本文件。
// 函数体逐字搬自 server.mjs,只动 import。

import {
  COGNITO_CHANNEL_CLIENT_ID,
  COGNITO_CLIENT_ID,
  COGNITO_REGION,
  COGNITO_USER_POOL_ID,
} from "./config.mjs";

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
export function parseExternalIssuers(raw) {
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
export async function verifyExternalIdToken(token) {
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
export async function verifyIdToken(token) {
  const cognito = await verifyCognitoIdToken(token);
  if (cognito && cognito.sub) return cognito;
  return await verifyExternalIdToken(token);
}

export async function verifyCognitoIdToken(token) {
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
export async function verifyCognitoAccessToken(token) {
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
