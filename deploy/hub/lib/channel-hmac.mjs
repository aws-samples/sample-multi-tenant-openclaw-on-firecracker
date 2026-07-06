// lib/channel-hmac.mjs — channel 注册 HMAC 验签 + #14 nonce 防重放(#136 拆分)。
// 单例状态唯一定义点(js-split 门3):_usedChannelSigs 只在本文件——拆出两份
// nonce store = 防重放脑裂,是安全洞,严禁复制。函数体逐字搬自 server.mjs。

import { createHmac, timingSafeEqual } from "node:crypto";
import { TS_WINDOW_SEC } from "./config.mjs";
import { getTenantSecret } from "./tenant-store.mjs";

// #14 channel 注册 HMAC 防重放:时间窗(±300s)只挡"迟到/超前"的请求,挡不住窗口内
// 重放——攻击者截获一条合法 {appId,ts,signature} 可在 300s 内重复提交劫持注册。加一个
// 一次性 nonce:每个签名(按 tenant:appId:ts:sig 唯一)只接受一次,窗口内再现即拒。
// 存活 TS_WINDOW_SEC 后自动过期回收(过了时间窗的旧签名本就会被时间窗挡,无需再记)。
// 说明:本地内存 store — 单 Pod 完整防重放;EKS 多 Pod 下同 Pod 防住,跨 Pod 完整防重放
// 需共享 store(Redis/ElastiCache,与连接路由同源),留作后续(注释标注,不假装已覆盖)。
const _usedChannelSigs = new Map(); // key = tenant:appId:ts:sig → expiresAtMs
function _channelSigSeenBefore(key, nowMs) {
  // 惰性清理:每次校验顺带回收已过期条目,避免 store 无界增长(无需定时器)。
  if (_usedChannelSigs.size > 0) {
    for (const [k, exp] of _usedChannelSigs) {
      if (exp <= nowMs) _usedChannelSigs.delete(k);
    }
  }
  if (_usedChannelSigs.has(key)) return true; // 重放
  _usedChannelSigs.set(key, nowMs + TS_WINDOW_SEC * 1000);
  return false;
}

// ---- channel registration HMAC ----
export async function verifyChannelSignature(tenantId, appId, ts, signature) {
  const secret = await getTenantSecret(tenantId);
  if (!secret || !appId || !ts || !signature) return false;
  const tsNum = Number(ts);
  if (!Number.isFinite(tsNum)) return false;
  if (Math.abs(Math.floor(Date.now() / 1000) - tsNum) > TS_WINDOW_SEC) return false;
  const expected = createHmac("sha256", Buffer.from(secret, "hex"))
    .update(`${appId}:${ts}`)
    .digest("hex");
  if (signature.length !== expected.length) return false;
  let sigOk = false;
  try {
    sigOk = timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
  } catch {
    return false;
  }
  if (!sigOk) return false;
  // #14 只在签名已验真后才记 nonce:① 防重放(窗口内同签名第二次即拒)② 避免用错误
  // 签名+随机 ts 撑爆 store(错签在上面就被拒,不占 nonce)。key 含 tenant 防跨租户串。
  // 已知毛刺(legacy HMAC 路径,将退役):签名输入 `{appId}:{ts}` 的 ts 是秒粒度,合法
  // channel 若在同一 wall-clock 秒内二次 fetchHubToken(如断连即重连撞同秒)会得到字节
  // 相同的签名、被 nonce 当重放拒一次;下一秒重试(ts+1)签名不同即自愈。非安全问题,
  // 主路径 Cognito access token 不受影响。彻底消除需客户端签名带 per-request 随机 nonce。
  const nowMs = Date.now();
  const nonceKey = `${tenantId}:${appId}:${ts}:${signature}`;
  if (_channelSigSeenBefore(nonceKey, nowMs)) return false; // 重放请求被拒
  return true;
}
