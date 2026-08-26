#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

TENANT_ID="${1:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_NUM="${2:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
HOST_VM_KEY="${OC_HOST_VM_KEY:-/etc/openclaw/host_vm_key}"
OC_STOP_FLUSH_MODE="${OC_STOP_FLUSH_MODE:-prefer}"

# SHORT*2 + EXTENDED 必须 >= 20(#608 前的基线单次窗口,保证本改动不降低持久性),
# 且给 suspend 的 SSM 窗口留出 TERM/KILL 与 require 回滚的余量。这里取 24。
# 不要为了让总链路塞进控制面窗口而把这个数往下压 —— #608 后基线最坏 46s(42s 是 #608 前
# 单次 20s flush 窗口的值)。#626 把预算侧抬到 50s(suspend)/66s(rebuild)与之对齐,
# 见 create_deadline.STOP_VM_WORST_SEC;要改这里的等待,先改那边的派生常量与 parity 测试。
STOP_FLUSH_SHORT_SEC=3
STOP_FLUSH_EXTENDED_SEC=18
STOP_SSH_CONNECT_SEC=3

log() { echo "[oc:stop] $(date +%H:%M:%S) $*"; }
log "stopping ${TENANT_ID} vm${VM_NUM}..."

# Serialize stop with every launch/recover path. launch-vm.sh holds this same
# per-tenant lock, so a stop racing a cold launch waits for that launch and then
# wins deterministically instead of killing a half-built VM.
#
# 把它持锁的 fd 号传进来,本脚本就复用那个 fd 而不再 `exec 9>` 重新 open。
# 为什么必须这样而不是"让调用方先持锁、脚本照旧 exec 9>":flock 绑定在【打开文件
# 描述】上,`exec 9>` 会对同一文件产生一个【新的】描述,继承来的锁保护不了它 →
# 下面的 `flock -w 2 9` 拿不到 → 走 `flock -w 15` → 15s 后 FATAL exit 1,
# 即每次都失败。复用调用方的 fd 才能让 flock 认出"这把锁本进程已持有"(flock 对
# 同一打开文件描述重复加锁是 no-op 成功)。
# 未设置该变量时行为与原来【完全一致】(delete 之外的所有调用点都不设它)。
mkdir -p /run/lock 2>/dev/null || true
LOCK_PATH="/run/lock/oc-launch-${TENANT_ID}.lock"
if [ -n "${OC_LIFECYCLE_LOCK_FD:-}" ]; then
  # 校验:必须是纯数字且该 fd 真的开着,否则宁可自己抢锁也不裸奔(fail-safe)。
  if printf '%s' "${OC_LIFECYCLE_LOCK_FD}" | grep -qE '^[0-9]+$' &&
     [ -e "/proc/self/fd/${OC_LIFECYCLE_LOCK_FD}" ]; then
    eval "exec 9<&${OC_LIFECYCLE_LOCK_FD}"
    log "reusing caller-held lifecycle lock (fd ${OC_LIFECYCLE_LOCK_FD})"
  else
    log "WARN: OC_LIFECYCLE_LOCK_FD='${OC_LIFECYCLE_LOCK_FD}' 不可用,回落自持锁"
    exec 9>"${LOCK_PATH}"
  fi
else
  exec 9>"${LOCK_PATH}"
fi
STOP_INTENT_PUBLISHED=0
LEGACY_FIRECRACKER_TERMINATED=0
GUEST_FLUSHED=0

# Freshness sentinel used by control-plane self-heal. A host script without this
# marker may terminate a live guest without first proving its page cache reached
# data.ext4.
OC_STOP_GUEST_FLUSH_REQUIRED=1

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

# ⑰ codex 独立复审第十轮 —— 判"这个进程是不是 Firecracker"必须用 /proc/<pid>/comm,
# 不能用 exe 的 basename。
#
# 实测(Linux 容器):二进制被替换或删除后,`readlink /proc/<pid>/exe` 返回
# `/path/firecracker (deleted)`,于是 `${exe##*/}` 变成 `firecracker (deleted)` ——
# 与 "firecracker" 不等,判据漏判。而滚动升级换镜像正是这个场景:进程还在跑,它的
# 二进制已经被换掉。
# 漏判的后果按调用点分两种,都很硬:
#   · 本脚本的孤儿扫描漏掉一个【活着的】孤儿 → 报告"已停" → 调用方释放 ps_<n> →
#     下一个租户被排到活 VM 上(跨租户);
#   · 探测函数漏判 fc_alive → 强制删除以为"VM 已停"而放行。
# comm 恒为进程名本身(不含路径、不带 deleted 后缀),截断到 15 字符 ——
# "firecracker" 11 字符,安全。
# 判据仍然是【两条】:comm + 完整的 `--api-sock <本租户目录>/fc.sock`。单靠 comm 会命中
# 别的租户的 Firecracker。
_oc_is_firecracker() {
  [ "$(cat "/proc/$1/comm" 2>/dev/null)" = "firecracker" ]
}

# 本批次新加的孤儿扫描路径,发信号前要逐个复验身份 —— pid 空间会绕回,而 TERM 与 KILL
# 之间还有 sleep,复用之后 kill 打中的可能是另一个租户的 VM(no-cross-tenant)。
# 判据必须与扫描时【完全相同】:comm 是 firecracker + cmdline 精确含本租户的 api-sock。
# 抽成顶层而不是内嵌一份副本:两份副本就有两处会漂,而"复验判据与扫描判据一致"恰恰是
# 它唯一要紧的性质。
#
# 只服务【孤儿路径】。legacy 锁那条路刻意保持 bb 基线原样(见下方 kill 处的说明)——
_oc_is_our_fc() {
  local _p="/proc/$1" _cmd
  [ -d "${_p}" ] || return 1
  _oc_is_firecracker "$1" || return 1
  [ -r "${_p}/cmdline" ] || return 1
  _cmd="$(tr '\0' ' ' < "${_p}/cmdline" 2>/dev/null)" || return 1
  case "${_cmd}" in
    *"--api-sock ${VM_DIR}/fc.sock"*) return 0 ;;
    *) return 1 ;;
  esac
}

flush_guest_before_stop() {
  [ "${GUEST_FLUSHED}" -eq 0 ] || return 0

  local proc guest_ip="" vm_alive=0 flush_rc=255 flush_reason="unreachable"
  local flush_attempts=0
  for proc in /proc/[0-9]*; do
    if _oc_is_our_fc "${proc##*/}"; then
      vm_alive=1
      break
    fi
  done
  # A stopped/non-existent VM has no recoverable page cache. Do not turn an
  # idempotent retry into a permanent failure just because SSH is unavailable.
  [ "${vm_alive}" -eq 1 ] || return 0

  guest_ip="$(python3 -c \
    "import json; print(json.load(open('${VM_DIR}/vm.json')).get('guest_ip',''))" \
    2>/dev/null || true)"
  if [ -n "${guest_ip}" ] && [ -f "${HOST_VM_KEY}" ]; then
    timeout "${STOP_FLUSH_SHORT_SEC}" ssh -i "${HOST_VM_KEY}" \
       -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       -o ConnectTimeout="${STOP_SSH_CONNECT_SEC}" -o LogLevel=ERROR -o BatchMode=yes \
       "agent@${guest_ip}" 'sync' >/dev/null 2>&1
    flush_rc=$?
    flush_attempts=$((flush_attempts + 1))

    if [ "${flush_rc}" -eq 0 ]; then
      GUEST_FLUSHED=1
      log "guest filesystem sync acknowledged before stop (${guest_ip})"
      return 0
    fi

    # ssh=255 是端口不通、认证失败、连接拒绝等 SSH 层失败,说明 guest 当前不可达;
    # 只给一次同样 3 秒的快速重试覆盖瞬时忙,不把无意义等待拉成长窗口。其它非零
    # 同样没有可靠的 sync ACK,按不可达处理但不额外扩张尝试次数。
    if [ "${flush_rc}" -eq 255 ]; then
      timeout "${STOP_FLUSH_SHORT_SEC}" ssh -i "${HOST_VM_KEY}" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout="${STOP_SSH_CONNECT_SEC}" -o LogLevel=ERROR -o BatchMode=yes \
        "agent@${guest_ip}" 'sync' >/dev/null 2>&1
      flush_rc=$?
      flush_attempts=$((flush_attempts + 1))
      if [ "${flush_rc}" -eq 0 ]; then
        GUEST_FLUSHED=1
        log "guest filesystem sync acknowledged before stop (${guest_ip})"
        return 0
      fi
    fi

    if [ "${flush_rc}" -eq 124 ]; then
      # timeout=124 与 255 必须分流:124 表示 sync 已进入可等待阶段但没在 3 秒短窗
      # 内 ACK,guest 活着且可能正在刷大量脏页,才值得升级到唯一一次 18 秒长窗。
      # 第二次已从 255 变成 124 时也按这份新证据升级;总 SSH 次数仍硬封顶为 3 次。
      flush_reason="sync-timeout"
      timeout "${STOP_FLUSH_EXTENDED_SEC}" ssh -i "${HOST_VM_KEY}" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout="${STOP_SSH_CONNECT_SEC}" -o LogLevel=ERROR -o BatchMode=yes \
        "agent@${guest_ip}" 'sync' >/dev/null 2>&1
      flush_rc=$?
      flush_attempts=$((flush_attempts + 1))
      if [ "${flush_rc}" -eq 0 ]; then
        GUEST_FLUSHED=1
        log "guest filesystem sync acknowledged before stop (${guest_ip})"
        return 0
      fi
      [ "${flush_rc}" -eq 124 ] || flush_reason="unreachable"
    fi
  fi

  if [ "${OC_STOP_FLUSH_MODE}" = "require" ]; then
    # The VM is still alive, so its dirty pages are recoverable. Keep it running
    # and let the caller retry instead of converting a transient SSH failure into
    # permanent transcript/data loss.
    if [ "${STOP_INTENT_PUBLISHED}" -eq 1 ]; then
      rm -f "${VM_DIR}/.stopped" || true
      STOP_INTENT_PUBLISHED=0
    fi
    log "FATAL: OC_STOP_GUEST_FLUSH_FAILED VM alive but guest sync did not ACK (${guest_ip:-no-ip}); refusing TERM/KILL"
    return 1
  fi

  # prefer 下绝不能撤回 .stopped:host-agent 把“vm.json + 无进程 + 无 .stopped”
  # 当成崩溃并自动拉起。这里接下来正要 TERM/KILL,撤标记会把刚停掉的 VM 立即复活。
  log "WARN: OC_STOP_GUEST_FLUSH_UNACKED reason=${flush_reason} attempts=${flush_attempts} (${guest_ip:-no-ip}); proceeding with TERM/KILL after bounded flush attempts"
  return 0
}

# Before this MR, Firecracker inherited fd9 from launch-vm.sh and retained the
# lifecycle flock for its entire lifetime. A script-only rollout therefore
# deadlocks every stop of an already-running VM. Identify that legacy state by
# both the exact tenant API socket and the lock inode; a command-line match alone
# is not enough to authorize killing a process.
find_legacy_firecracker_lock_holders() {
  local lock_identity proc pid cmdline fd fd_identity
  lock_identity="$(stat -Lc '%d:%i' "${LOCK_PATH}" 2>/dev/null)" || return 1
  LEGACY_FIRECRACKER_PIDS=()

  for proc in /proc/[0-9]*; do
    _oc_is_firecracker "${proc##*/}" || continue
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
    flush_guest_before_stop || exit 1
    log "legacy Firecracker inherited lifecycle lock; migrating pid(s): ${LEGACY_FIRECRACKER_PIDS[*]}"
    curl -sf --max-time 2 --unix-socket "${VM_DIR}/fc.sock" -X PUT http://localhost/actions \
      -H 'Content-Type: application/json' -d '{"action_type":"SendCtrlAltDel"}' 2>/dev/null || true
    sleep 2
    # ⚠ 这里【故意保持 bb 基线原样】,不加发信号前的身份复验。
    #
    # codex 第二十五轮在这条 legacy 路径上抓出一个真缺陷:上面那次 SendCtrlAltDel 的目的
    # 就是让 Firecracker 退出,所以"pid 在这 3 秒里已经不是它了"是预期路径而不是边缘情况;
    # 而 `kill -0` 只查"存在"、查不出"还是不是同一个进程",pid 复用时会打中别人的 VM
    # (no-cross-tenant)。
    #
    # bb 基线(aa18bd8f)上就已存在,是 fd9 继承那次滚动升级的兼容机制,本批次从未改动它的
    # 逻辑。按项目规则 2「只改必须改的,只清理自己的烂摊子」,它该单独开 issue 修,而不是
    # 顺手混进一个 P0 + 命中 IaC 安全红线的分支里 —— 那会让最后签 merge 的人分不清哪些是
    #
    # 已修的是【本批次自己引入】的那条孤儿路径(下方 ORPHAN_PIDS 那段),它有完整复验。
    # 判据不是"缺陷有多严重",而是"这段代码是不是本批次引入或改动的"。
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
  #
  # 这里原先是 `log "VM directory absent; nothing to stop"; exit 0` —— 目录没了就
  # 报告"已停"。那是错的:Linux 上 `rm -rf` 掉目录【不会】杀死持有那些文件描述符的
  # Firecracker,所以"目录不在"与"进程已停"是两件事。而本脚本的返回值被两个地方当作
  # 【权威停机证明】用:
  #   · tenant_service 的强制删除快路径(第五轮加的,CAS 前的确认)。
  # 两处拿到的都是"确认已停",随后释放 ps_<n> / 扣账本 —— 而 VM 还在跑。第五轮那次修复
  # 因此对它要针对的那一格(盘已删 + FC 还活着)是**空转**:上面那段 legacy kill 只在
  # `! flock -w 2 9`(锁被别人持着)时才走,而孤儿 FC 并不持有生命周期锁。
  #
  # 能杀:Firecracker 的 cmdline 里那串 `--api-sock ${VM_DIR}/fc.sock` 在目录被删后
  # 【仍然存在】(cmdline 是进程自己的内存,不随文件系统变),所以按它扫 /proc 就能精确
  # 定位。判据用完整的 `--api-sock <本租户目录>/fc.sock` 而不是租户名子串 —— 后者会被
  # 前缀相同的另一个租户名匹配上(t-1 匹配 t-10),那就是跨租户误杀。
  log "VM directory absent; scanning for an orphaned Firecracker before declaring it stopped"
  ORPHAN_PIDS=()
  for proc in /proc/[0-9]*; do
    _oc_is_firecracker "${proc##*/}" || continue
    [ -r "${proc}/cmdline" ] || continue
    cmdline="$(tr '\0' ' ' < "${proc}/cmdline" 2>/dev/null)" || continue
    case "${cmdline}" in
      *"--api-sock ${VM_DIR}/fc.sock"*) ORPHAN_PIDS+=("${proc##*/}") ;;
      *) continue ;;
    esac
  done
  if [ "${#ORPHAN_PIDS[@]}" -eq 0 ]; then
    log "no orphaned Firecracker for ${TENANT_ID}; nothing to stop"
    exit 0
  fi
  log "orphaned Firecracker still running for ${TENANT_ID} (disk already reclaimed); pid(s): ${ORPHAN_PIDS[*]}"
  # 每次发信号【之前】重验身份(codex 独立复审第七轮)。`kill -0` 只证明"某个进程还在",
  # 不证明【还是同一个】—— 从扫描到 TERM、再到 2 秒后的 KILL,原进程可能已退出而它的
  # pid 被系统回收给了另一个进程。那时 KILL 打的是一个无关进程(可能是同 host 上别的
  # 租户的 Firecracker,甚至是 host 自己的守护进程)。pid 空间会绕回,这不是理论风险。
  # 判据用与扫描时【完全相同】的两条:exe 名为 firecracker + cmdline 含本租户的
  # `--api-sock <VM_DIR>/fc.sock`。身份变了就跳过,不发信号。
  # ㉚ 判据已抽到顶层 _oc_is_our_fc(与 legacy 路径共用同一份代码)。
  # 原来这里是一个内嵌的同名函数副本 —— 两份副本就有两处会漂,而"复验判据必须与扫描
  # 判据完全相同"恰恰是它唯一要紧的性质。
  for _pid in "${ORPHAN_PIDS[@]}"; do
    if _oc_is_our_fc "${_pid}"; then
      kill -TERM "${_pid}" 2>/dev/null || true
    else
      log "pid ${_pid} is no longer our orphan (exited / pid reused) — not signalling"
    fi
  done
  sleep 2
  for _pid in "${ORPHAN_PIDS[@]}"; do
    if _oc_is_our_fc "${_pid}"; then
      kill -KILL "${_pid}" 2>/dev/null || true
    fi
  done
  sleep 1
  # 复检:必须**再扫一遍**确认没有匹配的进程活着,而不是"发过 KILL 就算完"。
  # 发信号成功 ≠ 进程已消失(D 状态、正在落盘的 KILL 都可能还在),而调用方拿这个
  # 返回码去释放槽位/扣账本,fail-closed 才安全:还剩就非零退出,调用方不释放、可重试。
  for proc in /proc/[0-9]*; do
    _oc_is_firecracker "${proc##*/}" || continue
    [ -r "${proc}/cmdline" ] || continue
    cmdline="$(tr '\0' ' ' < "${proc}/cmdline" 2>/dev/null)" || continue
    case "${cmdline}" in
      *"--api-sock ${VM_DIR}/fc.sock"*)
        log "FATAL: orphaned Firecracker ${proc##*/} survived TERM+KILL — NOT reporting stopped"
        exit 1
        ;;
    esac
  done
  log "orphaned Firecracker terminated and confirmed gone for ${TENANT_ID}"
  exit 0
fi

# A successful return from this point is allowed to destroy guest page cache.
# Require a sync ACK first whenever the target Firecracker is still alive.
flush_guest_before_stop || exit 1

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
