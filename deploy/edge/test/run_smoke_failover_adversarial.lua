-- deploy/edge/test/run_smoke_failover.lua
--
-- Standalone smoke check for R6.3 balancer failover (no busted needed).
-- Runs under plain `lua deploy/edge/test/run_smoke_failover.lua`.
-- Emits `OK`/`FAIL <n>` and exits 0/1 so CI + local dev can gate on it
-- when busted isn't installed. busted-based `balancer_failover_spec.lua`
-- covers the same properties in CI's edge job.

-- Resolve repo root from this script's own path (works no matter cwd).
local self_src = debug.getinfo(1, "S").source:sub(2)
local repo_root = self_src:gsub("/deploy/edge/test/run_smoke_failover%.lua$", "")
if repo_root == self_src then
    repo_root = "."  -- fallback if pattern didn't match
end
package.path = repo_root .. "/deploy/edge/test/?.lua;"
    .. repo_root .. "/?.lua;"
    .. repo_root .. "/deploy/?.lua;"
    .. repo_root .. "/deploy/edge/?.lua;"
    .. repo_root .. "/deploy/edge/lib/?.lua;"
    .. package.path
local helper = require "spec_helper"
local backend = require "edge.lib.backend"
local balancer = require "edge.lib.balancer"
local redis_client = require "edge.lib.redis_client"

local fails = 0
local function check(name, ok, msg)
    if ok then
        print("  ok  " .. name)
    else
        print("  FAIL " .. name .. ": " .. tostring(msg))
        fails = fails + 1
    end
end

-- ── first call: set_more_tries(1) + no invalidate ────────────────────
helper.reset_ngx()
backend.init_worker()
ngx.var.edge_self_ip = "10.0.0.1"
ngx.var.edge_redis_host = "redis.local"
ngx.var.edge_redis_port = "6379"
ngx.ctx.tenant_id = "t-1"
ngx.ctx.route_desc = { host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6" }
ngx.shared.route_cache:set("r:t-1",
    '{"host":"10.0.9.9","port":10042,"guest_ip":"172.16.0.6"}', 60)
balancer.balancer_pick()
check("first-call sets more_tries=1",
    package.loaded["ngx.balancer"]._more_tries == 1)
check("first-call does NOT clear L2 cache",
    ngx.shared.route_cache:get("r:t-1") ~= nil)

-- ── retry tick: invalidate + Redis re-query + new peer ────────────────
helper.reset_ngx()
backend.init_worker()
ngx.var.edge_self_ip = "10.0.0.1"
ngx.var.edge_redis_host = "redis.local"
ngx.var.edge_redis_port = "6379"
ngx.ctx.tenant_id = "t-mig"
ngx.ctx.route_desc = { host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6" }
ngx.shared.route_cache:set("r:t-mig",
    '{"host":"10.0.9.9","port":10042,"guest_ip":"172.16.0.6"}', 60)
ngx._fake_redis = {
    mode = "hit",
    value = '{"host":"10.0.7.7","port":11001,"guest_ip":"172.16.9.10","updated_at":123}',
}
redis_client._set_redis_module(helper.new_fake_redis_module())
backend._set_lock_module(helper.new_fake_lock_module())
package.loaded["ngx.balancer"]._last_failure = { state = "failed", code = 502 }
balancer.balancer_pick()
local last = package.loaded["ngx.balancer"]._last_peer
check("retry set_current_peer host=target",
    last and last.host == "10.0.7.7")
check("retry set_current_peer port=target",
    last and last.port == 11001)
check("retry re-filled L2 cache with new desc",
    (ngx.shared.route_cache:get("r:t-mig") or ""):match("10.0.7.7") ~= nil)

-- ── retry tick with Redis error: keep old desc, don't crash ───────────
helper.reset_ngx()
backend.init_worker()
ngx.var.edge_self_ip = "10.0.0.1"
ngx.var.edge_redis_host = "redis.local"
ngx.var.edge_redis_port = "6379"
ngx.ctx.tenant_id = "t-bad"
ngx.ctx.route_desc = { host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6" }
ngx._fake_redis = { mode = "error", err = "connection refused" }
redis_client._set_redis_module(helper.new_fake_redis_module())
backend._set_lock_module(helper.new_fake_lock_module())
package.loaded["ngx.balancer"]._last_failure = { state = "failed", code = 502 }
local ok, err = pcall(balancer.balancer_pick)
check("Redis error retry does not crash", ok, err)
last = package.loaded["ngx.balancer"]._last_peer
check("Redis error retry keeps old desc",
    last and last.host == "10.0.9.9")

if fails > 0 then
    print("FAIL " .. fails)
    os.exit(1)
end
print("OK")
