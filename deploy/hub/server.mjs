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
//
// #136 拆分:本文件是 composition root(装配+生命周期),业务逻辑在 lib/:
//   config(env 常量) · util(safeSend/readJsonBody) · tenant-store(DDB+授权)
//   · cognito-verify(JWT) · hub-token(会话 token) · channel-hmac(#14 防重放)
//   · media(S3 presign) · http-routes(HTTP 分发) · ws-routing(WS 路由表+双向路由)
// 入口契约不变:`node server.mjs`,端口/healthz/SIGTERM drain 行为逐字保持。

// Go-live B1: cross-Pod routing on EKS (degrade-safe — no Redis = local-only,
// i.e. today's single-process behavior unchanged). See cluster-routing.mjs.
import { initClusterRouting } from "./cluster-routing.mjs";
import { HUB_CLUSTERED, PORT } from "./lib/config.mjs";
import { safeSend } from "./lib/util.mjs";
import { createHubHttpServer } from "./lib/http-routes.mjs";
import { attachWs, channels, frontends } from "./lib/ws-routing.mjs";

const httpServer = createHubHttpServer();
attachWs(httpServer);

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
