-- deploy/edge/lib/hints.lua
--
-- Redis coordinates handed from nginx.conf's `init_by_lua_block` to route.lua's
-- warmup probe. The probe runs in `init_worker`, where `ngx.var` does not exist,
-- so it cannot read the `$edge_redis_*` vars the server block sets.
--
-- Why not `os.getenv` (#639): nginx wipes the worker environment except TZ
-- unless every name is declared with the `env` directive
-- (https://nginx.org/en/docs/ngx_core_module.html#env). `claw-edge.service`'s
-- four `Environment=ENGINE_REDIS_*_HINT` lines therefore never reached
-- `route.lua` — `os.getenv` returned nil, the probe took its "no coordinates"
-- branch and marked the instance ready WITHOUT ever proving Redis reachable.
-- #618's readiness gate was permanently fail-open, and silently so: one
-- `[warn]` line, `/healthz` 200, no metric.
--
-- These coordinates already flow into nginx.conf through install-edge.sh's
-- single `envsubst` call — the same one that renders `$edge_redis_*` — so this
-- rides that ONE channel. Declaring `env` directives instead would have kept
-- two places that must agree, which is exactly the drift that produced #639.

local _M = { _VERSION = "0.01" }

local coords = nil

-- An UNRENDERED placeholder is worse than a missing value: it is a non-empty
-- string, so every "is it set?" check passes and the probe dials a host literally
-- named "${ENGINE_REDIS_HOST}". Treat anything still starting with `$` as absent
-- so a template that never went through envsubst fails loudly instead of
-- silently probing garbage.
local function clean(v)
    if type(v) ~= "string" or v == "" then return nil end
    if v:find("^%$") then return nil end
    return v
end

function _M.set(t)
    t = t or {}
    coords = {
        primary_host = clean(t.primary_host),
        primary_port = tonumber(clean(t.primary_port)),
        reader_host  = clean(t.reader_host),
        reader_port  = tonumber(clean(t.reader_port)),
    }
end

-- Returns nil until `init_by_lua_block` has run, so a caller can tell "the block
-- is missing from nginx.conf" (the #639 shape) from "the block ran but the
-- values were blank". Both are misconfigurations; neither may mark ready.
function _M.get()
    return coords
end

return _M
