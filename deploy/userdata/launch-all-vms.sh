#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# launch-all-vms.sh — PUSH-mode manifest driver for the SQS dispatch pipeline.
#
# ONE SSM command per host from the batching consumer invokes this script with:
#   launch-all-vms.sh <command_id> <part_count> <max_parallel>
#
# The script:
#   1) Sources /etc/platform.env for OC_REGION + INSTANCE_ID (init-host.sh already
#      wrote both at boot; same convention as install-hub.sh:48 and launch-vm.sh:83
#      — don't re-dance IMDSv2 when the values are cached one dotfile away).
#   2) Loops part-0..part-<N-1> under
#      /openclaw/dispatch/manifests/<command_id>/<instance_id>/part-<N> and pulls
#      the SecureString via `aws ssm get-parameter --with-decryption`. Each part
#      is <3800B and contains JSON-lines:
#        {"t":"...","n":42,"c":2,"m":2048,"e":0[,"g":"<ct>","d":"<b64>"]}
#      (t=tenant_id, n=vm_num, c=vcpu, m=mem_mb, e=chat_endpoint_enabled,
#       g=gateway_token_ct KMS 密文 #187 P1, d=device_paired_b64 #188 — both
#       optional, present only when the CMK feature is on).
#   3) Idempotent CHECK-BEFORE-APPLY: if /data/firecracker-vms/<tenant>/vm.json
#      already exists, we skip (this SSM command may be a duplicate delivery).
#   4) Fan-outs launch-vm.sh in BOUNDED parallel (same job-count semaphore as
#      start-all-vms.sh — proven at 380 concurrent on r8g.metal-24xl).
#   5) Emits a single-line JSON summary {"launched":N,"skipped":N,"failed":N} on
#      stdout so the Poller (GetCommandInvocation) can parse it deterministically.
#
# Why bash (not POSIX sh): we already require bash 4.3+ for start-all-vms.sh's
# `wait -n` semaphore, and the process substitution / arithmetic below use the
# same features. The host base image (Ubuntu on r8g.metal) ships bash 5.x.
#
# fail-loud rules (PITFALLS.md #1): empty manifest part after 3 backoff tries
# exits non-zero and produces stderr. We do NOT swallow "no parts found" — the
# consumer must have written them BEFORE SendCommand, and a missing part means
# either Poller cleaned up too early (bug) or SendCommand raced Put (bug).

set -uo pipefail

# ── Two manifest sources (DISPATCH_MODE contract, #73) ──
#   paramstore (push, 回退):  launch-all-vms.sh <command_id> <part_count> <max_parallel> [param_prefix]
#   ddb (一期默认载体):        launch-all-vms.sh --from-ddb <command_id> <expected_count> <max_parallel> [assignments_table]
# The ddb path Queries openclaw-assignments for THIS host's pending rows and
# maps them to the same JSON-lines shape the paramstore path yields, so the
# fan-out below is source-agnostic. Why: PutParameter write-side is ~3 TPS
# (account/region) — a hard wall + 24KB param cap; BatchWriteItem is not.
FROM_DDB=0
if [ "${1:-}" = "--from-ddb" ]; then
  FROM_DDB=1
  shift
fi

COMMAND_ID="${1:?Usage: launch-all-vms.sh [--from-ddb] <command_id> <count> <max_parallel>}"
PART_COUNT="${2:?Usage: launch-all-vms.sh [--from-ddb] <command_id> <count> <max_parallel>}"
MAX_PARALLEL="${3:-96}"

VM_DIR="/data/firecracker-vms"
LAUNCH_SH="/home/ubuntu/launch-vm.sh"
PARAM_PREFIX="${DISPATCH_PARAM_PREFIX:-/openclaw/dispatch}"
# In ddb mode $4 is the assignments table name; in paramstore mode $4 (if given)
# is the param prefix — keep the old positional contract intact.
ASSIGN_TABLE="${4:-${ASSIGNMENTS_TABLE:-openclaw-assignments}}"
if [ "${FROM_DDB}" -eq 0 ] && [ -n "${4:-}" ]; then
  PARAM_PREFIX="$4"
fi

log() { echo "[oc:launch-all] $(date +%H:%M:%S) $*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# ── REGION + INSTANCE_ID from /etc/platform.env (init-host.sh wrote it) ──
# init-host.sh:160 writes INSTANCE_ID and OC_REGION into /etc/platform.env at
# first boot. install-hub.sh:48 and launch-vm.sh:83 follow the same convention.
# We do NOT re-hit IMDS here: sourcing a local file is faster, doesn't leak IMDS
# calls into audit logs, and stays alive during any IMDS blip.
[ -f /etc/platform.env ] && . /etc/platform.env
REGION="${OC_REGION:-${AWS_REGION:-}}"
[ -n "${REGION}" ] || die "OC_REGION empty in /etc/platform.env — init-host.sh didn't run?"
[ -n "${INSTANCE_ID:-}" ] || die "INSTANCE_ID empty in /etc/platform.env — init-host.sh didn't run?"

log "start command_id=${COMMAND_ID} parts=${PART_COUNT} parallel=${MAX_PARALLEL} host=${INSTANCE_ID}"

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
  local vm_path="${VM_DIR}/${tid}"
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
  bash "${LAUNCH_SH}" "${tid}" "${vm_num}" "${vcpu}" "${mem_mb}" \
    "" "${restore_key}" "" "" "" "${chat_ep}" "" "${gw_token_ct}" "${device_paired}" \
    >/dev/null 2>&1
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
# one word: "launched" | "skipped" | "failed" into a per-child result file.
RESULT_DIR="$(mktemp -d /tmp/oc-launch-all.XXXXXX)"
trap 'rm -rf "${RESULT_DIR}" 2>/dev/null || true' EXIT

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
    log "parse-error line=${line}"
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
  else
    echo "failed" > "${rfile}"
    [ "${FROM_DDB}" -eq 1 ] && _mark_assignment "${tid}" "failed" "launch-vm rc=${rc}"
    log "launch fail tenant=${tid} rc=${rc}"
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
  [ -n "${body}" ] || die "ddb source: zero pending assignments for ${INSTANCE_ID} after 3 tries (expected ~${PART_COUNT})"
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
    body=$(_get_part "${part_idx}") || die "part ${part_idx} unreadable after 3 retries"
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
      launched) launched=$((launched + 1)) ;;
      skipped)  skipped=$((skipped + 1)) ;;
      *)        failed=$((failed + 1)) ;;
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
# (log() writes to stderr so it doesn't pollute the JSON contract.)
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

log "DONE launched=${launched} skipped=${skipped} failed=${failed}"
# Exit non-zero if any launch failed so the Poller can classify the batch as
# partial/failed and roll back CAS + dispatch_retries+=1 per the contract.
if [ "${failed}" -gt 0 ]; then
  exit 1
fi
exit 0
