#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Issue #12 — same-host tenant clone.
# Pause source VM → cp --sparse data.ext4 + overlay.ext4 → resume source.
# Usage: clone-data.sh <src_tenant_id> <src_vm_num> <dst_tenant_id> <dst_vm_num>
# Caller is expected to follow up with launch-vm.sh on dst — this script only
# materializes the disks.
set -uo pipefail
SRC_TID="${1:?Usage: clone-data.sh <src_tenant_id> <src_vm_num> <dst_tenant_id> <dst_vm_num>}"
SRC_VM_NUM="${2:?Usage: clone-data.sh <src_tenant_id> <src_vm_num> <dst_tenant_id> <dst_vm_num>}"
DST_TID="${3:?Usage: clone-data.sh <src_tenant_id> <src_vm_num> <dst_tenant_id> <dst_vm_num>}"
DST_VM_NUM="${4:?Usage: clone-data.sh <src_tenant_id> <src_vm_num> <dst_tenant_id> <dst_vm_num>}"

SRC_DIR="/data/firecracker-vms/${SRC_TID}"
DST_DIR="/data/firecracker-vms/${DST_TID}"
SRC_SOCK="${SRC_DIR}/fc.sock"

log() { echo "[oc:clone] $(date +%H:%M:%S) $*"; }

cleanup() {
  # Always resume source even on error so we don't strand the live tenant.
  if [ -S "$SRC_SOCK" ]; then
    curl -sf --unix-socket "$SRC_SOCK" -X PATCH http://localhost/vm \
      -H 'Content-Type: application/json' -d '{"state":"Resumed"}' >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [ ! -f "${SRC_DIR}/data.ext4" ]; then
  log "ERROR: source data.ext4 not found at ${SRC_DIR}"
  exit 1
fi

mkdir -p "$DST_DIR"

# Pause source for filesystem-consistent copy
if [ -S "$SRC_SOCK" ]; then
  curl -sf --unix-socket "$SRC_SOCK" -X PATCH http://localhost/vm \
    -H 'Content-Type: application/json' -d '{"state":"Paused"}' >/dev/null 2>&1 || {
      log "ERROR: failed to pause source"
      exit 1
    }
  log "source paused"
fi

T0=$SECONDS
# --sparse=always preserves any holes in the ext4 image.
cp --sparse=always "${SRC_DIR}/data.ext4" "${DST_DIR}/data.ext4"
log "data.ext4 copied ($((SECONDS-T0))s)"

if [ -f "${SRC_DIR}/overlay.ext4" ]; then
  cp --sparse=always "${SRC_DIR}/overlay.ext4" "${DST_DIR}/overlay.ext4"
  log "overlay.ext4 copied"
fi

# Resume source explicitly so we log success; trap also resumes on EXIT.
if [ -S "$SRC_SOCK" ]; then
  curl -sf --unix-socket "$SRC_SOCK" -X PATCH http://localhost/vm \
    -H 'Content-Type: application/json' -d '{"state":"Resumed"}' >/dev/null 2>&1 || true
  log "source resumed"
fi

# Sanity: filesystem check on the clone (read-only) before launch-vm.sh
if ! e2fsck -fy "${DST_DIR}/data.ext4" >/dev/null 2>&1; then
  log "ERROR: clone data.ext4 failed e2fsck"
  exit 1
fi
log "clone data verified"

echo "${DST_DIR}"
