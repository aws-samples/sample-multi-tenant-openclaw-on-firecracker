#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -euo pipefail
# 1.3.2: trap any non-zero exit so we know which line failed even when
# stdout gets truncated by SSM's 8KB output limit. NOTE: do NOT pkill
# firecracker — once InstanceStart succeeds, the VM is genuinely up and
# any later cleanup step's failure (e.g. nginx reload race) shouldn't
# tear down a working VM. host-agent's auto-recovery + the Lambda's
# _verify_vm_actually_running probe handle the post-failure resync.
_oc_cleanup_on_err() {
  local rc=$?
  echo "[oc:launch] FAIL line=${BASH_LINENO[0]} rc=${rc} cmd=${BASH_COMMAND}" >&2
  # Only clean up resources allocated BEFORE firecracker started (tap, sock).
  # If FC is running, leave it alone — the VM may be perfectly healthy.
  if [ -n "${SOCK:-}" ] && [ -S "${SOCK}" ]; then
    if pgrep -f "api-sock ${SOCK}" >/dev/null 2>&1; then
      echo "[oc:launch] firecracker is running on ${SOCK}; leaving it alive" >&2
      exit $rc
    fi
  fi
  if [ -n "${TAP:-}" ]; then
    sudo ip link del "${TAP}" 2>/dev/null || true
  fi
  if [ -n "${VM_DIR:-}" ]; then
    sudo rm -f "${VM_DIR}/fc.sock" 2>/dev/null || true
  fi
  exit $rc
}
trap _oc_cleanup_on_err ERR
TENANT_ID="${1:?Usage: launch-vm.sh <tenant_id> <vm_num> [vcpu] [mem_mb] [config_template] [restore_backup_key] [scoped_skills]}"
VM_NUM="${2:?Usage: launch-vm.sh <tenant_id> <vm_num> [vcpu] [mem_mb] [config_template] [restore_backup_key] [scoped_skills]}"
VCPU="${3:-2}"
MEM_MB="${4:-4096}"
CONFIG_TEMPLATE="${5:-}"
RESTORE_KEY="${6:-}"
# 1.4.0 (#62) — comma-separated allow-list of skill names. Empty / "*"
# preserves the legacy v1.3.x broadcast behavior so old SSM commands
# without this 7th arg keep working unchanged.
SCOPED_SKILLS="${7:-}"
# task #15 — per-tenant LiteLLM vkey (8th arg). API Lambda mints it at
# create_tenant and passes it here; we inject it into openclaw.json's
# litellm.apiKey so this tenant's spend/budget bills to its own key. Empty
# preserves the shared image key (backward compatible with old SSM commands).
LITELLM_VKEY="${8:-}"
# channel_secret (9th arg) — the per-tenant hub HMAC secret, MINTED BY THE API
# Lambda at create_tenant and persisted to the DDB record BEFORE this script
# runs. We inject this exact value into openclaw.json so the in-VM channel signs
# with the same secret the hub verifies against (read from DDB). This kills the
# old startup race where launch-vm.sh `openssl rand`'d its own secret and relied
# on host-agent to SSH-read-back + mirror it to DDB ~15s later — by which time
# the channel had already exhausted its retry budget (token-fail/401) and given
# up ("agent offline" forever). Empty (legacy SSM commands without this arg)
# falls back to self-generating (preserves backward compat, but re-opens race).
INJECTED_CHANNEL_SECRET="${9:-}"
# chat_endpoint_enabled (10th arg) — per-tenant switch for the OpenAI-compatible
# gateway.http.endpoints.chatCompletions endpoint. DEFAULT OFF (empty / "0" /
# "false"): we keep deleting the endpoint (OpenClaw's secure default + this
# fork's policy — see the del() below and CLAUDE.md "chatCompletions 为什么不能
# 全局默认开"). Only when the API Lambda passes "1"/"true" (the tenant record's
# chat_endpoint_enabled flag) do we inject enabled:true for THAT tenant. Mitigations
# stay regardless: per-tenant gateway.auth.token + CloudFront/nginx reverse proxy +
# Bedrock Guardrail + LiteLLM vkey limit. Empty (legacy SSM commands) → off.
CHAT_EP_ENABLED="${10:-}"
# WI-002 (end-to-end Cognito) — 11th arg: base64(JSON) of the per-tenant Cognito
# machine-user creds the control plane provisioned: {region, clientId, username,
# password}. base64 avoids SSM quote-hell (the JSON has braces/quotes). When set,
# the channel signs in to Cognito (USER_PASSWORD_AUTH) and presents an access
# token to the hub instead of the legacy HMAC. Empty (legacy SSM commands / not
# yet provisioned) → fall back to channel_secret HMAC (graceful rollout). Both
# can be injected; the in-VM channel prefers Cognito when all fields are present.
INJECTED_COGNITO_B64="${11:-}"
# Caller may pass literal "" (quoted) as placeholder when only restore_key is set.
[ "${CONFIG_TEMPLATE}" = '""' ] && CONFIG_TEMPLATE=""
[ "${RESTORE_KEY}" = '""' ] && RESTORE_KEY=""
[ "${SCOPED_SKILLS}" = '""' ] && SCOPED_SKILLS=""
[ "${LITELLM_VKEY}" = '""' ] && LITELLM_VKEY=""
[ "${INJECTED_CHANNEL_SECRET}" = '""' ] && INJECTED_CHANNEL_SECRET=""
[ "${CHAT_EP_ENABLED}" = '""' ] && CHAT_EP_ENABLED=""
[ "${INJECTED_COGNITO_B64}" = '""' ] && INJECTED_COGNITO_B64=""
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
[ -f /etc/platform.env ] && source /etc/platform.env
mkdir -p ${VM_DIR}
rm -f ${VM_DIR}/.stopped
SOCK="${VM_DIR}/fc.sock"
TAP="tap-vm${VM_NUM}"
# ── Addressing: one /30 point-to-point link per VM (host .+1 / guest .+2) ──
# The old scheme mapped vm_num directly to the 3rd octet
# (SUBNET_PREFIX.<vm_num>.{1,2}/24), which capped a host at 254 VMs (3rd octet
# ≤254) and 255 MACs (single-byte suffix). To pack 480+ VMs on one big host we
# lay out a contiguous /30 per VM across the whole SUBNET_PREFIX/16:
#   block       = (vm_num-1) * 4            # 4 addrs per /30 (net/host/guest/bcast)
#   3rd octet   = block / 256
#   4th base    = block % 256
#   HOST_TAP_IP = SUBNET_PREFIX.<o3>.<base+1>   (the /30 host end)
#   GUEST_IP    = SUBNET_PREFIX.<o3>.<base+2>   (the /30 guest end)
# vm_num=1 → host .0.1 / guest .0.2 ; vm_num=480 → host .7.125 / guest .7.126.
# All inside SUBNET_PREFIX/16, so the /16 east-west DROP still covers every VM.
# MAC encodes vm_num in the last TWO bytes so it never overflows a single byte.
SUBNET_PREFIX="${SUBNET_PREFIX:-10.0}"
_BLOCK=$(( (VM_NUM - 1) * 4 ))
_O3=$(( _BLOCK / 256 ))
_O4=$(( _BLOCK % 256 ))
HOST_TAP_IP="${SUBNET_PREFIX}.${_O3}.$(( _O4 + 1 ))"
GUEST_IP="${SUBNET_PREFIX}.${_O3}.$(( _O4 + 2 ))"
GUEST_MAC="AA:FC:00:00:$(printf '%02x:%02x' $(( VM_NUM / 256 )) $(( VM_NUM % 256 )))"
log() { echo "[oc:launch] $(date +%H:%M:%S) $*"; }

# Write VM metadata for host-agent discovery
cat > "${VM_DIR}/vm.json" << VMEOF
{"tenant_id":"${TENANT_ID}","vm_num":${VM_NUM},"guest_ip":"${GUEST_IP}","vcpu":${VCPU},"mem_mb":${MEM_MB},"config_template":"${CONFIG_TEMPLATE}"}
VMEOF

log "START ${TENANT_ID} vm${VM_NUM} ${VCPU}vCPU/${MEM_MB}MB"

# Cleanup previous instance
pkill -f "api-sock ${SOCK}" 2>/dev/null || true
sudo ip link del ${TAP} 2>/dev/null || true
rm -f ${SOCK}; sleep 0.5

# Prepare disks
log "preparing disks..."
T0=$SECONDS
ROOTFS="/data/firecracker-assets/openclaw-rootfs.ext4"
DATA_TPL="/data/firecracker-assets/openclaw-data-template.ext4"
DATA_SIZE=$(stat -c%s ${DATA_TPL})
# Immutable authority disk (identity files + ops skills). Shared, read-only,
# attached to every VM as /dev/vdd with is_read_only:true. Optional: if the
# asset is absent (older image set), we skip the 4th drive so launch still works.
IMMUTABLE_TPL="/data/firecracker-assets/openclaw-immutable.ext4"

# Overlay: sparse file for rootfs copy-on-write (shared read-only rootfs + per-VM writable layer)
OVERLAY="${VM_DIR}/overlay.ext4"
if [ ! -f "${OVERLAY}" ]; then
  truncate -s "${ROOTFS_OVERLAY_MB:-8192}M" ${OVERLAY}
  mkfs.ext4 -q ${OVERLAY}
fi

# Data volume: first-time initialize, subsequent launches reuse existing.
#   - With RESTORE_KEY: download backup from S3, decompress, e2fsck. Size is whatever the backup is.
#   - Without:          sparse-copy from template. Size must match DATA_SIZE.
DATA_VOL="${VM_DIR}/data.ext4"
NEW_DATA=false
NEEDS_INIT=false
if [ ! -f "${DATA_VOL}" ]; then
  NEEDS_INIT=true
elif [ -z "${RESTORE_KEY}" ] && [ "$(stat -c%s ${DATA_VOL})" != "${DATA_SIZE}" ]; then
  # Template size drift — rebuild only if we're using the template path.
  NEEDS_INIT=true
fi
if [ "${NEEDS_INIT}" = "true" ]; then
  rm -f ${DATA_VOL}
  if [ -n "${RESTORE_KEY}" ]; then
    log "restoring from s3://${ASSETS_BUCKET}/${RESTORE_KEY}"
    aws s3 cp "s3://${ASSETS_BUCKET}/${RESTORE_KEY}" "/tmp/restore-${TENANT_ID}.gz" \
      --region "${OC_REGION:-ap-northeast-1}" --quiet
    pigz -d -c "/tmp/restore-${TENANT_ID}.gz" > ${DATA_VOL}
    rm -f "/tmp/restore-${TENANT_ID}.gz"
    # 1.3.1+1.3.2: backup-data.sh dumps the ext4 image while the VM is
    # *paused* (vCPUs frozen but pending journal not committed). On
    # restore, e2fsck must replay that journal — making it return:
    #   0 = clean
    #   1 = errors corrected (most common after journal replay)
    #   2 = errors corrected, system should reboot (we ignore reboot)
    #   4 = errors NOT corrected (real damage)
    #   8 = operational error (e.g. file IO issue or unsupported feature)
    #  16 = usage / syntax error (we never trigger this)
    # We accept 0/1/2/8: 8 happens on Firecracker's own e2fsck binary
    # when the backup uses ext4 features the host's e2fsck doesn't know
    # about (forward-compat issue, not corruption — the guest kernel
    # will mount it fine). Reject 4 and 16.
    fsck_rc=0
    e2fsck -fy ${DATA_VOL} >/dev/null 2>&1 || fsck_rc=$?
    if [ $fsck_rc -eq 4 ] || [ $fsck_rc -eq 16 ]; then
      log "FATAL: backup filesystem check failed (e2fsck rc=${fsck_rc})"
      exit 1
    fi
    log "restored $(stat -c%s ${DATA_VOL}) bytes (e2fsck rc=${fsck_rc})"
  else
    cp --sparse=always ${DATA_TPL} ${DATA_VOL}
  fi
  NEW_DATA=true
fi
log "disks ready ($((SECONDS-T0))s)"

# Inject shared skills into data disk
SHARED_SKILLS="/data/shared-skills"
MOUNT_TMP="/tmp/data-mount-${TENANT_ID}"
mkdir -p ${MOUNT_TMP}
sudo mount ${DATA_VOL} ${MOUNT_TMP}
# Skills (1.4.0 #62: optional per-tenant scope via $SCOPED_SKILLS comma-list)
if [ -d "${SHARED_SKILLS}" ] && [ "$(ls -A ${SHARED_SKILLS} 2>/dev/null)" ]; then
  if [ -z "${SCOPED_SKILLS}" ] || [ "${SCOPED_SKILLS}" = "*" ]; then
    log "injecting all shared skills (broadcast mode)"
    mkdir -p ${MOUNT_TMP}/.openclaw/skills
    cp -r ${SHARED_SKILLS}/* ${MOUNT_TMP}/.openclaw/skills/ 2>/dev/null || true
  else
    log "injecting scoped skills: ${SCOPED_SKILLS}"
    mkdir -p ${MOUNT_TMP}/.openclaw/skills
    IFS=',' read -ra SKILL_LIST <<< "${SCOPED_SKILLS}"
    for skill in "${SKILL_LIST[@]}"; do
      skill_dir="${SHARED_SKILLS}/${skill}"
      if [ -d "${skill_dir}" ]; then
        cp -r "${skill_dir}" ${MOUNT_TMP}/.openclaw/skills/ 2>/dev/null || true
      else
        log "  skipped unknown skill: ${skill}"
      fi
    done
  fi
  sudo chown -R 1000:1000 ${MOUNT_TMP}/.openclaw/skills
  log "skills injected"
fi
# Configure openclaw.json
OC_JSON="${MOUNT_TMP}/.openclaw/openclaw.json"
if [ -f "${OC_JSON}" ] && command -v jq &>/dev/null; then
  if [ "$NEW_DATA" = "true" ]; then
    # Download custom template from S3 (if specified)
    if [ -n "${CONFIG_TEMPLATE}" ] && [ -n "${ASSETS_BUCKET:-}" ]; then
      aws s3 cp "s3://${ASSETS_BUCKET}/templates/openclaw/${CONFIG_TEMPLATE}/openclaw.json" "${OC_JSON}" --region "${OC_REGION:-ap-northeast-1}" --quiet
      log "config template '${CONFIG_TEMPLATE}' applied"
    fi
    # Inject platform config. Two SEPARATE, ORTHOGONAL auth layers:
    #   (a) gateway.auth.token  — protects the control plane / control UI.
    #   (b) channels.claw-channel.secret — HMAC secret for the signed C-end
    #       webhook (the mini-app's backend signs with the same secret using the
    #       Cognito-verified `sub`). This is the user-message path; it does NOT
    #       touch gateway.auth.token.
    #
    # We DO NOT open the bare OpenAI chatCompletions HTTP endpoint anymore, and we
    # DO NOT weaken control-UI auth. The previous bb-branch patch
    # (allowedOrigins=["*"] + dangerouslyDisableDeviceAuth + chatCompletions.enabled)
    # was a wide-open, device-auth-off, token-in-browser surface. We avoid that
    # entirely: gateway.http={}, all messages flow over the claw-channel.
    # allowedOrigins is scoped to the CloudFront origin instead of "*".
    # chatCompletions stays at its OpenClaw default (off).
    NEW_TOKEN=$(openssl rand -hex 24)
    # claw-channel (outbound WS to the hub):
    #   appSecret  — per-VM hex secret. The channel signs HMAC("{appId}:{ts}",
    #                appSecret) to fetch a hub token. host-agent mirrors this same
    #                secret into the tenant's DDB record (channel_secret), so the
    #                self-hosted claw-hub can verify the channel's registration.
    #   appId      — the tenant id (single-account model: appId == tenant).
    #   hubUrl/wsUrl — where the channel dials OUT to register (the hub). The
    #                  browser only ever talks to the hub; the appSecret/gateway
    #                  token never reach the browser.
    # Prefer the control-plane-minted secret (already in DDB before boot → hub
    # verifies the channel's FIRST registration, no race). Only self-generate if
    # the caller didn't pass one (legacy path; host-agent read-back still mirrors
    # it to DDB but with the old startup race).
    if [ -n "${INJECTED_CHANNEL_SECRET}" ]; then
      CHANNEL_SECRET="${INJECTED_CHANNEL_SECRET}"
      log "using control-plane-minted channel_secret (race-free hub handshake)"
    else
      CHANNEL_SECRET=$(openssl rand -hex 32)
      log "channel_secret self-generated (legacy; relies on host-agent DDB mirror)"
    fi
    CF_ORIGIN="${CLOUDFRONT_ORIGIN:-https://d14etqjt4kt9t4.cloudfront.net}"
    HUB_URL="${CLAW_HUB_URL:-http://${HOST_TAP_IP:-172.16.0.1}:8790}"
    HUB_WS="${CLAW_HUB_WS:-ws://${HOST_TAP_IP:-172.16.0.1}:8790}"
    # WI-002 — decode the per-tenant Cognito machine-user creds (if provisioned).
    # base64(JSON) → individual vars. Empty / malformed → all blank → the channel
    # uses the HMAC path (graceful rollout). We do NOT log the password.
    COG_REGION=""; COG_CLIENT_ID=""; COG_USERNAME=""; COG_PASSWORD=""
    if [ -n "${INJECTED_COGNITO_B64}" ]; then
      _cog_json="$(printf '%s' "${INJECTED_COGNITO_B64}" | base64 -d 2>/dev/null || true)"
      if [ -n "${_cog_json}" ] && printf '%s' "${_cog_json}" | jq -e . >/dev/null 2>&1; then
        COG_REGION="$(printf '%s' "${_cog_json}" | jq -r '.region // ""')"
        COG_CLIENT_ID="$(printf '%s' "${_cog_json}" | jq -r '.clientId // ""')"
        COG_USERNAME="$(printf '%s' "${_cog_json}" | jq -r '.username // ""')"
        COG_PASSWORD="$(printf '%s' "${_cog_json}" | jq -r '.password // ""')"
        if [ -n "${COG_REGION}" ] && [ -n "${COG_CLIENT_ID}" ] && [ -n "${COG_USERNAME}" ] && [ -n "${COG_PASSWORD}" ]; then
          log "using control-plane-provisioned Cognito machine-user (end-to-end Cognito; tenant=${COG_USERNAME})"
        else
          log "WARN: Cognito creds incomplete after decode — falling back to HMAC channel_secret"
          COG_REGION=""; COG_CLIENT_ID=""; COG_USERNAME=""; COG_PASSWORD=""
        fi
      else
        log "WARN: Cognito creds base64/JSON decode failed — falling back to HMAC channel_secret"
      fi
    fi
    # per-tenant chatCompletions: default off → del the endpoint (secure default).
    # Only when this tenant's flag is on do we set enabled:true instead of deleting.
    # The jq fragment is chosen at runtime so the default path is byte-identical to
    # the old unconditional del() (no behavior change for the 99% default-off case).
    case "${CHAT_EP_ENABLED}" in
      1|true|TRUE|yes|on)
        _CHAT_EP_JQ='.gateway.http.endpoints.chatCompletions.enabled = true'
        log "chatCompletions endpoint ENABLED for this tenant (per-tenant flag on; mitigations: gateway_token + reverse proxy + Guardrail + vkey limit)"
        ;;
      *)
        _CHAT_EP_JQ='del(.gateway.http.endpoints.chatCompletions)'
        ;;
    esac
    # Cognito fields are added only when all four are present; otherwise the
    # object keeps just the HMAC fields (legacy path). The in-VM channel's
    # hasCognitoCreds() then picks Cognito vs HMAC. HMAC fields stay either way
    # so a half-rolled-out fleet always has a working fallback.
    if [ -n "${COG_REGION}" ] && [ -n "${COG_CLIENT_ID}" ] && [ -n "${COG_USERNAME}" ] && [ -n "${COG_PASSWORD}" ]; then
      _COGNITO_JQ='| .channels["claw-channel"] += { "cognitoRegion": $cogregion, "cognitoClientId": $cogclient, "cognitoUsername": $coguser, "cognitoPassword": $cogpass }'
    else
      _COGNITO_JQ=''
    fi
    jq --arg t "$NEW_TOKEN" --arg s "$CHANNEL_SECRET" --arg origin "$CF_ORIGIN" \
       --arg appid "$TENANT_ID" --arg huburl "$HUB_URL" --arg hubws "$HUB_WS" \
       --arg cogregion "$COG_REGION" --arg cogclient "$COG_CLIENT_ID" \
       --arg coguser "$COG_USERNAME" --arg cogpass "$COG_PASSWORD" "
      .gateway.auth.token = \$t |
      .gateway.controlUi.allowedOrigins = [\$origin] |
      del(.gateway.controlUi.dangerouslyDisableDeviceAuth) |
      ${_CHAT_EP_JQ} |
      .channels = ((.channels // {}) + { \"claw-channel\": (((.channels // {})[\"claw-channel\"] // {}) + {
        \"enabled\": true, \"secret\": \$s, \"appId\": \$appid,
        \"appSecret\": \$s, \"hubUrl\": \$huburl, \"wsUrl\": \$hubws
      }) })
      ${_COGNITO_JQ}
    " "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
    # The mini-app reaches the agent via the claw-hub WS (browser -> hub ->
    # outbound channel), NOT any bare gateway endpoint. The HMAC secret lives only
    # here on the per-VM data disk + mirrored to DDB for the hub — never baked
    # into the read-only golden image, never sent to the browser.
    log "gateway token + channel hub config generated"
    # task #15 — per-tenant LiteLLM billing key. If the API minted a vkey for
    # this tenant, override the shared apiKey baked in the image so this VM's
    # spend/budget/rate-limit bills to its OWN key (per-tenant↔sub split).
    # vkey lives ONLY on the per-VM data disk (like channel_secret), never in
    # the read-only golden image. Empty → keep the shared key (backward compat).
    # apiKey 注入:优先 per-tenant vkey(计费拆分);无专属 vkey 则用 shared vkey
    # (LITELLM_SHARED_VKEY,init-host 从 SSM 拉)。镜像里 apiKey 是 __INJECT_AT_DEPLOY__
    # 占位,必须运行时替换成真 key,否则 agent 拿占位符当 key 调 LiteLLM → 401 →
    # "Something went wrong"(实测 demo 租户踩到)。两者都空才保留占位(会失败,WARN)。
    _LK="${LITELLM_VKEY:-${LITELLM_SHARED_VKEY:-}}"
    if [ -n "${_LK}" ]; then
      jq --arg vk "$_LK" '
        .models.providers.litellm.apiKey = $vk
      ' "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
      if [ -n "${LITELLM_VKEY}" ]; then log "per-tenant LiteLLM vkey injected (billing split)"; else log "shared LiteLLM vkey injected (no per-tenant vkey)"; fi
    else
      log "WARN: 无 LITELLM_VKEY 也无 LITELLM_SHARED_VKEY — apiKey 保留占位符,LLM 调用会 401。设 SSM /openclaw/litellm-shared-vkey。"
    fi
    # LiteLLM baseUrl:镜像烤死的 __LITELLM_HOST__ 默认 127.0.0.1 是错的(microVM 本地无
    # LiteLLM);运行时用 platform.env 的 LITELLM_HOST(init-host 从 SSM /openclaw/litellm-host
    # 拉=堡垒机内网 IP)改写,microVM 经 metal host 网络访问堡垒机:4000(同 VPC 实测可达)。
    # LiteLLM 地址部署环境相关,必须运行时注入不能烤死(跨账号/重建堡垒机 IP 会变)。
    if [ -n "${LITELLM_HOST}" ]; then
      # LITELLM_HOST 可能是纯 IP/host(旧契约)或完整 URL http://IP:4000/v1(SSM
      # /openclaw/litellm-host 现存完整 URL,stack.py user-data + setup.sh 都这么写)。
      # 规范化:已含 http:// 直接用;纯 host 才拼 http://host:4000/v1。否则双重拼接成
      # http://http://IP:4000/v1:4000/v1 → openclaw 调 LiteLLM 必失败(重建实撞根因)。
      case "${LITELLM_HOST}" in
        http://*|https://*) LITELLM_BASEURL="${LITELLM_HOST}" ;;
        *) LITELLM_BASEURL="http://${LITELLM_HOST}:4000/v1" ;;
      esac
      jq --arg h "${LITELLM_BASEURL}" '
        .models.providers.litellm.baseUrl = $h
      ' "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
      log "LiteLLM baseUrl set to ${LITELLM_BASEURL} (runtime inject, normalized)"
    else
      log "WARN: LITELLM_HOST empty — openclaw.json baseUrl keeps baked value (likely 127.0.0.1, LLM calls will fail). Set SSM /openclaw/litellm-host."
    fi
  fi
  sudo chown 1000:1000 "${OC_JSON}"
  # AgentCore Gateway MCP injection (if configured).
  #
  # OpenClaw 2026.5+ moved MCP servers from a top-level `mcpServers` key to
  # `mcp.servers.<name>` (verified against `openclaw mcp set` output;
  # `openclaw mcp list` reads from this same path). The old top-level
  # location and a brief intermediate `tools.mcpServers` location both
  # fail config validation now. We write through `.mcp.servers` to match
  # what the CLI itself uses.
  if [ -f /data/agentcore.env ]; then
    source /data/agentcore.env
    if [ -n "${AGENTCORE_GATEWAY_URL:-}" ]; then
      jq --arg url "$AGENTCORE_GATEWAY_URL" '
        (.mcp // {}) as $mcp |
        .mcp = ($mcp + {
          "servers": ((($mcp.servers // {})) + {
            "agentcore-gateway": {"url": $url, "transport": "streamable-http"}
          })
        })
      ' "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
      sudo chown 1000:1000 "${OC_JSON}"
      log "AgentCore Gateway MCP injected at .mcp.servers: ${AGENTCORE_GATEWAY_URL}"
    fi
  fi
fi
# 1.5.0 security: inject THIS host's public key so host-agent can SSH into
# the guest with key auth (no shared password). The key is per-host
# (init-host.sh generates it), so each VM trusts only its own host. uid/gid
# 1000 = the in-guest `agent` user that owns /home/agent (this data disk).
if [ -f /etc/openclaw/host_vm_key.pub ]; then
  sudo mkdir -p "${MOUNT_TMP}/.ssh"
  sudo cp /etc/openclaw/host_vm_key.pub "${MOUNT_TMP}/.ssh/authorized_keys"
  sudo chmod 700 "${MOUNT_TMP}/.ssh"
  sudo chmod 600 "${MOUNT_TMP}/.ssh/authorized_keys"
  sudo chown -R 1000:1000 "${MOUNT_TMP}/.ssh"
  log "injected host SSH public key into VM data disk"
fi
sudo umount ${MOUNT_TMP}
rmdir ${MOUNT_TMP} 2>/dev/null || true

# Network setup
log "setting up network tap=${TAP}..."
# 1.3.2: TUNSETIFF can transiently return EBUSY if a previous launch
# attempt left a tap-vmN partially set up — even after `ip link del`,
# the kernel briefly holds the name. Retry once after a short sleep.
_tuntap_add_with_retry() {
  if sudo ip tuntap add dev ${TAP} mode tap 2>/dev/null; then
    return 0
  fi
  log "tuntap add ${TAP} EBUSY, force-cleaning + retrying..."
  sudo ip link set ${TAP} down 2>/dev/null || true
  sudo ip link del ${TAP} 2>/dev/null || true
  # Kill anyone still holding a fd on this tap (rare, but covers a stale
  # firecracker that didn't get pkill'd by our trap).
  sudo lsof -t /sys/devices/virtual/net/${TAP} 2>/dev/null | xargs -r sudo kill -KILL 2>/dev/null || true
  sleep 2
  sudo ip tuntap add dev ${TAP} mode tap
}
_tuntap_add_with_retry
sudo ip addr add ${HOST_TAP_IP}/30 dev ${TAP}
sudo ip link set dev ${TAP} up
HOST_IFACE=$(ip route show default | awk '{print $5}' | head -1)
sudo sysctl -q -w net.ipv4.ip_forward=1
# ── SECURITY (multi-tenant isolation): block guest → instance metadata ──
# Without this, a tenant inside its microVM can reach the host's IMDS at
# 169.254.169.254 through the MASQUERADE rule below and steal the host EC2
# instance-profile credentials (which can read/write the shared assets bucket
# and the tenants/hosts tables — i.e. every other tenant's data). Drop all
# guest-originated traffic to the link-local IMDS range BEFORE the ACCEPT
# rules. -I inserts at the top so it always precedes the FORWARD ACCEPT.
# Also covers IMDSv6 (fd00:ec2::254) defensively.
sudo iptables -C FORWARD -i ${TAP} -d 169.254.169.254 -j DROP 2>/dev/null || \
  sudo iptables -I FORWARD 1 -i ${TAP} -d 169.254.169.254 -j DROP
sudo iptables -C FORWARD -i ${TAP} -d 169.254.169.253 -j DROP 2>/dev/null || \
  sudo iptables -I FORWARD 1 -i ${TAP} -d 169.254.169.253 -j DROP
# ── SECURITY (L2 east-west isolation): block guest → other tenants ──
# Each tenant gets its own /30 point-to-point link (SUBNET_PREFIX.<o3>.<base>/30)
# on its own tap; all links live inside the SUBNET_PREFIX/16 tenant supernet.
# Without this rule, the FORWARD ACCEPT below would happily route packets from
# this guest into ANOTHER tenant's /30 (same SUBNET_PREFIX/16, different tap) —
# routed by the host kernel, so the per-tap isolation is meaningless. Verified
# in load tests: with no DROP, cross-tenant ping = 0% loss and a neighbour's
# gateway:18789 returns 200. We DROP any guest-originated traffic destined to
# the whole tenant supernet (SUBNET_PREFIX.0.0/16) BEFORE the ACCEPT. Public
# egress is unaffected: those packets are NOT in SUBNET_PREFIX/16, so they skip
# this DROP and hit the MASQUERADE→${HOST_IFACE} path. -I keeps it above ACCEPT.
TENANT_SUPERNET="${SUBNET_PREFIX:-10.0}.0.0/16"
sudo iptables -C FORWARD -i ${TAP} -d ${TENANT_SUPERNET} -j DROP 2>/dev/null || \
  sudo iptables -I FORWARD 1 -i ${TAP} -d ${TENANT_SUPERNET} -j DROP
# ── SECURITY (management-plane isolation): block guest → host services ──
# The guest's default route points at its tap's host IP (HOST_TAP_IP), and the
# host runs control-plane services bound to 0.0.0.0: host-agent metrics/control
# on :8899 (and :9090 if enabled) and sshd on :22. A tenant must never reach
# these — host-agent on :8899 can drive Firecracker (balloon, lifecycle) and
# read other tenants' gateway tokens. Drop guest→host on those ports in INPUT
# (traffic to the host itself hits INPUT, not FORWARD). host-agent SSHes INTO
# the guest (host→guest, a NEW outbound conn from the host), so blocking
# guest→host:22 here does not affect host-agent's reverse management.
for _port in 8899 9090 22; do
  sudo iptables -C INPUT -i ${TAP} -p tcp --dport ${_port} -j DROP 2>/dev/null || \
    sudo iptables -I INPUT 1 -i ${TAP} -p tcp --dport ${_port} -j DROP
done
# NOTE: the IMDS DROP lives ONLY in the FORWARD chain (above). The nat table
# is for address translation, not filtering — nft rejects `-j DROP` in
# nat/PREROUTING ("the use of DROP is therefore inhibited"), which under
# `set -e` aborts VM launch entirely. The FORWARD DROP already blocks all
# guest→IMDS traffic before it can be MASQUERADEd, so a nat-table drop is
# both illegal and redundant.
sudo iptables -t nat -C POSTROUTING -o ${HOST_IFACE} -j MASQUERADE 2>/dev/null || \
  sudo iptables -t nat -A POSTROUTING -o ${HOST_IFACE} -j MASQUERADE
sudo iptables -C FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
  sudo iptables -A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
sudo iptables -C FORWARD -i ${TAP} -o ${HOST_IFACE} -j ACCEPT 2>/dev/null || \
  sudo iptables -A FORWARD -i ${TAP} -o ${HOST_IFACE} -j ACCEPT

# Start Firecracker
log "starting firecracker..."
nohup firecracker --api-sock ${SOCK} --log-path ${VM_DIR}/fc.log --level Info &>/dev/null & disown
sleep 1

# Configure VM
# boot_args 安全加固(Firecracker prod-host-setup.md):
#   8250.nr_uarts=0 关闭 guest 8250 串口设备——guest 能借串口把数据灌进接到
#   firecracker stdout 的 host 侧,写爆 host 内存/磁盘(prod-host-setup.md:26-67)。
#   关串口后去掉 console=ttyS0(无串口则 console 无处输出)。host 侧调试不受影响:
#   fc.log 是 firecracker 自己的 --log-path,不依赖 guest console。
#   quiet loglevel=1 进一步压 guest 内核日志(也利于启动提速,console 日志拖慢 tap)。
curl -s --unix-socket ${SOCK} -X PUT http://localhost/boot-source \
  -H 'Content-Type: application/json' \
  -d '{"kernel_image_path":"/home/ubuntu/firecracker-assets/vmlinux","boot_args":"8250.nr_uarts=0 quiet loglevel=1 reboot=k panic=1 pci=off ro init=/sbin/overlay-init overlay_root=vdb ip='${GUEST_IP}'::'${HOST_TAP_IP}':255.255.255.252::eth0:off"}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/rootfs \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"rootfs","path_on_host":"'${ROOTFS}'","is_root_device":true,"is_read_only":true}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/overlay \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"overlay","path_on_host":"'${OVERLAY}'","is_root_device":false,"is_read_only":false}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/data \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"data","path_on_host":"'${DATA_VOL}'","is_root_device":false,"is_read_only":false}'

# Fourth drive — the IMMUTABLE authority disk. MUST be PUT after data so the
# guest sees it as /dev/vdd (Firecracker assigns /dev/vd<N> in PUT order, root
# device pinned to vda — see firecracker issue #1750). is_read_only:true makes
# this a hardware-level write barrier: the virtio-block device refuses every
# guest write, so even root inside the VM gets EROFS on the bound identity files
# and ops skills. Skipped only if the asset isn't present (backward compatible).
if [ -f "${IMMUTABLE_TPL}" ]; then
  curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/immutable \
    -H 'Content-Type: application/json' \
    -d '{"drive_id":"immutable","path_on_host":"'${IMMUTABLE_TPL}'","is_root_device":false,"is_read_only":true}'
  log "attached read-only immutable disk /dev/vdd (${IMMUTABLE_TPL})"
else
  log "WARN: ${IMMUTABLE_TPL} absent — launching WITHOUT immutable authority disk"
fi

curl -s --unix-socket ${SOCK} -X PUT http://localhost/machine-config \
  -H 'Content-Type: application/json' \
  -d '{"vcpu_count":'${VCPU}',"mem_size_mib":'${MEM_MB}'}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/network-interfaces/eth0 \
  -H 'Content-Type: application/json' \
  -d '{"iface_id":"eth0","guest_mac":"'${GUEST_MAC}'","host_dev_name":"'${TAP}'"}'

# Balloon device for memory overcommit (configured via /etc/platform.env or defaults)
BALLOON_ENABLED="${BALLOON_ENABLED:-false}"
if [ "${BALLOON_ENABLED}" = "true" ]; then
  BALLOON_DEFLATE_ON_OOM="${BALLOON_DEFLATE_ON_OOM:-true}"
  BALLOON_STATS_INTERVAL="${BALLOON_STATS_INTERVAL:-5}"
  BALLOON_FREE_PAGE_REPORTING="${BALLOON_FREE_PAGE_REPORTING:-true}"
  curl -s --unix-socket ${SOCK} -X PUT http://localhost/balloon \
    -H 'Content-Type: application/json' \
    -d '{"amount_mib":0,"deflate_on_oom":'${BALLOON_DEFLATE_ON_OOM}',"stats_polling_interval_s":'${BALLOON_STATS_INTERVAL}',"free_page_reporting":'${BALLOON_FREE_PAGE_REPORTING}'}'
  log "balloon configured: deflate_on_oom=${BALLOON_DEFLATE_ON_OOM} stats=${BALLOON_STATS_INTERVAL}s free_page_reporting=${BALLOON_FREE_PAGE_REPORTING}"
fi

RESULT=$(curl -s --unix-socket ${SOCK} -X PUT http://localhost/actions \
  -H 'Content-Type: application/json' -d '{"action_type":"InstanceStart"}')
[ -n "${RESULT}" ] && log "ERROR: ${RESULT}" && exit 1
log "InstanceStart succeeded — VM is now booting"
# 1.3.2: Past this point the VM is genuinely running. Any later step
# failing (nginx reload race, ssh-keygen leftovers, etc) shouldn't
# tear down a working VM. Disable strict mode + clear ERR trap so the
# script always reaches the DONE log even if nginx's reload returns
# non-zero on a transient race.
set +e
trap - ERR
ssh-keygen -R ${GUEST_IP} 2>/dev/null || true

# Nginx reverse proxy for this tenant. Two paths:
#   /vm/{tenant}/    -> gateway :18789  (control UI / dashboard, token-auth)
#   /chat/{tenant}/  -> claw-channel signed webhook :18790  (C-end user messages,
#                       HMAC-signed, Cognito-sub bound — replaces the bare
#                       /v1/chat/completions endpoint). The webhook itself rejects
#                       any unsigned request with 401, so this path is safe to
#                       expose through the same CloudFront->ALB origin.
sudo tee /etc/nginx/conf.d/tenants/${TENANT_ID}.conf > /dev/null <<EOF
location ~ ^/vm/${TENANT_ID}(/.*)?$ {
    proxy_pass http://${GUEST_IP}:18789\$1;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$connection_upgrade;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
}
location ~ ^/chat/${TENANT_ID}(/.*)?$ {
    proxy_pass http://${GUEST_IP}:18790\$1;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
}
EOF
sudo nginx -s reload 2>/dev/null || true

log "DONE ${TENANT_ID} IP:${GUEST_IP} (total $((SECONDS))s)"
