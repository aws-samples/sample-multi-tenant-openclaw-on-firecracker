#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# assert-parity.sh — 断言 Packer 与 EC2 Image Builder 产出的是【同一份】镜像内容。
#
# 为什么需要这个:两套构建工具并存的唯一风险是漂移 —— 一侧变更配方而另一侧未同步,
# 于是 golden host 的行为取决于"该实例由哪套工具构建"。该不确定性的风险高于工具选型本身。
#
# 本脚本不比 AMI 的块设备(其内容会因时间戳与日志而不同),而是比【决定内容的输入】:
#   1. 两侧执行的是同一个 provision-host.sh(内容摘要相同)
#   2. 两侧执行的是同一个 install-fluent-bit.sh
#   3. 两边的 recipe_version 一致
#   4. 两边的 EBS/IMDS/parent-AMI 参数一致
#   5. Packer 侧复刻了 Image Builder 的两条 validate 断言(按名字查)
#
# 用法: deploy/packer/assert-parity.sh
# 退出码: 0=一致, 1=有漂移(逐条打印), 2=前置缺失

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKR="$ROOT/deploy/packer/host-golden.pkr.hcl"
VARS="$ROOT/deploy/packer/apse1.pkrvars.hcl"
IB="$ROOT/deploy/stacks/host_image.py"
CFG="$ROOT/config.yml"

for f in "$PKR" "$VARS" "$IB"; do
  [ -f "$f" ] || { echo "GATE_FAIL: 缺少文件 $f" >&2; exit 2; }
done

fail=0
_ok()   { printf '  ok   %s\n' "$1"; }
_bad()  { printf '  DRIFT %s\n' "$1" >&2; fail=1; }

echo "== packer / Image Builder 一致性 =="

# ── 1+2. 两侧执行同一份脚本 ────────────────────────────────────────────────────
# Image Builder 通过 CDK Asset 传输 deploy/userdata/provision-host.sh 与
# deploy/edge/fluent-bit/install-fluent-bit.sh(host_image.py 的 S3Download);
# Packer 通过 file provisioner 传输同两个文件。所以"同一份"= HCL 里引用的相对路径
# 指向同两个文件。摘要在 packer 的 manifest custom_data 里落痕,这里比路径。
for pair in \
  "provision-host.sh:../userdata/provision-host.sh" \
  "install-fluent-bit.sh:../edge/fluent-bit/install-fluent-bit.sh"
do
  name="${pair%%:*}"; rel="${pair#*:}"
  if grep -q -- "$rel" "$PKR"; then
    _ok "packer 引用仓库源 $name"
  else
    _bad "packer 未引用 $rel —— 可能已改为内联重写(必然与 Image Builder 产生漂移)"
  fi
  # Image Builder 侧:host_image.py 须将同一个文件作为 asset 引入
  if grep -qE "\"$name\"|/ \"$(basename "$name")\"|$(basename "$name" .sh)" "$IB"; then
    _ok "Image Builder 引用同一个 $name"
  else
    _bad "host_image.py 中未找到 $name 的引用"
  fi
done

# ── 3. recipe_version 一致 ───────────────────────────────────────────────────
# Image Builder 的源是 config.yml host.golden_ami.recipe_version;
# Packer 的源是 apse1.pkrvars.hcl 的 recipe_version。两者必须同值,否则同一批
# 机器上会出现两种 marker.recipe_version,无法在机队中区分各实例的配方版本。
_cfg_ver=""
if [ -f "$CFG" ]; then
  _cfg_ver="$(python3 - "$CFG" <<'PY' 2>/dev/null
import sys, yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
print(((c.get("host") or {}).get("golden_ami") or {}).get("recipe_version", ""))
PY
)"
fi
# 只取双引号内的值。不要用 `s/.*=\s*"(...)"/` —— packer fmt 会把等号对齐成
# `recipe_version = "1.0.0"`(等号前后多空格),BSD sed 不将 \s* 识别为空白,
# 导致将整行误判为"值"(实测结果:匹配到 'recipe_version = "1.0.0"' 而非 '1.0.0')。
_pkr_ver="$(grep -E '^[[:space:]]*recipe_version[[:space:]]*=' "$VARS" | head -1 | sed -E 's/^[^"]*"([^"]*)".*$/\1/')"
if [ -z "$_cfg_ver" ]; then
  echo "  skip config.yml 无 recipe_version(gitignored 本地文件?)—— 无法比对"
elif [ "$_cfg_ver" = "$_pkr_ver" ]; then
  _ok "recipe_version 一致 ($_pkr_ver)"
else
  _bad "recipe_version 漂移: config.yml=$_cfg_ver vs pkrvars=$_pkr_ver"
fi

# ── 4. recipe 参数一致 ───────────────────────────────────────────────────────
# 逐项对应 host_image.py 的 CfnImageRecipe / CfnInfrastructureConfiguration。
# 这些值决定产出镜像的形状,不一致就不是"同一份镜像"。
_chk() {  # $1=说明 $2=packer 里必须出现的串 $3=host_image.py 里必须出现的串
  local what="$1" p="$2" i="$3"
  if grep -q -- "$p" "$PKR" && grep -q -- "$i" "$IB"; then
    _ok "$what"
  else
    grep -q -- "$p" "$PKR" || _bad "$what —— packer 侧缺 '$p'"
    grep -q -- "$i" "$IB"  || _bad "$what —— Image Builder 侧缺 '$i'"
  fi
}
_chk "root 卷 gp3"          'volume_type           = "gp3"'  'volume_type="gp3"'
_chk "root 卷加密"           'encrypted             = true'   'encrypted=True'
_chk "IMDSv2 强制"           'http_tokens                 = "required"' 'http_tokens="required"'
_chk "IMDS hop limit 1"      'http_put_response_hop_limit = 1' 'http_put_response_hop_limit=1'
_chk "parent AMI 走 SSM 指针" 'canonical/ubuntu/server/24.04'  'canonical/ubuntu/server/24.04'
_chk "bake 模式打开 scrub"    'OC_PROVISION_BAKE=1'            'OC_PROVISION_BAKE'

# ── 5. Packer 复刻了两条 validate 断言 ───────────────────────────────────────
# Image Builder 的 validate 阶段是它原生提供的能力;Packer 没有对应概念,必须显式写。
# 遗漏则产出的镜像未经过"零下载"和"身份已擦净"。
for probe in \
  "零下载断言:golden AMI validated" \
  "幂等断言:provision is idempotent" \
  "身份泄漏断言:LEAK" \
  "cloud-init 态断言:cloud-init instance state"
do
  what="${probe%%:*}"; needle="${probe#*:}"
  if grep -q -- "$needle" "$PKR"; then
    _ok "$what 已复刻"
  else
    _bad "$what 缺失 —— Image Builder 的 validate 阶段有,packer 没有"
  fi
done

# ── 6. SSM 分发(Image Builder 原生提供、packer 必须自己做)────────────────────────
if grep -q 'ssm put-parameter' "$PKR"; then
  _ok "AMI id 发布到 SSM(对应 Image Builder 的 distribution)"
else
  _bad "packer 未发布 SSM 参数 —— ha_edge 的 resolve:ssm 读不到新镜像"
fi

# ── 7. 每个用 pipefail 的 inline 块都显式给了 bash shebang ────────────────────
# 实测 2026-08-12 的真 bug:packer inline 的默认 shebang 是 `/bin/sh -e`,Ubuntu 的
# /bin/sh 是 dash,dash 没有 pipefail → "Illegal option -o pipefail" → 整个
# provisioner 立即退出,断言主体完全未执行。而 `packer validate` 不执行脚本,无法检出,
# 于是"验证通过"和"断言真的跑了"是两件事。本项检查将其转为静态可检出。
_pipefail_blocks="$(grep -c 'set -euo pipefail' "$PKR")"
_bash_shebangs="$(grep -c 'inline_shebang' "$PKR")"
if [ "$_pipefail_blocks" -eq 0 ]; then
  _ok "无 pipefail inline 块(无需 shebang)"
elif [ "$_bash_shebangs" -ge "$_pipefail_blocks" ]; then
  _ok "pipefail inline 块均声明 bash shebang ($_bash_shebangs 个声明 / $_pipefail_blocks 个块)"
else
  _bad "有 $_pipefail_blocks 个 pipefail 块但只有 $_bash_shebangs 个 inline_shebang —— dash 拒绝 pipefail 并静默跳过断言主体"
fi

# ── 8. 送进 AWS API 的字符串必须是纯 ASCII ───────────────────────────────────
# 实测 2026-08-12:ami_description 里一个 em-dash 让 ModifyImageAttribute 报
# 400 "Character sets beyond ASCII are not supported"。这一步在 AMI 生成之后
# 才调用,所以失败表现是"AMI 存在、build 报错、manifest 与 SSM 分发均未执行" ——
# 排查方向易被误导至收尾逻辑,而实际原因是一个字符。
# 只查会进 API 的字段(ami_name / ami_description / 标签值);HCL 的 description
# 为面向使用者的变量说明,不进 API,中文合法。
_nonascii="$(grep -nE '^[[:space:]]*(ami_name|ami_description)[[:space:]]*=' "$PKR" \
  | LC_ALL=C grep -n '[^ -~]' || true)"
if [ -z "$_nonascii" ]; then
  _ok "ami_name / ami_description 纯 ASCII"
else
  _bad "AMI 字段含非 ASCII —— EC2 ModifyImageAttribute 会 400: $_nonascii"
fi

# ── 9. 客户自定义阶段必须排在 validate 断言【之前】────────────────────────────
# 自定义脚本执行在 scrub 之后,因此它写入的任何 per-host 状态都不会再被清理。
# validate 断言是唯一的防线。若把自定义阶段挪到断言之后,客户脚本留下的主机密钥、
# platform.env、SSH host key 就没人检出,fail-closed 属性失效而【构建仍然显示成功】——
# 这是最危险的一类回归:没有报错,只是防线没了。按行号定序,不依赖人工审查。
# 必须定位【执行】自定义脚本的那一行,不是 locals 里的路径定义 —— locals 恒在文件
# 前部,拿它比行号会让门永远绿。实测:第一版取 'custom/customize.sh' 首次出现,
# 命中的是 locals 的 custom_dst 定义,把整个 provisioner 块搬到断言之后也不报警。
_custom_line="$(grep -n 'bash ${local.custom_dst}' "$PKR" | head -1 | cut -d: -f1)"
_assert_line="$(grep -n 'golden AMI validated' "$PKR" | head -1 | cut -d: -f1)"
if [ -z "$_custom_line" ]; then
  _bad "找不到客户自定义阶段 —— host-golden.pkr.hcl 应包含 custom/customize.sh"
elif [ -z "$_assert_line" ]; then
  _bad "找不到零下载断言 —— 无法校验自定义阶段的相对位置"
elif [ "$_custom_line" -lt "$_assert_line" ]; then
  _ok "客户自定义阶段在 validate 断言之前(行 $_custom_line < $_assert_line)"
else
  _bad "客户自定义阶段($_custom_line)排在 validate 断言($_assert_line)之后 —— 自定义脚本引入的身份泄漏将无人检出"
fi

# ── 10. 自定义阶段引入的泄漏面都有对应断言 ───────────────────────────────────
# 自定义脚本能重装 openssh-server 或跑 dpkg-reconfigure,两者都会重新生成 SSH host
# key。scrub 删过它们,但在自定义阶段【之后】没有断言兜住。整个机队共享一把 host key
# 意味着任何能起一台 host 的人都能冒充其余每一台。
# 匹配可执行的断言体,不能只 grep 'ssh_host_' —— 上面的注释里也有这串,
# 断言被删掉后注释仍会命中,门就永远是绿的。实测:第一版这条门验红失败,原因正是此。
if grep -q 'LEAK: SSH host keys' "$PKR"; then
  _ok "SSH host key 泄漏断言存在"
else
  _bad "缺 SSH host key 断言 —— 自定义脚本重装 openssh-server 会让全机队共享同一把 host key"
fi

# ── 11. customize.sh.{default,example} 也要过静态检查 ──────────────────────────
# scripts/checks/shell.sh 只收 .sh 后缀,这两个文件的后缀是 .default / .example,
# 于是永久逃过仓库的 shell 门。而 customize.sh.example 正是客户照着改的模板 ——
# 它里面的写法会被复制进客户的生产构建。在这里补上。
for _f in "$ROOT/deploy/packer/customize.sh.default" "$ROOT/deploy/packer/customize.sh.example"; do
  _n="$(basename "$_f")"
  if [ ! -f "$_f" ]; then
    _bad "缺 $_n —— custom_script 留空时 file provisioner 会因源不存在而失败"
    continue
  fi
  if ! bash -n "$_f" 2>/dev/null; then
    _bad "$_n 语法错(bash -n 不过)"
    continue
  fi
  # -S info 而不是 warning:实测 warning 门槛太松,连 `cd /nonexistent`(SC2164)都不报,
  # 那样这条门就只是个装饰。info 级会抓到未加引号的变量展开等真会伤到客户的写法。
  # 模板里确实需要豁免的单条(如 SC1091 找不到 /etc/os-release)用行内 disable 注明理由。
  if command -v shellcheck >/dev/null 2>&1; then
    if shellcheck -s bash -S info "$_f" >/dev/null 2>&1; then
      _ok "$_n 过 bash -n 与 shellcheck"
    else
      _bad "$_n shellcheck 有 info 及以上: $(shellcheck -s bash -S info "$_f" 2>&1 | grep -oE 'SC[0-9]+' | sort -u | tr '\n' ' ')"
    fi
  else
    _ok "$_n 过 bash -n(shellcheck 未装,跳过更严的检查)"
  fi
done

echo
if [ "$fail" = 0 ]; then
  echo "✓ 一致:两套工具的输入与断言等价"
  exit 0
fi
echo "⛔ 检出漂移(见上 DRIFT 行)。修复方式:让 packer 侧与 host_image.py / config.yml 同源," >&2
echo '  不应依赖「两侧各修改一次」—— 该做法正是双轨维护失效的根源。' >&2
exit 1
