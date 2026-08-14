#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -euo pipefail

TENANT_ID="${1:?Usage: reset-vm.sh tenant_id vm_num op_id fence_epoch attempt_id host_id -- launch-vm.sh args...}"
VM_NUM="${2:?missing vm_num}"
OP_ID="${3:?missing op_id}"
FENCE_EPOCH="${4:?missing fence_epoch}"
ATTEMPT_ID="${5:?missing attempt_id}"
EXPECTED_HOST_ID="${6:?missing host_id}"
shift 6
[ "${1:-}" = "--" ] || { echo "reset-vm.sh: missing -- before launch command" >&2; exit 64; }
shift
[ "$#" -gt 0 ] || { echo "reset-vm.sh: missing launch command" >&2; exit 64; }

case "${TENANT_ID}:${OP_ID}:${ATTEMPT_ID}:${EXPECTED_HOST_ID}" in
  *[!A-Za-z0-9._:-]*)
    echo "reset-vm.sh: unsafe identifier" >&2
    exit 64
    ;;
esac
case "${VM_NUM}" in
  "" | *[!0-9]*)
    echo "reset-vm.sh: vm_num and fence_epoch must be integers" >&2
    exit 64
    ;;
esac
case "${FENCE_EPOCH}" in
  "" | *[!0-9]*)
    echo "reset-vm.sh: vm_num and fence_epoch must be integers" >&2
    exit 64
    ;;
esac

[ -f /etc/platform.env ] && source /etc/platform.env
VM_ROOT="${OC_VM_ROOT:-/data/firecracker-vms}"
LOCK_ROOT="${OC_LOCK_ROOT:-/run/lock}"
PROC_ROOT="${OC_PROC_ROOT:-/proc}"
HOST_HOME="${OC_HOST_HOME:-/home/ubuntu}"
VM_DIR="${VM_ROOT}/${TENANT_ID}"
OP_DIR="${VM_DIR}/reset-ops/${OP_ID}"
INTENT="${OP_DIR}/intent.json"
RESULT="${OP_DIR}/result.json"
DROP_MARKER="${OP_DIR}/overlay-dropped"
TOMBSTONE="${OP_DIR}/overlay.ext4.tombstone"
OVERLAY="${VM_DIR}/overlay.ext4"
TENANTS_TABLE="${TENANTS_TABLE:-openclaw-tenants}"
REGION="${OC_REGION:-${AWS_REGION:-ap-northeast-1}}"

mkdir -p "${LOCK_ROOT}" "${OP_DIR}"
exec 7>"${LOCK_ROOT}/oc-reset-${TENANT_ID}.lock"
flock 7

log() { echo "[oc:reset] $(date +%H:%M:%S) $*" >&2; }

FENCE_ITEM=""
read_fence() {
  for _ in 1 2; do
    if FENCE_ITEM="$(aws dynamodb get-item \
      --table-name "${TENANTS_TABLE}" \
      --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
      --projection-expression \
        'active_lifecycle_op_id,lifecycle_fence_epoch,active_lifecycle_until,host_id,vm_num' \
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
  write_result "SUCCEEDED" "$1"
  rm -f "${TOMBSTONE}" || log "warning: could not remove completed tombstone"
}

load_or_create_intent() {
  local tmp
  if [ -f "${INTENT}" ]; then
    jq -e \
      --arg tenant "${TENANT_ID}" \
      --arg op "${OP_ID}" \
      --arg host "${EXPECTED_HOST_ID}" \
      --arg vm "${VM_NUM}" '
        .tenant_id == $tenant and .op_id == $op and
        .host_id == $host and (.vm_num | tostring) == $vm
      ' "${INTENT}" >/dev/null
    return
  fi
  tmp="${INTENT}.tmp.$$"
  jq -n \
    --arg tenant_id "${TENANT_ID}" \
    --arg op_id "${OP_ID}" \
    --arg host_id "${EXPECTED_HOST_ID}" \
    --arg vm_num "${VM_NUM}" \
    --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
      {
        tenant_id: $tenant_id,
        op_id: $op_id,
        host_id: $host_id,
        vm_num: ($vm_num | tonumber),
        created_at: $created_at
      }
    ' >"${tmp}"
  chmod 0600 "${tmp}"
  mv -f "${tmp}" "${INTENT}"
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

  OVERLAY_FD_ID=""
  for fd in "${proc}"/fd/*; do
    fd_id="$(stat -Lc '%d:%i' "${fd}" 2>/dev/null || true)"
    [ "${fd_id}" = "${OVERLAY_ID}" ] && OVERLAY_FD_ID="${fd_id}"
  done
  [ "${OVERLAY_FD_ID}" = "${OVERLAY_ID}" ] || return 1
  tombstone_id="$(stat -Lc '%d:%i' "${TOMBSTONE}" 2>/dev/null || true)"
  [ -z "${tombstone_id}" ] || [ "${OVERLAY_ID}" != "${tombstone_id}" ] || return 1
}

assert_fence || finish_superseded
load_or_create_intent || {
  write_result "FAILED" "could not persist reset intent"
  exit 70
}

if [ ! -f "${DROP_MARKER}" ] && [ ! -f "${TOMBSTONE}" ]; then
  if ! "${HOST_HOME}/stop-vm.sh" "${TENANT_ID}" "${VM_NUM}"; then
    write_result "FAILED" "stop-vm.sh failed before the overlay commit point"
    exit 71
  fi
  assert_fence || finish_superseded
  if [ -e "${OVERLAY}" ]; then
    mv "${OVERLAY}" "${TOMBSTONE}"
  fi
  printf '%s\n' "${ATTEMPT_ID}" >"${DROP_MARKER}.tmp.$$"
  mv -f "${DROP_MARKER}.tmp.$$" "${DROP_MARKER}"
  log "overlay commit point completed once for op ${OP_ID}"
fi

if collect_evidence; then
  assert_fence || finish_superseded
  finish_succeeded "existing runtime already has this operation's fresh overlay"
  exit 0
fi

assert_fence || finish_superseded
set +e
"$@" 7>&-
launch_rc=$?
set -e
if [ "${launch_rc}" -ne 0 ]; then
  write_result "FAILED" "launch-vm.sh failed after the overlay commit point (rc=${launch_rc})"
  exit "${launch_rc}"
fi

collect_evidence || {
  write_result "FAILED" "Firecracker overlay FD evidence did not match the fresh overlay"
  exit 72
}
assert_fence || finish_superseded
finish_succeeded "Firecracker fresh overlay FD evidence verified"
