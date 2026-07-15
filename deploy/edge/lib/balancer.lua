-- deploy/edge/lib/balancer.lua
--
-- balancer_pick — pick the upstream peer for the current request.
-- Two branches per the data-plane contract:
--   local  descriptor.host == self_ip  → connect guest_ip:18789 directly
--   remote                             → connect host:port (peer's DNAT)
--
-- balancer_by_lua runs after rewrite_by_lua has stashed the descriptor
-- into ngx.ctx / ngx.var, so this file is pure "pick and set_current_peer".
-- Kept isolated because balancer_by_lua has a tight allow-list of
-- primitives (no shared_dict writes, no cosockets); the function stays
-- small enough to fit that.
--
-- self_ip is injected via ngx.var.edge_self_ip (populated by install-edge.sh
-- from the host's private IP at systemd unit start).

local balancer = require "ngx.balancer"
local _M = { _VERSION = "0.02" }

-- Guest gateway listens on a fixed port inside the microVM
-- (launch-vm.sh:747, deploy/userdata/launch-vm.sh — grep shows 18789).
local GUEST_GATEWAY_PORT = 18789

--[[
    pick_peer: pure function, decides (peer_host, peer_port) from
    (self_ip, descriptor). Extracted so busted can test the branch
    logic without needing ngx.balancer.

    2 args:
      - self_ip: string, this edge box's own private IP (from ngx.var)
      - desc:    { host, port, guest_ip, ... } (already validated upstream)
    2 return values:
      - peer_host, peer_port

    NOTE: For edge deployments (dedicated OpenResty tier separated from the
    microVM hosts, per SPEC/§2 "3 台 c6in.xlarge") self_ip will normally
    NOT match any host in Redis. The "local" branch exists so that when the
    edge is later collapsed onto the microVM host (dev/small-tier layouts
    do this), route.lua avoids a wasted hairpin through DNAT.
--]]
function _M.pick_peer(self_ip, desc)
    if desc.host == self_ip then
        return desc.guest_ip, GUEST_GATEWAY_PORT
    end
    return desc.host, desc.port
end

--[[
    balancer_pick: called from balancer_by_lua. Reads the descriptor from
    ngx.ctx (set by rewrite phase) and calls ngx.balancer.set_current_peer.
    On any bookkeeping failure this exits 503 so the caller sees a real
    error instead of a silent proxy_pass to nowhere.

    R6.3② edge failover:如果这是重试 tick (get_last_failure 返回
    connection refused / timeout,说明第一次连旧 host 撞 RST 或超时),先
    调 backend.invalidate 强制清 route 缓存 + 重查 Redis 拿新 desc,再
    对新 desc 调 set_current_peer。避免"路由已切但 edge 仍打旧 host"。
    重试次数 set_more_tries(1) 只重投一次,失败即 502。

    SHALL NOT:重投仅覆盖握手阶段失败(connect refused/timeout);WS/SSE
    响应头已开始转发后,proxy_next_upstream 就不再触发(见 nginx.conf 的
    `proxy_next_upstream` 白名单不含 non_idempotent 之外的错误)。
--]]
function _M.balancer_pick()
    local ctx = ngx.ctx
    if not ctx then
        ngx.log(ngx.ERR, "balancer_pick: ngx.ctx missing")
        return ngx.exit(503)
    end

    -- R6.3② 检测是否为重试 tick:上一次 upstream 失败 → 清缓存 + 重查 Redis。
    -- get_last_failure 只在有前一次尝试失败时返回非 nil (nginx 官方语义)。
    local state, code = balancer.get_last_failure()
    if state then
        _M._retry_refresh_desc(ctx, state, code)
    else
        -- 首次:告诉 nginx 允许一次上游重试(见 nginx.conf proxy_next_upstream)。
        local ok_t, terr = balancer.set_more_tries(1)
        if not ok_t then
            ngx.log(ngx.WARN, "balancer.set_more_tries(1) failed: ", tostring(terr))
        end
    end

    local desc = ctx.route_desc
    if not desc then
        ngx.log(ngx.ERR, "balancer_pick: ngx.ctx.route_desc missing (rewrite phase did not run?)")
        return ngx.exit(503)
    end

    local self_ip = ngx.var.edge_self_ip or ""
    local peer_host, peer_port = _M.pick_peer(self_ip, desc)
    if not peer_host or not peer_port then
        ngx.log(ngx.ERR, "balancer_pick: nil peer for tenant ",
            tostring(ctx.tenant_id))
        return ngx.exit(503)
    end

    local ok, err = balancer.set_current_peer(peer_host, peer_port)
    if not ok then
        ngx.log(ngx.ERR, "balancer.set_current_peer(", peer_host, ":",
            peer_port, ") for tenant ", tostring(ctx.tenant_id),
            " failed: ", tostring(err))
        return ngx.exit(503)
    end
end

-- Retry seam split out so tests can stub backend/redis without ngx.balancer.
-- Reads shared dict + Redis host from ngx.var, mirrors on_rewrite.
function _M._retry_refresh_desc(ctx, state, code)
    local tid = ctx.tenant_id
    if not tid then return end
    ngx.log(ngx.WARN, "balancer retry for tenant ", tid,
        " (upstream failed state=", tostring(state), " code=", tostring(code),
        "); invalidating cache + re-querying Redis")

    local backend = require "edge.lib.backend"
    local shared = ngx.shared.route_cache
    backend.invalidate(shared, tid)

    -- 重查 Redis 拿最新 desc。redis host/port 在 nginx.conf server 块的
    -- ngx.var 上 (edge_redis_host/port)——rewrite 阶段已读过一次,这里
    -- 二次读同一变量。
    local redis_host = ngx.var.edge_redis_host
    local redis_port = tonumber(ngx.var.edge_redis_port) or 6379
    if not redis_host or redis_host == "" then
        ngx.log(ngx.ERR, "balancer retry: edge_redis_host unset")
        return
    end
    local new_desc, source, err_status = backend.lookup_backend(
        shared, tid, redis_host, redis_port)
    if err_status or not new_desc then
        ngx.log(ngx.WARN, "balancer retry: re-lookup failed for ", tid,
            " status=", tostring(err_status))
        return  -- 保留旧 desc:让 set_current_peer 撞第二次 RST 后 502
    end
    ctx.route_desc = new_desc
    ctx.route_source = source
end


-- Exposed for tests.
_M._GUEST_GATEWAY_PORT = GUEST_GATEWAY_PORT

return _M
