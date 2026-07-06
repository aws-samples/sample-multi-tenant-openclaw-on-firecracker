#!/usr/bin/env bash
# LiteLLM 网关起栈脚本（在堡垒机 ~/openclaw/deploy/litellm/ 跑）。
# 做四件事：① 生成缺失的 master_key / pg 密码写 .env(600) ② 把 config 的
# __GUARDRAIL_ID__ sed 成真实 ID 产出 config.runtime.yaml ③ compose up
# ④ 等 4000 healthy。所有密钥 openssl rand 现生成，绝不硬编码、绝不迁旧账号值。
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env"
EXAMPLE=".env.example"
SRC_CONFIG="../runtime-config-export/litellm-config.yaml"   # 仓库内权威 config（master_key 已脱敏，guardrail 占位）
RUNTIME_CONFIG="config.runtime.yaml"                          # 注入后产物，不提交

# --- 1. .env：缺失项现生成 ---
if [ ! -f "$ENV_FILE" ]; then
  cp "$EXAMPLE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "[up] 从 $EXAMPLE 初始化 $ENV_FILE (600)"
fi
chmod 600 "$ENV_FILE"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

gen_if_empty() {  # $1=var名 $2=生成命令
  local name="$1" cmd="$2" cur
  cur="$(eval "echo \${$name:-}")"
  if [ -z "$cur" ]; then
    local val; val="$(eval "$cmd")"
    # 覆盖写回 .env（保留其它行）
    if grep -q "^${name}=" "$ENV_FILE"; then
      sed -i "s|^${name}=.*|${name}=${val}|" "$ENV_FILE"
    else
      echo "${name}=${val}" >> "$ENV_FILE"
    fi
    export "${name}=${val}"
    echo "[up] 现生成 ${name}（已写回 .env，值脱敏不打印）"
  fi
}

gen_if_empty LITELLM_MASTER_KEY 'echo sk-$(openssl rand -hex 32)'
gen_if_empty POSTGRES_PASSWORD  'openssl rand -hex 24'
: "${AWS_REGION:=ap-southeast-1}"
# #80 — guardrail id 先查 SSM(栈内 CfnGuardrail 写入的 /openclaw/bedrock-guardrail-id),
# 环境变量显式指定优先(便于本地调试),存量账号 SSM 没值时兜底走 od6s8sm533fs(现存 795 账号)。
# 后者在 security.guardrail_managed_by_stack 切开后应删,现在保留是为了让存量部署不断服。
if [ -z "${GUARDRAIL_ID:-}" ]; then
  GUARDRAIL_ID="$(aws ssm get-parameter --name /openclaw/bedrock-guardrail-id \
    --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null || true)"
fi
: "${GUARDRAIL_ID:=od6s8sm533fs}"
echo "[up] guardrail id: ${GUARDRAIL_ID} (SSM 优先,兜底=od6s8sm533fs)"
chmod 600 "$ENV_FILE"

# --- 2. guardrail 注入 ---
if [ ! -f "$SRC_CONFIG" ]; then
  echo "[up][ERR] 找不到权威 config: $SRC_CONFIG" >&2; exit 1
fi
sed "s|__GUARDRAIL_ID__|${GUARDRAIL_ID}|g" "$SRC_CONFIG" > "$RUNTIME_CONFIG"
if grep -q "__GUARDRAIL_ID__" "$RUNTIME_CONFIG"; then
  echo "[up][ERR] guardrail 注入失败，runtime config 仍含占位符" >&2; exit 1
fi
# master_key 在 config 里仍是 [REDACTED] 占位；运行态由容器环境变量 LITELLM_MASTER_KEY 覆盖。
# 把 config 里的 master_key 行改成引用环境变量，避免 [REDACTED] 字面值被当成真 key。
sed -i 's|^\(\s*master_key:\).*|\1 os.environ/LITELLM_MASTER_KEY|' "$RUNTIME_CONFIG"
echo "[up] guardrail 注入完成 -> $RUNTIME_CONFIG（ID=${GUARDRAIL_ID}，master_key 改引用 env）"

# --- 3. compose up ---
echo "[up] docker compose up ..."
# compose 自动读当前目录 .env；不用 --env-file（部分 docker 版本不识别该全局 flag）。
docker compose -f docker-compose.litellm.yml up -d

# --- 4. 等 healthy ---
echo "[up] 等 litellm:4000 healthy ..."
for i in $(seq 1 40); do
  if curl -sf --max-time 4 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1; then
    echo "[up] OK: 4000 已 healthy（第 ${i} 次探测）"
    docker compose -f docker-compose.litellm.yml ps
    exit 0
  fi
  sleep 3
done
echo "[up][ERR] 等待超时，看日志：docker compose -f docker-compose.litellm.yml logs --tail=80 litellm" >&2
exit 1
