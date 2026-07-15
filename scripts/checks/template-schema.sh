#!/usr/bin/env bash
# template-schema.sh — #197 模板 schema 门的 run-all.sh 包装(委托 python gate)。
# 校验模板载体 key 集 ⊆ pin 版本 gateway schema,超版本 key(6.x-only,2.26
# .strict() 拒起致 gateway 崩溃重启仍报 running)拒过。全量扫仓内已知载体。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$DIR/lib.sh"

ck_hdr "template-schema · 模板 key ⊆ gateway schema(#197 超版本 key 拒过)"

if ! have python3; then
  ck_warn "无 python3,跳过 template-schema(CI 侧 python 环境会兜底)"
  exit 0
fi

if python3 "$DIR/template-schema-gate.py" >/tmp/ck-tplschema.log 2>&1; then
  ck_ok "$(tail -1 /tmp/ck-tplschema.log)"
  exit 0
else
  ck_bad "template-schema 发现超版本 key(拒过):"
  sed 's/^/      /' /tmp/ck-tplschema.log | head -20 >&2
  exit 1
fi
