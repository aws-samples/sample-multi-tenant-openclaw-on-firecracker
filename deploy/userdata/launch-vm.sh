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
# #41 — harden-config.sh 提供 POSIX sh 幂等 openclaw.json 收敛函数
# (oc_harden_config + oc_normalize_litellm_baseurl)。launch-vm.sh 每次启动都调
# oc_harden_config,不管 fresh/wake,收敛部署相关值(CloudFront origin/LiteLLM
# baseUrl/chatCompletions 三态/apiKey 显式非空)。缺文件 = 部署漂移 → fail-loud。
if [ -r /home/ubuntu/lib/harden-config.sh ]; then
  # shellcheck disable=SC1091
  . /home/ubuntu/lib/harden-config.sh
else
  echo "[oc:launch] FATAL: /home/ubuntu/lib/harden-config.sh missing (init-host.sh should have downloaded it)" >&2
  exit 1
fi
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
  # ─────────────────────────────────────────────────────────────────────
  # ONE-TIME 生成(NEW_DATA 才跑):config template 首次下载、gateway token 首铸、
  # channel_secret 首次落盘、Cognito 注入、per-tenant vkey 首次注入。这些是"一次
  # 性生成"的东西——重跑会破坏 DDB 握手(hub 校验 channel_secret 用的是首次那个),
  # 或用 shared vkey 覆盖已铸的 per-tenant vkey坏计费拆分。
  # ─────────────────────────────────────────────────────────────────────
  if [ "$NEW_DATA" = "true" ]; then
    # Download custom template from S3 (if specified). 幂等段跑之前先下,让
    # oc_harden_config 收敛新拉下来的模板;唤醒不重下(会冲掉用户配置)。
    if [ -n "${CONFIG_TEMPLATE}" ] && [ -n "${ASSETS_BUCKET:-}" ]; then
      aws s3 cp "s3://${ASSETS_BUCKET}/templates/openclaw/${CONFIG_TEMPLATE}/openclaw.json" "${OC_JSON}" --region "${OC_REGION:-ap-northeast-1}" --quiet
      log "config template '${CONFIG_TEMPLATE}' applied"
    fi
    # Two SEPARATE, ORTHOGONAL auth layers:
    #   (a) gateway.auth.token  — protects the control plane / control UI.
    #   (b) channels.claw-channel.secret — HMAC secret for the signed C-end
    #       webhook (the mini-app's backend signs with the same secret using the
    #       Cognito-verified `sub`). This is the user-message path; it does NOT
    #       touch gateway.auth.token.
    NEW_TOKEN=$(openssl rand -hex 24)
    # claw-channel (outbound WS to the hub) — see docstrings below.
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
    # Cognito fields are added only when all four are present; otherwise the
    # object keeps just the HMAC fields (legacy path). The in-VM channel's
    # hasCognitoCreds() then picks Cognito vs HMAC. HMAC fields stay either way
    # so a half-rolled-out fleet always has a working fallback.
    if [ -n "${COG_REGION}" ] && [ -n "${COG_CLIENT_ID}" ] && [ -n "${COG_USERNAME}" ] && [ -n "${COG_PASSWORD}" ]; then
      _COGNITO_JQ='| .channels["claw-channel"] += { "cognitoRegion": $cogregion, "cognitoClientId": $cogclient, "cognitoUsername": $coguser, "cognitoPassword": $cogpass }'
    else
      _COGNITO_JQ=''
    fi
    jq --arg t "$NEW_TOKEN" --arg s "$CHANNEL_SECRET" \
       --arg appid "$TENANT_ID" --arg huburl "$HUB_URL" --arg hubws "$HUB_WS" \
       --arg cogregion "$COG_REGION" --arg cogclient "$COG_CLIENT_ID" \
       --arg coguser "$COG_USERNAME" --arg cogpass "$COG_PASSWORD" "
      .gateway.auth.token = \$t |
      .channels = ((.channels // {}) + { \"claw-channel\": (((.channels // {})[\"claw-channel\"] // {}) + {
        \"enabled\": true, \"secret\": \$s, \"appId\": \$appid,
        \"appSecret\": \$s, \"hubUrl\": \$huburl, \"wsUrl\": \$hubws
      }) })
      ${_COGNITO_JQ}
    " "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
    log "gateway token + channel hub config generated (one-time)"
  fi

  # ─────────────────────────────────────────────────────────────────────
  # 幂等收敛(#41)—— 每次启动都跑(fresh + wake),把部署相关值收敛到当前:
  #   • dangerouslyDisableDeviceAuth 无条件 del(secure default)
  #   • allowedOrigins → 当前 CloudFront origin(SSM 拉最新)
  #   • baseUrl → 当前 LiteLLM host(堡垒机重建 IP 会变)
  #   • chatCompletions 三态(1/0/空)
  #   • apiKey 仅在显式非空时改写(唤醒空参绝不覆盖数据盘上的 per-tenant vkey)
  # 老版本把这块塞在 NEW_DATA-only 分支里,唤醒路径完全跳过 → 唤醒即漂移。
  # 详细语义见 lib/harden-config.sh 的 oc_harden_config 注释。
  # ─────────────────────────────────────────────────────────────────────
  CF_ORIGIN="${CLOUDFRONT_ORIGIN:-}"
  LITELLM_BASEURL="$(oc_normalize_litellm_baseurl "${LITELLM_HOST:-}")"

  # apiKey:优先 per-tenant LITELLM_VKEY 参数(SSM 传入,per-tenant 计费拆分);
  # 参数为空时才 fall back 到 platform.env 的 LITELLM_SHARED_VKEY(shared)。
  # 关键 fail-safe:LITELLM_VKEY 参数空 + LITELLM_SHARED_VKEY 也空 → _APIKEY 空 →
  # oc_harden_config 不写 apiKey(不会拿 shared 覆盖数据盘上的 per-tenant vkey)。
  # 老版本这一位失败会保留 __INJECT_AT_DEPLOY__ 占位 → agent 拿占位当 key → 401,
  # 现在只在有真 key 时改写,数据盘上首铸的 per-tenant vkey 会被幂等段保留。
  _APIKEY="${LITELLM_VKEY:-${LITELLM_SHARED_VKEY:-}}"

  # #80 · host 侧自愈:init-host 只在首启从 SSM 读一次 vkey,读空就永远空
  # (setup.sh 铸 vkey 晚于 host 首启就撞这个)。这里加单次「vkey 为空→补读 SSM」的
  # 自愈,把「铸 vkey 晚于 host 首启」的时序窗口封了。有值直接用,零延迟。
  # 关键:只补 shared vkey(未提供 per-tenant LITELLM_VKEY 时才走到这里);
  # 补到 _APIKEY 后传给 oc_harden_config,由 helper 幂等段真正落进 openclaw.json。
  if [ -z "${_APIKEY}" ]; then
    log "vkey empty in /etc/platform.env — 从 SSM /openclaw/litellm-shared-vkey 补读一次"
    _SSM_LK="$(aws ssm get-parameter --name /openclaw/litellm-shared-vkey --with-decryption \
                 --region "${REGION}" --query 'Parameter.Value' --output text 2>/dev/null || true)"
    if [ -n "${_SSM_LK}" ] && [ "${_SSM_LK}" != "None" ]; then
      _APIKEY="${_SSM_LK}"
      # 回写 /etc/platform.env 让下一台 VM 首发不用再补读:替换现有行或追加。
      if grep -q '^LITELLM_SHARED_VKEY=' /etc/platform.env 2>/dev/null; then
        sed -i "s|^LITELLM_SHARED_VKEY=.*|LITELLM_SHARED_VKEY=${_APIKEY}|" /etc/platform.env
      else
        echo "LITELLM_SHARED_VKEY=${_APIKEY}" >> /etc/platform.env
      fi
      chmod 600 /etc/platform.env || true
      # 同进程后续读到这个变量(下一台 microVM 起 launch-vm 会重 source)
      export LITELLM_SHARED_VKEY="${_APIKEY}"
      log "vkey 从 SSM 补读成功并回写 /etc/platform.env"
    else
      log "SSM /openclaw/litellm-shared-vkey 仍为空(setup.sh 铸 vkey 未完成?)"
    fi
  fi

  # 两者都空且 SSM 补读也空 → helper 幂等段跳过 apiKey 写入(保留数据盘上的老 key)。
  # 首启且数据盘上 apiKey 还是烤死的 __INJECT_AT_DEPLOY__ 占位时,agent 拿占位当 key 调
  # LiteLLM → 401 → "Something went wrong"。打红字 WARN 让运维一眼看见根因。
  if [ -z "${_APIKEY}" ]; then
    log "WARN: 无 LITELLM_VKEY 也无 LITELLM_SHARED_VKEY(SSM 也空)— apiKey 保留占位符,LLM 调用会 401。设 SSM /openclaw/litellm-shared-vkey(setup.sh 或手工 aws ssm put-parameter)。"
  fi

  if oc_harden_config "${OC_JSON}" "${CF_ORIGIN}" "${LITELLM_BASEURL}" "${_APIKEY}" "${CHAT_EP_ENABLED}"; then
    # 日志一行看清幂等段执行了什么(帮排查唤醒漂移)
    _log_origin="${CF_ORIGIN:-<unset,skipped>}"
    _log_url="${LITELLM_BASEURL:-<unset,skipped>}"
    _log_chat="${CHAT_EP_ENABLED:-<unset,no-op>}"
    _log_key=""
    if [ -n "${LITELLM_VKEY}" ]; then _log_key="per-tenant"
    elif [ -n "${LITELLM_SHARED_VKEY:-}" ]; then _log_key="shared"
    else _log_key="<unset,preserving-disk>"
    fi
    log "harden-config: origin=${_log_origin} baseUrl=${_log_url} chat=${_log_chat} apiKey=${_log_key}"
  else
    # fail-loud:静默吞过一次就是事故。exit 让 trap 上报。
    log "FATAL: harden-config failed on ${OC_JSON}"
    exit 1
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
# ── SECURITY (#34: IMDSv6 拦截,per-tap disable_ipv6=1)──
# 老版本注释声称 IPv6 IMDS(fd00:ec2::254)"defensively covered",但仅有下面的
# IPv4 iptables DROP,ip6tables 全仓零命中,注释名实不符。真堵法:tap 上关掉
# IPv6 协议栈,fd00:ec2::254 与 fe80 一并消失,不依赖 ip6tables 存在。
# 幂等 + 无 ip6tables 依赖 + 单条命令收敛,与 launch-vm 其它 sysctl 风格一致。
# 深度防御另一半在 init-host.sh step1b(host 全局 net.ipv6.conf.all.forwarding=0)。
sudo sysctl -q -w net.ipv6.conf.${TAP}.disable_ipv6=1 2>/dev/null || true
HOST_IFACE=$(ip route show default | awk '{print $5}' | head -1)
sudo sysctl -q -w net.ipv4.ip_forward=1
# ── SECURITY (multi-tenant isolation): block guest → instance metadata ──
# Without this, a tenant inside its microVM can reach the host's IMDS at
# 169.254.169.254 through the MASQUERADE rule below and steal the host EC2
# instance-profile credentials (which can read/write the shared assets bucket
# and the tenants/hosts tables — i.e. every other tenant's data). Drop all
# guest-originated traffic to the link-local IMDS range BEFORE the ACCEPT
# rules. -I inserts at the top so it always precedes the FORWARD ACCEPT.
# IPv6 IMDS (fd00:ec2::254) is blocked by disabling IPv6 on the tap above,
# and host-side net.ipv6.conf.all.forwarding=0 (init-host.sh step1b) — no
# need for ip6tables since the guest has no IPv6 stack on its tap link.
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
# ── SECURITY (#39: 出网默认拒绝白名单)──
# EGRESS_ALLOWLIST_ENABLED 从 /etc/platform.env(:54 source)取,由 config
# security.egress_allowlist_enabled 经 stack.py 渲染而来。默认 false → 保持历史
# 行为:无条件放行 guest→公网口(现状零变化)。true → 切默认拒绝:只放行 ①VPC CIDR +
# 自定义 CIDR(静态,覆盖 hub/LiteLLM/EKS ALB/VPC Endpoint 私网)②host dnsmasq 解析
# 内置 cognito-idp/s3 + 运营域名灌进 ipset oc_egress_allow 的真实 IP ③guest 的 :53
# 被透明 DNAT 到 host dnsmasq(硬编码 8.8.8.8 无需改),其余一律末尾 DROP 兜底。
# 上述 IMDS/租户超网/管理端口 DROP 都在链首(-I),先命中,白名单不误放。
if [ "${EGRESS_ALLOWLIST_ENABLED:-false}" = "true" ]; then
  # (1) 透明 DNS 劫持:guest 发往任意 DNS(含硬编码 8.8.8.8)的 :53 都 DNAT 到 host
  #     dnsmasq(HOST_TAP_IP:53)。UDP+TCP 都要(大响应/AXFR 走 TCP)。nat/PREROUTING
  #     的 DNAT 合法(-j DROP 才被 nft 禁);零 guest 改造破掉硬编码解析器。
  for _proto in udp tcp; do
    sudo iptables -t nat -C PREROUTING -i ${TAP} -p ${_proto} --dport 53 -j DNAT --to-destination ${HOST_TAP_IP}:53 2>/dev/null || \
      sudo iptables -t nat -I PREROUTING 1 -i ${TAP} -p ${_proto} --dport 53 -j DNAT --to-destination ${HOST_TAP_IP}:53
    # 显式放行 guest→host dnsmasq(INPUT)。当前 INPUT 默认 ACCEPT,显式写更 future-proof。
    sudo iptables -C INPUT -i ${TAP} -p ${_proto} -d ${HOST_TAP_IP} --dport 53 -j ACCEPT 2>/dev/null || \
      sudo iptables -I INPUT 1 -i ${TAP} -p ${_proto} -d ${HOST_TAP_IP} --dport 53 -j ACCEPT
  done
  # (2) 静态 CIDR 白名单:VPC CIDR(覆盖 hub/LiteLLM/EKS ALB/VPC Endpoint 私网)+ 运营 CIDR。
  _EGRESS_CIDRS=""
  [ "${EGRESS_INCLUDE_VPC_CIDR:-true}" = "true" ] && [ -n "${EGRESS_VPC_CIDR:-}" ] && _EGRESS_CIDRS="${EGRESS_VPC_CIDR}"
  _EGRESS_CIDRS="${_EGRESS_CIDRS} $(echo "${EGRESS_ALLOWLIST_CIDRS:-}" | tr ',' ' ')"
  # 白名单 ACCEPT 与末尾 DROP 都**不限出口网卡 -o**(按目的地放行/拒绝,不绑死主网卡)。
  # 为什么:末尾兜底若限 `-o ${HOST_IFACE}`,host 上出现第二条出网路径(第二 ENI/docker0/
  # VPN/策略路由)时,guest→该路径的流量既不命中链首 IMDS/超网 DROP、也不命中限主网卡的
  # 兜底 DROP、又无 ACCEPT → 落到 FORWARD 默认策略(常为 ACCEPT)fail-open 泄漏。去掉 -o 让
  # 兜底成为真正的 catch-all(guest→host 自身服务走 INPUT 链,不受 FORWARD 影响,不误伤
  # host-agent 反向 SSH)。ACCEPT 同去 -o,与 DROP 对称:白名单目标经任何路径都放行。
  for _cidr in ${_EGRESS_CIDRS}; do
    [ -z "${_cidr}" ] && continue
    sudo iptables -C FORWARD -i ${TAP} -d ${_cidr} -j ACCEPT 2>/dev/null || \
      sudo iptables -A FORWARD -i ${TAP} -d ${_cidr} -j ACCEPT
  done
  # (3) FQDN 白名单:dnsmasq 解析 cognito/s3/运营域名灌进共享 ipset 的真实 IP。ipset
  #     缺失(dnsmasq/ipset 没装成)时 -m set 规则加不上 → 跳过,退回只放静态 CIDR + DNS
  #     (fail-safe 不阻断 VM 启动;代价是 cognito/s3 出网被 DROP,真机验证前默认关兜住)。
  if sudo ipset list oc_egress_allow >/dev/null 2>&1; then
    sudo iptables -C FORWARD -i ${TAP} -m set --match-set oc_egress_allow dst -j ACCEPT 2>/dev/null || \
      sudo iptables -A FORWARD -i ${TAP} -m set --match-set oc_egress_allow dst -j ACCEPT
  else
    # ipset 缺失 = host 的 setup-egress-allowlist.sh 没跑成(S3 没下到/dnsmasq 没装),但本 VM
    # 的 gate 仍开 → cognito/s3 等 FQDN 目标会被下面的兜底 DROP 拦掉(fail-closed,不泄漏,但该
    # 租户 DNS/公网 AWS 端点不可用)。这是 host 基建与 VM 规则两半独立失败的可用性悬崖:告警落痕,
    # 便于 380 台里个别 host 脚本没下成时定位;运营应据此把该 host 视作 degraded 排查 dnsmasq。
    log "WARN: ipset oc_egress_allow MISSING — host setup-egress-allowlist.sh likely not run (dnsmasq down). FQDN allowlist skipped; cognito/s3 egress WILL be DROPPED for this VM. Treat host as degraded."
  fi
  # (4) 链尾兜底 catch-all DROP:默认拒绝该 guest 转发到任何目的地的其余一切(不限出口网卡,
  #     直连 IP 绕 DNS 白名单、以及经第二网卡/网桥的出网都被这条兜住)。
  sudo iptables -C FORWARD -i ${TAP} -j DROP 2>/dev/null || \
    sudo iptables -A FORWARD -i ${TAP} -j DROP
else
  # 默认(gate 关):现状零变化 —— 无条件放行 guest→公网口。
  sudo iptables -C FORWARD -i ${TAP} -o ${HOST_IFACE} -j ACCEPT 2>/dev/null || \
    sudo iptables -A FORWARD -i ${TAP} -o ${HOST_IFACE} -j ACCEPT
fi

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
