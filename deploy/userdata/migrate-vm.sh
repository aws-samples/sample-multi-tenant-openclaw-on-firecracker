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
    echo "snapshot ${TENANT} → ${S3_URI}"
    ;;
  restore)
    mkdir -p "$VM_DIR"
    aws s3 cp "${S3_URI}/snapshot.vm"  "${VM_DIR}/snapshot.vm" --quiet
    aws s3 cp "${S3_URI}/snapshot.mem" "${VM_DIR}/snapshot.mem" --quiet
    aws s3 cp "${S3_URI}/vm.json"      "${VM_DIR}/vm.json" --quiet
    # Start a Firecracker process bound to the new socket.
    rm -f "$SOCK"
    nohup firecracker --api-sock "$SOCK" >"${VM_DIR}/fc.log" 2>&1 &
    sleep 1
    # Load the snapshot.
    curl -sf --unix-socket "$SOCK" -X PUT "http://localhost/snapshot/load" \
      -H "Content-Type: application/json" \
      -d "{\"snapshot_path\":\"${VM_DIR}/snapshot.vm\",\"mem_file_path\":\"${VM_DIR}/snapshot.mem\",\"resume_vm\":true}"
    echo "restored ${TENANT} on this host"
    ;;
  *)
    echo "unknown mode: $MODE" >&2
    exit 2
    ;;
esac
