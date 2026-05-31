#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Build the OpenClaw rootfs + data template and upload to S3.
# Usage: ./build-rootfs.sh [version]
#        ./build-rootfs.sh v1.6
#
# Linux-only: relies on debootstrap (Linux package), KVM-friendly chroot,
# pigz, e2fsprogs. macOS users hit the OS guard below and are pointed at
# scripts/build-rootfs-on-ec2.sh, which spins up a one-shot Linux builder
# and runs the same script remotely.
set -euo pipefail

# Show line number + exit code on any failure so users know exactly where things broke
trap 'rc=$?; echo "❌ build-rootfs.sh failed at line $LINENO (exit $rc)" >&2; echo "💡 To capture full log next run: ./build-rootfs.sh ${1:-v1.0} 2>&1 | tee build.log" >&2' ERR

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# OS guard. The chroot+debootstrap path is Linux-only — no point in waiting
# for the "command not found" deep into the script when we know up front
# the host can't run this. Override with FORCE_LOCAL_BUILD=1 if you really
# want the script to keep going (e.g. you're on Linux but `uname -s` is
# unusual, or you've shimmed debootstrap somehow).
case "$(uname -s)" in
  Linux) ;;
  Darwin)
    if [ "${FORCE_LOCAL_BUILD:-0}" != "1" ]; then
      echo "❌ build-rootfs.sh requires Linux (debootstrap is Linux-only)."
      echo
      echo "  You're on macOS. Use the cloud builder instead — it spins up a"
      echo "  one-shot t3.medium / t4g.medium Ubuntu host in your AWS account,"
      echo "  runs this same build via SSM, uploads to S3, and terminates the"
      echo "  builder. ~10 minutes, no local Linux required:"
      echo
      echo "      ./scripts/build-rootfs-on-ec2.sh ${1:-v1.0}"
      echo
      echo "  If you have a real Linux build host nearby, ssh into it and run"
      echo "  this script there instead."
      echo
      echo "  Override (not recommended): FORCE_LOCAL_BUILD=1 ./build-rootfs.sh"
      exit 1
    fi
    echo "⚠ FORCE_LOCAL_BUILD=1 set on Darwin — proceeding anyway, but expect"
    echo "  debootstrap / mount / mkfs.ext4 etc. to fail."
    ;;
  *)
    echo "⚠ Untested host OS: $(uname -s). Continuing (set FORCE_LOCAL_BUILD=1 to silence)."
    ;;
esac

ENV_FILE="$SCRIPT_DIR/.env.deploy"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE"
else
  echo "❌ .env.deploy not found — run ./setup.sh first."
  exit 1
fi

OC_TEMPLATE="$SCRIPT_DIR/templates/openclaw.json"
if [ ! -f "$OC_TEMPLATE" ]; then
  echo "❌ 未找到 templates/openclaw.json，请从 openclaw.json.example 复制并配置"
  exit 1
fi

VERSION="${1:-v1.0}"
BUCKET="${ASSETS_BUCKET}"
ROOTFS_IMG="/tmp/openclaw-rootfs-${VERSION}.ext4"
DATA_IMG="/tmp/openclaw-data-template-${VERSION}.ext4"
ROOTFS_DIR="/tmp/openclaw-rootfs-build"

# 依赖检查
MISSING=()
for cmd in debootstrap aws mkfs.ext4 curl pigz e2fsck resize2fs; do
  command -v $cmd &>/dev/null || MISSING+=($cmd)
done
if [ ${#MISSING[@]} -gt 0 ]; then
  echo "❌ 缺少依赖: ${MISSING[*]}"
  echo "   sudo apt-get install -y debootstrap e2fsprogs awscli curl pigz"
  exit 1
fi

# 内存预检
MEM_AVAIL_MB=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
SWAP_TOTAL_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
TOTAL_MB=$((MEM_AVAIL_MB + SWAP_TOTAL_MB))
if [ "$TOTAL_MB" -lt 2048 ]; then
  echo "❌ 可用内存不足 (available=${MEM_AVAIL_MB}MB + swap=${SWAP_TOTAL_MB}MB = ${TOTAL_MB}MB, 建议 ≥2048MB)"
  echo "   npm install -g openclaw 容易因 OOM 被静默杀掉。"
  echo "   建议: 增加内存容量 或 增加 swap"
  exit 1
elif [ "$TOTAL_MB" -lt 3072 ]; then
  echo "⚠️  可用内存偏少 (${TOTAL_MB}MB)，构建可能较慢。建议 ≥4GB。"
fi

# /tmp 空间预检
TMP_AVAIL_MB=$(df -BM --output=avail /tmp 2>/dev/null | tail -1 | tr -d ' M')
if [ -n "${TMP_AVAIL_MB}" ] && [ "${TMP_AVAIL_MB}" -lt 10240 ]; then
  echo "❌ /tmp 空间不足 (${TMP_AVAIL_MB}MB, 需要 ≥10240MB / 10GB)"
  echo "   rootfs 镜像 + data template + 压缩临时文件 都写在 /tmp"
  exit 1
fi

# 根据 region 选择镜像源
case ${REGION} in
  ap-northeast-1) MIRROR="http://ap-northeast-1.ec2.archive.ubuntu.com/ubuntu" ;;
  ap-southeast-1) MIRROR="http://ap-southeast-1.ec2.archive.ubuntu.com/ubuntu" ;;
  eu-west-1)      MIRROR="http://eu-west-1.ec2.archive.ubuntu.com/ubuntu" ;;
  eu-central-1)   MIRROR="http://eu-central-1.ec2.archive.ubuntu.com/ubuntu" ;;
  *)              MIRROR="http://archive.ubuntu.com/ubuntu" ;;
esac

echo "=== Building rootfs + data template ${VERSION} ==="
echo "Mirror: ${MIRROR}"

# 清理
sudo umount -l ${ROOTFS_DIR}/proc ${ROOTFS_DIR}/sys ${ROOTFS_DIR}/dev 2>/dev/null || true
sudo umount -l ${ROOTFS_DIR} 2>/dev/null || true
rm -f ${ROOTFS_IMG} ${DATA_IMG}

# CPU arch selection (issue #19). Defaults to host arch; pass --arch arm64
# (or x86_64) to cross-build for Graviton vs Intel/AMD hosts.
ARCH="${ARCH:-$(dpkg --print-architecture 2>/dev/null || uname -m)}"
case "$ARCH" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
esac
for arg in "$@"; do
  case "$arg" in
    --arch=*) ARCH="${arg#--arch=}" ;;
    --arch) shift; ARCH="$1" ;;
  esac
done
echo "→ building for ${ARCH}"

# Build-time ext4 image size for base rootfs. Only needs to fit debootstrap + tools
# (~2-3GB), then shrunk via resize2fs if zerofree is used. Not a runtime limit.
ROOTFS_SIZE_MB="${ROOTFS_SIZE_MB:-6144}"
truncate -s ${ROOTFS_SIZE_MB}M ${ROOTFS_IMG}
mkfs.ext4 -q ${ROOTFS_IMG}
sudo mkdir -p ${ROOTFS_DIR}
sudo mount ${ROOTFS_IMG} ${ROOTFS_DIR}

sudo debootstrap --arch=${ARCH} --include=curl,ca-certificates,systemd,dbus,iproute2,iputils-ping,git,jq \
  noble ${ROOTFS_DIR} ${MIRROR}

sudo mount --bind /proc ${ROOTFS_DIR}/proc
sudo mount --bind /sys ${ROOTFS_DIR}/sys
sudo mount --bind /dev ${ROOTFS_DIR}/dev

# Copy openclaw config template into chroot
sudo cp "$OC_TEMPLATE" ${ROOTFS_DIR}/tmp/openclaw.json

sudo chroot ${ROOTFS_DIR} /bin/bash << 'CHROOT'
set -e
trap 'rc=$?; echo "❌ chroot script failed at line $LINENO (exit $rc)" >&2; if [ "$rc" = "137" ] || [ "$rc" = "9" ]; then echo "   killed by SIGKILL — almost certainly OOM. Increase RAM or add swap." >&2; fi' ERR

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive

echo "[1/8] apt-get update + base repos"
apt-get update -qq
apt-get install -y -qq software-properties-common 2>/dev/null || true
add-apt-repository -y universe 2>/dev/null || true
apt-get update -qq

echo "[2/8] system packages (openssh, build-essential, ...)"
apt-get install -y -qq openssh-server sudo dbus-user-session \
  wget htop tmux vim-tiny tree python3-venv build-essential
ssh-keygen -A
# 1.5.0 security: pubkey-only SSH. No password auth, no root login. The
# per-host public key is injected into each VM's data disk at launch
# (launch-vm.sh), so authorized_keys is NEVER baked into the shared image.
cat >> /etc/ssh/sshd_config << 'SSHD'
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
ChallengeResponseAuthentication no
SSHD

echo "[3/8] Node.js 22.x"
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y -qq nodejs

echo "[4/8] GitHub CLI"
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update -qq && apt-get install -y -qq gh

echo "[5/8] uv (Python package manager)"
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

echo "[6/8] systemd + user/agent + DNS"
systemctl enable systemd-networkd systemd-resolved

mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/dns.conf << 'DNSCONF'
[Resolve]
DNS=8.8.8.8 8.8.4.4
FallbackDNS=1.1.1.1
DNSCONF
echo "openclaw-vm" > /etc/hostname
echo "127.0.0.1 localhost openclaw-vm" > /etc/hosts
passwd -l root   # lock root password — pubkey-only, no console/password login

# Create agent user for openclaw
useradd -m -s /bin/bash agent
passwd -l agent  # lock agent password — SSH is pubkey-only (key injected at launch)
# pre-create the agent .ssh dir so launch-vm.sh can drop authorized_keys into
# the data template at runtime (700 / agent-owned). Empty here on purpose:
# each host injects its OWN public key, so every host gets a distinct key.
mkdir -p /home/agent/.ssh
chmod 700 /home/agent/.ssh
chown agent:agent /home/agent/.ssh
echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent

# npm global prefix for agent user (avoids writing to /usr/bin)
mkdir -p /home/agent/.npm-global/bin
echo "prefix=/home/agent/.npm-global" > /home/agent/.npmrc
echo 'export PATH="/home/agent/.npm-global/bin:$PATH"' >> /home/agent/.bashrc

# Enable systemd user session for agent
mkdir -p /var/lib/systemd/linger
touch /var/lib/systemd/linger/agent

mkdir -p /etc/systemd/system/serial-getty@ttyS0.service.d
cat > /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf << 'GETTY'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM
Type=idle
GETTY

# Mount /dev/vdb as /home/agent
cat > /etc/systemd/system/openclaw-data.service << 'OCSVC'
[Unit]
Description=Mount OpenClaw data volume to /home/agent
DefaultDependencies=no
Before=systemd-user-sessions.service
After=local-fs.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c "mount /dev/vdc /home/agent && chown agent:agent /home/agent"
ExecStartPost=/bin/bash -c "test -d /home/agent/.config && echo 'data mounted' || echo 'WARNING: mount failed'"
[Install]
WantedBy=multi-user.target
OCSVC
systemctl enable openclaw-data.service

echo "node=$(node --version) npm=$(npm --version)"

echo "[7/8] OpenClaw CLI (npm install -g openclaw — peak ~1GB RAM)"
npm install -g openclaw
chown -R agent:agent /usr/lib/node_modules

echo "[8/8] OpenClaw onboard (bootstrap files)"
# Config will be overwritten by template — onboard params are placeholders
HOME=/home/agent su -s /bin/bash agent -c "openclaw onboard --non-interactive \
  --accept-risk --mode local --auth-choice custom-api-key \
  --custom-base-url 'http://placeholder' --custom-model-id 'placeholder' \
  --custom-api-key 'placeholder' --gateway-bind lan --gateway-auth token --skip-health"
# Overwrite config with our template (onboard config replaced, bootstrap files kept)
cp /tmp/openclaw.json /home/agent/.openclaw/openclaw.json
chown agent:agent /home/agent/.openclaw/openclaw.json
rm -f /tmp/openclaw.json

# --- Gateway service file (built into /home/agent, will be in data template) ---
NODE_BIN=$(which node)
OC_DIST=$(npm root -g)/openclaw/dist/index.js

mkdir -p /home/agent/.config/systemd/user/default.target.wants
cat > /home/agent/.config/systemd/user/openclaw-gateway.service << GWSVC
[Unit]
Description=OpenClaw Gateway
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=${NODE_BIN} ${OC_DIST} gateway --port 18789
Restart=always
RestartSec=5
KillMode=process
Environment=HOME=/home/agent
Environment=TMPDIR=/tmp
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=OPENCLAW_GATEWAY_PORT=18789
Environment=OPENCLAW_SYSTEMD_UNIT=openclaw-gateway.service
Environment=OPENCLAW_SERVICE_MARKER=openclaw
Environment=OPENCLAW_SERVICE_KIND=gateway

[Install]
WantedBy=default.target
GWSVC
ln -sf ../openclaw-gateway.service /home/agent/.config/systemd/user/default.target.wants/openclaw-gateway.service
# --- Shared Skills directory ---
mkdir -p /home/agent/.openclaw/skills
# Skills will be synced from host's /data/shared-skills/ at VM launch time
# This directory is on the data disk, so it persists across rootfs resets

chown -R agent:agent /home/agent

# --- Cleanup ---
apt-get clean
rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/* /root/.npm /tmp/*
rm -rf /opt/openclaw-mission-control/.next/cache /opt/openclaw-mission-control/node_modules/.cache

echo "openclaw=$(openclaw --version 2>&1 || echo 'installed')"

# OverlayFS init script (enables shared read-only rootfs across VMs)
mkdir -p /overlay /mnt
rm -rf /tmp/*
CHROOT

# Install overlay-init from deploy/userdata
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
sudo cp "${SCRIPT_DIR}/deploy/userdata/overlay-init" "${ROOTFS_DIR}/sbin/overlay-init"
sudo chmod +x "${ROOTFS_DIR}/sbin/overlay-init"

# Unmount chroot binds first
sudo umount -l ${ROOTFS_DIR}/proc ${ROOTFS_DIR}/sys ${ROOTFS_DIR}/dev

# === Build data template from /home/agent ===
DATA_DISK_MB=$(grep 'data_disk_mb:' "${SCRIPT_DIR}/config.yml" | awk '{print $2}')
echo "=== Building data template (${DATA_DISK_MB}MB) ==="
DATA_DIR="/tmp/openclaw-data-build"
truncate -s ${DATA_DISK_MB}M ${DATA_IMG}
mkfs.ext4 -q ${DATA_IMG}
sudo mkdir -p ${DATA_DIR}
sudo mount ${DATA_IMG} ${DATA_DIR}
sudo cp -a ${ROOTFS_DIR}/home/agent/. ${DATA_DIR}/
sudo chown -R 1000:1000 ${DATA_DIR}
sudo umount ${DATA_DIR}
sudo rmdir ${DATA_DIR}

# Clear /home/agent in rootfs (now just a mount point)
sudo rm -rf ${ROOTFS_DIR}/home/agent/*
sudo rm -rf ${ROOTFS_DIR}/home/agent/.[!.]*

sudo umount ${ROOTFS_DIR}

echo "=== Compressing images ==="
pigz -f ${ROOTFS_IMG}
pigz -f ${DATA_IMG}

echo "=== Uploading to S3 ==="
PROFILE_FLAG="${PROFILE:+--profile ${PROFILE}}"
ROOTFS_KEY="openclaw-rootfs-${VERSION}.ext4.gz"
DATA_KEY="openclaw-data-template-${VERSION}.ext4.gz"
aws s3 cp ${ROOTFS_IMG}.gz s3://${BUCKET}/deployment/rootfs/${ROOTFS_KEY} ${PROFILE_FLAG}
aws s3 cp ${DATA_IMG}.gz s3://${BUCKET}/deployment/rootfs/${DATA_KEY} ${PROFILE_FLAG}

# Upload manifest (version pointer)
cat <<EOF | aws s3 cp - s3://${BUCKET}/deployment/rootfs/manifest.json ${PROFILE_FLAG} --content-type application/json
{"version":"${VERSION}","rootfs":"${ROOTFS_KEY}","data_template":"${DATA_KEY}"}
EOF

ROOTFS_SIZE=$(ls -lh ${ROOTFS_IMG}.gz | awk '{print $5}')
DATA_SIZE=$(ls -lh ${DATA_IMG}.gz | awk '{print $5}')
rm -f ${ROOTFS_IMG}.gz ${DATA_IMG}.gz

echo ""
echo "✓ rootfs ${VERSION} uploaded (${ROOTFS_SIZE})"
echo "  s3://${BUCKET}/deployment/rootfs/${ROOTFS_KEY}"
echo "✓ data template ${VERSION} uploaded (${DATA_SIZE})"
echo "  s3://${BUCKET}/deployment/rootfs/${DATA_KEY}"
echo "✓ manifest.json → ${VERSION}"

# Refresh on active hosts
if [ -n "${API_URL:-}" ] && [ -n "${API_KEY:-}" ]; then
  echo ""
  echo "→ Refreshing assets on active hosts..."
  curl -s -X POST "${API_URL}hosts/refresh-rootfs" -H "x-api-key: ${API_KEY}" | python3 -m json.tool
fi
