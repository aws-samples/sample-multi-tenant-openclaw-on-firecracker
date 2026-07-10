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

const __dirname = dirname(fileURLToPath(import.meta.url));
const WEB_DIR = join(__dirname, "web"); // 打包进来的 console 前端

const CTRL_BASE = process.env.CTRL_API_BASE; // https://xxx.execute-api.../v1
const CTRL_KEY = process.env.CTRL_API_KEY; // admin x-api-key(只在后端)
const API_PREFIX = "/capi"; // 前端调 /capi/tenants → 控制面 /tenants

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
function dynamicConfigJs() {
  return [
    `window.OC_DEFAULT_API_URL = "${API_PREFIX}/";`,
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
async function serveStatic(urlPath) {
  let rel = urlPath;
  if (rel === "/" || rel === "" || rel === "/index.html") {
    rel = "/index.html";
  }
  if (rel === "/config.js") {
    return textResp(200, dynamicConfigJs(), MIME[".js"]);
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
async function proxyControlPlane(event, subPath) {
  const qs = event.queryStringParameters || {};
  const query = new URLSearchParams(qs).toString();
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
    headers: { "x-api-key": CTRL_KEY, "content-type": "application/json" },
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

export async function handler(event) {
  const path = event.path || "/";
  try {
    if (path === "/healthz") return textResp(200, "ok");
    if (path === API_PREFIX || path.startsWith(API_PREFIX + "/")) {
      const subPath = path.slice(API_PREFIX.length) || "/";
      return await proxyControlPlane(event, subPath);
    }
    return await serveStatic(path);
  } catch (e) {
    // fail-loud:不静默吞,回 502 带简短原因(不泄 key)。
    return textResp(502, `bff error: ${String(e?.message || e).slice(0, 200)}`);
  }
}
