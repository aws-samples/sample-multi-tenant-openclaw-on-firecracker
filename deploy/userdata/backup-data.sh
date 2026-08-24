#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# 备份指定租户的 data.ext4 到 S3
# 用法: backup-data.sh <tenant_id> [bucket] [prefix] [cmk_key_id]
#
# 第 4 个参数 cmk_key_id 非空时,做**客户端 envelope 加密**(防内部威胁纵深第三层):
#   KMS GenerateDataKey 取一次性数据密钥 → openssl AES-256 本地加密 .gz → 上传密文
#   (.gz.enc)+ 加密态数据密钥(.gz.key)。S3 服务端/桶管理员拿到的永远是密文;只有
#   持 CMK kms:Decrypt 权限的备份/恢复角色能解出数据密钥、再解出明文。
# 桶侧另有 Object Lock COMPLIANCE(WORM 防删改)+ SSE-KMS,三层叠加。
set -uo pipefail
TENANT_ID="${1:?Usage: backup-data.sh <tenant_id> [bucket] [prefix] [cmk_key_id]}"
[ -f /etc/platform.env ] && source /etc/platform.env
BUCKET="${2:-${BACKUP_BUCKET:-${ASSETS_BUCKET}}}"
# 中间那层 ${BACKUP_PREFIX:-} 是本次补的(codex 复审):BUCKET/CMK_KEY_ID 都走
# `${N:-${ENV:-默认}}`,只有 PREFIX 漏了读 env,写死回落 backups。而 R7 让 host 成为
# 定时备份的唯一执行者、调用方(host-agent.py:2124 `[脚本, tid]`)【不传第 3 个参数】,
# 于是 config 一改成非默认前缀,host 本地备份就全传到 backups/ 下,而恢复/AZ failover
# 去【配置的】前缀找 —— 备份明明存在却找不到,且 last_backup_at 照样推进 = 静默的
# 不可恢复。init-host.sh 已把 BACKUP_PREFIX 写进 platform.env(上面刚 source 过)。
PREFIX="${3:-${BACKUP_PREFIX:-backups}}"
CMK_KEY_ID="${4:-${BACKUP_CMK_KEY_ID:-}}"
TOKEN=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)

VM_DIR="/data/firecracker-vms/${TENANT_ID}"
DATA_FILE="${VM_DIR}/data.ext4"
SOCK="${VM_DIR}/fc.sock"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# 临时文件带 PID:锁只在【本机】互斥,而删前备份可能由控制面在另一台上重投,
# 或极端情况下锁路径不可用。同名临时文件被两个实例交替写会产出截断的归档,
# 而它随后会被上传成一个"看起来成功"的恢复点 —— 那比备份失败危险得多。
GZ_FILE="/data/tmp-backup-${TENANT_ID}.$$.gz"
# S3 key 带【运行 id】,不只是秒精度时间戳(codex 独立复审第三轮)。
#
# 我上一版在这里写"不改 key 格式,风险已被 flock 压到很低,且 versioning 保证不覆盖
# 丢失"—— **那个判断是错的**,回答的不是真正的问题:`.enc` 与 `.key` 是【两次独立
# 上传】的两个对象。跨机同秒(迁移窗口)时两台各传一对,S3 上的"当前版本"完全可能是
# A 的 .enc 配 B 的 .key —— 用 B 的密钥解不开 A 加密的数据,得到一个**无法解密的
# 恢复点**。versioning 确实不丢版本,但它挡不住这种交错配对。
#
# 为什么现在敢改格式:下游三处解析都只看【后缀 + LastModified】,不解析时间戳本身 ——
# health_check:2944 按 LastModified 倒序 + 后缀配对、tenant_service:5579 取
# max(LastModified)、backup/handler.py:122 判 endswith(".gz"/".gz.enc")。
# 插一段 run id 后仍以 .gz.enc / .gz.key 结尾,三处都不受影响。
# 用 $$(pid)+纳秒:同机由 flock 串行、跨机 pid 撞的概率再叠纳秒可忽略。
_RUN_ID="$$-$(date -u +%N 2>/dev/null || echo 0)"
S3_KEY="${PREFIX}/${TENANT_ID}/${TIMESTAMP}-${_RUN_ID}.gz"

log() { echo "[oc:backup] $(date +%H:%M:%S) $*"; }

# #545(codex 评审 #1)—— 只有【本脚本真的 pause 过】才在 cleanup 里 resume。新增的
# oc_flush_guest 会在 pause 之前 fail-closed exit,那时 VM 从没被本脚本 pause,无脑发
# Resumed 会把一个本该 running(甚至本该 paused)的租户状态弄乱。用标志把 resume 收敛到
# "我 pause 的我才 resume"。
__OC_PAUSED=0
# 该 fail-closed 还是降级;pause 段也读它,而不是裸 `[ -S "$SOCK" ]`——否则死 VM 残留的
# fc.sock 会让 pause 对一个没进程的 socket 永久失败、被 fail-closed 卡住 delete 重试
__OC_VM_ALIVE=0

# ㉑ resume 必须【有界重试 + 最终失败非零退出】(codex 独立复审第十四轮)。
#
# 此前:主路径 resume 失败只打一行 WARN 就继续上传,而 EXIT trap 里那次是同一个瞬时动作
# 再试一遍、`|| true` 吞掉结果。于是两次都失败时脚本仍 **exit 0**,上游据此推进
# last_backup_at,而**客户的 VM 被永久留在 Paused** —— 系统认为一切正常,客户的 agent
# 却停了。这比丢一次备份严重:丢备份下轮会重来,而 Paused 不会自己好。
#
# 单次 curl 失败的常见原因是瞬时的(FC API 正忙于落盘 snapshot、socket 一时不可写),
# 所以重试有意义;但不能无限重试(会把 flock 一直握着、拖死这台 host 的其它生命周期动作),
# 故有界:_OC_RESUME_TRIES 次、每次间隔递增。全部用尽仍未恢复 → 让脚本非零退出,
# 由上游 fail-loud(不推进 last_backup_at、有 SSM 失败可见),人可以据此介入。
_OC_RESUME_TRIES="${OC_RESUME_TRIES:-5}"
# ㉕ 每次 curl 的墙钟上限(codex 独立复审第二十一轮)。
#
# 上面 ㉑ 那套有界重试【只界定了次数,没界定时长】:curl 原来不带 --max-time,一个卡住的
# FC API socket 能让单次调用挂任意久;再叠上 sleep 1+2+3+4+5 = 15s。
# 而 host-agent 在备份超时后先 SIGTERM、只等 _BACKUP_TERM_GRACE_SEC 就 SIGKILL ——
# 那个默认值当时是 15s,注释还写着"cleanup 只有一次 rm 与一次 curl,都是秒级"。
# 那句话在 ㉑ 之前是真的,㉑ 把它变成了假话:**EXIT trap 里的 Resume 会被 SIGKILL 掐断,
# 客户 VM 永久留在 Paused** —— 正是 ㉑ 本身要消灭的那个后果,从另一扇门回来了。
#
# 所以这里把"有界"补成【时长也有界】,让最坏耗时可以算出来:
#     最坏 = 次数 × max-time + (1+2+…+(次数-1))
#          = 5 × 5s        + 10s              = 35s
# 并据此把 host-agent 的宽限期抬到 60s(留 25s 余量)。两个数字分处 bash 与 Python、
# 无法共享常量,故加了一道 parity 测试把它们锁在一起
# (test_469_r7...::TestResumeBudgetFitsInTheTermGrace),谁改一边不改另一边就红。
_OC_RESUME_MAX_TIME="${OC_RESUME_MAX_TIME:-5}"

# 尝试把本脚本 pause 过的 VM 恢复回 running。成功返回 0 并清 __OC_PAUSED;
# 用尽重试仍失败返回 1(调用方决定 fail-loud 还是仅记录)。
oc_resume_vm() {
  [ "${__OC_PAUSED}" = "1" ] || return 0
  [ -S "$SOCK" ] || {
    log "WARN: cannot resume ${TENANT_ID}: fc.sock gone (VM likely died during backup)"
    return 1
  }
  local _i=1
  while [ "${_i}" -le "${_OC_RESUME_TRIES}" ]; do
    if curl -sf --max-time "${_OC_RESUME_MAX_TIME}" --unix-socket "$SOCK" \
        -X PATCH http://localhost/vm \
        -H 'Content-Type: application/json' -d '{"state":"Resumed"}' >/dev/null 2>&1; then
      __OC_PAUSED=0
      [ "${_i}" -eq 1 ] && log "VM resumed" || log "VM resumed (attempt ${_i})"
      return 0
    fi
    log "WARN: resume attempt ${_i}/${_OC_RESUME_TRIES} failed for ${TENANT_ID}"
    # 最后一次失败之后【不再 sleep】—— 睡完就退出循环,那段等待纯属浪费,而它花掉的
    # 正是 SIGKILL 到来之前的宽限时间。
    if [ "${_i}" -lt "${_OC_RESUME_TRIES}" ]; then
      sleep "${_i}"
    fi
    _i=$((_i + 1))
  done
  return 1
}

cleanup() {
  rm -f "$GZ_FILE"
  # 最后一道:走到这里说明主路径没能恢复(或根本没跑到那一步,例如压缩失败)。
  # 再试一轮带退避的恢复;仍然失败就 fail-loud —— 但 trap 里不能改退出码,所以只能
  # 打一条足够刺眼的日志,真正的非零退出由主路径那次调用负责。
  if ! oc_resume_vm; then
    # ㉘ 这一行的哨兵 OC_BACKUP_VM_LEFT_PAUSED 是【机器判据】,不是给人看的措辞
    # (codex 第二十三轮那条判据的第四个面,我自己数出来的)。
    #
    # 备份失败时上游 _tenant_suspend 会把 status 回滚成 running/stopped。但如果失败的原因
    # 而且 reaper **救不了它**:reaper 的 fc_alive 是进程存活检查,而一个 Paused 的
    # Firecracker 进程照样活着,所以它会得出同样的错误结论。
    # 因此必须由本脚本把"我把 VM 留在 Paused 了"这个事实回传给控制面。
    #
    # 通道是现成的:backup Lambda 已经在读本脚本的 stdout(它从里面抽 S3 key),
    # 所以只要打一个稳定哨兵即可,不需要新增接口。用哨兵而不是靠措辞匹配 ——
    # 措辞会改,哨兵不会(与本仓 stop-vm.sh 的 OC_STOP_ORPHAN_NO_VMDIR 同款手法)。
    log "OC_BACKUP_VM_LEFT_PAUSED FATAL: ${TENANT_ID} could NOT be resumed after ${_OC_RESUME_TRIES} attempts — the tenant VM is left PAUSED. Manual intervention required: curl --unix-socket ${SOCK} -X PATCH http://localhost/vm -d '{\"state\":\"Resumed\"}'"
  fi
}
trap cleanup EXIT

# 为什么现在必须加:本脚本会 Pause VM → 压缩 → Resume。此前调用方只有"人为触发"
# (手动备份 / 删前 / suspend),同租户并发窗口极小;R7 引入了每 tick 都可能触发的
# 本机定时循环,窗口变成常态。两个实例交错时:一个刚 Pause、另一个正好 Resume,
# 后者的压缩就备到了【运行中的盘】,产出的备份内容不一致 —— 而它会成为更新的恢复点。
# 更糟的是 GZ_FILE 路径只按 tenant_id 拼(不含运行 id),两个实例互相覆盖同一个临时文件。
#
# 用与 launch/stop/delete/migrate 同一把 inode advisory 锁(/run/lock/oc-launch-<tid>),
# 而不是新开一把:备份要与那些动作互斥,不只是与另一次备份互斥 —— 停机中的盘、
# 正在被 rm -rf 的盘都不该被备。
#
# 调用方可能【已经持有】这把锁(delete-vm.sh:70 全程持锁并把 fd 号导出到
# OC_LIFECYCLE_LOCK_FD 供子脚本复用)。此时必须复用它的 fd 而不是重新 open ——
# flock 绑在"打开文件描述"上,新 open 出来的描述不受继承锁保护,自己会阻塞到超时,
# 把删前备份变成必然失败。这与 stop-vm.sh 的处理方式一致。
mkdir -p /run/lock 2>/dev/null || true
if [ -n "${OC_LIFECYCLE_LOCK_FD:-}" ]; then
  log "reusing inherited lifecycle lock (fd=${OC_LIFECYCLE_LOCK_FD})"
else
  exec 9>"/run/lock/oc-launch-${TENANT_ID}.lock"
  # -w 30:备份是可重投的(定时循环下轮再来、删前备份由控制面重投),短暂让位给正在
  # 跑的 launch/restore 是对的。取 30s 是因为一次正常 launch 的持锁时长远小于它,而
  # 控制面给本脚本的预算是 300s。超时即非零退出,不硬闯 —— 硬闯就是备运行中的盘。
  if ! flock -w 30 9; then
    log "ERROR: per-tenant lifecycle lock busy >30s (concurrent launch/stop/delete/backup) — refusing to back up a possibly-changing disk"
    exit 1
  fi
fi

if [ ! -f "$DATA_FILE" ]; then
  # delete 重投要区分两种备份失败:
  #   · 盘还在但备份失败(权限/限流/压缩坏)→ 必须 fail-closed 拒删,数据还救得回;
  #   · 盘【已经不在】(上一次尝试已跑完 rm -rf)→ 重跑备份必然失败,若也 fail-closed
  # 靠 grep "not found" 判太脆(路径/locale/其它步骤也可能出现该词),故用固定哨兵。
  log "ERROR: OC_BACKUP_SOURCE_ABSENT ${DATA_FILE} not found"
  exit 1
fi

# oc_flush_guest <vm_dir> —— 让 guest 把 page cache 落到 data.ext4,必须在 pause 之前跑。
#
# 为什么:Firecracker `Paused` 只冻 vCPU,**不驱动 guest 落盘**。客户刚写、字节还在
# guest page cache 里时,data.ext4 里根本没有它们,tar 出来的备份就是缺的。真机实测
# (usw2,同一镜像文件):guest 写文件不 sync → host 侧 `grep -c <marker> data.ext4` = 0;
# guest 执行 sync 后 = 1。客户可见后果:suspend→restore 后文件还在但变成 0 字节
# 备份路径这条一直没修。
#
# 顺序不可换:pause 之后 guest 再也执行不了任何指令,那时 sync 已经没有意义。
#
# 失败语义按【VM 是否还活着】分两支(codex 独立评审 #1,no-data-loss 核心):
#   · VM 活着(fc.sock 是 socket + firecracker 进程在)但 sync 没成功(不可达/缺 key/无 ip)
#     → 脏页还在 guest 里、数据救得回 → **fail-closed exit 1**。上游 suspend/delete 拿到
#     失败会回滚、不删盘,运维重试即可救数据。绝不能降级成 crash-inconsistent 备份后继续
#     删盘——那会把【本可挽救】的客户数据永久丢掉。
#   · VM 已停/不存在(无 socket 或无进程)→ 未落盘字节本来就随 VM 一起没了,再拦着不备份
#     crash-consistent 继续,return 0。
oc_flush_guest() {
  __fg_dir="$1"
  __fg_sock="${__fg_dir}/fc.sock"
  # VM 存活判据:socket 存在 + api-sock <sock> 的 firecracker 进程在(与 host-agent 同款)。
  # 结果落全局 __OC_VM_ALIVE,pause 段复用同一判据(见变量区注释)。
  __OC_VM_ALIVE=0
  if [ -S "$__fg_sock" ] && pgrep -f "api-sock ${__fg_sock}" >/dev/null 2>&1; then
    __OC_VM_ALIVE=1
  fi
  __fg_ip="$(python3 -c "import json;print(json.load(open('${__fg_dir}/vm.json')).get('guest_ip',''))" 2>/dev/null || true)"
  if [ -n "${__fg_ip}" ] && [ -f /etc/openclaw/host_vm_key ] \
      && timeout 20 ssh -i /etc/openclaw/host_vm_key -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR \
        -o BatchMode=yes "agent@${__fg_ip}" 'sync' >/dev/null 2>&1; then
    log "guest filesystem synced before pause (${__fg_ip})"
    return 0
  fi
  # flush 没成功:VM 活着就 fail-closed(数据可救,不许降级删盘),VM 已停才降级。
  if [ "$__OC_VM_ALIVE" = "1" ]; then
    log "ERROR: OC_BACKUP_GUEST_FLUSH_FAILED VM alive but guest sync failed (${__fg_ip:-no-ip}) — refusing crash-inconsistent backup to avoid data loss"
    exit 1
  fi
  log "WARN: OC_BACKUP_GUEST_FLUSH_SKIPPED VM not running — backup is crash-consistent only"
  return 0
}

# Flush guest → Pause VM → compress → Resume
oc_flush_guest "$VM_DIR"
# 到这一步:要么 VM 已停(oc_flush_guest 降级放行,无 socket → 跳过 pause,tar 静态盘);
# 要么 VM 活且已 sync。VM 活时 pause 必须成功——pause 失败却继续 tar 运行中的盘 = 归档
# 故:pause 失败 fail-closed exit 1,让 suspend/delete 回滚不删盘。
# 判据用 __OC_VM_ALIVE(oc_flush_guest 已算,socket+进程),不用裸 `[ -S "$SOCK" ]`:
# 死 VM 残留的 fc.sock 不该触发 pause(会永久失败卡死 delete)——那种情况直接 tar 静态盘。
# R7(本批次)把这两道 fail-closed 从「保守」变成「必需」:此前备份靠人为触发、有人看着;
# R7 让 host 上的定时循环成为唯一执行者,无人值守 —— 吞掉失败就是持续发布坏恢复点,
# 而 last_backup_at 照样推进。
if [ "${__OC_VM_ALIVE}" = "1" ]; then
  if curl -sf --unix-socket "$SOCK" -X PATCH http://localhost/vm \
      -H 'Content-Type: application/json' -d '{"state":"Paused"}' >/dev/null 2>&1; then
    __OC_PAUSED=1
    log "VM paused"
  else
    log "ERROR: OC_BACKUP_PAUSE_FAILED could not pause live VM — refusing to archive a changing disk to avoid data loss"
    exit 1
  fi

fi

T0=$SECONDS
# fail-loud:压缩失败必须非零退出。脚本用 set -uo pipefail 但无 set -e,单命令失败
# 不会终止;删前备份(delete_tenant pre_delete)靠本脚本 exit code 判成败,任何一步
# 静默失败都会让上游误判备份成功继而 rm -rf 数据盘 → 不可逆数据丢失(CRITICAL)。
#
# `tar -S` 而非裸 pigz:data.ext4 是 8G 声明的稀疏盘,真实数据仅 ~77M(实测 275 个
# 盘稀疏率 99.66%)。裸 pigz 读它时内核把洞展开成零,于是"压缩 7.9G 零 → 解压 7.9G
# 零 → cp --sparse 再扔掉"——恢复端 pigz 串行 inflate 是算法级串行(实测 -p 96 与
# 默认同为 16.7s、%CPU 仅 135%),这 16.5s 全是无用功。tar -S 逐块 memcmp 判零,零块
# 只记进 GNU sparse 头部的段表,归档里只放真实数据。实测同一盘:
#   裸 pigz    备份 2.22s / 产物 14.07MB / 【恢复 16.70s】+ 稀疏化 0.92s
#   tar -S     备份 0.08s / 产物  4.78MB / 【恢复  0.14s】  md5 与源一致
# VM 冻结窗口(pause 期间)同时从 2.22s 降到 0.08s。
# 归档内只放 basename,恢复端 `tar -xSf - -C ${VM_DIR}` 即落回 data.ext4。
tar -cSf - -C "$(dirname "$DATA_FILE")" "$(basename "$DATA_FILE")" | pigz > "$GZ_FILE" \
  || { log "ERROR: tar -S | pigz compress failed"; exit 1; }
log "compressed ($((SECONDS-T0))s, sparse-aware tar)"

# 只在本脚本 pause 过时才 resume;成功后清 __OC_PAUSED,避免 EXIT trap 再 resume 一次
# 也避免覆盖上传期间其它操作可能设的状态)。
#
# 用尽重试仍恢复不了 → **非零退出,不上传**(codex 第十四轮)。理由:
#   · 客户 VM 还停着,而继续走下去会 exit 0 → 上游推进 last_backup_at → 系统认为一切
#     正常,而客户的 agent 停了。丢一次备份下轮会重来,Paused 不会自己好;
#   · 不上传也是有意的:这份归档本身是好的,但"备份成功"这个信号会掩盖 VM 还停着的事实。
#     宁可让这一轮整体失败、可见、可重试。
if ! oc_resume_vm; then
  log "ERROR: refusing to report success while ${TENANT_ID} is still PAUSED"
  exit 1
fi

# Upload (VM already running)
SIZE_MB=$(( $(stat -c%s "$GZ_FILE") / 1048576 ))
log "uploading ${SIZE_MB}MB..."

if [ -n "$CMK_KEY_ID" ]; then
  # ---- Client-side envelope encryption (insider-threat defense-in-depth) ----
  # 1) KMS 取一次性数据密钥:Plaintext 用来本地加密(用完即弃),CiphertextBlob 随密文存。
  DK_JSON=$(aws kms generate-data-key --key-id "$CMK_KEY_ID" --key-spec AES_256 \
    --region "$REGION" --output json) || { log "ERROR: KMS GenerateDataKey failed"; exit 1; }
  DK_PLAIN=$(echo "$DK_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['Plaintext'])")
  echo "$DK_JSON" | python3 -c "import sys,json;sys.stdout.buffer.write(__import__('base64').b64decode(json.load(sys.stdin)['CiphertextBlob']))" > "${GZ_FILE}.key"
  # 2) openssl AES-256-CBC 用数据密钥(hex)本地加密 .gz → .enc;明文密钥只在内存/管道。
  DK_HEX=$(echo "$DK_PLAIN" | base64 -d | xxd -p -c 256 | tr -d '\n')
  openssl enc -aes-256-cbc -pbkdf2 -in "$GZ_FILE" -out "${GZ_FILE}.enc" -K "$DK_HEX" -iv 00000000000000000000000000000000 \
    || { log "ERROR: openssl encrypt failed"; unset DK_PLAIN DK_HEX; exit 1; }
  unset DK_PLAIN DK_HEX
  # 3) 上传密文 + 加密态数据密钥(桶侧还有 SSE-KMS + Object Lock 兜底)。
  # fail-loud:任一 s3 cp 失败(权限/限流/Object Lock 拒写/桶问题)必须 exit 1。否则
  # 脚本(无 set -e)继续跑到末尾 echo 恒成功 → SSM Success → 上游误判备份成功 →
  # rm -rf 数据盘而 S3 无对象 → 不可逆数据丢失(CRITICAL,reviewer 实测抓到的另一半)。
  # 顺序有意:**.key 先传,.enc 最后传**(codex 独立复审第七轮)。
  # 原来是 .enc 在前 —— 若 .key 那步失败(限流/权限/Object Lock 拒写),S3 上就留下一个
  # 【解不开的】.enc。而选备份的三处都只看 `.gz` / `.gz.enc` 结尾 + max(LastModified)
  # (tenant_service._resolve_backup、backup/handler.py:122),于是它会被选中,把更早的
  # 那个【可用】恢复点盖住 —— 恢复时才发现解不开,那时已经无路可退。
  # 反过来先传 .key:失败则留一个孤儿 .key,而没人按 .key 选备份,无害。
  # 所以 .enc 成为"这份备份已完整发布"的完成标记 —— 它存在 ⟹ .key 一定已经在。
  # R7 把备份变成每台 host 无人值守地跑,这条部分发布路径会在整个机队上反复出现,
  # 不再是"偶发一次人工备份"。
  aws s3 cp "${GZ_FILE}.key" "s3://${BUCKET}/${S3_KEY}.key" --region "$REGION" --quiet \
    || { log "ERROR: s3 upload (.key) failed"; rm -f "$GZ_FILE" "${GZ_FILE}.enc" "${GZ_FILE}.key"; exit 1; }
  aws s3 cp "${GZ_FILE}.enc" "s3://${BUCKET}/${S3_KEY}.enc" --region "$REGION" --quiet \
    || { log "ERROR: s3 upload (.enc) failed"; rm -f "$GZ_FILE" "${GZ_FILE}.enc" "${GZ_FILE}.key"; exit 1; }
  rm -f "$GZ_FILE" "${GZ_FILE}.enc" "${GZ_FILE}.key"
  log "uploaded ENCRYPTED s3://${BUCKET}/${S3_KEY}.enc (+.key, envelope+SSE-KMS+ObjectLock)"
  echo "${S3_KEY}.enc"
else
  # No CMK configured: rely on bucket-side SSE-KMS (still encrypted at rest).
  # fail-loud:上传失败必须 exit 1(同上,防伪报成功导致删盘数据丢失)。
  aws s3 cp "$GZ_FILE" "s3://${BUCKET}/${S3_KEY}" --region "$REGION" --quiet \
    || { log "ERROR: s3 upload failed"; rm -f "$GZ_FILE"; exit 1; }
  rm -f "$GZ_FILE"
  log "uploaded s3://${BUCKET}/${S3_KEY} (bucket SSE only — no CMK passed)"
  echo "${S3_KEY}"
fi
