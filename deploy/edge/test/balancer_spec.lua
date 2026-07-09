-- deploy/edge/test/balancer_spec.lua
--
-- balancer.pick_peer — local vs remote branches per the routing contract.
-- We test the pure `pick_peer` function; `balancer_pick` is a thin wrapper
-- around ngx.balancer that we exercise via the fake in spec_helper.

local helper = require "spec_helper"
local balancer = require "edge.lib.balancer"

describe("balancer.pick_peer (pure branch logic)", function()
    it("local branch: host == self_ip → guest_ip:18789", function()
        local peer_host, peer_port = balancer.pick_peer("10.0.1.5", {
            host = "10.0.1.5", port = 10042, guest_ip = "172.16.0.6",
        })
        assert.are.equal("172.16.0.6", peer_host)
        assert.are.equal(18789, peer_port)
    end)

    it("remote branch: host != self_ip → host:port (DNAT)", function()
        local peer_host, peer_port = balancer.pick_peer("10.0.1.5", {
            host = "10.0.9.9", port = 10099, guest_ip = "172.16.9.10",
        })
        assert.are.equal("10.0.9.9", peer_host)
        assert.are.equal(10099, peer_port)
    end)

    it("empty self_ip treats every descriptor as remote", function()
        local peer_host, peer_port = balancer.pick_peer("", {
            host = "10.0.1.5", port = 10042, guest_ip = "172.16.0.6",
        })
        assert.are.equal("10.0.1.5", peer_host)
        assert.are.equal(10042, peer_port)
    end)

    it("guest gateway port is the fixed 18789", function()
        assert.are.equal(18789, balancer._GUEST_GATEWAY_PORT)
    end)
end)

describe("balancer.balancer_pick (ngx.balancer wrapper)", function()
    before_each(function() helper.reset_ngx() end)

    it("calls set_current_peer with local guest_ip:18789 when host matches self", function()
        ngx.var.edge_self_ip = "10.0.1.5"
        ngx.ctx.tenant_id = "tid-1"
        ngx.ctx.route_desc = {
            host = "10.0.1.5", port = 10042, guest_ip = "172.16.0.6",
        }
        balancer.balancer_pick()
        local last = package.loaded["ngx.balancer"]._last_peer
        assert.are.equal("172.16.0.6", last.host)
        assert.are.equal(18789, last.port)
    end)

    it("calls set_current_peer with remote host:port when self differs", function()
        ngx.var.edge_self_ip = "10.0.1.5"
        ngx.ctx.tenant_id = "tid-2"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10099, guest_ip = "172.16.9.10",
        }
        balancer.balancer_pick()
        local last = package.loaded["ngx.balancer"]._last_peer
        assert.are.equal("10.0.9.9", last.host)
        assert.are.equal(10099, last.port)
    end)

    it("exits 503 when ngx.ctx.route_desc missing (rewrite skipped)", function()
        ngx.var.edge_self_ip = "10.0.1.5"
        ngx.ctx = {}
        local ok, err = pcall(balancer.balancer_pick)
        assert.is_false(ok)
        assert.is_table(err)
        assert.are.equal(503, err.status)
    end)
end)
