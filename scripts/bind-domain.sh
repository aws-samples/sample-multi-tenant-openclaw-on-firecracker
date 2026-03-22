#!/bin/bash
# 将自定义域名 + ACM 证书关联到 Dashboard ALB
# 前置条件: 用户已自行申请 ACM 证书并完成验证，DNS CNAME 已指向 ALB
# 用法: ./bind-domain.sh <domain> <acm-certificate-arn>
# 示例: ./bind-domain.sh oc.example.com arn:aws:acm:ap-northeast-1:123456:certificate/xxx
set -euo pipefail

DOMAIN="${1:?Usage: $0 <domain> <acm-certificate-arn>}"
CERT_ARN="${2:?Usage: $0 <domain> <acm-certificate-arn>}"

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$SCRIPT_DIR/.env.deploy"

ALB_NAME="openclaw-dashboard"
ALB_ARN=$(aws elbv2 describe-load-balancers --names $ALB_NAME \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text \
  --profile "$PROFILE" --region "$REGION")
TG_ARN=$(aws elbv2 describe-target-groups --load-balancer-arn "$ALB_ARN" \
  --query 'TargetGroups[0].TargetGroupArn' --output text \
  --profile "$PROFILE" --region "$REGION")

# Check if HTTPS listener already exists
HTTPS_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
  --query 'Listeners[?Port==`443`].ListenerArn | [0]' --output text \
  --profile "$PROFILE" --region "$REGION" 2>/dev/null)

if [ -n "$HTTPS_ARN" ] && [ "$HTTPS_ARN" != "None" ]; then
  echo "→ Updating existing HTTPS listener certificate..."
  aws elbv2 modify-listener --listener-arn "$HTTPS_ARN" \
    --certificates CertificateArn="$CERT_ARN" \
    --profile "$PROFILE" --region "$REGION" --output text --query 'Listeners[0].ListenerArn'
else
  echo "→ Creating HTTPS listener on ALB..."
  aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" \
    --protocol HTTPS --port 443 \
    --certificates CertificateArn="$CERT_ARN" \
    --default-actions Type=forward,TargetGroupArn="$TG_ARN" \
    --profile "$PROFILE" --region "$REGION" --output text --query 'Listeners[0].ListenerArn'
fi

# Update .env.deploy
sed -i '/^DASHBOARD_URL=/d' "$SCRIPT_DIR/.env.deploy"
echo "DASHBOARD_URL=https://${DOMAIN}" >> "$SCRIPT_DIR/.env.deploy"

echo ""
echo "✓ HTTPS listener configured"
echo "✓ DASHBOARD_URL=https://${DOMAIN} → .env.deploy"
echo ""
echo "确保 DNS 已配置: ${DOMAIN} CNAME → $(aws elbv2 describe-load-balancers --names $ALB_NAME \
  --query 'LoadBalancers[0].DNSName' --output text --profile "$PROFILE" --region "$REGION")"
