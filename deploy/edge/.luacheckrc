-- deploy/edge/.luacheckrc — production Lua strict; tests get busted DSL globals.
std = "max+busted"
globals = { "ngx" }

files["test/"] = {
    -- Test helpers use idiomatic method shape (dict:get) with unused self
    -- receivers by design. Silence that narrowly for tests only.
    ignore = { "212/self" },  -- unused argument 'self'
}
