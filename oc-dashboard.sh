#!/bin/bash
# 通过 SSM 端口转发访问 VM 的 Mission Control Dashboard
# 用法: ./oc-dashboard.sh <tenant-id> [local-port]
# 示例: ./oc-dashboard.sh test-vm-a634
#       ./oc-dashboard.sh test-vm-a634 8080
set -euo pipefail

TENANT_ID="${1:?Usage: $0 <tenant-id> [local-port]}"
LOCAL_PORT="${2:-3333}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE"
  TABLE="${TENANTS_TABLE:-openclaw-tenants}"
else
  echo "⚠️  未找到 .env.deploy，请先运行 ./setup.sh"
  exit 1
fi
REGION="${REGION:-ap-northeast-1}"
PROFILE="${PROFILE:-lab}"

ITEM=$(aws dynamodb get-item --table-name "$TABLE" \
  --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
  --query 'Item.{host:host_id.S,ip:guest_ip.S,status:status.S}' \
  --output json --profile "$PROFILE" --region "$REGION")

HOST_ID=$(echo "$ITEM" | jq -r .host)
GUEST_IP=$(echo "$ITEM" | jq -r .ip)
STATUS=$(echo "$ITEM" | jq -r .status)

[ "$HOST_ID" = "null" ] && echo "❌ Tenant '${TENANT_ID}' not found" && exit 1
[ "$STATUS" != "running" ] && echo "⚠️  Tenant status: ${STATUS} (not running)" && exit 1

echo "→ ${TENANT_ID} @ ${HOST_ID} (${GUEST_IP}:3333)"
echo "  http://localhost:${LOCAL_PORT}"
aws ssm start-session --target "$HOST_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"${GUEST_IP}\"],\"portNumber\":[\"3333\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}" \
  --profile "$PROFILE" --region "$REGION"
