#!/usr/bin/env bash
# host-pin-parity.sh — #523 判据 2 的 run-all.sh 包装(委托 python gate)。
# 断言 provision-host.sh(烤进 AMI)与 init-host.sh(开机取件)的 FC_VER /
# VMLINUX_NAME 逐字相同。只改一处 pin 会让机队静默混版,而 create/rebuild/restart
# 全走 launch-vm.sh 用同一个 ${ASSETS}/vmlinux —— 没有任何既有断言会发现。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$DIR/lib.sh"

ck_hdr "host-pin-parity · 内核/FC pin 两条启动路径逐字相同(#523 混版拒过)"

if ! have python3; then
  ck_warn "无 python3,跳过 host-pin-parity(CI 侧 python 环境会兜底)"
  exit 0
fi

if python3 "$DIR/host-pin-parity.py" >/tmp/ck-hostpin.log 2>&1; then
  ck_ok "$(tail -1 /tmp/ck-hostpin.log)"
  exit 0
else
  ck_bad "host-pin-parity 发现 pin 不一致(拒过):"
  sed 's/^/      /' /tmp/ck-hostpin.log | head -20 >&2
  exit 1
fi
