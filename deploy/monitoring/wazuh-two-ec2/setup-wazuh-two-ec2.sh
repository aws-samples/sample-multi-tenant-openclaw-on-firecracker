#!/bin/bash
# Two-EC2 Wazuh deployment, security-hardened version of the reference article
#   "Setting Up Wazuh on AWS: Two EC2 Instances, Real-Time File Monitoring..."
#   (Nikhil Shakya, Apr 2026).
#
# Differences from the article — all to hold the project's SG red line:
#   - Article opens 22/80/443/1514/1515 to 0.0.0.0/0 (it's a throwaway lab).
#     Here NOTHING is open to 0.0.0.0/0:
#       * 1514/1515 (agent comms + enrollment, TCP+UDP)  <- agent SG only
#       * 55000 (API), 443/5601 (dashboard), 22 (SSH)     <- bastion SG only
#   - Both instances run in a PRIVATE subnet (no public IP). Egress for package
#     install goes through the VPC NAT gateway. Dashboard is reached over an SSH
#     tunnel through the bastion, not by opening it to the internet.
#   - Agent points at the manager's PRIVATE IP (stable inside the VPC), so the
#     article's "new public IP after restart -> agent Unknown" failure mode and
#     its Elastic IP fix do not apply.
#
# Run this FROM THE BASTION (it has admin + aws at /usr/local/bin/aws).
# Verified end-to-end on a demo account / region, 2026-06-30.
#
# Real values (instance IDs, IPs, generated admin password) are NOT committed;
# they are emitted to stdout / /tmp at run time. Edit the CONFIG block per env.
set -euo pipefail

# ---------- CONFIG (per environment; defaults = verified 795 ap-southeast-1) ----------
REGION="${REGION:-ap-southeast-1}"
VPC_ID="${VPC_ID:-vpc-0b308aa094fbf39e5}"
VPC_CIDR="${VPC_CIDR:-172.31.0.0/16}"
# Private subnet that routes egress via NAT (no public IP on instances):
PRIVATE_SUBNET="${PRIVATE_SUBNET:-subnet-09a74f97b5b6f8f09}"
KEY_NAME="${KEY_NAME:-openclaw-bastion}"
# Bastion SG — the ONLY source allowed to reach SSH / dashboard / API:
BASTION_SG="${BASTION_SG:-sg-0a78b7b8632997ee0}"
# Ubuntu 22.04 LTS amd64 (matches the article's OS; pin to a known AMI per region):
AMI_ID="${AMI_ID:-ami-06c2685db9a20aac5}"
MANAGER_TYPE="${MANAGER_TYPE:-m7i-flex.large}"   # >=4GB RAM for the indexer
AGENT_TYPE="${AGENT_TYPE:-t3.micro}"
AWS="${AWS:-/usr/local/bin/aws}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# --- 告警留存后端:独立 Amazon OpenSearch Service 域(治 all-in-one 单点全丢)---
# 默认 OFF。这套是生产推荐形态:告警从 manager 本地 indexer 改送一个**独立托管**的
# OpenSearch 域,manager 那台 EC2 挂了/被删/被攻陷,告警仍在独立信任域里(对标
# 生产级中心化 HIDS)。⚠ Amazon OpenSearch Service 按小时持续计费、停不掉
# (不像 EC2 能 stop),最小 t3.small.search 约 $26/月起,multi-AZ 翻倍。所以默认 OFF,
# 明确接受持续成本再设 ALERTS_OPENSEARCH_ENABLED=true 开。开关打开时建:VPC 内私网域
# (不公网暴露)+ 域 SG 入站 443 只对 manager SG(零 0.0.0.0/0)+ 细粒度访问控制 +
# 静态/传输加密。Filebeat output 改向那步进 manager 跑(见 RUNBOOK,带回滚指回本地)。
ALERTS_OPENSEARCH_ENABLED="${ALERTS_OPENSEARCH_ENABLED:-false}"
OPENSEARCH_DOMAIN="${OPENSEARCH_DOMAIN:-openclaw-wazuh-alerts}"
OPENSEARCH_INSTANCE_TYPE="${OPENSEARCH_INSTANCE_TYPE:-t3.small.search}"  # demo 最小单节点
OPENSEARCH_INSTANCE_COUNT="${OPENSEARCH_INSTANCE_COUNT:-1}"             # 生产改 ≥2 + multi-AZ
OPENSEARCH_VOLUME_GB="${OPENSEARCH_VOLUME_GB:-20}"
OPENSEARCH_MULTI_AZ="${OPENSEARCH_MULTI_AZ:-false}"
# --------------------------------------------------------------------------------------

echo "== 1. Security groups (zero 0.0.0.0/0) =="
AGENT_SG=$($AWS ec2 create-security-group --region "$REGION" \
  --group-name wazuh-agent-sg --vpc-id "$VPC_ID" \
  --description "Wazuh agent node - SSH from bastion only" \
  --query GroupId --output text)
MGR_SG=$($AWS ec2 create-security-group --region "$REGION" \
  --group-name wazuh-manager-sg --vpc-id "$VPC_ID" \
  --description "Wazuh manager all-in-one - agent comms + dashboard/api from bastion only" \
  --query GroupId --output text)
echo "AGENT_SG=$AGENT_SG  MGR_SG=$MGR_SG"

# agent inbound: SSH from bastion only
$AWS ec2 authorize-security-group-ingress --region "$REGION" --group-id "$AGENT_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,UserIdGroupPairs=[{GroupId=$BASTION_SG,Description=SSH-from-bastion}]" >/dev/null

# manager inbound: agent comms/enroll (1514-1515 TCP+UDP) from agent SG
$AWS ec2 authorize-security-group-ingress --region "$REGION" --group-id "$MGR_SG" --ip-permissions \
  "IpProtocol=tcp,FromPort=1514,ToPort=1515,UserIdGroupPairs=[{GroupId=$AGENT_SG,Description=agent-events-enroll-tcp}]" \
  "IpProtocol=udp,FromPort=1514,ToPort=1515,UserIdGroupPairs=[{GroupId=$AGENT_SG,Description=agent-events-enroll-udp}]" >/dev/null
# manager inbound: SSH + dashboard (443/5601) + API (55000) from bastion only
$AWS ec2 authorize-security-group-ingress --region "$REGION" --group-id "$MGR_SG" --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,UserIdGroupPairs=[{GroupId=$BASTION_SG,Description=SSH-from-bastion}]" \
  "IpProtocol=tcp,FromPort=443,ToPort=443,UserIdGroupPairs=[{GroupId=$BASTION_SG,Description=dashboard-https}]" \
  "IpProtocol=tcp,FromPort=5601,ToPort=5601,UserIdGroupPairs=[{GroupId=$BASTION_SG,Description=dashboard-5601}]" \
  "IpProtocol=tcp,FromPort=55000,ToPort=55000,UserIdGroupPairs=[{GroupId=$BASTION_SG,Description=wazuh-api}]" >/dev/null

# Red-line assertion: refuse to continue if anything opened 0.0.0.0/0
OPEN=$($AWS ec2 describe-security-groups --region "$REGION" --group-ids "$AGENT_SG" "$MGR_SG" \
  --query "SecurityGroups[].IpPermissions[].IpRanges[?CidrIp=='0.0.0.0/0']" --output text)
[ -z "$OPEN" ] || { echo "RED-LINE VIOLATION: 0.0.0.0/0 inbound found, aborting"; exit 1; }
echo "red-line OK: no 0.0.0.0/0 inbound"

echo "== 1b. Manager instance role: 告警外发到独立留存(防本地单点丢失) =="
# 问题:Wazuh 告警默认只落 manager 本地 /var/ossec/logs/alerts/alerts.json + 本地
# indexer —— 那台 EC2 挂了/被删/被攻陷先删日志,告警历史全没,等于没监控。
# 生产级要求告警出本机、汇到独立信任域、防删(对标生产级中心化 HIDS)。
# 这里给 manager 配最小权限 role:① CloudWatch Logs(把 alerts.json 推到独立 log group,
# manager 没了告警仍在)② SNS Publish(高危告警实时外发,不靠人盯 dashboard)③ SSM core
# (运维)。幂等:已存在则复用。
MGR_ROLE="openclaw-wazuh-manager-role"
MGR_PROFILE="openclaw-wazuh-manager-role"
ALERTS_LOG_GROUP="${ALERTS_LOG_GROUP:-/openclaw/wazuh/alerts}"
ALERTS_TOPIC="${ALERTS_TOPIC:-openclaw-wazuh-alerts}"
_acct=$($AWS sts get-caller-identity --query Account --output text)

# SNS topic(高危告警外发;订阅渠道由运维另接,见 RUNBOOK)
ALERTS_TOPIC_ARN=$($AWS sns create-topic --region "$REGION" --name "$ALERTS_TOPIC" \
  --query TopicArn --output text)
# CloudWatch log group(独立留存;保留 30 天,可按合规调)
$AWS logs create-log-group --region "$REGION" --log-group-name "$ALERTS_LOG_GROUP" 2>/dev/null || true
$AWS logs put-retention-policy --region "$REGION" --log-group-name "$ALERTS_LOG_GROUP" --retention-in-days 30 2>/dev/null || true

if $AWS iam get-role --role-name "$MGR_ROLE" >/dev/null 2>&1; then
  echo "  role $MGR_ROLE 已存在,复用"
else
  $AWS iam create-role --role-name "$MGR_ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
$AWS iam attach-role-policy --role-name "$MGR_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore 2>/dev/null || true
# 最小内联:只允许写本告警 log group + publish 本 topic,不给泛 logs:*/sns:*
$AWS iam put-role-policy --role-name "$MGR_ROLE" --policy-name wazuh-alert-egress \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogStream\",\"logs:PutLogEvents\",\"logs:DescribeLogStreams\"],\"Resource\":\"arn:aws:logs:${REGION}:${_acct}:log-group:${ALERTS_LOG_GROUP}:*\"},
    {\"Effect\":\"Allow\",\"Action\":\"sns:Publish\",\"Resource\":\"${ALERTS_TOPIC_ARN}\"}
  ]}"
if $AWS iam get-instance-profile --instance-profile-name "$MGR_PROFILE" >/dev/null 2>&1; then
  echo "  instance profile $MGR_PROFILE 已存在"
else
  $AWS iam create-instance-profile --instance-profile-name "$MGR_PROFILE" >/dev/null
  $AWS iam add-role-to-instance-profile --instance-profile-name "$MGR_PROFILE" --role-name "$MGR_ROLE"
  echo "  等 IAM 传播 ~10s"; sleep 10
fi
echo "ALERTS_TOPIC_ARN=$ALERTS_TOPIC_ARN  LOG_GROUP=$ALERTS_LOG_GROUP"

# == 1c. (可选) 独立 Amazon OpenSearch Service 域:告警留存到独立信任域 ==
# 默认 OFF(持续计费,见 CONFIG 段警告)。开 ALERTS_OPENSEARCH_ENABLED=true 才建。
OPENSEARCH_ENDPOINT=""
if [ "$ALERTS_OPENSEARCH_ENABLED" = "true" ]; then
  echo "== 1c. Amazon OpenSearch Service 域 $OPENSEARCH_DOMAIN(⚠ 持续计费)=="
  # 域 SG:入站 443 只对 manager SG,零 0.0.0.0/0(守红线)
  if ! OS_SG=$($AWS ec2 describe-security-groups --region "$REGION" \
        --filters "Name=group-name,Values=openclaw-opensearch-sg" "Name=vpc-id,Values=$VPC_ID" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null) || [ "$OS_SG" = "None" ]; then
    OS_SG=$($AWS ec2 create-security-group --region "$REGION" \
      --group-name openclaw-opensearch-sg --vpc-id "$VPC_ID" \
      --description "OpenSearch alerts domain - 443 from wazuh manager SG only" \
      --query GroupId --output text)
    $AWS ec2 authorize-security-group-ingress --region "$REGION" --group-id "$OS_SG" \
      --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,UserIdGroupPairs=[{GroupId=$MGR_SG,Description=https-from-wazuh-manager}]" >/dev/null
  fi
  # 红线断言:域 SG 不得有 0.0.0.0/0
  OS_OPEN=$($AWS ec2 describe-security-groups --region "$REGION" --group-ids "$OS_SG" \
    --query "SecurityGroups[].IpPermissions[].IpRanges[?CidrIp=='0.0.0.0/0']" --output text)
  [ -z "$OS_OPEN" ] || { echo "RED-LINE: opensearch SG 0.0.0.0/0, aborting"; exit 1; }
  # 域:私网子网、细粒度访问控制、静态+传输+节点间加密、HTTPS only、强制 TLS1.2
  _ZA="ZoneAwarenessEnabled=false"
  [ "$OPENSEARCH_MULTI_AZ" = "true" ] && _ZA="ZoneAwarenessEnabled=true,ZoneAwarenessConfig={AvailabilityZoneCount=2}"
  $AWS opensearch create-domain --region "$REGION" --domain-name "$OPENSEARCH_DOMAIN" \
    --engine-version "OpenSearch_2.11" \
    --cluster-config "InstanceType=$OPENSEARCH_INSTANCE_TYPE,InstanceCount=$OPENSEARCH_INSTANCE_COUNT,$_ZA" \
    --ebs-options "EBSEnabled=true,VolumeType=gp3,VolumeSize=$OPENSEARCH_VOLUME_GB" \
    --vpc-options "SubnetIds=$PRIVATE_SUBNET,SecurityGroupIds=$OS_SG" \
    --encryption-at-rest-options "Enabled=true" \
    --node-to-node-encryption-options "Enabled=true" \
    --domain-endpoint-options "EnforceHTTPS=true,TLSSecurityPolicy=Policy-Min-TLS-1-2-2019-07" \
    --advanced-security-options "Enabled=true,InternalUserDatabaseEnabled=true" >/dev/null \
    && echo "  域创建中(约 15-20 分钟变 active);endpoint 待 describe-domain 取" \
    || echo "  域已存在或创建调用失败,describe-domain 核对"
  OPENSEARCH_ENDPOINT=$($AWS opensearch describe-domain --region "$REGION" \
    --domain-name "$OPENSEARCH_DOMAIN" --query 'DomainStatus.Endpoints.vpc' --output text 2>/dev/null || echo "")
  echo "OPENSEARCH_ENDPOINT=${OPENSEARCH_ENDPOINT:-<pending, 域 active 后重取>}"
  echo "  下一步(进 manager SSH 跑,见 RUNBOOK):改 Filebeat output 指向该域 endpoint"
else
  echo "== 1c. OpenSearch 域 OFF(ALERTS_OPENSEARCH_ENABLED!=true);告警留存走 CloudWatch + 本地 indexer =="
fi

echo "== 2. Launch manager (all-in-one) =="
# 把告警外发的 log group / topic ARN / region 注入 userdata(占位符替换,
# 同 agent 的 manager IP 注入模式);userdata 据此装 CloudWatch agent 推 alerts.json。
MGR_UD=$(sed -e "s#__ALERTS_LOG_GROUP__#${ALERTS_LOG_GROUP}#" \
             -e "s#__ALERTS_TOPIC_ARN__#${ALERTS_TOPIC_ARN}#" \
             -e "s#__AWS_REGION__#${REGION}#" \
             "$HERE/userdata/server-userdata.sh" | base64 -w0)
MGR_ID=$($AWS ec2 run-instances --region "$REGION" --image-id "$AMI_ID" \
  --instance-type "$MANAGER_TYPE" --key-name "$KEY_NAME" \
  --subnet-id "$PRIVATE_SUBNET" --security-group-ids "$MGR_SG" --no-associate-public-ip-address \
  --iam-instance-profile "Name=$MGR_PROFILE" \
  --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=40,VolumeType=gp3}" \
  --user-data "$MGR_UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=Wazuh-Manager},{Key=project,Value=openclaw-monitoring},{Key=role,Value=wazuh-manager}]" \
  --query "Instances[0].InstanceId" --output text)
echo "MGR_ID=$MGR_ID — waiting for running + private IP..."
$AWS ec2 wait instance-running --region "$REGION" --instance-ids "$MGR_ID"
MGR_PRIV=$($AWS ec2 describe-instances --region "$REGION" --instance-ids "$MGR_ID" \
  --query "Reservations[0].Instances[0].PrivateIpAddress" --output text)
echo "MGR_PRIV=$MGR_PRIV"

echo "== 3. Launch agent (manager IP baked into userdata) =="
# Substitute the placeholder with the real manager private IP just-in-time:
AGENT_UD=$(sed "s/__MANAGER_PRIVATE_IP__/$MGR_PRIV/" "$HERE/userdata/agent-userdata.sh" | base64 -w0)
AGENT_ID=$($AWS ec2 run-instances --region "$REGION" --image-id "$AMI_ID" \
  --instance-type "$AGENT_TYPE" --key-name "$KEY_NAME" \
  --subnet-id "$PRIVATE_SUBNET" --security-group-ids "$AGENT_SG" --no-associate-public-ip-address \
  --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3}" \
  --user-data "$AGENT_UD" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=Wazuh-Agent-one},{Key=project,Value=openclaw-monitoring},{Key=role,Value=wazuh-agent}]" \
  --query "Instances[0].InstanceId" --output text)
echo "AGENT_ID=$AGENT_ID"

cat <<EOF

== DONE (instances launching) ==
  Manager:  $MGR_ID  ($MANAGER_TYPE)  private $MGR_PRIV   SG $MGR_SG
  Agent:    $AGENT_ID  ($AGENT_TYPE)               SG $AGENT_SG

Next (manager installer takes ~5-10 min; see RUNBOOK.md):
  # admin password (generated; store in a secret manager, then rotate):
  ssh -J <bastion> ubuntu@$MGR_PRIV \\
    'sudo tar -O -xf /root/wazuh-install-files.tar wazuh-install-files/wazuh-passwords.txt | grep -A1 "username: .admin."'
  # confirm agent registered:
  ssh -J <bastion> ubuntu@$MGR_PRIV 'sudo /var/ossec/bin/agent_control -l'
  # custom OpenClaw rules:
  scp ../wazuh-rules/openclaw_local_rules.xml ubuntu@$MGR_PRIV:/tmp/ && \\
    ssh ... 'sudo cp /tmp/openclaw_local_rules.xml /var/ossec/etc/rules/ && sudo systemctl restart wazuh-manager'
  # dashboard over SSH tunnel from your laptop (no public exposure):
  ssh -i key.pem -L 8443:$MGR_PRIV:443 ubuntu@<bastion-public-ip>
  # then open https://localhost:8443  (admin / generated password)
EOF
