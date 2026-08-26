-- deploy/edge/test/balancer_spec.lua
--
-- balancer.pick_peer — local vs remote branches per INTERFACE-CONTRACT §2.
-- We test the pure `pick_peer` function; `balancer_pick` is a thin wrapper
-- around ngx.balancer that we exercise via the fake in spec_helper.

local helper = require "spec_helper"
local balancer = require "edge.lib.balancer"
local backend = require "edge.lib.backend"
local route = require "edge.route"
local redis_client = require "edge.lib.redis_client"
local cjson = require "cjson.safe"

local function desc_json(host, port, guest_ip)
    return cjson.encode({
        host = host,
        port = port,
        guest_ip = guest_ip,
        updated_at = os.time(),
    })
end

local function set_route_request(tid, reader_host, primary_host)
    ngx.var.uri = "/ws/" .. tid .. "/v1/models"
    ngx.var.http_x_tenant_id = nil
    ngx.var.edge_redis_reader_host = reader_host
    ngx.var.edge_redis_reader_port = "6379"
    ngx.var.edge_redis_host = primary_host
    ngx.var.edge_redis_port = "6379"
end

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

    -- 防止相同三元组被误判为可用的新路由，造成无意义重投。
    it("_same_peer treats equal host, port and guest_ip as the same peer", function()
        local desc = {
            host = "10.0.1.5", port = 10042, guest_ip = "172.16.0.6",
        }
        assert.is_true(balancer._same_peer(desc, {
            host = desc.host, port = desc.port, guest_ip = desc.guest_ip,
        }))
    end)

    -- 防止只比较 host，漏掉同机不同 DNAT 端口的迁移。
    it("_same_peer detects a different port", function()
        assert.is_false(balancer._same_peer(
            { host = "10.0.1.5", port = 10042, guest_ip = "172.16.0.6" },
            { host = "10.0.1.5", port = 10043, guest_ip = "172.16.0.6" }))
    end)

    -- 防止不同宿主机被误判成同一个 peer。
    it("_same_peer detects a different host", function()
        assert.is_false(balancer._same_peer(
            { host = "10.0.1.5", port = 10042, guest_ip = "172.16.0.6" },
            { host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6" }))
    end)

    -- 防止空 desc 被当成“相同”而跳过下一次 rewrite 的 primary 提示。
    it("_same_peer treats either nil descriptor as different", function()
        local desc = {
            host = "10.0.1.5", port = 10042, guest_ip = "172.16.0.6",
        }
        assert.is_false(balancer._same_peer(nil, desc))
        assert.is_false(balancer._same_peer(desc, nil))
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

describe("#606 retry primary handoff", function()
    before_each(function()
        helper.reset_ngx()
        backend.init_worker()
        backend._set_lock_module(helper.new_fake_lock_module())
        redis_client._set_redis_module(helper.new_fake_redis_module())
        ngx.req = {
            set_uri = function(uri) ngx._rewritten_uri = uri end,
        }
    end)

    -- 原来钉 balancer 内直接用 primary 重查；现在钉重投只写一次性提示，
    -- 紧接着的 rewrite 连 primary。原读取不成立，因为 balancer 禁用 cosocket。
    it("forces the rewrite after a retry to connect to primary", function()
        local tid = "tid-retry"
        local primary_host = "primary.redis.internal"
        local reader_host = "reader.redis.internal"
        local shared = ngx.shared.route_cache
        ngx.ctx.tenant_id = tid
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        ngx._fake_redis = {
            mode = "hit",
            by_host = {
                [reader_host] = desc_json(
                    "10.0.9.9", 10042, "172.16.0.6"),
                [primary_host] = desc_json(
                    "10.0.7.7", 11001, "172.16.9.10"),
            },
        }
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }
        helper.set_phase("balancer")

        -- #628:共享缓存里没有不同 peer 时 balancer_pick 会 fail closed(ngx.exit),所以要
        -- pcall。一次性 primary 提示由 mark_retry_stale 写入,与是否 fail closed 无关 ——
        -- 下面对 p: 键与后续 rewrite 的断言正是要证明这一点没被 #628 破坏。
        local ok_pick, err_pick = pcall(balancer.balancer_pick)
        assert.is_false(ok_pick)
        assert.are.equal("table", type(err_pick), tostring(err_pick))
        assert.is_true(err_pick.ngx_exit)

        assert.is_not_nil(shared:get("p:" .. tid))
        ngx.ctx = {}
        set_route_request(tid, reader_host, primary_host)
        helper.set_phase("rewrite")
        route.on_rewrite()

        local connects = helper.fake_redis_connects()
        assert.are.equal(1, #connects)
        assert.are.equal(primary_host, connects[1])
        assert.are.equal("10.0.7.7", ngx.ctx.route_desc.host)
        assert.is_nil(shared:get("p:" .. tid))
    end)

    -- 防止提示被重复消费：首次 rewrite 必须读 primary 并拿到新 desc，
    -- 后续请求在离开缓存后应恢复 reader，而不是永久压向写节点。
    it("consumes the retry hint once then returns later rewrites to reader", function()
        local tid = "tid-one-shot"
        local primary_host = "primary.redis.internal"
        local reader_host = "reader.redis.internal"
        local shared = ngx.shared.route_cache
        ngx.ctx.tenant_id = tid
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        ngx._fake_redis = {
            mode = "hit",
            by_host = {
                [reader_host] = desc_json(
                    "10.0.8.8", 12002, "172.16.8.8"),
                [primary_host] = desc_json(
                    "10.0.7.7", 11001, "172.16.9.10"),
            },
        }
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }
        helper.set_phase("balancer")
        -- #628:同上,fail closed 走 ngx.exit,需 pcall;提示写入不受影响。
        local ok_pick = pcall(balancer.balancer_pick)
        assert.is_false(ok_pick)

        ngx.ctx = {}
        set_route_request(tid, reader_host, primary_host)
        helper.set_phase("rewrite")
        route.on_rewrite()
        assert.are.equal("10.0.7.7", ngx.ctx.route_desc.host)
        assert.is_nil(shared:get("p:" .. tid))

        -- 清掉本地/新鲜标记只为让第二次请求真实走到 endpoint 选择。
        backend.init_worker()
        shared:delete("f:" .. tid)
        ngx.ctx = {}
        set_route_request(tid, reader_host, primary_host)
        route.on_rewrite()

        local connects = helper.fake_redis_connects()
        assert.are.equal(2, #connects)
        assert.are.equal(primary_host, connects[1])
        assert.are.equal(reader_host, connects[2])
        assert.are.equal("10.0.8.8", ngx.ctx.route_desc.host)
    end)
end)
