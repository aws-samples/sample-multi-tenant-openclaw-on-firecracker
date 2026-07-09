#!/usr/bin/env bash
# 幂等创建/对齐 LiteLLM 宿主(堡垒机)的最小权限 Bedrock instance role,并关联到本机。
# LiteLLM 容器经宿主 IMDS 取这把受限 role 调 Bedrock,绝不放静态 AWS key、也不挂 admin。
#
# 背景(见 LITELLM-RUNBOOK.md「凭据模型」):堡垒机本身用 IAM user 静态 key,IMDS 上没有
# instance profile,容器 boto3 默认取不到凭据。解法是建本脚本这把最小权限 instance profile
# attach 到堡垒机,容器 method=iam-role 取临时凭据。此前这个 role 只在 RUNBOOK 文字描述、
# 无可执行定义,新账号重建得人肉手建 —— 本脚本把它落成可随部署继承的代码(2026-06-30)。
#
# 用法:./setup-bedrock-role.sh                 # 自动取本机 instance-id,建 role+policy+profile 并关联
#       ASSOCIATE=0 ./setup-bedrock-role.sh     # 只建 role/policy/profile,不关联到本机(在别处关联)
#
# 幂等:role/policy/profile 已存在则更新策略文档、不报错;关联已存在则跳过。
# 安全:inline policy 只给 5 个 bedrock 调用动作,不是 admin;trust 只允许 ec2.amazonaws.com。
set -euo pipefail

REGION="${AWS_REGION:-ap-southeast-1}"
ROLE="openclaw-litellm-bedrock-role"
POLICY="litellm-bedrock-invoke"
PROFILE="openclaw-litellm-bedrock-role"   # instance profile 同名
ASSOCIATE="${ASSOCIATE:-1}"

log() { echo "[bedrock-role] $*"; }

# trust policy:只允许 EC2 实例担任(instance profile 用)
TRUST='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "ec2.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}'

# inline policy:最小权限 —— 只给 LiteLLM 真正要用的 5 个 Bedrock 调用动作。
# Resource "*":InvokeModel 跨多个 inference profile + ApplyGuardrail 跨 guardrail,
# 收窄到具体 ARN 需逐一枚举 model/guardrail ARN(切生产锁定模型后可收窄,见 RUNBOOK 待办)。
PERM='{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "BedrockInvokeForLiteLLM",
    "Effect": "Allow",
    "Action": [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
      "bedrock:ApplyGuardrail"
    ],
    "Resource": "*"
  }]
}'

# 1) role(不存在则建,存在则对齐 trust)
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  log "role $ROLE 已存在,对齐 trust policy"
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$TRUST"
else
  log "建 role $ROLE"
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
    --description "Minimal Bedrock invoke role for LiteLLM host (IMDS); not admin" >/dev/null
fi

# 2) inline policy(put 是幂等 upsert)
log "put inline policy $POLICY(最小权限 5 动作)"
aws iam put-role-policy --role-name "$ROLE" --policy-name "$POLICY" --policy-document "$PERM"

# 3) instance profile(不存在则建)+ 把 role 放进去
if aws iam get-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1; then
  log "instance profile $PROFILE 已存在"
else
  log "建 instance profile $PROFILE"
  aws iam create-instance-profile --instance-profile-name "$PROFILE" >/dev/null
fi
# add-role 幂等性:已在则报 LimitExceeded,吞掉
if aws iam get-instance-profile --instance-profile-name "$PROFILE" \
     --query 'InstanceProfile.Roles[0].RoleName' --output text 2>/dev/null | grep -q "^${ROLE}$"; then
  log "role 已在 instance profile 内"
else
  aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE"
  log "role 已加入 instance profile,等 IAM 传播 ~10s"; sleep 10
fi

if [ "$ASSOCIATE" != "1" ]; then
  log "ASSOCIATE=0,跳过关联到本机。role/policy/profile 已就绪。"
  exit 0
fi

# 4) 关联 instance profile 到本机(堡垒机)。IMDSv2 取 instance-id。
TOK="$(curl -s --max-time 3 -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 180')"
IID="$(curl -s -H "X-aws-ec2-metadata-token: $TOK" \
  http://169.254.169.254/latest/meta-data/instance-id)"
if [ -z "$IID" ]; then
  log "ERR: 取不到本机 instance-id(非 EC2 上跑?)。role 已建,手动关联或用 ASSOCIATE=0。" >&2
  exit 1
fi

EXIST="$(aws ec2 describe-iam-instance-profile-associations --region "$REGION" \
  --filters "Name=instance-id,Values=$IID" \
  --query 'IamInstanceProfileAssociations[?State==`associated`].IamInstanceProfile.Arn' \
  --output text 2>/dev/null || true)"
if echo "$EXIST" | grep -q "$PROFILE"; then
  log "本机 $IID 已关联 $PROFILE,无需重复。"
elif [ -n "$EXIST" ]; then
  log "本机 $IID 已关联其它 profile($EXIST)。不自动替换以免误伤,需手动 disassociate 后再跑(见 RUNBOOK 回滚段)。"
  exit 1
else
  log "关联 $PROFILE 到本机 $IID"
  aws ec2 associate-iam-instance-profile --region "$REGION" \
    --instance-id "$IID" --iam-instance-profile "Name=$PROFILE" >/dev/null
  log "已关联。IMDS hop limit 须 ≥2(docker bridge 多一跳),堡垒机默认已是 2。"
fi

log "完成。验证:在容器内 boto3 用 method=iam-role 取凭据应能 InvokeModel。"
