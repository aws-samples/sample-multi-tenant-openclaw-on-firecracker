#!/usr/bin/env bash
# 给 LiteLLM 宿主（堡垒机）的 SG 开 4000 入站，且只对 VPC CIDR（绝不 0.0.0.0/0）。
# guest microVM 经 metal host 用私网 IP 访问 litellm:4000，所以源限 VPC CIDR 足够。
#
# 用法：./open-sg-4000.sh            # 自动取本机 SG + VPC CIDR，幂等加规则
#       VPC_CIDR=172.31.0.0/16 ./open-sg-4000.sh   # 手动指定
#
# 安全红线（违反即事故）：4000 入站源只能是 VPC CIDR 或 metal host SG，
#   绝不 0.0.0.0/0。本脚本硬拒任何 0.0.0.0/0 入参。
set -euo pipefail

REGION="${AWS_REGION:-ap-southeast-1}"
PORT=4000

# IMDSv2 取本机 instance-id + SG + VPC CIDR
TOK="$(curl -s --max-time 3 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 180')"
imds() { curl -s -H "X-aws-ec2-metadata-token: $TOK" "http://169.254.169.254/latest/meta-data/$1"; }

IID="$(imds instance-id)"
MAC="$(imds network/interfaces/macs/ | head -1)"
DETECTED_CIDR="$(imds network/interfaces/macs/${MAC}vpc-ipv4-cidr-block)"
VPC_CIDR="${VPC_CIDR:-$DETECTED_CIDR}"

if [ "$VPC_CIDR" = "0.0.0.0/0" ]; then
  echo "[sg][ERR] 拒绝：4000 入站不允许 0.0.0.0/0（安全红线）。" >&2; exit 1
fi

SG_ID="$(aws ec2 describe-instances --instance-ids "$IID" --region "$REGION" \
  --query 'Reservations[].Instances[].SecurityGroups[0].GroupId' --output text)"

echo "[sg] instance=$IID sg=$SG_ID vpc_cidr=$VPC_CIDR region=$REGION"

# 幂等：已存在该规则就跳过
EXISTS="$(aws ec2 describe-security-groups --group-ids "$SG_ID" --region "$REGION" \
  --query "SecurityGroups[].IpPermissions[?FromPort==\`$PORT\` && ToPort==\`$PORT\`].IpRanges[?CidrIp=='$VPC_CIDR'].CidrIp" \
  --output text)"
if [ -n "$EXISTS" ]; then
  echo "[sg] 规则已存在（tcp/$PORT <- $VPC_CIDR），跳过。"
  exit 0
fi

aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --region "$REGION" \
  --ip-permissions "IpProtocol=tcp,FromPort=$PORT,ToPort=$PORT,IpRanges=[{CidrIp=$VPC_CIDR,Description='LiteLLM 4000 - VPC internal only (guest microVM via metal host)'}]"

echo "[sg] 已加：tcp/$PORT 入站 <- $VPC_CIDR（VPC 内部，非 0.0.0.0/0）。"
