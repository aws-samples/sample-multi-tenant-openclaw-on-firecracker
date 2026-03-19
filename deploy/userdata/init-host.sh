set -e
exec > /var/log/openclaw-init.log 2>&1
log() { echo "[oc:init] $(date +%H:%M:%S) $*"; }
log "Starting host setup..."

TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
PRIVATE_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/local-ipv4)

# Step 1: KVM
log "step1: KVM setup"
chmod 666 /dev/kvm
echo 'KERNEL=="kvm", MODE="0666"' > /etc/udev/rules.d/99-kvm.rules

# Step 2: Install tools + Firecracker
log "step2: installing tools + firecracker"
apt-get update -qq
apt-get install -y -qq curl jq sshpass unzip pigz > /dev/null 2>&1
if ! command -v aws &>/dev/null; then
  curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  cd /tmp && unzip -qo awscliv2.zip && ./aws/install &>/dev/null; cd -
fi
ARCH="$(uname -m)"
FC_URL="https://github.com/firecracker-microvm/firecracker/releases"
FC_VER=$(basename $(curl -fsSLI -o /dev/null -w %{url_effective} ${FC_URL}/latest))
curl -sL ${FC_URL}/download/${FC_VER}/firecracker-${FC_VER}-${ARCH}.tgz | tar -xz
mv release-${FC_VER}-${ARCH}/firecracker-${FC_VER}-${ARCH} /usr/local/bin/firecracker
mv release-${FC_VER}-${ARCH}/jailer-${FC_VER}-${ARCH} /usr/local/bin/jailer
rm -rf release-${FC_VER}-${ARCH}
log "firecracker ${FC_VER} installed"

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
  aws s3 cp ${MANIFEST_URL} ${ASSETS}/manifest.json --region ${REGION} 2>/dev/null && break
  log "manifest.json not found, retrying in 30s ($i/20)..."
  sleep 30
done
[ -f ${ASSETS}/manifest.json ] || { log "ERROR: manifest.json not available after 10min"; exit 1; }
eval $(python3 -c "
import json; m=json.load(open('${ASSETS}/manifest.json'))
print(f'ROOTFS_KEY={m[\"rootfs\"]}')
print(f'DATA_KEY={m[\"data_template\"]}')
print(f'ROOTFS_VER={m[\"version\"]}')
")
aws s3 cp s3://{{ASSETS_BUCKET}}/{{ROOTFS_PREFIX}}/${ROOTFS_KEY} ${ASSETS}/rootfs.gz --region ${REGION}
aws s3 cp s3://{{ASSETS_BUCKET}}/{{ROOTFS_PREFIX}}/${DATA_KEY} ${ASSETS}/data.gz --region ${REGION}
pigz -dc ${ASSETS}/rootfs.gz > ${ASSETS}/openclaw-rootfs.ext4 && rm -f ${ASSETS}/rootfs.gz
pigz -dc ${ASSETS}/data.gz > ${ASSETS}/openclaw-data-template.ext4 && rm -f ${ASSETS}/data.gz
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
{{LAUNCH_VM_SCRIPT}}
{{STOP_VM_SCRIPT}}
{{BACKUP_DATA_SCRIPT}}

# Step 5: Self-register to DynamoDB
log "step5: registering to DynamoDB"
aws dynamodb put-item --table-name {{HOSTS_TABLE}} --region ${REGION} --item '{"instance_id":{"S":"'${INSTANCE_ID}'"},"private_ip":{"S":"'${PRIVATE_IP}'"},"total_vcpu":{"N":"{{AVAIL_VCPU}}"},"total_mem_mb":{"N":"{{AVAIL_MEM}}"},"used_vcpu":{"N":"0"},"used_mem_mb":{"N":"0"},"vm_count":{"N":"0"},"next_vm_num":{"N":"1"},"status":{"S":"active"},"rootfs_version":{"S":"'${ROOTFS_VER}'"}}'

# Step 6: Complete lifecycle hook
log "step6: completing lifecycle hook"
aws autoscaling complete-lifecycle-action --lifecycle-hook-name openclaw-host-init \
  --auto-scaling-group-name openclaw-hosts-asg --lifecycle-action-result CONTINUE \
  --instance-id ${INSTANCE_ID} --region ${REGION} || true

log "DONE host ready (total $((SECONDS))s)"
