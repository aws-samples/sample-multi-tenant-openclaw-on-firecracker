#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# 部署 CDK stack 并导出环境信息到 .env.deploy
# Usage: ./setup.sh <region> <profile> [--domain <domain>] [--cert <acm-arn>] [cdk-args...]
#   --domain "<domain>" 设置自定义域名 (留空 "" 取消)
#   --cert   "<arn>"    us-east-1 ACM 证书 ARN
set -euo pipefail

REGION="${1:?Usage: ./setup.sh <region> <profile> [--domain <domain>] [--cert <acm-arn>]}"
PROFILE="${2:?Usage: ./setup.sh <region> <profile> [--domain <domain>] [--cert <acm-arn>]}"
shift 2

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Parse optional --domain / --cert flags, leave the rest for CDK
DOMAIN_FLAG=""; CERT_FLAG=""
DOMAIN_SET=false; CERT_SET=false
CDK_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN_FLAG="$2"; DOMAIN_SET=true; shift 2 ;;
    --cert)   CERT_FLAG="$2";   CERT_SET=true;   shift 2 ;;
    *) CDK_ARGS+=("$1"); shift ;;
  esac
done

# If --domain/--cert provided, write into config.yml (overrides existing values)
if $DOMAIN_SET || $CERT_SET; then
  DOMAIN="$DOMAIN_FLAG" CERT="$CERT_FLAG" DS="$DOMAIN_SET" CS="$CERT_SET" python3 - <<'PYEOF'
import os, re, sys, pathlib
cfg_path = pathlib.Path("config.yml")
text = cfg_path.read_text()
has_section = re.search(r"^cloudfront:\s*$", text, re.MULTILINE)
if not has_section:
    sep = "" if text.endswith("\n") else "\n"
    text += f"{sep}\n# ========== CloudFront 自定义域名 (可选) ==========\ncloudfront:\n  custom_domain: \"\"\n  acm_cert_arn: \"\"\n"

def set_key(text, key, val):
    pat = re.compile(rf"^(\s*{re.escape(key)}:\s*)(?:\"[^\"]*\"|'[^']*'|\S*)(\s*(?:#.*)?)$", re.MULTILINE)
    repl = lambda m: f'{m.group(1)}"{val}"{m.group(2)}'
    new, n = pat.subn(repl, text, count=1)
    if n == 0:
        # Key missing under cloudfront: append under the section
        new = re.sub(r"(^cloudfront:\s*$)", rf"\1\n  {key}: \"{val}\"", text, count=1, flags=re.MULTILINE)
    return new

if os.environ["DS"] == "True":
    text = set_key(text, "custom_domain", os.environ["DOMAIN"])
if os.environ["CS"] == "True":
    text = set_key(text, "acm_cert_arn", os.environ["CERT"])

cfg_path.write_text(text)
print(f"✓ config.yml updated (custom_domain={os.environ.get('DOMAIN','<unchanged>')}, "
      f"acm_cert_arn={'<set>' if os.environ['CS']=='True' else '<unchanged>'})")
PYEOF
fi

# Auto-detect existing Cognito pool from prior deploy (1.1.x → 1.2.x upgrade path).
# 1.2.x changed the UserPoolDomain prefix; importing the pool keeps users from
# losing their accounts. The stack always recreates the domain + client itself,
# so only user_pool_id needs to be carried forward.
EXISTING_POOL=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' \
  --output text --profile "$PROFILE" --region "$REGION" 2>/dev/null || true)
if [ -n "${EXISTING_POOL:-}" ] && [ "$EXISTING_POOL" != "None" ]; then
  POOL="$EXISTING_POOL" python3 - <<'PYEOF'
import os, re, pathlib
cfg_path = pathlib.Path("config.yml")
text = cfg_path.read_text()
pool = os.environ["POOL"]

def current_value(key):
    m = re.search(rf'^\s*{re.escape(key)}:\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else None

if current_value("user_pool_id") == pool:
    raise SystemExit(0)

def upsert(text, key, val):
    pat = re.compile(rf'^(\s*){re.escape(key)}:\s*"[^"]*"\s*(?:#.*)?$', re.MULTILINE)
    if pat.search(text):
        return pat.sub(rf'\g<1>{key}: "{val}"', text, count=1)
    cpat = re.compile(rf'^(\s*)#\s*{re.escape(key)}:\s*"[^"]*"\s*(?:#.*)?$', re.MULTILINE)
    if cpat.search(text):
        return cpat.sub(rf'\g<1>{key}: "{val}"', text, count=1)
    return re.sub(r'(^console_auth:\s*\n(?:[ \t]+.*\n)*)',
                  rf'\g<1>  {key}: "{val}"\n', text, count=1, flags=re.MULTILINE)

text = upsert(text, "user_pool_id", pool)
cfg_path.write_text(text)
print(f"✓ config.yml: imported existing Cognito pool {pool[:25]}… (1.1.x → 1.2.x upgrade path)")
PYEOF
fi

PATH=".venv/bin:$PATH" cdk deploy -c region="$REGION" --profile "$PROFILE" --require-approval never ${CDK_ARGS[@]+"${CDK_ARGS[@]}"}

# Upload scripts to S3 (after deploy creates the bucket)
BUCKET=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].Outputs[?OutputKey==`AssetsBucket`].OutputValue' --output text \
  --profile "$PROFILE" --region "$REGION")
aws s3 cp "$SCRIPT_DIR/deploy/userdata/host-agent.py" "s3://${BUCKET}/deployment/scripts/host-agent.py" \
  --profile "$PROFILE" --region "$REGION" --quiet

# Re-deploy is a no-op if nothing changed but it forces ASG hosts to retry
# pulling scripts now that S3 has them — guards against the race where the
# host's user-data ran before scripts were uploaded.
echo "✓ Scripts uploaded; existing hosts will pick them up via init-host.sh retry loop"
aws s3 cp "$SCRIPT_DIR/deploy/userdata/adot-config.yaml" "s3://${BUCKET}/deployment/scripts/adot-config.yaml" \
  --profile "$PROFILE" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/backup-data.sh" "s3://${BUCKET}/deployment/scripts/backup-data.sh" \
  --profile "$PROFILE" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/launch-vm.sh" "s3://${BUCKET}/deployment/scripts/launch-vm.sh" \
  --profile "$PROFILE" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/stop-vm.sh" "s3://${BUCKET}/deployment/scripts/stop-vm.sh" \
  --profile "$PROFILE" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/clone-data.sh" "s3://${BUCKET}/deployment/scripts/clone-data.sh" \
  --profile "$PROFILE" --region "$REGION" --quiet

# 导出 stack outputs
echo "→ 导出部署信息..."
OUTPUTS=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output text \
  --profile "$PROFILE" --region "$REGION")

cat > "$SCRIPT_DIR/.env.deploy" << EOF
# Auto-generated by setup.sh — $(date -Iseconds)
REGION=$REGION
PROFILE=$PROFILE
$(echo "$OUTPUTS" | awk '{
  key=$1
  out=""
  for(i=1;i<=length(key);i++){
    c=substr(key,i,1)
    if(c ~ /[A-Z]/ && i>1) out=out"_"
    out=out toupper(c)
  }
  print out"="$2
}')
EOF

# 查询 API Key value (stack output 只有 ID)
API_KEY_ID=$(grep '^API_KEY_ID=' "$SCRIPT_DIR/.env.deploy" | cut -d= -f2)
if [ -n "$API_KEY_ID" ]; then
  API_KEY=$(aws apigateway get-api-key --api-key "$API_KEY_ID" --include-value \
    --query 'value' --output text --profile "$PROFILE" --region "$REGION")
  echo "API_KEY=$API_KEY" >> "$SCRIPT_DIR/.env.deploy"
fi

echo "✓ 环境信息已保存到 .env.deploy"
cat "$SCRIPT_DIR/.env.deploy"

# Upload console to S3 (generate config.js first)
source "$SCRIPT_DIR/.env.deploy"
VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('$SCRIPT_DIR/pyproject.toml','rb'))['project']['version'])" 2>/dev/null || echo "dev")
cat > "$SCRIPT_DIR/console/config.js" << CFGEOF
window.OC_DEFAULT_API_URL = "${API_URL:-}";
window.OC_DEFAULT_API_KEY = "${API_KEY:-}";
window.OC_DASHBOARD_BASE = "${DASHBOARD_URL:-}";
window.OC_VERSION = "${VERSION}";
window.OC_REGION = "${REGION:-}";
window.OC_ASSETS_BUCKET = "${ASSETS_BUCKET:-}";
window.OC_COGNITO_DOMAIN = "${COGNITO_DOMAIN:-}";
window.OC_COGNITO_CLIENT_ID = "${COGNITO_CLIENT_ID:-}";
window.OC_COGNITO_REDIRECT_URI = "${DASHBOARD_URL:-}/console/index.html";
CFGEOF
aws s3 sync "$SCRIPT_DIR/console/" "s3://${ASSETS_BUCKET}/console/" \
  --profile "$PROFILE" --region "$REGION" --quiet --delete
echo "✓ Console uploaded to s3://${ASSETS_BUCKET}/console/"
echo "→ Console URL: ${DASHBOARD_URL}/console/index.html"
