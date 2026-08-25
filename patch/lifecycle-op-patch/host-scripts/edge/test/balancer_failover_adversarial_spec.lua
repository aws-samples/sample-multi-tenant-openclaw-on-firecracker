-- deploy/edge/test/balancer_failover_spec.lua
--
-- R6.3② edge failover 对抗测试。
-- 覆盖:
--   ① 首次调用:set_more_tries(1) 允许一次重投,不清缓存
--   ② 重试 tick (get_last_failure 返回非 nil):backend.invalidate 被调
--      + Redis 重查 + 用新 desc 重投 (peer 变到 target)
--   ③ 重查失败时保留旧 desc,不 crash;第二次 set_current_peer 会撞 RST 触发 502

local helper = require "spec_helper"
local balancer = require "edge.lib.balancer"
local backend = require "edge.lib.backend"

describe("balancer R6.3 edge failover", function()
    before_each(function()
        helper.reset_ngx()
        backend.init_worker()
    end)

    it("first call: allows one retry (set_more_tries 1), does NOT invalidate", function()
        ngx.var.edge_self_ip = "10.0.0.1"
        ngx.var.edge_redis_host = "redis.local"
        ngx.var.edge_redis_port = "6379"
        ngx.ctx.tenant_id = "t-1"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        -- 提前塞一个 L2 cache 值,断言 invalidate 未被调 → 值仍在
        backend._set_l1(nil)  -- 无 L1
        local shared = ngx.shared.route_cache
        shared:set("r:t-1", '{"host":"10.0.9.9","port":10042,"guest_ip":"172.16.0.6"}', 60)

        balancer.balancer_pick()

        assert.are.equal(1, package.loaded["ngx.balancer"]._more_tries)
        assert.is_not_nil(shared:get("r:t-1"))  -- 缓存未被清
        local last = package.loaded["ngx.balancer"]._last_peer
        assert.are.equal("10.0.9.9", last.host)
    end)

    it("retry tick: invalidates cache + re-queries Redis + set_current_peer to NEW peer", function()
        ngx.var.edge_self_ip = "10.0.0.1"
        ngx.var.edge_redis_host = "redis.local"
        ngx.var.edge_redis_port = "6379"
        ngx.ctx.tenant_id = "t-mig"
        -- 模拟旧 desc:route 仍指源 host
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        -- L2 缓存里也是旧 desc
        local shared = ngx.shared.route_cache
        shared:set("r:t-mig", '{"host":"10.0.9.9","port":10042,"guest_ip":"172.16.0.6"}', 60)

        -- 让 Redis 现在返回新 desc (target host):迁移刚 commit
        ngx.stub_fake_redis = ngx  -- alias
        ngx._fake_redis = {
            mode = "hit",
            value = '{"host":"10.0.7.7","port":11001,"guest_ip":"172.16.9.10","updated_at":123}',
        }
        -- backend 用真实 redis_client → 我们要把它换成 fake module
        local redis_client = require "edge.lib.redis_client"
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())

        -- 模拟上一次 upstream 失败
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }

        balancer.balancer_pick()

        -- 缓存应已被清并重新填(新 desc 已进 L2)
        local val = shared:get("r:t-mig")
        assert.is_not_nil(val)
        assert.matches("10.0.7.7", val)
        assert.matches("11001", val)

        -- set_current_peer 用的是新 peer
        local last = package.loaded["ngx.balancer"]._last_peer
        assert.are.equal("10.0.7.7", last.host)
        assert.are.equal(11001, last.port)
    end)

    it("retry tick with Redis error: preserves old desc, does not crash", function()
        ngx.var.edge_self_ip = "10.0.0.1"
        ngx.var.edge_redis_host = "redis.local"
        ngx.var.edge_redis_port = "6379"
        ngx.ctx.tenant_id = "t-bad"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }

        ngx._fake_redis = { mode = "error", err = "connection refused" }
        local redis_client = require "edge.lib.redis_client"
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())

        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }

        balancer.balancer_pick()  -- 不应抛异常

        -- 保留旧 desc:让 nginx 第二次 set_current_peer 撞旧 host 后 502
        -- (与 spec references R6-edge-failover 一致:redis 抖不误降为 clean miss)
        local last = package.loaded["ngx.balancer"]._last_peer
        assert.are.equal("10.0.9.9", last.host)
    end)
end)

describe("balancer failover no-cross-tenant isolation", function()
    local redis_client = require "edge.lib.redis_client"

    local function contains(values, expected)
        for _, value in ipairs(values) do
            if value == expected then return true end
        end
        return false
    end

    before_each(function()
        helper.reset_ngx()
        backend.init_worker()
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())
    end)

    it("retry reads primary and never adopts a stale reader route from a victim slot", function()
        ngx.var.edge_redis_host = "primary.redis.local"
        ngx.var.edge_redis_port = "6379"
        ngx.var.edge_redis_reader_host = "reader.redis.local"
        ngx.var.edge_redis_reader_port = "6379"
        ngx.ctx.tenant_id = "t-migrating"
        -- reader 上的旧坐标代表已回收并重分配给另一租户的在役槽位(victim slot)。
        local victim_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        ngx.ctx.route_desc = victim_desc
        ngx._fake_redis = {
            mode = "hit",
            by_host = {
                ["reader.redis.local"] =
                    '{"host":"10.0.9.9","port":10042,"guest_ip":"172.16.0.6"}',
                ["primary.redis.local"] =
                    '{"host":"10.0.7.7","port":11001,"guest_ip":"172.16.9.10"}',
            },
        }
        -- 读副本可能因复制延迟拿到旧路由；旧 host:port 若已分配给别的租户，
        -- 重投就会进入其 microVM，形成跨租户误路由。
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }

        balancer.balancer_pick()

        local last = package.loaded["ngx.balancer"]._last_peer
        assert.are.equal("10.0.7.7", last.host)
        assert.are.equal(11001, last.port)
        assert.are_not.equal(victim_desc.host, last.host)
        local connects = helper.fake_redis_connects()
        assert.is_true(contains(connects, "primary.redis.local"))
        assert.is_false(contains(connects, "reader.redis.local"))
    end)

    it("retry touches only primary when the configured reader has a clean miss", function()
        ngx.var.edge_redis_host = "primary.redis.local"
        ngx.var.edge_redis_port = "6379"
        ngx.var.edge_redis_reader_host = "reader.redis.local"
        ngx.var.edge_redis_reader_port = "6379"
        ngx.ctx.tenant_id = "t-reader-miss"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        ngx._fake_redis = {
            mode = "hit",
            by_host = {
                ["reader.redis.local"] = ngx.null,
                ["primary.redis.local"] =
                    '{"host":"10.0.7.7","port":11001,"guest_ip":"172.16.9.10"}',
            },
        }
        -- 即使 reader 恰好 clean miss，也不能先读副本：复制延迟可能让旧路由
        -- 指向已回收并重分配给别的租户的 host:port，导致跨租户误路由。
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }

        balancer.balancer_pick()

        local last = package.loaded["ngx.balancer"]._last_peer
        assert.are.equal("10.0.7.7", last.host)
        assert.are.equal(11001, last.port)
        local connects = helper.fake_redis_connects()
        assert.is_true(contains(connects, "primary.redis.local"))
        assert.is_false(contains(connects, "reader.redis.local"))
    end)

    -- 防止重投依赖 nil 默认值维持权威语义：第 5 实参必须显式为 true。
    it("retry passes explicit authoritative true to lookup_backend", function()
        ngx.var.edge_redis_host = "primary.redis.local"
        ngx.var.edge_redis_port = "6379"
        ngx.ctx.tenant_id = "t-explicit-authoritative"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }

        local captured_authoritative
        local real_lookup = backend.lookup_backend
        backend.lookup_backend = function(_, _, _, _, authoritative)
            captured_authoritative = authoritative
            return {
                host = "10.0.7.7", port = 11001, guest_ip = "172.16.9.10",
            }, backend.SOURCE_L3, nil
        end

        local ok, err = pcall(balancer.balancer_pick)
        backend.lookup_backend = real_lookup

        assert.is_true(ok, tostring(err))
        assert.are.equal(true, captured_authoritative)
    end)
end)
