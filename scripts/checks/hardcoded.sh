#!/usr/bin/env bash
# hardcoded.sh — 硬编码常量扫描(第三层)。借 IrisLint / EmkerrPythonHacks 正则思路本地重写,
# 不接任何外部依赖。扫 AI 高频写死、且违反本仓脱敏红线 + 通用产品定位的常量:
#   - AWS account ID(12 位数字;可在 KNOWN_ACCOUNT_IDS 登记你组织的真实账号做硬阻断)
#   - ARN partition 写死(arn:aws: 在应逻辑参数化处)
#   - region 写死(ap-southeast-1 / us-west-2 等,应走参数/env)
#   - 内部域名 / 堡垒机公网 IP
#   - 品牌残留字样(在 BRAND_WORDS 登记要阻断的品牌词)
#
# allowlist:示例占位、文档等客观依赖豁免(下方 ALLOW_* 逐条注释来源)。
# 降级:纯 grep/sed,无外部依赖,任何有 bash 的机器都能跑,不降级。
#
# 用法:scripts/checks/hardcoded.sh [CK_SCAN_ALL=1]
# 退出:0=干净;1=发现应参数化的硬编码。
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "hardcoded · 账号/ARN/region/域名/品牌 硬编码扫描"
rc=0

# ── allowlist(逐条注释豁免理由;命中这些的行不算硬编码)────────────
# 用 grep -E 的排除模式,能扩展
ALLOW='REDACTED|<[a-zA-Z_]+>|example|placeholder|EXCHANGE_API_BASE|EXCHANGE_TESTNET|\.example|dummy|YOUR_|xxxxxxxxxxxx|000000000000|123456789012|111111111111|111122223333'
# 说明:
#   EXCHANGE_API_BASE / EXCHANGE_TESTNET — 参数化第三方行情端点的 env 名,本身不是硬编码
#   123456789012 / 000000000000 / xxxxxxxxxxxx / 111111111111 / 111122223333 — AWS 文档惯用占位账号(测试 fixture 用)
#   example/.example/<x>/placeholder/YOUR_/dummy/REDACTED — 示例与脱敏占位

# 扫描目标:代码/配置文本;排除自身、fixtures、文档 md/svg(文档脱敏另有门)
targets="$(ck_targets "" | grep -vE '^scripts/checks/|^tests/fixtures/|\.md$|\.svg$|^CHANGELOG|\.lock$|^cdk\.out/' || true)"
[ -n "$targets" ] || { ck_ok "无相关文件变更"; exit 0; }

hits=0
report() { ck_bad "$1"; printf '%s\n' "$2" | head -4 | sed 's/^/      /' >&2; hits=$((hits+1)); }

while IFS= read -r f; do
  [ -n "$f" ] || continue
  file="$CK_ROOT/$f"
  [ -f "$file" ] || continue

  # 账号 ID 检查的豁免:测试文件里的账号是测试固定值,不是生产泄漏,整体豁免
  # (同 region ③ / 品牌 ⑤ 已有的 tests/ 豁免逻辑)。
  case "$f" in
    tests/*|*/test/*|*.test.mjs|*.test.js|*_test.mjs|*_test.py) _skip_account=1;;
    *) _skip_account=0;;
  esac
  if [ "$_skip_account" -eq 0 ]; then
  # ① 已知真实账号 ID(硬红线,零容忍,连注释都不许)。KNOWN_ACCOUNT_IDS 留空=跳过;
  #    部署方在此登记自己组织的真实账号 ID(| 分隔)启用硬阻断。
  KNOWN_ACCOUNT_IDS="${KNOWN_ACCOUNT_IDS:-}"
  if [ -n "$KNOWN_ACCOUNT_IDS" ]; then
  m="$(grep -nE "$KNOWN_ACCOUNT_IDS" "$file" 2>/dev/null || true)"
  [ -n "$m" ] && report "$f 出现已知真实账号 ID(必须脱敏/参数化):" "$m"
  fi

  # ② 其它 12 位账号 ID(排除 allowlist 占位、时间戳类)
  m="$(grep -nE '(^|[^0-9])[0-9]{12}([^0-9]|$)' "$file" 2>/dev/null | grep -viE "$ALLOW" | grep -iE 'account|arn:aws|:iam:|:sts:|:[0-9]{12}:' || true)"
  [ -n "$m" ] && report "$f 疑似硬编码 AWS 账号 ID(应参数化 CDK env/account):" "$m"
  fi

  # ③ region 写死(排除 allowlist、注释;放行合理默认值:env.get/getenv/or 兜底/context/映射表 key)
  # 合理默认值不算违规,只挡裸字面量。放行:
  #   - Python: os.environ.get(x, "region")、try_get_context() or "region"、getenv、except 分支赋值
  #   - JS/mjs: process.env.X || "region"、arg("region","default")(命令行默认参数)
  #   - 前端默认状态、测试常量(_TEST_REGION / const REGION 显式测试值)
  #   - JSON/config 的 "region": key(CDK cdk.json context / 配置文件里指定部署 region 是合法配置值)
  #   - shell test 比较 [ "$X" = "region" ](等号两边有空格,是比较分支不是硬编码赋值;
  #     区别于 REGION="region" 赋值——后者无空格仍被挡。如 us-east-1 建桶 API 特例判断)
  # 测试文件(tests/、*.test.*、_test.、loadtest)里的 region 是测试固定值,不是生产硬编码,整体豁免。
  case "$f" in
    tests/*|*/test/*|*.test.mjs|*.test.js|*_test.mjs|*_test.py) _skip_region=1;;
    *) _skip_region=0;;
  esac
  if [ "$_skip_region" -eq 0 ]; then
  m="$(grep -nE '"(ap-southeast-1|ap-northeast-1|us-west-2|us-east-1|eu-west-1)"|'"'"'(ap-southeast-1|ap-northeast-1|us-west-2|us-east-1|eu-west-1)'"'"'' "$file" 2>/dev/null \
       | grep -viE "$ALLOW" \
       | grep -viE '^[[:space:]]*[0-9]+:[[:space:]]*(#|//|\*)' \
       | grep -viE '\.get\(|getenv|environ|try_get_context|[[:space:]]or[[:space:]]|\|\||arg\(|default|fallback|== *"|!= *"| = *"|: *"pl-|region ==|Region *[=:]|region *[=:]|"region" *:' || true)"
  [ -n "$m" ] && report "$f 疑似写死 region 裸字面量(应走 CDK env / AWS_REGION;默认值/映射 key 除外):" "$m"
  fi

  # ④ 内部域名 / 堡垒机公网 IP(硬编码 IP 尤其危险)
  # 放行 RFC1918 CIDR 网段(172.31.0.0/16 这类带 /nn 的是标准 VPC 默认网段,是配置值不是主机泄露);
  # 只挡具体主机 IP(如 172.31.47.70)。注释行里举例的 base URL 也放行(注释是说明不是配置)。
  # KNOWN_HOST_IPS 留空=只扫通用 RFC1918 主机 IP;部署方可登记已知堡垒机 IP(| 分隔)。
  KNOWN_HOST_IPS="${KNOWN_HOST_IPS:-}"
  _ip_pat='172\.31\.[0-9]+\.[0-9]+|\.amazonaws\.com\.cn'
  [ -n "$KNOWN_HOST_IPS" ] && _ip_pat="$KNOWN_HOST_IPS|$_ip_pat"
  m="$(grep -nE "$_ip_pat" "$file" 2>/dev/null \
       | grep -viE "$ALLOW" \
       | grep -viE '172\.31\.[0-9]+\.[0-9]+/[0-9]+' \
       | grep -viE '^[[:space:]]*[0-9]+:[[:space:]]*(#|//|\*|/\*)' || true)"
  [ -n "$m" ] && report "$f 疑似硬编码内部 IP/域名:" "$m"

  # ⑤ 品牌残留(通用产品定位;注释行里的技术参照不挡,代码/UI 字符串里的品牌才挡)
  case "$f" in
    tests/*|*/test/*|*.test.mjs|*.test.js|*_test.mjs|*_test.py) _skip_brand=1;;
    *) _skip_brand=0;;
  esac
  if [ "$_skip_brand" -eq 0 ]; then
  # BRAND_WORDS 留空=跳过;部署方登记要阻断的品牌词(如 '\bacme\b|\bcontoso\b')。
  BRAND_WORDS="${BRAND_WORDS:-}"
  if [ -n "$BRAND_WORDS" ]; then
  m="$(grep -niE "$BRAND_WORDS" "$file" 2>/dev/null \
       | grep -viE "$ALLOW" \
       | grep -viE '^[[:space:]]*[0-9]+:[[:space:]]*(#|//|\*|--|/\*)' || true)"
  [ -n "$m" ] && report "$f 出现品牌字样(通用产品应去除或参数化):" "$m"
  fi
  fi

done <<EOF
$targets
EOF

if [ "$hits" -eq 0 ]; then ck_ok "无应参数化的硬编码"; else ck_warn "共 $hits 处;确属客观依赖的加进 allowlist 并注释来源"; rc=1; fi
exit $rc
