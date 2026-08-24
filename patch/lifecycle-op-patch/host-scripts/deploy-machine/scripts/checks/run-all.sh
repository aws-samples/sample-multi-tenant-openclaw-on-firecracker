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
#   - 慢门(python ruff+bandit / cdk-nag / 依赖审计 / hardcoded 全量)offload 到 GitLab CI 服务端兜底
#     (.gitlab-ci.yml 的 mechanical-gate,MR 到 bb 必跑),本地不再每次 commit 都跑。
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
  # os.system/shell=True/凭据文件读/身份文件写/prompt injection 直接挡门。CI mechanical-gate
  # 天然继承(它就跑 run-all.sh)。默认只 CRITICAL 挡,别把 requests/open 的 MEDIUM 提示挡成假门。
  run skills.sh
  # 拆分未完成时无文件可扫 → 通过;拆分中挡住任何反向 import。
  run import-layers.sh
  # 崩溃重启仍报 running,数据面全死)直接挡。全量扫仓内已知模板载体。
  run template-schema.sh
  # 只跑静态配置层,不触碰 AWS,避免缺键拖到 synth 才 KeyError。
  run config-gate.sh
  # #523 判据 2:guest kernel / Firecracker 的 pin 在 provision-host.sh(烤进 AMI)与
  # init-host.sh(开机取件)各写一份。只改一处 → 机队静默混版且无任何断言发现。挡住。
  run host-pin-parity.sh
  # 分发、它拉的东西全靠 setup.sh / clawpool-deploy.sh 手动推。于是「改代码 + cdk deploy」
  # 更新了 init-host.sh 却不更新它要拉的资产 —— 新机用新脚本拉旧资产,而 host 侧无本地兜底,
  # 这道门断言:每个开机拉取键都有推送方,且每个前缀都在 layer-playbook 里明文列出。
  run s3-at-boot-parity.sh
  # #562:180s 创建死线是【一组配置的联立结果】,不是一个常量。执行段由
  # min(ceil(batch/slots)×per_vm+120, visibility-60) 决定,而 batch(ESM BatchSize)与
  # slots(DISPATCH_HOST_LAUNCH_CONCURRENCY)是两个互不相邻、可被独立修改的配置项。
  # 落地时实测:本地 config.yml 的 max_batch_size=500 / slots=30 → 17 轮 → 执行段 256s,
  # 单这一段就超过 180s 总死线 —— 在这道门之前没有任何检查会说一句话,而后果不是变慢,
  # 是孤儿 VM(租户已判死 failed、host 侧 SSM 还在起 VM,那台机没人认领也不在自愈扫描面里)。
  run create-deadline-config.sh
  # 决定页数的是全表字节数不是命中数。openclaw-tenants 实测 2.83MB(超 1MB 近三倍),
  # openclaw-hosts 的 deleted 死行只增不减。不翻页的后果全是静默看错:有容量却报 unplaced、
  # TTL 租户永不过期还在计费、健康检查漏掉一批、疏散把租户留在死机器上。
  # 本仓早把这条纪律写在 _registered_host_count 的注释里,42 处里仍有 17 处没照做。
  run ddb-scan-pagination.sh
fi

echo
if [ "$fail" = 0 ]; then
  printf '%s✓ 机械门全过%s\n' "$C_GRN" "$C_RST"
else
  printf '%s✗ 有 check 未过(见上)。修掉再进下一步;确属误报的调 allowlist / severity 并注释。%s\n' "$C_RED" "$C_RST" >&2
fi
exit $fail
