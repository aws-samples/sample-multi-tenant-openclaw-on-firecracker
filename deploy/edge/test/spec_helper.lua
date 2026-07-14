-- deploy/edge/test/spec_helper.lua
--
-- Boot the OpenResty-shaped globals just enough for our lua-only modules
-- to load under plain busted. We do NOT run under real openresty in tests
-- (that stays for nginx -t + integration/P7 real-machine).
--
-- Anything that requires ngx.balancer / ngx.shared / resty.redis is
-- injected via test seams (see backend/redis_client _set_* helpers).

-- Make deploy/edge/... modules importable when tests are run from the
-- repo root or from deploy/edge/test/.
local function repo_root()
    local script = debug.getinfo(1, "S").source:sub(2)
    -- script = "<root>/deploy/edge/test/spec_helper.lua"
    return script:gsub("/deploy/edge/test/spec_helper.lua$", "")
end

local root = repo_root()
package.path = root .. "/?.lua;"
    .. root .. "/deploy/?.lua;"
    .. root .. "/deploy/edge/?.lua;"
    .. root .. "/deploy/edge/lib/?.lua;"
    .. package.path

-- --- Minimal ngx stub ------------------------------------------------------
-- Only the fields our modules actually touch. Extend as needed.

local ngx_log_capture = {}

local ngx_stub = {
    null = setmetatable({}, { __tostring = function() return "ngx.null" end }),
    var  = {},
    ctx  = {},
    header = {},
    status = 200,
    ERR = 4, WARN = 5, NOTICE = 6, INFO = 7, DEBUG = 8,
    -- ngx.now returns fractional seconds since epoch.
    now  = function() return os.time() end,
    time = function() return os.time() end,
    worker = { id = function() return 0 end },
    -- Capture logs for test assertions.
    log  = function(_, ...)
        local parts = {}
        for i = 1, select("#", ...) do parts[i] = tostring(select(i, ...)) end
        ngx_log_capture[#ngx_log_capture + 1] = table.concat(parts)
    end,
    -- exit raises so tests can assert on the status code via pcall.
    exit = function(code)
        error({ ngx_exit = true, status = code }, 2)
    end,
    say = function() end,
}

-- --- Fake shared dict factory ---------------------------------------------
local function new_fake_shared_dict()
    local store = {}
    local dict = {}
    function dict:get(k)
        local entry = store[k]
        if not entry then return nil end
        if entry.expires and entry.expires < os.time() then
            store[k] = nil
            return nil
        end
        return entry.value
    end
    function dict:set(k, v, ttl)
        store[k] = { value = v, expires = ttl and (os.time() + ttl) or nil }
        return true, nil, false
    end
    function dict:delete(k) store[k] = nil end
    return dict
end

-- --- Fake resty.redis stub -------------------------------------------------
-- Behaviour selectable by setting ngx_stub._fake_redis.mode:
--   "hit"        → returns the JSON in _fake_redis.value
--   "miss"       → returns ngx.null (clean miss)
--   "error"      → returns transport error on connect()
--   "get_error"  → connect ok, GET returns err
local function new_fake_redis_module(ngx_ref)
    local mod = {}
    function mod.new(_self)
        local client = {}
        client._closed = false
        function client.set_timeouts(_c) end
        function client.connect(_c)
            local cfg = ngx_ref._fake_redis or {}
            if cfg.mode == "error" then
                return nil, cfg.err or "connect refused"
            end
            return 1, nil
        end
        function client.get(_c, _key)
            local cfg = ngx_ref._fake_redis or {}
            if cfg.mode == "get_error" then
                return nil, cfg.err or "connection reset"
            end
            if cfg.mode == "miss" then return ngx_ref.null end
            return cfg.value or ngx_ref.null
        end
        function client.set_keepalive(_c) return 1, nil end
        function client.close(c) c._closed = true end
        return client, nil
    end
    return mod
end

-- --- Fake resty.lock stub --------------------------------------------------
-- Non-blocking: lock always succeeds instantly. Enough for correctness
-- tests; stampede timing is covered separately at L4 (the test plan §5).
local function new_fake_lock_module()
    local mod = {}
    function mod:new(_name, _opts)
        return {
            lock = function(_self, _key) return 0, nil end,
            unlock = function(_self) return 1, nil end,
        }, nil
    end
    return mod
end

-- --- Install globals -------------------------------------------------------
_G.ngx = ngx_stub

-- Provide cjson.safe if cjson isn't installed system-wide. Busted usually
-- has lua-cjson; if not, an install hint is emitted by the runner script.
if not pcall(require, "cjson.safe") then
    error("cjson.safe not found. Install lua-cjson: `luarocks install lua-cjson`")
end

-- Fake resty.lrucache — minimal LRU semantics for L1 cache tests.
package.loaded["resty.lrucache"] = {
    new = function(_capacity)
        local store, order = {}, {}
        local cache = {}
        function cache:get(k)
            local e = store[k]
            if not e then return nil end
            if e.expires and e.expires < os.time() then
                store[k] = nil
                return nil
            end
            return e.v
        end
        function cache:set(k, v, ttl)
            store[k] = { v = v, expires = ttl and (os.time() + ttl) or nil }
            order[#order + 1] = k
        end
        function cache:delete(k) store[k] = nil end
        return cache, nil
    end,
}

-- Fake ngx.balancer for balancer_pick tests.
package.loaded["ngx.balancer"] = {
    _last_peer = nil,
    _last_failure = nil,  -- {state="failed", code=502} to simulate retry tick
    _more_tries = 0,
    set_current_peer = function(host, port)
        package.loaded["ngx.balancer"]._last_peer = { host = host, port = port }
        return true, nil
    end,
    set_more_tries = function(n)
        package.loaded["ngx.balancer"]._more_tries = n
        return true, nil
    end,
    get_last_failure = function()
        local f = package.loaded["ngx.balancer"]._last_failure
        if not f then return nil end
        return f.state, f.code
    end,
}

-- Fake ngx.shared registry: route_cache dict is looked up by name in balancer
-- retry path (_retry_refresh_desc reads ngx.shared.route_cache).
ngx_stub.shared = setmetatable({}, {
    __index = function(t, k)
        local d = new_fake_shared_dict()
        rawset(t, k, d)
        return d
    end,
})

-- --- Public helpers exposed to specs --------------------------------------
local M = {}
M.ngx = ngx_stub
M.log_capture = ngx_log_capture
M.new_fake_shared_dict = new_fake_shared_dict
M.new_fake_redis_module = function() return new_fake_redis_module(ngx_stub) end
M.new_fake_lock_module = new_fake_lock_module
M.reset_ngx = function()
    ngx_stub.var, ngx_stub.ctx, ngx_stub.header = {}, {}, {}
    ngx_stub.status = 200
    ngx_stub._fake_redis = nil
    while ngx_log_capture[1] do table.remove(ngx_log_capture) end
    package.loaded["ngx.balancer"]._last_peer = nil
    package.loaded["ngx.balancer"]._last_failure = nil
    package.loaded["ngx.balancer"]._more_tries = 0
    -- Reset shared dicts to a fresh table so tests are isolated.
    ngx_stub.shared = setmetatable({}, getmetatable(ngx_stub.shared))
end

return M
