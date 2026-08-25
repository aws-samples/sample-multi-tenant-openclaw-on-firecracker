-- deploy/edge/test/route_reader_isolation_spec.lua
--
-- #618 reader 热路径的公开入口回归测试。这里必须 require edge.route，
-- 防止只测 backend 而整段 reader 选择逻辑被删后测试仍然全绿。

local helper = require "spec_helper"
local route = require "edge.route"
local backend = require "edge.lib.backend"
local redis_client = require "edge.lib.redis_client"
local cjson = require "cjson.safe"

local function desc_json(host, port)
    return cjson.encode({
        host = host,
        port = port,
        guest_ip = "172.16.0.2",
        updated_at = os.time(),
    })
end

local function new_fake_l1()
    local store = {}
    return {
        get = function(_, key) return store[key] end,
        set = function(_, key, value) store[key] = value end,
        delete = function(_, key) store[key] = nil end,
    }
end

local function contains(values, expected)
    for _, value in ipairs(values) do
        if value == expected then return true end
    end
    return false
end

local function set_request(tid, reader_host, reader_port, primary_host, primary_port)
    ngx.var.uri = "/ws/" .. tid .. "/v1/models"
    ngx.var.http_x_tenant_id = nil
    ngx.var.edge_redis_reader_host = reader_host
    ngx.var.edge_redis_reader_port = reader_port
    ngx.var.edge_redis_host = primary_host
    ngx.var.edge_redis_port = primary_port
end

local function expect_rewrite_exit(status)
    local ok, err = pcall(route.on_rewrite)
    assert.is_false(ok)
    assert.are.equal(status, err.status)
end

-- 预置 fail-static blob 与 fresh marker，但让两次 happy-path freshness
-- 检查都观察到“已过期”，从而真实进入 Redis clean-miss 分支。底层 marker
-- 仍保留，便于断言 put_negative 有没有主动删除它。
local function seed_aged_material(shared, tid)
    shared:set("r:" .. tid, desc_json("10.0.1.5", 10042), 60)
    shared:set("f:" .. tid, "1", 5)
    local real_get = shared.get
    local fresh_misses = 2
    shared.get = function(self, key)
        if key == "f:" .. tid and fresh_misses > 0 then
            fresh_misses = fresh_misses - 1
            return nil
        end
        return real_get(self, key)
    end
end

describe("#618 route reader isolation", function()
    before_each(function()
        helper.reset_ngx()
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())
        backend._set_l1(new_fake_l1())
        ngx.req = {
            set_uri = function(uri) ngx._rewritten_uri = uri end,
        }
    end)

    -- 防止热路径退回 primary，重新把正常读流量压到写节点。
    it("reader-first: reader 与 primary 不同时只连接 reader", function()
        local tid = "reader-first"
        set_request(tid, "reader.redis.local", "6379",
            "primary.redis.local", "6379")
        ngx._fake_redis = {
            mode = "hit",
            by_host = {
                ["reader.redis.local"] = desc_json("10.0.1.5", 10042),
                ["primary.redis.local"] = desc_json("10.0.9.9", 10099),
            },
        }

        route.on_rewrite()

        local connects = helper.fake_redis_connects()
        assert.is_true(contains(connects, "reader.redis.local"))
        assert.is_false(contains(connects, "primary.redis.local"))
        assert.are.equal("10.0.1.5", ngx.ctx.route_desc.host)
    end)

    -- 防止缺 reader host 时直接 503，而不是保持滚动升级兼容。
    it("reader host 为空时回落 primary", function()
        local tid = "reader-host-empty"
        set_request(tid, "", "6379", "primary.redis.local", "6379")
        ngx._fake_redis = {
            mode = "hit",
            by_host = {
                ["primary.redis.local"] = desc_json("10.0.2.5", 10043),
            },
        }

        route.on_rewrite()

        local connects = helper.fake_redis_connects()
        assert.is_true(contains(connects, "primary.redis.local"))
        assert.are.equal("10.0.2.5", ngx.ctx.route_desc.host)
    end)

    -- 防止拼出“reader host + primary port”的跨 endpoint 错配。
    it("reader port 为空时 host 与 port 一起回落 primary", function()
        local tid = "reader-port-empty"
        set_request(tid, "reader.redis.local", "",
            "primary.redis.local", "6380")
        ngx._fake_redis = {
            mode = "hit",
            by_host = {
                ["reader.redis.local"] = desc_json("10.0.8.8", 10888),
                ["primary.redis.local"] = desc_json("10.0.3.5", 10044),
            },
        }

        route.on_rewrite()

        local connects = helper.fake_redis_connects()
        assert.is_true(contains(connects, "primary.redis.local"))
        assert.is_false(contains(connects, "reader.redis.local"))
        assert.are.equal("10.0.3.5", ngx.ctx.route_desc.host)
    end)

    -- 防止副本重同步时的二义 miss 主动删掉在役租户 L2 底料。
    it("非权威 reader clean miss 写负缓存但保留 fail-static 底料", function()
        local tid = "reader-clean-miss"
        set_request(tid, "reader.redis.local", "6379",
            "primary.redis.local", "6379")
        local shared = ngx.shared.route_cache
        seed_aged_material(shared, tid)
        ngx._fake_redis = { mode = "miss" }

        expect_rewrite_exit(404)

        assert.is_not_nil(shared:get("r:" .. tid))
        assert.is_not_nil(shared:get("f:" .. tid))
        assert.is_not_nil(shared:get("n:" .. tid))
    end)

    -- 锁住默认开关关闭时的旧语义：primary miss 仍清理已失效正缓存。
    it("开关关闭形态的权威 clean miss 仍删除 fail-static 底料", function()
        local tid = "primary-clean-miss"
        set_request(tid, "primary.redis.local", "6379",
            "primary.redis.local", "6379")
        local shared = ngx.shared.route_cache
        seed_aged_material(shared, tid)
        ngx._fake_redis = { mode = "miss" }

        expect_rewrite_exit(404)

        assert.is_nil(shared:get("r:" .. tid))
        assert.is_nil(shared:get("f:" .. tid))
        assert.is_not_nil(shared:get("n:" .. tid))
    end)
end)
