#!/usr/bin/env bash
# secrets.sh — 凭据/密钥泄漏扫描(第一层机械门 · 命根子)。
#
# 为什么是命根子:真实账号 ID/活 API key 进库是不可逆泄漏,
# 铁律 #5 脱敏红线。所以这条**不允许因工具没装就 skip**:gitleaks 在就用 gitleaks(规则全),
# 不在就用内置正则兜底扫(覆盖 AWS AKID/私钥/常见 token)。
#
# 用法:scripts/checks/secrets.sh            # 扫变更集(无基线回落全量)
#       CK_SCAN_ALL=1 scripts/checks/secrets.sh  # 全量(CI 用)
# 退出:0=干净;1=发现疑似凭据。
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "secrets · 凭据泄漏扫描"
rc=0

if have gitleaks; then
  # 注意:gitleaks 8.x 用子命令 `dir`(扫目录/文件)/ `git`(扫历史),旧的 `detect` 已废弃。
  # 曾踩坑:用 `detect` 会被当无效参数静默忽略、报「no leaks found」假绿(本仓静默失败坑清单)。
  ck_dim "用 gitleaks $(gitleaks version 2>/dev/null)(dir 子命令)"
  cfg=""
  [ -f "$CK_ROOT/.gitleaks.toml" ] && cfg="--config $CK_ROOT/.gitleaks.toml"
  # 扫整个工作区目录(默认:leaks found → exit 1,干净 → exit 0)。allowlist 在 .gitleaks.toml。
  # shellcheck disable=SC2086
  if gitleaks dir "$CK_ROOT" --no-banner --redact $cfg >/tmp/ck-gitleaks.log 2>&1; then
    ck_ok "gitleaks:无泄漏"
  else
    ck_bad "gitleaks 发现疑似凭据(已 redact):"
    grep -E 'Finding|File|Secret|RuleID|Line' /tmp/ck-gitleaks.log | head -30 >&2 || true
    rc=1
  fi
else
  # ── 内置正则兜底(gitleaks 未装也扫,绝不 skip)──────────────
  ck_warn "gitleaks 未装,用内置正则兜底扫(建议装 gitleaks 拿全规则:brew install gitleaks)"
  hits=0
  # 只扫变更集/全量的文本文件,排除本脚本自身与 fixtures 与文档示例
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      scripts/checks/*) continue;;                 # 别扫到自己的正则
      *.md|*.svg) continue;;                        # 文档/图里的示例不算泄漏(单独脱敏门管)
    esac
    # AKID(AKIA/ASIA + 16 大写数字)、私钥头、Slack/GitHub token、通用 secret 赋值
    m="$(grep -nE \
      '(AKIA|ASIA)[A-Z0-9]{16}|-----BEGIN[[:space:]]+(RSA|EC|OPENSSH|PGP)?[[:space:]]*PRIVATE KEY-----|xox[baprs]-[0-9A-Za-z-]{10,}|gh[pousr]_[0-9A-Za-z]{30,}|(aws_secret_access_key|secret_key|password|passwd|api[_-]?key|token)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9/+=_-]{16,}' \
      "$CK_ROOT/$f" 2>/dev/null | grep -viE 'REDACTED|example|<[a-z_]+>|placeholder|xxx|your[-_]|dummy|fake' || true)"
    if [ -n "$m" ]; then
      ck_bad "疑似凭据 in $f:"
      printf '%s\n' "$m" | head -5 | sed 's/^/      /' >&2
      hits=$((hits+1))
    fi
  done <<EOF
$(ck_targets "")
EOF
  if [ "$hits" -eq 0 ]; then ck_ok "内置正则:无疑似凭据"; else rc=1; fi
fi

exit $rc
