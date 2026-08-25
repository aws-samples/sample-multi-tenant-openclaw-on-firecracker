#!/usr/bin/env bash
# deploy/edge/install-edge.sh — bring an EC2 host up as an OpenClaw Pool edge.
#
# Runs at instance start (userdata) on Ubuntu 22.04+ / Amazon Linux 2023 hosts,
# on both arm64 (c7g/c6g) and x86_64 (c6in) shapes. Idempotent — re-running
# after ASG replacement or manual re-invocation is a no-op.
#
# Contract:
#   - Environment vars set by CloudFormation/CDK before this script runs:
#       ENGINE_REDIS_ENDPOINT   e.g. "clu.abc.cache.amazonaws.com:6379"
#     Optional overrides:
#       EDGE_LISTEN_PORT        (default 8080; ALB target group port)
#       OPENRESTY_VERSION       (default apt/dnf repo latest)
#   - IMDS reachable so we can read the host's own local-ipv4 for
#     $edge_self_ip (drives local vs remote branch in balancer_pick).
#
# Everything below the "install" section is kernel/network tuning called
# out in 03-TEST-PLAN §8 "压测前环境核对". If any check fails we exit
# non-zero so cloud-init records the failure; /healthz stays unhealthy, the
# ELB health check rejects the target, and the ASG replaces the instance.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() { printf '[install-edge] %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# rpm 锁竞争的总预算 = 90s 墙钟,给 edge.health_check_grace_period_seconds=300 留足
# 正常自举时间(拉包、编译无关的解压、nginx 起来、warmup 探测)。
#
# 关键:预算是【跨所有调用点共享的墙钟 deadline】,不是每次调用各算一遍的 sleep 累加。
# 早先按"每个调用点 18×5s sleep"计,漏了两件事 —— ① dnf 本身跑多久不计入
# ② rpm --import / 两次 dnf install 三个调用点各自重置预算。最坏情况下总耗时能远超
# 300s grace,于是"修好了锁竞争"反而变成"超时被 ASG 换机",故障形态换了但没修好。
# 现在 deadline 在脚本启动时一次性定下,任何调用点耗尽它就 die。
RPM_LOCK_RETRY_BUDGET_SECONDS=90
RPM_LOCK_RETRY_INTERVAL_SECONDS=5
RPM_LOCK_DEADLINE_EPOCH=$(( $(date +%s) + RPM_LOCK_RETRY_BUDGET_SECONDS ))
OPENRESTY_PUBKEY_SHA256=fc40f82ba62260bd4a7837fb9c38d7997d57f17797d4cb5d9e40f28226aa7b14

run_with_rpm_lock_retry() {
    local attempt=0 output rc now remaining
    while :; do
        attempt=$((attempt + 1))
        if output="$("$@" 2>&1)"; then
            [[ -z "$output" ]] || printf '%s\n' "$output" >&2
            return 0
        else
            rc=$?
        fi
        [[ -z "$output" ]] || printf '%s\n' "$output" >&2

        # "Resource temporarily unavailable" 单独出现不是重试依据;只有同时出现
        # rpm 锁签名,或 dnf 自己的 yum-lock 等待签名,才判定为锁竞争。
        if ! grep -Eqi \
            "can't create transaction lock|/var/lib/rpm/\\.rpm\\.lock|Waiting for process with pid|another app is currently holding the yum lock" \
            <<<"$output"; then
            die "command failed without rpm lock contention (rc=$rc): $*"
        fi

        # 墙钟判定:命令自身耗时也吃预算,所以这里用 date 而不是数 sleep 次数。
        now="$(date +%s)"
        remaining=$(( RPM_LOCK_DEADLINE_EPOCH - now ))
        if (( remaining <= RPM_LOCK_RETRY_INTERVAL_SECONDS )); then
            die "rpm lock contention exhausted the shared ${RPM_LOCK_RETRY_BUDGET_SECONDS}s wall-clock budget at /var/lib/rpm/.rpm.lock (attempt $attempt, ${remaining}s left): $*"
        fi
        log "rpm lock busy; retrying in ${RPM_LOCK_RETRY_INTERVAL_SECONDS}s (attempt $attempt, ${remaining}s of shared budget left)"
        sleep "$RPM_LOCK_RETRY_INTERVAL_SECONDS"
    done
}

# ── 0. Preflight: env + tools ────────────────────────────────────────────
[[ -n "${ENGINE_REDIS_ENDPOINT:-}" ]] || die "ENGINE_REDIS_ENDPOINT must be set"
LISTEN_PORT="${EDGE_LISTEN_PORT:-8080}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# Split "host:port" for the nginx template.
REDIS_HOST="${ENGINE_REDIS_ENDPOINT%:*}"
REDIS_PORT="${ENGINE_REDIS_ENDPOINT##*:}"
[[ "$REDIS_HOST" != "$REDIS_PORT" ]] || die "ENGINE_REDIS_ENDPOINT missing :port"

READER_HOST="$REDIS_HOST"
READER_PORT="$REDIS_PORT"
# AWS_REGION 在本脚本里是可选项(见下方 fluent-bit 段的 ${AWS_REGION:-...}),
# 而脚本开头是 set -euo pipefail —— 裸写 "$AWS_REGION" 会在未设时直接 unbound
# variable 退出,把 edge 拉不起来,正好是本段刻意要避免的失败。留空则 aws CLI
# 自己报错、走下面的 WARN 回落。
if ! READER_ENDPOINT="$(aws ssm get-parameter \
    --name /openclaw/engine/redis/reader-endpoint \
    --query 'Parameter.Value' \
    --output text \
    --region "${AWS_REGION:-}" 2>/dev/null)"; then
    log "WARN: failed to read Redis reader endpoint from SSM; falling back to primary"
elif [[ -z "$READER_ENDPOINT" || "$READER_ENDPOINT" == "None" ]]; then
    log "WARN: Redis reader endpoint from SSM is empty; falling back to primary"
else
    _reader_host="${READER_ENDPOINT%:*}"
    _reader_port="${READER_ENDPOINT##*:}"
    if [[ -z "$_reader_host" || -z "$_reader_port" || "$_reader_host" == "$_reader_port" ]]; then
        log "WARN: Redis reader endpoint missing host:port; falling back to primary"
    else
        READER_HOST="$_reader_host"
        READER_PORT="$_reader_port"
    fi
fi

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) ARCH_DEB=arm64 ;;
    x86_64|amd64)  ARCH_DEB=amd64 ;;
    *) die "unsupported arch: $ARCH" ;;
esac
log "arch=$ARCH deb-arch=$ARCH_DEB listen=$LISTEN_PORT redis=$REDIS_HOST:$REDIS_PORT reader=$READER_HOST:$READER_PORT"

# ── 1. Read this host's private IPv4 from IMDS ───────────────────────────
imds_token() {
    curl -fsS -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 300" 2>/dev/null
}
imds_get() {
    local tok="$1" path="$2"
    curl -fsS -H "X-aws-ec2-metadata-token: $tok" \
        "http://169.254.169.254/latest/meta-data/$path" 2>/dev/null
}
TOKEN="$(imds_token || true)"
[[ -n "$TOKEN" ]] || die "IMDSv2 token fetch failed"
SELF_IP="$(imds_get "$TOKEN" local-ipv4)"
[[ -n "$SELF_IP" ]] || die "IMDS local-ipv4 empty"
log "self_ip=$SELF_IP"

# ── 2. Install OpenResty (Ubuntu apt / AL2023 dnf) ───────────────────────
install_openresty_ubuntu() {
    apt-get update -qq
    apt-get install -y -qq curl gnupg ca-certificates lsb-release
    # OpenResty apt repo. See: https://openresty.org/en/linux-packages.html
    curl -fsSL https://openresty.org/package/pubkey.gpg \
        | gpg --dearmor -o /usr/share/keyrings/openresty.gpg
    codename="$(lsb_release -sc)"
    echo "deb [arch=$ARCH_DEB signed-by=/usr/share/keyrings/openresty.gpg] http://openresty.org/package/ubuntu $codename main" \
        > /etc/apt/sources.list.d/openresty.list
    apt-get update -qq
    apt-get install -y -qq openresty
}
install_openresty_al2023() {
    # gettext 提供 envsubst(下方渲染 nginx.conf 模板用);AL2023 最小镜像不带。
    run_with_rpm_lock_retry dnf install -y -q ca-certificates gettext
    local pubkey="$SRC_DIR/openresty-pubkey.gpg"
    local pubkey_sha256
    [[ -f "$pubkey" ]] || die "bundled OpenResty public key missing: $pubkey"
    pubkey_sha256="$(sha256sum "$pubkey" 2>&1)" || \
        die "failed to compute OpenResty public key SHA-256: $pubkey_sha256"
    pubkey_sha256="${pubkey_sha256%% *}"
    [[ "$pubkey_sha256" == "$OPENRESTY_PUBKEY_SHA256" ]] || \
        die "OpenResty public key SHA-256 mismatch: expected=$OPENRESTY_PUBKEY_SHA256 actual=$pubkey_sha256"
    run_with_rpm_lock_retry rpm --import "$pubkey"
    # OpenResty dnf/yum repo.
    cat > /etc/yum.repos.d/openresty.repo <<'REPO'
[openresty]
name=Official OpenResty Repository
baseurl=https://openresty.org/package/amazon/2023/$basearch
gpgcheck=1
enabled=1
REPO
    run_with_rpm_lock_retry dnf install -y -q openresty
}

if [[ -x /usr/local/openresty/nginx/sbin/nginx ]]; then
    log "openresty already installed; skipping install"
elif [[ -f /etc/os-release ]] && grep -qi ubuntu /etc/os-release; then
    install_openresty_ubuntu
elif [[ -f /etc/os-release ]] && grep -qi 'amazon linux' /etc/os-release; then
    install_openresty_al2023
else
    die "unsupported distro (need Ubuntu or Amazon Linux 2023)"
fi

# ── 3. Deploy route.lua + lib/ under lualib/edge ─────────────────────────
LUALIB=/usr/local/openresty/lualib/edge
mkdir -p "$LUALIB/lib"
install -m 0644 "$SRC_DIR/route.lua" "$LUALIB/route.lua"
install -m 0644 "$SRC_DIR"/lib/*.lua "$LUALIB/lib/"

# ── 4. Render nginx.conf template into /usr/local/openresty/nginx/conf ───
CONF_DIR=/usr/local/openresty/nginx/conf
mkdir -p "$CONF_DIR"
# envsubst 只替换模板里显式列出的五个占位符,其余 $ 变量保持原样。
export ENGINE_REDIS_HOST="$REDIS_HOST"
export ENGINE_REDIS_PORT="$REDIS_PORT"
export ENGINE_REDIS_READER_HOST="$READER_HOST"
export ENGINE_REDIS_READER_PORT="$READER_PORT"
export EDGE_SELF_IP="$SELF_IP"
# shellcheck disable=SC2016  # envsubst takes the variable list literally
envsubst '$ENGINE_REDIS_HOST $ENGINE_REDIS_PORT $ENGINE_REDIS_READER_HOST $ENGINE_REDIS_READER_PORT $EDGE_SELF_IP' \
    < "$SRC_DIR/nginx.conf" > "$CONF_DIR/nginx.conf"

# ── 5. Kernel / socket tuning (03-TEST-PLAN §8) ──────────────────────────
cat > /etc/sysctl.d/99-openclaw-edge.conf <<'SYSCTL'
# --- OpenClaw Pool edge tuning ---------------------------------
# Ephemeral port pool: needs to be wide open for upstream fan-out
# at 30w concurrent WS × keepalive churn.
net.ipv4.ip_local_port_range = 10000 65535
# Accept queue: nginx listen backlog is 65535; kernel must match.
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
# TIME_WAIT reuse under CPS bursts.
net.ipv4.tcp_tw_reuse = 1
# Faster reap of dead keepalive sockets.
net.ipv4.tcp_keepalive_time = 60
net.ipv4.tcp_keepalive_intvl = 15
net.ipv4.tcp_keepalive_probes = 4
# conntrack: edge doesn't NAT, but any stateful iptables rules use it.
net.netfilter.nf_conntrack_max = 1048576
# File descriptor headroom for 1M FDs / worker.
fs.file-max = 2097152
SYSCTL
sysctl -p /etc/sysctl.d/99-openclaw-edge.conf >/dev/null || \
    log "WARN: sysctl reload emitted warnings (nf_conntrack may need module load)"

# ── 6. systemd unit ──────────────────────────────────────────────────────
# The Environment= lines feed route.lua's warmup_probe(), which needs to
# know the endpoint at init_worker phase (before any request runs, so
# ngx.var.edge_redis_reader_host isn't available yet). Kept in sync with the
# same values baked into nginx.conf's server-block $edge_redis_* vars.
cat > /etc/systemd/system/claw-edge.service <<UNIT
[Unit]
Description=OpenClaw Pool edge (OpenResty)
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
PIDFile=/usr/local/openresty/nginx/logs/nginx.pid
Environment=ENGINE_REDIS_HOST_HINT=${REDIS_HOST}
Environment=ENGINE_REDIS_PORT_HINT=${REDIS_PORT}
Environment=ENGINE_REDIS_READER_HOST_HINT=${READER_HOST}
Environment=ENGINE_REDIS_READER_PORT_HINT=${READER_PORT}
ExecStartPre=/usr/local/openresty/nginx/sbin/nginx -t -c $CONF_DIR/nginx.conf
ExecStart=/usr/local/openresty/nginx/sbin/nginx -c $CONF_DIR/nginx.conf
ExecReload=/usr/local/openresty/nginx/sbin/nginx -s reload
KillSignal=SIGQUIT
LimitNOFILE=1048576
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
# Test config first — never activate a broken config.
/usr/local/openresty/nginx/sbin/nginx -t -c "$CONF_DIR/nginx.conf"
systemctl enable claw-edge.service
systemctl restart claw-edge.service

# ── 7. journald tuning (F21: default RateLimit too low for edge traffic) ─
# Drop-in raises burst ceiling so high-CPS bursts don't get silently dropped.
# Storage=persistent ensures logs survive reboots for Fluent Bit DB cursor.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/50-claw-edge.conf <<'JOURNALD'
[Journal]
Storage=persistent
RateLimitIntervalSec=30s
RateLimitBurst=50000
SystemMaxUse=2G
SystemKeepFree=1G
MaxFileSec=1day
JOURNALD
systemctl restart systemd-journald || log "WARN: journald restart failed"
log "journald drop-in applied (RateLimitBurst=50000, SystemMaxUse=2G)"

# ── 8. Fluent Bit (log shipper: journald → Firehose) ────────────────────
# Shared installer (deploy/edge/fluent-bit/install-fluent-bit.sh) does the
# install + S3 config pull + placeholder render + start for both edge and
# host roles. Edge maps its region/stream into the FB_* vars the config
# expects. Config-gated inside the shared script (LOGGING_ENABLED=false → no-op).
# Resolve defaults into locals first — same-line prefix assignments can't see
# each other's expansions (shellcheck SC2097/SC2098).
FB_AWS_REGION="${AWS_REGION:-ap-southeast-1}"
FB_LOGGING_ENABLED="${LOGGING_ENABLED:-true}"
FB_ASSETS_BUCKET="${ASSETS_BUCKET:-}"
FB_REGION="$FB_AWS_REGION" \
FB_STREAM="${FIREHOSE_DELIVERY_STREAM:-}" \
FB_ROLE=edge \
LOGGING_ENABLED="$FB_LOGGING_ENABLED" \
ASSETS_BUCKET="$FB_ASSETS_BUCKET" \
AWS_REGION="$FB_AWS_REGION" \
FB_LOCAL_DIR="$SRC_DIR/fluent-bit/edge" \
    bash "$SRC_DIR/fluent-bit/install-fluent-bit.sh"

# ── 9. Warmup wait on /healthz (INTERFACE-CONTRACT §6) ───────────────────
# nginx accepts on :8080 in ~200ms but route.lua's warmup gate returns 503
# until the async Redis PING succeeds (up to 30s + a bit of slack). We
# poll /healthz here so the userdata script only returns success once the
# instance is truly ready to serve; the ELB /healthz check is the gate, and
# an unhealthy target is replaced by the ASG. Cap at 90s to still fail-fast
# on genuinely broken Redis.
if command -v curl >/dev/null 2>&1; then
    for i in $(seq 1 45); do
        code="$(curl -o /dev/null -s -w '%{http_code}' \
            "http://127.0.0.1:${LISTEN_PORT}/healthz" || echo 000)"
        if [[ "$code" == "200" ]]; then
            log "healthz ready after ${i}×2s (redis reachable)"
            break
        fi
        [[ $i -eq 45 ]] && die "healthz never returned 200 (Redis unreachable?)"
        sleep 2
    done
fi

log "DONE: claw-edge active on :$LISTEN_PORT; self_ip=$SELF_IP; redis=$REDIS_HOST:$REDIS_PORT; reader=$READER_HOST:$READER_PORT"
