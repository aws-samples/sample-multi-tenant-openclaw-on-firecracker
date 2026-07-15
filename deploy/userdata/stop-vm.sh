#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

TENANT_ID="${1:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_NUM="${2:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
log() { echo "[oc:stop] $(date +%H:%M:%S) $*"; }
log "stopping ${TENANT_ID} vm${VM_NUM}..."
# 1) Graceful shutdown attempt via Firecracker action API (SendCtrlAltDel).
#    Best-effort — if Firecracker is mid-init the API socket may not be
#    serving yet, in which case curl returns non-zero and we fall through
#    to the kill path immediately.
curl -sf --max-time 2 --unix-socket ${VM_DIR}/fc.sock -X PUT http://localhost/actions \
  -H 'Content-Type: application/json' -d '{"action_type":"SendCtrlAltDel"}' 2>/dev/null || true
# 2) Brief pause for the kernel to ack the ctrl-alt-del.
sleep 2
# 3) SIGTERM first (clean), then SIGKILL after a short wait. Ensures the
#    Firecracker process is *gone* by the time we return — without the
#    `-9` chaser, racy DELETE /tenants calls leave zombie processes that
#    pile up on the host and silently consume vCPU/memory budget. This
#    matters for back-to-back e2e tests in particular.
pkill -TERM -f "api-sock ${VM_DIR}/fc.sock" 2>/dev/null || true
sleep 1
pkill -KILL -f "api-sock ${VM_DIR}/fc.sock" 2>/dev/null || true
# 4) Clean up the host-side network + sockets + nginx route.
sudo ip link del tap-vm${VM_NUM} 2>/dev/null || true
rm -f ${VM_DIR}/fc.sock ${VM_DIR}/fc.log
touch ${VM_DIR}/.stopped
sudo rm -f /etc/nginx/conf.d/tenants/${TENANT_ID}.conf
sudo nginx -s reload 2>/dev/null || true
log "DONE ${TENANT_ID} (data volume preserved)"
