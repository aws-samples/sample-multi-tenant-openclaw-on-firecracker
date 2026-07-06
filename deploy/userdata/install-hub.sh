#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# install-hub.sh — deploy the claw-hub WS 中枢 as a metal-local systemd service
# (single-process transition mode, before the EKS multi-Pod cutover).
#
# WHY this exists (architecture):
#   chat UI (console/chat/index.html) --wss--> CloudFront /hub/* --> dashboard ALB
#     --> metal nginx :80  --(/hub/*)-->  127.0.0.1:8790  [ claw-hub ]
#                                                              ^ outbound wss
#                                          VM claw-channel --/  (dials HOST_TAP_IP:8790)
#
#   In single-process / metal mode the hub keeps its routing tables (channels,
#   frontends) in memory — exactly the local-Map behaviour cluster-routing.mjs
#   degrades to when CLAW_HUB_REDIS_ENDPOINT is unset. NO Redis, NO clustering.
#   The per-process random HUB_TOKEN_KEY is correct here (one process signs and
#   verifies its own session tokens). Clustered mode (EKS) is the other half of
#   the same server.mjs and is configured by the k8s Deployment, not this script.
#
# WHY a script (not a hot hand-edit):
#   This is the "改部署代码 → 重建" path. The hub source is pulled from S3
#   (deployment/hub/), the systemd unit + nginx /hub reverse proxy are written
#   declaratively, so a host rebuild (or `bash install-hub.sh` re-run) reproduces
#   the exact same hub. init-host.sh invokes this at host boot when
#   {{HUB_LOCAL_ENABLED}}=true; it is also safe to run by hand on a live host.
#
# SECURITY:
#   - The hub listens on 0.0.0.0:8790 but is NEVER exposed to 0.0.0.0/0 at the SG
#     layer: the openclaw-host-sg has no inbound 8790 rule, so 8790 is reachable
#     only from the host itself (nginx → 127.0.0.1:8790) and from the per-VM tap
#     links (channel → HOST_TAP_IP:8790, host-internal 172.16/16). The browser
#     reaches it only through nginx :80 → /hub (fronted by CloudFront + ALB).
#   - Zero credentials in the image: Cognito JWTs are verified with JWKS public
#     keys; the per-tenant channel HMAC secret is read from DDB (channel_secret),
#     minted by the control-plane Lambda — never stored here.
#   - CLAW_HUB_SHARED_TENANT_ACCESS defaults OFF (go-live secure default).
#
# Idempotent: safe to re-run. Args: none. Reads /etc/platform.env + SSM.
set -euo pipefail
log() { echo "[oc:hub] $(date +%H:%M:%S) $*"; }

HUB_DIR="/opt/claw-hub"
HUB_ENV="/etc/openclaw/hub.env"
HUB_PORT="${CLAW_HUB_PORT:-8790}"

# ── source platform.env for region / table / bucket (written by init-host.sh) ──
[ -f /etc/platform.env ] && source /etc/platform.env
REGION="${OC_REGION:-ap-southeast-1}"
TENANTS_TABLE="${TENANTS_TABLE:-openclaw-tenants}"
ASSETS_BUCKET="${ASSETS_BUCKET:-}"
if [ -z "${ASSETS_BUCKET}" ]; then
  log "FATAL: ASSETS_BUCKET empty in /etc/platform.env — cannot fetch hub source"
  exit 1
fi

AWS_BIN="$(command -v aws || echo /usr/local/bin/aws)"

# ── Step 1: Node.js 20 (arm64/x86 auto) via NodeSource, idempotent ──
if ! command -v node >/dev/null 2>&1 || [ "$(node -v 2>/dev/null | grep -oE '^v[0-9]+' || echo v0)" \< "v18" ]; then
  log "installing Node.js 20 (NodeSource)"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1
  apt-get -o DPkg::Lock::Timeout=60 install -y -qq nodejs >/dev/null 2>&1
fi
log "node $(node -v) / npm $(npm -v)"

# ── Step 2: pull hub source from S3 (deployment/hub/) ──
mkdir -p "${HUB_DIR}"
for f in server.mjs cluster-routing.mjs package.json; do
  "${AWS_BIN}" s3 cp "s3://${ASSETS_BUCKET}/deployment/hub/${f}" "${HUB_DIR}/${f}" \
    --region "${REGION}" --no-progress
done
# #136 拆分:server.mjs 是 composition root,业务逻辑在 lib/*.mjs,整目录拉取。
# 缺 lib/ 时 node 起不来(ERR_MODULE_NOT_FOUND),脚本 set -euo 下拉取失败即 fail-loud。
"${AWS_BIN}" s3 cp "s3://${ASSETS_BUCKET}/deployment/hub/lib/" "${HUB_DIR}/lib/" \
  --recursive --region "${REGION}" --no-progress
log "hub source synced to ${HUB_DIR}"

# ── Step 3: prod deps (no Redis needed in single-process; ioredis is optional
#    and only initialised when CLAW_HUB_REDIS_ENDPOINT is set — which it is NOT
#    here, so cluster-routing.mjs no-ops). --omit=dev keeps the footprint small. ──
( cd "${HUB_DIR}" && npm install --omit=dev --no-audit --no-fund >/dev/null 2>&1 )
log "npm deps installed"

# ── Step 4: hub env. Cognito pool/client resolved from SSM so the script stays
#    account-agnostic (cross-account rebuilds get the right pool). Falls back to
#    empty (hub then refuses to mint frontend tokens, which fails closed). ──
COGNITO_POOL="$(${AWS_BIN} ssm get-parameter --name /openclaw/cognito-user-pool-id \
  --region "${REGION}" --query Parameter.Value --output text 2>/dev/null || echo "")"
COGNITO_CLIENT="$(${AWS_BIN} ssm get-parameter --name /openclaw/cognito-client-id \
  --region "${REGION}" --query Parameter.Value --output text 2>/dev/null || echo "")"
# WI-002 — channel machine-user app client id (end-to-end Cognito). Empty unless
# console_auth.channel_cognito_auth is enabled (setup.sh only writes this param
# then). server.mjs treats empty as "channel Cognito disabled" → verifies the
# legacy HMAC path instead, so this is a safe no-op for non-opted-in deploys.
COGNITO_CHANNEL_CLIENT="$(${AWS_BIN} ssm get-parameter --name /openclaw/cognito-channel-client-id \
  --region "${REGION}" --query Parameter.Value --output text 2>/dev/null || echo "")"
[ "${COGNITO_POOL}" = "None" ] && COGNITO_POOL=""
[ "${COGNITO_CLIENT}" = "None" ] && COGNITO_CLIENT=""
[ "${COGNITO_CHANNEL_CLIENT}" = "None" ] && COGNITO_CHANNEL_CLIENT=""

mkdir -p /etc/openclaw
# NOTE: single-process metal mode. We intentionally do NOT set
# CLAW_HUB_CLUSTERED / CLAW_HUB_REDIS_ENDPOINT / POD_NAME — those are EKS-only.
# No CLAW_HUB_TOKEN_KEY either: server.mjs uses a per-process random key, which
# is correct for a single process (it both signs and verifies). The per-tenant
# channel HMAC secret is NOT here — the hub reads channel_secret from DDB.
cat > "${HUB_ENV}" <<ENVEOF
CLAW_HUB_PORT=${HUB_PORT}
AWS_REGION=${REGION}
OC_REGION=${REGION}
TENANTS_TABLE=${TENANTS_TABLE}
OC_ASSETS_BUCKET=${ASSETS_BUCKET}
COGNITO_REGION=${REGION}
COGNITO_USER_POOL_ID=${COGNITO_POOL}
COGNITO_CLIENT_ID=${COGNITO_CLIENT}
COGNITO_CHANNEL_CLIENT_ID=${COGNITO_CHANNEL_CLIENT}
CLAW_HUB_SHARED_TENANT_ACCESS=${CLAW_HUB_SHARED_TENANT_ACCESS:-false}
ENVEOF
chmod 640 "${HUB_ENV}"
log "wrote ${HUB_ENV} (pool=${COGNITO_POOL:-<unset>} shared_access=${CLAW_HUB_SHARED_TENANT_ACCESS:-false})"

# ── Step 5: systemd unit ──
cat > /etc/systemd/system/claw-hub.service <<UNITEOF
[Unit]
Description=claw-hub WS 中枢 (single-process metal mode)
After=network.target
# 崩溃重启过快被 StartLimit 限制时,systemctl reset-failed claw-hub 再起。
# StartLimit* keys belong in [Unit] (a known systemd gotcha — they are ignored
# with a warning if placed in [Service]).
StartLimitIntervalSec=0

[Service]
Type=simple
EnvironmentFile=${HUB_ENV}
ExecStart=/usr/bin/node ${HUB_DIR}/server.mjs
WorkingDirectory=${HUB_DIR}
Restart=always
RestartSec=3
# 最小权限:hub 不需要 root(8790 是非特权口,DDB/S3 走 instance role)。
User=ubuntu
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable claw-hub >/dev/null 2>&1
systemctl restart claw-hub
log "claw-hub.service started"

# ── Step 6: nginx /hub/* reverse proxy → 127.0.0.1:8790 with WS Upgrade ──
# The hub strips a leading /hub itself (server.mjs handles both metal-direct and
# CloudFront→EKS). We pass the path THROUGH unchanged (proxy_pass without a URI),
# so /hub/healthz, /hub/token, /hub/ws all reach the hub which then strips /hub.
# $connection_upgrade is defined in openclaw-proxy.conf's map block.
mkdir -p /etc/nginx/conf.d/tenants
cat > /etc/nginx/conf.d/tenants/00-claw-hub.conf <<NGINXEOF
location /hub/ {
    proxy_pass http://127.0.0.1:${HUB_PORT};
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$connection_upgrade;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 4000s;
    proxy_send_timeout 4000s;
}
NGINXEOF
nginx -t >/dev/null 2>&1 && systemctl reload nginx
log "nginx /hub/ proxy → 127.0.0.1:${HUB_PORT} (WS Upgrade passthrough) reloaded"

# ── Step 7: self-check ──
sleep 1
HC="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:${HUB_PORT}/healthz || echo 000)"
HC_NGINX="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/hub/healthz || echo 000)"
log "self-check: hub:${HUB_PORT}/healthz=${HC}  nginx /hub/healthz=${HC_NGINX}"
[ "${HC}" = "200" ] || { log "FATAL: hub health != 200"; exit 1; }
log "DONE claw-hub up on :${HUB_PORT} (metal single-process)"
