# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apse1.pkrvars.hcl — ap-southeast-1 的构建参数。
#
# 本文件是【模板】。复制一份再改,不要直接编辑它:
#   cp deploy/packer/apse1.pkrvars.hcl deploy/packer/my.pkrvars.hcl
# my.pkrvars.hcl 已在 .gitignore 里 —— 账号 id、子网、角色名都属于部署方的坐标,
# 不进版本控制。
#
# 这些值必须与 config.yml 保持一致(config.yml 是 Image Builder 侧的源):
#   host.golden_ami.recipe_version  → recipe_version
#   host.root_volume_gb             → root_volume_gb
#   host.golden_ami.ssm_parameter   → ssm_parameter(留空时 CDK 用
#                                     _helpers.host_golden_ami_parameter_name())

region = "ap-southeast-1"
arch   = "arm64"

# ★ 必填 —— 替换 <ACCOUNT_ID> 为部署账号的 12 位 id(<GSUFFIX> 同 gsuffix,单环境留空)。
# 取值: aws sts get-caller-identity --query Account --output text
# 桶名规则是 openclaw-assets-<account><gsuffix>,由 OpenClawOrchestrator 栈创建。
assets_bucket = "openclaw-assets-<ACCOUNT_ID>"

# 与 config.yml host.golden_ami.recipe_version 同值。
# 改配方(装的东西变了)就 bump 这里,marker 的 recipe_version 字段会带上,
# host-agent 上报后可在机队里看出哪些机器还是旧配方。
recipe_version = "1.0.0"

# 与 config.yml host.root_volume_gb 同值。
root_volume_gb = 20

# #435 未落地 —— 当前留空即可,FC 取件不需要角色:provision-host.sh 无条件从
# github.com 拉 FC 二进制(该脚本 :132),不读 assets_bucket。实跑证据:留空在
# us-west-2 构建退出 0,第 3 步 `firecracker v1.15.1 installed`。
# 也就是说【当前任何一次构建产出的镜像都请求过 github.com】,#435 的"零 github 请求"
# 验收现在不成立 —— 这是 #435 未落地,不是本次构建配错。
# 等 #435 落地后,这里要填一个能读 deployment/binaries/firecracker/ 前缀的角色。
iam_instance_profile = ""

# 【内部选项,客户流程留空】构建完成后把 AMI id 写到该 SSM 参数,
# ha_edge.py 的 resolve_ssm_parameter_at_launch 读它。
#
# 客户流程是构建完直接改 LaunchTemplate 的 ImageId(CUSTOMER-GUIDE §6),不经过
# SSM 指针。内部环境的 LT 由 CDK 建、`cdk deploy` 会覆盖手改的版本,那时才需要
# 走指针(取值与 _helpers.host_golden_ami_parameter_name(gsuffix) 的默认值一致)。
ssm_parameter = ""

# 多环境后缀,与 CDK 的 _gsuffix 同值。ap-southeast-1 是主区,无后缀。
gsuffix = ""

# ── 客户自定义扩展(可选)─────────────────────────────────────────────────────
# 留空则跳过自定义阶段。指定后,该脚本在 provision-host.sh 之后、validate 断言之前
# 以 root 执行一次。复制 customize.sh.example 作为起点,其中列明了允许与禁止的操作。
custom_script     = ""
custom_script_env = {}
