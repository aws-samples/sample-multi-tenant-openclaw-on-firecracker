// lib/tenant-store.mjs — 租户记录读取(DDB)+ 缓存 + 显式授权决策(#136 拆分)。
// 单例状态唯一定义点(js-split 门3):_secretCache/_ownerCache/_ddb 只在本文件。
// 函数体逐字搬自 server.mjs,只动 import。

import {
  APP_SECRETS,
  AWS_REGION,
  SECRET_TTL_MS,
  SHARED_TENANT_ACCESS,
  TENANTS_TABLE,
} from "./config.mjs";

const _secretCache = new Map(); // tenant -> {secret, at}
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
export async function getTenantAccess(tenantId) {
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
export async function authorizeSubForTenant(sub, tenantId) {
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

export async function getTenantSecret(tenantId) {
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
