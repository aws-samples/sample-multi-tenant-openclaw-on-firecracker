#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# 备份指定租户的 data.ext4 到 S3
# 用法: backup-data.sh <tenant_id> [bucket] [prefix] [cmk_key_id]
#
# 第 4 个参数 cmk_key_id 非空时,做**客户端 envelope 加密**(防内部威胁纵深第三层):
#   KMS GenerateDataKey 取一次性数据密钥 → openssl AES-256 本地加密 .gz → 上传密文
#   (.gz.enc)+ 加密态数据密钥(.gz.key)。S3 服务端/桶管理员拿到的永远是密文;只有
#   持 CMK kms:Decrypt 权限的备份/恢复角色能解出数据密钥、再解出明文。
# 桶侧另有 Object Lock COMPLIANCE(WORM 防删改)+ SSE-KMS,三层叠加。
set -uo pipefail
TENANT_ID="${1:?Usage: backup-data.sh <tenant_id> [bucket] [prefix] [cmk_key_id]}"
[ -f /etc/platform.env ] && source /etc/platform.env
BUCKET="${2:-${BACKUP_BUCKET:-${ASSETS_BUCKET}}}"
PREFIX="${3:-backups}"
CMK_KEY_ID="${4:-${BACKUP_CMK_KEY_ID:-}}"
TOKEN=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)

VM_DIR="/data/firecracker-vms/${TENANT_ID}"
DATA_FILE="${VM_DIR}/data.ext4"
SOCK="${VM_DIR}/fc.sock"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
GZ_FILE="/data/tmp-backup-${TENANT_ID}.gz"
S3_KEY="${PREFIX}/${TENANT_ID}/${TIMESTAMP}.gz"

log() { echo "[oc:backup] $(date +%H:%M:%S) $*"; }

cleanup() {
  rm -f "$GZ_FILE"
  # Ensure VM is resumed even on error
  if [ -S "$SOCK" ]; then
    curl -sf --unix-socket "$SOCK" -X PATCH http://localhost/vm \
      -H 'Content-Type: application/json' -d '{"state":"Resumed"}' >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [ ! -f "$DATA_FILE" ]; then
  log "ERROR: ${DATA_FILE} not found"
  exit 1
fi

# Pause VM → compress → Resume
if [ -S "$SOCK" ]; then
  curl -sf --unix-socket "$SOCK" -X PATCH http://localhost/vm \
    -H 'Content-Type: application/json' -d '{"state":"Paused"}' >/dev/null 2>&1 || true
  log "VM paused"
fi

T0=$SECONDS
pigz -c "$DATA_FILE" > "$GZ_FILE"
log "compressed ($((SECONDS-T0))s)"

if [ -S "$SOCK" ]; then
  curl -sf --unix-socket "$SOCK" -X PATCH http://localhost/vm \
    -H 'Content-Type: application/json' -d '{"state":"Resumed"}' >/dev/null 2>&1 || true
  log "VM resumed"
fi

# Upload (VM already running)
SIZE_MB=$(( $(stat -c%s "$GZ_FILE") / 1048576 ))
log "uploading ${SIZE_MB}MB..."

if [ -n "$CMK_KEY_ID" ]; then
  # ---- Client-side envelope encryption (insider-threat defense-in-depth) ----
  # 1) KMS 取一次性数据密钥:Plaintext 用来本地加密(用完即弃),CiphertextBlob 随密文存。
  DK_JSON=$(aws kms generate-data-key --key-id "$CMK_KEY_ID" --key-spec AES_256 \
    --region "$REGION" --output json) || { log "ERROR: KMS GenerateDataKey failed"; exit 1; }
  DK_PLAIN=$(echo "$DK_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['Plaintext'])")
  echo "$DK_JSON" | python3 -c "import sys,json;sys.stdout.buffer.write(__import__('base64').b64decode(json.load(sys.stdin)['CiphertextBlob']))" > "${GZ_FILE}.key"
  # 2) openssl AES-256-CBC 用数据密钥(hex)本地加密 .gz → .enc;明文密钥只在内存/管道。
  DK_HEX=$(echo "$DK_PLAIN" | base64 -d | xxd -p -c 256 | tr -d '\n')
  openssl enc -aes-256-cbc -pbkdf2 -in "$GZ_FILE" -out "${GZ_FILE}.enc" -K "$DK_HEX" -iv 00000000000000000000000000000000
  unset DK_PLAIN DK_HEX
  # 3) 上传密文 + 加密态数据密钥(桶侧还有 SSE-KMS + Object Lock 兜底)。
  aws s3 cp "${GZ_FILE}.enc" "s3://${BUCKET}/${S3_KEY}.enc" --region "$REGION" --quiet
  aws s3 cp "${GZ_FILE}.key" "s3://${BUCKET}/${S3_KEY}.key" --region "$REGION" --quiet
  rm -f "$GZ_FILE" "${GZ_FILE}.enc" "${GZ_FILE}.key"
  log "uploaded ENCRYPTED s3://${BUCKET}/${S3_KEY}.enc (+.key, envelope+SSE-KMS+ObjectLock)"
  echo "${S3_KEY}.enc"
else
  # No CMK configured: rely on bucket-side SSE-KMS (still encrypted at rest).
  aws s3 cp "$GZ_FILE" "s3://${BUCKET}/${S3_KEY}" --region "$REGION" --quiet
  rm -f "$GZ_FILE"
  log "uploaded s3://${BUCKET}/${S3_KEY} (bucket SSE only — no CMK passed)"
  echo "${S3_KEY}"
fi
