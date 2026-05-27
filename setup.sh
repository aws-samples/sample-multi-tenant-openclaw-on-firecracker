#!/bin/bash
#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# 部署 CDK stack 并导出环境信息到 .env.deploy
#
# Usage:
#   Single-domain (legacy):
#     ./setup.sh <region> <profile> [--domain <domain>] [--cert <acm-arn>]
#   Dual-domain (1.3.4+ recommended for production):
#     ./setup.sh <region> <profile> \
#       --console-domain console.example.com --console-cert <acm-arn> \
#       --app-domain     app.example.com     --app-cert     <acm-arn>
#
#   --domain          legacy: 单域名（console + tenant dashboard 共用）
#   --cert            legacy: us-east-1 ACM 证书 ARN
#   --console-domain  console 专属域名（推荐生产用）
#   --console-cert    console 域名的 us-east-1 ACM cert ARN
#   --app-domain      tenant dashboard 专属域名
#   --app-cert        app 域名的 us-east-1 ACM cert ARN
#
# 双域名模式下 Cognito session cookie 会自动 scope 到 console-domain，
# 物理上隔离 console 凭证和 tenant 应用，是多租户场景的安全最佳实践。
set -euo pipefail

REGION="${1:?Usage: ./setup.sh <region> <profile> [--domain | --console-domain & --app-domain]}"
PROFILE="${2:?Usage: ./setup.sh <region> <profile> [--domain | --console-domain & --app-domain]}"
shift 2

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Parse domain flags. Both legacy single-domain and new dual-domain are supported.
DOMAIN_FLAG=""; CERT_FLAG=""
CONSOLE_DOMAIN_FLAG=""; CONSOLE_CERT_FLAG=""
APP_DOMAIN_FLAG=""; APP_CERT_FLAG=""
DOMAIN_SET=false; CERT_SET=false
CONSOLE_DOMAIN_SET=false; CONSOLE_CERT_SET=false
APP_DOMAIN_SET=false; APP_CERT_SET=false
CDK_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --domain)         DOMAIN_FLAG="$2";         DOMAIN_SET=true;         shift 2 ;;
    --cert)           CERT_FLAG="$2";           CERT_SET=true;           shift 2 ;;
    --console-domain) CONSOLE_DOMAIN_FLAG="$2"; CONSOLE_DOMAIN_SET=true; shift 2 ;;
    --console-cert)   CONSOLE_CERT_FLAG="$2";   CONSOLE_CERT_SET=true;   shift 2 ;;
    --app-domain)     APP_DOMAIN_FLAG="$2";     APP_DOMAIN_SET=true;     shift 2 ;;
    --app-cert)       APP_CERT_FLAG="$2";       APP_CERT_SET=true;       shift 2 ;;
    *) CDK_ARGS+=("$1"); shift ;;
  esac
done

# If any domain flag provided, write into config.yml (overrides existing values)
ANY_DOMAIN_FLAG=false
if $DOMAIN_SET || $CERT_SET || $CONSOLE_DOMAIN_SET || $CONSOLE_CERT_SET || $APP_DOMAIN_SET || $APP_CERT_SET; then
  ANY_DOMAIN_FLAG=true
fi

if $ANY_DOMAIN_FLAG; then
  DOMAIN="$DOMAIN_FLAG" CERT="$CERT_FLAG" \
  CONSOLE_DOMAIN="$CONSOLE_DOMAIN_FLAG" CONSOLE_CERT="$CONSOLE_CERT_FLAG" \
  APP_DOMAIN="$APP_DOMAIN_FLAG"         APP_CERT="$APP_CERT_FLAG" \
  DS="$DOMAIN_SET" CS="$CERT_SET" \
  CDS="$CONSOLE_DOMAIN_SET" CCS="$CONSOLE_CERT_SET" \
  ADS="$APP_DOMAIN_SET"     ACS="$APP_CERT_SET" \
  python3 - <<'PYEOF'
import os, re, pathlib
cfg_path = pathlib.Path("config.yml")
text = cfg_path.read_text()
has_section = re.search(r"^cloudfront:\s*$", text, re.MULTILINE)
if not has_section:
    sep = "" if text.endswith("\n") else "\n"
    text += (
        f"{sep}\n# ========== CloudFront 自定义域名 (可选) ==========\n"
        "# 1.3.4+: 推荐使用 console_domain + app_domain 双域名模式（安全分离）\n"
        "# 老的 custom_domain 仍兼容（单域名模式）\n"
        "cloudfront:\n"
        "  custom_domain: \"\"\n"
        "  acm_cert_arn: \"\"\n"
        "  console_domain: \"\"\n"
        "  console_cert_arn: \"\"\n"
        "  app_domain: \"\"\n"
        "  app_cert_arn: \"\"\n"
    )

def set_key(text, key, val):
    pat = re.compile(rf"^(\s*{re.escape(key)}:\s*)(?:\"[^\"]*\"|'[^']*'|\S*)(\s*(?:#.*)?)$", re.MULTILINE)
    repl = lambda m: f'{m.group(1)}"{val}"{m.group(2)}'
    new, n = pat.subn(repl, text, count=1)
    if n == 0:
        new = re.sub(r"(^cloudfront:\s*$)", rf"\1\n  {key}: \"{val}\"", text, count=1, flags=re.MULTILINE)
    return new

# Apply flags only when explicitly set (preserve existing config.yml values otherwise)
if os.environ["DS"]  == "True": text = set_key(text, "custom_domain",    os.environ["DOMAIN"])
if os.environ["CS"]  == "True": text = set_key(text, "acm_cert_arn",     os.environ["CERT"])
if os.environ["CDS"] == "True": text = set_key(text, "console_domain",   os.environ["CONSOLE_DOMAIN"])
if os.environ["CCS"] == "True": text = set_key(text, "console_cert_arn", os.environ["CONSOLE_CERT"])
if os.environ["ADS"] == "True": text = set_key(text, "app_domain",       os.environ["APP_DOMAIN"])
if os.environ["ACS"] == "True": text = set_key(text, "app_cert_arn",     os.environ["APP_CERT"])

cfg_path.write_text(text)
print("✓ config.yml updated:")
for env_key, cfg_key in (("DOMAIN","custom_domain"), ("CERT","acm_cert_arn"),
                          ("CONSOLE_DOMAIN","console_domain"), ("CONSOLE_CERT","console_cert_arn"),
                          ("APP_DOMAIN","app_domain"), ("APP_CERT","app_cert_arn")):
    set_env = {"DOMAIN":"DS","CERT":"CS","CONSOLE_DOMAIN":"CDS","CONSOLE_CERT":"CCS",
               "APP_DOMAIN":"ADS","APP_CERT":"ACS"}[env_key]
    if os.environ[set_env] == "True":
        val = os.environ[env_key] or "<cleared>"
        # Don't print full ACM ARN
        if "cert" in cfg_key and val != "<cleared>":
            val = "<set>"
        print(f"  {cfg_key:18s} = {val}")
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

# 1.3.4 (#61): in dual-domain mode, OC_DASHBOARD_BASE points at app_domain
# (the per-tenant ALB-fronted distribution), while OC_CONSOLE_BASE +
# OC_COGNITO_REDIRECT_URI point at console_domain (Cognito-protected).
# In legacy single-mode, both equal DASHBOARD_URL — backward-compat preserved.
CONSOLE_BASE="${CONSOLE_URL:-${DASHBOARD_URL:-}}"
DASHBOARD_BASE="${DASHBOARD_URL:-}"
cat > "$SCRIPT_DIR/console/config.js" << CFGEOF
window.OC_DEFAULT_API_URL = "${API_URL:-}";
window.OC_DEFAULT_API_KEY = "${API_KEY:-}";
window.OC_CONSOLE_BASE = "${CONSOLE_BASE}";
window.OC_DASHBOARD_BASE = "${DASHBOARD_BASE}";
window.OC_DUAL_DOMAIN_MODE = "${DUAL_DOMAIN_MODE:-false}";
window.OC_VERSION = "${VERSION}";
window.OC_REGION = "${REGION:-}";
window.OC_ASSETS_BUCKET = "${ASSETS_BUCKET:-}";
window.OC_COGNITO_DOMAIN = "${COGNITO_DOMAIN:-}";
window.OC_COGNITO_CLIENT_ID = "${COGNITO_CLIENT_ID:-}";
window.OC_COGNITO_REDIRECT_URI = "${CONSOLE_BASE}/console/index.html";
CFGEOF
aws s3 sync "$SCRIPT_DIR/console/" "s3://${ASSETS_BUCKET}/console/" \
  --profile "$PROFILE" --region "$REGION" --quiet --delete
echo "✓ Console uploaded to s3://${ASSETS_BUCKET}/console/"
echo ""
if [ "${DUAL_DOMAIN_MODE:-false}" = "true" ]; then
  echo "→ Console URL:    ${CONSOLE_BASE}/console/index.html  (operator login)"
  echo "→ Dashboard URL:  ${DASHBOARD_BASE}/vm/<tenant-id>/   (per-tenant)"
  echo "  ✓ Dual-domain mode active — Cognito session physically isolated from tenant dashboards"
else
  echo "→ Console URL: ${CONSOLE_BASE}/console/index.html"
  if [ -z "${CUSTOM_DOMAIN:-}" ]; then
    echo "  ⚠️  Single-domain mode — for production, see README §Multi-Domain Setup"
  fi
fi
