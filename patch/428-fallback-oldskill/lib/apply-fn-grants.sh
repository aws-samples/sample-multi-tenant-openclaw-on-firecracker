#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apply-lambda-grants.sh — deliver the NEW Lambda env vars + IAM grants this sync (a63d7b05) adds,
# WITHOUT a stack update. These live in deploy/stacks/lambdas.py + deploy/stacks/ha_edge.py, which a
# code-overlay alone does NOT apply — so the new image-jobs / bootstrap-version / lifecycle paths
# would AccessDenied or silently no-op until these grants + env exist.
#
# Delivered (each source-proven against the diff; RETAIN on rollback — a fail-closed prereq is not
# rolled back, and inline put-role-policy is additive/named so re-apply is idempotent):
#   api_fn:
#     env  IMAGE_JOBS_TABLE=openclaw-image-jobs, BACKUP_BUCKET=<backup-bucket>
#     iam  dynamodb TransactWriteItems+ConditionCheckItem on version-snapshots,image-jobs,tenants,hosts
#          dynamodb UpdateItem/ReadWrite on image-jobs+version-snapshots ; s3 read on backup bucket
#          ec2 DescribeLaunchTemplateVersions(*) + ModifyLaunchTemplate(host-LT)   [ha_edge.py]
#   lifecycle_consumer:
#     env  IMAGE_JOBS_TABLE, BACKUP_BUCKET (same as api_fn)
#     iam  version-snapshots read ; TransactWriteItems+ConditionCheckItem on version-snapshots,tenants
#          TransactWriteItems on hosts,tenants ; s3 read on backup bucket
#   health_fn:
#     iam  TransactWriteItems on hosts,tenants   (reaper capacity-token release)
#
# Usage:  apply-lambda-grants.sh <apply|verify> <region> <account-id> [<host-lt-id>]
#   host-lt-id: the host Launch Template id (for the ModifyLaunchTemplate resource lock). If omitted,
#   the ec2:ModifyLaunchTemplate resource falls back to "*" (matches ha_edge.py DescribeLTVersions;
#   the code pins ModifyLaunchTemplate to the host LT ARN — pass the id to reproduce that lock).
set -euo pipefail
CMD="${1:?usage: apply-lambda-grants.sh <apply|verify> <region> <account-id> [host-lt-id]}"
REGION="${2:?region required}"
ACCT="${3:?account id required}"
LT_ID="${4:-}"

IMAGE_JOBS_TABLE="openclaw-image-jobs"
ARN_PREFIX="arn:aws:dynamodb:${REGION}:${ACCT}:table"
VS="${ARN_PREFIX}/openclaw-version-snapshots"
IJ="${ARN_PREFIX}/openclaw-image-jobs"
TEN="${ARN_PREFIX}/openclaw-tenants"
HOSTS="${ARN_PREFIX}/openclaw-hosts"

# Resolve a function's execution-role NAME from its ARN (role-name is the last ARN segment).
role_of() {
  local arn
  arn="$(aws lambda get-function-configuration --function-name "$1" --region "$REGION" \
    --query Role --output text)"
  echo "${arn##*/}"
}

# Resolve the backup bucket the api function reads. The name is openclaw-backups-<account><suffix>
# (storage.py), surfaced as the BackupBucket CFN output — read that first, else discover by prefix.
# If the deployment never created one, this stays empty and the code falls back to ASSETS_BUCKET
# (no grant needed then).
backup_bucket() {
  local b
  b="$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='BackupBucket'].OutputValue | [0]" --output text 2>/dev/null || echo "")"
  if [ -z "$b" ] || [ "$b" = "None" ]; then
    b="$(aws s3api list-buckets --query "Buckets[?starts_with(Name,'openclaw-backups-')].Name | [0]" \
      --output text 2>/dev/null || echo "")"
  fi
  [ "$b" = "None" ] && b=""
  echo "$b"
}

# Resolve the backup CMK ARN behind alias/openclaw-tenant-backup (grant_read on a CMK-encrypted
# bucket auto-adds kms:Decrypt in CDK; a bucket-read policy alone can't read encrypted objects).
backup_cmk_arn() {
  aws kms describe-key --key-id alias/openclaw-tenant-backup --region "$REGION" \
    --query KeyMetadata.Arn --output text 2>/dev/null || echo ""
}

put_env() {
  # merge KEY=VALUE pairs into a function's env WITHOUT dropping existing vars
  local fn="$1"; shift
  local cur
  cur="$(aws lambda get-function-configuration --function-name "$fn" --region "$REGION" \
    --query 'Environment.Variables' --output json)"
  local merged
  merged="$(python3 - "$cur" "$@" <<'PY'
import json, sys
cur = json.loads(sys.argv[1] or "{}") or {}
for kv in sys.argv[2:]:
    k, _, v = kv.partition("=")
    cur[k] = v
print(json.dumps({"Variables": cur}))
PY
)"
  aws lambda update-function-configuration --function-name "$fn" --region "$REGION" \
    --environment "$merged" >/dev/null
  aws lambda wait function-updated --function-name "$fn" --region "$REGION"
}

put_policy() {
  # inline, named policy on a role — additive + idempotent (same name overwrites in place)
  aws iam put-role-policy --role-name "$1" --policy-name "$2" \
    --policy-document "$3" --region "$REGION" >/dev/null
}

case "$CMD" in
apply)
  BUCKET="$(backup_bucket)"
  CMK_ARN="$(backup_cmk_arn)"
  API_ROLE="$(role_of openclaw-api)"
  CONS_ROLE="$(role_of openclaw-lifecycle-consumer)"
  HEALTH_ROLE="$(role_of openclaw-health-check)"

  echo "[grants] env: IMAGE_JOBS_TABLE + BACKUP_BUCKET on api + lifecycle-consumer"
  if [ -n "$BUCKET" ]; then
    put_env openclaw-api "IMAGE_JOBS_TABLE=${IMAGE_JOBS_TABLE}" "BACKUP_BUCKET=${BUCKET}"
    put_env openclaw-lifecycle-consumer "IMAGE_JOBS_TABLE=${IMAGE_JOBS_TABLE}" "BACKUP_BUCKET=${BUCKET}"
  else
    echo "[grants] no backup bucket in this deployment — setting IMAGE_JOBS_TABLE only (code falls back to ASSETS_BUCKET)"
    put_env openclaw-api "IMAGE_JOBS_TABLE=${IMAGE_JOBS_TABLE}"
    put_env openclaw-lifecycle-consumer "IMAGE_JOBS_TABLE=${IMAGE_JOBS_TABLE}"
  fi

  LT_RESOURCE='"*"'
  [ -n "$LT_ID" ] && LT_RESOURCE="\"arn:aws:ec2:${REGION}:${ACCT}:launch-template/${LT_ID}\""

  echo "[grants] api_fn IAM (role=$API_ROLE)"
  put_policy "$API_ROLE" oc428-image-jobs-txn "{
    \"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:TransactWriteItems\",\"dynamodb:ConditionCheckItem\"],
       \"Resource\":[\"${VS}\",\"${IJ}\",\"${TEN}\",\"${HOSTS}\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:PutItem\",\"dynamodb:UpdateItem\",\"dynamodb:Query\",\"dynamodb:BatchWriteItem\",\"dynamodb:DeleteItem\"],
       \"Resource\":[\"${IJ}\",\"${IJ}/index/*\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:UpdateItem\"],\"Resource\":[\"${VS}\"]}
    ]}"
  put_policy "$API_ROLE" oc428-bootstrap-lt "{
    \"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"ec2:DescribeLaunchTemplateVersions\"],\"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":[\"ec2:ModifyLaunchTemplate\"],\"Resource\":[${LT_RESOURCE}]}
    ]}"
  if [ -n "$BUCKET" ]; then
    kms_stmt=""
    [ -n "$CMK_ARN" ] && kms_stmt=",
        {\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\",\"kms:DescribeKey\"],\"Resource\":\"${CMK_ARN}\"}"
    put_policy "$API_ROLE" oc428-backup-read "{
      \"Version\":\"2012-10-17\",\"Statement\":[
        {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:GetObjectVersion\",\"s3:GetBucketLocation\",\"s3:ListBucket\"],
         \"Resource\":[\"arn:aws:s3:::${BUCKET}\",\"arn:aws:s3:::${BUCKET}/*\"]}${kms_stmt}]}"
  fi

  echo "[grants] lifecycle_consumer IAM (role=$CONS_ROLE)"
  put_policy "$CONS_ROLE" oc428-consumer-txn "{
    \"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:TransactWriteItems\",\"dynamodb:ConditionCheckItem\"],
       \"Resource\":[\"${VS}\",\"${TEN}\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:TransactWriteItems\"],\"Resource\":[\"${HOSTS}\",\"${TEN}\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:Query\"],\"Resource\":[\"${VS}\",\"${VS}/index/*\"]}
    ]}"
  if [ -n "$BUCKET" ]; then
    kms_stmt=""
    [ -n "$CMK_ARN" ] && kms_stmt=",
        {\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\",\"kms:DescribeKey\"],\"Resource\":\"${CMK_ARN}\"}"
    put_policy "$CONS_ROLE" oc428-backup-read "{
      \"Version\":\"2012-10-17\",\"Statement\":[
        {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:GetObjectVersion\",\"s3:GetBucketLocation\",\"s3:ListBucket\"],
         \"Resource\":[\"arn:aws:s3:::${BUCKET}\",\"arn:aws:s3:::${BUCKET}/*\"]}${kms_stmt}]}"
  fi

  echo "[grants] health_fn IAM (role=$HEALTH_ROLE)"
  put_policy "$HEALTH_ROLE" oc428-reaper-txn "{
    \"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:TransactWriteItems\"],\"Resource\":[\"${HOSTS}\",\"${TEN}\"]}]}"

  echo "PASS: env + IAM grants applied (api_fn, lifecycle-consumer, health_fn). RETAIN on rollback."
  ;;

verify)
  fail=0
  for fn in openclaw-api openclaw-lifecycle-consumer; do
    v="$(aws lambda get-function-configuration --function-name "$fn" --region "$REGION" \
      --query 'Environment.Variables.IMAGE_JOBS_TABLE' --output text 2>/dev/null || echo None)"
    if [ "$v" = "$IMAGE_JOBS_TABLE" ]; then echo "PASS: $fn IMAGE_JOBS_TABLE=$v"
    else echo "FAIL: $fn IMAGE_JOBS_TABLE=$v (expected $IMAGE_JOBS_TABLE)" >&2; fail=1; fi
  done
  for pair in "openclaw-api:oc428-image-jobs-txn" "openclaw-api:oc428-bootstrap-lt" \
              "openclaw-lifecycle-consumer:oc428-consumer-txn" "openclaw-health-check:oc428-reaper-txn"; do
    fn="${pair%%:*}"; pol="${pair##*:}"; role="$(role_of "$fn")"
    if aws iam get-role-policy --role-name "$role" --policy-name "$pol" --region "$REGION" >/dev/null 2>&1; then
      echo "PASS: $fn role has inline policy $pol"
    else echo "FAIL: $fn role missing inline policy $pol" >&2; fail=1; fi
  done
  [ "$fail" -eq 0 ] || exit 1
  echo "PASS: all env + IAM grants present"
  ;;

*)
  echo "usage: apply-lambda-grants.sh <apply|verify> <region> <account-id> [host-lt-id]" >&2
  exit 2
  ;;
esac
