#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apply-iam.sh — Patch 311, Layer 3 (permission hotfix, for non-CDK deployments).
#
# Grants the host instance role dynamodb:GetItem on openclaw-tenant-secrets. This
# is a hard prerequisite of the token fallback: when positional args 12/13
# (gateway_token_ct / device_paired_b64) are empty, launch-vm.sh reads them from
# openclaw-tenant-secrets. It is fail-closed — if the read is denied, the launch
# aborts. Without this permission the VM never starts and the tenant is stuck
# creating.
#
# In the source, compute.py grants this. But `cdk deploy` is FORBIDDEN on this deployment
# (it was CDK-deployed once and then manually modified; a deploy would overwrite those
# changes). This inline policy IS the fix here — permanent, not a stopgap. Idempotent:
# put-role-policy overwrites the same policy name, so re-running is safe.
#
# Usage:  bash apply-iam.sh <host-role-name> <region> <account-id>
#   host-role-name: from `aws sts get-caller-identity` on the host (ARN tail), or
#   from the AccessDenied error message. region/account-id: your deployment's.
set -euo pipefail

ROLE="${1:?Usage: apply-iam.sh <host-role-name> <region> <account-id>}"
REGION="${2:?Usage: apply-iam.sh <host-role-name> <region> <account-id>}"
ACCOUNT="${3:?Usage: apply-iam.sh <host-role-name> <region> <account-id>}"

POLICY_NAME="patch-311-tenant-secrets-read"
ARN="arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/openclaw-tenant-secrets"

echo "[patch-311] Attaching inline policy ${POLICY_NAME} to role=${ROLE} (read-only ${ARN})..."
aws iam put-role-policy \
  --role-name "${ROLE}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"Patch311HostRoleReadTenantSecrets\",\"Effect\":\"Allow\",\"Action\":\"dynamodb:GetItem\",\"Resource\":\"${ARN}\"}]}"

echo "[patch-311] Applied. Verify on the host (should return {} or an item, not AccessDenied):"
echo "  aws dynamodb get-item --table-name openclaw-tenant-secrets --key '{\"tenant_id\":{\"S\":\"__probe__\"}}' --region ${REGION}"
echo "[patch-311] Note: read-only (the host never writes this table); equivalent to the CDK grant."
