#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# resize-disk.sh — grow a tenant's data.ext4 sparse file in place (issue #22).
#
# Usage: resize-disk.sh <tenant_id> <vm_num> <new_size_mb>
#
# Flow: pause Firecracker VM → truncate the sparse ext4 to the new size →
# run e2fsck + resize2fs → resume the VM. We operate on the file directly
# (data.ext4 is the whole device, no partition table) so we don't need
# in-guest partprobe/lsblk juggling.

set -euo pipefail

TENANT="${1:?usage: resize-disk.sh <tenant> <vm_num> <new_size_mb>}"
VM_NUM="${2:?missing vm_num}"
NEW_MB="${3:?missing new_size_mb}"

VM_DIR="/data/firecracker-vms/${TENANT}"
SOCK="${VM_DIR}/fc.sock"
DATA="${VM_DIR}/data.ext4"

[ -f "$DATA" ] || { echo "no data.ext4 at $DATA"; exit 1; }
[ -S "$SOCK" ] || { echo "no fc.sock at $SOCK"; exit 1; }

_curl_fc() {
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sf --unix-socket "$SOCK" -X "$method" \
      "http://localhost${path}" -H "Content-Type: application/json" -d "$body"
  else
    curl -sf --unix-socket "$SOCK" -X "$method" "http://localhost${path}"
  fi
}

# 1) Pause so the guest can't write while we grow the FS.
_curl_fc PATCH /vm '{"state":"Paused"}'

# 2) Grow the sparse ext4 file.
truncate -s "${NEW_MB}M" "$DATA"

# 3) ext4 fsck + grow. -fy is mandatory for resize2fs.
e2fsck -fy "$DATA" || true
resize2fs "$DATA"

# 4) Resume.
_curl_fc PATCH /vm '{"state":"Resumed"}'

echo "resized ${TENANT} data disk → ${NEW_MB}MB"
