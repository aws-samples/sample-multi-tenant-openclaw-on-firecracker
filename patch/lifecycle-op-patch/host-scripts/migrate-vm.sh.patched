#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
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

# 迁移复活的 VM 挂在一个无 iptables 规则的 tap 上,可跨租户串网 + 可达 IMDS(数据安全
# /etc/platform.env 取(与 launch-vm.sh:90 同源),缺省与 launch-vm 一致。
[ -f /etc/platform.env ] && source /etc/platform.env

# 重活),必须与 launch-vm.sh 共用同一批 host 级冷启动并发槽(/run/lock/oc-launch-slot-<i>.lock),
# 否则批量迁移恢复绕过槽闸造洪峰。抢法与 launch-vm 一致(非阻塞快扫一遍→全满 flock -w 内核阻塞睡等,
# 零 fork 零自旋);launcher 自持 fd8,起 firecracker 时 8>&- 关掉不让 FC 继承占死槽。
_oc_acquire_slot() {
  # 与 launch-vm.sh 同款:随机选一把固定槽,单次无超时阻塞 flock(仅 1 次 exec,零自旋零重扫)。
  local n="${OC_HOST_LAUNCH_SLOTS:-30}" i
  case "$n" in ''|*[!0-9]*) n=30;; esac; [ "$n" -lt 1 ] && n=1
  mkdir -p /run/lock 2>/dev/null || true
  i=$(( (RANDOM % n) + 1 ))
  exec 8>"/run/lock/oc-launch-slot-${i}.lock"
  flock 8
}

# _harden_restored_tap <vm_num> — 为 restore 复活的 VM 建 tap + 配 /30 + 打满
# 与 launch-vm.sh:503-582 一致的隔离规则(IMDS DROP / 东西向超网 DROP / 管理端口
# INPUT DROP / IPv6 关 / MASQUERADE)。tap 名 tap-vm{N} 与源 host 一致(同 vm_num),
# 故 Firecracker snapshot/load 按原名恢复网卡时能挂上(firecracker
# network-for-clones.md:112 "tap names match their original names")。规则用 -C 幂等,
# 重复 restore 不叠加。加固在 /snapshot/load 之前,VM 一 resume 就已隔离。
_harden_restored_tap() {
  local vm_num="$1"
  local tap="tap-vm${vm_num}"
  local subnet_prefix="${SUBNET_PREFIX:-10.0}"
  local block=$(( (vm_num - 1) * 4 ))
  local o3=$(( block / 256 ))
  local o4=$(( block % 256 ))
  local host_tap_ip="${subnet_prefix}.${o3}.$(( o4 + 1 ))"
  local tenant_supernet="${subnet_prefix}.0.0/16"
  local host_iface
  host_iface=$(ip route show default | awk '{print $5}' | head -1)

  # tap 建立(幂等:已存在则复用,不存在则建;EBUSY 强清重试同 launch-vm)。
  if ! ip link show "${tap}" >/dev/null 2>&1; then
    if ! sudo ip tuntap add dev "${tap}" mode tap 2>/dev/null; then
      sudo ip link set "${tap}" down 2>/dev/null || true
      sudo ip link del "${tap}" 2>/dev/null || true
      sudo lsof -t "/sys/devices/virtual/net/${tap}" 2>/dev/null | xargs -r sudo kill -KILL 2>/dev/null || true
      sleep 2
      sudo ip tuntap add dev "${tap}" mode tap
    fi
  fi
  sudo ip addr add "${host_tap_ip}/30" dev "${tap}" 2>/dev/null || true
  sudo ip link set dev "${tap}" up
  sudo sysctl -q -w "net.ipv6.conf.${tap}.disable_ipv6=1" 2>/dev/null || true
  sudo sysctl -q -w net.ipv4.ip_forward=1

  # IMDS DROP(链首,先于 ACCEPT):堵 guest→169.254.169.254/.253 偷 host 实例凭据。
  sudo iptables -C FORWARD -i "${tap}" -d 169.254.169.254 -j DROP 2>/dev/null || \
    sudo iptables -I FORWARD 1 -i "${tap}" -d 169.254.169.254 -j DROP
  sudo iptables -C FORWARD -i "${tap}" -d 169.254.169.253 -j DROP 2>/dev/null || \
    sudo iptables -I FORWARD 1 -i "${tap}" -d 169.254.169.253 -j DROP
  # 东西向超网 DROP:堵 guest→其它租户 /30(同 SUBNET_PREFIX/16)。这是「不跨租户串网」的核心。
  sudo iptables -C FORWARD -i "${tap}" -d "${tenant_supernet}" -j DROP 2>/dev/null || \
    sudo iptables -I FORWARD 1 -i "${tap}" -d "${tenant_supernet}" -j DROP
  # #528 F1 Redis DROP:堵 guest→VPC 内路由表 Redis :6379(无 auth/TLS)。Redis 在 VPC CIDR、
  # 不在 SUBNET_PREFIX/16,东西向 DROP 盖不到;与 launch-vm.sh 清单保持同步防漂移(迁移后的 VM
  # 不能悄悄丢掉这条护栏)。guest-origin(-i tap)专用,DNAT 后的数据面包走 host_iface 不受影响。
  if [ -n "${EGRESS_VPC_CIDR:-}" ]; then
    sudo iptables -C FORWARD -i "${tap}" -d "${EGRESS_VPC_CIDR}" -p tcp --dport 6379 -j DROP 2>/dev/null || \
      sudo iptables -I FORWARD 1 -i "${tap}" -d "${EGRESS_VPC_CIDR}" -p tcp --dport 6379 -j DROP
  fi
  # 管理端口 INPUT DROP:堵 guest→host 的 host-agent(:8899/:9090)+ sshd(:22)
  # launch-vm.sh 清单保持同步,防漂移)。
  for _port in 8899 9090 22 9100; do
    sudo iptables -C INPUT -i "${tap}" -p tcp --dport "${_port}" -j DROP 2>/dev/null || \
      sudo iptables -I INPUT 1 -i "${tap}" -p tcp --dport "${_port}" -j DROP
  done
  # 同上,但在 FORWARD:INPUT 只保护本机,打别的 host 走 FORWARD。迁移路径必须与 launch
  # 一样严,否则一次 migrate 就静默降级这台租户的隔离。见 ADR-603-guest-redline-ports-forward-drop.md。
  for _port in 8899 9090 22 9100; do
    sudo iptables -C FORWARD -i "${tap}" -p tcp --dport "${_port}" -j DROP 2>/dev/null || \
      sudo iptables -I FORWARD 1 -i "${tap}" -p tcp --dport "${_port}" -j DROP
  done
  # MASQUERADE(出网)+ conntrack ACCEPT:与 launch-vm 一致,公网出口正常。
  sudo iptables -t nat -C POSTROUTING -o "${host_iface}" -j MASQUERADE 2>/dev/null || \
    sudo iptables -t nat -A POSTROUTING -o "${host_iface}" -j MASQUERADE
  sudo iptables -C FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
    sudo iptables -A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  echo "  [#179] hardened restored tap ${tap} (${host_tap_ip}/30): IMDS+east-west+redis6379+mgmt-port DROP"
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
    # 分支同一把(/run/lock/oc-launch-<tid>.lock)。此前只有 restore 持锁,snapshot 从
    # Pause 到磁盘上传全程无锁。
    #
    # 为什么现在必须加:R7 把定时备份下沉成每台 host 自驱,backup-data.sh 同样会
    # Pause/Resume 同一个 VM 并读同一批盘,而且是常态化高频。两者交错的后果双向:
    #   · 备份持锁 Pause 后正在 tar data.ext4,snapshot 这边一句 Resume 让 guest 又开始
    #     写 → 备份出来的是撕裂盘,将来恢复即数据损坏(no-data-loss);
    #   · 反过来 snapshot 正在 `aws s3 cp data.ext4`,备份那边 Resume 放开写 → 迁到新
    #     host 的盘是撕裂的,VM 起来就是坏数据。
    # 单测看不见这类交错,只有同一把 inode advisory 锁能挡住。
    #
    # 用 `flock -w` 等待而不是 restore 那样 `-n` 直接 exit 75:备份是【常态后台动作】,
    # 一次可能跑几十秒(ADR 实测单备份最坏 22s,大盘更久)。若 snapshot 撞上就立刻放弃,
    # 迁移会被日常备份反复打断 —— 而迁移通常是人/调度在处理故障 host,更该等。
    # 等满仍拿不到才 exit 75(与 restore 同一个 skip 语义,调用方已认这个码)。
    mkdir -p /run/lock 2>/dev/null || true
    exec 7>"/run/lock/oc-launch-${TENANT}.lock"
    flock -w "${OC_MIGRATE_SNAPSHOT_LOCK_WAIT:-120}" 7 || {
      echo "snapshot skip: ${TENANT} another lifecycle op (backup/launch/stop/delete) holds the per-tenant lock" >&2
      exit 75
    }
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
    # restore 或 restore 与 launch 同时改同一 VM_DIR。抢不到=另一进程正起同租户 → skip(exit 75)。
    mkdir -p /run/lock 2>/dev/null || true
    exec 7>"/run/lock/oc-launch-${TENANT}.lock"
    flock -n 7 || { echo "restore skip: ${TENANT} launch/restore already in progress (flock held)" >&2; exit 75; }
    # 再抢 host 级启动槽【再】拉 S3(下载+load+FC 全是重活,codex:抢槽必须早于下载)。
    # 抢到才占额度、超限排队;fd8 常驻本进程到 restore 完成显式关。
    _oc_acquire_slot
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
    # 网卡,VM 一 resume 立刻有网,若此时 tap 无 iptables 规则则出现跨租户串网 +
    # 可达 IMDS 的窗口。故先建 tap + 打满隔离,再起 firecracker load。
    #
    # reviewer FAIL 修复(方案1):加固必须打在 VM 真正会挂的 tap 上。Firecracker
    # snapshot 里烤死了 source 的 host_dev_name = tap-vm{SOURCE_vm_num}
    # (launch-vm.sh:703),/snapshot/load 按原名恢复网卡 → VM resume 后挂
    # tap-vm{SOURCE_vm_num}。但本脚本的位置参 VM_NUM 是 health_check 传的
    # TARGET_vm_num(target host 新占的槽,tenant_service.py:1887 snapshot 用
    # source、health_check/handler.py:391 restore 用 target)。用 target 算 tap
    # 会建一个 VM 根本不用的死 tap,规则全落空(旧 bug)。修:从随 snapshot 一起
    # 下载的 source vm.json(launch-vm.sh:130 写 "vm_num":SOURCE)读出 source
    # vm_num,对齐 VM 实际挂的网卡再打加固。vm.json 缺失/解析失败 → fail-closed
    # 退出(不能在网卡归属不明时裸奔起 VM)。
    SRC_VM_NUM=""
    if [ -s "${VM_DIR}/vm.json" ]; then
      SRC_VM_NUM=$(jq -r '.vm_num // empty' "${VM_DIR}/vm.json" 2>/dev/null || true)
    fi
    if ! printf '%s' "${SRC_VM_NUM}" | grep -qE '^[0-9]+$'; then
      echo "restore FATAL(#179): cannot read source vm_num from ${VM_DIR}/vm.json — refusing to resume VM without knowing which tap it will attach to (would leave it un-hardened / cross-tenant reachable)" >&2
      exit 24
    fi
    echo "  [#179] source vm_num=${SRC_VM_NUM} (target arg vm_num=${VM_NUM}); hardening the tap the snapshot will actually attach to"
    _harden_restored_tap "${SRC_VM_NUM}"
    # Start a Firecracker process bound to the new socket.
    # 8>&- 关掉槽锁 fd,不让长命 firecracker 继承占死槽(否则本 migrate 退出后槽仍被 FC 持着)。
    rm -f "$SOCK"
    nohup firecracker --api-sock "$SOCK" >"${VM_DIR}/fc.log" 2>&1 8>&- &
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
    # 恢复完成(VM 已 resume),释放启动槽让排队的下一个进来。
    exec 8>&- 2>/dev/null || true
    echo "restored ${TENANT} on this host"
    ;;
  *)
    echo "unknown mode: $MODE" >&2
    exit 2
    ;;
esac
