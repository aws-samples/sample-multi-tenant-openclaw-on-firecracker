-- deploy/edge/test/backend_spec.lua
--
-- lookup_backend — three-tier cache + fail-static coverage for
-- the "route.lua lookup_backend" path.

local helper = require "spec_helper"
local backend = require "edge.lib.backend"
local redis_client = require "edge.lib.redis_client"
local cjson = require "cjson.safe"

local function fake_desc_json(host, port, guest_ip)
    return cjson.encode({
        host = host, port = port, guest_ip = guest_ip,
        updated_at = os.time(),
    })
end

describe("backend.lookup_backend", function()
    local shared
    before_each(function()
        helper.reset_ngx()
        shared = helper.new_fake_shared_dict()
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())
        assert.is_true(backend.init_worker())
    end)

    it("returns SOURCE_L3 on cold miss with Redis hit", function()
        ngx._fake_redis = {
            mode = "hit",
            value = fake_desc_json("10.0.1.5", 10042, "172.16.0.6"),
        }
        local desc, source, err = backend.lookup_backend(
            shared, "tid-1", "127.0.0.1", 6379)
        assert.is_nil(err)
        assert.are.equal(backend.SOURCE_L3, source)
        assert.are.equal("10.0.1.5", desc.host)
        assert.are.equal(10042, desc.port)
        assert.are.equal("172.16.0.6", desc.guest_ip)
    end)

    it("promotes L3 hit into L1/L2 (next call is L1)", function()
        ngx._fake_redis = {
            mode = "hit",
            value = fake_desc_json("10.0.1.5", 10042, "172.16.0.6"),
        }
        backend.lookup_backend(shared, "tid-2", "127.0.0.1", 6379)
        -- Second call: Redis unplugged; still must serve from L1.
        redis_client._set_redis_module(helper.new_fake_redis_module())
        ngx._fake_redis = { mode = "error", err = "unreachable" }
        local desc, source, err = backend.lookup_backend(
            shared, "tid-2", "127.0.0.1", 6379)
        assert.is_nil(err)
        assert.are.equal(backend.SOURCE_L1, source)
        assert.are.equal("10.0.1.5", desc.host)
    end)

    it("negative-caches clean Redis miss and returns 404 fast on retry", function()
        ngx._fake_redis = { mode = "miss" }
        local d, s, err = backend.lookup_backend(shared, "unknown", "127.0.0.1", 6379)
        assert.is_nil(d)
        assert.are.equal(404, err)
        assert.are.equal(backend.SOURCE_NEG, s)

        -- Second call must not hit Redis again — swap in an errored module
        -- and verify it never gets called (still returns 404).
        redis_client._set_redis_module({
            new = function() error("redis should not be called on neg-cache hit") end,
        })
        local d2, _, err2 = backend.lookup_backend(shared, "unknown", "127.0.0.1", 6379)
        assert.is_nil(d2)
        assert.are.equal(404, err2)
    end)

    it("fail-static: Redis error + L2 stale value → serve stale", function()
        -- Seed L2 with a valid descriptor by doing a hit first.
        ngx._fake_redis = {
            mode = "hit",
            value = fake_desc_json("10.0.9.9", 10099, "172.16.9.10"),
        }
        backend.lookup_backend(shared, "tid-stale", "127.0.0.1", 6379)

        -- Simulate worker rotation: rebuild L1 to force L2 lookup.
        assert.is_true(backend.init_worker())
        -- Redis now broken; L2 still hot.
        ngx._fake_redis = { mode = "error", err = "brownout" }
        local desc, source, err = backend.lookup_backend(
            shared, "tid-stale", "127.0.0.1", 6379)
        assert.is_nil(err)
        -- After init_worker wiped L1, L2 hit — not fail-static path yet.
        assert.are.equal(backend.SOURCE_L2, source)
        assert.are.equal("10.0.9.9", desc.host)
    end)

    it("fail-static: Redis error + NO L2 → 503", function()
        ngx._fake_redis = { mode = "error", err = "brownout" }
        local d, s, err = backend.lookup_backend(
            shared, "never-seen", "127.0.0.1", 6379)
        assert.is_nil(d)
        assert.are.equal(503, err)
        assert.are.equal(backend.SOURCE_STATIC, s)
    end)

    it("malformed Redis value (missing host) → 404 non-cached (no neg poison)", function()
        ngx._fake_redis = {
            mode = "hit",
            value = cjson.encode({ port = 10042, guest_ip = "1.2.3.4" }),
        }
        local d, s, err = backend.lookup_backend(shared, "broken", "127.0.0.1", 6379)
        assert.is_nil(d)
        assert.are.equal(404, err)
        assert.are.equal(backend.SOURCE_NEG, s)
    end)

    it("malformed Redis value (non-JSON garbage) → 404", function()
        ngx._fake_redis = { mode = "hit", value = "not-json {{{" }
        local d, _, err = backend.lookup_backend(shared, "junk", "127.0.0.1", 6379)
        assert.is_nil(d)
        assert.are.equal(404, err)
    end)

    it("rejects non-integer port field", function()
        local desc, perr = backend._parse_value(
            '{"host":"10.0.0.1","port":"not-a-number","guest_ip":"172.16.0.2"}')
        assert.is_nil(desc)
        assert.is_string(perr)
    end)

    it("positive TTL jitter stays within ±0.5s of nominal", function()
        for _ = 1, 100 do
            local t = backend._pos_ttl_jitter()
            assert.is_true(t >= backend._POS_TTL_SEC - 0.5)
            assert.is_true(t <= backend._POS_TTL_SEC + 0.5)
        end
    end)
end)
