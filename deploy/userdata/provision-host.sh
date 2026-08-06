# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# shellcheck shell=bash
# No shebang, matching init-host.sh: both are fetched and run as `bash <file>` by the
# bootstrap / Image Builder component, never exec'd directly.
#
# provision-host.sh — the PROVISION stage of host bring-up (#389 v2 block 3).
#
# Host bring-up is split in two stages, and the split line is exactly one question:
# does this step reach the internet or install a package?
#
#   provision (this file)  installs components. No per-host and no per-deployment input,
#                          so it can be baked into a golden AMI once and reused by every
#                          host. Every external download in the whole boot path lives here.
#   configure (init-host)  renders /etc/platform.env, mounts the data volume, generates this
#                          host's key, registers to DynamoDB. Needs per-host identity and
#                          per-deployment secrets, so it can NEVER be baked.
#
# The boundary is NOT the old step numbering. It is drawn where it is because the reason for
# the golden AMI is that network installs are unreliable: a wrong-arch awscli zip, a missing
# aarch64 vmlinux suffix and a Firecracker tarball 404 have each already cost a host its
# lifecycle hook and put the ASG into an ABANDON-and-replace loop. Baking every download
# means a golden host boots with zero external fetches, so none of those can happen again.
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
# It must NEVER write a per-host secret or a per-deployment value. Anything written here ends
# up in an AMI shared by the entire fleet, so a per-host key written here would become one
# key shared by all hosts — and that key's public half is injected into every tenant microVM.

set -euo pipefail

PROVISION_RECIPE_VERSION="${OC_PROVISION_RECIPE_VERSION:-unversioned}"
MARKER=/etc/openclaw/.ami-provisioned
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
# it: the whole point of provisioning is that the boot path installs nothing.
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
_fc_installed=""
if [ -x /usr/local/bin/firecracker ]; then
  _fc_installed="$(/usr/local/bin/firecracker --version 2>/dev/null | head -1 || true)"
fi
case "${_fc_installed}" in
  *"${FC_VER}"*) log "firecracker ${FC_VER} already installed" ;;
  *)
    _fc_dir="$(mktemp -d)"
    _fetch "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VER}/firecracker-${FC_VER}-${ARCH}.tgz" \
      "${_fc_dir}/fc.tgz"
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
  # it there. Say so, because it means this AMI does NOT have a zero-download boot path.
  log "WARN: fluent-bit installer not provided; boot path will install it (one network fetch)"
fi

# ── 7. Marker ──────────────────────────────────────────────────────────────────────────
# Records WHICH provision ran, so a host can report its provenance (host-agent surfaces it
# the same way #387 reports build_info) and so configure can tell a golden boot from a plain
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

# ── 8. Bake-only scrub ─────────────────────────────────────────────────────────────────
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

log "provision done: recipe=${PROVISION_RECIPE_VERSION} marker=${MARKER}"
