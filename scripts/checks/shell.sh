#!/usr/bin/env bash
# shell.sh — shell 脚本静态检查(第一层机械门)。
#
# 为什么:本仓 userdata/SSM 脚本踩过 dash vs bash、set -euo pipefail 在 dash 下静默失败
# (memory 踩过的坑)。shellcheck 专抓这类 + 未引用变量 + 陷阱。
# 降级:shellcheck 未装 → WARN + 每个脚本至少过 bash -n 语法检查(有 bash 就有),不硬挂。
#
# CDK 模板脚本(deploy/userdata/*.sh 含 {{PLACEHOLDER}},synth 时 stack.py 用 .replace() 注入
# 实际内容):占位符原样喂 shellcheck 会在 `{{` 处报 SC1054/SC1073 假错(bash 把 {{ 当花括号组)。
# 早先版本靠 `grep {{PLACEHOLDER}}` 判定是模板就整个跳过——但这会误伤本文件自己(注释/字符串里
# 写了 {{PLACEHOLDER}} 字面就把自己也跳过、逃过 lint),且放弃了对模板 shell 骨架的静态检查。
# 现在改为「渲染再检查」:把 {{PLACEHOLDER}} 统一替换成合法占位 token(__CK_TPL__)后再 lint,
# 模板的 shell 结构照样被 shellcheck 抓,check 脚本自己也不会被误跳。
#
# 用法:scripts/checks/shell.sh [CK_SCAN_ALL=1]
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "shell · shellcheck / bash -n"
rc=0
files="$(ck_targets ".sh")"
[ -n "$files" ] || { ck_ok "无 .sh 变更"; exit 0; }

# 把 {{PLACEHOLDER}} 渲染成合法 token,输出可静态检查的临时脚本路径(无占位符则原样返回路径)。
# 真模板往往无 shebang(靠部署侧 `exec bash` 跑),渲染后交由调用方用 `-s bash` 指定方言避免 SC2148。
ck_render() {
  local src="$1"
  if grep -qE '\{\{[A-Z_]+\}\}' "$src" 2>/dev/null; then
    sed -E 's/\{\{[A-Z_]+\}\}/__CK_TPL__/g' "$src" > /tmp/ck-render.sh
    printf '%s' /tmp/ck-render.sh
  else
    printf '%s' "$src"
  fi
}

if have shellcheck; then
  ck_dim "用 shellcheck"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    src="$CK_ROOT/$f"; tpl=0
    grep -qE '\{\{[A-Z_]+\}\}' "$src" 2>/dev/null && tpl=1
    scan="$(ck_render "$src")"
    # -S error:只把 error 级当挡门(warning/info 不挡,照 severity 契约);SSM 脚本另有 sh 方言
    # 模板渲染副本常无 shebang → -s bash 指定方言消除 SC2148(部署侧就是 bash 执行)
    if [ "$tpl" = 1 ]; then sc_args="-s bash -S error"; else sc_args="-S error"; fi
    # shellcheck disable=SC2086
    if shellcheck $sc_args "$scan" >/tmp/ck-sc.log 2>&1; then
      [ "$tpl" = 1 ] && ck_ok "$f (CDK 模板,占位符渲染后检查)" || ck_ok "$f"
    else
      ck_bad "$f shellcheck error:"; sed 's/^/      /' /tmp/ck-sc.log | head -20 >&2; rc=1
    fi
  done <<EOF
$files
EOF
else
  ck_warn "shellcheck 未装(brew install shellcheck),降级只跑 bash -n 语法检查"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    scan="$(ck_render "$CK_ROOT/$f")"
    if bash -n "$scan" 2>/tmp/ck-bn.log; then ck_ok "$f (bash -n)"; else ck_bad "$f 语法错:"; sed 's/^/      /' /tmp/ck-bn.log >&2; rc=1; fi
  done <<EOF
$files
EOF
fi
exit $rc
