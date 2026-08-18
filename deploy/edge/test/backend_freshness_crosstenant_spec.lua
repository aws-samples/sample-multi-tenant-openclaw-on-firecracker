-- deploy/edge/test/backend_freshness_crosstenant_spec.lua
--
-- #497 — pins the real route switch window and the two jobs L2 does.
--
-- Before #497 every L2 read served any entry inside L2_TTL(60s), so on the happy
-- path L1 kept being refilled from a stale value: a legit tenant move took up to
-- 60s to reach the edge while the file's own TTL rationale claimed 5s. The tests
-- below fail on that old behaviour and pass on the fresh-gated one.
--
-- No sleeping: "the freshness window has passed" is expressed by dropping the
-- `f:<tid>` marker, which is exactly the state the shdict reaches after
-- POS_TTL_SEC. L1 expiry is expressed by clearing the injected fake L1.

local helper = require "spec_helper"
local backend = require "edge.lib.backend"
local redis_client = require "edge.lib.redis_client"
local cjson = require "cjson.safe"

local function desc_json(host, port, guest_ip)
    return cjson.encode({
        host = host, port = port, guest_ip = guest_ip, updated_at = os.time(),
    })
end

-- resty.lock whose lock() always times out — exercises the waiter branch. `during` runs
-- inside lock(), which is how a test models what the holder does WHILE we are queued.
local function timeout_lock_module(during)
    local mod = {}
    function mod:new(_name, _opts)
        return {
            lock = function(_self, _key)
                if during then during() end
                return nil, "timeout"
            end,
            unlock = function(_self) return 1, nil end,
        }, nil
    end
    return mod
end

-- Wraps the shared fake redis module to count connection attempts, so a test can
-- assert "this path did not talk to Redis at all" rather than only inferring it
-- from the value that came back. Returns (module, counter_table).
local function counting_redis_module()
    local inner = helper.new_fake_redis_module()
    local count = { n = 0 }
    local mod = {}
    -- 参数照实命名(不用 `_` 前缀):luacheck 会对「带未使用提示却被使用」的变量报警,
    -- 而这道 CI 门里 luacheck 退非 0 会连带把后面的 busted / openresty -t 一起带崩。
    function mod.new(receiver)
        count.n = count.n + 1
        return inner.new(receiver)
    end
    return mod, count
end

-- Fake L1 so a test can drop it without waiting POS_TTL_SEC.
local function new_fake_l1()
    local store = {}
    local c = {}
    function c:get(k) return store[k] end
    function c:set(k, v) store[k] = v end
    function c:delete(k) store[k] = nil end
    function c:_clear() store = {} end
    return c
end

describe("#497 route freshness: L2 serves the happy path only while fresh", function()
    local shared, l1
    local TID = "tid-497"
    -- Frozen clock. The failure evidence no longer carries a timestamp (it is an atomic
    -- generation counter — see backend.lua), so nothing here depends on the clock any
    -- more; it stays frozen and fractional so that any future code reading ngx.now()
    -- cannot reintroduce a wall-clock dependency that only shows up as flake. An integer
    -- stand-in previously hid a real bug: the timestamp went through a helper that
    -- rejects non-integers, so every waiter 503'd in production while these stayed green.
    local fake_now = 1000.25

    before_each(function()
        helper.reset_ngx()
        fake_now = 1000.25
        ngx.now = function() return fake_now end
        shared = helper.new_fake_shared_dict()
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())
        -- No init_worker(): its only job is building the real resty.lrucache,
        -- which we replace immediately anyway. Skipping it keeps this spec free
        -- of the LuaJIT-only ffi dependency, which is what the _set_l1 seam is for.
        l1 = new_fake_l1()
        backend._set_l1(l1)
    end)

    -- Bring the caches to "filled from Redis with `host`" and then simulate
    -- POS_TTL_SEC having passed (L1 gone, L2 blob still there but not fresh).
    local function warm_then_age(host, port)
        ngx._fake_redis = { mode = "hit", value = desc_json(host, port, "172.16.0.2") }
        local d, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(backend.SOURCE_L3, src)
        assert.are.equal(host, d.host)
        l1:_clear()               -- L1 TTL (5s) elapsed
        shared:delete("f:" .. TID)  -- freshness marker expired; blob still in L2
    end

    -- Make a real lookup observe a Redis transport failure, which is what leaves the
    -- shared "endpoint is unhealthy" evidence a lock-timeout waiter needs. Deliberately
    -- not hand-planting the marker key: this also pins that the error path writes it.
    -- Leaves the cache exactly as it was (the error path writes nothing else).
    local function observe_redis_error()
        ngx._fake_redis = { mode = "error", err = "connect refused" }
        local _, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(backend.SOURCE_STATIC, src, "setup: this lookup must hit the error path")
        l1:_clear()
    end

    -- The only shape that legitimately authorises a waiter: the holder fails WHILE we are
    -- queued, so the evidence is stamped later than our wait began. Time advances because
    -- a failing attempt costs at least its socket timeouts. The literal key and value
    -- shape are anchored to the production writer by the "stamps the evidence" test below.
    local function holder_fails_during_wait()
        return timeout_lock_module(function()
            local gen = shared:incr("e:" .. TID, 1, 0)
            shared:set("e:" .. TID, gen, backend._ERR_TTL_SEC)
        end)
    end

    it("serves the NEW route after the freshness window, not the 60s-old L2 value", function()
        warm_then_age("10.0.1.5", 10042)
        -- host-agent moved the tenant; Redis is authoritative and healthy.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }

        local d, src, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.is_nil(err)
        assert.are.equal(backend.SOURCE_L3, src,
            "aged L2 must not answer while Redis is healthy (old behaviour: SOURCE_L2)")
        assert.are.equal("10.0.9.9", d.host)
        assert.are.equal(10077, d.port)
    end)

    it("never routes to a host:port the stale entry points at after port reuse", function()
        -- The no-cross-tenant shape from the issue: the old DNAT port has been
        -- handed to ANOTHER tenant's VM, so the upstream would connect fine and
        -- the balancer would never call invalidate().
        warm_then_age("10.0.1.5", 10042)
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10099, "172.16.0.7") }

        local d, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(backend.SOURCE_L3, src)
        assert.are_not.equal(10042, d.port, "must never serve the reused port from a stale entry")
        assert.are.equal(10099, d.port)
    end)

    it("still serves a FRESH L2 entry without touching Redis (stampede coalescing kept)", function()
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10042, "172.16.0.2") }
        local _, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(backend.SOURCE_L3, src)
        l1:_clear()  -- another worker: no L1, but L2 was just filled → still fresh

        -- If Redis were consulted we would see this value instead.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.9.9.9", 19999, "172.16.0.9") }
        local d2, src2 = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(backend.SOURCE_L2, src2)
        assert.are.equal("10.0.1.5", d2.host, "fresh L2 hit must not re-read Redis")
    end)

    it("keeps fail-static: an aged L2 entry answers when Redis is unreachable", function()
        warm_then_age("10.0.1.5", 10042)
        ngx._fake_redis = { mode = "error", err = "connect refused" }

        local d, src, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.is_nil(err, "fail-static must not 503 while a usable entry exists")
        assert.are.equal(backend.SOURCE_STATIC, src)
        assert.are.equal("10.0.1.5", d.host)
    end)

    it("keeps the negative shield: an L2 negative answers 404 without reading Redis", function()
        ngx._fake_redis = { mode = "miss" }
        local _, src, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(404, err)
        assert.are.equal(backend.SOURCE_NEG, src)
        l1:_clear()  -- neg still in L2 under its own 2s TTL

        -- A scanner's next hit must still be shielded, so this must NOT surface.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10042, "172.16.0.2") }
        local d2, src2, err2 = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(404, err2)
        assert.are.equal(backend.SOURCE_NEG, src2)
        assert.is_nil(d2)
    end)

    it("invalidate() clears the freshness marker too (next lookup re-reads Redis)", function()
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10042, "172.16.0.2") }
        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        backend.invalidate(shared, TID)
        assert.is_nil(shared:get("f:" .. TID), "stale marker would outlive the blob it describes")

        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }
        local d, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(backend.SOURCE_L3, src)
        assert.are.equal("10.0.9.9", d.host)
    end)

    -- ── 以下来自 Codex 跨模型 review 的 6 条发现(全部复现属实) ──

    it("does not re-arm L1 from an L2 hit (window stays POS_TTL, not 2x)", function()
        -- Codex finding 1: promoting an L2 hit into L1 with a full POS_TTL_SEC
        -- stacks the two windows — an L2 hit at t=4.9s would serve until t≈9.9s.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10042, "172.16.0.2") }
        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        l1:_clear()

        local _, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(backend.SOURCE_L2, src)
        assert.is_nil(l1:get(TID), "an L2 hit must not create an L1 entry with a fresh full TTL")

        -- Once the marker expires the very next request must go to Redis.
        shared:delete("f:" .. TID)
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }
        local d3, src3 = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(backend.SOURCE_L3, src3)
        assert.are.equal("10.0.9.9", d3.host)
    end)

    it("does not stamp an older blob fresh when the L2 write fails", function()
        -- Codex finding 2: marker written unconditionally → a failed blob write
        -- leaves the PREVIOUS route looking fresh, i.e. routes to the old endpoint.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10042, "172.16.0.2") }
        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        l1:_clear()
        shared:delete("f:" .. TID)  -- aged: old blob present, marker gone

        local real_set = shared.set
        shared.set = function(self, k, v, ttl)
            if k == "r:" .. TID then return false, "no memory", false end
            return real_set(self, k, v, ttl)
        end
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }
        local d, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        shared.set = real_set

        assert.are.equal(backend.SOURCE_L3, src)
        assert.are.equal("10.0.9.9", d.host)  -- this request itself is fine
        assert.is_nil(shared:get("f:" .. TID),
            "a failed blob write must not leave a marker that makes the OLD blob fresh")
        -- Codex round-7 finding: the blob itself must go too. Redis handed us a newer
        -- value, so the survivor is proven obsolete and must not remain as fail-static
        -- material for a later outage — by then its host:port may be another tenant's.
        assert.is_nil(shared:get("r:" .. TID),
            "a proven-obsolete blob must not survive as fail-static material")
    end)

    it("the L1 TTL jitter never pushes an entry past POS_TTL_SEC", function()
        -- Codex round-7 finding: jitter was ±0.5s, so L1 could live 5.5s while the L2
        -- marker expired at 5s — the window this issue pins was really 5.5s.
        local seen_below = false
        for _ = 1, 200 do
            local ttl = backend._pos_ttl_jitter()
            assert.is_true(ttl <= backend._POS_TTL_SEC,
                "jitter must not exceed the pinned window, got " .. tostring(ttl))
            assert.is_true(ttl > backend._POS_TTL_SEC - 0.6, "jitter must stay small")
            if ttl < backend._POS_TTL_SEC - 0.05 then seen_below = true end
        end
        assert.is_true(seen_below, "jitter must still spread expiry, not collapse to a constant")
    end)

    it("the single-flight lock waits longer than the holder's warm Redis budget", function()
        -- Codex round-7 finding: a flat 0.2s lock wait was SHORTER than the 300ms warm
        -- budget of one get_route(), so at the start of an outage every waiter gave up
        -- before the holder could publish its failure evidence and they all 503'd
        -- instead of falling back to fail-static.
        assert.is_true(backend._LOCK_TIMEOUT_SEC * 1000 > redis_client.WARM_BUDGET_MS,
            "lock wait " .. tostring(backend._LOCK_TIMEOUT_SEC * 1000) ..
            "ms must exceed the warm Redis budget " .. tostring(redis_client.WARM_BUDGET_MS) .. "ms")
    end)

    it("the lock lease outlives a DNS-stalled holder, so two writers cannot overlap", function()
        -- Codex round-8 finding: the endpoint is a DNS name and a cold resolve is
        -- budgeted separately (resolver_timeout 3s), so a holder can legitimately work
        -- far longer than the warm budget. With a 1s lease it lost the lock mid-flight,
        -- a second worker entered, and the two writes could land out of order.
        assert.is_true(backend._LOCK_EXPTIME_SEC * 1000 > redis_client.COLD_CEILING_MS,
            "lease " .. tostring(backend._LOCK_EXPTIME_SEC * 1000) ..
            "ms must outlast a cold DNS resolve " .. tostring(redis_client.COLD_CEILING_MS) .. "ms")
        assert.is_true(backend._LOCK_EXPTIME_SEC > backend._LOCK_TIMEOUT_SEC,
            "and a waiter must give up long before the lease does")
    end)

    it("lock timeout after an observed Redis failure serves the aged entry, not 503", function()
        -- Codex finding 3: the freshness gate turned this branch's "serve stale"
        -- into a 503 — fail-static becoming a failure under contention.
        warm_then_age("10.0.1.5", 10042)
        backend._set_lock_module(holder_fails_during_wait())

        local d, src, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.is_nil(err, "fail-static must survive the lock-timeout path")
        assert.are.equal(backend.SOURCE_STATIC, src,
            "an aged value must be reported as stale, not as a fresh L2 hit")
        assert.are.equal("10.0.1.5", d.host)
    end)

    it("lock contention with a healthy Redis fails closed instead of serving the aged route", function()
        -- Codex round-5 finding: losing a 0.2s lock race is not evidence Redis is
        -- broken. Serving an up-to-60s-old route on contention alone would route to a
        -- host:port that may already belong to another tenant's VM — the very bug this
        -- issue removes, just moved onto the contention path.
        warm_then_age("10.0.1.5", 10042)
        backend._set_lock_module(timeout_lock_module())

        local d, _, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(503, err, "no observed failure → must not serve the aged route")
        assert.is_nil(d)
    end)

    it("one tenant's Redis failure does not authorise serving another tenant's aged route", function()
        -- Codex round-6 finding: with endpoint-wide evidence, an error on tenant A let a
        -- lock-timeout waiter on tenant B serve B's aged route — and B's aged host:port
        -- may already belong to a third tenant's VM. no-cross-tenant outranks
        -- availability, so the evidence is per tenant.
        local OTHER = "tid-497-other"
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.2.7", 10055, "172.16.0.3") }
        backend.lookup_backend(shared, OTHER, "127.0.0.1", 6379)
        l1:_clear()
        shared:delete("f:" .. OTHER)     -- OTHER now has an aged blob

        observe_redis_error()            -- the failure is observed for TID, not OTHER
        backend._set_lock_module(timeout_lock_module())

        local d, _, err = backend.lookup_backend(shared, OTHER, "127.0.0.1", 6379)

        assert.are.equal(503, err, "another tenant's failure must not unlock OTHER's aged route")
        assert.is_nil(d)
    end)

    it("the lock holder always probes Redis, never backs off on the evidence", function()
        -- Cross-model review pressed for a holder-side backoff (fewer doomed connections
        -- during a brownout) and then, holding the opposite position, for its removal.
        -- Resolved by the project's priority order: a backoff suppresses re-reads, so
        -- after Redis recovers it keeps serving an aged blob whose host:port may already
        -- be another tenant's — a cross-tenant window bb does not have today, traded for
        -- a load problem it already has. no-cross-tenant is non-negotiable, so the holder
        -- probes every time and the storm is recorded as a separate follow-up.
        warm_then_age("10.0.1.5", 10042)
        observe_redis_error()  -- evidence is present for this tenant
        local counting, calls = counting_redis_module()
        redis_client._set_redis_module(counting)

        -- Redis has recovered. A backing-off holder would answer 10.0.1.5 from L2.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }
        local d, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(1, calls.n, "existing evidence must not stop the holder probing")
        assert.are.equal(backend.SOURCE_L3, src)
        assert.are.equal("10.0.9.9", d.host, "recovery must be visible at once, not after a TTL")
    end)

    it("evidence from BEFORE this wait does not authorise serving the aged route", function()
        -- Codex round-10 finding: `e:<tid>` records a past failure, not the outcome of the
        -- attempt this waiter is queued behind. If Redis recovered in between, merely
        -- "recent" evidence would still hand out a route up to L2_TTL_SEC old.
        warm_then_age("10.0.1.5", 10042)
        observe_redis_error()   -- failure observed at t=1000.25
        fake_now = 1001.75      -- this waiter only starts waiting later
        backend._set_lock_module(timeout_lock_module())

        local d, _, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(503, err, "stale evidence must not authorise a stale route")
        assert.is_nil(d)
    end)

    it("evidence stays visible WHILE a probe is in flight (waiters not stranded)", function()
        -- Codex round-14 finding: an earlier attempt at retracting the evidence at the
        -- START of each probe stranded every waiter queued behind that holder during a
        -- real outage — they timed out mid-probe, found nothing and 503'd even though L2
        -- held a usable entry. Retraction must happen only after a COMPLETED response.
        -- Asserted by looking at the marker from inside the Redis call itself.
        warm_then_age("10.0.1.5", 10042)
        observe_redis_error()   -- an outage is under way; evidence is standing

        local seen_during_probe
        local inner = helper.new_fake_redis_module()
        local probing = {}
        function probing.new(receiver)
            local client = inner.new(receiver)
            local real_get = client.get
            client.get = function(c, k)
                seen_during_probe = shared:get("e:" .. TID)
                return real_get(c, k)
            end
            return client
        end
        redis_client._set_redis_module(probing)
        -- Redis answers this probe (so the call actually reaches get()), which is also the
        -- case that retracts the evidence — pinning that retraction happens AFTER, not
        -- BEFORE, the response.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }

        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.is_not_nil(seen_during_probe,
            "a waiter timing out mid-probe must still find the evidence, or fail-static dies")
        assert.is_nil(shared:get("e:" .. TID), "…and a completed success must retract it")
    end)

    it("evidence does not survive into a successor holder's probe", function()
        -- Codex round-13 finding: resty.lock is not fair. An earlier holder can fail and
        -- stamp the evidence, a NEW healthy-but-slow holder can then take the lock, and a
        -- waiter queued behind that healthy holder would still be authorised by the older
        -- failure — handing out an aged route while Redis actually works. Clearing the
        -- marker at the START of every lock-held probe makes "evidence exists" mean
        -- exactly "the most recently completed probe failed".
        warm_then_age("10.0.1.5", 10042)
        observe_redis_error()                       -- earlier holder failed, marker set
        assert.is_not_nil(shared:get("e:" .. TID), "setup: the failure must have stamped it")

        -- A successor holder probes a recovered Redis. Its probe must retract the marker.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }
        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.is_nil(shared:get("e:" .. TID),
            "a successor probe must not leave the earlier holder's evidence standing")

        -- So a waiter behind that healthy holder gets no authorisation and fails closed.
        l1:_clear()
        shared:delete("f:" .. TID)
        backend._set_lock_module(timeout_lock_module())
        local d, _, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(503, err, "no completed failure → must not serve the aged route")
        assert.is_nil(d)
    end)

    it("a successful Redis read retracts the failure evidence immediately", function()
        -- Codex round-6 finding: relying on ERR_TTL_SEC to expire left a window where
        -- Redis was demonstrably healthy again but waiters still served aged routes.
        warm_then_age("10.0.1.5", 10042)
        observe_redis_error()
        shared:delete("e:" .. TID)  -- ERR_TTL_SEC lapsed: the half-open probe is allowed

        -- Redis recovers; this lookup proves reachability and refills the cache.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }
        local _, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(backend.SOURCE_L3, src)

        -- Age it again and lose the lock race: without retraction this would still
        -- serve the aged value under the stale evidence.
        l1:_clear()
        shared:delete("f:" .. TID)
        backend._set_lock_module(timeout_lock_module())
        local d2, _, err2 = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(503, err2, "evidence must not survive a proven-healthy read")
        assert.is_nil(d2)
    end)

    it("lock timeout with nothing cached still 503s (unchanged from before #497)", function()
        backend._set_lock_module(timeout_lock_module())
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10042, "172.16.0.2") }

        local d, _, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(503, err, "no cached answer and no Redis access left → 503")
        assert.is_nil(d)
    end)

    it("every failure bumps the evidence generation (and a success clears it)", function()
        -- Anchors the literal "e:<tid>" key and the value semantics the reader depends on,
        -- so the other waiter tests may drive the counter directly without drifting from
        -- production. A value that did not change per failure would silently disable
        -- fail-static: the waiter's "changed since I started waiting" test could never pass.
        warm_then_age("10.0.1.5", 10042)
        observe_redis_error()
        local g1 = shared:get("e:" .. TID)
        assert.are.equal("number", type(g1), "evidence must be a generation counter")

        observe_redis_error()
        local g2 = shared:get("e:" .. TID)
        assert.is_true(g2 > g1, "each failure must change the value, got " ..
            tostring(g1) .. " → " .. tostring(g2))

        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }
        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.is_nil(shared:get("e:" .. TID), "a successful read must retract the evidence")
    end)

    it("a failure that predates this wait does not authorise stale routing", function()
        -- Codex rounds 12/16: what authorises a waiter must be the outcome of the attempt
        -- it is actually queued behind, not "there was a failure at some point". Expressed
        -- with the generation counter this is exact and clock-free: sampled-before ==
        -- read-after means nothing failed during our wait, even if an older failure left
        -- evidence standing. (With timestamps this case was ambiguous whenever both landed
        -- in the same cached ngx.now() tick.)
        warm_then_age("10.0.1.5", 10042)
        observe_redis_error()   -- bumps the generation BEFORE this waiter starts waiting
        assert.is_not_nil(shared:get("e:" .. TID), "setup: evidence must be standing")
        backend._set_lock_module(timeout_lock_module())  -- times out, generation unchanged

        local d, _, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(503, err, "an older failure must not authorise a stale route")
        assert.is_nil(d)
    end)

    it("an authoritative Redis miss drops the cached positive (no deleted-tenant route)", function()
        -- Codex round-2 finding: put_negative left `r:<tid>` in place. Sequence
        -- [positive cached] → [tenant deleted, Redis miss] → [2s negative expires]
        -- → [Redis unreachable] then served the deleted tenant's host:port through
        -- fail-static, and that port may already belong to another tenant's VM.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10042, "172.16.0.2") }
        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        l1:_clear()
        shared:delete("f:" .. TID)  -- aged, blob still in L2

        ngx._fake_redis = { mode = "miss" }  -- tenant deleted; Redis is authoritative
        local _, src, err = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.are.equal(404, err)
        assert.are.equal(backend.SOURCE_NEG, src)
        assert.is_nil(shared:get("r:" .. TID), "an authoritative miss must drop the cached blob")
        assert.is_nil(shared:get("f:" .. TID), "…and its freshness marker")

        -- negative expires, Redis goes down: must NOT resurrect the old route.
        l1:_clear()
        shared:delete("n:" .. TID)
        ngx._fake_redis = { mode = "error", err = "connect refused" }
        local d2, _, err2 = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        assert.is_nil(d2, "fail-static must not serve a route Redis said does not exist")
        assert.are.equal(503, err2)
    end)

    it("reads the freshness marker before the blob (interleaving cannot pair old blob + new marker)", function()
        -- Codex round-2 finding: with blob-then-marker, a concurrent put_positive
        -- landing between the two gets makes an aged blob pass the gate. Assert the
        -- order structurally — that is the property that makes the race benign.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.1.5", 10042, "172.16.0.2") }
        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        l1:_clear()

        local seen = {}
        local real_get = shared.get
        shared.get = function(self, k)
            seen[#seen + 1] = k
            return real_get(self, k)
        end
        backend.lookup_backend(shared, TID, "127.0.0.1", 6379)
        shared.get = real_get

        local i_fresh, i_blob
        for i, k in ipairs(seen) do
            if k == "f:" .. TID and not i_fresh then i_fresh = i end
            if k == "r:" .. TID and not i_blob then i_blob = i end
        end
        assert.is_not_nil(i_fresh, "marker must be consulted; saw: " .. table.concat(seen, ","))
        assert.is_not_nil(i_blob)
        assert.is_true(i_fresh < i_blob,
            "marker must be read BEFORE the blob; saw: " .. table.concat(seen, ","))
    end)

    it("a lock-timeout waiter opens no Redis connection and mutates no cache state", function()
        -- Codex round-3 finding: the previous fix for finding 3 sent the waiter to
        -- Redis, i.e. cache-mutating lookups OUTSIDE the per-tenant lock. Two unlocked
        -- lookups can finish out of order and the later write may carry the OLDER
        -- value — overwriting a newer route and re-arming its marker. It also broke
        -- single-flight: every timed-out waiter would connect to Redis. The waiter now
        -- answers from the cache alone, so both properties are one assertion each.
        warm_then_age("10.0.1.5", 10042)
        local blob_before = shared:get("r:" .. TID)
        local counting, calls = counting_redis_module()
        redis_client._set_redis_module(counting)
        backend._set_lock_module(holder_fails_during_wait())

        -- If Redis were consulted the waiter would surface this newer value instead.
        ngx._fake_redis = { mode = "hit", value = desc_json("10.0.9.9", 10077, "172.16.0.2") }
        local d, src = backend.lookup_backend(shared, TID, "127.0.0.1", 6379)

        assert.are.equal(backend.SOURCE_STATIC, src)
        assert.are.equal("10.0.1.5", d.host, "the waiter must not reach Redis (single-flight)")
        assert.are.equal(0, calls.n,
            "a waiter must not open a Redis connection outside the lock")
        assert.are.equal(blob_before, shared:get("r:" .. TID),
            "a waiter must not rewrite the L2 blob outside the lock")
        assert.is_nil(shared:get("f:" .. TID),
            "a waiter must not re-arm the freshness marker outside the lock")
        assert.is_nil(shared:get("n:" .. TID),
            "…nor publish a negative other workers would trust")
    end)

    it("the failure-evidence window outlives a lock wait but stays inside the freshness budget", function()
        -- Lower bound is load-bearing: the holder writes the evidence and the waiter reads
        -- it up to LOCK_TIMEOUT_SEC later, so a shorter window would silently kill the
        -- fail-static path (waiters would always 503 during a real outage). Upper bound is
        -- hygiene: evidence must not linger long enough to describe an attempt from some
        -- earlier incident.
        assert.is_true(backend._ERR_TTL_SEC > backend._LOCK_TIMEOUT_SEC,
            "evidence " .. tostring(backend._ERR_TTL_SEC) .. "s must outlive a lock wait " ..
            tostring(backend._LOCK_TIMEOUT_SEC) .. "s or waiters can never use it")
        assert.is_true(backend._ERR_TTL_SEC < backend._POS_TTL_SEC,
            "…and must stay inside the freshness budget")
    end)

    it("the pinned window is POS_TTL_SEC, not L2_TTL_SEC", function()
        -- Guards the rationale in backend.lua's header: if someone widens the
        -- happy-path window back to the fail-static one, this reads wrong.
        assert.are.equal(5, backend._POS_TTL_SEC)
        assert.is_true(backend._L2_TTL_SEC > backend._POS_TTL_SEC,
            "L2 must stay the longer fail-static window, not the freshness one")
    end)
end)
