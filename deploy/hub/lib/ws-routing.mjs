// lib/ws-routing.mjs — WS 连接注册 + 双向路由(frontend↔channel)(#136 拆分)。
// 单例状态唯一定义点(js-split 门3):channels/frontends 路由表只在本文件,export 给
// server.mjs 的 cluster inbox 回调与 graceful drain 使用(同一份 Map,ESM live binding)。
// 函数体逐字搬自 server.mjs;wss 装配包成 attachWs(httpServer) 工厂,connection 回调体一字未动。

import { randomBytes } from "node:crypto";
import { WebSocketServer } from "ws";
// Go-live B1: cross-Pod routing on EKS (degrade-safe — no Redis = local-only,
// i.e. today's single-process behavior unchanged). See cluster-routing.mjs.
import {
  clusterEnabled,
  registerChannel as crRegisterChannel,
  registerFrontend as crRegisterFrontend,
  unregisterChannel as crUnregisterChannel,
  unregisterFrontend as crUnregisterFrontend,
  forwardToChannel as crForwardToChannel,
  forwardToFrontend as crForwardToFrontend,
} from "../cluster-routing.mjs";
import { safeSend } from "./util.mjs";
import { verifyHubToken } from "./hub-token.mjs";

// ---- routing tables ----
// channels: tenantId -> ws (the VM's outbound channel connection)
export const channels = new Map();
// frontends: sub -> Set<ws> (a user may have multiple tabs); each ws carries .tenantId
export const frontends = new Map();

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

// ---- WS: token in ?token= query; role decides routing table ----
export function attachWs(httpServer) {
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
  return wss;
}

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
