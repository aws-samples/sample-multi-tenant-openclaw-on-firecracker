#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# stop-all-vms.sh — HOST-LOCAL fan-out: stop EVERY microVM on THIS host in
# bounded parallel. Companion to start-all-vms.sh. The control plane sends ONE
# SSM command per host, so SSM concurrency == host count, not VM count. This is
# what makes "stop all 380 within 1 minute" reachable.
#
# Each VM stop reuses the EXACT same stop-vm.sh path (graceful ctrl-alt-del →
# SIGTERM → SIGKILL → tap/nginx cleanup → touch .stopped) so there's no second
# teardown code path. stop-vm.sh writes the .stopped marker, which makes
# host-agent's reconcile loop NOT auto-recover the VM (it skips .stopped dirs) —
# so a "stop all" stays stopped instead of being relaunched 15s later.
#
# Usage: stop-all-vms.sh [max_parallel]
#   max_parallel — bounded concurrency (default 128). Stop is lightweight
#   (no disk mount / no FC boot), so it tolerates a higher ceiling than start.

set -uo pipefail
VM_DIR="/data/firecracker-vms"
MAX_PARALLEL="${1:-128}"
log() { echo "[oc:stop-all] $(date +%H:%M:%S) $*"; }

if [ ! -d "${VM_DIR}" ]; then
  log "no VM dir ${VM_DIR} — nothing to stop"
  exit 0
fi

stopped=0
skipped=0
_wait_for_slot() {
  while [ "$(jobs -rp | wc -l)" -ge "${MAX_PARALLEL}" ]; do
    wait -n 2>/dev/null || sleep 0.1
  done
}

for vm_path in "${VM_DIR}"/*/; do
  [ -d "${vm_path}" ] || continue
  cfg="${vm_path}vm.json"
  [ -f "${cfg}" ] || continue
  tenant_id="$(basename "${vm_path}")"
  vm_num="$(jq -r '.vm_num // empty' "${cfg}" 2>/dev/null)"
  if [ -z "${vm_num}" ]; then
    log "skip ${tenant_id}: vm.json has no vm_num"
    skipped=$((skipped + 1))
    continue
  fi
  # Already stopped? (no firecracker process for this socket) → still call
  # stop-vm.sh to ensure the .stopped marker + tap/nginx are cleaned, but it's
  # cheap and idempotent. We DON'T skip here because a half-dead VM (FC gone,
  # tap leaked) still needs cleanup.
  _wait_for_slot
  bash /home/ubuntu/stop-vm.sh "${tenant_id}" "${vm_num}" >/dev/null 2>&1 &
  stopped=$((stopped + 1))
done

wait
log "DONE stopped=${stopped} skipped=${skipped} (max_parallel=${MAX_PARALLEL})"
