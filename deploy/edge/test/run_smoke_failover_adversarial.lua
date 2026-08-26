-- deploy/edge/test/run_smoke_failover_adversarial.lua
--
-- Standalone smoke check for the R6.3② balancer failover path (no busted needed).
-- Runs under plain `lua deploy/edge/test/run_smoke_failover_adversarial.lua`.
-- Emits `OK`/`FAIL <n>` and exits 0/1 so CI + local dev can gate on it when
-- busted isn't installed. The busted specs
-- (`balancer_failover_adversarial_spec.lua`) cover the same properties in CI.
--
-- #633 —— 这个文件被重写过一次。原版断言的是 #606 之前的行为("重投时清 L2 缓存 +
-- 重查 Redis 拿新 desc"),那条路径已经因为 OpenResty 在 `balancer_by_lua*` 禁用
-- cosocket 而被删掉;它还因为自己路径正则写的是 `run_smoke_failover.lua`(文件名带
-- `_adversarial` 后缀)而永远取不到 repo_root、只能回落 `"."`,再加上 `.busted` 的
-- pattern 是 `_spec`、CI 跑的是裸 `busted`,所以这个文件从来没被执行过 —— 一个从不
-- 运行的测试正是它能一直漂移的原因。现在断言的是当前契约,并由 CI 显式调用。
--
-- 当前契约(#606 + #628):
--   · 首次进 balancer:set_more_tries(1),不动 L2 缓存。
--   · 重投 tick:**只读 shared_dict**。route_cache 里有不同 peer 就换过去;
--     没有就 mark_retry_stale + fail closed(不重用已知失败坐标),
--     并把想返的 503 存进 ngx.ctx.edge_retry_status 由 header_filter 落地。
--   · 重投 tick 绝不建 Redis cosocket —— 这一条在真实 OpenResty 阶段里由
--     deploy/edge/test/integration/balancer_phase_integration.sh 判别性验证;
--     这里用"假 redis 模块一次都没被调用"作为单测侧的等价断言。

-- Resolve repo root from this script's own path (works no matter cwd).
local self_src = debug.getinfo(1, "S").source:sub(2)
local repo_root = self_src:gsub("/deploy/edge/test/[^/]+%.lua$", "")
if repo_root == self_src or repo_root == "" then
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

local OLD_DESC = '{"host":"10.0.9.9","port":10042,"guest_ip":"172.16.0.6"}'

-- 计数型假 redis 模块:任何一次 new()/connect 都记账,用来断言重投 tick 不碰 cosocket。
local function counting_redis_module()
    local inner = helper.new_fake_redis_module()
    local calls = { n = 0 }
    return {
        new = function(...)
            calls.n = calls.n + 1
            return inner.new(...)
        end,
    }, calls
end

local function fresh_ngx(tid, desc_json)
    helper.reset_ngx()
    backend.init_worker()
    ngx.var.edge_self_ip = "10.0.0.1"
    ngx.var.edge_redis_host = "redis.local"
    ngx.var.edge_redis_port = "6379"
    ngx.ctx.tenant_id = tid
    ngx.ctx.route_desc = { host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6" }
    if desc_json then
        ngx.shared.route_cache:set("r:" .. tid, desc_json, 60)
        ngx.shared.route_cache:set("f:" .. tid, "1", 60)
    end
    backend._set_lock_module(helper.new_fake_lock_module())
end

-- ── 首次进 balancer:set_more_tries(1),不动 L2 ────────────────────────
fresh_ngx("t-1", OLD_DESC)
balancer.balancer_pick()
check("first-call sets more_tries=1",
    package.loaded["ngx.balancer"]._more_tries == 1)
check("first-call does NOT clear L2 cache",
    ngx.shared.route_cache:get("r:t-1") ~= nil)

-- ── 重投 tick:shared_dict 里有不同 peer → 换过去,且不碰 Redis ──────────
fresh_ngx("t-mig", '{"host":"10.0.7.7","port":11001,"guest_ip":"172.16.9.10"}')
local rmod, rcalls = counting_redis_module()
redis_client._set_redis_module(rmod)
package.loaded["ngx.balancer"]._last_failure = { state = "failed", code = 502 }
balancer.balancer_pick()
local last = package.loaded["ngx.balancer"]._last_peer
check("retry switches set_current_peer host to the cached different peer",
    last and last.host == "10.0.7.7", last and last.host)
check("retry switches set_current_peer port to the cached different peer",
    last and last.port == 11001, last and last.port)
check("retry does NOT open a Redis cosocket (balancer phase forbids it)",
    rcalls.n == 0, "redis new() calls=" .. tostring(rcalls.n))
check("retry marks route_source=l2 (came from shared_dict, not Redis)",
    ngx.ctx.route_source == backend.SOURCE_L2, ngx.ctx.route_source)

-- ── 重投 tick:没有不同 peer → fail closed 503,不重用旧坐标 ─────────────
fresh_ngx("t-same", OLD_DESC)  -- 缓存里就是那个已经失败的 peer
local rmod2, rcalls2 = counting_redis_module()
redis_client._set_redis_module(rmod2)
package.loaded["ngx.balancer"]._last_failure = { state = "failed", code = 502 }
local ok, err = pcall(balancer.balancer_pick)
check("no-different-peer retry exits instead of reusing the failed peer",
    (not ok) and type(err) == "table" and err.ngx_exit == true, err)
check("no-different-peer retry exit status is 503",
    type(err) == "table" and err.status == balancer._RETRY_EXHAUSTED_STATUS,
    type(err) == "table" and err.status)
check("no-different-peer retry stashes edge_retry_status for header_filter",
    ngx.ctx.edge_retry_status == balancer._RETRY_EXHAUSTED_STATUS,
    ngx.ctx.edge_retry_status)
check("no-different-peer retry does NOT set_current_peer to the failed peer",
    package.loaded["ngx.balancer"]._last_peer == nil,
    package.loaded["ngx.balancer"]._last_peer
        and package.loaded["ngx.balancer"]._last_peer.host)
check("no-different-peer retry does NOT open a Redis cosocket",
    rcalls2.n == 0, "redis new() calls=" .. tostring(rcalls2.n))
-- fail-static(#497):旧 blob 留着,只撤 fresh 标记 + 写一次性重投提示。
check("no-different-peer retry keeps the L2 blob for fail-static",
    ngx.shared.route_cache:get("r:t-same") ~= nil)
check("no-different-peer retry drops the fresh marker",
    ngx.shared.route_cache:get("f:t-same") == nil)
check("no-different-peer retry sets the one-time retry hint",
    ngx.shared.route_cache:get("p:t-same") ~= nil)

-- ── header_filter:把 nginx 强制的 500 改写回 503 ───────────────────────
-- balancer 阶段的 ngx.exit(503) 发给客户的是 500(openresty/lua-resty-core#70),
-- 所以状态码只能在输出过滤器里落地。
ngx.status = 500
balancer.fixup_status()
check("fixup_status rewrites 500 -> 503 when ctx carries the wanted code",
    ngx.status == balancer._RETRY_EXHAUSTED_STATUS, ngx.status)

ngx.ctx.edge_retry_status = nil
ngx.status = 500
balancer.fixup_status()
check("fixup_status leaves unrelated 500 alone (no ctx marker)",
    ngx.status == 500, ngx.status)

ngx.ctx.edge_retry_status = balancer._RETRY_EXHAUSTED_STATUS
ngx.status = 502
balancer.fixup_status()
check("fixup_status leaves a real upstream 502 alone",
    ngx.status == 502, ngx.status)

-- ── 重投提示是一次性的:消费一次就没了 ─────────────────────────────────
helper.reset_ngx()
backend.init_worker()
local shared = ngx.shared.route_cache
backend.mark_retry_stale(shared, "t-hint")
check("consume_retry_hint is true on first read",
    backend.consume_retry_hint(shared, "t-hint") == true)
check("consume_retry_hint is false on second read (one-shot)",
    backend.consume_retry_hint(shared, "t-hint") == false)

if fails > 0 then
    print("FAIL " .. fails)
    os.exit(1)
end
print("OK")
