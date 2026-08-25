# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# shellcheck shell=bash
# No shebang, matching init-host.sh: both are fetched and run as `bash <file>` by the
# bootstrap / Image Builder component, never exec'd directly.
#
#
# Host bring-up is split in two stages, and the split line is exactly one question:
# does this step reach the internet or install a package?
#
#   provision (this file)  installs components. No per-host and no per-deployment input,
#                          so it can be baked into a golden AMI once and reused by every
#                          host. Every COMPONENT INSTALL in the boot path lives here.
#   configure (init-host)  renders /etc/platform.env, mounts the data volume, generates this
#                          host's key, registers to DynamoDB. Needs per-host identity and
#                          per-deployment secrets, so it can NEVER be baked.
#
# The boundary is NOT the old step numbering. It is drawn where it is because the reason for
# the golden AMI is that network installs are unreliable: a wrong-arch awscli zip, a missing
# aarch64 vmlinux suffix and a Firecracker tarball 404 have each already cost a host its
# lifecycle hook and put the ASG into an ABANDON-and-replace loop. Baking every INSTALL means
# none of those can happen again.
#
# #523 判据 5 —— 上面两句原本写的是「Every external download … lives here」与「a golden
# host boots with zero external fetches」。那不是事实,而且从未是事实:configure 仍然
# 【无条件】从 S3 拉 install-fluent-bit.sh(S3 miss 即 exit 1)、oc-guest-log-reader.py、
# rootfs manifest.json 与 step4 的全部生命周期脚本。成立的那一半是**组件零安装**。
# 说清楚这件事有实际代价上的理由:按「零下载」去排查启动失败会找错方向,而客户那两次
# canary ABANDON 恰好发生在「AMI 已经有、启动仍跑 S3 那份」这条路上(#520 A21 实例①)。
#
# Two callers, one script:
#   bake  EC2 Image Builder runs it with OC_PROVISION_BAKE=1. That additionally scrubs
#         anything host-identifying before the snapshot is taken.
#   boot  init-host.sh runs it when /etc/openclaw/.ami-provisioned is absent, i.e. on a
#         plain Ubuntu AMI. This keeps the non-golden path working exactly as before.
#
# Therefore every step must be idempotent: re-running on a provisioned host is a no-op, and
# a golden host that runs configure only must be indistinguishable from one that ran both.
#
# #523 判据 3 —— 这里只许放「装一次就够」的动作。init-host.sh 见到
# /etc/openclaw/.ami-provisioned 就【整段跳过】本脚本,所以任何**必须每次开机做**的动作
# 放进来,在 golden 机队上会静默不执行,而 plain 机队正常 —— 两种机器行为分叉,且没有
# 任何日志会说「这一步没做」。今天是安全的(本脚本只装组件),这条契约是为了让它保持
# 安全。判据很简单:**开机后不残留的动作一律不许放这里**。sysctl -w、modprobe、
# iptables/nft 规则、ip link/route、swapoff、mount、写 /proc 与 /sys、systemctl
# start/restart/daemon-reload(enable 可以,它落盘)都属于这一类;它们的正确位置是
# init-host.sh 里 marker 判定【之外】的段落(那里每次开机都跑)。
# 这条契约由 tests/test_523_provision_no_per_boot_actions.py 机械执行,不靠人自觉。
#
# It must NEVER write a per-host secret or a per-deployment value. Anything written here ends
# up in an AMI shared by the entire fleet, so a per-host key written here would become one
# key shared by all hosts — and that key's public half is injected into every tenant microVM.

set -euo pipefail

PROVISION_RECIPE_VERSION="${OC_PROVISION_RECIPE_VERSION:-unversioned}"
MARKER=/etc/openclaw/.ami-provisioned
# 摘要校验,查不到该版本的摘要就 die。换版本必须同时改这里的默认值与那张摘要表,再重跑 setup.sh
# 把新版本镜像到 S3(setup.sh 从本行解析版本号,传一个不一致的 FC_VERSION 会被它直接拒绝)。
# 这条口径已同步到 docs/aws-guide{,-en}/05-deploy-use-troubleshoot.md,并有断言钉住文档不再
# 承诺"只设环境变量即可覆盖"。
FC_VER="${FC_VERSION:-v1.15.1}"
# Guest kernel is fetched from the Firecracker CI bucket, whose layout is version- and
# arch-specific. Baked to the root volume, NOT to /data: the data volume does not exist at
# bake time and is reformatted per host, so anything staged there would be lost anyway.
BAKED_DIR=/opt/openclaw/baked

log() { echo "[oc:provision] $(date +%H:%M:%S) $*"; }
die() { echo "[oc:provision] FATAL: $*" >&2; exit 1; }

ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|aarch64) ;;
  *) die "unsupported CPU architecture ${ARCH}" ;;
esac

# Retry every network fetch. A single transient failure here is what turns into an ABANDONed
# lifecycle hook on the boot path, and into a failed bake on the Image Builder path.
_fetch() {  # $1=url $2=dest
  local url="$1" dest="$2" i
  for i in $(seq 1 10); do
    if curl -fsSL --connect-timeout 10 --max-time 600 -o "${dest}.part" "${url}"; then
      mv -f "${dest}.part" "${dest}"
      return 0
    fi
    log "fetch failed ($i/10): ${url}"
    sleep 15
  done
  rm -f "${dest}.part"
  die "could not fetch ${url} after 10 attempts"
}

# Skip apt entirely when every package is already present. `apt-get update` is a network
# call, so an unguarded install would mean a provisioned host still reaches out — which is
# the exact failure mode the golden image exists to remove.
_apt_install() {
  local missing=() pkg
  for pkg in "$@"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    log "apt: all present ($*)"
    return 0
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get -o DPkg::Lock::Timeout=60 update -qq
  apt-get -o DPkg::Lock::Timeout=60 install -y -qq "${missing[@]}" >/dev/null
  log "apt: installed ${missing[*]}"
}

log "provision start: arch=${ARCH} recipe=${PROVISION_RECIPE_VERSION} bake=${OC_PROVISION_BAKE:-0}"

# ── 1. Base packages ────────────────────────────────────────────────────────────────────
# gettext-base supplies envsubst, which step 2b below needs to render the ADOT config. It is
# listed explicitly because a golden host must not depend on the base AMI happening to ship
# it: the whole point of provisioning is that the boot path installs no components.
_apt_install curl jq unzip pigz python3-redis gettext-base
log "base packages present"

# ── 2. awscli ───────────────────────────────────────────────────────────────────────────
# The zip MUST match the host architecture. Installing the x86_64 zip on a Graviton metal
# gives /usr/local/bin/aws "Exec format error", every aws call in configure fails, its retry
# loops burn 2x20x15s = 10 min against a 600 s lifecycle timeout, and the ASG replaces the
# metal forever. Select by uname, and refuse an unknown arch above rather than guess.
if command -v aws >/dev/null 2>&1; then
  log "awscli already installed: $(aws --version 2>&1 | head -1)"
else
  # Unpack in a private mktemp dir, not a fixed /tmp path: a predictable name in a
  # world-writable directory is a symlink-swap target, and the installer runs as root.
  _cli_dir="$(mktemp -d)"
  _fetch "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" "${_cli_dir}/awscliv2.zip"
  unzip -qo "${_cli_dir}/awscliv2.zip" -d "${_cli_dir}"
  "${_cli_dir}/aws/install" >/dev/null
  rm -rf "${_cli_dir}"
  command -v aws >/dev/null 2>&1 || die "awscli install produced no aws binary"
  log "awscli installed: $(aws --version 2>&1 | head -1)"
fi

# ── 3. Firecracker + jailer ─────────────────────────────────────────────────────────────
# Pinned: `latest` may not have a matching CI guest kernel yet, which 404s the vmlinux fetch
# below. Guarded on the installed version so a re-run with the same pin is a no-op and a
# changed pin actually upgrades.
#
# 每台都打一次 github.com/firecracker-microvm/firecracker/releases,撞 GitHub rate limit →
# 部分 host bootstrap 失败 → lifecycle hook ABANDON → ASG 替换循环。改后只有【部署机一次】
# 打 GitHub(setup.sh 的 mirror 步),机队全部打 S3。
#
# 桶名为什么在这里自解析而不是由 init-host 传进来:init-host.sh 在 :151 就调用本脚本,而它
# 到 :232 才解析出 ASSETS_BUCKET —— 那时序反了。更根本的是 awscli 是本脚本第 2 节才装的,
# :151 之前连 `aws` 命令都没有,所以 init-host 也没法提前用 _stack_output 拿桶名。
# 本脚本又被 ha_edge.py:583 的 synth 护栏禁止携带模板占位符(它烤进全机队共享的 AMI,不能带
# per-deployment 值;护栏是纯字符串包含检查,连注释里出现双花括号都会判负,所以这里不写它)。
# 于是用 IMDS 取 accountId+region 按命名约定拼 —— 这个公式
# 【不是新发明的】,是 init-host.sh:237 已有的同一条 fallback。两处必须保持一致,
# tests/test_435_fc_binary_from_s3.py 有一条断言钉住它们逐字相同。
# 允许 OC_FC_S3_URI 覆盖:bake 路径(Image Builder)可直接指定,也便于测试。
_fc_s3_uri() {  # 输出 s3://... 或空(拿不到桶名)
  if [ -n "${OC_FC_S3_URI:-}" ]; then printf '%s' "${OC_FC_S3_URI}"; return; fi
  local _bkt="${OC_ASSETS_BUCKET:-}" _tok _acct _region _rsfx
  # OC_ASSETS_BUCKET 由 Packer 模板注入(host-golden.pkr.hcl:381 与 :447)。在本 issue 之前
  # 它【没有消费者】—— #520 B4 就是因为 deploy/packer/ 的文档已按"有人读"写、代码却不读,
  # 才加了一道机器护栏。这里让它真的有消费者:bake 路径不必再走 IMDS,客户手册里那个桶名
  # 参数也终于生效。取不到就退到 IMDS 自解析(boot 路径没有这个变量)。
  if [ -z "${_bkt}" ]; then
    _tok="$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" \
      -H "X-aws-ec2-metadata-token-ttl-seconds: 60" --max-time 3 2>/dev/null)" || return 0
    _acct="$(curl -fsS -H "X-aws-ec2-metadata-token: ${_tok}" --max-time 3 \
      "http://169.254.169.254/latest/dynamic/instance-identity/document" 2>/dev/null \
      | sed -n 's/.*"accountId"[ ]*:[ ]*"\([0-9]*\)".*/\1/p')" || return 0
    _region="$(curl -fsS -H "X-aws-ec2-metadata-token: ${_tok}" --max-time 3 \
      "http://169.254.169.254/latest/dynamic/instance-identity/document" 2>/dev/null \
      | sed -n 's/.*"region"[ ]*:[ ]*"\([a-z0-9-]*\)".*/\1/p')" || return 0
    # accountId 必须是合法 12 位,否则拼出 openclaw-assets--<region> 这种 double-dash 坏名
    # (init-host.sh:235-237 为同一原因 fail-loud)。这里拿不到就返回空 → 调用方回落 GitHub。
    echo "${_acct}" | grep -qE '^[0-9]{12}$' || return 0
    [ -n "${_region}" ] || return 0
    _rsfx=$([ "${_region}" = "ap-southeast-1" ] && echo "" || echo "-${_region}")
    _bkt="$(printf 'openclaw-assets-%s%s' "${_acct}" "${_rsfx}")"
  fi
  printf 's3://%s/deployment/binaries/firecracker/%s/firecracker-%s-%s.tgz' \
    "${_bkt}" "${FC_VER}" "${FC_VER}" "${ARCH}"
}

# 摘要固定 —— 换源到自家 S3【必须】同时钉住内容,否则这次改动是净负的安全变化:
# host role 对 assets 桶是 grant_read_write(compute.py:43),也就是任何一台被攻陷的 host
# 都能覆盖这个 tarball;取源从 github(别人改不了你装什么)换成自家桶(改得了)之后,
# 一台失陷就等于后续全机队以 root 装它塞进来的二进制。本仓对同一威胁已有先例:
# host_image.py 对从 S3 取回的 provision-host.sh 就做 sha256sum -c(注释原话「S3 对象可能被换」)。
#
# 键带版本号是刻意的:改 FC_VER 而不更新摘要 → 查不到 → die。这正是要的 fail-closed —— 宁可
# 版本升级被卡住,也不装一个没核对过的二进制。摘要来自 GitHub 官方发布物,与 S3 对象 metadata
# 里记录的 sha256 一致(真机 head-object 复核过)。
_fc_expected_sha() {  # 输出 64 位十六进制,或空(该版本/架构没有钉死的摘要)
  case "${FC_VER}:${ARCH}" in
    v1.15.1:aarch64) printf '00654ac1e702a22744121ea9f10a4f792ebd7c3a744cba587dfac9fcb79b41a5' ;;
    v1.15.1:x86_64)  printf 'd4a32ab2322d887ca1bc4a4e7afa9cc35393e6362dfc2b3becb389d362e4275a' ;;
    *) return 0 ;;
  esac
}

# S3 取件带有界重试。没有重试的话,一次瞬时 S3/IMDS 抖动会让并发启动的整批 host 同时回落
# github —— 那正是本 issue 要消灭的 rate-limit 雷群,只是触发条件换成了 S3 抖动。
_fc_s3_fetch() {  # $1=s3 uri $2=dest;成功 0,失败非 0
  local uri="$1" dest="$2" i _nap
  for i in 1 2 3; do
    if aws s3 cp "${uri}" "${dest}" --only-show-errors 2>/dev/null; then
      return 0
    fi
    # 退避【带抖动】。固定间隔的后果是整批并发启动的 host 保持同步:同一秒一起重试、
    # 同一秒一起放弃、同一秒一起去打 github —— 那就把 S3 的一次抖动放大成本 issue 要消灭的
    # GitHub 限流雷群。抖动把它摊开。指数部分用 i 递增。
    _nap=$(( i * 5 + RANDOM % 10 ))
    log "firecracker S3 fetch failed ($i/3), retry in ${_nap}s: ${uri}"
    sleep "${_nap}"
  done
  return 1
}

_fc_installed=""
if [ -x /usr/local/bin/firecracker ]; then
  _fc_installed="$(/usr/local/bin/firecracker --version 2>/dev/null | head -1 || true)"
fi
case "${_fc_installed}" in
  *"${FC_VER}"*) log "firecracker ${FC_VER} already installed" ;;
  *)
    _fc_dir="$(mktemp -d)"
    _fc_src="$(_fc_s3_uri)"
    if [ -n "${_fc_src}" ] && _fc_s3_fetch "${_fc_src}" "${_fc_dir}/fc.tgz"; then
      log "firecracker ${FC_VER} tarball from S3: ${_fc_src}"
    else
      # 回落只在 S3 拿不到时发生,且必须【大声】记录:10W 规模下静默回落 GitHub 等于
      log "WARN firecracker ${FC_VER} tarball NOT from S3 (src='${_fc_src:-unresolved}') — falling back to github.com; at fleet scale this is the rate-limit path #435 exists to remove"
      # OC_FC_REQUIRE_S3=1 把回落变成硬失败。为什么要这个开关:CUSTOMER-GUIDE §3 的立项理由
      # 有一条正确的反驳 —— 只要回落存在,"GitHub 不可达时 host 仍能起来"这项验收就永远通过,
      # 真实依赖被掩盖。但直接删掉回落会打死没有 S3 的 plain-AMI 与开发路径。于是把"零 github"
      # 从但愿如此变成可强制:烤镜像/验收路径显式打开它,失败落在这里,而不是落在机队里。
      if [ "${OC_FC_REQUIRE_S3:-0}" = "1" ]; then
        die "firecracker ${FC_VER} tarball unavailable from S3 (src='${_fc_src:-unresolved}') and OC_FC_REQUIRE_S3=1 forbids the github.com fallback"
      fi
      _fetch "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VER}/firecracker-${FC_VER}-${ARCH}.tgz" \
        "${_fc_dir}/fc.tgz"
    fi
    # 校验摆在解包与 install 之前,且对【两个来源都生效】:S3 副本可能被失陷 host 换掉,
    # github 也可能被劫持或换了发布物。不匹配就 die,绝不装。
    _fc_want="$(_fc_expected_sha)"
    [ -n "${_fc_want}" ] || die "no pinned sha256 for firecracker ${FC_VER} ${ARCH} — add it to _fc_expected_sha() before bumping FC_VER; refusing to install an unverified binary"
    _fc_got="$(sha256sum "${_fc_dir}/fc.tgz" | cut -d' ' -f1)"
    [ "${_fc_got}" = "${_fc_want}" ] || die "firecracker ${FC_VER} ${ARCH} tarball sha256 mismatch (source=${_fc_src:-github.com}): got ${_fc_got}, want ${_fc_want} — refusing to install"
    log "firecracker ${FC_VER} tarball sha256 verified: ${_fc_got}"
    tar -xzf "${_fc_dir}/fc.tgz" -C "${_fc_dir}"
    install -o root -g root -m 0755 \
      "${_fc_dir}/release-${FC_VER}-${ARCH}/firecracker-${FC_VER}-${ARCH}" /usr/local/bin/firecracker
    install -o root -g root -m 0755 \
      "${_fc_dir}/release-${FC_VER}-${ARCH}/jailer-${FC_VER}-${ARCH}" /usr/local/bin/jailer
    rm -rf "${_fc_dir}"
    log "firecracker ${FC_VER} installed"
    ;;
esac

# ── 4. Guest kernel ────────────────────────────────────────────────────────────────────
# x86_64 uses the -no-acpi variant; aarch64 has no such object and requesting it 404s, which
# used to exit 22 under set -e and ABANDON the hook, so the metal never came up. Baked here
# so a golden host has the kernel on disk before it ever needs it.
install -d -m 0755 "${BAKED_DIR}"
if [ "${ARCH}" = "aarch64" ]; then VMLINUX_NAME="vmlinux-5.10.245"; else VMLINUX_NAME="vmlinux-5.10.245-no-acpi"; fi
FC_MAJOR="$(echo "${FC_VER}" | grep -oE 'v[0-9]+\.[0-9]+')"
if [ -s "${BAKED_DIR}/vmlinux" ]; then
  log "guest kernel already baked: $(stat -c %s "${BAKED_DIR}/vmlinux") bytes"
else
  _fetch "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/${FC_MAJOR}/${ARCH}/${VMLINUX_NAME}" \
    "${BAKED_DIR}/vmlinux"
  log "guest kernel baked: ${VMLINUX_NAME} ($(stat -c %s "${BAKED_DIR}/vmlinux") bytes)"
fi

# ── 5. ADOT collector ──────────────────────────────────────────────────────────────────
# Package only. Its config is per-deployment (it carries the AMP remote-write URL), so
# rendering and enabling the unit stays in configure.
if dpkg -s aws-otel-collector >/dev/null 2>&1; then
  log "aws-otel-collector already installed"
else
  ARCH_DEB="amd64"; [ "${ARCH}" = "aarch64" ] && ARCH_DEB="arm64"
  _adot_dir="$(mktemp -d)"
  _fetch "https://aws-otel-collector.s3.amazonaws.com/ubuntu/${ARCH_DEB}/latest/aws-otel-collector.deb" \
    "${_adot_dir}/aws-otel-collector.deb"
  dpkg -i "${_adot_dir}/aws-otel-collector.deb" >/dev/null 2>&1 || apt-get -f install -y -qq
  rm -rf "${_adot_dir}"
  dpkg -s aws-otel-collector >/dev/null 2>&1 || die "aws-otel-collector install did not register"
  log "aws-otel-collector installed"
fi

# ── 6. Fluent Bit ──────────────────────────────────────────────────────────────────────
# Package only, through the same installer edge uses, so the repo key and distro handling
# have one source of truth. FB_INSTALL_ONLY makes it stop before writing any config, which is
# per-deployment (Firehose stream names). Absent on a boot-path host, configure's own call
# installs it then — the installer is idempotent either way.
# The packaged binary lands in /opt/fluent-bit/bin, off PATH (真机 2026-08-05: `dpkg -L
# fluent-bit` on Ubuntu 24.04 arm64), so a PATH-only probe would report absent on a golden
# AMI that has it and make configure reinstall it over the network on every boot.
if [ -x /opt/fluent-bit/bin/fluent-bit ] || command -v fluent-bit >/dev/null 2>&1; then
  log "fluent-bit already installed"
elif [ -n "${OC_PROVISION_FLUENT_BIT_INSTALLER:-}" ] && [ -f "${OC_PROVISION_FLUENT_BIT_INSTALLER}" ]; then
  FB_ROLE=host FB_INSTALL_ONLY=1 LOGGING_ENABLED=true \
    bash "${OC_PROVISION_FLUENT_BIT_INSTALLER}" || die "fluent-bit package install failed"
  log "fluent-bit installed"
else
  # No installer available at bake time is not fatal: configure pulls it from S3 and installs
  # it there. Say so, because it means this AMI does NOT even have a zero-INSTALL boot path
  # (configure fetches the installer from S3 either way — #523 判据 5 — but with the package
  # already baked that fetch is followed by a no-op instead of an apt install).
  log "WARN: fluent-bit installer not provided; boot path will install it (one network fetch)"
fi

# ── 7. Bake-only scrub ─────────────────────────────────────────────────────────────────
# Everything below runs ONLY when baking an image. An AMI is shared by every host in the
# fleet, so anything host-identifying left in it becomes fleet-wide shared state. The one
# that matters most is /etc/openclaw/host_vm_key: it is a per-host ed25519 key whose public
# half launch-vm.sh injects into every tenant microVM. Baked once and shared, ANY host's
# private key would SSH into ANY tenant's microVM on ANY host — a cross-tenant break.
#
# provision never creates that key, so this is defence in depth against a future edit or a
# bake taken from a machine that had already run configure. configure asserts the other half
# of the invariant at boot: see init-host.sh's host-key provenance check, which fails closed
# rather than silently adopting an inherited key.
if [ "${OC_PROVISION_BAKE:-0}" = "1" ]; then
  log "bake mode: scrubbing host-identifying state before snapshot"
  rm -f /etc/openclaw/host_vm_key /etc/openclaw/host_vm_key.pub \
        /etc/openclaw/host_vm_key.instance /etc/platform.env /data/agentcore.env
  rm -f /etc/ssh/ssh_host_*
  rm -rf /var/lib/cloud/instances /var/lib/cloud/instance /var/lib/cloud/data/instance-id
  rm -f /var/lib/cloud/init-host.sh /var/log/openclaw-init.log /var/log/openclaw-bootstrap.log
  rm -f /root/.bash_history /home/ubuntu/.bash_history
  # Deliberately NOT scrubbing /var/lib/amazon/ssm. Image Builder runs this script THROUGH the
  # SSM agent, and that directory holds the live per-instance IPC channel of the very command
  # executing us: on the real build box (真机 2026-08-05, i-05d84fd…) the only entry matching
  # `i-*` is this instance's own directory, containing channels/<command-id> for the running
  # command; `registration` does not exist at all on EC2 (it is a hybrid-activation artifact).
  # So deleting it could never remove anything but our own transport — the agent then failed
  # `write file .../tmp/worker-…: no such file or directory`, hit `ipc messaging received
  # timedout signal!`, and the bake FAILED at ApplyBuildComponents *after* every component and
  # assertion had already succeeded. The SSM docs say the same thing: the installation
  # directory holds credentials and IPC resources and nothing in it may be modified, moved or
  # deleted. Image Builder owns this cleanup itself — its sanitize step shreds
  # /var/log/amazon/ssm and uninstalls the agent per the recipe's uninstallAfterBuild setting.
  # Fail loud rather than ship an image that carries the shared-key hazard.
  for _leak in /etc/openclaw/host_vm_key /etc/platform.env; do
    [ ! -e "${_leak}" ] || die "scrub left ${_leak} in the image; refusing to bake"
  done
  log "scrub verified: no host key, no platform.env"
fi

# ── 8. Marker ──────────────────────────────────────────────────────────────────────────
# Records WHICH provision ran, so a host can report its provenance (host-agent surfaces it
# one. Written last: a partial provision must not look complete.
install -d -m 0755 /etc/openclaw
cat > "${MARKER}" <<MARKEREOF
recipe_version=${PROVISION_RECIPE_VERSION}
provisioned_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
provisioned_arch=${ARCH}
firecracker_version=${FC_VER}
guest_kernel=${VMLINUX_NAME}
baked_dir=${BAKED_DIR}
MARKEREOF
chmod 0644 "${MARKER}"

log "provision done: recipe=${PROVISION_RECIPE_VERSION} marker=${MARKER}"
