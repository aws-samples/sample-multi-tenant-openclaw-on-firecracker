#!/bin/bash
# 销毁 CDK stack 并清理资源
# 用法: ./destroy.sh [--purge]
#   --purge  同时删除 RETAIN 的 S3 bucket 和 DynamoDB 表
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ .env.deploy not found. Nothing to destroy."
  exit 1
fi

source "$ENV_FILE"
REGION="${REGION:?REGION not set in .env.deploy}"
PROFILE="${PROFILE:?PROFILE not set in .env.deploy}"
PURGE=false
[ "${1:-}" = "--purge" ] && PURGE=true

echo "⚠️  This will destroy ALL resources in $REGION (profile: $PROFILE)"
$PURGE && echo "   --purge: S3 bucket + DynamoDB tables will be PERMANENTLY deleted"
read -p "Type 'yes' to confirm: " confirm
[ "$confirm" = "yes" ] || { echo "Aborted."; exit 1; }

if $PURGE && [ -n "${ASSETS_BUCKET:-}" ]; then
  echo "→ Emptying S3 bucket: $ASSETS_BUCKET"
  aws s3 rm "s3://$ASSETS_BUCKET" --recursive --profile "$PROFILE" --region "$REGION" 2>/dev/null || true
fi

# CDK destroy
echo "→ Destroying CDK stack..."
cd "$SCRIPT_DIR/deploy"
PATH=".venv/bin:$PATH" cdk destroy -c region="$REGION" --profile "$PROFILE" --force

if $PURGE; then
  echo "→ Purging retained resources..."
  aws s3 rb "s3://${ASSETS_BUCKET}" --profile "$PROFILE" --region "$REGION" 2>/dev/null && echo "  ✓ S3 bucket deleted" || echo "  ⚠ S3 bucket already gone"
  for table in "${TENANTS_TABLE:-openclaw-tenants}" "${HOSTS_TABLE:-openclaw-hosts}"; do
    aws dynamodb delete-table --table-name "$table" --profile "$PROFILE" --region "$REGION" 2>/dev/null && echo "  ✓ DynamoDB table $table deleted" || echo "  ⚠ $table already gone"
  done
  # Delete orphaned data volumes
  VOLS=$(aws ec2 describe-volumes --filters Name=tag:openclaw:role,Values=host-data Name=status,Values=available \
    --query 'Volumes[*].VolumeId' --output text --profile "$PROFILE" --region "$REGION" 2>/dev/null || true)
  for vol in $VOLS; do
    aws ec2 delete-volume --volume-id "$vol" --profile "$PROFILE" --region "$REGION" 2>/dev/null && echo "  ✓ EBS volume $vol deleted" || echo "  ⚠ Failed to delete $vol"
  done
fi

rm -f "$ENV_FILE"
echo "✓ Stack destroyed, .env.deploy removed"
