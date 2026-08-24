#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# build-golden-ami.sh — 构建 host golden AMI 的【唯一入口】(#537 US-1)。
#
# 三段,顺序固定,任一段 FAIL 都中止后续:
#   V-1  构建前的可追溯性门 —— git SHA、主流程未被改、hook 可用、hook 阶段仍在正确位置
#   ──   packer build(把 V-1 算出的坐标作为变量注入)
#   V-3  构建后的元数据完整性门 —— AMI tag 与 manifest 的八项齐全,缺一项即不可晋级
#
# 为什么需要一个 wrapper 而不是直接 `packer build`:
#   ① packer 没有 git 概念。「这个 AMI 对应哪个变更集」(US-6)只能由外部解析后注入,
#      而那个解析必须【在构建前】做完 —— 构建完再补 tag 就无法保证注入的是同一个树。
#   ② V-1 的四条静态断言(主流程未改、hook 语法、hook 执行位、hook 阶段行位)应该在起
#      构建实例【之前】拒绝,而不是烧掉 14 分钟和一台实例再说。
#   ③ 客户不该需要读 HCL、拼 packer 参数、知道 SSM 参数名(US-1 的可见结果)。
#
# ★ 本脚本在【客户执行路径】上,因此只用 bash + awscli —— 不引 python / jq / yq。
#   CUSTOMER-GUIDE §1.1 对客户承诺的前置工具只有三项(packer / awscli / 插件),多一个
#   解释器就多一条前置依赖,而客户只会在自己环境上构建失败时才发现。
#   开发侧脚本(assert-parity.sh、scripts/checks/*.py)不受这条约束。
#
# 用法:
#   bash deploy/packer/build-golden-ami.sh --env test --var-file "$PWD/deploy/packer/my.pkrvars.hcl"
#   bash deploy/packer/build-golden-ami.sh --env test --var-file ... --validate-only
#
# 退出码: 0=构建成功且元数据齐全, 1=某道门 FAIL(逐条打印), 2=用法错误

set -euo pipefail

# ── 摘要工具 ─────────────────────────────────────────────────────────────────
# macOS 默认只有 shasum,多数 Linux 只有 sha256sum。CUSTOMER-GUIDE §1.2 已有同款
# 二选一模式。写成函数而不是把命令存进变量:zsh(macOS 默认 shell)不对未加引号的
# 变量做分词,会把整串当成一个命令名并报 command not found。
if command -v sha256sum >/dev/null 2>&1; then
  _sha256_stdin() { sha256sum | cut -d' ' -f1; }
else
  _sha256_stdin() { shasum -a 256 | cut -d' ' -f1; }
fi
_sha256_of() { _sha256_stdin < "$1"; }

# ── 参数 ─────────────────────────────────────────────────────────────────────

ENV_NAME=""
VAR_FILE=""
VALIDATE_ONLY=0
EXTRA_ARGS=()

_usage() {
  cat >&2 <<'USAGE'
用法: build-golden-ami.sh --env <test|staging|prod> [--var-file <绝对路径>] [--validate-only]

  --env           必填。目标环境,写进 AMI 的 Env tag。切指针时会校验它与目标环境相符
                  —— 这是防止把测试环境的 AMI 切进生产的那道断言(#537 V-5)。
  --var-file      可选。pkrvars 文件的【绝对路径】。不给时下列值全自动发现:
                    region                 AWS_REGION / aws 配置
                    assets_bucket          openclaw-assets-<account><gsuffix>
                    iam_instance_profile   openclaw-packer-builder
                    vpc_id / subnet_id     有 igw 默认路由且 MapPublicIpOnLaunch 的公有子网
                  给了 var-file 时,里面显式写下的键优先于自动发现。
                  私有子网 + NAT 的部署必须给 var-file(见 CUSTOMER-GUIDE §2.1)。
  --validate-only 只跑 V-1 门与 packer validate,不起构建实例。

  arch 不是参数 —— host AMI 只出 arm64(四个 host 机型全是 Graviton)。
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --env)           ENV_NAME="${2:?--env 需要取值}"; shift 2 ;;
    --var-file)      VAR_FILE="${2:?--var-file 需要取值}"; shift 2 ;;
    --validate-only) VALIDATE_ONLY=1; shift ;;
    -h|--help)       _usage ;;
    *)               EXTRA_ARGS+=("$1"); shift ;;
  esac
done

[ -n "$ENV_NAME" ] || { echo "ERROR: 必须给 --env" >&2; _usage; }
case "$ENV_NAME" in
  test|staging|prod) ;;
  # 收窄成三个枚举值而不是接受任意字符串:Env tag 是 V-5/V-6 两道门的判据,一个笔误
  # ("prd")会让「Env 与目标环境相符」这条断言永远不匹配,或更糟 —— 匹配上一个谁也
  # 没定义的环境。
  *) echo "ERROR: --env 只能是 test / staging / prod,收到 '$ENV_NAME'" >&2; exit 2 ;;
esac

if [ -n "$VAR_FILE" ]; then
  [ -f "$VAR_FILE" ] || { echo "ERROR: var-file 不存在: $VAR_FILE" >&2; exit 2; }
  case "$VAR_FILE" in
    /*) ;;
    *) echo "ERROR: --var-file 必须是绝对路径(相对路径下 packer 的 file() 会二次拼接)" >&2; exit 2 ;;
  esac
fi

# 模板目录必须是绝对路径。相对路径下 `${path.root}/../userdata/…` 先被归一成
# deploy/userdata/…,又被前缀成 deploy/packer/deploy/userdata/… —— validate 与 build
# 都直接失败(README「使用方式」一节有实测的报错原文)。这里由脚本保证,客户不必记。
PACKER_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$PACKER_DIR/../.." && pwd)"
HOOKS_DIR="$PACKER_DIR/hooks"
MANIFEST="$PACKER_DIR/manifest.json"

fail=0
_bad() { echo "  FAIL  $*"; fail=1; }
_ok()  { echo "  ok    $*"; }

# 断言计数。#537 §4 通用要求:断言数必须 > 0 且被打印 —— 本仓已有两次「门跑了但一条
asserts=0
_count() { asserts=$((asserts + 1)); }

# ── 自动发现(#537 简化参数配置)────────────────────────────────────────────────
# US-1 的可见结果里写着「客户不需要读 HCL、不需要拼 packer build 的参数、不需要知道
# SSM 参数名」。凡是能从当前账号/部署里【查出来】的,就不该让人填 —— 每一个手填字段
# 都是一次填错的机会,而 assets_bucket 那个 <ACCOUNT_ID> 占位符原本要客户自己 sed。
#
# 覆盖优先级:--var-file 里显式写了的 > 这里自动发现的。packer 的 -var 优先级高于
# -var-file,所以必须先查 var-file 里有没有该键,有就不注入 —— 否则自动值会静默盖掉
# 客户明确写下的值,那比不自动发现更糟。
AUTO_VARS=()
_var_in_file() {
  [ -n "$VAR_FILE" ] || return 1
  # 键名在行首(允许前置空白),后面跟 = —— 不匹配注释行里的同名字符串。
  grep -qE "^[[:space:]]*$1[[:space:]]*=" "$VAR_FILE"
}
_auto() {  # _auto <key> <value> <how>
  if _var_in_file "$1"; then
    echo "  跳过  $1 —— var-file 里已显式指定"
  else
    AUTO_VARS+=(-var "$1=$2")
    echo "  自动  $1 = $2   ($3)"
  fi
}

echo "=============================================================="
echo " 自动发现"
echo "=============================================================="

# region:环境变量 → aws 配置。两个都没有时不猜 —— 猜错 region 的表现是"在一个空账号里
# 找不到桶和子网",排查方向完全跑偏。
_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
[ -n "$_REGION" ] || _REGION="$(aws configure get region 2>/dev/null || true)"
if _var_in_file region; then
  # var-file 里的值优先,但下面 V-3 回读 tag 还要用 region,所以得把它读出来。
  _REGION="$(grep -E '^[[:space:]]*region[[:space:]]*=' "$VAR_FILE" | head -1 | sed 's/.*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/')"
  echo "  跳过  region —— var-file 里已显式指定($_REGION)"
else
  [ -n "$_REGION" ] || {
    echo "ERROR: 解析不到 region。设 AWS_REGION 或跑 aws configure set region <r>。" >&2
    exit 2
  }
  AUTO_VARS+=(-var "region=$_REGION")
  echo "  自动  region = $_REGION   (环境变量 / aws 配置)"
fi

_ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
[ -n "$_ACCOUNT" ] && [ "$_ACCOUNT" != "None" ] || {
  echo "ERROR: 拿不到账号 id —— 凭据无效或已过期。先跑 aws sts get-caller-identity 自查。" >&2
  exit 2
}
echo "  账号  $_ACCOUNT"

# gsuffix 参与桶名,所以要先知道它。var-file 没写就是空(单环境)。
_GSUFFIX=""
if _var_in_file gsuffix; then
  _GSUFFIX="$(grep -E '^[[:space:]]*gsuffix[[:space:]]*=' "$VAR_FILE" | head -1 | sed 's/.*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/')"
fi

# assets_bucket:桶名规则是 openclaw-assets-<account><gsuffix>,由 OpenClawOrchestrator
# 栈创建 —— 既然规则确定、account 又查得到,就没有理由让客户手填一个带占位符的字符串。
_auto assets_bucket "openclaw-assets-${_ACCOUNT}${_GSUFFIX}" "按 openclaw-assets-<account><gsuffix> 规则"

# iam_instance_profile:CUSTOMER-GUIDE §2.2 让客户建的就是这个固定名,没有第二种取值。
_auto iam_instance_profile "openclaw-packer-builder" "CUSTOMER-GUIDE §2.2 的固定名"

# vpc / subnet:找一个【确实能出网】的公有子网 —— 有 0.0.0.0/0 → igw 的路由,且
# MapPublicIpOnLaunch=true。不用默认 VPC:很多企业账号没有默认 VPC,或默认 VPC 无
# NAT/IGW,那时 packer 会卡在等 SSH 直到超时(CUSTOMER-GUIDE §2.1 记录过这个症状)。
#
# 只在两者都没显式指定时才自动发现 —— 只自动一半会拼出「A VPC 的子网配 B VPC」这种
# 组合,packer 报的错离原因很远。
if _var_in_file subnet_id || _var_in_file vpc_id; then
  echo "  跳过  vpc_id / subnet_id —— var-file 里已指定(不做部分自动发现)"
else
  _pub_subnet=""
  _pub_vpc=""
  # 先取所有有 0.0.0.0/0 默认路由的路由表(网关 id + 关联子网),再在 bash 里判前缀。
  #
  # ★ 不要在 JMESPath 里写 `starts_with(GatewayId, 'igw-')`:NAT 路由的 GatewayId 是
  #   null,starts_with 收到 null 会【直接报错】并让整个查询返回空。而错误进的是
  #   stderr —— 一旦顺手 `2>/dev/null`,结果就是一个空集合,表现成"这个 region 没有
  #   公有子网",而真实原因是查询自己炸了。实测本函数第一版正是如此:手工能查到
  #   subnet-…(1a, IGW, MapPublicIpOnLaunch=True),自动发现却报找不到。
  #
  # 所以这里既不吞 stderr,也不用 `|| true` —— 查询失败必须让脚本停,不能被读成
  # "没有符合条件的子网"。
  _rt_json="$(aws ec2 describe-route-tables --region "$_REGION" \
    --query 'RouteTables[].[join(`,`, Routes[?DestinationCidrBlock==`0.0.0.0/0`].GatewayId || `[]`), join(`,`, Associations[].SubnetId || `[]`)]' \
    --output text)" || {
    echo "ERROR: 查路由表失败(上面是 aws 的原始报错)。这不是'没有子网',是查询本身没跑通。" >&2
    exit 2
  }

  # 每行两列:第一列是该表的默认路由网关(可能空/多个),第二列是关联子网(可能空/多个)。
  # 制表符分隔;逗号分隔多值。
  while IFS=$'\t' read -r _gws _subs; do
    case "$_gws" in
      *igw-*) ;;      # 有 IGW 默认路由才继续
      *) continue ;;
    esac
    [ -n "$_subs" ] && [ "$_subs" != "None" ] || continue
    for _s in ${_subs//,/ }; do
      # 两个条件都要:只有路由没有自动公网 IP 时,associate_public_ip=true 仍能拿到 IP,
      # 但只有 MapPublicIpOnLaunch 才说明这个子网【设计上】是公有的。
      _info="$(aws ec2 describe-subnets --region "$_REGION" --subnet-ids "$_s" \
        --query 'Subnets[0].[MapPublicIpOnLaunch,VpcId]' --output text)" || continue
      case "$_info" in
        True*) _pub_subnet="$_s"; _pub_vpc="$(printf '%s' "$_info" | awk '{print $2}')"; break 2 ;;
      esac
    done
  done <<< "$_rt_json"
  if [ -n "$_pub_subnet" ]; then
    AUTO_VARS+=(-var "vpc_id=$_pub_vpc" -var "subnet_id=$_pub_subnet")
    echo "  自动  vpc_id    = $_pub_vpc"
    echo "  自动  subnet_id = $_pub_subnet   (有 igw 默认路由 + MapPublicIpOnLaunch)"
  else
    echo "ERROR: 在 $_REGION 找不到能出网的公有子网(需要 0.0.0.0/0 → igw 且" >&2
    echo "       MapPublicIpOnLaunch=true)。私有子网 + NAT 的部署请显式给 --var-file," >&2
    echo "       并按 CUSTOMER-GUIDE §2.1 设 associate_public_ip=false 与 ssh_interface。" >&2
    exit 2
  fi
fi

echo
echo "=============================================================="
echo " V-1  构建前可追溯性门"
echo "=============================================================="

# ── V-1.1 git 坐标 ──────────────────────────────────────────────────────────
# 解析不到 SHA 就拒绝构建。没得商量:整条流水线的价值是「每个 AMI 都能查到它对应哪个
# 变更集」(US-6),而 tarball 部署无法提供那个坐标。CUSTOMER-GUIDE §1.7 已经要求
# git clone,这里把它变成会红的门。
_count
if ! GIT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)"; then
  _bad "解析不到 git commit —— $REPO_ROOT 不是 git 仓库。可追溯性是本流水线的前提:" \
       "请按 CUSTOMER-GUIDE §1.7 用 git clone 获取仓库,不要用 tarball/zip 解压。"
  echo; echo "V-1 门 FAIL(已跑 $asserts 条)——已在起任何实例之前中止。"
  exit 1
fi
_ok "git commit = $GIT_SHA"

# ── V-1.2 主流程文件未被改动(D5)─────────────────────────────────────────────
# D5:客户自定义只能往 hook 目录放脚本,主流程被改动时构建必须失败。
#
# 判据用 git 而不是单独维护一份摘要基线文件:git 本身就是「仓库基线」,而另立基线会
# 引入「改了主流程忘了更新基线」的漂移 —— 那种漂移的表现是门恒绿,正是最坏的一种。
#
# 与下面 V-1.3 的整树 dirty 是【两个不同严重级】:
#   主流程 dirty  → 拒绝构建(D5 的红线)
#   其余树 dirty  → 允许构建,但 SHA 带 -dirty 且禁止晋级(可以本地试,不能进生产)
MAINLINE=(
  "deploy/packer/host-golden.pkr.hcl"
  "deploy/packer/run-hooks.sh"
  "deploy/packer/assert-image.sh"
  "deploy/userdata/provision-host.sh"
  "deploy/edge/fluent-bit/install-fluent-bit.sh"
)
_count
_dirty_mainline=""
_untracked_mainline=""
for _f in "${MAINLINE[@]}"; do
  [ -f "$REPO_ROOT/$_f" ] || { _bad "主流程文件缺失: $_f"; continue; }
  # 先确认它【在 git 索引里】。这一步不能省:`git diff --quiet HEAD -- <path>` 对
  # untracked 文件返回 0(diff 不知道有这个文件),于是"主流程文件不在 git 里"会被读成
  # "与 HEAD 一致" —— 而那正是绕过 D5 最省事的办法(删掉再新建同名文件)。
  # 实测:本门第一版就漏掉了自己新增的 run-hooks.sh。
  if ! git -C "$REPO_ROOT" ls-files --error-unmatch -- "$_f" >/dev/null 2>&1; then
    _untracked_mainline="$_untracked_mainline $_f"
  elif ! git -C "$REPO_ROOT" diff --quiet HEAD -- "$_f" 2>/dev/null; then
    _dirty_mainline="$_dirty_mainline $_f"
  fi
done
if [ -n "$_untracked_mainline" ]; then
  _bad "主流程文件不在 git 索引里,拒绝构建(#537 D5):$_untracked_mainline"
  echo "        未被 git 跟踪的主流程文件无法与仓库基线比对 —— 那等于没有基线。"
fi
if [ -n "$_dirty_mainline" ]; then
  _bad "主流程文件被改动,拒绝构建(#537 D5):$_dirty_mainline"
  echo "        自定义内容请放 deploy/packer/hooks/(见 hooks/README.md)。"
  echo "        若你是本仓开发者、这些改动是有意的:先提交它们,门比对的是 git HEAD。"
fi
if [ -z "$_untracked_mainline" ] && [ -z "$_dirty_mainline" ]; then
  _ok "主流程 ${#MAINLINE[@]} 个文件均被 git 跟踪且与 HEAD 一致"
fi

# ── V-1.3 工作树整体是否 dirty ──────────────────────────────────────────────
# dirty 时 SHA 带 -dirty 后缀并把 AMI 标成不可晋级。为什么不直接拒绝:开发者在本地试
# 构建是正常动作,拒绝会把工具变成障碍。但那个 AMI 绝不能进生产 —— 它对应的「变更集」
# 在 git 里不存在,V-6 的同源比对无从进行。
#
# untracked 也算 dirty:客户往 hooks/ 之外的地方丢一个未跟踪的脚本,同样让「这个 AMI
# 对应 git 的哪个树」不成立。hooks/*.sh 本身在 .gitignore 里,--exclude-standard 会
# 忽略它们,所以放 hook 不会把 AMI 变成不可晋级 —— 那正是 hook 目录存在的意义。
_count
if git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null && \
   [ -z "$(git -C "$REPO_ROOT" ls-files --others --exclude-standard 2>/dev/null | head -1)" ]; then
  GIT_DESC="$GIT_SHA"
  PROMOTABLE="true"
  _ok "工作树干净 —— 该 AMI 可晋级"
else
  GIT_DESC="${GIT_SHA}-dirty"
  PROMOTABLE="false"
  _ok "工作树 dirty —— GitCommit 记作 ${GIT_DESC},Promotable=false(可本地验证,不可晋级)"
fi

# ── V-1.4 hook 目录可用 ─────────────────────────────────────────────────────
# 每个 hook 都要:有执行位、bash -n 通过。两条都在【起构建实例之前】查完 —— 执行位丢了
# 或语法错了,在构建中途才炸的话已经烧掉一台实例和 20 分钟 provision。
#
# 用进程替换 `< <(find …)` 而不是管道 `find … | while`:管道会把 while 放进子 shell,
# 里面对 fail / _hook_count 的赋值出不来,于是"发现了问题但门照样绿"。
_count
_hook_names=""
_hook_lines=""
_hook_count=0
if [ -d "$HOOKS_DIR" ]; then
  # 与 run-hooks.sh 用【同一个】枚举方式(find -maxdepth 1 -type f -name '*.sh' | sort)。
  # 两处不一致会让门检查的集合与实际执行的集合不同 —— 那是最难发现的一类漂移,所以
  # HooksSha 由本脚本算、hooks-manifest 由 run-hooks.sh 在构建实例上算,V-3 再对账。
  while IFS= read -r _h; do
    [ -n "$_h" ] || continue
    _hook_count=$((_hook_count + 1))
    _n="$(basename "$_h")"
    _hook_names="$_hook_names $_n"
    _hook_lines="${_hook_lines}$(_sha256_of "$_h")  ${_n}"$'\n'
    [ -x "$_h" ] || _bad "hook 缺执行位: $_n(跑 chmod +x)"
    bash -n "$_h" 2>/dev/null || _bad "hook 语法错误: $_n(bash -n 不过)"
  done < <(find "$HOOKS_DIR" -maxdepth 1 -type f -name '*.sh' -print 2>/dev/null | sort)
fi
if [ "$_hook_count" -eq 0 ]; then
  _ok "hook 目录为空 —— 自定义阶段将 no-op"
else
  _ok "$_hook_count 个 hook,字典序:$_hook_names"
fi

# hook 集合摘要。逐行 "sha256  basename" 再对整串取一次摘要 —— 用 basename 而不是全
# 路径:全路径含客户的本地目录名,同一批 hook 在不同机器上会算出不同的 HooksSha,而
# V-6 正要跨环境比对它。改名也会让摘要变(执行顺序会变),这是想要的。
#
# 空目录得到的是空串的 sha256(一个固定值),不是缺字段 —— V-6 要比 HooksSha 相等,
# 缺字段无法参与比较。
_count
HOOKS_SHA="$(printf '%s' "$_hook_lines" | _sha256_stdin)"
_ok "HooksSha = $HOOKS_SHA"

# ── V-1.5 hook 阶段仍在正确位置 ─────────────────────────────────────────────
# ADR-packer-host-golden-ami §6 记录的失效模式:hook 阶段被挪到断言之后,泄漏防线静默
# 失效而构建仍然绿。这条门按行号复核顺序,和 assert-parity.sh 第 9 项同一形态。
#
# 用 grep -n 而不是解析 HCL:客户执行路径不能引 python。取【最后一次】出现而不是第一次
# —— 变量声明与注释都在文件前部,拿它们比行号会让门永远绿(assert-parity.sh 那条注释
# 记录了第一版正是如此)。
_count
_hcl="$PACKER_DIR/host-golden.pkr.hcl"
_ln_provision="$(grep -n 'sudo -E bash ${local.provision_dst}' "$_hcl" | head -1 | cut -d: -f1 || true)"
_ln_hooks="$(grep -n 'bash ${local.hooks_runner_dst}' "$_hcl" | tail -1 | cut -d: -f1 || true)"
_ln_assert="$(grep -n 'bash ${local.assert_dst} post-provision' "$_hcl" | tail -1 | cut -d: -f1 || true)"
if [ -z "$_ln_provision" ] || [ -z "$_ln_hooks" ] || [ -z "$_ln_assert" ]; then
  _bad "在 HCL 里定位不到 provision / hooks / assert 三个阶段之一" \
       "(provision=${_ln_provision:-未找到} hooks=${_ln_hooks:-未找到} assert=${_ln_assert:-未找到})"
elif [ "$_ln_provision" -lt "$_ln_hooks" ] && [ "$_ln_hooks" -lt "$_ln_assert" ]; then
  _ok "hook 阶段位置正确:provision(:$_ln_provision) < hooks(:$_ln_hooks) < assert(:$_ln_assert)"
else
  _bad "hook 阶段位置错误 —— 必须在 provision 之后、断言之前。" \
       "实测 provision=:$_ln_provision hooks=:$_ln_hooks assert=:$_ln_assert。" \
       "放到断言之后会让 hook 引入的身份泄漏无人检出,而构建仍显示成功(ADR §6)。"
fi

# ── V-1.6 模板与 provision 的摘要 ───────────────────────────────────────────
# PackerTemplateSha:模板变了但 provision-host.sh 没变时,provision_sha256 察觉不到 ——
# 而模板决定了「哪些步骤、什么顺序、注入什么环境变量」,那同样改变产出。V-6 要靠它发现
# 「上一级验的模板和这一级用的模板不是同一份」。
_count
PACKER_TEMPLATE_SHA="$(_sha256_of "$_hcl")"
PROVISION_SHA="$(_sha256_of "$REPO_ROOT/deploy/userdata/provision-host.sh")"
_ok "PackerTemplateSha = $PACKER_TEMPLATE_SHA"
_ok "ProvisionSha256   = $PROVISION_SHA"

echo
if [ "$fail" != 0 ]; then
  echo "V-1 门 FAIL(已跑 $asserts 条)——已在起任何实例之前中止。"
  exit 1
fi
echo "V-1 门 PASS($asserts 条断言全过)"

# ── packer ───────────────────────────────────────────────────────────────────

# BuiltAt 由本脚本生成而不是用 packer 的 {{isotime}}:V-3 要回读 tag 并与 manifest
# 比对,两边各自取一次时间会差几分钟(AMI 转 available 要 11 分钟),那条比对就没法做
# 成相等断言。一个来源,两处引用。
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

PACKER_VARS=(
  -var "git_commit=$GIT_DESC"
  -var "packer_template_sha=$PACKER_TEMPLATE_SHA"
  -var "hooks_sha=$HOOKS_SHA"
  -var "env=$ENV_NAME"
  -var "built_at=$BUILT_AT"
  -var "promotable=$PROMOTABLE"
)
PACKER_VARS+=("${AUTO_VARS[@]+"${AUTO_VARS[@]}"}")

# --var-file 是可选的:自动发现覆盖了 region / assets_bucket / iam_instance_profile /
# vpc / subnet,其余变量都有默认值。给了就带上(它的值优先于自动发现,见 _auto)。
VAR_FILE_ARG=()
[ -n "$VAR_FILE" ] && VAR_FILE_ARG=(-var-file="$VAR_FILE")

echo
echo "=============================================================="
echo " packer init / validate"
echo "=============================================================="
packer init "$PACKER_DIR"
packer validate "${VAR_FILE_ARG[@]+"${VAR_FILE_ARG[@]}"}" "${PACKER_VARS[@]}" "$PACKER_DIR"
echo "packer validate PASS"

if [ "$VALIDATE_ONLY" = 1 ]; then
  echo
  echo "--validate-only:到此为止,未起构建实例。"
  exit 0
fi

echo
echo "=============================================================="
echo " packer build(实测约 14 分钟,其中 11 分钟是 EBS 快照)"
echo "=============================================================="
# 旧 manifest 会让 V-3 读到上一次构建的 AMI id 并误判成本次产物。packer 的 manifest
# post-processor 默认【追加】而不是覆盖,所以必须显式清掉 —— 不清的话"本次构建失败但
# 上次成功"这种情况下,V-3 会拿着上次的 AMI 报 PASS。
rm -f "$MANIFEST"
packer build "${VAR_FILE_ARG[@]+"${VAR_FILE_ARG[@]}"}" "${PACKER_VARS[@]}" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" "$PACKER_DIR"

# ── V-3 元数据完整性门 ───────────────────────────────────────────────────────

echo
echo "=============================================================="
echo " V-3  元数据完整性门"
echo "=============================================================="

_count
[ -s "$MANIFEST" ] || {
  _bad "manifest 缺失或为空: $MANIFEST —— packer build 报成功但没落 manifest,无从核对"
  echo; echo "V-3 门 FAIL"; exit 1
}
_ok "manifest 存在"

# 取 AMI id。grep+sed 而不是 jq/python(客户执行路径约束)。tail -1 取最后一条 build:
# 同一个 manifest 可能累积多次构建记录(上面已 rm,这里仍取最后一条以防 packer 行为变化)。
_count
AMI_ID="$(grep -o '"artifact_id": *"[^"]*"' "$MANIFEST" | tail -1 | sed 's/.*:\(ami-[0-9a-f]*\)".*/\1/')"
case "$AMI_ID" in
  ami-*) _ok "AMI = $AMI_ID" ;;
  *) _bad "从 manifest 取不到 AMI id(得到 '$AMI_ID')"; echo; echo "V-3 门 FAIL"; exit 1 ;;
esac

# region 从 manifest 的 artifact_id 前半段取("<region>:<ami>")。不从 pkrvars 里 grep:
# 那是【输入】,而这里要断言的是【产物落在哪】—— 两者不一致时(比如 AWS_REGION 环境变量
# 覆盖了 var)我们要看到产物那一边。
AMI_REGION="$(grep -o '"artifact_id": *"[^"]*"' "$MANIFEST" | tail -1 | sed 's/.*"\([a-z0-9-]*\):ami-.*/\1/')"

# ── V-3 的八项。缺任一项 → 该 AMI 不可被晋级、不可被切指针(US-4 与 US-6 的物理前提)。
REQUIRED_TAGS=(GitCommit PackerTemplateSha ProvisionSha256 RecipeVersion Arch Env BuiltAt HooksSha)

_tags_out="$(aws ec2 describe-images --region "$AMI_REGION" --image-ids "$AMI_ID" \
  --query 'Images[0].Tags[].[Key,Value]' --output text 2>&1)" || {
  _bad "回读 AMI tag 失败: $_tags_out"; echo; echo "V-3 门 FAIL"; exit 1
}

for _k in "${REQUIRED_TAGS[@]}"; do
  _count
  # 制表符分隔的 "Key<TAB>Value"。用 awk 精确匹配第一列,不用 grep 子串 —— "Arch" 会
  # `"PollerHeartbeat" in "PollerHeartbeatV2"` 这个子串陷阱吃过一次假绿。
  _v="$(printf '%s\n' "$_tags_out" | awk -F'\t' -v k="$_k" '$1==k {print $2; found=1} END{if(!found) exit 1}')" || {
    _bad "AMI 缺 tag: $_k"
    continue
  }
  [ -n "$_v" ] || { _bad "AMI 的 tag $_k 为空值"; continue; }
  _ok "tag $_k = $_v"
done

# 逐项与本次构建实际注入的值比对。只查"tag 在不在"不够 —— tag 存在但写的是上一次
# 构建的 SHA,V-6 的同源比对就会拿错值去比,而那正是这条门要防的。
_assert_tag_equals() {
  _count
  local k="$1" expect="$2" got
  got="$(printf '%s\n' "$_tags_out" | awk -F'\t' -v k="$k" '$1==k {print $2}')"
  if [ "$got" = "$expect" ]; then
    _ok "tag $k 与本次构建注入值一致"
  else
    _bad "tag $k 不匹配:AMI 上是 '$got',本次注入的是 '$expect'"
  fi
}
_assert_tag_equals GitCommit        "$GIT_DESC"
_assert_tag_equals PackerTemplateSha "$PACKER_TEMPLATE_SHA"
_assert_tag_equals ProvisionSha256  "$PROVISION_SHA"
_assert_tag_equals Env              "$ENV_NAME"
_assert_tag_equals BuiltAt          "$BUILT_AT"
_assert_tag_equals HooksSha         "$HOOKS_SHA"

# manifest 侧同样八项 —— tag 在 EC2 上、manifest 在本地,晋级门读的是 manifest(它能
# 归档、能跨环境传),切指针门读的是 tag(它跟着镜像走)。两边都齐才算完整,少一边就有
# 一条链路读不到坐标。
for _k in git_commit packer_template_sha provision_sha256 recipe_version arch env built_at hooks_sha promotable; do
  _count
  grep -q "\"$_k\":" "$MANIFEST" || _bad "manifest 缺字段: $_k"
done
_ok "manifest 九个 custom_data 字段已查"

# hooks 对账:构建侧算的 HooksSha 必须与构建实例上 run-hooks.sh 实际执行的那批一致。
# 两侧独立计算才能发现「上传的和执行的不是同一批」—— 单侧计算的话,file provisioner
# 少传一个文件这种事完全看不出来。
_count
_hooks_in_manifest="$(grep -o '"hooks_count": *"[^"]*"' "$MANIFEST" | tail -1 | sed 's/.*: *"\(.*\)"/\1/' || true)"
if [ -z "$_hooks_in_manifest" ]; then
  _bad "manifest 里没有 hooks_count —— 无法与构建侧枚举的 $_hook_count 个 hook 对账"
elif [ "$_hooks_in_manifest" = "$_hook_count" ]; then
  _ok "hook 数量对账一致($_hook_count 个)"
else
  _bad "hook 数量不一致:构建侧枚举 $_hook_count 个,镜像里实际执行 $_hooks_in_manifest 个"
fi

echo
if [ "$fail" != 0 ]; then
  echo "V-3 门 FAIL(已跑 $asserts 条断言)—— AMI $AMI_ID 已产出但【不可晋级、不可切指针】。"
  exit 1
fi

echo "V-3 门 PASS($asserts 条断言全过)"
echo
echo "=============================================================="
echo " 构建成功"
echo "=============================================================="
echo "  AMI          $AMI_ID  ($AMI_REGION)"
echo "  Env          $ENV_NAME"
echo "  GitCommit    $GIT_DESC"
echo "  HooksSha     $HOOKS_SHA  ($_hook_count 个 hook)"
echo "  Promotable   $PROMOTABLE"
echo "  manifest     $MANIFEST"
echo
if [ "$PROMOTABLE" != "true" ]; then
  echo "⚠ 工作树 dirty,该 AMI 不可晋级到下一级环境 —— 先提交改动再重新构建。"
fi
echo "下一步:起一台真 host 验证(V-4),再切指针(V-5)。"
