#!/usr/bin/env bash
# setup-federation.sh — demo-marketplace 联邦测试床的一次性配置(开发用,不交付)。
#
# 做三件事(固定日本 region ap-northeast-1):
#   1) 建「二手电商 entry Cognito User Pool」+ Hosted UI 域 + app client(authorization_code+PKCE)
#      —— 这是模拟客户自有 IdP 的最小实现。demo 用户在这里注册/登录(账号密码)。
#   2) (可选,档 A 完整形态)把 entry pool 作为 upstream OIDC IdP 注册进本平台 ClawPool Cognito,
#      让本平台签发含 custom:tenant_user_id/platform_id 的 id_token。
#   3) 打印 marketplace.html / broker 需要的配置值(entryDomain/entryClientId/redirectUri/…)。
#
# 前置:AWS_PROFILE 指向能在日本 region 建 Cognito 的凭据(见 CLAUDE.local.md,不入库)。
# 幂等:重复跑先查已存在再建。凭据/pool-id 输出到 stdout,自己写进 CLAUDE.local.md,别提交。
#
# 状态:骨架脚本。真跑需有效日本 region 凭据 + 本平台 ClawPool 的 upstream IdP 写权限。
# 未真跑前标「待真机」(ADR 档 A 五步链条 step 4)。
set -euo pipefail

REGION="${REGION:-ap-northeast-1}"           # 固定日本
POOL_NAME="${POOL_NAME:-demo-marketplace-entry}"
DOMAIN_PREFIX="${DOMAIN_PREFIX:-demo-mkt-$(echo "$RANDOM" | md5 2>/dev/null | cut -c1-8 || echo demo)}"
CALLBACK="${CALLBACK:?需设 CALLBACK=marketplace.html 部署后的 URL}"
: "${AWS_PROFILE:?需设 AWS_PROFILE(日本 region 凭据)}"

echo "== demo-marketplace 联邦配置 · region=$REGION =="

# 1) entry pool(幂等:按名字查)
POOL_ID="$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='${POOL_NAME}'].Id | [0]" --output text 2>/dev/null || echo None)"
if [ "$POOL_ID" = "None" ] || [ -z "$POOL_ID" ]; then
  echo "→ 建 entry pool ${POOL_NAME}"
  POOL_ID="$(aws cognito-idp create-user-pool --pool-name "$POOL_NAME" --region "$REGION" \
    --auto-verified-attributes email \
    --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":false,"RequireNumbers":false,"RequireSymbols":false}}' \
    --query 'UserPool.Id' --output text)"
else
  echo "→ entry pool 已存在: $POOL_ID"
fi

# Hosted UI 域(幂等)
aws cognito-idp create-user-pool-domain --domain "$DOMAIN_PREFIX" --user-pool-id "$POOL_ID" --region "$REGION" 2>/dev/null \
  && echo "→ 建域 $DOMAIN_PREFIX" || echo "→ 域已存在/复用"

# app client(authorization_code + PKCE,public client 无 secret 便于 SPA)
CLIENT_ID="$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" --region "$REGION" \
  --client-name demo-mkt-spa --no-generate-secret \
  --allowed-o-auth-flows code --allowed-o-auth-scopes openid email profile \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers COGNITO \
  --callback-urls "$CALLBACK" --logout-urls "$CALLBACK" \
  --query 'UserPoolClient.ClientId' --output text 2>/dev/null || echo "")"

ENTRY_DOMAIN="https://${DOMAIN_PREFIX}.auth.${REGION}.amazoncognito.com"
ENTRY_ISSUER="https://cognito-idp.${REGION}.amazonaws.com/${POOL_ID}"

echo
echo "== marketplace.html / broker 配置(写进 CLAUDE.local.md,勿提交)=="
echo "  DEMO_CFG.entryDomain   = ${ENTRY_DOMAIN}"
echo "  DEMO_CFG.entryClientId = ${CLIENT_ID}"
echo "  DEMO_CFG.redirectUri   = ${CALLBACK}"
echo "  broker ENTRY_ISSUER    = ${ENTRY_ISSUER}"
echo "  broker ENTRY_JWKS_URL  = ${ENTRY_ISSUER}/.well-known/jwks.json"
echo "  broker PLATFORM_ID     = demo-marketplace"
echo "  broker CTRL_API_BASE   = <本平台控制面 API GW base>"
echo "  broker CTRL_API_KEY    = <x-api-key,[REDACTED],勿提交>"
echo
echo "下一步(档 A 完整形态,可选):把此 entry pool 作为 upstream OIDC IdP 注册进本平台 ClawPool Cognito:"
echo "  aws cognito-idp create-identity-provider --user-pool-id <ClawPool> --provider-name demo-marketplace \\"
echo "    --provider-type OIDC --provider-details \"oidc_issuer=${ENTRY_ISSUER},client_id=<claw侧client>,...\" --region <claw region>"
echo "  再在 ClawPool 加 Pre-Token-Generation Lambda 注入 custom:tenant_user_id/platform_id(照 aws-samples)。"
