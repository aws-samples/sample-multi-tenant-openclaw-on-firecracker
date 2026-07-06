// lib/http-routes.mjs — HTTP 请求分发(/healthz /token /channel-token /files/*)(#136 拆分)。
// createServer 回调体逐字搬自 server.mjs(544-771),只包了一层 createHubHttpServer()
// 工厂并把依赖换成 import;路由逻辑/响应/错误分支一字未动。

import { createServer } from "node:http";
import { randomBytes } from "node:crypto";
import {
  ASSETS_BUCKET,
  MEDIA_ALLOWED_MIMES,
  MEDIA_EXT_BY_MIME,
  MEDIA_MAX_BYTES,
  MEDIA_PREFIX,
  MEDIA_URL_TTL_SEC,
  TOKEN_TTL_SEC,
} from "./config.mjs";
import { readJsonBody } from "./util.mjs";
import { issueHubToken, mediaAuth } from "./hub-token.mjs";
import { verifyCognitoAccessToken, verifyIdToken } from "./cognito-verify.mjs";
import { verifyChannelSignature } from "./channel-hmac.mjs";
import { authorizeSubForTenant } from "./tenant-store.mjs";
import { getS3 } from "./media.mjs";

// ---- HTTP: token issuance ----
export function createHubHttpServer() {
  return createServer(async (req, res) => {
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
}
