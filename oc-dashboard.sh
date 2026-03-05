#!/bin/bash
# 通过 SSM 端口转发访问 OpenClaw Gateway Dashboard
# 用法: ./oc-console.sh <tenant-id> [local-port]
# 示例: ./oc-dashboard.sh test-vm-a634
#       ./oc-dashboard.sh test-vm-a634 8080
set -euo pipefail

TENANT_ID="${1:?Usage: $0 <tenant-id> [local-port]}"
LOCAL_PORT="${2:-18789}"

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

# Extract gateway token from VM
CMD_ID=$(aws ssm send-command --instance-ids "$HOST_ID" \
  --document-name AWS-RunShellScript \
  --parameters "{\"commands\":[\"SSHPASS='OpenCl@w2026' sshpass -e ssh -o StrictHostKeyChecking=no agent@${GUEST_IP} jq -r .gateway.auth.token .openclaw/openclaw.json\"]}" \
  --query 'Command.CommandId' --output text --profile "$PROFILE" --region "$REGION")
printf "⏳ Fetching gateway token"
TOKEN=""
for i in $(seq 1 10); do
  sleep 1; printf "."
  TOKEN=$(aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$HOST_ID" \
    --query 'StandardOutputContent' --output text --profile "$PROFILE" --region "$REGION" 2>/dev/null | tr -d '[:space:]')
  [ -n "$TOKEN" ] && break
done
echo ""

echo "→ ${TENANT_ID} @ ${HOST_ID} (${GUEST_IP}:18789)"
echo "  http://localhost:${LOCAL_PORT}/?token=${TOKEN}"
echo ""
echo "Device pairing required on first connect. Run in VM:"
echo "  openclaw devices list              # find requestId"
echo "  openclaw devices approve <requestId>"
aws ssm start-session --target "$HOST_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"${GUEST_IP}\"],\"portNumber\":[\"18789\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}" \
  --profile "$PROFILE" --region "$REGION"
