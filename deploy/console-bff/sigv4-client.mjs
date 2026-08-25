// sigv4-client.mjs — 用 execute-api SigV4(AWS_IAM)签名调用控制面 API(#572)。
// 场景:控制面 API Gateway 开启 IAM 鉴权后,BFF 用自身 execution role 的临时凭据对
// 请求做 SigV4 签名,无需再持 x-api-key。由 handler.mjs 的 CTRL_API_AUTH_MODE=iam 触发。
//
// 依赖只用 Lambda Node 20 runtime 内建的 @aws-sdk 包(与 handler.mjs 其它 SDK 同策略,
// 不打包)。真机验证(2026-08-23, nodejs20.x, v20.20.2):@aws-sdk/signature-v4 与
// @aws-sdk/credential-provider-node 可直接 import,而 @aws-crypto/sha256-js /
// @smithy/protocol-http 不在 runtime(ERR_MODULE_NOT_FOUND),故用 node:crypto 实现
// SignatureV4 需要的 sha256 HashConstructor、用普通对象当 request(不引 HttpRequest 类)。
import { SignatureV4 } from "@aws-sdk/signature-v4";
import { defaultProvider } from "@aws-sdk/credential-provider-node";
import { createHash, createHmac } from "node:crypto";

// SignatureV4 需要的 HashConstructor:new sha256(secret?) → { update(data), digest() }。
// digest 返回 Uint8Array(node Buffer 即是);SignatureV4 会 await 它。
class NodeSha256 {
  constructor(secret) {
    this.hash = secret ? createHmac("sha256", secret) : createHash("sha256");
  }
  update(data) {
    this.hash.update(data);
  }
  digest() {
    return Promise.resolve(this.hash.digest());
  }
}

let _signer = null;
function getSigner(region) {
  if (!_signer) {
    _signer = new SignatureV4({
      service: "execute-api",
      region,
      credentials: defaultProvider(),
      sha256: NodeSha256,
    });
  }
  return _signer;
}

// 对一个 execute-api 请求做 SigV4 签名,返回可直接喂 fetch 的 headers
// (含 authorization / x-amz-date / x-amz-security-token 及传入的 content-type)。
// region 从 host {id}.execute-api.{region}.amazonaws.com 取,与目标端点严格一致。
export async function signRequest({ url, method, body, headers }) {
  const u = new URL(url);
  const parts = u.hostname.split(".");
  const region = parts[1] === "execute-api" ? parts[2] : process.env.AWS_REGION;
  const query = {};
  // NOTE(#572):handler 用 URLSearchParams 建 wire query(空格→'+'),而 SigV4 canonical
  // 用 '%20'。控制面现有参数(ISO8601 时间 / tenant·host id / If-Match / Idempotency-Key)
  // 均无空格,两者一致。若未来 forward 的 query 值含空格,需统一 '+'→'%20' 再签,否则
  // execute-api 会因 canonical query 不匹配返回 403(仅 CTRL_API_AUTH_MODE=iam 时相关)。
  for (const [k, v] of u.searchParams) query[k] = v;
  const signed = await getSigner(region).sign({
    method,
    protocol: u.protocol,
    hostname: u.hostname,
    path: u.pathname,
    query,
    headers: { ...(headers || {}), host: u.hostname },
    body,
  });
  const out = { ...signed.headers };
  // undici(Node fetch)按 URL 自设 Host 且禁止手动覆盖;签名用的 host 与之相同,删之安全。
  delete out.host;
  return out;
}
