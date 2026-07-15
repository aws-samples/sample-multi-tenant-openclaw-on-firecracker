#!/usr/bin/env bash
# run-all.sh — 第一层机械静态门 + 第三层硬编码扫描的统一入口(被 hook / oc-dev-flow review / CI 复用)。
#
# 单一实现三处复用(ADR-genai-code-quality-gates):这一个脚本串起所有确定性 check,
# 三个嵌入点都调它,避免各处逻辑漂移。第二层 AI reviewer 是 judgment 门,不在这(在 oc-dev-flow review)。
#
# 用法:
#   scripts/checks/run-all.sh              # 扫变更集(本地/hook)
#   CK_SCAN_ALL=1 scripts/checks/run-all.sh # 全量(CI)
#   scripts/checks/run-all.sh --fast       # 快检(secrets+shell),给 commit-hook 用
#   scripts/checks/run-all.sh --secrets-only # 只扫凭据(最轻,commit 前命根子门)
# 退出:0=全过;1=有 check 挡门。
#
# 门分层(ADR-genai-code-quality-gates + hook offload 到 CI 的取舍):
#   - commit 前 hook(--fast):只留 secrets(命根子,凭据一旦 commit 推上去就可能被索引,必须最前置拦)
#     + shell(轻,抓 SSM/userdata 的 dash 方言致命坑);两者秒级,不拖慢 commit。
#   - 慢门(python ruff+bandit / cdk-nag / 依赖审计 / hardcoded 全量)offload 到 CI 服务端兜底,
#     本地不再每次 commit 都跑。
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
. "$DIR/lib.sh"

fast=0; secrets_only=0
case "${1:-}" in
  --fast) fast=1 ;;
  --secrets-only) secrets_only=1 ;;
esac

fail=0
run() { bash "$DIR/$1" || fail=1; }

if [ "$secrets_only" = 1 ]; then
  ck_hdr "机械门 · 凭据门(commit 前命根子:secrets)"
  export CK_NO_FALLBACK=1
  run secrets.sh
elif [ "$fast" = 1 ]; then
  ck_hdr "机械门 · 快检模式(commit 前:secrets + shell;python/cdk/deps/hardcoded 已 offload 到 CI)"
  # 快检不回落全量:没变更就快速放行,不扫 5 万行拖慢每次 commit(secrets 仍全仓扫,那是命根子)
  export CK_NO_FALLBACK=1
  run secrets.sh
  run shell.sh
else
  ck_hdr "机械门 + 硬编码 + skill 恶意扫描 · 全量(secrets/shell/python/cdk/deps/hardcoded/skills)"
  run secrets.sh
  run shell.sh
  run python.sh
  run cdk.sh
  run deps.sh
  run hardcoded.sh
  # skill 恶意模式扫描(#85 · issue-85-skill-vetter):samples/*/skills/ 里出现 eval/exec/
  # os.system/shell=True/凭据文件读/身份文件写/prompt injection 直接挡门。CI mechanical-gate
  # 天然继承(它就跑 run-all.sh)。默认只 CRITICAL 挡,别把 requests/open 的 MEDIUM 提示挡成假门。
  run skills.sh
  # handler-split 分层包 import 方向单向检查(#132 · design.md 层间契约 / R1.2)。
  # 拆分未完成时无文件可扫 → 通过;拆分中挡住任何反向 import。
  run import-layers.sh
  # #197 模板 schema 门:模板载体带超版本 config key(2.26 .strict() 拒起致 gateway
  # 崩溃重启仍报 running,数据面全死)直接挡。全量扫仓内已知模板载体。
  run template-schema.sh
fi

echo
if [ "$fail" = 0 ]; then
  printf '%s✓ 机械门全过%s\n' "$C_GRN" "$C_RST"
else
  printf '%s✗ 有 check 未过(见上)。修掉再进下一步;确属误报的调 allowlist / severity 并注释。%s\n' "$C_RED" "$C_RST" >&2
fi
exit $fail
