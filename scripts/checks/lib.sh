#!/usr/bin/env bash
# lib.sh — scripts/checks/ 各 check 脚本的公共库(颜色 / 工具探测 / 降级契约 / 变更文件集)。
#
# 为什么存在:本仓近乎全 AI 生成,ADR-genai-code-quality-gates 定的三层 check 要「单一实现三处
# 复用」(本地 commit-hook / review 阶段 / CI 都调同一批脚本)。这份库把
# 三处共用的东西收口一处,避免漂移。
#
# 降级契约(重要,别乱改):
#   - 机械工具(shellcheck/bandit/ruff/pip-audit/cdk-nag)未装 → WARN 跳过、exit 0,别把「没装工具」
#     当「有问题」挡住所有人(别人机器没装不该全挂)。CI 环境会装齐,那里才是硬门。
#   - 凭据扫描是命根子:gitleaks 未装也必须用内置正则兜底扫,绝不 skip(铁律 #5 脱敏红线)。
# POSIX 尽量兼容;本库用 bash(hook/CI 都有 bash),不追 dash。

set -eu

# ── 颜色(非 tty 关色,CI 日志干净)──────────────────────────
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_YLW=''; C_DIM=''; C_RST=''
fi

ck_ok()   { printf '  %s✓%s %s\n' "$C_GRN" "$C_RST" "$*"; }
ck_bad()  { printf '  %s✗%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }
ck_warn() { printf '  %s!%s %s\n' "$C_YLW" "$C_RST" "$*"; }
ck_dim()  { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_RST"; }
ck_hdr()  { printf '\n%s== %s ==%s\n' "$C_YLW" "$*" "$C_RST"; }

# 仓库根(scripts/checks/lib.sh → ../..)
CK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"

have() { command -v "$1" >/dev/null 2>&1; }

# ── 变更文件集 ──────────────────────────────────────────────
# 优先拿「相对 bb 的变更文件」(review 阶段/CI 用);拿不到就 staged;再不行全仓扫。
# 用法:ck_changed_files [<后缀过滤，如 .py>]
ck_changed_files() {
  local filter="${1:-}" base files
  cd "$CK_ROOT"
  # 相对 bb 的三点 diff(功能分支 vs bb 分叉点)
  if git rev-parse --verify -q origin/main >/dev/null 2>&1; then base="origin/main"
  elif git rev-parse --verify -q main >/dev/null 2>&1; then base="main"
  else base=""; fi

  if [ -n "$base" ]; then
    files="$(git diff --name-only --diff-filter=ACMR "${base}...HEAD" 2>/dev/null || true)"
  else
    files=""
  fi
  # 叠加 staged + 工作区改动(commit-hook 场景 HEAD 还没提交)
  files="$files
$(git diff --name-only --diff-filter=ACMR --cached 2>/dev/null || true)
$(git diff --name-only --diff-filter=ACMR 2>/dev/null || true)"

  # 去空、去重、只保留真实存在的文件;按后缀过滤
  printf '%s\n' "$files" | sed '/^$/d' | sort -u | while IFS= read -r f; do
    [ -f "$CK_ROOT/$f" ] || continue
    if [ -n "$filter" ]; then case "$f" in *"$filter") echo "$f";; esac
    else echo "$f"; fi
  done
}

# 全量模式:给定后缀,列全仓文件(CI 全量扫 / 无 git 基线时用)。排除噪音目录。
ck_all_files() {
  local filter="${1:-}"
  cd "$CK_ROOT"
  # shellcheck disable=SC2086
  # -not -name .git 也排除 worktree 根的 .git 指针文件(是文件不是目录,'./.git/*' 漏它)
  # .remote-drift/ 是本地远程漂移暂存(已 gitignore,不进 CI clone),扫它会误报暂存的旧品牌/账号
  find . -type f ${filter:+-name "*$filter"} \
    -not -name .git -not -path './.git/*' -not -path './.remote-drift/*' \
    -not -path './.venv/*' -not -path './node_modules/*' \
    -not -path './opensource/*' -not -path '*/__pycache__/*' -not -path '*/.ruff_cache/*' \
    -not -path './cdk.out/*' -not -path './tests/fixtures/*' 2>/dev/null | sed 's|^\./||'
}

# 决定扫描目标:CK_SCAN_ALL=1 → 全量;否则变更集。
# 变更集空时:默认回落全量(CI 首跑无基线兜底);CK_NO_FALLBACK=1(commit-hook 快检用)则返回空、
# 不扫全仓——commit 前没变更就没什么可扫,别扫 5 万行拖慢每次 commit。
ck_targets() {
  local filter="${1:-}" out
  if [ "${CK_SCAN_ALL:-0}" = "1" ]; then ck_all_files "$filter"; return; fi
  out="$(ck_changed_files "$filter")"
  if [ -n "$out" ]; then printf '%s\n' "$out"; return; fi
  [ "${CK_NO_FALLBACK:-0}" = "1" ] && return   # 快检:变更集空即返回空
  ck_all_files "$filter"                        # 否则回落全量
}
