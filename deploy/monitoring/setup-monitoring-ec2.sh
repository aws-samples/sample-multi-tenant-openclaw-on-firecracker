#!/usr/bin/env bash
# 一键起一台监控 EC2 跑自建 Prometheus + Grafana(替代 AMP/AMG,免 SSO)。
#
# 做什么:
#   ① 建一个独立 SG —— 入站只对 VPC CIDR(或你给的 ALLOW_CIDR)开 9090/3000,
#      绝不 0.0.0.0/0(硬红线)。② 建 instance role(ec2:DescribeInstances 只读,
#      给 Prometheus ec2_sd 发现 metal host;+ S3 只读拉监控资产)。③ 起一台
#      AL2023 EC2,userdata 装 docker+compose、从 S3 assets 拉本目录资产、
#      用 openssl rand 现生成 Grafana admin 密码写 600 .env、compose up。
#
# 前置: ① aws CLI 已配好目标账号/region 凭据 ② 本目录的资产已上传到
#   s3://$ASSETS_BUCKET/$ASSETS_PREFIX/(setup.sh 会传;或手动 aws s3 sync)。
#
# 用法:
#   VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ASSETS_BUCKET=openclaw-assets-<acct> \
#     bash setup-monitoring-ec2.sh
# 可选 env:
#   REGION(默认 ap-southeast-1) INSTANCE_TYPE(默认 c7i.large)
#   ALLOW_CIDR(默认 = VPC CIDR;可设为办公/VPN /32,但绝不 0.0.0.0/0)
#   ASSETS_PREFIX(默认 deployment/monitoring) KEY_NAME(可选 SSH key)
set -euo pipefail

REGION="${REGION:-ap-southeast-1}"
INSTANCE_TYPE="${INSTANCE_TYPE:-c7i.large}"
ASSETS_PREFIX="${ASSETS_PREFIX:-deployment/monitoring}"
: "${VPC_ID:?need VPC_ID (same VPC as metal hosts so :8899 is reachable)}"
: "${SUBNET_ID:?need SUBNET_ID (a subnet in VPC_ID)}"
: "${ASSETS_BUCKET:?need ASSETS_BUCKET (s3 bucket holding monitoring assets)}"

# ---- 解析放行 CIDR:默认用 VPC CIDR;绝不 0.0.0.0/0 ----
VPC_CIDR="$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$REGION" \
  --query 'Vpcs[0].CidrBlock' --output text)"
ALLOW_CIDR="${ALLOW_CIDR:-$VPC_CIDR}"
if [ "$ALLOW_CIDR" = "0.0.0.0/0" ]; then
  echo "FATAL: ALLOW_CIDR=0.0.0.0/0 违反暴露红线,拒绝执行。" >&2
  echo "       Grafana/Prometheus 入站只能对 VPC CIDR 或办公/VPN IP 开。" >&2
  exit 1
fi
echo ">> region=$REGION vpc=$VPC_ID subnet=$SUBNET_ID allow_cidr=$ALLOW_CIDR"

# ---- ① SG:入站只对 ALLOW_CIDR 开 9090/3000 ----
SG_ID="$(aws ec2 create-security-group --region "$REGION" \
  --group-name "openclaw-prom-grafana-$(date +%s)" \
  --description "OpenClaw Prometheus+Grafana monitoring — no 0.0.0.0" \
  --vpc-id "$VPC_ID" --query 'GroupId' --output text)"
echo ">> created SG $SG_ID"
for PORT in 9090 3000; do
  aws ec2 authorize-security-group-ingress --region "$REGION" \
    --group-id "$SG_ID" --protocol tcp --port "$PORT" --cidr "$ALLOW_CIDR" \
    >/dev/null
  echo ">> ingress tcp/$PORT <- $ALLOW_CIDR"
done
# 注:不开任何对 0.0.0.0/0 的入站;SSH 如需调试请单独加办公 IP/堡垒 SG。

# ---- ② IAM role:ec2:DescribeInstances(ec2_sd 发现)+ S3 只读(拉资产) ----
ROLE="openclaw-monitoring-ec2"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  aws iam put-role-policy --role-name "$ROLE" --policy-name prom-ec2-discovery \
    --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ec2:DescribeInstances","ec2:DescribeAvailabilityZones"],"Resource":"*"}]}' \
    >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null
  aws iam create-instance-profile --instance-profile-name "$ROLE" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" \
    --role-name "$ROLE" >/dev/null
  # S3 只读 — 仅拉本桶的监控资产
  aws iam put-role-policy --role-name "$ROLE" --policy-name s3-assets-read \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::${ASSETS_BUCKET}\",\"arn:aws:s3:::${ASSETS_BUCKET}/*\"]}]}" \
    >/dev/null
  echo ">> created role+profile $ROLE; sleep 10s for IAM propagation"; sleep 10
fi

# ---- ③ userdata:装 docker+compose,拉资产,生成 admin 密码,compose up ----
# Grafana admin 密码用 openssl rand 现生成(同 wazuh 套路),不硬编码、不进 git。
UD="$(cat <<EOF | base64
#!/bin/bash
set -euxo pipefail
dnf install -y docker || yum install -y docker
systemctl enable --now docker
curl -sSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-\$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose
mkdir -p /opt/monitoring/grafana
cd /opt/monitoring
# 从 S3 拉本目录全部资产(setup.sh 已 sync 到 \$ASSETS_PREFIX/)
for i in \$(seq 1 30); do aws s3 sync s3://${ASSETS_BUCKET}/${ASSETS_PREFIX}/ /opt/monitoring/ --region ${REGION} && break || sleep 10; done
# 强随机 Grafana admin 密码,写 600 .env(绝不硬编码)
echo "GRAFANA_ADMIN_PASSWORD=\$(openssl rand -base64 24)" > /opt/monitoring/.env
chmod 600 /opt/monitoring/.env
cd /opt/monitoring && /usr/local/bin/docker-compose --env-file .env -f docker-compose.prom-grafana.yml up -d
EOF
)"

AMI_ID="$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameter.Value' --output text)"

RUN_ARGS=(--region "$REGION" --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE"
  --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID"
  --iam-instance-profile "Name=$ROLE" --user-data "$UD"
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=50,Encrypted=true}'
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=openclaw-monitoring},{Key=Project,Value=openclaw},{Key=Role,Value=monitoring}]')
[ -n "${KEY_NAME:-}" ] && RUN_ARGS+=(--key-name "$KEY_NAME")

IID="$(aws ec2 run-instances "${RUN_ARGS[@]}" \
  --query 'Instances[0].InstanceId' --output text)"
echo ">> launched monitoring EC2: $IID ($INSTANCE_TYPE, AL2023)"
echo ">> SG=$SG_ID ingress 9090/3000 <- $ALLOW_CIDR only (no 0.0.0.0/0)"
echo ">> 等约 2-3 分钟 userdata 跑完。验证见 MONITORING-RUNBOOK.md §4/§5。"
echo ">> 取 Grafana admin 密码(经堡垒/SSM): sudo cat /opt/monitoring/.env"
echo ">> 私网访问(推荐 SSH 隧道,不暴露公网): "
echo "   ssh -L 3000:<monitoring-private-ip>:3000 -L 9090:<monitoring-private-ip>:9090 ..."
