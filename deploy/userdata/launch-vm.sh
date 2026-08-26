#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

set -euo pipefail
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
                          e:(if .chat_ep.BOOL then 1 else 0 end),
                          q:(.capacity_reservation_id.S // "")}
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
    local reservation_id="${9:-}"
    local vm_path="${FAN_VM_DIR}/${tid}"
    # Idempotent completion requires both discovery metadata and a live Firecracker
    # process for this exact socket. vm.json is written before restore/config/network
    # setup finishes, so the file alone can describe a launch that later failed.
    # In that case re-enter launch-vm.sh; its per-tenant flock serializes us with any
    # launch still in progress and host-agent uses the same process-level truth.
    if [ -f "${vm_path}/vm.json" ] &&
       pgrep -f "api-sock ${vm_path}/fc.sock" >/dev/null 2>&1; then
      return 42  # sentinel: skipped
    fi
    # 13 positional args mirror the launch-vm.sh signature: config_template=$5,
    # config_template($5) 走 DDB tenants 记录(host-agent/launch-vm 自取),此处
    # 空;restore_key($6) 从 assignment/manifest 透传。Empty 7-9 keep defaults.
    # Secrets like vkey/channel_secret are still pulled from DDB by launch-vm.
    # 故落 dispatcher 后的单 VM 主体,不会递归回 fan_out_main。
    # Fluent Bit host.platform → AOS claw-logs-host,不再被 /dev/null 吞掉。fan-out
    # 批量创建卡住时,console Logs viewer 才查得到某租户为何没起来。
    systemd-cat -t claw-launch \
      bash "$0" "${tid}" "${vm_num}" "${vcpu}" "${mem_mb}" \
      "" "${restore_key}" "" "" "" "${chat_ep}" "" "${gw_token_ct}" \
      "${device_paired}" "${reservation_id}"
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
    local line="$1" rfile="$2"
    local tid vm_num vcpu mem_mb chat_ep gw_token_ct device_paired restore_key
    local reservation_id
    tid=$(jq -r '.t // empty' <<<"${line}" 2>/dev/null)
    vm_num=$(jq -r '.n // empty' <<<"${line}" 2>/dev/null)
    vcpu=$(jq -r '.c // 2' <<<"${line}" 2>/dev/null)
    mem_mb=$(jq -r '.m // 2048' <<<"${line}" 2>/dev/null)
    chat_ep=$(jq -r '.e // 0' <<<"${line}" 2>/dev/null)
    gw_token_ct=$(jq -r '.g // empty' <<<"${line}" 2>/dev/null)
    device_paired=$(jq -r '.d // empty' <<<"${line}" 2>/dev/null)
    restore_key=$(jq -r '.r // empty' <<<"${line}" 2>/dev/null)
    reservation_id=$(jq -r '.q // empty' <<<"${line}" 2>/dev/null)
    if [ -z "${tid}" ] || [ -z "${vm_num}" ]; then
      echo "failed" > "${rfile}"
      _fo_log "parse-error line=${line}"
      return
    fi
    _launch_one "${tid}" "${vm_num}" "${vcpu}" "${mem_mb}" "${chat_ep}" \
      "${gw_token_ct}" "${device_paired}" "${restore_key}" "${reservation_id}"
    rc=$?
    printf '%s' "${tid}" > "${rfile}.tid"  # v2 per-tenant report (see stdout JSON)
    if [ "${rc}" -eq 0 ]; then
      echo "launched" > "${rfile}"
      [ "${FROM_DDB}" -eq 1 ] && _mark_assignment "${tid}" "done"
    elif [ "${rc}" -eq 42 ]; then
      echo "skipped" > "${rfile}"
      [ "${FROM_DDB}" -eq 1 ] && _mark_assignment "${tid}" "done"
    elif [ "${rc}" -eq 44 ]; then
      # 马上要删的 VM。标 assignment done 让它从 pending 过滤掉(停止重捞),写独立哨兵
      echo "aborted" > "${rfile}"
      [ "${FROM_DDB}" -eq 1 ] && _mark_assignment "${tid}" "done"
      _fo_log "launch abort(deleted) tenant=${tid} rc=44 — 租户已删,拒起并停重投"
    elif [ "${rc}" -eq 45 ]; then
      # 会把状态回滚到 creating/running。绝不标 assignment done —— 那会让回滚后的 creating
      # 租户永久无 VM 无 pending。写 inprogress 哨兵(同 flock-skip rc75 语义):不进
      # launched/skipped/failed 清单、不触发批回滚、保持 pending → 下轮重投重判(读到
      # deleted 走 44 终结,读到 creating 正常起)。
      echo "inprogress" > "${rfile}"
      _fo_log "launch defer(deleting) tenant=${tid} rc=45 — 删除在途,保持 pending 待重投重判"
    elif [ "${rc}" -eq 75 ]; then
      # 关键:绝不 _mark_assignment done。持锁 winner 可能在 Firecracker 真正启动前失败;
      # 即使 vm.json 已写也不构成完成证据。若这里标 done,assignment 会从 pending 过滤掉,
      # 只能依赖 host-agent 的较慢本地恢复,控制面则已错误声称创建完成。
      # 写 inprogress → 不进 v2 JSON 的 launched/skipped/failed 三清单(_result_list 只精确
      # 匹配那三个)→ Poller 看不到 → assignment 保持 pending → 下一轮 dispatch 重新 pick。
      echo "inprogress" > "${rfile}"
      _fo_log "launch skip(flock held) tenant=${tid} rc=75 — 保持 pending 待重投"
    else
      echo "failed" > "${rfile}"
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
        aborted)    ;;  # #411/6.3 status 闸拒起(deleted/deleting)= 终态且【非失败】。
                        # 兄弟被 Poller 判 partial 回滚/重投。assignment 已由 caller 标 done。
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

# single-VM launch body, so it cannot acquire launch locks, stop the VM, touch
# the tenant overlay, or enter the live tenant disk mount window.
_oc_pre_rebuild_probe() (
  local _pr_tenant="${1:?missing tenant_id}"
  local _pr_vm_num="${2:?missing vm_num}"
  local _pr_snapshot="${3:-__legacy_flat__}"
  local _pr_assets="${OC_ASSET_ROOT:-/data/firecracker-assets}"
  local _pr_work _pr_root_mnt _pr_lower_mnt _pr_overlay_mnt _pr_tpl_mnt _pr_stage
  local _pr_rootfs _pr_data_tpl _pr_binding _pr_template _pr_current
  local _pr_creds _pr_plan _pr_scheme _pr_owner _pr_version _pr_vid _pr_sha
  local _pr_raw _pr_plan_raw _pr_guest_ip _pr_port="" _pr_rc=1
  local _pr_path_kind _pr_path_b64 _pr_path _pr_parent

  [ -f /etc/platform.env ] && source /etc/platform.env
  if [ -z "${OC_REAPPLY_BINDING_B64:-}" ]; then
    echo '{"state":"INCOMPATIBLE","reason":"missing reapply binding"}'
    echo "openclaw.json 不兼容" >&2
    return 1
  fi
  _pr_binding="$(printf '%s' "${OC_REAPPLY_BINDING_B64}" | base64 -d 2>/dev/null || true)"
  if ! printf '%s' "${_pr_binding}" | jq -e 'type == "object"' >/dev/null 2>&1; then
    echo '{"state":"INCOMPATIBLE","reason":"invalid reapply binding"}'
    echo "openclaw.json 不兼容" >&2
    return 1
  fi

  if [ "${_pr_snapshot}" = "__legacy_flat__" ] || [ -z "${_pr_snapshot}" ]; then
    _pr_rootfs="${_pr_assets}/openclaw-rootfs.ext4"
    _pr_data_tpl="${_pr_assets}/openclaw-data-template.ext4"
  else
    _pr_rootfs="${_pr_assets}/versions/${_pr_snapshot}/openclaw-rootfs.ext4"
    _pr_data_tpl="${_pr_assets}/versions/${_pr_snapshot}/openclaw-data-template.ext4"
  fi
  if [ ! -s "${_pr_rootfs}" ] || [ ! -s "${_pr_data_tpl}" ]; then
    echo '{"state":"INCOMPATIBLE","reason":"target image not pulled"}'
    echo "openclaw.json 不兼容" >&2
    return 1
  fi

  _pr_work="$(mktemp -d "/tmp/oc-reapply-probe-${_pr_tenant}.XXXXXX")"
  _pr_root_mnt="${_pr_work}/root"
  _pr_lower_mnt="${_pr_work}/lower"
  _pr_overlay_mnt="${_pr_work}/overlay"
  _pr_tpl_mnt="${_pr_work}/template"
  _pr_stage="${_pr_work}/stage"
  mkdir -p \
    "${_pr_root_mnt}" "${_pr_lower_mnt}" "${_pr_overlay_mnt}" \
    "${_pr_tpl_mnt}" "${_pr_stage}"
  _pr_template="${_pr_work}/template.json"
  _pr_current="${_pr_work}/current.json"
  _pr_creds="${_pr_work}/credentials.json"

  _oc_probe_cleanup() {
    # briefly keep the overlay busy. Kill it hard, kill any mount users, then LAZY-unmount
    # (detaches even if busy) so cleanup never fails and never masks the validation result.
    if [ -n "${_pr_port}" ]; then
      pkill -f "openclaw gateway.*--port ${_pr_port}" 2>/dev/null || true
      sleep 1
      pkill -9 -f "openclaw gateway.*--port ${_pr_port}" 2>/dev/null || true
    fi
    sudo fuser -km "${_pr_root_mnt}" 2>/dev/null || true
    mountpoint -q "${_pr_root_mnt}/tmp" 2>/dev/null && sudo umount -l "${_pr_root_mnt}/tmp" 2>/dev/null || true
    mountpoint -q "${_pr_root_mnt}/proc" 2>/dev/null && sudo umount -l "${_pr_root_mnt}/proc" 2>/dev/null || true
    mountpoint -q "${_pr_root_mnt}/dev" 2>/dev/null && sudo umount -Rl "${_pr_root_mnt}/dev" 2>/dev/null || true
    mountpoint -q "${_pr_root_mnt}" 2>/dev/null && sudo umount -l "${_pr_root_mnt}" 2>/dev/null || true
    mountpoint -q "${_pr_overlay_mnt}" 2>/dev/null && sudo umount -l "${_pr_overlay_mnt}" 2>/dev/null || true
    mountpoint -q "${_pr_lower_mnt}" 2>/dev/null && sudo umount -l "${_pr_lower_mnt}" 2>/dev/null || true
    mountpoint -q "${_pr_tpl_mnt}" 2>/dev/null && sudo umount -l "${_pr_tpl_mnt}" 2>/dev/null || true
    rm -rf "${_pr_work}" 2>/dev/null || true
  }
  trap _oc_probe_cleanup EXIT

  # PULL:the selected image pair must already be present; mount target rootfs RO
  # as the overlay lowerdir so validation-only writes never reach the image.
  sudo mount -o ro,noload "${_pr_rootfs}" "${_pr_lower_mnt}"
  sudo mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs "${_pr_overlay_mnt}"
  sudo mkdir -p "${_pr_overlay_mnt}/upper" "${_pr_overlay_mnt}/work"
  if ! sudo mount -t overlay overlay \
      -o "lowerdir=${_pr_lower_mnt},upperdir=${_pr_overlay_mnt}/upper,workdir=${_pr_overlay_mnt}/work" \
      "${_pr_root_mnt}"; then
    echo '{"state":"PROBE_FAILED","reason":"validation writable layer unavailable"}'
    return 1
  fi

  _pr_vid="$(printf '%s' "${_pr_binding}" | jq -r '.body_version_id // ""')"
  _pr_sha="$(printf '%s' "${_pr_binding}" | jq -r '.body_sha256 // ""')"
  _pr_version="$(printf '%s' "${_pr_binding}" | jq -r '.target_openclaw_version // ""')"
  if [ "$(printf '%s' "${_pr_binding}" | jq -r '.host_baked // false')" = "true" ]; then
    sudo mount -o ro,noload "${_pr_data_tpl}" "${_pr_tpl_mnt}"
    sudo cp "${_pr_tpl_mnt}/.openclaw/openclaw.json" "${_pr_template}"
  else
    _pr_name="$(printf '%s' "${_pr_binding}" | jq -r '.config_template // ""')"
    if [ -z "${ASSETS_BUCKET:-}" ] || [ -z "${_pr_name}" ] || [ -z "${_pr_vid}" ]; then
      echo '{"state":"INCOMPATIBLE","reason":"named template binding incomplete"}'
      echo "openclaw.json 不兼容" >&2
      return 1
    fi
    aws s3api get-object \
      --bucket "${ASSETS_BUCKET}" \
      --key "templates/openclaw/${_pr_name}/openclaw.json" \
      --version-id "${_pr_vid}" \
      "${_pr_template}" \
      --region "${OC_REGION:-ap-northeast-1}" >/dev/null
    if [ -n "${_pr_sha}" ] &&
       [ "$(sha256sum "${_pr_template}" | cut -d' ' -f1)" != "${_pr_sha}" ]; then
      echo '{"state":"INCOMPATIBLE","reason":"template body hash mismatch"}'
      echo "openclaw.json 不兼容" >&2
      return 1
    fi
  fi

  jq -n \
    --arg token "__OC_SCHEMA_GATEWAY_TOKEN__" \
    --arg vkey "__OC_SCHEMA_LITELLM_VKEY__" \
    '{gateway_token:$token,litellm_vkey:$vkey}' > "${_pr_creds}"

  if ! _pr_raw="$(aws dynamodb get-item \
      --table-name "${TENANTS_TABLE:-openclaw-tenants}" \
      --key "{\"id\":{\"S\":\"${_pr_tenant}\"}}" \
      --projection-expression 'frozen_injection_plan, owner_id, scheme, guest_ip' \
      --consistent-read \
      --region "${OC_REGION:-ap-northeast-1}" \
      --output json 2>/dev/null)"; then
    echo '{"state":"INCOMPATIBLE","reason":"cannot read frozen injection plan"}'
    echo "openclaw.json 不兼容" >&2
    return 1
  fi
  _pr_owner="$(printf '%s' "${_pr_raw}" | jq -r '.Item.owner_id.S // ""')"
  _pr_scheme="$(printf '%s' "${_pr_raw}" | jq -r '.Item.scheme.S // "kms-cmk"')"
  _pr_guest_ip="$(printf '%s' "${_pr_raw}" | jq -r '.Item.guest_ip.S // ""')"
  # Reapply currently requires a running tenant whose guest is SSH-reachable.
  # TODO: for a stopped tenant, read the merge-base config from its data disk.
  if [ -z "${_pr_guest_ip}" ] || [ ! -f /etc/openclaw/host_vm_key ]; then
    echo '{"state":"PROBE_FAILED","reason":"running guest config is unreadable"}'
    return 1
  fi
  if ! timeout 20 ssh \
      -i /etc/openclaw/host_vm_key \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=5 \
      -o LogLevel=ERROR \
      -o BatchMode=yes \
      "agent@${_pr_guest_ip}" \
      'cat /home/agent/.openclaw/openclaw.json' 2>/dev/null \
    | jq \
        --arg token "__OC_SCHEMA_GATEWAY_TOKEN__" \
        --arg vkey "__OC_SCHEMA_LITELLM_VKEY__" '
          setpath(["gateway", "auth", "token"]; $token)
          | setpath(["models", "providers", "litellm", "apiKey"]; $vkey)
        ' > "${_pr_current}"; then
    echo '{"state":"PROBE_FAILED","reason":"running guest config read failed"}'
    return 1
  fi
  _pr_plan_raw="$(printf '%s' "${_pr_raw}" | jq -c '.Item.frozen_injection_plan.M // empty' 2>/dev/null || true)"
  _pr_plan=""
  if [ -n "${_pr_plan_raw}" ] && [ "${_pr_plan_raw}" != "null" ]; then
    _pr_plan="$(printf '%s' "${_pr_raw}" | jq -c '
      [.Item.frozen_injection_plan.M | to_entries[] |
       {(.key): {
         param_class: .value.M.param_class.S,
         injection_target: .value.M.injection_target.S,
         sensitive: (.value.M.sensitive.BOOL // false),
         mode: .value.M.mode.S,
         value_ref: (.value.M.value_ref.S // ""),
         empty_fallback: (.value.M.empty_fallback.S // "")
       }}] | add // {}' 2>/dev/null || true)"
  fi

  if [ -r /home/ubuntu/lib/harden-config.sh ]; then
    # shellcheck disable=SC1091
    . /home/ubuntu/lib/harden-config.sh
  else
    echo '{"state":"INCOMPATIBLE","reason":"harden-config library missing"}'
    echo "openclaw.json 不兼容" >&2
    return 1
  fi
  _pr_baseurl="$(oc_normalize_litellm_baseurl "${LITELLM_HOST:-}")"
  # 探针的 origin-policy.json 只是 staging 产物,会随 staging 目录刻意丢弃；它只做兼容性检查,不收敛 live tenant,绝不复制到数据盘。
  if ! oc_assemble_config \
      "${_pr_current}" "${_pr_template}" "${_pr_stage}/openclaw.json" \
      "${_pr_plan}" "$(cat "${_pr_creds}")" "${_pr_scheme}" "${_pr_owner}" \
      "${OC_REGION:-ap-northeast-1}" "${CLOUDFRONT_ORIGIN:-}" "${_pr_baseurl}" \
      "__OC_SCHEMA_LITELLM_VKEY__" "0" "${CLAWPOOL_RSA_CMK_ARN:-}"; then
    echo '{"state":"INCOMPATIBLE","reason":"assembly failed"}'
    echo "openclaw.json 不兼容" >&2
    return 1
  fi

  _oc_probe_collect_paths() {
    jq -r '
      [
        (.plugins.load.paths[]? | {kind:"dir", path:.}),
        (.mcpServers // {} | .[]? | .command? | {kind:"parent", path:.}),
        (.mcpServers // {} | .[]? | .args[]? | {kind:"dir", path:.}),
        (.mcp.servers // {} | .[]? | .command? | {kind:"parent", path:.}),
        (.mcp.servers // {} | .[]? | .args[]? | {kind:"dir", path:.}),
        (paths(strings) as $p
          | ($p | map(select(type == "string") | ascii_downcase)) as $keys
          | select(
              ($keys | any(test("hook|skill")))
              and ($keys | any(test("^(path|paths|dir|dirs|directory|directories)$")))
            )
          | {kind:"dir", path:getpath($p)}),
        (paths(strings) as $p
          | ($p | map(select(type == "string") | ascii_downcase)) as $keys
          | select(
              ($keys | any(. == "tls"))
              and ($keys | any(test("^(certpath|keypath)$")))
            )
          | {kind:"file", path:getpath($p)}),
        (paths(strings) as $p
          | ($p | map(select(type == "string") | ascii_downcase)) as $keys
          | select(
              ($keys | any(test("^state(dir|directory|path)$")))
              or (
                ($keys | any(. == "state"))
                and ($keys | any(test("^(path|dir|directory)$")))
              )
            )
          | {kind:"dir", path:getpath($p)})
      ]
      | map(select(
          (.path | type) == "string"
          and (.path | startswith("/"))
          and .path != "/"
        ))
      | unique_by([.kind, .path])[]
      | [.kind, (.path | @base64)]
      | @tsv
    ' "$1"
  }

  sudo chroot "${_pr_root_mnt}" /bin/mkdir -p -- /tmp /tmp/state /tmp/home
  sudo chroot "${_pr_root_mnt}" /bin/chmod 1777 /tmp
  sudo cp "${_pr_stage}/openclaw.json" "${_pr_root_mnt}/tmp/openclaw.json"
  if ! _oc_probe_collect_paths "${_pr_stage}/openclaw.json" > "${_pr_work}/paths.tsv"; then
    echo '{"state":"PROBE_FAILED","reason":"validation path preparation failed"}'
    return 1
  fi
  while IFS=$'\t' read -r _pr_path_kind _pr_path_b64; do
    [ -n "${_pr_path_b64}" ] || continue
    _pr_path="$(printf '%s' "${_pr_path_b64}" | base64 -d)"
    case "${_pr_path_kind}" in
      dir)
        sudo chroot "${_pr_root_mnt}" /bin/mkdir -p -- "${_pr_path}"
        ;;
      parent|file)
        _pr_parent="${_pr_path%/*}"
        [ -n "${_pr_parent}" ] || _pr_parent="/"
        sudo chroot "${_pr_root_mnt}" /bin/mkdir -p -- "${_pr_parent}"
        if [ "${_pr_path_kind}" = "file" ]; then
          sudo chroot "${_pr_root_mnt}" /usr/bin/touch -- "${_pr_path}"
        fi
        ;;
    esac
  done < "${_pr_work}/paths.tsv"

  sudo mount -t proc proc "${_pr_root_mnt}/proc"
  sudo mount --rbind /dev "${_pr_root_mnt}/dev"
  if [ "${_pr_version}" = "2026.2.26" ]; then
    _pr_port=$((24000 + (($$ + _pr_vm_num) % 12000)))
    while ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${_pr_port}$"; do
      _pr_port=$((_pr_port + 1))
    done
    set +e
    timeout --signal=TERM --kill-after=2s 8s \
      sudo chroot "${_pr_root_mnt}" /bin/sh -c \
      "OPENCLAW_CONFIG_PATH=/tmp/openclaw.json OPENCLAW_STATE_DIR=/tmp/state HOME=/tmp/home TMPDIR=/tmp openclaw gateway --allow-unconfigured --port ${_pr_port}" \
      >/dev/null 2>"${_pr_work}/validator.err"
    _pr_rc=$?
    set -e
    [ "${_pr_rc}" -eq 124 ] && _pr_rc=0
  else
    set +e
    sudo chroot "${_pr_root_mnt}" /bin/sh -c \
      'OPENCLAW_CONFIG_PATH=/tmp/openclaw.json OPENCLAW_STATE_DIR=/tmp/state HOME=/tmp/home TMPDIR=/tmp openclaw config validate' \
      >/dev/null 2>"${_pr_work}/validator.err"
    _pr_rc=$?
    set -e
  fi
  if [ "${_pr_rc}" -ne 0 ]; then
    printf '{"state":"INCOMPATIBLE","reason":"target validator rejected config","rc":%s}\n' "${_pr_rc}"
    echo "openclaw.json 不兼容" >&2
    return 1
  fi
  printf '{"state":"COMPATIBLE","tenant_id":"%s","registry_version":%s,"body_version_id":"%s","body_sha256":"%s"}\n' \
    "${_pr_tenant}" \
    "$(printf '%s' "${_pr_binding}" | jq -r '.registry_version')" \
    "${_pr_vid}" "${_pr_sha}"
  return 0
)
# OC_REAPPLY_PROBE_END

case "${1:-}" in
  --manifest|--from-ddb)
    fan_out_main "$@"
    exit $?
    ;;
  --pre-rebuild-probe)
    shift
    _oc_pre_rebuild_probe "$@"
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
# preserves the legacy v1.3.x broadcast behavior so old SSM commands
# without this 7th arg keep working unchanged.
SCOPED_SKILLS="${7:-}"
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
# Cognito 渠道机器用户 base64)。channel/hub 数据面已下线,数据面走两级路由直连
# microVM:18789 gateway。参数位保留以维持 12 位对齐,取值不再使用。
INJECTED_COGNITO_B64="${11:-}"
# ciphertext of the pre-minted gateway token (tenant_id EncryptionContext, ClawPool
# CMK). Empty (legacy SSM commands / feature off) → keep the openssl-generated
# in-VM token. Non-empty → we `aws kms decrypt` here on the host (has kms:Decrypt
# on the ClawPool CMK), inject the plaintext as `.gateway.auth.token`, replacing
# the openssl one. This closes the "control plane can't reveal the gateway token"
# gap that hub → gateway direct-connect (11-ENGINE-TRANSFORM) needs. Reveal window
# is enforced control-side (openclaw-tenant-secrets TTL 15min); this side is just
# the injection step.
INJECTED_GATEWAY_TOKEN_CT="${12:-}"
# device: deviceId + publicKey + roles + scopes, tokens:{} for 2026.2.26). The
# control plane mints the device at create_tenant, base64-encodes the paired.json
# object, and passes it here. We base64-decode it and write it to the data disk's
# <stateDir>/devices/paired.json so a remote WSS client (JDWS) preloaded with the
# matching device identity connects to the in-VM gateway with NO manual approve.
# Empty (legacy SSM commands / feature off / owner unknown / CMK off) → skip the
INJECTED_DEVICE_PAIRED_B64="${13:-}"
# Before touching disks or Firecracker, the status gate below verifies that the
# tenant still owns this exact reservation, host and vm_num. A stale assignment
# whose reservation was rolled back therefore exits 45 instead of launching an
# unaccounted VM.
EXPECTED_RESERVATION_ID="${14:-}"
# commit point and passes it here on retries. This prevents a live-slot advance
# between drop and launch from silently changing the operation's target.
# `__legacy_flat__` explicitly pins the pre-slots flat layout.
REBUILD_IMAGE_SNAPSHOT="${15:-}"
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
[ "${EXPECTED_RESERVATION_ID}" = '""' ] && EXPECTED_RESERVATION_ID=""
[ "${REBUILD_IMAGE_SNAPSHOT}" = '""' ] && REBUILD_IMAGE_SNAPSHOT=""
VM_DIR="/data/firecracker-vms/${TENANT_ID}"
[ -f /etc/platform.env ] && source /etc/platform.env
# 现实触发源(新加坡真机 5 轮坐实):dispatch 已认领 host(host_id 已写)但 VM 未 launch
# 的窗口内 DELETE → tenant_service 把 status 改 deleting、_backfill_placement 的
# ConditionExpression(#s=:creating)写 CCF 被跳过(赢家已推进,跳过本身是对的),但
# 随后的 SSM wake / assignment 仍照发,而此处历史上只查 assignment=pending、无 status 闸
# → 删意图之后 VM 照起 → 租户遗留 running + 活 firecracker。这里在建任何盘/起 FC 之前
# 补一道强一致 status 闸。
# codex(round4)#521 — fail-CLOSED:get-item 调用失败(throttle/IAM/network)、读不到
# item、或 status 非明确可起态,一律【暂拒起 + 保持 pending 待重投】(exit 45),绝不
# fail-open 放行。原实现把调用失败 `|| true` 塞成空串再落 * 放行 → DDB 抖动窗口内一条
# 属于已删租户的陈旧 assignment 会被放过起孤儿 VM。判据反转为白名单:只有【确证读到】
# 一个可起状态(creating/running/stopped/…)才 launch;deleted 终态 exit 44,其余(含读
# 失败/空/未知/deleting)exit 45 保持 pending —— 拒起从不丢数据(scheduler 会重投),
# fail-open 才会起错。get-item 与 jq 分两步,才能区分"调用失败"与"读到但状态不可起"。
# codex(round6)#529 — 本 status 闸是【纯只读 DDB 检查,未分配任何资源(tap/sock 都还没建,
# SOCK 变量尚未定义)】。此刻 EXIT trap(_oc_cleanup_on_err,line 378)已装,其"FC 在则不动"
# 守卫靠 ${SOCK} 判断,而 SOCK 要到 line ~650 才赋值 → 若这里带着 trap 退出(44/45),cleanup
# 会跳过守卫直冲到 `rm -f ${VM_DIR}/fc.sock` → 误删【正在被 winner / 回滚后 VM 使用】的 live
# sock。故进闸前先 `trap - ERR EXIT` 摘掉 trap(与 flock-skip rc75:line 395 同款处理);闸内
# 三个 intentional exit(44/45)都在无 trap 下安全退出;放行(可起态)时再把 trap 装回,后续
# 真正建盘/起 FC 的失败仍由它兜底清理。
trap - ERR EXIT
if _OC_TS_RAW="$(aws dynamodb get-item \
  --table-name "${TENANTS_TABLE:-openclaw-tenants}" \
  --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
  --projection-expression '#s, host_id, vm_num, capacity_reservation_id' \
  --expression-attribute-names '{"#s":"status"}' \
  --consistent-read \
  --region "${OC_REGION:-ap-northeast-1}" \
  --output json 2>/dev/null)"; then
  _OC_TSTATUS="$(printf '%s' "${_OC_TS_RAW}" | jq -r '.Item.status.S // ""' 2>/dev/null || echo "")"
else
  # get-item 调用失败 → 未知,fail-closed 暂拒起待重投(绝不放行冒充可起)。
  echo "[oc:launch] DEFER(#411/6.3): tenant ${TENANT_ID} status get-item 调用失败(throttle/IAM/network)— fail-closed 暂拒起、保持 pending 待重投" >&2
  exit 45
fi
if [ -n "${EXPECTED_RESERVATION_ID}" ]; then
  _OC_ASSIGNMENT_MATCH="$(
    printf '%s' "${_OC_TS_RAW:-}" | jq -r \
      --arg host "${INSTANCE_ID:-}" \
      --arg vm "${VM_NUM}" \
      --arg rid "${EXPECTED_RESERVATION_ID}" '
        if (.Item.status.S // "") == "creating"
           and (.Item.host_id.S // "") == $host
           and (.Item.vm_num.N // "") == $vm
           and (.Item.capacity_reservation_id.S // "") == $rid
        then "yes" else "no" end
      ' 2>/dev/null || echo "no"
  )"
  if [ "${_OC_ASSIGNMENT_MATCH}" != "yes" ]; then
    echo "[oc:launch] DEFER(#412): stale assignment reservation/host/vm mismatch for ${TENANT_ID}" >&2
    exit 45
  fi
  unset _OC_ASSIGNMENT_MATCH
fi
unset _OC_TS_RAW
case "${_OC_TSTATUS}" in
  creating|running|stopped|paused|stopping|starting|restarting|rebuilding|migrating|restoring)
    # 并发起冷恢复 launch,此处正是要起 VM)。不加它会被下方 *) 分支 exit 45 DEFER 拒起 →
    # restore 永远起不来。它非终态(恢复失败控制面回滚 suspended),放行安全。
    :  # 明确可起态(白名单)→ 放行:把 EXIT trap 装回,后续建盘/起 FC 失败仍被兜底清理。
    trap _oc_cleanup_on_err ERR EXIT
    ;;
  deleted)
    # 终态:租户已删。拒起 + 让 caller 标 assignment done(不再重投)。trap 已摘,安全退。
    echo "[oc:launch] ABORT(#411/6.3): tenant ${TENANT_ID} status=deleted — 已删,拒起 VM 并停重投" >&2
    exit 44
    ;;
  failed|requires_intervention)
    # #624 — 这里必须 exit 44 而不是 45。45 的语义是“状态会变,等它变”:deleting 若
    # delete 失败会回滚 creating/running,所以该重投;但 failed 是 API 明确定义的墓碑
    # (只能换 client_token 重建),requires_intervention 是 host-agent 的 no-reset 状态,
    # 二者都不会自行回可起态。用 45 只会让同租户 FIFO 每轮占住一个可见性窗口,直到
    # #564/#565 死线闸终结。44 让 caller 标 assignment done、不重投、不计失败;abort
    # 路径只 mark done,不释放容量、不删盘、不改 tenant status,容量由 health_check 的
    # requires_intervention/failed orphan reaper 回收。客户仍可 stop requires_intervention
    # 回 stopped(#619 真机实测)后重新 start;failed 可换 client_token 重建。这里只终结当前消息。
    echo "[oc:launch] ABORT(#624): tenant ${TENANT_ID} status='${_OC_TSTATUS}' — 当前状态不可起 VM,标 assignment done 并停重投这条消息" >&2
    exit 44
    ;;
  *)
    # deleting / 空 / 未知状态:非终态、非明确可起 → fail-closed 暂拒起,保持 pending。
    # deleting 尤其【不是】终态(delete 失败会回滚到 creating/running),若标 done 会让回滚后
    # 的 creating 租户永久无 VM 无 pending。exit 45 保持 pending 待重投重判:delete 成功则下轮
    # 读到 deleted 走 44 终结,delete 回滚则下轮读到 creating 正常起,读不到也下轮再判。
    echo "[oc:launch] DEFER(#411/6.3): tenant ${TENANT_ID} status='${_OC_TSTATUS}'(deleting/空/未知)— fail-closed 暂拒起、保持 pending 待重投重判" >&2
    exit 45
    ;;
esac
unset _OC_TSTATUS
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
# 位置 12/13(gateway_token_ct / device_paired_b64)为空。若此时 NEW_DATA=true(首次
# 建盘或数据盘被清),token 注入段会走 openssl rand 回退,产出的 token 跟 DDB 里控制
# 面 mint 的不一致 → JDWS 拿到 DDB 的 A,VM 实际是 B → 连不上。
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
    # 与本块 else 分支同款,直接 echo。
    [ -n "${INJECTED_GATEWAY_TOKEN_CT}" ] && echo "[oc:launch] DDB fallback: got gateway_token_ct from ${_SECRETS_TABLE} (#290)"
    [ -n "${INJECTED_DEVICE_PAIRED_B64}" ] && echo "[oc:launch] DDB fallback: got device_paired_b64 from ${_SECRETS_TABLE} (#290)"
  else
    echo "[oc:launch] FATAL(#290): DDB get-item for gateway_token_ct/device_paired_b64 failed (throttle/IAM/network) — fail-closed, scheduler will retry" >&2
    exit 1
  fi
  unset _SEC_RAW _SECRETS_TABLE
fi
# 一级回落(查 tenant_secrets)拿到空 → paired.json 无源重注入 → 网关读空盘配对 → 前端
# NOT_PAIRED(真机复现)。paired.json 是公开信息(deviceId+publicKey+roles+scopes,无私钥),
# create 时已长期存 tenants.device_paired_b64(无 TTL),这里作长期兜底。gateway_token 不做
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
# ─────────────────────────────────────────────────────────────────────────
# 三块盘全部从这一个目录取,绝不混版。
#
# 解析顺序:
#   1. 租户记录里固定的 image_snapshot_time(canary 租户在 admission 时固定下来的具体
#      版本)→ 直接用该版本目录。restart/reset/recover 都走这条,所以 canary 租户【不会】
#      因为 canary 指针被 promote 清空而漂移到别的版本(ADR §6.1)。
#   2. 否则读 slots.json 的 live 指针(普通 live 租户;每次启动解析一次当前 live)。
#   3. slots.json 不存在(还没导入版本目录的老 host)→ 回落到历史扁平路径。
#      这是唯一的兼容回落,且只在"整台 host 都还没有版本目录"时成立。
#
# fail-loud 边界:租户固定了版本、但该版本目录在本机【缺失】→ 拒起(exit 1),绝不
# 静默改用 live 或扁平盘。起错版本比起不来更糟:验证结论会挂在错误的镜像上,而且
# 客户看到的是"验证通过"的假信号(ADR §11.1 末条 / §6.1)。
# ─────────────────────────────────────────────────────────────────────────
_FC_ASSETS="/data/firecracker-assets"
_SLOTS_FILE="${_FC_ASSETS}/slots.json"
IMAGE_SNAPSHOT_TIME=""   # 本次启动实际使用的版本(空=走扁平回落);写进 vm.json 供审计

# destructive commit point. It has priority over a later DDB/live-pointer read.
if [ -n "${REBUILD_IMAGE_SNAPSHOT}" ]; then
  if [ "${REBUILD_IMAGE_SNAPSHOT}" = "__legacy_flat__" ]; then
    IMAGE_SNAPSHOT_TIME=""
  else
    IMAGE_SNAPSHOT_TIME="${REBUILD_IMAGE_SNAPSHOT}"
  fi
fi

# (1) 租户固定版本:位置参不传,统一从 DDB 读(与 :491-505 读 restore_backup_key 同款
# fail-CLOSED 模式 —— get-item 调用失败即中止,绝不在读不到版本时盲目起 live)。
if [ -z "${REBUILD_IMAGE_SNAPSHOT}" ] && _IMG_RAW="$(aws dynamodb get-item \
  --table-name "${TENANTS_TABLE:-openclaw-tenants}" \
  --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
  --projection-expression 'image_snapshot_time' \
  --consistent-read \
  --region "${OC_REGION:-ap-northeast-1}" \
  --output json 2>/dev/null)"; then
  IMAGE_SNAPSHOT_TIME="$(printf '%s' "${_IMG_RAW}" | jq -r '.Item.image_snapshot_time.S // ""' 2>/dev/null || true)"
elif [ -z "${REBUILD_IMAGE_SNAPSHOT}" ]; then
  log "FATAL(#394): DDB get-item for image_snapshot_time failed (throttle/IAM/network) — 拒起 fail-closed,绝不猜版本"
  exit 1
fi
unset _IMG_RAW

# (2) 没固定版本 → 读 slots.json 的 live 指针。解析失败(文件损坏)也 fail-loud:
#     静默当空会回落到扁平盘 = 起了运维以为已经换掉的旧版本。
if [ -z "${REBUILD_IMAGE_SNAPSHOT}" ] && [ -z "${IMAGE_SNAPSHOT_TIME}" ] && [ -f "${_SLOTS_FILE}" ]; then
  IMAGE_SNAPSHOT_TIME="$(jq -er '.live // ""' "${_SLOTS_FILE}" 2>/dev/null)" || {
    log "FATAL(#394): ${_SLOTS_FILE} 存在但无法解析(损坏?)— 拒起,不回落扁平盘冒充"
    exit 1
  }
fi

if [ -n "${IMAGE_SNAPSHOT_TIME}" ]; then
  _VER_DIR="${_FC_ASSETS}/versions/${IMAGE_SNAPSHOT_TIME}"
  ROOTFS="${_VER_DIR}/openclaw-rootfs.ext4"
  DATA_TPL="${_VER_DIR}/openclaw-data-template.ext4"
  IMMUTABLE_TPL="${_VER_DIR}/openclaw-immutable.ext4"
  # 版本目录里 rootfs/data-template 必须都在(immutable 仍是可选盘,与历史一致)。
  if [ ! -f "${ROOTFS}" ] || [ ! -f "${DATA_TPL}" ]; then
    log "FATAL(#394): 版本 ${IMAGE_SNAPSHOT_TIME} 在本机缺失(${_VER_DIR})— 拒起,绝不改用其它版本"
    exit 1
  fi
  log "image version pinned: ${IMAGE_SNAPSHOT_TIME} (dir=${_VER_DIR})"
else
  # (3) 兼容回落:整台 host 还没 slots.json/版本目录 → 历史扁平路径,行为与改动前一致。
  ROOTFS="${_FC_ASSETS}/openclaw-rootfs.ext4"
  DATA_TPL="${_FC_ASSETS}/openclaw-data-template.ext4"
  IMMUTABLE_TPL="${_FC_ASSETS}/openclaw-immutable.ext4"
  log "image version: legacy flat layout (no slots.json on this host)"
fi
# #517 阶段3(G1 fail-closed)—— 只读身份盘缺失 + 开关开 → 在【起 FC / 写 vm.json 之前】拒起,
# 与上面 rootfs 缺失 FATAL 同款早退(codex 交叉审 C1:放到 attach 处再退时 vm.json 已写、FC 已起,
# EXIT trap 留下空跑 FC + retry 见 vm.json 误判 done)。默认关=既有兼容(盘缺走后面 WARN 照常起),
# 消除 §4 G1 的静默旧副本:开 true 后宁可这台起不来也不静默跑 data 盘烤制当天的旧身份。
if [ "${IMMUTABLE_DISK_REQUIRED:-false}" = "true" ] && [ ! -f "${IMMUTABLE_TPL}" ]; then
  log "FATAL(#517): ${IMMUTABLE_TPL} absent but IMMUTABLE_DISK_REQUIRED=true — refusing to launch on a stale identity fallback"
  exit 1
fi
# #526 — 归一化本次启动的 chatCompletions 意图,随 vm.json 落盘供 host-agent 差异化探测
# app_health(chat_ep=1 的租户探 /v1/chat/completions,404=端点缺失=down)。CHAT_EP_ENABLED
# 已在上方把 '""' 归一为空;1/true/yes/on→1,其余(含 0/false/空/未知)→0(保守:健康检查只对
# 明确开 chat 的租户收紧,不误报 chat=0 的租户)。#526 落库后 wake/restore 传对的 1 → 记 1。
case "$(printf '%s' "${CHAT_EP_ENABLED}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) VM_JSON_CHAT_EP=1 ;;
  *) VM_JSON_CHAT_EP=0 ;;
esac
# Write VM metadata only after all image-selection fail-closed gates pass.
# DDB fan-out retries pair this discovery file with a matching live Firecracker
# process. Keep it after image fail-closed gates so host-agent does not discover
# and repeatedly recover a launch that can never pass those gates.
cat > "${VM_DIR}/vm.json" << VMEOF
{"tenant_id":"${TENANT_ID}","vm_num":${VM_NUM},"guest_ip":"${GUEST_IP}","vcpu":${VCPU},"mem_mb":${MEM_MB},"config_template":"${CONFIG_TEMPLATE}","chat_ep":${VM_JSON_CHAT_EP}}
VMEOF
# 记录本次启动【实际使用】的版本到 vm.json(ADR §4.3 末句:每次启动记录实际 snapshot_time,
# 供审计与迁移判断)。失败不阻断启动(纯审计信息)。
if [ -n "${IMAGE_SNAPSHOT_TIME}" ] && command -v jq >/dev/null 2>&1; then
  if jq --arg v "${IMAGE_SNAPSHOT_TIME}" '.image_snapshot_time = $v' \
    "${VM_DIR}/vm.json" > "${VM_DIR}/vm.json.tmp" 2>/dev/null; then
    mv "${VM_DIR}/vm.json.tmp" "${VM_DIR}/vm.json"
  else
    rm -f "${VM_DIR}/vm.json.tmp"
  fi
fi
DATA_SIZE=$(stat -c%s ${DATA_TPL})
# Immutable authority disk (identity files + ops skills). Shared, read-only,
# attached to every VM as /dev/vdd with is_read_only:true. Optional: if the
# asset is absent (older image set), we skip the 4th drive so launch still works.

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
  # (identity/skills/config/channel_secret/vkey/用户数据全在里面)。升级镜像时
  # refresh_rootfs 会 mv 换新 data-template(host_service.py:377),其逻辑尺寸常
  # 与租户现有盘不同;而 rebuild/restart 的 wake 传空 RESTORE_KEY。原逻辑在此
  # 分支 NEEDS_INIT=true → 下面 `rm -f ${DATA_VOL}` 从模板重建 = **静默删光客户数据**
  # (真机复现:升级后 rebuild 数据全丢)。铁律 no-data-loss:存量盘遇模板尺寸漂移
  # **绝不重建**,保留原盘照常挂载启动(模板尺寸只是"新建时用多大",不是"存量盘必须
  # 换盘走显式 RESTORE_KEY(下面恢复路径)。这里只对存量盘 fail-safe 保留 + 告警。
  log "WARN(#303): data.ext4 尺寸($(stat -c%s ${DATA_VOL}))≠ 模板(${DATA_SIZE}),但存量盘含客户数据 — 保留原盘不重建(扩盘用 resize-disk,换盘用 restore)"
fi
if [ "${NEEDS_INIT}" = "true" ]; then
  rm -f ${DATA_VOL}
  if [ -n "${RESTORE_KEY}" ]; then
    # 不是 ASSETS_BUCKET。原来从 ASSETS_BUCKET 拉 → 永远下载失败 → restore 拒起。
    # 回退 ASSETS_BUCKET 兼容未配 BACKUP_BUCKET 的旧 host(与 _resolve_backup 同源)。
    _RESTORE_BUCKET="${BACKUP_BUCKET:-${ASSETS_BUCKET}}"
    log "restoring from s3://${_RESTORE_BUCKET}/${RESTORE_KEY}"
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
    # ── 解压 data.ext4:双格式(新 tar -S / 旧裸 pigz),必须都能恢复 ──
    #
    # 为什么分两种:data.ext4 是 8G 声明的稀疏盘,真实数据仅 ~77M(实测 275 个盘稀疏率
    # 99.66%)。旧格式(裸 pigz)读它时内核把洞展开成零 → 归档含 7.9G 零 → 恢复端 pigz
    # 必须串行 inflate 出完整 8G。pigz 解压是【算法级串行】(DEFLATE 滑动窗口有前后依赖;
    # 实测 -p 96 与默认同为 16.7s、%CPU 仅 135%,96 核用不上),这 16.5s 全在解无用的零。
    # 新格式 tar -S 逐块判零、零块只进段表,恢复时 lseek 跳洞(实测 34 次 lseek 跳过 7.9G,
    # 只 write 76.6M)。同一盘实测:旧 16.70s+0.92s 稀疏化 → 新 0.14s,且 md5 与源一致。
    #
    # 【双格式探测是硬要求,不是兼容性优化】:S3 里现存全部是旧格式,而 restore 是 5 条
    # 链路的共同出口(POST /restore、POST /tenants{restore_from}、AZ failover、scaler
    # image_refresh、queue 重投)。其中 health_check 的 failover 找不到可恢复备份就【拒绝
    # 迁移】——若旧备份读不出,宿主机故障时全部租户无法容灾(no-data-loss 违规)。
    #
    # 探测用 `tar -tf` 试读归档目录:tar 头部有 magic("ustar")+校验和,裸 ext4 流几乎
    # 不可能通过,误判风险极低;失败则回落旧路径,行为与改动前完全一致。
    # 注意探测【隐含也校验了 gz 完整性】:损坏/截断/空/非 gzip 的输入都过不了这一步,
    # 于是一律落到 legacy 分支,由那里的 `if ! pigz` 统一 FATAL。真机实测 T3/T4/T5
    # (截断 tar+gz / 空 / 随机垃圾)全部 exit 1 且零残留——结果正确,只是日志会记成
    # legacy 解压失败而非 tar 解包失败,归因略偏但不影响 fail-loud 语义。
    _RAW="${DATA_VOL}.raw"
    _IS_TAR=0
    # ── 成员清单必须【精确】等于 data.ext4(codex 独立复审 blocker)────────────
    # 只要 `tar -tf` 读得通就当新格式、然后 `tar -x -C ${VM_DIR}` 解【全部成员】,
    # 等于把归档内容当可信输入:一个合法 tar 里若含 `overlay.ext4`(五盘契约的 rw 层)、
    # `vm.json`(host-agent 的 recover 标记)或 `../<别的租户>/data.ext4`(路径遍历),
    # 就能改写 VM 目录乃至越界;而下方的大小门只看 data.ext4,那些成员即使让整次 restore
    # 失败也已经落地(FATAL 只 rm data.ext4)。
    # 故先取清单严格比对:必须【恰好一行】且等于 `data.ext4`。不等 → 不认作新格式。
    # 用 `tar -tf` 的输出而非 `--wildcards` 之类:清单比对是白名单,任何意外成员一律拒。
    # ★ 必须 `set +e` 圈住这次探测(codex 独立复审)。顶层是 `set -euo pipefail`(:5),
    #   而 legacy(裸 pigz)备份让 `tar -tf` 失败 = 命令替换非零 → **整个脚本在这一行
    #   就退出**,`_MEMBERS` 为空的 legacy 分支永远不可达。S3 里现存全部是旧格式,
    #   这会让 AZ failover / restore_from 全线恢复失败(真机实测:legacy 输入 rc=2,
    #   探测行之后的语句一句都没执行)。pipefail 下 pigz 侧失败同样触发。
    set +e
    _MEMBERS="$(pigz -d -c "${_GZ}" 2>/dev/null | tar -tf - 2>/dev/null)"
    set -e
    if [ "${_MEMBERS}" = "data.ext4" ]; then
      _IS_TAR=1
    elif [ -n "${_MEMBERS}" ]; then
      # 读得通 tar 但成员清单不对:这【不是】本仓 backup-data.sh 的产物。绝不落 legacy
      # 分支拿裸 pigz 当 ext4 用(那会把 tar 头部当文件系统),直接 fail-loud 拒起。
      rm -f "${_GZ}" "${_RAW}" ${DATA_VOL}
      log "FATAL(#199): restore 归档成员清单非预期(期望恰好 data.ext4,实得:$(printf '%s' "${_MEMBERS}" | tr '\n' ',' | cut -c1-200))— 拒起,不解包"
      exit 1
    fi
    if [ "${_IS_TAR}" -eq 1 ]; then
      # 新格式:tar -xS 直接产出稀疏文件,无需 .raw 中间文件、无需 cp --sparse。
      # 顶层是 `set -euo pipefail`(:5):管道任一环失败会【立刻】触发 -e 退出,来不及
      # 读 PIPESTATUS。必须 set +e 圈住,自己判两个码再决定清理+FATAL——否则数据盘
      # 半成品会随 ERR trap 退出而残留(trap 只 kill FC,不删盘)。
      #
      # 纵深防御(即使清单已校验过):
      #  · 解到【专用空目录】而非 ${VM_DIR},解成功后只 mv 出 data.ext4 —— 万一 tar 的
      #    清单显示与实际解出内容不一致(GNU tar 的 sparse 成员由多个头部描述),落地面
      #    也只有这个临时目录,VM_DIR 里的 overlay/vm.json 碰不到;
      #  · `--no-same-owner` + 只解 `data.ext4` 这一个成员(而非整个归档)。
      # 注:不用 `--no-absolute-names` —— GNU tar 1.35 【没有】这个选项(真机实测
      # `unrecognized option`,rc=64,曾让本段正路直接失败)。它也不需要:GNU tar
      # 提取时默认就剥掉前导 `/`(实测打印 "Removing leading '/' from member names",
      # 归档里的 /etc/hostname 落到 -C 目标下而非真的 /etc)。`../` 由上面的清单
      # 白名单挡住(清单必须恰好等于 data.ext4)。
      _XD="${VM_DIR}/.restore-x"
      rm -rf "${_XD}"; mkdir -p "${_XD}"
      rm -f ${DATA_VOL}
      set +e
      pigz -d -c "${_GZ}" | tar -xSf - -C "${_XD}" --no-same-owner data.ext4
      # 必须【同一行】一次性取两个码:任何中间命令(含赋值)都会覆盖 $? 和 PIPESTATUS。
      _pipe_rc="${PIPESTATUS[0]}" _tar_rc="${PIPESTATUS[1]}"
      set -e
      # 解出的内容必须【只有】data.ext4 这一个普通文件,否则连临时目录一起丢掉。
      # `-h`(是否符号链接)这一条是 codex 独立复审抓出、我本地实测复现的真漏洞:
      # 成员名叫 data.ext4 的【符号链接】能通过上面的清单白名单(tar -tf 只显示
      # "data.ext4"),而 `[ -f ]` 会【跟随链接】判定为真 —— 目标存在时直接放行,
      # `find ! -name data.ext4` 也不报警(名字确实是 data.ext4)。
      # 后果:下面的 mv 把这个链接搬进 VM 目录,随后的 e2fsck / 挂载 / 写入全部落到
      # 链接指向的路径 —— 归档由调用方提供,指向别的租户盘就是跨租户写。
      # 实测(本地复现):目标可解析时 `[ -f ]` 返回真、find 无输出,两道守卫都被绕过。
      # 故必须先用 `-h` 显式拒掉链接,再要求它是普通文件。
      # 硬链接同理但更隐蔽:`-h` 不认它、`-f` 认它是普通文件、find 也不报警,而 mv
      # 之后写入会落到被链接的那个 inode。判据是 link count —— tar 解到【新建的空
      # 目录】里,正常成员的 nlink 必为 1;>1 说明这个 inode 在别处也有名字。
      # 本地实测:硬链接情形下 nlink=2,而三道原守卫全部放行。
      _nlink="$(stat -c%h "${_XD}/data.ext4" 2>/dev/null || echo 0)"
      _xtra="$(find "${_XD}" -mindepth 1 ! -name data.ext4 2>/dev/null | head -3)"
      if [ "${_pipe_rc:-1}" -ne 0 ] || [ "${_tar_rc:-1}" -ne 0 ] ||
         [ -h "${_XD}/data.ext4" ] || [ "${_nlink}" != "1" ] ||
         [ ! -f "${_XD}/data.ext4" ] || [ -n "${_xtra}" ]; then
        rm -rf "${_XD}"
        rm -f "${_GZ}" ${DATA_VOL}
        log "FATAL(#199): restore tar 解包失败(pigz rc=${_pipe_rc} tar rc=${_tar_rc} extra='${_xtra}' nlink=${_nlink};截断/损坏/成员非预期/符号或硬链接)— 拒起,不留半个盘"
        exit 1
      fi
      # 同一文件系统内 mv = rename,保留稀疏性(不会物化空洞)。
      if ! mv -f "${_XD}/data.ext4" ${DATA_VOL}; then
        rm -rf "${_XD}"; rm -f "${_GZ}" ${DATA_VOL}
        log "FATAL(#199): restore 产物移入 VM 目录失败 — 拒起,不留半个盘"
        exit 1
      fi
      rm -rf "${_XD}"
      log "restore: sparse-aware tar 解包完成"
    else
      # 旧格式(裸 pigz 流):解到 .raw 再 cp --sparse=always 转稀疏——与下方新建路径
      # (`cp --sparse=always ${DATA_TPL}`)同一写法。不用 `pigz | dd conv=sparse`:
      # 本段无 pipefail,pigz 失败时 $? 取 dd 的退出码,dd 收不完整数据仍可能返 0。
      # 临时文件保留 `if ! pigz` 直接捕获 pigz 自身退出码的 fail-loud 语义。
      # .raw 落 ${VM_DIR}(/data,与 DATA_VOL 同盘)而非 /tmp:8G 放不进 20G 根卷。
      if ! pigz -d -c "${_GZ}" > "${_RAW}"; then
        rm -f "${_GZ}" "${_RAW}" ${DATA_VOL}
        log "FATAL(#199): restore .gz 解压失败(截断/损坏)— 拒起,不留半个盘"
        exit 1
      fi
      if ! cp --sparse=always "${_RAW}" ${DATA_VOL}; then
        rm -f "${_GZ}" "${_RAW}" ${DATA_VOL}
        log "FATAL: restore 稀疏化失败(磁盘空间/IO)— 拒起,不留半个盘"
        exit 1
      fi
      rm -f "${_RAW}"
      log "restore: legacy 裸 pigz 流 + 稀疏化完成"
    fi
    rm -f "${_RAW}" "${_GZ}"
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
# 就死了会泄漏这个挂载点,下次进来直接撞 "already mounted" 卡死。这里 mount 前先卸残留。
# 用 plain umount(不用 -l 惰性):此刻已持有 per-tenant flock(见上,是本租户唯一 owner),
# 残留必来自被 SIGKILL 的死进程(无活写者),plain umount 必成功;若 busy(有意外活写者)
# = 违反不变量,让 set -e fail-loud 中止 + 调度重试,绝不 lazy-detach-then-remount
# (那会让活写者继续写旧挂载 + remount 双挂同一 backing file → ext4 损坏,踩 no-data-loss)。
mountpoint -q "${MOUNT_TMP}" && sudo umount "${MOUNT_TMP}"
sudo mount ${DATA_VOL} ${MOUNT_TMP}
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
  # OC_REAPPLY_COMMIT_BEGIN
  # so this is the first safe point to read disk-only credentials.
  _OC_REAPPLY_ASSEMBLED=0
  if [ "${OC_REAPPLY_CONFIG:-}" = "1" ]; then
    _RA_BINDING="$(printf '%s' "${OC_REAPPLY_BINDING_B64:-}" | base64 -d 2>/dev/null || true)"
    if ! printf '%s' "${_RA_BINDING}" | jq -e 'type == "object"' >/dev/null 2>&1; then
      log "FATAL(#429): invalid reapply binding"
      exit 1
    fi
    _RA_TEMPLATE="$(printf '%s' "${_RA_BINDING}" | jq -r '.config_template // "default"')"
    _RA_VERSION_ID="$(printf '%s' "${_RA_BINDING}" | jq -r '.body_version_id // ""')"
    _RA_SHA="$(printf '%s' "${_RA_BINDING}" | jq -r '.body_sha256 // ""')"
    _RA_DIR="$(mktemp -d "/tmp/oc-reapply-commit-${TENANT_ID}.XXXXXX")"
    _RA_BODY="${_RA_DIR}/template.json"
    _RA_OUTPUT="${OC_JSON}.reapply.$$"
    _RA_TPL_MNT="${_RA_DIR}/template-mount"
    mkdir -p "${_RA_TPL_MNT}"
    if [ "$(printf '%s' "${_RA_BINDING}" | jq -r '.host_baked // false')" = "true" ]; then
      if ! sudo mount -o ro,noload "${DATA_TPL}" "${_RA_TPL_MNT}"; then
        log "FATAL(#429): cannot read host-baked target template"
        rm -rf "${_RA_DIR}"
        exit 1
      fi
      if ! sudo cp "${_RA_TPL_MNT}/.openclaw/openclaw.json" "${_RA_BODY}"; then
        sudo umount "${_RA_TPL_MNT}" 2>/dev/null || true
        rm -rf "${_RA_DIR}"
        log "FATAL(#429): host-baked openclaw.json missing"
        exit 1
      fi
      sudo umount "${_RA_TPL_MNT}" 2>/dev/null || {
        rm -rf "${_RA_DIR}"
        log "FATAL(#429): target template mount cleanup failed"
        exit 1
      }
    else
      if [ -z "${ASSETS_BUCKET:-}" ] || [ -z "${_RA_VERSION_ID}" ]; then
        rm -rf "${_RA_DIR}"
        log "FATAL(#429): named template binding incomplete"
        exit 1
      fi
      if ! aws s3api get-object \
          --bucket "${ASSETS_BUCKET}" \
          --key "templates/openclaw/${_RA_TEMPLATE}/openclaw.json" \
          --version-id "${_RA_VERSION_ID}" \
          "${_RA_BODY}" \
          --region "${OC_REGION:-ap-northeast-1}" >/dev/null; then
        rm -rf "${_RA_DIR}"
        log "FATAL(#429): exact template body download failed"
        exit 1
      fi
      if [ -n "${_RA_SHA}" ] &&
         [ "$(sha256sum "${_RA_BODY}" | cut -d' ' -f1)" != "${_RA_SHA}" ]; then
        rm -rf "${_RA_DIR}"
        log "FATAL(#429): exact template body hash mismatch"
        exit 1
      fi
    fi

    _RA_TOKEN="$(jq -r '.gateway.auth.token // ""' "${OC_JSON}" 2>/dev/null || true)"
    _RA_VKEY="$(jq -r '.models.providers.litellm.apiKey // ""' "${OC_JSON}" 2>/dev/null || true)"
    _RA_CREDS="$(jq -nc --arg token "${_RA_TOKEN}" --arg vkey "${_RA_VKEY}" \
      '{gateway_token:$token,litellm_vkey:$vkey}')"
    _RA_BASEURL="$(oc_normalize_litellm_baseurl "${LITELLM_HOST:-}")"
    if ! oc_assemble_config \
        "${OC_JSON}" "${_RA_BODY}" "${_RA_OUTPUT}" \
        "${_FP_PURE:-}" "${_RA_CREDS}" "${_FP_SCHEME:-kms-cmk}" \
        "${_CRED_OWNER:-}" "${OC_REGION:-ap-northeast-1}" \
        "${CLOUDFRONT_ORIGIN:-}" "${_RA_BASEURL}" \
        "${LITELLM_SHARED_VKEY:-}" "${CHAT_EP_ENABLED}" \
        "${CLAWPOOL_RSA_CMK_ARN:-}"; then
      rm -f "${_RA_OUTPUT}"
      rm -rf "${_RA_DIR}"
      log "FATAL(#429): final openclaw.json assembly failed"
      exit 1
    fi
    chmod 600 "${_RA_OUTPUT}"
    sudo chown 1000:1000 "${_RA_OUTPUT}"
    mv -f "${_RA_OUTPUT}" "${OC_JSON}"
    _RA_OUTPUT_DIR="$(dirname -- "${_RA_OUTPUT}")"
    _RA_OC_DIR="$(dirname -- "${OC_JSON}")"
    _RA_MARKER_SRC="${_RA_OUTPUT_DIR}/origin-policy.json"
    _RA_MARKER_DST="${_RA_OC_DIR}/origin-policy.json"
    # oc_assemble_config 把 marker 写在其收敛文件旁；本路径收敛的是 scratch 文件,故 commit 必须把它带到数据盘,否则下方 rm -rf 会静默丢掉 degraded origin 的唯一记录。
    # marker 只含 Origin 字符串与启动时分类、无凭据,故用 0644 让 operator / guest 检查可读。
    if [ "${_RA_OUTPUT_DIR}" != "${_RA_OC_DIR}" ]; then
      if [ ! -f "${_RA_MARKER_SRC}" ]; then
        log "WARN(#642): origin policy marker absent at ${_RA_MARKER_SRC}; continuing without marker relocation"
      elif chmod 0644 "${_RA_MARKER_SRC}" 2>/dev/null \
        && sudo chown 1000:1000 "${_RA_MARKER_SRC}" 2>/dev/null \
        && mv -f "${_RA_MARKER_SRC}" "${_RA_MARKER_DST}"; then
        :
      else
        log "WARN(#642): origin policy marker relocation failed ${_RA_MARKER_SRC} -> ${_RA_MARKER_DST}; continuing"
      fi
    fi
    # Keep the normal wake convergence from selecting the shared key later.
    [ -z "${_RA_VKEY}" ] || LITELLM_VKEY="${_RA_VKEY}"
    _OC_REAPPLY_ASSEMBLED=1
    rm -rf "${_RA_DIR}"
    log "config template '${_RA_TEMPLATE}' re-applied (registry=$(printf '%s' "${_RA_BINDING}" | jq -r '.registry_version'))"
    unset _RA_BINDING _RA_TEMPLATE _RA_VERSION_ID _RA_SHA _RA_DIR _RA_BODY
    unset _RA_OUTPUT _RA_TPL_MNT _RA_TOKEN _RA_VKEY _RA_CREDS _RA_BASEURL
    unset _RA_OUTPUT_DIR _RA_OC_DIR _RA_MARKER_SRC _RA_MARKER_DST
  fi
  # OC_REAPPLY_COMMIT_END

  # ─────────────────────────────────────────────────────────────────────
  # ONE-TIME 生成(NEW_DATA 才跑):config template 首次下载、gateway token 首铸、
  # channel_secret 首次落盘、Cognito 注入、per-tenant vkey 首次注入。这些是"一次
  # 性生成"的东西——重跑会破坏 DDB 握手(hub 校验 channel_secret 用的是首次那个),
  # 或用 shared vkey 覆盖已铸的 per-tenant vkey坏计费拆分。
  # ─────────────────────────────────────────────────────────────────────
  if [ "$NEW_DATA" = "true" ]; then
    # Download custom template from S3 (if specified). 幂等段跑之前先下,让
    # oc_harden_config 收敛新拉下来的模板;唤醒不重下(会冲掉用户配置)。
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
    # 镜像 v5 (P3) 已在 build-rootfs 阶段 del(.channels["claw-channel"]),launch-vm
    # 只需注入 gateway token(数据面走两级路由直连 microVM:18789)。
    jq --arg t "$NEW_TOKEN" \
      '.gateway.auth.token = $t' \
      "${OC_JSON}" > "${OC_JSON}.tmp" && mv "${OC_JSON}.tmp" "${OC_JSON}"
    log "gateway token injected (one-time; #187 P5: hub/Cognito channel 已下线)"

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
      # (配合 re-inject 的损坏 fail-closed,半文件会让后续重启永久拒起)。
      _TMP_CI="${_DEVICES_DIR}/.paired.json.tmp.$$"
      printf '%s' "${_PAIRED_JSON}" > "${_TMP_CI}"
      chmod 600 "${_TMP_CI}"
      sudo chown 1000:1000 "${_TMP_CI}"
      mv -f "${_TMP_CI}" "${_DEVICES_DIR}/paired.json"
      sudo chown 1000:1000 "${_DEVICES_DIR}"
      # Log只打 deviceId 前16字符(paired.json 的顶层 key),不泄全量。
      _DID16="$(printf '%s' "${_PAIRED_JSON}" | jq -r 'keys[0] // "?"' 2>/dev/null | cut -c1-16)"
      log "paired.json cold-injected (one-time; #188 device=${_DID16}… wss 免 approve)"
      unset _PAIRED_JSON _DID16 _TMP_CI
    fi
  fi

  # ─────────────────────────────────────────────────────────────────────
  #   • dangerouslyDisableDeviceAuth 无条件 del(secure default)
  #   • allowedOrigins → 当前 fleet origin 列表(SSM 拉最新,可为逗号分隔多值)
  #   • baseUrl → 当前 LiteLLM host(堡垒机重建 IP 会变)
  #   • chatCompletions 三态(1/0/空)
  #   • apiKey 仅在显式非空时改写(唤醒空参绝不覆盖数据盘上的 per-tenant vkey)
  # 老版本把这块塞在 NEW_DATA-only 分支里,唤醒路径完全跳过 → 唤醒即漂移。
  # origin 空值仍保留盘上值,但会写可查询 degraded marker,不再静默跳过。
  # 详细语义见 lib/harden-config.sh 的 oc_harden_config 注释。
  # ─────────────────────────────────────────────────────────────────────
  CF_ORIGIN="${CLOUDFRONT_ORIGIN:-}"
  LITELLM_BASEURL="$(oc_normalize_litellm_baseurl "${LITELLM_HOST:-}")"

  # 作位置参 8 传入;wake/restart(start-all-vms/自愈/fan-out)位置 8 传空 → 原逻辑落到
  # LITELLM_SHARED_VKEY → oc_harden_config 每次启动把盘上 per-tenant apiKey 覆盖成
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

  # 参数为空时才 fall back 到 platform.env 的 LITELLM_SHARED_VKEY(shared)。
  # 关键 fail-safe:LITELLM_VKEY 参数空 + LITELLM_SHARED_VKEY 也空 → _APIKEY 空 →
  # oc_harden_config 不写 apiKey(不会拿 shared 覆盖数据盘上的 per-tenant vkey)。
  # 老版本这一位失败会保留 __INJECT_AT_DEPLOY__ 占位 → agent 拿占位当 key → 401,
  # 现在只在有真 key 时改写,数据盘上首铸的 per-tenant vkey 会被幂等段保留。
  _APIKEY="${LITELLM_VKEY:-${LITELLM_SHARED_VKEY:-}}"

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

  if [ "${_OC_REAPPLY_ASSEMBLED:-0}" = "1" ]; then
    log "harden-config: already applied by shared #429 assembler"
  elif oc_harden_config "${OC_JSON}" "${CF_ORIGIN}" "${LITELLM_BASEURL}" "${_APIKEY}" "${CHAT_EP_ENABLED}"; then
    # Task 8.3: frozen plan config-class dot-path 覆盖(新契约)
    if [ -n "${_FP_PURE:-}" ]; then
      if ! oc_inject_config_from_plan "${OC_JSON}" "${_FP_PURE}" "${_FP_SCHEME}" "${_CRED_OWNER}" "${OC_REGION:-ap-northeast-1}" "${LITELLM_SHARED_VKEY:-}" "${CLAWPOOL_RSA_CMK_ARN:-}"; then
        log "FATAL: config-class injection from frozen plan failed — aborting"
        exit 1
      fi
    fi
    # 日志一行看清幂等段执行了什么(帮排查唤醒漂移)
    _log_origin="${CF_ORIGIN:-<unset,degraded-marker-written>}"
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
  # 根因(新加坡真机 + openclaw 源码双证):镜像更新 → 在跑 FC 掉线 → 平台自愈
  # restart(fleet-power → start-all-vms.sh → launch-vm 只传 4 参 + 复用 data 盘
  # NEW_DATA=false)→ 上面 NEW_DATA-only 冷注入块整体跳过 → 若那次盘上 paired.json
  # 恰空(新盘/被清)→ gateway 读到空(message-handler.ts:786 getPairedDevice→
  # isPaired=false)→ 前端 NOT_PAIRED。修:每次都把控制面权威的 device_paired_b64
  # 4 参调用也拿得到)幂等写回 data 盘,网关(重)启动永远读到 approved backend 条目。
  # 仅在 INJECTED_* 非空时写(老租户/无 pre-minted → 空 → 不动,保留盘上现值,零漂移;
  # gateway token 的 openssl rand 首铸仍只在 NEW_DATA 块,这里绝不用随机值覆盖)。
  # ─────────────────────────────────────────────────────────────────────
  if [ "${OC_REAPPLY_CONFIG:-}" != "1" ] && [ -n "${INJECTED_GATEWAY_TOKEN_CT}" ]; then
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
    # 盘上 paired.json 可能含网关运行时 approve 的【其它设备】+ 每设备运行时字段
    # (tokens/lastSeen*)。整体覆盖会删掉它们(丢运行时授权状态)。
    # 深合并会让【控制面的非空 tokens 覆盖盘上 tokens】——盘上 openclaw 运行时若已轮换
    # operator token,重启 re-inject 就会被预铸值压回,使新 token 失效、旧 token 复活。
    # token,重加回来就带活 token → 撤销被复活(安全回归)。故 re-inject 必须区分:
    #   • 盘上 paired.json 文件【存在且是合法 JSON object,含空 {}】(_DISK_VALID=true):
    #     这是 gateway 维护的【权威已批准名单】。只更新盘上【已存在】的 device
    #     (更新 publicKey/roles/scopes,tokens 盘上优先);盘上【缺失】的 device = 被
    #     `openclaw devices remove` 撤销,尊重撤销、绝不重加(reduce 里 `$c[$k]==null` 跳过)。
    #     ★ 含空 {}:单 device 租户 remove 掉唯一 device 后盘上正是 {},必须视为权威空名单、
    #       不复活(codex review 第5轮:否则普通重启即绕过撤销)。
    #   • 盘上文件【缺失/读不到/损坏(非合法 JSON)】(_DISK_VALID=false):灾难恢复/从未
    #     注入过 → 全量注入控制面这份(等价冷注入,7.1 首连免 approve)。
    # NEW_DATA=true 的首次冷注入走上面 :1077 整文件写,不进本 re-inject 块;故本块跑时盘已
    # 初始化过,文件存在即代表 gateway 权威状态(含被清空成 {} 的情形)。
    # 三态(codex review 第6轮:损坏不得静默复活):
    #   • 文件【不存在】(从没注入过/盘全新)→ _DISK_VALID=false,全量注入控制面这份
    #     (7.1 首连免 approve;这是唯一该"凭空注入"的情形)。
    #   • 文件【存在但读不到 / 非合法 JSON object】(写中断/坏块/被截断)→ **fail-closed**:
    #     不能当"缺失"去凭空注入(那会把 remove 后损坏的盘静默重新授权该 device);也不能
    #     拿损坏内容 merge。exit 1 让调度重试(下次要么读到完好文件、要么盘已被判坏重建)。
    #   • 文件【存在且合法 object,含 {}】→ _DISK_VALID=true,权威名单,merge(尊重缺失=remove)。
    _PAIRED_FILE_RI="${_DEVICES_DIR_RI}/paired.json"
    if [ ! -e "${_PAIRED_FILE_RI}" ]; then
      _DISK_VALID=false            # 文件不存在 → 首连/灾难恢复:全量注入
      _CUR_PAIRED='{}'
    elif _CUR_PAIRED="$(cat "${_PAIRED_FILE_RI}" 2>/dev/null)" \
         && printf '%s' "${_CUR_PAIRED}" | jq -e 'type == "object"' >/dev/null 2>&1; then
      _DISK_VALID=true             # 文件存在且合法 object(含 {})→ 权威名单,尊重 remove
    else
      # 文件存在但读不到/损坏 → fail-closed,绝不静默凭空注入(防 remove 后损坏静默复活)
      log "FATAL(#415): 盘上 paired.json 存在但读不到/非合法 JSON object(写中断/坏块?)— fail-closed 拒起,调度重试"
      exit 1
    fi
    unset _PAIRED_FILE_RI
    _MERGED_RI="$(jq -cn --argjson cur "${_CUR_PAIRED}" --argjson ctl "${_PAIRED_RI}" --argjson valid "${_DISK_VALID}" '
      $cur as $c
      | reduce ($ctl | keys[]) as $k ($c;
          if ($valid and ($c[$k] == null))
          then .                                       # 盘上有效但无此 device = 被 remove,尊重撤销,不重加
          else .[$k] = (($c[$k] // {}) * $ctl[$k])
               | (if (($c[$k].tokens // {}) | length) > 0
                  then .[$k].tokens = $c[$k].tokens     # 盘上已有非空 tokens(运行时权威)→ 保留,不被预铸值覆盖
                  else . end)
          end)' 2>/dev/null || true)"
    if [ -z "${_MERGED_RI}" ]; then
      log "FATAL(#314): paired.json merge(jq \$cur * \$ctl)失败 — aborting fail-closed"
      exit 1
    fi
    # #490 PAIRING_SELF_HEAL_BEGIN
    # 只在盘上 paired.json 合法(_DISK_VALID=true)且 merge 后仍有该 device 时补 token:
    # 两者合起来证明该 device 仍在 gateway 维护的权威名单中,不会复活 remove 的 device。
    # _DISK_VALID=false 表示盘丢失/重建/全新盘,无从知道磁盘侧撤销状态,绝对不补;
    # 保持原有全量注入行为,不扩大任何授权暴露面。
    # effective roles 按「未撤销 token role ∩ (roles ∪ role)」计算,只有空集才处理。
    # 7.1 是否真的使用 revokedAtMs 未经核实;若使用则这里正确排除已撤销 token,
    # 若不使用则该判断为 no-op、无副作用。已撤销 operator 绝不重铸覆盖。
    _PAIRING_HEALED_RI=0
    _PAIRING_SELF_HEAL_JQ_RI='
      # #490 SELF_HEAL_JQ_BEGIN
      def approved_roles($entry):
        ([
          (if (($entry.roles // null) | type) == "array"
           then $entry.roles[]
           else empty
           end),
          ($entry.role // empty)
        ] | map(select((type == "string") and (length > 0))) | unique);
      def active_roles($entry):
        ([
          ($entry.tokens // {}
           | if type == "object" then to_entries[] else empty end
           | .value
           | select(type == "object")
           | select((((.role // "") | type) == "string")
                    and (((.role // "") | length) > 0))
           | select((.revokedAtMs // false) | not)
           | .role)
        ] | unique);
      def effective_roles($entry):
        (approved_roles($entry)) as $approved
        | (active_roles($entry)
           | map(. as $role | select(($approved | index($role)) != null)));
      def has_revoked_operator($entry):
        ([
          ($entry.tokens // {}
           | if type == "object" then to_entries[] else empty end
           | .value
           | select(type == "object")
           | select(.role == "operator")
           | select(.revokedAtMs // false))
        ] | length) > 0;
      def action_for($entry):
        if ((effective_roles($entry) | length) > 0) then "skip"
        elif ((approved_roles($entry) | index("operator")) == null) then "unsupported"
        elif has_revoked_operator($entry) then "revoked"
        else "heal"
        end;
      reduce ($ctl | keys[]) as $k (
        {"result": $merged, "actions": []};
        if (($valid | not) or (.result[$k] == null)) then .
        else .result[$k] as $entry
          | (action_for($entry)) as $action
          | if $action == "skip" then .
            else .actions += [{"deviceId": $k, "action": $action}]
              | if (($action == "heal") and (($minted[$k] // "") != "")) then
                  .result[$k].tokens = (($entry.tokens // {}) + {
                    "operator": {
                      "token": $minted[$k],
                      "role": "operator",
                      "scopes": (if $entry.scopes == null
                                 then ["operator.write", "operator.read"]
                                 else $entry.scopes
                                 end),
                      "createdAtMs": $now,
                      "lastUsedAtMs": $now
                    }
                  })
                else .
                end
            end
        end)
      # #490 SELF_HEAL_JQ_END
    '
    if ! _HEAL_EVAL_RI="$(jq -cn \
      --argjson merged "${_MERGED_RI}" \
      --argjson ctl "${_PAIRED_RI}" \
      --argjson valid "${_DISK_VALID}" \
      --argjson minted '{}' \
      --argjson now 0 \
      "${_PAIRING_SELF_HEAL_JQ_RI}" 2>/dev/null)"; then
      log "FATAL(#490): paired.json empty-effective-roles self-heal planning failed — aborting fail-closed"
      exit 1
    fi
    if ! _HEAL_ROWS_RI="$(printf '%s' "${_HEAL_EVAL_RI}" \
      | jq -r '.actions[] | [.deviceId, .action] | @tsv' 2>/dev/null)"; then
      log "FATAL(#490): paired.json self-heal action decode failed — aborting fail-closed"
      exit 1
    fi
    _HEAL_TOKENS_RI='{}'
    while IFS=$'\t' read -r _HEAL_DID_RI _HEAL_ACTION_RI; do
      [ -n "${_HEAL_DID_RI}" ] || continue
      _HEAL_DID16_RI="$(printf '%s' "${_HEAL_DID_RI}" | cut -c1-16)"
      case "${_HEAL_ACTION_RI}" in
        unsupported)
          log "WARN(#490): device=${_HEAL_DID16_RI}… empty effective roles but operator is not approved; skipped"
          ;;
        revoked)
          log "WARN(#490): device=${_HEAL_DID16_RI}… revoked operator token present; skipped without remint"
          ;;
        heal)
          if ! _HEAL_TOKEN_RI="$(openssl rand -base64 32 2>/dev/null \
            | tr -d '[:space:]=' | tr '+/' '-_')" \
             || [ "${#_HEAL_TOKEN_RI}" -ne 43 ]; then
            log "FATAL(#490): operator token generation failed — aborting fail-closed"
            exit 1
          fi
          if ! _HEAL_TOKENS_NEXT_RI="$(jq -cn \
            --argjson minted "${_HEAL_TOKENS_RI}" \
            --arg did "${_HEAL_DID_RI}" \
            --arg token "${_HEAL_TOKEN_RI}" \
            '$minted + {($did): $token}' 2>/dev/null)"; then
            log "FATAL(#490): operator token map construction failed — aborting fail-closed"
            exit 1
          fi
          _HEAL_TOKENS_RI="${_HEAL_TOKENS_NEXT_RI}"
          unset _HEAL_TOKEN_RI _HEAL_TOKENS_NEXT_RI
          ;;
      esac
      unset _HEAL_DID16_RI
    done <<< "${_HEAL_ROWS_RI}"
    if [ "${_HEAL_TOKENS_RI}" != "{}" ]; then
      if ! _HEAL_NOW_MS_RI="$(date +%s%3N)" \
         || ! [[ "${_HEAL_NOW_MS_RI}" =~ ^[0-9]+$ ]]; then
        log "FATAL(#490): self-heal timestamp generation failed — aborting fail-closed"
        exit 1
      fi
      if ! _HEALED_RI="$(jq -cn \
        --argjson merged "${_MERGED_RI}" \
        --argjson ctl "${_PAIRED_RI}" \
        --argjson valid "${_DISK_VALID}" \
        --argjson minted "${_HEAL_TOKENS_RI}" \
        --argjson now "${_HEAL_NOW_MS_RI}" \
        "${_PAIRING_SELF_HEAL_JQ_RI} | .result" 2>/dev/null)"; then
        log "FATAL(#490): paired.json empty-effective-roles self-heal failed — aborting fail-closed"
        exit 1
      fi
      if [ "${_HEALED_RI}" = "${_MERGED_RI}" ]; then
        log "FATAL(#490): paired.json self-heal planned tokens but produced no change — aborting fail-closed"
        exit 1
      fi
      _MERGED_RI="${_HEALED_RI}"
      _PAIRING_HEALED_RI=1
      while IFS=$'\t' read -r _HEAL_DID_RI _HEAL_ACTION_RI; do
        [ "${_HEAL_ACTION_RI}" = "heal" ] || continue
        _HEAL_DID16_RI="$(printf '%s' "${_HEAL_DID_RI}" | cut -c1-16)"
        log "device=${_HEAL_DID16_RI}… self-healed empty effective roles (#490)"
      done <<< "${_HEAL_ROWS_RI}"
    fi
    unset _HEAL_ACTION_RI _HEAL_DID_RI _HEAL_DID16_RI _HEALED_RI
    unset _HEAL_EVAL_RI _HEAL_NOW_MS_RI _HEAL_ROWS_RI _HEAL_TOKENS_RI
    unset _PAIRING_SELF_HEAL_JQ_RI
    # #490 PAIRING_SELF_HEAL_END
    # 幂等:merge 结果与盘上一致则不写(避免无谓写盘 + mtime 抖动)。
    if [ "${_CUR_PAIRED}" != "${_MERGED_RI}" ]; then
      # 否则 `printf > paired.json` 中途被杀会留半个损坏文件,配合上面的损坏 fail-closed
      # 会让后续每次重试都 exit 1、租户永久起不来。temp+mv 保证盘上要么旧内容、要么完整新内容。
      # mv(root:root)后、chown 前被杀 → 下次重试因内容相同(幂等)跳过写入+chown →
      # paired.json 永久 root:root,uid 1000 读不了 → gateway 永久起不来。故先 chown temp。
      _TMP_RI="${_DEVICES_DIR_RI}/.paired.json.tmp.$$"
      printf '%s' "${_MERGED_RI}" > "${_TMP_RI}"
      chmod 600 "${_TMP_RI}"
      sudo chown 1000:1000 "${_TMP_RI}"
      mv -f "${_TMP_RI}" "${_DEVICES_DIR_RI}/paired.json"
      sudo chown 1000:1000 "${_DEVICES_DIR_RI}"
      _DID16_RI="$(printf '%s' "${_PAIRED_RI}" | jq -r 'keys[0] // "?"' 2>/dev/null | cut -c1-16)"
      log "paired.json re-injected (#312/#314 merge; device=${_DID16_RI}… 控制面这条已 merge,盘上其它设备+运行时字段保留)"
      unset _DID16_RI _TMP_RI
      # #490 PAIRING_SELF_HEAL_DDB_BEGIN
      # 盘上原子写已成功后才回写完整快照。DDB 失败 fail-open:盘上已经可用,下次 launch 再试。
      if [ "${_PAIRING_HEALED_RI}" = "1" ]; then
        if _HEALED_PAIRED_B64_RI="$(printf '%s' "${_MERGED_RI}" | base64 | tr -d '\n')" \
           && [ -n "${_HEALED_PAIRED_B64_RI}" ]; then
          aws dynamodb update-item \
            --table-name "${TENANTS_TABLE:-openclaw-tenants}" \
            --key "{\"id\":{\"S\":\"${TENANT_ID}\"}}" \
            --update-expression "SET device_paired_b64 = :dpb" \
            --expression-attribute-values "{\":dpb\":{\"S\":\"${_HEALED_PAIRED_B64_RI}\"}}" \
            --region "${OC_REGION:-ap-northeast-1}" >/dev/null 2>&1 \
            && log "backfilled self-healed device_paired_b64 to tenants table (#490)" \
            || log "WARN(#490): backfill self-healed device_paired_b64 to tenants failed (non-fatal)"
        else
          log "WARN(#490): base64 encode for self-healed device_paired_b64 failed (non-fatal)"
        fi
        unset _HEALED_PAIRED_B64_RI
      fi
      # #490 PAIRING_SELF_HEAL_DDB_END
    fi
    unset _PAIRING_HEALED_RI
    unset _PAIRED_RI _DEVICES_DIR_RI _CUR_PAIRED _MERGED_RI _DISK_VALID
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
# ── SECURITY (#528 F1: block guest → internal route-table Redis) ──
# The guest egresses via the host's MASQUERADE (HOST_IFACE below), so at the SG
# layer its packets are indistinguishable from the host's — a guest can reach the
# route-table Valkey/Redis on :6379 (transit_encryption/auth_token both off,
# ha_edge.py) and KEYS/SET the tenant→host route map, hijacking or blackholing
# other tenants' traffic. The tenant-supernet DROP above does NOT cover Redis: it
# lives in the VPC CIDR (EGRESS_VPC_CIDR), not SUBNET_PREFIX/16. Drop guest→VPC
# :6379 here (guest-originated, -i ${TAP}) BEFORE the ACCEPT. This is guest-origin
# only: the edge→gateway data-plane packet arrives DNAT'd on ${HOST_IFACE} (not
# ${TAP}) and its return rides the conntrack ESTABLISHED ACCEPT below, so tenant
# routing is untouched. EGRESS_VPC_CIDR is rendered into /etc/platform.env by
if [ -n "${EGRESS_VPC_CIDR:-}" ]; then
  sudo iptables -C FORWARD -i ${TAP} -d ${EGRESS_VPC_CIDR} -p tcp --dport 6379 -j DROP 2>/dev/null || \
    sudo iptables -I FORWARD 1 -i ${TAP} -d ${EGRESS_VPC_CIDR} -p tcp --dport 6379 -j DROP
else
  log "WARN(#528 F1): EGRESS_VPC_CIDR empty in platform.env — guest→Redis :6379 DROP skipped; guest can reach internal Redis unauthenticated"
fi
# ── SECURITY (management-plane isolation): block guest → host services ──
# The guest's default route points at its tap's host IP (HOST_TAP_IP), and the
# host runs control-plane services bound to 0.0.0.0: host-agent metrics/control
# on :8899 (and :9090 if enabled) and sshd on :22. A tenant must never reach
# these — host-agent on :8899 can drive Firecracker (balloon, lifecycle) and
# read other tenants' gateway tokens. Drop guest→host on those ports in INPUT
# (traffic to the host itself hits INPUT, not FORWARD). host-agent SSHes INTO
# the guest (host→guest, a NEW outbound conn from the host), so blocking
# guest→host:22 here does not affect host-agent's reverse management.
# Runbook guides users to self-install it on hosts at the standard :9100 —
# pre-dropping guest→host:9100 protects users who install the exporter but
# forget the isolation step. Keep this list in sync with migrate-vm.sh.
for _port in 8899 9090 22 9100; do
  sudo iptables -C INPUT -i ${TAP} -p tcp --dport ${_port} -j DROP 2>/dev/null || \
    sudo iptables -I INPUT 1 -i ${TAP} -p tcp --dport ${_port} -j DROP
done
# 同一批端口在 FORWARD 里也挡:上面的 INPUT 只保护 guest 所在的那一台 host,打机队里
# 别的 host 走 FORWARD。刻意不带 -d(目的地是任何地方的这些端口),与 egress 模式解耦。
# 清单与上面的 INPUT 循环、migrate-vm.sh、oc-egress-sim.py 同源,改一处同步三处。
# 理由与实测见 ADR-603-guest-redline-ports-forward-drop.md。
for _port in 8899 9090 22 9100; do
  sudo iptables -C FORWARD -i ${TAP} -p tcp --dport ${_port} -j DROP 2>/dev/null || \
    sudo iptables -I FORWARD 1 -i ${TAP} -p tcp --dport ${_port} -j DROP
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
# child. fd8 is the host launch slot; fd9 is the per-tenant lifecycle lock
# shared with stop-vm.sh. The launcher parent keeps both until InstanceStart,
# but Firecracker must not retain either for the VM lifetime.
nohup firecracker --api-sock ${SOCK} --log-path ${VM_DIR}/fc.log --level Info &>/dev/null 8>&- 9>&- & disown
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
  # #517 阶段3(G1 fail-closed):required 情况已在上方版本解析处(起 FC / 写 vm.json 之前)
  # 早退拦掉,到这里必然是 IMMUTABLE_DISK_REQUIRED!=true → 保持既有兼容:WARN 后照常起。
  log "WARN: ${IMMUTABLE_TPL} absent — launching WITHOUT immutable authority disk"
fi

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
