#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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

# Re-source updated .env.deploy so downstream steps see new DASHBOARD_URL
source "$SCRIPT_DIR/.env.deploy"

# ========== Cognito: add new callback/logout URLs (keep existing for smooth cutover) ==========
if [ -n "${COGNITO_USER_POOL_ID:-}" ] && [ -n "${COGNITO_CLIENT_ID:-}" ]; then
  echo ""
  echo "→ Updating Cognito App Client callback URLs..."
  NEW_CALLBACK="https://${DOMAIN}/console/index.html"

  # Fetch existing URLs, append new one if not present
  CLIENT_JSON=$(aws cognito-idp describe-user-pool-client \
    --user-pool-id "$COGNITO_USER_POOL_ID" \
    --client-id "$COGNITO_CLIENT_ID" \
    --profile "$PROFILE" --region "$REGION" --output json)

  NEW_CALLBACK="$NEW_CALLBACK" CLIENT_JSON="$CLIENT_JSON" python3 <<'PYEOF' > /tmp/cognito-update-args.json
import json, os, sys
client = json.loads(os.environ["CLIENT_JSON"])["UserPoolClient"]
new_url = os.environ["NEW_CALLBACK"]
callbacks = list(dict.fromkeys(client.get("CallbackURLs", []) + [new_url]))
logouts   = list(dict.fromkeys(client.get("LogoutURLs", []) + [new_url]))
args = {
    "UserPoolId": client["UserPoolId"],
    "ClientId": client["ClientId"],
    "CallbackURLs": callbacks,
    "LogoutURLs": logouts,
    "SupportedIdentityProviders": client.get("SupportedIdentityProviders", ["COGNITO"]),
    "AllowedOAuthFlows": client.get("AllowedOAuthFlows", ["implicit"]),
    "AllowedOAuthScopes": client.get("AllowedOAuthScopes", ["openid", "email"]),
    "AllowedOAuthFlowsUserPoolClient": client.get("AllowedOAuthFlowsUserPoolClient", True),
}
json.dump(args, sys.stdout)
PYEOF

  aws cognito-idp update-user-pool-client \
    --cli-input-json "file:///tmp/cognito-update-args.json" \
    --profile "$PROFILE" --region "$REGION" --output text --query 'UserPoolClient.ClientId' > /dev/null
  rm -f /tmp/cognito-update-args.json
  echo "✓ Cognito CallbackURLs now include: ${NEW_CALLBACK}"
fi

# ========== Console: regenerate config.js + upload to S3 ==========
if [ -n "${ASSETS_BUCKET:-}" ]; then
  echo ""
  echo "→ Regenerating console/config.js with new domain..."
  VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('$SCRIPT_DIR/pyproject.toml','rb'))['project']['version'])" 2>/dev/null || echo "dev")
  cat > "$SCRIPT_DIR/console/config.js" << CFGEOF
window.OC_DEFAULT_API_URL = "${API_URL:-}";
window.OC_DEFAULT_API_KEY = "${API_KEY:-}";
window.OC_DASHBOARD_BASE = "${DASHBOARD_URL:-}";
window.OC_VERSION = "${VERSION}";
window.OC_COGNITO_DOMAIN = "${COGNITO_DOMAIN:-}";
window.OC_COGNITO_CLIENT_ID = "${COGNITO_CLIENT_ID:-}";
window.OC_COGNITO_REDIRECT_URI = "${DASHBOARD_URL:-}/console/index.html";
CFGEOF
  aws s3 cp "$SCRIPT_DIR/console/config.js" "s3://${ASSETS_BUCKET}/console/config.js" \
    --profile "$PROFILE" --region "$REGION" --quiet
  echo "✓ config.js uploaded to s3://${ASSETS_BUCKET}/console/config.js"

  # Invalidate CloudFront cache for config.js so browsers pick up new redirect
  aws cloudfront create-invalidation --distribution-id "$CF_ID" \
    --paths "/console/config.js" "/console/index.html" \
    --profile "$PROFILE" --output text --query 'Invalidation.Id' > /dev/null
  echo "✓ CloudFront cache invalidated for /console/*"
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  All done. Next step: point DNS to CloudFront."
echo "═══════════════════════════════════════════════"
echo "  ${DOMAIN} CNAME → ${CF_DOMAIN}"
echo ""
echo "  Console URL: https://${DOMAIN}/console/index.html"
