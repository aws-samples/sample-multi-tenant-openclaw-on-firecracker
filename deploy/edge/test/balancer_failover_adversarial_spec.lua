-- deploy/edge/test/balancer_failover_spec.lua
--
-- R6.3② edge failover 对抗测试。
-- 覆盖:
--   ① 首次调用:set_more_tries(1) 允许一次重投,不清缓存
--   ② 重试 tick 只读共享缓存,已有不同 peer 时采用它,全程不连 Redis
--   ③ 没有不同 peer 时**不重用旧 desc**:标记下一次 rewrite 强制读 primary 后 fail closed,
--      客户拿 503(#628)。旧坐标可能已被回收并重分配给另一个在役租户,重用会造成跨租户
--      误路由 —— #605 evidence 实测 13 条残留 route 中 2 条指向他人在役 VM,而真机实测
--      过重投确实会对同一个失败坐标再连一次(`ua=127.0.0.1:1, 127.0.0.1:1`)。

local helper = require "spec_helper"
local balancer = require "edge.lib.balancer"
local backend = require "edge.lib.backend"
local redis_client = require "edge.lib.redis_client"

-- 重投没换到不同 peer 时,balancer_pick 走 ngx.exit;spec_helper 的 exit 会抛
-- { ngx_exit = true, status = code }。
--
-- 注意 err.status **不是**客户可见状态码:`balancer_by_lua*` 里的 ngx.exit(<code>) 到客户端
-- 一律是 500(openresty/lua-resty-core#70),状态码传不出去。只断言 err.status 会得到假绿 ——
-- 断言的是传进去的参数,不是客户看到的东西。客户可见状态码由两段决定:balancer 阶段写
-- ngx.ctx.edge_retry_status,header_filter 阶段 fixup_status 把 nginx 生成的 500 改写过来。
-- 所以这里复刻 nginx 的行为:exit 之后把 ngx.status 置成 500,再跑 fixup_status,返回最终值。
local function client_visible_status_after_failed_retry()
    local ok, err = pcall(balancer.balancer_pick)
    assert.is_false(ok)
    assert.are.equal("table", type(err))
    assert.is_true(err.ngx_exit)
    assert.are.equal(503, ngx.ctx.edge_retry_status)
    ngx.status = 500  -- nginx 在 balancer 阶段 exit 后生成的就是 500
    balancer.fixup_status()
    return ngx.status
end

describe("balancer R6.3 edge failover", function()
    before_each(function()
        helper.reset_ngx()
        backend.init_worker()
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())
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

    -- 原来钉“清缓存后重查 Redis 拿新 peer”；现在钉共享缓存已有不同 peer
    -- 就直接采用且零 Redis 连接。原契约不成立，因为 balancer 阶段禁用 cosocket。
    it("retry tick adopts a different peer already in shared cache without Redis", function()
        ngx.var.edge_self_ip = "10.0.0.1"
        ngx.ctx.tenant_id = "t-mig"
        -- 模拟旧 desc:route 仍指源 host
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        -- 别的请求已在 rewrite 阶段把 target 写入 L2。
        local shared = ngx.shared.route_cache
        shared:set("r:t-mig",
            '{"host":"10.0.7.7","port":11001,"guest_ip":"172.16.9.10"}', 60)

        -- 模拟上一次 upstream 失败
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }
        helper.set_phase("balancer")

        balancer.balancer_pick()

        assert.are.equal(0, #helper.fake_redis_connects())
        assert.are.equal(backend.SOURCE_L2, ngx.ctx.route_source)
        assert.are.equal("10.0.7.7", ngx.ctx.route_desc.host)
        local last = package.loaded["ngx.balancer"]._last_peer
        assert.are.equal("10.0.7.7", last.host)
        assert.are.equal(11001, last.port)
    end)

    -- 原来钉“Redis 报错时保留旧 desc”；现在钉没有不同缓存 peer 时不连 Redis，
    -- 保留 fail-static blob、删 fresh 并写提示。原路径会在 Redis new() 直接抛 500。
    -- 原来钉"没有不同 peer 时保留旧 desc 让 nginx 撞第二次后 502"。#628 翻转:不重用旧坐标,
    -- fail closed 返 503。fail-static 材料与 primary 提示的语义保持不变,仍然逐条断言。
    it("retry tick without a new peer fails closed and never reuses the failed peer", function()
        ngx.var.edge_self_ip = "10.0.0.1"
        ngx.ctx.tenant_id = "t-bad"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        local shared = ngx.shared.route_cache
        shared:set("r:t-bad",
            '{"host":"10.0.9.9","port":10042,"guest_ip":"172.16.0.6"}', 60)
        shared:set("f:t-bad", "1", 5)

        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }
        helper.set_phase("balancer")

        assert.are.equal(503, client_visible_status_after_failed_retry())

        -- 不重用那个已知失败的坐标:一次 set_current_peer 都不许有(#628)
        assert.is_nil(package.loaded["ngx.balancer"]._last_peer)
        -- bb 的 fail-static 语义不变:blob 留着、fresh 撤销、primary 提示写入
        assert.is_not_nil(shared:get("r:t-bad"))
        assert.is_nil(shared:get("f:t-bad"))
        assert.is_not_nil(shared:get("p:t-bad"))
        assert.are.equal(0, #helper.fake_redis_connects())
    end)
end)

describe("balancer failover no-cross-tenant isolation", function()
    before_each(function()
        helper.reset_ngx()
        backend.init_worker()
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())
    end)

    -- 原来钉“重投读 primary 避开 reader victim slot”；现在钉负缓存优先，
    -- 即使 L2 有不同 peer 也不得采用。原来的 Redis 读取在 balancer 阶段不可能成功。
    it("negative cache prevents retry from adopting an old victim-slot blob", function()
        ngx.ctx.tenant_id = "t-migrating"
        local victim_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        ngx.ctx.route_desc = victim_desc
        local shared = ngx.shared.route_cache
        shared:set("r:t-migrating",
            '{"host":"10.0.7.7","port":11001,"guest_ip":"172.16.9.10"}', 60)
        shared:set("f:t-migrating", "1", 5)
        shared:set("n:t-migrating", "1", 2)
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }
        helper.set_phase("balancer")

        assert.are.equal(503, client_visible_status_after_failed_retry())

        -- 负缓存挡住采用 L2 里那个 blob(bb 的语义,不变);#628 再加一层:也不回落到
        -- victim_desc 本身 —— 一个 set_current_peer 都没有,这条隔离因此不依赖任何缓存判断。
        assert.is_nil(package.loaded["ngx.balancer"]._last_peer)
        assert.is_not_nil(shared:get("r:t-migrating"))
        assert.is_not_nil(shared:get("n:t-migrating"))
        assert.are.equal(0, #helper.fake_redis_connects())
    end)

    -- 原来钉“reader clean miss 时重投只碰 primary”；现在钉重投既不碰 reader
    -- 也不碰 primary。原契约忽略了 balancer 阶段所有 cosocket 都被禁用。
    it("retry touches neither reader nor primary", function()
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
        helper.set_phase("balancer")

        assert.are.equal(503, client_visible_status_after_failed_retry())

        -- 既不碰 reader 也不碰 primary(bb 的语义),且不回落到那个失败坐标(#628)
        assert.is_nil(package.loaded["ngx.balancer"]._last_peer)
        assert.are.equal(0, #helper.fake_redis_connects())
    end)

    -- 原来钉“lookup_backend 显式收 authoritative=true”；现在钉该函数完全
    -- 不在重投路径。原断言已作废，因为任何 lookup 都会触发禁用的 cosocket。
    it("retry never calls lookup_backend", function()
        ngx.ctx.tenant_id = "t-explicit-authoritative"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }

        local real_lookup = backend.lookup_backend
        backend.lookup_backend = function()
            error("lookup_backend must not run in balancer phase")
        end
        helper.set_phase("balancer")

        -- 这里不能直接 pcall balancer_pick 就断言 is_true:#628 之后它会 ngx.exit(也是抛),
        -- 那样 lookup_backend 的 error 和 ngx.exit 的抛就分不清了。用 helper 断言抛的是
        -- ngx_exit 标记而不是 lookup_backend 那条 error,才真正证明没走 lookup。
        local status = client_visible_status_after_failed_retry()
        backend.lookup_backend = real_lookup

        assert.are.equal(503, status)
        assert.are.equal(0, #helper.fake_redis_connects())
    end)
end)

describe("balancer phase Redis fidelity", function()
    before_each(function()
        helper.reset_ngx()
        backend.init_worker()
        redis_client._set_redis_module(helper.new_fake_redis_module())
        backend._set_lock_module(helper.new_fake_lock_module())
    end)

    -- 防止测试夹具再次把 balancer 阶段连 Redis 伪装成合法操作。
    it("fake Redis rejects cosocket use in balancer phase", function()
        helper.set_phase("balancer")

        local ok, err = pcall(
            redis_client.get_route, "redis.local", 6379, "route:t-phase")

        assert.is_false(ok)
        assert.is_not_nil(string.find(tostring(err),
            "API disabled in the context of balancer_by_lua*", 1, true))
    end)

    -- 防止完整重投 tick 回归到 lookup_backend,并由 Lua 抛错把网关错误变成 500。
    -- #628 之后 balancer_pick 在没有不同 peer 时走 ngx.exit,所以"不抛"要精确成
    -- "抛的是 ngx_exit 而不是 Lua 运行错误" —— 真机上后者才是那个 500。
    it("full retry tick raises only ngx.exit, never a Lua error, in balancer phase", function()
        ngx.ctx.tenant_id = "t-regression"
        ngx.ctx.route_desc = {
            host = "10.0.9.9", port = 10042, guest_ip = "172.16.0.6",
        }
        package.loaded["ngx.balancer"]._last_failure = {
            state = "failed", code = 502,
        }
        helper.set_phase("balancer")

        local ok, err = pcall(balancer.balancer_pick)

        assert.is_false(ok)
        -- 关键区分:table + ngx_exit 是受控退出;string 才是 Lua 抛错(真机 500)
        assert.are.equal("table", type(err), "expected ngx.exit, got a Lua error: "
            .. tostring(err))
        assert.is_true(err.ngx_exit)
        assert.are.equal(0, #helper.fake_redis_connects())
        assert.is_nil(package.loaded["ngx.balancer"]._last_peer)
    end)
end)

-- `balancer_by_lua*` 里的 ngx.exit(<code>) 到客户端一律是 500,状态码传不出去
-- (openresty/lua-resty-core#70)。所以客户能不能看到 503 取决于 header_filter 阶段的
-- fixup_status,而不是那个 exit 参数。这一组锁住它的每一条边界 —— 少任何一条,改写就会
-- 覆盖到不该覆盖的响应上。
describe("balancer.fixup_status", function()
    before_each(function()
        helper.reset_ngx()
    end)

    it("rewrites the balancer-generated 500 into the wanted status", function()
        ngx.ctx.edge_retry_status = 503
        ngx.status = 500

        balancer.fixup_status()

        assert.are.equal(503, ngx.status)
    end)

    it("leaves a real upstream 5xx alone", function()
        -- 上游自己返的 502/504 是真实状态,不能被本机制覆盖成 503
        ngx.ctx.edge_retry_status = 503
        ngx.status = 502

        balancer.fixup_status()

        assert.are.equal(502, ngx.status)
    end)

    it("leaves a successful upgrade or response alone", function()
        -- 正常 WS 握手 101 / 普通 200 绝不能被改
        for _, ok_status in ipairs({ 101, 200 }) do
            helper.reset_ngx()
            ngx.ctx.edge_retry_status = 503
            ngx.status = ok_status

            balancer.fixup_status()

            assert.are.equal(ok_status, ngx.status)
        end
    end)

    it("leaves an unrelated 500 alone when the balancer left no marker", function()
        -- 别处的 bug 产生的 500 不许被冒充成"暂时不可用,请重试"
        ngx.status = 500

        balancer.fixup_status()

        assert.are.equal(500, ngx.status)
    end)
end)
