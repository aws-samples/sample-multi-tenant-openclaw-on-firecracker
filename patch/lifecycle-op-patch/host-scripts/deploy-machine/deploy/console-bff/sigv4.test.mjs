// node --test sigv4.test.mjs
// #572 — 控制面调用授权模式:默认 apikey(x-api-key),CTRL_API_AUTH_MODE=iam 走 SigV4。
// 用注入的 fake signer + mock global fetch 断言两模式的 outbound header,不装真 SDK。
// (sigv4-client.mjs 的真实签名已在真机 nodejs20.x Lambda 验证,2026-08-23。)

import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

process.env.CTRL_API_BASE = "https://ctrlapi.execute-api.us-east-1.amazonaws.com/v1";
process.env.CTRL_API_KEY = "test-key-xxx";
process.env.AWS_REGION = "us-east-1";

const { handler, __setSigner } = await import("./handler.mjs");

function fakeFetchCapture() {
  const calls = [];
  const orig = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return { status: 200, headers: { get: () => "application/json" }, text: async () => "[]" };
  };
  return { calls, restore: () => { globalThis.fetch = orig; } };
}

const ev = (over = {}) => ({
  path: "/capi/tenants",
  httpMethod: "GET",
  headers: {},
  queryStringParameters: null,
  ...over,
});

describe("proxyControlPlane auth mode (#572)", () => {
  let f;
  beforeEach(() => { f = fakeFetchCapture(); });
  afterEach(() => { f.restore(); delete process.env.CTRL_API_AUTH_MODE; __setSigner(null); });

  it("apikey mode (default): forwards x-api-key, no Authorization", async () => {
    const resp = await handler(ev());
    assert.equal(resp.statusCode, 200);
    assert.equal(f.calls.length, 1);
    const h = f.calls[0].opts.headers;
    assert.equal(h["x-api-key"], "test-key-xxx");
    assert.equal(h.authorization, undefined);
    assert.equal(h.Authorization, undefined);
  });

  it("iam mode: signs with SigV4, drops x-api-key", async () => {
    process.env.CTRL_API_AUTH_MODE = "iam";
    let signArg = null;
    __setSigner(async (a) => {
      signArg = a;
      return {
        authorization:
          "AWS4-HMAC-SHA256 Credential=AKIA.../20260823/us-east-1/execute-api/aws4_request",
        "x-amz-date": "20260823T000000Z",
        "x-amz-security-token": "FAKE-SESSION-TOKEN",
        "content-type": "application/json",
      };
    });
    const resp = await handler(ev());
    assert.equal(resp.statusCode, 200);
    const h = f.calls[0].opts.headers;
    assert.ok(String(h.authorization).startsWith("AWS4-HMAC-SHA256"));
    assert.equal(h["x-amz-security-token"], "FAKE-SESSION-TOKEN");
    assert.equal(h["x-api-key"], undefined);
    // signer 收到目标 execute-api URL(含 /v1/tenants)、method、content-type。
    assert.match(signArg.url, /ctrlapi\.execute-api\.us-east-1\.amazonaws\.com\/v1\/tenants/);
    assert.equal(signArg.method, "GET");
    assert.equal(signArg.headers["content-type"], "application/json");
  });

  it("iam mode is case-insensitive (IAM)", async () => {
    process.env.CTRL_API_AUTH_MODE = "IAM";
    __setSigner(async () => ({
      authorization: "AWS4-HMAC-SHA256 x",
      "content-type": "application/json",
    }));
    const resp = await handler(ev());
    assert.equal(resp.statusCode, 200);
    const h = f.calls[0].opts.headers;
    assert.ok(String(h.authorization).startsWith("AWS4-HMAC-SHA256"));
    assert.equal(h["x-api-key"], undefined);
  });

  it("iam mode forwards POST body to signer for payload signing", async () => {
    process.env.CTRL_API_AUTH_MODE = "iam";
    let signArg = null;
    __setSigner(async (a) => {
      signArg = a;
      return { authorization: "AWS4-HMAC-SHA256 x", "content-type": "application/json" };
    });
    await handler(ev({ httpMethod: "POST", body: '{"name":"t1"}' }));
    assert.equal(signArg.method, "POST");
    assert.equal(signArg.body, '{"name":"t1"}');
  });
});
