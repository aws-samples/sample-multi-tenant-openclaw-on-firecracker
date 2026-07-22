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

# Show line number + exit code on any failure so users know exactly where things broke.
# 顺手 lazy-umount ROOTFS_DIR(评审 LOW #4):失败路径下如果 chroot 挂载/主 rootfs 还挂着,
# 用户手动清理容易忘。lazy(-l)在正常路径已 umount 时也无副作用(路径不存在直接返 0)。
trap 'rc=$?; echo "❌ build-rootfs.sh failed at line $LINENO (exit $rc)" >&2; echo "💡 To capture full log next run: ./build-rootfs.sh ${1:-v1.0} 2>&1 | tee build.log" >&2; [ -n "${ROOTFS_DIR:-}" ] && sudo umount -l "${ROOTFS_DIR}" 2>/dev/null || true' ERR

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# #35 image hygiene lib — strip/assert 逻辑抽出成可独立执行 + 可 subprocess-test
# 的 shell 库,让 immutable/data 两条路径共用同一份 find 谓词,消除漂移。
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/scripts/lib/image-hygiene.sh"

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

# #197 build 机器门(升级自旧防呆注释):校验烤入的 openclaw.json 及仓内模板载体
# key 集 ⊆ pin 版本 gateway schema,超版本 key(6.x-only,2.26 .strict() fail-closed
# 拒起 → gateway 崩溃重启仍报 running)直接拒烤。gate 从 build-rootfs.sh 的
# OPENCLAW_PIN 读版本、按 FORBIDDEN_BY_PIN 判。升级版本时更新该表(gate 内有注释)。
if command -v python3 >/dev/null 2>&1; then
  python3 "$SCRIPT_DIR/scripts/checks/template-schema-gate.py" "$OC_TEMPLATE" || {
    echo "❌ template-schema-gate 拒烤:模板含超版本 key(见上),先清或改 OPENCLAW_PIN。"
    exit 1
  }
else
  echo "⚠ 无 python3,跳过 template-schema-gate（CI 侧会兜底全量扫）"
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
_want_arch=""
while [ $# -gt 0 ]; do
  case "$1" in
    --arch=*) _want_arch="${1#--arch=}" ;;
    --arch)   shift; _want_arch="${1:-}" ;;
  esac
  shift
done
[ -n "$_want_arch" ] && ARCH="$_want_arch"
case "$ARCH" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
esac

# arm64 (Graviton) packages are NOT on the *.ec2.archive.ubuntu.com / archive.ubuntu.com
# mirrors (those carry amd64/i386 only). They live on ports.ubuntu.com. Override the
# region mirror selected above when cross/native-building for arm64.
if [ "$ARCH" = "arm64" ]; then
  MIRROR="http://ports.ubuntu.com/ubuntu-ports"
  echo "→ arm64 build: using ports mirror ${MIRROR}"
fi
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

# --- Golden image content: stage the chosen sample into the chroot ---
# ClawPool ships sample agent images under samples/<name>/. Pick one with the
# SAMPLE env var (default: finance-agent). Each sample is a self-contained,
# replaceable template — copy one to samples/<your-brand>/ and point SAMPLE at
# it to bake your own branded OpenClaw tenant. The pool itself is sample-agnostic.
# Copied to a fixed /tmp/image-sample staging path (outside chroot) so the quoted
# 'CHROOT' heredoc can read it regardless of which sample was selected.
SAMPLE="${SAMPLE:-finance-agent}"
SAMPLE_DIR="$SCRIPT_DIR/samples/$SAMPLE"
if [ -d "$SAMPLE_DIR" ]; then
  echo "  → staging golden image content from sample '$SAMPLE' (persona + skills + plugins)"
  sudo rm -rf ${ROOTFS_DIR}/tmp/image-sample
  sudo cp -a "$SAMPLE_DIR" ${ROOTFS_DIR}/tmp/image-sample
else
  echo "  ⚠ samples/$SAMPLE/ not found — building plain rootfs without golden image"
fi

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

echo "[2/8] system packages (openssh, build-essential, auditd, ...)"
# auditd: in-guest runtime monitor (our analogue of the tested product's
# Wazuh HIDS that runs in a ns the agent can't see). Baked here so v4 ships the
# kernel-whodata reverse-shell + sensitive-file watches enabled at boot.
apt-get install -y -qq openssh-server sudo dbus-user-session \
  wget htop tmux vim-tiny tree python3-venv build-essential auditd audispd-plugins
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

# Mount /dev/vdc as /home/agent  (writable per-tenant data disk)
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

# Mount /dev/vdd READ-ONLY and bind it over the identity files + ops skills.
#
# Security cornerstone: /dev/vdd is the immutable authority disk, attached by
# launch-vm.sh with Firecracker is_read_only:true. We mount it `-o ro` and then
# bind-mount each authoritative file/dir (also `-o ro`) over the writable data
# disk's copy. Result: ~/.openclaw/workspace/{SOUL,AGENTS,IDENTITY,HEARTBEAT,...}
# and ~/.openclaw/skills/<ops skills> are backed by a device the guest CANNOT
# write — `echo x >> SOUL.md` as root returns EROFS (Read-only file system),
# because the virtio device rejects the write before it ever reaches a backing
# file. Strictly stronger than chmod/chattr (root can undo those).
#
# Runs AFTER openclaw-data.service (the bind targets live under /home/agent,
# which data.service mounts) and BEFORE the agent's user session starts.
cat > /etc/systemd/system/openclaw-immutable.service << 'OCIMM'
[Unit]
Description=Mount immutable identity+ops-skills disk (read-only) and bind over data
DefaultDependencies=no
Before=systemd-user-sessions.service
After=openclaw-data.service
Requires=openclaw-data.service
ConditionPathExists=/dev/vdd
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/openclaw-mount-immutable.sh
[Install]
WantedBy=multi-user.target
OCIMM

# The bind-mount worker. Iterates the read-only disk's tree and shadows the
# matching path under /home/agent/.openclaw with a read-only bind mount.
cat > /usr/local/sbin/openclaw-mount-immutable.sh << 'IMMSH'
#!/bin/bash
set -e
RO_MNT=/mnt/immutable
WS_DST=/home/agent/.openclaw/workspace
SK_DST=/home/agent/.openclaw/skills
mkdir -p "${RO_MNT}"
# 1) Mount the immutable disk read-only.
mount -o ro /dev/vdd "${RO_MNT}"
# 2) Bind each workspace identity file over the data-disk copy (read-only).
mkdir -p "${WS_DST}"
if [ -d "${RO_MNT}/workspace" ]; then
  for f in "${RO_MNT}/workspace"/*; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    # Ensure a target node exists to bind onto (data disk usually already has it).
    [ -e "${WS_DST}/${name}" ] || install -o agent -g agent -m 0644 /dev/null "${WS_DST}/${name}"
    mount --bind "$f" "${WS_DST}/${name}"
    mount -o remount,ro,bind "${WS_DST}/${name}"
  done
fi
# 3) Bind each ops/safety skill dir over the data-disk copy (read-only).
mkdir -p "${SK_DST}"
if [ -d "${RO_MNT}/skills" ]; then
  for d in "${RO_MNT}/skills"/*; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    [ -d "${SK_DST}/${name}" ] || install -d -o agent -g agent "${SK_DST}/${name}"
    mount --bind "$d" "${SK_DST}/${name}"
    mount -o remount,ro,bind "${SK_DST}/${name}"
  done
fi
echo "openclaw-immutable: bound $(ls ${RO_MNT}/workspace 2>/dev/null | wc -l) identity files + $(ls ${RO_MNT}/skills 2>/dev/null | wc -l) skills read-only from /dev/vdd"
IMMSH
chmod +x /usr/local/sbin/openclaw-mount-immutable.sh
systemctl enable openclaw-immutable.service

# Mount the per-VM READ-ONLY credentials disk (#118/#116) and bind its .env over
# ~/.openclaw/.env. launch-vm.sh attaches a per-tenant ext4 (holding a .env of
# platform-injected creds the HOST decrypted from KMS ciphertext) READ-ONLY.
# OpenClaw's native dotenv loader reads ~/.openclaw/.env at startup
# (src/infra/dotenv.ts → process.env → agent exec subprocess inherits it, verified
# against bash-tools.exec.ts) so plaintext creds never live in openclaw.json.
# Read-only bind = the guest (even root) cannot rewrite them (EROFS).
#
# Identification by MARKER-FILE presence, not by-label: this image ships no
# udev/blkid (see debootstrap --include), so /dev/disk/by-label never populates;
# and the device letter can shift when the immutable disk is absent. So we scan the
# raw virtio block devices for our marker file (.clawcreds-marker, written by
# launch-vm.sh) and mount whichever one HAS that file. The immutable skills disk
# (workspace/skills, no marker) is thus never mistaken for the creds disk. No marker
# on any device = this tenant has no injected creds → clean no-op (the common
# case). This runs unconditionally (no
# ConditionPathExists) so it can always scan; it exits 0 when no creds disk is found.
cat > /etc/systemd/system/openclaw-creds.service << 'OCCRED'
[Unit]
Description=Mount per-VM credentials disk (read-only, content-identified) and bind .env into ~/.openclaw
DefaultDependencies=no
Before=systemd-user-sessions.service
After=openclaw-data.service
Requires=openclaw-data.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/openclaw-mount-creds.sh
[Install]
WantedBy=multi-user.target
OCCRED

cat > /usr/local/sbin/openclaw-mount-creds.sh << 'CREDSH'
#!/bin/bash
# Fail-loud: if a creds disk IS found but can't be bound, exit non-zero so the
# unit goes `failed` and is visible — never silently boot without the creds a
# tenant was provisioned with. Finding no creds disk at all is a clean exit 0.
set -euo pipefail
RO_MNT=/mnt/creds
ENV_DST=/home/agent/.openclaw/.env
PROBE=/mnt/creds-probe
mkdir -p "${RO_MNT}" "${PROBE}"

# Candidate raw devices, in the order launch-vm.sh PUTs disks. rootfs(vda) and
# overlay(vdb)/data(vdc) are excluded; the creds disk is attached after immutable,
# so vde/vdd are the realistic slots. Probe read-only; the marker file tells us
# which (if any) is the creds disk vs the immutable skills disk. OC_CREDS_DEVS
# overrides the list (loop devices in the host-side mount test; unset in prod).
found_dev=""
for dev in ${OC_CREDS_DEVS:-/dev/vde /dev/vdd}; do
  [ -b "${dev}" ] || continue
  if mount -o ro "${dev}" "${PROBE}" 2>/dev/null; then
    if [ -f "${PROBE}/.clawcreds-marker" ]; then
      umount "${PROBE}"
      found_dev="${dev}"
      break
    fi
    umount "${PROBE}"
  fi
done
rmdir "${PROBE}" 2>/dev/null || true

if [ -z "${found_dev}" ]; then
  echo "openclaw-creds: no credentials disk attached (no marker on vde/vdd) — clean no-op"
  exit 0
fi

# Found the creds disk. From here any failure is fail-loud (set -e).
mount -o ro "${found_dev}" "${RO_MNT}"
if [ ! -f "${RO_MNT}/.env" ]; then
  echo "openclaw-creds: FATAL: creds disk ${found_dev} present but has no .env" >&2
  exit 1
fi
mkdir -p "$(dirname "${ENV_DST}")"
[ -e "${ENV_DST}" ] || install -o agent -g agent -m 0600 /dev/null "${ENV_DST}"
mount --bind "${RO_MNT}/.env" "${ENV_DST}"
mount -o remount,ro,bind "${ENV_DST}"
echo "openclaw-creds: bound $(grep -c '=' "${RO_MNT}/.env" 2>/dev/null || echo 0) credential(s) read-only into ~/.openclaw/.env from ${found_dev}"
CREDSH
chmod +x /usr/local/sbin/openclaw-mount-creds.sh
systemctl enable openclaw-creds.service

# === FIM: host-invisible file-integrity monitor (our Wazuh-style analogue) ===
# The real a production-grade hardened sandbox runs Wazuh syscheck (FIM) + logcollector +
# active-response in a separate namespace the agent CANNOT see or modify:
# `/var/ossec` simply does not exist in the agent's mount namespace, so even a
# fully compromised agent cannot tamper with the monitor that watches it.
#
# Our equivalent, scaled to a single-tenant microVM: a ROOT-owned systemd
# *system* service + timer that periodically re-hashes the live identity files
# and ops/safety skills and diffs them against the tamper-proof golden baseline
# on the read-only authority disk (/mnt/immutable/golden-image.sha256, on
# /dev/vdd — is_read_only:true at the virtio layer).
#
# The monitor is invisible & immutable to the monitored party (the `agent` user,
# uid 1000), exactly like Wazuh-in-its-own-ns:
#   * worker script  /usr/local/sbin/openclaw-fim.sh   → root:root 0700
#   * findings log    /var/log/openclaw-fim/            → root:root 0700
#   * baseline         /mnt/immutable/golden-image.sha256 → read-only device
# None of these live under /home/agent, and all are root-only, so the agent can
# neither read the findings nor edit the monitor. A tamper attempt on SOUL.md /
# a guardrail skill is detected out-of-band and logged where the agent can't
# reach. (Defence-in-depth on top of the read-only bind-mount, which already
# makes the writes themselves return EROFS.)
cat > /usr/local/sbin/openclaw-fim.sh << 'FIMSH'
#!/bin/bash
# openclaw-fim — root-only file-integrity monitor. Diffs live identity+skill
# files against the immutable golden baseline. Exit 0 = clean, 2 = drift.
set -uo pipefail
RO_MNT=/mnt/immutable
BASELINE="${RO_MNT}/golden-image.sha256"
WS=/home/agent/.openclaw/workspace
SK=/home/agent/.openclaw/skills
LOG_DIR=/var/log/openclaw-fim
LOG="${LOG_DIR}/fim.log"
mkdir -p "${LOG_DIR}"; chmod 700 "${LOG_DIR}"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) $*" >> "${LOG}"; }

if [ ! -f "${BASELINE}" ]; then
  log "WARN no baseline at ${BASELINE} (immutable disk absent?) — FIM cannot verify"
  exit 0
fi

drift=0
# Baseline lines look like: "<sha256>  workspace/SOUL.md" or
# "<sha256>  skills/ops-guardrails/SKILL.md" (paths relative to the disk root).
while read -r want rel; do
  [ -n "${rel:-}" ] || continue
  case "$rel" in
    workspace/*) live="${WS}/${rel#workspace/}" ;;
    skills/*)    live="${SK}/${rel#skills/}" ;;
    *)           continue ;;
  esac
  if [ ! -f "$live" ]; then
    log "DRIFT missing ${rel} (expected at ${live})"
    drift=1
    continue
  fi
  got=$(sha256sum "$live" | awk '{print $1}')
  if [ "$got" != "$want" ]; then
    log "DRIFT hash-mismatch ${rel} baseline=${want} live=${got}"
    drift=1
  fi
done < "${BASELINE}"

if [ "$drift" = "0" ]; then
  log "OK all golden files match baseline ($(grep -c '' "${BASELINE}") tracked)"
  exit 0
else
  log "TAMPER one or more identity/guardrail files drifted from golden baseline — P1"
  exit 2
fi
FIMSH
chmod 700 /usr/local/sbin/openclaw-fim.sh
chown root:root /usr/local/sbin/openclaw-fim.sh

cat > /etc/systemd/system/openclaw-fim.service << 'FIMSVC'
[Unit]
Description=OpenClaw file-integrity monitor (root-only, agent-invisible)
After=openclaw-immutable.service
Requires=openclaw-immutable.service
ConditionPathExists=/mnt/immutable/golden-image.sha256
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/openclaw-fim.sh
# The monitor MUST see the live mount namespace: openclaw-immutable.service
# bind-mounts the authoritative identity/skill files over /home/agent/.openclaw
# *after* boot. Namespace-isolating directives (ProtectHome / ProtectSystem)
# snapshot the mount table and hide those later bind-mounts, making FIM falsely
# report the files "missing". So we deliberately DON'T isolate the mount ns here.
# The monitor's invisibility to the agent comes from filesystem perms instead —
# the worker (0700 root:root) and the findings log (/var/log/openclaw-fim, 0700
# root:root) are unreadable to uid 1000 (verified: agent gets "Permission
# denied"). We still drop privileges that aren't needed to read+hash files:
NoNewPrivileges=true
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
AmbientCapabilities=
RestrictSUIDSGID=true
PrivateTmp=true
FIMSVC

cat > /etc/systemd/system/openclaw-fim.timer << 'FIMTMR'
[Unit]
Description=Run OpenClaw file-integrity monitor periodically
[Timer]
# First check shortly after boot, then every 5 minutes.
OnBootSec=90s
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true
[Install]
WantedBy=timers.target
FIMTMR
# Pre-create the root-only findings dir so perms are baked (agent uid 1000 can't read).
mkdir -p /var/log/openclaw-fim
chmod 700 /var/log/openclaw-fim
chown root:root /var/log/openclaw-fim
systemctl enable openclaw-fim.timer

# === v4 read-only hardening: belt-and-suspenders RO bind-mounts + /proc hidepid ===
# Layered ON TOP of the existing block-level read-only guarantees:
#   * rootfs /dev/vda — is_read_only:true at the virtio layer (overlayfs lower).
#   * immutable /dev/vdd — is_read_only:true; identity files + ops skills bound ro.
# This service closes the remaining gaps the block-level RO does not cover:
#   (a) the plugins dir lives on the WRITABLE data disk — a compromised agent
#       could otherwise rewrite its own guard plugin (acl-guard / sentinel-guard).
#       We re-bind it read-only over itself so the running guard code is frozen.
#   (b) overlayfs lets a writer SHADOW a lower-layer file in the writable upper.
#       Re-binding the real /etc credential files + the overlay-init script + the
#       openclaw binary read-only over themselves makes even an upper-layer
#       shadow write return EROFS.
#   (c) /proc hidepid=2 — the agent can no longer enumerate or inspect other
#       processes' /proc entries (anti-reconnaissance; the FIM + rtmon run as
#       root and stay visible to root only).
# Runs after the data + immutable mounts are in place, before the user session.
cat > /usr/local/sbin/openclaw-ro-harden.sh << 'ROHARDEN'
#!/bin/bash
# openclaw-ro-harden — freeze guard code + sensitive files read-only and hide
# other processes from the agent. Each step is best-effort + idempotent; a
# missing target is skipped (never fail the boot).
set -uo pipefail
LOG_TAG="openclaw-ro-harden"
note() { echo "${LOG_TAG}: $*"; }

ro_bind() {
  # Re-bind a path read-only over itself. Works for files and dirs.
  local p="$1"
  [ -e "$p" ] || { note "skip (absent) $p"; return 0; }
  if mount --bind "$p" "$p" 2>/dev/null && mount -o remount,ro,bind "$p" 2>/dev/null; then
    note "ro-bind OK $p"
  else
    note "ro-bind FAILED $p (continuing)"
  fi
}

# (a) Freeze the security plugin tree (on the writable data disk).
ro_bind /home/agent/.openclaw/plugins

# (b) Freeze sensitive system files + boot/runtime binaries (shadow-write guard).
ro_bind /etc/passwd
ro_bind /etc/shadow
ro_bind /etc/gshadow
ro_bind /etc/sudoers
ro_bind /etc/sudoers.d
ro_bind /etc/ssh/sshd_config
ro_bind /sbin/overlay-init
# The openclaw CLI/runtime — resolve real path (npm global symlink).
OC_BIN="$(command -v openclaw 2>/dev/null || true)"
[ -n "$OC_BIN" ] && ro_bind "$(readlink -f "$OC_BIN" 2>/dev/null || echo "$OC_BIN")"

# (c) hidepid=2 on /proc — agent (uid 1000) cannot see other processes.
if mount -o remount,rw,hidepid=2 /proc 2>/dev/null || mount -o remount,hidepid=2 /proc 2>/dev/null; then
  note "hidepid=2 OK on /proc"
else
  note "hidepid remount FAILED (kernel may not support; continuing)"
fi

note "done"
ROHARDEN
chmod 700 /usr/local/sbin/openclaw-ro-harden.sh
chown root:root /usr/local/sbin/openclaw-ro-harden.sh

cat > /etc/systemd/system/openclaw-ro-harden.service << 'ROHSVC'
[Unit]
Description=OpenClaw read-only hardening (freeze guards + sensitive files, hidepid)
DefaultDependencies=no
After=openclaw-immutable.service openclaw-data.service
Before=systemd-user-sessions.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/openclaw-ro-harden.sh
[Install]
WantedBy=multi-user.target
ROHSVC
systemctl enable openclaw-ro-harden.service

# === v4 in-guest runtime monitor (auditd): reverse-shell + sensitive-file FIM ===
# Our analogue of the tested product's Wazuh HIDS (which runs in a ns the agent
# cannot see). Root-owned, 0700, agent-invisible. Kernel whodata: attributes a
# sudo-escalated write back to the original login uid. Detection + alert only
# (no auto active-response). Baked + enabled here so v4 ships it at boot.
mkdir -p /etc/audit/rules.d
cat > /etc/audit/rules.d/claw-rtmon.rules << 'RTMONRULES'
## ClawPool runtime monitor — delete prior, big buffer, fail-loud
-D
-b 8192
-f 1
## ReverseShell class: execve carries the cmdline; analyzer matches signatures.
-a always,exit -F arch=b64 -S execve -F key=claw_exec
-a always,exit -F arch=b32 -S execve -F key=claw_exec
## ReverseShell (network): outbound connect by the agent uid (obfuscation-proof).
-a always,exit -F arch=b64 -S connect -F auid>=1000 -F auid!=4294967295 -F key=claw_netconn
## SensitiveFileModified class: write/attr-change on identity + credential paths.
-w /home/agent/.openclaw/workspace -p wa -k claw_fim_identity
-w /home/agent/.openclaw/skills    -p wa -k claw_fim_skills
-w /home/agent/.openclaw/plugins   -p wa -k claw_fim_plugins
-w /etc/passwd                     -p wa -k claw_fim_passwd
-w /etc/shadow                     -p wa -k claw_fim_shadow
RTMONRULES

install -d -m 700 -o root -g root /var/log/claw-rtmon
cat > /usr/local/sbin/claw-rtmon-analyzer.py << 'RTMONPY'
#!/usr/bin/env python3
# claw-rtmon-analyzer — agent-invisible (root:root 0700). Reads the auditd log
# and raises ClawPool runtime alerts. Two rule classes:
#   RULE 100210 ReverseShell          (P1 / level 12)
#   RULE 100110 SensitiveFileModified (P1 / level 10)
import json, os, re, sys, time, datetime

AUDIT_LOG = "/var/log/audit/audit.log"
ALERT_LOG = "/var/log/claw-rtmon/alerts.json"

REV_SHELL_SIGS = [
    (re.compile(r"/dev/tcp/"),              "bash /dev/tcp redirection"),
    (re.compile(r"bash\s+-i"),              "interactive bash (bash -i)"),
    (re.compile(r"\bnc\b.*\s-e\b"),         "netcat -e exec"),
    (re.compile(r"\bncat\b.*--exec"),       "ncat --exec"),
    (re.compile(r"socat\b.*exec"),          "socat exec"),
    (re.compile(r"python3?\b.*pty\.spawn"), "python pty.spawn shell"),
    (re.compile(r"python3?\b.*socket.*sh"), "python socket reverse shell"),
    (re.compile(r"sh\s+-i\b"),              "interactive sh (sh -i)"),
    (re.compile(r"/dev/udp/"),              "bash /dev/udp redirection"),
]

def now(): return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def decode_hex_args(execve_line):
    parts = dict(re.findall(r'\b(a\d+|key|auid|uid|pid|ppid|comm|exe)=("[^"]*"|\S+)', execve_line))
    args = []
    i = 0
    while True:
        k = "a%d" % i
        if k not in parts: break
        v = parts[k].strip('"')
        if re.fullmatch(r'[0-9A-Fa-f]+', v) and len(v) % 2 == 0 and len(v) >= 2:
            try: v = bytes.fromhex(v).decode('utf-8', 'replace')
            except Exception: pass
        args.append(v); i += 1
    return " ".join(args), parts

def emit(alert):
    with open(ALERT_LOG, "a") as f:
        f.write(json.dumps(alert) + "\n")
    sys.stdout.write(json.dumps(alert) + "\n"); sys.stdout.flush()

def main():
    while not os.path.exists(AUDIT_LOG):
        time.sleep(1)
    with open(AUDIT_LOG, "r", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.3); continue
            if "type=EXECVE" in line:
                cmd, parts = decode_hex_args(line)
                for rx, desc in REV_SHELL_SIGS:
                    if rx.search(cmd):
                        emit({"ts": now(), "product": "ClawPool Agent Runtime Monitor",
                              "rule_id": 100210, "rule": "ReverseShell", "level": 12,
                              "severity": "P1", "signature": desc, "command": cmd[:400],
                              "detector": "auditd execve (in-guest, agent-invisible)"})
                        break
            if "type=SYSCALL" in line and "key=" in line:
                m = re.search(r'key="?(claw_fim_[a-z]+)"?', line)
                if m:
                    g = lambda p: (re.search(p, line).group(1) if re.search(p, line) else "?")
                    emit({"ts": now(), "product": "ClawPool Agent Runtime Monitor",
                          "rule_id": 100110, "rule": "SensitiveFileModified", "level": 10,
                          "severity": "P1", "fim_key": m.group(1),
                          "who_auid": g(r'\bauid=(\S+)'), "who_uid": g(r'\buid=(\S+)'),
                          "via_comm": g(r'\bcomm="([^"]*)"'), "syscall": g(r'\bsyscall=(\S+)'),
                          "success": g(r'\bsuccess=(\S+)'),
                          "detector": "auditd FIM watch -p wa (in-guest, agent-invisible)"})
if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: pass
RTMONPY
chmod 700 /usr/local/sbin/claw-rtmon-analyzer.py
chown root:root /usr/local/sbin/claw-rtmon-analyzer.py

cat > /etc/systemd/system/claw-rtmon.service << 'RTMONSVC'
[Unit]
Description=ClawPool runtime monitor analyzer (root-only, agent-invisible)
After=auditd.service
Requires=auditd.service
[Service]
Type=simple
ExecStart=/usr/local/sbin/claw-rtmon-analyzer.py
Restart=always
RestartSec=2
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
RTMONSVC
systemctl enable auditd >/dev/null 2>&1 || true
systemctl enable claw-rtmon.service >/dev/null 2>&1 || true

echo "node=$(node --version) npm=$(npm --version)"

# OpenClaw CLI — pin 到确定版本(CalVer),不装 latest:latest 会随上游漂移,
# 同一份 build 脚本不同时间烤出不同 OpenClaw,launch-vm 的 openclaw.json schema
# 适配是针对特定版本。
# #188:钉客户线上版本 2026.2.26(协议 v3),对齐 wss 免 approve 冷注入基准。
# 2.26 schema 比 6.x 严(.strict()),几个 6.x 才加的 config 键必须不出现在
# 烤进镜像的 openclaw.json,否则 gateway startup 校验失败拒起(真机用 2.26
# dist 的 validateConfigObjectWithPlugins 实测):① 本文件下方 sentinel-guard
# 不带 hooks 键 ② openclaw.json 不含 heartbeat.isolatedSession/lightContext、
# compaction.midTurnPrecheck/maxActiveTranscriptBytes。**这条不再靠自觉**:
# 脚本顶部已跑 scripts/checks/template-schema-gate.py 机器门(#197),超版本 key
# 直接拒烤。升级 OpenClaw = 改这里 OPENCLAW_PIN + 更新 gate 的 FORBIDDEN_BY_PIN 表
# + 重跑目标版 schema 校验 + 验 launch-vm jq → 重烤。
OPENCLAW_PIN="2026.2.26"
echo "[7/8] OpenClaw CLI (npm install -g openclaw@${OPENCLAW_PIN} — peak ~1GB RAM)"
npm install -g "openclaw@${OPENCLAW_PIN}"
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

# --- Golden image: bake identity files + skills over the onboard baseline ---
if [ -d /tmp/image-sample ]; then
  echo "  → baking golden image into workspace + skills"
  mkdir -p /home/agent/.openclaw/workspace /home/agent/.openclaw/skills
  # 6 identity files overwrite the onboard-generated workspace docs
  if [ -d /tmp/image-sample/persona ]; then
    cp -a /tmp/image-sample/persona/. /home/agent/.openclaw/workspace/
  fi
  # 10 skills baked as the golden baseline (each skills/<name>/SKILL.md)
  if [ -d /tmp/image-sample/skills ]; then
    cp -a /tmp/image-sample/skills/. /home/agent/.openclaw/skills/
  fi
  # Security plugins: code-enforced before_tool_call enforcement (moves
  # ops-guardrails from prompt self-discipline to a hard runtime veto). Two
  # complementary, independently-vetoing plugins are baked + enabled so every
  # VM enforces them:
  #   acl-guard      — tight secret/IMDS exfil deny-list (priority 1000, first).
  #   sentinel-guard — broad mechanism set: traversal-safe path-prefix protection,
  #                    identity-file read/attachment/reply protection, reverse-shell
  #                    / destructive / priv-esc command rules, CIDR-aware SSRF,
  #                    three-layer secret redaction on llm_output + message_sending,
  #                    prompt-injection screening, skill-content scan, and a
  #                    sliding-window behaviour-anomaly monitor (priority 200).
  # Both fail closed (a guard error never crashes the gateway; the block stands).
  if [ -d /tmp/image-sample/security ]; then
    echo "  → baking + enabling plugins (acl-guard + sentinel-guard)"
    mkdir -p /home/agent/.openclaw/plugins
    cp -a /tmp/image-sample/security/. /home/agent/.openclaw/plugins/
    find /home/agent/.openclaw/plugins -name '._*' -delete 2>/dev/null || true
    # Defensive: never ship test-only artifacts (stub SDK / harness) even if a
    # future change drops them under a plugin dir.
    find /home/agent/.openclaw/plugins -name 'node_modules' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find /home/agent/.openclaw/plugins -name 'test-harness.*' -delete 2>/dev/null || true
    # Register the two guard plugins in config: load paths + enabled entries.
    # jq merge keeps whatever the template already had under .plugins.
    #   acl-guard / sentinel-guard — before_tool_call hooks (priority 1000 / 200)
    # claw-channel was retired in the data-plane refactor
    # (the data-plane design doc §A): the WSS-hub reverse channel
    # is replaced by two-tier routing (OpenResty edge → host DNAT → in-VM native
    # gateway on :18789). The gateway now serves chat via the OpenAI-compatible
    # /v1/chat/completions and /v1/responses HTTP endpoints directly; the plugin
    # is archived under an internal archive for history.
    if command -v jq >/dev/null 2>&1; then
      jq '
        (.plugins // {}) as $p |
        .plugins = ($p + {
          "load":    (((($p.load // {})) + { "paths": (((($p.load // {}).paths) // []) + ["/home/agent/.openclaw/plugins/acl-guard", "/home/agent/.openclaw/plugins/sentinel-guard"] | unique) })),
          "entries": (((($p.entries // {})) + { "acl-guard": { "enabled": true }, "sentinel-guard": { "enabled": true } }))
        })
      ' /home/agent/.openclaw/openclaw.json > /home/agent/.openclaw/openclaw.json.tmp \
        && mv /home/agent/.openclaw/openclaw.json.tmp /home/agent/.openclaw/openclaw.json
    fi
  fi
  # Strip macOS AppleDouble sidecars (._*) that may tag along when the repo
  # was staged from a Mac — harmless but untidy in the golden image.
  find /home/agent/.openclaw/workspace /home/agent/.openclaw/skills -name '._*' -delete 2>/dev/null || true
  chown -R agent:agent /home/agent/.openclaw/workspace /home/agent/.openclaw/skills /home/agent/.openclaw/plugins /home/agent/.openclaw/openclaw.json
  rm -rf /tmp/image-sample
else
  echo "  ⚠ /tmp/image-sample absent — no golden image baked"
fi

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

# ── HARDENING (privilege drop) — our adaptation of a production-grade hardened CapBnd=0 ──
# The real reference hardened sandbox runs the agent with CapBnd=0000…0000
# (root inside, but ZERO Linux capabilities) plus a seccomp filter. Our gateway
# already runs as the unprivileged `agent` user (uid 1000, a systemd *user*
# service — never root), so we layer defence-in-depth on top:
#   NoNewPrivileges        — no setuid/setgid escalation; even a compromised
#                            gateway cannot regain privileges via exec (mirrors
#                            CapBnd=0's "cannot re-acquire any cap").
#   CapabilityBoundingSet= — empty bounding set: no capability obtainable
#                            (the systemd analogue of the hardened-sandbox CapBnd=0).
#   RestrictSUIDSGID       — block creating SUID/SGID files.
#   ProtectKernelTunables / ProtectKernelModules / ProtectControlGroups —
#                            read-only /proc/sys, /sys; no module load; no cgroup
#                            edits from inside the gateway.
#   RestrictNamespaces / LockPersonality / MemoryDenyWriteExecute(off — Node JIT
#                            needs W^X relaxed) — narrow the kernel attack surface.
#   SystemCallFilter=@system-service — seccomp allowlist (the reference sandbox had 1 active
#                            BPF filter; this is our equivalent, scoped to the
#                            syscalls a Node service legitimately needs).
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictSUIDSGID=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
LockPersonality=true
RestrictRealtime=true
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM

# ── HARDENING (cgroup resource limits) — our adaptation of a hardened cgroup v2 ──
# the reference profile pins each sandbox to memory.max=4GB + cpu.max=2.5cores at the cgroup
# layer. Our microVM is already capped by Firecracker (≈2GB/1vCPU on the dense
# default), so this is a *sub-limit* INSIDE the guest: it bounds the gateway +
# all its tool-exec children so a runaway/forked workload OOMs its own slice
# instead of taking down the whole VM (PID 1, sshd, the FIM monitor). Values are
# scaled to the 4GB/2vCPU default node and leave headroom for the OS and sshd.
#   MemoryHigh — soft throttle (reclaim pressure) before the hard cap.
#   MemoryMax  — hard ceiling for the gateway slice (OOM-kills inside the slice).
#     2026-07-09: bumped 1536M→3072M. The old 1536M was written for a 2GB/1vCPU
#     node; on 2026.2.26 the gateway's V8 heap alone crosses 1.5G at startup and
#     the slice OOM-killed it in a restart loop (status=6/ABRT, 70+ restarts),
#     so :18789 never stayed up (400 load test: VM Health green / Gateway red on
#     every tenant). 3072M ≈ 75% of the 4G VM, leaving ~1G for OS/sshd/FIM.
#     NOTE: still coupled to the default VM size baked here; a smaller-mem tenant
#     would want a launch-vm drop-in that scales this to the VM's actual mem
#     (deferred; see evidence/CRITICAL-226-gateway-oom-plugin-incompat).
#   CPUQuota   — ≈1.8 core sustained (was 90%/~0.9 core for the 1vCPU node; the
#                default is now 2 vCPU, so cap just under 2 cores).
#   TasksMax   — fork-bomb guard.
MemoryHigh=2560M
MemoryMax=3072M
MemorySwapMax=0
CPUQuota=180%
TasksMax=512

[Install]
WantedBy=default.target
GWSVC
ln -sf ../openclaw-gateway.service /home/agent/.config/systemd/user/default.target.wants/openclaw-gateway.service

# --- guest log forwarder (vsock) user unit ---
# tail 三类 OpenClaw 日志 → guest 主动 connect vsock → host 侧 reader。guest 零凭据,
# 只往本地 vsock 写(学 Lambda/FireLens:凭据在 host 侧,不在 guest)。默认不采集:
# host 侧 launch-vm 只在 OC_GUEST_LOG_ENABLED=true 时才 PUT /vsock;forwarder connect
# 失败即丢行不阻塞,所以未启用时它空跑无害(不拖垮 gateway)。脚本 :OCFWDBIN 段装入。
#
# 【放 rootfs 全局 user 目录 /usr/lib/systemd/user,不放 data 盘 /home/agent/.config】
# 因为 /home/agent 是 data 盘(vdc),存量租户 rebuild 换 rootfs 时复用旧 data.ext4 →
# 若 unit 在 data 盘,存量租户升级后只有新脚本没有 unit,采集永不启动(codex 复审抓出:
# "rebuild 后仍有效"的硬要求)。rootfs 每次 rebuild 刷新,全局 user unit 随之更新,
# 存量租户换 rootfs 即拿到。全局 enable 用 /usr/lib/systemd/user/default.target.wants。
mkdir -p /usr/lib/systemd/user/default.target.wants
cat > /usr/lib/systemd/user/openclaw-log-forwarder.service << 'FWDSVC'
[Unit]
Description=OpenClaw guest log forwarder (tail -> vsock -> host)
After=openclaw-gateway.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/oc-guest-log-forwarder.py
Restart=always
RestartSec=5
Environment=HOME=/home/agent
# 背压兜底 + 零凭据:纯本地 vsock 写,无出网、无云凭据。收紧权限(仿 gateway)。
NoNewPrivileges=true
CapabilityBoundingSet=
RestrictSUIDSGID=true
MemoryMax=128M
TasksMax=16

[Install]
WantedBy=default.target
FWDSVC
ln -sf ../openclaw-log-forwarder.service /usr/lib/systemd/user/default.target.wants/openclaw-log-forwarder.service

# Delegate the memory/cpu/pids cgroup controllers to the agent's user manager so
# the gateway service's MemoryMax / CPUQuota / TasksMax above actually bite.
# Without delegation, a systemd *user* service silently ignores resource limits
# (the controllers aren't available in the user slice). Ubuntu Noble is cgroup
# v2 unified, so memory+cpu+pids are delegatable. This is the plumbing that turns
# the hardened cgroup sub-limit from decoration into enforcement.
mkdir -p /etc/systemd/system/user@.service.d
cat > /etc/systemd/system/user@.service.d/10-openclaw-delegate.conf << 'DELEG'
[Service]
Delegate=memory cpu pids
DELEG

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

# Install guest log forwarder (referenced by openclaw-log-forwarder.service above)
sudo cp "${SCRIPT_DIR}/deploy/userdata/oc-guest-log-forwarder.py" "${ROOTFS_DIR}/usr/local/bin/oc-guest-log-forwarder.py"
sudo chmod 755 "${ROOTFS_DIR}/usr/local/bin/oc-guest-log-forwarder.py"

# Unmount chroot binds first
sudo umount -l ${ROOTFS_DIR}/proc ${ROOTFS_DIR}/sys ${ROOTFS_DIR}/dev

# === Build data template from /home/agent ===
# 行首缩进锚定,只取 vm 段的 data_disk_mb。裸 grep 'data_disk_mb:' 是子串匹配,
# 会连 quotas 段的 max_data_disk_mb 一起命中 → DATA_DISK_MB 变多行 "8192\n0" →
# truncate -s 解析失败 → mkfs "Not enough space"(#276 首次部署实撞)。^\s+ 锚定
# 排除 max_ 前缀行;head -1 兜底防未来再有同名键。
DATA_DISK_MB=$(grep -E '^[[:space:]]+data_disk_mb:' "${SCRIPT_DIR}/config.yml" | awk '{print $2}' | head -1)
# #277 fail-loud:config 里连 vm.data_disk_mb 都没有时,DATA_DISK_MB 为空会让
# 下面的 truncate 报含糊错;这里显式拦下,直接说清缺哪个键。
[ -n "${DATA_DISK_MB}" ] || { echo "[FATAL] config.yml 未解析到 vm.data_disk_mb"; exit 1; }
echo "=== Building data template (${DATA_DISK_MB}MB) ==="
DATA_DIR="/tmp/openclaw-data-build"
truncate -s ${DATA_DISK_MB}M ${DATA_IMG}
mkfs.ext4 -q ${DATA_IMG}
sudo mkdir -p ${DATA_DIR}
sudo mount ${DATA_IMG} ${DATA_DIR}
# #35 image hygiene: strip python build caches from the agent home BEFORE it is
# copied into the data template (scaffold/__pycache__ etc. would otherwise ride
# into every tenant's writable data disk). Strip source then copy clean.
sudo bash -c "$(declare -f image_hygiene_strip _image_hygiene_cache_dir_predicate _image_hygiene_pyc_predicate); image_hygiene_strip '${ROOTFS_DIR}/home/agent'"
sudo cp -a ${ROOTFS_DIR}/home/agent/. ${DATA_DIR}/
# #35 fail-loud (评审 MEDIUM #1):对称于 immutable 侧,在挂载中的 data 盘上跑
# 同一份 find 断言。命中即打印 + umount + exit 1,别让污染的数据盘静默滚下去。
if ! sudo bash -c "$(declare -f image_hygiene_assert image_hygiene_find_hits _image_hygiene_cache_dir_predicate _image_hygiene_pyc_predicate); image_hygiene_assert '${DATA_DIR}' '<data>'"; then
  sudo umount ${DATA_DIR} 2>/dev/null || true
  sudo rmdir ${DATA_DIR} 2>/dev/null || true
  exit 1
fi
echo "  → #35 hygiene OK: data disk carries no python build cache"
sudo chown -R 1000:1000 ${DATA_DIR}
sudo umount ${DATA_DIR}
sudo rmdir ${DATA_DIR}

# === Build IMMUTABLE template (read-only authority disk) ===
# Security cornerstone: the agent's identity files + ops/safety skills live on
# a SEPARATE ext4 image that launch-vm.sh attaches with is_read_only:true at the
# Firecracker virtio layer. The guest then bind-mounts (-o ro) those paths over
# ~/.openclaw/workspace/*.md and ~/.openclaw/skills/. Because the device itself
# is read-only, even root inside the VM cannot write to it — write requests
# never reach a writable backing file (EROFS). This is a HARDWARE-level boundary,
# strictly stronger than file perms (root chmod) or chattr +i (root chattr -i).
#
# The same files ALSO stay on the writable data template (above) so first-boot
# onboard / any tooling that expects them present still works; the ro bind-mount
# simply shadows them with the authoritative copy at runtime. Zero regression.
echo "=== Building IMMUTABLE template (identity + ops skills, read-only authority) ==="
IMMUTABLE_IMG="/tmp/openclaw-immutable-${VERSION}.ext4"
IMMUTABLE_DIR="/tmp/openclaw-immutable-build"
rm -f ${IMMUTABLE_IMG}
# Identity files baked into the golden image (the canonical, tamper-proof set).
# TOOLS.md is included: it holds NO credentials (tools are described, keys are
# platform-injected + guardrail-masked) and is a protected identity file, so it
# must be tamper-proof too.
IMMUTABLE_WORKSPACE_FILES="SOUL.md AGENTS.md IDENTITY.md HEARTBEAT.md COMMUNICATION_STYLE.md TOOLS.md USER.md"
# Ops / safety skills that must never be editable from inside the VM. An attacker
# editing a vetting skill to fabricate a "safe" verdict is a risk, so these are
# baked read-only + FIM-monitored. This AWS sample ships a minimal neutral set
# (skill-vetter + weather); brand-specific skills are added when you bake your own
# golden image (see docs). The cp loop below tolerates a missing dir (`[ -d ]`
# guard) so trimming the set never breaks the build.
IMMUTABLE_SKILLS="skill-vetter weather"

# Stage the immutable set from the just-built golden /home/agent tree.
IMMUTABLE_STAGE="/tmp/openclaw-immutable-stage"
sudo rm -rf ${IMMUTABLE_STAGE}
sudo mkdir -p ${IMMUTABLE_STAGE}/workspace ${IMMUTABLE_STAGE}/skills
GOLDEN_WS="${ROOTFS_DIR}/home/agent/.openclaw/workspace"
GOLDEN_SK="${ROOTFS_DIR}/home/agent/.openclaw/skills"
for f in ${IMMUTABLE_WORKSPACE_FILES}; do
  if [ -f "${GOLDEN_WS}/${f}" ]; then
    sudo cp -a "${GOLDEN_WS}/${f}" "${IMMUTABLE_STAGE}/workspace/${f}"
  else
    echo "  ⚠ immutable workspace file missing from golden image: ${f}"
  fi
done
for s in ${IMMUTABLE_SKILLS}; do
  if [ -d "${GOLDEN_SK}/${s}" ]; then
    sudo cp -a "${GOLDEN_SK}/${s}" "${IMMUTABLE_STAGE}/skills/${s}"
  else
    echo "  ⚠ immutable skill missing from golden image: ${s}"
  fi
done

# #35 image hygiene: strip Python build caches BEFORE hashing/sizing/baking.
# scaffold/__pycache__, .ruff_cache, .pytest_cache and *.pyc are build-time
# debris that would otherwise (a) get sha256-hashed into golden-image.sha256 and
# baked into the READ-ONLY authority disk (bloat + non-reproducible bytes that
# vary by interpreter version), and (b) leak build-host state into every tenant
# microVM. The existing find-cleanup only covered ._* / node_modules / test-harness.
echo "  → #35 stripping python build caches from immutable stage"
sudo bash -c "$(declare -f image_hygiene_strip _image_hygiene_cache_dir_predicate _image_hygiene_pyc_predicate); image_hygiene_strip '${IMMUTABLE_STAGE}'"

# sha256 baseline (P3-9): hash every file in the immutable set so the healthcheck
# skill can verify the golden image at runtime. Path is relative to the disk root
# (e.g. workspace/SOUL.md, skills/ops-guardrails/SKILL.md). Written INTO the
# read-only disk itself so it can't be tampered with either.
echo "  → generating golden-image.sha256 baseline"
( cd ${IMMUTABLE_STAGE} && sudo find workspace skills -type f ! -name 'golden-image.sha256' -print0 \
    | sort -z | xargs -0 sha256sum | sudo tee golden-image.sha256 >/dev/null )
IMMUTABLE_FILE_COUNT=$(sudo grep -c '' ${IMMUTABLE_STAGE}/golden-image.sha256 || echo 0)
echo "  → ${IMMUTABLE_FILE_COUNT} files hashed into golden-image.sha256"

# Size the immutable image from the staged content + slack, rounded up to MB.
IMMUTABLE_CONTENT_KB=$(sudo du -sk ${IMMUTABLE_STAGE} | awk '{print $1}')
IMMUTABLE_SIZE_MB=$(( (IMMUTABLE_CONTENT_KB / 1024) + 16 ))
[ "${IMMUTABLE_SIZE_MB}" -lt 16 ] && IMMUTABLE_SIZE_MB=16
echo "  → immutable content ${IMMUTABLE_CONTENT_KB}KB → image ${IMMUTABLE_SIZE_MB}MB"
truncate -s ${IMMUTABLE_SIZE_MB}M ${IMMUTABLE_IMG}
mkfs.ext4 -q ${IMMUTABLE_IMG}
sudo mkdir -p ${IMMUTABLE_DIR}
sudo mount ${IMMUTABLE_IMG} ${IMMUTABLE_DIR}
sudo cp -a ${IMMUTABLE_STAGE}/. ${IMMUTABLE_DIR}/
# #35 image-hygiene assertion (fail-loud, DoD core): after the content lands on
# the real immutable disk, assert NO python build cache made it through. If any
# survives the strip above (e.g. a future skill dir sneaks one in), abort the
# build rather than silently ship a polluted read-only golden image. Runs while
# still mounted so it inspects the exact bytes that become the disk image.
# 谓词与 immutable strip / data strip 共用同一份库函数,避免漂移(评审 LOW #3)。
if ! sudo bash -c "$(declare -f image_hygiene_assert image_hygiene_find_hits _image_hygiene_cache_dir_predicate _image_hygiene_pyc_predicate); image_hygiene_assert '${IMMUTABLE_DIR}' '<immutable>'"; then
  sudo umount ${IMMUTABLE_DIR} 2>/dev/null || true
  sudo rmdir ${IMMUTABLE_DIR} 2>/dev/null || true
  exit 1
fi
echo "  → #35 hygiene OK: immutable disk carries no python build cache"
# Own by agent uid:gid (1000) so the read-only bind-mount presents agent-owned files.
sudo chown -R 1000:1000 ${IMMUTABLE_DIR}
sudo umount ${IMMUTABLE_DIR}
sudo rmdir ${IMMUTABLE_DIR}
sudo rm -rf ${IMMUTABLE_STAGE}

# Clear /home/agent in rootfs (now just a mount point)
sudo rm -rf ${ROOTFS_DIR}/home/agent/*
sudo rm -rf ${ROOTFS_DIR}/home/agent/.[!.]*

sudo umount ${ROOTFS_DIR}

echo "=== Compressing images ==="
pigz -f ${ROOTFS_IMG}
pigz -f ${DATA_IMG}
pigz -f ${IMMUTABLE_IMG}

echo "=== Uploading to S3 ==="
PROFILE_FLAG="${PROFILE:+--profile ${PROFILE}}"
ROOTFS_KEY="openclaw-rootfs-${VERSION}.ext4.gz"
DATA_KEY="openclaw-data-template-${VERSION}.ext4.gz"
IMMUTABLE_KEY="openclaw-immutable-${VERSION}.ext4.gz"
aws s3 cp ${ROOTFS_IMG}.gz s3://${BUCKET}/deployment/rootfs/${ROOTFS_KEY} ${PROFILE_FLAG}
aws s3 cp ${DATA_IMG}.gz s3://${BUCKET}/deployment/rootfs/${DATA_KEY} ${PROFILE_FLAG}
aws s3 cp ${IMMUTABLE_IMG}.gz s3://${BUCKET}/deployment/rootfs/${IMMUTABLE_KEY} ${PROFILE_FLAG}

# Upload manifest (version pointer). SKIP_MANIFEST=1 publishes the version-suffixed
# images WITHOUT moving the live manifest pointer — used when baking a new image
# next to an in-use one (e.g. validating v3 hardening while v2 stays the default).
if [ "${SKIP_MANIFEST:-0}" = "1" ]; then
  echo "→ SKIP_MANIFEST=1 — leaving manifest.json untouched (live pointer preserved)"
else
  cat <<EOF | aws s3 cp - s3://${BUCKET}/deployment/rootfs/manifest.json ${PROFILE_FLAG} --content-type application/json
{"version":"${VERSION}","rootfs":"${ROOTFS_KEY}","data_template":"${DATA_KEY}","immutable":"${IMMUTABLE_KEY}"}
EOF
fi

ROOTFS_SIZE=$(ls -lh ${ROOTFS_IMG}.gz | awk '{print $5}')
DATA_SIZE=$(ls -lh ${DATA_IMG}.gz | awk '{print $5}')
IMMUTABLE_SIZE=$(ls -lh ${IMMUTABLE_IMG}.gz | awk '{print $5}')
rm -f ${ROOTFS_IMG}.gz ${DATA_IMG}.gz ${IMMUTABLE_IMG}.gz

echo ""
echo "✓ rootfs ${VERSION} uploaded (${ROOTFS_SIZE})"
echo "  s3://${BUCKET}/deployment/rootfs/${ROOTFS_KEY}"
echo "✓ data template ${VERSION} uploaded (${DATA_SIZE})"
echo "  s3://${BUCKET}/deployment/rootfs/${DATA_KEY}"
echo "✓ immutable template ${VERSION} uploaded (${IMMUTABLE_SIZE})"
echo "  s3://${BUCKET}/deployment/rootfs/${IMMUTABLE_KEY}"
if [ "${SKIP_MANIFEST:-0}" = "1" ]; then
  echo "• manifest.json UNCHANGED (SKIP_MANIFEST=1) — new images uploaded alongside the live pointer"
else
  echo "✓ manifest.json → ${VERSION}"
fi

# Refresh on active hosts. Also gated by SKIP_MANIFEST so a side-by-side bake
# never pushes the new rootfs onto live VMs.
if [ "${SKIP_MANIFEST:-0}" != "1" ] && [ -n "${API_URL:-}" ] && [ -n "${API_KEY:-}" ]; then
  echo ""
  echo "→ Refreshing assets on active hosts..."
  curl -s -X POST "${API_URL}hosts/refresh-rootfs" -H "x-api-key: ${API_KEY}" | python3 -m json.tool
fi
