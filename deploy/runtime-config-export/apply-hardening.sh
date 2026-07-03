#!/bin/bash
# 在新账号回放带外加固(Bedrock Guardrail + Route53 DNS Firewall),从本仓库 export 的
# JSON 确定性重建。这些加固在旧账号是带外建的,export 成 deploy/runtime-config-export/*.json
# 当真相源;本脚本让它们"随代码仓库迭代",符合"长期做法非临时补丁"。
#
# 禁账号间拷数据:脚本只读本仓库的 JSON,在目标账号用 CLI create,不从旧账号 API 拉。
#
# 用法: ./apply-hardening.sh <PROFILE> [REGION]
#   ./apply-hardening.sh <aws-profile> ap-southeast-1
#
# 产出:① Bedrock Guardrail(OWASP 5层)+ 版本,打印 guardrailId/version 供 LiteLLM/镜像引用
#       ② DNS Firewall domain list(block-c2)+ rule group,关联到 VPC(VPC id 自动发现)
set -euo pipefail
# PROFILE="-" 表示用环境/instance role(堡垒机场景),不加 --profile;否则用具名 profile(部署机)
PROFILE="${1:?Usage: ./apply-hardening.sh <PROFILE|-> [REGION]  (堡垒机用 - 表示 instance role)}"
REGION="${2:-ap-southeast-1}"
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$PROFILE" = "-" ]; then AWS="aws --region $REGION"; else AWS="aws --profile $PROFILE --region $REGION"; fi
echo "=== 目标账号 $($AWS sts get-caller-identity --query Account --output text) / $REGION ==="

# ---------- 1. Bedrock Guardrail(OWASP 5层)----------
echo "=== 1. Bedrock Guardrail 回放 ==="
GR_INPUT=/tmp/guardrail-create-input.json
python3 - "$DIR/bedrock-guardrail.json" "$GR_INPUT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
# dump(get-guardrail) → create-guardrail 入参:xxxPolicy → xxxPolicyConfig,
# 内层数组键加 Config 后缀,去掉只读/运行态字段。确定性转换,不手抄。
out = {
    "name": d["name"],
    "description": d.get("description", "")[:200],
    "blockedInputMessaging": d.get("blockedInputMessaging", "Blocked."),
    "blockedOutputsMessaging": d.get("blockedOutputsMessaging", "Blocked."),
}
# topicPolicy.topics[] → topicPolicyConfig.topicsConfig[]  (name/definition/examples/type)
tp = d.get("topicPolicy")
if tp and tp.get("topics"):
    out["topicPolicyConfig"] = {"topicsConfig": [
        {"name": t["name"], "definition": t["definition"],
         "examples": t.get("examples", []), "type": t.get("type", "DENY")}
        for t in tp["topics"]]}
# contentPolicy.filters[] → contentPolicyConfig.filtersConfig[]  (type/inputStrength/outputStrength)
cp = d.get("contentPolicy")
if cp and cp.get("filters"):
    out["contentPolicyConfig"] = {"filtersConfig": [
        {"type": f["type"], "inputStrength": f["inputStrength"], "outputStrength": f["outputStrength"]}
        for f in cp["filters"]]}
# wordPolicy → wordsConfig + managedWordListsConfig
wp = d.get("wordPolicy")
if wp:
    wcfg = {}
    if wp.get("words"): wcfg["wordsConfig"] = [{"text": w["text"]} for w in wp["words"]]
    if wp.get("managedWordLists"): wcfg["managedWordListsConfig"] = [{"type": m["type"]} for m in wp["managedWordLists"]]
    if wcfg: out["wordPolicyConfig"] = wcfg
# sensitiveInformationPolicy → piiEntitiesConfig + regexesConfig
sp = d.get("sensitiveInformationPolicy")
if sp:
    scfg = {}
    if sp.get("piiEntities"): scfg["piiEntitiesConfig"] = [{"type": p["type"], "action": p["action"]} for p in sp["piiEntities"]]
    if sp.get("regexes"): scfg["regexesConfig"] = [
        {"name": r["name"], "description": r.get("description", ""), "pattern": r["pattern"], "action": r["action"]}
        for r in sp["regexes"]]
    if scfg: out["sensitiveInformationPolicyConfig"] = scfg
# contextualGroundingPolicy → filtersConfig
gp = d.get("contextualGroundingPolicy")
if gp and gp.get("filters"):
    out["contextualGroundingPolicyConfig"] = {"filtersConfig": [
        {"type": f["type"], "threshold": f["threshold"]} for f in gp["filters"]]}
json.dump(out, open(sys.argv[2], "w"), ensure_ascii=False)
print(f"  转换完成:{len(out.get('topicPolicyConfig',{}).get('topicsConfig',[]))} topics, "
      f"{len(out.get('contentPolicyConfig',{}).get('filtersConfig',[]))} content filters, "
      f"{len(out.get('sensitiveInformationPolicyConfig',{}).get('piiEntitiesConfig',[]))} PII")
PYEOF

GR_NAME=$(python3 -c "import json;print(json.load(open('$DIR/bedrock-guardrail.json'))['name'])")
EXIST_ID=$($AWS bedrock list-guardrails --query "guardrails[?name=='$GR_NAME'].id|[0]" --output text 2>/dev/null || echo "None")
if [ "$EXIST_ID" != "None" ] && [ -n "$EXIST_ID" ]; then
  echo "  已存在 guardrail $GR_NAME ($EXIST_ID) — 跳过创建(幂等);如要更新用 update-guardrail"
  GID="$EXIST_ID"
else
  GID=$($AWS bedrock create-guardrail --cli-input-json "file://$GR_INPUT" --query "guardrailId" --output text)
  echo "  ✓ 创建 guardrail id=$GID"
fi
GVER=$($AWS bedrock create-guardrail-version --guardrail-identifier "$GID" --description "apply-hardening replay" --query "version" --output text 2>/dev/null || echo "DRAFT")
echo "  guardrailId=$GID version=$GVER  (LiteLLM/镜像引用此 id)"

# ---------- 2. DNS Firewall(block-c2 domain list + rule group)----------
echo "=== 2. Route53 Resolver DNS Firewall 回放 ==="
# 旧账号 export 的 dns-firewall.json 只留了 rule_group_id 与 block 动作引用(domain list
# 内容在旧账号 rslvr-fdl-...)。C2 域清单是安全敏感数据,本仓库不存明文域名。
# 这里建一个空壳 domain list + rule group + BLOCK 规则,域名清单由运营用
# import-firewall-domains 从受控来源灌(留 TODO 指针,不编造域名)。
# 命名对齐旧账号146(rule group=openclaw-egress-fw, domain list=openclaw-egress-blocklist),
# 跨账号迁移命名一致(两账号巡检对得上,避免漂移)。
DL_NAME="openclaw-egress-blocklist"
DL_ID=$($AWS route53resolver list-firewall-domain-lists --query "FirewallDomainLists[?Name=='$DL_NAME'].Id|[0]" --output text 2>/dev/null || echo "None")
if [ "$DL_ID" = "None" ] || [ -z "$DL_ID" ]; then
  DL_ID=$($AWS route53resolver create-firewall-domain-list --name "$DL_NAME" --query "FirewallDomainList.Id" --output text)
  echo "  ✓ 建 domain list $DL_NAME ($DL_ID)"
  # 灌默认 demo 黑名单(与旧账号146一致的演示域名,证明 DNS egress 拦截可演示)。
  # 这两个是 demo 占位非真实情报;真实 C2 清单运营另用 import-firewall-domains 从
  # 受控威胁情报源灌(仓库不存真实 C2 明文)。update-firewall-domains ADD 幂等。
  $AWS route53resolver update-firewall-domains --firewall-domain-list-id "$DL_ID" \
    --operation ADD --domains "evil-c2-demo.com" "exfil-test.net" >/dev/null 2>&1 \
    && echo "  ✓ 灌 demo 黑名单(evil-c2-demo.com/exfil-test.net);真实C2清单运营另灌" \
    || echo "  demo 黑名单灌入跳过"
else
  echo "  domain list 已存在 ($DL_ID)"
fi
RG_NAME="openclaw-egress-fw"
RG_ID=$($AWS route53resolver list-firewall-rule-groups --query "FirewallRuleGroups[?Name=='$RG_NAME'].Id|[0]" --output text 2>/dev/null || echo "None")
if [ "$RG_ID" = "None" ] || [ -z "$RG_ID" ]; then
  RG_ID=$($AWS route53resolver create-firewall-rule-group --name "$RG_NAME" --query "FirewallRuleGroup.Id" --output text)
  echo "  ✓ 建 rule group $RG_NAME ($RG_ID)"
else
  echo "  rule group 已存在 ($RG_ID)"
fi
# BLOCK 规则(priority 100,对齐旧账号 block-c2)。--action BLOCK 必须带 --block-response
# (NXDOMAIN/NODATA/OVERRIDE),否则 ValidationException RSLVR-02016。用 NXDOMAIN:
# 对 C2 域回"域不存在",最干净的阻断,guest 解析直接失败。
$AWS route53resolver create-firewall-rule --firewall-rule-group-id "$RG_ID" \
  --firewall-domain-list-id "$DL_ID" --priority 100 --action BLOCK --block-response NXDOMAIN --name "block-c2" >/dev/null 2>&1 \
  && echo "  ✓ BLOCK 规则 block-c2 (priority 100, NXDOMAIN)" || echo "  BLOCK 规则已存在"
# 关联到 VPC(自动发现 openclaw VPC)
VPC_ID=$($AWS ec2 describe-vpcs --filters "Name=tag:Name,Values=*openclaw*,*OpenClaw*" --query "Vpcs[0].VpcId" --output text 2>/dev/null || echo "None")
[ "$VPC_ID" = "None" ] && VPC_ID=$($AWS ec2 describe-vpcs --query "Vpcs[?IsDefault==\`false\`]|[0].VpcId" --output text 2>/dev/null || echo "None")
if [ "$VPC_ID" != "None" ] && [ -n "$VPC_ID" ]; then
  $AWS route53resolver associate-firewall-rule-group --firewall-rule-group-id "$RG_ID" \
    --vpc-id "$VPC_ID" --priority 101 --name "openclaw-assoc" >/dev/null 2>&1 \
    && echo "  ✓ 关联 rule group → VPC $VPC_ID" || echo "  关联已存在 (VPC $VPC_ID)"
else
  echo "  ⚠ 未找到 openclaw VPC,跳过关联;部署 CDK 后手动 associate-firewall-rule-group"
fi

echo ""
echo "=== 加固复刻完成 ==="
echo "  Guardrail: id=$GID version=$GVER (写进 LiteLLM config 的 guardrail 引用 + 镜像 openclaw.json)"
echo "  DNS FW:    rule_group=$RG_ID domain_list=$DL_ID → VPC $VPC_ID"
echo "  TODO(运营): C2 域名清单经受控来源 import-firewall-domains --firewall-domain-list-id $DL_ID --domains file://<受控清单>"
