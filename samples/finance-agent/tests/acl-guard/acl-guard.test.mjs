// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// acl-guard 单测(L1 内部零件) — 补 F-GUARD-22/23/24 的零覆盖(盘点确认 acl-guard 整块无测试)。
// 被测: samples/finance-agent/security/acl-guard/index.js —— priority 1000 的 before_tool_call
// default-deny 层,9 条 exfil 规则在代码里硬拦(绕过 prompt 自律)。
//
// 用 node --test。openclaw/plugin-sdk 走 samples/finance-agent/node_modules 的 test-only stub。
// 覆盖三面: happy(合法命令放行)/ adversarial(9 类 exfil 必 block + 绕过尝试)/ contract(block 结构 + harvest 字段)。

import { test } from "node:test";
import assert from "node:assert/strict";
import plugin from "../../security/acl-guard/index.js";

// 捕获 register 的 before_tool_call 钩子(同 sentinel harness 模式)
const hooks = {};
plugin.register({ on: (name, fn) => (hooks[name] = fn) });

async function call(toolName, params) {
  return await hooks.before_tool_call(
    { toolName, params, ctx: { agentId: "t-test", sessionKey: "s-1" } },
  );
}
const isBlock = (r) => !!(r && r.block === true);

// ── adversarial: 9 类 exfil 每类至少一个必 block ──────────────────────
test("adversarial: env-dump 被拒", async () => {
  assert.ok(isBlock(await call("exec", { command: "env" })));
  assert.ok(isBlock(await call("exec", { command: "printenv | grep -i key" })));
});
test("adversarial: 读 .env 被拒", async () => {
  assert.ok(isBlock(await call("read", { path: "/home/agent/.env" })));
  assert.ok(isBlock(await call("read", { path: "/app/.env.production" })));
});
test("adversarial: /proc/<pid>/environ 被拒", async () => {
  assert.ok(isBlock(await call("exec", { command: "cat /proc/self/environ" })));
  assert.ok(isBlock(await call("read", { path: "/proc/1/environ" })));
});
test("adversarial: ~/.aws 凭据被拒", async () => {
  assert.ok(isBlock(await call("read", { path: "/root/.aws/credentials" })));
  assert.ok(isBlock(await call("exec", { command: "cat ~/.aws/config" })));
});
test("adversarial: .ssh 私钥 command-agnostic 被拒(awk/dd/base64 都拦)", async () => {
  // acl-guard 匹配路径而非命令白名单,闭 sentinel 只覆盖 cat/cp 的绕过
  assert.ok(isBlock(await call("exec", { command: "awk '{print}' ~/.ssh/id_rsa" })));
  assert.ok(isBlock(await call("exec", { command: "base64 /home/x/.ssh/id_ed25519" })));
  assert.ok(isBlock(await call("read", { path: "/home/agent/.ssh/authorized_keys" })));
});
test("adversarial: AWS 凭据环境变量名被拒", async () => {
  assert.ok(isBlock(await call("exec", { command: "echo $AWS_SECRET_ACCESS_KEY" })));
  assert.ok(isBlock(await call("exec", { command: "printf %s $AWS_SESSION_TOKEN" })));
});
test("adversarial: IMDS link-local IP 被拒", async () => {
  assert.ok(isBlock(await call("exec", { command: "curl http://169.254.169.254/latest/" })));
  assert.ok(isBlock(await call("net", { url: "http://169.254.169.253/" })));
});
test("adversarial: IMDS 凭据路径被拒", async () => {
  assert.ok(isBlock(await call("net", { url: "http://x/iam/security-credentials/role" })));
  assert.ok(isBlock(await call("exec", { command: "curl .../instance-identity/document" })));
});
test("adversarial: secret 关键词扫描(仅 command 字段)被拒", async () => {
  assert.ok(isBlock(await call("exec", { command: "grep -r API_KEY /app" })));
  assert.ok(isBlock(await call("exec", { command: "find / -name '*password*'" })));
});

// ── happy: 合法工具调用放行(不误伤)────────────────────────────────
test("happy: 正常命令/路径放行(返回 undefined 不 block)", async () => {
  assert.ok(!isBlock(await call("exec", { command: "ls -la /home/agent/workspace" })));
  assert.ok(!isBlock(await call("read", { path: "/home/agent/data/report.md" })));
  assert.ok(!isBlock(await call("net", { url: "https://api.exchange.example/v5/ticker" })));
});
test("happy: 无可检字段的调用放行(harvest 空 -> allow)", async () => {
  assert.ok(!isBlock(await call("noop", {})));
  assert.ok(!isBlock(await call("noop", null)));
});
test("happy: secret 关键词只扫 command,不误伤 url/path 里的 token", async () => {
  // secret-grep 规则 fieldsOnly:['command'];token 出现在 url 不该 block
  assert.ok(!isBlock(await call("net", { url: "https://x/callback?token=abc" })));
});

// ── contract: block 结构 + harvest 覆盖多字段 ──────────────────────
test("contract: block 返回 {block:true, blockReason} 且点明 code-enforced", async () => {
  const r = await call("exec", { command: "env" });
  assert.equal(r.block, true);
  assert.match(r.blockReason, /ACL Guard denied/);
  assert.match(r.blockReason, /code/i); // 强调代码强制而非策略文本
});
test("contract: harvest 覆盖 args 数组(join 后匹配)", async () => {
  // args 为数组时 map(String).join(' ') 后匹配
  assert.ok(isBlock(await call("exec", { args: ["cat", "/home/a/.ssh/id_rsa"] })));
});
test("contract: harvest 覆盖 cmd/filePath/target/query 多字段", async () => {
  assert.ok(isBlock(await call("exec", { cmd: "printenv" })));
  assert.ok(isBlock(await call("read", { filePath: "/x/.env" })));
  assert.ok(isBlock(await call("read", { target: "/root/.aws/credentials" })));
});
