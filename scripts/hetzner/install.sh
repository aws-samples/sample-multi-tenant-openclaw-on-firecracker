#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -euo pipefail

BUILD_ROOTFS=0
ROOTFS_VERSION="v1.0"
API_PORT="${SCF_API_PORT:-8080}"
APP_PORT="${SCF_APP_PORT:-3456}"
HOST_PORT_BASE="${SCF_HOST_PORT_BASE:-18789}"
SUBNET_PREFIX="${SCF_SUBNET_PREFIX:-172.16}"
KERNEL_PATH=""
KERNEL_URL=""
SKIP_KERNEL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --build-rootfs)
      BUILD_ROOTFS=1
      shift
      ;;
    --version)
      ROOTFS_VERSION="${2:?--version requires a value}"
      shift 2
      ;;
    --api-port)
      API_PORT="${2:?--api-port requires a value}"
      shift 2
      ;;
    --app-port)
      APP_PORT="${2:?--app-port requires a value}"
      shift 2
      ;;
    --host-port-base)
      HOST_PORT_BASE="${2:?--host-port-base requires a value}"
      shift 2
      ;;
    --subnet-prefix)
      SUBNET_PREFIX="${2:?--subnet-prefix requires a value}"
      shift 2
      ;;
    --kernel-path)
      KERNEL_PATH="${2:?--kernel-path requires a value}"
      shift 2
      ;;
    --kernel-url)
      KERNEL_URL="${2:?--kernel-url requires a value}"
      shift 2
      ;;
    --skip-kernel)
      SKIP_KERNEL=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/hetzner/install.sh [options]

Options:
  --build-rootfs           Build and install the SwarmClaw Firecracker rootfs locally.
  --version <tag>          Rootfs version tag for --build-rootfs (default: v1.0).
  --api-port <port>        Local control-plane API port (default: 8080).
  --app-port <port>        In-guest SwarmClaw port (default: 3456).
  --host-port-base <port>  First host-side tenant port (default: 18789).
  --subnet-prefix <cidr>   First two guest subnet octets (default: 172.16).
  --kernel-path <path>     Copy a local Firecracker-compatible vmlinux.
  --kernel-url <url>       Download a Firecracker-compatible vmlinux.
  --skip-kernel            Do not install a kernel now.
EOF
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0 $*" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_DIR="/opt/swarmclaw-firecracker"
SCRIPTS_DIR="${INSTALL_DIR}/scripts"
BASE_DIR="/data/swarmclaw-firecracker"
ASSETS_DIR="/data/firecracker-assets"
VM_DIR="/data/firecracker-vms"
TEMPLATES_DIR="${BASE_DIR}/templates"
CONFIG_DIR="/etc/swarmclaw-firecracker"
API_KEY_FILE="${CONFIG_DIR}/api-key"
ENV_FILE="${CONFIG_DIR}/env"

log() { echo "[scf:install] $(date +%H:%M:%S) $*"; }

if [ ! -e /dev/kvm ]; then
  cat >&2 <<'EOF'
ERROR: /dev/kvm is missing.

Firecracker requires KVM. Hetzner Cloud VPS instances do not expose nested
virtualization; use a Hetzner dedicated/root/bare-metal server or another host
that exposes /dev/kvm.
EOF
  exit 1
fi

log "installing Ubuntu packages"
apt-get -o DPkg::Lock::Timeout=60 update -qq
apt-get -o DPkg::Lock::Timeout=60 install -y -qq \
  ca-certificates curl jq unzip pigz nginx iptables lsof \
  debootstrap e2fsprogs build-essential git nodejs npm python3

chmod 666 /dev/kvm
echo 'KERNEL=="kvm", MODE="0666"' >/etc/udev/rules.d/99-kvm.rules

if ! command -v firecracker >/dev/null 2>&1; then
  log "installing Firecracker"
  ARCH="$(uname -m)"
  FC_URL="https://github.com/firecracker-microvm/firecracker/releases"
  FC_VER="$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' "${FC_URL}/latest")")"
  tmpdir="$(mktemp -d)"
  curl -fsSL "${FC_URL}/download/${FC_VER}/firecracker-${FC_VER}-${ARCH}.tgz" | tar -xz -C "${tmpdir}"
  install -m 0755 "${tmpdir}/release-${FC_VER}-${ARCH}/firecracker-${FC_VER}-${ARCH}" /usr/local/bin/firecracker
  install -m 0755 "${tmpdir}/release-${FC_VER}-${ARCH}/jailer-${FC_VER}-${ARCH}" /usr/local/bin/jailer
  rm -rf "${tmpdir}"
fi

log "creating local directories"
install -d -m 0755 "${INSTALL_DIR}" "${SCRIPTS_DIR}" "${BASE_DIR}/state" "${BASE_DIR}/backups" \
  "${BASE_DIR}/rootfs" "${ASSETS_DIR}" "${VM_DIR}" "${TEMPLATES_DIR}" "${CONFIG_DIR}"

log "installing control plane and VM scripts"
install -m 0755 "${REPO_ROOT}/deploy/hetzner/local-api.py" "${INSTALL_DIR}/local-api.py"
for script in launch-vm.sh stop-vm.sh clone-data.sh resize-disk.sh; do
  install -m 0755 "${REPO_ROOT}/deploy/userdata/${script}" "${SCRIPTS_DIR}/${script}"
done
install -m 0755 "${REPO_ROOT}/deploy/userdata/overlay-init" "${SCRIPTS_DIR}/overlay-init"

if [ ! -f "${API_KEY_FILE}" ]; then
  openssl rand -hex 24 >"${API_KEY_FILE}"
  chmod 600 "${API_KEY_FILE}"
fi

cat >"${ENV_FILE}" <<EOF
SCF_BASE_DIR=${BASE_DIR}
SCF_STATE_FILE=${BASE_DIR}/state/tenants.json
SCF_BACKUPS_DIR=${BASE_DIR}/backups
SCF_VM_DIR=${VM_DIR}
SCF_SCRIPTS_DIR=${SCRIPTS_DIR}
SCF_API_KEY_FILE=${API_KEY_FILE}
SCF_API_HOST=0.0.0.0
SCF_API_PORT=${API_PORT}
SCF_APP_PORT=${APP_PORT}
SCF_HOST_PORT_BASE=${HOST_PORT_BASE}
SCF_SUBNET_PREFIX=${SUBNET_PREFIX}
EOF
chmod 600 "${ENV_FILE}"

cat >/etc/platform.env <<EOF
OC_REGION=local
ASSETS_BUCKET=
FIRECRACKER_ASSETS=${ASSETS_DIR}
LOCAL_TEMPLATES_DIR=${TEMPLATES_DIR}
SUBNET_PREFIX=${SUBNET_PREFIX}
ROOTFS_OVERLAY_MB=8192
VM_APP_PORT=${APP_PORT}
BALLOON_ENABLED=false
BALLOON_DEFLATE_ON_OOM=true
BALLOON_STATS_INTERVAL=5
BALLOON_FREE_PAGE_REPORTING=true
BALLOON_MAX_INFLATE_RATIO=0.4
BALLOON_MIN_GUEST_AVAILABLE_MB=512
EOF

log "configuring nginx tenant proxy"
install -d -m 0755 /etc/nginx/conf.d/tenants
cat >/etc/nginx/conf.d/swarmclaw-firecracker.conf <<'NGINX'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
server {
    listen 80 default_server;
    location /health { return 200 'ok'; add_header Content-Type text/plain; }
    include /etc/nginx/conf.d/tenants/*.conf;
}
NGINX
rm -f /etc/nginx/sites-enabled/default
systemctl enable nginx >/dev/null
systemctl restart nginx

log "installing systemd service"
cat >/etc/systemd/system/swarmclaw-firecracker-api.service <<EOF
[Unit]
Description=SwarmClaw Firecracker Hetzner API
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/local-api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable swarmclaw-firecracker-api.service >/dev/null

if [ -n "${KERNEL_PATH}" ]; then
  log "installing Firecracker kernel from ${KERNEL_PATH}"
  install -m 0644 "${KERNEL_PATH}" "${ASSETS_DIR}/vmlinux"
elif [ -n "${KERNEL_URL}" ]; then
  log "downloading Firecracker kernel from ${KERNEL_URL}"
  curl -fsSL -o "${ASSETS_DIR}/vmlinux" "${KERNEL_URL}"
elif [ "${SKIP_KERNEL}" = "1" ] || [ -f "${ASSETS_DIR}/vmlinux" ]; then
  log "kernel install skipped; using existing ${ASSETS_DIR}/vmlinux if present"
else
  cat >&2 <<EOF
ERROR: no Firecracker kernel installed.

Provide a Firecracker-compatible uncompressed Linux kernel with one of:
  sudo $0 --kernel-path /path/to/vmlinux
  sudo $0 --kernel-url https://example.com/vmlinux

Or place it at:
  ${ASSETS_DIR}/vmlinux
EOF
  exit 1
fi

if [ "${BUILD_ROOTFS}" = "1" ]; then
  log "building local SwarmClaw rootfs ${ROOTFS_VERSION}"
  HETZNER_LOCAL=1 \
  LOCAL_OUTPUT_DIR="${BASE_DIR}/rootfs" \
  LOCAL_INSTALL_DIR="${ASSETS_DIR}" \
  "${REPO_ROOT}/build-rootfs.sh" "${ROOTFS_VERSION}"
else
  log "skipping rootfs build; run with --build-rootfs when ready"
fi

systemctl restart swarmclaw-firecracker-api.service

cat <<EOF

SwarmClaw Firecracker Hetzner install complete.

API URL:     http://<host>:${API_PORT}
API key:     ${API_KEY_FILE}
Tenant URLs: http://<host>/vm/<tenant-id>/

Create a tenant:
  curl -s -X POST http://127.0.0.1:${API_PORT}/tenants \\
    -H "x-api-key: $(cat "${API_KEY_FILE}")" \\
    -H "content-type: application/json" \\
    -d '{"name":"demo","vcpu":2,"mem_mb":4096}' | jq .
EOF
