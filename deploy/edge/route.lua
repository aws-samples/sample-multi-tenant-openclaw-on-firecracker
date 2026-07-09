-- deploy/edge/route.lua
--
-- Public entry orchestrator for the OpenResty edge. Exposes three phases
-- that nginx.conf wires into rewrite / access / balancer / init_worker.
-- Each phase is a thin dispatcher — logic lives in edge.lib.*.
--
-- Shares a data contract with host-agent + the IaC layer.
--
-- Do not add business logic here. Keep this file the "entry only, routes to
-- domain" boundary.

local tenant_mod   = require "edge.lib.tenant"
local backend_mod  = require "edge.lib.backend"
local balancer_mod = require "edge.lib.balancer"
local redis_client = require "edge.lib.redis_client"

local _M = { _VERSION = "0.02" }

-- Warmup gate: /healthz reads this and returns 503 until the async probe
-- below succeeds. Prevents ASG rotating traffic into a cold instance
-- whose Redis connection has not yet been proven.
local function mark_ready()
    local flag = ngx.shared.edge_ready
    if flag then flag:set("ready", 1) end
end

-- warmup_probe: run once (on worker 0) to verify Redis reachability.
-- We deliberately keep this cheap — a single successful GET against a
-- known-empty key confirms TCP + Redis protocol + endpoint DNS resolves.
-- Any failure keeps the box out of ELB rotation; a later probe retries.
local function warmup_probe()
    local host = os.getenv("ENGINE_REDIS_HOST_HINT")
    -- Read the same variables nginx.conf sets on the server block. We do
    -- this via ngx.var when a request runs; at init_worker phase we have
    -- to fall back to the env var install-edge.sh exports.
    local port_str = os.getenv("ENGINE_REDIS_PORT_HINT")
    local port = tonumber(port_str) or 6379
    if not host or host == "" then
        -- Best-effort: without endpoint info we still must eventually flip
        -- ready so that /healthz doesn't stay 503 forever in tests or
        -- misconfigured deploys. Log loudly.
        ngx.log(ngx.WARN, "edge warmup: no ENGINE_REDIS_HOST_HINT; ",
            "marking ready without probe (misconfig?)")
        mark_ready()
        return
    end
    local _, err = redis_client.get_route(host, port, "route:__warmup__")
    if err then
        ngx.log(ngx.WARN, "edge warmup probe failed: ", tostring(err),
            "; will retry")
        return
    end
    mark_ready()
    ngx.log(ngx.NOTICE, "edge warmup ok; healthz now 200")
end

--[[
    on_init_worker: fills the per-worker lrucache. Called from
    init_worker_by_lua_file in nginx.conf. Any failure is fatal to route
    fidelity but not to Nginx booting — logged at ERROR.

    Only worker 0 schedules the warmup probe so we don't hammer Redis with
    N-worker probes at boot. Every retry runs on a 2s timer up to 30s;
    after that we mark ready to avoid never-healthy loops in dev.
--]]
function _M.on_init_worker()
    -- Distribute math.random state across workers so ttl jitter is truly
    -- per-worker (all workers otherwise share seed=1).
    math.randomseed(ngx.now() * 1000 + ngx.worker.id())
    local ok = backend_mod.init_worker()
    if not ok then
        ngx.log(ngx.ERR, "edge init_worker: backend init failed")
    end

    if ngx.worker.id() ~= 0 then return end
    -- Kick off async warmup: try every 2s, cap at 15 attempts (30s total),
    -- then flip ready regardless so we don't hang out of rotation forever.
    local attempts = 0
    local function tick(premature)
        if premature then return end
        attempts = attempts + 1
        warmup_probe()
        local flag = ngx.shared.edge_ready
        if flag and flag:get("ready") == 1 then return end
        if attempts >= 15 then
            ngx.log(ngx.WARN, "edge warmup: giving up after 15 attempts, ",
                "marking ready (fail-open in dev, real Redis outage will ",
                "surface as 503 from lookup)")
            mark_ready()
            return
        end
        ngx.timer.at(2, tick)
    end
    local ok_t, terr = pcall(ngx.timer.at, 0, tick)
    if not ok_t then
        ngx.log(ngx.ERR, "edge warmup: timer.at failed: ", tostring(terr),
            "; flipping ready without probe")
        mark_ready()
    end
end

-- shared_dict handle is looked up once per phase to keep hot path allocs
-- low. Errors here are unusual (missing lua_shared_dict declaration in
-- nginx.conf) and are non-recoverable; we log and 503.
local function get_shared()
    local s = ngx.shared.route_cache
    if not s then
        ngx.log(ngx.ERR, "lua_shared_dict route_cache missing from nginx.conf")
    end
    return s
end

--[[
    on_rewrite: parse tenant id, look up backend, stash into ngx.ctx.
    Runs in rewrite_by_lua_file. On any client-facing error we ngx.exit
    with the right status (fail-closed, no info leak).
--]]
function _M.on_rewrite()
    local uri = ngx.var.uri or ""
    local hdr = ngx.var.http_x_tenant_id
    local tid, terr = tenant_mod.extract_tenant_id(uri, hdr)
    if not tid then
        -- 400 → malformed URI; 404 → id absent or bad charset.
        return ngx.exit(terr)
    end

    local shared = get_shared()
    if not shared then return ngx.exit(503) end

    local redis_host = ngx.var.edge_redis_host
    local redis_port = tonumber(ngx.var.edge_redis_port) or 6379
    if not redis_host or redis_host == "" then
        ngx.log(ngx.ERR, "edge_redis_host not set in nginx.conf")
        return ngx.exit(503)
    end

    local desc, source, err_status = backend_mod.lookup_backend(
        shared, tid, redis_host, redis_port)
    if err_status then
        return ngx.exit(err_status)
    end

    -- Strip the `/ws/<tid>` routing prefix before proxying: the microVM
    -- gateway serves the OpenAI-compatible family at the ROOT
    -- (`/v1/chat/completions`, `/v1/models`) and the native WS handshake at
    -- `/` — it does NOT recognise the `/ws/<tid>/...` form (real-host check:
    -- gateway returns 404 for `/ws/<tid>/v1/chat/completions` but 401 for the
    -- stripped `/v1/chat/completions`). `/ws/<tid>` → `/`, `/ws/<tid>/v1/x` →
    -- `/v1/x`. The prefix is consumed here for routing (tid already extracted).
    local stripped = uri:gsub("^/ws/[^/]+", "")
    if stripped == "" then stripped = "/" end
    ngx.req.set_uri(stripped, false)

    -- Stash for balancer_by_lua + log phase.
    ngx.ctx.tenant_id = tid
    ngx.ctx.route_desc = desc
    ngx.ctx.route_source = source
end

--[[
    on_balancer: called from balancer_by_lua_block. Thin passthrough.
--]]
function _M.on_balancer()
    balancer_mod.balancer_pick()
end

--[[
    on_log: called from log_by_lua_block. Emits a compact structured line
    (via ngx.log) so Prometheus scrape and CloudWatch subscription can
    tally cache-hit source without a full JSON parser on the hot path.
    Metric endpoint /metrics is implemented in a separate content_by_lua
    (not this module) to keep this file phase-only.
--]]
function _M.on_log()
    local ctx = ngx.ctx
    if not ctx or not ctx.tenant_id then return end
    -- Single-line, tab-separated: easy to grep, cheap to emit.
    ngx.log(ngx.INFO, "edge_route\ttid=", ctx.tenant_id,
        "\tsrc=", ctx.route_source or "-",
        "\tstatus=", ngx.status,
        "\tup=", ngx.var.upstream_addr or "-",
        "\trt=", ngx.var.request_time or "-")
end

-- Exposed for tests to force-ready without a real probe.
_M._mark_ready = mark_ready

return _M
