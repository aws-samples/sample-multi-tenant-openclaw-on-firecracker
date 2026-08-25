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
# ── manifest 行契约:【只传意图,不传坐标】────────────────────────────────────
# 决策见 engineering/00-knowledge-base/decisions/ADR-delete-fanout-manifest-contract.md
#   t = tenant_id                (必填)
#   o = active_lifecycle_op_id   (必填 —— 缺则拒删,见下)
#   f = lifecycle_fence_epoch    (必填 —— 缺则拒删,见下)
#   l = legacy_port              (缺省 0 —— 唯一保留的坐标类字段,理由见下)
# 载体沿用 launch 侧:${PARAM_PREFIX}/manifests/<command_id>/<INSTANCE_ID>/part-<n>
# SecureString,每 part ≤3800B(core/dispatch/manifest.py:23)。不引入第二套约定。
#
# ★ vm_num / host_port / guest_ip 【不在 manifest 里】,由 lifecycle-guard.sh 在它本来
#   就要做的那次 `get-item --consistent-read` 里取权威值(多投影三列,零额外往返)。
#   理由:围栏绑的是 tenant+租约+host,不绑坐标;manifest 里的 vm_num 一旦陈旧/错装,
#   整类"manifest 说 X、DDB 说 Y"在设计上不存在。
#
# ★ l(legacy_port)是唯一例外:它不是 DDB 里的状态,而是控制面用 `VM_PORT_BASE +
#   vm_num - 1` 推出的派生常量,而 VM_PORT_BASE 按设计只在控制面(clients.py:236),
#   host 侧无从推导(route_ops.py:803-805 也只接受显式传参)。且它【不构成跨租户风险】:
#   route_ops.py:820-821 的 `dnat_remove_all(legacy_port, guest_ip)` 以本租户
#   guest_ip 为键(现在是权威值),传错最多是删一条不存在的规则或本租户自己的陈旧规则。
#
# ★ k(keep_data)/ b(quiesced_backup)【不在 manifest 里,出现即拒行】。
#   fan-out 收窄成【只做软删】:keep_data=true / quiesced_backup=false 在下面硬编码,
#   于是磁盘销毁在本脚本里【不可达】(delete-vm.sh:104 与 :146 两条破坏性磁盘路径都以
#   KEEP_DATA != true 为门)。依据:批删入口传空 query(fleet_service.py:323)⇒
#   keep_data 恒 true(tenant_service.py:3245 缺省 "true")⇒ b 恒 false
#   (tenant_service.py:3349 要求 not keep_data)。既然是不变量,把它们放进 manifest
#   只是凭空造一个可以传错的通道 —— 而 k 传错是【不可逆的数据丢失】。
#   出现即 fail-loud 拒行(不是忽略):将来要做批量硬删的人会立刻撞明确报错,
#   而不是拿到"静默按软删处理"这个最坏结果;升级路径见 ADR §5(意图落库)。
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

# 为什么这里【自己写一段】而不复用 core.ssm_dispatch.host_script_self_heal:
#   那个函数 `ssm_dispatch.py:230-231` 显式 `if "/" in name: raise ValueError`(只收裸
#   文件名),而且不 mkdir —— 全仓唯一的 `mkdir -p /home/ubuntu/lib` 在 init-host.sh:689,
#   **只在开机时跑**。存量 host(本 MR 之前起的)上没有 lib/ 目录也没有这个脚本,
#   而控制面一上线就会调 fan-out。扩那个函数会把 9 条在役生命周期路径一起拖进来
#   (它们共用同一个入口),所以刻意在这里重复一小段。
# ★ 这是【刻意的重复,不是漏抽象】:它只服务 delete fan-out 这一条路径,失败语义也是
#   本路径专有的(fail-closed → 整批 defer → 控制面重投)。与别人共用会把两种重投契约
#   耦合在一起。改动时不要"顺手抽取"。
# 失败即 exit 1 让整批 defer,绝不 `|| true` —— 没有围栏脚本时每个租户都会判 78,
# 批删会永不收敛(比 #532 更隐蔽:方向"安全"却不收敛)。
_GUARD_PATH=/home/ubuntu/lib/lifecycle-guard.sh
if [ ! -s "${_GUARD_PATH}" ]; then
  _log "lib/lifecycle-guard.sh 缺失,从 S3 自愈装载(存量 host 首次收到 fan-out 命令)"
  mkdir -p /home/ubuntu/lib ||
    _die "mkdir -p /home/ubuntu/lib 失败,无法装载围栏脚本 —— 整批 defer"
  [ -n "${ASSETS_BUCKET:-}" ] ||
    _die "ASSETS_BUCKET 未在 /etc/platform.env 里,无法自愈装载围栏脚本 —— 整批 defer"
  aws s3 cp "s3://${ASSETS_BUCKET}/deployment/scripts/lib/lifecycle-guard.sh" \
    "${_GUARD_PATH}" --region "${REGION}" --no-progress >/dev/null 2>&1 ||
    _die "拉取 lib/lifecycle-guard.sh 失败 —— 拒绝在无围栏的状态下删除,整批 defer"
  chmod +x "${_GUARD_PATH}" ||
    _die "chmod +x lib/lifecycle-guard.sh 失败 —— 整批 defer"
  [ -s "${_GUARD_PATH}" ] || _die "自愈后 lib/lifecycle-guard.sh 仍为空 —— 整批 defer"
  _log "lib/lifecycle-guard.sh 自愈完成"
fi
# 上面三道 `_die`(cp / chmod / -s)【互为兜底】:变异实测,只把任一道换成 `|| true`
# 都还有另一道拦住(cp 失败 → 文件不存在 → chmod 失败),三道一起去掉才会真的带着
# 无围栏状态往下删。改动其一时不要以为另外两道是冗余。

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
# outfile 收 stdout 那一行权威坐标(`vm_num<TAB>host_port<TAB>guest_ip`)。同样用
# `o-` 前缀,理由与 errfile 的 `e-` 一样:任何 `r-…` 形状都会被结果 glob 数成一个结果。
_run_guard() {
  local tid="$1" op="$2" epoch="$3" errfile="$4" outfile="$5" rc=0
  /home/ubuntu/lib/lifecycle-guard.sh "${tid}" "${op}" "${epoch}" "${INSTANCE_ID}" \
    >"${outfile}" 2>"${errfile}" || rc=$?
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
#
# 坐标(vm_num/host_port/guest_ip)来自【前置围栏】的 stdout,不来自 manifest ——
# 见文件头的 manifest 契约那段。keep_data / quiesced_backup 硬编码成 true/false:
# fan-out 只做软删,磁盘销毁在本脚本里不可达。
_GUARD_EXPIRED_RC=80
_delete_one() {
  local tid="$1" op="$2" epoch="$3" legacy_port="$4" errfile="$5" outfile="$6"
  local rc=0 coords vm_num host_port guest_ip
  _run_guard "${tid}" "${op}" "${epoch}" "${errfile}" "${outfile}" || rc=$?
  [ "${rc}" -eq 0 ] || return "$(_classify_guard_rc "${rc}" "${errfile}")"

  coords="$(cat "${outfile}" 2>/dev/null || echo "")"
  vm_num="$(printf '%s' "${coords}" | cut -f1)"
  host_port="$(printf '%s' "${coords}" | cut -f2)"
  guest_ip="$(printf '%s' "${coords}" | cut -f3)"
  # 围栏在四参形态下必打这一行;打不出来(空/非数字)= 拿不到权威坐标,fail-closed。
  # 绝不退回 manifest 或默认值 —— 那正是本次契约改造要消掉的那条路。
  # 这道判定与围栏里的 LIFECYCLE_COORDS_MISSING 【互为兜底】:变异实测,只去掉任一道
  # 都还有另一道拦住(围栏放行 "None" 时这里的非数字判定会接住),两道一起去掉才会
  # 真的拿着空坐标去 stop 一个错的 tap。
  case "${vm_num}" in
    ''|*[!0-9]*)
      _log "coords missing tenant=${tid} —— 围栏没给出权威 vm_num,拒删(留 deleting 重投)"
      return 78
      ;;
  esac

  _run_delete_vm "${tid}" "${vm_num}" "${host_port}" "${guest_ip}" \
    "${legacy_port}" "true" "false" || rc=$?
  [ "${rc}" -eq 0 ] || return 1
  _run_guard "${tid}" "${op}" "${epoch}" "${errfile}" "${outfile}" || rc=$?
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
# 坐标不再从这里取(见文件头的 manifest 契约):本函数只解意图 + 围栏凭据,
# `vm_num`/`host_port`/`guest_ip` 由 _delete_one 从前置围栏的 stdout 拿权威值。
# 这一改也顺带消掉了"上一行的值留给下一行"整类问题 —— 那三个变量现在根本不经过 manifest。
# 字段一律 `jq -r` 取,绝不 sed/正则(launch-vm.sh:172 已写明 regex 会在任何未来
# 字段新增时崩),也绝不把行内容当 shell 代码求值 —— tid 里的 `; rm -rf /` /
# `$( )` / 反引号 / 换行 / `--` 前缀全部只是数据。变量全 local(纵深防御,见 launch-vm.sh:29)。
_dispatch_line() {
  local line="$1" rfile="$2" errfile="$3" outfile="$4"
  local tid op epoch legacy_port has_kb rc
  tid=$(jq -r '.t // empty' <<<"${line}" 2>/dev/null)
  op=$(jq -r '.o // empty' <<<"${line}" 2>/dev/null)
  epoch=$(jq -r '.f // empty' <<<"${line}" 2>/dev/null)
  legacy_port=$(jq -r '.l // 0' <<<"${line}" 2>/dev/null)
  # k/b 出现即拒行(不是忽略):fan-out 只做软删,磁盘销毁不可达。带 k/b 的行说明有人
  # 想用 fan-out 硬删 —— 必须撞明确报错,而不是拿到"静默按软删处理"这个最坏结果。
  has_kb=$(jq -r 'if (has("k") or has("b")) then "1" else "" end' <<<"${line}" 2>/dev/null)
  # tid 先落 .tid:哪怕后面判 failed,控制面也要知道是【谁】失败了。
  # 存的是【JSON 编码后】的字符串(带引号),不是裸 tid —— 见 _result_list 的说明:
  # 手拼 `"${tid}"` 在含双引号/换行的 tid 上会造出畸形 JSON。这里用 jq --arg 一次编码,
  # 汇总处直接拼即可(--arg 是数据通道,jq 1.5 起就有,不依赖 1.7 的 `--` 语义)。
  [ -n "${tid}" ] && jq -n --arg t "${tid}" '$t' > "${rfile}.tid"
  if [ -n "${has_kb}" ]; then
    echo "failed" > "${rfile}"
    _log "reject tenant='${tid}' 行里带 k/b —— fan-out 只做软删,不支持批量硬删;硬删走 per-tenant 路径"
    return
  fi
  if [ -z "${tid}" ] || [ -z "${op}" ] || [ -z "${epoch}" ]; then
    echo "failed" > "${rfile}"
    # 不打印整行:该行含 op_id 之类的操作凭据,进 journald 无必要。只报缺了什么。
    _log "parse-error tid='${tid}' 缺 t/o/f 之一 — 拒删(无围栏凭据绝不动手)"
    return
  fi
  _delete_one "${tid}" "${op}" "${epoch}" "${legacy_port}" "${errfile}" "${outfile}"
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
    _dispatch_line "${line}" "${rfile}" "${RESULT_DIR}/e-${job_seq}" \
      "${RESULT_DIR}/o-${job_seq}" &
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
