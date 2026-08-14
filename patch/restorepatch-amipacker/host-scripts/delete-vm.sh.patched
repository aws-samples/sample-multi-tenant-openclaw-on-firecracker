#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
#
# 用法: delete-vm.sh <tenant_id> <vm_num> <host_port> <guest_ip> <legacy_port> <keep_data>
# 退出: 0=全部完成 | 1=任一步失败(含 ⓿ 抢锁超时)
#
# 注:不像 launch-vm.sh 那样返回 75 区分 flock-skip。launch 的 75 表示"别人正在起同
# 一租户,本次让位即可,不算失败";而删除让位后【必须有人重投】,否则租户永卡 deleting。
# 控制面对"抢不到锁"和"某步真失败"的正确处理相同(留 deleting 等重投),故统一 exit 1,
# 不引入一个调用方分不清、也不会被区别对待的哨兵码。
#
# ── 为什么要这个脚本 ────────────────────────────────────────────────────────
# 控制面原本逐条下发【4 条】SSM 才做完删除收尾(tenant_service.py stop-vm /
# rm vm.json / route_ops delete-route / touch+rm -rf)。两个后果:
#
#  ① SendCommand 速率放大 4 倍。真机实测(2026-08-12,us-west-2,QPS20×10s 派发
#     200 个 DELETE):800 次 SendCommand / 27s ≈ 30 次/秒 → SSM 服务端
#     `ThrottlingException: Rate exceeded`(日志 89 次,且已 reached max retries: 4),
#     HTTP 502 占 89.5%。把 host 侧 CommandWorkersLimit 从 20 提到 50 【无改善】
#     (89.5%→89.0%,throttle 反从 89 涨到 99)——因为限的是控制面侧的 API 提交速率,
#     不是 host 的执行并发。合并成 1 条把速率降到 1/4。
#
#  ② `deleting` 中间态。4 条里后 2 条(route / rm -rf)落在【不可回滚区】:此时 VM 已停,
#     回滚成 running 会谎报"已毁租户存活",故只能留 deleting 等重投。任一条被限流打挂
#     就卡住——实测 46/200 = 23% 卡 deleting(全部 delete_retryable=True)。合并后控制面
#     只有【一个】判定点,"stop 成功但 rm 失败"这个中间态对控制面不再可见。
#
# 同款模式已在本仓验证:start-all-vms.sh / stop-all-vms.sh 都是"控制面发一条、host
# 本地 fan-out"。内部标杆亦然(Lambda MicroManager:大规模 microVM fleet 不靠控制面逐
# VM 直推,靠 host 本地 agent)——见 ADR-batch-delete-throttle.md §2.1。
#
# ── 幂等 ───────────────────────────────────────────────────────────────────
# 每步都对"已完成"无害:stop-vm 对已停 VM 是 no-op、rm -f 对不存在文件返 0、
# delete-route 对已清路由幂等、rm -rf 对已删目录返 0。故 SSM 重投安全。
set -uo pipefail

TENANT_ID="${1:?usage: delete-vm.sh <tenant_id> <vm_num> <host_port> <guest_ip> <legacy_port> <keep_data> [quiesced_backup]}"
VM_NUM="${2:?}"
HOST_PORT="${3:-0}"
GUEST_IP="${4:-}"
LEGACY_PORT="${5:-0}"
KEEP_DATA="${6:-true}"
# 第 7 个参数(可选,默认 false):停机后再补一次【静止盘】备份。见 ①' 的说明。
# 默认 false 保持与旧调用方的兼容(少传参数时行为完全不变)。
QUIESCED_BACKUP="${7:-false}"

VM_ROOT="/data/firecracker-vms"
VM_DIR="${VM_ROOT}/${TENANT_ID}"
log() { echo "[oc:delete] $(date +%H:%M:%S) $*"; }

log "START ${TENANT_ID} vm${VM_NUM} keep_data=${KEEP_DATA}"
T0=$SECONDS

# 与 launch-vm.sh:399 / stop-vm.sh / migrate-vm.sh 共用同一把 inode advisory 锁。
# 为什么四步必须【同一把锁一次持到底】:若只让 stop-vm.sh 持锁,它 exit 时 fd 关闭、
# 锁即释放,后面删 vm.json / 清路由 / rm -rf 三步就在无锁下跑。虽然并发 launch 会被
# launch-vm.sh:585-596 的状态门拦住(读到 deleting → exit 45 拒起,且锁与状态门之间
# 零文件系统写入),但那是"另一处代码恰好也检查了"而非本地互斥——把破坏性清理和
# launch 的互斥建立在远端 DDB 状态读上太脆。这里直接锁住,不依赖对方的门。
#
# 用 -w 20 而非 -n:删除是可重投的收尾动作,短暂让位给正在跑的 launch/restore 是对的
# (对方通常几秒内结束);超时则退 1 让控制面留 deleting 重投,不硬闯。
# 上限取 20s:控制面给本脚本 300s 预算,20s 只占零头,又远大于一次正常 launch 的持锁时长。
mkdir -p /run/lock 2>/dev/null || true
exec 9>"/run/lock/oc-launch-${TENANT_ID}.lock"
if ! flock -w 20 9; then
  log "FATAL ${TENANT_ID}: per-tenant lifecycle lock 被占超 20s(并发 launch/restore 在跑)— 拒绝删除,留 deleting 重投"
  exit 1
fi
# 把本进程持锁的 fd 号交给 stop-vm.sh 复用。不能让它 `exec 9>` 重新 open —— flock 绑
# 在【打开文件描述】上,新描述不受继承锁保护,它会 `flock -w 2` 失败 → 15s 后 FATAL。
export OC_LIFECYCLE_LOCK_FD=9

# ── ① 停 VM ───────────────────────────────────────────────────────────────
# 锁已由 ⓿ 持住并通过 OC_LIFECYCLE_LOCK_FD 交给 stop-vm.sh 复用(它据此 `exec 9<&9`
# 而非重新 open;原因见 ⓿ 的说明)。它内部 touch .stopped 让 host-agent reconcile
# 不再拉起。
_stop_rc=0
/home/ubuntu/stop-vm.sh "${TENANT_ID}" "${VM_NUM}" || _stop_rc=$?
if [ "${_stop_rc}" -ne 0 ]; then
  # stop-vm 的 lock-busy 路径也是 exit 1(:88),与真失败同码,无法在此区分。两者处理相同:
  # 拒绝继续删、让控制面留 deleting 重投(重投时并发操作大概率已收敛)。
  log "FATAL ${TENANT_ID}: stop-vm failed rc=${_stop_rc} — VM 可能仍在跑或锁被占,拒绝继续删"
  exit 1
fi

# ── ①' 静止盘补备份(仅 quiesced_backup=true)──────────────────────────────
# 关掉"备份完成 → VM 又被 resume → 这段时间的写入进不了备份却随盘一起删"的窗口
# (codex 独立复审)。窗口成因:控制面的 pre-delete backup 走 backup Lambda →
# backup-data.sh,而后者压缩完会把 VM【resume】(:79 与 trap cleanup:36);控制面随后
# 还要写 marker、发 SSM,这几秒里 VM 在跑、可以 ack 新写入。
#
# 这里在 VM 已停(① 成功)之后再备一次:此刻盘是【静止】的,不可能再有新写入,
# 所以这份产物是删盘前的最终状态。同一 tenant 的 S3 key 按时间戳命名,故这是
# 一次【追加】而非覆盖,前一份仍在(两份都可恢复,取最新即最完整)。
#
# fail-loud:这一步失败就不许继续删盘 —— 否则又回到"删了但备份不含最后的写入"。
# 控制面留 deleting 重投:重投时 ① 对已停 VM 是 no-op、这一步幂等重跑(新时间戳)。
# 只在 keep_data=false 时做:软删不删盘,没有"删前最终状态"的必要。
if [ "${KEEP_DATA}" != "true" ] && [ "${QUIESCED_BACKUP}" = "true" ]; then
  if [ -f "${VM_DIR}/data.ext4" ]; then
    _bk_rc=0
    /home/ubuntu/backup-data.sh "${TENANT_ID}" || _bk_rc=$?
    if [ "${_bk_rc}" -ne 0 ]; then
      log "FATAL ${TENANT_ID}: 静止盘补备份失败 rc=${_bk_rc} — 拒绝删盘(否则丢掉备份之后的写入),留 deleting 重投"
      exit 1
    fi
    log "静止盘补备份完成(VM 已停,此份为删前最终状态)"
  else
    # 盘已不在 = 上一次尝试已过第 ④ 步。没有可备份的东西,继续走完收尾即可
    # (控制面靠 OC_BACKUP_SOURCE_ABSENT 走同一条放行逻辑,不在此重复判定)。
    log "静止盘补备份跳过:data.ext4 已不存在(上一次尝试已删盘),继续收尾"
  fi
fi

# ── ② 删 vm.json ──────────────────────────────────────────────────────────
# 必须在 stop 成功【之后】:vm.json 是 host-agent 的 recover 标记,先删会让 host-agent
if ! rm -f "${VM_DIR}/vm.json"; then
  log "FATAL ${TENANT_ID}: rm vm.json failed — host-agent 会 recover 半删租户"
  exit 1
fi

# ── ③ 路由/DNAT/端口位图/Redis ────────────────────────────────────────────
# 一条 route_ops 命令收口 bitmap DNAT + 历史 DNAT 族 + quarantine + Redis
# (与控制面原来下发的同一条命令,原样搬过来)。
set -a
. /etc/environment 2>/dev/null || true
. /etc/platform.env 2>/dev/null || true
set +a
if ! python3 /opt/openclaw/route_ops.py delete-route \
      "${TENANT_ID}" "${HOST_PORT}" "${GUEST_IP}" "${LEGACY_PORT}"; then
  log "FATAL ${TENANT_ID}: route cleanup failed (bitmap/DNAT/Redis) — 拒绝继续,防路由泄漏"
  exit 1
fi

# ── ④ 数据盘(仅 keep_data=false)────────────────────────────────────────────
# 连带删掉)再 rm -rf。若 rm 被 SIGKILL 中断,tombstone 仍在,host-agent 的
# _gc_orphan_vm_dirs 下轮据此补删。keep_data=true 的软删【不写】tombstone → GC 绝不
# 碰其盘(no-data-loss)。`&&` 而非 `;`:touch 失败就整条失败,杜绝"盘没标记却已删"。
if [ "${KEEP_DATA}" != "true" ]; then
  if ! { touch "${VM_ROOT}/.purge-${TENANT_ID}" && rm -rf "${VM_DIR}"; }; then
    log "FATAL ${TENANT_ID}: data disk rm failed — VM 已停但盘未回收,留给 GC 兜底"
    exit 1
  fi
  # rm -rf 真成功才清 tombstone:意图票用完即焚,不留给未来的软删误用。
  #   有效的 GC 删盘许可】。若清票失败却 exit 0,控制面会把租户推进 deleted,而
  #   此刻【全部满足】。租户后续若被软删重建/或该 id 目录再次出现,GC 会凭这张陈旧
  #   票删掉本该保留的盘(no-data-loss 违规)。故清票失败必须 exit 1 留 deleting 重投;
  #   重投时前三步幂等 no-op、第 ④ 步 rm -rf 对已删目录返 0,再试清票。
  if ! rm -f "${VM_ROOT}/.purge-${TENANT_ID}"; then
    log "FATAL ${TENANT_ID}: tombstone 清理失败 — 盘已删但 .purge 票仍有效,GC 双门已全满足,拒绝报成功"
    exit 1
  fi
fi

log "DONE ${TENANT_ID} keep_data=${KEEP_DATA} (total $((SECONDS-T0))s)"
exit 0
