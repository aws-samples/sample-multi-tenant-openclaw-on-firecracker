#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# 部署 CDK stack 并导出环境信息到 .env.deploy
#
# Usage:
#   Single-domain (legacy):
#     ./setup.sh <region> <profile> [--domain <domain>] [--cert <acm-arn>]
#   Dual-domain (1.3.4+ recommended for production):
#     ./setup.sh <region> <profile> \
#       --console-domain console.example.com --console-cert <acm-arn> \
#       --app-domain     app.example.com     --app-cert     <acm-arn>
#
#   --domain          legacy: 单域名（console + tenant dashboard 共用）
#   --cert            legacy: us-east-1 ACM 证书 ARN
#   --console-domain  console 专属域名（推荐生产用）
#   --console-cert    console 域名的 us-east-1 ACM cert ARN
#   --app-domain      tenant dashboard 专属域名
#   --app-cert        app 域名的 us-east-1 ACM cert ARN
#
# 双域名模式下 Cognito session cookie 会自动 scope 到 console-domain，
# 物理上隔离 console 凭证和 tenant 应用，是多租户场景的安全最佳实践。
set -euo pipefail

REGION="${1:?Usage: ./setup.sh <region> <profile> [--domain | --console-domain & --app-domain]}"
PROFILE="${2:?Usage: ./setup.sh <region> <profile> [--domain | --console-domain & --app-domain]}"
shift 2

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# instance-role 模式:PROFILE="-" 表示不带 --profile,让 aws-cli 和 cdk 都走默认凭据链吃 IMDS
# (堡垒机 / CI / EC2 instance role 场景)。沿用 deploy/lib/mint-shared-vkey.sh 早有的 "-" 约定。
# 为什么必须这样:cdk(aws-sdk-js)读不了只有 `credential_source=Ec2InstanceMetadata`、没有
# `role_arn` 的畸形 profile(aws-cli 宽容能读,sdk 严格拒绝 → "no credentials configured")。
# 全脚本用 PROFILE_ARGS 展开:instance-role 模式为空(不传 profile),否则 --profile <name>。
if [ "$PROFILE" = "-" ]; then
  PROFILE_ARGS=()
else
  PROFILE_ARGS=(--profile "$PROFILE")
fi

# Parse domain flags. Both legacy single-domain and new dual-domain are supported.
DOMAIN_FLAG=""; CERT_FLAG=""
CONSOLE_DOMAIN_FLAG=""; CONSOLE_CERT_FLAG=""
APP_DOMAIN_FLAG=""; APP_CERT_FLAG=""
DOMAIN_SET=false; CERT_SET=false
CONSOLE_DOMAIN_SET=false; CONSOLE_CERT_SET=false
APP_DOMAIN_SET=false; APP_CERT_SET=false
CDK_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --domain)         DOMAIN_FLAG="$2";         DOMAIN_SET=true;         shift 2 ;;
    --cert)           CERT_FLAG="$2";           CERT_SET=true;           shift 2 ;;
    --console-domain) CONSOLE_DOMAIN_FLAG="$2"; CONSOLE_DOMAIN_SET=true; shift 2 ;;
    --console-cert)   CONSOLE_CERT_FLAG="$2";   CONSOLE_CERT_SET=true;   shift 2 ;;
    --app-domain)     APP_DOMAIN_FLAG="$2";     APP_DOMAIN_SET=true;     shift 2 ;;
    --app-cert)       APP_CERT_FLAG="$2";       APP_CERT_SET=true;       shift 2 ;;
    *) CDK_ARGS+=("$1"); shift ;;
  esac
done

# If any domain flag provided, write into config.yml (overrides existing values)
ANY_DOMAIN_FLAG=false
if $DOMAIN_SET || $CERT_SET || $CONSOLE_DOMAIN_SET || $CONSOLE_CERT_SET || $APP_DOMAIN_SET || $APP_CERT_SET; then
  ANY_DOMAIN_FLAG=true
fi

if $ANY_DOMAIN_FLAG; then
  DOMAIN="$DOMAIN_FLAG" CERT="$CERT_FLAG" \
  CONSOLE_DOMAIN="$CONSOLE_DOMAIN_FLAG" CONSOLE_CERT="$CONSOLE_CERT_FLAG" \
  APP_DOMAIN="$APP_DOMAIN_FLAG"         APP_CERT="$APP_CERT_FLAG" \
  DS="$DOMAIN_SET" CS="$CERT_SET" \
  CDS="$CONSOLE_DOMAIN_SET" CCS="$CONSOLE_CERT_SET" \
  ADS="$APP_DOMAIN_SET"     ACS="$APP_CERT_SET" \
  python3 - <<'PYEOF'
import os, re, pathlib
cfg_path = pathlib.Path("config.yml")
text = cfg_path.read_text()
has_section = re.search(r"^cloudfront:\s*$", text, re.MULTILINE)
if not has_section:
    sep = "" if text.endswith("\n") else "\n"
    text += (
        f"{sep}\n# ========== CloudFront 自定义域名 (可选) ==========\n"
        "# 1.3.4+: 推荐使用 console_domain + app_domain 双域名模式（安全分离）\n"
        "# 老的 custom_domain 仍兼容（单域名模式）\n"
        "cloudfront:\n"
        "  custom_domain: \"\"\n"
        "  acm_cert_arn: \"\"\n"
        "  console_domain: \"\"\n"
        "  console_cert_arn: \"\"\n"
        "  app_domain: \"\"\n"
        "  app_cert_arn: \"\"\n"
    )

def set_key(text, key, val):
    pat = re.compile(rf"^(\s*{re.escape(key)}:\s*)(?:\"[^\"]*\"|'[^']*'|\S*)(\s*(?:#.*)?)$", re.MULTILINE)
    repl = lambda m: f'{m.group(1)}"{val}"{m.group(2)}'
    new, n = pat.subn(repl, text, count=1)
    if n == 0:
        new = re.sub(r"(^cloudfront:\s*$)", rf"\1\n  {key}: \"{val}\"", text, count=1, flags=re.MULTILINE)
    return new

# Apply flags only when explicitly set (preserve existing config.yml values otherwise)
if os.environ["DS"]  == "True": text = set_key(text, "custom_domain",    os.environ["DOMAIN"])
if os.environ["CS"]  == "True": text = set_key(text, "acm_cert_arn",     os.environ["CERT"])
if os.environ["CDS"] == "True": text = set_key(text, "console_domain",   os.environ["CONSOLE_DOMAIN"])
if os.environ["CCS"] == "True": text = set_key(text, "console_cert_arn", os.environ["CONSOLE_CERT"])
if os.environ["ADS"] == "True": text = set_key(text, "app_domain",       os.environ["APP_DOMAIN"])
if os.environ["ACS"] == "True": text = set_key(text, "app_cert_arn",     os.environ["APP_CERT"])

cfg_path.write_text(text)
print("✓ config.yml updated:")
for env_key, cfg_key in (("DOMAIN","custom_domain"), ("CERT","acm_cert_arn"),
                          ("CONSOLE_DOMAIN","console_domain"), ("CONSOLE_CERT","console_cert_arn"),
                          ("APP_DOMAIN","app_domain"), ("APP_CERT","app_cert_arn")):
    set_env = {"DOMAIN":"DS","CERT":"CS","CONSOLE_DOMAIN":"CDS","CONSOLE_CERT":"CCS",
               "APP_DOMAIN":"ADS","APP_CERT":"ACS"}[env_key]
    if os.environ[set_env] == "True":
        val = os.environ[env_key] or "<cleared>"
        # Don't print full ACM ARN
        if "cert" in cfg_key and val != "<cleared>":
            val = "<set>"
        print(f"  {cfg_key:18s} = {val}")
PYEOF
fi

# Auto-detect existing Cognito pool from prior deploy (1.1.x → 1.2.x upgrade path).
# 1.2.x changed the UserPoolDomain prefix; importing the pool keeps users from
# losing their accounts. The stack always recreates the domain + client itself,
# so only user_pool_id needs to be carried forward.
EXISTING_POOL=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' \
  --output text "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" 2>/dev/null || true)
EXISTING_DOMAIN=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].Outputs[?OutputKey==`CognitoDomain`].OutputValue' \
  --output text "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" 2>/dev/null || true)
if [[ "${EXISTING_DOMAIN:-}" == openclaw-console-* ]]; then
  # #479:带账号后缀的是本栈自建域,不能误走 1.1.x 升级路径。
  echo "✓ config.yml: skipped self-managed Cognito pool import (${EXISTING_DOMAIN})"
  EXISTING_POOL=""
fi
if [ -n "${EXISTING_POOL:-}" ] && [ "$EXISTING_POOL" != "None" ]; then
  POOL="$EXISTING_POOL" python3 - <<'PYEOF'
import os, re, pathlib
cfg_path = pathlib.Path("config.yml")
text = cfg_path.read_text()
pool = os.environ["POOL"]

def current_value(key):
    m = re.search(rf'^\s*{re.escape(key)}:\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else None

if current_value("user_pool_id") == pool:
    raise SystemExit(0)

def upsert(text, key, val):
    pat = re.compile(rf'^(\s*){re.escape(key)}:\s*"[^"]*"\s*(?:#.*)?$', re.MULTILINE)
    if pat.search(text):
        return pat.sub(rf'\g<1>{key}: "{val}"', text, count=1)
    cpat = re.compile(rf'^(\s*)#\s*{re.escape(key)}:\s*"[^"]*"\s*(?:#.*)?$', re.MULTILINE)
    if cpat.search(text):
        return cpat.sub(rf'\g<1>{key}: "{val}"', text, count=1)
    return re.sub(r'(^console_auth:\s*\n(?:[ \t]+.*\n)*)',
                  rf'\g<1>  {key}: "{val}"\n', text, count=1, flags=re.MULTILINE)

text = upsert(text, "user_pool_id", pool)
cfg_path.write_text(text)
print(f"✓ config.yml: imported existing Cognito pool {pool[:25]}… (1.1.x → 1.2.x upgrade path)")
PYEOF
fi

# ═══════════════════════════════════════════════════════════════════════════════
# Interactive VPC configuration — ask user to choose network mode before deploy.
# Modes:
#   1) self_managed — CDK creates a new /20 VPC with 3 public + 3 private subnets
#   2) imported     — deploy into an existing VPC (user provides IDs interactively)
#   3) default_vpc  — use the region's default VPC (dev/demo only, not recommended)
#
# The chosen mode + parameters are written into config.yml's network: section.
# If config.yml already has a network: section, this step is skipped (idempotent
# re-runs don't re-prompt). Pass --skip-vpc-prompt to force skip.
# ═══════════════════════════════════════════════════════════════════════════════
_SKIP_VPC_PROMPT=false
for _arg in "${CDK_ARGS[@]+"${CDK_ARGS[@]}"}"; do
  [ "$_arg" = "--skip-vpc-prompt" ] && _SKIP_VPC_PROMPT=true
done
# Remove --skip-vpc-prompt from CDK_ARGS so it doesn't confuse cdk deploy
_CDK_ARGS_CLEAN=()
for _arg in ${CDK_ARGS[@]+"${CDK_ARGS[@]}"}; do
  [ "$_arg" != "--skip-vpc-prompt" ] && _CDK_ARGS_CLEAN+=("$_arg")
done
CDK_ARGS=("${_CDK_ARGS_CLEAN[@]+"${_CDK_ARGS_CLEAN[@]}"}")

# Detect whether VPC mode has been explicitly chosen.
# Trigger prompt when: mode is empty/unset (sentinel "").
# Skip when mode is any valid value (default_vpc/self_managed/imported).
# Pure awk — no third-party deps (pyyaml is only in .venv, not system python3).
_CURRENT_NET_MODE=$(awk '/^network:/{found=1; next} found && /^[^[:space:]]/{exit} found && /^[[:space:]]+mode:/{gsub(/.*mode:[[:space:]]*/,""); gsub(/^["'\'']/,""); gsub(/["'\''].*$/,""); gsub(/[[:space:]]*#.*$/,""); print; exit}' config.yml 2>/dev/null)

if [ "$_SKIP_VPC_PROMPT" = "false" ] && [ -z "$_CURRENT_NET_MODE" ]; then
  # M2: non-interactive stdin (CI/piped) without --skip-vpc-prompt → fail-loud
  if ! [ -t 0 ]; then
    echo "ERROR: VPC mode not configured (network.mode is empty in config.yml)" >&2
    echo "  Either: 1) set network.mode in config.yml before running, or" >&2
    echo "          2) pass --skip-vpc-prompt (will use default_vpc)" >&2
    exit 1
  fi
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║             VPC Network Configuration                       ║"
  echo "╠══════════════════════════════════════════════════════════════╣"
  echo "║  1) New VPC        — CDK creates a fresh /20 VPC (3 AZ,    ║"
  echo "║                      3 public + 3 private subnets, 3 NAT)  ║"
  echo "║  2) Existing VPC   — Deploy into your existing VPC         ║"
  echo "║                      (you provide VPC ID + 6 subnet IDs)   ║"
  echo "║  3) Default VPC    — Use region default VPC (dev only)     ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  printf "Select VPC mode [1/2/3] (default: 1): "
  read -r _VPC_CHOICE
  _VPC_CHOICE="${_VPC_CHOICE:-1}"

  case "$_VPC_CHOICE" in
    1)
      _NET_MODE="self_managed"
      printf "  VPC CIDR [10.20.0.0/20]: "
      read -r _VPC_CIDR
      _VPC_CIDR="${_VPC_CIDR:-10.20.0.0/20}"
      echo "  → Will create new VPC: $_VPC_CIDR"
      ;;
    2)
      _NET_MODE="imported"
      echo ""
      echo "  Please provide your existing VPC details (us-west-2 example):"
      echo "  ─────────────────────────────────────────────────────────────"
      printf "  VPC ID (e.g. vpc-0abc123def456): "
      read -r _IMP_VPC_ID
      if [ -z "$_IMP_VPC_ID" ]; then
        echo "  ✗ VPC ID is required. Aborting." >&2; exit 1
      fi
      if ! echo "$_IMP_VPC_ID" | grep -qE '^vpc-[0-9a-f]{8,17}$'; then
        echo "  ✗ VPC ID format invalid (expected vpc-<hex>). Got: $_IMP_VPC_ID" >&2; exit 1
      fi

      printf "  VPC CIDR (e.g. 10.0.0.0/16): "
      read -r _IMP_CIDR
      if [ -z "$_IMP_CIDR" ]; then
        echo "  ✗ VPC CIDR is required. Aborting." >&2; exit 1
      fi

      echo ""
      echo "  Public subnets (for ALB/NAT GW) — need exactly 3, one per AZ:"
      printf "    Public subnet 1: "
      read -r _PUB1
      printf "    Public subnet 2: "
      read -r _PUB2
      printf "    Public subnet 3: "
      read -r _PUB3
      if [ -z "$_PUB1" ] || [ -z "$_PUB2" ] || [ -z "$_PUB3" ]; then
        echo "  ✗ All 3 public subnet IDs are required. Aborting." >&2; exit 1
      fi
      for _sid in "$_PUB1" "$_PUB2" "$_PUB3"; do
        if ! echo "$_sid" | grep -qE '^subnet-[0-9a-f]{8,17}$'; then
          echo "  ✗ Subnet ID format invalid (expected subnet-<hex>). Got: $_sid" >&2; exit 1
        fi
      done

      echo ""
      echo "  Private subnets (for hosts/edge/Redis) — need exactly 3, one per AZ:"
      printf "    Private subnet 1: "
      read -r _PRIV1
      printf "    Private subnet 2: "
      read -r _PRIV2
      printf "    Private subnet 3: "
      read -r _PRIV3
      if [ -z "$_PRIV1" ] || [ -z "$_PRIV2" ] || [ -z "$_PRIV3" ]; then
        echo "  ✗ All 3 private subnet IDs are required. Aborting." >&2; exit 1
      fi
      for _sid in "$_PRIV1" "$_PRIV2" "$_PRIV3"; do
        if ! echo "$_sid" | grep -qE '^subnet-[0-9a-f]{8,17}$'; then
          echo "  ✗ Subnet ID format invalid (expected subnet-<hex>). Got: $_sid" >&2; exit 1
        fi
      done

      echo ""
      echo "  → Will deploy into existing VPC: $_IMP_VPC_ID ($_IMP_CIDR)"
      echo "    Public:  $_PUB1, $_PUB2, $_PUB3"
      echo "    Private: $_PRIV1, $_PRIV2, $_PRIV3"
      ;;
    3)
      _NET_MODE="default_vpc"
      echo "  → Will use region default VPC (dev/demo only)"
      ;;
    *)
      echo "  ✗ Invalid choice '$_VPC_CHOICE'. Aborting." >&2; exit 1
      ;;
  esac

  # Write network section into config.yml
  _NET_MODE="$_NET_MODE" \
  _VPC_CIDR="${_VPC_CIDR:-}" \
  _IMP_VPC_ID="${_IMP_VPC_ID:-}" _IMP_CIDR="${_IMP_CIDR:-}" \
  _PUB1="${_PUB1:-}" _PUB2="${_PUB2:-}" _PUB3="${_PUB3:-}" \
  _PRIV1="${_PRIV1:-}" _PRIV2="${_PRIV2:-}" _PRIV3="${_PRIV3:-}" \
  python3 - <<'PYEOF'
import os, re, pathlib

cfg_path = pathlib.Path("config.yml")
text = cfg_path.read_text()
mode = os.environ["_NET_MODE"]

# Guard already ensures network mode is empty/unset, so we replace it.
# Safe removal: only strip the network: block (up to next top-level key or EOF).
# This does NOT use greedy .* — it stops at the next ^[a-z] top-level key line.
text = re.sub(
    r'(\n*# ===*[^\n]*网络[^\n]*\n)?^network:\n(?:[ \t]+[^\n]*\n)*',
    '', text, count=1, flags=re.MULTILINE
)

# Build the network section
sep = "" if text.endswith("\n") else "\n"

if mode == "self_managed":
    cidr = os.environ.get("_VPC_CIDR") or "10.20.0.0/20"
    block = (
        f'{sep}\n# ========== 网络 (VPC) ==========\n'
        f'network:\n'
        f'  mode: self_managed\n'
        f'  self_managed:\n'
        f'    cidr: "{cidr}"\n'
        f'  imported:\n'
        f'    vpc_id: ""\n'
        f'    cidr: ""\n'
        f'    public_subnet_ids: []\n'
        f'    private_subnet_ids: []\n'
    )
elif mode == "imported":
    vpc_id = os.environ["_IMP_VPC_ID"]
    cidr = os.environ["_IMP_CIDR"]
    pub1, pub2, pub3 = os.environ["_PUB1"], os.environ["_PUB2"], os.environ["_PUB3"]
    priv1, priv2, priv3 = os.environ["_PRIV1"], os.environ["_PRIV2"], os.environ["_PRIV3"]
    block = (
        f'{sep}\n# ========== 网络 (VPC) ==========\n'
        f'network:\n'
        f'  mode: imported\n'
        f'  self_managed:\n'
        f'    cidr: "10.20.0.0/20"\n'
        f'  imported:\n'
        f'    vpc_id: "{vpc_id}"\n'
        f'    cidr: "{cidr}"\n'
        f'    public_subnet_ids: ["{pub1}", "{pub2}", "{pub3}"]\n'
        f'    private_subnet_ids: ["{priv1}", "{priv2}", "{priv3}"]\n'
    )
else:  # default_vpc
    block = (
        f'{sep}\n# ========== 网络 (VPC) ==========\n'
        f'network:\n'
        f'  mode: default_vpc\n'
        f'  self_managed:\n'
        f'    cidr: "10.20.0.0/20"\n'
        f'  imported:\n'
        f'    vpc_id: ""\n'
        f'    cidr: ""\n'
        f'    public_subnet_ids: []\n'
        f'    private_subnet_ids: []\n'
    )

text += block
cfg_path.write_text(text)
print(f"✓ config.yml: network.mode = {mode}")
PYEOF

  echo ""
fi

# ── 部署前配置门(#489)────────────────────────────────────────────────────────
# scripts/preflight-check.sh 一直存在却【从未被任何部署路径调用】,于是 2026-08-13 的真机
# 0→1 把它本来能挡的坑踩了一遍(VPCE 冲突、残骸撞名、死键)。门不跑等于不存在,所以焊在这里:
# cdk deploy 之前无条件跑一次,有 🔴 BLOCK 就不进 deploy。
# 只读:该脚本全程只 describe/list/get,不会改任何资源(它自己的头注释也这么写)。
# 逃生开关 PREFLIGHT_SKIP=1 默认关;用了会打醒目告警并把「你跳过了什么」说清楚 ——
# 留开关是因为这是部署入口的新中止点,任何误报都会直接挡住部署;但默认必须是拦。
if [ "${PREFLIGHT_SKIP:-0}" = "1" ]; then
  echo ""
  echo "⚠️  PREFLIGHT_SKIP=1 —— 跳过部署前配置门(#489)。"
  echo "    被跳过的判据包括:在役资源误判、VPCE private-dns 冲突、残骸撞名、config 死键、"
  echo "    Redis 子网组漂移(会致整栈回滚)。出问题时请先不带这个开关重跑一次再报。"
# 判据是 -f 不是 -x:这个脚本在 git 里是 100644(没有执行位,与 scripts/deploy-cdk.sh
# 的 100755 不同),而下面本来就是用 `bash <path>` 调它 —— 执行位跟这里的行为无关。写
# `-x` 会让门在【每一个新 clone 上都被静默跳过】,恰好就是 #489 要治的「门不跑等于没门」,
# 真机跑 setup.sh 撞到过。zip/tar 分发丢执行位的场景同理,所以 -f 也是更稳的判据。
elif [ -f scripts/preflight-check.sh ]; then
  echo ""
  echo "── 部署前配置门(scripts/preflight-check.sh,只读)──"
  # 主动传 config.yml/region/profile:门自己不猜这三样。PROFILE 为空时传 "-"(它的约定)。
  if bash scripts/preflight-check.sh config.yml "$REGION" "${PROFILE:--}"; then
    echo "✓ 部署前配置门通过,继续 cdk deploy"
  else
    echo "" >&2
    echo "⛔ 部署前配置门有 🔴 BLOCK 项,已在 cdk deploy 之前中止(#489)。" >&2
    echo "   逐条修掉上面的 BLOCK 再重跑 ./setup.sh。" >&2
    echo "   确认是误报、必须先部署时:PREFLIGHT_SKIP=1 ./setup.sh $REGION $PROFILE ..." >&2
    echo "   —— 但请顺手开一条 issue 记下那条误报,否则这道门会被逐渐绕成摆设。" >&2
    exit 1
  fi
  echo ""
else
  echo "⚠️  scripts/preflight-check.sh 不存在,跳过部署前配置门(#489)—— checkout 不完整?" >&2
fi

# ── host init 必需脚本清单(单一真相)─────────────────────────────────────────
# 定义提到 `cdk deploy` 之【前】,因为下面那道 #532 AC7 的门要用它,而复核(见文件后半
# 「上传后 fail-loud 校验」)用的是同一个变量 —— 清单只能有一份,两份必然漂。
#
# #532(Codex 独立复审 blocker-4)—— 清单从这里的内联字符串**移到文件**
# `deploy/userdata/required-scripts.list`。原因:它原先只存在于本文件,而**标准部署通道**
# `engineering/deploy/clawpool-deploy.sh` 用的是「桶里 .sh/.py 计数 ≥ 8」这种阈值判据 ——
# 22 个必需脚本、门在 8 就放行,缺 14 个都能绿。真机实证:`delete-all-vms.sh` 与
# `lib/lifecycle-guard.sh` 此刻在桶里 404,而那道计数门是绿的。移成文件后两条部署路径
# 读同一份,不可能漂。
# 读法:剥注释与空行;`.list` 不匹配 s3 sync 的 --include(*.sh/*.py/*.yaml/*.service),
# 所以它自己不会被上传到桶里。
_REQUIRED_SCRIPTS_FILE="$SCRIPT_DIR/deploy/userdata/required-scripts.list"
[ -f "$_REQUIRED_SCRIPTS_FILE" ] || {
  echo "FATAL: 缺 $_REQUIRED_SCRIPTS_FILE —— host 脚本发布门的清单是单一真相," >&2
  echo "  没有它就无法判断桶里齐不齐;拒绝在无清单的状态下部署(#532 AC7)。" >&2
  exit 1
}
# `|| true`:本文件是 `set -e`,清单全是注释时 grep 返 1 会让脚本**静默**死在这一行,
# 那就轮不到下面那句说明原因了。让它落空,由下面的自证门报话。
_REQUIRED_SCRIPTS="$(grep -vE '^[[:space:]]*(#|$)' "$_REQUIRED_SCRIPTS_FILE" | tr '\n' ' ' || true)"
# 判别力自证:清单被读空(路径错/文件被清)时这道门会在空集合上恒真 = 等于没有门。
_REQUIRED_SCRIPTS_N=$(printf '%s' "$_REQUIRED_SCRIPTS" | wc -w | tr -d ' ')
[ "${_REQUIRED_SCRIPTS_N:-0}" -ge 10 ] || {
  echo "FATAL: 从 $_REQUIRED_SCRIPTS_FILE 只读到 $_REQUIRED_SCRIPTS_N 个条目(期望 ≥10)" >&2
  echo "  —— 清单读空/读残时本门在空集合上恒真,拒绝放行(#532 AC7)。" >&2
  exit 1
}

# ── #532 AC7:deploy 前的 host 脚本发布门 ────────────────────────────────────
#
# AC7 原文:「部署检查在 required script 缺失时 fail loud,**禁止形成「控制面已上线、
# 恢复脚本不存在」的窗口**」。
#
# 那个窗口是 #532 生产事故的根因之一(Root cause 第 1 条)。ap-southeast-1 2026-08-18 的
# 一手证据:两个租户卡在 `deleting`,SSM 回执逐字是
#     [oc:delete] host 脚本缺失/过期,从 S3 自愈装载
#     [oc:delete] FATAL 拉取 delete-vm.sh 失败
# —— 桶里当时**没有** `delete-vm.sh`,所以连 `host_script_self_heal` 也救不了;
# 脚本直到 08:17:10Z 才补进 S3,而那两个租户已经耗尽主队列重投进了 DLQ。
#
# **为什么原有的那道门不够**:它在下面 `cdk deploy` 之【后】(先 deploy → 取桶名 → 上传
# → 复核)。桶名只能从栈输出取,所以上传本身没法前移;但「控制面已经上线、而桶里缺脚本」
# 这个状态是可以**在 deploy 之前就判掉**的 —— 只要栈已经存在。
#
# 判据分两种情形:
#   · **栈已存在**(= 增量部署,正是事故那个场景):桶里必须已有全部 required 脚本,
#     否则停在 deploy 之前 —— 新控制面不上线,不会去引用一个不存在的脚本;
#   · **栈不存在**(首次部署):跳过。此时没有控制面在跑、也没有租户,窗口不存在。
#
# **缺脚本的处置方式是这道门自己把缺的补上**(见下面那段),不是绕过它重跑 ——
# 重跑仍然是 deploy 在前、上传在后,补救本身就会重建这个窗口(Codex 独立复审指出)。
# 逃生舱 `OC_SKIP_PREDEPLOY_SCRIPT_GATE=1` 保留,是为了防「一道判错的门把部署彻底锁死」,
# 不是缺脚本的补救路径;用它就等于这一次自愿接受 AC7 那个窗口。
if [ "${OC_SKIP_PREDEPLOY_SCRIPT_GATE:-}" = "1" ]; then
  echo "⚠️  OC_SKIP_PREDEPLOY_SCRIPT_GATE=1:跳过 deploy 前的 host 脚本发布门(#532 AC7)" >&2
  echo "⚠️  这一次自愿接受「控制面已上线、恢复脚本可能不在桶里」的窗口;缺脚本的正常处置是" >&2
  echo "⚠️  让本门自己把缺的补上(把开关去掉重跑),不是绕过它。" >&2
else
  # #532:把「栈确实不存在」与「查不动」分开。原写法是 `2>/dev/null || true` ——
  # 吞掉全部错误,于是 AccessDenied / 凭据过期 / 限流 / 网络故障都退化成空串、被下面
  # 当成「首次部署」而**静默跳过这道门**,这条部署路径整个 fail-open
  # (Codex 独立复审在本 MR 上指出)。把「读不到」当成「不存在」是本仓明令禁止的假判定。
  # 只有 CloudFormation 明确回 "does not exist" 才算首次部署;其余一律 fail-closed。
  _pre_bucket=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
    --query 'Stacks[0].Outputs[?OutputKey==`AssetsBucket`].OutputValue' --output text \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" 2>&1) && _pre_rc=0 || _pre_rc=$?
  if [ "$_pre_rc" -ne 0 ]; then
    case "$_pre_bucket" in
      *"does not exist"*) _pre_bucket="" ;;   # 栈确实不存在 = 首次部署
      *)
        echo "FATAL: deploy 前脚本门:查 OpenClawOrchestrator 失败,判不出桶里齐不齐" >&2
        echo "  拒绝放行(不把「读不到」当成「不存在」)。原始错误:$_pre_bucket" >&2
        exit 1 ;;
    esac
  elif [ "$_pre_bucket" = "None" ]; then
    # 栈在、但没有 AssetsBucket 输出:那不是首次部署,而是判不出 ⇒ 同样 fail-closed。
    echo "FATAL: deploy 前脚本门:栈存在但没有 AssetsBucket 输出,判不出桶里齐不齐,拒绝放行" >&2
    exit 1
  fi
  if [ -z "$_pre_bucket" ]; then
    echo "· deploy 前脚本门:栈确实不存在(首次部署)—— 跳过。此时没有控制面在跑,无窗口。"
  else
    _pre_uploaded=$(aws s3 ls "s3://${_pre_bucket}/deployment/scripts/" --recursive \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" 2>&1) \
      || { echo "FATAL: deploy 前脚本门:列桶 s3://${_pre_bucket}/deployment/scripts/ 失败,拒绝放行(不猜)" >&2
           echo "  原始错误:$_pre_uploaded" >&2; exit 1; }
    _pre_uploaded=$(printf '%s\n' "$_pre_uploaded" | awk '{print $NF}')
    _pre_missing=""
    for _s in $_REQUIRED_SCRIPTS; do
      # #532:`-x` 整行精确匹配。原来是 `-qF`(子串),桶里一个 `route_ops.py.bak` 之类的
      # 邻居就能把这道门骗过去说「在桶里」,而 host 拉真键时照样 404 —— 那是 AC7 明文
      # 要禁的失效方式,不是无关的洁癖。
      # #532:用 here-string,**不要** `printf … | grep -q`。本文件是 `set -o pipefail`,
      # 而 `grep -q` 命中即退出 ⇒ 上游收 SIGPIPE、管道退出码 141 ⇒ 明明在桶里的对象被判
      # 成「缺失」。列表小时不触发,桶里对象一多就会 —— 一个只在规模上暴露的假判定。
      grep -qxF "deployment/scripts/$_s" <<<"$_pre_uploaded" \
        || _pre_missing="$_pre_missing $_s"
    done
    # #532:缺了就**在这里把缺的那几个补上、再复核**,而不是叫人绕过这道门重跑。
    # 原来那句补救写的是「OC_SKIP_PREDEPLOY_SCRIPT_GATE=1 bash setup.sh …」—— 可那一次
    # 重跑仍然是 deploy 在前、上传在后,**补救本身重建了这个窗口**。发版新增一个 required
    # 脚本时,那个脚本本来就还不在桶里,于是每次发版都会走上这条重建窗口的路
    # (Codex 独立复审在本 MR 上指出;`clawpool-deploy.sh` 那条路径已按同样形状修过)。
    # 只补缺的那几个、不在这里跑全量同步:全量会把本次所有改过的 host 脚本提前发布,而控制面
    # 还没上线;cdk deploy 随后若失败就是一次部分发布。全量同步仍在 deploy 之后(见文件后半)。
    if [ -n "$_pre_missing" ]; then
      echo "· deploy 前脚本门:桶里缺$_pre_missing —— 只补这几个,再复核(不让控制面先上线)"
      for _s in $_pre_missing; do
        [ -f "$SCRIPT_DIR/deploy/userdata/$_s" ] || {
          echo "FATAL: 清单里的 $_s 在仓内不存在(deploy/userdata/$_s)—— 清单与源码漂了" >&2
          echo "  推也推不上去;先修 $_REQUIRED_SCRIPTS_FILE 或补上源码文件。" >&2
          exit 1; }
        aws s3 cp "$SCRIPT_DIR/deploy/userdata/$_s" \
          "s3://${_pre_bucket}/deployment/scripts/$_s" \
          "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" >/dev/null || {
          echo "FATAL: deploy 前脚本门:上传 $_s 失败,拒绝放行" >&2; exit 1; }
      done
      _pre_uploaded=$(aws s3 ls "s3://${_pre_bucket}/deployment/scripts/" --recursive \
        "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" 2>&1) \
        || { echo "FATAL: deploy 前脚本门:补齐后列桶失败,拒绝放行(不猜)" >&2
             echo "  原始错误:$_pre_uploaded" >&2; exit 1; }
      _pre_uploaded=$(printf '%s\n' "$_pre_uploaded" | awk '{print $NF}')
      _pre_missing=""
      for _s in $_REQUIRED_SCRIPTS; do
        grep -qxF "deployment/scripts/$_s" <<<"$_pre_uploaded" \
          || _pre_missing="$_pre_missing $_s"
      done
    fi
    if [ -n "$_pre_missing" ]; then
      echo "" >&2
      echo "⛔ deploy 前脚本门(#532 AC7):补齐之后桶 s3://${_pre_bucket}/deployment/scripts/" >&2
      echo "   仍缺这些 required host 脚本:$_pre_missing" >&2
      echo "" >&2
      echo "   已在 cdk deploy 之【前】中止 —— 不让新控制面上线去引用一个不存在的脚本。" >&2
      echo "   那正是 #532 的根因:租户会卡在 deleting,连 host_script_self_heal 也救不了" >&2
      echo "   (它的来源对象本身不存在),而主队列重投耗尽后消息进 DLQ、无人接管。" >&2
      echo "" >&2
      echo "   逐个 cp 都推不上去,说明是真问题:查仓内是否真有这些文件、写桶权限," >&2
      echo "   以及 deploy 之后那次全量同步的 --include/--exclude 是否会把它们挡掉。" >&2
      echo "   清单在 $_REQUIRED_SCRIPTS_FILE。" >&2
      exit 1
    fi
    echo "✓ deploy 前脚本门:桶里已有全部 $(echo $_REQUIRED_SCRIPTS | wc -w | tr -d ' ') 个 required host 脚本"
  fi
fi
echo ""

# stack 选择符的裸 cdk deploy 会报 "specify which stacks ... or --all" 并退出。
# --all 按 add_dependency 拓扑序先 Orchestrator(建桶)后 OpenClawImage(烤镜像)。
PATH=".venv/bin:$PATH" scripts/deploy-cdk.sh "$REGION" "$PROFILE" \
  --all -c region="$REGION" "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" \
  --require-approval never ${CDK_ARGS[@]+"${CDK_ARGS[@]}"}

# Upload scripts to S3 (after deploy creates the bucket)
BUCKET=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].Outputs[?OutputKey==`AssetsBucket`].OutputValue' --output text \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION")
aws s3 cp "$SCRIPT_DIR/deploy/userdata/host-agent.py" "s3://${BUCKET}/deployment/scripts/host-agent.py" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# route_ops.py — host-agent.py:27 `import route_ops`(同目录 /opt/openclaw)。P2b 两级
# 路由的端口位图 + iptables DNAT + Redis 路由全在这。**没上传 → host-agent
# 同类:源码在但 setup.sh 上传清单漏了。init-host.sh 拉到 /opt/openclaw/route_ops.py。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/route_ops.py" "s3://${BUCKET}/deployment/scripts/route_ops.py" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# oc-guest-log-reader.py — init-host.sh 在 LOGGING_ENABLED=true 时拉到 /opt/openclaw/
# 装成 oc-guest-log-reader.service(收 guest vsock 日志落 per-VM oc-guest.log)。
# 漏传 → reader service 拉不起来(同 route_ops.py 的"源码在但清单漏"类)。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/oc-guest-log-reader.py" "s3://${BUCKET}/deployment/scripts/oc-guest-log-reader.py" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/sync-shared-skills.py" "s3://${BUCKET}/deployment/scripts/sync-shared-skills.py" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet

# Re-deploy is a no-op if nothing changed but it forces ASG hosts to retry
# pulling scripts now that S3 has them — guards against the race where the
# host's user-data ran before scripts were uploaded.
echo "✓ Scripts uploaded; existing hosts will pick them up via init-host.sh retry loop"
# 发到 deployment/bootstrap/edge/<sha256>/,edge userdata 绑同一个 sha256 —— 与
# host 的 deployment/bootstrap/host/<sha256>/init-host.sh 同一套语义。
# 删掉它才让 sha 绑定真的具备权威性:否则两个上传者写同一份资产,谁赢看时序。
# 改 edge 资产 = cdk deploy(或块 5 的 /bootstrap/promote 在已有版本间切换)。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/adot-config.yaml" "s3://${BUCKET}/deployment/scripts/adot-config.yaml" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet

# deployment/observability/ 的 10 个对象现在由 deploy/stacks/obs_assets.py 的
# BucketDeployment 随上面第 416 行的 cdk deploy 一起投放,并被 Host/Edge ASG 用
# add_dependency 挡在启动之前。原因:这个前缀是**开机时读**的,靠"记得重跑 setup.sh
# promote 到本前缀 → 下次开机自动回退)、#531(前缀比 bb 落后一天,护栏命中 0)。
# 顺带修掉一个不对称:下面 _REQUIRED_SCRIPTS 的桶内复核只覆盖 deployment/scripts/,
# 观测前缀从来没有门 —— 所以"漏传"在那半边是响的、在这半边一直是静默的。
#
# 判据用 sha256 而不是"对象存在":元数据里的 sha256 是 CDK 按**本次 checkout 的字节**
# 算的,与仓库文件现算的摘要相等,才证明机队开机拉到的就是这份代码。缺对象、摘要不等、
# 元数据没写上,三种都停 —— 这三种都会让 host 起坏或静默跑旧配置。
_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
  else echo "FATAL: 无 sha256sum/shasum,装一个再部署(证据摘要不可空)" >&2; return 1; fi
}
# key(相对 deployment/observability/)=仓库源文件。清单的权威副本在
# deploy/stacks/obs_assets.py 的 OBS_ASSETS;tests/test_265_obs_assets_bucketdeployment.py
# 断言两边逐条相等,所以这里改漏一条会在 CI 红,而不是等到真机。
_OBS_PAIRS="adot/adot-config.yaml=deploy/userdata/adot-config.yaml
fluent-bit/install-fluent-bit.sh=deploy/edge/fluent-bit/install-fluent-bit.sh
fluent-bit/edge/parsers.conf=deploy/edge/fluent-bit/edge/parsers.conf
fluent-bit/edge/extract_trace_root.lua=deploy/edge/fluent-bit/edge/extract_trace_root.lua
fluent-bit/edge/add_timestamp.lua=deploy/edge/fluent-bit/edge/add_timestamp.lua
fluent-bit/edge/fluent-bit.conf=deploy/edge/fluent-bit/edge/fluent-bit.conf
fluent-bit/host/parsers.conf=deploy/edge/fluent-bit/host/parsers.conf
fluent-bit/host/extract_tenant_id.lua=deploy/edge/fluent-bit/host/extract_tenant_id.lua
fluent-bit/host/add_timestamp.lua=deploy/edge/fluent-bit/host/add_timestamp.lua
fluent-bit/host/fluent-bit.conf=deploy/edge/fluent-bit/host/fluent-bit.conf"
_OBS_BAD=""
_OBS_OK=0
for _pair in $_OBS_PAIRS; do
  _k="${_pair%%=*}"; _src="${_pair#*=}"
  _want=$(_sha256 "$SCRIPT_DIR/$_src") || { _OBS_BAD="$_OBS_BAD $_k(本地摘要算不出)"; continue; }
  _got=$(aws s3api head-object --bucket "$BUCKET" --key "deployment/observability/$_k" \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" \
    --query 'Metadata.sha256' --output text 2>/dev/null || echo ABSENT)
  if [ "$_got" = "$_want" ]; then
    _OBS_OK=$((_OBS_OK + 1))
  else
    _OBS_BAD="$_OBS_BAD $_k(S3=$(printf %.12s "$_got") 仓库=$(printf %.12s "$_want"))"
  fi
done
if [ -n "$_OBS_BAD" ]; then
  echo "FATAL: deployment/observability/ 与本次 checkout 不一致(host 会起坏或静默跑旧观测配置):$_OBS_BAD" >&2
  echo "  这些对象由 CDK 的 BucketDeployment 投放(deploy/stacks/obs_assets.py),不由本脚本上传。" >&2
  echo "  查:上面第 416 行的 cdk deploy 是否真跑过、Obs* 自定义资源是否 CREATE/UPDATE_COMPLETE。" >&2
  exit 1
fi
echo "✓ 复核 deployment/observability/ 与本次 checkout 逐字节一致($_OBS_OK/10,CDK 投放)"

# 为什么在这里做:10W 规模并发启 host 时每台都打一次 github.com 的 releases,撞 GitHub
# rate limit → bootstrap 失败 → lifecycle hook ABANDON → ASG 替换循环。把「打 GitHub」
# 从【每台 host 一次】收敛成【部署机一次】,机队全部从 S3 拉
# (provision-host.sh 第 3 节的 _fc_s3_uri 按同一 key 布局取)。
#
# 两个架构都传:host 机队目前是 arm64 metal(m7g/m8g/r7g/r8g → aarch64),但 plain-AMI 路径
# 也可能跑在 x86_64 上。少传一个架构 = 那类机器静默回落 GitHub,正是本 issue 要消除的路。
# 幂等:已存在且摘要相同就跳过(重跑 setup.sh 不重复下载 ~10MB)。
# 版本号【从 provision-host.sh 解析】,不在这里各持一份。
# 为什么不写 "${FC_VERSION:-v1.15.1}":那样部署机的 FC_VERSION 只影响镜像哪个版本,
# 而 host / Packer / Image Builder 拿不到这个变量,仍按脚本里的 FC_VER 取件 —— 于是
# `FC_VERSION=v1.16.0 ./setup.sh` 会把 v1.16.0 传上去、报成功,机队却仍要 v1.15.1:
# boot 路径静默回落 github,bake 路径(强制 S3)直接构建失败。典型的假成功。
# 唯一源就选 host 真正执行的那个脚本 —— 而且升版本本来就必须编辑它(钉死摘要也在那里)。
_FC_VER_MIRROR="$(sed -n 's/^FC_VER="\${FC_VERSION:-\(v[0-9][0-9.]*\)}".*/\1/p' \
  "$SCRIPT_DIR/deploy/userdata/provision-host.sh" | head -1)"
if [ -z "$_FC_VER_MIRROR" ]; then
  echo "FATAL: 解析不出 provision-host.sh 的 FC_VER —— 该行写法变了?不猜版本,现在停。" >&2
  exit 1
fi
if [ -n "${FC_VERSION:-}" ] && [ "${FC_VERSION}" != "$_FC_VER_MIRROR" ]; then
  echo "FATAL: FC_VERSION=${FC_VERSION} 与 provision-host.sh 的 FC_VER=${_FC_VER_MIRROR} 不一致。" >&2
  echo "  改版本请编辑 provision-host.sh(同时补上该版本的钉死 sha256),而不是只在部署机设环境变量" >&2
  echo "  —— 后者只会镜像一个机队不要的版本并谎报成功。" >&2
  exit 1
fi
# 摘要固定,与 provision-host.sh 的 _fc_expected_sha() 【逐字同表】——
# tests/test_435_fc_binary_from_s3.py 有断言钉住两处一致。
# 这一侧校验的意义:别把一个被劫持/被换过的 GitHub 发布物镜像进自家桶,那等于亲手把污染源
# 搬到机队门口。键带版本号 → 改 _FC_VER_MIRROR 而不更新摘要就查不到 → 拒绝上传(fail-closed)。
_fc_expected_sha() {  # $1=arch;输出 64 位十六进制或空
  case "${_FC_VER_MIRROR}:$1" in
    v1.15.1:aarch64) printf '00654ac1e702a22744121ea9f10a4f792ebd7c3a744cba587dfac9fcb79b41a5' ;;
    v1.15.1:x86_64)  printf 'd4a32ab2322d887ca1bc4a4e7afa9cc35393e6362dfc2b3becb389d362e4275a' ;;
    *) return 0 ;;
  esac
}
_fc_mirror() {
  local arch="$1"
  local name="firecracker-${_FC_VER_MIRROR}-${arch}.tgz"
  local key="deployment/binaries/firecracker/${_FC_VER_MIRROR}/${name}"
  # 先取本(版本, 架构)钉死的摘要 —— 跳过判据要用它。
  local want; want=$(_fc_expected_sha "$arch")
  [ -n "$want" ] || { echo "  ✗ ${_FC_VER_MIRROR}/${arch} 没有钉死的 sha256 —— 升版本请先在 _fc_expected_sha 里补上,拒绝镜像未核对的制品" >&2; return 1; }
  # 幂等跳过的判据是【对象记录的 sha256 == 钉死值】,而不是"key 存在"。
  # 只判存在会留下一个洞:对象损坏或被覆盖后,mirror 永远跳过 → 永远不自愈,而 setup 报成功;
  # 于是 boot 路径静默回落 github,bake 路径(强制 S3)直接构建失败。所以不匹配就重传覆盖。
  # 【这不是安全控制】:metadata 由能写该对象的人自己写,攻击者会把它设成匹配值。安全控制在
  # host 侧 —— provision-host.sh 安装前对【真实字节】强制校验(_fc_expected_sha)。
  # 这里管的是运维正确性:让 mirror 能自愈,并且不谎报就绪。
  # fc-version/mirrored-at/arch,真机 head-object 实测),它恰好也记了 sha256 且与钉死值一致。
  local _existing_sha
  _existing_sha=$(aws s3api head-object --bucket "$BUCKET" --key "$key" \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" \
    --query 'Metadata.sha256' --output text 2>/dev/null) || _existing_sha=""
  if [ "$_existing_sha" = "$want" ]; then
    echo "  ✓ ${name} 已在 S3 且记录摘要与钉死值一致,跳过(sha256=${want:0:12}…)"
    return 0
  fi
  if [ -n "$_existing_sha" ] && [ "$_existing_sha" != "None" ]; then
    echo "  ⚠ ${name} 已在 S3 但记录摘要与钉死值不符(记录 ${_existing_sha:0:12}…,期望 ${want:0:12}…)—— 重新下载并覆盖" >&2
  fi
  local tmp; tmp="$(mktemp -d)"
  # 部署机这一次是允许打 GitHub 的 —— 它就是本 issue 要把 N 次收敛成 1 次的那 1 次。
  if ! curl -fsSL --connect-timeout 10 --max-time 300 \
       -o "${tmp}/${name}" \
       "https://github.com/firecracker-microvm/firecracker/releases/download/${_FC_VER_MIRROR}/${name}"; then
    echo "  ✗ 从 GitHub 下载 ${name} 失败" >&2
    mv "$tmp" "${TMPDIR:-/tmp}/oc435-failed-$$" 2>/dev/null || true
    return 1
  fi
  # 的 BucketDeployment 投放并自带 metadata,这里是 deployment/scripts/ 侧的同款做法)。
  local sha; sha=$(_sha256 "${tmp}/${name}") || { echo "  ✗ sha256 计算失败" >&2; return 1; }
  echo "$sha" | grep -qiE '^[0-9a-f]{64}$' || { echo "  ✗ sha256 格式非法: '$sha'" >&2; return 1; }
  # 与钉死的摘要比对(want 已在函数开头取好)。格式合法 ≠ 内容正确 —— 只验格式的话,
  # 一个被换过的发布物照样通过。
  [ "$sha" = "$want" ] || { echo "  ✗ ${name} sha256 不匹配:实得 ${sha},期望 ${want} —— 拒绝上传" >&2; return 1; }
  # tar 完整性先验一遍,别把坏包传上去让 380 台 host 一起 tar 失败。
  tar -tzf "${tmp}/${name}" >/dev/null 2>&1 || { echo "  ✗ ${name} 不是合法 tgz" >&2; return 1; }
  aws s3 cp "${tmp}/${name}" "s3://${BUCKET}/${key}" \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet \
    --metadata "sha256=${sha},uploaded-at=${_OBS_TS},git-commit=${_OBS_COMMIT}" \
    || { echo "  ✗ 上传 ${name} 失败" >&2; return 1; }
  echo "  ✓ ${name} → s3://${BUCKET}/${key} (sha256=${sha:0:12}…)"
  rm -rf "$tmp"
}
_fc_mirror_failed=0
for _arch in aarch64 x86_64; do
  _fc_mirror "$_arch" || _fc_mirror_failed=1
done
if [ "$_fc_mirror_failed" -ne 0 ]; then
  # 【故意不在这里 exit】。第一版就是在这里 exit 1,那是把严重性排序搞反了:本段下面还要上传
  # launch-vm.sh / lib/harden-config.sh / lib/cred-inject.sh 等 host init 必需脚本 —— 缺
  # harden-config.sh 的后果是「launch-vm.sh 每次启动 exit 1,一台都起不来」(见下方各条注释),
  # 比「FC 回落 github」严重得多。也就是说 GitHub 抖一下就能让部署在关键脚本落地前中止,
  # 反而制造更大的故障。
  # 所以只记账,等必需脚本上传【并校验】完再 exit(见下方 _fc_mirror_failed 的最终判定)。
  echo "⚠ #435 Firecracker mirror 未全部就绪 —— 先把 host init 必需脚本传完再报错" >&2
else
  echo "✓ Firecracker ${_FC_VER_MIRROR} binaries mirrored to s3://${BUCKET}/deployment/binaries/firecracker/"
fi
aws s3 cp "$SCRIPT_DIR/deploy/userdata/backup-data.sh" "s3://${BUCKET}/deployment/scripts/backup-data.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/launch-vm.sh" "s3://${BUCKET}/deployment/scripts/launch-vm.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# 而非内联 init-host.sh(避免撑爆 user-data 16KB 硬限);init-host.sh 拉到 /home/ubuntu/
# 后执行,config security.egress_allowlist_enabled 默认 false 时脚本自身跳过。缺它 →
# init-host WARN 跳过(host-agent 仍起),egress 退回现状放行(非致命)。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/setup-egress-allowlist.sh" "s3://${BUCKET}/deployment/scripts/setup-egress-allowlist.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# oc-egress-chain.sh + oc-egress-sim.py(#566)— guest 出网 default-deny 白名单基线。
# sim 是唯一权威规则序(--emit-rules),chain 脚本据此原子换入 host 级 tap 共享链
# OPENCLAW-EGRESS。init-host.sh 在 egress_mode=deny 时拉到 /home/ubuntu/ 后 apply;
# egress_mode=off(默认)时 init-host 跳过(host 零变化,非致命)。两个必须同时在桶,
# chain 脚本 SPEC_SCRIPT 指向同目录 oc-egress-sim.py,缺 sim → apply die。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/oc-egress-chain.sh" "s3://${BUCKET}/deployment/scripts/oc-egress-chain.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/oc-egress-sim.py" "s3://${BUCKET}/deployment/scripts/oc-egress-sim.py" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# 收敛库(每次启动跑,不管 fresh/wake)。同 host-agent.py / launch-vm.sh 的下发
# 契约:setup.sh 上传到 S3,init-host.sh 拉到 /home/ubuntu/lib/。缺它 →
# launch-vm.sh 顶部 `. lib/harden-config.sh` 失败 → 每次启动 exit 1,一台起不来。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/lib/harden-config.sh" "s3://${BUCKET}/deployment/scripts/lib/harden-config.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# 契约:setup.sh 上传,init-host.sh 拉到 /home/ubuntu/lib/。只在租户有 injected_
# credentials 时才 source;缺它则该 VM fail-loud 中止(不静默注入空凭据)。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/lib/cred-inject.sh" "s3://${BUCKET}/deployment/scripts/lib/cred-inject.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# spire-kit/(#516)— init-host.sh step4c 在 SSM /openclaw/spire-kit/enabled=true 时逐个拉
# 这四个键。这里必须逐个上传:整目录 sync 只存在于内部部署脚本里,而 setup.sh 是对外唯一
# delete-vm.sh 那个坑。开机拉不到时 step4c 是 fail-open(host 照常服务、落
# spire-kit.install-failed 标记),所以症状不是起不来而是这台 host 静默没装上 broker。
# 【故意不在这里 exit】同 _fc_mirror_failed 的理由:本段下面还要传 launch-vm.sh /
# lib/harden-config.sh 等 host init 必需脚本,缺它们是「一台都起不来」。spire-kit 是可选组件
# (SSM 开关默认关、开机 fail-open),让它的一次瞬时上传失败在必需脚本落地前中止部署,是把
# 严重性排序搞反 —— 反而制造半部署状态。所以只记账,最末统一判定(搜 _spire_kit_upload_failed)。
_spire_kit_upload_failed=0
for _spire_f in spire-kit-setup.sh install.sh spire-join-broker.py spire-join-broker.service; do
  aws s3 cp "$SCRIPT_DIR/deploy/userdata/spire-kit/${_spire_f}" \
    "s3://${BUCKET}/deployment/scripts/spire-kit/${_spire_f}" \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet \
    || { echo "⚠ spire-kit/${_spire_f} 上传失败 —— 先把 host init 必需脚本传完再报错" >&2
         _spire_kit_upload_failed=1; }
done
aws s3 cp "$SCRIPT_DIR/deploy/userdata/stop-vm.sh" "s3://${BUCKET}/deployment/scripts/stop-vm.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# launch + runtime FD evidence. Referenced by the rebuild control path.
aws s3 cp "$SCRIPT_DIR/deploy/userdata/rebuild-vm.sh" "s3://${BUCKET}/deployment/scripts/rebuild-vm.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# #485 — op-scoped reset transaction; never reconstruct as bare rm overlay.
aws s3 cp "$SCRIPT_DIR/deploy/userdata/reset-vm.sh" "s3://${BUCKET}/deployment/scripts/reset-vm.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# 必须上传 + 进 _REQUIRED_SCRIPTS 门:否则 SSM 调它得到 exit 127 而 delete 静默失败
aws s3 cp "$SCRIPT_DIR/deploy/userdata/delete-vm.sh" "s3://${BUCKET}/deployment/scripts/delete-vm.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/clone-data.sh" "s3://${BUCKET}/deployment/scripts/clone-data.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# missing file (exit 127) and live migration silently failed end-to-end.
aws s3 cp "$SCRIPT_DIR/deploy/userdata/migrate-vm.sh" "s3://${BUCKET}/deployment/scripts/migrate-vm.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# the tenant data volume). Referenced by the resize-disk API but never uploaded.
aws s3 cp "$SCRIPT_DIR/deploy/userdata/resize-disk.sh" "s3://${BUCKET}/deployment/scripts/resize-disk.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# start-all-vms / stop-all-vms — host-local fan-out for the 1-minute fleet power
# goal (control plane sends ONE SSM per host; host starts/stops all its VMs in
# bounded parallel). Same upload-or-404 contract as the helpers above.
aws s3 cp "$SCRIPT_DIR/deploy/userdata/start-all-vms.sh" "s3://${BUCKET}/deployment/scripts/start-all-vms.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
aws s3 cp "$SCRIPT_DIR/deploy/userdata/stop-all-vms.sh" "s3://${BUCKET}/deployment/scripts/stop-all-vms.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# SSM 并发 = host 数而非租户数)。与 start/stop-all-vms.sh 同一模式,但清单驱动:
# 只删 manifest 里指定的那批租户,不按目录删。
# 上传必须先于任何会调它的控制面上线 —— #532 的根因就是"控制面已上线、脚本不在桶里"。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/delete-all-vms.sh" "s3://${BUCKET}/deployment/scripts/delete-all-vms.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet
# delete-all-vms.sh 对清单里【每个】租户在破坏性动作前后各调一次;缺它 = 无围栏
# 的批量 rm -rf,故与 lib/harden-config.sh / lib/cred-inject.sh 同档进桶 + 进清单。
aws s3 cp "$SCRIPT_DIR/deploy/userdata/lib/lifecycle-guard.sh" "s3://${BUCKET}/deployment/scripts/lib/lifecycle-guard.sh" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet

# ── 上传后 fail-loud 校验(根治「源码在但没进桶」这一类反复踩的 bug:route_ops.py /
#    → crashloop → lifecycle ABANDON 换机器死循环,顺路径测不出来,只有真部署才炸)。
#    不用 `s3 sync deploy/userdata/`:那目录里有 init-host.sh(LT 烤进镜像的,不走 S3)、
#    *.bak / __pycache__ / runtimes/ / host-agent.service,sync 会把它们污染进 scripts 前缀。
#    保留逐个 cp(每条带 why),这里独立维护一份「host init 必需脚本」清单,传完直接查桶——
#    缺任一个立即停,别把「某脚本静默没传」的软 bug 拖成「host 永远起不来」的硬 bug。
#    也兜住上面 `|| true` 吞错、SSM 后台跑到一半被砍这类 set -e 抓不到的漏传。
# #532 AC7 —— 清单的定义已提到 `cdk deploy` 之前(搜 `_REQUIRED_SCRIPTS=`),因为那里新增了
# 一道 deploy 前的门要用它。这里**复用同一个变量**:清单只能有一份,两份必然漂 ——
# 而漂的表现正是这道门要防的那件事(某个脚本没进桶,却以为查过了)。
# #532:列桶失败要与「列到了但缺」分开报。原写法 `2>/dev/null` 吞错 —— 判定方向仍是
# fail-closed(列不出 ⇒ 全判缺 ⇒ exit 1),但报出来的原因会是「脚本没进 S3」,把人引去查
# s3 cp,而真因是权限/网络。同上面那道 deploy 前门一条纪律:不把「读不到」说成「不存在」。
_UPLOADED=$(aws s3 ls "s3://${BUCKET}/deployment/scripts/" --recursive \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" 2>&1) \
  || { echo "FATAL: 列桶 s3://${BUCKET}/deployment/scripts/ 失败,判不出必需脚本齐不齐,现在停" >&2
       echo "  原始错误:$_UPLOADED" >&2; exit 1; }
_UPLOADED=$(printf '%s\n' "$_UPLOADED" | awk '{print $NF}')
_MISSING=""
for _s in $_REQUIRED_SCRIPTS; do
  # #532:`-x` 整行精确匹配(与上面那道 deploy 前门同一条理由)。子串匹配下桶里一个
  # `route_ops.py.bak` 之类的邻居就能把门骗绿,而 host 拉真键时照样 404。
  # #532:here-string,不要 `printf … | grep -q`(pipefail + grep -q 提前退出 = 141,
  # 在桶里的对象被判成缺失)。这一处的方向更坏:它会**误拦一次合法部署**。
  grep -qxF "deployment/scripts/$_s" <<<"$_UPLOADED" \
    || _MISSING="$_MISSING $_s"
done
if [ -n "$_MISSING" ]; then
  echo "FATAL: host init 必需脚本没进 S3(host 会 crashloop-ABANDON):$_MISSING" >&2
  echo "  桶 s3://${BUCKET}/deployment/scripts/ 缺上面这些,现在停。查上面对应 aws s3 cp 是否失败/被跳过。" >&2
  exit 1
fi
echo "✓ 校验 host init 必需脚本全部在桶($(echo $_REQUIRED_SCRIPTS | wc -w) 个)"

# 演进过程记在这里,因为这条顺序被改了两次、每次都是为同一个道理:
#   v1 紧跟 mirror 循环 exit → 一次 GitHub 抖动就让部署停在 launch-vm.sh / harden-config.sh
#      落地之前,把「FC 回落 github」这个可恢复问题换成「host 永远起不来」;
#   v2 挪到本处(必需脚本查桶校验之后)→ 好一些,但后面还有 LiteLLM/监控资产、SSM 参数、
#      部署输出与 console 配置,提前退出仍然留下半部署状态;
#   v3(当前)推到脚本最末 → 该做的全做完,再以非零退出如实报告 mirror 没就绪。
# 早退没有任何好处:mirror 缺件不影响后续步骤,而后续步骤缺了都会各自制造故障。

# 聚合 SSM 命令现调 `launch-vm.sh --manifest|--from-ddb ...`(见 dispatch_service.py),
# 不再单独上传 launch-all-vms.sh。launch-vm.sh 的上传在上方(第 415 行)。

# 全部归档到 engineering/04-archive/p4-cutover-deprecated/。数据面改两级路由
# 直连 microVM 原生 gateway(ALB LOR → OpenResty edge → Redis → host DNAT →
# microVM:18789),setup.sh 不再上传 hub 资产;init-host.sh 里 install-hub.sh
# 引用也应一并删(独立 issue,同 stack.py CloudFront /hub behavior + HubTG 收尾)。

# LiteLLM 网关资产(ai_gateway.url 留空时,CDK 起的 LiteLLM EC2 userdata 从这拉
# docker-compose + config 跑起网关)。总是上传(幂等),开关在 CDK 侧。
aws s3 cp "$SCRIPT_DIR/deploy/litellm/" "s3://${BUCKET}/deployment/litellm/" \
  --recursive --exclude "*.md" "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet 2>/dev/null || true
# config 模板在 runtime-config-export/(litellm-up.sh 默认从 ../runtime-config-export 读),
# 单独补传进 deployment/litellm/ 让 EC2 userdata 能就地 sed 生成 config.runtime.yaml。
# 缺它 → compose 把不存在的挂载源当目录建 → 容器 IsADirectoryError 崩溃(已踩坑)。
aws s3 cp "$SCRIPT_DIR/deploy/runtime-config-export/litellm-config.yaml" "s3://${BUCKET}/deployment/litellm/litellm-config.yaml" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet 2>/dev/null || true

# 监控平台资产 → s3://.../deployment/monitoring/。
# 自建 Prometheus+Grafana(stack.py PromGrafanaMonitor)+ Wazuh(WazuhMonitor)
# 的 EC2 userdata 都从这个前缀 s3 sync 整目录拉;userdata 侧重试判据看
# compose 文件是否到位(stack.py),兜住 setup.sh 上传 vs 实例首启的竞态。
# 用 sync 整目录(而非逐文件 cp),避免漏传子资产 —— 曾只 cp wazuh 两个文件,
# 导致 prom-grafana 的 compose/prometheus.yml/grafana/ 从未上传, 监控 EC2 的
# docker compose up 报 no such file, 监控名存实亡。整目录 sync 一次性覆盖两套栈。
# --exclude 掉 wazuh-two-ec2/(那是双机 Wazuh 的独立 userdata, 不进本前缀)。
aws s3 sync "$SCRIPT_DIR/deploy/monitoring/" "s3://${BUCKET}/deployment/monitoring/" \
  --exclude "wazuh-two-ec2/*" --exclude "*.md" \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" --quiet

# 编排层(post-deploy,非 CFN CR)——CR 塞进去会让部署耦合 LiteLLM 健康态、timeout
# 回滚更脆。这里等 LiteLLM /health/liveliness healthy 后 POST /key/generate → put SSM。
# 幂等:SSM 已有值就跳过(轮换用 SKIP_MINT_SHARED_VKEY=1 手动 aws ssm put + LiteLLM /key/delete 老 key)。
# 存量部署接过来时首次运行会自动铸,不需要人工先跑 curl。
#
# #480 — 没有配置任何 LiteLLM 网关时跳过铸造。判据必须与 CDK build_litellm 一致:
# ai_gateway.url 空【且】managed_by_stack 非 true = 本栈不托管网关,SSM /openclaw/litellm-host
# 不存在,mint-shared-vkey.sh 会去连一个不存在的网关,默认卡到 600s 超时后报错。
# (url 填了 = 外部网关可达;managed_by_stack=true = CDK 起了网关 → 两种情况都仍要铸。)
_OC_AIGW_DEPLOYED=$(python3 - <<'PY' 2>/dev/null || echo unknown
import pathlib, yaml
try:
    c = yaml.safe_load(pathlib.Path("config.yml").read_text()) or {}
    g = c.get("ai_gateway", {}) or {}
    url = (g.get("url") or "").strip()
    managed = bool(g.get("managed_by_stack", False))
    print("yes" if (url or managed) else "no")
except Exception:
    print("unknown")
PY
)
if [ "$_OC_AIGW_DEPLOYED" = "no" ]; then
  echo "→ 跳过 shared vkey 铸造(#480:ai_gateway 未配置网关 —— url 空且 managed_by_stack=false)。"
  echo "  数据面 chat 需要网关时,填 ai_gateway.url 复用外部网关,或设 managed_by_stack=true 让 CDK 自建,再重跑 setup.sh。"
elif [ "${SKIP_MINT_SHARED_VKEY:-0}" = "1" ]; then
  echo "→ 跳过 shared vkey 铸造(SKIP_MINT_SHARED_VKEY=1)"
else
  echo "→ 铸/校验 LiteLLM shared vkey → SSM /openclaw/litellm-shared-vkey ..."
  # $VKEY_MINT_RC 供调用者判断。生产 setup 若要严格失败就在此 exit $VKEY_MINT_RC。
  set +e
  REGION="$REGION" PROFILE="$PROFILE" \
    bash "$SCRIPT_DIR/deploy/lib/mint-shared-vkey.sh"
  VKEY_MINT_RC=$?
  set -e
  if [ "$VKEY_MINT_RC" -ne 0 ]; then
    echo "⚠  shared vkey 铸造失败(rc=$VKEY_MINT_RC)。host launch-vm 侧有自愈重读兜底," >&2
    echo "  但对话可能持续 401 直到手工修复。请查 mint-shared-vkey 日志、修好后重跑 setup.sh。" >&2
  fi
fi

# 导出 stack outputs
echo "→ 导出部署信息..."
OUTPUTS=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output text \
  "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION")

cat > "$SCRIPT_DIR/.env.deploy" << EOF
# Auto-generated by setup.sh — $(date -Iseconds)
REGION=$REGION
PROFILE=$PROFILE
$(echo "$OUTPUTS" | awk '{
  key=$1
  out=""
  for(i=1;i<=length(key);i++){
    c=substr(key,i,1)
    if(c ~ /[A-Z]/ && i>1) out=out"_"
    out=out toupper(c)
  }
  print out"="$2
}')
EOF

# 查询 API Key value (stack output 只有 ID)
API_KEY_ID=$(grep '^API_KEY_ID=' "$SCRIPT_DIR/.env.deploy" | cut -d= -f2)
if [ -n "$API_KEY_ID" ]; then
  API_KEY=$(aws apigateway get-api-key --api-key "$API_KEY_ID" --include-value \
    --query 'value' --output text "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION")
  echo "API_KEY=$API_KEY" >> "$SCRIPT_DIR/.env.deploy"
fi

echo "✓ 环境信息已保存到 .env.deploy"
cat "$SCRIPT_DIR/.env.deploy"

# Upload console to S3 (generate config.js first)
source "$SCRIPT_DIR/.env.deploy"

# 规范化 COGNITO_DOMAIN:前端代码自己拼 "https://" + domain（console/chat 两页
# 的 fetch/redirect 都是这逻辑），所以注入值必须是**裸主机名**，不能带 scheme。
# 历史上有部署因 stack output 带了 https:// 前缀，sed 进去后拼成 "https://https://…"
# 畸形 URL → 登录重定向 ERR_TIMED_OUT、chat 拿不到用户身份显示"名下无节点"。
# 这里无条件 strip 掉 scheme 和尾部斜杠，无论上游 output 怎么填都对，随重建继承。
COGNITO_DOMAIN="$(printf '%s' "${COGNITO_DOMAIN:-}" | sed -E 's#^https?://##; s#/+$##')"

VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('$SCRIPT_DIR/pyproject.toml','rb'))['project']['version'])" 2>/dev/null || echo "dev")

# (the per-tenant ALB-fronted distribution), while OC_CONSOLE_BASE +
# OC_COGNITO_REDIRECT_URI point at console_domain (Cognito-protected).
# In legacy single-mode, both equal DASHBOARD_URL — backward-compat preserved.
CONSOLE_BASE="${CONSOLE_URL:-${DASHBOARD_URL:-}}"
DASHBOARD_BASE="${DASHBOARD_URL:-}"

# 真 key 不进 IaC 模板(CDK auth.py 里是 PLACEHOLDER_INJECT_AT_DEPLOY),部署后由此注入,浏览器全程零 key。
# 仅当配了 console_auth.bff_certificate_arn 部署出该 Lambda 时才注;没部署则 describe 失败静默跳过。
if [ -n "${API_KEY:-}" ] && \
   aws lambda get-function --function-name openclaw-console-bff \
     "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" >/dev/null 2>&1; then
  # update-function-configuration 的 --environment 是整体替换,故先完整读回现有 env、只覆写
  # CTRL_API_KEY,用 jq 合并回去(不丢 CDK 注入的 CTRL_API_BASE 及其它 env)。
  CUR_ENV_JSON="$(aws lambda get-function-configuration --function-name openclaw-console-bff \
    "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" \
    --query 'Environment.Variables' --output json 2>/dev/null)"
  if [ -z "$CUR_ENV_JSON" ] || [ "$CUR_ENV_JSON" = "null" ]; then
    echo "⚠ Console BFF: 读现有 env 失败,跳过 key 注入(避免整体替换丢 CTRL_API_BASE)"
  else
    # --environment 直接用 JSON 格式(比 shorthand Variables={} 稳:value 含特殊字符不崩)。
    ENV_ARG="$(printf '%s' "$CUR_ENV_JSON" | jq -c --arg k "$API_KEY" '{Variables: (. + {CTRL_API_KEY:$k})}')"
    aws lambda update-function-configuration --function-name openclaw-console-bff \
      --environment "$ENV_ARG" \
      "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" >/dev/null 2>&1 \
      && echo "✓ Console BFF: admin key injected into openclaw-console-bff env (browser stays key-less)" \
      || echo "⚠ Console BFF: key injection failed (check openclaw-console-bff exists / IAM lambda:UpdateFunctionConfiguration)"
  fi
fi

# 写 CloudFront 域到 SSM,供 init-host.sh 运行时拉(解 CloudFront 晚于 LaunchTemplate 的
# CDK 循环依赖)。租户 gateway allowedOrigins 用它,不硬编码旧账号域。
CF_ORIGIN_VAL="${DASHBOARD_BASE:-${CONSOLE_BASE:-}}"
if [ -n "$CF_ORIGIN_VAL" ]; then
  aws ssm put-parameter --name /openclaw/cloudfront-origin --type String \
    --value "$CF_ORIGIN_VAL" --overwrite "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" >/dev/null 2>&1
  echo "✓ SSM /openclaw/cloudfront-origin = ${CF_ORIGIN_VAL}"
fi

# WI-002 — publish the channel machine-user app client id to SSM so the metal
# single-process hub (install-hub.sh) can read it and verify channel access
# tokens. Empty unless console_auth.channel_cognito_auth is enabled (the stack
# only emits CognitoChannelClientId then). install-hub treats empty as "channel
# Cognito disabled" → HMAC-only, so this is a no-op for non-opted-in deployments.
if [ -n "${COGNITO_CHANNEL_CLIENT_ID:-}" ]; then
  aws ssm put-parameter --name /openclaw/cognito-channel-client-id --type String \
    --value "$COGNITO_CHANNEL_CLIENT_ID" --overwrite "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" >/dev/null 2>&1
  echo "✓ SSM /openclaw/cognito-channel-client-id = ${COGNITO_CHANNEL_CLIENT_ID}"
fi

# hub(install-hub.sh)从 SSM 读 Cognito pool/client id 验前端 id_token 签名。之前
# 没人在部署时写这俩 → 重建后 SSM 残留旧 pool 值 → hub 用旧 JWKS 验新 token → 401
# (重建实撞)。栈每次重建 pool/client 都会变,故 setup.sh 拿到新值后覆写 SSM。
if [ -n "${COGNITO_USER_POOL_ID:-}" ]; then
  aws ssm put-parameter --name /openclaw/cognito-user-pool-id --type String \
    --value "$COGNITO_USER_POOL_ID" --overwrite "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" >/dev/null 2>&1
  echo "✓ SSM /openclaw/cognito-user-pool-id = ${COGNITO_USER_POOL_ID}"
fi
if [ -n "${COGNITO_CLIENT_ID:-}" ]; then
  aws ssm put-parameter --name /openclaw/cognito-client-id --type String \
    --value "$COGNITO_CLIENT_ID" --overwrite "${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"}" --region "$REGION" >/dev/null 2>&1
  echo "✓ SSM /openclaw/cognito-client-id = ${COGNITO_CLIENT_ID}"
fi
echo ""
if [ "${DUAL_DOMAIN_MODE:-false}" = "true" ]; then
  echo "→ Console URL:    ${CONSOLE_BASE}/console/index.html  (operator login)"
  echo "→ Dashboard URL:  ${DASHBOARD_BASE}/vm/<tenant-id>/   (per-tenant)"
  echo "  ✓ Dual-domain mode active — Cognito session physically isolated from tenant dashboards"
else
  echo "→ Console URL: ${CONSOLE_BASE}/console/index.html"
  if [ -z "${CUSTOM_DOMAIN:-}" ]; then
    echo "  ⚠️  Single-domain mode — for production, see README §Multi-Domain Setup"
  fi
fi

# 部署该做的全部做完了,现在才如实报告 mirror 是否就绪。放这里而不是紧跟 mirror 循环的理由
# 见上方 _fc_mirror_failed 处的注释:早退会把一个可恢复问题(FC 回落 github)换成更严重的
# 半部署状态(缺 launch-vm.sh / harden-config.sh / LiteLLM 配置 / SSM 参数 / console 配置)。
# 但仍必须非零退出:mirror 缺件时机队会回落 github,10W 规模照样撞墙,而 bake 路径(强制 S3)
# 会直接构建失败 —— 那种情况下把部署报成绿的就是假绿。
if [ "$_fc_mirror_failed" -ne 0 ]; then
  echo "" >&2
  echo "FATAL: #435 Firecracker mirror 未全部就绪 —— 机队会回落 github.com,拒绝静默通过" >&2
  echo "  上面所有部署步骤均已执行完毕(必需脚本已查桶确认),所以现有机队不受影响。" >&2
  echo "  补齐 mirror 后重跑本脚本即可 —— mirror 步骤幂等,不会重复上传已就绪的对象。" >&2
  exit 1
fi

# ── #580 spire-kit 分发的最终判定(同上,刻意放在最末)──────────────────────────────────
# 可选组件,所以上传失败不在当场中止(理由见上方 _spire_kit_upload_failed 处)。但仍必须非零
# 退出:开机 step4c 拉不到时是 fail-open —— host 照常服务、只落 spire-kit.install-failed 标记,
if [ "${_spire_kit_upload_failed:-0}" -ne 0 ]; then
  echo "" >&2
  echo "FATAL: #580 spire-kit 四个开机键未全部上传 —— 开启 spire-kit 的 host 会静默没装上 broker" >&2
  echo "  上面所有部署步骤均已执行完毕(必需脚本已查桶确认),所以现有机队不受影响。" >&2
  echo "  重跑本脚本即可 —— s3 cp 幂等。若 spire-kit 本就不用,把 SSM /openclaw/spire-kit/enabled 留空。" >&2
  exit 1
fi
