// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// sentinel-guard 行为异常监控(60s 滑窗)单测(L1 驱动) — 补 F-GUARD-19 零覆盖。
// after_tool_call 记录事件到滑窗,检测: rapid-exec(>30 exec/60s)、read-then-exfil(读凭据后
// 网络调用)、credential-scan(≥3 凭据路径读/60s)。anomaly 写 audit 日志(监控非阻断)。
// 测法: 设 OPENCLAW_SENTINEL_LOG 到临时文件,连续驱动 after_tool_call(测试<1s 全在窗口内),
// 读日志断言 anomaly 落痕。参照 sentinel-guard/index.js:1164-1235。

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync, rmSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// 每个测试独立日志文件(在 import plugin 前设 env,plugin 读 LOG_PATH 时生效)
const LOG = join(mkdtempSync(join(tmpdir(), "sentinel-")), "audit.log");
process.env.OPENCLAW_SENTINEL_LOG = LOG;

const plugin = (await import("../../security/sentinel-guard/index.js")).default;
const hooks = {};
plugin.register({ on: (name, fn) => (hooks[name] = fn) });
const after = (toolName, params, result) =>
  hooks.after_tool_call({ toolName, params, result }, { agentId: "t-test" });

function logHas(pattern) {
  if (!existsSync(LOG)) return false;
  return readFileSync(LOG, "utf8").split("\n").some((l) => {
    if (!l.trim()) return false;
    try { const e = JSON.parse(l); return e.rule === pattern || e.decision === "anomaly" && e.rule === pattern; }
    catch { return l.includes(pattern); }
  });
}

test("F-GUARD-19: rapid-exec — >30 exec/60s 触发行为异常告警", async () => {
  for (let i = 0; i < 35; i++) await after("exec", { command: `echo ${i}` });
  assert.ok(logHas("rapid-exec"), "35 次 exec 应触发 rapid-exec anomaly(阈值30)");
});

test("F-GUARD-19: credential-scan — ≥3 凭据路径读/60s 触发告警", async () => {
  // 独立日志(避免与上个测试的窗口叠加)
  const LOG2 = join(mkdtempSync(join(tmpdir(), "sentinel2-")), "a.log");
  process.env.OPENCLAW_SENTINEL_LOG = LOG2;
  const p2 = (await import("../../security/sentinel-guard/index.js?v=2")).default;
  const h2 = {};
  p2.register({ on: (n, f) => (h2[n] = f) });
  for (const path of ["/home/a/.aws/credentials", "/home/a/.ssh/id_rsa", "/home/a/.env"]) {
    await h2.after_tool_call({ toolName: "read", params: { path } }, { agentId: "t" });
  }
  const txt = existsSync(LOG2) ? readFileSync(LOG2, "utf8") : "";
  assert.ok(/credential-scan|read-then-exfil|anomaly/.test(txt),
    "3 次凭据路径读应触发 credential-scan/anomaly");
});

test("happy: 少量正常 exec 不触发 rapid-exec", async () => {
  const LOG3 = join(mkdtempSync(join(tmpdir(), "sentinel3-")), "a.log");
  process.env.OPENCLAW_SENTINEL_LOG = LOG3;
  const p3 = (await import("../../security/sentinel-guard/index.js?v=3")).default;
  const h3 = {};
  p3.register({ on: (n, f) => (h3[n] = f) });
  for (let i = 0; i < 5; i++) await h3.after_tool_call({ toolName: "exec", params: { command: "ls" } }, { agentId: "t" });
  const txt = existsSync(LOG3) ? readFileSync(LOG3, "utf8") : "";
  assert.ok(!/rapid-exec/.test(txt), "5 次 exec 不该触发 rapid-exec(阈值30)");
});

test("cleanup", () => {
  try { rmSync(join(LOG, ".."), { recursive: true, force: true }); } catch {}
});
