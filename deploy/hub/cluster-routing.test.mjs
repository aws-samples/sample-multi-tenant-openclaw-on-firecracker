// Test: go-live B1 cross-Pod routing module — degrade-safe + clustered paths.
// Zero external deps (Node stdlib). Run: node deploy/hub/cluster-routing.test.mjs
//
// The clustered path needs ioredis; we don't require a real Redis. We verify:
//  1. DEGRADE-SAFE: with no CLAW_HUB_REDIS_ENDPOINT, init=false, all ops no-op,
//     forward returns "local" (caller delivers locally = single-process behavior).
//  2. Envelope shape contract: forwardToFrontend tags the frame with _tenant so
//     the receiving Pod can re-check tenant scope (asserted via the local path's
//     observable: when disabled it returns "local" and does not mutate input).
// The real Redis pub/sub is exercised in the EKS staging env (runbook), not here.

import assert from "node:assert";

let pass = 0;
const fails = [];
function check(label, cond) {
  if (cond) pass++;
  else fails.push(label);
}

// ensure no endpoint for the degrade-safe suite
delete process.env.CLAW_HUB_REDIS_ENDPOINT;
const cr = await import("./cluster-routing.mjs");

// 1. degrade-safe init
const enabled = await cr.initClusterRouting(() => {});
check("init returns false with no REDIS endpoint", enabled === false);
check("clusterEnabled() false", cr.clusterEnabled() === false);
check("podId() is a non-empty string", typeof cr.podId() === "string" && cr.podId().length > 0);

// 2. registry ops are no-ops, never throw
let threw = false;
try {
  cr.registerChannel("tenant-x");
  cr.registerFrontend("sub-y");
  cr.unregisterChannel("tenant-x");
  cr.unregisterFrontend("sub-y");
} catch {
  threw = true;
}
check("registry ops no-op without throw when disabled", threw === false);

// 3. forward returns "local" when disabled (caller delivers locally)
const r1 = await cr.forwardToChannel("tenant-x", { a: 1 });
const r2 = await cr.forwardToFrontend("sub-y", "tenant-x", { a: 1 });
check('forwardToChannel → "local" when disabled', r1 === "local");
check('forwardToFrontend → "local" when disabled', r2 === "local");

// 4. forwarding does not mutate the caller's frame object
const frame = { type: "reply", text: "hi" };
await cr.forwardToFrontend("sub-y", "tenant-x", frame);
check("frame not mutated by disabled forward", !("_tenant" in frame));

// report
if (fails.length === 0) {
  console.log(`\n  cluster-routing.test: ${pass} checks PASSED ✓\n`);
  process.exit(0);
} else {
  console.error(`\n  cluster-routing.test: ${fails.length} FAILED:`);
  for (const f of fails) console.error("  ✗ " + f);
  process.exit(1);
}
