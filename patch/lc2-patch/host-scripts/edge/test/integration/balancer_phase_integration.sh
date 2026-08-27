#!/usr/bin/env bash
# deploy/edge/test/integration/balancer_phase_integration.sh
#
# 在**真实 OpenResty** 的 `balancer_by_lua*` 阶段内跑 edge 重投路径(#633)。
#
# 为什么需要它:busted 单测用 `spec_helper.new_fake_redis_module()` 替掉
# `resty.redis`,而假模块**没有阶段限制** —— 它模拟不出 OpenResty 对
# `balancer_by_lua*` 的 cosocket 禁令,所以 #606 那个"在 balancer 阶段重读 Redis"
# 的缺陷在单测里全绿通过,只有真机才炸(usw2 2026-08-24 实测 500)。
# 本脚本起真 OpenResty + 真 Redis,让阶段限制自己说话。
#
# 判别性设计(三条数据面臂,缺一不可):
#   ARM A = 仓库当前代码       → 期望:无 `API disabled`,失败对端得 503,换 route 后恢复 200
#   ARM B = 注入 pre-#606 形状 → 期望:**必须**出现 `API disabled` 且客户拿 500
#   ARM C = 注入 pre-#628 形状 → 期望:复用失败坐标,访问日志出现两个相同 upstream
# ARM B/C 是这个探针的自证:少了它们,"没看到缺陷"可能只是因为重投分支从没跑到。
#
# 上游契约:lua-resty-core/lib/ngx/balancer.md 要求把需要 cosocket 的解析(例:DNS)
# 放在 `access_by_lua*` 等更早阶段、经 `ngx.ctx` 传进 balancer;lua-nginx-module
# README 的 `ngx.shared.DICT` 与 `ngx.shared.DICT.set` 两处 context 都**列出**
# `balancer_by_lua*` —— 该阶段禁的是 cosocket,不是 shared_dict(读和写都允许)。
#
# 运行环境(两种,断言实现只有这一份):
#   · CI      : p2-edge-gate 本身就跑在 openresty/openresty:alpine 里,加一个
#               `services: redis:...` 即可,不需要 docker-in-docker。
#               REDIS_HOST=redis bash deploy/edge/test/integration/balancer_phase_integration.sh
#   · 本机 mac: bash deploy/edge/test/integration/run_local_docker.sh(起容器后调本脚本)
#
# 需要:openresty 可执行、bash、awk、envsubst(gettext)、可达的 Redis。
# 退出码:0 = 三条数据面臂与 readiness 负向臂都符合期望;1 = 有断言失败;
# 2 = 环境不满足(SKIP)。
#
# 安全:本脚本会把 lua 模块写进 /usr/local/openresty/lualib/edge/ 并起临时
# openresty 实例,只应在一次性容器/CI job 里跑。检测到 claw-edge.service 就拒绝,
# 除非显式设 OC_EDGE_PROBE_FORCE=1。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
EDGE_DIR="$REPO_ROOT/deploy/edge"
REDIS_HOST="${REDIS_HOST:-}"
REDIS_PORT="${REDIS_PORT:-6379}"
EDGE_PORT="${EDGE_PORT:-8080}"
PEER_PORT="${PEER_PORT:-10000}"
DEAD_PORT="${DEAD_PORT:-10099}"
BLACKHOLE_PORT="${BLACKHOLE_PORT:-10098}"
LUALIB="/usr/local/openresty/lualib"
OPENRESTY="${OPENRESTY:-}"

FAILS=0
PASSES=0
check() { # check <name> <exit-code> <detail>
    if [ "$2" -eq 0 ]; then
        printf '  ok   %s\n' "$1"; PASSES=$((PASSES + 1))
    else
        printf '  FAIL %s: %s\n' "$1" "${3:-}"; FAILS=$((FAILS + 1))
    fi
}

WORK=""
stop_instance() { # stop_instance <edge|peer> —— 只停指定实例并【等它真的退出】。
                  # 活对端必须一直在(早期版本连 peer 一起停,T2 就永远连不上)。
                  # 早期版本 kill -QUIT 之后只 sleep 1 就往下走:ARM B 的 worker 在
                  # balancer 阶段抛错后优雅退出要超过 1s,下一条臂 bind 9145 撞
                  # "Address in use",被报成"OpenResty 起不来"这种误导性的红。所以
                  # 轮询到进程真的消失,超时再降级成快速退出,最后才 KILL。
    [ -n "$WORK" ] || return 0
    local pidfile="$WORK/$1/logs/nginx.pid" pid sig
    [ -f "$pidfile" ] || return 0
    pid="$(cat "$pidfile" 2>/dev/null)"
    [ -n "$pid" ] || return 0
    for sig in QUIT TERM KILL; do
        kill "-$sig" "$pid" 2>/dev/null
        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || return 0
            sleep 1
        done
    done
    return 0
}
# shellcheck disable=SC2329  # 由下面的 trap 调用
cleanup() {
    stop_instance edge; stop_instance peer
    [ -n "${KEEP_WORK:-}" ] || rm -rf "$WORK"
}
trap cleanup EXIT

skip() { echo "SKIP: $*"; exit 2; }

# ── 环境前置 ──────────────────────────────────────────────────────────────
[ -n "$REDIS_HOST" ] || skip "需要 REDIS_HOST(CI 用 services 的 redis 主机名)"
if [ -z "$OPENRESTY" ]; then
    OPENRESTY="$(command -v openresty 2>/dev/null)"
    [ -n "$OPENRESTY" ] || OPENRESTY=/usr/local/openresty/bin/openresty
fi
[ -x "$OPENRESTY" ] || skip "找不到 openresty 可执行文件($OPENRESTY)"
command -v envsubst >/dev/null 2>&1 || skip "缺 envsubst(gettext);install-edge.sh 用的是同一渲染路径"
if [ -f /etc/systemd/system/claw-edge.service ] && [ -z "${OC_EDGE_PROBE_FORCE:-}" ]; then
    skip "检测到 claw-edge.service —— 这看起来是在役 edge 主机。本脚本会覆写 $LUALIB/edge/,拒绝执行"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/oc-edge-probe-XXXXXX")"
echo "== 0) 准备 =="
echo "repo      : $REPO_ROOT"
echo "commit    : ${OC_PROBE_COMMIT:-$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)}"
echo "openresty : $OPENRESTY ($("$OPENRESTY" -v 2>&1 | head -1))"
echo "redis     : $REDIS_HOST:$REDIS_PORT"
echo "workdir   : $WORK"

# ── Redis:直接说 RESP,不依赖 redis-cli ───────────────────────────────────
redis_send() { # redis_send <arg>... → 首行回复
    exec 3<>"/dev/tcp/$REDIS_HOST/$REDIS_PORT" 2>/dev/null || return 1
    { printf '*%d\r\n' "$#"
      for a in "$@"; do printf '$%d\r\n%s\r\n' "${#a}" "$a"; done
    } >&3
    local line
    IFS= read -r -t 5 line <&3
    exec 3<&- 2>/dev/null; exec 3>&- 2>/dev/null
    printf '%s' "${line%$'\r'}"
}
redis_dump() { # redis_dump <arg>... → 全部回复行
    exec 3<>"/dev/tcp/$REDIS_HOST/$REDIS_PORT" 2>/dev/null || return 1
    { printf '*%d\r\n' "$#"
      for a in "$@"; do printf '$%d\r\n%s\r\n' "${#a}" "$a"; done
    } >&3
    local line
    while IFS= read -r -t 1 line <&3; do printf '%s\n' "${line%$'\r'}"; done
    exec 3<&- 2>/dev/null; exec 3>&- 2>/dev/null
}

# 探针自证 1:Redis 真的能用(否则后面全 FAIL 会被误读成被测行为)
PING="$(redis_send PING)"
check "redis PING 自证" "$([ "$PING" = "+PONG" ] && echo 0 || echo 1)" "got=$PING"
[ "$PING" = "+PONG" ] || { echo "环境不可用,不继续"; exit 1; }

set_route() { # set_route <tid> <host> <port>
    redis_send SET "route:$1" \
        "{\"host\":\"$2\",\"port\":$3,\"guest_ip\":\"172.16.0.9\",\"updated_at\":1}" >/dev/null
}

# ── 渲染真实 nginx.conf,只做**声明过的**偏离 ─────────────────────────────
# 偏离清单(容器/CI 里没有 /dev/log,也不该跑 auto 个 worker):
#   1. error_log  syslog: → 文件
#   2. access_log syslog: → 文件
#   3. worker_processes auto → 1
#   4. proxy_read_timeout 3600s → ${EDGE_READ_TIMEOUT:-25}s:出口 2 需要读超时在
#      探针预算内触发;T1/T2/ARM B/ARM W 都不经过读超时,行为不受影响。
#      25s 不是随手取的:T3 里 R1 必须一直挂在黑洞上,直到 R2 真的把活对端写进
#      路由缓存 L2(R2 最多重试 3 次,最晚约 t+17s 收敛,见 T3 那段注释),而黑洞
#      fixture 只 hold 30s,所以这个值必须落在 (17, 30) 里。
# 占位符按 install-edge.sh 的同一份 envsubst 列表替换。其余字节保持原样,
# 下面用 diff 断言"没有第 5 类偏离"。
render_conf() { # render_conf [redis-host] [redis-port] —— 默认用真 Redis 坐标;
                # #639 的负向臂传一个死端口,证明探不到 Redis 时 /healthz 不翻 200。
    # shellcheck disable=SC2016  # envsubst 的白名单参数必须是字面量,不能展开
    ENGINE_REDIS_HOST="${1:-$REDIS_HOST}" ENGINE_REDIS_PORT="${2:-$REDIS_PORT}" \
    ENGINE_REDIS_READER_HOST="${1:-$REDIS_HOST}" ENGINE_REDIS_READER_PORT="${2:-$REDIS_PORT}" \
    EDGE_SELF_IP="10.255.255.255" \
    envsubst \
        '$ENGINE_REDIS_HOST $ENGINE_REDIS_PORT $ENGINE_REDIS_READER_HOST $ENGINE_REDIS_READER_PORT $EDGE_SELF_IP' \
        <"$EDGE_DIR/nginx.conf" \
    | sed \
        -e "s#^error_log syslog:.*#error_log $WORK/edge-error.log notice;#" \
        -e "s#^\( *\)access_log syslog:.*#\1access_log $WORK/edge-access.log edge_access;#" \
        -e 's#^worker_processes  *auto;#worker_processes 1;#' \
        -e "s#^\( *\)proxy_read_timeout  *3600s;#\1proxy_read_timeout ${EDGE_READ_TIMEOUT:-25}s;#"
}
render_conf >"$WORK/nginx.conf"

echo "== 保真度:渲染后与仓库 nginx.conf 的逐行差异 =="
diff "$EDGE_DIR/nginx.conf" "$WORK/nginx.conf" | grep '^[<>]' | sed 's/^/    /'
# 五个占位符现在渲染进【两处】:server 块的 set $edge_redis_*,以及 init_by_lua_block 里
# 交给 edge.lib.hints 的四个坐标(#639)。后者渲染后的行不含 edge_redis 字样,所以键名要
# 逐个列进白名单 —— 列的是这四个确定的键,不是放宽成任意行,其它偏离照旧判红。
UNEXPECTED="$(diff "$EDGE_DIR/nginx.conf" "$WORK/nginx.conf" | grep '^[<>]' \
    | grep -vcE 'error_log|access_log|worker_processes|proxy_read_timeout|ENGINE_REDIS|EDGE_SELF_IP|edge_redis|edge_self_ip|primary_host|primary_port|reader_host|reader_port')"
check "渲染只含声明过的 4 类偏离 + 5 个占位符(server 块 + init_by_lua_block 两处)" \
    "$([ "$UNEXPECTED" = "0" ] && echo 0 || echo 1)" "意外差异行=$UNEXPECTED"

# ── 活对端:返回可判别 body,用来证明重投真的换到了新 peer ─────────────────
mkdir -p "$WORK/peer/logs" "$WORK/peer/conf"
cat >"$WORK/peer/nginx.conf" <<PEERCONF
worker_processes 1;
error_log $WORK/peer-error.log notice;
events { worker_connections 64; }
http {
    access_log off;
    server {
        listen $PEER_PORT;
        location / { default_type text/plain; return 200 "LIVE-PEER-OK\n"; }
    }
    server {
        listen $BLACKHOLE_PORT;
        location / { content_by_lua_block { ngx.sleep(30) } }
    }
}
PEERCONF
"$OPENRESTY" -p "$WORK/peer" -c "$WORK/peer/nginx.conf" || skip "活对端起不来"

# 探针自证 2:活对端真的在监听。少了这条,T2 的失败会被误读成"重投没兑现",
# 实际只是对端没起来(早期版本就踩过:两条臂之间把 peer 一起停了)。
peer_probe() {
    exec 5<>"/dev/tcp/127.0.0.1/$PEER_PORT" 2>/dev/null || return 1
    printf 'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n' >&5
    local l out=""
    while IFS= read -r -t 5 l <&5; do out="$out$l"; done
    exec 5<&- 2>/dev/null; exec 5>&- 2>/dev/null
    case "$out" in *LIVE-PEER-OK*) return 0;; *) return 1;; esac
}
sleep 1
peer_probe
check "活对端 127.0.0.1:$PEER_PORT 自证可达(返回 LIVE-PEER-OK)" $?

blackhole_probe() {
    exec 6<>"/dev/tcp/127.0.0.1/$BLACKHOLE_PORT" 2>/dev/null || return 1
    printf 'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n' >&6
    local line rc
    IFS= read -r -t 5 line <&6
    rc=$?
    exec 6<&- 2>/dev/null; exec 6>&- 2>/dev/null
    [ "$rc" -ne 0 ]
}


# ── ARM B:把 pre-#606 的形状放回 _retry_refresh_desc(只改这一个函数体) ──
cat >"$WORK/inject-B.lua" <<'INJECT'
function _M._retry_refresh_desc(ctx, state, code)
    local tid = ctx.tenant_id
    if not tid then return false end
    ngx.log(ngx.WARN, "balancer retry for tenant ", tid,
        " (upstream failed state=", tostring(state), " code=", tostring(code),
        "); ARM-B injected pre-#606 shape: re-reading Redis from balancer phase")
    local redis_client = require "edge.lib.redis_client"
    local raw = redis_client.get_route(ngx.var.edge_redis_host,
        tonumber(ngx.var.edge_redis_port) or 6379, "route:" .. tid)
    if not raw then return false end
    local cjson = require "cjson.safe"
    local obj = cjson.decode(raw)
    if type(obj) ~= "table" then return false end
    ctx.route_desc = { host = obj.host, port = tonumber(obj.port),
        guest_ip = obj.guest_ip }
    return true
end
INJECT

# ── ARM C:把 pre-#628 的"复用旧坐标"形状放回 _retry_refresh_desc ───────
cat >"$WORK/inject-C.lua" <<'INJECT'
function _M._retry_refresh_desc(ctx, state, code)
    local tid = ctx.tenant_id
    if not tid then return false end
    ngx.log(ngx.WARN, "balancer retry for tenant ", tid,
        " (upstream failed state=", tostring(state), " code=", tostring(code),
        "); ARM-C injected pre-#628 shape: reuse old desc when no different peer")
    local backend = require "edge.lib.backend"
    local shared = ngx.shared.route_cache
    local cached = backend.peek_cached(shared, tid)
    if cached and not same_peer(cached, ctx.route_desc) then
        ctx.route_desc = cached
        ctx.route_source = backend.SOURCE_L2
        return true
    end
    backend.mark_retry_stale(shared, tid)
    -- pre-#628:保留旧 desc,让 set_current_peer 再撞一次同一个坐标。
    -- 50311a01 原版返回 nil;当前调用方把 nil 当 false 并 fail-closed,所以这里必须返回 true。
    return true
end
INJECT

deploy_lua() { # deploy_lua <arm: A|B|C>
    local arm="$1" inject="$WORK/inject-$1.lua" marker
    rm -rf "$LUALIB/edge"
    mkdir -p "$LUALIB/edge/lib"
    cp "$EDGE_DIR"/lib/*.lua "$LUALIB/edge/lib/"
    cp "$EDGE_DIR/route.lua" "$LUALIB/edge/"
    [ -f "$inject" ] || return 0
    case "$arm" in
        B) marker='ARM-B injected pre-#606 shape' ;;
        C) marker='ARM-C injected pre-#628 shape' ;;
        *) return 1 ;;
    esac
    awk 'FNR==NR { inj = inj $0 ORS; next }
         /^function _M\._retry_refresh_desc/ { printf "%s", inj; skip = 1; next }
         skip && /^end$/ { skip = 0; next }
         skip { next }
         { print }' "$inject" "$EDGE_DIR/lib/balancer.lua" \
        >"$LUALIB/edge/lib/balancer.lua"
    grep -qF "$marker" "$LUALIB/edge/lib/balancer.lua"
}

start_edge() { # start_edge [conf] —— 起 edge 并等 /healthz=200
    # 刻意【不】传 ENGINE_REDIS_*_HINT 环境变量(#639):nginx 会抹掉从父进程继承的
    # 环境(只留 TZ),除非每个名字都用 `env` 指令声明过 —— 所以那条通道从来没通。
    # 坐标现在走 nginx.conf 的 init_by_lua_block,由上面 render_conf 的同一次
    # envsubst 渲染进去,和 $edge_redis_* 同一条渠道。
    mkdir -p "$WORK/edge/logs" "$WORK/edge/conf"
    : >"$WORK/edge-error.log"
    "$OPENRESTY" -p "$WORK/edge" -c "${1:-$WORK/nginx.conf}" || return 1
    for _ in $(seq 1 30); do
        [ "$(http_code "/healthz")" = "200" ] && return 0
        sleep 1
    done
    return 1
}

# 只用 bash 内建 /dev/tcp 说 HTTP,避免依赖 curl/wget(openresty:alpine 不带 curl)。
http_req() { # http_req <path> <ws?> [read-timeout] → 原始响应
    local path="$1" ws="${2:-}" read_timeout="${3:-8}"
    exec 4<>"/dev/tcp/127.0.0.1/$EDGE_PORT" 2>/dev/null || return 1
    { printf 'GET %s HTTP/1.1\r\n' "$path"
      printf 'Host: 127.0.0.1:%s\r\n' "$EDGE_PORT"
      if [ -n "$ws" ]; then
          printf 'Connection: Upgrade\r\nUpgrade: websocket\r\n'
          printf 'Sec-WebSocket-Version: 13\r\n'
          # RFC 6455 §1.3 的示例 nonce(base64 of "the sample nonce")。握手必填字段,
          # 服务端只用它算 Sec-WebSocket-Accept,不是凭据 —— 但 gitleaks 的
          # generic-api-key 会因为 "Key:" + 高熵值命中,故就地豁免而不放宽路径 allowlist。
          printf 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'  # gitleaks:allow
      else
          printf 'Connection: close\r\n'
      fi
      printf '\r\n'
    } >&4
    local line
    while IFS= read -r -t "$read_timeout" line <&4; do printf '%s\n' "${line%$'\r'}"; done
    exec 4<&- 2>/dev/null; exec 4>&- 2>/dev/null
}
http_code() { http_req "$1" "${2:-}" | head -1 | awk '{print $2}'; }
ws_req() {  # ws_req <tid> [read-timeout] → "<code> <body-last-line>"
    local raw; raw="$(http_req "/ws/$1" ws "${2:-8}")"
    printf '%s %s' "$(printf '%s' "$raw" | head -1 | awk '{print $2}')" \
        "$(printf '%s' "$raw" | grep -c 'LIVE-PEER-OK')"
}
errlog() { cat "$WORK/edge-error.log" 2>/dev/null; }
wait_errlog() { # wait_errlog <固定串> [秒数] —— 【等】一条证据出现在 error.log,返回 0/1。
                # 为什么不能一次性 grep:warmup 探测是 route.lua 里 2s 一跳的异步
                # timer,实测第一条失败日志落在 nginx 启动后约 4s(09:42:36 起、
                # 09:42:40 才出第一条)。一次性采样会在负载高的机器上采在写入之前,
                # 而同一条断言的 detail(tail -2)在几微秒后才取,于是打印出
                # 【自相矛盾的红】:FAIL 的详情里明明就有要 grep 的那句话。
                # 2026-08-26 本机实测撞过一次(PASS=45 FAIL=1),同一份代码重跑即
                # PASS=46 FAIL=0 —— 观察窗口必须长于重试节拍,否则这条断言既能假红
                # 也能在真缺陷下假绿(日志永远不出现和"还没出现"分不开)。
                # 直接 grep 文件而不是 cat|grep:少一层管道,顺带避开 pipefail 下
                # grep -q 早退把 SIGPIPE 算进管道状态的那类坑。
    local pat="$1" secs="${2:-15}" _i
    for _i in $(seq 1 "$secs"); do
        grep -qF "$pat" "$WORK/edge-error.log" 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

run_arm() { # run_arm <A|B|C>
    local arm="$1"
    local tid="t-probe-$arm-$RANDOM"
    echo
    echo "== ARM $arm =="
    stop_instance edge
    if ! deploy_lua "$arm"; then
        check "ARM $arm: lua 部署(该 arm 注入标记生效)" 1 "注入后找不到标记"; return
    fi
    if [ -f "$WORK/inject-$arm.lua" ]; then
        check "ARM $arm: lua 部署到 $LUALIB/edge(该 arm 注入标记已核对)" 0
    else
        check "ARM $arm: lua 部署到 $LUALIB/edge(仓库当前代码)" 0
    fi
    if ! start_edge; then
        check "ARM $arm: OpenResty 起来且 /healthz=200" 1 "$(errlog | tail -3)"; return
    fi
    check "ARM $arm: OpenResty 起来且 /healthz=200" 0
    # #639 门禁(此前只是一行 info):/healthz=200 必须是【探过 Redis】换来的。
    # 坐标通道一断,warmup_probe 会不探测就标 ready —— /healthz 200、只有一行日志、
    # 没有指标,#618 的 readiness 门就此形同虚设,而新起的实例会带着空缓存接流量。
    check "ARM $arm: /healthz=200 是探到 Redis 换来的(warmup ok 出现)" \
        "$(wait_errlog 'edge warmup ok; healthz now 200' 5 && echo 0 || echo 1)" \
        "error.log 里没有 'edge warmup ok'"
    check "ARM $arm: warmup 没有走「无坐标直接标 ready」分支" \
        "$(errlog | grep -qF 'refusing to mark ready without a probe' && echo 1 || echo 0)" \
        "$(errlog | grep -F 'refusing to mark ready' | tail -1)"

    # 负控:未知租户必须 404 —— 证明请求真的走了 route.lua + Redis 的 MISS 分支
    local c_unknown; c_unknown="$(http_code "/ws/t-definitely-not-real-0000" ws)"
    check "ARM $arm: 负控 未知租户=404(证明探针打在 /ws 数据面且 Redis MISS 生效)" \
        "$([ "$c_unknown" = "404" ] && echo 0 || echo 1)" "got=$c_unknown"

    # T1:route 指向死端口 → 上游 connect refused → 必进重投分支
    set_route "$tid" "127.0.0.1" "$DEAD_PORT"
    : >"$WORK/edge-access.log"
    local r1 c1; r1="$(ws_req "$tid")"; c1="${r1% *}"
    sleep 1
    local t1_access t1_lines t1_upstream
    t1_access="$(cat "$WORK/edge-access.log" 2>/dev/null)"
    t1_lines="$(wc -l <"$WORK/edge-access.log" | tr -d ' ')"
    t1_upstream="$(printf '%s\n' "$t1_access" \
        | sed -n 's/.*"upstream_addr":"\([^"]*\)".*/\1/p')"
    echo "  T1 route→死端口($DEAD_PORT): http=$c1 ua=[$t1_upstream]"

    # T2:route 换成活对端(模拟 rebuild/restore 完成)→ 下一次请求应恢复
    set_route "$tid" "127.0.0.1" "$PEER_PORT"
    local r2 c2 hit2; r2="$(ws_req "$tid")"; c2="${r2% *}"; hit2="${r2##* }"
    echo "  T2 route→活对端($PEER_PORT): http=$c2 live-peer-marker=$hit2"

    if [ "$arm" = "A" ]; then
        local tid3="$tid-t3"
        blackhole_probe
        check "ARM A T3-1: 黑洞对端 127.0.0.1:$BLACKHOLE_PORT 自证(5s 内无状态行)" $?

        set_route "$tid3" "127.0.0.1" "$BLACKHOLE_PORT"
        : >"$WORK/edge-access.log"
        local t3_disabled_before t3_r1_pid
        t3_disabled_before="$(errlog | grep -c 'API disabled in the context of balancer_by_lua')"
        ( ws_req "$tid3" 35 >"$WORK/t3-r1.out" ) &
        t3_r1_pid=$!
        sleep 7
        set_route "$tid3" "127.0.0.1" "$PEER_PORT"

        # R2 要做的事是"重读 Redis 并把活对端写进路由缓存 L2",可它自己也可能仍落在
        # L1 / `f:` 新鲜标记的窗口里被服务旧值(两者 TTL 都以 POS_TTL_SEC=5s 为上限,
        # 且【从 R1 的 rewrite 起算】——而 R1 是后台子进程,起跑时刻不可观测)。
        # 2026-08-26 本机实测撞过一次:R2 被服务了黑洞坐标,8s 内读不到状态行(空
        # http),它把黑洞又写回 L2,于是 R1 的重投看不到不同 peer、走出口 1 拿 503,
        # 三条断言连锁变红(PASS=43 FAIL=3),同一份代码重跑即 PASS=46 FAIL=0。
        # 所以这里不赌单次 sleep 的余量,循环到 R2 真的重读了 Redis 为止:每次
        # 客户端读窗口 3s(200 的状态行是立刻到的,黑洞则等满 3s),最多 3 次、
        # 最晚约 t+17s 收敛,仍早于 R1 的上游读超时(EDGE_READ_TIMEOUT=25s)。
        local t3_r2 t3_c2 t3_hit2 t3_tries=0
        while [ "$t3_tries" -lt 3 ]; do
            t3_tries=$((t3_tries + 1))
            t3_r2="$(ws_req "$tid3" 3)"; t3_c2="${t3_r2% *}"; t3_hit2="${t3_r2##* }"
            [ "$t3_c2" = "200" ] && [ "${t3_hit2:-0}" -ge 1 ] && break
            sleep 1
        done
        check "ARM A T3 准备: R2 重读 Redis 后到达活对端(第 $t3_tries/3 次)" \
            "$([ "$t3_c2" = "200" ] && [ "${t3_hit2:-0}" -ge 1 ] && echo 0 || echo 1)" \
            "R2 http=$t3_c2 marker=${t3_hit2:-0}"
        wait "$t3_r1_pid"
        sleep 1

        local t3_r1 t3_c1 t3_hit1 t3_access
        local t3_disabled_after t3_disabled
        t3_r1="$(cat "$WORK/t3-r1.out" 2>/dev/null)"
        t3_c1="${t3_r1% *}"; t3_hit1="${t3_r1##* }"
        t3_access="$(cat "$WORK/edge-access.log" 2>/dev/null)"
        t3_disabled_after="$(errlog | grep -c 'API disabled in the context of balancer_by_lua')"
        t3_disabled=$((t3_disabled_after - t3_disabled_before))
        # 照 T1 的先例把实测值打出来:T3 断言里最容易红的是"R2 到底连到了谁",
        # 只报期望值的话红了只能靠重跑猜。
        echo "  T3 R1=$t3_c1/marker=${t3_hit1:-0} R2=$t3_c2/marker=${t3_hit2:-0}(第 $t3_tries/3 次) ua=[$(printf '%s\n' "$t3_access" \
            | sed -n 's/.*"upstream_addr":"\([^"]*\)".*/\1/p' | tr '\n' '|')]"

        check "ARM A T3-2: R1 读超时后采纳不同 peer 并返回 LIVE-PEER-OK" \
            "$([ "$t3_c1" = "200" ] && [ "${t3_hit1:-0}" -ge 1 ] && echo 0 || echo 1)" \
            "R1 http=$t3_c1 marker=${t3_hit1:-0}"
        check "ARM A T3-3: R1 upstream_addr 是黑洞后接活对端的两个不同地址" \
            "$(printf '%s\n' "$t3_access" \
                | grep -cF "\"upstream_addr\":\"127.0.0.1:$BLACKHOLE_PORT, 127.0.0.1:$PEER_PORT\"" \
                | awk '{print ($1 == 1) ? 0 : 1}')" \
            "expect=127.0.0.1:$BLACKHOLE_PORT, 127.0.0.1:$PEER_PORT"
        check "ARM A T3-4: 出口 2 窗口内零 cosocket(无 API disabled)" \
            "$([ "$t3_disabled" -eq 0 ] && echo 0 || echo 1)" "n_disabled=$t3_disabled"
        # 反空转:R2 自己那条行必须在,且 upstream_addr 恰好只有活对端一个地址。
        # 这里【不】断言总行数:R2 若重试过,失败的那几次仍挂在黑洞上,要等各自的
        # 25s 上游读超时才落 access_log,落的时刻在本段采样之后,总行数因此不确定。
        # 断"至少一行 ua 恰为活对端"比数行数更强也更稳:0 行照旧判红。
        check "ARM A T3-5: access_log 里有 R2 自己那条(ua 恰为活对端,反空转)" \
            "$(printf '%s\n' "$t3_access" \
                | grep -cF "\"upstream_addr\":\"127.0.0.1:$PEER_PORT\"" \
                | awk '{print ($1 >= 1) ? 0 : 1}')" \
            "expect≥1 行 ua=127.0.0.1:$PEER_PORT"
    fi

    local log n_disabled n_retry
    log="$(errlog)"
    n_disabled="$(printf '%s' "$log" | grep -c 'API disabled in the context of balancer_by_lua')"
    n_retry="$(printf '%s' "$log" | grep -c 'balancer retry for tenant')"
    echo "  error.log: 'API disabled'×$n_disabled  'balancer retry for tenant'×$n_retry"

    # 反假绿:重投分支必须真的跑过,否则"没有 API disabled"毫无意义
    check "ARM $arm: 重投分支确实执行过(error.log 有 balancer retry)" \
        "$([ "$n_retry" -ge 1 ] && echo 0 || echo 1)" "n_retry=$n_retry"

    # 反假绿:从 Redis 侧证明 edge 真的发过 GET(不是被 shdict/负缓存全接住)
    local n_get
    n_get="$(redis_dump INFO commandstats | grep -o 'cmdstat_get:calls=[0-9]*' | head -1 | cut -d= -f2)"
    check "ARM $arm: Redis 侧看到真实 GET 调用(证明走的是真 Redis 不是假模块)" \
        "$([ -n "$n_get" ] && [ "$n_get" -ge 1 ] && echo 0 || echo 1)" \
        "cmdstat_get:calls=${n_get:-0}"

    case "$arm" in
        A)
            check "ARM A: balancer 阶段零 cosocket(无 API disabled)" \
                "$([ "$n_disabled" -eq 0 ] && echo 0 || echo 1)" "n_disabled=$n_disabled"
            check "ARM A: T1 access_log 截断窗口恰有一行(0 行不得假绿)" \
                "$([ "$t1_lines" = "1" ] && echo 0 || echo 1)" "lines=$t1_lines"
            # nginx 的 $upstream_addr 用 ", " 连接同一次 upstream 里换过的 peer,用
            # " : " 表示一段【始终没分配到 peer】的 upstream state。#628 修好之后实测值
            # 是 "127.0.0.1:<dead> : ":第一次连接落在死坐标,重投那一格因为 balancer
            # 阶段 ngx.exit 而从来没拿到坐标 —— 这比"只有一个地址"更强,直接证明第二次
            # 连接没有发生。所以断言写成结构判据(前缀 + 只出现一个地址 + 无 ", "),
            # 不逐字相等,免得挂在 nginx 拼出来的末尾空格上。
            local t1_n_addr t1_rc=1
            t1_n_addr="$(printf '%s' "$t1_upstream" | grep -o '127\.0\.0\.1:' | wc -l | tr -d ' ')"
            case "$t1_upstream" in
                "127.0.0.1:$DEAD_PORT"*)
                    case "$t1_upstream" in
                        *", "*) ;;
                        *) [ "$t1_n_addr" = "1" ] && t1_rc=0 ;;
                    esac ;;
            esac
            check "ARM A: T1 upstream_addr 只有失败坐标且没有第二次连接" "$t1_rc" \
                "got=[$t1_upstream] n_addr=$t1_n_addr"
            check "ARM A: 无可用不同 peer 时客户拿 503 而不是 500(#628 fail-closed + fixup_status)" \
                "$([ "$c1" = "503" ] && echo 0 || echo 1)" "T1 http=$c1"
            check "ARM A: route 换新后一次请求内恢复到新 peer(重投提示在 rewrite 阶段兑现)" \
                "$([ "$c2" = "200" ] && [ "$hit2" -ge 1 ] && echo 0 || echo 1)" \
                "T2 http=$c2 marker=$hit2"
            ;;
        B)
            check "ARM B: 注入 pre-#606 形状后**必须**出现 API disabled(证明本探针能判别该缺陷)" \
                "$([ "$n_disabled" -ge 1 ] && echo 0 || echo 1)" "n_disabled=$n_disabled"
            check "ARM B: 该缺陷下客户拿 500(balancer 阶段抛错,状态码传不出去)" \
                "$([ "$c1" = "500" ] && echo 0 || echo 1)" "T1 http=$c1"
            ;;
        C)
            check "ARM C: 注入 pre-#628 形状后客户拿 502(复用失败坐标)" \
                "$([ "$c1" = "502" ] && echo 0 || echo 1)" "T1 http=$c1"
            check "ARM C: T1 upstream_addr 是两个完全相同的失败坐标" \
                "$([ "$t1_upstream" = "127.0.0.1:$DEAD_PORT, 127.0.0.1:$DEAD_PORT" ] \
                    && echo 0 || echo 1)" "got=$t1_upstream"
            check "ARM C: 坐标复用路径零 cosocket(无 API disabled)" \
                "$([ "$n_disabled" -eq 0 ] && echo 0 || echo 1)" "n_disabled=$n_disabled"
            check "ARM C: route 换新后仍恢复到活对端(变异只改重投出口)" \
                "$([ "$c2" = "200" ] && [ "$hit2" -ge 1 ] && echo 0 || echo 1)" \
                "T2 http=$c2 marker=$hit2"
            ;;
    esac
    printf '%s\n' "$log" | grep -E 'API disabled|balancer retry|failed to run balancer' \
        | tail -4 | sed 's/^/      | /'
}

# ── ARM W(#639):Redis 不可达时 readiness 门必须挡住 ─────────────────────
# 正向的 "warmup ok" 断言单独不够:坐标通道断掉时它只是缺一行日志,而这里要证明
# 门【真的会挡】。把坐标指到一个死端口(真 Redis 仍在跑,只是 edge 连不上它),
# /healthz 在整个观察窗口内都不许翻 200。
# 修前形状:坐标恒 nil → 第一次 tick 就 mark_ready → t≈0 就 200,本臂必红。
run_arm_warmup_negative() {
    echo
    echo "== ARM W(#639 负向:Redis 不可达)=="
    stop_instance edge
    deploy_lua A || { check "ARM W: lua 部署" 1 "部署失败"; return; }
    render_conf "127.0.0.1" "$DEAD_PORT" >"$WORK/nginx-deadredis.conf"
    mkdir -p "$WORK/edge/logs" "$WORK/edge/conf"
    : >"$WORK/edge-error.log"
    if ! "$OPENRESTY" -p "$WORK/edge" -c "$WORK/nginx-deadredis.conf"; then
        check "ARM W: OpenResty 起得来(坐标坏不该让 nginx 起不来)" 1 "$(errlog | tail -3)"
        return
    fi
    check "ARM W: OpenResty 起得来(坐标坏不该让 nginx 起不来)" 0
    # 观察 8 秒:warmup 的重试间隔是 2s,15 次后才 fail-open 兜底(30s),
    # 所以 8 秒窗口内 200 只可能来自「不探测直接标 ready」。
    local seen_200=0 code
    for _ in $(seq 1 8); do
        code="$(http_code "/healthz")"
        [ "$code" = "200" ] && { seen_200=1; break; }
        sleep 1
    done
    check "ARM W: Redis 不可达时 /healthz 不返 200(readiness 门真的挡住)" \
        "$seen_200" "8s 窗口内 /healthz 已经是 200(got=$code)"
    # 20s 远大于 2s 的重试节拍,也大于实测的首条日志延迟(约 4s);15 次后
    # (约 30s)才走 fail-open 兜底,所以这个窗口不会把 give-up 那条误当证据。
    check "ARM W: 探测失败被记下来(不是静默)" \
        "$(wait_errlog 'edge warmup probe via' 20 && echo 0 || echo 1)" \
        "$(errlog | tail -2)"
    stop_instance edge
}

# ── 静态:坐标通道不许再回到 env(#639 的漂移类别)────────────────────────
# 这两条是提交期就能判的:route.lua 若又去读 os.getenv,或 nginx.conf 的
# init_by_lua_block 漏了某个占位符,通道就又断一半,而运行时只表现为少一行日志。
n_getenv="$(grep -c 'os%.getenv("ENGINE_REDIS' "$EDGE_DIR/route.lua" || true)"
check "静态: route.lua 不再用 os.getenv 读 Redis 坐标(nginx 会抹掉 env)" \
    "$([ "$n_getenv" = "0" ] && echo 0 || echo 1)" "命中=$n_getenv"
#
# #658:占位符这条断言此前是 flaky —— 同一份 nginx.conf(blob d92490b9)在 2026-08-26
# 的四个 bb job 里给出两种结论:27974877 与 27975616 判「命中=3/4」FAIL,27975951 与
# 27976450 判 4/4 PASS。它既能随机拦下与改动无关的 MR,也能在真该红时随机判绿。
# 判定逻辑因此抽成 count_hint_placeholders(),并配三条自证臂锁住它的性质:
#   可重复(同一输入判定恒定)、能识块(块外出现不算)、真缺占位符必须判红。
# 实现要点(两处都是 flaky 的来源,一起去掉):
#   1. 不再 `awk 窗口 | grep -qF`。脚本全局 `set -uo pipefail`,`grep -q` 命中即退出,
#      awk 的后续写入拿到 EPIPE 后以非零码死掉,pipefail 于是把整个管道判成失败,
#      `&&` 不加计数 —— 该占位符被漏计。是否发生取决于 grep 退出与 awk 下一次写入
#      的先后,所以同一份文件会给出不同结论(runner 实测 40 轮里出现 3 和 4 两种)。
#      改成单个 awk 进程算完,没有管道,退出码只有它自己的。
#   2. 块边界不再按 /^    }/ 猜缩进,改成花括号配对计数:块内只要出现一行缩进恰好
#      四格的闭合花括号(嵌套 Lua 表就会),旧写法立刻收窗,后面的占位符全数不到。
count_hint_placeholders() { # count_hint_placeholders <nginx.conf> → 命中数(0-4)
    awk '
        BEGIN {
            split("ENGINE_REDIS_HOST ENGINE_REDIS_PORT ENGINE_REDIS_READER_HOST ENGINE_REDIS_READER_PORT", nm, " ")
            for (i = 1; i <= 4; i++) want[i] = "${" nm[i] "}"
        }
        {
            line = $0
            if (!inblk) {
                if (line ~ /^[ \t]*#/) next                      # 注释里提到指令名不算开块
                if (index(line, "init_by_lua_block") == 0) next
                inblk = 1; depth = 0
            }
            for (i = 1; i <= 4; i++) if (index(line, want[i])) seen[i] = 1
            opens = gsub(/\{/, "{")                              # 替换成自身:只取计数,不改内容
            closes = gsub(/\}/, "}")
            depth += opens - closes
            if (depth <= 0) inblk = 0
        }
        END { n = 0; for (i = 1; i <= 4; i++) if (i in seen) n++; printf "%d", n }
    ' "$1"
}
n_hints="$(count_hint_placeholders "$EDGE_DIR/nginx.conf")"
check "静态: nginx.conf 的 init_by_lua_block 带齐四个坐标占位符" \
    "$([ "$n_hints" = "4" ] && echo 0 || echo 1)" "命中=$n_hints/4"

# 自证臂 1(可重复):同一份文件连判 N 轮,命中数必须只出现一个取值。
# 这条才是 #658 的回归 —— 单次判定看不出 flaky,只有重复判定的取值集合能。
HINT_ROUNDS="${OC_EDGE_HINT_ROUNDS:-40}"
: >"$WORK/hint-rounds.txt"
for _i in $(seq 1 "$HINT_ROUNDS"); do
    printf '%s\n' "$(count_hint_placeholders "$EDGE_DIR/nginx.conf")" >>"$WORK/hint-rounds.txt"
done
hint_set="$(sort -u "$WORK/hint-rounds.txt" | tr '\n' ' ')"
check "自证: 占位符判定可重复($HINT_ROUNDS 轮取值集合只有一个元素,#658)" \
    "$([ "$(sort -u "$WORK/hint-rounds.txt" | wc -l | tr -d ' ')" = "1" ] && echo 0 || echo 1)" \
    "$HINT_ROUNDS 轮观测到的命中数集合=[$hint_set]"

# 自证臂 2(能识块):把整块摘掉、只把四个占位符留在块外,必须判 0。
# 少了这条,判定退化成全文 grep 也能一直绿,而通道其实已经断了。
sed '/init_by_lua_block/,/^    }/d' "$EDGE_DIR/nginx.conf" >"$WORK/hint-outside.conf"
# shellcheck disable=SC2016  # 占位符要的就是字面量,不能展开
printf '# ${ENGINE_REDIS_HOST} ${ENGINE_REDIS_PORT} ${ENGINE_REDIS_READER_HOST} ${ENGINE_REDIS_READER_PORT}\n' \
    >>"$WORK/hint-outside.conf"
n_outside="$(count_hint_placeholders "$WORK/hint-outside.conf")"
check "自证: 占位符只出现在 init_by_lua_block 之外时判 0(不许退化成全文 grep)" \
    "$([ "$n_outside" = "0" ] && echo 0 || echo 1)" "命中=$n_outside/4,期望 0"

# 自证臂 3(块内嵌套花括号):块内插入一对配平的嵌套花括号,其中闭合行的缩进恰好四格。
# 旧实现按 /^    }/ 关窗,会在这里提前收窗、把后面的占位符全数不到(判 0);块界定
# 按花括号配对计数才能仍判 4。这份 fixture 只喂给静态计数,不喂 openresty。
awk '{ print }
     !ins && index($0, "init_by_lua_block") { print "        local _probe = {"; print "    }"; ins = 1 }' \
    "$EDGE_DIR/nginx.conf" >"$WORK/hint-nested.conf"
n_nested="$(count_hint_placeholders "$WORK/hint-nested.conf")"
check "自证: 块内出现缩进四格的嵌套闭合花括号时仍判 4(不靠缩进猜块边界)" \
    "$([ "$n_nested" = "4" ] && echo 0 || echo 1)" "命中=$n_nested/4,期望 4"

# 反向臂:真抠掉一个坐标占位符必须判红 —— 一条改动前后都通过的断言不算判据。
sed '/reader_port  *= /d' "$EDGE_DIR/nginx.conf" >"$WORK/hint-missing.conf"
n_missing="$(count_hint_placeholders "$WORK/hint-missing.conf")"
check "反向: 抠掉一个坐标占位符后必须判不齐(证明这条门不是恒绿)" \
    "$([ "$n_missing" = "3" ] && echo 0 || echo 1)" "命中=$n_missing/4,期望 3"

run_arm A
run_arm B
run_arm C
run_arm_warmup_negative

echo
echo "== 汇总 =="
echo "PASS=$PASSES FAIL=$FAILS"
if [ "$FAILS" -eq 0 ]; then echo "OK"; exit 0; fi
echo "FAIL $FAILS"; exit 1
