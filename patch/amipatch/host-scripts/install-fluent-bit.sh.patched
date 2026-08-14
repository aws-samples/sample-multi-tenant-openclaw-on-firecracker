#!/usr/bin/env bash
# deploy/edge/fluent-bit/install-fluent-bit.sh — shared Fluent Bit installer
# for both edge and host roles.
#
# Runs at instance start (called by install-edge.sh on edge, by init-host.sh
# on host). Idempotent. The install *mechanism* is shared here; the collection
# *config* (what to tail, which stream) lives per-role in S3 under
# deployment/observability/fluent-bit/<role>/ and is pulled at runtime — so
# changing the collection target = edit S3 config + roll fresh instances,
#
# Contract (env set by caller):
#   FB_ROLE               edge | host — selects S3 subprefix + local fallback dir
#   LOGGING_ENABLED       true|false (default true); false = skip entirely
#                         Golden-image bake needs the package but MUST NOT bake the
#                         config: it carries per-deployment Firehose stream names, and
#                         an AMI is shared by the whole fleet. Boot then renders the
#                         config through a normal (non-install-only) call, which is a
#                         no-op on the install step because that is already guarded.
#   ASSETS_BUCKET         S3 bucket holding deployment/observability/fluent-bit/
#   AWS_REGION            region for s3 cp + Fluent Bit output
#   FB_LOCAL_DIR          local fallback config dir (edge only; host has none)
#   Any FB_* var          templated into the config via ${FB_*} placeholders
#                         (e.g. FB_STREAM_* → ${FB_STREAM_*}). Generic render
#                         so a new output target only needs new placeholders.
#
# The caller maps role-specific values into FB_* before calling, e.g. edge
# sets FB_REGION=$AWS_REGION, FB_STREAM=<edge stream>.

set -euo pipefail

log() { printf '[install-fluent-bit] %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

FB_ROLE="${FB_ROLE:?FB_ROLE (edge|host) must be set}"
LOGGING_ENABLED="${LOGGING_ENABLED:-true}"
if [[ "$LOGGING_ENABLED" != "true" ]]; then
    log "LOGGING_ENABLED=$LOGGING_ENABLED; skipping Fluent Bit install"
    exit 0
fi

FB_REGION="${FB_REGION:-${AWS_REGION:-ap-southeast-1}}"

# ── 1. Install Fluent Bit (Ubuntu apt / AL2023 dnf) ──────────────────────
# The official package installs the binary at /opt/fluent-bit/bin/fluent-bit, which is NOT
# on PATH (真机 2026-08-05, `dpkg -L fluent-bit` on Ubuntu 24.04 arm64; the AL2023 package
# uses the same prefix). So `command -v fluent-bit` alone reports "absent" on a machine that
# already has it — on a golden AMI that means every boot re-runs apt-get update + install,
# i.e. the zero-download boot path this whole block exists for is silently broken. Check the
# packaged location first, keep the PATH probe for a self-built or relocated binary.
fb_installed() {
    [[ -x /opt/fluent-bit/bin/fluent-bit ]] || command -v fluent-bit >/dev/null 2>&1
}

if ! fb_installed; then
    ARCH_DEB="amd64"; [[ "$(uname -m)" == "aarch64" || "$(uname -m)" == "arm64" ]] && ARCH_DEB="arm64"
    if grep -qi ubuntu /etc/os-release 2>/dev/null; then
        # modules:final 无 /dev/tty → rc=2 → init-host 非零 → ASG ABANDON,且每次
        # boot 复现不收敛。真机实测 --batch --no-tty 无效(仍 rc=2,错误变 "dearmoring
        # failed: File exists"),只有 --yes 才覆盖既有 keyring。
        curl -fsSL https://packages.fluentbit.io/fluentbit.key \
            | gpg --batch --yes --dearmor -o /usr/share/keyrings/fluentbit.gpg
        codename="$(lsb_release -sc)"
        echo "deb [arch=$ARCH_DEB signed-by=/usr/share/keyrings/fluentbit.gpg] https://packages.fluentbit.io/ubuntu/$codename $codename main" \
            > /etc/apt/sources.list.d/fluent-bit.list
        apt-get update -qq
        apt-get install -y -qq fluent-bit
    elif grep -qi 'amazon linux' /etc/os-release 2>/dev/null; then
        # 的 AL repo 不按 basearch 分子目录, 带上会 404 装不上。
        cat > /etc/yum.repos.d/fluent-bit.repo <<'FBREPO'
[fluent-bit]
name=Fluent Bit
baseurl=https://packages.fluentbit.io/amazonlinux/2023/
gpgcheck=1
enabled=1
gpgkey=https://packages.fluentbit.io/fluentbit.key
FBREPO
        dnf install -y -q fluent-bit
    else
        die "cannot install fluent-bit on this distro (need Ubuntu or Amazon Linux)"
    fi
fi

if [[ "${FB_INSTALL_ONLY:-0}" == "1" ]]; then
    # config pull so a bake needs neither ASSETS_BUCKET nor network S3 access, and cannot
    # accidentally capture another deployment's stream names into a shared image.
    log "FB_INSTALL_ONLY=1; package installed, skipping config + enable (bake mode)"
    exit 0
fi

# ── 2. Deploy config: pull latest from S3, fall back to baked local copy ──
# S3-asset first (下发新版无需重烤 AMI); on miss fall back to FB_LOCAL_DIR
# (edge ships a baked copy). Host has no local dir → S3 miss is fail-loud.
FB_CONF_DIR="${FB_CONF_DIR:-/etc/fluent-bit}"
FB_STORAGE_DIR="${FB_STORAGE_DIR:-/var/lib/fluent-bit/storage}"
mkdir -p "$FB_CONF_DIR" "$FB_STORAGE_DIR"
_s3_prefix="deployment/observability/fluent-bit/${FB_ROLE}"
if [[ -n "${ASSETS_BUCKET:-}" ]] && \
   aws s3 cp "s3://${ASSETS_BUCKET}/${_s3_prefix}/" "$FB_CONF_DIR/" \
     --recursive --region "$FB_REGION" --no-progress 2>/dev/null && \
   [[ -f "$FB_CONF_DIR/fluent-bit.conf" ]]; then
    log "pulled ${FB_ROLE} config from s3://${ASSETS_BUCKET}/${_s3_prefix}/"
elif [[ -n "${FB_LOCAL_DIR:-}" && -f "${FB_LOCAL_DIR}/fluent-bit.conf" ]]; then
    log "WARN: S3 config unavailable; falling back to baked ${FB_LOCAL_DIR}"
    install -m 0644 "${FB_LOCAL_DIR}"/* "$FB_CONF_DIR/"
else
    die "no Fluent Bit config for role=${FB_ROLE} (S3 miss + no local fallback)"
fi

# ── 3. Render ${FB_*} placeholders from the environment ──────────────────
# Generic: every FB_* env var replaces its ${FB_*} placeholder in the config.
# Adding a new output (e.g. Kafka) only needs new FB_* vars + placeholders,
# not a change to this script.
while IFS='=' read -r _name _val; do
    [[ "$_name" == FB_* ]] || continue
    sed -i "s|\${${_name}}|${_val}|g" "$FB_CONF_DIR/fluent-bit.conf"
done < <(env)

if grep -Eq '\$\{FB_[A-Z0-9_]+\}' "$FB_CONF_DIR/fluent-bit.conf"; then
    grep -nE '\$\{FB_[A-Z0-9_]+\}' "$FB_CONF_DIR/fluent-bit.conf" >&2 || true
    die "unresolved FB_* placeholder(s) remain in fluent-bit.conf"
fi
if grep -Eq '^[[:space:]]*delivery_stream[[:space:]]*$' "$FB_CONF_DIR/fluent-bit.conf"; then
    die "delivery_stream rendered empty"
fi

FB_BIN="$(command -v fluent-bit || true)"
[[ -n "$FB_BIN" ]] || FB_BIN=/opt/fluent-bit/bin/fluent-bit
[[ -x "$FB_BIN" ]] || die "fluent-bit binary not found after installation"
"$FB_BIN" --dry-run -c "$FB_CONF_DIR/fluent-bit.conf" \
    || die "fluent-bit config dry-run failed"

# ── 4. Enable + (re)start ────────────────────────────────────────────────
systemctl enable fluent-bit
systemctl restart fluent-bit
systemctl is-active --quiet fluent-bit || die "fluent-bit did not become active"
log "fluent-bit installed and started (role=${FB_ROLE})"
