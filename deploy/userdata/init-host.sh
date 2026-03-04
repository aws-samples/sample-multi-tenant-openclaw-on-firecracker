set -e
exec > /var/log/openclaw-init.log 2>&1
echo '[openclaw] Starting host setup...'

TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
PRIVATE_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/local-ipv4)

# Step 1: KVM
chmod 666 /dev/kvm
echo 'KERNEL=="kvm", MODE="0666"' > /etc/udev/rules.d/99-kvm.rules

# Step 2: Install tools + Firecracker
apt-get update -qq
apt-get install -y -qq curl jq sshpass unzip > /dev/null 2>&1
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
if [ -z "$DATA_DEV" ]; then echo "ERROR: data volume not found"; exit 1; fi
echo "[openclaw] Data volume: ${DATA_DEV}"
if ! blkid ${DATA_DEV} | grep -q ext4; then mkfs.ext4 -q ${DATA_DEV}; fi
mkdir -p /data
mount ${DATA_DEV} /data
echo "${DATA_DEV} /data ext4 defaults,nofail 0 2" >> /etc/fstab
mkdir -p /data/firecracker-assets
chown ubuntu:ubuntu /data /data/firecracker-assets
rm -rf /home/ubuntu/firecracker-assets
ln -sfn /data/firecracker-assets /home/ubuntu/firecracker-assets

# Tag data volume
DATA_VOL_ID=$(aws ec2 describe-volumes --filters Name=attachment.instance-id,Values=${INSTANCE_ID} Name=attachment.device,Values=/dev/sdf --query 'Volumes[0].VolumeId' --output text --region ${REGION})
aws ec2 create-tags --resources ${DATA_VOL_ID} --tags Key=Name,Value=openclaw-data-${INSTANCE_ID} Key=openclaw:role,Value=host-data --region ${REGION}

# Step 3b: Kernel + rootfs from S3 (downloads directly to data volume via symlink)
ASSETS=/home/ubuntu/firecracker-assets
FC_MAJOR=$(echo ${FC_VER} | grep -oP "v\d+\.\d+")
curl -fsSL -o ${ASSETS}/vmlinux "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/${FC_MAJOR}/${ARCH}/vmlinux-5.10.245-no-acpi"
aws s3 cp s3://{{ASSETS_BUCKET}}/{{ROOTFS_PREFIX}}/{{ROOTFS_FILENAME}} ${ASSETS}/openclaw-rootfs.ext4 --region ${REGION}
aws s3 cp s3://{{ASSETS_BUCKET}}/{{ROOTFS_PREFIX}}/{{DATA_TEMPLATE_FILENAME}} ${ASSETS}/openclaw-data-template.ext4 --region ${REGION}
chown -R ubuntu:ubuntu ${ASSETS}

# Step 4: Deploy launch/stop scripts
{{LAUNCH_VM_SCRIPT}}
{{STOP_VM_SCRIPT}}

# Step 5: Self-register to DynamoDB
aws dynamodb put-item --table-name {{HOSTS_TABLE}} --region ${REGION} --item '{"instance_id":{"S":"'${INSTANCE_ID}'"},"private_ip":{"S":"'${PRIVATE_IP}'"},"total_vcpu":{"N":"{{AVAIL_VCPU}}"},"total_mem_mb":{"N":"{{AVAIL_MEM}}"},"used_vcpu":{"N":"0"},"used_mem_mb":{"N":"0"},"vm_count":{"N":"0"},"next_vm_num":{"N":"1"},"status":{"S":"active"}}'

# Step 6: Complete lifecycle hook
aws autoscaling complete-lifecycle-action --lifecycle-hook-name openclaw-host-init \
  --auto-scaling-group-name openclaw-hosts-asg --lifecycle-action-result CONTINUE \
  --instance-id ${INSTANCE_ID} --region ${REGION} || true

echo '[openclaw] Host ready!'
