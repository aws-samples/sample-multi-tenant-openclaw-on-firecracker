-- deploy/edge/lib/balancer.lua
--
-- balancer_pick — pick the upstream peer for the current request.
-- Two branches per INTERFACE-CONTRACT §2:
--   local  descriptor.host == self_ip  → connect guest_ip:18789 directly
--   remote                             → connect host:port (peer's DNAT)
--
-- balancer_by_lua runs after rewrite_by_lua has stashed the descriptor
-- into ngx.ctx / ngx.var, so this file only reads request state and shared
-- route cache before calling set_current_peer.
-- Kept isolated because balancer_by_lua has a tight allow-list of
-- primitives (shared_dict is allowed, cosockets are not); the function stays
-- small enough to fit that.
--
-- self_ip is injected via ngx.var.edge_self_ip (populated by install-edge.sh
-- from the host's private IP at systemd unit start).

local balancer = require "ngx.balancer"
local _M = { _VERSION = "0.03" }

-- Guest gateway listens on a fixed port inside the microVM
-- (launch-vm.sh:747, deploy/userdata/launch-vm.sh — grep shows 18789).
local GUEST_GATEWAY_PORT = 18789
local CHAT_COMPLETIONS_URI = "/v1/chat/completions"
local CHAT_COMPLETIONS_READ_TIMEOUT_SECONDS = 60
-- 重投没有可用的**不同** peer 时给客户的状态码。503 而非 502:语义是"暂时不可用,请重试",
-- 与本文件其余几处 ngx.exit(503) 一致。它**不是**靠 balancer 阶段的 ngx.exit 生效的,
-- 见 balancer_pick 里那段注释与 _M.fixup_status。
local RETRY_EXHAUSTED_STATUS = 503

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

-- 同一 peer 必须同时匹配宿主机、DNAT 端口和 guest 地址；任一 desc 缺失时
-- 不视为相同，避免把一个可用的新共享缓存条目误丢掉。
local function same_peer(left, right)
    if left == nil or right == nil then return false end
    return left.host == right.host
        and left.port == right.port
        and left.guest_ip == right.guest_ip
end

--[[
    balancer_pick: called from balancer_by_lua. Reads the descriptor from
    ngx.ctx (set by rewrite phase) and calls ngx.balancer.set_current_peer.
    On any bookkeeping failure this exits 503 so the caller sees a real
    error instead of a silent proxy_pass to nowhere.

    R6.3② edge failover:如果这是重试 tick (get_last_failure 返回
    connection refused / timeout,说明第一次连旧 host 撞 RST 或超时),只
    从共享缓存采用一个与失败 peer 不同的新 desc,并对它重投。

    这里绝不能重读 Redis:balancer_by_lua* 禁用 cosocket,调用会直接抛错并
    把本应可重试的网关错误变成 500。重投要求的 read-after-write 没有放弃,
    而是通过一次性提示搬到下一次请求的 rewrite 阶段,由那里强制读 primary。
    重试次数 set_more_tries(1) 只重投一次。

    #628 —— 共享缓存里**没有**不同 peer 时,撤销 fresh 标记后 fail closed,
    而不是保留旧 desc 让第二次连接去撞。原来那条兜底假设"旧坐标已经死了,
    所以第二次会撞 RST 然后 502",但 route 键从不回收(#605 evidence:usw2
    实测 13 条残留中 2 条指向他人在役 VM),旧坐标可能已被回收并重分配给
    另一个**正在服务**的租户 —— 此时第二次连接会**成功**,客户被路由进别人
    的 microVM。真机实测过这一跳确实会发生:bb 上一版的 upstream 记录是
    `ua=127.0.0.1:1, 127.0.0.1:1`,同一个失败坐标被连了两次。
    不向任何坐标重投,这条保护就不依赖 Redis 复制一致性,也不依赖缓存新鲜度。

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

    -- R6.3② 检测是否为重试 tick:上一次 upstream 失败 → 只查共享缓存。
    -- get_last_failure 只在有前一次尝试失败时返回非 nil (nginx 官方语义)。
    local state, code = balancer.get_last_failure()
    if state then
        if not _M._retry_refresh_desc(ctx, state, code) then
            -- 没有可用的不同 peer(#628)。不重用已知失败的坐标,直接 fail closed。
            --
            -- 为什么要先写 ctx 再 exit:`balancer_by_lua*` 里 ngx.exit(503) 实际发给客户的
            -- 是 **500** —— nginx 在该阶段只能把"选 peer 失败"表达成 500,传进去的状态码到
            -- 不了客户端(openresty/lua-resty-core#70,标题即 "balancer_by_lua: cannot return
            -- status codes other than 500";agentzh 在该 issue 里给的官方解法就是这两段:
            -- balancer 阶段把想要的码存进 ngx.ctx,再在输出过滤器里改写)。状态码由
            -- _M.fixup_status() 在 header_filter 阶段落地,单看这里的 exit 参数会得到
            -- "已经返 503"的错误结论。
            ctx.edge_retry_status = RETRY_EXHAUSTED_STATUS
            return ngx.exit(RETRY_EXHAUSTED_STATUS)
        end
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

    -- The location-level 3600s timeout is intentional for native WebSockets.
    -- OpenAI-compatible chat is HTTP/SSE and must fail within a bounded budget
    -- when the model backend accepts a connection but never returns bytes.
    -- route.lua has already stripped /ws/<tenant>, so ngx.var.uri is the guest
    -- path here. nil preserves the configured connect/send timeout values.
    if ngx.var.uri == CHAT_COMPLETIONS_URI then
        local ok_timeout, timeout_err = balancer.set_timeouts(
            nil, nil, CHAT_COMPLETIONS_READ_TIMEOUT_SECONDS)
        if not ok_timeout then
            ngx.log(ngx.ERR, "balancer.set_timeouts for chat failed: ",
                tostring(timeout_err))
            return ngx.exit(503)
        end
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

-- Retry seam split out so tests can cover shared-cache failover without
-- ngx.balancer. 本阶段禁止 Redis/cosocket。
--
-- 返回值(#628):true = 已换到一个与失败 peer 不同的 desc,调用方可以重投;
-- false = 没有可用的不同 peer,调用方**必须** fail closed,不得重用旧 desc。
-- 用返回值而不是让调用方自己比较 ctx.route_desc,是为了让"换没换到"这个判断只有一处。
function _M._retry_refresh_desc(ctx, state, code)
    local tid = ctx.tenant_id
    if not tid then return false end
    ngx.log(ngx.WARN, "balancer retry for tenant ", tid,
        " (upstream failed state=", tostring(state), " code=", tostring(code),
        "); checking shared cache for a different peer")

    local backend = require "edge.lib.backend"
    local shared = ngx.shared.route_cache
    local cached = backend.peek_cached(shared, tid)
    if cached and not same_peer(cached, ctx.route_desc) then
        ctx.route_desc = cached
        ctx.route_source = backend.SOURCE_L2
        return true
    end

    backend.mark_retry_stale(shared, tid)
    -- 没有不同 peer:撤销 fresh 标记后交给调用方 fail closed。旧 desc 不重用 —— 那个坐标
    -- 可能已被回收并重分配给另一个在役租户(#605/#628),第二次连接会成功并造成跨租户误路由。
    -- mark_retry_stale 仍然要做:它让下一次请求的 rewrite 强制读 primary,拿到权威新路由。
    return false
end


-- 挂在 header_filter_by_lua*,把 balancer 阶段 fail closed 产生的 500 改写成本来想返的码。
--
-- 这是 openresty/lua-resty-core#70 里 agentzh 给的官方两段式做法。必要性:
-- `balancer_by_lua*` 阶段的 ngx.exit(<code>) 到客户端一律是 500,状态码传不出去;
-- 只有输出过滤器能改写已生成的响应头。真机差分验证过:少了这一段就是 500。
--
-- 只在 ngx.status 恰为 500 时改写:
--   · 上游真的返了 5xx(502/503/504 等)不动 —— 那是上游的真实状态,不能被覆盖;
--   · 上游成功(2xx/101)时不动 —— 正常 WS/SSE 流量绝不能被误改;
--   · 没有 ctx 标记时不动 —— 与本改动无关的 500(别处的 bug)保持原样,不冒充成 503。
function _M.fixup_status()
    local ctx = ngx.ctx
    if not ctx then return end
    local wanted = ctx.edge_retry_status
    if not wanted then return end
    if ngx.status ~= 500 then return end
    ngx.status = wanted
end


-- Exposed for tests.
_M._GUEST_GATEWAY_PORT = GUEST_GATEWAY_PORT
_M._same_peer = same_peer
_M._RETRY_EXHAUSTED_STATUS = RETRY_EXHAUSTED_STATUS

return _M
