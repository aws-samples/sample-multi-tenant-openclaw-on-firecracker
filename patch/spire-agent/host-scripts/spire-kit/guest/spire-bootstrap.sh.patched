#!/bin/bash
# spire-bootstrap.sh —— guest 侧引导:向 host broker 取一次性 join token 后拉起 spire-agent
#
# 为什么不是 MMDS:MMDS 只能 pre-boot 配,要改 launch-vm.sh(全租户启动路径上风险最高
# 的文件)。这里走【已经存在】的 /30 tap 链路 —— guest 的默认网关就是自己 tap 的 host
# 端 IP,broker 用"目的 IP + 源 IP"把请求钉到唯一一台 VM。详见 ../spire-join-broker.py。
#
# 落点(全部在【数据盘】/home/agent 上,rootfs 一个字节都不改):
#   ~/.spire-kit/bin/spire-agent        二进制
#   ~/.spire-kit/agent.conf.tmpl        配置模板(server 地址由 broker 回传,不硬编码)
#   ~/.spire-kit/state/                 agent data_dir
#   ~/.spire-kit/run/agent.sock         Workload API socket(OpenClaw 连这里)
#   ~/.spire-kit/log/                   日志
#
# 以 uid 1000(agent)运行,不需要 root、不需要改任何既有 systemd unit。
# 它是独立 user unit:失败只影响自己,OpenClaw gateway 不依赖它,不会因它起不来而降级。

set -uo pipefail

KIT_DIR="${SPIRE_KIT_DIR:-$HOME/.spire-kit}"
BIN="${SPIRE_KIT_AGENT_BIN:-$KIT_DIR/bin/spire-agent}"
CONF_TMPL="${SPIRE_KIT_CONF_TMPL:-$KIT_DIR/agent.conf.tmpl}"
CONF_OUT="${SPIRE_KIT_CONF_OUT:-$KIT_DIR/agent.conf}"
STATE_DIR="${SPIRE_KIT_STATE_DIR:-$KIT_DIR/state}"
RUN_DIR="${SPIRE_KIT_RUN_DIR:-$KIT_DIR/run}"
LOG_DIR="${SPIRE_KIT_LOG_DIR:-$KIT_DIR/log}"
BROKER_PORT="${SPIRE_KIT_BROKER_PORT:-8877}"
BROKER_HOST="${SPIRE_KIT_BROKER_HOST:-}"
MAX_RETRY="${SPIRE_KIT_MAX_RETRY:-15}"
RETRY_INTERVAL="${SPIRE_KIT_RETRY_INTERVAL:-2}"
CURL_TIMEOUT="${SPIRE_KIT_CURL_TIMEOUT:-5}"
# 额外 curl 选项(多网卡 guest 用 --interface 钉源地址;验收脚本也用它模拟 /30 源端)。
# 只影响本地怎么发请求,不影响 broker 侧的判定 —— 判定权始终在 host。
CURL_OPTS="${SPIRE_KIT_CURL_OPTS:-}"
DRY_RUN="${SPIRE_KIT_BOOTSTRAP_DRY_RUN:-0}"
# ── 插件口:客户不改 ClawPool、也不改本脚本就能换掉"怎么拿 token"这段逻辑 ──────
#   SPIRE_KIT_TOKEN_CMD  自定义取 token 的可执行文件。约定:stdout 输出与 broker 同形的 JSON
#                        {"join_token","trust_domain","server_address","server_port",...}。
#                        想改回 MMDS、换成自家 KMS/registrar、或直接读挂载盘,都只放一个文件。
#   SPIRE_KIT_PRE_HOOK   渲染 agent.conf 之前执行(可改模板、可写 trust bundle)
#   SPIRE_KIT_POST_HOOK  exec agent 之前执行(可做自检、可打点)
#   agent.conf.tmpl 本身也是可替换文件 —— 换模板即换 agent 行为,不碰代码。
TOKEN_CMD="${SPIRE_KIT_TOKEN_CMD:-}"
PRE_HOOK="${SPIRE_KIT_PRE_HOOK:-}"
POST_HOOK="${SPIRE_KIT_POST_HOOK:-}"
# 开关标记:文件不存在就整段不跑(unit 上也有 ConditionPathExists,这里是双保险)
ENABLE_MARKER="${SPIRE_KIT_ENABLE_MARKER:-${KIT_DIR}/enabled}"

log() { echo "[spire-kit:guest] $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"; }
die() { log "FATAL: $*"; exit 1; }

# broker 的响应体里有 join_token。curl 需要一个落点,所以优先放【内存文件系统】,
# 并且 0600 + trap 清理:独立 review 指出"token 从不落盘"的原说法不成立(旧版用
# mktemp 落到 /tmp 再删)。这里把它收紧成"只在 tmpfs 上短暂存在"。
_pick_tmpdir() {
  for d in "${SPIRE_KIT_RESP_DIR:-}" /run /dev/shm "${XDG_RUNTIME_DIR:-}" "${TMPDIR:-/tmp}"; do
    [ -n "$d" ] || continue
    [ -d "$d" ] && [ -w "$d" ] || continue
    printf '%s' "$d"; return 0
  done
  printf '%s' "/tmp"
}
RESP_DIR="$(_pick_tmpdir)"
BODY_FILE=""
cleanup_body() { [ -n "$BODY_FILE" ] && rm -f "$BODY_FILE"; }
trap cleanup_body EXIT INT TERM

if [ ! -e "$ENABLE_MARKER" ]; then
  log "开关关闭(缺 ${ENABLE_MARKER}),不取证不起 agent,退出 0 —— OpenClaw 不受影响"
  exit 0
fi

mkdir -p "$STATE_DIR" "$RUN_DIR" "$LOG_DIR" || die "cannot create kit dirs under $KIT_DIR"

# ── 1. 找 broker:默认网关就是本 VM /30 的 host 端 ────────────────────────────
if [ -z "$BROKER_HOST" ]; then
  BROKER_HOST="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
fi
[ -n "$BROKER_HOST" ] || die "no default gateway — cannot locate host broker"
log "broker endpoint http://${BROKER_HOST}:${BROKER_PORT}"

# ── 2. 取一次性 join token ──────────────────────────────────────────────────
# 插件口优先:给了 SPIRE_KIT_TOKEN_CMD 就整段交给客户的程序,broker 都不碰。
RESP=""
HTTP_CODE=""
if [ -n "$TOKEN_CMD" ]; then
  [ -x "$TOKEN_CMD" ] || die "SPIRE_KIT_TOKEN_CMD 不可执行: $TOKEN_CMD"
  log "用自定义取证插件: $TOKEN_CMD"
  if RESP="$("$TOKEN_CMD" 2>/dev/null)"; then
    HTTP_CODE=200
  else
    die "取证插件失败(rc=$?): $TOKEN_CMD"
  fi
fi
if [ -z "$RESP" ]; then
for i in $(seq 1 "$MAX_RETRY"); do
  BODY_FILE="$(umask 077; mktemp "${RESP_DIR}/spire-join.XXXXXX")" || die "cannot create response file under ${RESP_DIR}"
  # CURL_OPTS 需要按空格拆成多个 curl 参数
  # shellcheck disable=SC2086
  HTTP_CODE="$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time "$CURL_TIMEOUT" ${CURL_OPTS} \
    "http://${BROKER_HOST}:${BROKER_PORT}/v1/join-token" 2>/dev/null || echo 000)"
  RESP="$(cat "$BODY_FILE" 2>/dev/null)"
  rm -f "$BODY_FILE"; BODY_FILE=""
  case "$HTTP_CODE" in
    200) break ;;
    409) log "broker says token already issued for this boot (409) — agent 已在跑或本次开机已发过,退出让 systemd 决定"; exit 3 ;;
    403) log "attempt ${i}: broker DENY (403) reason=$(echo "$RESP" | sed -n 's/.*"reason": *"\([^"]*\)".*/\1/p')" ;;
    429) log "attempt ${i}: rate limited (429)" ;;
    503) log "attempt ${i}: registrar unavailable (503)" ;;
    000) log "attempt ${i}: broker unreachable (network/timeout)" ;;
    *)   log "attempt ${i}: unexpected HTTP ${HTTP_CODE}" ;;
  esac
  sleep "$RETRY_INTERVAL"
done
fi
[ "$HTTP_CODE" = "200" ] || die "no join token after ${MAX_RETRY} attempts (last HTTP ${HTTP_CODE})"

# jq 在 rootfs 里(build-rootfs.sh debootstrap --include 带 jq),但仍做缺失兜底。
_json() {
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$RESP" | jq -r ".$1 // empty"
  else
    printf '%s' "$RESP" | sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" | head -1
  fi
}

JOIN_TOKEN="$(_json join_token)"
TRUST_DOMAIN="$(_json trust_domain)"
SERVER_ADDRESS="$(_json server_address)"
SERVER_PORT="$(_json server_port)"
SPIFFE_ID="$(_json spiffe_id)"

[ -n "$JOIN_TOKEN" ] || die "broker 200 but no join_token in body"
[ -n "$TRUST_DOMAIN" ] || die "broker 200 but no trust_domain in body"
[ -n "$SERVER_ADDRESS" ] || die "broker 200 but no server_address in body"
log "got join token (len=${#JOIN_TOKEN}) spiffe_id=${SPIFFE_ID:-unset} server=${SERVER_ADDRESS}:${SERVER_PORT:-8081}"

# ── 3. 渲染 agent.conf(server 坐标由 broker 回传 → guest 里零硬编码)──────────
if [ -n "$PRE_HOOK" ] && [ -x "$PRE_HOOK" ]; then
  log "跑 pre hook: $PRE_HOOK"
  "$PRE_HOOK" || die "pre hook 失败: $PRE_HOOK"
fi
[ -f "$CONF_TMPL" ] || die "config template missing: $CONF_TMPL"
umask 077
sed -e "s|{{TRUST_DOMAIN}}|${TRUST_DOMAIN}|g" \
    -e "s|{{SERVER_ADDRESS}}|${SERVER_ADDRESS}|g" \
    -e "s|{{SERVER_PORT}}|${SERVER_PORT:-8081}|g" \
    -e "s|{{DATA_DIR}}|${STATE_DIR}|g" \
    -e "s|{{SOCKET_PATH}}|${RUN_DIR}/agent.sock|g" \
    -e "s|{{LOG_FILE}}|${LOG_DIR}/spire-agent.log|g" \
    "$CONF_TMPL" > "${CONF_OUT}.tmp" || die "render agent.conf failed"
mv "${CONF_OUT}.tmp" "$CONF_OUT"
grep -q '{{' "$CONF_OUT" && die "agent.conf still has unrendered placeholders: $(grep -o '{{[A-Z_]*}}' "$CONF_OUT" | sort -u | tr '\n' ' ')"

# ── 4. 每次开机 clean start:归档还原后的旧 state 一定无效(token 已消耗/SVID 过期)
rm -rf "${STATE_DIR:?}"/* 2>/dev/null || true
rm -f "${RUN_DIR}/agent.sock" 2>/dev/null || true

if [ "$DRY_RUN" = "1" ]; then
  log "DRY RUN(SPIRE_KIT_BOOTSTRAP_DRY_RUN=1):已取到 token 并渲染 ${CONF_OUT},不 exec agent"
  exit 0
fi

if [ -n "$POST_HOOK" ] && [ -x "$POST_HOOK" ]; then
  log "跑 post hook: $POST_HOOK"
  "$POST_HOOK" || die "post hook 失败: $POST_HOOK"
fi

[ -x "$BIN" ] || die "spire-agent binary missing/not executable: $BIN"

# ── 5. 拉起 agent(join_token attestation)────────────────────────────────────
log "exec spire-agent run"
exec "$BIN" run -config "$CONF_OUT" -joinToken "$JOIN_TOKEN"
