#!/bin/bash
# preflight-region.sh — 部署前一次性验完所有前置,全绿才 setup.sh。在 bastion 的 /opt/deploy 跑。
#
# 为什么(2026-07-18 美东1 实战):前置缺失一次撞一个、串行浪费三轮 setup.sh——
#   deploy1: network.mode 空 → 拦;deploy2: 缺 aws_cdk(venv 没建);deploy3: CDK 没 bootstrap。
# 一次全验,红的先修,别让 setup.sh 跑一半才发现。
#
# 用法(在 bastion /opt/deploy 目录): bash preflight-region.sh <region> [account]
# 退出码非 0 = 有前置没过,别 setup.sh。
set -uo pipefail
R="${1:?用法: preflight-region.sh <region> [account]}"
ACCT="${2:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}"
FAIL=0
ok(){ echo "  ✓ $1"; }
no(){ echo "  ✗ $1"; FAIL=1; }

echo "=== preflight $R (account=$ACCT) ==="

# 1. CDK bootstrap 过没(缺则 setup.sh 报 SSM /cdk-bootstrap/version not found)
if aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --region "$R" >/dev/null 2>&1; then
  ok "CDK 已 bootstrap"
else
  no "CDK 未 bootstrap → 先跑: PATH=.venv/bin:\$PATH cdk bootstrap aws://$ACCT/$R"
fi

# 2. venv + aws_cdk(缺则 ModuleNotFoundError: aws_cdk)
if [ -x .venv/bin/python ] && .venv/bin/python -c "import aws_cdk, aws_cdk.aws_bedrock_agentcore_alpha" 2>/dev/null; then
  ok "venv + aws-cdk-lib + agentcore-alpha 就绪"
else
  no "venv 缺 aws_cdk → python3.12 -m venv .venv && .venv/bin/pip install 'aws-cdk-lib>=2.251.0' 'aws-cdk.aws-bedrock-agentcore-alpha==2.251.0a0' constructs pyyaml"
fi

# 3. cdk / docker CLI(bundling 要 docker)
command -v cdk >/dev/null 2>&1 && ok "cdk CLI 在" || no "cdk CLI 缺 → npm i -g aws-cdk@2.1129.0"
docker info >/dev/null 2>&1 && ok "docker 可用" || no "docker 不可用 → systemctl enable --now docker"

# 4. config.yml 关键项
CFG=config.yml
[ -f "$CFG" ] || { no "config.yml 不存在(cp config.yml.example config.yml)"; echo "FAIL=$FAIL"; exit $FAIL; }
NM=$(grep -E '^\s+mode:' "$CFG" | head -1 | awk '{print $2}' | tr -d '"')
[ -n "$NM" ] && [ "$NM" != '""' ] && ok "network.mode=$NM(非空)" || no "network.mode 空 → 填 default_vpc|self_managed|imported"
grep -qE 'lifecycle_hook_timeout:\s*(2700|[3-9][0-9]{3})' "$CFG" && ok "lifecycle_hook_timeout≥2700(host 等得起镜像)" || no "lifecycle_hook_timeout<2700 → brand-new region host 可能等不到镜像超时,设 2700"
# imported 模式:校验没有别的账号/region 硬编码残留
if [ "$NM" = "imported" ]; then
  BAD=$(grep -cE 'arn:aws:(acm|elasticache):[a-z-]+:[0-9]{12}|ap-southeast-1|by-litellm' "$CFG" 2>/dev/null)
  [ "${BAD:-0}" = "0" ] && ok "imported config 无外部账号/region 硬编码残留" || no "imported config 有 $BAD 处外部 ARN/region/内网url 残留 → 全换本 region 自建或清空(见 skill 坑#10)"
fi

# 5. 残留(重部署撞 already exists 的源)。先看主栈状态分流:CREATE_COMPLETE = 已有健康部署,
#    桶/表是「在用」不是「残留」,别误报清它(2026-07-18 实测 preflight 对已部署环境跑会把在用资源
#    误报成残留)。只有全新(GONE)或坏态(ROLLBACK/REVIEW/FAILED)才把桶/表当残留检。
SS=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator --region "$R" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo GONE)
NB=$(aws s3api list-buckets --query "length(Buckets[?contains(Name,'openclaw-assets')])" --output text 2>/dev/null)
NT=$(aws dynamodb list-tables --region "$R" --query "length(TableNames[?contains(@,'openclaw')])" --output text 2>/dev/null)
if echo "$SS" | grep -qE "CREATE_COMPLETE|UPDATE_COMPLETE"; then
  echo "  ⚠ 主栈已 $SS —— 目标 region 已有健康部署(桶 $NB/表 $NT 是在用,非残留)。若要全新重建先 destroy;若只想跑基准直接测,别 force-clean。"
elif echo "$SS" | grep -qE "GONE|does not exist"; then
  ok "主栈名可用(无残壳),全新部署"
  [ "${NB:-0}" = "0" ] && ok "无 openclaw-assets 桶残留" || no "有 $NB 个 assets 桶残留 → force-clean-region.sh $R(versioning 桶要删所有版本;只删带 region 后缀桶)"
  [ "${NT:-0}" = "0" ] && ok "无 openclaw DDB 表残留" || no "有 $NT 张 openclaw 表残留 → force-clean-region.sh $R"
else
  no "主栈坏态 $SS(ROLLBACK/REVIEW/FAILED)→ delete-stack + force-clean-region.sh $R 清残留再重部署(桶 $NB/表 $NT)"
fi

# 6. 镜像触发幂等陷阱(2026-07-18 实撞):build_in_stack 的 CodeBuild 触发是 CustomResource,只在
#    image.version 变化时才 start-build(no-version-change redeploy = no-op)。重部署 / 换了新桶但
#    version 没 bump → 镜像不重烤 → 新桶 deployment/rootfs/ 空 → host 等不到 → ABANDON。
#    首次部署这里必空(正常,setup 会触发一次);但"删桶重部署且 version 不变"就是隐坑。
ACCT2="${ACCT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}"
IMGBKT="openclaw-assets-${ACCT2}-${R}"  # gsuffix=-<region>(ap-southeast-1 例外裸名,那时去掉后缀)
[ "$R" = "ap-southeast-1" ] && IMGBKT="openclaw-assets-${ACCT2}"
NIMG=$(aws s3 ls "s3://$IMGBKT/deployment/rootfs/" --region "$R" 2>/dev/null | grep -c rootfs)
if [ "${NIMG:-0}" -ge 1 ]; then ok "镜像已在 $IMGBKT/deployment/rootfs/(host 起来能拉到)"
else echo "  ⚠ 镜像不在 $IMGBKT/deployment/rootfs/ — 首次部署正常(setup 触发烤);但若是重部署/换桶且 image.version 没 bump,CustomResource 不会重烤 → 部署后立即手动 start-build openclaw-golden-image-builder-${R},否则 host ABANDON"; fi

echo ""
[ "$FAIL" = "0" ] && echo "=== PREFLIGHT PASS — 可 setup.sh $R - ===" || echo "=== PREFLIGHT FAIL — 先修上面 ✗ 项,别 setup.sh ==="
exit $FAIL
