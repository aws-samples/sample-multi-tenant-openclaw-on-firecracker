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

# Issue #72: BALLOON_ENABLED comes from /etc/platform.env (written by
# init-host.sh). When balloon is on, host-agent polls this VM's /balloon
# socket every few seconds; that traffic races PUT /snapshot/create on
# Firecracker's single-threaded API server. We drop a per-tenant sentinel so
# host-agent backs off (skips probing + balloon PATCH) for the pause window.
[ -f /etc/platform.env ] && . /etc/platform.env 2>/dev/null || true
BALLOON_ENABLED="${BALLOON_ENABLED:-false}"
SENTINEL="${VM_DIR}/.migrating"

_migration_begin() {
  # Signal host-agent to quiesce, then let any in-flight agent curl drain.
  : > "$SENTINEL" 2>/dev/null || true
  # host-agent poll interval is 5s (host-agent.service OC_AGENT_POLL_INTERVAL=5);
  # sleep just over one interval so an already-started probe/balloon curl
  # finishes before we pause + snapshot.
  sleep 6
}

_migration_end() {
  rm -f "$SENTINEL" 2>/dev/null || true
}

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
    # Issue #72: quiesce host-agent's balloon poller for the whole pause window.
    # The sentinel is honored by host-agent (_is_migrating) so it stops probing
    # (no dead-zone relaunch of the paused VM) and stops PATCHing /balloon. A
    # trap guarantees the sentinel + a Resume attempt happen even on an
    # unexpected exit, so we never strand the VM Paused or wedged out of
    # supervision.
    if [ "$BALLOON_ENABLED" = "true" ]; then
      _snap_cleanup() { _curl_fc PATCH /vm '{"state":"Resumed"}' >/dev/null 2>&1 || true; _migration_end; }
      trap '_snap_cleanup' EXIT
      _migration_begin
      # Best-effort: deflate the balloon to 0 before snapshot (FC maintainer
      # guidance, firecracker#5566). stats_polling_interval_s can't be disabled
      # post-boot, but a deflated + quiesced balloon snapshots cleanly.
      _curl_fc PATCH /balloon '{"amount_mib":0}' >/dev/null 2>&1 || true
    fi
    # 1) Pause for a consistent snapshot. Tolerate a pause failure with a
    #    visible message rather than a bare `set -e` abort (the VM stays
    #    running, so we can still bail cleanly below).
    if ! _curl_fc PATCH /vm '{"state":"Paused"}'; then
      echo "pause failed on source; aborting snapshot (VM left running)" >&2
      [ "$BALLOON_ENABLED" = "true" ] && _migration_end
      exit 51
    fi
    # 2) Snapshot to local files. Capture Firecracker's error body (issue #72):
    #    if /snapshot/create fails (e.g. a stray concurrent /balloon request on
    #    Firecracker's single-threaded API socket) the old bare `curl -sf` under
    #    `set -e` turned it into a silent exit 52 AND skipped the Resume below,
    #    stranding the tenant Paused. Echo the FC body FIRST (the health_check
    #    sweep truncates SSM stderr to ~200 chars) and always resume on failure.
    SNAPSHOT_PATH="${VM_DIR}/snapshot.vm"
    MEMFILE_PATH="${VM_DIR}/snapshot.mem"
    SNAP_RC=0
    SNAP_OUT=$(curl -s --show-error --unix-socket "$SOCK" -X PUT \
      "http://localhost/snapshot/create" -H "Content-Type: application/json" \
      -d "{\"snapshot_path\":\"${SNAPSHOT_PATH}\",\"mem_file_path\":\"${MEMFILE_PATH}\"}" \
      -w 'HTTP:%{http_code}' 2>&1) || SNAP_RC=$?
    if [ "$SNAP_RC" -ne 0 ] || ! printf '%s' "$SNAP_OUT" | grep -q 'HTTP:2[0-9][0-9]'; then
      echo "snapshot/create failed (curl rc=$SNAP_RC). Firecracker said: ${SNAP_OUT}" >&2
      echo "(balloon migrate_mode=cold avoids this path entirely — issue #72)" >&2
      tail -5 "${VM_DIR}/fc.log" >&2 2>/dev/null || true
      # Never leave the source paused — the async sweep's rollback assumes the
      # source VM was resumed after a failed snapshot.
      _curl_fc PATCH /vm '{"state":"Resumed"}' || true
      [ "$BALLOON_ENABLED" = "true" ] && _migration_end
      exit 52
    fi
    # 3) Resume the source so the user only sees a brief pause if migration fails.
    _curl_fc PATCH /vm '{"state":"Resumed"}' || true
    # Balloon quiesced window is over — let host-agent resume supervision.
    if [ "$BALLOON_ENABLED" = "true" ]; then _migration_end; trap - EXIT; fi
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
    # Issue #72: sentinel BEFORE writing vm.json — otherwise the target host's
    # host-agent sees vm.json with no Firecracker process and races _recover_vm
    # (launch-vm.sh) against our /snapshot/load. Cleared once the VM is loaded.
    : > "$SENTINEL" 2>/dev/null || true
    trap 'rm -f "$SENTINEL" 2>/dev/null || true' EXIT
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

    # ── T3-1 P2b: reconstruct the guest's network + route on this host ──
    # The live snapshot preserves the SOURCE's guest networking baked into
    # snapshot.mem: guest IP 10.0.<src_vm_num>.2 talking to tap tap-vm<src_vm_num>.
    # We MUST recreate that exact tap here (NOT this invocation's target VM_NUM)
    # or the resumed guest sends packets into a tap that doesn't exist and the
    # dashboard is dead. Parse the source vm_num / guest_ip from the shipped
    # vm.json. Previously `restore` skipped ALL of this — cold-restore got it via
    # launch-vm.sh, but live restore left the VM unreachable AND (critically)
    # without the IMDS DROP, i.e. a tenant→instance-credential theft hole.
    SRC_VM_NUM=$(sed -n 's/.*"vm_num"[ ]*:[ ]*\([0-9]*\).*/\1/p' "${VM_DIR}/vm.json" 2>/dev/null) || true
    SRC_GUEST_IP=$(sed -n 's/.*"guest_ip"[ ]*:[ ]*"\([0-9.]*\)".*/\1/p' "${VM_DIR}/vm.json" 2>/dev/null) || true
    if [ -z "${SRC_VM_NUM}" ]; then
      echo "restore: no vm_num in vm.json — cannot rebuild guest network" >&2
      exit 25
    fi
    R_TAP="tap-vm${SRC_VM_NUM}"
    R_HOST_TAP_IP="${SUBNET_PREFIX:-10.0}.${SRC_VM_NUM}.1"
    R_GUEST_IP="${SRC_GUEST_IP:-${SUBNET_PREFIX:-10.0}.${SRC_VM_NUM}.2}"
    R_IFACE=$(ip route show default | awk '{print $5}' | head -1)
    sudo ip link del "${R_TAP}" 2>/dev/null || true
    sudo ip tuntap add dev "${R_TAP}" mode tap
    sudo ip addr add "${R_HOST_TAP_IP}/24" dev "${R_TAP}"
    sudo ip link set dev "${R_TAP}" up
    sudo sysctl -q -w net.ipv4.ip_forward=1
    # SECURITY: block guest → IMDS BEFORE the ACCEPT/MASQUERADE rules (identical
    # to launch-vm.sh) — omitting it lets the restored tenant steal the host's
    # EC2 instance-profile credentials. -I inserts above the FORWARD ACCEPT.
    sudo iptables -C FORWARD -i "${R_TAP}" -d 169.254.169.254 -j DROP 2>/dev/null || \
      sudo iptables -I FORWARD 1 -i "${R_TAP}" -d 169.254.169.254 -j DROP
    sudo iptables -C FORWARD -i "${R_TAP}" -d 169.254.169.253 -j DROP 2>/dev/null || \
      sudo iptables -I FORWARD 1 -i "${R_TAP}" -d 169.254.169.253 -j DROP
    sudo iptables -t nat -C POSTROUTING -o "${R_IFACE}" -j MASQUERADE 2>/dev/null || \
      sudo iptables -t nat -A POSTROUTING -o "${R_IFACE}" -j MASQUERADE
    sudo iptables -C FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
      sudo iptables -A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    sudo iptables -C FORWARD -i "${R_TAP}" -o "${R_IFACE}" -j ACCEPT 2>/dev/null || \
      sudo iptables -A FORWARD -i "${R_TAP}" -o "${R_IFACE}" -j ACCEPT

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

    # ── T3-1 P2b: route this tenant locally on the NEW owner ──
    # Under host-tg routing the ALB /vm/* catch-all can land any host, which
    # proxies to the owner's :8081; that owner is now THIS host, so nginx here
    # MUST have the tenant's location block or every request 404s. (Under legacy
    # per-tenant routing the ALB rule was repointed by health_check, masking
    # this gap.) Mirror launch-vm.sh's block verbatim except the upstream is the
    # SOURCE guest IP preserved in the snapshot. Best-effort reload.
    sudo tee "/etc/nginx/conf.d/tenants/${TENANT}.conf" > /dev/null <<NGX
location ~ ^/vm/${TENANT}(/.*)?\$ {
    proxy_pass http://${R_GUEST_IP}:18789\$1;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$connection_upgrade;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
}
NGX
    sudo nginx -s reload 2>/dev/null || true

    rm -f "$SENTINEL" 2>/dev/null || true; trap - EXIT
    echo "restored ${TENANT} on this host"
    ;;

  # ── Issue #72: COLD migration (always-correct fallback, balloon-agnostic) ──
  # No live snapshot: stop the source VM (quiescing its data volume), ship the
  # disks + vm.json to S3, then re-launch on the target. Costs a restart (loses
  # in-memory state) but works for any VM regardless of balloon/free-page-
  # reporting. Used when balloon.migrate_mode=cold.
  cold-dump)
    [ -S "$SOCK" ] || echo "warn: no fc.sock at $SOCK (VM may already be stopped)" >&2
    # Stop the VM cleanly FIRST so data.ext4 is quiescent before we copy it.
    # stop-vm.sh writes the .stopped marker, so host-agent already skips it.
    if [ -x /home/ubuntu/stop-vm.sh ]; then
      /home/ubuntu/stop-vm.sh "$TENANT" "$VM_NUM" || echo "warn: stop-vm.sh rc=$?" >&2
    else
      _curl_fc PATCH /vm '{"state":"Paused"}' >/dev/null 2>&1 || true
    fi
    sleep 2
    for disk in data.ext4 overlay.ext4 rootfs.ext4; do
      if [ -f "${VM_DIR}/${disk}" ]; then
        aws s3 cp "${VM_DIR}/${disk}" "${S3_URI}/${disk}" --quiet && echo "  uploaded ${disk}"
      fi
    done
    [ -f "${VM_DIR}/vm.json" ] && aws s3 cp "${VM_DIR}/vm.json" "${S3_URI}/vm.json" --quiet
    echo "cold-dump ${TENANT} → ${S3_URI}"
    ;;
  cold-restore)
    mkdir -p "$VM_DIR"
    : > "$SENTINEL" 2>/dev/null || true
    trap 'rm -f "$SENTINEL" 2>/dev/null || true' EXIT
    for disk in data.ext4 overlay.ext4 rootfs.ext4; do
      aws s3 cp "${S3_URI}/${disk}" "${VM_DIR}/${disk}" --quiet 2>/dev/null \
        && echo "  fetched ${disk}" || true
    done
    # vm.json carries the tenant's vcpu/mem — fetch it BEFORE parsing (the
    # earlier bug: parsing a not-yet-fetched vm.json under `set -e` made the
    # command substitution abort the whole script with exit 2).
    aws s3 cp "${S3_URI}/vm.json" "${VM_DIR}/vm.json" --quiet 2>/dev/null \
      && echo "  fetched vm.json" || true
    # Re-launch from the shipped data volume. launch-vm.sh boots a fresh VM
    # bound to VM_NUM on this host using the standard rootfs + the tenant's
    # data.ext4 (its persistent /home/agent). It derives guest_ip / host_port
    # from VM_NUM internally, so we only pass vcpu/mem. No snapshot involved —
    # no in-memory state is preserved. Parse defensively (|| true so a missing
    # field can't abort under set -e; launch-vm.sh defaults 2 vCPU / 4096 MB).
    VCPU=""
    MEM=""
    if [ -f "${VM_DIR}/vm.json" ]; then
      VCPU=$(sed -n 's/.*"vcpu"[ ]*:[ ]*\([0-9]*\).*/\1/p' "${VM_DIR}/vm.json" 2>/dev/null) || true
      MEM=$(sed -n 's/.*"mem_mb"[ ]*:[ ]*\([0-9]*\).*/\1/p' "${VM_DIR}/vm.json" 2>/dev/null) || true
    fi
    if [ -x /home/ubuntu/launch-vm.sh ]; then
      /home/ubuntu/launch-vm.sh "$TENANT" "$VM_NUM" "${VCPU:-2}" "${MEM:-4096}" || {
        echo "launch-vm.sh failed on cold-restore" >&2; exit 23; }
    else
      echo "launch-vm.sh not found on target host" >&2; exit 24
    fi
    rm -f "$SENTINEL" 2>/dev/null || true; trap - EXIT
    echo "cold-restored ${TENANT} on this host"
    ;;
  *)
    echo "unknown mode: $MODE" >&2
    exit 2
    ;;
esac
