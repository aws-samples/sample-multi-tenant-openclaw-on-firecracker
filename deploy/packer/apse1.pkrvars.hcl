# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# apse1.pkrvars.hcl — ap-southeast-1 的构建参数。
#
# ★ #537 起本文件【通常不需要】。build-golden-ami.sh 会自动发现 region、
#   assets_bucket、iam_instance_profile、vpc_id、subnet_id,其余变量都有默认值,
#   所以标准构建是:
#
#     bash deploy/packer/build-golden-ami.sh --env test
#
#   只在下面两种情况才需要复制本文件并传 --var-file:
#     ① 私有子网 + NAT 的部署 —— 自动发现只找公有子网(有 igw 默认路由 +
#        MapPublicIpOnLaunch),私有子网要显式给 subnet_id 并按 §2.1 设
#        associate_public_ip=false 与 ssh_interface;
#     ② 要覆盖某个自动值(var-file 里显式写下的键优先于自动发现)。
#
#   复制一份再改,不要直接编辑它:
#     cp deploy/packer/apse1.pkrvars.hcl deploy/packer/my.pkrvars.hcl
#   my.pkrvars.hcl 已在 .gitignore 里 —— 账号 id、子网、角色名都属于部署方的坐标,
#   不进版本控制。
#
# 这些值必须与 config.yml 保持一致(config.yml 是 Image Builder 侧的源):
#   host.golden_ami.recipe_version  → recipe_version
#   host.root_volume_gb             → root_volume_gb
#   host.golden_ami.ssm_parameter   → ssm_parameter(留空时 CDK 用
#                                     _helpers.host_golden_ami_parameter_name())

# ── 自动发现的项:全部注释掉。取消注释即覆盖自动值。 ─────────────────────────
# region               = "ap-southeast-1"              # 自动:AWS_REGION / aws 配置
# assets_bucket        = "openclaw-assets-<ACCOUNT_ID>" # 自动:按账号 id 拼
# iam_instance_profile = "openclaw-packer-builder"      # 自动:固定名(见 §2.2)
# vpc_id               = "vpc-xxxxxxxx"                 # 自动:公有子网所在 VPC
# subnet_id            = "subnet-xxxxxxxx"              # 自动:有 igw 出网的公有子网

# ── 私有子网 + NAT 的部署需要这两项(自动发现只覆盖公有子网场景)─────────────
# associate_public_ip = false
# ssh_interface       = "private_ip"      # 或 "session_manager"(需 SSM 插件 + 端点)
# security_group_id   = ""                # 留空 = packer 建临时 SG,跑完删

# ── 真正因部署而异、且无法从账号里查出来的语义参数 ───────────────────────────

# 改配方(装的东西变了)就 bump 这里,marker 的 recipe_version 字段会带上,
# host-agent 上报后可在机队里看出哪些机器还是旧配方。
recipe_version = "1.0.0"

# 与 config.yml host.root_volume_gb 同值。
root_volume_gb = 20

# 多环境后缀,与 CDK 的 _gsuffix 同值。ap-southeast-1 是主区,无后缀。
# 注意:它参与 assets_bucket 的自动推导(openclaw-assets-<account><gsuffix>)。
gsuffix = ""

# 【内部选项,客户流程留空】构建完成后把 AMI id 写到该 SSM 参数,
# ha_edge.py 的 resolve_ssm_parameter_at_launch 读它。
#
# 客户流程是构建完直接改 LaunchTemplate 的 ImageId(CUSTOMER-GUIDE §6),不经过
# SSM 指针。内部环境的 LT 由 CDK 建、`cdk deploy` 会覆盖手改的版本,那时才需要
# 走指针(取值与 _helpers.host_golden_ami_parameter_name(gsuffix) 的默认值一致)。
ssm_parameter = ""

# ── 客户自定义扩展(可选)─────────────────────────────────────────────────────
# #537 D5 起扩展点是【固定 hook 目录】deploy/packer/hooks/,不再是单文件 custom_script。
# 往那个目录放 *.sh(记得 chmod +x),构建时按文件名字典序逐个执行,位置固定在
# provision-host.sh 之后、两条 validate 断言之前。用法与禁止事项见 hooks/README.md。
# 目录为空时该阶段自动 no-op,这里不需要配任何东西。
#
# hook_env 的键值对作为环境变量传给每个 hook —— 把参数外置于脚本本体。
# 不要经由它传密钥:值会出现在构建日志里。
hook_env = {}

# ── 注意:arch 与 instance_type 不再是参数 ───────────────────────────────────
# host AMI 只出 arm64 —— config.yml 生产基线是 arm64,而 host.instance_types 的四个
# 机型(r8g.metal-24xl / r7g.metal / m8g.metal-24xl / m7g.metal)全是 Graviton,
# config.yml 本身也约束「所有成员须与 host.arch 同架构」。构建机型固定 c7g.large
# (一次性实例,无调优空间)。
#
# git_commit / packer_template_sha / hooks_sha / env / built_at / promotable 这六个
# 也不在这里配 —— 由 build-golden-ami.sh 在构建前解析并注入。直接 `packer build`
# 会因缺它们而失败,那是刻意的:坐标不全的 AMI 无法被晋级、无法被切指针(#537 V-3)。
