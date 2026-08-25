#!/usr/bin/env bash
# create-deadline-config.sh — #562 G13 的 run-all.sh 包装(委托 python gate)。
# 断言创建死线的三段预算与配置【联立成立】:ESM BatchSize <= DISPATCH_HOST_LAUNCH_CONCURRENCY、
# 攒批窗口等于口径里的 2s、visibility 留得下 SSM 超时、ESM 开了 ReportBatchItemFailures、
# DLQ 有界。这些项互不相邻、可被独立修改,谁单独调一个 180s 契约就静默失效。
#
# 落地时实测到的违反:本地 config.yml 的 max_batch_size=500 而 slots=30 → 一批 17 轮 →
# 执行段 256s,单这一段就超过 180s 总死线。也就是说在这道门存在之前,我们自己这套部署
# 在数学上不可能满足 3 分钟契约,而没有任何检查会说一句话。
#
# 默认只核仓内意图(不需要 AWS 凭证,CI 里能跑)。线上真值(部署漂移)要加 --live,
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$DIR/lib.sh"

ck_hdr "create-deadline-config · 180s 创建死线的配置联立约束(#562 G13)"

if ! have python3; then
  ck_warn "无 python3,跳过 create-deadline-config(CI 侧 python 环境会兜底)"
  exit 0
fi

# config.yml 是 gitignored 的本机文件:在没有它的检出里(CI 干净克隆)不该把门判红,
# 但也【不能静默跳过】—— 要说清跳过的理由,否则「没红」会被读成「已达标」。
if [ ! -f "$DIR/../../config.yml" ]; then
  ck_warn "本检出没有 config.yml(gitignored 的本机配置),跳过配置面核对;
      客户面的基线在 samples/config-sg-prod.yaml,发布前用 --live 对线上真值核一遍"
  exit 0
fi

if python3 "$DIR/create-deadline-config.py" >/tmp/ck-deadline-cfg.log 2>&1; then
  ck_ok "$(tail -1 /tmp/ck-deadline-cfg.log)"
  exit 0
else
  ck_bad "create-deadline-config 发现配置与 180s 死线不联立(拒过):"
  sed 's/^/      /' /tmp/ck-deadline-cfg.log | head -24 >&2
  exit 1
fi
