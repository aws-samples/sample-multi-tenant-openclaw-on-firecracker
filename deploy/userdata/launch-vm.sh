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
TENANT_ID="${1:?Usage: launch-vm.sh <tenant_id> <vm_num> [vcpu] [mem_mb] [config_template] [restore_backup_key]}"
VM_NUM="${2:?Usage: launch-vm.sh <tenant_id> <vm_num> [vcpu] [mem_mb] [config_template] [restore_backup_key]}"
VCPU="${3:-2}"
MEM_MB="${4:-4096}"
CONFIG_TEMPLATE="${5:-}"
RESTORE_KEY="${6:-}"
# Caller may pass literal "" (quoted) as placeholder when only restore_key is set.
[ "${CONFIG_TEMPLATE}" = '""' ] && CONFIG_TEMPLATE=""
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
[ -f /etc/platform.env ] && source /etc/platform.env
mkdir -p ${VM_DIR}
rm -f ${VM_DIR}/.stopped
SOCK="${VM_DIR}/fc.sock"
TAP="tap-vm${VM_NUM}"
GUEST_IP="${SUBNET_PREFIX:-10.0}.${VM_NUM}.2"
HOST_TAP_IP="${SUBNET_PREFIX:-10.0}.${VM_NUM}.1"
GUEST_MAC="AA:FC:00:00:00:$(printf '%02x' ${VM_NUM})"
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
# Skills
if [ -d "${SHARED_SKILLS}" ] && [ "$(ls -A ${SHARED_SKILLS} 2>/dev/null)" ]; then
  log "injecting shared skills..."
  mkdir -p ${MOUNT_TMP}/.openclaw/skills
  cp -r ${SHARED_SKILLS}/* ${MOUNT_TMP}/.openclaw/skills/ 2>/dev/null || true
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
    # Inject platform config: unique token + allowedOrigins + disableDeviceAuth
    NEW_TOKEN=$(openssl rand -hex 24)
    jq --arg t "$NEW_TOKEN" '
      .gateway.auth.token = $t |
      .gateway.controlUi.allowedOrigins = ["*"] |
      .gateway.controlUi.dangerouslyDisableDeviceAuth = true
    ' "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
    log "gateway token generated"
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
sudo ip addr add ${HOST_TAP_IP}/24 dev ${TAP}
sudo ip link set dev ${TAP} up
HOST_IFACE=$(ip route show default | awk '{print $5}' | head -1)
sudo sysctl -q -w net.ipv4.ip_forward=1
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
curl -s --unix-socket ${SOCK} -X PUT http://localhost/boot-source \
  -H 'Content-Type: application/json' \
  -d '{"kernel_image_path":"/home/ubuntu/firecracker-assets/vmlinux","boot_args":"console=ttyS0 reboot=k panic=1 pci=off init=/sbin/overlay-init overlay_root=vdb ip='${GUEST_IP}'::'${HOST_TAP_IP}':255.255.255.0::eth0:off"}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/rootfs \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"rootfs","path_on_host":"'${ROOTFS}'","is_root_device":true,"is_read_only":true}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/overlay \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"overlay","path_on_host":"'${OVERLAY}'","is_root_device":false,"is_read_only":false}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/data \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"data","path_on_host":"'${DATA_VOL}'","is_root_device":false,"is_read_only":false}'

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

# Nginx reverse proxy for this tenant's dashboard
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
EOF
sudo nginx -s reload 2>/dev/null || true

log "DONE ${TENANT_ID} IP:${GUEST_IP} (total $((SECONDS))s)"
