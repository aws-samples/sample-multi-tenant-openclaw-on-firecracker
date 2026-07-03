// claw-channel — consumer-facing messaging channel (external plugin, pure JS).
//
// ARCHITECTURE — outbound-dial channel over a self-hosted WS hub:
//
//   frontend (chat mini-app) --wss--> [ claw-hub ] <--wss(OUTBOUND)-- THIS channel --> agent
//
// This channel does NOT open an inbound port. On startAccount it dials OUT to
// the self-hosted hub: it fetches a short-lived token by signing appId/appSecret,
// opens a WebSocket to the hub, then receives USER frames and sends BOT replies
// over that single long-lived connection. The hub multiplexes between this
// channel and the browser, and is the only component the browser talks to.
// gateway.auth.token is untouched. The outbound-dial design keeps the VM with
// zero inbound ports, so there is no listening attack surface on the guest.
//
// Wire protocol (frame shape):
//   { messageId, senderId, senderType:"USER"|"BOT", receiverId, receiverType,
//     parts:[{kind:"TEXT", text, isDone:true}], operationType, threadId, chatType }
//
// Token auth:
//   signature = HMAC-SHA256("{appId}:{timestamp}", Buffer.from(appSecret,"hex"))
//   POST {tokenUrl} {appId,timestamp,signature} -> {token}
//   wsUrl?token={token}
//
// SECURITY MODEL (threats T1-T7):
//   T1 forged userId  -> senderId is set by the HUB from the server-verified
//                        Cognito sub; this channel trusts only what the hub
//                        delivers, and isolates by that id.
//   T2 replay         -> hub tokens are short-lived + HMAC; the channel<->hub
//                        link is a single authenticated long connection.
//   T3 cross-tenant   -> the hub issues a token bound to ONE tenant; this
//                        channel registers ONE tenant; core.buildAgentSessionKey
//                        (dmScope per-channel-peer) isolates per sub.
//   T4 identity-file  -> assertNotIdentityFile basename guard on outbound.
//   T5 injection->RCE -> this channel only forwards; L2 acl/sentinel-guard +
//                        L5 cap-drop/read-only/IMDS-DROP remain the backstops.
//   T7 secret leak    -> appSecret never leaves the VM; logs are redacted.

import { createHmac } from "node:crypto";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, basename } from "node:path";

const CHANNEL_ID = "claw-channel";
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const TOKEN_REFRESH_SEC = 240; // refresh before the hub's 300s TTL
const MAX_INBOUND_MEDIA = 4; // images per user message handed to the agent
const STREAM_THROTTLE_MS = 250; // coalesce streaming reply_delta frames
const LOG_PATH =
  process.env.CLAW_CHANNEL_LOG || "/home/agent/.openclaw/logs/claw-channel.log";

// ---- T4: identity-file basename guard (copied, not imported — plugins must not
// deep-import another extension's src). Mirrors security-guard IDENTITY_FILE_NAMES.
const IDENTITY_FILE_BASENAMES = new Set([
  "soul.md",
  "agents.md",
  "identity.md",
  "user.md",
  "bootstrap.md",
  "heartbeat.md",
  "tools.md",
  "memory.md",
]);
function isIdentityFileName(name) {
  return IDENTITY_FILE_BASENAMES.has(
    basename(String(name).trim()).toLowerCase(),
  );
}

function logLine(entry) {
  try {
    mkdirSync(dirname(LOG_PATH), { recursive: true });
    appendFileSync(LOG_PATH, JSON.stringify(entry) + "\n");
  } catch (_e) {
    /* never let logging crash the channel; fail safe */
  }
}

// ---- Outbound media (agent 出图) — ports outbound-media.ts safety, trimmed.
// Agent reply may reference a LOCAL image file (absolute path / file:// / ~).
// We sniff+whitelist+size-cap it, then upload via the hub's presigned URL and
// emit a FILE part. Remote http(s)/data: URIs are REJECTED (SSRF scope). The
// hub holds the only S3 credentials; we never touch S3.
const OUTBOUND_MEDIA_MAX_BYTES = 20 * 1024 * 1024;
const MEDIA_ALLOWED_MIMES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/pdf",
  "text/plain",
  "text/csv",
]);
const MEDIA_EXT_TO_MIME = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  pdf: "application/pdf",
  txt: "text/plain",
  csv: "text/csv",
};
// magic-byte sniff; undefined when unsure (then fall back to extension).
function sniffMime(buf) {
  if (
    buf.length >= 8 &&
    buf[0] === 0x89 &&
    buf[1] === 0x50 &&
    buf[2] === 0x4e &&
    buf[3] === 0x47 &&
    buf[4] === 0x0d &&
    buf[5] === 0x0a &&
    buf[6] === 0x1a &&
    buf[7] === 0x0a
  )
    return "image/png";
  if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff)
    return "image/jpeg";
  if (buf.length >= 6) {
    const h = buf.subarray(0, 6).toString("ascii");
    if (h === "GIF87a" || h === "GIF89a") return "image/gif";
  }
  if (
    buf.length >= 12 &&
    buf.subarray(0, 4).toString("ascii") === "RIFF" &&
    buf.subarray(8, 12).toString("ascii") === "WEBP"
  )
    return "image/webp";
  if (buf.length >= 5 && buf.subarray(0, 4).toString("ascii") === "%PDF")
    return "application/pdf";
  return undefined;
}
function classifyType(mime) {
  return mime.startsWith("image/") ? "image" : "document";
}

// Read a local media file referenced by the agent. Mirrors readMediaSource:
// rejects remote/data URIs, enforces size cap + MIME whitelist + identity guard.
async function readLocalMedia(mediaUrl) {
  const fs = await import("node:fs/promises");
  const os = await import("node:os");
  const path = await import("node:path");
  const { fileURLToPath } = await import("node:url");
  if (!mediaUrl || typeof mediaUrl !== "string")
    throw new Error("empty mediaUrl");
  if (/^https?:\/\//i.test(mediaUrl))
    throw new Error("remote URL not supported (SSRF scope)");
  if (mediaUrl.startsWith("data:")) throw new Error("data: URI not supported");
  let p = mediaUrl.startsWith("file://")
    ? fileURLToPath(mediaUrl)
    : mediaUrl.startsWith("~")
      ? path.join(os.homedir(), mediaUrl.slice(1))
      : mediaUrl;
  const stat = await fs.stat(p);
  if (!stat.isFile()) throw new Error("not a file");
  if (stat.size > OUTBOUND_MEDIA_MAX_BYTES)
    throw new Error(
      `exceeds ${OUTBOUND_MEDIA_MAX_BYTES / (1024 * 1024)}MB cap`,
    );
  const buffer = await fs.readFile(p);
  const fileName = basename(p) || `media-${Date.now()}`;
  if (isIdentityFileName(fileName))
    throw new Error("refused: protected identity file");
  const ext = (fileName.split(".").pop() || "").toLowerCase();
  const mime =
    sniffMime(buffer) || MEDIA_EXT_TO_MIME[ext] || "application/octet-stream";
  if (!MEDIA_ALLOWED_MIMES.has(mime))
    throw new Error(`unsupported MIME: ${mime}`);
  return { buffer, fileName, mimeType: mime };
}

// Upload a local media file via the hub's presigned URL, return its fileKey.
async function uploadMediaViaHub(account, fetchToken, media) {
  const token = await fetchToken(account);
  if (!token) throw new Error("no hub token for media upload");
  const base = account.hubUrl.replace(/\/+$/, "");
  const up = await fetch(`${base}/files/upload-url`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      mimeType: media.mimeType,
      size: media.buffer.length,
    }),
  });
  if (!up.ok) throw new Error(`upload-url ${up.status}`);
  const j = await up.json();
  const { fileKey, url } = j?.data || {};
  if (!fileKey || !url) throw new Error("upload-url missing fileKey/url");
  const put = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": media.mimeType },
    body: media.buffer,
  });
  if (!put.ok) throw new Error(`S3 PUT ${put.status}`);
  return fileKey;
}

// ---- Inbound media (用户 → agent「看图」, multimodal vision input).
// The user uploads an image; the frontend PUTs it to S3 via the hub presign and
// sends the channel a USER frame carrying a FILE part (metadata.fileKey, uri
// empty — an IDOR-safe descriptor that never carries a resolvable URL). We resolve that
// fileKey to a short-lived presigned GET via the hub's /files/download-url
// (which re-checks tenant ownership), fetch the bytes, re-validate magic-byte
// MIME + size cap, write to a per-message temp file under the VM, and hand the
// LOCAL path to core's media-understanding via ctx.MediaPaths. core then runs
// applyMediaUnderstanding (get-reply.ts) and feeds the image to the multimodal
// model — we never hand-roll an LLM call. Remote URLs are never fetched from
// frame content (only the hub-issued presigned URL), and the file is unlinked
// after dispatch.
const INBOUND_MEDIA_MAX_BYTES = 20 * 1024 * 1024;
async function downloadInboundMedia(account, fetchToken, fileKey, mimeHint) {
  const fs = await import("node:fs/promises");
  const os = await import("node:os");
  const path = await import("node:path");
  const crypto = await import("node:crypto");
  if (!fileKey || typeof fileKey !== "string") throw new Error("empty fileKey");
  // fileKey shape is media/{tenant}/{uuid}.{ext}; reject anything else so a
  // crafted frame can't make us request arbitrary keys (the hub IDOR guard is
  // the real authority, this is defense in depth).
  if (!/^media\/[^/]+\/[A-Za-z0-9._-]+$/.test(fileKey))
    throw new Error("bad fileKey shape");
  const token = await fetchToken(account);
  if (!token) throw new Error("no hub token for media download");
  const base = account.hubUrl.replace(/\/+$/, "");
  const dl = await fetch(
    `${base}/files/download-url?fileKey=${encodeURIComponent(fileKey)}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!dl.ok) throw new Error(`download-url ${dl.status}`);
  const dj = await dl.json();
  const getUrl = dj?.data?.url;
  if (!getUrl) throw new Error("download-url missing url");
  const obj = await fetch(getUrl);
  if (!obj.ok) throw new Error(`S3 GET ${obj.status}`);
  const buffer = Buffer.from(await obj.arrayBuffer());
  if (buffer.length > INBOUND_MEDIA_MAX_BYTES)
    throw new Error(`exceeds ${INBOUND_MEDIA_MAX_BYTES / (1024 * 1024)}MB cap`);
  // Trust the sniffed bytes over the client-asserted mimeHint; only fall back to
  // the hint (then a safe default) when the magic-byte sniff is inconclusive.
  const mime = sniffMime(buffer) || mimeHint || "application/octet-stream";
  if (!MEDIA_ALLOWED_MIMES.has(mime))
    throw new Error(`unsupported MIME: ${mime}`);
  const ext =
    Object.keys(MEDIA_EXT_TO_MIME).find((k) => MEDIA_EXT_TO_MIME[k] === mime) ||
    "bin";
  const tmp = path.join(
    os.tmpdir(),
    `claw-inbound-${crypto.randomUUID()}.${ext}`,
  );
  await fs.writeFile(tmp, buffer, { mode: 0o600 });
  return { path: tmp, mimeType: mime };
}

// ---- 会话历史回看 (用户→agent「历史记录」, PRD #40-43) ──────────────────
// core 的 chat.history RPC / readSessionMessages 在 gateway 层,未导出到
// plugin-sdk,channel 无法直接调(且禁止 deep-import core src)。但历史 transcript
// 就在 VM 自己的 agent 数据盘上(~/.openclaw/agents/{agentId}/sessions/{sessionId}
// .jsonl),channel 在 guest 内对它有 fs 只读权限。因此在本地重新实现读取逻辑
// 三步(读 session 消息 + 去信封前缀 + slice/limit),纯 fs 只读、零新依赖、不碰 core src。
//
// IDOR 守卫: 历史的 sessionKey 由 channel 用 hub 转发的「server-verified sub」
// (T1,前端不可伪造) + threadId 经 resolveAgentRoute 派生(与发消息同一路径),
// 所以一个 sub 永远只能读到自己 thread 的历史,跨 sub/跨 thread 读不到。
const HISTORY_HARD_MAX = 1000; // hard cap on history turns returned in one read
const HISTORY_DEFAULT_LIMIT = 50; // 前端「历史记录」首屏默认(50 够且更省传输)

// stripEnvelope: 去用户消息里的 "[Channel Timestamp] " / "[ISO 时间戳] " 信封
// 前缀,只留用户可见文本(与写入侧的信封格式对应)。
const _ENVELOPE_PREFIX = /^\[([^\]]+)\]\s*/;
const _ENVELOPE_CHANNELS = [
  "WebChat",
  "WhatsApp",
  "Telegram",
  "Signal",
  "Slack",
  "Discord",
  "Google Chat",
  "iMessage",
  "Teams",
  "Matrix",
  "Zalo",
  "Zalo Personal",
  "BlueBubbles",
];
function _looksLikeEnvelopeHeader(header) {
  if (/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z\b/.test(header)) return true;
  if (/\d{4}-\d{2}-\d{2} \d{2}:\d{2}\b/.test(header)) return true;
  return _ENVELOPE_CHANNELS.some((label) => header.startsWith(`${label} `));
}
function stripEnvelope(text) {
  const match = String(text).match(_ENVELOPE_PREFIX);
  if (!match) return text;
  if (!_looksLikeEnvelopeHeader(match[1] ?? "")) return text;
  return text.slice(match[0].length);
}

// Extract the user-visible text from a transcript message's content (string or
// [{type,text}]).
function _extractText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter(
      (p) =>
        p &&
        typeof p.text === "string" &&
        (p.type === "text" ||
          p.type === "output_text" ||
          p.type === "input_text"),
    )
    .map((p) => p.text)
    .join("");
}

// Read a session transcript JSONL and return frontend-renderable history items.
// Derives sessionKey + storePath from the SAME resolveSessionContext the message
// path uses (verified sub + threadId), so the IDOR guard is structural: a sub can
// only read the thread it can write. sessionsDir = dirname(storePath); transcript
// file = {sessionId}.jsonl (or the entry's sessionFile). Returns
// [{role:"user"|"assistant", text}] chronological, last `limit` turns, envelopes
// stripped. Read-only — never writes the agent data disk.
async function readSessionHistory({
  core,
  cfg,
  account,
  cognitoSub,
  threadId,
  limit,
}) {
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const max = Math.min(
    HISTORY_HARD_MAX,
    Math.max(1, Number(limit) || HISTORY_DEFAULT_LIMIT),
  );
  const { route, storePath } = resolveSessionContext({
    core,
    cfg,
    account,
    cognitoSub,
    threadId,
  });
  const sessionKey = route.sessionKey;
  // 1. resolve the store entry for this sessionKey → sessionId + sessionFile.
  let store;
  try {
    store = JSON.parse(await fs.readFile(storePath, "utf-8"));
  } catch {
    return []; // no store yet = no history
  }
  const entry = store?.[sessionKey];
  const sessionId = entry?.sessionId;
  if (!sessionId || typeof sessionId !== "string") return [];
  // 2. resolve the transcript path. sessionFile (relative to sessionsDir) wins;
  // else {sessionId}.jsonl. Validate the basename so a crafted store can't make
  // us read outside the sessions dir (defense in depth; the store is VM-local).
  const sessionsDir = path.dirname(storePath);
  let transcriptPath;
  const sf =
    typeof entry?.sessionFile === "string" ? entry.sessionFile.trim() : "";
  if (sf) {
    const resolved = path.resolve(sessionsDir, sf);
    if (!resolved.startsWith(path.resolve(sessionsDir) + path.sep)) return [];
    transcriptPath = resolved;
  } else {
    if (!/^[a-z0-9][a-z0-9._-]{0,127}$/i.test(sessionId)) return [];
    transcriptPath = path.join(sessionsDir, `${sessionId}.jsonl`);
  }
  // 3. read + parse JSONL, keep only user/assistant turns with visible text.
  let raw;
  try {
    raw = await fs.readFile(transcriptPath, "utf-8");
  } catch {
    return [];
  }
  const items = [];
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch {
      continue;
    }
    const msg = parsed?.message;
    if (!msg || typeof msg !== "object") continue;
    const role = typeof msg.role === "string" ? msg.role.toLowerCase() : "";
    if (role !== "user" && role !== "assistant") continue; // drop system/tool/toolResult
    let text = _extractText(msg.content);
    if (typeof text !== "string") continue;
    text = role === "user" ? stripEnvelope(text) : text;
    text = text.trim();
    if (!text) continue; // skip empty/media-only turns (no renderable text)
    items.push({ role: role === "user" ? "user" : "assistant", text });
  }
  // 4. last `max` turns (chronological).
  return items.length > max ? items.slice(-max) : items;
}

// ---- token signature: HMAC-SHA256("{appId}:{ts}", appSecret-as-hex)
function generateSignature(appId, appSecret, timestamp) {
  return createHmac("sha256", Buffer.from(appSecret, "hex"))
    .update(`${appId}:${timestamp}`)
    .digest("hex");
}

// Resolve the per-account hub credentials from channel config. These are
// injected at LAUNCH into channels.claw-channel (per-VM, never baked into the
// read-only golden image, never sent to the browser).
//   hubUrl     — base http(s) URL of the hub for token fetch (POST /channel-token)
//   wsUrl      — ws(s) URL of the hub for the long connection
//   appId      — per-tenant app id
//   appSecret  — per-tenant app secret (hex), used to sign the token request
function resolveAccount(cfg, accountId) {
  const ch = cfg?.channels?.[CHANNEL_ID] ?? {};
  return {
    accountId: accountId || "default",
    enabled: ch.enabled === true,
    hubUrl: ch.hubUrl ?? process.env.CLAW_HUB_URL ?? "",
    wsUrl: ch.wsUrl ?? process.env.CLAW_HUB_WS ?? "",
    appId: ch.appId ?? process.env.CLAW_CHANNEL_APP_ID ?? "",
    appSecret:
      ch.appSecret ?? ch.secret ?? process.env.CLAW_CHANNEL_SECRET ?? "",
    // WI-002 — end-to-end Cognito. When the control plane provisions a per-tenant
    // Cognito machine-user and injects these at LAUNCH, the channel proves its
    // tenant identity with a Cognito access token instead of the self-rolled HMAC.
    // All fields present → Cognito path; otherwise → legacy HMAC (graceful rollout).
    // The password is a per-tenant secret minted by the control plane, cold-injected
    // to the read-only disk — same blast-radius class as appSecret, never in browser.
    cognitoRegion:
      ch.cognitoRegion ?? process.env.CLAW_CHANNEL_COGNITO_REGION ?? "",
    cognitoClientId:
      ch.cognitoClientId ?? process.env.CLAW_CHANNEL_COGNITO_CLIENT_ID ?? "",
    cognitoUsername:
      ch.cognitoUsername ?? process.env.CLAW_CHANNEL_COGNITO_USERNAME ?? "",
    cognitoPassword:
      ch.cognitoPassword ?? process.env.CLAW_CHANNEL_COGNITO_PASSWORD ?? "",
  };
}

// True when every field the Cognito sign-in needs is present. Drives the
// fetchHubToken branch (Cognito vs legacy HMAC) so a half-configured account
// never half-attempts Cognito and silently fails — it cleanly uses HMAC.
function hasCognitoCreds(account) {
  return Boolean(
    account.cognitoRegion &&
    account.cognitoClientId &&
    account.cognitoUsername &&
    account.cognitoPassword,
  );
}

// Sign in the per-tenant machine-user via Cognito's InitiateAuth (USER_PASSWORD_AUTH)
// and return its access token. Pure HTTPS POST to the public Cognito IDP endpoint —
// NO AWS SDK, NO SigV4, NO AWS credentials (USER_PASSWORD_AUTH is an unsigned call),
// so the zero-credential golden-image guarantee holds. The app client is public
// (no client secret) so there is no SECRET_HASH to compute in-guest.
async function fetchCognitoAccessToken(account) {
  const endpoint = `https://cognito-idp.${account.cognitoRegion}.amazonaws.com/`;
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
    },
    body: JSON.stringify({
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: account.cognitoClientId,
      AuthParameters: {
        USERNAME: account.cognitoUsername,
        PASSWORD: account.cognitoPassword,
      },
    }),
  });
  if (!resp.ok) return null;
  const data = await resp.json().catch(() => null);
  const tok = data?.AuthenticationResult?.AccessToken;
  return typeof tok === "string" && tok.length > 0 ? tok : null;
}

// ---- fetch a short-lived hub token by signing appId/appSecret.
async function fetchHubToken(account) {
  const tokenUrl = `${account.hubUrl.replace(/\/+$/, "")}/channel-token`;
  let resp;
  if (hasCognitoCreds(account)) {
    // WI-002 Cognito path: sign in the per-tenant machine-user, present the
    // access token as a Bearer to the hub. tenant identity rides in the token's
    // (Cognito-signed, unforgeable) username claim — no body needed.
    const accessToken = await fetchCognitoAccessToken(account).catch(
      () => null,
    );
    if (!accessToken) return null;
    resp = await fetch(tokenUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${accessToken}`,
      },
      body: "{}",
    });
  } else {
    // Legacy HMAC path: sign appId/appSecret. Kept for graceful rollout — VMs
    // baked before the Cognito image still connect during the rolling rebuild.
    const timestamp = Math.floor(Date.now() / 1000);
    const signature = generateSignature(
      account.appId,
      account.appSecret,
      timestamp,
    );
    resp = await fetch(tokenUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: account.appId, // appId == tenantId in our single-account model
        appId: account.appId,
        timestamp,
        signature,
      }),
    });
  }
  if (!resp.ok) return null;
  const data = await resp.json();
  if (!data?.token) return null;
  // hub returns {token, expires_in} (TTL 300s). Carry an absolute expiry so the
  // connection layer can PROACTIVELY refresh ~60s early instead of blindly
  // reconnecting on a fixed timer and risking sends on an about-to-expire token.
  const ttl = Number(data.expires_in) || 300;
  return { token: data.token, expiresAt: Date.now() + ttl * 1000 };
}

function buildChannelPlugin(getCore) {
  return {
    id: CHANNEL_ID,
    meta: {
      id: CHANNEL_ID,
      label: "Claw Channel",
      selectionLabel: "Consumer (WS via hub)",
      docsPath: "/channels/claw-channel",
      blurb:
        "Consumer-facing AI chat over an outbound WS to the claw-hub, Cognito-sub bound.",
      aliases: ["claw"],
    },
    capabilities: {
      chatTypes: ["direct"],
      reactions: false,
      threads: false,
      media: true,
      nativeCommands: false,
      blockStreaming: true,
    },
    config: {
      listAccountIds: (cfg) =>
        Object.keys(cfg?.channels?.[CHANNEL_ID]?.accounts ?? { default: {} }),
      resolveAccount: (cfg, accountId) => resolveAccount(cfg, accountId),
    },
    // gateway.startAccount: dial OUT to the hub and keep a long WS connection.
    // No inbound port is opened.
    gateway: {
      startAccount: async (ctx) => {
        const core = getCore();
        const cfg = ctx?.cfg ?? core.config.loadConfig();
        const account = resolveAccount(cfg, ctx?.accountId);
        const log = core.logging.getChildLogger();
        if (!account.appSecret || !account.wsUrl || !account.hubUrl) {
          log.error(
            "[claw-channel] missing hub config (wsUrl/hubUrl/appSecret) — refusing to start (fail closed)",
          );
          return { stop: () => {} };
        }

        // Resolve `ws` (CommonJS). An external plugin lives in its own dir with
        // NO node_modules, so a bare `await import("ws")` resolves from the
        // plugin's location and FAILS (ERR_MODULE_NOT_FOUND) — that early-exit
        // was making startAccount return immediately and the runtime treated it
        // as "channel exited". `ws` IS installed under the openclaw package, so
        // we anchor a require() at the openclaw CLI entry (resolved from PATH /
        // well-known global locations) and load `ws` from there. Try several
        // strategies; only fail closed if all miss.
        let WebSocket = null;
        try {
          const { createRequire } = await import("node:module");
          const anchors = [
            "/usr/lib/node_modules/openclaw/dist/index.js",
            "/usr/local/lib/node_modules/openclaw/dist/index.js",
            process.env.OPENCLAW_DIST || "",
          ].filter(Boolean);
          for (const a of anchors) {
            try {
              const req = createRequire(a);
              const m = req("ws");
              WebSocket = m.WebSocket || m.default?.WebSocket || m;
              if (typeof WebSocket === "function") break;
            } catch {
              /* try next anchor */
            }
          }
          if (typeof WebSocket !== "function") {
            // last resort: bare dynamic import (works if plugin dir can resolve ws)
            const m = await import("ws");
            WebSocket = m.WebSocket || m.default?.WebSocket || m.default || m;
          }
        } catch {
          WebSocket = null;
        }
        if (typeof WebSocket !== "function") {
          log.error("[claw-channel] 'ws' module unavailable — cannot start");
          return { stop: () => {} };
        }

        let ws = null;
        let stopped = false;
        let backoff = RECONNECT_BASE_MS;
        let refreshTimer = null;
        let keepaliveTimer = null;
        // ── 连接层自愈状态 ──────────
        // 治"用一阵后 Failed to fetch":根因是 token 过期边界 + 无心跳 + send
        // 失败不重试。下面四件套补齐(单账号模型,无需多账号 Map)。
        let cachedToken = null; // { token, expiresAt }
        let lastPongAt = 0;
        const KEEPALIVE_MS = 20000; // 20s 协议层 ping 间隔
        const TOKEN_MARGIN_MS = 60000; // 提前 60s 刷
        const SEND_RETRY_DEADLINE_MS = 8000; // delta 可丢用短 deadline
        const SEND_RETRY_DEADLINE_FINAL_MS = 30000; // 最终 reply 不可丢用长 deadline

        // ensureToken:有效就复用,临过期(<60s)或无则重取。避免每次重连盲取、
        // 也避免在 token 即将过期时还拿它发帧被 hub 静默拒。
        const ensureToken = async () => {
          if (
            cachedToken &&
            cachedToken.expiresAt &&
            cachedToken.expiresAt > Date.now() + TOKEN_MARGIN_MS
          ) {
            return cachedToken.token;
          }
          const fresh = await fetchHubToken(account).catch(() => null);
          if (!fresh) return null;
          cachedToken = fresh;
          return fresh.token;
        };

        // sendJsonWithRetry:发送失败(连接表面 open 实则死)→ 等新连接重试,
        // 直到 deadline。WS 死了由 close handler 触发 reconnect 拉起新 ws,
        // 这里轮询等它 OPEN 再发。
        const waitForLiveWs = async (deadline) => {
          while (Date.now() < deadline) {
            if (ws && ws.readyState === 1) return ws;
            await new Promise((r) => setTimeout(r, 500));
          }
          return ws && ws.readyState === 1 ? ws : null;
        };
        const sendJsonWithRetry = async (payload, deadlineMs) => {
          const deadline = Date.now() + (deadlineMs || SEND_RETRY_DEADLINE_MS);
          let attempt = 0;
          while (Date.now() < deadline) {
            const live = await waitForLiveWs(deadline);
            if (!live) break;
            try {
              await new Promise((resolve, reject) => {
                live.send(payload, (err) => (err ? reject(err) : resolve()));
              });
              return true; // sent
            } catch (e) {
              // send 失败 = 连接死了。主动 terminate 触发 close→reconnect,
              // 等下一个 live ws 再试(删死连接)。
              attempt++;
              try {
                ws?.terminate?.();
              } catch {
                /* ignore */
              }
              await new Promise((r) => setTimeout(r, 300 * attempt));
            }
          }
          logLine({
            ts: new Date().toISOString(),
            decision: "send-failed-deadline",
          });
          return false; // gave up
        };
        // 并发修复 (CRITICAL): connecting clients can fire several messages at
        // once on the SAME session (sub+threadId). core's default queueMode is
        // "collect" — a concurrent 2nd/3rd turn on a live session returns
        // undefined (enqueueFollowupRun), which made the channel emit an EMPTY
        // reply and left the user's bubble spinning forever. We serialize per
        // session key so each turn fully completes (its own reply) before the
        // next starts. Map<sessionKey, Promise> is the tail of each session's
        // chain; entries are pruned when their chain drains. Survives across the
        // handler closures (lives in startAccount scope, not per-connection).
        const _sessionQueues = new Map();
        const runSerialBySession = (sessionKey, task) => {
          const prev = _sessionQueues.get(sessionKey) || Promise.resolve();
          const next = prev.then(task, task); // run regardless of prior outcome
          // prune when this is the last link (avoid unbounded Map growth)
          _sessionQueues.set(sessionKey, next);
          next.finally(() => {
            if (_sessionQueues.get(sessionKey) === next)
              _sessionQueues.delete(sessionKey);
          });
          return next;
        };

        const connect = async () => {
          if (stopped) return;
          const token = await ensureToken();
          if (!token) {
            logLine({ ts: new Date().toISOString(), decision: "token-fail" });
            scheduleReconnect();
            return;
          }
          const url = `${account.wsUrl.replace(/\/+$/, "")}?token=${encodeURIComponent(token)}`;
          ws = new WebSocket(url);

          // 非 101 升级响应(403 token拒/429 限流/5xx):优雅 backoff 重连而非
          // 静默挂死。标准 ws 库事件名同此。
          ws.on("unexpected-response", (_req, res) => {
            log.error(
              `[claw-channel] hub upgrade rejected HTTP ${res?.statusCode} — backoff reconnect`,
            );
            logLine({
              ts: new Date().toISOString(),
              decision: "ws-unexpected-response",
              status: res?.statusCode,
            });
            // token 被拒可能是它过期了 → 清缓存,下次 ensureToken 重取
            if (res?.statusCode === 403) cachedToken = null;
            try {
              ws?.terminate?.();
            } catch {
              /* ignore */
            }
          });

          // 协议层 pong(标准 ws 库:ws.ping() 后服务端自动回 pong)。记录活性。
          ws.on("pong", () => {
            lastPongAt = Date.now();
          });

          ws.on("open", () => {
            backoff = RECONNECT_BASE_MS;
            lastPongAt = Date.now();
            log.info("[claw-channel] connected to hub (outbound WS)");
            // keepalive:每 20s 协议层 ping,防 NAT/LB 静默 drop 空闲连接。
            // 连续 2 个周期没 pong(>2.5×interval)判定连接冻死 → terminate 触发
            // reconnect。
            keepaliveTimer = setInterval(() => {
              if (!ws || ws.readyState !== 1) return;
              if (lastPongAt && Date.now() - lastPongAt > KEEPALIVE_MS * 2.5) {
                log.error(
                  "[claw-channel] keepalive: no pong, terminating dead conn",
                );
                try {
                  ws.terminate();
                } catch {
                  /* ignore */
                }
                return;
              }
              try {
                ws.ping();
              } catch {
                /* ignore */
              }
            }, KEEPALIVE_MS);
            // 到点提前刷 token:清缓存让 ensureToken 重取,再干净重连。比旧的
            // 盲目 close 多了"复用未过期 token"能力(短于 TTL-margin 的重连不必换token)。
            refreshTimer = setTimeout(() => {
              cachedToken = null;
              try {
                ws?.close(1000, "token-refresh");
              } catch {
                /* ignore */
              }
            }, TOKEN_REFRESH_SEC * 1000);
          });

          ws.on("message", async (raw) => {
            let frame;
            try {
              frame = JSON.parse(raw.toString());
            } catch {
              return;
            }
            // Only handle USER message frames (senderType gate).
            if (frame?.type === "registered" || frame?.type === "ready") return;
            // 会话历史回看 (PRD #40-43): a history_request frame asks for the past
            // turns of the user's own thread. senderId is the hub-forwarded
            // server-verified sub (T1) — the channel reads ONLY the session that
            // sub+threadId resolves to (structural IDOR guard, same route the
            // message path writes). We read the VM-local transcript JSONL and
            // reply with a history_reply frame the hub routes back to that user.
            if (frame?.type === "history_request") {
              const hSub = String(frame?.senderId || "")
                .trim()
                .toLowerCase();
              const hThread =
                typeof frame?.threadId === "string" && frame.threadId.trim()
                  ? frame.threadId.trim().toLowerCase()
                  : undefined;
              const hReqId =
                (typeof frame?.requestId === "string" &&
                  frame.requestId.trim()) ||
                `${Date.now()}`;
              if (!hSub) return;
              (async () => {
                let messages = [];
                try {
                  messages = await readSessionHistory({
                    core,
                    cfg,
                    account,
                    cognitoSub: hSub,
                    threadId: hThread,
                    limit: frame?.limit,
                  });
                } catch (hErr) {
                  logLine({
                    ts: new Date().toISOString(),
                    decision: "history-error",
                    reason: String(hErr).slice(0, 160),
                  });
                }
                await sendJsonWithRetry(
                  JSON.stringify({
                    type: "history_reply",
                    requestId: hReqId,
                    senderId: account.appId,
                    senderType: "BOT",
                    receiverId: hSub,
                    receiverType: "USER",
                    threadId: hThread || hSub,
                    messages,
                    chatType: "dm",
                  }),
                  SEND_RETRY_DEADLINE_FINAL_MS,
                ).catch(() => {});
              })();
              return;
            }
            if (frame?.senderType && frame.senderType !== "USER") return;
            // 小写化:同一逻辑用户若大小写不一致(前端传混合大小写 threadId)
            // 会裂成多个 session → 上下文丢失。
            // sub 是 UUID 本就小写,threadId 是主要风险点,统一小写保 session 一致。
            const cognitoSub = String(frame?.senderId || "")
              .trim()
              .toLowerCase();
            // 多会话 threadId: a USER frame may carry a threadId so
            // one user can hold several isolated conversations. We thread it into
            // the agent session key (peerId = sub:t:threadId) and echo it back on
            // the BOT reply so the hub routes the answer to the right thread.
            // Absent/empty → falls back to single-thread-per-sub (backward compat).
            const threadId =
              typeof frame?.threadId === "string" && frame.threadId.trim()
                ? frame.threadId.trim().toLowerCase()
                : undefined;
            // 并发修复: the frontend stamps its own correlation id (clientMessageId)
            // so reply / reply_delta / error frames can be matched back to the
            // exact request bubble (no FIFO guessing). Echo it on every outbound
            // frame for this turn. Falls back to the hub messageId.
            const clientMessageId =
              (typeof frame?.clientMessageId === "string" &&
                frame.clientMessageId.trim()) ||
              frame?.messageId ||
              `${Date.now()}`;
            // Serialization key = the agent session dimension (sub + thread).
            const _serialKey = `${cognitoSub}::${threadId || ""}`;
            const text = (frame?.parts || [])
              .map((p) => (typeof p?.text === "string" ? p.text : ""))
              .join("")
              .trim();
            // Inbound 看图: a USER frame may carry FILE parts (metadata.fileKey)
            // for images the user uploaded. Resolve each via the hub's
            // download-url, fetch to a local temp file, and hand the paths to
            // the agent through ctx.MediaPaths (core multimodal). Cap the number
            // per message; an image-only message (empty text) is still valid.
            const inboundFileParts = (frame?.parts || []).filter(
              (p) => p?.kind === "FILE" && p?.metadata?.fileKey,
            );
            if (!cognitoSub || (!text && inboundFileParts.length === 0)) return;
            // 并发修复: process this turn inside the per-session serial chain so a
            // burst of messages on the same session runs one-at-a-time (each gets
            // its own non-empty reply) instead of tripping core's collect-mode
            // empty-reply path. Different sessions/threads still run in parallel.
            runSerialBySession(_serialKey, async () => {
              const mediaPaths = [];
              const mediaTypes = [];
              const tempPaths = [];
              for (const fp of inboundFileParts.slice(0, MAX_INBOUND_MEDIA)) {
                try {
                  const got = await downloadInboundMedia(
                    account,
                    fetchHubToken,
                    String(fp.metadata.fileKey),
                    typeof fp?.file?.mimeType === "string"
                      ? fp.file.mimeType
                      : undefined,
                  );
                  mediaPaths.push(got.path);
                  mediaTypes.push(got.mimeType);
                  tempPaths.push(got.path);
                } catch (dErr) {
                  // a bad/foreign fileKey must not drop the whole turn — log + skip.
                  logLine({
                    ts: new Date().toISOString(),
                    decision: "inbound-media-skip",
                    reason: String(dErr).slice(0, 160),
                  });
                }
              }
              // 流式输出: throttled emitter of reply_delta frames during dispatch.
              // core hands cumulative cleaned text; we coalesce to STREAM_THROTTLE_MS
              // and send a delta frame the hub forwards to the user's thread. The
              // frontend REPLACES the streaming bubble each delta; the authoritative
              // final text arrives in the normal BOT (msg_create) frame after.
              let _lastStreamAt = 0;
              let _streamTimer = null;
              let _pendingStreamText = "";
              const sendDeltaFrame = (cumulativeText) => {
                // delta 是高频流式预览(可丢、不可阻塞节流):用短 deadline 的
                // sendJsonWithRetry 但 fire-and-forget(不 await),失败就丢这一帧,
                // 下一帧累积文本会补上。最终 reply 才是不可丢的(下面 await 长 deadline)。
                const payload = JSON.stringify({
                  messageId: frame.messageId || `${Date.now()}`,
                  clientMessageId,
                  senderId: account.appId,
                  senderType: "BOT",
                  receiverId: cognitoSub,
                  receiverType: "USER",
                  parts: [
                    { kind: "TEXT", text: cumulativeText, isDone: false },
                  ],
                  operationType: "msg_update",
                  type: "reply_delta",
                  threadId: threadId || cognitoSub,
                  chatType: "dm",
                });
                void sendJsonWithRetry(payload, SEND_RETRY_DEADLINE_MS);
              };
              const onDelta = (cumulativeText) => {
                _pendingStreamText = cumulativeText;
                const now = Date.now();
                if (now - _lastStreamAt >= STREAM_THROTTLE_MS) {
                  _lastStreamAt = now;
                  sendDeltaFrame(_pendingStreamText);
                } else if (!_streamTimer) {
                  _streamTimer = setTimeout(
                    () => {
                      _streamTimer = null;
                      _lastStreamAt = Date.now();
                      sendDeltaFrame(_pendingStreamText);
                    },
                    STREAM_THROTTLE_MS - (now - _lastStreamAt),
                  );
                }
              };
              try {
                const { text: reply, mediaUrls } = await dispatchToAgent({
                  core,
                  cfg,
                  account,
                  cognitoSub,
                  threadId,
                  text,
                  mediaPaths,
                  mediaTypes,
                  onDelta,
                });
                if (_streamTimer) {
                  clearTimeout(_streamTimer);
                  _streamTimer = null;
                }
                // Build parts: TEXT first, then a FILE part per agent-generated
                // image. Each local image is uploaded via the hub's presigned URL;
                // file.uri is left empty on purpose so
                // the browser resolves the real URL via metadata.fileKey + the
                // hub's download-url endpoint (defends IDOR/SSRF).
                const parts = [];
                if (reply && reply.length > 0) {
                  parts.push({ kind: "TEXT", text: reply, isDone: true });
                }
                for (const mediaUrl of mediaUrls) {
                  try {
                    const media = await readLocalMedia(mediaUrl);
                    const fileKey = await uploadMediaViaHub(
                      account,
                      fetchHubToken,
                      media,
                    );
                    parts.push({
                      kind: "FILE",
                      file: {
                        uri: "",
                        name: media.fileName,
                        mimeType: media.mimeType,
                      },
                      metadata: { fileKey, type: classifyType(media.mimeType) },
                      isDone: true,
                    });
                  } catch (mErr) {
                    // media failure must not drop the text reply — log + skip.
                    logLine({
                      ts: new Date().toISOString(),
                      decision: "media-skip",
                      reason: String(mErr).slice(0, 160),
                    });
                  }
                }
                if (parts.length === 0) {
                  parts.push({ kind: "TEXT", text: "", isDone: true });
                }
                // BOT reply frame back to the hub (routed to that user only).
                const out = {
                  messageId: frame.messageId || `${Date.now()}`,
                  clientMessageId,
                  senderId: account.appId,
                  senderType: "BOT",
                  receiverId: cognitoSub,
                  receiverType: "USER",
                  parts,
                  operationType: "msg_create",
                  threadId: threadId || cognitoSub,
                  chatType: "dm",
                };
                // 最终 reply 不可丢:长 deadline 重试(send 失败等新连接重发),
                // 治"agent 答完了但 send 进黑洞、用户永远等不到回复"(所有 send 都走 retry)。
                await sendJsonWithRetry(
                  JSON.stringify(out),
                  SEND_RETRY_DEADLINE_FINAL_MS,
                );
              } catch (err) {
                logLine({
                  ts: new Date().toISOString(),
                  decision: "error",
                  reason: String(err).slice(0, 200),
                });
                // 并发修复: still send an (error) reply frame so the frontend's
                // pending request for THIS clientMessageId resolves instead of
                // spinning forever. Echo clientMessageId for exact routing.
                // error reply 也走重试(不可丢:让前端那条 pending 解掉不转圈)
                await sendJsonWithRetry(
                  JSON.stringify({
                    messageId: frame.messageId || `${Date.now()}`,
                    clientMessageId,
                    senderId: account.appId,
                    senderType: "BOT",
                    receiverId: cognitoSub,
                    receiverType: "USER",
                    parts: [{ kind: "TEXT", text: "", isDone: true }],
                    operationType: "msg_create",
                    type: "reply_error",
                    threadId: threadId || cognitoSub,
                    chatType: "dm",
                  }),
                  SEND_RETRY_DEADLINE_FINAL_MS,
                ).catch(() => {});
              } finally {
                // Unlink the inbound image temp files once the agent has consumed
                // them (core copies/encodes during dispatch). Best-effort.
                if (tempPaths.length > 0) {
                  const fsp = await import("node:fs/promises");
                  for (const tp of tempPaths) {
                    await fsp.unlink(tp).catch(() => {});
                  }
                }
              }
            }); // end runSerialBySession
          });

          ws.on("close", (code, reason) => {
            if (refreshTimer) {
              clearTimeout(refreshTimer);
              refreshTimer = null;
            }
            if (keepaliveTimer) {
              clearInterval(keepaliveTimer);
              keepaliveTimer = null;
            }
            // 连接诊断:记 code/reason 便于
            // 区分 token-refresh 主动断 vs 网络异常断,排查频繁重连。
            logLine({
              ts: new Date().toISOString(),
              decision: "ws-close",
              code,
              reason: String(reason || "").slice(0, 40),
            });
            ws = null;
            scheduleReconnect();
          });
          ws.on("error", (err) => {
            log.warn(
              `[claw-channel] hub WS error: ${String(err).slice(0, 120)}`,
            );
            try {
              ws?.close();
            } catch {
              /* ignore */
            }
          });
        };

        const scheduleReconnect = () => {
          if (stopped) return;
          const wait = backoff;
          backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
          setTimeout(connect, wait);
        };

        await connect();
        log.info("[claw-channel] outbound hub channel started");

        // CRITICAL: keep startAccount PENDING for the channel's whole lifetime.
        // OpenClaw's channel runtime treats startAccount RETURNING as "channel
        // exited" and auto-restarts it. So we await an abort-gated promise that
        // only resolves on stop: resolve only via stop() or ctx.abortSignal.
        // Returning {stop} immediately (the old code) caused the "channel exited
        // without an error" restart loop.
        let release = null;
        const stop = () => {
          stopped = true;
          if (refreshTimer) clearTimeout(refreshTimer);
          if (keepaliveTimer) clearInterval(keepaliveTimer);
          try {
            ws?.close(1000, "stop");
          } catch {
            /* ignore */
          }
          if (release) release();
        };
        await new Promise((resolve) => {
          release = resolve;
          const sig = ctx?.abortSignal;
          if (sig) {
            if (sig.aborted) {
              stop();
              return;
            }
            sig.addEventListener("abort", stop, { once: true });
          }
        });
        return { stop };
      },
    },
    outbound: {
      deliveryMode: "direct",
      sendText: async ({ text }) => {
        // The request/response reply path is inline in the WS handler above;
        // this satisfies core's outbound contract for non-inline emits.
        return {
          ok: true,
          info: "claw-channel delivers inline over the hub WS",
          text,
        };
      },
    },
  };
}

// ---- Resolve the isolated agent session context (route + storePath) for a
// given verified sub + threadId. SINGLE source of truth for the session
// dimension, shared by dispatchToAgent (send) and readSessionHistory (read) so
// the history IDOR guard derives the SAME sessionKey the message path writes to
// — a sub can only ever read the thread it can write. Forces per-channel-peer
// isolation (T3) without mutating the original cfg. cognitoSub MUST be the
// hub-forwarded server-verified sub (never client-asserted).
function resolveSessionContext({ core, cfg, account, cognitoSub, threadId }) {
  // 断言兜底 (#17): cognitoSub 是会话隔离的唯一维度键。若调用方漏传/传空/传非
  // 字符串,peerId 会退化成 `undefined`/空串,导致所有用户被路由到同一个 session
  // ——跨租户串台。这里 fail-loud 立即抛错,绝不让串台的会话上下文被静默构造。
  // cognitoSub MUST 是 hub 转发的服务端已验证 sub(绝不接受 client-asserted)。
  if (typeof cognitoSub !== "string" || cognitoSub.trim() === "") {
    throw new Error(
      `[claw-channel] resolveSessionContext: cognitoSub must be a non-empty ` +
        `string (server-verified sub); got ${typeof cognitoSub}. Refusing to ` +
        `build a session context that could cross-talk.`,
    );
  }
  const isolatedCfg = {
    ...cfg,
    session: { ...(cfg?.session ?? {}), dmScope: "per-channel-peer" },
  };
  // 断言兜底 (#17): dmScope 是 core routing 判定"每个 peer 一个独立会话"的开关。
  // 上一行已硬编码 per-channel-peer,但显式断言防将来有人重构时把它改坏/漏设——
  // 没带对的 dmScope 就 fail-loud,而不是静默退化成共享会话串台。
  if (isolatedCfg.session.dmScope !== "per-channel-peer") {
    throw new Error(
      `[claw-channel] resolveSessionContext: dmScope must be ` +
        `"per-channel-peer" for tenant isolation; got ` +
        `"${isolatedCfg.session.dmScope}". Refusing to route without ` +
        `per-peer session isolation.`,
    );
  }
  // 多会话: embed threadId into the peer id with a `:t:` separator (NOT
  // `:thread:`). No threadId → peerId = sub (single thread per user, backward
  // compatible).
  const peerId = threadId ? `${cognitoSub}:t:${threadId}` : cognitoSub;
  const route = core.channel.routing.resolveAgentRoute({
    cfg: isolatedCfg,
    channel: CHANNEL_ID,
    accountId: account.accountId,
    peer: { kind: "direct", id: peerId },
  });
  const storePath = core.channel.session.resolveStorePath(cfg?.session?.store, {
    agentId: route.agentId,
  });
  return { route, storePath, isolatedCfg };
}

// ---- Dispatch an inbound message into an isolated agent session and collect
// the reply. Uses core's own routing + session + reply APIs so session
// isolation (T3) and envelope handling are core-owned, not hand-rolled.
async function dispatchToAgent({
  core,
  cfg,
  account,
  cognitoSub,
  threadId,
  text,
  mediaPaths,
  mediaTypes,
  onDelta,
}) {
  // T3: one isolated session per Cognito sub + thread (per-channel-peer). The
  // route + storePath come from the shared resolveSessionContext so the read
  // (history) and write (this dispatch) paths agree on the exact sessionKey.
  const { route, storePath } = resolveSessionContext({
    core,
    cfg,
    account,
    cognitoSub,
    threadId,
  });
  // 看图: hand the locally-downloaded image paths to core's media-understanding.
  // normalizeAttachments (media-understanding/attachments.ts) reads ctx.MediaPaths
  // / ctx.MediaTypes (parallel arrays) and applyMediaUnderstanding (get-reply.ts)
  // runs the multimodal model over them, folding the image description into the
  // agent's Body. We never call the LLM ourselves.
  const hasMedia = Array.isArray(mediaPaths) && mediaPaths.length > 0;
  const inbound = {
    Body: text,
    BodyForAgent: text,
    RawBody: text,
    CommandBody: text,
    From: `${CHANNEL_ID}:${cognitoSub}`,
    To: `${CHANNEL_ID}:${cognitoSub}`,
    SessionKey: route.sessionKey,
    AccountId: route.accountId,
    ChatType: "direct",
    ConversationLabel: `cognito:${cognitoSub}`,
    SenderId: cognitoSub,
    Provider: CHANNEL_ID,
    Surface: CHANNEL_ID,
    OriginatingChannel: CHANNEL_ID,
    OriginatingTo: `${CHANNEL_ID}:${cognitoSub}`,
    Timestamp: Date.now(),
  };
  if (hasMedia) {
    inbound.MediaPaths = mediaPaths;
    if (Array.isArray(mediaTypes) && mediaTypes.length > 0) {
      inbound.MediaTypes = mediaTypes;
    }
  }
  const ctxPayload = core.channel.reply.finalizeInboundContext(inbound);
  await core.channel.session.recordInboundSession({
    storePath,
    sessionKey: ctxPayload.SessionKey ?? route.sessionKey,
    ctx: ctxPayload,
    onRecordError: (err) =>
      core.logging
        .getChildLogger()
        .error(`[claw-channel] session record error: ${String(err)}`),
  });

  // Collect the agent's reply text + any local media it wants to send.
  // T4: refuse identity-file attachments outright. Local media paths are queued
  // here and uploaded by the caller (which holds the hub-token fetcher).
  let collected = "";
  const mediaUrls = [];
  // ⚠️ DO NOT pass replyOptions.onPartialReply here. Registering core's
  // onPartialReply flips the run into STREAMING mode, which makes runReplyAgent
  // (auto-reply/reply/agent-runner.ts:161 `shouldSteer && isStreaming`) route the
  // turn through queueEmbeddedPiMessage / the active-run steering path instead of
  // running it directly. In this build that streaming path wedges inside
  // pi-coding-agent's createAgentSession (attempt.ts:484 — armed BEFORE the run's
  // only abort timer, so a stall there never times out): the agent run hangs
  // forever before any LLM HTTP request, the per-session lane never settles, and
  // the browser sees "回复超时". Proof: the CLI `openclaw agent` path (non-streaming,
  // direct runEmbeddedPiAgent) returns in ~8s for the SAME session/agent/model,
  // while the streaming channel path never reaches the LLM. So we run NON-streaming
  // (no onPartialReply) and approximate streaming from the buffered deliver()
  // callback instead — each text payload is fed to onDelta as it lands, giving the
  // frontend incremental reply_delta frames without the wedging streaming run.
  await core.channel.reply.dispatchReplyWithBufferedBlockDispatcher({
    ctx: ctxPayload,
    cfg,
    dispatcherOptions: {
      deliver: async (payload) => {
        if (payload?.mediaUrl) {
          if (isIdentityFileName(payload.mediaUrl)) {
            throw new Error(
              "outbound attachment refused: protected identity file",
            );
          }
          mediaUrls.push(String(payload.mediaUrl));
        }
        if (typeof payload?.text === "string") {
          collected += payload.text;
          // Pseudo-stream: surface incremental text from the buffered dispatcher
          // (not core's streaming hook) so the UI still updates progressively.
          if (onDelta) {
            try {
              onDelta(collected);
            } catch {
              /* never let a streaming-frame send crash the dispatch */
            }
          }
        }
      },
    },
  });
  return { text: collected, mediaUrls };
}

// ---- External plugin entry: plain register(api).
let RUNTIME = null;
export default {
  id: CHANNEL_ID,
  configSchema: { type: "object", additionalProperties: false, properties: {} },
  register(api) {
    RUNTIME = api.runtime;
    api.registerChannel({ plugin: buildChannelPlugin(() => RUNTIME) });
    api.logger?.info?.("[claw-channel] registered (external, outbound hub WS)");
  },
};

// Named exports for unit testing the pure history-reading path (no runtime,
// no WS). These are not used by the plugin loader (which uses the default
// export) — they let the test harness exercise the JSONL parse + envelope
// strip + slice + IDOR-route derivation without standing up a VM.
export {
  readSessionHistory,
  resolveSessionContext,
  stripEnvelope,
  HISTORY_HARD_MAX,
  HISTORY_DEFAULT_LIMIT,
  // Pure security/parse helpers — exported for unit testing. Exercising these
  // in isolation (no runtime, no WS) locks in the guarantees the channel leans
  // on: the HMAC token signature, the magic-byte MIME sniff, the identity-file
  // basename guard (T4), the media-type classifier, and envelope-header
  // detection. None are used by the plugin loader (it uses the default export).
  generateSignature,
  sniffMime,
  classifyType,
  isIdentityFileName,
  resolveAccount,
  hasCognitoCreds,
  IDENTITY_FILE_BASENAMES,
  MEDIA_ALLOWED_MIMES,
  MEDIA_EXT_TO_MIME,
};
