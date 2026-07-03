# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -e
# Mirror to serial console so get-console-output shows [oc:init] progress (#73)
exec > >(tee /var/log/openclaw-init.log > /dev/console) 2>&1
log() { echo "[oc:init] $(date +%H:%M:%S) $*"; }
log "Starting host setup..."

TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
AZ=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/availability-zone)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
PRIVATE_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/local-ipv4)

# IMDS values required to settle the hook; bail loudly if empty (#73).
[ -n "${INSTANCE_ID}" ] && [ -n "${REGION}" ] || { echo "[oc:init] FATAL: empty INSTANCE_ID/REGION" > /dev/console; exit 1; }

# Always settle the ASG hook on exit: CONTINUE on success, ABANDON on any
# failure, so a broken init never hangs until hook timeout (#73).
_complete_hook() {
  rc=$?; trap - EXIT
  result=$([ "$rc" -eq 0 ] && echo CONTINUE || echo ABANDON)
  log "init exiting rc=$rc → lifecycle $result"
  aws autoscaling complete-lifecycle-action --lifecycle-hook-name openclaw-host-init \
    --auto-scaling-group-name openclaw-hosts-asg --lifecycle-action-result "$result" \
    --instance-id "${INSTANCE_ID}" --region "${REGION}" || true
}
trap _complete_hook EXIT

# Query stack outputs — retries because the host can race ahead of CFN
# finalising outputs / setup.sh uploading scripts to S3 (issue: real-deploy
# regression surfaced in v1.0 E2E).
_stack_output() {
  for _i in $(seq 1 20); do
    val=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
      --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue" --output text --region ${REGION} 2>/dev/null)
    if [ -n "$val" ] && [ "$val" != "None" ]; then echo "$val"; return; fi
    sleep 15
  done
  echo "" # 5 minutes elapsed; give up
}

# S3 download with retries (scripts are uploaded by setup.sh AFTER cdk deploy
# completes, but the host's user-data starts as soon as ASG creates it).
_s3_get() {
  local src="$1" dst="$2"
  for _i in $(seq 1 20); do
    if aws s3 cp "$src" "$dst" --region ${REGION} --no-progress 2>/dev/null; then
      return 0
    fi
    sleep 15
  done
  return 1
}

# Step 0: stop (not disable) the boot auto-upgrade run so a stale AMI's kernel
# update can't reboot mid-init and orphan the lifecycle hook (#74).
systemctl stop unattended-upgrades apt-daily-upgrade.service 2>/dev/null || true

# Step 1: KVM
log "step1: KVM setup"
chmod 666 /dev/kvm
echo 'KERNEL=="kvm", MODE="0666"' > /etc/udev/rules.d/99-kvm.rules

# Step 1b: 多租户宿主侧信道加固(防机型/AMI 漂移)。这些加固在 Graviton4 metal 上
# 多数是机型默认满足(SMT 不支持、KSM 默认关、无 swap),但默认满足 ≠ 部署代码保证——
# 换机型/换 AMI/内核升级都可能悄悄改变默认。这里显式强制 + 落日志,让加固随重建继承、
# 可审计,不靠"碰巧机型如此"(对照 Firecracker prod-host-setup.md;见 memory
# firecracker-hardening-audit)。全部幂等,机型本就满足时是 no-op。
log "step1b: multi-tenant host hardening (KSM/swap/SMT)"
# KSM:跨租户内存去重是侧信道+数据残留面,强制关(默认 0 时 no-op)
if [ -w /sys/kernel/mm/ksm/run ]; then echo 0 > /sys/kernel/mm/ksm/run; fi
# swap:换出页是跨租户数据残留面,全关(无 swap 时 swapoff -a 无害)
swapoff -a 2>/dev/null || true
# SMT/超线程:跨租户共享物理核的侧信道面。Graviton4 无 SMT;若机型支持则强制关
if [ -w /sys/devices/system/cpu/smt/control ]; then
  echo off > /sys/devices/system/cpu/smt/control 2>/dev/null || true
fi
log "step1b done: ksm=$(cat /sys/kernel/mm/ksm/run 2>/dev/null||echo n/a) swaps=$(grep -c partition /proc/swaps 2>/dev/null||echo 0) smt=$(cat /sys/devices/system/cpu/smt/control 2>/dev/null||echo n/a)"

# Step 2: Install tools + Firecracker
log "step2: installing tools + firecracker"
apt-get -o DPkg::Lock::Timeout=60 update -qq
apt-get -o DPkg::Lock::Timeout=60 install -y -qq curl jq unzip pigz nginx > /dev/null 2>&1

# 1.5.0: per-host ed25519 key for host→guest SSH (private stays here, public
# injected into each VM by launch-vm.sh). Guarded for set -e re-runs.
mkdir -p /etc/openclaw
if [ ! -f /etc/openclaw/host_vm_key ]; then
  ssh-keygen -t ed25519 -N "" -C "openclaw-host-$(hostname)" -f /etc/openclaw/host_vm_key
  chmod 600 /etc/openclaw/host_vm_key
fi
ARCH="$(uname -m)"
# awscli zip 必须匹配 host 架构。arm64 metal(r8g/Graviton)是 aarch64;装错架构
# (x86_64)→ /usr/local/bin/aws "Exec format error" → _stack_output 里每次 aws 调用
# 失败 → 20×15s sleep 循环 ×2 = 10min > 600s lifecycle timeout → Heartbeat Timeout
# → ASG ABANDON 反复替换 metal,host 永远起不来。按 uname -m 选对 zip。
if ! command -v aws &>/dev/null; then
  AWSCLI_ARCH="x86_64"; [ "${ARCH}" = "aarch64" ] && AWSCLI_ARCH="aarch64"
  curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-${AWSCLI_ARCH}.zip" -o /tmp/awscliv2.zip
  cd /tmp && unzip -qo awscliv2.zip && ./aws/install &>/dev/null; cd -
fi
FC_URL="https://github.com/firecracker-microvm/firecracker/releases"
# Pin Firecracker version — `latest` may not have CI guest-kernel yet, 404s step3b (#74)
FC_VER="${FC_VERSION:-v1.15.1}"
curl -sL ${FC_URL}/download/${FC_VER}/firecracker-${FC_VER}-${ARCH}.tgz | tar -xz
mv release-${FC_VER}-${ARCH}/firecracker-${FC_VER}-${ARCH} /usr/local/bin/firecracker
mv release-${FC_VER}-${ARCH}/jailer-${FC_VER}-${ARCH} /usr/local/bin/jailer
rm -rf release-${FC_VER}-${ARCH}
log "firecracker ${FC_VER} installed"

# Resolve table names from stack outputs
HOSTS_TABLE=$(_stack_output HostsTable)
TENANTS_TABLE=$(_stack_output TenantsTable)
# Fallback to known constants if stack outputs aren't ready yet (chicken-
# and-egg: outputs only become visible AFTER the stack reaches
# CREATE_COMPLETE, but the host is launched mid-CREATE by the ASG).
# Both tables are stack-defined with these exact names in deploy/stack.py.
[ -z "$HOSTS_TABLE" ] || [ "$HOSTS_TABLE" = "None" ] && HOSTS_TABLE="openclaw-hosts"
[ -z "$TENANTS_TABLE" ] || [ "$TENANTS_TABLE" = "None" ] && TENANTS_TABLE="openclaw-tenants"
log "tables: hosts=${HOSTS_TABLE} tenants=${TENANTS_TABLE}"

# Resolve bucket names + backup CMK at runtime from stack outputs instead of
# baking them into user-data. EC2 user-data has a hard 16KB limit; init-host.sh
# alone is ~21KB and the assets-bucket token was Fn::Join'd into ~19 spots,
# blowing the limit. Pulling them from outputs here keeps user-data a plain,
# compressible string. ACCOUNT_ID from IMDS gives a deterministic fallback for
# the chicken-and-egg window before outputs are visible (host launches mid-CREATE).
ACCOUNT_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/dynamic/instance-identity/document | sed -n 's/.*"accountId"[ ]*:[ ]*"\([0-9]*\)".*/\1/p')
_RSFX=$([ "$REGION" = "ap-southeast-1" ] && echo "" || echo "-${REGION}")
ASSETS_BUCKET=$(_stack_output AssetsBucket)
[ -z "$ASSETS_BUCKET" ] || [ "$ASSETS_BUCKET" = "None" ] && ASSETS_BUCKET="openclaw-assets-${ACCOUNT_ID}${_RSFX}"
BACKUP_BUCKET=$(_stack_output BackupBucket)
[ -z "$BACKUP_BUCKET" ] || [ "$BACKUP_BUCKET" = "None" ] && BACKUP_BUCKET="openclaw-backups-${ACCOUNT_ID}${_RSFX}"
BACKUP_CMK_KEY_ID=$(_stack_output BackupCmkKeyId)
[ "$BACKUP_CMK_KEY_ID" = "None" ] && BACKUP_CMK_KEY_ID=""
log "buckets: assets=${ASSETS_BUCKET} backup=${BACKUP_BUCKET}"

# Write env for launch-vm.sh and host-agent
cat > /etc/platform.env << ENVEOF
OC_REGION=${REGION}
ASSETS_BUCKET=${ASSETS_BUCKET}
BACKUP_BUCKET=${BACKUP_BUCKET}
BACKUP_CMK_KEY_ID=${BACKUP_CMK_KEY_ID}
TENANTS_TABLE=${TENANTS_TABLE}
HOSTS_TABLE=${HOSTS_TABLE}
INSTANCE_ID=${INSTANCE_ID}
SUBNET_PREFIX={{SUBNET_PREFIX}}
ROOTFS_OVERLAY_MB={{ROOTFS_OVERLAY_MB}}
BALLOON_ENABLED={{BALLOON_ENABLED}}
BALLOON_DEFLATE_ON_OOM={{BALLOON_DEFLATE_ON_OOM}}
BALLOON_STATS_INTERVAL={{BALLOON_STATS_INTERVAL}}
BALLOON_FREE_PAGE_REPORTING={{BALLOON_FREE_PAGE_REPORTING}}
BALLOON_MAX_INFLATE_RATIO={{BALLOON_MAX_INFLATE_RATIO}}
BALLOON_MIN_GUEST_AVAILABLE_MB={{BALLOON_MIN_GUEST_AVAILABLE_MB}}
CLAW_HUB_URL={{CLAW_HUB_URL}}
CLAW_HUB_WS={{CLAW_HUB_WS}}
ENVEOF

# CLOUDFRONT_ORIGIN:CloudFront 分发域在 CDK 里晚于 LaunchTemplate 创建(循环依赖),
# 无法在 userdata 模板渲染期拿到。改为运行时从 SSM Parameter 拉(setup.sh 部署后写入
# /openclaw/cloudfront-origin)。launch-vm.sh:239 读此值设租户 gateway allowedOrigins,
# 避免硬编码旧账号 CloudFront 域。拉不到则留空(launch-vm 有 fallback,但应保证 SSM 已写)。
CF_ORIGIN=$(aws ssm get-parameter --name /openclaw/cloudfront-origin --region ${REGION} \
  --query "Parameter.Value" --output text 2>/dev/null || echo "")
echo "CLOUDFRONT_ORIGIN=${CF_ORIGIN}" >> /etc/platform.env
log "CLOUDFRONT_ORIGIN from SSM: ${CF_ORIGIN:-<empty, set /openclaw/cloudfront-origin>}"

# LITELLM_HOST:LiteLLM 网关地址(堡垒机 docker litellm:4000)。部署环境相关(堡垒机内网 IP
# 跨账号/重建会变),不能烤死在黄金镜像(镜像 baseUrl 的 __LITELLM_HOST__ 烤时默认 127.0.0.1
# 是错的,microVM 本地无 LiteLLM)。运行时从 SSM 拉(setup/运营写 /openclaw/litellm-host=
# 堡垒机内网 IP),launch-vm.sh 注入到租户 openclaw.json 的 models.providers.litellm.baseUrl。
# microVM 经 metal host 网络访问堡垒机:4000(同 VPC,SG 4000 放 VPC CIDR,实测 metal→堡垒机 200)。
LITELLM_HOST=$(aws ssm get-parameter --name /openclaw/litellm-host --region ${REGION} \
  --query "Parameter.Value" --output text 2>/dev/null || echo "")
echo "LITELLM_HOST=${LITELLM_HOST}" >> /etc/platform.env
log "LITELLM_HOST from SSM: ${LITELLM_HOST:-<empty, set /openclaw/litellm-host=bastion internal IP>}"

# LITELLM_SHARED_VKEY:无专属 vkey 的租户共用的 LiteLLM virtual key(有预算上限)。镜像里
# openclaw.json 的 apiKey 是 __INJECT_AT_DEPLOY__ 占位(CodeBuild 烤时不知道真实 key,不该
# 烤死管理密钥),运行时注入。launch-vm:没传 per-tenant vkey 时用这个 shared vkey 替换占位
# (否则 agent 拿占位符当 key 调 LiteLLM → 401 → "Something went wrong")。SecureString。
LITELLM_SHARED_VKEY=$(aws ssm get-parameter --name /openclaw/litellm-shared-vkey --with-decryption --region ${REGION} \
  --query "Parameter.Value" --output text 2>/dev/null || echo "")
echo "LITELLM_SHARED_VKEY=${LITELLM_SHARED_VKEY}" >> /etc/platform.env
log "LITELLM_SHARED_VKEY from SSM: ${LITELLM_SHARED_VKEY:+<set>}${LITELLM_SHARED_VKEY:-<empty, set /openclaw/litellm-shared-vkey>}"

# Nginx reverse proxy for tenant dashboards
mkdir -p /etc/nginx/conf.d/tenants
cat > /etc/nginx/conf.d/openclaw-proxy.conf <<'NGINX'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
server {
    listen 80 default_server;
    location /health { return 200 'ok'; add_header Content-Type text/plain; }
    location /agent/health { proxy_pass http://127.0.0.1:8899/health; }
    include /etc/nginx/conf.d/tenants/*.conf;
}
NGINX
# The claw-hub /hub/* reverse proxy (WS Upgrade passthrough) is written by
# install-hub.sh into conf.d/tenants/00-claw-hub.conf (included above). It is
# only present when the local single-process hub is installed (metal transition
# mode); after the EKS cutover (CLAW_HUB_URL set) it is simply absent.
rm -f /etc/nginx/sites-enabled/default
systemctl enable nginx
systemctl restart nginx
log "nginx proxy configured"

# Host agent — probes all local VMs, writes health to DynamoDB
{{HOST_AGENT_SCRIPT}}
mkdir -p /opt/openclaw
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /opt/openclaw/host-agent.py --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /opt/openclaw/host-agent.py
# Inject tenants table name into service (same mechanism as hosts table)
systemctl daemon-reload
systemctl enable host-agent
systemctl start host-agent
log "host agent started on :8899 (health + prom metrics)"

# AWS Distro for OpenTelemetry (ADOT) collector — scrapes localhost:8899/metrics
# and remote-writes to Amazon Managed Prometheus (issue #4). Skipped if
# AMP_REMOTE_WRITE_URL is unset (metrics.enabled: false in config.yml).
AMP_REMOTE_WRITE_URL="{{AMP_REMOTE_WRITE_URL}}"
if [ -n "${AMP_REMOTE_WRITE_URL}" ] && [ "${AMP_REMOTE_WRITE_URL}" != "none" ]; then
  log "step2b: installing aws-otel-collector"
  ARCH_DEB="amd64"; [ "$(uname -m)" = "aarch64" ] && ARCH_DEB="arm64"
  curl -fsSL "https://aws-otel-collector.s3.amazonaws.com/ubuntu/${ARCH_DEB}/latest/aws-otel-collector.deb" \
    -o /tmp/aws-otel-collector.deb
  dpkg -i /tmp/aws-otel-collector.deb >/dev/null 2>&1 || apt-get -f install -y -qq
  rm -f /tmp/aws-otel-collector.deb
  # Pull the templated config from S3 and substitute env vars.
  aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/adot-config.yaml \
    /opt/aws/aws-otel-collector/etc/config.yaml --region ${REGION} --no-progress
  AWS_REGION=${REGION} INSTANCE_ID=${INSTANCE_ID} AMP_REMOTE_WRITE_URL="${AMP_REMOTE_WRITE_URL}" \
    envsubst < /opt/aws/aws-otel-collector/etc/config.yaml \
    > /opt/aws/aws-otel-collector/etc/config.rendered.yaml
  mv /opt/aws/aws-otel-collector/etc/config.rendered.yaml /opt/aws/aws-otel-collector/etc/config.yaml
  systemctl enable aws-otel-collector
  systemctl restart aws-otel-collector
  log "adot collector started → ${AMP_REMOTE_WRITE_URL}"
else
  log "metrics disabled (no AMP url) — skipping ADOT collector"
fi

# Step 3: Mount data volume (before downloading to avoid filling root partition)
# Nitro instances map /dev/sdf to unpredictable /dev/nvmeXn1.
# Data volume has no partitions; root volume has partitions.
DATA_DEV=""
if [ -b /dev/sdf ]; then
  DATA_DEV=/dev/sdf
elif [ -b /dev/xvdf ]; then
  DATA_DEV=/dev/xvdf
else
  DATA_DEV=$(lsblk -dnpo NAME,TYPE | awk '$2=="disk"{print $1}' | while read d; do
    lsblk -n "$d" | grep -q part || echo "$d"
  done | head -1)
fi
if [ -z "$DATA_DEV" ]; then log "ERROR: data volume not found"; exit 1; fi
log "step3: mounting data volume ${DATA_DEV}"
if ! blkid ${DATA_DEV} | grep -q ext4; then mkfs.ext4 -q ${DATA_DEV}; fi
mkdir -p /data
mount ${DATA_DEV} /data
DATA_UUID=$(blkid -s UUID -o value ${DATA_DEV})
echo "UUID=${DATA_UUID} /data ext4 defaults,nofail 0 2" >> /etc/fstab
mkdir -p /data/firecracker-assets
chown ubuntu:ubuntu /data /data/firecracker-assets
rm -rf /home/ubuntu/firecracker-assets
ln -sfn /data/firecracker-assets /home/ubuntu/firecracker-assets

# Tag data volume
DATA_VOL_ID=$(aws ec2 describe-volumes --filters Name=attachment.instance-id,Values=${INSTANCE_ID} Name=attachment.device,Values=/dev/sdf --query 'Volumes[0].VolumeId' --output text --region ${REGION})
aws ec2 create-tags --resources ${DATA_VOL_ID} --tags Key=Name,Value=openclaw-data-${INSTANCE_ID} Key=openclaw:role,Value=host-data --region ${REGION}

# Step 3b: Kernel + rootfs from S3 (downloads directly to data volume via symlink)
log "step3b: waiting for rootfs in S3..."
T0=$SECONDS
ASSETS=/home/ubuntu/firecracker-assets
FC_MAJOR=$(echo ${FC_VER} | grep -oP "v\d+\.\d+")
# guest kernel 文件名按架构区分:x86_64 用 -no-acpi 变体(无 ACPI,x86 microVM 启动更快);
# aarch64 该后缀的对象不存在(实测 404 → curl -f exit 22 → init ABANDON,metal 永远起不来),
# arm64 用标准 vmlinux-5.10.245(实测 firecracker-ci/<ver>/aarch64/ 下真实存在)。
if [ "${ARCH}" = "aarch64" ]; then VMLINUX_NAME="vmlinux-5.10.245"; else VMLINUX_NAME="vmlinux-5.10.245-no-acpi"; fi
curl -fsSL -o ${ASSETS}/vmlinux "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/${FC_MAJOR}/${ARCH}/${VMLINUX_NAME}"
MANIFEST_URL="s3://${ASSETS_BUCKET}/{{ROOTFS_PREFIX}}/manifest.json"
for i in $(seq 1 20); do
  aws s3 cp ${MANIFEST_URL} ${ASSETS}/manifest.json --region ${REGION} --no-progress 2>/dev/null && break
  log "manifest.json not found, retrying in 30s ($i/20)..."
  sleep 30
done
if [ ! -f ${ASSETS}/manifest.json ]; then
  log "ERROR: no manifest.json after 10min — run ./build-rootfs.sh (or scripts/build-rootfs-on-ec2.sh) to build+upload rootfs"
  exit 1
fi
eval $(python3 -c "
import json; m=json.load(open('${ASSETS}/manifest.json'))
print(f'ROOTFS_KEY={m[\"rootfs\"]}')
print(f'DATA_KEY={m[\"data_template\"]}')
print(f'IMMUTABLE_KEY={m.get(\"immutable\",\"\")}')
print(f'ROOTFS_VER={m[\"version\"]}')
")
aws s3 cp s3://${ASSETS_BUCKET}/{{ROOTFS_PREFIX}}/${ROOTFS_KEY} ${ASSETS}/rootfs.gz --region ${REGION} --no-progress
aws s3 cp s3://${ASSETS_BUCKET}/{{ROOTFS_PREFIX}}/${DATA_KEY} ${ASSETS}/data.gz --region ${REGION} --no-progress
pigz -dc ${ASSETS}/rootfs.gz > ${ASSETS}/openclaw-rootfs.ext4 && rm -f ${ASSETS}/rootfs.gz
pigz -dc ${ASSETS}/data.gz > ${ASSETS}/openclaw-data-template.ext4 && rm -f ${ASSETS}/data.gz
fallocate --dig-holes ${ASSETS}/openclaw-data-template.ext4
# 只读权威盘(immutable):烤死 Finance Agent 身份 + 2 skill + 护栏,launch-vm 挂为 /dev/vdd
# is_read_only:true(见 launch-vm:432)。漏下它 → 租户起来但无身份/skill(launch-vm WARN
# "immutable absent — launching WITHOUT immutable authority disk")。manifest 有 immutable
# 字段就必须下,这是黄金镜像"启动即成品"的核心盘,不是可选。
if [ -n "${IMMUTABLE_KEY}" ]; then
  aws s3 cp s3://${ASSETS_BUCKET}/{{ROOTFS_PREFIX}}/${IMMUTABLE_KEY} ${ASSETS}/immutable.gz --region ${REGION} --no-progress
  pigz -dc ${ASSETS}/immutable.gz > ${ASSETS}/openclaw-immutable.ext4 && rm -f ${ASSETS}/immutable.gz
  log "immutable authority disk downloaded (${IMMUTABLE_KEY})"
else
  log "WARN: manifest 无 immutable 字段 — 租户将无只读身份/skill 盘"
fi
chown -R ubuntu:ubuntu ${ASSETS}
log "assets downloaded: rootfs=${ROOTFS_VER} ($((SECONDS-T0))s)"

# Step 3c: Sync shared skills from S3
log "step3c: syncing shared skills"
mkdir -p /data/shared-skills
aws s3 sync s3://${ASSETS_BUCKET}/skills/ /data/shared-skills/ --region ${REGION} 2>/dev/null || true
chown -R ubuntu:ubuntu /data/shared-skills
# Cron job to sync skills every 5 minutes
echo "*/5 * * * * root aws s3 sync s3://${ASSETS_BUCKET}/skills/ /data/shared-skills/ --region ${REGION} 2>/dev/null" > /etc/cron.d/openclaw-skills-sync
log "shared skills ready ($(ls /data/shared-skills/ 2>/dev/null | wc -l) skills)"

# Step 4: Deploy launch/stop scripts
log "step4: deploying scripts"
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/launch-vm.sh /home/ubuntu/launch-vm.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/launch-vm.sh /home/ubuntu/launch-vm.sh
chmod +x /home/ubuntu/launch-vm.sh && chown ubuntu:ubuntu /home/ubuntu/launch-vm.sh
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/stop-vm.sh /home/ubuntu/stop-vm.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/stop-vm.sh /home/ubuntu/stop-vm.sh
chmod +x /home/ubuntu/stop-vm.sh && chown ubuntu:ubuntu /home/ubuntu/stop-vm.sh
# clone(#12) / migrate(#64) / resize(#22) helpers — all must reach the host or
# the matching API hits a missing /home/ubuntu/*.sh and fails exit 127.
# start-all-vms / stop-all-vms — host-local fan-out for the 1-minute fleet
# power goal: control plane sends ONE SSM per host, host starts/stops all its
# VMs in bounded parallel (SSM concurrency = host count, not VM count).
for _s in clone-data migrate-vm resize-disk start-all-vms stop-all-vms; do
  aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/${_s}.sh /home/ubuntu/${_s}.sh --region ${REGION} --no-progress 2>/dev/null \
    || _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/${_s}.sh /home/ubuntu/${_s}.sh || true
  chmod +x /home/ubuntu/${_s}.sh && chown ubuntu:ubuntu /home/ubuntu/${_s}.sh
done
{{BACKUP_DATA_SCRIPT}}

# Step 4a2: claw-hub local single-process install (metal transition mode).
# When CLAW_HUB_URL is EMPTY in platform.env, every VM's channel dials this
# host's own hub at ws://HOST_TAP_IP:8790 (launch-vm.sh default). So this host
# MUST run a local hub or chat breaks ("agent offline"). When CLAW_HUB_URL is
# set (EKS cutover), channels dial the cluster ALB instead and we SKIP the local
# hub. install-hub.sh is the "改部署代码 → 重建" path: it pulls the hub source
# from S3, writes the systemd unit + nginx /hub proxy, and starts it. Idempotent.
if [ -z "${CLAW_HUB_URL:-}" ]; then
  log "step4a2: CLAW_HUB_URL empty → installing local single-process claw-hub"
  aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/install-hub.sh /home/ubuntu/install-hub.sh --region ${REGION} --no-progress 2>/dev/null \
    || _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/install-hub.sh /home/ubuntu/install-hub.sh || true
  chmod +x /home/ubuntu/install-hub.sh
  # install-hub.sh reads /etc/platform.env (region/table/bucket) + SSM (Cognito);
  # run it inline so a host rebuild brings the hub up with the host. A hub failure
  # must NOT ABANDON the whole host (other tenants still serve via gateway), so we
  # tolerate non-zero here and let host-agent/ops re-run install-hub.sh if needed.
  bash /home/ubuntu/install-hub.sh || log "WARN: install-hub.sh returned non-zero — hub may need a manual re-run"
else
  log "step4a2: CLAW_HUB_URL set (${CLAW_HUB_URL}) → channels dial cluster hub; skipping local hub"
fi

# Step 4b: AgentCore config (if enabled)
AGENTCORE_GW_URL="{{AGENTCORE_GATEWAY_URL}}"
if [ -n "${AGENTCORE_GW_URL}" ] && [ "${AGENTCORE_GW_URL}" != "none" ]; then
  echo "AGENTCORE_GATEWAY_URL=${AGENTCORE_GW_URL}" > /data/agentcore.env
  chown ubuntu:ubuntu /data/agentcore.env
  log "AgentCore config written: gateway=${AGENTCORE_GW_URL}"
fi

# Step 5: register to DDB. Retry (concurrent launches throttle writes); on
# total failure exit non-zero → trap ABANDONs (unregistered host is useless) (#73)
#
# Mixed-instance-type support: compute THIS host's real capacity from the host
# itself (nproc + /proc/meminfo) minus the reserved headroom, instead of a
# single stack-baked value. The old {{AVAIL_VCPU}}/{{AVAIL_MEM}} placeholders
# were substituted at synth time from config's ONE instance_type, so a host of a
# different size in a mixed ASG registered the WRONG capacity (a smaller host
# claiming a bigger host's vCPU/mem → oversell). Self-reporting makes every host
# register its TRUE size, so an ASG can mix equal-arch types of different sizes.
# register_host() (the API-driven path) already resolves capacity via the EC2
# API; this aligns the userdata direct-write path with it.
_RES_VCPU={{HOST_RESERVED_VCPU}}
_RES_MEM={{HOST_RESERVED_MEM}}
_HOST_VCPU=$(nproc)
# MemTotal is in kB; convert to MB. Firecracker/host overhead is covered by the
# configured reserved_mem headroom, same as the old synth-time computation.
_HOST_MEM_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
_HOST_MEM_MB=$(( _HOST_MEM_KB / 1024 ))
AVAIL_VCPU=$(( _HOST_VCPU - _RES_VCPU ))
AVAIL_MEM=$(( _HOST_MEM_MB - _RES_MEM ))
# Defensive: never register a non-positive capacity (would silently make the
# host unschedulable or, worse, wrap negative). Fail loud so the hook ABANDONs.
{ [ "${AVAIL_VCPU}" -gt 0 ] && [ "${AVAIL_MEM}" -gt 0 ]; } || {
  echo "[oc:init] FATAL: computed non-positive capacity vcpu=${AVAIL_VCPU} mem=${AVAIL_MEM} (host ${_HOST_VCPU}vcpu/${_HOST_MEM_MB}MB, reserved ${_RES_VCPU}/${_RES_MEM})" > /dev/console
  exit 1
}
log "step5: registering to DynamoDB (az=${AZ}, capacity ${AVAIL_VCPU}vcpu/${AVAIL_MEM}MB from ${_HOST_VCPU}vcpu/${_HOST_MEM_MB}MB host)"
_registered=0
for _r in $(seq 1 10); do
  aws dynamodb put-item --table-name ${HOSTS_TABLE} --region ${REGION} --item '{"instance_id":{"S":"'${INSTANCE_ID}'"},"private_ip":{"S":"'${PRIVATE_IP}'"},"az":{"S":"'${AZ}'"},"total_vcpu":{"N":"'${AVAIL_VCPU}'"},"total_mem_mb":{"N":"'${AVAIL_MEM}'"},"used_vcpu":{"N":"0"},"used_mem_mb":{"N":"0"},"vm_count":{"N":"0"},"next_vm_num":{"N":"1"},"status":{"S":"active"},"rootfs_version":{"S":"'${ROOTFS_VER}'"}}' && { _registered=1; break; }
  log "register attempt $_r failed, retrying in 15s..."
  sleep 15
done
[ "$_registered" -eq 1 ] || { log "ERROR: DDB registration failed after 10 attempts — ABANDON"; exit 1; }

# Lifecycle hook is settled by the EXIT trap (_complete_hook) — CONTINUE on success, ABANDON on any failure.
log "DONE host ready (total $((SECONDS))s)"
