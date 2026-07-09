-- deploy/edge/lib/balancer.lua
--
-- balancer_pick — pick the upstream peer for the current request.
-- Two branches per the routing contract:
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
local _M = { _VERSION = "0.01" }

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
--]]
function _M.balancer_pick()
    local ctx = ngx.ctx
    local desc = ctx and ctx.route_desc
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

-- Exposed for tests.
_M._GUEST_GATEWAY_PORT = GUEST_GATEWAY_PORT

return _M
