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
# 判别性设计(两条臂,缺一不可):
#   ARM A = 仓库当前代码       → 期望:无 `API disabled`,失败对端得 503,换 route 后恢复 200
#   ARM B = 注入 pre-#606 形状 → 期望:**必须**出现 `API disabled` 且客户拿 500
# ARM B 是这个探针的自证:少了它,"没看到 API disabled"可能只是因为重投分支从没跑到。
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
# 退出码:0 = 两条臂都符合期望;1 = 有断言失败;2 = 环境不满足(SKIP)。
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
stop_instance() { # stop_instance <edge|peer> —— 只停指定实例。两条臂之间只重启 edge,
                  # 活对端必须一直在(早期版本连 peer 一起停,T2 就永远连不上)。
    [ -n "$WORK" ] || return 0
    if [ -f "$WORK/$1/logs/nginx.pid" ]; then
        kill -QUIT "$(cat "$WORK/$1/logs/nginx.pid")" 2>/dev/null
        sleep 1
    fi
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
# 占位符按 install-edge.sh 的同一份 envsubst 列表替换。其余字节保持原样,
# 下面用 diff 断言"没有第 4 类偏离"。
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
        -e 's#^worker_processes  *auto;#worker_processes 1;#'
}
render_conf >"$WORK/nginx.conf"

echo "== 保真度:渲染后与仓库 nginx.conf 的逐行差异 =="
diff "$EDGE_DIR/nginx.conf" "$WORK/nginx.conf" | grep '^[<>]' | sed 's/^/    /'
# 五个占位符现在渲染进【两处】:server 块的 set $edge_redis_*,以及 init_by_lua_block 里
# 交给 edge.lib.hints 的四个坐标(#639)。后者渲染后的行不含 edge_redis 字样,所以键名要
# 逐个列进白名单 —— 列的是这四个确定的键,不是放宽成任意行,其它偏离照旧判红。
UNEXPECTED="$(diff "$EDGE_DIR/nginx.conf" "$WORK/nginx.conf" | grep '^[<>]' \
    | grep -vcE 'error_log|access_log|worker_processes|ENGINE_REDIS|EDGE_SELF_IP|edge_redis|edge_self_ip|primary_host|primary_port|reader_host|reader_port')"
check "渲染只含声明过的 3 类偏离 + 5 个占位符(server 块 + init_by_lua_block 两处)" \
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


# ── ARM B:把 pre-#606 的形状放回 _retry_refresh_desc(只改这一个函数体) ──
cat >"$WORK/inject.lua" <<'INJECT'
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

deploy_lua() { # deploy_lua <arm: A|B>
    rm -rf "$LUALIB/edge"
    mkdir -p "$LUALIB/edge/lib"
    cp "$EDGE_DIR"/lib/*.lua "$LUALIB/edge/lib/"
    cp "$EDGE_DIR/route.lua" "$LUALIB/edge/"
    [ "$1" = "B" ] || return 0
    awk 'FNR==NR { inj = inj $0 ORS; next }
         /^function _M\._retry_refresh_desc/ { printf "%s", inj; skip = 1; next }
         skip && /^end$/ { skip = 0; next }
         skip { next }
         { print }' "$WORK/inject.lua" "$EDGE_DIR/lib/balancer.lua" \
        >"$LUALIB/edge/lib/balancer.lua"
    grep -q 'ARM-B injected pre-#606 shape' "$LUALIB/edge/lib/balancer.lua"
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
http_req() { # http_req <path> <ws?> → 原始响应
    local path="$1" ws="${2:-}"
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
    while IFS= read -r -t 8 line <&4; do printf '%s\n' "${line%$'\r'}"; done
    exec 4<&- 2>/dev/null; exec 4>&- 2>/dev/null
}
http_code() { http_req "$1" "${2:-}" | head -1 | awk '{print $2}'; }
ws_req() {  # ws_req <tid> → "<code> <body-last-line>"
    local raw; raw="$(http_req "/ws/$1" ws)"
    printf '%s %s' "$(printf '%s' "$raw" | head -1 | awk '{print $2}')" \
        "$(printf '%s' "$raw" | grep -c 'LIVE-PEER-OK')"
}
errlog() { cat "$WORK/edge-error.log" 2>/dev/null; }

run_arm() { # run_arm <A|B>
    local arm="$1"
    local tid="t-probe-$arm-$RANDOM"
    echo
    echo "== ARM $arm =="
    stop_instance edge
    if ! deploy_lua "$arm"; then
        check "ARM $arm: lua 部署(ARM B 注入生效)" 1 "注入后找不到标记"; return
    fi
    check "ARM $arm: lua 部署到 $LUALIB/edge(ARM B 注入已生效)" 0
    if ! start_edge; then
        check "ARM $arm: OpenResty 起来且 /healthz=200" 1 "$(errlog | tail -3)"; return
    fi
    check "ARM $arm: OpenResty 起来且 /healthz=200" 0
    # #639 门禁(此前只是一行 info):/healthz=200 必须是【探过 Redis】换来的。
    # 坐标通道一断,warmup_probe 会不探测就标 ready —— /healthz 200、只有一行日志、
    # 没有指标,#618 的 readiness 门就此形同虚设,而新起的实例会带着空缓存接流量。
    check "ARM $arm: /healthz=200 是探到 Redis 换来的(warmup ok 出现)" \
        "$(errlog | grep -qF 'edge warmup ok; healthz now 200' && echo 0 || echo 1)" \
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
    local r1 c1; r1="$(ws_req "$tid")"; c1="${r1% *}"
    echo "  T1 route→死端口($DEAD_PORT): http=$c1"

    # T2:route 换成活对端(模拟 rebuild/restore 完成)→ 下一次请求应恢复
    set_route "$tid" "127.0.0.1" "$PEER_PORT"
    local r2 c2 hit2; r2="$(ws_req "$tid")"; c2="${r2% *}"; hit2="${r2##* }"
    echo "  T2 route→活对端($PEER_PORT): http=$c2 live-peer-marker=$hit2"

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

    if [ "$arm" = "A" ]; then
        check "ARM A: balancer 阶段零 cosocket(无 API disabled)" \
            "$([ "$n_disabled" -eq 0 ] && echo 0 || echo 1)" "n_disabled=$n_disabled"
        check "ARM A: 无可用不同 peer 时客户拿 503 而不是 500(#628 fail-closed + fixup_status)" \
            "$([ "$c1" = "503" ] && echo 0 || echo 1)" "T1 http=$c1"
        check "ARM A: route 换新后一次请求内恢复到新 peer(重投提示在 rewrite 阶段兑现)" \
            "$([ "$c2" = "200" ] && [ "$hit2" -ge 1 ] && echo 0 || echo 1)" \
            "T2 http=$c2 marker=$hit2"
    else
        check "ARM B: 注入 pre-#606 形状后**必须**出现 API disabled(证明本探针能判别该缺陷)" \
            "$([ "$n_disabled" -ge 1 ] && echo 0 || echo 1)" "n_disabled=$n_disabled"
        check "ARM B: 该缺陷下客户拿 500(balancer 阶段抛错,状态码传不出去)" \
            "$([ "$c1" = "500" ] && echo 0 || echo 1)" "T1 http=$c1"
    fi
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
    check "ARM W: 探测失败被记下来(不是静默)" \
        "$(errlog | grep -qF 'edge warmup probe via' && echo 0 || echo 1)" \
        "$(errlog | tail -2)"
    stop_instance edge
}

# ── 静态:坐标通道不许再回到 env(#639 的漂移类别)────────────────────────
# 这两条是提交期就能判的:route.lua 若又去读 os.getenv,或 nginx.conf 的
# init_by_lua_block 漏了某个占位符,通道就又断一半,而运行时只表现为少一行日志。
n_getenv="$(grep -c 'os%.getenv("ENGINE_REDIS' "$EDGE_DIR/route.lua" || true)"
check "静态: route.lua 不再用 os.getenv 读 Redis 坐标(nginx 会抹掉 env)" \
    "$([ "$n_getenv" = "0" ] && echo 0 || echo 1)" "命中=$n_getenv"
n_hints=0
for ph in ENGINE_REDIS_HOST ENGINE_REDIS_PORT ENGINE_REDIS_READER_HOST ENGINE_REDIS_READER_PORT; do
    awk '/init_by_lua_block/{f=1} f&&/^    }/{f=0} f' "$EDGE_DIR/nginx.conf" \
        | grep -qF "\${$ph}" && n_hints=$((n_hints + 1))
done
check "静态: nginx.conf 的 init_by_lua_block 带齐四个坐标占位符" \
    "$([ "$n_hints" = "4" ] && echo 0 || echo 1)" "命中=$n_hints/4"

run_arm A
run_arm B
run_arm_warmup_negative

echo
echo "== 汇总 =="
echo "PASS=$PASSES FAIL=$FAILS"
if [ "$FAILS" -eq 0 ]; then echo "OK"; exit 0; fi
echo "FAIL $FAILS"; exit 1
