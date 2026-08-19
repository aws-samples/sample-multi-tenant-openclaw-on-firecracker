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
# #531 — stage the pull, then judge what THIS run fetched. The guard used to be
# `[[ -f "$FB_CONF_DIR/fluent-bit.conf" ]]` right after an in-place recursive cp,
# and two facts made it unable to fail: (a) `aws s3 cp <prefix>/ <dst>/ --recursive`
# exits 0 when the prefix matches ZERO objects (unlike single-file cp), and (b) step 1
# already let the distro package write its own default config into $FB_CONF_DIR. So an
# empty prefix logged "pulled <role> config", skipped the FB_LOCAL_DIR fallback, and
# every later check passed against the package default: no ${FB_*} left unresolved, no
# delivery_stream line to be empty, no script line to be missing, and a dry-run that is
# valid because the package ships valid config. Measured 2026-08-18 on three in-service
# edges: input cpu.local → stdout, zero firehose hits, service active, installer exit 0.
_fb_stage="$(mktemp -d)"
_fb_cp_err="$(mktemp)"
trap 'rm -rf -- "$_fb_stage" "$_fb_cp_err"' EXIT
_fb_pulled=0
if [[ -n "${ASSETS_BUCKET:-}" ]]; then
    # stderr is where the zero-match notice lands, so it is kept, not sent to /dev/null.
    if aws s3 cp "s3://${ASSETS_BUCKET}/${_s3_prefix}/" "$_fb_stage/" \
         --recursive --region "$FB_REGION" --no-progress 2>"$_fb_cp_err"; then
        _fb_pulled="$(find "$_fb_stage" -type f | wc -l | tr -d '[:space:]')"
    else
        log "WARN: s3 cp of ${_s3_prefix}/ failed: $(tr '\n' ' ' < "$_fb_cp_err" | tail -c 300)"
    fi
fi
if [[ "$_fb_pulled" -gt 0 && -f "$_fb_stage/fluent-bit.conf" ]]; then
    while IFS= read -r _abs; do
        # Keep the prefix's relative layout: a flat install would break a config that
        # references a script by a subdirectory path.
        _rel="${_abs#"$_fb_stage"/}"
        mkdir -p "$FB_CONF_DIR/$(dirname "$_rel")"
        install -m 0644 "$_abs" "$FB_CONF_DIR/$_rel"
    done < <(find "$_fb_stage" -type f)
    log "pulled ${_fb_pulled} object(s) for role=${FB_ROLE} from s3://${ASSETS_BUCKET}/${_s3_prefix}/"
elif [[ -n "${FB_LOCAL_DIR:-}" && -f "${FB_LOCAL_DIR}/fluent-bit.conf" ]]; then
    log "WARN: S3 staged ${_fb_pulled} object(s) and no fluent-bit.conf among them; falling back to baked ${FB_LOCAL_DIR}"
    install -m 0644 "${FB_LOCAL_DIR}"/* "$FB_CONF_DIR/"
else
    die "no Fluent Bit config for role=${FB_ROLE}: s3://${ASSETS_BUCKET:-<unset>}/${_s3_prefix}/ staged ${_fb_pulled} object(s) with no fluent-bit.conf, and no FB_LOCAL_DIR fallback"
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

# #531 — every check above is an ABSENCE check: it can only fail on a config that
# already contains the thing being checked, so a config from the wrong source passes
# them all. Assert positively that this config forwards somewhere.
# Scope: this catches "no firehose output at all" — the distro package default, whether
# it came from the package install or from a stale S3 prefix. It does NOT catch the
# OTHER role's config: an edge config on a host has a firehose output too and passes
# here. Detecting role confusion needs a role-specific marker and is not done here.
if ! grep -Eqi '^[[:space:]]*Name[[:space:]]+kinesis_firehose[[:space:]]*$' "$FB_CONF_DIR/fluent-bit.conf"; then
    die "fluent-bit.conf has no kinesis_firehose output; refusing to start a collector that forwards nowhere (role=${FB_ROLE}, source ${_s3_prefix}/)"
fi
if ! grep -Eq '^[[:space:]]*delivery_stream[[:space:]]+[^[:space:]]' "$FB_CONF_DIR/fluent-bit.conf"; then
    die "fluent-bit.conf has a kinesis_firehose output but no delivery_stream carrying a value (role=${FB_ROLE})"
fi

# Every Lua script the config references must be on disk before we start.
# A stale S3 role prefix that still ships fluent-bit.conf but not its filters is
# without naming the file — the class of message that sent past investigations
# to Firehose and the host instead of to the asset prefix. Relative script paths
# resolve against the config dir (host style); absolute ones are used as-is
# (edge style).
while read -r _script; do
    [[ -n "$_script" ]] || continue
    case "$_script" in
        /*) _script_path="$_script" ;;
        *)  _script_path="$FB_CONF_DIR/$_script" ;;
    esac
    [[ -f "$_script_path" ]] || die "fluent-bit.conf references a missing Lua script: ${_script} (expected ${_script_path}; stale ${_s3_prefix}/ in S3?)"
done < <(sed -nE 's/^[[:space:]]*script[[:space:]]+([^[:space:]]+)[[:space:]]*$/\1/p' "$FB_CONF_DIR/fluent-bit.conf")

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
