#!/usr/bin/env bash
# deps.sh — 依赖漏洞审计(第一层机械门):pip-audit(Python)+ npm audit(Node)。
#
# 为什么:AI 常拉带已知 CVE 的依赖。只在依赖清单(pyproject.toml/requirements*.txt/package.json)
# 变更时跑,平时不拖慢。降级:工具未装 → WARN 跳过,不硬挂(网络/环境依赖强,CI 里跑更靠谱)。
#
# 用法:scripts/checks/deps.sh [CK_SCAN_ALL=1]
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "deps · pip-audit + npm audit"
rc=0

changed="$(ck_changed_files)"
py_dep=0; node_dep=0
if [ "${CK_SCAN_ALL:-0}" = "1" ]; then py_dep=1; node_dep=1; else
  printf '%s\n' "$changed" | grep -qE 'pyproject\.toml|requirements.*\.txt' && py_dep=1
  printf '%s\n' "$changed" | grep -qE 'package\.json|package-lock\.json' && node_dep=1
fi
[ "$py_dep" = 0 ] && [ "$node_dep" = 0 ] && { ck_ok "无依赖清单变更"; exit 0; }

# ── Python ──
if [ "$py_dep" = 1 ]; then
  if have pip-audit; then
    reqs="$CK_ROOT/deploy/lambda/api/requirements.txt"
    if [ -f "$reqs" ]; then
      if pip-audit -r "$reqs" >/tmp/ck-pipaudit.log 2>&1; then ck_ok "pip-audit(Lambda requirements):无已知 CVE"
      else ck_bad "pip-audit 发现漏洞:"; grep -iE 'name|vuln|GHSA|CVE|PYSEC' /tmp/ck-pipaudit.log | head -20 | sed 's/^/      /' >&2; rc=1; fi
    else ck_warn "无 deploy/lambda/api/requirements.txt,跳过"; fi
  else ck_warn "pip-audit 未装(pipx install pip-audit),跳过 Python 依赖审计"; fi
fi

# ── Node ──
if [ "$node_dep" = 1 ]; then
  if have npm; then
    # 找有 package.json 的目录(tests/loadtest 等),逐个 audit;--omit=dev 只看运行时
    found=0
    while IFS= read -r pj; do
      d="$(dirname "$CK_ROOT/$pj")"; found=1
      if (cd "$d" && npm audit --omit=dev --audit-level=high >/tmp/ck-npmaudit.log 2>&1); then ck_ok "npm audit($pj):无 high+ 漏洞"
      else ck_warn "npm audit($pj)报告 high+(或无 lockfile 无法审计),看 /tmp/ck-npmaudit.log"; grep -iE 'high|critical|vulnerabilit' /tmp/ck-npmaudit.log | head -8 | sed 's/^/      /'; fi
    done <<EOF
$(ck_all_files "package.json" | grep -v node_modules || true)
EOF
    [ "$found" = 0 ] && ck_ok "无 package.json"
  else ck_warn "npm 未装,跳过 Node 依赖审计"; fi
fi
exit $rc
