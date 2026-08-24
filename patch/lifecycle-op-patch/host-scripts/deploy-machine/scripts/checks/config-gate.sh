#!/usr/bin/env bash
# 校验 shipped config 与 profile 都声明 CDK 直接下标的键,避免 synth 才 KeyError。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$DIR/lib.sh"

ck_hdr "config-gate · shipped config 必填键(#274)"

if ! have python3; then
  ck_warn "无 python3,跳过 config-gate(CI 侧 python 环境会兜底)"
  exit 0
fi

if result=$(cd "$CK_ROOT" && python3 - <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("NO_PYYAML")
    raise SystemExit(2)

required = (
    "host.root_volume_gb", "host.data_volume_gb", "host.reserved_vcpu", "host.reserved_mem_mb",
    "vm.default_vcpu", "vm.default_mem_mb", "vm.data_disk_mb", "vm.gateway_port_base",
    "vm.subnet_prefix", "asg.max_capacity", "scaler.interval_minutes", "scaler.idle_timeout_minutes",
    "health_check.interval_minutes", "alb.internal", "asg.min_capacity", "asg.lifecycle_hook_timeout",
)
missing = []
for path in [Path("config.yml.example"), *sorted(Path("samples/profiles").glob("*.yml"))]:
    data = yaml.safe_load(path.read_text()) or {}
    for key in required:
        value = data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                missing.append(f"{path}: {key}")
                break
            value = value[part]
print("\n".join(missing))
raise SystemExit(bool(missing))
PY
); then
  ck_ok "config.yml.example 与 samples/profiles/*.yml 必填键齐全"
  exit 0
else
  rc=$?
  # 写成 if 而不是 `[ ... ] && { ... }`:后者在 rc=1(真的缺键)时整条 && 列表返回 1,
  # 而本文件是 set -e —— 那会在打印诊断【之前】就退出,门变成静默红灯。缺哪个键必须说出来。
  if [ "$rc" -eq 2 ]; then
    ck_warn "无 PyYAML,跳过 config-gate(CI 侧依赖环境会兜底)"
    exit 0
  fi
  ck_bad "config-gate 发现缺失键(拒过):"
  printf '%s\n' "$result" | sed 's/^/      /' >&2
  exit 1
fi
