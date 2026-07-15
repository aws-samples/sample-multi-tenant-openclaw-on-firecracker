#!/bin/bash
# #80 下半 — 铸 LiteLLM shared vkey 到 SSM /openclaw/litellm-shared-vkey。
#
# 为什么放 setup.sh(post-deploy 编排层)而不是 CloudFormation Custom Resource:
#   铸 vkey 要 POST 到运行中的 LiteLLM /key/generate(是 HTTP,不是 CFN 资源)。
#   塞进 CR 会让部署耦合 LiteLLM 健康态,CR 内等 healthy 就 timeout 回滚更脆。
#   setup.sh 已经是 post-deploy 编排层(cdk deploy 之后一串 S3 上传),铸 vkey
#   放这里天然按顺:cdk deploy → LiteLLM EC2 启动/healthy → 铸 vkey → 写 SSM
#   → host scale。评审拍板走这条路。
#
# 使用 (在 setup.sh 里):
#   REGION=ap-southeast-1 PROFILE=<aws-profile> bash deploy/lib/mint-shared-vkey.sh
#
# 环境变量 hook(单测/幂等控制):
#   REGION            — AWS region(必填)
#   PROFILE           — AWS profile;设为 "-" 用 instance role/env creds(堡垒机场景)
#   GSUFFIX           — stack _gsuffix(默认空;secret 名 openclaw-litellm${GSUFFIX})
#   MAX_HEALTH_WAIT_S — LiteLLM /health 轮询总超时(默认 600s)
#   POLL_INTERVAL_S   — /health 轮询间隔(默认 10s)
#   TENANT_BUDGET_USD — max_budget(默认 0=不设)
#   TENANT_RPM        — rpm_limit(默认 0=不设)
#   FORCE_REMINT      — 1=就算 SSM 已有值也重铸(轮换用);默认幂等,已有值直接跳过
#   VKEY_SSM_NAME     — SSM 参数名(默认 /openclaw/litellm-shared-vkey)
#   MASTER_KEY        — 显式传 master key(单测用);未传则从 Secrets Manager 拿
#
# 退出码:
#   0 — 已铸/幂等跳过/写 SSM 成功
#   1 — 前置条件不足(region/profile 缺)
#   2 — LiteLLM /health 等超时,vkey 未铸
#   3 — Secrets Manager 拿 master key 失败
#   4 — LiteLLM /key/generate 失败(非 200 或响应没 "key")
#   5 — SSM put-parameter 失败
#
# 铁律 #5(fail-loud):任何步骤失败就退非零、打诊断,绝不静默继续。

set -euo pipefail

: "${REGION:?REGION env is required}"
: "${PROFILE:?PROFILE env is required (use '-' for instance role)}"
: "${GSUFFIX:=}"
: "${MAX_HEALTH_WAIT_S:=600}"
: "${POLL_INTERVAL_S:=10}"
: "${TENANT_BUDGET_USD:=0}"
: "${TENANT_RPM:=0}"
: "${FORCE_REMINT:=0}"
: "${VKEY_SSM_NAME:=/openclaw/litellm-shared-vkey}"
: "${MASTER_KEY:=}"

# 拼 aws cli profile flag(- 表示不加 --profile,用 instance role)
if [ "$PROFILE" = "-" ]; then
  AWS_ARGS="--region $REGION"
else
  AWS_ARGS="--profile $PROFILE --region $REGION"
fi

log() { echo "[mint-shared-vkey] $*" >&2; }

# ---------- 0. 幂等:SSM 已有值且非 FORCE_REMINT 直接跳过 ----------
EXISTING="$(aws $AWS_ARGS ssm get-parameter --name "$VKEY_SSM_NAME" --with-decryption \
              --query 'Parameter.Value' --output text 2>/dev/null || true)"
# 干净空值(第一次) or 存量已铸;FORCE_REMINT=1 时强制重铸
if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ] && [ "$FORCE_REMINT" != "1" ]; then
  log "SSM 已存 shared vkey(FORCE_REMINT=0),跳过铸新 vkey(幂等)"
  exit 0
fi

# ---------- 1. 等 LiteLLM SSM /openclaw/litellm-host 就绪 ----------
# 无论 ai_gateway.url 填不填,CDK 都把 litellm-host 写在这个 SSM。
LITELLM_HOST=""
DEADLINE=$(( $(date +%s) + MAX_HEALTH_WAIT_S ))
log "等 SSM /openclaw/litellm-host 就绪(最多 ${MAX_HEALTH_WAIT_S}s)"
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  LITELLM_HOST="$(aws $AWS_ARGS ssm get-parameter --name /openclaw/litellm-host \
                    --query 'Parameter.Value' --output text 2>/dev/null || true)"
  if [ -n "$LITELLM_HOST" ] && [ "$LITELLM_HOST" != "None" ]; then
    log "拿到 LITELLM_HOST=$LITELLM_HOST"
    break
  fi
  sleep "$POLL_INTERVAL_S"
done
if [ -z "$LITELLM_HOST" ] || [ "$LITELLM_HOST" = "None" ]; then
  log "ERROR: 等 SSM /openclaw/litellm-host 超时(${MAX_HEALTH_WAIT_S}s),LiteLLM 网关未 boot"
  exit 2
fi

# 规范化 base url:接受 http://IP:4000/v1 或纯 IP;/health/liveliness 在根路径
case "$LITELLM_HOST" in
  http://*|https://*) BASE_URL="$LITELLM_HOST" ;;
  *) BASE_URL="http://${LITELLM_HOST}:4000/v1" ;;
esac
HEALTH_URL="${BASE_URL%/v1}/health/liveliness"

# ---------- 2. 等 LiteLLM /health/liveliness healthy ----------
log "等 $HEALTH_URL healthy(每 ${POLL_INTERVAL_S}s 探一次,复用同一 deadline)"
HEALTHY=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if curl --noproxy '*' -fsSL --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
    HEALTHY=1
    log "LiteLLM healthy"
    break
  fi
  sleep "$POLL_INTERVAL_S"
done
if [ "$HEALTHY" != "1" ]; then
  log "ERROR: LiteLLM /health/liveliness 等超时(${MAX_HEALTH_WAIT_S}s 总窗口),网关未起来"
  exit 2
fi

# ---------- 3. 拿 master key ----------
# 明式 MASTER_KEY(测试注入) > Secrets Manager(prod)。
if [ -z "$MASTER_KEY" ]; then
  SECRET_ID="openclaw-litellm${GSUFFIX}"
  log "从 Secrets Manager $SECRET_ID 取 master key"
  RAW="$(aws $AWS_ARGS secretsmanager get-secret-value --secret-id "$SECRET_ID" \
          --query SecretString --output text 2>/dev/null || true)"
  if [ -z "$RAW" ] || [ "$RAW" = "None" ]; then
    log "ERROR: 拿不到 Secrets Manager 值:$SECRET_ID"
    exit 3
  fi
  # secret 是 JSON {"user":"litellm","master_key":"..."}
  MASTER_KEY="$(echo "$RAW" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("master_key",""))' 2>/dev/null || true)"
  if [ -z "$MASTER_KEY" ]; then
    log "ERROR: Secrets Manager 值不是 {master_key: ...} 结构"
    exit 3
  fi
fi

# ---------- 4. POST /key/generate ----------
# #17 修复:LiteLLM 的管理端点(/key/generate、/health/*)在根路径,不带 /v1 前缀。
# BASE_URL 可能是 http://IP:4000/v1(见 :89),拼 "$BASE_URL/key/generate" 会得
# /v1/key/generate → 404 "Not Found"。与 HEALTH_URL(:91)同样剥掉尾部 /v1。
ROOT_URL="${BASE_URL%/v1}"
log "POST $ROOT_URL/key/generate 铸 shared vkey"
PAYLOAD_FILE="$(mktemp)"
trap 'rm -f "$PAYLOAD_FILE"' EXIT
python3 - "$PAYLOAD_FILE" "$TENANT_BUDGET_USD" "$TENANT_RPM" <<'PYEOF'
import json, sys
path, budget, rpm = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
payload = {
    "key_alias": "openclaw-shared",
    "metadata": {"purpose": "shared_vkey_default", "created_by": "setup.sh #80"},
}
if budget > 0:
    payload["max_budget"] = budget
if rpm > 0:
    payload["rpm_limit"] = int(rpm)
with open(path, "w") as f:
    json.dump(payload, f)
PYEOF
RESP_FILE="$(mktemp)"
trap 'rm -f "$PAYLOAD_FILE" "$RESP_FILE"' EXIT
HTTP_CODE="$(curl --noproxy '*' -sS --max-time 30 -o "$RESP_FILE" -w '%{http_code}' \
              -X POST "$ROOT_URL/key/generate" \
              -H "Authorization: Bearer $MASTER_KEY" \
              -H "Content-Type: application/json" \
              --data-binary "@$PAYLOAD_FILE" || echo "000")"
if [ "$HTTP_CODE" != "200" ]; then
  log "ERROR: /key/generate 返回 HTTP $HTTP_CODE;响应体(限 500 字节):"
  head -c 500 "$RESP_FILE" >&2 || true
  echo "" >&2
  exit 4
fi

VKEY="$(python3 -c 'import sys,json; d=json.load(open(sys.argv[1])); print(d.get("key",""))' "$RESP_FILE" 2>/dev/null || true)"
if [ -z "$VKEY" ] || ! printf '%s' "$VKEY" | grep -qE '^sk-'; then
  log "ERROR: /key/generate 200 但响应无 'key' 或格式不像 sk-*(响应 500 字节):"
  head -c 500 "$RESP_FILE" >&2 || true
  echo "" >&2
  exit 4
fi
log "拿到 shared vkey(前 8 字符=${VKEY:0:8}…,长度=${#VKEY})"

# ---------- 5. 写 SSM SecureString ----------
# #17 修复:显式指定 --key-id,用 host role 已被授权 Decrypt 的 CMK(alias/clawpool-general)。
# 若省略,SecureString 会用账号默认 alias/aws/ssm 加密 —— host instance role 无该 key 的
# kms:Decrypt 权限,导致 launch-vm 侧 `get-parameter --with-decryption` 报 AccessDenied、vkey 读空。
# 可用 VKEY_KMS_KEY_ID 覆盖(默认 alias/clawpool-general; 环境用同名 alias)。
: "${VKEY_KMS_KEY_ID:=alias/clawpool-general}"
if ! aws $AWS_ARGS ssm put-parameter \
       --name "$VKEY_SSM_NAME" \
       --type SecureString \
       --key-id "$VKEY_KMS_KEY_ID" \
       --value "$VKEY" \
       --overwrite >/dev/null 2>&1; then
  log "ERROR: aws ssm put-parameter $VKEY_SSM_NAME 失败(key-id=$VKEY_KMS_KEY_ID)"
  exit 5
fi
log "✓ shared vkey 已写 SSM $VKEY_SSM_NAME(SecureString)"

# ---------- 6. 反查确认(#4/#0-D 教训:改配置类必反查确认真生效)----------
BACK="$(aws $AWS_ARGS ssm get-parameter --name "$VKEY_SSM_NAME" --with-decryption \
         --query 'Parameter.Value' --output text 2>/dev/null || true)"
if [ "$BACK" != "$VKEY" ]; then
  log "ERROR: SSM 反查回值与写入不符(可能被并发覆盖?)"
  exit 5
fi
log "✓ SSM 反查确认 shared vkey 已生效"
exit 0
