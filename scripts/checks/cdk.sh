#!/usr/bin/env bash
# cdk.sh — CDK 基础设施静态检查(第一层机械门):IAM 最小权限 / 加密 / 公网暴露。
#
# 为什么:多租户隔离 + AWS 暴露红线(SG 禁 0.0.0.0/0、禁 Lambda Function URL)是本仓安全基石。
# 首选 cdk-nag(AwsSolutions 规则集,需 synth);cdk-nag 未接入时用轻量正则兜底扫最危险的几类
# (SG 全网入站 / IAM Action:* Resource:* / Function URL),不硬挂但把红线漏洞捞出来。
# 只在 deploy/ 下 .py 变更时跑(改基础设施才需要)。
#
# 用法:scripts/checks/cdk.sh [CK_SCAN_ALL=1]
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "cdk · IAM/加密/暴露面(cdk-nag 或正则兜底)"
rc=0

# 只在 deploy/ 基础设施相关 .py 变更时跑
files="$(ck_targets ".py" | grep -E '^deploy/' || true)"
if [ -z "$files" ] && [ "${CK_SCAN_ALL:-0}" != "1" ]; then ck_ok "无 deploy/ 基础设施变更"; exit 0; fi

# cdk-nag 走 CDK synth 上下文(重),这里默认走「正则兜底扫红线」——快、无依赖、抓最危险的几类。
# 完整 cdk-nag 集成在 CI 里跑(有 node_modules + cdk),本地/hook 用轻量兜底。
ck_dim "轻量红线扫描(SG 全网入站 / IAM 通配 / Function URL);完整 cdk-nag 在 CI 跑"

scan="$files"
[ "${CK_SCAN_ALL:-0}" = "1" ] && scan="$(ck_all_files ".py" | grep -E '^deploy/' || true)"

hits=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  # ① SG 入站对 0.0.0.0/0(排除:整行注释、行尾注释、描述字符串里的提及、出站、否定语)
  # 正则兜底难分辨"真开放" vs "注释/描述里提到 0.0.0.0/0",故保守排除这些语境;
  # 真正的 SG 开放判定由 CI 层 cdk-nag synth 后做(AwsSolutions-EC23 等)。
  m="$(grep -nE '0\.0\.0\.0/0|::/0|any_ipv4\(\)|Peer\.any_ipv4' "$CK_ROOT/$f" 2>/dev/null \
       | grep -viE '^[0-9]+:[[:space:]]*(#|//|\*)|#|egress|out_bound|outbound|allow_all_outbound|never|no[[:space:]]|only|open=False|"[^"]*0\.0\.0\.0/0[^"]*"' || true)"
  [ -n "$m" ] && { ck_bad "$f 可能的全网入站(核对是否 CloudFront prefix / 出站):"; printf '%s\n' "$m" | head -4 | sed 's/^/      /' >&2; hits=$((hits+1)); }
  # ② IAM Action:* + Resource:*
  m="$(grep -nE 'actions=\[[^]]*"\*"|"Action"[[:space:]]*:[[:space:]]*"\*"|add_to_policy.*"\*".*"\*"' "$CK_ROOT/$f" 2>/dev/null | grep -viE '^[0-9]+:[[:space:]]*(#|//|\*)' || true)"
  [ -n "$m" ] && { ck_bad "$f 可能的 IAM 通配(Action/Resource:*):"; printf '%s\n' "$m" | head -4 | sed 's/^/      /' >&2; hits=$((hits+1)); }
  # ③ Lambda Function URL(明令禁止,绕过 CloudFront+ALB+WAF)
  m="$(grep -nE 'add_function_url|FunctionUrl|function_url' "$CK_ROOT/$f" 2>/dev/null | grep -viE '^[0-9]+:[[:space:]]*(#|//|\*)' || true)"
  [ -n "$m" ] && { ck_bad "$f 出现 Lambda Function URL(红线禁止):"; printf '%s\n' "$m" | head -3 | sed 's/^/      /' >&2; hits=$((hits+1)); }
  # ④ SG description 非 ASCII(EC2 GroupDescription 硬约束:CFN 400 rollback,反复踩#239)
  # 只查会渲染进 CFN GroupDescription 的 description= 值:em-dash/中文等致 create 失败。
  # CfnOutput 的 description 允许非 ASCII,靠 python AST 判上下文类不误伤(有 python3 时);
  # 无 python3 回落 grep,保守只报 SecurityGroup 附近的行。
  if have python3; then
    m="$(python3 "$CK_ROOT/scripts/checks/sg_desc_ascii.py" "$CK_ROOT/$f" 2>/dev/null || true)"
  else
    m="$(grep -nE 'description[[:space:]]*=' "$CK_ROOT/$f" 2>/dev/null | grep -P '[^\x00-\x7F]' | grep -viE 'CfnOutput|^[0-9]+:[[:space:]]*#' || true)"
  fi
  [ -n "$m" ] && { ck_bad "$f SG/资源 description 含非 ASCII(CFN GroupDescription 只接受 ASCII,会 400 rollback):"; printf '%s\n' "$m" | head -4 | sed 's/^/      /' >&2; hits=$((hits+1)); }
done <<EOF
$scan
EOF

if [ "$hits" -eq 0 ]; then ck_ok "无红线暴露模式"; else rc=1; fi
exit $rc
