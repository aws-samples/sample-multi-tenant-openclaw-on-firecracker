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
PREFIX="${3:-backups}"
CMK_KEY_ID="${4:-${BACKUP_CMK_KEY_ID:-}}"
TOKEN=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)

VM_DIR="/data/firecracker-vms/${TENANT_ID}"
DATA_FILE="${VM_DIR}/data.ext4"
SOCK="${VM_DIR}/fc.sock"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
GZ_FILE="/data/tmp-backup-${TENANT_ID}.gz"
S3_KEY="${PREFIX}/${TENANT_ID}/${TIMESTAMP}.gz"

log() { echo "[oc:backup] $(date +%H:%M:%S) $*"; }

# #545(codex 评审 #1)—— 只有【本脚本真的 pause 过】才在 cleanup 里 resume。新增的
# oc_flush_guest 会在 pause 之前 fail-closed exit,那时 VM 从没被本脚本 pause,无脑发
# Resumed 会把一个本该 running(甚至本该 paused)的租户状态弄乱。用标志把 resume 收敛到
# "我 pause 的我才 resume"。
__OC_PAUSED=0
# 该 fail-closed 还是降级;pause 段也读它,而不是裸 `[ -S "$SOCK" ]`——否则死 VM 残留的
# fc.sock 会让 pause 对一个没进程的 socket 永久失败、被 fail-closed 卡住 delete 重试
__OC_VM_ALIVE=0

cleanup() {
  rm -f "$GZ_FILE"
  if [ "${__OC_PAUSED}" = "1" ] && [ -S "$SOCK" ]; then
    curl -sf --unix-socket "$SOCK" -X PATCH http://localhost/vm \
      -H 'Content-Type: application/json' -d '{"state":"Resumed"}' >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

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
# 也避免覆盖上传期间其它操作可能设的状态)。resume 失败保留标志,交给 cleanup 兜底重试。
if [ "${__OC_PAUSED}" = "1" ] && [ -S "$SOCK" ]; then
  if curl -sf --unix-socket "$SOCK" -X PATCH http://localhost/vm \
      -H 'Content-Type: application/json' -d '{"state":"Resumed"}' >/dev/null 2>&1; then
    __OC_PAUSED=0
    log "VM resumed"
  else
    log "WARN: resume failed, leaving __OC_PAUSED set for cleanup to retry"
  fi
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
  aws s3 cp "${GZ_FILE}.enc" "s3://${BUCKET}/${S3_KEY}.enc" --region "$REGION" --quiet \
    || { log "ERROR: s3 upload (.enc) failed"; rm -f "$GZ_FILE" "${GZ_FILE}.enc" "${GZ_FILE}.key"; exit 1; }
  aws s3 cp "${GZ_FILE}.key" "s3://${BUCKET}/${S3_KEY}.key" --region "$REGION" --quiet \
    || { log "ERROR: s3 upload (.key) failed"; rm -f "$GZ_FILE" "${GZ_FILE}.enc" "${GZ_FILE}.key"; exit 1; }
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
