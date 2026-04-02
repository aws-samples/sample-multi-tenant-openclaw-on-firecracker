#!/bin/bash
# 将自定义域名 + ACM 证书关联到 CloudFront
# 前置条件: 用户已申请 ACM 证书(在 us-east-1 区域)并完成验证
# 用法: ./bind-domain.sh <domain> <acm-certificate-arn>
# 示例: ./bind-domain.sh oc.example.com arn:aws:acm:us-east-1:123456:certificate/xxx
set -euo pipefail

DOMAIN="${1:?Usage: $0 <domain> <acm-certificate-arn-in-us-east-1>}"
CERT_ARN="${2:?Usage: $0 <domain> <acm-certificate-arn-in-us-east-1>}"

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$SCRIPT_DIR/.env.deploy"

# Validate inputs
if [[ ! "$DOMAIN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]]; then
  echo "❌ Invalid domain: $DOMAIN"; exit 1
fi
if [[ "$CERT_ARN" != *":us-east-1:"* ]]; then
  echo "❌ ACM certificate must be in us-east-1 for CloudFront"
  echo "   Got: $CERT_ARN"; exit 1
fi

# Find CloudFront distribution
CF_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='OpenClaw Dashboard'].Id | [0]" \
  --output text --profile "$PROFILE") || { echo "❌ Failed to query CloudFront"; exit 1; }

if [ -z "$CF_ID" ] || [ "$CF_ID" = "None" ]; then
  echo "❌ CloudFront distribution not found (Comment='OpenClaw Dashboard')"; exit 1
fi

echo "→ Updating CloudFront distribution ${CF_ID}..."

# Get config + ETag in one call, update via python
TMPFILE=$(mktemp /tmp/cf-bind.XXXXXX.json)
trap 'rm -f "$TMPFILE"' EXIT

aws cloudfront get-distribution-config --id "$CF_ID" --profile "$PROFILE" --output json > "$TMPFILE"

DOMAIN="$DOMAIN" CERT_ARN="$CERT_ARN" TMPFILE="$TMPFILE" python3 -c "
import json, os
with open(os.environ['TMPFILE']) as f:
    raw = json.load(f)
etag = raw['ETag']
cfg = raw['DistributionConfig']
cfg['Aliases'] = {'Quantity': 1, 'Items': [os.environ['DOMAIN']]}
cfg['ViewerCertificate'] = {
    'ACMCertificateArn': os.environ['CERT_ARN'],
    'SSLSupportMethod': 'sni-only',
    'MinimumProtocolVersion': 'TLSv1.2_2021',
}
with open(os.environ['TMPFILE'] + '.cfg', 'w') as f:
    json.dump(cfg, f)
with open(os.environ['TMPFILE'] + '.etag', 'w') as f:
    f.write(etag)
"

ETAG=$(cat "$TMPFILE.etag")
aws cloudfront update-distribution --id "$CF_ID" --if-match "$ETAG" \
  --distribution-config "file://${TMPFILE}.cfg" \
  --profile "$PROFILE" --output text --query 'Distribution.Id'
rm -f "$TMPFILE.cfg" "$TMPFILE.etag"

# Update .env.deploy
sed -i '/^DASHBOARD_URL=/d' "$SCRIPT_DIR/.env.deploy"
echo "DASHBOARD_URL=https://${DOMAIN}" >> "$SCRIPT_DIR/.env.deploy"

CF_DOMAIN=$(aws cloudfront get-distribution --id "$CF_ID" --profile "$PROFILE" \
  --query 'Distribution.DomainName' --output text)

echo ""
echo "✓ CloudFront distribution updated"
echo "✓ DASHBOARD_URL=https://${DOMAIN} → .env.deploy"
echo ""
echo "确保 DNS 已配置: ${DOMAIN} CNAME → ${CF_DOMAIN}"
