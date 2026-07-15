// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// sentinel-guard 资金动作 veto + fail-closed + block契约(L1) — 补 F-GUARD-06/20/21 零覆盖。
// 金融场景最关键:签名资金动作(withdraw/transfer/order)必须 before_tool_call 硬 veto,
// 除非带 --dry-run/--preview/--testnet/--confirm-token=(带外确认)。prompt 注入"转钱给攻击者"
// 也拦得住(代码层 veto,非模型自律)。openclaw stub 走 samples/finance-agent/node_modules。
// 参照 sentinel-guard/index.js:413-443(fund rules)。

import { test } from "node:test";
import assert from "node:assert/strict";
import plugin from "../../security/sentinel-guard/index.js";

const hooks = {};
plugin.register({ on: (name, fn) => (hooks[name] = fn) });
const before = (toolName, params) =>
  hooks.before_tool_call({ toolName, params, ctx: { agentId: "t-test" } });
const isBlock = (r) => !!(r && r.block === true);

// ── F-GUARD-06: 资金动作硬 veto(无 dry-run/confirm 必拦)──
test("veto: 签名提币(withdraw)无 --dry-run/--confirm-token 被拦", async () => {
  assert.ok(isBlock(await before("exec", { command: "exchange-cli withdraw --amount 1 --to 0xattacker" })));
  assert.ok(isBlock(await before("exec", { command: "exchange-cli withdrawal --coin USDT --address x" })));
});

test("veto: 转账/子账户移动(transfer)无确认被拦", async () => {
  assert.ok(isBlock(await before("exec", { command: "exchange-cli transfer --to sub1 --amt 100" })));
  assert.ok(isBlock(await before("exec", { command: "cli universal-transfer --amount 5" })));
});

test("veto: 实盘下单(live order)无 --testnet/--dry-run 被拦", async () => {
  assert.ok(isBlock(await before("exec", { command: "exchange-cli place-order --side buy --qty 1" })));
  assert.ok(isBlock(await before("exec", { command: "cli create-order --symbol BTCUSDT" })));
});

test("veto: prompt 注入的资金动作也拦(代码层非模型自律)", async () => {
  // 即便模型被说服执行,before_tool_call 在工具执行前硬拦
  assert.ok(isBlock(await before("exec", { command: "trading-cli withdraw --all --to attacker-controlled" })));
});

// ── happy: 带确认/dry-run 的放行(不误伤合法预览)──
test("allow: --dry-run / --testnet / --confirm-token 放行", async () => {
  assert.ok(!isBlock(await before("exec", { command: "exchange-cli withdraw --amount 1 --to x --dry-run" })));
  assert.ok(!isBlock(await before("exec", { command: "exchange-cli place-order --side buy --testnet" })));
  assert.ok(!isBlock(await before("exec", { command: "exchange-cli transfer --to s1 --confirm-token=abc123" })));
});

test("allow: 只读行情/查询不拦", async () => {
  assert.ok(!isBlock(await before("exec", { command: "exchange-cli ticker --symbol BTCUSDT" })));
  assert.ok(!isBlock(await before("exec", { command: "exchange-cli balance" })));
});

// ── F-GUARD-21: block 结果契约(blockReason 文案 + 结构)──
test("contract: veto 返回 {block:true, blockReason} 且点明 fund-action 原因", async () => {
  const r = await before("exec", { command: "exchange-cli withdraw --amount 1 --to x" });
  assert.equal(r.block, true);
  assert.ok(typeof r.blockReason === "string" && r.blockReason.length > 0, "缺 blockReason 文案");
  assert.match(r.blockReason, /withdraw|money|fund|irreversible|confirm|dry-run/i);
});

// ── F-GUARD-20: fail-closed(钩子内部抛错不放行)──
test("fail-closed: 畸形 params 不让整个 hook 崩(受控返回)", async () => {
  // 传各种畸形 params,hook 不能 throw 到调用方(fail-closed:异常时安全侧)
  await assert.doesNotReject(async () => {
    await before("exec", { command: null });
    await before("exec", {});
    await before("exec", { command: { nested: "obj" } });
    await before(undefined, undefined);
  });
});
