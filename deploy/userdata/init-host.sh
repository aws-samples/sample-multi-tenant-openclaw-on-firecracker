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
if ! command -v aws &>/dev/null; then
  curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  cd /tmp && unzip -qo awscliv2.zip && ./aws/install &>/dev/null; cd -
fi
ARCH="$(uname -m)"
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

# Write env for launch-vm.sh and host-agent
cat > /etc/platform.env << ENVEOF
OC_REGION=${REGION}
ASSETS_BUCKET={{ASSETS_BUCKET}}
TENANTS_TABLE=${TENANTS_TABLE}
HOSTS_TABLE=${HOSTS_TABLE}
INSTANCE_ID=${INSTANCE_ID}
SUBNET_PREFIX={{SUBNET_PREFIX}}
# Used by launch-vm.sh to deny guest→fleet east-west traffic. The host's own
# :80 is open to the whole VPC CIDR (the ALB health check needs it), and a
# guest's egress is MASQUERADEd to its host's private IP — so without an
# explicit deny a tenant can curl any other host's nginx and read other
# tenants' dashboards. In-VPC :80 is only ever our own hosts/ALB (interface
# endpoints answer on 443), so denying it costs the guest nothing.
OC_VPC_CIDR={{VPC_CIDR}}
ROOTFS_OVERLAY_MB={{ROOTFS_OVERLAY_MB}}
BALLOON_ENABLED={{BALLOON_ENABLED}}
BALLOON_DEFLATE_ON_OOM={{BALLOON_DEFLATE_ON_OOM}}
BALLOON_STATS_INTERVAL={{BALLOON_STATS_INTERVAL}}
BALLOON_FREE_PAGE_REPORTING={{BALLOON_FREE_PAGE_REPORTING}}
BALLOON_MAX_INFLATE_RATIO={{BALLOON_MAX_INFLATE_RATIO}}
BALLOON_MIN_GUEST_AVAILABLE_MB={{BALLOON_MIN_GUEST_AVAILABLE_MB}}
ENVEOF

# Nginx reverse proxy for tenant dashboards
mkdir -p /etc/nginx/conf.d/tenants
# T3-1: peer-map confs for tenants owned by OTHER hosts (populated by
# host-agent._sync_tenant_routes). Empty today → the include is a no-op.
mkdir -p /etc/nginx/conf.d/tenant-peers
cat > /etc/nginx/conf.d/openclaw-proxy.conf <<'NGINX'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
# Public server (ALB → :80). Serves local tenants (conf.d/tenants) AND, via the
# T3-1 peer-map (conf.d/tenant-peers), forwards /vm/<tid> for tenants owned by
# other hosts to that host's internal :8081. `tenant-peers` sorts before
# `tenants` in the glob, but a tenant is EITHER local or peered, never both
# (host-agent skips local tids), so include order is immaterial.
# NOTE: there is deliberately NO `/agent/health` proxy here. host-agent's
# /health enumerates every tenant on this host (ids, health, metrics), and this
# server block is reachable from the internet via the ALB — exposing it handed
# out the fleet's tenant inventory to any unauthenticated caller. host-agent now
# binds 127.0.0.1 only; reach it over SSM/SSH when debugging a host.
server {
    listen 80 default_server;
    location /health { return 200 'ok'; add_header Content-Type text/plain; }
    # Readiness, as opposed to liveness. `/health` is a constant 200 from the
    # moment nginx starts — which is BEFORE the rootfs download (minutes) and
    # before host-agent's first peer-map sync. Under host-tg routing the shared
    # target group would therefore start sending a brand-new host its 1/N share
    # of /vm/* traffic while it has no peer routes at all, and 404 all of it:
    # every ASG scale-out became a planned partial outage.
    #
    # host-agent creates /run/openclaw/ready only after a peer-map generation
    # is fully applied. /run is tmpfs, so a reboot or replacement correctly
    # starts out NOT ready. This covers cold start; a host-agent that dies
    # while running is covered by the openclaw_peer_map_last_success_age_seconds
    # metric (flapping the host out of the TG on a transient DDB throttle would
    # do more harm than the staleness it detects).
    location = /ready {
        root /run/openclaw;
        try_files /ready =503;
        add_header Content-Type text/plain;
    }
    include /etc/nginx/conf.d/tenant-peers/*.conf;
    include /etc/nginx/conf.d/tenants/*.conf;
}
# T3-1 internal server (host↔host, :8081). Serves ONLY this host's local
# tenants — deliberately NO tenant-peers include, so a peered request can never
# be re-proxied onward: ALB→ownerless-host:80(peer conf)→owner:8081→local VM is
# capped at 2 hops by construction. SG allows :8081 only from the host fleet.
server {
    listen 8081;
    location /health { return 200 'ok'; add_header Content-Type text/plain; }
    include /etc/nginx/conf.d/tenants/*.conf;
}
NGINX
rm -f /etc/nginx/sites-enabled/default
systemctl enable nginx
systemctl restart nginx
log "nginx proxy configured"

# Host agent — probes all local VMs, writes health to DynamoDB
{{HOST_AGENT_SCRIPT}}
mkdir -p /opt/openclaw
aws s3 cp s3://{{ASSETS_BUCKET}}/deployment/scripts/host-agent.py /opt/openclaw/host-agent.py --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://{{ASSETS_BUCKET}}/deployment/scripts/host-agent.py /opt/openclaw/host-agent.py
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
  aws s3 cp s3://{{ASSETS_BUCKET}}/deployment/scripts/adot-config.yaml \
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
curl -fsSL -o ${ASSETS}/vmlinux "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/${FC_MAJOR}/${ARCH}/vmlinux-5.10.245-no-acpi"
MANIFEST_URL="s3://{{ASSETS_BUCKET}}/{{ROOTFS_PREFIX}}/manifest.json"
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
print(f'ROOTFS_VER={m[\"version\"]}')
")
aws s3 cp s3://{{ASSETS_BUCKET}}/{{ROOTFS_PREFIX}}/${ROOTFS_KEY} ${ASSETS}/rootfs.gz --region ${REGION} --no-progress
aws s3 cp s3://{{ASSETS_BUCKET}}/{{ROOTFS_PREFIX}}/${DATA_KEY} ${ASSETS}/data.gz --region ${REGION} --no-progress
pigz -dc ${ASSETS}/rootfs.gz > ${ASSETS}/openclaw-rootfs.ext4 && rm -f ${ASSETS}/rootfs.gz
pigz -dc ${ASSETS}/data.gz > ${ASSETS}/openclaw-data-template.ext4 && rm -f ${ASSETS}/data.gz
fallocate --dig-holes ${ASSETS}/openclaw-data-template.ext4
chown -R ubuntu:ubuntu ${ASSETS}
log "assets downloaded: rootfs=${ROOTFS_VER} ($((SECONDS-T0))s)"

# Step 3c: Sync shared skills from S3
log "step3c: syncing shared skills"
mkdir -p /data/shared-skills
aws s3 sync s3://{{ASSETS_BUCKET}}/skills/ /data/shared-skills/ --region ${REGION} 2>/dev/null || true
chown -R ubuntu:ubuntu /data/shared-skills
# Cron job to sync skills every 5 minutes
echo "*/5 * * * * root aws s3 sync s3://{{ASSETS_BUCKET}}/skills/ /data/shared-skills/ --region ${REGION} 2>/dev/null" > /etc/cron.d/openclaw-skills-sync
log "shared skills ready ($(ls /data/shared-skills/ 2>/dev/null | wc -l) skills)"

# Step 4: Deploy launch/stop scripts
log "step4: deploying scripts"
aws s3 cp s3://{{ASSETS_BUCKET}}/deployment/scripts/launch-vm.sh /home/ubuntu/launch-vm.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://{{ASSETS_BUCKET}}/deployment/scripts/launch-vm.sh /home/ubuntu/launch-vm.sh
chmod +x /home/ubuntu/launch-vm.sh && chown ubuntu:ubuntu /home/ubuntu/launch-vm.sh
aws s3 cp s3://{{ASSETS_BUCKET}}/deployment/scripts/stop-vm.sh /home/ubuntu/stop-vm.sh --region ${REGION} --no-progress 2>/dev/null || \
  _s3_get s3://{{ASSETS_BUCKET}}/deployment/scripts/stop-vm.sh /home/ubuntu/stop-vm.sh
chmod +x /home/ubuntu/stop-vm.sh && chown ubuntu:ubuntu /home/ubuntu/stop-vm.sh
# clone(#12) / migrate(#64) / resize(#22) helpers — all must reach the host or
# the matching API hits a missing /home/ubuntu/*.sh and fails exit 127.
for _s in clone-data migrate-vm resize-disk; do
  aws s3 cp s3://{{ASSETS_BUCKET}}/deployment/scripts/${_s}.sh /home/ubuntu/${_s}.sh --region ${REGION} --no-progress 2>/dev/null \
    || _s3_get s3://{{ASSETS_BUCKET}}/deployment/scripts/${_s}.sh /home/ubuntu/${_s}.sh || true
  chmod +x /home/ubuntu/${_s}.sh && chown ubuntu:ubuntu /home/ubuntu/${_s}.sh
done
{{BACKUP_DATA_SCRIPT}}

# Step 4b: AgentCore config (if enabled)
AGENTCORE_GW_URL="{{AGENTCORE_GATEWAY_URL}}"
if [ -n "${AGENTCORE_GW_URL}" ] && [ "${AGENTCORE_GW_URL}" != "none" ]; then
  echo "AGENTCORE_GATEWAY_URL=${AGENTCORE_GW_URL}" > /data/agentcore.env
  chown ubuntu:ubuntu /data/agentcore.env
  log "AgentCore config written: gateway=${AGENTCORE_GW_URL}"
fi

# Step 5: register to DDB. Retry (concurrent launches throttle writes); on
# total failure exit non-zero → trap ABANDONs (unregistered host is useless) (#73)
log "step5: registering to DynamoDB (az=${AZ})"
_registered=0
for _r in $(seq 1 10); do
  aws dynamodb put-item --table-name ${HOSTS_TABLE} --region ${REGION} --item '{"instance_id":{"S":"'${INSTANCE_ID}'"},"private_ip":{"S":"'${PRIVATE_IP}'"},"az":{"S":"'${AZ}'"},"total_vcpu":{"N":"{{AVAIL_VCPU}}"},"total_mem_mb":{"N":"{{AVAIL_MEM}}"},"used_vcpu":{"N":"0"},"used_mem_mb":{"N":"0"},"vm_count":{"N":"0"},"next_vm_num":{"N":"1"},"status":{"S":"active"},"rootfs_version":{"S":"'${ROOTFS_VER}'"}}' && { _registered=1; break; }
  log "register attempt $_r failed, retrying in 15s..."
  sleep 15
done
[ "$_registered" -eq 1 ] || { log "ERROR: DDB registration failed after 10 attempts — ABANDON"; exit 1; }

# Lifecycle hook is settled by the EXIT trap (_complete_hook) — CONTINUE on success, ABANDON on any failure.
log "DONE host ready (total $((SECONDS))s)"
