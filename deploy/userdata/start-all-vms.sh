#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# start-all-vms.sh — HOST-LOCAL fan-out: start EVERY microVM on THIS host in
# bounded parallel. The control plane sends ONE SSM command per host (not one
# per VM), so SSM concurrency == number of hosts (single/low double digits),
# NOT number of VMs (380×N). This is the only architecture that hits the
# "control plane consumes 380 openclaw starts within 1 minute" goal — the old
# per-tenant SSM path collapsed at ~40 concurrent (SSM single-instance limit,
# measured on : 40 concurrent → 11 TimedOut).
#
# Each VM start is just `launch-vm.sh <tenant_id> <vm_num>` (data disk is
# reused, .stopped marker is cleared by launch-vm.sh:76). We reuse the EXACT
# same per-VM launch path host-agent's _recover_vm uses, so there is no second
# code path to keep in sync.
#
# Usage: start-all-vms.sh [max_parallel]
#   max_parallel — bounded concurrency (default 96 = the metal host's vCPU count).
#   Launch is heavier than stop (mounts data disk, cp skills, jq inject, boots FC).
#   MEASURED (us-east-1 r8g.metal-24xl, 2026-07-01, 380 VMs): start wall-clock is
#   ~50s and FLAT across max_parallel=96/160/256 (50.1s / 51.0s / 50.4s) — the
#   bottleneck is each firecracker's fixed cold-boot cost (kernel load + disk
#   mount + boot), NOT fan-out concurrency. So raising this further does NOT
#   speed up a 380-VM start; 96 (one slot per vCPU) is the sweet spot. STOP is
#   sub-second per VM so it keeps a higher ceiling (see stop-all-vms.sh).
#   Control-plane end-to-end (POST /hosts/fleet-power → SSM → host) adds ~5-8s of
#   dispatch on top, landing 380-start at ~55-58s: inside the 1-minute SLA but
#   thin margin — the lever for more headroom is faster per-VM boot, not parallelism.

set -uo pipefail
VM_DIR="/data/firecracker-vms"
MAX_PARALLEL="${1:-96}"
log() { echo "[oc:start-all] $(date +%H:%M:%S) $*"; }

if [ ! -d "${VM_DIR}" ]; then
  log "no VM dir ${VM_DIR} — nothing to start"
  exit 0
fi

started=0
skipped=0
# Throttle: cap concurrent launch-vm.sh children at MAX_PARALLEL using a simple
# job-count gate (portable, no GNU parallel dependency).
_wait_for_slot() {
  while [ "$(jobs -rp | wc -l)" -ge "${MAX_PARALLEL}" ]; do
    # wait -n returns when ANY child exits (bash 4.3+, present on Ubuntu metal);
    # fall back to a short sleep on the off chance it's unavailable.
    wait -n 2>/dev/null || sleep 0.2
  done
}

for vm_path in "${VM_DIR}"/*/; do
  [ -d "${vm_path}" ] || continue
  cfg="${vm_path}vm.json"
  [ -f "${cfg}" ] || continue
  tenant_id="$(basename "${vm_path}")"
  # Parse vm.json with jq (present on host); fall back to skip if unreadable.
  vm_num="$(jq -r '.vm_num // empty' "${cfg}" 2>/dev/null)"
  vcpu="$(jq -r '.vcpu // 2' "${cfg}" 2>/dev/null)"
  mem_mb="$(jq -r '.mem_mb // 4096' "${cfg}" 2>/dev/null)"
  if [ -z "${vm_num}" ]; then
    log "skip ${tenant_id}: vm.json has no vm_num"
    skipped=$((skipped + 1))
    continue
  fi
  # Already running? (firecracker process alive for this VM's socket) → skip,
  # so a "start all" is idempotent and doesn't relaunch healthy VMs.
  if pgrep -f "api-sock ${vm_path}fc.sock" >/dev/null 2>&1; then
    skipped=$((skipped + 1))
    continue
  fi
  _wait_for_slot
  # launch-vm.sh clears .stopped (line 76) and reuses the existing data disk.
  # #266 — wrap in systemd-cat so launch diagnostics land in journald (tag
  # claw-launch) → Fluent Bit host.platform → AOS claw-logs-host, instead of
  # being swallowed by /dev/null. Console Logs viewer needs these when a VM
  # fails to come up on a fleet-wide start. Same pattern as host-agent.py:345.
  systemd-cat -t claw-launch \
    bash /home/ubuntu/launch-vm.sh "${tenant_id}" "${vm_num}" "${vcpu}" "${mem_mb}" &
  started=$((started + 1))
done

# Wait for all in-flight launches to finish so the SSM command's exit status
# reflects the whole host's start (the control plane polls this).
wait
log "DONE started=${started} skipped=${skipped} (max_parallel=${MAX_PARALLEL})"
