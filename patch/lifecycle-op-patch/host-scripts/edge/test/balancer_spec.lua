-- deploy/edge/test/balancer_spec.lua
--
-- balancer.pick_peer — local vs remote branches per INTERFACE-CONTRACT §2.
-- We test the pure `pick_peer` function; `balancer_pick` is a thin wrapper
-- around ngx.balancer that we exercise via the fake in spec_helper.

local helper = require "spec_helper"
local balancer = require "edge.lib.balancer"
local backend = require "edge.lib.backend"

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

    it("sets a 60s read timeout only for stripped chat completions", function()
        ngx.var.uri = "/v1/chat/completions"
        ngx.var.edge_self_ip = "10.0.1.5"
        ngx.ctx.tenant_id = "tid-chat"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10099, guest_ip = "172.16.9.10",
        }

        balancer.balancer_pick()

        local timeouts = package.loaded["ngx.balancer"]._timeouts
        assert.is_not_nil(timeouts)
        assert.is_nil(timeouts.connect)
        assert.is_nil(timeouts.send)
        assert.are.equal(60, timeouts.read)
    end)

    it("leaves native websocket timeout inherited from nginx", function()
        ngx.var.uri = "/"
        ngx.var.edge_self_ip = "10.0.1.5"
        ngx.ctx.tenant_id = "tid-ws"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10099, guest_ip = "172.16.9.10",
        }

        balancer.balancer_pick()

        assert.is_nil(package.loaded["ngx.balancer"]._timeouts)
    end)

    it("fails closed when the per-request chat timeout cannot be set", function()
        ngx.var.uri = "/v1/chat/completions"
        ngx.var.edge_self_ip = "10.0.1.5"
        ngx.ctx.tenant_id = "tid-chat-error"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10099, guest_ip = "172.16.9.10",
        }
        package.loaded["ngx.balancer"]._set_timeouts_error = "ffi failure"

        local ok, err = pcall(balancer.balancer_pick)

        assert.is_false(ok)
        assert.is_table(err)
        assert.are.equal(503, err.status)
        assert.is_nil(package.loaded["ngx.balancer"]._last_peer)
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

describe("balancer._retry_refresh_desc Redis endpoint", function()
    local original_invalidate
    local original_lookup_backend

    before_each(function()
        helper.reset_ngx()
        original_invalidate = backend.invalidate
        original_lookup_backend = backend.lookup_backend
    end)

    after_each(function()
        backend.invalidate = original_invalidate
        backend.lookup_backend = original_lookup_backend
    end)

    it("uses primary instead of reader for failover re-lookup", function()
        local primary_host = "primary.redis.internal"
        local reader_host = "reader.redis.internal"
        local lookup_host

        ngx.var.edge_redis_host = primary_host
        ngx.var.edge_redis_port = "6379"
        ngx.var.edge_redis_reader_host = reader_host
        ngx.var.edge_redis_reader_port = "6380"
        backend.invalidate = function() end
        backend.lookup_backend = function(_shared, _tid, host, _port)
            lookup_host = host
            return {
                host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
            }, "redis", nil
        end

        balancer._retry_refresh_desc({ tenant_id = "tid-retry" }, "failed", 502)

        assert.are.equal(primary_host, lookup_host)
        assert.are_not.equal(reader_host, lookup_host)
    end)
end)
