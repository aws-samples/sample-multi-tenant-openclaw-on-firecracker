#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

TENANT_ID="${1:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_NUM="${2:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
log() { echo "[oc:stop] $(date +%H:%M:%S) $*"; }
log "stopping ${TENANT_ID} vm${VM_NUM}..."

# Serialize stop with every launch/recover path. launch-vm.sh holds this same
# per-tenant lock, so a stop racing a cold launch waits for that launch and then
# wins deterministically instead of killing a half-built VM.
mkdir -p /run/lock 2>/dev/null || true
LOCK_PATH="/run/lock/oc-launch-${TENANT_ID}.lock"
exec 9>"${LOCK_PATH}"
STOP_INTENT_PUBLISHED=0
LEGACY_FIRECRACKER_TERMINATED=0

publish_stop_intent() {
  if [ ! -d "${VM_DIR}" ]; then
    return 1
  fi
  touch "${VM_DIR}/.stopped" || {
    log "FATAL: cannot write stop marker"
    exit 1
  }
  STOP_INTENT_PUBLISHED=1
}

# Before this MR, Firecracker inherited fd9 from launch-vm.sh and retained the
# lifecycle flock for its entire lifetime. A script-only rollout therefore
# deadlocks every stop of an already-running VM. Identify that legacy state by
# both the exact tenant API socket and the lock inode; a command-line match alone
# is not enough to authorize killing a process.
find_legacy_firecracker_lock_holders() {
  local lock_identity proc pid executable cmdline fd fd_identity
  lock_identity="$(stat -Lc '%d:%i' "${LOCK_PATH}" 2>/dev/null)" || return 1
  LEGACY_FIRECRACKER_PIDS=()

  for proc in /proc/[0-9]*; do
    executable="$(readlink -f "${proc}/exe" 2>/dev/null)" || continue
    [ "${executable##*/}" = "firecracker" ] || continue
    [ -r "${proc}/cmdline" ] || continue
    cmdline="$(tr '\0' ' ' < "${proc}/cmdline" 2>/dev/null)" || continue
    case "${cmdline}" in
      *"--api-sock ${VM_DIR}/fc.sock"*) ;;
      *) continue ;;
    esac

    for fd in "${proc}"/fd/*; do
      fd_identity="$(stat -Lc '%d:%i' "${fd}" 2>/dev/null)" || continue
      if [ "${fd_identity}" = "${lock_identity}" ]; then
        pid="${proc##*/}"
        LEGACY_FIRECRACKER_PIDS+=("${pid}")
        break
      fi
    done
  done

  [ "${#LEGACY_FIRECRACKER_PIDS[@]}" -gt 0 ]
}

# Keep the whole stop below the API's 30-second SSM timeout. A normal launch or
# another stop still gets time to finish; only a proven legacy Firecracker lock
# holder takes the migration path.
if ! flock -w 2 9; then
  if find_legacy_firecracker_lock_holders; then
    if ! publish_stop_intent; then
      log "VM directory absent; terminating orphaned legacy Firecracker"
    fi
    log "legacy Firecracker inherited lifecycle lock; migrating pid(s): ${LEGACY_FIRECRACKER_PIDS[*]}"
    curl -sf --max-time 2 --unix-socket "${VM_DIR}/fc.sock" -X PUT http://localhost/actions \
      -H 'Content-Type: application/json' -d '{"action_type":"SendCtrlAltDel"}' 2>/dev/null || true
    sleep 2
    kill -TERM "${LEGACY_FIRECRACKER_PIDS[@]}" 2>/dev/null || true
    sleep 1
    for _pid in "${LEGACY_FIRECRACKER_PIDS[@]}"; do
      kill -0 "${_pid}" 2>/dev/null && kill -KILL "${_pid}" 2>/dev/null || true
    done
    flock -w 10 9 || {
      log "FATAL: legacy Firecracker exited but lifecycle lock remained held"
      exit 1
    }
    LEGACY_FIRECRACKER_TERMINATED=1
  else
    flock -w 15 9 || {
      log "FATAL: tenant lifecycle lock remained busy"
      exit 1
    }
  fi
fi

# Publish stop intent before Firecracker can disappear. host-agent treats
# vm.json + no process + no .stopped as a crash and auto-recovers it; writing
# this marker after pkill leaves a real recovery race.
if [ -d "${VM_DIR}" ]; then
  [ "${STOP_INTENT_PUBLISHED}" -eq 1 ] || publish_stop_intent
else
  log "VM directory absent; nothing to stop"
  exit 0
fi

# 1) Graceful shutdown attempt via Firecracker action API (SendCtrlAltDel).
#    Best-effort — if Firecracker is mid-init the API socket may not be
#    serving yet, in which case curl returns non-zero and we fall through
#    to the kill path immediately.
if [ "${LEGACY_FIRECRACKER_TERMINATED}" -eq 0 ]; then
  curl -sf --max-time 2 --unix-socket "${VM_DIR}/fc.sock" -X PUT http://localhost/actions \
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
fi
# 4) Clean up the host-side network + sockets + nginx route.
sudo ip link del "tap-vm${VM_NUM}" 2>/dev/null || true
rm -f "${VM_DIR}/fc.sock" "${VM_DIR}/fc.log"
sudo rm -f "/etc/nginx/conf.d/tenants/${TENANT_ID}.conf"
sudo nginx -s reload 2>/dev/null || true
log "DONE ${TENANT_ID} (data volume preserved)"
