#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -euo pipefail
# #256 — launch-vm.sh 是唯一起 VM 脚本。fan_out_main 内联了原 launch-all-vms.sh 的批量
# fan-out 逻辑(SQS dispatch:一条 SSM 命令叫醒本 host → 读 manifest/DDB assignment →
# 逐行 re-invoke `bash "$0" <单租户 13 位参数>` 起 VM)。函数后的 dispatcher case 据 $1
# 是否为 --manifest(paramstore)/--from-ddb(DDB)决定走向:批量 → fan_out_main;否则
# (单租户位置参)→ 落下面的单 VM 主体。每个 re-invoke 的子进程都是独立的单 VM 主体
# (进程隔离:一个租户 exit 不拆整批;各自抢自己租户的 flock,kubelet per-pod worker 同款)。
#
# 顶层是 `set -euo pipefail`(有 -e),原 launch-all 是 `set -uo pipefail`(无 -e):它靠
# `wait` 后子进程失败不中断、继续聚合 v2 JSON 给 Poller 清算。故 fan_out_main 第一行
# `set +e` 关掉 -e 恢复此行为(-u / -o pipefail 由顶层保留)。log 走 stderr([oc:launch-all]
# tag),stdout 只留给 v2 JSON(Poller 解析),绝不混。
fan_out_main() {
  set +e

  # dispatcher 透传全部参数;$1 是 --manifest(paramstore 回退)或 --from-ddb(DDB 载体)。
  local mode="$1"; shift
  FROM_DDB=0
  [ "${mode}" = "--from-ddb" ] && FROM_DDB=1

  COMMAND_ID="${1:?Usage: launch-vm.sh --manifest|--from-ddb <command_id> <count> <max_parallel>}"
  PART_COUNT="${2:?Usage: launch-vm.sh --manifest|--from-ddb <command_id> <count> <max_parallel>}"
  MAX_PARALLEL="${3:-96}"

  # FAN_VM_DIR 必须 local:与单 VM 主体的全局 VM_DIR=/data/firecracker-vms/<tid> 同名,
  # local 化杜绝 fan-out 段污染主体的 VM_DIR(dynamic scoping 让 _launch_one 也读得到)。
  local FAN_VM_DIR="/data/firecracker-vms"
  PARAM_PREFIX="${DISPATCH_PARAM_PREFIX:-/openclaw/dispatch}"
  # In ddb mode $4 is the assignments table name; in paramstore mode $4 (if given)
  # is the param prefix — keep the old positional contract intact.
  ASSIGN_TABLE="${4:-${ASSIGNMENTS_TABLE:-openclaw-assignments}}"
  if [ "${FROM_DDB}" -eq 0 ] && [ -n "${4:-}" ]; then
    PARAM_PREFIX="$4"
  fi

  _fo_log() { echo "[oc:launch-all] $(date +%H:%M:%S) $*" >&2; }
  _fo_die() { _fo_log "FATAL: $*"; rm -rf "${RESULT_DIR:-}" 2>/dev/null || true; exit 1; }

  # ── REGION + INSTANCE_ID from /etc/platform.env (init-host.sh wrote it) ──
  # init-host.sh writes INSTANCE_ID + OC_REGION at first boot; source not re-hit IMDS.
  [ -f /etc/platform.env ] && . /etc/platform.env
  REGION="${OC_REGION:-${AWS_REGION:-}}"
  [ -n "${REGION}" ] || _fo_die "OC_REGION empty in /etc/platform.env — init-host.sh didn't run?"
  [ -n "${INSTANCE_ID:-}" ] || _fo_die "INSTANCE_ID empty in /etc/platform.env — init-host.sh didn't run?"

  _fo_log "start command_id=${COMMAND_ID} parts=${PART_COUNT} parallel=${MAX_PARALLEL} host=${INSTANCE_ID}"

  # ── Fetch one manifest part with exponential backoff (jitter via $RANDOM) ──
  # ParamStore is eventually consistent across regions but strong within one
  # region; the retry loop guards against ParameterNotFound during the tiny
  # window where SSM SendCommand outraces the last PutParameter. We DO NOT
  # silently return empty on total failure — that would launch 0 VMs and
  # report success (fail-loud rule).
  _get_part() {
    local n="$1" name attempt=0 out rc
    name="${PARAM_PREFIX}/manifests/${COMMAND_ID}/${INSTANCE_ID}/part-${n}"
    while [ "${attempt}" -lt 3 ]; do
      out=$(aws ssm get-parameter --region "${REGION}" --name "${name}" \
        --with-decryption --query 'Parameter.Value' --output text 2>/dev/null)
      rc=$?
      if [ "${rc}" -eq 0 ] && [ -n "${out}" ] && [ "${out}" != "None" ]; then
        printf '%s' "${out}"
        return 0
      fi
      attempt=$((attempt + 1))
      sleep "$(awk "BEGIN{print (2^${attempt}) + (${RANDOM}%1000)/1000}")"
    done
    return 1
  }

  # ── DDB source: Query this host's pending assignments → same JSON-lines ──
  # Filter to status=pending + action=create (idempotent redelivery: done rows are
  # skipped at the source, vm.json check still guards the race). command_id is NOT
  # part of the key — a host drains ALL its pending rows, which self-heals any
  # earlier batch whose wake-up SSM command was lost (at-least-once safety net).
  _get_ddb_lines() {
    aws dynamodb query --region "${REGION}" \
      --table-name "${ASSIGN_TABLE}" \
      --key-condition-expression "instance_id = :i" \
      --filter-expression "#s = :p AND #a = :c" \
      --expression-attribute-names '{"#s":"status","#a":"action"}' \
      --expression-attribute-values \
        "{\":i\":{\"S\":\"${INSTANCE_ID}\"},\":p\":{\"S\":\"pending\"},\":c\":{\"S\":\"create\"}}" \
      --output json 2>/dev/null \
    | jq -c '.Items[] | {t:.tenant_id.S, n:(.vm_num.N|tonumber),
                          c:(.vcpu.N|tonumber), m:(.mem_mb.N|tonumber),
                          e:(if .chat_ep.BOOL then 1 else 0 end)}
                         + (if .gateway_token_ct.S then {g:.gateway_token_ct.S} else {} end)
                         + (if .device_paired_b64.S then {d:.device_paired_b64.S} else {} end)
                         + (if .restore_backup_key.S then {r:.restore_backup_key.S} else {} end)'
  }

  # ── Mark one assignment done|failed (conditional; Poller/agent reconcile) ──
  # ConditionExpression status=pending: two executors racing the same row (SSM
  # wake-up + a future agent poll both saw it) — only the first transition wins,
  # the loser's write is rejected instead of silently overwriting (same discipline
  # as tenants creating→running). Adds <st>_ts stamp + fail_reason for postmortem
  # without SSHing the host. best-effort (|| true): a mark failure never fails the
  # launch (the VM is already up; Poller reconciles from stdout v2 report anyway).
  _mark_assignment() {
    local tid="$1" st="$2" reason="${3:-}"
    local expr="SET #s = :v, ${st}_ts = :ts"
    local now_iso vals
    now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    vals="{\":v\":{\"S\":\"${st}\"},\":ts\":{\"S\":\"${now_iso}\"},\":pend\":{\"S\":\"pending\"}"
    if [ -n "${reason}" ]; then
      expr="${expr}, fail_reason = :r"
      vals="${vals},\":r\":{\"S\":\"${reason:0:1024}\"}"
    fi
    vals="${vals}}"
    aws dynamodb update-item --region "${REGION}" \
      --table-name "${ASSIGN_TABLE}" \
      --key "{\"instance_id\":{\"S\":\"${INSTANCE_ID}\"},\"tenant_id\":{\"S\":\"${tid}\"}}" \
      --update-expression "${expr}" \
      --condition-expression "#s = :pend" \
      --expression-attribute-names '{"#s":"status"}' \
      --expression-attribute-values "${vals}" \
      >/dev/null 2>&1 || true
  }

  # ── Launch a single VM (called in background as a semaphore job) ───────
  _launch_one() {
    local tid="$1" vm_num="$2" vcpu="$3" mem_mb="$4" chat_ep="$5"
    local gw_token_ct="${6:-}" device_paired="${7:-}" restore_key="${8:-}"
    local vm_path="${FAN_VM_DIR}/${tid}"
    # Idempotent check: if vm.json already exists we treat this as an at-least-once
    # duplicate delivery. host-agent's poll loop will still recover it if FC died.
    if [ -f "${vm_path}/vm.json" ]; then
      return 42  # sentinel: skipped
    fi
    # 13 positional args mirror the launch-vm.sh signature: config_template=$5,
    # restore_backup_key=$6 (#199 — 缺则 launch 建空白盘=数据丢失,故必须透传),
    # chat_ep=$10, position 11 (cognito, retired #187 P5) empty,
    # gateway_token_ct=$12 (#187 P1), device_paired_b64=$13 (#188 wss 免 approve).
    # config_template($5) 走 DDB tenants 记录(host-agent/launch-vm 自取),此处
    # 空;restore_key($6) 从 assignment/manifest 透传。Empty 7-9 keep defaults.
    # Secrets like vkey/channel_secret are still pulled from DDB by launch-vm.
    # #256 — re-invoke 本脚本自己(bash "$0"):单租户位置参不含 --manifest/--from-ddb,
    # 故落 dispatcher 后的单 VM 主体,不会递归回 fan_out_main。
    # #266 — systemd-cat 包裹让每租户 launch 诊断进 journald(tag claw-launch)→
    # Fluent Bit host.platform → AOS claw-logs-host,不再被 /dev/null 吞掉。fan-out
    # 批量创建卡住时,console Logs viewer 才查得到某租户为何没起来。
    systemd-cat -t claw-launch \
      bash "$0" "${tid}" "${vm_num}" "${vcpu}" "${mem_mb}" \
      "" "${restore_key}" "" "" "" "${chat_ep}" "" "${gw_token_ct}" "${device_paired}"
    return $?
  }

  # ── Bounded-parallel fan-out (same job-count semaphore as start-all-vms.sh) ──
  _wait_for_slot() {
    while [ "$(jobs -rp | wc -l)" -ge "${MAX_PARALLEL}" ]; do
      wait -n 2>/dev/null || sleep 0.2
    done
  }

  # Aggregate results via a tempfile per child (bash can't return values across
  # background jobs, and a single log file would race). Each _launch_one writes
  # one word: "launched" | "skipped" | "failed" | "inprogress" into a result file.
  RESULT_DIR="$(mktemp -d /tmp/oc-launch-all.XXXXXX)"

  _dispatch_line() {
    # Parse ONE JSON-line: {"t":"t-xxx","n":42,"c":2,"m":2048,"e":0[,"g":"<ct>","d":"<b64>"]}
    # We use jq (present on host, same as start-all-vms.sh:59-61) — a stdlib
    # sed regex here would break on any future field addition.
    # g = gateway_token_ct (base64 KMS 密文, #187 P1); d = device_paired_b64
    # (base64 paired.json, #188). 缺省空串 → launch-vm fail-open。
    local line="$1" rfile="$2"
    local tid vm_num vcpu mem_mb chat_ep gw_token_ct device_paired restore_key
    tid=$(jq -r '.t // empty' <<<"${line}" 2>/dev/null)
    vm_num=$(jq -r '.n // empty' <<<"${line}" 2>/dev/null)
    vcpu=$(jq -r '.c // 2' <<<"${line}" 2>/dev/null)
    mem_mb=$(jq -r '.m // 2048' <<<"${line}" 2>/dev/null)
    chat_ep=$(jq -r '.e // 0' <<<"${line}" 2>/dev/null)
    gw_token_ct=$(jq -r '.g // empty' <<<"${line}" 2>/dev/null)
    device_paired=$(jq -r '.d // empty' <<<"${line}" 2>/dev/null)
    # #199 — restore 意图(r=S3 backup key);空 → 普通建盘,非空 → launch 恢复该备份
    restore_key=$(jq -r '.r // empty' <<<"${line}" 2>/dev/null)
    if [ -z "${tid}" ] || [ -z "${vm_num}" ]; then
      echo "failed" > "${rfile}"
      _fo_log "parse-error line=${line}"
      return
    fi
    _launch_one "${tid}" "${vm_num}" "${vcpu}" "${mem_mb}" "${chat_ep}" \
      "${gw_token_ct}" "${device_paired}" "${restore_key}"
    rc=$?
    printf '%s' "${tid}" > "${rfile}.tid"  # v2 per-tenant report (see stdout JSON)
    if [ "${rc}" -eq 0 ]; then
      echo "launched" > "${rfile}"
      [ "${FROM_DDB}" -eq 1 ] && _mark_assignment "${tid}" "done"
    elif [ "${rc}" -eq 42 ]; then
      echo "skipped" > "${rfile}"
      [ "${FROM_DDB}" -eq 1 ] && _mark_assignment "${tid}" "done"
    elif [ "${rc}" -eq 75 ]; then
      # #256 — launch-vm.sh 抢 per-tenant flock 失败(另一进程正在起同租户)的 skip 哨兵。
      # 关键:绝不 _mark_assignment done。持锁 winner 可能死在写 vm.json 之前(如 #199
      # DDB get-item fail-closed exit 1),若这里把 assignment 标 done,它就从 pending 过滤
      # 掉永不重投 + 无 vm.json 让 host-agent 本地恢复也没锚点 = 永久孤儿(no-data-loss)。
      # 写 inprogress → 不进 v2 JSON 的 launched/skipped/failed 三清单(_result_list 只精确
      # 匹配那三个)→ Poller 看不到 → assignment 保持 pending → 下一轮 dispatch 重新 pick。
      echo "inprogress" > "${rfile}"
      _fo_log "launch skip(flock held) tenant=${tid} rc=75 — 保持 pending 待重投"
    else
      echo "failed" > "${rfile}"
      # #315(codex 8.4 HIGH)—— ddb 模式:普通非零退出【不】标 assignment failed,保持 pending。
      # 与 host-agent _dispatch_tick 点5 同源:assignment 一旦 failed,host-agent
      # _query_pending_assignments 只查 pending → 再也捞不到 → 永久卡 creating、不经预算、不进 DLQ
      # (SSM --from-ddb 执行器的"首败即终态",与 Python reconciler 是同一 bug 的 shell 副本)。
      # 保持 pending 让 host-agent 下轮重捞重试;retry budget 耗尽转终态由控制面单一处判(host-agent
      # _bump_dispatch_retries + 点5),执行体不知道 budget、不做终态判定。rfile 仍写 "failed" 供
      # push 模式 v2 报告给 poller(push 保留原语义,不受此改影响)。
      _fo_log "launch fail tenant=${tid} rc=${rc} — ddb 保持 pending 待重投(不标终态)"
    fi
  }

  job_seq=0
  if [ "${FROM_DDB}" -eq 1 ]; then
    # ddb 载体:Query 本 host 全部 pending 行(数据在表,SSM 只叫醒)。零行可能是
    # DDB 最终一致读窗口(BatchWrite 落 leader、replica 未追上,同区 lag 通常
    # <100ms 但叫醒命令跑得更快),退避重试 2 次再 die;fail-loud 不静默 launch 0 个。
    attempt=0
    body=""
    while [ "${attempt}" -lt 3 ]; do
      body=$(_get_ddb_lines)
      [ -n "${body}" ] && break
      attempt=$((attempt + 1))
      sleep "$(awk "BEGIN{print ${attempt} + (${RANDOM}%1000)/1000}")"
    done
    [ -n "${body}" ] || _fo_die "ddb source: zero pending assignments for ${INSTANCE_ID} after 3 tries (expected ~${PART_COUNT})"
    while IFS= read -r line; do
      [ -z "${line}" ] && continue
      _wait_for_slot
      rfile="${RESULT_DIR}/r-${job_seq}"
      _dispatch_line "${line}" "${rfile}" &
      job_seq=$((job_seq + 1))
    done <<<"${body}"
  else
    part_idx=0
    while [ "${part_idx}" -lt "${PART_COUNT}" ]; do
      body=$(_get_part "${part_idx}") || _fo_die "part ${part_idx} unreadable after 3 retries"
      while IFS= read -r line; do
        [ -z "${line}" ] && continue
        _wait_for_slot
        rfile="${RESULT_DIR}/r-${job_seq}"
        _dispatch_line "${line}" "${rfile}" &
        job_seq=$((job_seq + 1))
      done <<<"${body}"
      part_idx=$((part_idx + 1))
    done
  fi

  # Wait for the whole fan-out so the SSM exit status reflects reality.
  wait

  launched=0
  skipped=0
  failed=0
  if [ -d "${RESULT_DIR}" ]; then
    for f in "${RESULT_DIR}"/r-*; do
      case "${f}" in *.tid) continue ;; esac  # 别把 .tid 附属文件数成 failed(真机第 6 bug)
      [ -f "${f}" ] || continue
      v=$(cat "${f}" 2>/dev/null || echo "failed")
      case "${v}" in
        launched)   launched=$((launched + 1)) ;;
        skipped)    skipped=$((skipped + 1)) ;;
        inprogress) ;;  # #256 flock skip:不计 launched/skipped/failed,不进 v2 清单,不触发批回滚(保持 pending 待重投)
        *)          failed=$((failed + 1)) ;;
      esac
    done
  fi

  # Single-line JSON on stdout — Poller GetCommandInvocation parses this.
  # v2: per-tenant result lists so the Poller can mark tenants running WITHOUT
  # relying on tenants.dispatch_claim — the claim is deliberately volatile
  # (released on retry paths to break the claim deadlock), so a visibility
  # retry racing a still-running SSM command used to orphan the whole batch in
  # `creating` forever (e2ev2-probe, 2026-07-05). The executor reporting WHO it
  # launched is the only association that can't be cleared underneath us.
  # Budget: 380 ids × ~20B ≈ 8KB < GetCommandInvocation's 24,000-char stdout cap.
  # (_fo_log writes to stderr so it doesn't pollute the JSON contract.)
  _result_list() {  # $1 = launched|skipped|failed → JSON array of tenant ids
    local want="$1" out="" f v tid
    for f in "${RESULT_DIR}"/r-*; do
      case "${f}" in *.tid) continue ;; esac  # .tid 是附属文件,不是结果
      [ -f "${f}" ] || continue
      v=$(cat "${f}" 2>/dev/null || echo "failed")
      tid=$(cat "${f}.tid" 2>/dev/null || echo "")
      [ "${v}" = "${want}" ] && [ -n "${tid}" ] && out="${out}\"${tid}\","
    done
    printf '[%s]' "${out%,}"
  }
  printf '{"v":2,"launched":%d,"skipped":%d,"failed":%d,"host":"%s","command_id":"%s","tenants":{"launched":%s,"skipped":%s,"failed":%s}}\n' \
    "${launched}" "${skipped}" "${failed}" "${INSTANCE_ID}" "${COMMAND_ID}" \
    "$(_result_list launched)" "$(_result_list skipped)" "$(_result_list failed)"

  _fo_log "DONE launched=${launched} skipped=${skipped} failed=${failed}"
  # 函数末尾显式清理 RESULT_DIR(不用 EXIT trap:避免与单 VM 主体 line ~ 的 trap ... EXIT 冲突;
  # _fo_die 里也已各自清理)。
  rm -rf "${RESULT_DIR}" 2>/dev/null || true
  # Exit non-zero if any launch failed so the Poller can classify the batch as
  # partial/failed and roll back CAS + dispatch_retries+=1 per the contract.
  if [ "${failed}" -gt 0 ]; then
    return 1
  fi
  return 0
}
case "${1:-}" in
  --manifest|--from-ddb)
    fan_out_main "$@"
    exit $?
    ;;
esac
# 1.3.2: trap any non-zero exit so we know which line failed even when
# stdout gets truncated by SSM's 8KB output limit. NOTE: do NOT pkill
# firecracker — once InstanceStart succeeds, the VM is genuinely up and
# any later cleanup step's failure (e.g. nginx reload race) shouldn't
# tear down a working VM. host-agent's auto-recovery + the Lambda's
# _verify_vm_actually_running probe handle the post-failure resync.
_oc_cleanup_on_err() {
  local rc=$?
  echo "[oc:launch] FAIL line=${BASH_LINENO[0]} rc=${rc} cmd=${BASH_COMMAND}" >&2
  # Only clean up resources allocated BEFORE firecracker started (tap, sock).
  # If FC is running, leave it alone — the VM may be perfectly healthy.
  if [ -n "${SOCK:-}" ] && [ -S "${SOCK}" ]; then
    if pgrep -f "api-sock ${SOCK}" >/dev/null 2>&1; then
      echo "[oc:launch] firecracker is running on ${SOCK}; leaving it alive" >&2
      exit $rc
    fi
  fi
  if [ -n "${TAP:-}" ]; then
    sudo ip link del "${TAP}" 2>/dev/null || true
  fi
  if [ -n "${VM_DIR:-}" ]; then
    sudo rm -f "${VM_DIR}/fc.sock" 2>/dev/null || true
  fi
  # umount 兜底(数据盘/creds 盘):挂载窗口内任何 exit 1(config/env 注入 FATAL、
  # jq/mount 失败等)若不卸载,data.ext4 会一直挂在 /tmp/data-mount-<tid> → 下次
  # 该租户重试时 `mount ${DATA_VOL} ${MOUNT_TMP}` 撞已挂载点 rc=1 → 永久 recovering
  # 卡死(新加坡真机实证:config 注入 FATAL 后漏 umount,C/D/F 全卡死)。FC 未起才走到
  # 这(上面 FC 在跑已 exit),此时卸载挂载点安全。-l 惰性卸载防"target busy"。变量名
  # 与主体一致(MOUNT_TMP=/tmp/data-mount-<tid>, _CREDS_MNT=/tmp/creds-mount-<tid>)。
  for _mp in "${MOUNT_TMP:-}" "${_CREDS_MNT:-}"; do
    if [ -n "${_mp}" ] && mountpoint -q "${_mp}" 2>/dev/null; then
      sudo umount "${_mp}" 2>/dev/null || sudo umount -l "${_mp}" 2>/dev/null || true
    fi
  done
  exit $rc
}
# ERR + EXIT:bash 的 `trap ... ERR` 对**显式 `exit 1`** 不触发(只对 set -e 的隐式命令失败
# 触发,已实测)。而 launch 主体在 mount 窗口(数据盘挂到 /tmp/data-mount-<tid> ~ umount)内
# 有 14+ 处 FATAL `exit 1`(config/env 注入失败、oc_harden_config jq 失败 line629、结尾
# RESULT 非空 line919 等)。仅注册 ERR 时,这些 exit 1 **绕过** _oc_cleanup_on_err → 上面的
# umount 兜底根本没机会跑 → data.ext4 泄漏挂载 → 该租户下次重试撞 `already mounted` rc=1 →
# 永久 recovering 卡死(真机:自定义 config_template 租户首启走 line629 exit 1 稳定复现)。
# 加 EXIT 让 cleanup 对任何退出路径都跑;成功路径在 InstanceStart 后 `trap - ERR EXIT` 清掉
# (见下),故不误拆已起的 VM。cleanup 内 `exit $rc` 在 EXIT 上下文不会递归重入(已实测)。
trap _oc_cleanup_on_err ERR EXIT
TENANT_ID="${1:?Usage: launch-vm.sh <tenant_id> <vm_num> [vcpu] [mem_mb] [config_template] [restore_backup_key] [scoped_skills]}"
VM_NUM="${2:?Usage: launch-vm.sh <tenant_id> <vm_num> [vcpu] [mem_mb] [config_template] [restore_backup_key] [scoped_skills]}"
# #256 — per-tenant 跨进程互斥(kubelet per-pod worker 的跨进程等价物)。
# launch-vm.sh 有多类并发调用者:fan-out(bash 子进程)/host-agent recover(Popen)/
# ssm wake/scaler/health failover(独立 SSM 进程)。Python 的 _recovering/_dispatch_inflight
# 是进程内 set,跨不过 bash 和 SSM 那几类进程。flock 是 inode advisory 锁,跨语言/跨进程
# 有效,内核在持锁 fd 关闭时自动释放(正常退出/SIGKILL/OOM/断电都释放),锁永不泄漏
# (残留是 0 字节锁文件,/run/lock tmpfs 重启清,无死锁)。
# 必须在 SOCK/TAP(下面)定义之前:此时 TAP/SOCK/MOUNT_TMP 全未定义,抢锁失败的 loser
# 退出即使跑 EXIT trap 也是 no-op,绝不会 ip link del 掉 winner 正在用的同名 tap-vm${VM_NUM}。
# 抢不到锁 → exit 75(skip 专用哨兵,绝不 0/42):launch-all 映射成 inprogress、保持
# assignment pending、不计 launched、不 _mark_assignment done(否则持锁 winner 若死在写
# vm.json 之前,assignment 被标 done + 无 vm.json = 永久孤儿,踩 no-data-loss)。
# 先 trap - ERR EXIT 再退,避免良性 skip 触发 _oc_cleanup_on_err 刷一条误导性 FAIL 日志。
mkdir -p /run/lock 2>/dev/null || true
exec 9>"/run/lock/oc-launch-${TENANT_ID}.lock"
flock -n 9 || { trap - ERR EXIT; echo "[oc:launch] ${TENANT_ID} launch already in progress (flock held) — skip" >&2; exit 75; }
# #331/#327 — host 级启动并发闸(跨进程):一个 host 同时冷启的 VM 数不超过 N 个,防批量 FC
# 死亡 recover / 多批 SSM fan-out 各自 fire 几百个 launch-vm 造成二次 CPU/IO 洪峰压垮 host。
# 所有起 VM 的路径(dispatch fan-out / host-agent recover/force-relaunch / ssm wake)都走本脚本,
# 故在此单一咽喉口装一把跨进程信号量:N 把槽锁 fd10-fd(9+N),抢到任一把才继续,抢不到就阻塞等
# (非 skip:本租户该起,只是排队限速)。flock 是 inode advisory 锁,持锁进程死/OOM/SIGKILL 内核
# 自动释放,永不泄漏、无死锁。N=OC_HOST_LAUNCH_SLOTS(默认 30,与 dispatch 波宽同量级)。per-tenant
# 锁(fd9)先抢:同租户重复调只有一个进到这里,不会占多把槽。取消 flock 超时兜底=让洪峰真排队,
# 慢启不压垮(design decision:可以启动得慢一些)。
# platform.env 完整 source 在下方(:476);slot 数在抢锁前就要,先补读一次(幂等、cheap)。
[ -f /etc/platform.env ] && . /etc/platform.env 2>/dev/null || true
_OC_SLOTS="${OC_HOST_LAUNCH_SLOTS:-30}"
case "$_OC_SLOTS" in ''|*[!0-9]*) _OC_SLOTS=30 ;; esac
[ "$_OC_SLOTS" -lt 1 ] && _OC_SLOTS=1
# launcher 自持锁模型(codex review 定案,零 fork):launcher 进程【自己】持某把槽的 flock fd8,
# 不起任何后台/子进程抢锁(旧 guardian 版每等待者每 0.3s fork setsid×N = 进程风暴,codex 抓)。
# 抢法:先 flock -n 非阻塞扫一遍 N 把槽(纯 in-process,零 fork);全满则对轮转的一把做【阻塞】
# flock -w(在内核里睡等,不自旋、不 fork),超时再换一把扫。抢到即 fd8 常驻本进程。
# fd 不泄漏给 firecracker:起 firecracker 时 8>&- 显式关掉,长命 FC 不继承/不占死槽。
# launcher 被 SIGKILL/OOM/正常退出 → fd8 关 → 内核自动释放槽(无 fail-open:持锁的就是干活的本尊,
# 不存在"锁没了但还在冷启"的窗口)。释放=InstanceStart 后 exec 8>&-(重活已完)。
_OC_SLOT_HELD=0
_oc_acquire_launch_slot() {
  # 每 launcher 恰好【1 次 flock exec】:随机选一把固定槽,单次【无超时阻塞】flock——内核睡等到这
  # 把槽空出(不扫 N 把、不自旋、不周期重扫,彻底无 fork 洪峰,codex 反复抓的点)。随机选槽让 N 个
  # 等待者按均匀分布摊到 N 把槽,谁先释放谁的排队者先进,整体稳态吞吐 = N 并发。
  local i
  i=$(( (RANDOM % _OC_SLOTS) + 1 ))
  exec 8>"/run/lock/oc-launch-slot-${i}.lock"
  echo "[oc:launch] ${TENANT_ID} acquiring host launch slot ${i}/${_OC_SLOTS}" >&2
  flock 8
  _OC_SLOT_HELD=1
}
_oc_release_launch_slot() {
  # 关 fd8 → 内核放锁。★槽闸限【冷启动 CPU/IO 重活】(mkfs/cp/解压/boot),到 InstanceStart
  # 成功那刻重活结束、VM 自持运行 → 在此释放;不持到 VM 生命周期(否则封顶常驻数=灾难)。幂等。
  [ "${_OC_SLOT_HELD}" -eq 1 ] || return 0
  exec 8>&- 2>/dev/null || true
  _OC_SLOT_HELD=0
}
_oc_acquire_launch_slot
VCPU="${3:-2}"
MEM_MB="${4:-4096}"
CONFIG_TEMPLATE="${5:-}"
RESTORE_KEY="${6:-}"
# 1.4.0 (#62) — comma-separated allow-list of skill names. Empty / "*"
# preserves the legacy v1.3.x broadcast behavior so old SSM commands
# without this 7th arg keep working unchanged.
SCOPED_SKILLS="${7:-}"
# task #15 — per-tenant LiteLLM vkey (8th arg). API Lambda mints it at
# create_tenant and passes it here; we inject it into openclaw.json's
# litellm.apiKey so this tenant's spend/budget bills to its own key. Empty
# preserves the shared image key (backward compatible with old SSM commands).
LITELLM_VKEY="${8:-}"
# channel_secret (9th arg) — the per-tenant hub HMAC secret, MINTED BY THE API
# Lambda at create_tenant and persisted to the DDB record BEFORE this script
# runs. We inject this exact value into openclaw.json so the in-VM channel signs
# with the same secret the hub verifies against (read from DDB). This kills the
# old startup race where launch-vm.sh `openssl rand`'d its own secret and relied
# on host-agent to SSH-read-back + mirror it to DDB ~15s later — by which time
# the channel had already exhausted its retry budget (token-fail/401) and given
# up ("agent offline" forever). Empty (legacy SSM commands without this arg)
# falls back to self-generating (preserves backward compat, but re-opens race).
INJECTED_CHANNEL_SECRET="${9:-}"
# chat_endpoint_enabled (10th arg) — per-tenant switch for the OpenAI-compatible
# gateway.http.endpoints.chatCompletions endpoint. DEFAULT OFF (empty / "0" /
# "false"): we keep deleting the endpoint (OpenClaw's secure default + this
# fork's policy — see the del() below and CLAUDE.md "chatCompletions 为什么不能
# 全局默认开"). Only when the API Lambda passes "1"/"true" (the tenant record's
# chat_endpoint_enabled flag) do we inject enabled:true for THAT tenant. Mitigations
# stay regardless: per-tenant gateway.auth.token + CloudFront/nginx reverse proxy +
# Bedrock Guardrail + LiteLLM vkey limit. Empty (legacy SSM commands) → off.
CHAT_EP_ENABLED="${10:-}"
# #187 P5 — 11th arg 保留空占位(转型前是 INJECTED_COGNITO_B64 WI-002 端到端
# Cognito 渠道机器用户 base64)。channel/hub 数据面已下线,数据面走两级路由直连
# microVM:18789 gateway。参数位保留以维持 12 位对齐,取值不再使用。
INJECTED_COGNITO_B64="${11:-}"
# #187 P1 (SPEC/11-ENGINE-TRANSFORM D+B): 12th positional arg — base64 KMS
# ciphertext of the pre-minted gateway token (tenant_id EncryptionContext, ClawPool
# CMK). Empty (legacy SSM commands / feature off) → keep the openssl-generated
# in-VM token. Non-empty → we `aws kms decrypt` here on the host (has kms:Decrypt
# on the ClawPool CMK), inject the plaintext as `.gateway.auth.token`, replacing
# the openssl one. This closes the "control plane can't reveal the gateway token"
# gap that hub → gateway direct-connect (11-ENGINE-TRANSFORM) needs. Reveal window
# is enforced control-side (openclaw-tenant-secrets TTL 15min); this side is just
# the injection step.
INJECTED_GATEWAY_TOKEN_CT="${12:-}"
# #188 — 13th positional arg — base64 of the paired.json entry (one Ed25519
# device: deviceId + publicKey + roles + scopes, tokens:{} for 2026.2.26). The
# control plane mints the device at create_tenant, base64-encodes the paired.json
# object, and passes it here. We base64-decode it and write it to the data disk's
# <stateDir>/devices/paired.json so a remote WSS client (JDWS) preloaded with the
# matching device identity connects to the in-VM gateway with NO manual approve.
# Empty (legacy SSM commands / feature off / owner unknown / CMK off) → skip the
# write (byte-identical pre-#188 behavior; no paired.json = default pairing gate).
INJECTED_DEVICE_PAIRED_B64="${13:-}"
# Caller may pass literal "" (quoted) as placeholder when only restore_key is set.
[ "${CONFIG_TEMPLATE}" = '""' ] && CONFIG_TEMPLATE=""
[ "${RESTORE_KEY}" = '""' ] && RESTORE_KEY=""
[ "${SCOPED_SKILLS}" = '""' ] && SCOPED_SKILLS=""
[ "${LITELLM_VKEY}" = '""' ] && LITELLM_VKEY=""
[ "${INJECTED_CHANNEL_SECRET}" = '""' ] && INJECTED_CHANNEL_SECRET=""
[ "${CHAT_EP_ENABLED}" = '""' ] && CHAT_EP_ENABLED=""
[ "${INJECTED_COGNITO_B64}" = '""' ] && INJECTED_COGNITO_B64=""
[ "${INJECTED_GATEWAY_TOKEN_CT}" = '""' ] && INJECTED_GATEWAY_TOKEN_CT=""
[ "${INJECTED_DEVICE_PAIRED_B64}" = '""' ] && INJECTED_DEVICE_PAIRED_B64=""
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
[ -f /etc/platform.env ] && source /etc/platform.env
# #199 fix(数据丢失级):pull 模式 dispatch(host-agent _execute_launch)把位置参数
# 5/6 留空,让 launch-vm 自己从 DDB 读——但历史上只读了 injected_credentials,漏了
# restore_backup_key/config_template。结果 restore-create 走队列时 RESTORE_KEY 为空,
# 下面建盘分支静默用空白模板盘冒充恢复(备份数据丢失)。这里在建盘前补读:仅当位置参数
# 空时才回退 DDB(push/SSM 直发路径已带值,不覆盖)。fail-CLOSED:get-item 调用失败即
# 中止(throttle/IAM/network),绝不在读不到 restore_backup_key 时盲目建空白盘。
if [ -z "${RESTORE_KEY}" ] || [ -z "${CONFIG_TEMPLATE}" ]; then
  if _RC_RAW="$(aws dynamodb get-item \
    --table-name "${TENANTS_TABLE:-openclaw-tenants}" \
    --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
    --projection-expression 'restore_backup_key, config_template' \
    --consistent-read \
    --region "${OC_REGION:-ap-northeast-1}" \
    --output json 2>/dev/null)"; then
    [ -z "${RESTORE_KEY}" ] && RESTORE_KEY="$(printf '%s' "${_RC_RAW}" | jq -r '.Item.restore_backup_key.S // ""' 2>/dev/null || true)"
    [ -z "${CONFIG_TEMPLATE}" ] && CONFIG_TEMPLATE="$(printf '%s' "${_RC_RAW}" | jq -r '.Item.config_template.S // ""' 2>/dev/null || true)"
  else
    echo "[oc:launch] FATAL(#199): DDB get-item for restore_backup_key/config_template failed (throttle/IAM/network) — 拒起 fail-closed(scheduler 会重试),绝不用空白盘冒充恢复" >&2
    exit 1
  fi
fi
# #290 fix(token 漂移级):host-agent _recover_vm / _force_relaunch_vm 只传 4 位置参数,
# 位置 12/13(gateway_token_ct / device_paired_b64)为空。若此时 NEW_DATA=true(首次
# 建盘或数据盘被清),token 注入段会走 openssl rand 回退,产出的 token 跟 DDB 里控制
# 面 mint 的不一致 → JDWS 拿到 DDB 的 A,VM 实际是 B → 连不上。
# 修法同 #199 模式:位置参数空时从 tenant_secrets 表回退读取。fail-CLOSED:读失败即
# 中止(throttle/IAM/network),绝不用随机 token 覆盖已 mint 的 token。
if [ -z "${INJECTED_GATEWAY_TOKEN_CT}" ] || [ -z "${INJECTED_DEVICE_PAIRED_B64}" ]; then
  _SECRETS_TABLE="${TENANT_SECRETS_TABLE:-openclaw-tenant-secrets}"
  if _SEC_RAW="$(aws dynamodb get-item \
    --table-name "${_SECRETS_TABLE}" \
    --key "{\"tenant_id\":{\"S\":\"${TENANT_ID}\"}}" \
    --projection-expression 'gateway_token_ct, device_paired_b64' \
    --consistent-read \
    --region "${OC_REGION:-ap-northeast-1}" \
    --output json 2>/dev/null)"; then
    [ -z "${INJECTED_GATEWAY_TOKEN_CT}" ] && INJECTED_GATEWAY_TOKEN_CT="$(printf '%s' "${_SEC_RAW}" | jq -r '.Item.gateway_token_ct.S // ""' 2>/dev/null || true)"
    [ -z "${INJECTED_DEVICE_PAIRED_B64}" ] && INJECTED_DEVICE_PAIRED_B64="$(printf '%s' "${_SEC_RAW}" | jq -r '.Item.device_paired_b64.S // ""' 2>/dev/null || true)"
    # 不能用 log():它定义在本块之后(GUEST_MAC 段),set -e 下此处调用 rc=127
    # 直接退出——恰在 fallback 读到 token 的成功路径上崩,#290 修复被自己废掉。
    # 与本块 else 分支同款,直接 echo。
    [ -n "${INJECTED_GATEWAY_TOKEN_CT}" ] && echo "[oc:launch] DDB fallback: got gateway_token_ct from ${_SECRETS_TABLE} (#290)"
    [ -n "${INJECTED_DEVICE_PAIRED_B64}" ] && echo "[oc:launch] DDB fallback: got device_paired_b64 from ${_SECRETS_TABLE} (#290)"
  else
    echo "[oc:launch] FATAL(#290): DDB get-item for gateway_token_ct/device_paired_b64 failed (throttle/IAM/network) — fail-closed, scheduler will retry" >&2
    exit 1
  fi
  unset _SEC_RAW _SECRETS_TABLE
fi
# #312 — device_paired_b64 二级回落到 tenants 表(长期,无 TTL)。存量租户(改动前建的)
# 或 create 时持久化失败的租户,tenant_secrets 里可能没有 device_paired_b64,上面 #290
# 一级回落(查 tenant_secrets)拿到空 → paired.json 无源重注入 → 网关读空盘配对 → 前端
# NOT_PAIRED(真机复现)。paired.json 是公开信息(deviceId+publicKey+roles+scopes,无私钥),
# create 时已长期存 tenants.device_paired_b64(无 TTL),这里作长期兜底。gateway_token 不做
# 此回落:它是机密,只从 tenant_secrets 读(#353 起该表也无 TTL 长存,回读不会落空)。
# fail-open:tenants 读失败不中止(paired 缺失只是回退到人工 approve,非 fail-closed 安全事件)。
_PAIRED_FROM_TENANTS=0
if [ -z "${INJECTED_DEVICE_PAIRED_B64}" ]; then
  _TENANTS_TABLE="${TENANTS_TABLE:-openclaw-tenants}"
  if _TEN_RAW="$(aws dynamodb get-item \
    --table-name "${_TENANTS_TABLE}" \
    --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
    --projection-expression 'device_paired_b64' \
    --consistent-read \
    --region "${OC_REGION:-ap-northeast-1}" \
    --output json 2>/dev/null)"; then
    INJECTED_DEVICE_PAIRED_B64="$(printf '%s' "${_TEN_RAW}" | jq -r '.Item.device_paired_b64.S // ""' 2>/dev/null || true)"
    [ -n "${INJECTED_DEVICE_PAIRED_B64}" ] && { echo "[oc:launch] DDB fallback: got device_paired_b64 from ${_TENANTS_TABLE} (#312 long-term)"; _PAIRED_FROM_TENANTS=1; }
  fi
  unset _TEN_RAW _TENANTS_TABLE
fi
# #314(codex review 缺陷2 修复:存量租户 backfill)——若 device_paired_b64 来自位置参
# (12/13,首建 dispatch)或 tenant_secrets 一级回落(#290),而【不是】从 tenants 表读来的,
# 说明 tenants 表可能还没这条(存量租户:改动前建的;或 create 时持久化失败被兜底)。
# 回写 tenants 表(无 TTL),让该租户下次 restart data 盘丢了也有长期源可重建。host_role
# 有 tenants 表读写权(compute.py:57)。幂等 update 单字段;写失败 fail-open(不阻塞 launch)。
if [ -n "${INJECTED_DEVICE_PAIRED_B64}" ] && [ "${_PAIRED_FROM_TENANTS}" != "1" ]; then
  aws dynamodb update-item \
    --table-name "${TENANTS_TABLE:-openclaw-tenants}" \
    --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
    --update-expression "SET device_paired_b64 = :dpb" \
    --expression-attribute-values "{\":dpb\":{\"S\":\"${INJECTED_DEVICE_PAIRED_B64}\"}}" \
    --region "${OC_REGION:-ap-northeast-1}" >/dev/null 2>&1 \
    && echo "[oc:launch] backfilled device_paired_b64 to tenants table (#314 存量租户自愈)" \
    || echo "[oc:launch] WARN(#314): backfill device_paired_b64 to tenants failed (non-fatal)"
fi
unset _PAIRED_FROM_TENANTS
# #41 — harden-config.sh 提供 POSIX sh 幂等 openclaw.json 收敛函数
# (oc_harden_config + oc_normalize_litellm_baseurl)。launch-vm.sh 每次启动都调
# oc_harden_config,不管 fresh/wake,收敛部署相关值(CloudFront origin/LiteLLM
# baseUrl/chatCompletions 三态/apiKey 显式非空)。缺文件 = 部署漂移 → fail-loud。
if [ -r /home/ubuntu/lib/harden-config.sh ]; then
  # shellcheck disable=SC1091
  . /home/ubuntu/lib/harden-config.sh
else
  echo "[oc:launch] FATAL: /home/ubuntu/lib/harden-config.sh missing (init-host.sh should have downloaded it)" >&2
  exit 1
fi
mkdir -p ${VM_DIR}
rm -f ${VM_DIR}/.stopped
SOCK="${VM_DIR}/fc.sock"
TAP="tap-vm${VM_NUM}"
# ── Addressing: one /30 point-to-point link per VM (host .+1 / guest .+2) ──
# The old scheme mapped vm_num directly to the 3rd octet
# (SUBNET_PREFIX.<vm_num>.{1,2}/24), which capped a host at 254 VMs (3rd octet
# ≤254) and 255 MACs (single-byte suffix). To pack 480+ VMs on one big host we
# lay out a contiguous /30 per VM across the whole SUBNET_PREFIX/16:
#   block       = (vm_num-1) * 4            # 4 addrs per /30 (net/host/guest/bcast)
#   3rd octet   = block / 256
#   4th base    = block % 256
#   HOST_TAP_IP = SUBNET_PREFIX.<o3>.<base+1>   (the /30 host end)
#   GUEST_IP    = SUBNET_PREFIX.<o3>.<base+2>   (the /30 guest end)
# vm_num=1 → host .0.1 / guest .0.2 ; vm_num=480 → host .7.125 / guest .7.126.
# All inside SUBNET_PREFIX/16, so the /16 east-west DROP still covers every VM.
# MAC encodes vm_num in the last TWO bytes so it never overflows a single byte.
SUBNET_PREFIX="${SUBNET_PREFIX:-10.0}"
_BLOCK=$(( (VM_NUM - 1) * 4 ))
_O3=$(( _BLOCK / 256 ))
_O4=$(( _BLOCK % 256 ))
HOST_TAP_IP="${SUBNET_PREFIX}.${_O3}.$(( _O4 + 1 ))"
GUEST_IP="${SUBNET_PREFIX}.${_O3}.$(( _O4 + 2 ))"
GUEST_MAC="AA:FC:00:00:$(printf '%02x:%02x' $(( VM_NUM / 256 )) $(( VM_NUM % 256 )))"
log() { echo "[oc:launch] $(date +%H:%M:%S) $*"; }

# Write VM metadata for host-agent discovery
cat > "${VM_DIR}/vm.json" << VMEOF
{"tenant_id":"${TENANT_ID}","vm_num":${VM_NUM},"guest_ip":"${GUEST_IP}","vcpu":${VCPU},"mem_mb":${MEM_MB},"config_template":"${CONFIG_TEMPLATE}"}
VMEOF

log "START ${TENANT_ID} vm${VM_NUM} ${VCPU}vCPU/${MEM_MB}MB"

# Cleanup previous instance
pkill -f "api-sock ${SOCK}" 2>/dev/null || true
sudo ip link del ${TAP} 2>/dev/null || true
rm -f ${SOCK}; sleep 0.5
# 清残留 vsock UDS:stop-vm 只删 fc.sock/fc.log,Firecracker PUT /vsock 要 bind 的
# 裸 vsock.sock 会存活 → 下次 PUT 撞 "Address in use"(真机实测)。此处每次 launch 都
# 跑、由 :376 的 per-tenant flock 保护,覆盖 create/restart/restore/recover 所有路径。
# 只清裸 vsock.sock:后缀 socket vsock.sock_<port> 是 reader 侧 listen 的落点,归 reader
# 独占管理(reader _bind 自己 unlink),launch 不能通配删,否则删掉 reader 正在监听的
# socket → reader 线程仍登记但连接断、重启后永久丢日志(codex 复审抓出)。
rm -f ${VM_DIR}/vsock.sock

# Prepare disks
log "preparing disks..."
T0=$SECONDS
ROOTFS="/data/firecracker-assets/openclaw-rootfs.ext4"
DATA_TPL="/data/firecracker-assets/openclaw-data-template.ext4"
DATA_SIZE=$(stat -c%s ${DATA_TPL})
# Immutable authority disk (identity files + ops skills). Shared, read-only,
# attached to every VM as /dev/vdd with is_read_only:true. Optional: if the
# asset is absent (older image set), we skip the 4th drive so launch still works.
IMMUTABLE_TPL="/data/firecracker-assets/openclaw-immutable.ext4"

# Overlay: sparse file for rootfs copy-on-write (shared read-only rootfs + per-VM writable layer)
OVERLAY="${VM_DIR}/overlay.ext4"
if [ ! -f "${OVERLAY}" ]; then
  truncate -s "${ROOTFS_OVERLAY_MB:-8192}M" ${OVERLAY}
  mkfs.ext4 -q ${OVERLAY}
fi

# Data volume: first-time initialize, subsequent launches reuse existing.
#   - With RESTORE_KEY: download backup from S3, decompress, e2fsck. Size is whatever the backup is.
#   - Without:          sparse-copy from template. Size must match DATA_SIZE.
DATA_VOL="${VM_DIR}/data.ext4"
NEW_DATA=false
NEEDS_INIT=false
if [ ! -f "${DATA_VOL}" ]; then
  NEEDS_INIT=true
elif [ -z "${RESTORE_KEY}" ] && [ "$(stat -c%s ${DATA_VOL})" != "${DATA_SIZE}" ]; then
  # #303 数据丢失级修复:一个**已存在**的 data.ext4 是该租户的真实数据盘
  # (identity/skills/config/channel_secret/vkey/用户数据全在里面)。升级镜像时
  # refresh_rootfs 会 mv 换新 data-template(host_service.py:377),其逻辑尺寸常
  # 与租户现有盘不同;而 rebuild/restart 的 wake 传空 RESTORE_KEY。原逻辑在此
  # 分支 NEEDS_INIT=true → 下面 `rm -f ${DATA_VOL}` 从模板重建 = **静默删光客户数据**
  # (真机复现:升级后 rebuild 数据全丢)。铁律 no-data-loss:存量盘遇模板尺寸漂移
  # **绝不重建**,保留原盘照常挂载启动(模板尺寸只是"新建时用多大",不是"存量盘必须
  # 等于它")。真要扩盘走显式 resize-disk(#22,resize2fs 在线扩,不删数据);真要
  # 换盘走显式 RESTORE_KEY(下面恢复路径)。这里只对存量盘 fail-safe 保留 + 告警。
  log "WARN(#303): data.ext4 尺寸($(stat -c%s ${DATA_VOL}))≠ 模板(${DATA_SIZE}),但存量盘含客户数据 — 保留原盘不重建(扩盘用 resize-disk,换盘用 restore)"
fi
if [ "${NEEDS_INIT}" = "true" ]; then
  rm -f ${DATA_VOL}
  if [ -n "${RESTORE_KEY}" ]; then
    # #199 fix — 备份写在 BACKUP_BUCKET(WORM+CMK 专用桶,见 backup-data.sh:16),
    # 不是 ASSETS_BUCKET。原来从 ASSETS_BUCKET 拉 → 永远下载失败 → restore 拒起。
    # 回退 ASSETS_BUCKET 兼容未配 BACKUP_BUCKET 的旧 host(与 _resolve_backup 同源)。
    _RESTORE_BUCKET="${BACKUP_BUCKET:-${ASSETS_BUCKET}}"
    log "restoring from s3://${_RESTORE_BUCKET}/${RESTORE_KEY}"
    # #199 fail-loud(对标 restic checker:missing/truncated 绝不静默放行,
    # internal/repository/checker.go:232 用实际大小≠期望判 Truncated 报错)。
    # 现状:aws s3 cp --quiet 失败或拿到 0 字节 → pigz 出空盘 → e2fsck 在空盘上
    # 可能过 → VM 带空白盘 running = 数据丢失级。三道 fail-loud 守卫,任一不过
    # 立即 exit 1(绝不 fall through 到建空白盘):
    #   ① 下载必须成功且非空(cp 失败/空对象 → 拒起,不建空盘)
    #   ② (加密备份)envelope 解密必须成功;解压必须成功(截断/损坏 → 拒起)
    #   ③ 解出的 data.ext4 必须非空且够一个最小 ext4 superblock(>64KiB)
    _DL="/tmp/restore-${TENANT_ID}.dl"
    if ! aws s3 cp "s3://${_RESTORE_BUCKET}/${RESTORE_KEY}" "${_DL}" \
         --region "${OC_REGION:-ap-northeast-1}" --quiet \
       || [ ! -s "${_DL}" ]; then
      rm -f "${_DL}"
      log "FATAL(#199): restore backup s3://${_RESTORE_BUCKET}/${RESTORE_KEY} 下载失败或为空 — 拒起,绝不用空白盘冒充恢复(数据丢失级)"
      exit 1
    fi
    # #199 fix — 加密备份(.gz.enc + 同前缀 .gz.key)客户端 envelope 解密,对称
    # backup-data.sh 的加密段:KMS decrypt .gz.key 拿数据密钥 → openssl AES-256-CBC
    # 解 .gz.enc → 得明文 .gz。host restore 段原来完全没有解密逻辑(只会 pigz 明文
    # .gz),生产配了 BACKUP_CMK_KEY_ID 时备份是 .gz.enc → 直接 pigz 必失败 → 数据
    # restore 不出来。非 .enc 后缀 = 明文备份,跳过解密直接当 .gz 用。
    _GZ="/tmp/restore-${TENANT_ID}.gz"
    case "${RESTORE_KEY}" in
      *.enc)
        _KEYOBJ="${RESTORE_KEY%.enc}.key"   # <ts>.gz.enc → <ts>.gz.key
        if ! aws s3 cp "s3://${_RESTORE_BUCKET}/${_KEYOBJ}" "${_DL}.key" \
             --region "${OC_REGION:-ap-northeast-1}" --quiet \
           || [ ! -s "${_DL}.key" ]; then
          rm -f "${_DL}" "${_DL}.key"
          log "FATAL(#199): 加密备份缺数据密钥对象 s3://${_RESTORE_BUCKET}/${_KEYOBJ} — 拒起"
          exit 1
        fi
        # KMS decrypt CiphertextBlob(.key 是 raw 密文字节)→ Plaintext(base64)→ hex
        _DK_HEX="$(aws kms decrypt --ciphertext-blob "fileb://${_DL}.key" \
                     --region "${OC_REGION:-ap-northeast-1}" \
                     --query Plaintext --output text 2>/dev/null \
                   | base64 -d 2>/dev/null | xxd -p -c 256 | tr -d '\n')"
        if [ -z "${_DK_HEX}" ]; then
          rm -f "${_DL}" "${_DL}.key"
          log "FATAL(#199): 数据密钥 KMS 解密失败(无 kms:Decrypt 权限/密钥不匹配)— 拒起"
          exit 1
        fi
        if ! openssl enc -d -aes-256-cbc -pbkdf2 -in "${_DL}" -out "${_GZ}" \
               -K "${_DK_HEX}" -iv 00000000000000000000000000000000 2>/dev/null \
           || [ ! -s "${_GZ}" ]; then
          unset _DK_HEX; rm -f "${_DL}" "${_DL}.key" "${_GZ}"
          log "FATAL(#199): 备份 AES 解密失败(密文损坏/密钥不符)— 拒起,不留半个盘"
          exit 1
        fi
        unset _DK_HEX; rm -f "${_DL}" "${_DL}.key"
        ;;
      *)
        mv "${_DL}" "${_GZ}"   # 明文备份:直接当 .gz
        ;;
    esac
    if ! pigz -d -c "${_GZ}" > ${DATA_VOL}; then
      rm -f "${_GZ}" ${DATA_VOL}
      log "FATAL(#199): restore .gz 解压失败(截断/损坏)— 拒起,不留半个盘"
      exit 1
    fi
    rm -f "${_GZ}"
    _restored_bytes="$(stat -c%s ${DATA_VOL} 2>/dev/null || echo 0)"
    if [ "${_restored_bytes}" -lt 65536 ]; then
      rm -f ${DATA_VOL}
      log "FATAL(#199): restore 出的 data.ext4 仅 ${_restored_bytes} 字节(< 最小 ext4 superblock)— 空/截断盘,拒起"
      exit 1
    fi
    # 1.3.1+1.3.2: backup-data.sh dumps the ext4 image while the VM is
    # *paused* (vCPUs frozen but pending journal not committed). On
    # restore, e2fsck must replay that journal — making it return:
    #   0 = clean
    #   1 = errors corrected (most common after journal replay)
    #   2 = errors corrected, system should reboot (we ignore reboot)
    #   4 = errors NOT corrected (real damage)
    #   8 = operational error (e.g. file IO issue or unsupported feature)
    #  16 = usage / syntax error (we never trigger this)
    # We accept 0/1/2/8: 8 happens on Firecracker's own e2fsck binary
    # when the backup uses ext4 features the host's e2fsck doesn't know
    # about (forward-compat issue, not corruption — the guest kernel
    # will mount it fine). Reject 4 and 16.
    fsck_rc=0
    e2fsck -fy ${DATA_VOL} >/dev/null 2>&1 || fsck_rc=$?
    if [ $fsck_rc -eq 4 ] || [ $fsck_rc -eq 16 ]; then
      log "FATAL: backup filesystem check failed (e2fsck rc=${fsck_rc})"
      exit 1
    fi
    log "restored $(stat -c%s ${DATA_VOL}) bytes (e2fsck rc=${fsck_rc})"
  else
    cp --sparse=always ${DATA_TPL} ${DATA_VOL}
  fi
  NEW_DATA=true
fi
log "disks ready ($((SECONDS-T0))s)"

# Inject shared skills into data disk
SHARED_SKILLS="/data/shared-skills"
MOUNT_TMP="/tmp/data-mount-${TENANT_ID}"
mkdir -p ${MOUNT_TMP}
# #256 — 入口幂等预清理:上次 attempt 被强杀(SIGKILL/OOM/host 重启)在 trap 兜底跑之前
# 就死了会泄漏这个挂载点,下次进来直接撞 "already mounted" 卡死。这里 mount 前先卸残留。
# 用 plain umount(不用 -l 惰性):此刻已持有 per-tenant flock(见上,是本租户唯一 owner),
# 残留必来自被 SIGKILL 的死进程(无活写者),plain umount 必成功;若 busy(有意外活写者)
# = 违反不变量,让 set -e fail-loud 中止 + 调度重试,绝不 lazy-detach-then-remount
# (那会让活写者继续写旧挂载 + remount 双挂同一 backing file → ext4 损坏,踩 no-data-loss)。
mountpoint -q "${MOUNT_TMP}" && sudo umount "${MOUNT_TMP}"
sudo mount ${DATA_VOL} ${MOUNT_TMP}
# Skills (1.4.0 #62: optional per-tenant scope via $SCOPED_SKILLS comma-list)
if [ -d "${SHARED_SKILLS}" ] && [ "$(ls -A ${SHARED_SKILLS} 2>/dev/null)" ]; then
  if [ -z "${SCOPED_SKILLS}" ] || [ "${SCOPED_SKILLS}" = "*" ]; then
    log "injecting all shared skills (broadcast mode)"
    mkdir -p ${MOUNT_TMP}/.openclaw/skills
    cp -r ${SHARED_SKILLS}/* ${MOUNT_TMP}/.openclaw/skills/ 2>/dev/null || true
  else
    log "injecting scoped skills: ${SCOPED_SKILLS}"
    mkdir -p ${MOUNT_TMP}/.openclaw/skills
    IFS=',' read -ra SKILL_LIST <<< "${SCOPED_SKILLS}"
    for skill in "${SKILL_LIST[@]}"; do
      skill_dir="${SHARED_SKILLS}/${skill}"
      if [ -d "${skill_dir}" ]; then
        cp -r "${skill_dir}" ${MOUNT_TMP}/.openclaw/skills/ 2>/dev/null || true
      else
        log "  skipped unknown skill: ${skill}"
      fi
    done
  fi
  sudo chown -R 1000:1000 ${MOUNT_TMP}/.openclaw/skills
  log "skills injected"
fi
# ─────────────────────────────────────────────────────────────────────────
# #118/#116 + #149 — platform-injected credentials: read tenant record + decrypt.
#
# MOVED here (before the openclaw.json config block) from its old post-umount
# position so that: (a) config-class injection (oc_inject_config_from_plan) can
# see _FP_PURE while the data disk is STILL MOUNTED, and (b) env-class plaintext
# is prepared into _CREDS_ENV; the read-only creds disk is BUILT later (after
# umount) from that file — see the "build creds disk" block below.
#
# The control plane stores each credential as ciphertext on the tenant record
# (never plaintext, never on the SSM command line). The host — the ONLY place
# with kms:Decrypt on the ClawPool CMK(s) — decrypts each value:
#   • kms-cmk (legacy/default): symmetric CMK + owner_id EncryptionContext
#     (cross-tenant containment: a ciphertext minted for another tenant fails).
#   • asymmetric-v1 (#149): RSA-4096 OAEP-SHA256 via the RSA CMK; KMS asymmetric
#     Decrypt does NOT accept EncryptionContext (verified ValidationException),
#     so tenant binding is the per-tenant frozen plan + envelope key_id (scheme-B).
# env-class → dotenv on a per-VM ext4 attached READ-ONLY as /dev/vde; config-class
# → jq dot-path overwrite in openclaw.json (plaintext never lands on the data disk).
# No creds disk / no plan → unchanged behavior for tenants without injected creds.
CREDS_VOL="${VM_DIR}/creds.ext4"
rm -f "${CREDS_VOL}"
_CREDS_ENV=""   # set below iff env-class creds were decrypted; drives disk build
# Read the tenant record. --consistent-read closes the create→launch race (the
# default eventually-consistent read could miss injected_credentials/frozen plan
# just written by create_tenant). Separate the AWS CALL from the jq PARSE so we
# can tell three cases apart instead of collapsing them to "empty" (old fail-OPEN
# bug):
#   • call errors (throttle / IAM / network) → fail-CLOSED: abort launch, the
#     scheduler retries. Never boot a credential-provisioned VM without its creds.
#   • call ok, no injected field → clean no-op (the 99% common case).
#   • call ok, field present → decrypt below.
# if-condition form: set -e is disabled for the command, so a non-zero aws exit
# reaches our explicit fail-closed branch instead of aborting at the assignment.
if _IC_RAW="$(aws dynamodb get-item \
  --table-name "${TENANTS_TABLE:-openclaw-tenants}" \
  --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
  --projection-expression 'injected_credentials, owner_id, frozen_injection_plan, scheme, registry_version, litellm_vkey' \
  --consistent-read \
  --region "${OC_REGION:-ap-northeast-1}" \
  --output json 2>/dev/null)"; then
  :
else
  log "FATAL: DDB get-item for injected_credentials failed (throttle/IAM/network) — aborting launch fail-closed (scheduler will retry)"
  exit 1
fi
_IC_JSON="$(printf '%s' "${_IC_RAW}" | jq -c '.Item.injected_credentials.M // empty' 2>/dev/null || true)"
# #149 Task 8.1: frozen_injection_plan 新契约(优先于旧 injected_credentials)
_FP_JSON="$(printf '%s' "${_IC_RAW}" | jq -c '.Item.frozen_injection_plan.M // empty' 2>/dev/null || true)"
_FP_SCHEME="$(printf '%s' "${_IC_RAW}" | jq -r '.Item.scheme.S // "kms-cmk"' 2>/dev/null || true)"
if [ -n "${_FP_JSON}" ] && [ "${_FP_JSON}" != "null" ]; then
  # 新契约: env-class → cred-inject(下面建盘), config-class → harden-config(配置块内)
  _CRED_OWNER="$(printf '%s' "${_IC_RAW}" | jq -r '.Item.owner_id.S // empty' 2>/dev/null || true)"
  if [ -z "${_CRED_OWNER}" ]; then
    log "FATAL: frozen_injection_plan present but no owner_id — aborting fail-closed"
    exit 1
  fi
  log "frozen_injection_plan present (scheme=${_FP_SCHEME}) — injecting via new contract"
  if [ -r /home/ubuntu/lib/cred-inject.sh ]; then
    # shellcheck disable=SC1091
    . /home/ubuntu/lib/cred-inject.sh
  else
    log "FATAL: /home/ubuntu/lib/cred-inject.sh missing"; exit 1
  fi
  # 转换 DDB M 格式为纯 JSON(去掉 DDB 类型标注);供 env 解密与 config 注入共用。
  _FP_PURE="$(printf '%s' "${_IC_RAW}" | jq -c '[.Item.frozen_injection_plan.M | to_entries[] | {(.key): {param_class: .value.M.param_class.S, injection_target: .value.M.injection_target.S, sensitive: (.value.M.sensitive.BOOL // false), mode: .value.M.mode.S, value_ref: (.value.M.value_ref.S // ""), empty_fallback: (.value.M.empty_fallback.S // "")}}] | add // {}' 2>/dev/null || true)"
  export _FP_PURE _FP_SCHEME _CRED_OWNER
  # env-class 解密进临时 dotenv(建盘在 umount 之后)。config-class 由
  # oc_inject_config_from_plan 在配置块内处理(此时数据盘仍挂载)。
  _CREDS_ENV_TMP="$(mktemp /tmp/creds-${TENANT_ID}.XXXXXX.env)"
  chmod 600 "${_CREDS_ENV_TMP}"
  if ! _FP_COUNT="$(oc_decrypt_frozen_plan "${_FP_PURE}" "${_CRED_OWNER}" "${_FP_SCHEME}" "${OC_REGION:-ap-northeast-1}" "${_CREDS_ENV_TMP}" "${CLAWPOOL_RSA_CMK_ARN:-}")"; then
    log "FATAL: frozen plan env-class decrypt failed — aborting"
    shred -u "${_CREDS_ENV_TMP}" 2>/dev/null || rm -f "${_CREDS_ENV_TMP}"
    exit 1
  fi
  # 仅当真有 env-class 条目写出时才建盘(纯 config-class 计划不需要 creds 盘)。
  if [ -s "${_CREDS_ENV_TMP}" ]; then
    _CREDS_ENV="${_CREDS_ENV_TMP}"
  else
    rm -f "${_CREDS_ENV_TMP}"
  fi
elif [ -n "${_IC_JSON}" ] && [ "${_IC_JSON}" != "null" ]; then
  # owner_id is the EncryptionContext the upstream encrypted the userkey under.
  _CRED_OWNER="$(printf '%s' "${_IC_RAW}" | jq -r '.Item.owner_id.S // empty' 2>/dev/null || true)"
  if [ -z "${_CRED_OWNER}" ]; then
    log "FATAL: injected_credentials present but tenant record has no owner_id (EC binding missing) — aborting launch fail-closed"
    exit 1
  fi
  log "injected_credentials present — decrypting via host KMS role (EC owner_id=${_CRED_OWNER})"
  # decrypt logic lives in lib/cred-inject.sh (unit-tested with a stubbed aws).
  if [ -r /home/ubuntu/lib/cred-inject.sh ]; then
    # shellcheck disable=SC1091
    . /home/ubuntu/lib/cred-inject.sh
  else
    log "FATAL: /home/ubuntu/lib/cred-inject.sh missing (init-host.sh should have downloaded it)"
    exit 1
  fi
  _CREDS_ENV_TMP="$(mktemp /tmp/creds-${TENANT_ID}.XXXXXX.env)"
  chmod 600 "${_CREDS_ENV_TMP}"
  # fail-closed: any decrypt failure (EC mismatch / tampered / no perm / malformed)
  # returns non-zero → we abort the launch, never boot with half/empty creds.
  if ! _IC_COUNT="$(oc_decrypt_injected_creds "${_IC_JSON}" "${_CRED_OWNER}" "${OC_REGION:-ap-northeast-1}" "${_CREDS_ENV_TMP}")"; then
    log "FATAL: injected credential decrypt failed (see [oc:cred] above) — aborting launch"
    shred -u "${_CREDS_ENV_TMP}" 2>/dev/null || rm -f "${_CREDS_ENV_TMP}"
    exit 1
  fi
  _CREDS_ENV="${_CREDS_ENV_TMP}"
  log "decrypted ${_IC_COUNT} credential(s) — creds disk built after umount"
fi

# Configure openclaw.json
OC_JSON="${MOUNT_TMP}/.openclaw/openclaw.json"
if [ -f "${OC_JSON}" ] && command -v jq &>/dev/null; then
  # ─────────────────────────────────────────────────────────────────────
  # ONE-TIME 生成(NEW_DATA 才跑):config template 首次下载、gateway token 首铸、
  # channel_secret 首次落盘、Cognito 注入、per-tenant vkey 首次注入。这些是"一次
  # 性生成"的东西——重跑会破坏 DDB 握手(hub 校验 channel_secret 用的是首次那个),
  # 或用 shared vkey 覆盖已铸的 per-tenant vkey坏计费拆分。
  # ─────────────────────────────────────────────────────────────────────
  if [ "$NEW_DATA" = "true" ]; then
    # Download custom template from S3 (if specified). 幂等段跑之前先下,让
    # oc_harden_config 收敛新拉下来的模板;唤醒不重下(会冲掉用户配置)。
    # #301 — "default" 是烤进 rootfs 的基线模板(OC_JSON 已是它),S3 无
    # templates/openclaw/default/openclaw.json。旧代码对字面 "default" 也 s3 cp →
    # 404 → set -e 在 token 注入前 die → 半盘 → 重投 token 漂移。跳过 default(与空
    # 模板同义:都用基线),只对真正的具名自定义模板拉 S3。
    if [ -n "${CONFIG_TEMPLATE}" ] && [ "${CONFIG_TEMPLATE}" != "default" ] && [ -n "${ASSETS_BUCKET:-}" ]; then
      aws s3 cp "s3://${ASSETS_BUCKET}/templates/openclaw/${CONFIG_TEMPLATE}/openclaw.json" "${OC_JSON}" --region "${OC_REGION:-ap-northeast-1}" --quiet
      log "config template '${CONFIG_TEMPLATE}' applied"
    fi
    # Two SEPARATE, ORTHOGONAL auth layers:
    #   (a) gateway.auth.token  — protects the control plane / control UI.
    #   (b) channels.claw-channel.secret — HMAC secret for the signed C-end
    #       webhook (the mini-app's backend signs with the same secret using the
    #       Cognito-verified `sub`). This is the user-message path; it does NOT
    #       touch gateway.auth.token.
    NEW_TOKEN=$(openssl rand -hex 24)
    # #187 P1 — if the control plane pre-minted a gateway token (KMS envelope,
    # tenant_id EncryptionContext), decrypt it here on the host and use THAT as
    # NEW_TOKEN, overriding the openssl rand above. Rationale:
    #   • Control plane needs `GET /tenants/{id}/token` reveal for direct-gateway
    #     data plane (11-ENGINE-TRANSFORM): only pre-minted tokens can be revealed.
    #   • openssl rand stays as the FEATURE-OFF fallback (byte-identical old path
    #     for un-migrated deployments; no CMK → no ciphertext → no override).
    # Fail-closed: ciphertext present but decrypt fails (EC mismatch, no perm,
    # tampered blob) → abort the whole launch. Never boot a VM with a "control
    # plane THINKS this is the token" mismatch — reveal would hand out a token
    # the VM doesn't accept, which is worse than a plain 502.
    if [ -n "${INJECTED_GATEWAY_TOKEN_CT}" ]; then
      _GW_TOKEN_PLAIN="$(printf '%s' "${INJECTED_GATEWAY_TOKEN_CT}" | base64 -d 2>/dev/null \
        | aws kms decrypt \
            --ciphertext-blob fileb:///dev/stdin \
            --encryption-context "tenant_id=${TENANT_ID}" \
            --region "${OC_REGION:-ap-northeast-1}" \
            --query Plaintext --output text 2>/dev/null \
        | base64 -d 2>/dev/null || true)"
      if [ -z "${_GW_TOKEN_PLAIN}" ]; then
        log "FATAL: pre-minted gateway token decrypt failed (EC mismatch / no perm / tampered) — aborting launch fail-closed"
        exit 1
      fi
      # Same control-char guard as cred-inject: a stray \r would corrupt the jq
      # --arg on the openclaw.json write (JSON string with embedded newline → not
      # a valid token to bearer-auth against). Byte-count via tr -dc [:cntrl:].
      if [ "$(printf '%s' "${_GW_TOKEN_PLAIN}" | LC_ALL=C tr -dc '[:cntrl:]' | wc -c | tr -d ' ')" != "0" ]; then
        log "FATAL: pre-minted gateway token contains control chars (rejected)"
        exit 1
      fi
      NEW_TOKEN="${_GW_TOKEN_PLAIN}"
      unset _GW_TOKEN_PLAIN
      log "using control-plane pre-minted gateway token (reveal-capable, #187 P1)"
    fi
    # #187 P5 — claw-channel HMAC/Cognito 注入整段随 channel/hub 数据面下线一并移除。
    # 镜像 v5 (P3) 已在 build-rootfs 阶段 del(.channels["claw-channel"]),launch-vm
    # 只需注入 gateway token(数据面走两级路由直连 microVM:18789)。
    jq --arg t "$NEW_TOKEN" \
      '.gateway.auth.token = $t' \
      "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
    log "gateway token injected (one-time; #187 P5: hub/Cognito channel 已下线)"

    # #188 — cold-inject devices/paired.json so a remote WSS client (JDWS)
    # preloaded with the matching Ed25519 device connects to the gateway with NO
    # manual approve (INJECTION-SPEC-2026.2.26.md, protocol v3: pairing gate只看
    # roles + publicKey 匹配,tokens 可空). One-time (NEW_DATA-only): the file
    # lives on the data disk, so wake never re-injects.
    # fail-CLOSED: cold-injection是免approve的安全前提。base64 解码失败 / 解出的不是
    # 合法 JSON object / 含控制字符 → log FATAL + exit 1,绝不半灌一个坏 paired.json
    # (半灌会让 gateway 起来但配对门行为不可预期,比不注入更糟)。
    if [ -n "${INJECTED_DEVICE_PAIRED_B64}" ]; then
      _PAIRED_JSON="$(printf '%s' "${INJECTED_DEVICE_PAIRED_B64}" | base64 -d 2>/dev/null || true)"
      if [ -z "${_PAIRED_JSON}" ]; then
        log "FATAL: paired.json base64 decode failed (#188) — aborting launch fail-closed"
        exit 1
      fi
      # Control-char guard: a stray \r/\n mid-string would corrupt the JSON we
      # write to disk. Same tr -dc [:cntrl:] byte-count guard as the gateway token.
      if [ "$(printf '%s' "${_PAIRED_JSON}" | LC_ALL=C tr -dc '[:cntrl:]' | wc -c | tr -d ' ')" != "0" ]; then
        log "FATAL: paired.json contains control chars (rejected #188)"
        exit 1
      fi
      # Validate it parses as a JSON OBJECT (not array/scalar) before writing.
      if ! printf '%s' "${_PAIRED_JSON}" | jq -e 'type == "object"' >/dev/null 2>&1; then
        log "FATAL: paired.json is not a valid JSON object (#188) — aborting fail-closed"
        exit 1
      fi
      _DEVICES_DIR="${MOUNT_TMP}/.openclaw/devices"
      mkdir -p "${_DEVICES_DIR}"
      printf '%s' "${_PAIRED_JSON}" > "${_DEVICES_DIR}/paired.json"
      chmod 600 "${_DEVICES_DIR}/paired.json"
      sudo chown -R 1000:1000 "${_DEVICES_DIR}"
      # Log只打 deviceId 前16字符(paired.json 的顶层 key),不泄全量。
      _DID16="$(printf '%s' "${_PAIRED_JSON}" | jq -r 'keys[0] // "?"' 2>/dev/null | cut -c1-16)"
      log "paired.json cold-injected (one-time; #188 device=${_DID16}… wss 免 approve)"
      unset _PAIRED_JSON _DID16
    fi
  fi

  # ─────────────────────────────────────────────────────────────────────
  # 幂等收敛(#41)—— 每次启动都跑(fresh + wake),把部署相关值收敛到当前:
  #   • dangerouslyDisableDeviceAuth 无条件 del(secure default)
  #   • allowedOrigins → 当前 CloudFront origin(SSM 拉最新)
  #   • baseUrl → 当前 LiteLLM host(堡垒机重建 IP 会变)
  #   • chatCompletions 三态(1/0/空)
  #   • apiKey 仅在显式非空时改写(唤醒空参绝不覆盖数据盘上的 per-tenant vkey)
  # 老版本把这块塞在 NEW_DATA-only 分支里,唤醒路径完全跳过 → 唤醒即漂移。
  # 详细语义见 lib/harden-config.sh 的 oc_harden_config 注释。
  # ─────────────────────────────────────────────────────────────────────
  CF_ORIGIN="${CLOUDFRONT_ORIGIN:-}"
  LITELLM_BASEURL="$(oc_normalize_litellm_baseurl "${LITELLM_HOST:-}")"

  # #312 — per-tenant vkey 二级回落(tenants 表,无 TTL)。gap:vkey 只在 create/push
  # 作位置参 8 传入;wake/restart(start-all-vms/自愈/fan-out)位置 8 传空 → 原逻辑落到
  # LITELLM_SHARED_VKEY → oc_harden_config 每次启动把盘上 per-tenant apiKey 覆盖成
  # 共享 key → 每次 restart/镜像更新 per-tenant 计费拆分静默漂成 shared(task #15)。
  # 修:位置参空时,从 :767 已读的 tenants 表(_IC_RAW,投影含 litellm_vkey,无 TTL)回落。
  # 只在开了 per-tenant 计费的部署有 litellm_vkey 字段;没开则读空→行为不变(向后兼容)。
  if [ -z "${LITELLM_VKEY}" ]; then
    _TEN_VKEY="$(printf '%s' "${_IC_RAW}" | jq -r '.Item.litellm_vkey.S // ""' 2>/dev/null || true)"
    if [ -n "${_TEN_VKEY}" ]; then
      LITELLM_VKEY="${_TEN_VKEY}"
      log "per-tenant vkey re-read from tenants table (#312; restart 不漂成 shared)"
    fi
    unset _TEN_VKEY
  fi

  # apiKey:优先 per-tenant LITELLM_VKEY 参数(SSM 传入 / #312 tenants 回落,per-tenant 计费拆分);
  # 参数为空时才 fall back 到 platform.env 的 LITELLM_SHARED_VKEY(shared)。
  # 关键 fail-safe:LITELLM_VKEY 参数空 + LITELLM_SHARED_VKEY 也空 → _APIKEY 空 →
  # oc_harden_config 不写 apiKey(不会拿 shared 覆盖数据盘上的 per-tenant vkey)。
  # 老版本这一位失败会保留 __INJECT_AT_DEPLOY__ 占位 → agent 拿占位当 key → 401,
  # 现在只在有真 key 时改写,数据盘上首铸的 per-tenant vkey 会被幂等段保留。
  _APIKEY="${LITELLM_VKEY:-${LITELLM_SHARED_VKEY:-}}"

  # #80 · host 侧自愈:init-host 只在首启从 SSM 读一次 vkey,读空就永远空
  # (setup.sh 铸 vkey 晚于 host 首启就撞这个)。这里加单次「vkey 为空→补读 SSM」的
  # 自愈,把「铸 vkey 晚于 host 首启」的时序窗口封了。有值直接用,零延迟。
  # 关键:只补 shared vkey(未提供 per-tenant LITELLM_VKEY 时才走到这里);
  # 补到 _APIKEY 后传给 oc_harden_config,由 helper 幂等段真正落进 openclaw.json。
  if [ -z "${_APIKEY}" ]; then
    log "vkey empty in /etc/platform.env — 从 SSM /openclaw/litellm-shared-vkey 补读一次"
    _SSM_LK="$(aws ssm get-parameter --name /openclaw/litellm-shared-vkey --with-decryption \
                 --region "${OC_REGION:-ap-northeast-1}" --query 'Parameter.Value' --output text 2>/dev/null || true)"
    if [ -n "${_SSM_LK}" ] && [ "${_SSM_LK}" != "None" ]; then
      _APIKEY="${_SSM_LK}"
      # 回写 /etc/platform.env 让下一台 VM 首发不用再补读:替换现有行或追加。
      if grep -q '^LITELLM_SHARED_VKEY=' /etc/platform.env 2>/dev/null; then
        sed -i "s|^LITELLM_SHARED_VKEY=.*|LITELLM_SHARED_VKEY=${_APIKEY}|" /etc/platform.env
      else
        echo "LITELLM_SHARED_VKEY=${_APIKEY}" >> /etc/platform.env
      fi
      chmod 600 /etc/platform.env || true
      # 同进程后续读到这个变量(下一台 microVM 起 launch-vm 会重 source)
      export LITELLM_SHARED_VKEY="${_APIKEY}"
      log "vkey 从 SSM 补读成功并回写 /etc/platform.env"
    else
      log "SSM /openclaw/litellm-shared-vkey 仍为空(setup.sh 铸 vkey 未完成?)"
    fi
  fi

  # 两者都空且 SSM 补读也空 → helper 幂等段跳过 apiKey 写入(保留数据盘上的老 key)。
  # 首启且数据盘上 apiKey 还是烤死的 __INJECT_AT_DEPLOY__ 占位时,agent 拿占位当 key 调
  # LiteLLM → 401 → "Something went wrong"。打红字 WARN 让运维一眼看见根因。
  if [ -z "${_APIKEY}" ]; then
    log "WARN: 无 LITELLM_VKEY 也无 LITELLM_SHARED_VKEY(SSM 也空)— apiKey 保留占位符,LLM 调用会 401。设 SSM /openclaw/litellm-shared-vkey(setup.sh 或手工 aws ssm put-parameter)。"
  fi

  if oc_harden_config "${OC_JSON}" "${CF_ORIGIN}" "${LITELLM_BASEURL}" "${_APIKEY}" "${CHAT_EP_ENABLED}"; then
    # Task 8.3: frozen plan config-class dot-path 覆盖(新契约)
    if [ -n "${_FP_PURE:-}" ]; then
      if ! oc_inject_config_from_plan "${OC_JSON}" "${_FP_PURE}" "${_FP_SCHEME}" "${_CRED_OWNER}" "${OC_REGION:-ap-northeast-1}" "${LITELLM_SHARED_VKEY:-}" "${CLAWPOOL_RSA_CMK_ARN:-}"; then
        log "FATAL: config-class injection from frozen plan failed — aborting"
        exit 1
      fi
    fi
    # 日志一行看清幂等段执行了什么(帮排查唤醒漂移)
    _log_origin="${CF_ORIGIN:-<unset,skipped>}"
    _log_url="${LITELLM_BASEURL:-<unset,skipped>}"
    _log_chat="${CHAT_EP_ENABLED:-<unset,no-op>}"
    _log_key=""
    if [ -n "${LITELLM_VKEY}" ]; then _log_key="per-tenant"
    elif [ -n "${LITELLM_SHARED_VKEY:-}" ]; then _log_key="shared"
    else _log_key="<unset,preserving-disk>"
    fi
    log "harden-config: origin=${_log_origin} baseUrl=${_log_url} chat=${_log_chat} apiKey=${_log_key}"
  else
    # fail-loud:静默吞过一次就是事故。exit 让 trap 上报。
    log "FATAL: harden-config failed on ${OC_JSON}"
    exit 1
  fi
  sudo chown 1000:1000 "${OC_JSON}"

  # ─────────────────────────────────────────────────────────────────────
  # #312 幂等重注入 device 配对 + pre-minted gateway token —— 每次启动都跑
  # (fresh + wake/restart/recovery),与上面 #41 apiKey/origin 幂等收敛同源思路。
  # 根因(新加坡真机 + openclaw 源码双证):镜像更新 → 在跑 FC 掉线 → 平台自愈
  # restart(fleet-power → start-all-vms.sh → launch-vm 只传 4 参 + 复用 data 盘
  # NEW_DATA=false)→ 上面 NEW_DATA-only 冷注入块整体跳过 → 若那次盘上 paired.json
  # 恰空(新盘/被清)→ gateway 读到空(message-handler.ts:786 getPairedDevice→
  # isPaired=false)→ 前端 NOT_PAIRED。修:每次都把控制面权威的 device_paired_b64
  # + pre-minted token(脚本头 #290 DDB fallback 已从 openclaw-tenant-secrets 补齐,
  # 4 参调用也拿得到)幂等写回 data 盘,网关(重)启动永远读到 approved backend 条目。
  # 仅在 INJECTED_* 非空时写(老租户/无 pre-minted → 空 → 不动,保留盘上现值,零漂移;
  # gateway token 的 openssl rand 首铸仍只在 NEW_DATA 块,这里绝不用随机值覆盖)。
  # ─────────────────────────────────────────────────────────────────────
  if [ -n "${INJECTED_GATEWAY_TOKEN_CT}" ]; then
    _GW_TOKEN_RI="$(printf '%s' "${INJECTED_GATEWAY_TOKEN_CT}" | base64 -d 2>/dev/null \
      | aws kms decrypt \
          --ciphertext-blob fileb:///dev/stdin \
          --encryption-context "tenant_id=${TENANT_ID}" \
          --region "${OC_REGION:-ap-northeast-1}" \
          --query Plaintext --output text 2>/dev/null \
      | base64 -d 2>/dev/null || true)"
    if [ -z "${_GW_TOKEN_RI}" ]; then
      log "FATAL(#312): pre-minted gateway token decrypt failed on re-inject — aborting fail-closed"
      exit 1
    fi
    if [ "$(printf '%s' "${_GW_TOKEN_RI}" | LC_ALL=C tr -dc '[:cntrl:]' | wc -c | tr -d ' ')" != "0" ]; then
      log "FATAL(#312): re-inject gateway token contains control chars (rejected)"
      exit 1
    fi
    # 幂等:只在盘上 token 与权威值不一致时改写(避免每次 launch 无谓写盘)。
    _CUR_TOK="$(jq -r '.gateway.auth.token // ""' "${OC_JSON}" 2>/dev/null || true)"
    if [ "${_CUR_TOK}" != "${_GW_TOKEN_RI}" ]; then
      jq --arg t "${_GW_TOKEN_RI}" '.gateway.auth.token = $t' \
        "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
      sudo chown 1000:1000 "${OC_JSON}"
      log "gateway token re-injected (#312 idempotent; 盘上 token 与控制面权威不一致已收敛)"
    fi
    unset _GW_TOKEN_RI _CUR_TOK
  fi
  if [ -n "${INJECTED_DEVICE_PAIRED_B64}" ]; then
    _PAIRED_RI="$(printf '%s' "${INJECTED_DEVICE_PAIRED_B64}" | base64 -d 2>/dev/null || true)"
    if [ -z "${_PAIRED_RI}" ]; then
      log "FATAL(#312): paired.json base64 decode failed on re-inject — aborting fail-closed"
      exit 1
    fi
    if [ "$(printf '%s' "${_PAIRED_RI}" | LC_ALL=C tr -dc '[:cntrl:]' | wc -c | tr -d ' ')" != "0" ]; then
      log "FATAL(#312): re-inject paired.json contains control chars (rejected)"
      exit 1
    fi
    if ! printf '%s' "${_PAIRED_RI}" | jq -e 'type == "object"' >/dev/null 2>&1; then
      log "FATAL(#312): re-inject paired.json is not a JSON object — aborting fail-closed"
      exit 1
    fi
    _DEVICES_DIR_RI="${MOUNT_TMP}/.openclaw/devices"
    mkdir -p "${_DEVICES_DIR_RI}"
    # #314(codex review 缺陷1 修复):按 deviceId **merge**,不整体覆盖。
    # 盘上 paired.json 可能含网关运行时 approve 的【其它设备】+ 每设备运行时字段
    # (tokens/lastSeen*)。整体覆盖会删掉它们(丢运行时授权状态)。用 jq 递归 merge
    # `盘上 * 控制面`:控制面这条(们)device 新增/更新公钥·roles·scopes;盘上已有的
    # 其它 deviceId 原样保留;同 deviceId 递归 merge(盘上在左),盘上运行时独有字段
    # (tokens/lastSeen)保留,控制面的 tokens:{} 空对象 merge 不覆盖盘上非空 tokens。
    _CUR_PAIRED="$(cat "${_DEVICES_DIR_RI}/paired.json" 2>/dev/null || true)"
    if ! printf '%s' "${_CUR_PAIRED}" | jq -e 'type == "object"' >/dev/null 2>&1; then
      _CUR_PAIRED='{}'  # 盘上空/损坏 → 退化成只用控制面这份(等价冷注入)
    fi
    _MERGED_RI="$(jq -cn --argjson cur "${_CUR_PAIRED}" --argjson ctl "${_PAIRED_RI}" '$cur * $ctl' 2>/dev/null || true)"
    if [ -z "${_MERGED_RI}" ]; then
      log "FATAL(#314): paired.json merge(jq \$cur * \$ctl)失败 — aborting fail-closed"
      exit 1
    fi
    # 幂等:merge 结果与盘上一致则不写(避免无谓写盘 + mtime 抖动)。
    if [ "${_CUR_PAIRED}" != "${_MERGED_RI}" ]; then
      printf '%s' "${_MERGED_RI}" > "${_DEVICES_DIR_RI}/paired.json"
      chmod 600 "${_DEVICES_DIR_RI}/paired.json"
      sudo chown -R 1000:1000 "${_DEVICES_DIR_RI}"
      _DID16_RI="$(printf '%s' "${_PAIRED_RI}" | jq -r 'keys[0] // "?"' 2>/dev/null | cut -c1-16)"
      log "paired.json re-injected (#312/#314 merge; device=${_DID16_RI}… 控制面这条已 merge,盘上其它设备+运行时字段保留)"
      unset _DID16_RI
    fi
    unset _PAIRED_RI _DEVICES_DIR_RI _CUR_PAIRED _MERGED_RI
  fi

  # AgentCore Gateway MCP injection (if configured).
  #
  # OpenClaw 2026.5+ moved MCP servers from a top-level `mcpServers` key to
  # `mcp.servers.<name>` (verified against `openclaw mcp set` output;
  # `openclaw mcp list` reads from this same path). The old top-level
  # location and a brief intermediate `tools.mcpServers` location both
  # fail config validation now. We write through `.mcp.servers` to match
  # what the CLI itself uses.
  if [ -f /data/agentcore.env ]; then
    source /data/agentcore.env
    if [ -n "${AGENTCORE_GATEWAY_URL:-}" ]; then
      jq --arg url "$AGENTCORE_GATEWAY_URL" '
        (.mcp // {}) as $mcp |
        .mcp = ($mcp + {
          "servers": ((($mcp.servers // {})) + {
            "agentcore-gateway": {"url": $url, "transport": "streamable-http"}
          })
        })
      ' "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
      sudo chown 1000:1000 "${OC_JSON}"
      log "AgentCore Gateway MCP injected at .mcp.servers: ${AGENTCORE_GATEWAY_URL}"
    fi
  fi
fi
# 1.5.0 security: inject THIS host's public key so host-agent can SSH into
# the guest with key auth (no shared password). The key is per-host
# (init-host.sh generates it), so each VM trusts only its own host. uid/gid
# 1000 = the in-guest `agent` user that owns /home/agent (this data disk).
if [ -f /etc/openclaw/host_vm_key.pub ]; then
  sudo mkdir -p "${MOUNT_TMP}/.ssh"
  sudo cp /etc/openclaw/host_vm_key.pub "${MOUNT_TMP}/.ssh/authorized_keys"
  sudo chmod 700 "${MOUNT_TMP}/.ssh"
  sudo chmod 600 "${MOUNT_TMP}/.ssh/authorized_keys"
  sudo chown -R 1000:1000 "${MOUNT_TMP}/.ssh"
  log "injected host SSH public key into VM data disk"
fi
sudo umount ${MOUNT_TMP}
rmdir ${MOUNT_TMP} 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────
# #118/#116 + #149 — build the per-VM READ-ONLY credentials disk from the dotenv
# decrypted above (env-class only). Unified for both the new frozen-plan contract
# and the legacy injected_credentials path: whichever set _CREDS_ENV (a non-empty
# temp dotenv) gets a creds.ext4. Empty _CREDS_ENV (no env creds / no injection)
# → no disk, so tenants without injected creds stay byte-identical (guest
# openclaw-creds.service no-ops on ConditionPathExists=/dev/vde). Attached
# READ-ONLY as /dev/vde (marker file .clawcreds-marker); the guest binds it over
# ~/.openclaw/.env — plaintext never touched the data disk or openclaw.json.
if [ -n "${_CREDS_ENV:-}" ] && [ -s "${_CREDS_ENV}" ]; then
  # Build a small per-VM ext4 holding just the .env. The guest identifies it by a
  # marker file (below), not by label — the image has no udev/blkid so by-label
  # never populates. The label is cosmetic (aids host-side lsblk debugging).
  truncate -s 16M "${CREDS_VOL}"
  mkfs.ext4 -q -L clawcreds "${CREDS_VOL}"
  _CREDS_MNT="/tmp/creds-mount-${TENANT_ID}"
  mkdir -p "${_CREDS_MNT}"
  # #256 — 同 data 盘:入口幂等预清理上次强杀泄漏的挂载点。持锁后残留必是死进程,
  # plain umount(不用 -l)必成;busy 则 fail-loud 中止,绝不 lazy-detach 致双挂损坏。
  mountpoint -q "${_CREDS_MNT}" && sudo umount "${_CREDS_MNT}"
  sudo mount "${CREDS_VOL}" "${_CREDS_MNT}"
  sudo cp "${_CREDS_ENV}" "${_CREDS_MNT}/.env"
  sudo chmod 600 "${_CREDS_MNT}/.env"
  sudo chown 1000:1000 "${_CREDS_MNT}/.env"
  echo "clawcreds" | sudo tee "${_CREDS_MNT}/.clawcreds-marker" >/dev/null
  sudo umount "${_CREDS_MNT}"
  rmdir "${_CREDS_MNT}" 2>/dev/null || true
  # host-side plaintext lived only in this tmp file; shred it now.
  shred -u "${_CREDS_ENV}" 2>/dev/null || rm -f "${_CREDS_ENV}"
  log "injected credential(s) onto read-only creds disk (${CREDS_VOL})"
else
  CREDS_VOL=""
fi

# Network setup
log "setting up network tap=${TAP}..."
# 1.3.2: TUNSETIFF can transiently return EBUSY if a previous launch
# attempt left a tap-vmN partially set up — even after `ip link del`,
# the kernel briefly holds the name. Retry once after a short sleep.
_tuntap_add_with_retry() {
  if sudo ip tuntap add dev ${TAP} mode tap 2>/dev/null; then
    return 0
  fi
  log "tuntap add ${TAP} EBUSY, force-cleaning + retrying..."
  sudo ip link set ${TAP} down 2>/dev/null || true
  sudo ip link del ${TAP} 2>/dev/null || true
  # Kill anyone still holding a fd on this tap (rare, but covers a stale
  # firecracker that didn't get pkill'd by our trap).
  sudo lsof -t /sys/devices/virtual/net/${TAP} 2>/dev/null | xargs -r sudo kill -KILL 2>/dev/null || true
  sleep 2
  sudo ip tuntap add dev ${TAP} mode tap
}
_tuntap_add_with_retry
sudo ip addr add ${HOST_TAP_IP}/30 dev ${TAP}
sudo ip link set dev ${TAP} up
# ── SECURITY (#34: IMDSv6 拦截,per-tap disable_ipv6=1)──
# 老版本注释声称 IPv6 IMDS(fd00:ec2::254)"defensively covered",但仅有下面的
# IPv4 iptables DROP,ip6tables 全仓零命中,注释名实不符。真堵法:tap 上关掉
# IPv6 协议栈,fd00:ec2::254 与 fe80 一并消失,不依赖 ip6tables 存在。
# 幂等 + 无 ip6tables 依赖 + 单条命令收敛,与 launch-vm 其它 sysctl 风格一致。
# 深度防御另一半在 init-host.sh step1b(host 全局 net.ipv6.conf.all.forwarding=0)。
sudo sysctl -q -w net.ipv6.conf.${TAP}.disable_ipv6=1 2>/dev/null || true
HOST_IFACE=$(ip route show default | awk '{print $5}' | head -1)
sudo sysctl -q -w net.ipv4.ip_forward=1
# ── SECURITY (multi-tenant isolation): block guest → instance metadata ──
# Without this, a tenant inside its microVM can reach the host's IMDS at
# 169.254.169.254 through the MASQUERADE rule below and steal the host EC2
# instance-profile credentials (which can read/write the shared assets bucket
# and the tenants/hosts tables — i.e. every other tenant's data). Drop all
# guest-originated traffic to the link-local IMDS range BEFORE the ACCEPT
# rules. -I inserts at the top so it always precedes the FORWARD ACCEPT.
# IPv6 IMDS (fd00:ec2::254) is blocked by disabling IPv6 on the tap above,
# and host-side net.ipv6.conf.all.forwarding=0 (init-host.sh step1b) — no
# need for ip6tables since the guest has no IPv6 stack on its tap link.
sudo iptables -C FORWARD -i ${TAP} -d 169.254.169.254 -j DROP 2>/dev/null || \
  sudo iptables -I FORWARD 1 -i ${TAP} -d 169.254.169.254 -j DROP
sudo iptables -C FORWARD -i ${TAP} -d 169.254.169.253 -j DROP 2>/dev/null || \
  sudo iptables -I FORWARD 1 -i ${TAP} -d 169.254.169.253 -j DROP
# ── SECURITY (L2 east-west isolation): block guest → other tenants ──
# Each tenant gets its own /30 point-to-point link (SUBNET_PREFIX.<o3>.<base>/30)
# on its own tap; all links live inside the SUBNET_PREFIX/16 tenant supernet.
# Without this rule, the FORWARD ACCEPT below would happily route packets from
# this guest into ANOTHER tenant's /30 (same SUBNET_PREFIX/16, different tap) —
# routed by the host kernel, so the per-tap isolation is meaningless. Verified
# in load tests: with no DROP, cross-tenant ping = 0% loss and a neighbour's
# gateway:18789 returns 200. We DROP any guest-originated traffic destined to
# the whole tenant supernet (SUBNET_PREFIX.0.0/16) BEFORE the ACCEPT. Public
# egress is unaffected: those packets are NOT in SUBNET_PREFIX/16, so they skip
# this DROP and hit the MASQUERADE→${HOST_IFACE} path. -I keeps it above ACCEPT.
TENANT_SUPERNET="${SUBNET_PREFIX:-10.0}.0.0/16"
sudo iptables -C FORWARD -i ${TAP} -d ${TENANT_SUPERNET} -j DROP 2>/dev/null || \
  sudo iptables -I FORWARD 1 -i ${TAP} -d ${TENANT_SUPERNET} -j DROP
# ── SECURITY (management-plane isolation): block guest → host services ──
# The guest's default route points at its tap's host IP (HOST_TAP_IP), and the
# host runs control-plane services bound to 0.0.0.0: host-agent metrics/control
# on :8899 (and :9090 if enabled) and sshd on :22. A tenant must never reach
# these — host-agent on :8899 can drive Firecracker (balloon, lifecycle) and
# read other tenants' gateway tokens. Drop guest→host on those ports in INPUT
# (traffic to the host itself hits INPUT, not FORWARD). host-agent SSHes INTO
# the guest (host→guest, a NEW outbound conn from the host), so blocking
# guest→host:22 here does not affect host-agent's reverse management.
for _port in 8899 9090 22; do
  sudo iptables -C INPUT -i ${TAP} -p tcp --dport ${_port} -j DROP 2>/dev/null || \
    sudo iptables -I INPUT 1 -i ${TAP} -p tcp --dport ${_port} -j DROP
done
# NOTE: the IMDS DROP lives ONLY in the FORWARD chain (above). The nat table
# is for address translation, not filtering — nft rejects `-j DROP` in
# nat/PREROUTING ("the use of DROP is therefore inhibited"), which under
# `set -e` aborts VM launch entirely. The FORWARD DROP already blocks all
# guest→IMDS traffic before it can be MASQUERADEd, so a nat-table drop is
# both illegal and redundant.
sudo iptables -t nat -C POSTROUTING -o ${HOST_IFACE} -j MASQUERADE 2>/dev/null || \
  sudo iptables -t nat -A POSTROUTING -o ${HOST_IFACE} -j MASQUERADE
sudo iptables -C FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
  sudo iptables -A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
# ── SECURITY (#39: 出网默认拒绝白名单)──
# EGRESS_ALLOWLIST_ENABLED 从 /etc/platform.env(:54 source)取,由 config
# security.egress_allowlist_enabled 经 stack.py 渲染而来。默认 false → 保持历史
# 行为:无条件放行 guest→公网口(现状零变化)。true → 切默认拒绝:只放行 ①VPC CIDR +
# 自定义 CIDR(静态,覆盖 hub/LiteLLM/EKS ALB/VPC Endpoint 私网)②host dnsmasq 解析
# 内置 cognito-idp/s3 + 运营域名灌进 ipset oc_egress_allow 的真实 IP ③guest 的 :53
# 被透明 DNAT 到 host dnsmasq(硬编码 8.8.8.8 无需改),其余一律末尾 DROP 兜底。
# 上述 IMDS/租户超网/管理端口 DROP 都在链首(-I),先命中,白名单不误放。
if [ "${EGRESS_ALLOWLIST_ENABLED:-false}" = "true" ]; then
  # (1) 透明 DNS 劫持:guest 发往任意 DNS(含硬编码 8.8.8.8)的 :53 都 DNAT 到 host
  #     dnsmasq(HOST_TAP_IP:53)。UDP+TCP 都要(大响应/AXFR 走 TCP)。nat/PREROUTING
  #     的 DNAT 合法(-j DROP 才被 nft 禁);零 guest 改造破掉硬编码解析器。
  for _proto in udp tcp; do
    sudo iptables -t nat -C PREROUTING -i ${TAP} -p ${_proto} --dport 53 -j DNAT --to-destination ${HOST_TAP_IP}:53 2>/dev/null || \
      sudo iptables -t nat -I PREROUTING 1 -i ${TAP} -p ${_proto} --dport 53 -j DNAT --to-destination ${HOST_TAP_IP}:53
    # 显式放行 guest→host dnsmasq(INPUT)。当前 INPUT 默认 ACCEPT,显式写更 future-proof。
    sudo iptables -C INPUT -i ${TAP} -p ${_proto} -d ${HOST_TAP_IP} --dport 53 -j ACCEPT 2>/dev/null || \
      sudo iptables -I INPUT 1 -i ${TAP} -p ${_proto} -d ${HOST_TAP_IP} --dport 53 -j ACCEPT
  done
  # (2) 静态 CIDR 白名单:VPC CIDR(覆盖 hub/LiteLLM/EKS ALB/VPC Endpoint 私网)+ 运营 CIDR。
  _EGRESS_CIDRS=""
  [ "${EGRESS_INCLUDE_VPC_CIDR:-true}" = "true" ] && [ -n "${EGRESS_VPC_CIDR:-}" ] && _EGRESS_CIDRS="${EGRESS_VPC_CIDR}"
  _EGRESS_CIDRS="${_EGRESS_CIDRS} $(echo "${EGRESS_ALLOWLIST_CIDRS:-}" | tr ',' ' ')"
  # 白名单 ACCEPT 与末尾 DROP 都**不限出口网卡 -o**(按目的地放行/拒绝,不绑死主网卡)。
  # 为什么:末尾兜底若限 `-o ${HOST_IFACE}`,host 上出现第二条出网路径(第二 ENI/docker0/
  # VPN/策略路由)时,guest→该路径的流量既不命中链首 IMDS/超网 DROP、也不命中限主网卡的
  # 兜底 DROP、又无 ACCEPT → 落到 FORWARD 默认策略(常为 ACCEPT)fail-open 泄漏。去掉 -o 让
  # 兜底成为真正的 catch-all(guest→host 自身服务走 INPUT 链,不受 FORWARD 影响,不误伤
  # host-agent 反向 SSH)。ACCEPT 同去 -o,与 DROP 对称:白名单目标经任何路径都放行。
  for _cidr in ${_EGRESS_CIDRS}; do
    [ -z "${_cidr}" ] && continue
    sudo iptables -C FORWARD -i ${TAP} -d ${_cidr} -j ACCEPT 2>/dev/null || \
      sudo iptables -A FORWARD -i ${TAP} -d ${_cidr} -j ACCEPT
  done
  # (3) FQDN 白名单:dnsmasq 解析 cognito/s3/运营域名灌进共享 ipset 的真实 IP。ipset
  #     缺失(dnsmasq/ipset 没装成)时 -m set 规则加不上 → 跳过,退回只放静态 CIDR + DNS
  #     (fail-safe 不阻断 VM 启动;代价是 cognito/s3 出网被 DROP,真机验证前默认关兜住)。
  if sudo ipset list oc_egress_allow >/dev/null 2>&1; then
    sudo iptables -C FORWARD -i ${TAP} -m set --match-set oc_egress_allow dst -j ACCEPT 2>/dev/null || \
      sudo iptables -A FORWARD -i ${TAP} -m set --match-set oc_egress_allow dst -j ACCEPT
  else
    # ipset 缺失 = host 的 setup-egress-allowlist.sh 没跑成(S3 没下到/dnsmasq 没装),但本 VM
    # 的 gate 仍开 → cognito/s3 等 FQDN 目标会被下面的兜底 DROP 拦掉(fail-closed,不泄漏,但该
    # 租户 DNS/公网 AWS 端点不可用)。这是 host 基建与 VM 规则两半独立失败的可用性悬崖:告警落痕,
    # 便于 380 台里个别 host 脚本没下成时定位;运营应据此把该 host 视作 degraded 排查 dnsmasq。
    log "WARN: ipset oc_egress_allow MISSING — host setup-egress-allowlist.sh likely not run (dnsmasq down). FQDN allowlist skipped; cognito/s3 egress WILL be DROPPED for this VM. Treat host as degraded."
  fi
  # (4) 链尾兜底 catch-all DROP:默认拒绝该 guest 转发到任何目的地的其余一切(不限出口网卡,
  #     直连 IP 绕 DNS 白名单、以及经第二网卡/网桥的出网都被这条兜住)。
  sudo iptables -C FORWARD -i ${TAP} -j DROP 2>/dev/null || \
    sudo iptables -A FORWARD -i ${TAP} -j DROP
else
  # 默认(gate 关):现状零变化 —— 无条件放行 guest→公网口。
  sudo iptables -C FORWARD -i ${TAP} -o ${HOST_IFACE} -j ACCEPT 2>/dev/null || \
    sudo iptables -A FORWARD -i ${TAP} -o ${HOST_IFACE} -j ACCEPT
fi

# Start Firecracker
log "starting firecracker..."
# #331/#327 — 8>&- 关掉本 launcher 持的槽锁 fd,不让长命 disown 的 firecracker 继承它:否则
# launcher 退出后 FC 仍持槽 → host 封顶 N 个【常驻】VM(而非 N 个【并发启动】)。槽在 InstanceStart
# 后由 _oc_release_launch_slot 显式释放;这里只是确保 FC 子进程不继承。
nohup firecracker --api-sock ${SOCK} --log-path ${VM_DIR}/fc.log --level Info &>/dev/null 8>&- & disown
sleep 1

# Configure VM
# boot_args 安全加固(Firecracker prod-host-setup.md):
#   8250.nr_uarts=0 关闭 guest 8250 串口设备——guest 能借串口把数据灌进接到
#   firecracker stdout 的 host 侧,写爆 host 内存/磁盘(prod-host-setup.md:26-67)。
#   关串口后去掉 console=ttyS0(无串口则 console 无处输出)。host 侧调试不受影响:
#   fc.log 是 firecracker 自己的 --log-path,不依赖 guest console。
#   quiet loglevel=1 进一步压 guest 内核日志(也利于启动提速,console 日志拖慢 tap)。
curl -s --unix-socket ${SOCK} -X PUT http://localhost/boot-source \
  -H 'Content-Type: application/json' \
  -d '{"kernel_image_path":"/home/ubuntu/firecracker-assets/vmlinux","boot_args":"8250.nr_uarts=0 quiet loglevel=1 reboot=k panic=1 pci=off ro init=/sbin/overlay-init overlay_root=vdb ip='${GUEST_IP}'::'${HOST_TAP_IP}':255.255.255.252::eth0:off"}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/rootfs \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"rootfs","path_on_host":"'${ROOTFS}'","is_root_device":true,"is_read_only":true}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/overlay \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"overlay","path_on_host":"'${OVERLAY}'","is_root_device":false,"is_read_only":false}'

curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/data \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"data","path_on_host":"'${DATA_VOL}'","is_root_device":false,"is_read_only":false}'

# Fourth drive — the IMMUTABLE authority disk. MUST be PUT after data so the
# guest sees it as /dev/vdd (Firecracker assigns /dev/vd<N> in PUT order, root
# device pinned to vda — see firecracker issue #1750). is_read_only:true makes
# this a hardware-level write barrier: the virtio-block device refuses every
# guest write, so even root inside the VM gets EROFS on the bound identity files
# and ops skills. Skipped only if the asset isn't present (backward compatible).
if [ -f "${IMMUTABLE_TPL}" ]; then
  curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/immutable \
    -H 'Content-Type: application/json' \
    -d '{"drive_id":"immutable","path_on_host":"'${IMMUTABLE_TPL}'","is_root_device":false,"is_read_only":true}'
  log "attached read-only immutable disk /dev/vdd (${IMMUTABLE_TPL})"
else
  log "WARN: ${IMMUTABLE_TPL} absent — launching WITHOUT immutable authority disk"
fi

# Fifth drive — the per-VM READ-ONLY credentials disk (#118/#116). PUT after
# immutable so the guest sees it as /dev/vde. is_read_only:true = hardware write
# barrier (even guest root gets EROFS). Only attached when this tenant had
# injected_credentials (CREDS_VOL set above); otherwise skipped so tenants
# without injected creds are byte-identical (guest openclaw-creds.service
# no-ops on ConditionPathExists=/dev/vde). Holds only the decrypted dotenv;
# plaintext never touched the data disk or openclaw.json.
if [ -n "${CREDS_VOL}" ] && [ -f "${CREDS_VOL}" ]; then
  curl -s --unix-socket ${SOCK} -X PUT http://localhost/drives/creds \
    -H 'Content-Type: application/json' \
    -d '{"drive_id":"creds","path_on_host":"'${CREDS_VOL}'","is_root_device":false,"is_read_only":true}'
  log "attached read-only credentials disk /dev/vde (${CREDS_VOL})"
fi

curl -s --unix-socket ${SOCK} -X PUT http://localhost/machine-config \
  -H 'Content-Type: application/json' \
  -d '{"vcpu_count":'${VCPU}',"mem_size_mib":'${MEM_MB}'}'

# guest 日志采集通道(vsock)——默认关,OC_GUEST_LOG_ENABLED=true 才配。
# 必须在 InstanceStart 之前 PUT(vsock 不能热挂,真机实测)。这是所有生命周期
# 路径(create/restart/restore/recover)的单一 boot 收敛点 → 一处覆盖全部。
# guest_cid 固定 3:每个 VM 是独立 firecracker 进程 + 独立 UDS,CID 只在单 VM
# vsock 命名空间内有意义,不跨 VM(两租户真机实测 cid=3 无冲突)。guest forwarder
# connect(HOST_CID=2, port) → Firecracker 落到 ${VM_DIR}/vsock.sock_<port>,
# host vsock reader 在那 listen(guest 只 connect 不 bind,不开入站面)。
# 两级开关都开才挂 vsock:① host 级 OC_GUEST_LOG_ENABLED,默认 true(采集默认开)
# ② per-tenant DDB flag guest_log_enabled(强一致读)。语义(codex 复审):
#   - 显式 false → 关(不能用 jq `.BOOL // true`:`//` 对布尔 false 也兜底成 true,吞掉显式关)
#   - 属性缺失/true(调用成功但没设或设 true)→ 开(功能测试阶段默认收)
#   - DDB 调用失败(网络/IAM/限流)→ 【不确定】,对含会话的敏感采集 fail-CLOSED 不挂 vsock
#     (与只读 fail-open 相反:读不到 flag 时宁可不采,也不冒险把敏感会话重新抽出去)。
if [ "${OC_GUEST_LOG_ENABLED:-true}" != "false" ]; then
  if _GLOG_RAW="$(aws dynamodb get-item \
    --table-name "${TENANTS_TABLE:-openclaw-tenants}" \
    --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
    --projection-expression 'guest_log_enabled' \
    --consistent-read \
    --region "${OC_REGION:-ap-northeast-1}" \
    --output json 2>/dev/null)"; then
    # 调用成功:精确三态,严格类型校验(codex 三次抓的同源坑)。绝不用 `.BOOL // x`
    # (`//` 对布尔 false 也兜底)。要求 guest_log_enabled 字段【存在且确实是 BOOL 类型】
    # 才取值(true/false);字段缺失=unset(默认开);类型不对(存了 S/N 等)=当不确定,
    # 归 read_failed 走 fail-closed,不能取到 null 被当"开"。
    # 先判 .Item 整体:DDB get-item 未命中返回 {}(无 Item 键)→ 租户记录不存在,是异常
    # 状态,当 read_failed 走 fail-closed(codex:不能把'整个 Item 不存在'当字段 unset 开)。
    # Item 存在再看字段:缺失=unset(默认开);是 BOOL=取值;其他类型=read_failed。
    _GLOG_TENANT_FLAG="$(printf '%s' "${_GLOG_RAW}" | jq -r '
      if (has("Item") | not) then "read_failed"
      else (.Item.guest_log_enabled) as $f
        | if $f == null then "unset"
          elif ($f | type) == "object" and ($f | has("BOOL")) then ($f.BOOL | tostring)
          else "read_failed" end
      end' 2>/dev/null || echo read_failed)"
  else
    _GLOG_TENANT_FLAG="read_failed"  # DDB 读失败:fail-closed 不挂 vsock
  fi
  if [ "${_GLOG_TENANT_FLAG}" != "false" ] && [ "${_GLOG_TENANT_FLAG}" != "read_failed" ]; then
    # #vsock PUT 必须检查 HTTP 结果:失败不能假成功打印 attached(codex)。--fail-with-body
    # 让 4xx/5xx 返非零,--max-time 防挂死。失败 → 记 degraded,不打印 attached。
    if _VSOCK_RES="$(curl -s --fail-with-body --max-time 5 --unix-socket ${SOCK} \
        -X PUT http://localhost/vsock -H 'Content-Type: application/json' \
        -d '{"guest_cid":3,"uds_path":"'${VM_DIR}/vsock.sock'"}' 2>&1)"; then
      log "guest-log vsock attached uds=${VM_DIR}/vsock.sock (default-on; both flags not false)"
    else
      log "WARN: guest-log vsock PUT failed (degraded, 采集不启用): ${_VSOCK_RES}"
    fi
  else
    log "guest-log skipped: per-tenant guest_log_enabled=${_GLOG_TENANT_FLAG}"
  fi
fi

curl -s --unix-socket ${SOCK} -X PUT http://localhost/network-interfaces/eth0 \
  -H 'Content-Type: application/json' \
  -d '{"iface_id":"eth0","guest_mac":"'${GUEST_MAC}'","host_dev_name":"'${TAP}'"}'

# Balloon device for memory overcommit (configured via /etc/platform.env or defaults)
BALLOON_ENABLED="${BALLOON_ENABLED:-false}"
if [ "${BALLOON_ENABLED}" = "true" ]; then
  BALLOON_DEFLATE_ON_OOM="${BALLOON_DEFLATE_ON_OOM:-true}"
  BALLOON_STATS_INTERVAL="${BALLOON_STATS_INTERVAL:-5}"
  BALLOON_FREE_PAGE_REPORTING="${BALLOON_FREE_PAGE_REPORTING:-true}"
  curl -s --unix-socket ${SOCK} -X PUT http://localhost/balloon \
    -H 'Content-Type: application/json' \
    -d '{"amount_mib":0,"deflate_on_oom":'${BALLOON_DEFLATE_ON_OOM}',"stats_polling_interval_s":'${BALLOON_STATS_INTERVAL}',"free_page_reporting":'${BALLOON_FREE_PAGE_REPORTING}'}'
  log "balloon configured: deflate_on_oom=${BALLOON_DEFLATE_ON_OOM} stats=${BALLOON_STATS_INTERVAL}s free_page_reporting=${BALLOON_FREE_PAGE_REPORTING}"
fi

RESULT=$(curl -s --unix-socket ${SOCK} -X PUT http://localhost/actions \
  -H 'Content-Type: application/json' -d '{"action_type":"InstanceStart"}')
[ -n "${RESULT}" ] && log "ERROR: ${RESULT}" && exit 1
log "InstanceStart succeeded — VM is now booting"
# #331/#327 — 冷启动重活(mkfs/cp/解压/firecracker boot)到此结束,VM 已自持运行 →
# 立刻释放 host 级启动槽,让排队的下一个 launch 进来。之后的 nginx/ssh 收尾不占启动并发额度。
_oc_release_launch_slot
# 1.3.2: Past this point the VM is genuinely running. Any later step
# failing (nginx reload race, ssh-keygen leftovers, etc) shouldn't
# tear down a working VM. Disable strict mode + clear ERR trap so the
# script always reaches the DONE log even if nginx's reload returns
# non-zero on a transient race.
set +e
# 清 ERR **和** EXIT(与上面 `trap ... ERR EXIT` 配套):InstanceStart 已成功、VM 在跑,
# 此后 nginx reload/ssh-keygen 等失败不该拆 VM,也不该让 EXIT trap 在脚本正常结束时误触发
# _oc_cleanup_on_err。只清 ERR 会漏掉 EXIT,导致成功路径走到结尾自然退出时仍进 cleanup。
trap - ERR EXIT
ssh-keygen -R ${GUEST_IP} 2>/dev/null || true

# Nginx reverse proxy for this tenant. Two paths:
#   /vm/{tenant}/    -> gateway :18789  (control UI / dashboard, token-auth)
#   /chat/{tenant}/  -> claw-channel signed webhook :18790  (C-end user messages,
#                       HMAC-signed, Cognito-sub bound — replaces the bare
#                       /v1/chat/completions endpoint). The webhook itself rejects
#                       any unsigned request with 401, so this path is safe to
#                       expose through the same CloudFront->ALB origin.
sudo tee /etc/nginx/conf.d/tenants/${TENANT_ID}.conf > /dev/null <<EOF
location ~ ^/vm/${TENANT_ID}(/.*)?$ {
    proxy_pass http://${GUEST_IP}:18789\$1;
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
location ~ ^/chat/${TENANT_ID}(/.*)?$ {
    proxy_pass http://${GUEST_IP}:18790\$1;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
}
EOF
sudo nginx -s reload 2>/dev/null || true

log "DONE ${TENANT_ID} IP:${GUEST_IP} (total $((SECONDS))s)"
