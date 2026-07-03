// cluster-routing — go-live B1: cross-Pod routing for the WS hub on EKS.
//
// THE PROBLEM (why multi-Pod hub is fake without this):
//   The hub keeps two in-memory maps — channels (tenant→ws) and frontends
//   (sub→ws). On a SINGLE process that's fine. On EKS with N Pods behind an
//   ALB, a user's browser may land on Pod-A while that tenant's VM channel
//   (an outbound WS) lands on Pod-B. Pod-A's `channels` map has no entry for
//   the tenant, so the message is dropped — chat silently breaks under scale.
//
// THE FIX (this module):
//   A shared ElastiCache Redis holds the location registry:
//     route:channel:{tenant} = podId   (which Pod holds that tenant's channel)
//     route:frontend:{sub}    = podId   (which Pod holds that user's tabs)
//   and a pub/sub fan-out: each Pod SUBSCRIBES to its own inbox channel
//   `hub:pod:{podId}`. When a Pod must deliver a frame to a peer it does NOT
//   hold locally, it looks up the owner Pod in the registry and PUBLISHES the
//   frame to that Pod's inbox; the owner Pod receives it and delivers locally.
//
// DEGRADE-SAFE (critical): if CLAW_HUB_REDIS_ENDPOINT is unset (single-process
// / metal hub), every function here is a no-op and the caller falls back to the
// pure local-Map behavior — i.e. EXACTLY today's single-process semantics, zero
// behavior change. Redis is additive; it only matters when clustered.
//
// ioredis is an optional dependency: if it's not installed but an endpoint IS
// configured, we log and stay local (fail-open to single-Pod, never crash).

import { randomBytes } from "node:crypto";

const REDIS_ENDPOINT = process.env.CLAW_HUB_REDIS_ENDPOINT || "";
const REDIS_TLS = String(process.env.CLAW_HUB_REDIS_TLS || "true").toLowerCase() === "true";
const ROUTE_TTL_SEC = Number(process.env.CLAW_HUB_ROUTE_TTL_SEC || 90); // re-registered on activity
// This Pod's stable id for the lifetime of the process. K8s downward API can
// inject POD_NAME; else a random id. Used as the pub/sub inbox channel key.
const POD_ID = process.env.POD_NAME || `pod-${randomBytes(6).toString("hex")}`;

const KEY_CHANNEL = (tenant) => `route:channel:${tenant}`;
const KEY_FRONTEND = (sub) => `route:frontend:${sub}`;
const INBOX = (podId) => `hub:pod:${podId}`;

let _enabled = false;
let _pub = null; // ioredis client for commands + publish
let _sub = null; // ioredis client for subscribe (must be a separate connection)
let _onInbox = null; // callback(envelope) installed by init()

// Build an ioredis client or return null (dependency missing / endpoint unset).
async function _mkClient() {
  let Redis;
  try {
    ({ default: Redis } = await import("ioredis"));
  } catch {
    return null; // ioredis not installed
  }
  const [host, portStr] = REDIS_ENDPOINT.split(":");
  const opts = {
    host,
    port: Number(portStr || 6379),
    // ElastiCache in-transit encryption → TLS; SNI uses the host.
    ...(REDIS_TLS ? { tls: { servername: host } } : {}),
    lazyConnect: false,
    maxRetriesPerRequest: 2,
    enableOfflineQueue: true,
  };
  return new Redis(opts);
}

// init(onInbox): wire Redis if configured. onInbox(envelope) is called when a
// frame published by another Pod arrives for THIS Pod to deliver locally.
// Returns true if clustering is active, false if running local-only.
export async function initClusterRouting(onInbox) {
  _onInbox = onInbox;
  if (!REDIS_ENDPOINT) {
    console.log("[cluster-routing] no CLAW_HUB_REDIS_ENDPOINT — single-process (local maps only)");
    return false;
  }
  try {
    _pub = await _mkClient();
    _sub = await _mkClient();
    if (!_pub || !_sub) {
      console.error(
        "[cluster-routing] CLAW_HUB_REDIS_ENDPOINT set but ioredis unavailable — " +
          "staying LOCAL-ONLY (cross-Pod routing OFF). Install ioredis in the hub image.",
      );
      _pub = _sub = null;
      return false;
    }
    _pub.on("error", (e) => console.error(`[cluster-routing] redis(pub) error: ${e.message}`));
    _sub.on("error", (e) => console.error(`[cluster-routing] redis(sub) error: ${e.message}`));
    await _sub.subscribe(INBOX(POD_ID));
    _sub.on("message", (_chan, payload) => {
      try {
        const env = JSON.parse(payload);
        if (_onInbox) _onInbox(env);
      } catch {
        /* ignore malformed inbox frame */
      }
    });
    _enabled = true;
    console.log(`[cluster-routing] clustered: podId=${POD_ID}, inbox=${INBOX(POD_ID)}`);
    return true;
  } catch (e) {
    console.error(`[cluster-routing] init failed (${e.message}) — staying LOCAL-ONLY`);
    _pub = _sub = null;
    _enabled = false;
    return false;
  }
}

export function clusterEnabled() {
  return _enabled;
}
export function podId() {
  return POD_ID;
}

// Registry writes — called when a channel/frontend connects or stays active.
// TTL'd so a dead Pod's entries expire; callers refresh on activity.
export function registerChannel(tenant) {
  if (!_enabled) return;
  _pub.set(KEY_CHANNEL(tenant), POD_ID, "EX", ROUTE_TTL_SEC).catch(() => {});
}
export function registerFrontend(sub) {
  if (!_enabled) return;
  _pub.set(KEY_FRONTEND(sub), POD_ID, "EX", ROUTE_TTL_SEC).catch(() => {});
}
// Only clear if WE are the recorded owner (avoid clobbering a newer Pod's claim
// after a reconnect elsewhere — CAS-ish via a tiny Lua check).
const _DEL_IF_MINE =
  "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end";
export function unregisterChannel(tenant) {
  if (!_enabled) return;
  _pub.eval(_DEL_IF_MINE, 1, KEY_CHANNEL(tenant), POD_ID).catch(() => {});
}
export function unregisterFrontend(sub) {
  if (!_enabled) return;
  _pub.eval(_DEL_IF_MINE, 1, KEY_FRONTEND(sub), POD_ID).catch(() => {});
}

// Forward a frame to the Pod that holds `tenant`'s channel. Returns:
//   "local"   — caller should deliver locally (owner is this Pod or unknown)
//   "remote"  — published to the owner Pod; caller should NOT deliver locally
// kind/target identify what the receiving Pod must do with the envelope.
async function _forward(kind, key, target, frame) {
  if (!_enabled) return "local";
  let owner;
  try {
    owner = await _pub.get(key);
  } catch {
    return "local"; // redis hiccup → best-effort local
  }
  if (!owner || owner === POD_ID) return "local";
  const env = { kind, target, frame, from: POD_ID };
  try {
    await _pub.publish(INBOX(owner), JSON.stringify(env));
    return "remote";
  } catch {
    return "local";
  }
}

// frontend→channel: deliver `frame` to tenant's channel, wherever it lives.
export function forwardToChannel(tenant, frame) {
  return _forward("to_channel", KEY_CHANNEL(tenant), tenant, frame);
}
// channel→frontend: deliver `frame` to sub's tabs, wherever they live.
export function forwardToFrontend(sub, tenant, frame) {
  return _forward("to_frontend", KEY_FRONTEND(sub), sub, { ...frame, _tenant: tenant });
}

export async function shutdownClusterRouting() {
  try {
    await _sub?.quit();
  } catch {
    /* ignore */
  }
  try {
    await _pub?.quit();
  } catch {
    /* ignore */
  }
}
