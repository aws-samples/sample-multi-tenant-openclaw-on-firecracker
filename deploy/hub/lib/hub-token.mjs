// lib/hub-token.mjs — hub 自签会话 token(HMAC)+ media 双源取 token(#136 拆分)。
// 函数体逐字搬自 server.mjs,只动 import。key 的 fail-closed 初始化在 config.mjs。

import { createHmac, timingSafeEqual } from "node:crypto";
import { HUB_TOKEN_KEY } from "./config.mjs";

// ---- hub session token: HMAC over a compact claims string, with a TTL ----
export function issueHubToken(claims) {
  const body = Buffer.from(JSON.stringify(claims)).toString("base64url");
  const sig = createHmac("sha256", HUB_TOKEN_KEY).update(body).digest("base64url");
  return `${body}.${sig}`;
}
export function verifyHubToken(tok) {
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
export function mediaAuth(req) {
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
