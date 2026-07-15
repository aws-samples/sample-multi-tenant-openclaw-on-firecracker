#!/usr/bin/env bash
# import-layers.sh — handler-split 分层包的 import 方向单向检查(第一层机械门)。
#
# 为什么:handler.py 按域拆成分层包后(core/services/routes/consumers/router,见
# SPEC/specs/handler-split/design.md 层间契约表 + ADR-api-handler-split-by-domain §3),
# 层间依赖必须单向向下:consumers/routes → services → core → clients/utils。反向 import
# (core 反依赖 services、services 依赖 routes、consumer↔crud 成环)会把拆环的努力打回原形,
# 也会让 Lambda 冷启动 import 顺序出问题。这个门在拆分过程中挡住任何反向 import。
#
# 拆分未完成时(包目录还没建)自然无文件可扫 → 通过,不阻塞 Phase 0。
# requirements.md R1.2:违反时 SHALL exit≠0。
#
# 用法:scripts/checks/import-layers.sh [CK_SCAN_ALL=1]
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "import-layers · handler-split 分层包 import 方向单向(design.md 层间契约)"

PKG="deploy/lambda/api"
rc=0

# 层间契约表(design.md):每层「禁止 import」的上层。key=本层目录,value=禁止出现的兄弟层。
#   core/clients, core/utils : 仓内一切都不准 import(纯 stdlib/boto3 叶子)
#   core/其它                : 禁 core 横向、services、routes、consumers、router
#   services/*               : 禁 routes、consumers、router
#   routes/*                 : 禁 consumers、router、彼此
#   consumers/*              : 禁 routes、router
# 检查实现:纯 grep import 行 + 禁止清单,POSIX-friendly。

# 只在包内 .py 变更时跑(拆分相关改动才需要)
files="$(ck_targets ".py" | grep -E "^${PKG}/(core|services|routes|consumers|router\.py)" || true)"
if [ -z "$files" ] && [ "${CK_SCAN_ALL:-0}" != "1" ]; then
  ck_ok "无 ${PKG}/ 分层包变更"
  exit 0
fi
[ "${CK_SCAN_ALL:-0}" = "1" ] && files="$(ck_all_files ".py" | grep -E "^${PKG}/(core|services|routes|consumers|router\.py)" || true)"

if [ -z "$files" ]; then
  ck_ok "分层包尚未建立(拆分未开始)或无匹配文件 — 通过"
  exit 0
fi

# 给定文件路径,返回它所属的层名(core-leaf/core/services/routes/consumers/router)
_layer_of() {
  case "$1" in
    ${PKG}/core/clients.py|${PKG}/core/utils.py) echo "core-leaf" ;;
    ${PKG}/core/*)      echo "core" ;;
    ${PKG}/services/*)  echo "services" ;;
    ${PKG}/routes/*)    echo "routes" ;;
    ${PKG}/consumers/*) echo "consumers" ;;
    ${PKG}/router.py)   echo "router" ;;
    *) echo "other" ;;
  esac
}

# 检查一个文件的 import 行是否违反其层的禁止清单。
# 匹配 `from <sib>.` / `from <sib> import` / `import <sib>`(含 core.services 这种带前缀写法)。
_check_file() {
  local f="$1" layer="$2" forbidden="$3" sib
  # 抽 import 行(去注释),只看 from/import 开头
  imports="$(grep -nE '^[[:space:]]*(from|import)[[:space:]]' "$CK_ROOT/$f" 2>/dev/null || true)"
  [ -z "$imports" ] && return 0
  for sib in $forbidden; do
    # 反向 import 的形态:from <sib> / from core.<sib> / import <sib> / from .<sib>
    hits="$(printf '%s\n' "$imports" | grep -nE "(from|import)[[:space:]]+([a-zA-Z_.]*\.)?${sib}([[:space:].]|$|[[:space:]]+import)" || true)"
    if [ -n "$hits" ]; then
      ck_bad "$f ($layer 层)反向 import 了 $sib 层:"
      printf '%s\n' "$hits" | sed 's/^/      /' >&2
      rc=1
    fi
  done
}

for f in $files; do
  layer="$(_layer_of "$f")"
  case "$layer" in
    core-leaf) _check_file "$f" "$layer" "core services routes consumers router" ;;
    core)      _check_file "$f" "$layer" "services routes consumers router" ;;
    services)  _check_file "$f" "$layer" "routes consumers router" ;;
    routes)    _check_file "$f" "$layer" "consumers router" ;;
    consumers) _check_file "$f" "$layer" "routes router" ;;
    router)    : ;;  # router 是顶层,可 import 任何下层
    *)         : ;;
  esac
done

if [ "$rc" = 0 ]; then
  ck_ok "分层包 import 方向单向,无反向依赖"
else
  ck_bad "存在反向 import(违反 design.md 层间契约 / requirements R1.2)。修:把被依赖符号下沉到更底层,或走 facade。"
fi
exit $rc
