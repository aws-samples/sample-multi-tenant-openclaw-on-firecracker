#!/usr/bin/env bash
# python.sh — Python 静态检查(第一层机械门):ruff(lint/格式/死代码/未用 import)+ bandit(SAST)。
#
# 为什么:控制面 Lambda / CDK 全 Python 且近乎全 AI 生成。ruff 抓死代码/未用 import/风格,
# bandit 抓安全模式(硬编码密钥、broad except、subprocess shell=True、yaml.load 等)。
# 降级:任一工具未装 → 该工具 WARN 跳过、不硬挂;两个都在才是完整门。
#
# 用法:scripts/checks/python.sh [CK_SCAN_ALL=1]
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "python · ruff + bandit"
rc=0
files="$(ck_targets ".py")"
[ -n "$files" ] || { ck_ok "无 .py 变更"; exit 0; }

# ruff(优先本地 .venv,回落 PATH)
RUFF=""
[ -x "$CK_ROOT/.venv/bin/ruff" ] && RUFF="$CK_ROOT/.venv/bin/ruff"
[ -z "$RUFF" ] && have ruff && RUFF="ruff"
if [ -n "$RUFF" ]; then
  # 只挡 error 级(E9 语法/F 未定义名/死代码);风格类不挡门,避免噪音
  # shellcheck disable=SC2086
  if (cd "$CK_ROOT" && printf '%s\n' $files | xargs "$RUFF" check --select E9,F63,F7,F82,F401,F811,F841 2>/tmp/ck-ruff.log); then
    ck_ok "ruff:无 error 级问题"
  else
    ck_bad "ruff 发现问题:"; sed 's/^/      /' /tmp/ck-ruff.log | head -25 >&2; rc=1
  fi
else
  ck_warn "ruff 未装(pip install ruff),跳过 lint"
fi

# bandit(pipx/PATH)
if have bandit; then
  # -lll 只把 HIGH severity 当挡门(照 severity 契约:只 Error 挡);-ii 只报 high 置信度,减噪。
  # medium/low(如 B310 受控 urlopen)作提示单独打印、不挡门,避免噪音让人关掉。
  # shellcheck disable=SC2086
  if (cd "$CK_ROOT" && printf '%s\n' $files | xargs bandit -lll -ii --quiet 2>/tmp/ck-bandit.log); then
    ck_ok "bandit:无 HIGH 安全问题"
    # medium 提示(不挡门):统计 medium 条数,>0 才提示
    (cd "$CK_ROOT" && printf '%s\n' $files | xargs bandit -ll -ii --quiet 2>/tmp/ck-bandit-med.log) || true
    # 用 tr 保证单行整数(grep -c 找不到时 exit1,叠加 || echo 0 会产生多行值致整数比较崩)
    med="$(grep -c 'Severity: Medium' /tmp/ck-bandit-med.log 2>/dev/null | head -1 | tr -dc '0-9')"
    [ -n "${med:-}" ] && [ "${med}" -gt 0 ] 2>/dev/null && ck_warn "bandit 有 ${med} 处 medium 提示(不挡门,建议看一眼 /tmp/ck-bandit-med.log)"
  else
    ck_bad "bandit 发现 HIGH 安全问题:"; grep -E 'Issue|Severity|Location' /tmp/ck-bandit.log | head -25 | sed 's/^/      /' >&2; rc=1
  fi
else
  ck_warn "bandit 未装(pipx install bandit),跳过 SAST"
fi
exit $rc
