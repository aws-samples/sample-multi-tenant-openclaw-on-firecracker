-- deploy/edge/lib/redis_client.lua
--
-- Thin, single-purpose wrapper around lua-resty-redis. The only op we need
-- is GET on the "route:{tenant_id}" key (INTERFACE-CONTRACT §1). Kept
-- separate so unit tests can inject a stub for the "redis" table without
-- touching lookup_backend.
--
-- Design:
--   - No pipeline, no auth (ElastiCache in VPC uses SG + subnet isolation).
--   - Short timeouts: this runs on the hot path of every request; a stuck
--     Redis must fall through to fail-static (backend.lua handles that).
--   - Connection pool via set_keepalive so we don't reopen TCP per request.
--
-- Deps:
--   - VERIFY: require "resty.redis" is the standard lua-resty-redis module
--     shipped with OpenResty (openresty/lua-resty-redis). API used:
--       new()  set_timeouts(connect_ms, send_ms, read_ms)  connect(ip, port)
--       get(key)  set_keepalive(idle_ms, pool_size)  close()

local resty_redis  -- lazily required so busted tests can pre-inject a stub
local _M = { _VERSION = "0.01" }

-- Defaults kept modest — we want to fail fast, not stall the request under
-- Redis brownout. The middle layer (shared_dict fail-static) takes over.
local DEFAULT_CONNECT_MS = 100
local DEFAULT_SEND_MS    = 100
local DEFAULT_READ_MS    = 100
-- #497 — budget for one get_route() on a WARM connection, published as a contract
-- because backend.lua's single-flight lock must wait at least this long: a waiter that
-- gives up earlier than the holder can possibly finish never learns the outcome.
--
-- Deliberately EXCLUDES DNS. The endpoint is an ElastiCache/Valkey DNS name, and a
-- cold connect resolves it through nginx's `resolver` (see nginx.conf: `resolver_timeout
-- 3s`, `valid=30s`), whose budget is separate from set_timeouts() — so a cold, unlucky
-- call can take seconds, not 300ms. Callers must size a *lock lease* against that
-- larger figure but must NOT make a request wait for it; see backend.lua.
_M.WARM_BUDGET_MS = DEFAULT_CONNECT_MS + DEFAULT_SEND_MS + DEFAULT_READ_MS
-- Ceiling including a cold DNS resolution (resolver_timeout 3s in nginx.conf). Only
-- for sizing leases/timeouts that must not expire while a holder is still working.
_M.COLD_CEILING_MS = 3000 + _M.WARM_BUDGET_MS
-- #625 — 一次 lookup 最多顺序读取两次 Redis：先读 reader；仅当 clean miss
-- 与存活 L2 blob 矛盾时，再向 primary 复核一次。锁预算必须覆盖这两次读取。
_M.MAX_SEQUENTIAL_READS = 2
-- Keepalive: 60s idle, 100 conns per worker. Matches lua-resty-redis
-- recommended values for a hot-path lookup service.
local KEEPALIVE_IDLE_MS = 60 * 1000
local KEEPALIVE_POOL    = 100

-- Test seam: allow injecting a fake resty.redis module before the first
-- call. In production this stays nil and we require the real module.
function _M._set_redis_module(m)
    resty_redis = m
end

local function get_redis_module()
    if resty_redis ~= nil then return resty_redis end
    resty_redis = require "resty.redis"
    return resty_redis
end

--[[
    get_route: fetches the raw JSON string stored at "route:{tenant_id}".

    3 args:
      - host: Redis host (string). ENGINE_REDIS_ENDPOINT split by nginx.conf.
      - port: Redis port (integer, usually 6379).
      - key:  full Redis key (already prefixed).

    2 return values:
      - value:  string on hit; nil on clean miss (Redis returned null)
      - err:    nil on success; string on transport / protocol failure

    A clean miss (nil, nil) is a semantic "unknown tenant" — caller should
    negative-cache. A transport error (nil, err) means Redis is unhealthy;
    caller should fail-static, not negative-cache.
--]]
function _M.get_route(host, port, key)
    local red_mod = get_redis_module()
    local red, err = red_mod:new()
    if not red then return nil, "resty.redis new failed: " .. tostring(err) end

    red:set_timeouts(DEFAULT_CONNECT_MS, DEFAULT_SEND_MS, DEFAULT_READ_MS)

    local ok, cerr = red:connect(host, port)
    if not ok then
        return nil, "redis connect " .. tostring(host) .. ":" .. tostring(port)
            .. " failed: " .. tostring(cerr)
    end

    local val, gerr = red:get(key)
    if gerr then
        -- Don't pool a connection that just errored.
        pcall(red.close, red)
        return nil, "redis GET " .. key .. " failed: " .. tostring(gerr)
    end

    -- Return connection to pool for reuse.
    local ok2, kerr = red:set_keepalive(KEEPALIVE_IDLE_MS, KEEPALIVE_POOL)
    if not ok2 then
        -- Non-fatal: log at caller. We already have the value.
        ngx.log(ngx.WARN, "redis set_keepalive: ", tostring(kerr))
        pcall(red.close, red)
    end

    -- lua-resty-redis returns ngx.null for missing keys.
    if val == nil or val == ngx.null then
        return nil, nil
    end
    return val, nil
end

-- Exposed for tests.
_M._DEFAULT_CONNECT_MS = DEFAULT_CONNECT_MS
_M._KEEPALIVE_POOL     = KEEPALIVE_POOL

return _M
