-- deploy/edge/lib/backend.lua
--
-- Three-tier route cache + fail-static (INTERFACE-CONTRACT §2).
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
--     #497 — that last sentence was NOT true before #497: every L2 read served
--     any entry within L2_TTL, so on the happy path L1 kept being refilled from
--     a value up to 60s old and a legit tenant move took up to 60s to reach the
--     edge. Worse, if the stale host:port had meanwhile been reused by ANOTHER
--     tenant's VM the upstream connected fine, the balancer never errored and
--     invalidate() never fired, so traffic kept landing on the wrong microVM
--     for the whole window. Enforcement now lives in l2_get's allow_stale
--     argument (fresh-only by default, stale only for fail-static); the window
--     is pinned by deploy/edge/test/backend_freshness_crosstenant_spec.lua.
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
-- INTERFACE-CONTRACT and the header comment above.
local L2_TTL_SEC      = 60
local NEG_TTL_SEC     = 2
-- #497 — how long an observed Redis failure for ONE tenant's key counts as evidence
-- that its route cannot be re-read. It does two jobs: authorise a lock-timeout waiter
-- to fall back to that tenant's aged entry (answer_without_redis), and back the lock
-- holder off the doomed connection (try_redis).
--
-- Deliberately much SHORTER than POS_TTL_SEC. The evidence suppresses re-reads, so
-- after Redis recovers we keep serving an aged blob — which may be up to L2_TTL_SEC
-- old and whose host:port may already belong to another tenant's VM — for as long as
-- this window lasts. Keeping it well inside the freshness budget bounds that exposure;
-- a persistent outage is unaffected because every holder attempt re-observes the error
-- and refreshes the marker, so it stays present for the whole outage regardless of how
-- short the TTL is. The trade was reviewed both ways across four cross-model rounds
-- (add the backoff / drop it); this bounds it instead of picking a side.
local ERR_TTL_SEC     = 0.5
-- Small jitter on positive TTL avoids herd expiry across workers.
-- #497 — DOWNWARD only. It used to be ±0.5s, so an L1 entry could live 5.5s while the
-- L2 freshness marker expired at exactly POS_TTL_SEC — making the switch window this
-- issue pins actually 5.5s, not 5s. Subtracting only keeps the herd spread out while
-- making POS_TTL_SEC a true ceiling.
local function pos_ttl_jitter()
    return POS_TTL_SEC - math.random() * 0.5
end

-- Max time waiting behind another worker. #497 — must EXCEED the holder's WARM Redis
-- budget (300ms) plus scheduling margin. It used to be a flat 0.2s, i.e. shorter than
-- the holder could possibly take to fail: at the start of an outage every waiter gave
-- up before the holder published its failure evidence, so they all 503'd instead of
-- falling back to fail-static. A waiter has nothing useful to do on its own now
-- (answer_without_redis never touches Redis), so waiting out the holder is strictly
-- better than giving up early. Derived, not hardcoded, so retuning the Redis timeouts
-- cannot silently reintroduce the gap.
--
-- It deliberately does NOT cover a cold DNS resolution (redis_client.COLD_CEILING_MS,
-- seconds). Blocking a request for that long to spare it a 503 would trade a bounded
-- blip for worker starvation across the fleet — the wrong direction. Residual: when a
-- holder is stuck in DNS, this tenant's waiters get the bounded 503 window described in
-- answer_without_redis until the holder finally publishes its outcome.
local LOCK_TIMEOUT_SEC = (redis_client.WARM_BUDGET_MS + 100) / 1000  -- 0.4s
-- Lease, NOT a wait: sized against the COLD ceiling so the lock cannot expire while its
-- holder is still legitimately working. With a shorter lease a DNS-stalled holder loses
-- the lock, a second worker enters, and the two writes can land out of order — the
-- stale/cross-tenant routing this issue removes. Nobody blocks for this long: waiters
-- give up after LOCK_TIMEOUT_SEC and answer from the cache.
local LOCK_EXPTIME_SEC = (redis_client.COLD_CEILING_MS + 700) / 1000  -- 4s

-- Worker-local cache created at init_worker time via _M.init_worker().
-- Capacity 4000 handles a hot subset of ~10w tenants per worker generously.
local L1_CAPACITY = 4000
local L1_CACHE  -- set by init_worker

local NEG_SENTINEL = { __neg__ = true }

--[[
    parse_value: decode the JSON string written by host-agent (see
    INTERFACE-CONTRACT §1). Returns descriptor table, or nil on bad JSON.
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
-- #497 — "this L2 entry was written within the L1 freshness window". Same value
-- lives in L2 under two lifetimes: fresh (POS_TTL_SEC, servable on the happy
-- path) and stale (L2_TTL_SEC, fail-static only). One extra shdict key is what
-- lets l2_get tell the two apart; without it a 60s-old entry is indistinguishable
-- from one the winning worker just filled.
local function fresh_key(tid) return "f:" .. tid end
-- #497 — per-tenant FAILURE GENERATION: a counter bumped every time reading this
-- tenant's route from Redis fails, deleted as soon as a read succeeds. A waiter samples
-- it before waiting and re-reads it after a lock timeout; a CHANGED value means "a
-- failure completed during MY wait", which is the only thing that authorises serving an
-- aged route.
--
-- A counter, not a timestamp: ngx.now() is cached per worker and only refreshed at
-- yields, so events in one event-loop iteration share an instant. Comparing timestamps
-- therefore misclassifies same-tick cases in BOTH directions — authorising an older
-- failure, or refusing a genuine one and 503-ing mid-outage. incr() is atomic and
-- changes on every failure regardless of the clock.
--
-- Per tenant, not endpoint-wide: a failure on tenant A must not authorise serving
-- tenant B's aged route, because B's aged host:port may already belong to a third
-- tenant's VM and no-cross-tenant outranks availability.
local function err_key(tid) return "e:" .. tid end

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
            -- #497 — drop the entry entirely, marker included. Redis just handed us a
            -- newer value, so whatever survived the failed write is proven obsolete:
            -- keeping the marker would stamp it "fresh" and route traffic to the
            -- previous endpoint, and keeping the blob alone would still let a later
            -- Redis outage serve it through fail-static, where the old host:port may
            -- by then belong to ANOTHER tenant's VM. Cost: this tenant loses its
            -- fail-static material until the next successful write (a later outage
            -- 503s instead) — the right side of no-cross-tenant over availability.
            shared:delete(fresh_key(tid))
            shared:delete(l2_key(tid))
        else
            if forcible then
                ngx.log(ngx.INFO, "route_cache LRU evicted an entry (dict full)")
            end
            -- #497 — freshness marker, expires with the L1 window. Its absence does
            -- not drop the route: the blob above still backs fail-static.
            shared:set(fresh_key(tid), "1", POS_TTL_SEC)
        end
    end
end

-- #618 — clean miss 只有来自 primary 时才是权威“不存在”。reader 可能正处于
-- 复制滞后、重同步或数据集尚未补齐，此时只能写短负缓存挡扫描器，不能删除
-- L2 blob/fresh marker 这份 fail-static 底料。代价是 #497 已识别的风险在
-- 非权威路径上部分回归：若租户确已删除，旧 blob 以后可能被 fail-static 供出；
-- 这是用该风险换取副本异常时不主动摧毁在役租户故障兜底的刻意权衡。
local function put_negative(shared, tid, authoritative)
    if L1_CACHE then L1_CACHE:set(tid, NEG_SENTINEL, NEG_TTL_SEC) end
    if shared then
        shared:set(neg_key(tid), "1", NEG_TTL_SEC)
        -- #497 — Redis authoritatively has no route for this tenant, so any cached
        -- positive is known-invalid and must not survive as fail-static material.
        -- Without this, the sequence [positive cached] → [tenant deleted, Redis says
        -- miss] → [2s negative expires] → [Redis unreachable] makes _finish_lookup
        -- serve the deleted tenant's old host:port, which by then may belong to
        -- ANOTHER tenant's VM. The negative's own 2s TTL is not a substitute: the
        -- blob outlives it by up to L2_TTL_SEC.
        if authoritative then
            shared:delete(l2_key(tid))
            shared:delete(fresh_key(tid))
        end
    end
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
--
-- #497 — allow_stale splits the two jobs L2 does:
--   * nil/false (default, happy path): serve only entries still inside the L1
--     freshness window. A route that changed in Redis therefore reaches the edge
--     in POS_TTL_SEC, not L2_TTL_SEC.
--   * true (fail-static, _finish_lookup only): Redis is unreachable, so an old
--     entry is the best available answer and is served as SOURCE_STATIC.
local function l2_get(shared, tid, allow_stale)
    if not shared then return nil, nil end
    if shared:get(neg_key(tid)) then return nil, true end
    -- #497 — marker BEFORE blob, deliberately. These are two separate shdict gets,
    -- so a concurrent put_positive can land between them. Reading the blob first
    -- would let an OLD blob (already past its window) be paired with the NEW marker
    -- written moments later, sending traffic to the previous endpoint. In this order
    -- the worst interleaving pairs an older marker with a NEWER blob — harmless,
    -- because a blob is only ever replaced by a fresher one, never reverted.
    local is_fresh = allow_stale or shared:get(fresh_key(tid)) ~= nil
    local blob = shared:get(l2_key(tid))
    if utils.is_blank(blob) then return nil, nil end
    if not is_fresh then
        -- Present but past its freshness window: treat as a miss so the caller
        -- goes to Redis. Deliberately NOT deleted — fail-static still needs it.
        return nil, nil
    end
    local desc = parse_value(blob)
    if not desc then return nil, nil end
    return desc, false
end

-- try_redis: single-flighted L3 read + parse. Returns (desc, source, err).
-- source is SOURCE_L3 (fresh from Redis), SOURCE_NEG (Redis said miss),
-- SOURCE_STATIC (transport error, caller must serve L2 stale).
--
-- Writes the cache, so it must only ever run under the per-tenant lock (or in the
-- degraded no-lock-available mode). #497 — two lookups racing outside the lock can
-- complete out of order, and the later write may carry the OLDER Redis value:
-- that would overwrite a newer route and re-arm its freshness marker, i.e. exactly
-- the stale/cross-tenant routing this issue removes. The lock-timeout path
-- therefore never reaches here; it answers from the cache alone.
local function try_redis(shared, tid, redis_host, redis_port, authoritative)
    -- #497 — the lock holder ALWAYS probes Redis, never backs off on the failure
    -- evidence. A backoff would suppress re-reads, so for as long as the evidence lived
    -- we would keep serving an aged blob (up to L2_TTL_SEC old, its host:port possibly
    -- reused by another tenant's VM) even after Redis recovered — introducing a
    -- cross-tenant window bb does not have today, to fix a load problem it already has.
    -- no-cross-tenant is non-negotiable and outranks load, so the trade is refused here
    -- and the connection-storm gap is recorded as a separate follow-up instead.
    local key = "route:" .. tid
    local raw, err = redis_client.get_route(redis_host, redis_port, key)
    if err then
        -- #497 — record that this tenant's route really could not be re-read, so a
        -- lock-timeout waiter on the same tenant may fall back to its aged entry.
        -- Without this, "Redis is down" and "we merely lost a 0.2s lock race" are
        -- indistinguishable.
        -- Bump the generation so a waiter can tell "this failed while I was waiting"
        -- from "this failed some time ago and may well be fixed by now".
        -- Two ops on purpose: incr() atomically guarantees the value CHANGES, but its
        -- init_ttl applies only when the key is created — later increments would not
        -- refresh the TTL, so during a sustained outage the evidence would expire mid
        -- outage and strand every waiter. The following set() refreshes it. A concurrent
        -- interleaving can only write back an equal-or-lower value, which makes a waiter
        -- see "unchanged" and fail closed — the safe direction.
        if shared then
            local gen = shared:incr(err_key(tid), 1, 0) or 1
            shared:set(err_key(tid), gen, ERR_TTL_SEC)
        end
        return nil, _M.SOURCE_STATIC, err
    end
    -- Redis answered, so retract the evidence — but only NOW, after a completed
    -- response. Clearing it before the probe instead would strand every waiter queued
    -- behind this holder during a real outage: they would time out mid-probe, find no
    -- evidence and 503 even though L2 holds a usable entry. Retracting on success keeps
    -- fail-static working while still making recovery visible immediately (the next
    -- waiter finds nothing and fails closed). The remaining case — a waiter timing out
    -- while a successor's probe is still in flight, authorised by a failure that
    -- happened inside its own wait — is bounded by one lock wait and is the honest
    -- state: the last completed information said Redis was failing.
    if shared then shared:delete(err_key(tid)) end
    if raw == nil then
        -- Clean miss: unknown tenant.
        put_negative(shared, tid, authoritative)
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

-- answer_without_redis: the lock-timeout waiter's answer. Returns (desc, source, err).
--
-- #497 — this path runs OUTSIDE the per-tenant lock, so it deliberately touches
-- neither Redis nor the cache:
--   * no Redis connection — otherwise every timed-out waiter would open one and
--     contention (or a slow Redis) would turn into a connection stampede, defeating
--     the single-flight this lock exists for;
--   * no cache write — two unlocked lookups can complete out of order and the later
--     write may carry the OLDER value, overwriting a newer route and re-arming its
--     freshness marker, which is exactly the stale routing this issue removes.
--
-- It serves the aged entry only when reading THIS tenant's route from Redis failed
-- DURING THIS WAIT: `gen_before` is the failure generation sampled before the wait began,
-- and the current one must DIFFER. Weaker gates were rejected — losing a lock race is not
-- evidence Redis is broken at all, and merely "there was a failure recently" lets one that
-- has since been fixed authorise serving a route up to L2_TTL_SEC old, whose host:port may
-- by then belong to ANOTHER tenant's VM. Tying the answer to the very attempt we waited on
-- is what makes it honest. The four cases:
--   nil → nil : no failure at all                     → fail closed
--   5   → 5   : the failure predates our wait          → fail closed
--   5   → nil : a success during our wait retracted it → fail closed
--   5   → 6   : a failure completed during our wait    → serve the aged entry
-- (`~=`, not `>`: a success deletes the key, so the counter legitimately restarts at 1.)
--
-- Failing closed means the same 503 the pre-#497 code returned when L2 held nothing, and
-- the next request will find whatever the holder is about to write.
local function answer_without_redis(shared, tid, gen_before)
    if shared then
        local gen_now = shared:get(err_key(tid))
        if gen_now ~= nil and gen_now ~= gen_before then
            local stale_desc = l2_get(shared, tid, true)
            if stale_desc ~= nil then return stale_desc, _M.SOURCE_STATIC, nil end
        end
    end
    return nil, _M.SOURCE_STATIC, 503
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
-- #618 — 末位第 5 参数 authoritative 表示本次 Redis clean miss 是否来自
-- primary；省略时默认 true，保证所有旧调用方与开关关闭形态保持原行为。
function _M.lookup_backend(shared, tid, redis_host, redis_port, authoritative)
    if authoritative == nil then authoritative = true end
    -- L1: worker-local hot path.
    local desc, is_neg = l1_get(tid)
    if desc ~= nil then return desc, _M.SOURCE_L1, nil end
    if is_neg then return nil, _M.SOURCE_NEG, 404 end

    -- L2: cross-worker cache. #497 — fresh entries only (see l2_get): an entry
    -- older than the L1 freshness window is reachable solely through the
    -- fail-static path in _finish_lookup, never here.
    desc, is_neg = l2_get(shared, tid)
    if desc ~= nil then
        -- #497 — deliberately NOT promoted into L1. Re-arming a full POS_TTL_SEC
        -- L1 entry from a marker that may have milliseconds left would stack the
        -- two windows: an L2 hit at t=4.9s would keep serving until t≈9.9s, so the
        -- worst case becomes ~2×POS_TTL_SEC instead of POS_TTL_SEC. The marker is
        -- the single budget for this value; a shdict get per request on this path
        -- is in-process and cheap, so paying it keeps the window provably bounded.
        return desc, _M.SOURCE_L2, nil
    end
    if is_neg then return nil, _M.SOURCE_NEG, 404 end

    -- L3: single-flight to Redis.
    local lock, lerr = get_lock_module():new("route_locks",
        { timeout = LOCK_TIMEOUT_SEC, exptime = LOCK_EXPTIME_SEC })
    if not lock then
        ngx.log(ngx.WARN, "resty.lock new failed: ", tostring(lerr))
        -- Skip the lock and go direct — losing the stampede shield is
        -- better than dropping the request.
        -- #497 — this is the one unlocked path that still WRITES the cache.
        -- `resty.lock:new()` only fails when the `route_locks` dict itself is broken,
        -- i.e. NO request can be locked; if these lookups also refused to write, the
        -- cache would never fill and the whole fleet would sit on Redis. In that
        -- degraded mode a filled cache is worth more than write ordering.
        return _M._finish_lookup(
            shared, tid, redis_host, redis_port, authoritative)
    end

    -- #497 — sampled BEFORE the wait: answer_without_redis only accepts a failure
    -- generation that CHANGED since this point, i.e. one produced by the attempt we are
    -- actually waiting on.
    local fail_gen_before = shared and shared:get(err_key(tid)) or nil
    local elapsed, wait_err = lock:lock(tid)
    if not elapsed then
        -- Waited past LOCK_TIMEOUT_SEC. Retry cache (winner may have filled).
        desc, is_neg = l2_get(shared, tid)
        if desc ~= nil then return desc, _M.SOURCE_L2, nil end
        if is_neg then return nil, _M.SOURCE_NEG, 404 end
        ngx.log(ngx.WARN, "route_locks lock timeout for ", tid, ": ",
            tostring(wait_err))
        -- #497 — before the freshness gate this branch was reached with an "any entry
        -- within L2_TTL" read, so a stale entry served the request. The gate would turn
        -- that into a 503 exactly when Redis is slow and contention is high, so the
        -- fallback moved into answer_without_redis (see its header for the two
        -- properties it holds and why the staleness needs evidence).
        return answer_without_redis(shared, tid, fail_gen_before)
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
        _M._finish_lookup(
            shared, tid, redis_host, redis_port, authoritative)
    pcall(lock.unlock, lock)
    return out_desc, out_source, out_status
end

-- _finish_lookup: separated so the "lock creation failed" fast path can
-- reuse the exact same fail-static logic without duplicating branches.
function _M._finish_lookup(shared, tid, redis_host, redis_port, authoritative)
    local desc, source, rerr = try_redis(
        shared, tid, redis_host, redis_port, authoritative)
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
    -- #497 — allow_stale=true: this is the one path where an entry past its
    -- freshness window is the right answer (Redis cannot be reached at all).
    local stale_desc, stale_neg = l2_get(shared, tid, true)
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
        shared:delete(fresh_key(tid))  -- #497 — 别留下指向已删 blob 的新鲜标记
    end
end

-- Exposed for tests.
_M._parse_value = parse_value
_M._pos_ttl_jitter = pos_ttl_jitter
_M._POS_TTL_SEC = POS_TTL_SEC
_M._LOCK_TIMEOUT_SEC = LOCK_TIMEOUT_SEC
_M._LOCK_EXPTIME_SEC = LOCK_EXPTIME_SEC
_M._L2_TTL_SEC  = L2_TTL_SEC
_M._NEG_TTL_SEC = NEG_TTL_SEC
_M._ERR_TTL_SEC = ERR_TTL_SEC

return _M
