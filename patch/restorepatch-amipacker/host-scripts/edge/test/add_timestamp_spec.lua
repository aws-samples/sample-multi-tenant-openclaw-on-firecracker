-- deploy/edge/test/add_timestamp_spec.lua
--
-- add_timestamp — the Fluent Bit filter that stamps @timestamp onto records so
-- the Console log query (console-bff/logs.mjs range+sort) can read the index.
-- Covers both event-time shapes, the idempotence guard, and the invariant that
-- the host/ and edge/ copies stay identical.

-- The filter is a Fluent Bit entrypoint: a global function in a script loaded by
-- name, not a module. Load the chunk and read the function off the environment
-- (a table field access, so luacheck sees no undefined global).
local FILTER_PATHS = {
    "fluent-bit/host/add_timestamp.lua",         -- busted run from deploy/edge/
    "deploy/edge/fluent-bit/host/add_timestamp.lua", -- ... or from repo root
}

local function load_filter()
    for _, path in ipairs(FILTER_PATHS) do
        local chunk = loadfile(path)
        if chunk then
            chunk()
            return _G["add_timestamp"], path
        end
    end
    error("add_timestamp.lua not found; looked in: " .. table.concat(FILTER_PATHS, ", "))
end

local function read_file(path)
    local fh = io.open(path, "rb")
    if not fh then return nil end
    local body = fh:read("*a")
    fh:close()
    return body
end

describe("add_timestamp", function()
    local add_timestamp, loaded_from

    setup(function()
        add_timestamp, loaded_from = load_filter()
    end)

    it("is a callable global entrypoint (Fluent Bit calls it by name)", function()
        assert.are.equal("function", type(add_timestamp))
    end)

    -- --- Event time as a table (time_as_table On, what the conf sets) --------
    it("stamps ISO8601 UTC with ms from a {sec,nsec} table", function()
        local rc, ts, record = add_timestamp(
            "host.vm", { sec = 1786603686, nsec = 506013000 }, { log = "x" })
        assert.are.equal(1, rc)
        assert.are.equal("2026-08-13T06:48:06.506Z", record["@timestamp"])
        -- rc=1 means "record changed, event time untouched" — ts passes through.
        assert.are.same({ sec = 1786603686, nsec = 506013000 }, ts)
    end)

    it("floors sub-millisecond nsec rather than rounding up", function()
        local _, _, record = add_timestamp(
            "host.vm", { sec = 1786603686, nsec = 999999 }, {})
        assert.are.equal("2026-08-13T06:48:06.000Z", record["@timestamp"])
    end)

    it("pads ms to three digits", function()
        local _, _, record = add_timestamp(
            "host.vm", { sec = 1786603686, nsec = 7000000 }, {})
        assert.are.equal("2026-08-13T06:48:06.007Z", record["@timestamp"])
    end)

    it("tolerates a table with no nsec", function()
        local _, _, record = add_timestamp("host.vm", { sec = 1786603686 }, {})
        assert.are.equal("2026-08-13T06:48:06.000Z", record["@timestamp"])
    end)

    -- --- Event time as a plain number (time_as_table Off / absent) ----------
    it("stamps from a float epoch when time_as_table is not set", function()
        local _, _, record = add_timestamp("edge.access", 1786603686.506, {})
        assert.are.equal("2026-08-13T06:48:06.506Z", record["@timestamp"])
    end)

    it("handles a whole-second number with no fraction", function()
        local _, _, record = add_timestamp("edge.access", 1786603686, {})
        assert.are.equal("2026-08-13T06:48:06.000Z", record["@timestamp"])
    end)

    it("keeps ms on the number path when the value is representable", function()
        local _, _, record = add_timestamp("edge.access", 1786603686.999, {})
        assert.are.equal("2026-08-13T06:48:06.999Z", record["@timestamp"])
    end)

    -- The next two pin *measured* double-precision behaviour of the number path,
    -- so nobody "fixes" them into something arithmetically impossible. At epoch
    -- magnitude a double has only ~6 fractional digits left, which is why the
    -- conf sets time_as_table On and gets exact integer nsec instead.
    it("number path: a literal within half a ms of the next second IS that second", function()
        -- 1786603686.9999999 is not representable; the nearest double is exactly
        -- 1786603687.0, so .000Z on the following second is the correct answer,
        -- not a rolled-over millisecond.
        local _, _, record = add_timestamp("edge.access", 1786603686.9999999, {})
        assert.are.equal("2026-08-13T06:48:07.000Z", record["@timestamp"])
    end)

    it("number path: sub-ms representation error can truncate the last ms", function()
        -- .001 lands at frac 0.000999928 → floor gives 0 ms. Documented, accepted:
        -- 1 ms on a log timestamp is immaterial, and the table path is exact.
        local _, _, record = add_timestamp("edge.access", 1786603686.001, {})
        assert.are.equal("2026-08-13T06:48:06.000Z", record["@timestamp"])
    end)

    -- --- Always UTC, regardless of the host's local zone -------------------
    it("emits UTC even when TZ is not UTC", function()
        -- os.date("!...") is UTC by contract; assert it so a future rewrite that
        -- drops the "!" cannot silently start writing local time into the index.
        local _, _, record = add_timestamp("host.vm", { sec = 0, nsec = 0 }, {})
        assert.are.equal("1970-01-01T00:00:00.000Z", record["@timestamp"])
    end)

    -- --- Idempotence -------------------------------------------------------
    it("keeps an @timestamp the pipeline already carries", function()
        local _, _, record = add_timestamp(
            "edge.access", { sec = 1786603686, nsec = 0 },
            { ["@timestamp"] = "2026-01-01T00:00:00.000Z" })
        assert.are.equal("2026-01-01T00:00:00.000Z", record["@timestamp"])
    end)

    it("leaves the rest of the record untouched", function()
        local _, _, record = add_timestamp(
            "host.vm", { sec = 1786603686, nsec = 0 },
            { tenant_id = "acme-1a2b", log_path = "/data/firecracker-vms/acme-1a2b/fc.log" })
        assert.are.equal("acme-1a2b", record["tenant_id"])
        assert.are.equal("/data/firecracker-vms/acme-1a2b/fc.log", record["log_path"])
    end)

    -- --- The two role copies must not drift --------------------------------
    it("ships byte-identical copies in host/ and edge/ (modulo the path header)", function()
        local base = loaded_from:gsub("host/add_timestamp%.lua$", "")
        local host_body = read_file(base .. "host/add_timestamp.lua")
        local edge_body = read_file(base .. "edge/add_timestamp.lua")
        assert.is_truthy(host_body, "host copy unreadable")
        assert.is_truthy(edge_body, "edge copy unreadable: install-fluent-bit.sh pulls " ..
            "one role prefix, so edge/ needs its own copy")
        -- Only the first line (the self-referencing path comment) may differ.
        local function drop_first_line(s) return s:gsub("^[^\n]*\n", "") end
        assert.are.equal(drop_first_line(host_body), drop_first_line(edge_body))
    end)
end)
