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

# Prepare disks (parallel cp)
log "copying disks..."
T0=$SECONDS
if [ ! -f "${VM_DIR}/rootfs.ext4" ]; then
  cp /data/firecracker-assets/openclaw-rootfs.ext4 ${VM_DIR}/rootfs.ext4 &
fi
DATA_VOL="${VM_DIR}/data.ext4"
if [ ! -f "${DATA_VOL}" ]; then
  cp /data/firecracker-assets/openclaw-data-template.ext4 ${DATA_VOL} &
fi
wait
log "disks ready ($((SECONDS-T0))s)"

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
log "DONE ${TENANT_ID} IP:${GUEST_IP} (total $((SECONDS))s)"
