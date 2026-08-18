-- deploy/edge/.luacheckrc — production Lua strict; tests get busted DSL globals.
std = "max+busted"
globals = { "ngx" }

files["test/"] = {
    -- Test helpers use idiomatic method shape (dict:get) with unused self
    -- receivers by design. Silence that narrowly for tests only.
    ignore = { "212/self" },  -- unused argument 'self'
}

files["fluent-bit/"] = {
    -- Fluent Bit Lua FILTER calls the entrypoint by NAME from fluent-bit.conf,
    -- so it MUST be a global function (not local) — and its signature is fixed
    -- by Fluent Bit's calling convention (tag, timestamp, record), some args
    -- go unused. Silence those two narrowly for the fluent-bit filter dir only.
    -- extract_trace_root: edge/ filter; extract_tenant_id: host/ vm-log filter (#245);
    -- add_timestamp: both roles, stamps @timestamp for the Console query (#265).
    globals = { "extract_trace_root", "extract_tenant_id", "add_timestamp" },  -- 111: global entrypoint
    ignore = { "212" },  -- unused argument (tag/timestamp per FB filter contract)
}
