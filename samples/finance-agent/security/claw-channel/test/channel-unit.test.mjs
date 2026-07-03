// claw-channel unit tests — pure security/parse helpers, no runtime, no WS.
//
// Run:  node --test samples/finance-agent/security/claw-channel/test/
//
// These lock in the guarantees the channel leans on for tenant safety:
//   - generateSignature  : HMAC token signature is deterministic + key-bound
//   - sniffMime          : magic-byte MIME sniff (don't trust client extension)
//   - isIdentityFileName : T4 identity-file basename guard (SOUL.md etc.)
//   - classifyType       : image vs document split for outbound FILE parts
//   - resolveAccount     : per-VM hub creds resolution + fail-closed defaults
//   - stripEnvelope      : channel/timestamp envelope prefix removal for history
//
// No network, no AWS, no VM — pure logic. Fast enough to run on every change.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";

// fixture region:测 cognitoRegion 字段解析用,走 env 兜底(default)不写死裸字面量。
const TEST_REGION = process.env.AWS_REGION || "ap-southeast-1"; // default fixture region

import {
  generateSignature,
  sniffMime,
  classifyType,
  isIdentityFileName,
  resolveAccount,
  hasCognitoCreds,
  stripEnvelope,
  resolveSessionContext,
  IDENTITY_FILE_BASENAMES,
  MEDIA_ALLOWED_MIMES,
  MEDIA_EXT_TO_MIME,
} from "../index.js";

// ───────────────────────── generateSignature (token HMAC) ─────────────────────────

test("generateSignature: matches HMAC-SHA256({appId}:{ts}, hex-decoded secret)", () => {
  const appId = "tenant-abc";
  const secretHex = "00112233445566778899aabbccddeeff";
  const ts = 1_700_000_000;
  const got = generateSignature(appId, secretHex, ts);
  const want = createHmac("sha256", Buffer.from(secretHex, "hex"))
    .update(`${appId}:${ts}`)
    .digest("hex");
  assert.equal(got, want);
});

test("generateSignature: deterministic for same inputs", () => {
  const a = generateSignature("t", "deadbeef", 42);
  const b = generateSignature("t", "deadbeef", 42);
  assert.equal(a, b);
});

test("generateSignature: changes when any input changes (key/appId/ts bound)", () => {
  const base = generateSignature("t1", "deadbeef", 100);
  assert.notEqual(base, generateSignature("t2", "deadbeef", 100)); // appId
  assert.notEqual(base, generateSignature("t1", "deadbee0", 100)); // secret
  assert.notEqual(base, generateSignature("t1", "deadbeef", 101)); // timestamp
});

test("generateSignature: output is lowercase hex of length 64 (SHA-256)", () => {
  const sig = generateSignature("t", "abcd", 1);
  assert.match(sig, /^[0-9a-f]{64}$/);
});

// ───────────────────────── sniffMime (magic-byte, don't trust extension) ─────────────────────────

test("sniffMime: PNG magic bytes", () => {
  const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00]);
  assert.equal(sniffMime(png), "image/png");
});

test("sniffMime: JPEG magic bytes", () => {
  const jpg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]);
  assert.equal(sniffMime(jpg), "image/jpeg");
});

test("sniffMime: GIF87a / GIF89a", () => {
  assert.equal(sniffMime(Buffer.from("GIF87a....")), "image/gif");
  assert.equal(sniffMime(Buffer.from("GIF89a....")), "image/gif");
});

test("sniffMime: WEBP (RIFF....WEBP)", () => {
  const webp = Buffer.concat([
    Buffer.from("RIFF"),
    Buffer.from([0x00, 0x00, 0x00, 0x00]),
    Buffer.from("WEBP"),
  ]);
  assert.equal(sniffMime(webp), "image/webp");
});

test("sniffMime: PDF (%PDF)", () => {
  assert.equal(sniffMime(Buffer.from("%PDF-1.7\n")), "application/pdf");
});

test("sniffMime: unknown bytes return undefined (caller falls back to extension)", () => {
  assert.equal(sniffMime(Buffer.from([0x00, 0x01, 0x02, 0x03])), undefined);
  assert.equal(sniffMime(Buffer.from([])), undefined);
});

test("sniffMime: a renamed executable does NOT sniff as an allowed image", () => {
  // ELF header — an attacker renaming evil.bin → evil.png must not pass the
  // magic-byte sniff. It returns undefined, and the allowed-MIME gate (below)
  // is what ultimately rejects it.
  const elf = Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x02, 0x01, 0x01, 0x00]);
  assert.equal(sniffMime(elf), undefined);
});

// ───────────────────────── classifyType ─────────────────────────

test("classifyType: image/* → image, everything else → document", () => {
  assert.equal(classifyType("image/png"), "image");
  assert.equal(classifyType("image/jpeg"), "image");
  assert.equal(classifyType("application/pdf"), "document");
  assert.equal(classifyType("text/csv"), "document");
});

// ───────────────────────── isIdentityFileName (T4 guard) ─────────────────────────

test("isIdentityFileName: blocks protected identity files (case-insensitive)", () => {
  for (const name of ["SOUL.md", "soul.md", "AGENTS.md", "identity.md", "USER.md", "bootstrap.md", "HEARTBEAT.md", "tools.md", "memory.md"]) {
    assert.equal(isIdentityFileName(name), true, `should block ${name}`);
  }
});

test("isIdentityFileName: blocks identity file even with a directory prefix (basename match)", () => {
  assert.equal(isIdentityFileName("/home/agent/.openclaw/SOUL.md"), true);
  assert.equal(isIdentityFileName("../../persona/AGENTS.md"), true);
  assert.equal(isIdentityFileName("  identity.md  "), true); // trimmed
});

test("isIdentityFileName: allows ordinary media filenames", () => {
  for (const name of ["chart.png", "report.pdf", "data.csv", "soulmate.png", "agents-list.jpg"]) {
    assert.equal(isIdentityFileName(name), false, `should allow ${name}`);
  }
});

test("IDENTITY_FILE_BASENAMES: is the authoritative protected set", () => {
  assert.ok(IDENTITY_FILE_BASENAMES.has("soul.md"));
  assert.ok(IDENTITY_FILE_BASENAMES.has("agents.md"));
  assert.ok(!IDENTITY_FILE_BASENAMES.has("readme.md"));
});

// ───────────────────────── MEDIA allow-list (SSRF/exfil scope) ─────────────────────────

test("MEDIA_ALLOWED_MIMES: only the intended safe set", () => {
  for (const m of ["image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain", "text/csv"]) {
    assert.ok(MEDIA_ALLOWED_MIMES.has(m), `should allow ${m}`);
  }
  for (const m of ["application/x-sh", "application/octet-stream", "text/html", "image/svg+xml"]) {
    assert.ok(!MEDIA_ALLOWED_MIMES.has(m), `should reject ${m}`);
  }
});

test("MEDIA_EXT_TO_MIME: extension fallback maps only known-safe extensions", () => {
  assert.equal(MEDIA_EXT_TO_MIME.png, "image/png");
  assert.equal(MEDIA_EXT_TO_MIME.pdf, "application/pdf");
  assert.equal(MEDIA_EXT_TO_MIME.exe, undefined);
  assert.equal(MEDIA_EXT_TO_MIME.sh, undefined);
});

// ───────────────────────── resolveAccount (hub creds, fail-closed) ─────────────────────────

test("resolveAccount: reads channel config for the claw-channel id", () => {
  const cfg = {
    channels: {
      "claw-channel": {
        enabled: true,
        hubUrl: "https://hub.example",
        wsUrl: "wss://hub.example/ws",
        appId: "tenant-1",
        appSecret: "abcdef",
      },
    },
  };
  const acc = resolveAccount(cfg, "default");
  assert.equal(acc.enabled, true);
  assert.equal(acc.hubUrl, "https://hub.example");
  assert.equal(acc.wsUrl, "wss://hub.example/ws");
  assert.equal(acc.appId, "tenant-1");
  assert.equal(acc.appSecret, "abcdef");
});

test("resolveAccount: enabled is strictly boolean true (not truthy strings)", () => {
  const cfg = { channels: { "claw-channel": { enabled: "yes" } } };
  assert.equal(resolveAccount(cfg, "default").enabled, false);
});

test("resolveAccount: falls back to `secret` when `appSecret` absent", () => {
  const cfg = { channels: { "claw-channel": { secret: "fallbacksecret" } } };
  assert.equal(resolveAccount(cfg, "default").appSecret, "fallbacksecret");
});

test("resolveAccount: empty config → blank creds (channel start fails closed)", () => {
  const acc = resolveAccount({}, "default");
  assert.equal(acc.enabled, false);
  assert.equal(acc.hubUrl, "");
  assert.equal(acc.appSecret, "");
});

// ───────────────────────── WI-002: Cognito creds resolution + path selection ─────────────────────────

test("resolveAccount: reads injected Cognito machine-user creds", () => {
  const cfg = {
    channels: {
      "claw-channel": {
        enabled: true,
        hubUrl: "https://hub.example",
        wsUrl: "wss://hub.example/ws",
        appId: "tenant-1",
        cognitoRegion: TEST_REGION,
        cognitoClientId: "channel-client-xxxx",
        cognitoUsername: "tenant-1",
        cognitoPassword: "s3cret-per-tenant",
      },
    },
  };
  const acc = resolveAccount(cfg, "default");
  assert.equal(acc.cognitoRegion, TEST_REGION);
  assert.equal(acc.cognitoClientId, "channel-client-xxxx");
  assert.equal(acc.cognitoUsername, "tenant-1");
  assert.equal(acc.cognitoPassword, "s3cret-per-tenant");
});

test("hasCognitoCreds: true only when ALL four Cognito fields present", () => {
  const full = {
    cognitoRegion: TEST_REGION,
    cognitoClientId: "c",
    cognitoUsername: "u",
    cognitoPassword: "p",
  };
  assert.equal(hasCognitoCreds(full), true);
});

test("hasCognitoCreds: false when any Cognito field missing (→ falls back to HMAC)", () => {
  const base = {
    cognitoRegion: TEST_REGION,
    cognitoClientId: "c",
    cognitoUsername: "u",
    cognitoPassword: "p",
  };
  for (const k of ["cognitoRegion", "cognitoClientId", "cognitoUsername", "cognitoPassword"]) {
    assert.equal(hasCognitoCreds({ ...base, [k]: "" }), false, `missing ${k} → false`);
  }
});

test("hasCognitoCreds: false for legacy HMAC-only account (no Cognito fields)", () => {
  const acc = resolveAccount(
    { channels: { "claw-channel": { enabled: true, appId: "t", appSecret: "abc" } } },
    "default",
  );
  assert.equal(hasCognitoCreds(acc), false);
});

// ───────────────────────── stripEnvelope (history sanitize) ─────────────────────────

test("stripEnvelope: removes a channel-label header (label + trailing content in the bracket)", () => {
  // Real envelope shape is "[<Label> <...>] body" — the label is followed by
  // more text inside the bracket (a timestamp/peer), so the captured header
  // starts with "<Label> ". A bare "[WebChat]" with nothing after is NOT an
  // envelope (verified against _looksLikeEnvelopeHeader).
  assert.equal(stripEnvelope("[WebChat 2026-07-01T12:30Z] hello world"), "hello world");
  assert.equal(stripEnvelope("[Telegram 12345] hi"), "hi");
});

test("stripEnvelope: removes an ISO-timestamp header", () => {
  assert.equal(stripEnvelope("[2026-07-01T12:30Z] the message"), "the message");
  assert.equal(stripEnvelope("[2026-07-01 12:30] the message"), "the message");
});

test("stripEnvelope: leaves a bare bracket token untouched (not a known header)", () => {
  // A bracketed token that is NOT a known channel/timestamp header is preserved
  // verbatim — we must not eat real user content like "[TODO] buy milk".
  assert.equal(stripEnvelope("[TODO] buy milk"), "[TODO] buy milk");
  assert.equal(stripEnvelope("[WebChat] no trailing content"), "[WebChat] no trailing content");
  assert.equal(stripEnvelope("no brackets here"), "no brackets here");
});

// ───────────────────────── resolveSessionContext (T3 断言兜底 · #17) ─────────────────────────
// #17: claw-channel 会话隔离靠 cognitoSub(唯一维度键)+ dmScope="per-channel-peer"。
// 加显式断言兜底:调用方漏传/传空 sub,或 dmScope 被改坏,立即 fail-loud 抛错,
// 绝不静默构造一个会跨租户串台的会话上下文。下面用一个记录调用的 mock core 验证。

// 记录 resolveAgentRoute 收到的 peer.id,用来断言隔离维度键正确嵌入。
function makeMockCore(seen) {
  return {
    channel: {
      routing: {
        resolveAgentRoute: ({ peer, accountId }) => {
          if (seen) seen.push(peer?.id);
          return { agentId: `agent-${peer?.id}`, sessionKey: `sk-${peer?.id}`, accountId };
        },
      },
      session: {
        resolveStorePath: (_store, { agentId }) => `/store/${agentId}`,
      },
    },
  };
}
const MOCK_ACCOUNT = { accountId: "acct-1" };

test("resolveSessionContext: 漏传 cognitoSub → 抛错(不静默串台)", () => {
  assert.throws(
    () => resolveSessionContext({ core: makeMockCore(), cfg: {}, account: MOCK_ACCOUNT }),
    /cognitoSub must be a non-empty string/,
  );
});

test("resolveSessionContext: cognitoSub 为空串/空白 → 抛错", () => {
  for (const bad of ["", "   ", "\t"]) {
    assert.throws(
      () => resolveSessionContext({ core: makeMockCore(), cfg: {}, account: MOCK_ACCOUNT, cognitoSub: bad }),
      /cognitoSub must be a non-empty string/,
      `cognitoSub=${JSON.stringify(bad)} 应抛错`,
    );
  }
});

test("resolveSessionContext: cognitoSub 非字符串(number/null/object) → 抛错", () => {
  for (const bad of [123, null, {}, [], true]) {
    assert.throws(
      () => resolveSessionContext({ core: makeMockCore(), cfg: {}, account: MOCK_ACCOUNT, cognitoSub: bad }),
      /cognitoSub must be a non-empty string/,
      `cognitoSub=${JSON.stringify(bad)} 应抛错`,
    );
  }
});

test("resolveSessionContext: 合法 sub 无 threadId → peerId = sub(向后兼容单会话)", () => {
  const seen = [];
  const { route, isolatedCfg } = resolveSessionContext({
    core: makeMockCore(seen), cfg: { session: { store: "/s" } }, account: MOCK_ACCOUNT, cognitoSub: "sub-abc",
  });
  assert.equal(seen[0], "sub-abc", "无 threadId 时 peerId 应等于 sub");
  assert.equal(isolatedCfg.session.dmScope, "per-channel-peer");
  assert.equal(route.sessionKey, "sk-sub-abc");
});

test("resolveSessionContext: 合法 sub + threadId → peerId 用 :t: 分隔嵌入", () => {
  const seen = [];
  resolveSessionContext({
    core: makeMockCore(seen), cfg: {}, account: MOCK_ACCOUNT, cognitoSub: "sub-abc", threadId: "th1",
  });
  assert.equal(seen[0], "sub-abc:t:th1", "threadId 应用 :t: 分隔嵌入 peerId");
});

test("resolveSessionContext: 两个不同 sub → 两个不同 sessionKey(隔离不串台)", () => {
  const seen = [];
  const a = resolveSessionContext({ core: makeMockCore(seen), cfg: {}, account: MOCK_ACCOUNT, cognitoSub: "alice" });
  const b = resolveSessionContext({ core: makeMockCore(seen), cfg: {}, account: MOCK_ACCOUNT, cognitoSub: "bob" });
  assert.notEqual(a.route.sessionKey, b.route.sessionKey, "不同 sub 必须落不同 session");
});

test("resolveSessionContext: dmScope 始终被强制为 per-channel-peer(即便 cfg 传了别的)", () => {
  // 调用方在 cfg.session.dmScope 传一个会串台的值,resolveSessionContext 必须覆盖回
  // per-channel-peer(不信任传入),断言通过 = 隔离维度未被污染。
  const { isolatedCfg } = resolveSessionContext({
    core: makeMockCore(), cfg: { session: { dmScope: "shared-global" } }, account: MOCK_ACCOUNT, cognitoSub: "sub-x",
  });
  assert.equal(isolatedCfg.session.dmScope, "per-channel-peer", "传入的坏 dmScope 必须被覆盖");
});

test("resolveSessionContext: 不 mutate 原始 cfg(隔离配置只在副本上改)", () => {
  const cfg = { session: { store: "/s" }, other: "keep" };
  resolveSessionContext({ core: makeMockCore(), cfg, account: MOCK_ACCOUNT, cognitoSub: "sub-y" });
  assert.equal(cfg.session.dmScope, undefined, "原始 cfg.session 不应被写入 dmScope");
  assert.equal(cfg.other, "keep");
});
