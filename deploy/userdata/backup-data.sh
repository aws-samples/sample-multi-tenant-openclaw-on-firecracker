#!/bin/bash
# 备份指定租户的 data.ext4 到 S3
# 用法: backup-data.sh <tenant_id> [bucket] [prefix]
set -euo pipefail
TENANT_ID="${1:?Usage: backup-data.sh <tenant_id> [bucket] [prefix]}"
BUCKET="${2:-{{ASSETS_BUCKET}}}"
PREFIX="${3:-backups}"
TOKEN=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)

VM_DIR="/data/firecracker-vms/${TENANT_ID}"
DATA_FILE="${VM_DIR}/data.ext4"
SOCK="${VM_DIR}/fc.sock"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
TMP_FILE="/tmp/backup-${TENANT_ID}-${TIMESTAMP}.ext4"
GZ_FILE="${TMP_FILE}.gz"
S3_KEY="${PREFIX}/${TENANT_ID}/${TIMESTAMP}.gz"

log() { echo "[oc:backup] $(date +%H:%M:%S) $*"; }

if [ ! -f "$DATA_FILE" ]; then
  log "ERROR: ${DATA_FILE} not found"
  exit 1
fi

# Pause VM for consistent copy
PAUSED=false
if [ -S "$SOCK" ]; then
  curl -sf --unix-socket "$SOCK" -X PATCH http://localhost/vm \
    -H 'Content-Type: application/json' -d '{"state":"Paused"}' >/dev/null 2>&1 && PAUSED=true
  log "VM paused"
fi

T0=$SECONDS
cp "$DATA_FILE" "$TMP_FILE"
log "data copied ($((SECONDS-T0))s)"

# Resume VM immediately after copy
if $PAUSED; then
  curl -sf --unix-socket "$SOCK" -X PATCH http://localhost/vm \
    -H 'Content-Type: application/json' -d '{"state":"Resumed"}' >/dev/null 2>&1
  log "VM resumed"
fi

# Compress and upload (VM already running)
pigz -c "$TMP_FILE" > "$GZ_FILE"
rm -f "$TMP_FILE"
SIZE_MB=$(( $(stat -c%s "$GZ_FILE") / 1048576 ))
log "compressed to ${SIZE_MB}MB"

aws s3 cp "$GZ_FILE" "s3://${BUCKET}/${S3_KEY}" --region "$REGION" --quiet
rm -f "$GZ_FILE"
log "uploaded s3://${BUCKET}/${S3_KEY}"

echo "${S3_KEY}"
