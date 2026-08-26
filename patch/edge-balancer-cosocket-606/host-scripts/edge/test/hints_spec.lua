-- deploy/edge/test/hints_spec.lua
--
-- edge.lib.hints — the coordinate channel that replaced os.getenv (#639).
--
-- These are unit-level guards on the PARSING only. They cannot prove the channel
-- itself works: that needs a real OpenResty with a real rendered nginx.conf, and
-- lives in test/integration/balancer_phase_integration.sh (ARM W + the two static
-- checks). #633's lesson applies here — a fake-module unit test went green while
-- the real phase was red, so treat this file as the cheap half only.

local hints = require "edge.lib.hints"

local function fresh()
    package.loaded["edge.lib.hints"] = nil
    return require "edge.lib.hints"
end

describe("edge.lib.hints", function()
    it("returns nil until init_by_lua_block ran (the #639 shape)", function()
        -- An nginx.conf without the init_by_lua_block leaves the module unset.
        -- Callers must be able to tell that apart from "set but blank", because
        -- both are misconfigurations that must NOT mark the instance ready.
        assert.is_nil(fresh().get())
    end)

    it("parses rendered coordinates and coerces ports to numbers", function()
        local m = fresh()
        m.set({
            primary_host = "redis.example",
            primary_port = "6379",
            reader_host  = "redis-ro.example",
            reader_port  = "6380",
        })
        local c = m.get()
        assert.are.equal("redis.example", c.primary_host)
        assert.are.equal(6379, c.primary_port)
        assert.are.equal("redis-ro.example", c.reader_host)
        assert.are.equal(6380, c.reader_port)
    end)

    it("treats an UNRENDERED placeholder as absent, not as a hostname", function()
        -- The trap this guard exists for: envsubst never ran, so the value is the
        -- literal template. It is non-empty, so every "is it set?" check passes and
        -- the warmup probe would dial a host named "${ENGINE_REDIS_HOST}" — a
        -- failure that looks like a Redis outage instead of a render bug.
        local m = fresh()
        m.set({
            primary_host = "${ENGINE_REDIS_HOST}",
            primary_port = "${ENGINE_REDIS_PORT}",
            reader_host  = "$ENGINE_REDIS_READER_HOST",
            reader_port  = "$ENGINE_REDIS_READER_PORT",
        })
        local c = m.get()
        assert.is_nil(c.primary_host)
        assert.is_nil(c.primary_port)
        assert.is_nil(c.reader_host)
        assert.is_nil(c.reader_port)
    end)

    it("treats blank values as absent", function()
        local m = fresh()
        m.set({ primary_host = "", reader_host = "", primary_port = "", reader_port = "" })
        local c = m.get()
        assert.is_nil(c.primary_host)
        assert.is_nil(c.reader_host)
    end)

    it("keeps primary when only the reader coordinate is missing", function()
        -- Rolling-upgrade shape: warmup falls back to primary, so primary must
        -- survive a blank reader rather than the whole table being discarded.
        local m = fresh()
        m.set({ primary_host = "redis.example", primary_port = "6379", reader_host = "" })
        local c = m.get()
        assert.are.equal("redis.example", c.primary_host)
        assert.is_nil(c.reader_host)
    end)

    it("drops a non-numeric port so the caller can apply its own default", function()
        local m = fresh()
        m.set({ primary_host = "redis.example", primary_port = "not-a-port" })
        assert.is_nil(m.get().primary_port)
    end)

    it("does not blow up when init_by_lua_block passes nothing", function()
        local m = fresh()
        m.set(nil)
        local c = m.get()
        assert.is_table(c)
        assert.is_nil(c.primary_host)
    end)
end)

-- Keep the module loaded for any later spec that requires it.
package.loaded["edge.lib.hints"] = hints
