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

# #625 —— reader endpoint 的采用形态必须离开这台机器。三种 SSM 失败(调用失败 /
# 空值 / 缺 :port)此前都只往安装日志写一行 WARN 就回落 primary,而安装日志不进
# 任何采集通道:edge 的 Fluent Bit 只采 journald 里 SYSLOG_IDENTIFIER=claw_edge
# 的记录(那是 nginx access log),而且它在本段之后才装、配的又是 Read_From_Tail
# On —— 本段写的任何东西它都读不到。结果是"部分箱子读 reader、部分箱子回落
# primary"这种半收敛机队在机器外面没有任何信号:开关翻开后 primary 仍在承担一部
# 分本该分流走的读,而容量判断照着"全机队已分流"走。
#
# 发一个 namespace 受限的自定义指标补上这个信号。发送失败只 WARN 不 die:缺可观测
# 比 edge 拉不起来轻,而这一段刻意要避免的失败正是"把 edge 拉挂"。
#
# 已知坑(不靠不做来规避,写在这里):定向 promotion 只推代码不同步 IAM,所以走定向
# promotion 部署本改动时必须手工把 EdgeRole 的 cloudwatch:PutMetricData 一起同步。
# 少了它每台都只写 WARN、指标恒缺、告警按 notBreaching 恒不响 —— 外部表现与"没装
# 这个改动"完全一致,只能靠读本行 WARN 区分。
# 这个指标是"回落台数的下界",不是精确值,原因写在这里而不是靠沉默:三档回落里
# 只有 empty / malformed 一定发得出去(网络是通的,只是 SSM 参数值不对);
# ssm_error 那一档,如果根因是本机出不了网(NAT/VPC endpoint/SG),那么紧接着的
# PutMetricData 会同源失败,这台的数据点就丢了。下面的有限重试只能兜住瞬时抖动
# (DNS、限流),兜不住持续断网。所以告警只能作单向解读:响了一定有箱子回落;
# 没响不等于全机队都分流了。持续断网那一档的检测归 #606(edge readiness 与
# primary/控制面可达性解耦,目前无指标无告警),本 MR 不在这里假装解决。
EDGE_METRIC_NAMESPACE="OpenClaw/Edge"
EDGE_METRIC_PUT_ATTEMPTS=3
EDGE_METRIC_PUT_BACKOFF_SEC=2

# 单次 aws 调用的网络预算。CLI v2 的默认值是 connect 60s / read 60s(aws-cli
# 2.34.30 的 `aws ... help` 原文),重试用 standard 模式、默认 3 次总尝试
# (退避上限 20s,见 cli-configure-retries)。于是一次打到黑洞端点的调用
# (缺 NAT / 缺 VPC endpoint / SG 拦出网,SYN 被丢而不是被拒)最坏能耗 ~180s;
# 乘上下面 3 轮外层重试是 ~540s,远超 edge 的 health_check_grace_period=300s ——
# "补一个观测信号"就变成"被 ASG 换机",故障形态换了但没修好。这里把单次调用钉在
# ~10s 并关掉 CLI 自己的重试(改由下面两个外层有界重试接手)。
#
# AWS_MAX_ATTEMPTS=1 是【总尝试次数】而不是重试次数:官方原文
# (cli-configure-retries「Max attempts」)是 "the initial call counts toward the
# value that you provide",所以 1 = 打一次、零重试。关掉 CLI 的重试必须同时给
# 两个调用各自补一层外层重试,否则一次 DNS 抖动或限流就把这台机器永久钉在
# primary —— 那正是本次要修的失效形态,只是换了触发源。
# AWS_MAX_ATTEMPTS 只作命令级前缀,不 export:第 8 段的 fluent-bit 安装要从 S3
# 拉大文件,那条链路需要 CLI 的默认重试,不能被这里的收紧连带影响。
#
# 总预算:SSM 3 次 × ~10s + 2 次 2s 退避 ≈ 34s,PutMetricData 同形 ≈ 34s,
# 合计 ≤ ~68s,仍远小于 300s 宽限期。
EDGE_AWS_BUDGET_ARGS=(--cli-connect-timeout 5 --cli-read-timeout 5)
EDGE_AWS_MAX_ATTEMPTS=1
EDGE_SSM_GET_ATTEMPTS=3
EDGE_SSM_GET_BACKOFF_SEC=2

# 空的 AWS_REGION 必须【unset】,不是"不传 --region"就够了。实测
# (aws-cli 2.34.30):导出 AWS_REGION="" 时 CLI 把区域解析成空串、来源报 env
# (解析链是 ['AWS_REGION','AWS_DEFAULT_REGION']),压过 profile/config/IMDS,
# 直接报 `Invalid endpoint: https://sts..amazonaws.com`。也就是说光是不传参数,
# 环境里那个空值仍然赢过 IMDS,调用照样必然失败 —— 而在下面那段里"调用失败"
# 表现成静默回落 primary,正是本次要修的失效形态。CDK 的 edge userdata 导出的是
# 真区域(ha_edge.py 那行 `AWS_REGION="{self.region}"`),但手工/救援执行时可能
# 继承到空值。空则 unset,让 CLI 自己走 config/IMDS。unset 不影响第 8 段的
# `${AWS_REGION:-ap-southeast-1}`:`:-` 对"未设"与"空值"取同一个默认。
for _region_var in AWS_REGION AWS_DEFAULT_REGION; do
    if [[ -z "${!_region_var:-}" ]]; then
        unset "$_region_var"
    fi
done

# 区域已知就显式传,不依赖 IMDS 能不能读到。
# 注意不能写 `[[ -n ... ]] && arr=(...)`:条件为假时整行返回 1,在
# set -e 下会直接把 edge 拉挂。
AWS_REGION_ARGS=()
if [[ -n "${AWS_REGION:-}" ]]; then
    AWS_REGION_ARGS=(--region "$AWS_REGION")
fi

emit_reader_endpoint_metric() {
    # $1: 0=采用了 SSM 的 reader endpoint / 1=回落 primary
    # $2: 回落原因,取值来自本脚本的固定白名单(none/ssm_error/empty/malformed)
    local fallback="$1" reason="$2" payload attempt
    # 两个数据点一次发完:无维度那条给告警用(Sum>0 = 至少一台回落),带 Reason 维度
    # 那条给排查用 —— 回落原因决定处置(缺 IAM 权限 vs SSM 参数本身畸形)。正常档
    # 也发 0,所以"指标有 0 数据点"与"指标完全缺失"可区分,后者是通道自己坏了。
    payload="$(printf '[{"MetricName":"RedisReaderEndpointFallback","Value":%s,"Unit":"Count"},{"MetricName":"RedisReaderEndpointFallback","Dimensions":[{"Name":"Reason","Value":"%s"}],"Value":%s,"Unit":"Count"}]' \
        "$fallback" "$reason" "$fallback")"
    for ((attempt = 1; attempt <= EDGE_METRIC_PUT_ATTEMPTS; attempt++)); do
        if AWS_MAX_ATTEMPTS="$EDGE_AWS_MAX_ATTEMPTS" aws cloudwatch put-metric-data \
            --namespace "$EDGE_METRIC_NAMESPACE" \
            --metric-data "$payload" \
            "${EDGE_AWS_BUDGET_ARGS[@]}" \
            "${AWS_REGION_ARGS[@]+"${AWS_REGION_ARGS[@]}"}" >/dev/null 2>&1; then
            return 0
        fi
        # 最后一次失败不再 sleep:那几秒纯粹是拖长 edge 的启动时间,而这条链路
        # 已经确定拿不到信号了。写成 if 而不是 `[[ ]] && sleep`:后者在最后一轮
        # 返回 1,是循环体的最后一条命令,set -e 会就地把 edge 拉挂。
        if [[ "$attempt" -lt "$EDGE_METRIC_PUT_ATTEMPTS" ]]; then
            sleep "$EDGE_METRIC_PUT_BACKOFF_SEC"
        fi
    done
    log "WARN: failed to publish ${EDGE_METRIC_NAMESPACE}/RedisReaderEndpointFallback=${fallback} reason=${reason} after ${EDGE_METRIC_PUT_ATTEMPTS} attempts; fleet convergence stays invisible outside this instance"
}

fetch_reader_endpoint() {
    # 只对【调用失败】重试。空值 / 缺 :port 不是瞬时故障:SSM 参数值本身不对,再读
    # N 次还是同一个值,重试纯粹白花 edge 的启动时间,所以那两档由调用方判、不重试。
    #
    # 结果写全局 READER_ENDPOINT 而不是 echo 出去让调用方做命令替换:命令替换会起子
    # shell,退避次数和重试轮数留在子 shell 里,外面既看不到也测不到。
    local attempt
    for ((attempt = 1; attempt <= EDGE_SSM_GET_ATTEMPTS; attempt++)); do
        if READER_ENDPOINT="$(AWS_MAX_ATTEMPTS="$EDGE_AWS_MAX_ATTEMPTS" aws ssm get-parameter \
            --name /openclaw/engine/redis/reader-endpoint \
            --query 'Parameter.Value' \
            --output text \
            "${EDGE_AWS_BUDGET_ARGS[@]}" \
            "${AWS_REGION_ARGS[@]+"${AWS_REGION_ARGS[@]}"}" 2>/dev/null)"; then
            return 0
        fi
        # 与 emit_reader_endpoint_metric 同款:最后一次失败不再退避,且写成 if 而不是
        # `[[ ]] && sleep` —— 后者在最后一轮返回 1,作为循环体最后一条命令会被 set -e
        # 当成脚本失败,直接把 edge 拉挂。
        if [[ "$attempt" -lt "$EDGE_SSM_GET_ATTEMPTS" ]]; then
            sleep "$EDGE_SSM_GET_BACKOFF_SEC"
        fi
    done
    return 1
}

_reader_fallback=1
_reader_fallback_reason=ssm_error
if ! fetch_reader_endpoint; then
    log "WARN: failed to read Redis reader endpoint from SSM after ${EDGE_SSM_GET_ATTEMPTS} attempts; falling back to primary"
elif [[ -z "$READER_ENDPOINT" || "$READER_ENDPOINT" == "None" ]]; then
    _reader_fallback_reason=empty
    log "WARN: Redis reader endpoint from SSM is empty; falling back to primary"
else
    _reader_host="${READER_ENDPOINT%:*}"
    _reader_port="${READER_ENDPOINT##*:}"
    if [[ -z "$_reader_host" || -z "$_reader_port" || "$_reader_host" == "$_reader_port" ]]; then
        _reader_fallback_reason=malformed
        log "WARN: Redis reader endpoint missing host:port; falling back to primary"
    else
        READER_HOST="$_reader_host"
        READER_PORT="$_reader_port"
        _reader_fallback=0
        _reader_fallback_reason=none
    fi
fi
emit_reader_endpoint_metric "$_reader_fallback" "$_reader_fallback_reason"

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
# NO Environment=ENGINE_REDIS_*_HINT lines here on purpose (#639). They used to
# be here to feed route.lua's warmup_probe(), which needs the endpoint at
# init_worker phase (ngx.var isn't available yet) — but nginx wipes the worker
# environment except TZ unless each name is declared with the `env` directive,
# and nginx.conf declared none. So that channel never worked: os.getenv returned
# nil, the probe marked the instance ready WITHOUT reaching Redis, and #618's
# readiness gate was permanently fail-open. The coordinates now travel through
# nginx.conf's init_by_lua_block, rendered by the SAME envsubst call above that
# renders $edge_redis_* — one channel instead of two that must agree.
cat > /etc/systemd/system/claw-edge.service <<UNIT
[Unit]
Description=OpenClaw Pool edge (OpenResty)
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
PIDFile=/usr/local/openresty/nginx/logs/nginx.pid
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
