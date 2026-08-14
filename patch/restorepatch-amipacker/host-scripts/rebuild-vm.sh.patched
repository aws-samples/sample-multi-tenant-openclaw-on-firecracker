#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -euo pipefail

TENANT_ID="${1:?Usage: rebuild-vm.sh tenant_id vm_num op_id fence_epoch attempt_id host_id -- launch-vm.sh args...}"
VM_NUM="${2:?missing vm_num}"
OP_ID="${3:?missing op_id}"
FENCE_EPOCH="${4:?missing fence_epoch}"
ATTEMPT_ID="${5:?missing attempt_id}"
EXPECTED_HOST_ID="${6:?missing host_id}"
shift 6
[ "${1:-}" = "--" ] || { echo "rebuild-vm.sh: missing -- before launch command" >&2; exit 64; }
shift
[ "$#" -gt 0 ] || { echo "rebuild-vm.sh: missing launch command" >&2; exit 64; }

case "${TENANT_ID}:${OP_ID}:${ATTEMPT_ID}:${EXPECTED_HOST_ID}" in
  *[!A-Za-z0-9._:-]*)
    echo "rebuild-vm.sh: unsafe identifier" >&2
    exit 64
    ;;
esac
case "${VM_NUM}" in
  "" | *[!0-9]*)
    echo "rebuild-vm.sh: vm_num and fence_epoch must be integers" >&2
    exit 64
    ;;
esac
case "${FENCE_EPOCH}" in
  "" | *[!0-9]*)
    echo "rebuild-vm.sh: vm_num and fence_epoch must be integers" >&2
    exit 64
    ;;
esac

[ -f /etc/platform.env ] && source /etc/platform.env
VM_ROOT="${OC_VM_ROOT:-/data/firecracker-vms}"
ASSET_ROOT="${OC_ASSET_ROOT:-/data/firecracker-assets}"
LOCK_ROOT="${OC_LOCK_ROOT:-/run/lock}"
PROC_ROOT="${OC_PROC_ROOT:-/proc}"
HOST_HOME="${OC_HOST_HOME:-/home/ubuntu}"
VM_DIR="${VM_ROOT}/${TENANT_ID}"
OP_DIR="${VM_DIR}/rebuild-ops/${OP_ID}"
INTENT="${OP_DIR}/intent.json"
RESULT="${OP_DIR}/result.json"
DROP_MARKER="${OP_DIR}/overlay-dropped"
TOMBSTONE="${OP_DIR}/overlay.ext4.tombstone"
OVERLAY="${VM_DIR}/overlay.ext4"
TENANTS_TABLE="${TENANTS_TABLE:-openclaw-tenants}"
REGION="${OC_REGION:-${AWS_REGION:-ap-northeast-1}}"

mkdir -p "${LOCK_ROOT}" "${OP_DIR}"
exec 7>"${LOCK_ROOT}/oc-rebuild-${TENANT_ID}.lock"
flock 7

log() { echo "[oc:rebuild] $(date +%H:%M:%S) $*" >&2; }

FENCE_ITEM=""
read_fence() {
  for _ in 1 2; do
    if FENCE_ITEM="$(aws dynamodb get-item \
      --table-name "${TENANTS_TABLE}" \
      --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
      --projection-expression \
        'active_lifecycle_op_id,lifecycle_fence_epoch,active_lifecycle_until,host_id,vm_num,image_snapshot_time' \
      --consistent-read --region "${REGION}" --output json 2>/dev/null)" \
      && [ -n "${FENCE_ITEM}" ]; then
      return 0
    fi
  done
  return 1
}

assert_fence() {
  local now
  now="$(date +%s)"
  read_fence || return 1
  jq -e \
    --arg op "${OP_ID}" \
    --arg epoch "${FENCE_EPOCH}" \
    --arg now "${now}" \
    --arg host "${EXPECTED_HOST_ID}" \
    --arg vm "${VM_NUM}" '
      (.Item.active_lifecycle_op_id.S // "") == $op
      and (.Item.lifecycle_fence_epoch.N // "") == $epoch
      and ((.Item.active_lifecycle_until.N // "0") | tonumber) > ($now | tonumber)
      and (.Item.host_id.S // "") == $host
      and (.Item.vm_num.N // "") == $vm
    ' >/dev/null 2>&1 <<<"${FENCE_ITEM}"
}

TARGET_SNAPSHOT=""
TARGET_ROOTFS=""
TARGET_ROOTFS_ID=""
FC_PID=""
FC_EXE_ID=""
FC_START_TICKS=""
OVERLAY_ID=""
OVERLAY_FD_ID=""

write_result() {
  local state="$1" reason="${2:-}" tmp
  tmp="${RESULT}.tmp.$$"
  jq -n \
    --arg state "${state}" \
    --arg reason "${reason}" \
    --arg tenant_id "${TENANT_ID}" \
    --arg op_id "${OP_ID}" \
    --arg attempt_id "${ATTEMPT_ID}" \
    --arg host_id "${EXPECTED_HOST_ID}" \
    --arg vm_num "${VM_NUM}" \
    --arg fence_epoch "${FENCE_EPOCH}" \
    --arg target_snapshot_time "${TARGET_SNAPSHOT}" \
    --arg target_rootfs "${TARGET_ROOTFS}" \
    --arg target_rootfs_dev_inode "${TARGET_ROOTFS_ID}" \
    --arg firecracker_pid "${FC_PID}" \
    --arg firecracker_exe_dev_inode "${FC_EXE_ID}" \
    --arg firecracker_start_ticks "${FC_START_TICKS}" \
    --arg overlay_dev_inode "${OVERLAY_ID}" \
    --arg overlay_fd_dev_inode "${OVERLAY_FD_ID}" \
    --arg tombstone_dev_inode "$(stat -Lc '%d:%i' "${TOMBSTONE}" 2>/dev/null || true)" \
    --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
      {
        state: $state,
        reason: $reason,
        tenant_id: $tenant_id,
        op_id: $op_id,
        attempt_id: $attempt_id,
        host_id: $host_id,
        vm_num: ($vm_num | tonumber),
        fence_epoch: ($fence_epoch | tonumber),
        target_snapshot_time: $target_snapshot_time,
        target_rootfs: $target_rootfs,
        target_rootfs_dev_inode: $target_rootfs_dev_inode,
        firecracker_pid: $firecracker_pid,
        firecracker_exe_dev_inode: $firecracker_exe_dev_inode,
        firecracker_start_ticks: $firecracker_start_ticks,
        overlay_dev_inode: $overlay_dev_inode,
        overlay_fd_dev_inode: $overlay_fd_dev_inode,
        tombstone_dev_inode: $tombstone_dev_inode,
        completed_at: $completed_at
      }
    ' >"${tmp}"
  chmod 0600 "${tmp}"
  mv -f "${tmp}" "${RESULT}"
  jq -c . "${RESULT}"
}

finish_superseded() {
  log "fence no longer belongs to ${OP_ID}/${FENCE_EPOCH}; refusing host effects"
  write_result "SUPERSEDED" "lifecycle owner, epoch, host, vm, or lease no longer matches"
  exit 79
}

finish_succeeded() {
  local reason="$1"
  write_result "SUCCEEDED" "${reason}"
  # The old overlay is needed only while this op is converging. Once runtime
  # FD evidence is confirmed, retaining one large tombstone per rebuild would
  # leak host disk indefinitely; the small result/intent records remain.
  rm -f "${TOMBSTONE}" || log "warning: could not remove completed tombstone"
}

resolve_current_target() {
  local pinned slots_snapshot
  pinned="$(jq -r '.Item.image_snapshot_time.S // ""' <<<"${FENCE_ITEM}")"
  if [ -n "${pinned}" ]; then
    TARGET_SNAPSHOT="${pinned}"
    TARGET_ROOTFS="${ASSET_ROOT}/versions/${pinned}/openclaw-rootfs.ext4"
  elif [ -f "${ASSET_ROOT}/slots.json" ]; then
    slots_snapshot="$(jq -er '.live // ""' "${ASSET_ROOT}/slots.json")" || return 1
    [ -n "${slots_snapshot}" ] || return 1
    TARGET_SNAPSHOT="${slots_snapshot}"
    TARGET_ROOTFS="${ASSET_ROOT}/versions/${slots_snapshot}/openclaw-rootfs.ext4"
  else
    TARGET_SNAPSHOT=""
    TARGET_ROOTFS="${ASSET_ROOT}/openclaw-rootfs.ext4"
  fi
  [ -f "${TARGET_ROOTFS}" ] || return 1
  TARGET_ROOTFS_ID="$(stat -Lc '%d:%i' "${TARGET_ROOTFS}")"
}

load_or_create_intent() {
  local tmp current_snapshot current_rootfs current_id
  if [ -f "${INTENT}" ]; then
    jq -e --arg tenant "${TENANT_ID}" --arg op "${OP_ID}" \
      '.tenant_id == $tenant and .op_id == $op' "${INTENT}" >/dev/null
    TARGET_SNAPSHOT="$(jq -r '.target_snapshot_time' "${INTENT}")"
    TARGET_ROOTFS="$(jq -r '.target_rootfs' "${INTENT}")"
    TARGET_ROOTFS_ID="$(jq -r '.target_rootfs_dev_inode' "${INTENT}")"
    [ -f "${TARGET_ROOTFS}" ] || return 1
    [ "$(stat -Lc '%d:%i' "${TARGET_ROOTFS}")" = "${TARGET_ROOTFS_ID}" ] || return 1
    return 0
  fi

  resolve_current_target || return 1
  current_snapshot="${TARGET_SNAPSHOT}"
  current_rootfs="${TARGET_ROOTFS}"
  current_id="${TARGET_ROOTFS_ID}"
  tmp="${INTENT}.tmp.$$"
  jq -n \
    --arg tenant_id "${TENANT_ID}" \
    --arg op_id "${OP_ID}" \
    --arg host_id "${EXPECTED_HOST_ID}" \
    --arg vm_num "${VM_NUM}" \
    --arg target_snapshot_time "${current_snapshot}" \
    --arg target_rootfs "${current_rootfs}" \
    --arg target_rootfs_dev_inode "${current_id}" \
    --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
      {
        tenant_id: $tenant_id,
        op_id: $op_id,
        host_id: $host_id,
        vm_num: ($vm_num | tonumber),
        target_snapshot_time: $target_snapshot_time,
        target_rootfs: $target_rootfs,
        target_rootfs_dev_inode: $target_rootfs_dev_inode,
        created_at: $created_at
      }
    ' >"${tmp}"
  chmod 0600 "${tmp}"
  mv -f "${tmp}" "${INTENT}"
}

target_still_current_before_drop() {
  local saved_snapshot="${TARGET_SNAPSHOT}" saved_rootfs="${TARGET_ROOTFS}"
  local saved_id="${TARGET_ROOTFS_ID}"
  resolve_current_target || return 1
  [ "${TARGET_SNAPSHOT}" = "${saved_snapshot}" ] \
    && [ "${TARGET_ROOTFS}" = "${saved_rootfs}" ] \
    && [ "${TARGET_ROOTFS_ID}" = "${saved_id}" ]
}

find_firecracker_pid() {
  local proc cmdline exe
  FC_PID=""
  for proc in "${PROC_ROOT}"/[0-9]*; do
    [ -r "${proc}/cmdline" ] || continue
    exe="$(readlink -f "${proc}/exe" 2>/dev/null || true)"
    [ "${exe##*/}" = "firecracker" ] || continue
    cmdline="$(tr '\0' ' ' <"${proc}/cmdline" 2>/dev/null || true)"
    case "${cmdline}" in
      *"--api-sock ${VM_DIR}/fc.sock"*)
        FC_PID="${proc##*/}"
        return 0
        ;;
    esac
  done
  return 1
}

collect_evidence() {
  local proc fd fd_id tombstone_id
  find_firecracker_pid || return 1
  proc="${PROC_ROOT}/${FC_PID}"
  FC_EXE_ID="$(stat -Lc '%d:%i' "${proc}/exe" 2>/dev/null || true)"
  FC_START_TICKS="$(awk '{print $22}' "${proc}/stat" 2>/dev/null || true)"
  OVERLAY_ID="$(stat -Lc '%d:%i' "${OVERLAY}" 2>/dev/null || true)"
  [ -n "${FC_EXE_ID}" ] && [ -n "${FC_START_TICKS}" ] && [ -n "${OVERLAY_ID}" ] \
    || return 1

  local rootfs_seen=0
  OVERLAY_FD_ID=""
  for fd in "${proc}"/fd/*; do
    fd_id="$(stat -Lc '%d:%i' "${fd}" 2>/dev/null || true)"
    [ -n "${fd_id}" ] || continue
    [ "${fd_id}" = "${TARGET_ROOTFS_ID}" ] && rootfs_seen=1
    [ "${fd_id}" = "${OVERLAY_ID}" ] && OVERLAY_FD_ID="${fd_id}"
  done
  [ "${rootfs_seen}" -eq 1 ] && [ "${OVERLAY_FD_ID}" = "${OVERLAY_ID}" ] || return 1
  tombstone_id="$(stat -Lc '%d:%i' "${TOMBSTONE}" 2>/dev/null || true)"
  [ -z "${tombstone_id}" ] || [ "${OVERLAY_ID}" != "${tombstone_id}" ] || return 1
}

assert_fence || finish_superseded
load_or_create_intent || {
  write_result "FAILED" "could not resolve or persist the pinned rebuild target"
  exit 70
}

if [ ! -f "${DROP_MARKER}" ] && [ ! -f "${TOMBSTONE}" ]; then
  target_still_current_before_drop || finish_superseded
  if ! "${HOST_HOME}/stop-vm.sh" "${TENANT_ID}" "${VM_NUM}"; then
    write_result "FAILED" "stop-vm.sh failed before the overlay commit point"
    exit 71
  fi
  assert_fence || finish_superseded
  target_still_current_before_drop || finish_superseded
  if [ -e "${OVERLAY}" ]; then
    mv "${OVERLAY}" "${TOMBSTONE}"
  fi
  printf '%s\n' "${ATTEMPT_ID}" >"${DROP_MARKER}.tmp.$$"
  mv -f "${DROP_MARKER}.tmp.$$" "${DROP_MARKER}"
  log "overlay commit point completed once for op ${OP_ID}"
fi

if collect_evidence; then
  assert_fence || finish_superseded
  finish_succeeded "existing runtime already matches this operation's pinned evidence"
  exit 0
fi

assert_fence || finish_superseded
TARGET_OVERRIDE="${TARGET_SNAPSHOT:-__legacy_flat__}"
set +e
"$@" "" "${TARGET_OVERRIDE}" 7>&-
launch_rc=$?
set -e
if [ "${launch_rc}" -ne 0 ]; then
  write_result "FAILED" "launch-vm.sh failed after the overlay commit point (rc=${launch_rc})"
  exit "${launch_rc}"
fi

collect_evidence || {
  write_result "FAILED" "Firecracker rootfs/overlay FD evidence did not match the pinned target"
  exit 72
}
assert_fence || finish_superseded
finish_succeeded "Firecracker rootfs and fresh overlay FD evidence verified"
