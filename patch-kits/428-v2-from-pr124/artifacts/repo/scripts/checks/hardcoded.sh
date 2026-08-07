#!/usr/bin/env bash
# hardcoded.sh — 硬编码常量扫描(第三层)。借 IrisLint / EmkerrPythonHacks 正则思路本地重写,
# 不接任何外部依赖。扫 AI 高频写死、且违反本仓脱敏红线 + 通用产品定位的常量:
#   - AWS account ID(12 位数字;部署方可登记真实账号做硬阻断)
#   - ARN partition 写死(arn:aws: 在应逻辑参数化处)
#   - region 写死(ap-southeast-1 / us-west-2 等,应走参数/env)
#   - 内部域名 / 堡垒机公网 IP
#   - 品牌残留字样(部署方可登记要阻断的品牌词)
#
# allowlist:第三方公共端点、示例占位、文档等客观依赖豁免(下方 ALLOW_* 逐条注释来源)。
# 降级:纯 grep/sed,无外部依赖,任何有 bash 的机器都能跑,不降级。
#
# 用法:scripts/checks/hardcoded.sh [CK_SCAN_ALL=1]
# 退出:0=干净;1=发现应参数化的硬编码。
set -eu
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib.sh"

ck_hdr "hardcoded · 账号/ARN/region/域名/品牌 硬编码扫描"
rc=0

validate_optional_ere() {
  local name="$1" pattern="$2" probe_rc
  if [ -z "$pattern" ]; then
    return 0
  fi
  if [[ "" =~ $pattern ]]; then
    :
  else
    probe_rc=$?
    if [ "$probe_rc" -eq 2 ]; then
      echo "[hardcoded][ERR] $name is not a valid extended regular expression"
      exit 2
    fi
  fi
  return 0
}

validate_optional_ere KNOWN_ACCOUNT_IDS "${KNOWN_ACCOUNT_IDS:-}"
validate_optional_ere KNOWN_HOST_IPS "${KNOWN_HOST_IPS:-}"
validate_optional_ere BRAND_WORDS "${BRAND_WORDS:-}"

# ── allowlist(逐条注释豁免理由;命中这些的行不算硬编码)────────────
# 用 grep -E 的排除模式,能扩展
ALLOW='REDACTED|<[a-zA-Z_]+>|example|placeholder|EXCHANGE_API_BASE|EXCHANGE_TESTNET|\.example|dummy|YOUR_|xxxxxxxxxxxx|000000000000|123456789012|111111111111|111122223333'
# 说明:
#   公共行情端点应通过 EXCHANGE_API_BASE 配置，不在检查器里写死供应商域名
#   测试端点应通过 EXCHANGE_TESTNET 配置，不在检查器里写死供应商域名
#   第三方 SDK 包名属于客观依赖，应由对应样例自己的检查规则处理
#   EXCHANGE_API_BASE / EXCHANGE_TESTNET — 参数化第三方端点的 env 名,本身不是硬编码
#   123456789012 / 000000000000 / xxxxxxxxxxxx / 111111111111 / 111122223333 — AWS 文档惯用占位账号(测试 fixture 用)
#   example/.example/<x>/placeholder/YOUR_/dummy/REDACTED — 示例与脱敏占位

# 扫描目标:代码/配置文本;排除自身、fixtures、文档 md/svg(文档脱敏另有门)、engineering 知识库。
# 发布/补丁 skill 的规则实现、sanitize mapping 与 selftest 诱饵必须包含待拦截的
# 品牌、账号、IP 等字面量，扫描这些规则/fixtures 只会自命中。
# #401 —— opensource-publish 的 cli/ 整树是脱敏工具自身实现(guards.py 等把账号/资源 id 的
# 脱敏示例当文档/诱饵),扫它只会自命中;整树排除(原来只逐文件列了 selftest/mappings)。
RULE_DEFINITIONS='^\.claude/skills/(claw-patch-skill/scripts/open-pr\.sh|opensource-publish/(scripts/(gen-sync-manifest\.py|open-pr\.sh|redline-scan\.sh)|cli/))'
# patch/ 是【公开 gateway 独有】的已部署客户补丁包(bb 无此目录):补丁天生针对真实客户环境,
# 含该客户的真账号/instance-id/region 是其本质,不是泄漏,不该被本 sample 自带的 hardcoded 扫。
# 只在发布产物(gateway 侧)会遇到 patch/;bb 侧无此目录、该排除是 no-op。
targets="$(ck_targets "" | grep -vE '^scripts/checks/|^tests/fixtures/|^tests/checks-selftest\.sh$|\.md$|\.svg$|^engineering/|^CHANGELOG|\.lock$|^cdk\.out/|^patch/|^\.claude/worktrees/' | grep -vE "$RULE_DEFINITIONS" || true)"

# #401 —— 资源 id 类规则(⑥ 资源 id / ⑦ Guardrail id / ⑧ 裸账号简称 / ⑨ 真 instance id)【必须也
# 扫 docs/*.md】:63 处泄漏主战场在文档(pull-image-api.md 一处就 8 个真 instance id),而上面
# targets 为避免误伤大量现有文档刻意排除了 .md。故资源标识符类用独立目标集:保留 .md/docs,仍排除
# engineering 知识库(不发布)、规则定义自命中文件、fixtures、自身、CHANGELOG(历史另有发布门复扫)。
resid_targets="$(ck_targets "" | grep -vE '^scripts/checks/|^tests/fixtures/|^tests/checks-selftest\.sh$|\.svg$|^engineering/|^CHANGELOG|\.lock$|^cdk\.out/|^patch/|^\.claude/worktrees/' | grep -vE "$RULE_DEFINITIONS" || true)"

# 资源 id allowlist:只放行【整个 token】等于标准占位的情况。不能按子串放行 abc123/deadbeef,
# 否则 vpc-ffdeadbeef1234567 这类真实形状的 ID 会被误判为占位。
RESID_RESOURCE_ALLOW='^[0-9]+:(vpc|subnet|sg|ami|igw|nat|rtb|eni|vpce)-(0abc123def456|0abc123def4567890|0abc1234deadbeef0|0123456789abcdef0|1234567890abcdef|deadbeef|00000000)$'
RESID_INSTANCE_ALLOW='^[0-9]+:i-(0abc123def4567890|0123456789abcdef0|1234567890abcdef|0000000000000000)$'
RESID_TEXT_ALLOW='REPLACE_WITH|YOUR_|<[a-zA-Z_-]+>|123456789012|[0-9]{12}'

[ -n "$targets$resid_targets" ] || { ck_ok "无相关文件变更"; exit 0; }

hits=0
report() { ck_bad "$1"; printf '%s\n' "$2" | head -4 | sed 's/^/      /' >&2; hits=$((hits+1)); }

while IFS= read -r f; do
  [ -n "$f" ] || continue
  file="$CK_ROOT/$f"
  [ -f "$file" ] || continue

  # 账号 ID 检查的豁免:tests/ 是内部不发布文件(RELEASE-CHECKLIST 明确排除 tests/engineering/
  # CLAUDE*.md 等内部资产,发布用 export-ignore/干净分支剔除)。硬编码零容忍红线是防真账号泄进
  # 【公开发布物】;对内部开发测试文件里的真账号(如坐标默认值、测试元数据)扫描是误伤,整体豁免
  # (同 region ③ / 品牌 ⑤ 已有的 tests/ 豁免逻辑)。发布门在 RELEASE-CHECKLIST 复扫,不在这里。
  case "$f" in
    tests/*|*/test/*|*.test.mjs|*.test.js|*_test.mjs|*_test.py) _skip_account=1;;
    *) _skip_account=0;;
  esac
  if [ "$_skip_account" -eq 0 ]; then
  # ① 已知真实账号 ID(硬红线,零容忍,连注释都不许)
  m=""
  if [ -n "${KNOWN_ACCOUNT_IDS:-}" ]; then
    m="$(grep -nE "$KNOWN_ACCOUNT_IDS" "$file" 2>/dev/null || true)"
  fi
  [ -n "$m" ] && report "$f 出现已知真实账号 ID(必须脱敏/参数化):" "$m"

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
  host_pattern='172\.31\.[0-9]+\.[0-9]+|\.amazonaws\.com\.cn'
  [ -z "${KNOWN_HOST_IPS:-}" ] || host_pattern="$KNOWN_HOST_IPS|$host_pattern"
  m="$(grep -nE "$host_pattern" "$file" 2>/dev/null \
       | grep -viE "$ALLOW" \
       | grep -viE '172\.31\.[0-9]+\.[0-9]+/[0-9]+' \
       | grep -viE '^[[:space:]]*[0-9]+:[[:space:]]*(#|//|\*|/\*)' || true)"
  [ -n "$m" ] && report "$f 疑似硬编码内部 IP/域名:" "$m"

  # ⑤ 品牌残留(通用产品定位;第三方示例端点按配置处理)
  # 豁免:
  #   - 注释行里的品牌引用(#/// /* 开头)= 工程借鉴出处说明,
  #     是技术参照不是产品品牌植入;代码/UI 字符串里的品牌才挡。
  #   - 去品牌守卫测试本身可能包含旧品牌 token，测试目录整体豁免。
  #   - demo 测试账号属于测试 fixture，测试目录整体豁免。
  #   - 测试文件里的第三方端点断言是测试固定值，整体豁免。
  case "$f" in
    tests/test_config_key_consistency.py) _skip_brand=1;;
    tests/*|*/test/*|*.test.mjs|*.test.js|*_test.mjs|*_test.py) _skip_brand=1;;
    *) _skip_brand=0;;
  esac
  if [ "$_skip_brand" -eq 0 ]; then
  m=""
  if [ -n "${BRAND_WORDS:-}" ]; then
    m="$(grep -niE "$BRAND_WORDS" "$file" 2>/dev/null \
       | grep -viE "$ALLOW" \
       | grep -viE '^[[:space:]]*[0-9]+:[[:space:]]*(#|//|\*|--|/\*)' \
       || true)"
  fi
  [ -n "$m" ] && report "$f 出现竞品/客户品牌字样(通用产品应去除或参数化):" "$m"
  fi

done <<EOF
$targets
EOF

# ══ #401 —— 资源标识符规则(⑥-⑨),扫【含 docs/.md 的】resid_targets ══════════════
# 病根:hardcoded.sh 原本对 vpc-/subnet-/sg-/ami-/i- 前缀、裸账号简称、Guardrail id 覆盖 0 条,
# 63 处真实环境标识符两个月里被 4 人各自带进可发布路径 CI 一次没拦。这几条补词表缺口。
while IFS= read -r f; do
  [ -n "$f" ] || continue
  file="$CK_ROOT/$f"
  [ -f "$file" ] || continue

  # 测试文件豁免(同 ①③⑤):tests/ 是内部不发布资产,里面的真 id 是 fixture/回归元数据,
  # 发布门(RELEASE-CHECKLIST)复扫,这里扫是误伤。
  case "$f" in
    tests/*|*/test/*|*.test.mjs|*.test.js|*_test.mjs|*_test.py) continue;;
  esac

  # ⑥ AWS 资源 id 前缀写死(vpc/subnet/sg/ami/igw/nat/rtb/eni/vpce-),排除人造占位。
  # 【逐 token 提取再按 token 过滤占位】(codex #2):grep -oE 让每个 id 独占一行(输出 行号:token),
  # 之后的 allowlist 过滤只作用在【单个 id token】上——避免同一源码行里真 id 与占位 token 并存时,
  # 旧的整行 grep -v 把真 id 一起误放过(CI 绕过)。
  m="$(grep -noE '\b(vpc|subnet|sg|ami|igw|nat|rtb|eni|vpce)-[0-9a-f]{8,}\b' "$file" 2>/dev/null \
       | grep -vE "$RESID_RESOURCE_ALLOW" || true)"
  [ -n "$m" ] && report "$f 出现真实 AWS 资源 id(vpc-/subnet-/sg-/ami-… 应参数化,占位除外):" "$m"

  # ⑨ 真实 EC2 instance id(i- 后 16+ hex;占位 i-0abc…/i-0123… 已在 allowlist)。逐 token 提取过滤。
  m="$(grep -noE '\bi-[0-9a-f]{16,}\b' "$file" 2>/dev/null | grep -vE "$RESID_INSTANCE_ALLOW" || true)"
  [ -n "$m" ] && report "$f 出现真实 EC2 instance id(应换占位如 i-0abc123def4567890):" "$m"

  # ⑦ Bedrock Guardrail id:12 位 [a-z0-9] 且【字母数字混合】(真 id 形如 12 位小写字母+数字
  #    混合串,必含数字+字母;纯字母英文词 guardrails/credential/monitoring 不匹配 → 不误报)。
  #    占位/SSM 查找上下文放行。不要求同行含 "guardrail"(id 常单独出现在赋值/注释里)。
  m="$(grep -noE '\b[a-z0-9]{12}\b' "$file" 2>/dev/null \
       | grep -E ':[a-z0-9]*[0-9][a-z0-9]*$' \
       | grep -E ':[a-z0-9]*[a-z][a-z0-9]*$' \
       | grep -viE "$RESID_TEXT_ALLOW" || true)"
  # 仅当该 12 位混合串确与 guardrail 语境相关(同文件出现 guardrail 关键词)才报,避免撞其它 12 位 hash。
  if [ -n "$m" ] && grep -qiE 'guardrail' "$file" 2>/dev/null; then
    report "$f 疑似真实 Bedrock Guardrail id(12 位字母数字混合,应参数化/占位):" "$m"
  fi

  # ⑧ 裸账号前缀简称(795/146/421)仅在"实测/验证/账号/measured"上下文才算泄露
  #    (否则满仓无关数字如端口/计数会误报)。完整 12 位账号由上面 ① 已零容忍挡。
  m="$(grep -nE '(^|[^0-9])(795|146|421)([^0-9]|$)' "$file" 2>/dev/null \
       | grep -iE '实测|验证|账号|measured|account' \
       | grep -viE "$RESID_TEXT_ALLOW" || true)"
  [ -n "$m" ] && report "$f 出现裸账号前缀简称(795/146…实测记录,应中性化保留结论):" "$m"

done <<EOF
$resid_targets
EOF

if [ "$hits" -eq 0 ]; then ck_ok "无应参数化的硬编码"; else ck_warn "共 $hits 处;确属客观依赖的加进 allowlist 并注释来源"; rc=1; fi
exit $rc
