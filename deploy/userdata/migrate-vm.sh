#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# migrate-vm.sh — Firecracker live migration via snapshot/restore (issue #20).
#
# Two modes:
#   snapshot <tenant_id> <vm_num> <s3://bucket/prefix>
#       Pause the running VM, take a Firecracker snapshot, upload to S3.
#
#   restore  <tenant_id> <vm_num> <s3://bucket/prefix>
#       Download a snapshot from S3 and resume a Firecracker microVM
#       from it on this host.
#
# Invoked by the API Lambda via SSM SendCommand on source/target hosts.

set -euo pipefail

MODE="${1:?usage: migrate-vm.sh snapshot|restore <tenant> <vm_num> <s3-uri>}"
TENANT="${2:?missing tenant_id}"
VM_NUM="${3:?missing vm_num}"
S3_URI="${4:?missing s3 uri}"

VM_DIR="/data/firecracker-vms/${TENANT}"
SOCK="${VM_DIR}/fc.sock"

_curl_fc() {
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sf --unix-socket "$SOCK" -X "$method" \
      "http://localhost${path}" -H "Content-Type: application/json" -d "$body"
  else
    curl -sf --unix-socket "$SOCK" -X "$method" "http://localhost${path}"
  fi
}

case "$MODE" in
  snapshot)
    [ -S "$SOCK" ] || { echo "no fc.sock at $SOCK"; exit 1; }
    # 1) Pause for a consistent snapshot.
    _curl_fc PATCH /vm '{"state":"Paused"}'
    # 2) Snapshot to local files.
    SNAPSHOT_PATH="${VM_DIR}/snapshot.vm"
    MEMFILE_PATH="${VM_DIR}/snapshot.mem"
    _curl_fc PUT /snapshot/create \
      "{\"snapshot_path\":\"${SNAPSHOT_PATH}\",\"mem_file_path\":\"${MEMFILE_PATH}\"}"
    # 3) Resume the source so the user only sees a brief pause if migration fails.
    _curl_fc PATCH /vm '{"state":"Resumed"}' || true
    # 4) Upload snapshot files + vm.json to S3.
    aws s3 cp "$SNAPSHOT_PATH" "${S3_URI}/snapshot.vm" --quiet
    aws s3 cp "$MEMFILE_PATH"  "${S3_URI}/snapshot.mem" --quiet
    aws s3 cp "${VM_DIR}/vm.json" "${S3_URI}/vm.json" --quiet
    # 5) Upload the block-device backing files too. A Firecracker snapshot only
    #    records the *path* of each virtio-block backing file, not its contents,
    #    so restore on another host fails with "No such file or directory ...
    #    data.ext4" unless the disks are shipped alongside snapshot.vm/.mem.
    #    Ship whichever of the standard tenant disks exist (data = persistent
    #    tenant volume, overlay = copy-on-write rootfs layer).
    for disk in data.ext4 overlay.ext4 rootfs.ext4; do
      if [ -f "${VM_DIR}/${disk}" ]; then
        aws s3 cp "${VM_DIR}/${disk}" "${S3_URI}/${disk}" --quiet && echo "  uploaded ${disk}"
      fi
    done
    echo "snapshot ${TENANT} → ${S3_URI}"
    ;;
  restore)
    mkdir -p "$VM_DIR"
    # Download the block-device backing files FIRST — Firecracker opens them by
    # the absolute path baked into snapshot.vm during /snapshot/load, so they
    # must already be on local disk before the load call below. Missing disks
    # are what caused the "os error 2 ... data.ext4" 400 on the first real
    # cross-host migration (the snapshot mode never shipped them). Tolerate a
    # disk that doesn't exist in S3 (not every tenant has an overlay).
    for disk in data.ext4 overlay.ext4 rootfs.ext4; do
      aws s3 cp "${S3_URI}/${disk}" "${VM_DIR}/${disk}" --quiet 2>/dev/null \
        && echo "  fetched ${disk}" || true
    done
    aws s3 cp "${S3_URI}/snapshot.vm"  "${VM_DIR}/snapshot.vm" --quiet
    aws s3 cp "${S3_URI}/snapshot.mem" "${VM_DIR}/snapshot.mem" --quiet
    aws s3 cp "${S3_URI}/vm.json"      "${VM_DIR}/vm.json" --quiet
    # Edge-case guard (issue #64 follow-up): fail FAST and CLEARLY before the
    # load call if the snapshot artifacts are missing/empty or the mandatory
    # data disk didn't arrive. Without this, a truncated S3 upload or a snapshot
    # that referenced a disk we never shipped surfaces as an opaque os-error
    # deep inside /snapshot/load — minutes later, after the watchdog window. We
    # check the three invariants the load HARD-depends on:
    #   1. snapshot.vm and snapshot.mem exist and are non-empty.
    #   2. data.ext4 (the persistent tenant volume — every VM has one) arrived
    #      non-empty. A missing/zero data disk = an unrecoverable restore.
    for f in snapshot.vm snapshot.mem data.ext4; do
      if [ ! -s "${VM_DIR}/${f}" ]; then
        echo "restore preflight FAILED: ${VM_DIR}/${f} missing or empty — aborting before /snapshot/load (snapshot at ${S3_URI} is incomplete or was never shipped)" >&2
        exit 23
      fi
    done
    # Sanity: the snapshot.vm references backing files by absolute path. If the
    # source host's VM_DIR path differs from ours the load will fail; ours is the
    # canonical /data/firecracker-vms/<tenant> on every host (same launch-vm.sh
    # layout), so a path mismatch means a cross-version/cross-layout host — warn
    # loudly rather than fail cryptically inside the load.
    if command -v grep >/dev/null && grep -q '"path_on_host"' "${VM_DIR}/snapshot.vm" 2>/dev/null; then
      if ! grep -q "${VM_DIR}" "${VM_DIR}/snapshot.vm" 2>/dev/null; then
        echo "restore preflight WARN: snapshot.vm backing paths don't reference ${VM_DIR} — source host used a different layout; /snapshot/load may fail" >&2
      fi
    fi
    # Start a Firecracker process bound to the new socket.
    rm -f "$SOCK"
    nohup firecracker --api-sock "$SOCK" >"${VM_DIR}/fc.log" 2>&1 &
    sleep 1
    # Load the snapshot. Surface Firecracker's own error body on failure so the
    # SSM output explains *why* (e.g. a missing backing file) instead of just
    # curl exit 22.
    if ! curl -sf --unix-socket "$SOCK" -X PUT "http://localhost/snapshot/load" \
      -H "Content-Type: application/json" \
      -d "{\"snapshot_path\":\"${VM_DIR}/snapshot.vm\",\"mem_file_path\":\"${VM_DIR}/snapshot.mem\",\"resume_vm\":true}"; then
      echo "snapshot/load failed; firecracker said:" >&2
      tail -5 "${VM_DIR}/fc.log" >&2 || true
      exit 22
    fi
    echo "restored ${TENANT} on this host"
    ;;
  *)
    echo "unknown mode: $MODE" >&2
    exit 2
    ;;
esac
