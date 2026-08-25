#!/bin/bash
#
# 为什么要这个文件
# ----------------
# install.sh 需要人 sudo 手动跑一次。ASG 新起的 host 上没人跑,broker 就不存在 ——
# 那台 host 会正常接租户,而它上面的 VM 全都静默没有身份。这个 hook 把"装 broker"
# 接到平台既有的 first-boot 钩子上,做到**零改 ClawPool**:
#
#   config.yml:
#     user_hooks:
#       host:
#         s3_uri: s3://<客户自管桶>/spire-kit/host-user-hook.sh
#         sha256: <该对象的 64 位 sha256>
#         timeout_seconds: 300
#         failure_policy: fail        # 配错/装不上 → host 不注册 active → ABANDON
#
# 平台侧只填这段 config,init-host.sh:678 的占位会自动下载 + 校验 sha256 + 以 root
# 执行本脚本(渲染逻辑见 deploy/stacks/ha_edge.py:_render_user_hook)。
#
# 关于 failure_policy
# -------------------
# 建议 `fail`:SPIRE 配错的 host 直接 ABANDON、不注册 active,不会带着错配置接租户。
# 这比"装了个连不上 registrar 的 broker 然后 host 照常上线"安全得多 —— 后者的失败
# 是静默的(VM 起得来、业务正常、只是没身份),而没身份的 VM 会一直跑到下游拒绝它
# 的请求才被发现。
# 想要"SPIRE 挂了也不许挡住 host 上线"就改成 `warn`,但要自己盯 §巡检。
#
# 关于 timeout
# ------------
# _render_user_hook 用 `timeout --signal=TERM --kill-after=10s Ns bash <hook>`,
# 只对 hook 的直接子进程发信号。`systemctl enable --now` 拉起的 broker 是 systemd
# (PID 1)的子进程、独立 cgroup,**不在 timeout 的进程组里,不会被连坐 kill**
# (与官方 node_exporter 示例同构)。所以本脚本装完 unit 就退出是安全的。
#
# 配置从哪来
# ----------
# SSM Parameter Store,`/openclaw/spire-kit/*`。host instance role 本来就有
# `ssm:GetParameter` on `/openclaw/*`(compute.py:73-80,原本给 CLOUDFRONT_ORIGIN
# 用),所以**不需要改任何 IAM**。
#
# 选 SSM 而不是把值写死在本脚本里的理由:SSM 参数天生按 region/账号隔离,同一份
# hook 脚本(同一个 sha256)能走遍 dev/staging/prod;改地址只改参数值 + 重启 broker,
# 不用重算 sha256、不用换 Launch Template。
#
#   必填:
#     /openclaw/spire-kit/enabled              "true" 才装(缺 = 不装,退出 0)
#     /openclaw/spire-kit/trust-domain         客户 SPIRE 的 trust domain
#     /openclaw/spire-kit/spire-server-address 回传给 guest 写进 agent.conf(不能填 loopback)
#     /openclaw/spire-kit/kit-s3-uri           kit tar.gz 的 s3:// 位置
#     /openclaw/spire-kit/kit-sha256           上述对象的 sha256(防对象被换)
#     /openclaw/spire-kit/registrar-url        backend=http 时必填
#     /openclaw/spire-kit/registrar-cmd        backend=exec 时必填(客户自己的发证程序)
#   可选:
#     /openclaw/spire-kit/registrar-backend    http(默认)| exec | local
#     /openclaw/spire-kit/spire-server-port    默认 8081
#     /openclaw/spire-kit/broker-port          默认 8877
#     /openclaw/spire-kit/spire-server-socket  backend=local 时 spire-server 的 admin socket
#                                              (默认 /run/spire-server/private/api.sock;
#                                               不要放 /tmp,broker unit 有 PrivateTmp=yes)
#
# 开关:`/openclaw/spire-kit/enabled` != "true" → 什么都不装、退出 0。这是最外层的
# 总开关,比"装了再关"更彻底 —— 关掉时 host 上不存在任何 spire 相关文件与进程。

set -euo pipefail

log() { echo "[spire-kit:hook] $*"; }
die() { echo "[spire-kit:hook] FATAL: $*" >&2; exit 1; }

# OC_REGION 由 _render_user_hook 注入;/etc/platform.env 是兜底(它也带 OC_REGION)。
if [ -z "${OC_REGION:-}" ] && [ -r /etc/platform.env ]; then
  # shellcheck disable=SC1091
  . /etc/platform.env
fi
[ -n "${OC_REGION:-}" ] || die "拿不到 OC_REGION(hook 注入与 /etc/platform.env 都没有)"

SSM_PREFIX="${SPIRE_KIT_SSM_PREFIX:-/openclaw/spire-kit}"
WORK="$(mktemp -d /tmp/spire-kit-hook.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# ── 1. 读 SSM ─────────────────────────────────────────────────────────────────
# 刻意用 get-parameter(**单数**)逐个读,不用 get-parameters / get-parameters-by-path。
# 原因是 IAM:host role 只被授了 `ssm:GetParameter`(compute.py 里给 CLOUDFRONT_ORIGIN
# 用的那条)。`GetParameters`(复数)和 `GetParametersByPath` 是**不同的 IAM action**,
# 用它们会 AccessDenied —— 那就得改 IAM,"零改 ClawPool"也就不成立了。
# 逐个读多几次 API 调用,但只在 host 首 boot 发生一次,代价可以忽略。
#
# 参数不存在时 CLI 返回非 0(ParameterNotFound)。这里按"缺 = 空串"处理,缺哪个
# 由下面的必填校验统一判定 —— 好过在这里为每个参数分别写一份报错。
ssm() {
  local v
  v="$(aws ssm get-parameter --region "$OC_REGION" \
        --name "${SSM_PREFIX}/$1" --with-decryption \
        --query 'Parameter.Value' --output text 2>/dev/null || true)"
  # `--output text` 对 null 值会打印字面量 None;当空串处理,否则 "None" 会被当成
  # 一个合法的 trust domain / URL 传下去。
  [ "$v" = "None" ] && v=""
  printf '%s' "$v"
}

ENABLED="$(ssm enabled)"
# tr 而不是 ${VAR,,}:后者是 bash 4+,而 acceptance 套件在 macOS 的 bash 3.2 上跑
if [ "$(printf '%s' "$ENABLED" | tr '[:upper:]' '[:lower:]')" != "true" ]; then
  log "总开关未开(${SSM_PREFIX}/enabled='${ENABLED:-<缺>}')—— 不装任何东西,退出 0"
  exit 0
fi

REGISTRAR_BACKEND="$(ssm registrar-backend)"; REGISTRAR_BACKEND="${REGISTRAR_BACKEND:-http}"
REGISTRAR_URL="$(ssm registrar-url)"
REGISTRAR_CMD="$(ssm registrar-cmd)"
TRUST_DOMAIN="$(ssm trust-domain)"
SERVER_ADDRESS="$(ssm spire-server-address)"
SERVER_PORT="$(ssm spire-server-port)"; SERVER_PORT="${SERVER_PORT:-8081}"
KIT_S3_URI="$(ssm kit-s3-uri)"
KIT_SHA256="$(ssm kit-sha256)"
BROKER_PORT="$(ssm broker-port)"; BROKER_PORT="${BROKER_PORT:-8877}"
# backend=local 才有意义:broker 要 exec `spire-server token generate -socketPath <这个>`。
# 默认值与 broker 一致,且刻意不在 /tmp(unit 有 PrivateTmp=yes,详见 broker 里的注释)。
SERVER_SOCKET="$(ssm spire-server-socket)"
SERVER_SOCKET="${SERVER_SOCKET:-/run/spire-server/private/api.sock}"

# ── 2. 必填校验(fail-closed,绝不落默认值)─────────────────────────────────────
# 这一段是整个 hook 最重要的部分。install.sh 的 --registrar-url / --trust-domain /
# --spire-server-address 都有内置默认值(其中 server-address 默认 127.0.0.1),
# 不传照样装得起来、healthz 照样绿 —— 然后 guest 拿着 server_address=127.0.0.1 去
# 连自己的 8081,attest 永远失败。那种失败极难排查(broker 绿的、token 发出去了),
# 所以这里宁可拒装:配不全就让 host ABANDON,别带着错配置上线接租户。
MISSING=()
[ -n "$TRUST_DOMAIN" ]   || MISSING+=("trust-domain")
[ -n "$SERVER_ADDRESS" ] || MISSING+=("spire-server-address")
[ -n "$KIT_S3_URI" ]     || MISSING+=("kit-s3-uri")
[ -n "$KIT_SHA256" ]     || MISSING+=("kit-sha256")
case "$REGISTRAR_BACKEND" in
  http) [ -n "$REGISTRAR_URL" ] || MISSING+=("registrar-url(backend=http 必填)") ;;
  exec) [ -n "$REGISTRAR_CMD" ] || MISSING+=("registrar-cmd(backend=exec 必填)") ;;
  local) : ;;  # 自建 spire-server,不需要 registrar
  *) die "registrar-backend 只能是 http|exec|local,收到 '${REGISTRAR_BACKEND}'" ;;
esac
if [ ${#MISSING[@]} -gt 0 ]; then
  die "SSM 配置不全,缺:${MISSING[*]} —— 拒装(failure_policy: fail 会让本 host ABANDON,这是有意的:错配置的 host 不该接租户)"
fi
# server-address 是最容易配错也最难排查的一个,单独兜一道:默认值意味着没真配
case "$SERVER_ADDRESS" in
  127.0.0.1|localhost|::1)
    die "spire-server-address='${SERVER_ADDRESS}' 指向本机 —— guest 会去连自己的 ${SERVER_PORT},attest 永远失败。请填真实 SPIRE Server 地址" ;;
esac

log "配置就绪:backend=${REGISTRAR_BACKEND} trust_domain=${TRUST_DOMAIN} server=${SERVER_ADDRESS}:${SERVER_PORT} port=${BROKER_PORT}"

# ── 3. 取 kit 并校验 sha256 ───────────────────────────────────────────────────
# 与平台对 hook 自身的处理同款:私有 S3 + 全长 sha256 校验。差别只在这里校验的是
# kit 载荷,而 hook 脚本自身的 sha256 由 _render_user_hook 校验。
case "$KIT_S3_URI" in
  s3://*) : ;;
  *) die "kit-s3-uri 必须是 s3:// 开头,收到 '${KIT_S3_URI}'" ;;
esac
[[ "$KIT_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "kit-sha256 必须是 64 位小写十六进制,收到 '${KIT_SHA256}'"

TARBALL="${WORK}/spire-kit.tar.gz"
aws s3 cp "$KIT_S3_URI" "$TARBALL" --region "$OC_REGION" --no-progress \
  || die "下载 kit 失败:${KIT_S3_URI}"
[ -s "$TARBALL" ] || die "kit 下载到 0 字节:${KIT_S3_URI}"
printf '%s  %s\n' "$KIT_SHA256" "$TARBALL" | sha256sum -c - >/dev/null \
  || die "kit sha256 不匹配 —— 对象被换过或参数没同步更新,拒装"
log "kit 校验通过($(stat -c%s "$TARBALL") bytes)"

EXTRACT="${WORK}/extract"
mkdir -p "$EXTRACT"
tar -xzf "$TARBALL" -C "$EXTRACT" || die "解包失败(不是 gzip tar?)"

# 打包方式两种都接受(带顶层目录 / 直接平铺),所以不猜结构,直接找 install.sh。
# 刻意【不】用 `tar --strip-components=1` 做兼容:对平铺的包它不会报错,而是把顶层文件
# 名剥成空、静默一个都不解出来 —— 那就变成"解包成功但目录是空的"这种难查的假成功。
KIT_DIR="$(dirname "$(find "$EXTRACT" -name install.sh -maxdepth 3 -print -quit)")"
[ -n "$KIT_DIR" ] && [ -f "${KIT_DIR}/install.sh" ] \
  || die "包里找不到 install.sh(打包结构不对?顶层应是 spire-kit 目录或直接平铺)"
[ -f "${KIT_DIR}/spire-join-broker.py" ] \
  || die "包里有 install.sh 但缺 spire-join-broker.py —— 包不完整,拒装"

# ── 4. 装 broker ─────────────────────────────────────────────────────────────
# install.sh 自身幂等:iptables 规则先删后插,systemctl enable --now 可重复执行。
#
# --force-env 是必须的:SSM 是本 host 配置的**唯一来源**,所以每次跑都要按 SSM 的当前值
# 重写 broker.env。不加的话 install.sh 默认走"保留既有 broker.env、不覆盖"分支 ——
# 真机上踩过:hook 第一次因别的原因失败但已写下 broker.env,之后每次重跑(实例刷新、
# 参数改完重跑)SSM 里的新值都被静默丢弃,现象是"参数改了、hook 跑了、broker 还用旧配置"。
# 旧文件会被备份成 broker.env.bak,运维手改过的值不会无痕消失。
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
# 只在 local 后端传:另两个后端不看这个值,传了会让 broker.env 里多一行无意义配置,
# 排查时容易被误读成"这套在用本机 spire-server"。
[ "$REGISTRAR_BACKEND" = "local" ] && INSTALL_ARGS+=(--spire-server-socket "$SERVER_SOCKET")

log "跑 install.sh ${INSTALL_ARGS[*]}"
bash "${KIT_DIR}/install.sh" "${INSTALL_ARGS[@]}" || die "install.sh 失败(见上方输出)"
# install.sh 的自检已断言 healthz 的 ok=true,而 ok 现在把 registrar 可达性也算进去,
# 所以走到这里意味着"进程活着 + 发证依赖此刻可达",不再只是进程活检。

# ── 5. 自检 ──────────────────────────────────────────────────────────────────
# install.sh 内部已经 curl 过 /healthz。这里再查一遍 systemd 的 enabled 状态 ——
# 那是"下次 reboot 还在不在"的唯一凭据,healthz 只证明"此刻活着"。
systemctl is-enabled spire-join-broker.service >/dev/null 2>&1 \
  || die "broker unit 没 enabled —— 重启后不会自启"
log "PASS:broker 已装 + enabled,配置来自 ${SSM_PREFIX}/*"
