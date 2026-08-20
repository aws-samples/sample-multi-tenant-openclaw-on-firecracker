#!/bin/bash
# spire-kit-setup.sh —— host 首 boot / SSM patch 共用的 broker 安装入口(#516 二期)
#
# 调用方与失败语义
# ----------------
# 两个调用方,同一份脚本:
#   1. init-host.sh Step 4c(新 host 首 boot):从 assets 桶拉本目录 4 个文件后执行。
#      调用方是 fail-open 的 —— 本脚本 exit 非 0 只会落告警 marker + 日志令牌,
#      host 照常注册接租户(客户明确要求:装失败留告警接口、人工介入,不 ABANDON)。
#   2. 存量 host 的 SSM send-command patch(见 README.md):同样先拉文件再跑本脚本。
#
# 所以本脚本自身保持 fail-closed(缺参数 / 装失败就 die,exit 非 0),
# "要不要挡住 host 上线"由调用方决定 —— 这让同一份脚本既能给 fail-open 的
# init-host 用,也能给"人工修完要一个硬结论"的 SSM patch 用。
#
# 与一期 hooks/host-user-hook.sh 的关系
# -------------------------------------
# 本脚本是它的直接后继:SSM 读参、必填校验、loopback 拒绝、--force-env 语义全部
# 保留;删掉的是 kit tar.gz 下载 + sha256 校验那一段 —— 文件现在随平台部署走
# `clawpool-deploy.sh` 的 userdata sync 进 assets 桶,init-host.sh 逐文件拉取,
# 与 host-agent.py 等平台脚本同一通道,不再有"kit 换了但 sha256 没同步"的坑。
#
# 配置从哪来
# ----------
# SSM Parameter Store `/openclaw/spire-kit/*`。host instance role 已有
# `ssm:GetParameter` on `/openclaw/*`(compute.py,原为 CLOUDFRONT_ORIGIN 加的),
# 不需要改 IAM。
#
#   必填:
#     /openclaw/spire-kit/enabled              "true" 才装(缺/其他值 = 不装,exit 0)
#     /openclaw/spire-kit/trust-domain         客户 SPIRE 的 trust domain
#     /openclaw/spire-kit/spire-server-address 回传给 guest 写进 agent.conf(不能填 loopback)
#     /openclaw/spire-kit/registrar-url        backend=http 时必填
#     /openclaw/spire-kit/registrar-cmd        backend=exec 时必填
#   可选:
#     /openclaw/spire-kit/registrar-backend    http(默认)| exec | local
#     /openclaw/spire-kit/spire-server-port    默认 8081
#     /openclaw/spire-kit/broker-port          默认 8877
#     /openclaw/spire-kit/spire-server-socket  backend=local 时 spire-server 的 admin socket

set -euo pipefail

log() { echo "[spire-kit:setup] $*"; }
die() { echo "[spire-kit:setup] FATAL: $*" >&2; exit 1; }

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# OC_REGION:init-host.sh 会 export REGION;SSM patch 场景兜底走 IMDS。
REGION="${OC_REGION:-${REGION:-}}"
if [ -z "$REGION" ]; then
  _tok=$(curl -s -X PUT http://169.254.169.254/latest/api/token \
          -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)
  REGION=$(curl -s -H "X-aws-ec2-metadata-token: $_tok" \
          http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || true)
fi
[ -n "$REGION" ] || die "拿不到 region(OC_REGION/REGION env 与 IMDS 都没有)"

SSM_PREFIX="${SPIRE_KIT_SSM_PREFIX:-/openclaw/spire-kit}"

# ── 1. 读 SSM ─────────────────────────────────────────────────────────────────
# 刻意用 get-parameter(**单数**)逐个读:host role 只被授了 `ssm:GetParameter`,
# `GetParameters`(复数)/`GetParametersByPath` 是不同的 IAM action,会 AccessDenied。
# 参数不存在按"缺 = 空串"处理,由下面必填校验统一判定。
ssm() {
  local v
  v="$(aws ssm get-parameter --region "$REGION" \
        --name "${SSM_PREFIX}/$1" --with-decryption \
        --query 'Parameter.Value' --output text 2>/dev/null || true)"
  # `--output text` 对 null 打印字面量 None;当空串,否则 "None" 会被当合法值传下去。
  [ "$v" = "None" ] && v=""
  printf '%s' "$v"
}

ENABLED="$(ssm enabled)"
# tr 而不是 ${VAR,,}:后者是 bash 4+,macOS bash 3.2 跑测试会挂
if [ "$(printf '%s' "$ENABLED" | tr '[:upper:]' '[:lower:]')" != "true" ]; then
  log "总开关未开(${SSM_PREFIX}/enabled='${ENABLED:-<缺>}')—— 不装任何东西,exit 0"
  exit 0
fi

REGISTRAR_BACKEND="$(ssm registrar-backend)"; REGISTRAR_BACKEND="${REGISTRAR_BACKEND:-http}"
REGISTRAR_URL="$(ssm registrar-url)"
REGISTRAR_CMD="$(ssm registrar-cmd)"
TRUST_DOMAIN="$(ssm trust-domain)"
SERVER_ADDRESS="$(ssm spire-server-address)"
SERVER_PORT="$(ssm spire-server-port)"; SERVER_PORT="${SERVER_PORT:-8081}"
BROKER_PORT="$(ssm broker-port)"; BROKER_PORT="${BROKER_PORT:-8877}"
# backend=local 才有意义;默认值与 broker 一致,刻意不在 /tmp(unit 有 PrivateTmp=yes)。
SERVER_SOCKET="$(ssm spire-server-socket)"
SERVER_SOCKET="${SERVER_SOCKET:-/run/spire-server/private/api.sock}"

# ── 2. 必填校验(缺参数拒装,绝不落默认值)───────────────────────────────────────
# install.sh 的 --registrar-url / --trust-domain / --spire-server-address 都有内置
# 默认值(server-address 默认 127.0.0.1),不传照样装得起来、healthz 照样绿 ——
# 然后 guest 拿着 127.0.0.1 去连自己的 8081,attest 永远失败,且极难排查
# (broker 绿的、token 也发出去了)。所以配不全宁可拒装,让告警接口暴露它。
MISSING=()
[ -n "$TRUST_DOMAIN" ]   || MISSING+=("trust-domain")
[ -n "$SERVER_ADDRESS" ] || MISSING+=("spire-server-address")
case "$REGISTRAR_BACKEND" in
  http) [ -n "$REGISTRAR_URL" ] || MISSING+=("registrar-url(backend=http 必填)") ;;
  exec) [ -n "$REGISTRAR_CMD" ] || MISSING+=("registrar-cmd(backend=exec 必填)") ;;
  local) : ;;  # 自建 spire-server,不需要 registrar
  *) die "registrar-backend 只能是 http|exec|local,收到 '${REGISTRAR_BACKEND}'" ;;
esac
if [ ${#MISSING[@]} -gt 0 ]; then
  die "SSM 配置不全,缺:${MISSING[*]} —— 拒装(enabled=true 但配置残缺,调用方会落告警 marker)"
fi
# server-address 是最容易配错也最难排查的一个,单独兜一道
case "$SERVER_ADDRESS" in
  127.0.0.1|localhost|::1)
    die "spire-server-address='${SERVER_ADDRESS}' 指向本机 —— guest 会去连自己的 ${SERVER_PORT},attest 永远失败。请填真实 SPIRE Server 地址" ;;
esac

log "配置就绪:backend=${REGISTRAR_BACKEND} trust_domain=${TRUST_DOMAIN} server=${SERVER_ADDRESS}:${SERVER_PORT} port=${BROKER_PORT}"

# ── 3. 完整性检查:同目录必须有 install.sh 与 broker 源 ──────────────────────────
# 文件由调用方(init-host.sh / SSM patch)从 assets 桶拉到本目录;哪个没拉到就在
# 这里显式报出来,好过 install.sh 内部一个"缺文件"的笼统报错。
[ -f "${SELF_DIR}/install.sh" ]            || die "缺 ${SELF_DIR}/install.sh(assets 桶没同步全?)"
[ -f "${SELF_DIR}/spire-join-broker.py" ]  || die "缺 ${SELF_DIR}/spire-join-broker.py"
[ -f "${SELF_DIR}/spire-join-broker.service" ] || die "缺 ${SELF_DIR}/spire-join-broker.service"

# ── 4. 装 broker ─────────────────────────────────────────────────────────────
# install.sh 幂等:iptables 先删后插、systemctl restart 覆盖首装与重入。
# --force-env 必须:SSM 是本 host 配置的唯一来源,每次都按 SSM 当前值重写 broker.env。
# 不加会走"保留既有 broker.env"分支 —— 真机踩过:参数改了、脚本重跑了、broker 还用旧配置。
INSTALL_ARGS=(
  --force-env
  --port "$BROKER_PORT"
  --registrar-backend "$REGISTRAR_BACKEND"
  --trust-domain "$TRUST_DOMAIN"
  --spire-server-address "$SERVER_ADDRESS"
  --spire-server-port "$SERVER_PORT"
)
[ -n "$REGISTRAR_URL" ] && INSTALL_ARGS+=(--registrar-url "$REGISTRAR_URL")
[ -n "$REGISTRAR_CMD" ] && INSTALL_ARGS+=(--registrar-cmd "$REGISTRAR_CMD")
# 只在 local 后端传:另两个后端不看这个值,传了会让 broker.env 多一行易误读的配置。
[ "$REGISTRAR_BACKEND" = "local" ] && INSTALL_ARGS+=(--spire-server-socket "$SERVER_SOCKET")

log "跑 install.sh ${INSTALL_ARGS[*]}"
bash "${SELF_DIR}/install.sh" "${INSTALL_ARGS[@]}" || die "install.sh 失败(见上方输出)"
# install.sh 自检已断言 healthz ok=true(含 registrar 可达性),走到这里 = 此刻真的可用。

# ── 5. 自检:reboot 后还在 ───────────────────────────────────────────────────────
systemctl is-enabled spire-join-broker.service >/dev/null 2>&1 \
  || die "broker unit 没 enabled —— 重启后不会自启"
log "PASS:broker 已装 + enabled,配置来自 ${SSM_PREFIX}/*"
