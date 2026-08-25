// clawconsole-bff — ALB target Lambda。两职责,入口只做路由分发:
//   1) 托管 console 前端静态文件(取代 S3);config.js 动态返回,里面【没有】任何 API key。
//   2) /capi/* 代持 admin key,把控制面调用转发到现有控制面 API Gateway。
//
// 身份门在 ALB(authenticate-cognito):到得了本 Lambda 的请求 = 已登录运营员。
// 门后所有运营员共享同一把 admin key 的权限(不做 per-user 分级,产品决策)。
// key 从不下发到浏览器 —— 前端 config.js 零 key,真 key 只存在于本 Lambda 的 env。

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize, extname } from "node:path";
import { route as routeTraces } from "./traces.mjs";
import { route as routeLogs } from "./logs.mjs";

// @aws-sdk/client-ssm 由 Lambda Node 20 runtime 内建(未装进部署包)。
// 本地开发/单测机器上可能没有 → 走 lazy import,允许 handler.mjs 无依赖载入。
// 真跑 SSM 端点时才会 import;pure validator 类单测无需装 SDK。

const __dirname = dirname(fileURLToPath(import.meta.url));
const WEB_DIR = join(__dirname, "web"); // 打包进来的 console 前端

const CTRL_BASE = process.env.CTRL_API_BASE; // https://xxx.execute-api.../v1
const CTRL_KEY = process.env.CTRL_API_KEY; // admin x-api-key(只在后端)
const API_PREFIX = "/capi"; // 前端调 /capi/tenants → 控制面 /tenants
const COGNITO_DOMAIN = process.env.COGNITO_DOMAIN || "";
const COGNITO_CLIENT_ID = process.env.COGNITO_CLIENT_ID || "";
const LOGOUT_URI = process.env.BFF_LOGOUT_URI || "/";
const OBS_ASSETS_BUCKET = process.env.OBS_ASSETS_BUCKET || "";
const OBS_S3_PREFIX = "deployment/observability/";
const OBS_ALLOWED_KEYS = new Set([
  "adot/adot-config.yaml",
  "fluent-bit/edge/fluent-bit.conf",
  "fluent-bit/edge/parsers.conf",
  "fluent-bit/edge/extract_trace_root.lua",
]);
// ponytail: 惰性加载 @aws-sdk/client-s3 —— Lambda Node 20 runtime 内置该 SDK,
// 本地跑单测不需装它(测试用 setObsFetcher 注入 stub)。
let _obsFetcher = null;
export function setObsFetcher(fn) { _obsFetcher = fn; }
async function obsFetch(key) {
  if (_obsFetcher) return _obsFetcher(key);
  const { S3Client, GetObjectCommand } = await import("@aws-sdk/client-s3");
  const client = new S3Client({ region: process.env.AWS_REGION || "us-east-1" });
  return client.send(new GetObjectCommand({ Bucket: OBS_ASSETS_BUCKET, Key: OBS_S3_PREFIX + key }));
}

// ALB 要求 statusDescription 为「码 + 空格 + 原因短语」(如 "200 OK"),纯 "200" 会被判无效 → 502。
const REASON = {
  200: "200 OK",
  400: "400 Bad Request",
  403: "403 Forbidden",
  404: "404 Not Found",
  500: "500 Internal Server Error",
  502: "502 Bad Gateway",
};
function statusDesc(code) {
  return REASON[code] || `${code} Status`;
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
};

// 动态 config.js:同源相对路径 /capi,零 key。前端 app.core.js 读 OC_DEFAULT_API_URL
// 作为 apiUrl、OC_DEFAULT_API_KEY 作为 x-api-key 头 —— 这里把 key 置空,url 指向 BFF。
// sub/email/username/exp/iss)。故用 token 里的 username 调 Cognito AdminListGroupsForUser
// 查真实组 → 角色。USER_POOL_ID 由 CDK 注入 env;查失败/无 pool → 安全默认 viewer。
async function roleForUser(username) {
  const poolId = process.env.USER_POOL_ID || "";
  if (!poolId || !username || username === "unknown") return "viewer";
  try {
    const { CognitoIdentityProviderClient, AdminListGroupsForUserCommand } =
      await import("@aws-sdk/client-cognito-identity-provider");
    const c = new CognitoIdentityProviderClient({});
    const r = await c.send(new AdminListGroupsForUserCommand({
      UserPoolId: poolId, Username: username,
    }));
    return roleFromGroups((r.Groups || []).map((g) => g.GroupName));
  } catch (e) {
    console.log("[oidc] roleForUser failed for " + username + ": " + e);
    return "viewer";
  }
}

async function dynamicConfigJs(headers) {
  // 经 window.OC_ROLE 暴露给前端,让 canWrite() 在 BFF 架构下拿到真角色。
  const ident = parseOidcIdentity(headers);
  const role = await roleForUser(ident.username);
  return [
    `window.OC_DEFAULT_API_URL = "${API_PREFIX}/";`,
    `window.OC_ROLE = "${role}";`,
    // 前端不再持真 key。给一个非敏感占位哨兵 —— console 现有逻辑用 `if (apiUrl && apiKey)`
    // 守卫是否发请求(app.core.js/app.tenants.js),空串会让它永不发请求。占位值满足守卫,
    // 且它到 BFF 后被丢弃(BFF 只用自己 env 里的真 key 注入),泄露了也毫无价值。
    // 这样 console UI/逻辑一行不改,仍达成"前端零真实 key"。
    `window.OC_DEFAULT_API_KEY = "via-bff-no-key";`,
    `window.OC_CONSOLE_BASE = "";`,
    `window.OC_DASHBOARD_BASE = "";`,
    `window.OC_DUAL_DOMAIN_MODE = "false";`,
    `window.OC_VERSION = "bff-poc";`,
    `window.OC_REGION = "${process.env.AWS_REGION || "us-east-1"}";`,
    `window.OC_ASSETS_BUCKET = "";`,
    // Cognito 登录门在 ALB 层,前端不需要再自己跳登录 → 留空走 auth.js 放行分支。
    `window.OC_COGNITO_DOMAIN = "";`,
    `window.OC_COGNITO_CLIENT_ID = "";`,
    `window.OC_COGNITO_REDIRECT_URI = "";`,
    "",
  ].join("\n");
}

function textResp(status, body, contentType) {
  return {
    statusCode: status,
    statusDescription: statusDesc(status),
    isBase64Encoded: false,
    headers: { "Content-Type": contentType || "text/plain; charset=utf-8" },
    body,
  };
}

// ── 静态文件 serve(白名单后缀 + 路径归一化防穿越)──────────────────────────
async function serveStatic(urlPath, headers) {
  let rel = urlPath;
  if (rel === "/" || rel === "" || rel === "/index.html") {
    rel = "/index.html";
  }
  if (rel === "/config.js") {
    return textResp(200, await dynamicConfigJs(headers), MIME[".js"]);
  }
  const ext = extname(rel);
  if (!MIME[ext]) return textResp(404, "not found");
  const abs = normalize(join(WEB_DIR, rel));
  if (!abs.startsWith(WEB_DIR)) return textResp(403, "forbidden"); // 穿越防护
  try {
    const buf = await readFile(abs);
    const isText = /text|json|svg/.test(MIME[ext]);
    return {
      statusCode: 200,
      statusDescription: statusDesc(200),
      isBase64Encoded: !isText,
      headers: { "Content-Type": MIME[ext] },
      body: isText ? buf.toString("utf-8") : buf.toString("base64"),
    };
  } catch {
    return textResp(404, "not found");
  }
}

// ── 控制面代理:注入 admin key,原样透传 method/body/query ────────────────────
// ALB 传进来的 query 值是 URL 编码态且不解码(与 API GW 不同);若直接喂
// URLSearchParams,它会把已存在的 %-转义再编一次(%3A→%253A),双重编码 →
// 控制面收到 %3A → ISO8601 正则失败 400(snapshot_time 带冒号最先暴露)。
// 先 decode 一次再重编码,保证恰好单层编码;对未编码值 decode 为 no-op。
export function buildForwardQuery(qs) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(qs || {})) {
    let val = v;
    try { val = decodeURIComponent(v); } catch { /* 非法 %-序列:原样透传 */ }
    params.append(k, val);
  }
  return params.toString();
}

// ── 控制面授权头:默认 x-api-key;CTRL_API_AUTH_MODE=iam 时用 execute-api SigV4 ──
// (#572)IAM 模式用 BFF 自身 execution role 的临时凭据签名,供控制面 API Gateway 开启
// IAM 鉴权后无缝适配(BFF 不再依赖 x-api-key)。默认 apikey = 现状零回归。signer 惰性
// 加载(SDK 仅 IAM 模式才拉);__setSigner 供单测注入,避免真装 SDK。
let _signRequest = null;
export function __setSigner(fn) { _signRequest = fn; }
async function loadSigner() {
  if (!_signRequest) {
    const mod = await import("./sigv4-client.mjs");
    _signRequest = mod.signRequest;
  }
  return _signRequest;
}
async function ctrlAuthHeaders(url, method, body) {
  const base = { "content-type": "application/json" };
  if ((process.env.CTRL_API_AUTH_MODE || "apikey").toLowerCase() === "iam") {
    const sign = await loadSigner();
    return await sign({ url, method, body, headers: base });
  }
  return { ...base, "x-api-key": CTRL_KEY };
}

async function proxyControlPlane(event, subPath) {
  const qs = event.queryStringParameters || {};
  const query = buildForwardQuery(qs);
  const url = `${CTRL_BASE.replace(/\/+$/, "")}/${subPath.replace(/^\/+/, "")}${query ? "?" + query : ""}`;
  const method = event.httpMethod || "GET";

  let body;
  if (method !== "GET" && method !== "HEAD" && event.body != null) {
    body = event.isBase64Encoded
      ? Buffer.from(event.body, "base64").toString("utf-8")
      : event.body;
  }

  const res = await fetch(url, {
    method,
    headers: await ctrlAuthHeaders(url, method, body),
    body,
  });
  const text = await res.text();
  return {
    statusCode: res.status,
    statusDescription: statusDesc(res.status),
    isBase64Encoded: false,
    headers: { "Content-Type": res.headers.get("content-type") || "application/json" },
    body: text,
  };
}

// GET /capi/obs-config          → 列所有观测配置对象 + 版本 metadata
// GET /capi/obs-config?key=<w>  → 拉指定对象全文 + 版本 metadata(w 必须在白名单)
// PUT /capi/obs-config          → 501 未实现:改配置=写 S3,涉及 IAM 写权限(安全红线)+
//                                  YAML 校验 + 审计 + 触发 systemd reload,留人工评审。
async function handleObsConfig(event) {
  if ((event.httpMethod || "GET") !== "GET") {
    return textResp(501, JSON.stringify({
      error: "write not implemented",
      reason: "PUT/POST 涉及 S3 写 IAM(安全红线) + YAML 校验 + 审计 + 触发重载;留人工评审(#229 后续)",
    }), "application/json");
  }
  if (!OBS_ASSETS_BUCKET) {
    return textResp(502, JSON.stringify({ error: "OBS_ASSETS_BUCKET not configured" }), "application/json");
  }
  const qs = event.queryStringParameters || {};
  const key = qs.key;
  if (key) {
    if (!OBS_ALLOWED_KEYS.has(key)) {
      return textResp(400, JSON.stringify({ error: "key not in allowlist", allowed: [...OBS_ALLOWED_KEYS] }), "application/json");
    }
    return await fetchObsObject(key);
  }
  // 无 key = 列全部配置对象概要(逐个 HEAD-equivalent via GetObject)。
  const items = [];
  for (const k of OBS_ALLOWED_KEYS) {
    try {
      const r = await obsFetch(k);
      items.push({
        key: k,
        last_modified: r.LastModified?.toISOString?.() || null,
        content_length: r.ContentLength ?? null,
        etag: r.ETag || null,
        sha256: r.Metadata?.sha256 || null,
        uploaded_at: r.Metadata?.["uploaded-at"] || null,
        git_commit: r.Metadata?.["git-commit"] || null,
      });
      // 释放流(不读 body)
      r.Body?.destroy?.();
    } catch (e) {
      items.push({ key: k, error: String(e?.name || e?.message || e) });
    }
  }
  return textResp(200, JSON.stringify({ bucket: OBS_ASSETS_BUCKET, prefix: OBS_S3_PREFIX, items }), "application/json");
}

async function fetchObsObject(key) {
  const r = await obsFetch(key);
  const chunks = [];
  for await (const chunk of r.Body) chunks.push(chunk);
  const body = Buffer.concat(chunks).toString("utf-8");
  return {
    statusCode: 200,
    statusDescription: statusDesc(200),
    isBase64Encoded: false,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "X-Obs-Sha256": r.Metadata?.sha256 || "",
      "X-Obs-Uploaded-At": r.Metadata?.["uploaded-at"] || "",
      "X-Obs-Git-Commit": r.Metadata?.["git-commit"] || "",
    },
    body: JSON.stringify({
      key,
      body,
      last_modified: r.LastModified?.toISOString?.() || null,
      sha256: r.Metadata?.sha256 || null,
      uploaded_at: r.Metadata?.["uploaded-at"] || null,
      git_commit: r.Metadata?.["git-commit"] || null,
    }),
  };
}

// ── 审计:掩码敏感参数值 ─────────────────────────────────────────────────────
const SENSITIVE_RE = /token|key|secret|password/i;
export function maskParams(params) {
  if (!params || typeof params !== "object") return params;
  const out = {};
  for (const [k, v] of Object.entries(params)) {
    if (SENSITIVE_RE.test(k)) {
      out[k] = "[MASKED]";
    } else {
      out[k] = typeof v === "string" && v.length > 64 ? v.slice(0, 64) + "..." : v;
    }
  }
  return out;
}

// 角色。角色 = cognito:groups 里最高权限(admin>operator>viewer),没组 → viewer。
// 前端拿不到这个 header(它在 ALB→BFF 之间),故 BFF 解出来经动态 config.js 暴露给前端,
// 让 console 按真实角色隐藏写操作入口(canWrite 门控在 BFF 架构下才能生效)。
function roleFromGroups(groups) {
  let g = groups || [];
  if (typeof g === "string") g = [g];
  const rank = { viewer: 0, operator: 1, admin: 2 };
  let best = "viewer", bestRank = -1;
  for (const x of g) {
    if (rank[x] !== undefined && rank[x] > bestRank) { best = x; bestRank = rank[x]; }
  }
  return best;
}

function parseOidcIdentity(headers) {
  const data = headers?.["x-amzn-oidc-data"] || "";
  if (!data) return { sub: "unknown", email: "unknown", role: "viewer" };
  try {
    const payload = JSON.parse(Buffer.from(data.split(".")[1], "base64").toString());
    // ALB 的 x-amzn-oidc-data 走 OIDC userInfo,不含 cognito:groups(实测),故这里只取
    // sub/email/username;角色由 roleForUser(username) 另查 Cognito 组(见 dynamicConfigJs)。
    return {
      sub: payload.sub || "unknown",
      email: payload.email || "unknown",
      username: payload.username || payload.sub || "unknown",
    };
  } catch {
    return { sub: "parse-error", email: "parse-error", username: "unknown" };
  }
}

function emitAudit(event, identity, resp, startMs) {
  const record = {
    audit: true,
    ts: new Date().toISOString(),
    sub: identity.sub,
    email: identity.email,
    method: event.httpMethod || "GET",
    path: event.path || "/",
    status: resp.statusCode,
    params_masked: maskParams(event.queryStringParameters),
    latency_ms: Date.now() - startMs,
  };
  console.log(JSON.stringify(record));
}

// ── Logout:清 ALB session cookie + 302 Cognito hosted UI /logout ────────────
function handleLogout() {
  if (!COGNITO_DOMAIN || !COGNITO_CLIENT_ID) {
    return textResp(502, "logout not configured: missing COGNITO_DOMAIN or COGNITO_CLIENT_ID");
  }
  const logoutUrl =
    `https://${COGNITO_DOMAIN}/logout?client_id=${COGNITO_CLIENT_ID}&logout_uri=${encodeURIComponent(LOGOUT_URI)}`;
  return {
    statusCode: 302,
    statusDescription: "302 Found",
    isBase64Encoded: false,
    headers: {
      Location: logoutUrl,
      "Set-Cookie":
        "AWSELBAuthSessionCookie-0=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Secure",
    },
    body: "",
  };
}

// ── /console 前缀 strip(客户 PDF 10):去前缀后路由到静态或 /capi ────────────
function stripConsolePrefix(path) {
  return path.replace(/^\/console(?=\/|$)/, "") || "/";
}

// Lazy X-Ray SDK loader — the real SDK is only required for the trace viewer
// routes, and tests replace this via __setXray() so nothing pulls the SDK.
let _xray = null;
async function loadXray() {
  if (_xray) return _xray;
  const mod = await import("./xray-client.mjs");
  _xray = mod.xray;
  return _xray;
}
export function __setXray(fake) {
  _xray = fake;
}

// Insights + AOS). Same pattern as X-Ray: SDK loads only when the route is hit,
// tests inject fakes via __setLogDeps() so nothing pulls the SDK.
let _logDeps = null;
async function loadLogDeps() {
  if (_logDeps) return _logDeps;
  const [cw, ao] = await Promise.all([import("./cwlogs-client.mjs"), import("./aos-client.mjs")]);
  _logDeps = { cwlogs: cw.cwlogs, aos: ao.aos };
  return _logDeps;
}
export function __setLogDeps(fake) {
  _logDeps = fake;
}

// ── R15.2 SSM 默认值 console 可查改 ─────────────────────────────────────────
// 客户运维要看/改的 SSM 参数 —— LiteLLM 网关 host、shared vkey、config_template、
// 镜像 manifest 版本。GET 全部读回(vkey 类字段掩码只回尾 4 位);POST 写入 +
// 校验非空 + 简单格式(sk- 前缀或长度)+ 复用现有 emitAudit 记审计。key 写 SecureString,
// 让 host role 的 kms:Decrypt 能读(mint-shared-vkey.sh 契约同款)。
const SSM_PARAMS = {
  litellm_host: { name: "/openclaw/litellm-host", masked: false, secure: false },
  litellm_shared_vkey: {
    name: "/openclaw/litellm-shared-vkey",
    masked: true,
    secure: true,
  },
  config_template: {
    name: "/openclaw/config-template",
    masked: false,
    secure: false,
  },
  rootfs_manifest_version: {
    name: "/openclaw/rootfs-manifest-version",
    masked: false,
    secure: false,
  },
};

let _ssmClient;
let _ssmMod;
async function getSsmClient() {
  if (_ssmClient) return _ssmClient;
  if (!_ssmMod) _ssmMod = await import("@aws-sdk/client-ssm");
  _ssmClient = new _ssmMod.SSMClient({});
  return _ssmClient;
}
async function ssmMod() {
  if (!_ssmMod) _ssmMod = await import("@aws-sdk/client-ssm");
  return _ssmMod;
}

// 掩码 key 类值:回尾 4 位,前面固定 `sk-****` 让 UI 看得出这是 sk-key。
export function maskSecretValue(v) {
  if (!v) return "";
  const s = String(v);
  if (s.length <= 4) return "****";
  return "****" + s.slice(-4);
}

// R15.2 GET:一次读全部 SSM 默认值(vkey 类掩码)。SSM GetParameters 支持批量 (up
// to 10),我们只有 4 个,一次 API 调完事。缺参数(SSM 里没设)返回空串,不 500。
async function getSystemDefaults() {
  const names = Object.values(SSM_PARAMS).map((p) => p.name);
  const client = await getSsmClient();
  const { GetParametersCommand } = await ssmMod();
  const cmd = new GetParametersCommand({ Names: names, WithDecryption: true });
  const res = await client.send(cmd);
  const byName = new Map((res.Parameters || []).map((p) => [p.Name, p.Value]));
  const out = {};
  for (const [key, meta] of Object.entries(SSM_PARAMS)) {
    const raw = byName.get(meta.name) || "";
    out[key] = meta.masked ? maskSecretValue(raw) : raw;
  }
  return out;
}

// R15.2 校验:key 类要求非空 + sk- 前缀或至少 20 字符(litellm 兼容 sk-... / raw
// hex,两种都放行,拒空 + 短过分)。其它值只要求非空 + 长度 ≤ 4KB(SSM 硬上限)。
export function validateSystemDefault(key, value) {
  if (!Object.prototype.hasOwnProperty.call(SSM_PARAMS, key)) {
    return "unknown key";
  }
  if (typeof value !== "string" || value.length === 0) {
    return "value must be non-empty string";
  }
  if (value.length > 4096) return "value exceeds 4KB SSM limit";
  const meta = SSM_PARAMS[key];
  if (meta.secure) {
    // sk- 前缀 OR 长度 >= 20 且只含可打印 ascii
    const okShape = value.startsWith("sk-") || value.length >= 20;
    if (!okShape) return "secret must start with sk- or be >= 20 chars";
    if (!/^[\x20-\x7e]+$/.test(value)) return "secret must be printable ASCII";
  }
  return null;
}

async function putSystemDefault(key, value) {
  const meta = SSM_PARAMS[key];
  const client = await getSsmClient();
  const { PutParameterCommand } = await ssmMod();
  const cmd = new PutParameterCommand({
    Name: meta.name,
    Value: value,
    Type: meta.secure ? "SecureString" : "String",
    Overwrite: true,
    // litellm-shared-vkey 现有加密方式一致,真机实测)。原来硬编码 alias/clawpool-general
    // 是不存在的 alias(真机 NotFoundException)→ 写 secure 默认值必 KMS 失败。BFF role
    // 的 kms 权限用 ViaService 限定 ssm,与此一致。
  });
  await client.send(cmd);
}

// jsonResp 帮手:BFF 现有 response 都是 raw string body,这里也保持一致格式。
function jsonResp(status, obj) {
  return {
    statusCode: status,
    statusDescription: statusDesc(status),
    isBase64Encoded: false,
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(obj),
  };
}

async function handleSystemDefaults(event) {
  const method = event.httpMethod || "GET";
  if (method === "GET") {
    const values = await getSystemDefaults();
    return jsonResp(200, { values });
  }
  if (method === "POST") {
    let body;
    try {
      const raw = event.isBase64Encoded
        ? Buffer.from(event.body || "", "base64").toString("utf-8")
        : event.body || "{}";
      body = JSON.parse(raw);
    } catch {
      return jsonResp(400, { error: "invalid JSON body" });
    }
    if (!body || typeof body !== "object") {
      return jsonResp(400, { error: "body must be JSON object of {key: value}" });
    }
    const errors = {};
    for (const [k, v] of Object.entries(body)) {
      const e = validateSystemDefault(k, v);
      if (e) errors[k] = e;
    }
    if (Object.keys(errors).length > 0) {
      return jsonResp(400, { error: "validation failed", details: errors });
    }
    // 校验全过再写,避免部分成功
    for (const [k, v] of Object.entries(body)) {
      await putSystemDefault(k, v);
    }
    return jsonResp(200, { updated: Object.keys(body) });
  }
  return jsonResp(405, { error: "method not allowed" });
}

export async function handler(event) {
  const startMs = Date.now();
  let path = event.path || "/";

  // /console 前缀 strip: /console/x → /x, /console → /
  if (path === "/console" || path.startsWith("/console/")) {
    path = stripConsolePrefix(path);
  }

  const identity = parseOidcIdentity(event.headers);

  try {
    if (path === "/healthz") return textResp(200, "ok");
    if (path === "/capi/logout") {
      const resp = handleLogout();
      emitAudit(event, identity, resp, startMs);
      return resp;
    }
    if (path === "/capi/obs-config") {
      const resp = await handleObsConfig(event);
      emitAudit(event, identity, resp, startMs);
      return resp;
    }
    // R15.2:SSM 默认值 console 可见+可下发,BFF 本地处理不透传控制面
    // (SSM 不属于控制面 API Gateway 的资源;这批参数是平台底座级设置)。
    if (path === "/capi/system/defaults") {
      const resp = await handleSystemDefaults(event);
      emitAudit(event, identity, resp, startMs);
      return resp;
    }
    if (path === API_PREFIX || path.startsWith(API_PREFIX + "/")) {
      const subPath = path.slice(API_PREFIX.length) || "/";
      // not proxied to the control-plane API GW. xray client loads lazily so
      // unit tests can inject a fake via handler.__setXray().
      if (subPath === "/traces" || subPath.startsWith("/traces/")) {
        const xray = await loadXray();
        const resp = (await routeTraces(subPath, event, xray)) || textResp(404, "not found");
        emitAudit(event, identity, resp, startMs);
        return resp;
      }
      // (AOS)log viewer. Adapters load lazily; tests inject via __setLogDeps().
      if (subPath === "/logs") {
        const deps = await loadLogDeps();
        const resp = (await routeLogs(subPath, event, deps)) || textResp(404, "not found");
        emitAudit(event, identity, resp, startMs);
        return resp;
      }
      const resp = await proxyControlPlane(event, subPath);
      emitAudit(event, identity, resp, startMs);
      return resp;
    }
    return await serveStatic(path, event.headers);
  } catch (e) {
    // fail-loud:不静默吞,回 502 带简短原因(不泄 key)。
    const resp = textResp(502, `bff error: ${String(e?.message || e).slice(0, 200)}`);
    emitAudit(event, identity, resp, startMs);
    return resp;
  }
}
