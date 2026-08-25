#!/usr/bin/env bash
# 断言 host 开机从 S3 拉的【每个】键都有推送方,且每个前缀都在 claw-patch-skill 的
# 到开机读的 observability/fluent-bit/host/ → 8/7 新起的 host 拉回旧配置,51 个租户 guest
# deployment/scripts,命中不了那个前缀 —— 文档补句话拦不住,故机械化。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$DIR/lib.sh"

ck_hdr "s3-at-boot-parity · 开机拉取的每个 S3 键都有推送方(#458 静默起坏拒过)"

if ! have python3; then
  ck_warn "无 python3,跳过 s3-at-boot-parity(CI 侧 python 环境会兜底)"
  exit 0
fi

if python3 "$DIR/s3-at-boot-parity.py" --repo-root "$DIR/../.." >/tmp/ck-s3boot.log 2>&1; then
  ck_ok "$(tail -1 /tmp/ck-s3boot.log)"
  exit 0
else
  ck_bad "s3-at-boot-parity 发现开机分发面有缺口(拒过):"
  sed 's/^/      /' /tmp/ck-s3boot.log | head -24 >&2
  exit 1
fi
