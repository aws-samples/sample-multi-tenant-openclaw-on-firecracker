-- deploy/edge/route.lua
--
-- Public entry orchestrator for the OpenResty edge. Exposes three phases
-- that nginx.conf wires into rewrite / access / balancer / init_worker.
-- Each phase is a thin dispatcher — logic lives in edge.lib.*.
--
-- Data contract with host-agent + iac-dev:
--   engineering/00-knowledge-base/SPEC/11-ENGINE-TRANSFORM/INTERFACE-CONTRACT.md
--
-- Do not add business logic here. Keep this file the "entry only, routes to
-- domain" boundary per .claude/rules/code-craft-discipline.md.

local tenant_mod   = require "edge.lib.tenant"
local backend_mod  = require "edge.lib.backend"
local balancer_mod = require "edge.lib.balancer"
local redis_client = require "edge.lib.redis_client"
local hints_mod    = require "edge.lib.hints"

local _M = { _VERSION = "0.02" }

-- Warmup gate: /healthz reads this and returns 503 until the async probe
-- below succeeds. Prevents ASG rotating traffic into a cold instance
-- whose Redis connection has not yet been proven (INTERFACE-CONTRACT §6).
local function mark_ready()
    local flag = ngx.shared.edge_ready
    if flag then flag:set("ready", 1) end
end

local function probe_primary_if_distinct(hot_host, hot_port)
    local coords = hints_mod.get() or {}
    local host = coords.primary_host
    if not host then
        ngx.log(ngx.ERR, "edge warmup: primary Redis coordinate absent from ",
            "nginx.conf's init_by_lua_block; primary probe skipped; ",
            "failover retry will fail")
        return
    end
    local port = coords.primary_port or 6379
    if host == hot_host and port == hot_port then return end
    local _, err = redis_client.get_route(host, port, "route:__warmup__")
    if err then
        ngx.log(ngx.ERR, "edge warmup primary probe failed: ", tostring(err),
            "; failover retry will fail")
    end
end

-- warmup_probe: run once (on worker 0) to verify Redis reachability.
-- We deliberately keep this cheap — a single successful GET against a
-- known-empty key confirms TCP + Redis protocol + endpoint DNS resolves.
-- Any failure keeps the box out of ELB rotation; a later probe retries.
-- #618：这里的 failure 只指热路径 endpoint；primary 探测失败仅报错，
-- 不撤销 reader 已经建立的 readiness。
local function warmup_probe()
    -- init_worker 阶段拿不到 ngx.var,坐标来自 nginx.conf 的 init_by_lua_block
    -- (见 edge.lib.hints 的注释:走 env 那条通道被 nginx 抹掉,#639)。
    -- 优先 reader,缺失时回落 primary,保持滚动升级兼容。
    local coords = hints_mod.get() or {}
    local host, port = coords.reader_host, coords.reader_port
    local which = "reader"
    if not host then
        host, port = coords.primary_host, coords.primary_port
        which = "primary"
    end
    if not host then
        -- FAIL CLOSED (#639). This branch used to call mark_ready(): a broken
        -- coordinate channel therefore produced a healthy /healthz on an
        -- instance that had never reached Redis, ASG/ALB rotated traffic into
        -- it, and every tenant got 404/5xx — exactly what #618's gate exists to
        -- prevent. A missing coordinate cannot heal by retrying, so say it at
        -- ERROR and stay out of rotation. on_init_worker's own give-up path
        -- still stops a permanently unhealthy box in dev.
        ngx.log(ngx.ERR, "edge warmup: no Redis coordinate from nginx.conf's ",
            "init_by_lua_block (reader and primary both absent); refusing to ",
            "mark ready without a probe")
        return
    end
    port = port or 6379
    local _, err = redis_client.get_route(host, port, "route:__warmup__")
    if err then
        ngx.log(ngx.WARN, "edge warmup probe via ", which, " coordinate",
            " failed: ", tostring(err), "; will retry")
    else
        mark_ready()
        ngx.log(ngx.NOTICE, "edge warmup ok; healthz now 200")
    end

    probe_primary_if_distinct(host, port)
end

-- #643 — warmup 重试节奏。前 15 次 2s(覆盖正常冷启动),之后 10s 封顶并【永不放弃】:
-- 原来第 15 次会 mark_ready() fail-open,Redis 真故障时 /healthz 约 30s 后翻 200,
-- 实例进 ALB 轮转而每个租户在 lookup 拿 503。ALB 在全 target unhealthy 时本来就会
-- fail-open 照发流量,所以保持 unready 不会让入口消失,只是不再谎报 healthy。
local WARMUP_FAST_ATTEMPTS = 15
local WARMUP_FAST_DELAY_SEC = 2
local WARMUP_SLOW_DELAY_SEC = 10
local function warmup_retry_delay(attempts)
    if attempts < WARMUP_FAST_ATTEMPTS then return WARMUP_FAST_DELAY_SEC end
    return WARMUP_SLOW_DELAY_SEC
end

--[[
    on_init_worker: fills the per-worker lrucache. Called from
    init_worker_by_lua_file in nginx.conf. Any failure is fatal to route
    fidelity but not to Nginx booting — logged at ERROR.

    Only worker 0 schedules the warmup probe so we don't hammer Redis with
    N-worker probes at boot. Retries run every 2s for the first 15 attempts,
    then every 10s until Redis is reachable and the worker can mark ready.
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
    -- Kick off async warmup: every 2s for the first 15 attempts (~30s), then
    -- every 10s, forever. We never flip ready without a successful probe (#643).
    local attempts = 0
    local function tick(premature)
        if premature then return end
        attempts = attempts + 1
        warmup_probe()
        local flag = ngx.shared.edge_ready
        if flag and flag:get("ready") == 1 then return end
        if attempts == WARMUP_FAST_ATTEMPTS then
            -- 只在跨过快节奏边界时说一次,避免每 10s 刷一条。
            ngx.log(ngx.ERR, "edge warmup: still no Redis after ",
                WARMUP_FAST_ATTEMPTS, " attempts; staying UNREADY (#643 ",
                "fail-closed) and retrying every ", WARMUP_SLOW_DELAY_SEC,
                "s; /healthz flips 200 as soon as one probe succeeds")
        end
        local ok_next, terr_next =
            ngx.timer.at(warmup_retry_delay(attempts), tick)
        if not ok_next then
            -- #643 —— 兜底 mark_ready 删掉之后,这条链断了就没有第二次机会:worker
            -- 永久 unready 且再也不探测。原来有 fail-open 兜底时断链的后果被掩盖,
            -- 现在必须说出来,否则又变成 #643 要治的那个"没人知道"。
            ngx.log(ngx.ERR, "edge warmup: failed to schedule next probe: ",
                tostring(terr_next), "; staying unready and no further probe ",
                "will run (ELB pulls the box, ASG replaces it)")
        end
    end
    local ok_t, terr = pcall(ngx.timer.at, 0, tick)
    if not ok_t then
        -- FAIL CLOSED (#639). No timer means the probe never runs *at all* —
        -- not even the give-up path above — so marking ready here puts a
        -- never-probed worker into rotation: the same defect class this issue
        -- fixed, just triggered by a broken timer instead of a broken
        -- coordinate channel. Stay unready; ELB pulls the box and ASG replaces
        -- it. timer.at failing is uncorrelated with Redis health, so this
        -- cannot mask a real outage as "no capacity".
        ngx.log(ngx.ERR, "edge warmup: timer.at failed: ", tostring(terr),
            "; staying unready (no probe will ever run)")
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

    local primary_host = ngx.var.edge_redis_host
    local primary_port_str = ngx.var.edge_redis_port
    local redis_host = ngx.var.edge_redis_reader_host
    local redis_port_str = ngx.var.edge_redis_reader_port
    if not redis_host or redis_host == ""
        or not redis_port_str or redis_port_str == "" then
        redis_host = primary_host
        redis_port_str = primary_port_str
    end
    -- #606 — 上一次请求的 balancer 重投只能写共享内存提示；这里在 rewrite
    -- 阶段消费它并强制读 primary，替代原来不可能成功的 balancer primary
    -- 重读，同时兑现迁移后的 read-after-write。
    if backend_mod.consume_retry_hint(shared, tid) then
        redis_host = primary_host
        redis_port_str = primary_port_str
    end
    local redis_port = tonumber(redis_port_str) or 6379
    local primary_port = tonumber(primary_port_str) or 6379
    if not redis_host or redis_host == "" then
        ngx.log(ngx.ERR, "edge_redis_reader_host and fallback ",
            "edge_redis_host are not set in nginx.conf")
        return ngx.exit(503)
    end
    local authoritative = redis_host == primary_host
        and redis_port_str == primary_port_str

    local desc, source, err_status = backend_mod.lookup_backend(
        shared, tid, redis_host, redis_port, authoritative,
        primary_host, primary_port)
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
    on_header_filter: called from header_filter_by_lua_block.

    唯一职责是把 balancer 阶段 fail closed 产生的 500 改写回本来想返的状态码 ——
    `balancer_by_lua*` 里的 ngx.exit(<code>) 传不出状态码,客户一律看到 500
    (openresty/lua-resty-core#70)。改写只能在输出过滤器做。
--]]
function _M.on_header_filter()
    balancer_mod.fixup_status()
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
-- Exposed so the readiness gate itself is testable: on_init_worker only ever
-- reaches warmup_probe through an ngx.timer, which plain busted cannot drive.
_M._warmup_probe = warmup_probe
-- #643 — 退避节奏是可测的纯函数;tick 本身是 on_init_worker 的 local 闭包,
-- busted 驱动不到,所以门的"永不放弃"语义只能从这里回归。
_M._warmup_retry_delay = warmup_retry_delay

return _M
