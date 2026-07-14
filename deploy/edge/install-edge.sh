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
# out in the test plan §8 "压测前环境核对". If any check fails we exit
# non-zero so ASG lifecycle hooks catch it — no silent success.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() { printf '[install-edge] %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# ── 0. Preflight: env + tools ────────────────────────────────────────────
[[ -n "${ENGINE_REDIS_ENDPOINT:-}" ]] || die "ENGINE_REDIS_ENDPOINT must be set"
LISTEN_PORT="${EDGE_LISTEN_PORT:-8080}"

# Split "host:port" for the nginx template.
REDIS_HOST="${ENGINE_REDIS_ENDPOINT%:*}"
REDIS_PORT="${ENGINE_REDIS_ENDPOINT##*:}"
[[ "$REDIS_HOST" != "$REDIS_PORT" ]] || die "ENGINE_REDIS_ENDPOINT missing :port"

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) ARCH_DEB=arm64 ;;
    x86_64|amd64)  ARCH_DEB=amd64 ;;
    *) die "unsupported arch: $ARCH" ;;
esac
log "arch=$ARCH deb-arch=$ARCH_DEB listen=$LISTEN_PORT redis=$REDIS_HOST:$REDIS_PORT"

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
    dnf install -y -q ca-certificates gettext
    # OpenResty dnf/yum repo.
    cat > /etc/yum.repos.d/openresty.repo <<'REPO'
[openresty]
name=Official OpenResty Repository
baseurl=https://openresty.org/package/amazon/2023/$basearch
gpgcheck=1
enabled=1
gpgkey=https://openresty.org/package/pubkey.gpg
REPO
    dnf install -y -q openresty
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
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$LUALIB/lib"
install -m 0644 "$SRC_DIR/route.lua" "$LUALIB/route.lua"
install -m 0644 "$SRC_DIR"/lib/*.lua "$LUALIB/lib/"

# ── 4. Render nginx.conf template into /usr/local/openresty/nginx/conf ───
CONF_DIR=/usr/local/openresty/nginx/conf
mkdir -p "$CONF_DIR"
# envsubst replaces ONLY the three placeholders we templated — nothing else.
export ENGINE_REDIS_HOST="$REDIS_HOST"
export ENGINE_REDIS_PORT="$REDIS_PORT"
export EDGE_SELF_IP="$SELF_IP"
# shellcheck disable=SC2016  # envsubst takes the variable list literally
envsubst '$ENGINE_REDIS_HOST $ENGINE_REDIS_PORT $EDGE_SELF_IP' \
    < "$SRC_DIR/nginx.conf" > "$CONF_DIR/nginx.conf"

# ── 5. Kernel / socket tuning (the test plan §8) ──────────────────────────
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
# ngx.var.edge_redis_host isn't available yet). Kept in sync with the
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
# Config-gated: skip when LOGGING_ENABLED=false (CDK passes this from config).
LOGGING_ENABLED="${LOGGING_ENABLED:-true}"
if [[ "$LOGGING_ENABLED" == "true" ]]; then
    # 8a. Install Fluent Bit from official repo (pin major version 3.x).
    if ! command -v fluent-bit >/dev/null 2>&1; then
        if grep -qi ubuntu /etc/os-release 2>/dev/null; then
            curl -fsSL https://packages.fluentbit.io/fluentbit.key \
                | gpg --dearmor -o /usr/share/keyrings/fluentbit.gpg
            codename="$(lsb_release -sc)"
            echo "deb [arch=$ARCH_DEB signed-by=/usr/share/keyrings/fluentbit.gpg] https://packages.fluentbit.io/ubuntu/$codename $codename main" \
                > /etc/apt/sources.list.d/fluent-bit.list
            apt-get update -qq
            apt-get install -y -qq fluent-bit
        elif grep -qi 'amazon linux' /etc/os-release 2>/dev/null; then
            cat > /etc/yum.repos.d/fluent-bit.repo <<'FBREPO'
[fluent-bit]
name=Fluent Bit
baseurl=https://packages.fluentbit.io/amazonlinux/2023/$basearch/
gpgcheck=1
enabled=1
gpgkey=https://packages.fluentbit.io/fluentbit.key
FBREPO
            dnf install -y -q fluent-bit
        else
            log "WARN: cannot install fluent-bit on this distro; skipping"
        fi
    fi

    # 8b. Deploy Fluent Bit config files.
    # #229: 优先从 s3://<assets>/deployment/observability/fluent-bit/ 拉最新版
    # (S3 asset 化,可下发新版无需重烤 edge AMI);拉失败回退 $SRC_DIR 里
    # asset bundle 版兜底(setup.sh 仍会同步 deploy/edge/ 全套过去),不静默
    # 起空配置。铁律#3:edge 重建 / systemctl restart 时才吃新配置,不开热注入。
    FB_CONF_DIR=/etc/fluent-bit
    mkdir -p "$FB_CONF_DIR"
    mkdir -p /var/lib/fluent-bit/storage
    FB_AWS_REGION_PULL="${AWS_REGION:-ap-southeast-1}"
    _fb_pull() {
        local name="$1"
        if [[ -n "${ASSETS_BUCKET:-}" ]] && \
           aws s3 cp "s3://${ASSETS_BUCKET}/deployment/observability/fluent-bit/${name}" \
             "$FB_CONF_DIR/${name}" \
             --region "$FB_AWS_REGION_PULL" --no-progress 2>/dev/null; then
            return 0
        fi
        log "WARN(#229): ${name} 未从 S3 拉到,回退 $SRC_DIR/fluent-bit 兜底"
        install -m 0644 "$SRC_DIR/fluent-bit/${name}" "$FB_CONF_DIR/${name}"
    }
    _fb_pull fluent-bit.conf
    _fb_pull parsers.conf
    _fb_pull extract_trace_root.lua

    # 8c. Template environment variables into Fluent Bit config.
    # AWS_REGION and FIREHOSE_DELIVERY_STREAM are passed by CDK/userdata.
    FB_AWS_REGION="${AWS_REGION:-ap-southeast-1}"
    FB_STREAM="${FIREHOSE_DELIVERY_STREAM:-}"
    if [[ -z "$FB_STREAM" ]]; then
        log "WARN: FIREHOSE_DELIVERY_STREAM not set; Fluent Bit output will fail until configured"
    fi
    sed -i "s|\${AWS_REGION}|${FB_AWS_REGION}|g" "$FB_CONF_DIR/fluent-bit.conf"
    sed -i "s|\${FIREHOSE_DELIVERY_STREAM}|${FB_STREAM}|g" "$FB_CONF_DIR/fluent-bit.conf"

    # 8d. Enable and start.
    systemctl enable fluent-bit
    systemctl restart fluent-bit
    log "fluent-bit installed and started (stream=${FB_STREAM:-UNSET})"
else
    log "LOGGING_ENABLED=false; skipping Fluent Bit install"
fi

# ── 9. Warmup wait on /healthz (the data-plane contract §6) ───────────────────
# nginx accepts on :8080 in ~200ms but route.lua's warmup gate returns 503
# until the async Redis PING succeeds (up to 30s + a bit of slack). We
# poll /healthz here so the userdata script only returns success once the
# instance is truly ready to serve — ASG lifecycle hook then signals
# CONTINUE. Cap at 90s to still fail-fast on genuinely broken Redis.
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

log "DONE: claw-edge active on :$LISTEN_PORT; self_ip=$SELF_IP; redis=$REDIS_HOST:$REDIS_PORT"
