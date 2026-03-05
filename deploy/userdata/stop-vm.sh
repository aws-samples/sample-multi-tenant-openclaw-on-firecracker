#!/bin/bash
TENANT_ID="${1:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_NUM="${2:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
log() { echo "[oc:stop] $(date +%H:%M:%S) $*"; }
log "stopping ${TENANT_ID} vm${VM_NUM}..."
curl -s --unix-socket ${VM_DIR}/fc.sock -X PUT http://localhost/actions \
  -H 'Content-Type: application/json' -d '{"action_type":"SendCtrlAltDel"}' 2>/dev/null || true
sleep 2
pkill -f "api-sock ${VM_DIR}/fc.sock" 2>/dev/null || true
sudo ip link del tap-vm${VM_NUM} 2>/dev/null || true
rm -f ${VM_DIR}/fc.sock ${VM_DIR}/fc.log
log "DONE ${TENANT_ID} (data volume preserved)"
