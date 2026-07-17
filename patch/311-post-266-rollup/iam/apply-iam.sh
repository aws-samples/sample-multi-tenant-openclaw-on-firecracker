#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apply-iam.sh — patch #311 第 3 层(权限热补,非 CDK 部署时用)。
#
# 给 host instance-role 授 openclaw-tenant-secrets 的 dynamodb:GetItem。这是 #290/#306
# DDB fallback 的**硬前提**:launch-vm.sh 在位置参 12/13(gateway_token_ct /
# device_paired_b64)为空时会 get-item openclaw-tenant-secrets 自取,fail-closed —— 读
# 失败即中止启动。缺这条权限 → AccessDenied → VM 起不来卡 creating。
#
# 正式做法是走 CDK(#307 compute.py 已加 grant,cdk deploy 即生效)。本脚本是"来不及
# deploy、先让线上跑通"的等价 inline policy 热补。deploy 后可删这条 inline policy。
#
# 用法:  bash apply-iam.sh <host-role-name> <region> <account-id>
#   host-role-name 从 host 上 `aws sts get-caller-identity` 的 ARN 尾段取,或 AccessDenied
#   报错信息里带。region/account-id 同你的部署。
set -euo pipefail

ROLE="${1:?用法: apply-iam.sh <host-role-name> <region> <account-id>}"
REGION="${2:?用法: apply-iam.sh <host-role-name> <region> <account-id>}"
ACCOUNT="${3:?用法: apply-iam.sh <host-role-name> <region> <account-id>}"

POLICY_NAME="patch-311-tenant-secrets-read"
ARN="arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/openclaw-tenant-secrets"

echo "[patch-311] 给 role=${ROLE} 打 inline policy ${POLICY_NAME}(只读 ${ARN})..."
aws iam put-role-policy \
  --role-name "${ROLE}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"Patch311HostRoleReadTenantSecrets\",\"Effect\":\"Allow\",\"Action\":\"dynamodb:GetItem\",\"Resource\":\"${ARN}\"}]}"

echo "[patch-311] 已应用。验证(在 host 上跑,应返回 {} 或条目、而非 AccessDenied):"
echo "  aws dynamodb get-item --table-name openclaw-tenant-secrets --key '{\"tenant_id\":{\"S\":\"__probe__\"}}' --region ${REGION}"
echo "[patch-311] 提示:只读权限(host 从不写该表,mint 归 api_fn);与 CDK #307 grant 等价。"
