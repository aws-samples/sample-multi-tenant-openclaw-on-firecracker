# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -e
# Mirror to serial console so get-console-output shows [oc:init] progress (#73)
exec > >(tee /var/log/openclaw-init.log > /dev/console) 2>&1
log() { echo "[oc:init] $(date +%H:%M:%S) $*"; }
log "Starting host setup..."

# IMDSv2 token. TTL=300s covers L11-14 (取完立即用),但后面 _stack_output 必需项会
# 等最多 20×15s=300s(每个,累计更久) → 到取 accountId 时这个 token 早过期 → 401 空 →
# accountId 空 → bucket 名拼成 `openclaw-assets--<region>`(double-dash)→ 拉镜像 404 →
# crashloop → ABANDON 换机器死循环(2026-07-17 新加坡实撞根因,pit#14)。
# 修:凡在可能耗时的 _stack_output 之后再取 IMDS 的,一律现取 fresh token(_fresh_imds)。
_fresh_imds() {  # $1=metadata-path(如 dynamic/instance-identity/document);现取 token,不复用过期的
  local _tok
  _tok=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -s -H "X-aws-ec2-metadata-token: $_tok" "http://169.254.169.254/latest/$1"
}
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
# $1 = output key. Matches by EXACT key first, then falls back to a prefix/
# substring match — CDK prefixes outputs defined inside a nested Construct with
# the construct path + a hash suffix (e.g. the DispatchInfra construct turns
# `AssignmentsTableName` into `DispatchAssignmentsTableName676F1D7B`). Querying
# the bare name would never match → the retry loop below burns the full
# 20×15s=5min then gives up empty, silently, on every host boot → blows the ASG
# lifecycle heartbeat → ABANDON. So we grep the full output list for `<key>` as
# a substring after the exact match misses.
# $2 = "optional": single attempt, no 5-min retry (key legitimately may not
# exist, e.g. dispatch disabled). Absent = required, retry up to 5 min for the
# chicken-and-egg window (host launches mid-CREATE, before outputs are visible).
_stack_output() {
  _key="$1"; _optional="${2:-}"
  _attempts=20; [ "$_optional" = "optional" ] && _attempts=1
  for _i in $(seq 1 "$_attempts"); do
    # one describe-stacks, then match exact-or-substring locally (avoids a
    # second API call and resolves nested-construct suffixed keys).
    _all=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
      --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output text --region ${REGION} 2>/dev/null)
    val=$(printf '%s\n' "$_all" | awk -v k="$_key" '$1==k{print $2; found=1; exit} END{if(!found)exit 1}')
    [ -z "$val" ] && val=$(printf '%s\n' "$_all" | awk -v k="$_key" 'index($1,k){print $2; exit}')
    if [ -n "$val" ] && [ "$val" != "None" ]; then echo "$val"; return; fi
    [ "$_i" -lt "$_attempts" ] && sleep 15
  done
  echo "" # gave up (required: 5 min elapsed; optional: single miss)
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
# IPv6 forwarding:内核默认 0,显式关一遍防 AMI 漂移(与上面显式关 SMT 同理)。
# 铁律 #6:默认满足 ≠ 部署代码保证。租户 tap 走 per-tap disable_ipv6=1(launch-vm.sh
# 里做),host 侧 all.forwarding=0 是纵深防御:即便某台 tap 漏配了 disable_ipv6,
# host 不转发也守住 IPv6 IMDS fd00:ec2::254。
sysctl -q -w net.ipv6.conf.all.forwarding=0 2>/dev/null || true
# nf_conntrack table sizing for NFR-3 (INTERFACE-CONTRACT §6/§7 / 11-ENGINE-TRANSFORM
# 01-REQUIREMENTS NFR-3): a single r8g.metal-24xl runs up to 400 microVMs, each
# with 2-3 outbound WS + per-tap DNAT rules. Kernel default nf_conntrack_max is
# 262144 on Ubuntu 22.04 aarch64 which the host can blow past under peak fan-in
# well before all 400 tenants are active. Load the module first (host may not
# have any stateful iptables yet at boot on a fresh AMI), then set the ceiling
# to 1M matching the edge box tuning in deploy/edge/install-edge.sh:131.
# Idempotent: modprobe is a no-op if already loaded; sysctl -w overwrites.
modprobe -q nf_conntrack 2>/dev/null || true
cat > /etc/sysctl.d/99-openclaw-host-conntrack.conf <<SYSCTL
net.netfilter.nf_conntrack_max = 1048576
SYSCTL
sysctl -q -p /etc/sysctl.d/99-openclaw-host-conntrack.conf 2>/dev/null || log "WARN: nf_conntrack sysctl reload emitted warnings"
_ipv6fwd=$(cat /proc/sys/net/ipv6/conf/all/forwarding 2>/dev/null || echo n/a)
_ctmax=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo n/a)
log "step1b done: ksm=$(cat /sys/kernel/mm/ksm/run 2>/dev/null||echo n/a) swaps=$(grep -c partition /proc/swaps 2>/dev/null||echo 0) smt=$(cat /sys/devices/system/cpu/smt/control 2>/dev/null||echo n/a) ipv6fwd=${_ipv6fwd} conntrack_max=${_ctmax}"

# Step 2: Install tools + Firecracker
log "step2: installing tools + firecracker"
apt-get -o DPkg::Lock::Timeout=60 update -qq
apt-get -o DPkg::Lock::Timeout=60 install -y -qq curl jq unzip pigz python3-redis > /dev/null 2>&1

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
# SQS dispatch 二期 pull 模式:host-agent 从 openclaw-assignments 拉每台 host 的
# desired 状态。真实 output key 是 `DispatchAssignmentsTableName<hash>`(定义在
# DispatchInfra 构造内,CDK 自动前缀化)——_stack_output 现按子串匹配 `AssignmentsTable`
# 命中它。dispatch 关时该 output 不存在 → 传 optional 单次探测不空烧 5min,
# 清空让 host-agent 不启 dispatch 线程(零变化)。
ASSIGNMENTS_TABLE=$(_stack_output AssignmentsTable optional)
# Fallback to known constants if stack outputs aren't ready yet (chicken-
# and-egg: outputs only become visible AFTER the stack reaches
# CREATE_COMPLETE, but the host is launched mid-CREATE by the ASG).
# Both tables are stack-defined with these exact names in deploy/stack.py.
[ -z "$HOSTS_TABLE" ] || [ "$HOSTS_TABLE" = "None" ] && HOSTS_TABLE="openclaw-hosts"
[ -z "$TENANTS_TABLE" ] || [ "$TENANTS_TABLE" = "None" ] && TENANTS_TABLE="openclaw-tenants"
[ -z "$ASSIGNMENTS_TABLE" ] || [ "$ASSIGNMENTS_TABLE" = "None" ] && ASSIGNMENTS_TABLE=""
log "tables: hosts=${HOSTS_TABLE} tenants=${TENANTS_TABLE}"

# Resolve bucket names + backup CMK at runtime from stack outputs instead of
# baking them into user-data. EC2 user-data has a hard 16KB limit; init-host.sh
# alone is ~21KB and the assets-bucket token was Fn::Join'd into ~19 spots,
# blowing the limit. Pulling them from outputs here keeps user-data a plain,
# compressible string. ACCOUNT_ID from IMDS gives a deterministic fallback for
# the chicken-and-egg window before outputs are visible (host launches mid-CREATE).
# fresh token(上面 _stack_output 可能已烧掉 L10 那个 300s token,pit#14)。
ACCOUNT_ID=$(_fresh_imds dynamic/instance-identity/document | sed -n 's/.*"accountId"[ ]*:[ ]*"\([0-9]*\)".*/\1/p')
_RSFX=$([ "$REGION" = "ap-southeast-1" ] && echo "" || echo "-${REGION}")
ASSETS_BUCKET=$(_stack_output AssetsBucket)
# fallback 拼 bucket 名前必须有合法 12 位 accountId,否则拼出 `openclaw-assets--<region>`
# (double-dash)→ 拉镜像 404 → crashloop。宁 fail-loud 中止(ABANDON 带明确错)也不静默拼坏名。
if [ -z "$ASSETS_BUCKET" ] || [ "$ASSETS_BUCKET" = "None" ]; then
  echo "$ACCOUNT_ID" | grep -qE '^[0-9]{12}$' || { echo "[oc:init] FATAL: stack output AssetsBucket 未就绪且 IMDS accountId 非法('$ACCOUNT_ID'),拒绝拼 double-dash bucket 名" > /dev/console; exit 1; }
  ASSETS_BUCKET="openclaw-assets-${ACCOUNT_ID}${_RSFX}"
fi
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
TENANT_SECRETS_TABLE=openclaw-tenant-secrets
HOSTS_TABLE=${HOSTS_TABLE}
ASSIGNMENTS_TABLE=${ASSIGNMENTS_TABLE}
INSTANCE_ID=${INSTANCE_ID}
SUBNET_PREFIX={{SUBNET_PREFIX}}
ROOTFS_OVERLAY_MB={{ROOTFS_OVERLAY_MB}}
DNAT_PORT_LOW={{DNAT_PORT_LOW}}
DNAT_PORT_HIGH={{DNAT_PORT_HIGH}}
PORT_QUARANTINE_SECONDS={{PORT_QUARANTINE_SECONDS}}
BALLOON_ENABLED={{BALLOON_ENABLED}}
BALLOON_DEFLATE_ON_OOM={{BALLOON_DEFLATE_ON_OOM}}
BALLOON_STATS_INTERVAL={{BALLOON_STATS_INTERVAL}}
BALLOON_FREE_PAGE_REPORTING={{BALLOON_FREE_PAGE_REPORTING}}
BALLOON_MAX_INFLATE_RATIO={{BALLOON_MAX_INFLATE_RATIO}}
BALLOON_MIN_GUEST_AVAILABLE_MB={{BALLOON_MIN_GUEST_AVAILABLE_MB}}
EGRESS_ALLOWLIST_ENABLED={{EGRESS_ALLOWLIST_ENABLED}}
EGRESS_INCLUDE_VPC_CIDR={{EGRESS_INCLUDE_VPC_CIDR}}
EGRESS_VPC_CIDR={{EGRESS_VPC_CIDR}}
EGRESS_ALLOWLIST_CIDRS={{EGRESS_ALLOWLIST_CIDRS}}
EGRESS_ALLOWLIST_DOMAINS={{EGRESS_ALLOWLIST_DOMAINS}}
EGRESS_DNS_UPSTREAM={{EGRESS_DNS_UPSTREAM}}
OC_HOST_LAUNCH_SLOTS={{OC_HOST_LAUNCH_SLOTS}}
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

# CLAWPOOL_RSA_CMK_ARN:#149 asymmetric-v1 注入用。cred-inject.sh 对 scheme=asymmetric-v1
# 的 env 凭据调 `aws kms decrypt --key-id ${CLAWPOOL_RSA_CMK_ARN} --encryption-algorithm
# RSAES_OAEP_SHA_256`(信封里的 key_id 是逻辑名,解密需真实 ARN)。栈建 RSA CMK 时写此 SSM。
CLAWPOOL_RSA_CMK_ARN=$(aws ssm get-parameter --name /openclaw/clawpool-rsa-cmk-arn --region ${REGION} \
  --query "Parameter.Value" --output text 2>/dev/null || echo "")
echo "CLAWPOOL_RSA_CMK_ARN=${CLAWPOOL_RSA_CMK_ARN}" >> /etc/platform.env
log "CLAWPOOL_RSA_CMK_ARN from SSM: ${CLAWPOOL_RSA_CMK_ARN:+<set>}${CLAWPOOL_RSA_CMK_ARN:-<empty>}"

# LITELLM_SHARED_VKEY:无专属 vkey 的租户共用的 LiteLLM virtual key(有预算上限)。镜像里
# openclaw.json 的 apiKey 是 __INJECT_AT_DEPLOY__ 占位(CodeBuild 烤时不知道真实 key,不该
# 烤死管理密钥),运行时注入。launch-vm:没传 per-tenant vkey 时用这个 shared vkey 替换占位
# (否则 agent 拿占位符当 key 调 LiteLLM → 401 → "Something went wrong")。SecureString。
LITELLM_SHARED_VKEY=$(aws ssm get-parameter --name /openclaw/litellm-shared-vkey --with-decryption --region ${REGION} \
  --query "Parameter.Value" --output text 2>/dev/null || echo "")
echo "LITELLM_SHARED_VKEY=${LITELLM_SHARED_VKEY}" >> /etc/platform.env
log "LITELLM_SHARED_VKEY from SSM: ${LITELLM_SHARED_VKEY:+<set>}${LITELLM_SHARED_VKEY:-<empty, set /openclaw/litellm-shared-vkey>}"

# ENGINE_REDIS_ENDPOINT/PORT:#187 两级路由的 host 侧对接(P3 未完的桥接,P7 补)。
# host-agent.py:214 靠 ENGINE_REDIS_ENDPOINT 触发 RedisRouteWriter,promote 时把
# route:{tid}→{host,port,guest_ip} 写进 Redis,edge OpenResty route.lua 查它转发。
# 没这个 env → host-agent 不写 Redis → edge 查空 → /ws 404(P7 真机实撞根因)。
# stack.py:4248 已把 endpoint(host:port 一整串)写进 SSM,这里拉出来剥分成
# 独立 host + port 两个 env(RedisRouteWriter 构造函数 endpoint 与 port 分开两参,
# route_ops.py:290-300;直接塞 host:port 整串当主机名会连不上)。
_REDIS_EP=$(aws ssm get-parameter --name /openclaw/engine/redis/primary-endpoint --region ${REGION} \
  --query "Parameter.Value" --output text 2>/dev/null || echo "")
if [ -n "${_REDIS_EP}" ] && [ "${_REDIS_EP}" != "None" ]; then
  # 剥分 host:port(ElastiCache primary endpoint 形如 xxx.cache.amazonaws.com:6379)。
  echo "ENGINE_REDIS_ENDPOINT=${_REDIS_EP%:*}" >> /etc/platform.env
  echo "ENGINE_REDIS_PORT=${_REDIS_EP##*:}" >> /etc/platform.env
  log "ENGINE_REDIS_ENDPOINT from SSM: ${_REDIS_EP%:*}:${_REDIS_EP##*:}"
else
  # fail-open:redis.enabled=false(未建 ElastiCache)时 SSM 无此参数,host-agent
  # 走「写 DDB + DNAT 不写 Redis」降级路径(= 现有行为),不阻塞 host 起来。
  log "ENGINE_REDIS_ENDPOINT: <empty, redis disabled or param missing — host-agent runs degraded, no Redis route>"
fi

# nginx removed (#215 R18 14.1): ALB target group now points directly to
# host-agent :8899/health, no proxy layer needed.

# ── #39 microVM 出网默认拒绝白名单 — host 侧基建(dnsmasq + ipset)──
# 逻辑在独立脚本 setup-egress-allowlist.sh(S3 分发,不内联进 init-host 以免撑爆 user-data
# 16KB 硬限;memory: uswest2-deploy-deadlock)。它读 /etc/platform.env 的 EGRESS_* + REGION,
# config security.egress_allowlist_enabled 默认 false 时脚本自身直接跳过(host 零变化)。
# 逐个 tap 的 DNAT/ACCEPT/DROP 规则在 launch-vm.sh 里做(每 VM 一份,-i $TAP 隔离)。
mkdir -p /home/ubuntu
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/setup-egress-allowlist.sh /home/ubuntu/setup-egress-allowlist.sh --region ${REGION} --no-progress 2>/dev/null \
  || _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/setup-egress-allowlist.sh /home/ubuntu/setup-egress-allowlist.sh || true
if [ -f /home/ubuntu/setup-egress-allowlist.sh ]; then
  chmod +x /home/ubuntu/setup-egress-allowlist.sh
  bash /home/ubuntu/setup-egress-allowlist.sh || log "WARN: setup-egress-allowlist.sh returned non-zero (egress allowlist degraded)"
else
  log "WARN: setup-egress-allowlist.sh not downloaded — egress allowlist skipped (host-agent still starts)"
fi

# Host agent — probes all local VMs, writes health to DynamoDB
{{HOST_AGENT_SCRIPT}}
mkdir -p /opt/openclaw
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /opt/openclaw/host-agent.py --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/host-agent.py /opt/openclaw/host-agent.py
# route_ops.py 必须与 host-agent.py 同目录(host-agent.py:27 `import route_ops`,
# sys.path 插的是 __file__ 所在目录 /opt/openclaw)。缺它 host-agent
# ModuleNotFoundError crashloop、数据面 host 侧全挂(P7 真机实撞:NRestarts 225+)。
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/route_ops.py /opt/openclaw/route_ops.py --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/route_ops.py /opt/openclaw/route_ops.py
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
  # Pull the templated config from S3. #229: 优先 deployment/observability/adot/
  # (S3 asset 化,可下发新版无需重烤镜像);拉失败回退老前缀 deployment/scripts/
  # 保兼容;两个都拉不到 fail-loud(镜像没烤兜底,静默继续 = ADOT 起不来还蒙在鼓里)。
  if ! aws s3 cp s3://${ASSETS_BUCKET}/deployment/observability/adot/adot-config.yaml \
       /opt/aws/aws-otel-collector/etc/config.yaml --region ${REGION} --no-progress 2>/dev/null; then
    log "WARN(#229): observability/adot 配置未拉到,回退 deployment/scripts/"
    aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/adot-config.yaml \
      /opt/aws/aws-otel-collector/etc/config.yaml --region ${REGION} --no-progress
  fi
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

# Step 3a2: Fluent Bit host log shipper (#245). Runs after /data is mounted
# so the vm pipeline can tail /data/firecracker-vms/*/fc.log. Shared installer
# + host config pull from S3, same mechanism as edge. Config-gated: the shared
# script no-ops when LOGGING_ENABLED=false. Host has no baked fallback (no
# FB_LOCAL_DIR) — S3 miss is fail-loud inside the installer, matching #229.
LOGGING_ENABLED="{{LOGGING_ENABLED}}"
if [ "${LOGGING_ENABLED}" = "true" ]; then
  log "step3a2: installing host Fluent Bit (journald + fc.log → Firehose)"
  mkdir -p /opt/openclaw/fluent-bit
  aws s3 cp s3://${ASSETS_BUCKET}/deployment/observability/fluent-bit/install-fluent-bit.sh \
    /opt/openclaw/fluent-bit/install-fluent-bit.sh --region ${REGION} --no-progress \
    || { log "ERROR(#245): install-fluent-bit.sh 未拉到 (S3 miss)"; exit 1; }
  chmod +x /opt/openclaw/fluent-bit/install-fluent-bit.sh
  FB_ROLE=host \
  FB_REGION="${REGION}" \
  FB_STREAM_HOST="{{FB_STREAM_HOST}}" \
  FB_STREAM_VM="{{FB_STREAM_VM}}" \
  LOGGING_ENABLED="true" \
  ASSETS_BUCKET="${ASSETS_BUCKET}" \
  AWS_REGION="${REGION}" \
    bash /opt/openclaw/fluent-bit/install-fluent-bit.sh
  # guest 日志 reader:收 guest 从 vsock 写来的日志落 per-VM oc-guest.log,交给上面
  # 装的 Fluent Bit tail 管道(见 fluent-bit host conf 的 oc-guest.log input)。复用
  # LOGGING_ENABLED 门控(guest 日志采集是 host 日志能力的一部分,不新造开关)。
  # 实际是否采集还取决于 launch-vm 的 OC_GUEST_LOG_ENABLED(控制 PUT /vsock);二者都开
  # 才成链路。reader 空跑无害(无 vsock UDS 时只是空扫)。
  aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/oc-guest-log-reader.py /opt/openclaw/oc-guest-log-reader.py \
    --region ${REGION} --no-progress \
    || { log "ERROR: oc-guest-log-reader.py 未拉到 (S3 miss)"; exit 1; }
  # fresh-host 首启时 /data/firecracker-vms 可能还没建(还没起过 VM),而 sandbox 的
  # ReadWritePaths= 指向不存在的目录会让 reader 启动失败(codex + 真机 h1 实测:reader
  # 卡 activating 起不来)。先建好目录,unit 再 RequiresMountsFor 确保 /data 挂载后才起。
  install -d -m 0755 /data/firecracker-vms
  cat > /etc/systemd/system/oc-guest-log-reader.service << 'GLRSVC'
[Unit]
Description=OpenClaw guest log reader (vsock -> per-VM file for Fluent Bit)
After=network.target host-agent.service
RequiresMountsFor=/data/firecracker-vms
[Service]
# 不加载 /etc/platform.env:reader 处理不可信 guest 帧,不该把含共享密钥的 env 带进
# 这个进程(codex:最小权限)。它只需 VSOCK_PORT,单独 Environment 给。
ExecStart=/usr/bin/python3 /opt/openclaw/oc-guest-log-reader.py
Restart=always
RestartSec=5
KillMode=process
Environment=OC_GUEST_LOG_VSOCK_PORT=9999
UMask=0077
# systemd 沙箱:reader 以 root 处理不可信帧,收紧攻击面(codex)。只需读写
# /data/firecracker-vms 下的 UDS + oc-guest.log,其余文件系统只读/隔离。
# CapabilityBoundingSet= 清空:reader 不需要任何 Linux capability(仿 gateway CapBnd=0)。
NoNewPrivileges=true
CapabilityBoundingSet=
ProtectSystem=strict
ReadWritePaths=/data/firecracker-vms
ProtectHome=true
PrivateTmp=true
RestrictSUIDSGID=true
[Install]
WantedBy=multi-user.target
GLRSVC
  systemctl daemon-reload
  systemctl enable oc-guest-log-reader
  systemctl start oc-guest-log-reader
  log "guest log reader started (vsock -> per-VM oc-guest.log -> Fluent Bit)"

  # fc.log local rotation: keep 3 days on-host; AOS holds the searchable copy.
  cat > /etc/logrotate.d/openclaw-fcvm <<'LOGROTATE'
/data/firecracker-vms/*/fc.log {
    daily
    rotate 3
    missingok
    notifempty
    copytruncate
    compress
    delaycompress
}
LOGROTATE
  log "host Fluent Bit + fc.log logrotate(3d) configured"
else
  log "LOGGING_ENABLED=false; skipping host Fluent Bit"
fi

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
# 只读权威盘(immutable):烤死 ClawPool Agent 身份 + 15skill + 护栏,launch-vm 挂为 /dev/vdd
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
# harden-config.sh(#41)— launch-vm.sh source 的 POSIX sh 幂等收敛库。必须在
# launch-vm.sh 首次被调用前落地,否则 launch-vm 顶部 . lib/harden-config.sh 失败
# → 每次启动 exit 1 → 一台 host 起不来任何租户。走同一 _s3_get 重试骨架。
mkdir -p /home/ubuntu/lib
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/lib/harden-config.sh /home/ubuntu/lib/harden-config.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/lib/harden-config.sh /home/ubuntu/lib/harden-config.sh
# cred-inject.sh(#118)— launch-vm.sh source 的凭据 KMS 解密库(只在租户有
# injected_credentials 时才 source;缺失即 fail-loud 中止该 VM 启动,不静默注入空凭据)。
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/lib/cred-inject.sh /home/ubuntu/lib/cred-inject.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/lib/cred-inject.sh /home/ubuntu/lib/cred-inject.sh
chmod +x /home/ubuntu/lib/harden-config.sh /home/ubuntu/lib/cred-inject.sh && chown -R ubuntu:ubuntu /home/ubuntu/lib
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/stop-vm.sh /home/ubuntu/stop-vm.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/stop-vm.sh /home/ubuntu/stop-vm.sh
chmod +x /home/ubuntu/stop-vm.sh && chown ubuntu:ubuntu /home/ubuntu/stop-vm.sh
# clone(#12) / migrate(#64) / resize(#22) helpers — all must reach the host or
# the matching API hits a missing /home/ubuntu/*.sh and fails exit 127.
# start-all-vms / stop-all-vms — host-local fan-out for the 1-minute fleet
# power goal: control plane sends ONE SSM per host, host starts/stops all its
# VMs in bounded parallel (SSM concurrency = host count, not VM count).
# #256 — launch-all-vms.sh 已内联进 launch-vm.sh 的 fan_out_main(唯一起 VM 脚本,已在
# 上方单独下载),聚合 SSM 命令调 `launch-vm.sh --manifest|--from-ddb ...`,此处不再拉它。
for _s in clone-data migrate-vm resize-disk start-all-vms stop-all-vms; do
  aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/${_s}.sh /home/ubuntu/${_s}.sh --region ${REGION} --no-progress 2>/dev/null \
    || _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/${_s}.sh /home/ubuntu/${_s}.sh || true
  chmod +x /home/ubuntu/${_s}.sh && chown ubuntu:ubuntu /home/ubuntu/${_s}.sh
done
{{BACKUP_DATA_SCRIPT}}

# #187 转型:step4a2 claw-hub 本地安装已下线。数据面改两级路由直连 microVM
# 原生 gateway(ALB LOR → OpenResty edge → Redis 查表 → host iptables DNAT →
# microVM:18789)。install-hub.sh + deploy/hub/ 源已归档到
# engineering/04-archive/p4-cutover-deprecated/。#187 P5:CLAW_HUB_URL/CLAW_HUB_WS
# env 与 stack.py 模板替换、CloudFront /hub/* behavior、HubTargetGroup 已一并删除。

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
  aws dynamodb put-item --table-name ${HOSTS_TABLE} --region ${REGION} --item '{"instance_id":{"S":"'${INSTANCE_ID}'"},"private_ip":{"S":"'${PRIVATE_IP}'"},"az":{"S":"'${AZ}'"},"total_vcpu":{"N":"'${AVAIL_VCPU}'"},"total_mem_mb":{"N":"'${AVAIL_MEM}'"},"used_vcpu":{"N":"0"},"used_mem_mb":{"N":"0"},"vm_count":{"N":"0"},"next_vm_num":{"N":"1"},"status":{"S":"active"},"rootfs_version":{"S":"'${ROOTFS_VER}'"},"snapshot_time":{"S":"'${ROOTFS_VER}'"}}' && { _registered=1; break; }
  log "register attempt $_r failed, retrying in 15s..."
  sleep 15
done
[ "$_registered" -eq 1 ] || { log "ERROR: DDB registration failed after 10 attempts — ABANDON"; exit 1; }

# Lifecycle hook is settled by the EXIT trap (_complete_hook) — CONTINUE on success, ABANDON on any failure.
log "DONE host ready (total $((SECONDS))s)"
