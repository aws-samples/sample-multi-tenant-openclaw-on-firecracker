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
# 经验来源:两个真机部署环境的坑清单 + FAILURE-MODES + stack 各 fail-loud 点。
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

grep -q '^alb\.internal=' "$CFGDUMP" && _pass "alb.internal 已显式声明" \
  || _block "config 缺 alb.internal(#423 起必须显式写,缺键 synth 直接 raise,ha_edge.py:1241)"

# 启动 → 拉不到镜像 → lifecycle hook ABANDON → ASG 反复换机。栈已存在时是增量,不判。
# 主栈状态:必须区分「确实不存在」与「查不出来」。后者若被当成"不存在",会让下面的
# SSM/残骸判定把【在役】资源报成残留并建议删除 —— 那是不可恢复的破坏性误导。
ORCH_ERR=$(mktemp); ORCH_EXISTS=$(${AWSQ} cloudformation describe-stacks --stack-name OpenClawOrchestrator --query 'Stacks[0].StackStatus' 2>"$ORCH_ERR")
ORCH_RC=$?
ORCH_STATE=unknown
if [ "$ORCH_RC" -eq 0 ] && [ -n "$ORCH_EXISTS" ]; then ORCH_STATE=present
elif grep -qi 'does not exist\|ValidationError' "$ORCH_ERR"; then ORCH_STATE=absent
fi
if [ "$ORCH_STATE" = unknown ]; then
  _block "查不出主栈 OpenClawOrchestrator 的状态(describe-stacks 退出码 $ORCH_RC,非「不存在」错误:权限/限流/网络)— 首次与增量的判定全靠它,状态不明时下面的残留判定可能把【在役】资源报成残骸并建议删除。先修凭据/权限再跑。错误原文:$(tr '\n' ' ' < "$ORCH_ERR" | cut -c1-200)"
fi
rm -f "$ORCH_ERR"

# CDK 是直接下标 CFG["asg"]["min_capacity"](ha_edge.py:943)与
# CFG["asg"]["lifecycle_hook_timeout"](:1041)—— 缺键必 KeyError。门不能放它过去,
# 否则「预检全绿 → synth 崩」正是这道门要防的事。
MINCAP_RAW=$(cfg asg.min_capacity)
case "$MINCAP_RAW" in
  ''|*[!0-9]*) _block "asg.min_capacity 缺失或非整数(${MINCAP_RAW:-空})— CDK 直接下标 CFG[\"asg\"][\"min_capacity\"](ha_edge.py:943),缺键 synth 就 KeyError";;
  0) [ "$ORCH_STATE" = present ] \
       && _pass "asg.min_capacity=0(栈已存在=增量部署,本条不按首次判)" \
       || _pass "asg.min_capacity=0(首次部署正确:等镜像就绪再扩)";;
  *) [ "$ORCH_STATE" = present ] \
       && _warn "asg.min_capacity=${MINCAP_RAW} 且栈已存在(增量部署,不按首次判);全新部署前记得回 0" \
       || _block "首次部署 asg.min_capacity=${MINCAP_RAW}(必须 0)— 镜像烤制非阻塞,host 会抢跑拉不到镜像 → ABANDON → ASG 反复换机";;
esac

# lifecycle_hook_timeout:与 preflight-region.sh:42 的 >=2700 门对齐。imported+metal 实测 1200 不够。
HOST_IT_EARLY=$(cfg host.instance_type)
HOOK_TO=$(cfg asg.lifecycle_hook_timeout)
case "$HOOK_TO" in
  ''|*[!0-9]*) _block "asg.lifecycle_hook_timeout 缺失或非整数(${HOOK_TO:-空})— CDK 直接下标 CFG[\"asg\"][\"lifecycle_hook_timeout\"](ha_edge.py:1041),缺键 synth 就 KeyError;imported VPC + metal 用 3600";;
  *) if [ "$HOOK_TO" -ge 2700 ]; then
       _pass "asg.lifecycle_hook_timeout=$HOOK_TO ≥2700"
     # 只提醒不拦,免得把没证据的结论当硬门拦掉合法部署。
     elif [ "$MODE" = "imported" ] && echo "$HOST_IT_EARLY" | grep -q metal; then
       _block "asg.lifecycle_hook_timeout=$HOOK_TO <2700,且形态是 imported VPC + metal($HOST_IT_EARLY)— 这一组合实测 1200s 不够,会连续 ABANDON churn(preflight-region.sh:42 同一道门)"
     else
       _warn "asg.lifecycle_hook_timeout=$HOOK_TO <2700(形态:mode=$MODE / ${HOST_IT_EARLY:-未设})— 硬性证据只覆盖 imported+metal,这里不拦;但冷启慢的机型仍可能 ABANDON,建议抬到 2700 以上"
     fi;;
esac

# console_auth.user_pool_id 不能手填:auth.py 会走 legacy 分支建【不带账号后缀】的裸前缀
UPI=$(cfg console_auth.user_pool_id); CA_EN=$(cfg console_auth.enabled)
if [ -z "$UPI" ]; then
  :   # 未填 = 正常路径,由 setup.sh 自行判定
elif [ "$CA_EN" != "True" ]; then
  _warn "console_auth.user_pool_id 有值但 console_auth.enabled 非 true — auth.py:96 整段不生效(auth_cfg.get(\"enabled\")),这个键是死配置,建议删掉"
else
  # 说明填进去的是【本栈自己建的】池 → 走 legacy 分支建裸前缀域 → 全局撞名 → 整栈回滚。
  SELF_POOL=$(${AWSQ} cloudformation describe-stacks --stack-name OpenClawOrchestrator --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' 2>/dev/null)
  if [ -n "$SELF_POOL" ] && [ "$SELF_POOL" = "$UPI" ]; then
    _block "console_auth.user_pool_id=$UPI 正是本栈自己输出的 CognitoUserPoolId — 把自建池当成待导入的旧池,auth.py:117 走 legacy 分支建【裸前缀】Cognito 域,全局撞名 → 整栈 UPDATE_ROLLBACK(#479 B10 真机复现)。删掉这个键"
  else
    _warn "console_auth.user_pool_id=$UPI 是外部池(不等于本栈输出)— 这是 1.1.x→1.2.x 真升级路径,auth.py:117 支持导入。确认它确实是旧版遗留池;若只是想复用本栈的池,删掉这个键"
  fi
fi

# bff_alb_subnet_ids:ConsoleBffALB 硬编码 internet_facing=True(auth.py:629),没有配置开关。
BFF_N=$(cfglen console_auth.bff_alb_subnet_ids); PUB_N=$(cfglen network.imported.public_subnet_ids)
if [ -n "$BFF_N" ] && [ -n "$PUB_N" ]; then
  bffs=""; i=0; while [ "$i" -lt "$BFF_N" ]; do bffs="$bffs $(cfg console_auth.bff_alb_subnet_ids.$i)"; i=$((i+1)); done
  pubs=""; i=0; while [ "$i" -lt "$PUB_N" ]; do pubs="$pubs $(cfg network.imported.public_subnet_ids.$i)"; i=$((i+1)); done
  bff_bad=0
  for s in $bffs; do
    echo "$pubs" | tr ' ' '\n' | grep -qx "$s" && continue
    bff_bad=1
    _block "console_auth.bff_alb_subnet_ids 含 $s,它不在 public_subnet_ids 里 — ConsoleBffALB 是 internet-facing(auth.py:629),放非公有子网会建成功但公网不可达,控制台打不开"
  done
  [ "$bff_bad" = 0 ] && _pass "console_auth.bff_alb_subnet_ids 全在 public_subnet_ids 里"
fi

# 代码不读的键(写了静默不生效)。redis.instance_type 已在上面 redis 段单独提示,这里补其余 6 个。
# IDLE_RECLAIM_ENABLED=False 硬关闭且不读 env,所以两个缩容键都是死键,别互相指认。
for dk in edge.data_volume_gb edge.migration_drain_seconds alb.certificate_arn \
          console_auth.bff_in_vpc asg.scale_in_enabled scaler.idle_reclaim_enabled; do
  grep -q "^${dk}=" "$CFGDUMP" && _warn "$dk 是栈不读的键,写了静默不生效(deploy/ 下读取次数 0,#488);删掉避免误以为已生效"
done
grep -q '^redis\.existing_parameter_group_arn=\|^redis\.existing_subnet_group_arn=' "$CFGDUMP" \
  && _pass "redis.existing_*_group_arn 已配置 — 这两个键代码【确实读】(ha_edge.py:1422/1466,#281 复用现网),不是死键,别误删"

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
# 残骸 vs 在役:必须按主栈状态分流。主栈健康时这些表是【在役】的,报成"残骸,先 delete-table"
# 会诱导删掉租户数据(不可恢复)。preflight-region.sh:49-53 早就防住了同一类误报,这里补上。
if [ "$DDBLEFT" -eq 0 ]; then
  _pass "无 openclaw-* DDB 表残骸"
elif [ "$ORCH_STATE" = absent ]; then
  _block "存在 ${DDBLEFT} 张 openclaw-* DDB 表但主栈确认不存在(RETAIN 残骸,全新部署会撞 already exists;确认无用数据后再 delete-table)"
elif [ "$ORCH_STATE" = present ]; then
  _warn "存在 ${DDBLEFT} 张 openclaw-* DDB 表,主栈也在($ORCH_EXISTS)— 增量部署下这是【在役】数据表,不是残骸,不要删。要全新重建先 destroy,再按残留清单清"
else
  _warn "存在 ${DDBLEFT} 张 openclaw-* DDB 表,但主栈状态查不出来 — 无法判断在役还是残骸,【不要删】。先解决上面那条主栈状态 BLOCK"
fi

# ========== Cat 4 · VPCE 冲突(坑F/F++,用户点名)==========
_sec "Interface VPCE 冲突(private-dns 同服务一 VPC 只能一个)"
VPCID=$(cfg network.imported.vpc_id)
if [ -n "$VPCID" ]; then
  # 只有【栈这次真的会自建】同服务端点时,已有端点才构成冲突。
  #   · secretsmanager:由 logging.enabled + logging.aos.create_secretsmanager_vpce 决定。
  #     配置写明复用(false)时,已有端点正是想要的结果 —— 报 BLOCK 会让文档里的复用路径走不通,
  #   · execute-api:上游【没有】复用开关(lambdas.py 无条件自建),所以已有端点确实是冲突,
  #     除非它就是本套栈自己建的(按 CFN 标签判归属)。
  _SM_WANT=0
  if [ "$LOG_EN" = "True" ]; then
    _SM_CREATE=$(cfg logging.aos.create_secretsmanager_vpce)
    [ "$_SM_CREATE" = "False" ] || _SM_WANT=1     # 未显式设置时上游默认 true
  fi
  for svc in secretsmanager execute-api; do
    hit=$(${AWSQ} ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$VPCID" "Name=service-name,Values=com.amazonaws.${REGION}.${svc}" --query 'VpcEndpoints[?PrivateDnsEnabled==`true`].VpcEndpointId' 2>/dev/null)
    if [ -z "$hit" ]; then
      if [ "$svc" = secretsmanager ] && [ "$LOG_EN" = "True" ] && [ "$_SM_WANT" = 0 ]; then
        _block "logging.aos.create_secretsmanager_vpce=false(复用),但 VPC $VPCID 里没有开 private-dns 的 secretsmanager VPCE — AOS rolesmapping 取不到口令会卡住。改成 true 让栈自建"
      else
        _pass "无冲突 $svc VPCE(栈可自建)"
      fi
      continue
    fi
    if [ "$svc" = secretsmanager ] && [ "$_SM_WANT" = 0 ]; then
      _pass "已有 secretsmanager VPCE($hit),配置也是复用($([ "$LOG_EN" = "True" ] && echo create_secretsmanager_vpce=false || echo logging.enabled≠true))— 不冲突。仍需确认它的 SG 放行 VPC CIDR 的 443,否则整个 VPC 的 Secrets Manager 会静默不可用"
      continue
    fi
    owner=$(${AWSQ} ec2 describe-tags --filters "Name=resource-id,Values=$hit" "Name=key,Values=aws:cloudformation:stack-name" --query 'Tags[0].Value' 2>/dev/null)
    case "$owner" in
      ''|None)
        if [ "$svc" = execute-api ]; then
          _block "VPC $VPCID 已有 private-dns execute-api VPCE($hit)且不属于任何栈 — 上游无条件自建同服务端点(lambdas.py),会 CREATE_FAILED 回滚。三条出路:① 给它关 private DNS(先确认现有依赖)② 删掉让栈自建 ③ 改部署代码支持复用(见 #488 说明)。不要在没确认归属前删客户自有端点"
        else
          _block "VPC $VPCID 已有 private-dns secretsmanager VPCE($hit),而配置要求自建(create_secretsmanager_vpce=true)— 同服务双 private DNS 会整栈 ROLLBACK。改成 false 复用它"
        fi;;
      OpenClawOrchestrator)
        _warn "已有 private-dns $svc VPCE($hit)属于栈 $owner — 是这套部署自己建的,增量更新不冲突;销毁重建前确认它随栈删掉了";;
      *)
        # 别的 CFN 栈拥有它 —— 对本栈来说和客户自有端点一样是硬冲突,不能因为"有归属"就放行。
        _block "已有 private-dns $svc VPCE($hit)属于【另一个】栈 $owner — 本栈仍会自建同服务端点,private DNS 冲突 → CREATE_FAILED 回滚。先和 $owner 的所有者确认怎么处置(关它的 private DNS / 由本栈复用),不要直接删别人栈的资源";;
    esac
  done
else
  _warn "非 imported 或无 vpc_id,跳过 VPCE 冲突检查(self_managed 自建 VPC 通常无此问题)"
fi

# host.ssh_key_name 指向不存在的 EC2 密钥对 → LaunchTemplate 能建、ASG 起实例时才失败,
# 表现为「host 一台都起不来」而不是 CREATE_FAILED,极难往配置方向想(2026-08-13 第三账号真机撞)。
_sec "EC2 密钥对(host.ssh_key_name)"
KEYN=$(cfg host.ssh_key_name)
if [ -z "$KEYN" ]; then
  _pass "host.ssh_key_name 留空(生产姿态,host 走 SSM 不走 SSH)"
elif ${AWSQ} ec2 describe-key-pairs --key-names "$KEYN" --query 'KeyPairs[0].KeyName' >/dev/null 2>&1; then
  _pass "EC2 密钥对 $KEYN 存在"
else
  _block "host.ssh_key_name=$KEYN 在本区不存在 — ASG 起 host 时会失败,现象是「host 一台都起不来」而非 CREATE_FAILED。用现网已有的密钥对名,或留空走 SSM"
fi

# ========== Cat 4b · OpenSearch 服务关联角色(建 VPC 内域的账号级前置) ==========
_sec "OpenSearch 服务关联角色(logging.enabled 时必需)"
# AWS 文档(OpenSearch 开发者指南 · Using service-linked roles to create VPC domains):
# 建【VPC 内】的域需要 AWSServiceRoleForAmazonOpenSearchService(信任
# opensearchservice.amazonaws.com)。用【控制台】建域会自动创建它,通过 API/CloudFormation
# 【不会】—— 于是全新账号第一次开 logging 停在:
#   AWS::OpenSearchService::Domain CREATE_FAILED
#   Invalid request provided: Before you can proceed, you must enable a service-linked
#   role to give Amazon OpenSearch Service permissions to access your VPC.
# 账号里若已有 legacy 的 AWSServiceRoleForAmazonElasticsearchService,则继续用那个,也算过。
#
# 两个可用测试账号都已有该角色且都有依赖它的在役域,复现需删在役 SLR 会打断在役域。
# 本检查只做一次 iam get-role(只读),误报代价为零;判据若有偏差表现为"该 BLOCK 时没 BLOCK",
# 即回落到当前行为(部署到 LogDomain 才失败),不引入新失败模式。
if [ "$LOG_EN" = "True" ]; then
  SLR=""
  for role in AWSServiceRoleForAmazonOpenSearchService AWSServiceRoleForAmazonElasticsearchService; do
    if aws ${PROFILE_ARG} iam get-role --role-name "$role" --query 'Role.RoleName' --output text >/dev/null 2>&1; then
      SLR="$role"; break
    fi
  done
  if [ -n "$SLR" ]; then _pass "服务关联角色 $SLR 已存在(VPC 内域可建)"
  else
    _block "缺 OpenSearch 服务关联角色 — logging.enabled=true 会建 VPC 内的域,CloudFormation 不会自动创建该角色,必挂 LogDomain CREATE_FAILED。先跑一次(全账号一次性,幂等):aws iam create-service-linked-role --aws-service-name opensearchservice.amazonaws.com"
  fi
else
  _pass "logging.enabled 非 true,跳过 OpenSearch 服务关联角色检查"
fi

# ========== Cat 9b · /openclaw/* SSM 参数残留(重建撞名,与桶/表残骸不同源) ==========
_sec "SSM 参数残留(/openclaw/* 不随栈销毁)"
# /openclaw/litellm-host 在模板里是 CFN 资源,同时又被 userdata / setup.sh 带外写入,
# 所以它【不随栈销毁】。重建时会在 changeset 阶段就挂:
#   Early validation failed: ... AWS::SSM::Parameter ... already exists
# 先单独拿 aws 的退出码 —— 塞进管道会把"权限不足/限流/网络故障"变成空输出,
# grep -c 得 0,门就假绿放行了(门宁可红也不能假绿)。
SSMRAW=$(${AWSQ} ssm describe-parameters --parameter-filters "Key=Name,Option=BeginsWith,Values=/openclaw" --query 'Parameters[].Name' 2>/dev/null)
SSMRC=$?
if [ "$SSMRC" -ne 0 ]; then
  _block "查不到 /openclaw/* SSM 参数(describe-parameters 退出码 $SSMRC:权限/限流/网络)— 这条【没有检查】,不是通过。修好凭据或补 ssm:DescribeParameters 权限后重跑"
  SSMLEFT=0
else
  SSMLEFT=$(printf '%s' "$SSMRAW" | tr '\t' '\n' | grep -c '^/openclaw')
  SSMLEFT=$(printf '%s' "${SSMLEFT:-0}" | tr -dc '0-9'); SSMLEFT=${SSMLEFT:-0}
fi
if [ "$SSMRC" -ne 0 ]; then
  :   # 已在上面记 BLOCK,不再按残留数判
elif [ "$SSMLEFT" -gt 0 ]; then
  if [ "$ORCH_STATE" = absent ]; then
    _block "存在 ${SSMLEFT} 个 /openclaw/* SSM 参数但主栈确认不存在(带外残留,不随栈销毁)— 重建会在 changeset 阶段 Early validation failed: already exists,一个资源都建不出来。先【解密】备份再 aws ssm delete-parameters(#479 B2)"
  elif [ "$ORCH_STATE" = present ]; then
    _warn "存在 ${SSMLEFT} 个 /openclaw/* SSM 参数,主栈也在($ORCH_EXISTS)— 增量部署下正常(userdata/setup.sh 写的);但它们不随栈销毁,【销毁重建之前】必须先解密备份再删,否则重建撞 already exists"
  else
    # 主栈状态不明时绝不能说"带外残留,先删" —— 那可能是在役参数,删掉不可恢复。
    _warn "存在 ${SSMLEFT} 个 /openclaw/* SSM 参数,但主栈状态查不出来 — 无法判断它们是残留还是在役,【不要删】。先把上面那条主栈状态 BLOCK 解决掉再判"
  fi
else _pass "无 /openclaw/* SSM 参数残留"; fi

# ========== Cat 5b · 公有子网的 IGW 默认路由(WARN:证伪过不是 CREATE 失败) ==========
_sec "公有子网 IGW 默认路由(影响控制台可达性)"
# **建得出来**(Scheme=internet-facing、State=provisioning 都正常),所以这【不是】
# CREATE 失败,而是"建好了公网到不了"。因此只能 WARN —— 记 BLOCK 会拦掉合法部署。
if [ "$MODE" = "imported" ] && [ -n "${PUB_N:-}" ]; then
  noigw=""
  i=0; while [ "$i" -lt "$PUB_N" ]; do
    s=$(cfg network.imported.public_subnet_ids.$i); i=$((i+1)); [ -z "$s" ] && continue
    gw=$(${AWSQ} ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$s" --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'].GatewayId|[0]" 2>/dev/null)
    # igw-* = 真公有。其余(None/空=无显式关联或无默认路由、nat-*=私有出网)一律列出来让人核。
    case "$gw" in igw-*) ;; *) noigw="$noigw $s";; esac
  done
  if [ -n "$noigw" ]; then
    _warn "这些 public_subnet_ids 查不到指向 Internet Gateway 的默认路由:$noigw — internet-facing ALB 仍会建成功(真机验过),但公网不可达,控制台打不开。无显式路由表关联的子网走 VPC 主路由表,本条查不到,自己核一眼"
  else _pass "public_subnet_ids 均有 IGW 默认路由"; fi
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
  # 首次部署 min 必须是 0(等镜像),但步骤 7 一定会拉起至少 1 台 —— 用 max(min,1) 算需求,
  _need_hosts=${MINCAP:-2}; [ "${_need_hosts:-0}" -lt 1 ] && _need_hosts=1
  need=$(( _need_hosts * 96 ))
  if [ -n "$q" ]; then
    qi=${q%.*}
    [ "${qi:-0}" -lt "$need" ] && _block "On-Demand Standard vCPU 配额=$qi < 需求≈$need(${_need_hosts}×r8g.metal-24xl 96vCPU;min_capacity=${MINCAP:-未设} 时按至少 1 台算),会撞配额墙,先提工单" || _pass "vCPU 配额 $qi ≥ 需求≈$need(按 ${_need_hosts} 台算)"
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
