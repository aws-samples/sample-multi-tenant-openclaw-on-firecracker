#!/bin/bash
# 构建 OpenClaw rootfs + data template 镜像并上传到 S3
# 用法: ./build-rootfs.sh [version]
# 示例: ./build-rootfs.sh v1.6
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE"
else
  echo "❌ 未找到 .env.deploy，请先运行 ./setup.sh"
  exit 1
fi

OC_ENV_FILE="$SCRIPT_DIR/.env.openclaw"
if [ -f "$OC_ENV_FILE" ]; then
  source "$OC_ENV_FILE"
else
  echo "❌ 未找到 .env.openclaw，请创建并填写 OpenClaw 应用配置"
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

ROOTFS_SIZE_MB=$(grep 'rootfs_size_mb:' "${SCRIPT_DIR}/config.yml" | awk '{print $2}')
truncate -s ${ROOTFS_SIZE_MB}M ${ROOTFS_IMG}
mkfs.ext4 -q ${ROOTFS_IMG}
sudo mkdir -p ${ROOTFS_DIR}
sudo mount ${ROOTFS_IMG} ${ROOTFS_DIR}

sudo debootstrap --include=curl,ca-certificates,systemd,dbus,iproute2,iputils-ping,git,jq \
  noble ${ROOTFS_DIR} ${MIRROR}

sudo mount --bind /proc ${ROOTFS_DIR}/proc
sudo mount --bind /sys ${ROOTFS_DIR}/sys
sudo mount --bind /dev ${ROOTFS_DIR}/dev

# 将 openclaw 变量注入 chroot（heredoc 'CHROOT' 不展开变量）
sudo cp "$OC_ENV_FILE" ${ROOTFS_DIR}/tmp/.env.openclaw

sudo chroot ${ROOTFS_DIR} /bin/bash << 'CHROOT'
set -e
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq openssh-server sudo dbus-user-session
ssh-keygen -A
echo "PermitRootLogin yes" >> /etc/ssh/sshd_config

curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y -qq nodejs

# GitHub CLI
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update -qq && apt-get install -y -qq gh

# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

systemctl enable systemd-networkd systemd-resolved

mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/dns.conf << 'DNSCONF'
[Resolve]
DNS=8.8.8.8 8.8.4.4
FallbackDNS=1.1.1.1
DNSCONF
echo "openclaw-vm" > /etc/hostname
echo "127.0.0.1 localhost openclaw-vm" > /etc/hosts
echo "root:OpenCl@w2026" | chpasswd

# Create agent user for openclaw
useradd -m -s /bin/bash agent
echo "agent:OpenCl@w2026" | chpasswd
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
ExecStart=/bin/bash -c "mount /dev/vdb /home/agent && chown agent:agent /home/agent"
ExecStartPost=/bin/bash -c "test -d /home/agent/.config && echo 'data mounted' || echo 'WARNING: mount failed'"
[Install]
WantedBy=multi-user.target
OCSVC
systemctl enable openclaw-data.service

echo "node=$(node --version) npm=$(npm --version)"

# --- OpenClaw CLI ---
npm install -g openclaw
chown -R agent:agent /usr/lib/node_modules

# Configure gateway via onboard (platform-level defaults, from .env.openclaw)
source /tmp/.env.openclaw
OC_BASE_URL="${OPENCLAW_BASE_URL:?OPENCLAW_BASE_URL required}"
OC_MODEL_ID="${OPENCLAW_MODEL_ID:?OPENCLAW_MODEL_ID required}"
OC_API_KEY="${OPENCLAW_API_KEY:?OPENCLAW_API_KEY required}"
OC_TOOLS_PROFILE="${OPENCLAW_TOOLS_PROFILE:-coding}"
OC_DM_SCOPE="${OPENCLAW_DM_SCOPE:-per-peer}"

HOME=/home/agent su -s /bin/bash agent -c "openclaw onboard --non-interactive \
  --accept-risk \
  --mode local \
  --auth-choice custom-api-key \
  --custom-base-url '${OC_BASE_URL}' \
  --custom-model-id '${OC_MODEL_ID}' \
  --custom-api-key '${OC_API_KEY}' \
  --gateway-bind lan \
  --gateway-auth token \
  --skip-health"

HOME=/home/agent su -s /bin/bash agent -c "openclaw config set 'tools.profile' '${OC_TOOLS_PROFILE}'"
HOME=/home/agent su -s /bin/bash agent -c "openclaw config set 'session.dmScope' '${OC_DM_SCOPE}'"
rm -f /tmp/.env.openclaw

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
chown -R agent:agent /home/agent

# --- Cleanup ---
apt-get clean
rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/* /root/.npm /tmp/*
rm -rf /opt/openclaw-mission-control/.next/cache /opt/openclaw-mission-control/node_modules/.cache

echo "openclaw=$(openclaw --version 2>&1 || echo 'installed')"
CHROOT

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
aws s3 cp ${ROOTFS_IMG}.gz s3://${BUCKET}/rootfs/${ROOTFS_KEY} ${PROFILE_FLAG}
aws s3 cp ${DATA_IMG}.gz s3://${BUCKET}/rootfs/${DATA_KEY} ${PROFILE_FLAG}

# Upload manifest (version pointer)
cat <<EOF | aws s3 cp - s3://${BUCKET}/rootfs/manifest.json ${PROFILE_FLAG} --content-type application/json
{"version":"${VERSION}","rootfs":"${ROOTFS_KEY}","data_template":"${DATA_KEY}"}
EOF

ROOTFS_SIZE=$(ls -lh ${ROOTFS_IMG}.gz | awk '{print $5}')
DATA_SIZE=$(ls -lh ${DATA_IMG}.gz | awk '{print $5}')
rm -f ${ROOTFS_IMG}.gz ${DATA_IMG}.gz

echo ""
echo "✓ rootfs ${VERSION} uploaded (${ROOTFS_SIZE})"
echo "  s3://${BUCKET}/rootfs/${ROOTFS_KEY}"
echo "✓ data template ${VERSION} uploaded (${DATA_SIZE})"
echo "  s3://${BUCKET}/rootfs/${DATA_KEY}"
echo "✓ manifest.json → ${VERSION}"

# Refresh on active hosts
if [ -n "${API_URL:-}" ] && [ -n "${API_KEY:-}" ]; then
  echo ""
  echo "→ Refreshing assets on active hosts..."
  curl -s -X POST "${API_URL}hosts/refresh-rootfs" -H "x-api-key: ${API_KEY}" | python3 -m json.tool
fi
