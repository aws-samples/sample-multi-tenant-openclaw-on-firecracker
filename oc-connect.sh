#!/bin/bash
# 快速登录 microVM: ./oc-connect.sh openclaw-01
set -euo pipefail

# 检查 Session Manager 插件
if ! command -v session-manager-plugin &>/dev/null; then
  echo "❌ Session Manager 插件未安装"
  read -p "是否现在安装? [y/N] " yn
  if [[ "$yn" =~ ^[Yy]$ ]]; then
    curl -s "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" -o /tmp/ssm-plugin.deb
    sudo dpkg -i /tmp/ssm-plugin.deb
    echo "✓ 安装完成"
  else
    echo "请手动安装: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"
    exit 1
  fi
fi

TENANT_ID="${1:?Usage: $0 <tenant-id>}"

# 加载部署环境信息
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE"
  TABLE="${TENANTS_TABLE:-openclaw-tenants}"
else
  echo "⚠️  未找到 .env.deploy，请先运行 ./deploy.sh 或手动指定环境变量"
  TABLE="openclaw-tenants"
fi
REGION="${REGION:-ap-northeast-1}"
PROFILE="${PROFILE:-lab}"

# 从 DynamoDB 查租户信息
ITEM=$(aws dynamodb get-item --table-name "$TABLE" \
  --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
  --query 'Item.{host:host_id.S,ip:guest_ip.S,status:status.S}' \
  --output json --profile "$PROFILE" --region "$REGION")

HOST_ID=$(echo "$ITEM" | jq -r .host)
GUEST_IP=$(echo "$ITEM" | jq -r .ip)
STATUS=$(echo "$ITEM" | jq -r .status)

[ "$HOST_ID" = "null" ] && echo "❌ Tenant '${TENANT_ID}' not found" && exit 1
[ "$STATUS" != "running" ] && echo "⚠️  Tenant status: ${STATUS} (not running)" && exit 1

echo "→ ${TENANT_ID} @ ${HOST_ID} (${GUEST_IP})"
aws ssm start-session --target "$HOST_ID" \
  --document-name AWS-StartInteractiveCommand \
  --parameters "{\"command\":[\"sshpass -p openclaw ssh -o StrictHostKeyChecking=no root@${GUEST_IP}\"]}" \
  --profile "$PROFILE" --region "$REGION"
