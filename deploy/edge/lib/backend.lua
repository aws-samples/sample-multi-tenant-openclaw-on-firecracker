-- deploy/edge/lib/backend.lua
--
-- Three-tier route cache + fail-static (the data-plane contract).
--
-- Layers (top-down):
--   L1  worker-local lrucache (resty.lrucache)   — nanosecond hits
--   L2  ngx.shared.DICT "route_cache"            — shared across workers
--   L3  Redis "route:{tenant_id}"                — cell-wide source of truth
--
-- Fail-static: when Redis returns a transport error, we fall back to
-- whatever value is still in L2 (workers survive Redis brownouts). Missing
-- tenants get a short-TTL negative entry to shield Redis from lookup
-- storms during a scan or ID enumeration attack.
--
-- Stampede protection: on L3 miss we take a lua-resty-lock keyed by the
-- tenant id. Only one worker per key opens a Redis connection; the losers
-- wait, then re-read the cache.
--
-- Cache TTL rationale:
--   * Positive (freshness path, L1_TTL): 5s + small jitter — host-agent
--     double-writes DDB + Redis, so 5s worst-case propagation for a legit
--     tenant move keeps the SLA.
--   * L2 stale window (L2_TTL): 60s — this is the fail-static safety net
--     covering an ElastiCache primary→replica automatic failover. AWS
--     ElastiCache Multi-AZ failover window is typically 10-30s; we hold
--     stale route entries in L2 for 60s so route.lua can keep serving
--     traffic through the switchover instead of 503-ing. INTERFACE-
--     CONTRACT §8 fixes this as the quantified lower bound: "L2 TTL ≥
--     预期最长 failover 窗口(建议 ≥30-60s)". After 60s host-agent will
--     have re-written fresh values anyway.
--     Freshness comes from L1 (5s) invalidating first; L2 only feeds
--     stale reads during Redis brownouts / failover, so a longer TTL
--     does NOT slow down route updates on the happy path.
--   * Negative: 2s — unknown ids for scanners, don't cache long enough to
--     make later legitimate creation feel slow. Deliberately short.

local cjson         = require "cjson.safe"
local lrucache_mod  -- resty.lrucache (per-worker, lazily required)
local resty_lock    -- resty.lock (per-request stampede guard)
local utils         = require "edge.lib.utils"
local redis_client  = require "edge.lib.redis_client"

local _M = { _VERSION = "0.02" }

-- Layer identifiers used in metrics + tests.
_M.SOURCE_L1 = "l1"
_M.SOURCE_L2 = "l2"
_M.SOURCE_L3 = "l3"
_M.SOURCE_NEG = "neg"      -- negative cache hit (unknown tenant)
_M.SOURCE_STATIC = "static"-- fail-static (Redis error, L2 stale)

-- L1 (worker-local) freshness TTL — short so a real route move is picked
-- up within seconds even for tenants that stay hot in cache.
local POS_TTL_SEC     = 5
-- L2 (shared_dict) stale TTL — long enough to cover an ElastiCache
-- Multi-AZ failover window while Redis is unreachable. See §8 of
-- the data-plane contract and the header comment above.
local L2_TTL_SEC      = 60
local NEG_TTL_SEC     = 2
-- Small ±0.5s jitter on positive TTL avoids herd expiry across workers.
local function pos_ttl_jitter()
    return POS_TTL_SEC + (math.random() - 0.5)
end

local LOCK_TIMEOUT_SEC = 0.2  -- max time waiting behind another worker

-- Worker-local cache created at init_worker time via _M.init_worker().
-- Capacity 4000 handles a hot subset of ~10w tenants per worker generously.
local L1_CAPACITY = 4000
local L1_CACHE  -- set by init_worker

local NEG_SENTINEL = { __neg__ = true }

--[[
    parse_value: decode the JSON string written by host-agent (see
    the data-plane contract). Returns descriptor table, or nil on bad JSON.
    Rejects entries missing any required field so the caller can 404
    instead of routing to a black hole.
--]]
local function parse_value(json_str)
    if utils.is_blank(json_str) then return nil end
    local obj, err = cjson.decode(json_str)
    if not obj or type(obj) ~= "table" then
        return nil, "decode: " .. tostring(err)
    end
    local host = obj.host
    local guest_ip = obj.guest_ip
    local port = utils.safe_tonumber(obj.port)
    if utils.is_blank(host) or utils.is_blank(guest_ip) or port == nil then
        return nil, "missing required field host/port/guest_ip"
    end
    return {
        host = host,
        port = port,
        guest_ip = guest_ip,
        updated_at = utils.safe_tonumber(obj.updated_at) or 0,
    }, nil
end

local function l2_key(tid) return "r:" .. tid end
local function neg_key(tid) return "n:" .. tid end

--[[
    init_worker: called from init_worker_by_lua_file. Creates the per-worker
    lrucache. Idempotent (nginx reload / SIGHUP re-runs init_worker).
--]]
function _M.init_worker()
    if lrucache_mod == nil then
        lrucache_mod = require "resty.lrucache"
    end
    local c, err = lrucache_mod.new(L1_CAPACITY)
    if not c then
        ngx.log(ngx.ERR, "route lrucache new failed: ", tostring(err))
        return false
    end
    L1_CACHE = c
    return true
end

-- Test seams: allow busted to inject fakes without booting ngx.
function _M._set_l1(cache) L1_CACHE = cache end
function _M._set_lock_module(m) resty_lock = m end

local function get_lock_module()
    if resty_lock ~= nil then return resty_lock end
    resty_lock = require "resty.lock"
    return resty_lock
end

-- put_positive: fill L1 (short TTL, freshness) + L2 (long TTL, fail-static
-- covers ElastiCache failover). See header comment for rationale.
local function put_positive(shared, tid, desc)
    if L1_CACHE then L1_CACHE:set(tid, desc, pos_ttl_jitter()) end
    if shared then
        local blob = cjson.encode(desc)
        -- set() may evict LRU; forcible=true is fine, log at INFO for ops.
        local ok, err, forcible = shared:set(l2_key(tid), blob, L2_TTL_SEC)
        if not ok then
            ngx.log(ngx.WARN, "route_cache set positive failed: ", tostring(err))
        elseif forcible then
            ngx.log(ngx.INFO, "route_cache LRU evicted an entry (dict full)")
        end
    end
end

local function put_negative(shared, tid)
    if L1_CACHE then L1_CACHE:set(tid, NEG_SENTINEL, NEG_TTL_SEC) end
    if shared then shared:set(neg_key(tid), "1", NEG_TTL_SEC) end
end

-- l1_get returns desc, is_neg (or nil,nil for miss)
local function l1_get(tid)
    if not L1_CACHE then return nil, nil end
    local v = L1_CACHE:get(tid)
    if v == nil then return nil, nil end
    if v == NEG_SENTINEL then return nil, true end
    return v, false
end

-- l2_get: hit → (desc, false); neg hit → (nil, true); miss → (nil, nil)
local function l2_get(shared, tid)
    if not shared then return nil, nil end
    if shared:get(neg_key(tid)) then return nil, true end
    local blob = shared:get(l2_key(tid))
    if utils.is_blank(blob) then return nil, nil end
    local desc = parse_value(blob)
    if not desc then return nil, nil end
    return desc, false
end

-- try_redis: single-flighted L3 read + parse. Returns (desc, source, err).
-- source is SOURCE_L3 (fresh from Redis), SOURCE_NEG (Redis said miss),
-- SOURCE_STATIC (transport error, caller must serve L2 stale).
local function try_redis(shared, tid, redis_host, redis_port)
    local key = "route:" .. tid
    local raw, err = redis_client.get_route(redis_host, redis_port, key)
    if err then
        return nil, _M.SOURCE_STATIC, err
    end
    if raw == nil then
        -- Clean miss: unknown tenant.
        put_negative(shared, tid)
        return nil, _M.SOURCE_NEG, nil
    end
    local desc, perr = parse_value(raw)
    if not desc then
        -- Malformed value in Redis (host-agent bug or partial write). Treat
        -- as miss but log loudly; do NOT negative-cache — a fix by
        -- host-agent should propagate immediately, not wait for TTL.
        ngx.log(ngx.ERR, "route:", tid, " malformed value: ", tostring(perr))
        return nil, _M.SOURCE_NEG, nil
    end
    put_positive(shared, tid, desc)
    return desc, _M.SOURCE_L3, nil
end

--[[
    lookup_backend: main API. Returns (descriptor, source, err_status).

    - descriptor: { host, port, guest_ip, updated_at } or nil
    - source: SOURCE_L1/L2/L3/NEG/STATIC (test + metric attribution)
    - err_status: nil on hit; 404 on unknown tenant; 503 only for the
      pathological case of Redis error AND no stale L2 value.

    4 args:
      - shared: ngx.shared.route_cache dict (may be nil in tests)
      - tid:    validated tenant id string
      - redis_host, redis_port: nginx.conf-configured endpoint
--]]
function _M.lookup_backend(shared, tid, redis_host, redis_port)
    -- L1: worker-local hot path.
    local desc, is_neg = l1_get(tid)
    if desc ~= nil then return desc, _M.SOURCE_L1, nil end
    if is_neg then return nil, _M.SOURCE_NEG, 404 end

    -- L2: cross-worker cache.
    desc, is_neg = l2_get(shared, tid)
    if desc ~= nil then
        if L1_CACHE then L1_CACHE:set(tid, desc, pos_ttl_jitter()) end
        return desc, _M.SOURCE_L2, nil
    end
    if is_neg then return nil, _M.SOURCE_NEG, 404 end

    -- L3: single-flight to Redis.
    local lock, lerr = get_lock_module():new("route_locks",
        { timeout = LOCK_TIMEOUT_SEC, exptime = 1 })
    if not lock then
        ngx.log(ngx.WARN, "resty.lock new failed: ", tostring(lerr))
        -- Skip the lock and go direct — losing the stampede shield is
        -- better than dropping the request.
        return _M._finish_lookup(shared, tid, redis_host, redis_port)
    end

    local elapsed, wait_err = lock:lock(tid)
    if not elapsed then
        -- Waited past LOCK_TIMEOUT_SEC. Retry cache (winner may have filled).
        desc, is_neg = l2_get(shared, tid)
        if desc ~= nil then return desc, _M.SOURCE_L2, nil end
        if is_neg then return nil, _M.SOURCE_NEG, 404 end
        ngx.log(ngx.WARN, "route_locks lock timeout for ", tid, ": ",
            tostring(wait_err))
        return nil, _M.SOURCE_STATIC, 503
    end

    -- We hold the lock. Re-check cache first (another worker may have
    -- filled it while we were queued).
    desc, is_neg = l2_get(shared, tid)
    if desc ~= nil then
        pcall(lock.unlock, lock)
        return desc, _M.SOURCE_L2, nil
    end
    if is_neg then
        pcall(lock.unlock, lock)
        return nil, _M.SOURCE_NEG, 404
    end

    local out_desc, out_source, out_status =
        _M._finish_lookup(shared, tid, redis_host, redis_port)
    pcall(lock.unlock, lock)
    return out_desc, out_source, out_status
end

-- _finish_lookup: separated so the "lock creation failed" fast path can
-- reuse the exact same fail-static logic without duplicating branches.
function _M._finish_lookup(shared, tid, redis_host, redis_port)
    local desc, source, rerr = try_redis(shared, tid, redis_host, redis_port)
    if source == _M.SOURCE_L3 then
        return desc, source, nil
    end
    if source == _M.SOURCE_NEG then
        return nil, _M.SOURCE_NEG, 404
    end
    -- SOURCE_STATIC: Redis is unhealthy. Try L2 one more time — under
    -- brownouts host-agent may still have valid entries that were slot-
    -- evicted by unrelated churn; the second read is nearly free.
    ngx.log(ngx.WARN, "redis transport err, fail-static for ", tid, ": ",
        tostring(rerr))
    local stale_desc, stale_neg = l2_get(shared, tid)
    if stale_desc ~= nil then return stale_desc, _M.SOURCE_STATIC, nil end
    if stale_neg then return nil, _M.SOURCE_NEG, 404 end
    return nil, _M.SOURCE_STATIC, 503
end

--[[
    invalidate: 强制清除 tenant_id 在 L1/L2 的 route 缓存与 negative marker。
    R6.3② edge failover:balancer 检测到上游连接失败(源 host DNAT 已拆,
    内核 RST)时,调本函数把当前缓存降为空,下一次 lookup_backend 会重查
    Redis 拿新 route (target host)。避免"路由已切但 edge 仍走旧 host"
    造成的连接抖动窗口(TTL 未过前不主动失效则依赖 5s 自然过期)。

    幂等:调多次无副作用;清缓存不是清 route 权威(Redis 才是权威)。
    不写 negative:留给下一次 lookup 决定(可能拿到新 desc,也可能真的没了)。
--]]
function _M.invalidate(shared, tid)
    if not tid or tid == "" then return end
    if L1_CACHE then L1_CACHE:delete(tid) end
    if shared then
        shared:delete(l2_key(tid))
        shared:delete(neg_key(tid))
    end
end

-- Exposed for tests.
_M._parse_value = parse_value
_M._pos_ttl_jitter = pos_ttl_jitter
_M._POS_TTL_SEC = POS_TTL_SEC
_M._L2_TTL_SEC  = L2_TTL_SEC
_M._NEG_TTL_SEC = NEG_TTL_SEC

return _M
