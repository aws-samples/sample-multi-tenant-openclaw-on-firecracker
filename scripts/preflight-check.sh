#!/usr/bin/env bash
# preflight-check.sh — ClawPool/OpenClaw-on-Firecracker 部署前配置冲突预检(只读)
#
# 用法: bash preflight-check.sh <config.yml> <region> [--profile <p>|-]
#   例:  bash preflight-check.sh config.yml ap-southeast-1 -
#
# 目的:客户写好 config.yml,部署前跑一次,提前列出"这次 cdk deploy/setup.sh 会撞什么"。
# 分级:🔴 BLOCK(会导致 CREATE_FAILED/ROLLBACK 或 synth 报错,必须先解决)
#       🟡 WARN (可能有问题/静默失效/需人工确认)
#       ✅ PASS
# 经验来源:多轮真机部署的坑清单 + 各 stack 的 fail-loud 校验点。
# 只读:全程只 describe/list/get + 解析 config,绝不建/删/改任何资源。
set -uo pipefail

CONFIG="${1:?用法: preflight-check.sh <config.yml> <region> [--profile <p>|-]}"
REGION="${2:?region required (如 ap-southeast-1)}"
PROFILE_ARG=""
[ "${3:-}" ] && [ "${3}" != "-" ] && PROFILE_ARG="--profile ${3}"
AWSQ="aws ${PROFILE_ARG} --region ${REGION} --output text"

[ -r "$CONFIG" ] || { echo "config 读不到: $CONFIG"; exit 2; }
command -v python3 >/dev/null || { echo "需要 python3 解析 YAML"; exit 2; }

BLOCK=0; WARN=0; PASS=0
_block(){ echo "🔴 BLOCK  $1"; BLOCK=$((BLOCK+1)); }
_warn(){  echo "🟡 WARN   $1"; WARN=$((WARN+1)); }
_pass(){  echo "✅ PASS   $1"; PASS=$((PASS+1)); }
_sec(){   echo; echo "── $1 ──"; }

# ── 用 python 把 config 解析成 shell 可读的 KEY=VAL(点分路径),存临时文件 ──
CFGDUMP=$(mktemp)
trap 'rm -f "$CFGDUMP"' EXIT
python3 - "$CONFIG" > "$CFGDUMP" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
def walk(p, o):
    if isinstance(o, dict):
        for k, v in o.items(): walk(f"{p}.{k}" if p else k, v)
    elif isinstance(o, list):
        print(f"{p}.__len__={len(o)}")
        for i, v in enumerate(o): walk(f"{p}.{i}", v)
    else:
        print(f"{p}={o}")
walk("", d)
PY
cfg(){ grep -m1 "^$1=" "$CFGDUMP" 2>/dev/null | cut -d= -f2-; }
cfglen(){ grep -m1 "^$1.__len__=" "$CFGDUMP" 2>/dev/null | cut -d= -f2-; }

ACCT=$(${AWSQ} sts get-caller-identity --query Account 2>/dev/null)
echo "========================================================"
echo " ClawPool 部署前预检 | account=$ACCT region=$REGION"
echo " config=$CONFIG"
echo "========================================================"
[ -z "$ACCT" ] && { echo "🔴 拿不到凭据(sts get-caller-identity 失败),先修 aws 凭据再跑"; exit 2; }

# 计算命名后缀(影响所有 S3/DDB/域名检查)—— region==ap-southeast-1 时 gsuffix="",否则 -<region>
GS=""; [ "$REGION" != "ap-southeast-1" ] && GS="-${REGION}"

# ========== Cat 7 · config 静态契约(fail-loud 点,不需 AWS 调用) ==========
_sec "config 静态契约(违反 → synth 直接报错)"

MODE=$(cfg network.mode)
API_MODE=$(cfg api.mode)
[ -n "$API_MODE" ] && { case "$API_MODE" in edge|private|both) _pass "api.mode=$API_MODE 合法";; *) _block "api.mode=$API_MODE 非法(只能 edge|private|both,network_vpc.py:46)";; esac; }

R_ENGINE=$(cfg redis.engine); R_VER=$(cfg redis.engine_version); R_ENABLED=$(cfg redis.enabled)
if [ "$R_ENABLED" = "True" ]; then
  case "$R_ENGINE" in redis|valkey) _pass "redis.engine=$R_ENGINE 合法";; *) _block "redis.engine=$R_ENGINE 非法(只能 redis|valkey,ha_edge.py:1040)";; esac
  [ "$R_ENGINE" = "valkey" ] && case "$R_VER" in 7.1*|"") _block "redis.engine=valkey 但 engine_version=$R_VER;valkey 无 7.1,必须 ≥7.2(ha_edge.py:1044)";; esac
  [ -n "$(cfg redis.instance_type)" ] && _warn "redis.instance_type 是栈不读的键(应为 node_type),会静默回落默认值(用户实撞)"
fi

MASTER=$(cfg logging.aos.master_nodes); LOG_EN=$(cfg logging.enabled)
[ "$LOG_EN" = "True" ] && case "$MASTER" in 0|3) _pass "logging.aos.master_nodes=$MASTER 合法";; *) _block "logging.aos.master_nodes=$MASTER 非法(只能 0 或 3,observability.py:196)";; esac

BFF_CIDR=$(cfg console_auth.bff_ingress_cidrs)
if [ -n "$BFF_CIDR" ]; then
  echo "$BFF_CIDR" | grep -q "0.0.0.0/0" && _block "console_auth.bff_ingress_cidrs 含 0.0.0.0/0(暴露红线,_bff_cidr.py 会 synth 报错)"
  echo "$BFF_CIDR" | tr ',' '\n' | grep -vqE '^[[:space:]]*([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}[[:space:]]*$' && _warn "bff_ingress_cidrs 疑似含非 IPv4-CIDR 项:$BFF_CIDR"
fi

EDGE_EN=$(cfg edge.enabled)
[ "$EDGE_EN" = "True" ] && [ "$R_ENABLED" != "True" ] && _block "edge.enabled=true 但 redis.enabled≠true(edge 靠 redis 查路由,ha_edge.py:1120)"

DISP_EN=$(cfg dispatch.enabled); CVQ=$(cfg scaler.create_via_queue)
[ "$DISP_EN" = "True" ] && [ "$CVQ" = "True" ] && _block "dispatch.enabled 与 scaler.create_via_queue 不能同为 true(双入队,stack.py synth raise)"

APIMODE_PRIV=0; { [ "$API_MODE" = "private" ] || [ "$API_MODE" = "both" ]; } && APIMODE_PRIV=1
CF_EN=$(cfg cloudfront.enabled)
[ "$APIMODE_PRIV" = 1 ] && [ "$CF_EN" = "True" ] && _warn "api.mode=$API_MODE(私有)但 cloudfront.enabled=true — 私有 ALB CloudFront 回源不通,通常应 false"

# ========== Cat 5 · imported 子网契约 ==========
_sec "imported 网络子网契约"
if [ "$MODE" = "imported" ]; then
  VPCID=$(cfg network.imported.vpc_id); ICIDR=$(cfg network.imported.cidr)
  NPUB=$(cfglen network.imported.public_subnet_ids); NPRIV=$(cfglen network.imported.private_subnet_ids); NDB=$(cfglen network.imported.database_subnet_ids)
  [ -z "$VPCID" ] && _block "imported 缺 vpc_id(_helpers.py:89)"
  [ -z "$ICIDR" ] && _block "imported 缺 cidr(必填,否则 CannotPerformOperationVpcCidr,_helpers.py:108)"
  [ "${NPUB:-0}" = 3 ] || _block "imported public_subnet_ids 必须恰好 3 个,当前 ${NPUB:-0}(_helpers.py:89)"
  [ "${NPRIV:-0}" = 3 ] || _block "imported private_subnet_ids 必须恰好 3 个,当前 ${NPRIV:-0}(_helpers.py:89)"
  [ -n "$NDB" ] && [ "$NDB" != 3 ] && _block "imported database_subnet_ids 要么空要么恰好 3,当前 $NDB(半配 fail-loud,_helpers.py:96)"
  # 实查:子网归属 + AZ
  if [ -n "$VPCID" ] && [ "${NPUB:-0}" = 3 ]; then
    ALLSUB=""
    for grp in public_subnet_ids private_subnet_ids database_subnet_ids; do
      n=$(cfglen network.imported.$grp); [ -z "$n" ] && continue
      i=0; while [ $i -lt "$n" ]; do ALLSUB="$ALLSUB $(cfg network.imported.$grp.$i)"; i=$((i+1)); done
    done
    for s in $ALLSUB; do
      svpc=$(${AWSQ} ec2 describe-subnets --subnet-ids "$s" --query 'Subnets[0].VpcId' 2>/dev/null)
      if [ -z "$svpc" ]; then _block "子网 $s 不存在(config 写错或已删)"
      elif [ "$svpc" != "$VPCID" ]; then _block "子网 $s 不属于 $VPCID(属于 $svpc)"; fi
    done
    [ -n "$ALLSUB" ] && _pass "imported 子网数量契约通过,已实查归属(请另核 AZ 按 a/b/c 顺序排 — CDK 靠 index 对应 AZ)"
  fi
else
  _pass "network.mode=$MODE(非 imported,跳过子网契约)"
fi

# ========== Cat 9a + 1 + 3 · 残骸撞名(ROLLBACK 空壳栈 / S3 / DDB) ==========
_sec "上一轮残骸(会挡 deploy / 撞 already exists)"
for st in OpenClawOrchestrator OpenClawImage; do
  s=$(${AWSQ} cloudformation describe-stacks --stack-name "$st" --query 'Stacks[0].StackStatus' 2>/dev/null)
  case "$s" in
    "" ) _pass "$st 不存在(干净)";;
    *ROLLBACK_COMPLETE|*ROLLBACK_FAILED|REVIEW_IN_PROGRESS|*FAILED ) _block "$st 处于 $s(空壳/失败态,挡下一轮 deploy,先 delete-stack)";;
    *IN_PROGRESS ) _warn "$st 处于 $s(有部署/删除正在进行,别叠着跑)";;
    *COMPLETE ) _warn "$st 已存在($s)— 若要全新部署需先 destroy,否则会 UPDATE 而非 CREATE";;
  esac
done
# S3 桶撞名
for b in "openclaw-assets-${ACCT}${GS}" "openclaw-backups-${ACCT}${GS}$(cfg s3.backup_bucket_suffix)" "openclaw-log-archive-${ACCT}${GS}"; do
  if ${AWSQ} s3api head-bucket --bucket "$b" 2>/dev/null; then
    lock=$(${AWSQ} s3api get-object-lock-configuration --bucket "$b" --query 'ObjectLockConfiguration.ObjectLockEnabled' 2>/dev/null)
    if [ "$lock" = "Enabled" ]; then _block "S3 桶 $b 已存在且 WORM 锁定(删不掉,需设 s3.backup_bucket_suffix 换名)"
    else _warn "S3 桶 $b 已存在(非 WORM,重建前需清/确认归属)"; fi
  fi
done
# DDB 表撞名(grep -c 可能返回多行计数,用 tr -d 挤成单数)
DDBLEFT=$(${AWSQ} dynamodb list-tables --query 'TableNames' 2>/dev/null | tr '\t' '\n' | grep -c '^openclaw-')
DDBLEFT=$(printf '%s' "${DDBLEFT:-0}" | tr -dc '0-9'); DDBLEFT=${DDBLEFT:-0}
if [ "$DDBLEFT" -gt 0 ]; then _block "存在 ${DDBLEFT} 张 openclaw-* DDB 表(RETAIN 残骸,全新部署会撞 already exists;先 delete-table)"; else _pass "无 openclaw-* DDB 表残骸"; fi

# ========== Cat 4 · VPCE 冲突(坑F/F++,用户点名)==========
_sec "Interface VPCE 冲突(private-dns 同服务一 VPC 只能一个)"
VPCID=$(cfg network.imported.vpc_id)
if [ -n "$VPCID" ]; then
  for svc in secretsmanager execute-api; do
    hit=$(${AWSQ} ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$VPCID" "Name=service-name,Values=com.amazonaws.${REGION}.${svc}" --query 'VpcEndpoints[?PrivateDnsEnabled==`true`].VpcEndpointId' 2>/dev/null)
    if [ -n "$hit" ]; then
      _block "VPC $VPCID 已有 private-dns $svc VPCE($hit)— 栈自建同服务 VPCE 会 CREATE_FAILED 回滚(坑F++)。删掉它让栈自建(SM 场景 SG 才对),或复用时确认其 SG 放行 443"
    else _pass "无冲突 $svc VPCE(栈可自建)"; fi
  done
else
  _warn "非 imported 或无 vpc_id,跳过 VPCE 冲突检查(self_managed 自建 VPC 通常无此问题)"
fi

# ========== Cat 6 · ACM 证书 ==========
_sec "ACM 证书(区域 + ISSUED)"
BFF_CERT=$(cfg console_auth.bff_certificate_arn)
if [ -n "$BFF_CERT" ]; then
  echo "$BFF_CERT" | grep -q ":${REGION}:" || _block "bff_certificate_arn 不在部署区域 $REGION(ALB 证书必须区域内):$BFF_CERT"
  st=$(${AWSQ} acm describe-certificate --certificate-arn "$BFF_CERT" --query 'Certificate.Status' 2>/dev/null)
  [ "$st" = "ISSUED" ] && _pass "bff 证书 ISSUED" || _block "bff 证书状态=$st(非 ISSUED / 不存在 / 跨账户)"
fi
for ck in cloudfront.console_cert_arn cloudfront.app_cert_arn cloudfront.acm_cert_arn; do
  arn=$(cfg $ck); [ -z "$arn" ] && continue
  echo "$arn" | grep -q ":us-east-1:" || _block "$ck 必须在 us-east-1(CloudFront 要求),当前:$arn"
done

# ========== Cat 8 · 容量/配额(WARN) ==========
_sec "容量/配额(提醒)"
HOST_IT=$(cfg host.instance_type); MINCAP=$(cfg asg.min_capacity)
if echo "$HOST_IT" | grep -q metal; then
  q=$(${AWSQ} service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A --query 'Quota.Value' 2>/dev/null)
  need=$(( ${MINCAP:-2} * 96 ))
  if [ -n "$q" ]; then
    qi=${q%.*}
    [ "${qi:-0}" -lt "$need" ] && _block "On-Demand Standard vCPU 配额=$qi < 需求≈$need(${MINCAP:-2}×r8g.metal-24xl 96vCPU),会撞配额墙,先提工单" || _pass "vCPU 配额 $qi ≥ 需求≈$need"
  fi
fi

# ========== Cat 9f · CloudFront prefix list context ==========
_sec "CloudFront origin-facing prefix list"
case "$REGION" in
  ap-southeast-1|us-east-1|us-west-2) _pass "region $REGION 有内置 prefix list(deploy 时仍建议带 -c cf_origin_facing_prefix_list=<pl>)";;
  *) _warn "region $REGION 无内置 prefix list,必须 deploy 时传 -c cf_origin_facing_prefix_list=<pl-id>,否则 CloudFront→ALB 回源被 SG 拒 /hub 504";;
esac

# ========== 汇总 ==========
echo
echo "========================================================"
echo " 结果: 🔴 BLOCK=$BLOCK   🟡 WARN=$WARN   ✅ PASS=$PASS"
if [ "$BLOCK" -gt 0 ]; then
  echo " ⛔ 有 $BLOCK 个 BLOCK 项,先解决再部署(否则大概率 CREATE_FAILED/回滚)"
  echo "========================================================"
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo " ⚠ 无 BLOCK,但有 $WARN 个 WARN,逐条确认后可部署"
  echo "========================================================"
  exit 0
else
  echo " ✅ 全部通过,可以部署"
  echo "========================================================"
  exit 0
fi
