# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -e
exec > >(tee /var/log/openclaw-init.log > /dev/console) 2>&1
log() { echo "[oc:init] $(date +%H:%M:%S) $*"; }
log "Starting host setup..."

# IMDSv2 token. TTL=300s covers L11-14 (取完立即用),但后面 _stack_output 必需项会
# 等最多 20×15s=300s(每个,累计更久) → 到取 accountId 时这个 token 早过期 → 401 空 →
# accountId 空 → bucket 名拼成 `openclaw-assets--<region>`(double-dash)→ 拉镜像 404 →
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

[ -n "${INSTANCE_ID}" ] && [ -n "${REGION}" ] || { echo "[oc:init] FATAL: empty INSTANCE_ID/REGION" > /dev/console; exit 1; }

# Always settle the ASG hook on exit: CONTINUE on success, ABANDON on any
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

# Step 2: components (provision stage) + per-host identity
# provision-host.sh, which EC2 Image Builder bakes into the host AMI. This file is
# the configure stage: it runs on EVERY boot and only does work that needs per-host
# identity or per-deployment config. The boundary is "does it reach the internet",
# not the old step numbering — network installs are the unreliable part (wrong-arch
# awscli, firecracker tgz 404, aarch64 vmlinux 404 have each ABANDONed a metal).
#
# provision-host.sh is inlined here rather than fetched, so its bytes are part of
# A provision change therefore changes the LT, exactly like an init change does.
log "step2: components + host identity"
mkdir -p /etc/openclaw /opt/openclaw
{{PROVISION_SCRIPT}}
# Golden AMI already ran it: skip. Plain Ubuntu AMI: run it now, so the non-golden
# path keeps working byte-for-byte as before. Idempotent either way.
if [ -f /etc/openclaw/.ami-provisioned ]; then
  log "step2: AMI pre-provisioned ($(tr '\n' ' ' < /etc/openclaw/.ami-provisioned)) — skipping component install"
  _OC_AMI_PROVISIONED=1
else
  log "step2: no provision marker — running provision-host.sh inline (plain-AMI path)"
  _OC_AMI_PROVISIONED=0
  bash /opt/openclaw/provision-host.sh
fi

# 1.5.0: per-host ed25519 key for host→guest SSH (private stays here, public
# injected into each VM by launch-vm.sh).
#
# fleet, so a key baked into one would let ANY host's private key SSH into ANY
# tenant's microVM on ANY host. provision never creates the key and its bake mode
# refuses to snapshot one; this side proves the key on disk belongs to THIS instance.
#
# The .instance marker records who generated it. Rules, and why they are safe:
#   marker matches this instance   -> adopt (normal reboot / re-run)
#   marker names another instance  -> inherited, unambiguously. Rotate.
#   marker absent + AMI-provisioned-> the key can only have come from the image
#                                     (a fresh golden boot has run nothing else).
#                                     Rotate, loudly.
#   marker absent + plain AMI      -> pre-#389v2 host: the key was necessarily made
#                                     by an earlier configure run on this same
#                                     instance, so adopt and backfill the marker.
#                                     Rotating here would break host→guest SSH for
#                                     microVMs already holding the public half.
# Rotation is the fail-closed action, not ABANDON: it removes the hazard instead of
# bricking the host, and a host that reaches this line has no tenant VMs yet.
_OC_KEY_INSTANCE_FILE=/etc/openclaw/host_vm_key.instance
if [ -f /etc/openclaw/host_vm_key ]; then
  _oc_key_owner="$(cat "${_OC_KEY_INSTANCE_FILE}" 2>/dev/null || echo "")"
  if [ "${_oc_key_owner}" = "${INSTANCE_ID}" ]; then
    log "host_vm_key belongs to this instance — keeping"
  elif [ -z "${_oc_key_owner}" ] && [ "${_OC_AMI_PROVISIONED}" = "0" ]; then
    log "host_vm_key predates the provenance marker on a plain-AMI host — adopting, backfilling marker"
    printf '%s\n' "${INSTANCE_ID}" > "${_OC_KEY_INSTANCE_FILE}"
  else
    log "SECURITY: host_vm_key was inherited (owner='${_oc_key_owner}' this='${INSTANCE_ID}' ami_provisioned=${_OC_AMI_PROVISIONED}) — rotating so no key is shared across hosts"
    rm -f /etc/openclaw/host_vm_key /etc/openclaw/host_vm_key.pub "${_OC_KEY_INSTANCE_FILE}"
  fi
fi
if [ ! -f /etc/openclaw/host_vm_key ]; then
  ssh-keygen -t ed25519 -N "" -C "openclaw-host-$(hostname)" -f /etc/openclaw/host_vm_key
  chmod 600 /etc/openclaw/host_vm_key
  printf '%s\n' "${INSTANCE_ID}" > "${_OC_KEY_INSTANCE_FILE}"
  log "host_vm_key generated for ${INSTANCE_ID}"
fi
chmod 600 /etc/openclaw/host_vm_key
chmod 644 "${_OC_KEY_INSTANCE_FILE}"

ARCH="$(uname -m)"
# Kept here (not only in provision) because step3b derives the guest-kernel path from it.
FC_VER="${FC_VERSION:-v1.15.1}"
command -v aws >/dev/null 2>&1 || { echo "[oc:init] FATAL: awscli absent after provision" > /dev/console; exit 1; }
[ -x /usr/local/bin/firecracker ] || { echo "[oc:init] FATAL: firecracker absent after provision" > /dev/console; exit 1; }
log "components ready: $(aws --version 2>&1 | head -1) / $(/usr/local/bin/firecracker --version 2>/dev/null | head -1)"

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
CPU_OVERCOMMIT_RATIO={{CPU_OVERCOMMIT_RATIO}}
MEM_OVERCOMMIT_RATIO={{MEM_OVERCOMMIT_RATIO}}
OVERCOMMIT_BY_FAMILY={{OVERCOMMIT_BY_FAMILY}}
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

# host-agent :8899/health, no proxy layer needed.

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
# AMP_REMOTE_WRITE_URL is unset (metrics.enabled: false in config.yml).
AMP_REMOTE_WRITE_URL="{{AMP_REMOTE_WRITE_URL}}"
if [ -n "${AMP_REMOTE_WRITE_URL}" ] && [ "${AMP_REMOTE_WRITE_URL}" != "none" ]; then
  log "step2b: configuring aws-otel-collector"
  # per-deployment part stays here — the config carries the AMP remote-write URL, which
  # differs per deployment and must never be baked into a shared AMI.
  dpkg -s aws-otel-collector >/dev/null 2>&1 || { echo "[oc:init] FATAL: aws-otel-collector absent after provision" > /dev/console; exit 1; }
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
#   mount        -> exit 32 "already mounted" on a re-run, and `set -e` turns that into
#                   ABANDON, so a host that merely re-ran configure got replaced.
#   >> /etc/fstab-> appended a duplicate UUID line on every run; enough reboots and the
#                   file is unreadable, and a stale duplicate can shadow the real device.
#   rm -rf       -> if /home/ubuntu/firecracker-assets were ever a real directory holding
#                   downloaded images instead of the symlink, this silently destroys them.
# Guard by observed state, not by assuming which boot this is.
if mountpoint -q /data; then
  log "/data already mounted from $(findmnt -no SOURCE /data)"
else
  mount ${DATA_DEV} /data
fi
DATA_UUID=$(blkid -s UUID -o value ${DATA_DEV})
if [ -z "${DATA_UUID}" ]; then
  echo "[oc:init] FATAL: no filesystem UUID on ${DATA_DEV}; refusing to write a blank fstab entry" > /dev/console; exit 1
fi
# Match on the UUID only: an existing entry with different options is still an entry for
# this device, and rewriting it on every boot would fight an operator's deliberate change.
if grep -q "UUID=${DATA_UUID}[[:space:]]" /etc/fstab; then
  log "fstab already has an entry for ${DATA_UUID}"
else
  echo "UUID=${DATA_UUID} /data ext4 defaults,nofail 0 2" >> /etc/fstab
  log "fstab entry added for ${DATA_UUID}"
fi
mkdir -p /data/firecracker-assets
chown ubuntu:ubuntu /data /data/firecracker-assets
# Only replace it when it is not already the symlink we want. Never rm -rf a real
# directory: if one exists here it holds multi-GB rootfs images someone downloaded.
if [ "$(readlink -f /home/ubuntu/firecracker-assets 2>/dev/null || echo "")" != "/data/firecracker-assets" ]; then
  if [ -d /home/ubuntu/firecracker-assets ] && [ ! -L /home/ubuntu/firecracker-assets ]; then
    log "WARN: /home/ubuntu/firecracker-assets is a real directory; moving it aside instead of deleting"
    mv -f /home/ubuntu/firecracker-assets "/home/ubuntu/firecracker-assets.pre-oc.$$"
  else
    rm -f /home/ubuntu/firecracker-assets
  fi
  ln -sfn /data/firecracker-assets /home/ubuntu/firecracker-assets
fi

# so the vm pipeline can tail /data/firecracker-vms/*/fc.log. Shared installer
# + host config pull from S3, same mechanism as edge. Config-gated: the shared
# script no-ops when LOGGING_ENABLED=false. Host has no baked fallback (no
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
# 非 tty → CPython 8KB 块缓冲 → 永不退出即永不 flush。reader 业务日志量更小,实测 4h43m 零业务
# 输出。关缓冲让每行即时进 journal。只加 Environment=,不碰下面的 systemd 沙箱收紧项。
Environment=PYTHONUNBUFFERED=1
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
# the kernel cannot be staged there at bake time; it lives on the root volume and is copied
# across here. This is the download the aarch64 404 killed, so on a golden AMI it is the
# single most valuable fetch to have already done.
if [ -s /opt/openclaw/baked/vmlinux ]; then
  install -o ubuntu -g ubuntu -m 0644 /opt/openclaw/baked/vmlinux ${ASSETS}/vmlinux
  log "guest kernel from baked AMI copy ($(stat -c %s ${ASSETS}/vmlinux) bytes)"
else
  curl -fsSL -o ${ASSETS}/vmlinux "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/${FC_MAJOR}/${ARCH}/${VMLINUX_NAME}"
  log "guest kernel downloaded (${VMLINUX_NAME}) — AMI had no baked copy"
fi
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
# launch-vm.sh 首次被调用前落地,否则 launch-vm 顶部 . lib/harden-config.sh 失败
# → 每次启动 exit 1 → 一台 host 起不来任何租户。走同一 _s3_get 重试骨架。
mkdir -p /home/ubuntu/lib
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/lib/harden-config.sh /home/ubuntu/lib/harden-config.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/lib/harden-config.sh /home/ubuntu/lib/harden-config.sh
# injected_credentials 时才 source;缺失即 fail-loud 中止该 VM 启动,不静默注入空凭据)。
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/lib/cred-inject.sh /home/ubuntu/lib/cred-inject.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/lib/cred-inject.sh /home/ubuntu/lib/cred-inject.sh
chmod +x /home/ubuntu/lib/harden-config.sh /home/ubuntu/lib/cred-inject.sh && chown -R ubuntu:ubuntu /home/ubuntu/lib
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/stop-vm.sh /home/ubuntu/stop-vm.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/stop-vm.sh /home/ubuntu/stop-vm.sh
chmod +x /home/ubuntu/stop-vm.sh && chown ubuntu:ubuntu /home/ubuntu/stop-vm.sh
# commands. This script owns the atomic overlay tombstone and op result.
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/rebuild-vm.sh /home/ubuntu/rebuild-vm.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/rebuild-vm.sh /home/ubuntu/rebuild-vm.sh
chmod +x /home/ubuntu/rebuild-vm.sh && chown ubuntu:ubuntu /home/ubuntu/rebuild-vm.sh
# image repin semantics.
aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/reset-vm.sh /home/ubuntu/reset-vm.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/reset-vm.sh /home/ubuntu/reset-vm.sh
chmod +x /home/ubuntu/reset-vm.sh && chown ubuntu:ubuntu /home/ubuntu/reset-vm.sh
# the matching API hits a missing /home/ubuntu/*.sh and fails exit 127.
# start-all-vms / stop-all-vms — host-local fan-out for the 1-minute fleet
# power goal: control plane sends ONE SSM per host, host starts/stops all its
# VMs in bounded parallel (SSM concurrency = host count, not VM count).
# 上方单独下载),聚合 SSM 命令调 `launch-vm.sh --manifest|--from-ddb ...`,此处不再拉它。
for _s in clone-data migrate-vm resize-disk start-all-vms stop-all-vms; do
  aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/${_s}.sh /home/ubuntu/${_s}.sh --region ${REGION} --no-progress 2>/dev/null \
    || _s3_get s3://${ASSETS_BUCKET}/deployment/scripts/${_s}.sh /home/ubuntu/${_s}.sh || true
  chmod +x /home/ubuntu/${_s}.sh && chown ubuntu:ubuntu /home/ubuntu/${_s}.sh
done
{{BACKUP_DATA_SCRIPT}}

# 原生 gateway(ALB LOR → OpenResty edge → Redis 查表 → host iptables DNAT →
# microVM:18789)。install-hub.sh + deploy/hub/ 源已归档到
# env 与 stack.py 模板替换、CloudFront /hub/* behavior、HubTargetGroup 已一并删除。

# Step 4b: AgentCore config (if enabled)
AGENTCORE_GW_URL="{{AGENTCORE_GATEWAY_URL}}"
if [ -n "${AGENTCORE_GW_URL}" ] && [ "${AGENTCORE_GW_URL}" != "none" ]; then
  echo "AGENTCORE_GATEWAY_URL=${AGENTCORE_GW_URL}" > /data/agentcore.env
  chown ubuntu:ubuntu /data/agentcore.env
  log "AgentCore config written: gateway=${AGENTCORE_GW_URL}"
fi

# disabled; enabled hooks are private-S3 downloaded, SHA256 verified, atomically
# installed and bounded by timeout before this host can register active.
{{HOST_USER_HOOK}}

# Step 5: register to DDB. Retry (concurrent launches throttle writes); on
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
#   ① 标称规格表按 instance_type 查(见下方 _NOMINAL_SPECS)
#   ② 落 DDB 供调度侧四级机型亲和排序(r8g>r7g>m8g>m7g)
# 用 _fresh_imds(现取 token,不复用上面可能已过期的 TOKEN —— 这里在耗时的
# _stack_output 之后)。取不到不 fail:标称表查不到会回落自报容量,且调度侧对空
# instance_type 有 fail-safe(affinity_tier 主键 mem_per_vcpu 由 total_* 算出,
# 缺机型只丢失同档内代际次序,R/M 大类判定仍正确)。
# DDB 的 S 类型不接受空字符串,故 IMDS 取不到时回落 "unknown"(而非空串)。
INSTANCE_TYPE=$(_fresh_imds meta-data/instance-type || true)
[ -n "${INSTANCE_TYPE}" ] || INSTANCE_TYPE="unknown"

_RES_VCPU={{HOST_RESERVED_VCPU}}
_RES_MEM={{HOST_RESERVED_MEM}}
_HOST_VCPU=$(nproc)
# MemTotal is in kB; convert to MB. Firecracker/host overhead is covered by the
# configured reserved_mem headroom, same as the old synth-time computation.
_HOST_MEM_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
_HOST_MEM_MB=$(( _HOST_MEM_KB / 1024 ))

#
# 为什么不用 nproc + /proc/meminfo 的实测值:那是【真机可用】容量,比标称小
# 1.8-1.9%(固件/硬件保留:r8g.metal-24xl 标称 768GiB 而 MemTotal 只有 754GiB),
# 再扣 reserved_* 之后,调度侧算出的 allocatable 就达不到按标称定义的容量目标
# (1c2G 口径:实测值只到 375,而标称理论值是 384)。若靠 per-family 补偿系数把这
# 段差补回去,就要为每个机型手算一个魔数 —— 每上一款新机型都得重算,且极易算错。
# 故改为查标称表:ratio 保持干净的 cpu=4.0 / mem=1.0,零系数。
#
# 代价(已知并接受):账本按标称记账,比机器真实可用多约 1.8%,故 host OS 与
# Firecracker 驻留内存不再由账本预留保护,改由 scheduling.mem_safety_floor_ratio
# 的物理水位门(读 host 自报的实测 MemAvailable)承担 —— 两者是一套,别单独关物理门。
#
# fail-safe:表里查不到本机机型(新机型未进 config 池 / size token 未知)时,回落
# nproc + /proc/meminfo 自报,保留 Phase 7 的混池安全性(小机型绝不冒充大机型),
# 绝不因表缺项就注册 0 或注册别人的容量。
_NOMINAL_SPECS="{{NOMINAL_SPECS}}"
_NOM_VCPU=""
_NOM_MEM=""
if [ -n "${INSTANCE_TYPE}" ] && [ "${INSTANCE_TYPE}" != "unknown" ]; then
  # 表是每行 "<instance_type> <vcpu> <mem_mb>";精确匹配第一列。
  _NOM_LINE=$(printf '%s\n' "${_NOMINAL_SPECS}" | awk -v t="${INSTANCE_TYPE}" '$1==t{print; exit}')
  if [ -n "${_NOM_LINE}" ]; then
    _NOM_VCPU=$(printf '%s' "${_NOM_LINE}" | awk '{print $2}')
    _NOM_MEM=$(printf '%s' "${_NOM_LINE}" | awk '{print $3}')
  fi
fi
if [ -n "${_NOM_VCPU}" ] && [ -n "${_NOM_MEM}" ] && [ "${_NOM_VCPU}" -gt 0 ] 2>/dev/null; then
  AVAIL_VCPU=${_NOM_VCPU}
  AVAIL_MEM=${_NOM_MEM}
  log "capacity: nominal ${INSTANCE_TYPE} ${AVAIL_VCPU}vcpu/${AVAIL_MEM}MB (measured ${_HOST_VCPU}vcpu/${_HOST_MEM_MB}MB; reserved_* not deducted — physical water-mark gate covers host overhead)"
else
  AVAIL_VCPU=$(( _HOST_VCPU - _RES_VCPU ))
  AVAIL_MEM=$(( _HOST_MEM_MB - _RES_MEM ))
  log "capacity: FALLBACK self-reported ${AVAIL_VCPU}vcpu/${AVAIL_MEM}MB (type='${INSTANCE_TYPE}' not in nominal table; measured ${_HOST_VCPU}/${_HOST_MEM_MB} minus reserved ${_RES_VCPU}/${_RES_MEM})"
fi
# Defensive: never register a non-positive capacity (would silently make the
# host unschedulable or, worse, wrap negative). Fail loud so the hook ABANDONs.
{ [ "${AVAIL_VCPU}" -gt 0 ] && [ "${AVAIL_MEM}" -gt 0 ]; } || {
  echo "[oc:init] FATAL: computed non-positive capacity vcpu=${AVAIL_VCPU} mem=${AVAIL_MEM} (host ${_HOST_VCPU}vcpu/${_HOST_MEM_MB}MB, reserved ${_RES_VCPU}/${_RES_MEM})" > /dev/console
  exit 1
}
log "step5: registering to DynamoDB (az=${AZ}, type=${INSTANCE_TYPE:-unknown}, capacity ${AVAIL_VCPU}vcpu/${AVAIL_MEM}MB from ${_HOST_VCPU}vcpu/${_HOST_MEM_MB}MB host)"
# 写成首启常量 0/0/0/1,于是第二次执行就把已有租户的记账抹掉:apse1 一台
# r8g.metal-24xl 实测,3 个租户 running、账本 3/6/12288/4,重跑同一份 asset(sha256
# 校验通过、退出码 0)后账本变 0/0/0/1,而 3 个 microVM 仍在跑。next_vm_num 从 4 退回 1
# 是同一次写:分配器会重新发出物理已占用的号,_phys_tap_occupied fail-closed 拒掉(每次
# lifecycle hook 因 bootstrap 超时 ABANDON 后 ASG 重试、未来任何 host 自愈重跑 bootstrap。
# 修法:首启走带 attribute_not_exists(instance_id) 的条件 put(整项创建,记账起点 0/0/0/1
# 只由这一次写),CCF 说明行已存在 → 改 update-item 只 SET 静态字段。账本四个字段
# (used_vcpu/used_mem_mb/vm_count/next_vm_num)在重跑路径上【不覆盖已有值】—— 它们的权威
# 是控制面的认领/释放 CAS,host 侧读回再写都不行:读与写之间落地的并发 create 会被抹掉。
# 静态字段仍要刷新(不能"行存在就跳过"):private_ip 跨 stop/start 会变、rootfs_version/
# snapshot_time 每次 bootstrap 都可能换、total_* 在标称表新增机型后会变、status 要把
# draining 的 host 拉回 active(重跑 bootstrap 正是为了让它重新可调度)。
#
# ★ 账本四字段必须用 if_not_exists 补,不能"一个字都不写"(2026-08-12 apse1 实测的真 bug):
# host-agent 在 init 途中就被 systemd 拉起(实测 06:02:38,而 step5 在 06:05:19,早 2 分 41 秒),
# 它的心跳是【无条件 update_item】(host-agent.py:895 SET last_seen=...),而 DDB 的
# update_item 对不存在的 key 会【创建行】。于是 host-agent 先建出一行只有心跳字段的记录,
# init 的条件 put 必然 CCF → 走这个 update 分支 → 若只写静态字段,四个记账字段就【永不创建】。
# 后果:apse1 实测 status=active(调度器会选它)但 used_vcpu/vm_count/
# next_vm_num 全不存在 → _reserve_slot 的 CAS 条件 `next_vm_num = :expected AND
# used_vcpu <= :cap_v` 恒假 → 每次分配 CCF → exclude 排除 → 503。每台新 host 都会这样,
# if_not_exists 的语义正是"有就不动、没有才补",同时满足"不覆盖已有记账"与"不留缺字段的行"。
# 同款范式见 health_check/handler.py:426。
_registered=0
for _r in $(seq 1 10); do
  # stderr 收进变量而不是临时文件:CCF 是本分支的正常信号,得判定;真错误得进日志。用固定
  # 路径的 tmp 文件还要防符号链接和清理,变量没这些面。
  _reg_err=$(aws dynamodb put-item --table-name ${HOSTS_TABLE} --region ${REGION} --condition-expression 'attribute_not_exists(instance_id)' --item '{"instance_id":{"S":"'${INSTANCE_ID}'"},"instance_type":{"S":"'${INSTANCE_TYPE}'"},"private_ip":{"S":"'${PRIVATE_IP}'"},"az":{"S":"'${AZ}'"},"total_vcpu":{"N":"'${AVAIL_VCPU}'"},"total_mem_mb":{"N":"'${AVAIL_MEM}'"},"used_vcpu":{"N":"0"},"used_mem_mb":{"N":"0"},"vm_count":{"N":"0"},"next_vm_num":{"N":"1"},"status":{"S":"active"},"rootfs_version":{"S":"'${ROOTFS_VER}'"},"snapshot_time":{"S":"'${ROOTFS_VER}'"}}' 2>&1) \
    && { _registered=1; log "registered (first boot: ledger starts at 0/0/0/1)"; break; }
  # CCF = 本实例已注册过(重跑)。只刷静态字段,账本原封不动。其它错误(限流/权限/网络)
  # 落到下面的重试,不能与"已存在"混为一谈 —— 那会把真失败当成功。
  case "${_reg_err}" in
    *ConditionalCheckFailedException*)
      aws dynamodb update-item --table-name ${HOSTS_TABLE} --region ${REGION} \
        --key '{"instance_id":{"S":"'${INSTANCE_ID}'"}}' \
        --condition-expression 'attribute_exists(instance_id)' \
        --update-expression 'SET instance_type = :it, private_ip = :ip, az = :az, total_vcpu = :tv, total_mem_mb = :tm, #s = :st, rootfs_version = :rv, snapshot_time = :sv, used_vcpu = if_not_exists(used_vcpu, :zero), used_mem_mb = if_not_exists(used_mem_mb, :zero), vm_count = if_not_exists(vm_count, :zero), next_vm_num = if_not_exists(next_vm_num, :one)' \
        --expression-attribute-names '{"#s":"status"}' \
        --expression-attribute-values '{":it":{"S":"'${INSTANCE_TYPE}'"},":ip":{"S":"'${PRIVATE_IP}'"},":az":{"S":"'${AZ}'"},":tv":{"N":"'${AVAIL_VCPU}'"},":tm":{"N":"'${AVAIL_MEM}'"},":st":{"S":"active"},":rv":{"S":"'${ROOTFS_VER}'"},":sv":{"S":"'${ROOTFS_VER}'"},":zero":{"N":"0"},":one":{"N":"1"}}' \
        && { _registered=1; log "re-run: refreshed static fields; ledger preserved (missing counters seeded)"; break; }
      ;;
    *) log "register attempt $_r error: ${_reg_err}" ;;
  esac
  log "register attempt $_r failed, retrying in 15s..."
  sleep 15
done
[ "$_registered" -eq 1 ] || { log "ERROR: DDB registration failed after 10 attempts — ABANDON"; exit 1; }

# Lifecycle hook is settled by the EXIT trap (_complete_hook) — CONTINUE on success, ABANDON on any failure.
log "DONE host ready (total $((SECONDS))s)"
