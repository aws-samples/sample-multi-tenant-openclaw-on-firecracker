#!/bin/bash
set -euo pipefail
TENANT_ID="${1:?Usage: launch-vm.sh <tenant_id> <vm_num> [vcpu] [mem_mb]}"
VM_NUM="${2:?Usage: launch-vm.sh <tenant_id> <vm_num> [vcpu] [mem_mb]}"
VCPU="${3:-2}"
MEM_MB="${4:-4096}"
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
mkdir -p ${VM_DIR}
SOCK="${VM_DIR}/fc.sock"
TAP="tap-vm${VM_NUM}"
GUEST_IP="{{SUBNET_PREFIX}}.${VM_NUM}.2"
HOST_TAP_IP="{{SUBNET_PREFIX}}.${VM_NUM}.1"
GUEST_MAC="AA:FC:00:00:00:$(printf '%02x' ${VM_NUM})"
log() { echo "[oc:launch] $(date +%H:%M:%S) $*"; }

log "START ${TENANT_ID} vm${VM_NUM} ${VCPU}vCPU/${MEM_MB}MB"

# Cleanup previous instance
pkill -f "api-sock ${SOCK}" 2>/dev/null || true
sudo ip link del ${TAP} 2>/dev/null || true
rm -f ${SOCK}; sleep 0.5

# Prepare disks (parallel cp with integrity check)
log "copying disks..."
T0=$SECONDS
ROOTFS_TPL="/data/firecracker-assets/openclaw-rootfs.ext4"
DATA_TPL="/data/firecracker-assets/openclaw-data-template.ext4"
ROOTFS_SIZE=$(stat -c%s ${ROOTFS_TPL})
DATA_SIZE=$(stat -c%s ${DATA_TPL})
if [ ! -f "${VM_DIR}/rootfs.ext4" ] || [ "$(stat -c%s ${VM_DIR}/rootfs.ext4 2>/dev/null)" != "${ROOTFS_SIZE}" ]; then
  rm -f ${VM_DIR}/rootfs.ext4
  cp ${ROOTFS_TPL} ${VM_DIR}/rootfs.ext4 &
fi
DATA_VOL="${VM_DIR}/data.ext4"
if [ ! -f "${DATA_VOL}" ] || [ "$(stat -c%s ${DATA_VOL} 2>/dev/null)" != "${DATA_SIZE}" ]; then
  rm -f ${DATA_VOL}
  cp ${DATA_TPL} ${DATA_VOL} &
fi
wait
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
# Allow dashboard access from any origin (token auth is the security layer)
OC_JSON="${MOUNT_TMP}/.openclaw/openclaw.json"
if [ -f "${OC_JSON}" ] && command -v jq &>/dev/null; then
  jq '.gateway.controlUi.allowedOrigins = ["*"]' "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
  sudo chown 1000:1000 "${OC_JSON}"
  log "gateway allowedOrigins set to *"
  # AgentCore Gateway MCP injection (if configured)
  if [ -f /data/agentcore.env ]; then
    source /data/agentcore.env
    if [ -n "${AGENTCORE_GATEWAY_URL:-}" ]; then
      jq --arg url "$AGENTCORE_GATEWAY_URL" '.mcpServers["agentcore-gateway"] = {"url": $url, "transport": "streamable-http"}' "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
      sudo chown 1000:1000 "${OC_JSON}"
      log "AgentCore Gateway MCP injected: ${AGENTCORE_GATEWAY_URL}"
    fi
  fi
fi
sudo umount ${MOUNT_TMP}
rmdir ${MOUNT_TMP} 2>/dev/null || true

# Network setup
log "setting up network tap=${TAP}..."
sudo ip tuntap add dev ${TAP} mode tap
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
  -d '{"kernel_image_path":"'$HOME'/firecracker-assets/vmlinux","boot_args":"console=ttyS0 reboot=k panic=1 pci=off ip='${GUEST_IP}'::'${HOST_TAP_IP}':255.255.255.0::eth0:off"}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/rootfs \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"rootfs","path_on_host":"'${VM_DIR}'/rootfs.ext4","is_root_device":true,"is_read_only":false}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/data \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"data","path_on_host":"'${DATA_VOL}'","is_root_device":false,"is_read_only":false}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/machine-config \
  -H 'Content-Type: application/json' \
  -d '{"vcpu_count":'${VCPU}',"mem_size_mib":'${MEM_MB}'}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/network-interfaces/eth0 \
  -H 'Content-Type: application/json' \
  -d '{"iface_id":"eth0","guest_mac":"'${GUEST_MAC}'","host_dev_name":"'${TAP}'"}'

RESULT=$(curl -s --unix-socket ${SOCK} -X PUT http://localhost/actions \
  -H 'Content-Type: application/json' -d '{"action_type":"InstanceStart"}')
[ -n "${RESULT}" ] && log "ERROR: ${RESULT}" && exit 1
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
