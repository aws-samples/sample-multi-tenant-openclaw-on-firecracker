#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
#
# 用法: lifecycle-guard.sh <tenant_id> <op_id> <fence_epoch> [expected_host_id]
# 退出: 0=本操作仍持有该租户的生命周期租约(可以动手)
#       78=fence 读不出来,【或权威坐标缺失】(fail-closed,留 deleting 等重投)
#       79=已被抢占 / epoch 前进 / 租约过期(本 op 已失效)
#       81=该租户按 DDB 不在本机(仅四参形态;控制面分组错或迁移竞态)
#
# 四参形态额外在 **stdout** 打印一行权威坐标:`vm_num<TAB>host_port<TAB>guest_ip`。
# 三参形态 stdout 恒空 —— 那是与 `lifecycle_fence.host_guard()` 逐档等价的形态。
# 见文件末尾"为什么坐标由这里返回"。
#
# ── 为什么需要一个 host 侧的可执行 guard ────────────────────────────────────
# 控制面原本用 `core/lifecycle_fence.py:host_guard()` 为【单个】租户生成一段 shell
# fan-out 里一条聚合 SSM 命令带 M 个租户,每个租户必须评估【自己那一份】租约 ——
# 而把 M 段 guard 文本拼进一条命令不可行:380 × ~700B ≈ 266KB,必撞 SSM 参数上限,
# 且注入面随租户数线性放大。故把判定逻辑下到 host,按 tid 传参调用。
#
# ── 与 lifecycle_fence.host_guard() 的关系 ─────────────────────────────────
# 本脚本是那段片段的【逐条等价】实现:同一次 get-item --consistent-read、同样的
# "空则重读一次"、同样的 owner→epoch→until 三段判定、同样的 78/79 与同样的 stderr
# 哨兵串(LIFECYCLE_FENCE_READ_FAILED / LIFECYCLE_SUPERSEDED / LIFECYCLE_FENCE_EXPIRED)。
# 等价性不靠"我读过觉得一样":tests/test_241_delete_all_vms_fanout_adversarial.py 的
# equivalence 用例把两者放进同一个 bash + 同一套 aws 桩,逐档对跑退出码守住。
# migrate/restart/stop/start/resize 九条在役路径一起拖进来(它们都经
# tenant_service.py:5759 的 _lifecycle_host_guard),而本 issue 只要求删除侧对称。
# 收敛成一份实现留给控制面编排那一版(见 changelog fragment 的"剩余清单")。
#
# ── 唯一的刻意差异 ─────────────────────────────────────────────────────────
# Python 版把 table/region 烤进命令文本;本脚本从 /etc/platform.env 读(host 上
# TENANTS_TABLE / OC_REGION 就在那里,与 reset-vm.sh:49 / rebuild-vm.sh:54 同款)。
# 两者取不到时【都】落在 78:Python 版会发出 `--region ''` 让 aws 调用失败 → 读不到
# → 78;本脚本直接判空 → 78。退出码一致,只是本脚本的 stderr 说得更清楚。
set -uo pipefail

TENANT_ID="${1:?usage: lifecycle-guard.sh <tenant_id> <op_id> <fence_epoch> [expected_host_id]}"
OP_ID="${2:?usage: lifecycle-guard.sh <tenant_id> <op_id> <fence_epoch> [expected_host_id]}"
FENCE_EPOCH="${3:?usage: lifecycle-guard.sh <tenant_id> <op_id> <fence_epoch> [expected_host_id]}"
# 第 4 个参数(可选)= 本机 instance id。给了就额外断言 DDB 里该租户的 host_id 就是本机,
# 不匹配 exit 81。见文件末尾那段说明:三参形态与 host_guard() 逐档等价,四参形态是
# host 级 fan-out 专用的加固,只有 delete-all-vms.sh 会传。
EXPECT_HOST="${4:-}"

# 只 source platform.env。刻意【不】碰 /etc/environment:Ubuntu 在那里【定义 PATH】,
# source 它会覆盖调用方的 PATH(delete-vm.sh 的测试里已经踩过这一脚,见
# test_469_delete_vm_atomic_adversarial.sh 顶部关于 env 文件必须一起重写的说明)。
if [ -f /etc/platform.env ]; then
  # shellcheck disable=SC1091
  . /etc/platform.env
fi
TENANTS_TABLE="${TENANTS_TABLE:-openclaw-tenants}"
REGION="${OC_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"

if [ -z "${TENANTS_TABLE}" ] || [ -z "${REGION}" ]; then
  echo "LIFECYCLE_FENCE_READ_FAILED table/region unresolved" >&2
  exit 78
fi

# key 用 jq 构造而不是 printf 拼串:tenant_id 是外部输入,拼串在含引号/反斜线/换行的
# id 上会产出非法 JSON,甚至改变 --key 的语义。jq --arg 是数据通道,不是代码通道。
_KEY="$(jq -cn --arg id "${TENANT_ID}" '{id:{S:$id}}')" || {
  echo "LIFECYCLE_FENCE_READ_FAILED key encode failed" >&2
  exit 78
}

_read_fence() {
  aws dynamodb get-item \
    --table-name "${TENANTS_TABLE}" \
    --region "${REGION}" \
    --key "${_KEY}" \
    --consistent-read \
    --query "[Item.active_lifecycle_op_id.S,Item.lifecycle_fence_epoch.N,Item.active_lifecycle_until.N,Item.host_id.S,Item.vm_num.N,Item.host_port.N,Item.guest_ip.S]" \
    --output text 2>/dev/null
}

# 两次读:与 Python 版逐条对应。第一次空(DDB 瞬时错误 / CLI 非零)时重读一次,
# 仍空即 fail-closed。绝不"读不到就放行"—— 那等于在无围栏状态下做破坏性动作。
_LF="$(_read_fence)" || _LF=""
if [ -z "${_LF}" ]; then
  _LF="$(_read_fence)" || _LF=""
fi
[ -n "${_LF}" ] || { echo "LIFECYCLE_FENCE_READ_FAILED" >&2; exit 78; }

# --output text 的各列用 TAB 分隔。租户不存在时 aws 返 "None\tNone\tNone\tNone",
# 于是下面的判定全部失配 → 79。这与 Python 版行为一致(不另开"租户不存在"分支)。
_LF_OWNER="$(printf "%s" "${_LF}" | cut -f1)"
_LF_EPOCH="$(printf "%s" "${_LF}" | cut -f2)"
_LF_UNTIL="$(printf "%s" "${_LF}" | cut -f3)"
_LF_HOST="$(printf "%s" "${_LF}" | cut -f4)"
_LF_VMNUM="$(printf "%s" "${_LF}" | cut -f5)"
_LF_HPORT="$(printf "%s" "${_LF}" | cut -f6)"
_LF_GIP="$(printf "%s" "${_LF}" | cut -f7)"

# ── 判定顺序:【过期先判】,再判 owner/epoch(Codex 独立复审第二轮)────────────
# 三种情形都退 79,但调用方的重投语义相反:owner/epoch 不符 = 另一个【活着的】op 持有
# 租约(它会把活做完)→ 不该重投;租约过期 = 【没有】owner → 必须有人重投,否则
#
# 原来 owner 判在前,于是「租约过期【且】owner 还是别人」这一档会打出
# LIFECYCLE_SUPERSEDED —— 调用方据此不重投,而那个 owner 的租约本身也已过期、不会再动,
# 租户就永久钉在 deleting。过期提到最前面后,只要过期就一定是 EXPIRED(可重投),
# 「另有活 owner」才可能是 SUPERSEDED,两者不再混。
#
# ★ 退出码【一列都没变】(仍全是 79),只有哨兵串变。所以:
#   · 与 lifecycle_fence.host_guard() 的退出码等价性不受影响(那道 equivalence 门是
#     仓里两份实现不漂的唯一凭据),Python 侧同步做了同样的重排;
#   · 既有 9 条在役生命周期路径只看"零/非零",行为逐字不变;
#   · tests/test_413_lifecycle_fence.py:186-188 只断言三个哨兵都在,不依赖顺序。
[ -n "${_LF_UNTIL}" ] && [ "${_LF_UNTIL}" -gt "$(date +%s)" ] || {
  echo "LIFECYCLE_FENCE_EXPIRED" >&2; exit 79
}
[ "${_LF_OWNER}" = "${OP_ID}" ] || {
  echo "LIFECYCLE_SUPERSEDED owner=${_LF_OWNER}" >&2; exit 79
}
[ "${_LF_EPOCH}" = "${FENCE_EPOCH}" ] || {
  echo "LIFECYCLE_SUPERSEDED epoch=${_LF_EPOCH}" >&2; exit 79
}

# ── ④ 可选:租户是否真的属于本机(Codex 独立复审第二轮的另一条)──────────────
# 围栏本身是【无 host 维度】的:它只证明"本 op 仍持有该租户的生命周期租约",不证明
# "该租户在这台机器上"。host 级 fan-out 里这是个真缺口 —— 控制面分组写错、或 CAS 之后
# 租户被迁走,一行就会落到错误的 host 上;此时 tid/op/epoch 全都对得上,围栏放行,
# 然后 delete-vm.sh 会拿【别人机器上的】vm_num/host_port/guest_ip 去停 tap-vm<n>、
# 故 fan-out 传第 4 参做权威归属校验:以 DDB 的 host_id 为准,不信 manifest。
# 刻意【只】做成可选参数而不加进三参形态:migrate 这类路径在迁移过程中 host_id 本来就
# 在变,把 host 断言塞进共享的 host_guard() 会打挂它们。
# exit 81 → 调用方判 failed(可重投):这是控制面分组错或迁移竞态,重读后重投是对的。
if [ -n "${EXPECT_HOST}" ] && [ "${_LF_HOST}" != "${EXPECT_HOST}" ]; then
  echo "LIFECYCLE_HOST_MISMATCH ddb_host=${_LF_HOST} this_host=${EXPECT_HOST}" >&2
  exit 81
fi

# 为什么坐标由这里返回,而不是由 manifest 传:
#   围栏绑的是 tenant + 租约 + host,【不绑坐标】。manifest 里的 vm_num 一旦陈旧或错装,
#   同一台 host 上就会 `stop-vm.sh <tid> <别人的 vm_num>` —— 停掉同机另一个租户的 VM、
#   整类"manifest 说 X、DDB 说 Y"就在设计上不存在,而不是靠逐字段比对去发现。
#   代价为零:上面那次 get-item --consistent-read 本来就要做,坐标只是多投影三列 ——
#   同一次往返,没有新增请求;manifest 反而更短(每 part 装更多租户,摊薄 PutParameter 的 3 TPS)。
#   所以 DDB 的这两个字段是权威的;而控制面用 legacy 公式推算的端口"从未落到 iptables"
#   (host-agent.py:1391-1404 的病史)—— 这本身就是"别信推算值、要信权威值"的直接证据。
#
# vm_num 缺失即 fail-closed 78:它是 tap-vm<n> 与 VM 目录的唯一定位依据,取不到就无法
# 安全地动手。绝不当 0 或空处理 —— 那会让 stop-vm.sh 去停一个错的 tap。
# host_port / guest_ip 缺失是【合法】的(没有 bitmap 路由的租户),按 delete-vm.sh 的
# 缺省方向归一成 0 / 空串;`--output text` 对缺失属性返回字面 "None",要显式抹掉。
if [ -n "${EXPECT_HOST}" ]; then
  case "${_LF_VMNUM}" in
    ''|None|*[!0-9]*)
      echo "LIFECYCLE_COORDS_MISSING vm_num=${_LF_VMNUM}" >&2
      exit 78
      ;;
  esac
  [ "${_LF_HPORT}" = "None" ] && _LF_HPORT=0
  [ -z "${_LF_HPORT}" ] && _LF_HPORT=0
  [ "${_LF_GIP}" = "None" ] && _LF_GIP=""
  printf '%s\t%s\t%s\n' "${_LF_VMNUM}" "${_LF_HPORT}" "${_LF_GIP}"
fi
exit 0
