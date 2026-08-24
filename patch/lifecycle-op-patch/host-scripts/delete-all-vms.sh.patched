#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# delete-all-vms.sh — HOST-LOCAL fan-out: 删掉本 host 上【清单里指定的】那批租户。
#
# 用法: delete-all-vms.sh --manifest <command_id> <part_count> [max_parallel] [param_prefix]
# 退出: 0=清单里每个租户都已 deleted | 1=有 failed/deferred/superseded(见 stdout 清单)
#
# stdout 【只有】一行 v2 JSON(控制面 GetCommandInvocation 解它);所有日志走 stderr。
# 子进程(delete-vm.sh / lifecycle-guard.sh)的输出一律不许流到本进程 stdout,否则
# 会把那行 JSON 冲烂 —— 见 _run_delete_vm / _run_guard 里的重定向。
#
# ── 与 stop-all-vms.sh 的关键差别:清单驱动,不是目录驱动 ────────────────────
# stop-all-vms.sh 遍历 /data/firecracker-vms/*/ 停光所有 VM。删除【绝不能】这么做:
# 目录里有的是控制面这一批没打算删的租户,按目录删就是"删了没人要求删的租户"。
# 故本脚本只处理 manifest 里逐行给出的租户,一行一租户,清单外的目录一个都不碰。
#
# ── manifest 行契约(JSON-lines,短键,与 launch 侧同一套约定)─────────────────
#   t = tenant_id           (必填)
#   n = vm_num              (必填)
#   o = active_lifecycle_op_id   (必填 —— 缺则拒删,见下)
#   f = lifecycle_fence_epoch    (必填 —— 缺则拒删,见下)
#   p = host_port            (缺省 0)
#   i = guest_ip             (缺省空)
#   l = legacy_port          (缺省 0)
#   k = keep_data true|false (缺省 true = 软删保盘,与 delete-vm.sh:45 同一缺省方向)
#   b = quiesced_backup      (缺省 false)
# 载体沿用 launch 侧:${PARAM_PREFIX}/manifests/<command_id>/<INSTANCE_ID>/part-<n>
# SecureString,每 part ≤3800B(core/dispatch/manifest.py:23)。不引入第二套约定。
#
# ★ o/f 必填且缺失即【拒删】:o/f 是这一行的 per-tenant 围栏凭据。缺了就没有围栏,
#   【绝不】调用 delete-vm.sh(测试里有这一档)。
#   这道判定与 lifecycle-guard.sh 顶部的 `${2:?}`/`${3:?}` 【互为兜底】:变异实测,
#   只去掉任一道都还有另一道拦住,两道一起去掉才会真的下发无围栏删除。改动其一时
#   不要以为另一处是冗余。
#
# ── 幂等 ───────────────────────────────────────────────────────────────────
# 不新增任何"已删就跳过"的判据。delete-vm.sh 四步各自对"已完成"无害(stop 对已停
# VM no-op、rm -f 对不存在文件返 0、delete-route 幂等、rm -rf 对已删目录返 0),
# SSM 重投直接重跑即可。刻意【不】加"vm.json 不存在就跳过":软删的目录仍在、而
# vm.json 已删也可能是上一次跑到第 ② 步就挂了 —— 跳过会漏掉后面的路由/盘清理。
#
# ── 退出码语义:方向与 launch-vm.sh 的 fan_out_main 【相反】────────────────
# launch 侧"清单里没出现的租户"保持 creating 等 900s reaper 兜底,"不管它"是安全的。
# 删除侧相反:没人来兜。health_check/handler.py:1539-1544 明文把 `deleting` 排除在
# _reap_stuck_lifecycle 只扫 suspending/restoring。所以删除侧的"不管它"= 永久泄漏。
# ⇒ 本脚本对每个租户都必须给出结论,由控制面按清单逐个清算:
#     deleted    → 走原有收尾(令牌释放/账本/route/vkey)
#     failed     → 留 deleting + delete_retryable,重投
#     deferred   → 围栏读不出来(78)【或租约已过期】,留 deleting 重投
#     superseded → 79 且哨兵是 LIFECYCLE_SUPERSEDED:另一个【活着的】op 持有租约,
#                  留 deleting 但【不重投本 op】—— 持锁那个 op 会把活做完。
#   ★ 租约【过期】刻意归 deferred 而不是 superseded:过期意味着【没有】owner,没有任何
#     人会来接手,而 deleting 不被任何 reaper 回收 → 不重投就是永久泄漏。详见 _delete_one。
#   清单里没出现的租户由控制面判 unknown 并重投(host 侧无从知道自己漏了谁)。
# 也【不】沿用 launch 的 75(flock-skip)哨兵:delete-vm.sh:9-14 已明确拒绝那个语义
# ——"抢不到锁"和"某步真失败"对控制面的正确处理相同(留 deleting 等重投)。

set -uo pipefail

MODE="${1:?usage: delete-all-vms.sh --manifest <command_id> <part_count> [max_parallel] [param_prefix]}"
[ "${MODE}" = "--manifest" ] || {
  echo "[oc:delete-all] FATAL 不认识的模式 '${MODE}'(只支持 --manifest)" >&2
  exit 1
}
shift
COMMAND_ID="${1:?usage: delete-all-vms.sh --manifest <command_id> <part_count> [max_parallel] [param_prefix]}"
PART_COUNT="${2:?usage: delete-all-vms.sh --manifest <command_id> <part_count> [max_parallel] [param_prefix]}"
# 64 是【未实测】的起点:delete-vm.sh 含优雅关机(ctrl-alt-del → SIGTERM → SIGKILL),
# 比 stop-all-vms.sh 的 128 重、比 launch 的 FC 冷启动(96)轻,故取中间。真机测出
# 新速率后再调,不要把这个数当测量值。
MAX_PARALLEL="${3:-64}"
PARAM_PREFIX="${4:-${DISPATCH_PARAM_PREFIX:-/openclaw/dispatch}}"

_log() { echo "[oc:delete-all] $(date +%H:%M:%S) $*" >&2; }
_die() { _log "FATAL: $*"; rm -rf "${RESULT_DIR:-}" 2>/dev/null || true; exit 1; }

if [ -f /etc/platform.env ]; then
  # shellcheck disable=SC1091
  . /etc/platform.env
fi
REGION="${OC_REGION:-${AWS_REGION:-}}"
[ -n "${REGION}" ] || _die "OC_REGION empty in /etc/platform.env — init-host.sh didn't run?"
[ -n "${INSTANCE_ID:-}" ] || _die "INSTANCE_ID empty in /etc/platform.env — init-host.sh didn't run?"

_log "start command_id=${COMMAND_ID} parts=${PART_COUNT} parallel=${MAX_PARALLEL} host=${INSTANCE_ID}"

# ── 取一个 manifest part,指数退避 + $RANDOM 抖动,三次全失败即 fail-loud ────
# 与 launch-vm.sh:58-73 同款。绝不"读不到就当空清单"—— 那会删 0 个租户然后报成功。
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

# 子进程输出绝不许上 stdout(那里只放一行 v2 JSON)。有 systemd-cat 时把 per-tenant
# 诊断送进 journald(tag claw-delete → Fluent Bit host.platform → AOS claw-logs-host,
_SDCAT=0
command -v systemd-cat >/dev/null 2>&1 && _SDCAT=1

# 围栏的 stderr 要【留下来】而不只是转发:exit 79 混了三种情形,只有哨兵串能区分,
# 而它们的重投语义相反(见 _delete_one 上方那段)。errfile 用 `e-` 前缀而不是 `.err`
# 后缀 —— 结果聚合的 glob 是 `r-*`,`.err` 后缀会被数成一个结果文件进而计入 failed
# (launch-vm.sh:287 标为「真机第 6 bug」的同款陷阱)。
# 第 4 参 = 本机 INSTANCE_ID:让围栏顺带以 DDB 的 host_id 为准断言"这个租户真在本机"。
# 围栏本身无 host 维度,而 fan-out 最危险的失效正是"一行落到错误的 host" —— 那时
# 跨租户损伤)。不信 manifest 里的分组,只信 DDB。
_run_guard() {
  local tid="$1" op="$2" epoch="$3" errfile="$4" rc=0
  /home/ubuntu/lib/lifecycle-guard.sh "${tid}" "${op}" "${epoch}" "${INSTANCE_ID}" \
    >/dev/null 2>"${errfile}" || rc=$?
  cat "${errfile}" >&2 2>/dev/null || true
  return "${rc}"
}

_run_delete_vm() {
  if [ "${_SDCAT}" -eq 1 ]; then
    systemd-cat -t claw-delete bash /home/ubuntu/delete-vm.sh "$@"
  else
    bash /home/ubuntu/delete-vm.sh "$@" >&2
  fi
}

# ── 删一个租户:破坏性动作【前后】各夹一次 per-tenant 围栏 ──────────────────
# 逐字对齐单删路径 tenant_service.py:3514/3518 的 `guard && delete-vm.sh && guard`。
# 前置 guard 确认本 op 仍持租约;后置 guard 确认整个破坏性序列期间没被抢占。
# 参数串号即使发生,guard 用的也是【该 tid 自己那行 DDB】的 op_id/epoch,不匹配就
# exit 79 什么都不做(R1 第四道防线)。
#
# ★ exit 79 必须再分成两类(Codex 独立复审 CHANGES_NEEDED,已复核成立)。
#   lifecycle-guard.sh 对三种情形都退 79(:88 owner 不符 / :91 epoch 不符 / :94 租约
#   过期),但它们的重投语义【相反】:
#     · owner/epoch 不符 = 另一个【活着的】op 持有租约 → 它会把活干完 → 本 op 不该重投
#       (重投会撞 acquire 冲突 → 5 次进 DLQ + 告警,给一个正在被处理的租户报假警);
#     · 租约【过期】= 根本【没有】owner → 没有任何人会来接手。而
#       所以"不重投"在这一档等于把租户永久钉在 deleting = 永久泄漏,正是本脚本头部
#       那条「删除侧的『不管它』= 永久泄漏」自己禁止的事。
#   我第一版把 79 一律判 superseded(不重投),就是踩了这个。
#   修法不动退出码(否则与 lifecycle_fence.host_guard() 的等价性就断了,而那道
#   equivalence 门是仓里两份实现不漂的唯一凭据),改为读 guard 自己打出的哨兵串:
#   LIFECYCLE_FENCE_EXPIRED → 内部 80(可重投),其余 79 → superseded(不重投)。
_GUARD_EXPIRED_RC=80
_delete_one() {
  local tid="$1" vm_num="$2" op="$3" epoch="$4"
  local host_port="$5" guest_ip="$6" legacy_port="$7" keep_data="$8" quiesced="$9"
  local errfile="${10}" rc=0
  _run_guard "${tid}" "${op}" "${epoch}" "${errfile}" || rc=$?
  [ "${rc}" -eq 0 ] || return "$(_classify_guard_rc "${rc}" "${errfile}")"
  _run_delete_vm "${tid}" "${vm_num}" "${host_port}" "${guest_ip}" \
    "${legacy_port}" "${keep_data}" "${quiesced}" || rc=$?
  [ "${rc}" -eq 0 ] || return 1
  _run_guard "${tid}" "${op}" "${epoch}" "${errfile}" || rc=$?
  [ "${rc}" -eq 0 ] || return "$(_classify_guard_rc "${rc}" "${errfile}")"
  return 0
}

# 79 + LIFECYCLE_FENCE_EXPIRED → 80(无 owner,必须重投);其余原样透传。
_classify_guard_rc() {
  local rc="$1" errfile="$2"
  if [ "${rc}" -eq 79 ] && grep -q 'LIFECYCLE_FENCE_EXPIRED' "${errfile}" 2>/dev/null; then
    printf '%s' "${_GUARD_EXPIRED_RC}"
    return 0
  fi
  printf '%s' "${rc}"
}

# ── 解析一行 manifest 并派发 ────────────────────────────────────────────────
# 防"上一个租户的值留给下一个"(= 摘 A 的 DNAT 记在 B 头上)靠三道,按有效性排序:
#   ① 每个可选字段都带 jq 默认值(`.p // 0` / `.i // ""` / `.l // 0`)—— 缺字段也会被
#      【赋值】,变量永远不可能是上一行的残留。这是真正起作用的那道:变异测试实测,
#      把 `.p // 0` 改成 `.p // empty` 立刻让跨租户用例变红(其余两道都拦不住)。
#   ② 每行一个子进程(下方 `_dispatch_line ... &`),进程隔离本身就断了共享。
#   ③ local。①② 已经关住,故这道是纵深防御 —— 单独去掉它测不出差别(已实测)。
#      留着的理由是它让①② 之一被将来改掉时不至于立刻裸奔,同款陷阱在
#      launch-vm.sh:29 是 FAN_VM_DIR 必须 local。
# 字段一律 `jq -r` 取,绝不 sed/正则(launch-vm.sh:172 已写明 regex 会在任何未来
# 字段新增时崩),也绝不把行内容当 shell 代码求值 —— tid 里的 `; rm -rf /` /
# `$( )` / 反引号 / 换行 / `--` 前缀全部只是数据。
_dispatch_line() {
  local line="$1" rfile="$2" errfile="$3"
  local tid vm_num op epoch host_port guest_ip legacy_port keep_data quiesced rc
  tid=$(jq -r '.t // empty' <<<"${line}" 2>/dev/null)
  vm_num=$(jq -r '.n // empty' <<<"${line}" 2>/dev/null)
  op=$(jq -r '.o // empty' <<<"${line}" 2>/dev/null)
  epoch=$(jq -r '.f // empty' <<<"${line}" 2>/dev/null)
  host_port=$(jq -r '.p // 0' <<<"${line}" 2>/dev/null)
  guest_ip=$(jq -r '.i // ""' <<<"${line}" 2>/dev/null)
  legacy_port=$(jq -r '.l // 0' <<<"${line}" 2>/dev/null)
  # k/b 只认【真布尔】。故意不宽松到字符串 "false":`k` 判错的方向不对称 —— 判成
  # keep_data=true 是"盘留着没删"(可补删),判成 false 是"盘删了"(不可逆)。所以任何
  # 形状不对/缺失/jq 失败的输入都必须落在 true 这一侧。控制面写 manifest 用
  # json.dumps(Python bool → JSON false),类型是对的;这里守的是它哪天不对。
  keep_data=$(jq -r 'if .k == false then "false" else "true" end' <<<"${line}" 2>/dev/null)
  quiesced=$(jq -r 'if .b == true then "true" else "false" end' <<<"${line}" 2>/dev/null)
  # tid 先落 .tid:哪怕后面判 failed,控制面也要知道是【谁】失败了。
  # 存的是【JSON 编码后】的字符串(带引号),不是裸 tid —— 见 _result_list 的说明:
  # 手拼 `"${tid}"` 在含双引号/换行的 tid 上会造出畸形 JSON。这里用 jq --arg 一次编码,
  # 汇总处直接拼即可(--arg 是数据通道,jq 1.5 起就有,不依赖 1.7 的 `--` 语义)。
  [ -n "${tid}" ] && jq -n --arg t "${tid}" '$t' > "${rfile}.tid"
  if [ -z "${tid}" ] || [ -z "${vm_num}" ] || [ -z "${op}" ] || [ -z "${epoch}" ]; then
    echo "failed" > "${rfile}"
    # 不打印整行:该行含 op_id 之类的操作凭据,进 journald 无必要。只报缺了什么。
    _log "parse-error tid='${tid}' 缺 t/n/o/f 之一 — 拒删(无围栏凭据绝不动手)"
    return
  fi
  _delete_one "${tid}" "${vm_num}" "${op}" "${epoch}" \
    "${host_port}" "${guest_ip}" "${legacy_port}" "${keep_data}" "${quiesced}" \
    "${errfile}"
  rc=$?
  case "${rc}" in
    0)  echo "deleted"    > "${rfile}" ;;
    78) echo "deferred"   > "${rfile}"
        _log "defer(fence unreadable) tenant=${tid} rc=78 — 什么都没做,留 deleting 重投" ;;
    80) echo "deferred"   > "${rfile}"
        _log "defer(lease expired) tenant=${tid} — 租约过期【无人持有】,没有 reaper 会来收 deleting,必须重投" ;;
    79) echo "superseded" > "${rfile}"
        _log "superseded tenant=${tid} rc=79 — 另一个活 op 持有租约,留 deleting 且不重投本 op(它会做完)" ;;
    81) echo "failed"     > "${rfile}"
        _log "HOST_MISMATCH tenant=${tid} — DDB 说它不在本机,拒删(控制面分组错或迁移竞态);留 deleting 重投" ;;
    *)  echo "failed"     > "${rfile}"
        _log "delete fail tenant=${tid} rc=${rc} — 留 deleting 重投(delete-vm.sh 每步幂等)" ;;
  esac
}

# job 计数信号量,与 launch-vm.sh:161 / stop-all-vms.sh:32 同一实现。
_wait_for_slot() {
  while [ "$(jobs -rp | wc -l)" -ge "${MAX_PARALLEL}" ]; do
    wait -n 2>/dev/null || sleep 0.2
  done
}

# 每子进程一个 tempfile 写一个词 + 一个 <rfile>.tid。bash 不能从后台作业回传值,
# 共用一个文件会 race。
# ★ mktemp 失败必须立刻死(Codex 独立复审第三轮):不检查的话 RESULT_DIR 是空串,
#   `${RESULT_DIR}/r-N` 退化成【绝对路径】`/r-N` —— 而 SSM 命令是 root 跑的,真能写进
#   文件系统根;两次运行还会互相读到对方的 `/r-*`,把上一批的结果当本批的报上去。
RESULT_DIR="$(mktemp -d /tmp/oc-delete-all.XXXXXX)" ||
  _die "mktemp -d 失败,拒绝在无结果目录的情况下动手删(否则结果会落到 / 下且跨运行串号)"
[ -n "${RESULT_DIR}" ] && [ -d "${RESULT_DIR}" ] ||
  _die "mktemp -d 返回空/非目录,拒绝继续"

job_seq=0
part_idx=0
while [ "${part_idx}" -lt "${PART_COUNT}" ]; do
  body=$(_get_part "${part_idx}") || _die "part ${part_idx} unreadable after 3 retries"
  while IFS= read -r line; do
    [ -z "${line}" ] && continue
    _wait_for_slot
    rfile="${RESULT_DIR}/r-${job_seq}"
    # errfile 用 `e-` 前缀:结果聚合的 glob 是 `r-*`,任何 `r-…` 形状的附属文件都会被
    # 数成一个结果(launch-vm.sh:287「真机第 6 bug」同款),所以刻意不用后缀。
    _dispatch_line "${line}" "${rfile}" "${RESULT_DIR}/e-${job_seq}" &
    job_seq=$((job_seq + 1))
  done <<<"${body}"
  part_idx=$((part_idx + 1))
done

wait

deleted=0
failed=0
deferred=0
superseded=0
accounted=0
if [ -d "${RESULT_DIR}" ]; then
  for f in "${RESULT_DIR}"/r-*; do
    # .tid 是附属文件,不是结果 —— 数进来就会被 *) 计成 failed(launch-vm.sh:287
    # 标为"真机第 6 bug",同款一字不改地防住)。
    case "${f}" in *.tid) continue ;; esac
    [ -f "${f}" ] || continue
    accounted=$((accounted + 1))
    v=$(cat "${f}" 2>/dev/null || echo "failed")
    case "${v}" in
      deleted)    deleted=$((deleted + 1)) ;;
      deferred)   deferred=$((deferred + 1)) ;;
      superseded) superseded=$((superseded + 1)) ;;
      *)          failed=$((failed + 1)) ;;
    esac
  done
fi

# ★ 每一行都必须有一个结果文件(Codex 独立复审第三轮)。
# 子进程被 OOM-kill、tempfile 写失败、或 RESULT_DIR 被外力清掉时,那一行会【从四个清单
# 里同时消失】—— 控制面看到的是一份"短"报告,而它无法从报告本身看出短了。此时
# 那些租户落进"清单里没出现"= unknown → 会被重投,方向是安全的;但绝不允许在这种
# 情况下 exit 0,否则控制面会把整批当成"全都处理过了"。
# 不用 `wait` 的退出码做这件事:它只说"有子进程非零",而我们的判据是"有没有人失踪",
# 且正常流程里子进程非零(failed/deferred)本来就是预期的。
unaccounted=$((job_seq - accounted))
if [ "${unaccounted}" -gt 0 ]; then
  _log "FATAL 清单不完整:派发 ${job_seq} 行,只回收到 ${accounted} 个结果 —— ${unaccounted} 个租户无结果文件(子进程被杀/写盘失败?)。报告照发但整批置非零,缺的那些由控制面按 unknown 重投。"
fi

# per-tenant 清单是控制面清算的【唯一可信来源】。反查 DDB"还有谁是 deleting"不能
# 代替它:那分不清"本批失败"与"上一批遗留"(dispatch_poller._get_ssm_status 的
# docstring 记的是同一条教训 —— 执行体自报"我处理了谁"是唯一不会被从下面抽走的关联)。
# 预算:380 ids × ~20B ≈ 8KB < GetCommandInvocation 的 24,000 字符 stdout 上限。
#
# ★ .tid 里存的已经是 jq 编码好的 JSON 字符串(见 _dispatch_line),这里只做拼接。
#   绝不手拼 `"${tid}",`(launch-vm.sh:318 是手拼的 —— 那是既有形态,不在本 issue
#   范围内改,但这里【不能】照抄):tenant_id 是外部可影响的输入,含双引号会造出畸形
#   JSON,含【换行】更直接把这一行劈成两行 —— 控制面 json.loads 整批解不出来 → 该
#   host 全部落 unknown → 每次重投撞同一份 manifest,永不收敛。已由测试实测证伪过。
#
# ★ 控制面必须按【计数】而不是清单长度判收敛:某一行连 tid 都解不出来时,它进了
#   failed 的【计数】却进不了 failed[](我们不知道它是谁)。于是
#   `failed > len(tenants.failed)` 是合法状态,含义是"这一批里有无法归属的坏行" ——
#   控制面据此该告警并人工看 manifest,不能当成"清单为空所以没失败"。
_result_list() {
  local want="$1" out="" f v tidjson
  for f in "${RESULT_DIR}"/r-*; do
    case "${f}" in *.tid) continue ;; esac
    [ -f "${f}" ] || continue
    v=$(cat "${f}" 2>/dev/null || echo "failed")
    tidjson=$(cat "${f}.tid" 2>/dev/null || echo "")
    [ "${v}" = "${want}" ] && [ -n "${tidjson}" ] && out="${out}${tidjson},"
  done
  printf '[%s]' "${out%,}"
}
# dispatched 让控制面能自己核对"派发了几行 / 回收到几个结果",不必靠数四个清单的长度
# (那还会被"连 tid 都解不出来的坏行"混淆,见上方 _result_list 的说明)。
printf '{"v":2,"dispatched":%d,"deleted":%d,"failed":%d,"deferred":%d,"superseded":%d,"host":"%s","command_id":"%s","tenants":{"deleted":%s,"failed":%s,"deferred":%s,"superseded":%s}}\n' \
  "${job_seq}" "${deleted}" "${failed}" "${deferred}" "${superseded}" \
  "${INSTANCE_ID}" "${COMMAND_ID}" \
  "$(_result_list deleted)" "$(_result_list failed)" \
  "$(_result_list deferred)" "$(_result_list superseded)"

_log "DONE dispatched=${job_seq} deleted=${deleted} failed=${failed} deferred=${deferred} superseded=${superseded}"
rm -rf "${RESULT_DIR}" 2>/dev/null || true

# 整批非零只是告诉控制面"去看清单",【不】触发批量回滚 —— 成功的那 M-k 个照常收尾。
# 判 partial 回滚)。
# 有租户失踪(unaccounted>0)也必须非零:哪怕四个清单里一个 failed 都没有,
# "报告是短的"这件事本身就必须让控制面去看。
if [ "$((failed + deferred + superseded + unaccounted))" -gt 0 ]; then
  exit 1
fi
exit 0
