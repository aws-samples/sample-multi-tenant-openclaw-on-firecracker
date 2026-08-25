# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# host-golden.pkr.hcl — Packer 版 host golden AMI 构建，与 EC2 Image Builder
# (deploy/stacks/host_image.py) 产出【同一份】镜像内容。
#
# 为什么两套并存:Image Builder 是 CFN 资源、与 ASG 在同一张依赖图里,且原生提供
# SSM 参数分发 + post-AMI 启动测试;Packer 本地可跑、HCL 可读、不依赖 AWS 编排。
# 现阶段 Packer 作为可本地验证的等价实现存在,不删 Image Builder ——
# 换工具的前提是先有两边一致性的证据(见下方 assert_parity)。
#
# ★ 核心设计:provisioner 直接调仓库里的 provision-host.sh,不在 HCL 里重写安装步骤。
# 双轨维护必然漂移 —— #440(gpg --batch --yes)、#451(marker 移到 scrub 后)、
# #435(FC 二进制迁 S3)这三条修复都在那个脚本里,复用即自动带上,重写就要手工同步两遍。
# HCL 因此看不到 8 节细节,那是刻意的取舍:一致性 > 可读性。
#
# 用法:
#   packer init  deploy/packer
#   packer validate -var-file=deploy/packer/apse1.pkrvars.hcl deploy/packer
#   packer build    -var-file="$PWD/deploy/packer/apse1.pkrvars.hcl" "$PWD/deploy/packer"
#   (模板目录必须是绝对路径:相对路径下 file() 会二次拼接,validate/build 都直接失败。)
#
# 前置:
#   - 调用者身份需能 RunInstances / CreateImage / CreateTags / PutParameter
#   - assets_bucket 现在【真的有人读】:模板把它注进 provisioner 环境(OC_ASSETS_BUCKET),
#     provision-host.sh 的 _fc_s3_uri() 优先按它拼 deployment/binaries/firecracker/<ver>/
#     取 FC 二进制(#435),取回后还要过 _fc_expected_sha() 的强制摘要校验才安装。
#   - iam_instance_profile 是【必填】:bake 阶段模板固定注入 OC_FC_REQUIRE_S3=1,S3 取不到
#     即中止构建(不回落 github)。留空 = 无角色 = AccessDenied = 构建红。这是刻意的 ——
#     回落会让「零 github 请求」验收永远通过,把假绿换成明确失败更有用。

packer {
  required_version = ">= 1.9.0"
  required_plugins {
    amazon = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

# ── 变量 ─────────────────────────────────────────────────────────────────────
# 默认值与 config.yml 的 host.golden_ami / host 段保持一致。改这里不会改 CDK 侧,
# 两边都要改时以 config.yml 为准(它是 Image Builder 的源)。

variable "region" {
  type        = string
  description = "构建与发布 AMI 的 region"
}

# ★ arch 不再是变量(#537 简化)。host AMI 只出 arm64:
#   - config.yml.example:26-27 写明「生产基线=arm64(Graviton metal 原生 KVM)」;
#   - host.instance_types 的四个机型 r8g.metal-24xl / r7g.metal / m8g.metal-24xl /
#     m7g.metal 全是 Graviton,没有一个 x86 机型(#430 的优先级顺序同样是这四个);
#   - config.yml 的约束原文:「所有成员须与 host.arch 同架构(AMI 是按架构的,x86 与
#     arm64 不能混)」—— 也就是说混池本来就不允许跨架构。
# 保留一个 amd64 分支的代价是:每次改模板都要想它、pkrvars 里多一个客户会填错的字段、
# 而 amd64 那条路在这套部署里【从来没有消费者】(实测 2026-08-12 造过一个 amd64 AMI,
# 至今零使用)。真要支持 x86 host 时,重新加回一个变量比维护一条没人走的分支便宜。
#
# Image Builder 侧(host_image.py:209)仍按 config 的 host.arch 二选一 —— 那是另一套
# 工具的既有行为,本 issue 不动它;assert-parity.sh 比对 parent AMI 时按 arm64 对齐。
# 取值见下方 locals 的 arch;parent AMI 的 SSM 路径直接写死在 data 块里(packer 的
# data 源在 locals 之前求值,引用不了 local.*)。

variable "recipe_version" {
  type        = string
  default     = "1.0.0"
  description = <<-EOT
    对应 config.yml 的 host.golden_ami.recipe_version。写进 marker 的 recipe_version
    字段、AMI 名称与标签。Image Builder 侧同版本号重复部署会被拒;Packer 不拦,
    但 AMI 名称含时间戳所以不会撞名 —— 版本号在这里的作用是【标记这批镜像的配方代次】。
  EOT
}

variable "assets_bucket" {
  type        = string
  description = <<-EOT
    openclaw-assets-<account><gsuffix>。
    #435 起本变量【有了消费者】:模板把它注进 provisioner 环境(OC_ASSETS_BUCKET),
    provision-host.sh 的 _fc_s3_uri() 优先按它拼 deployment/binaries/firecracker/ 取件,
    读不到才回落 github.com。构建实例的 instance profile 需有该前缀的 s3:GetObject,
    否则 AccessDenied → 回落。
  EOT
  # 模板里 assets_bucket 是占位符 openclaw-assets-<ACCOUNT_ID>。这条校验要留:
  # 占位符是"客户没配完"的信号,在 validate 阶段拦住比起了构建实例再说更省事 —— 尽管
  # bake 现在带 OC_FC_REQUIRE_S3=1,占位符桶名会在 provision 第 3 节 die(构建照样红),
  # 但那要先起一台机、跑完 apt 与 awscli 才炸,白烧几分钟。validate 阶段拦更便宜。
  validation {
    condition     = !can(regex("<[A-Z_]+>", var.assets_bucket))
    error_message = "The assets_bucket variable still contains a placeholder. Replace <ACCOUNT_ID> with the 12-digit deployment account id."
  }
}

# instance_type 也不再是变量(#537 简化):arch 固定成 arm64 之后,「按 arch 选
# c7g.large / c7i.large」这个分支只剩一个分支。构建实例是一次性的、跑一遍 provision
# 就终止,机型没有调优空间 —— 留一个变量只是多一个客户可以填错的字段。
# 值与 host_image.py:369 的默认同源。

variable "root_volume_gb" {
  type        = number
  default     = 20
  description = "对应 config.yml host.root_volume_gb;与 Image Builder recipe 的 volume_size 同值"
}

variable "iam_instance_profile" {
  type        = string
  default     = ""
  description = <<-EOT
    构建实例的 instance profile 名。留空则构建实例上没有角色。
    #435 起【必填,留空构建会失败】:provision-host.sh 先从 deployment/binaries/firecracker/
    前缀取 FC 二进制,所以这个角色需要该前缀的 s3:GetObject;而 bake 阶段模板固定注入
    OC_FC_REQUIRE_S3=1,S3 取不到即 die 而不回落 github。留空 = AccessDenied = 构建红。
    默认值保持留空只为让 packer validate 免配即可跑(validate 不起构建实例)。
    给 SSM 权限仍便于排障。
  EOT
}

variable "ssm_parameter" {
  type        = string
  default     = ""
  description = <<-EOT
    【内部选项,客户手册未涉及】构建完成后把 AMI id 写到这个 SSM 参数
    (ha_edge.py 的 resolve_ssm_parameter_at_launch 读它)。留空 = 不写,只产出 AMI。

    客户流程是构建完直接改 LaunchTemplate 的 ImageId(CUSTOMER-GUIDE §6),不经过
    SSM 指针 —— 少一层间接就少一个"参数写了但 LT 没读它"的排查面。这里保留该能力是
    因为内部环境的 LT 由 CDK 建,`cdk deploy` 会覆盖手改的版本,走 SSM 指针才不被覆盖。
    Image Builder 的 distribution 原生提供该步骤,Packer 需自己做 —— 见下方
    post-processor 里的显式 aws ssm put-parameter。
  EOT
}

variable "gsuffix" {
  type        = string
  default     = ""
  description = "多环境后缀,与 CDK 的 _gsuffix 同值;拼进 AMI 名与 SSM 参数名"
}

# ── 可追溯性坐标(由 build-golden-ami.sh 注入,不要手填)──────────────────────
# 这六个变量没有默认值 —— 缺任一个 packer 就拒绝跑。这是刻意的:#537 V-3 要求 AMI tag
# 与 manifest 各带齐八项坐标,缺一项该 AMI 就不可晋级、不可切指针。给默认值会让
# 「直接 packer build」产出一个坐标不全的 AMI,而那种 AMI 的存在本身就是 US-6
# (查某台 host 跑的是哪个变更集)做不成的原因。
#
# 注入方 deploy/packer/build-golden-ami.sh 在构建【之前】解析它们并跑 V-1 门。
# packer 自己没有 git 概念,这些值只能由外部算出后传进来。

variable "git_commit" {
  type        = string
  description = <<-EOT
    本次构建对应的 git commit SHA。工作树 dirty 时带 `-dirty` 后缀,且 promotable=false。
    这是 US-6「某台在役 host 跑的是哪个变更集」的唯一锚点。
  EOT
  # error_message 必须英文整句(大写开头、句号结尾)—— Packer 自己校验这条格式。
  validation {
    condition     = length(var.git_commit) >= 7
    error_message = "The git_commit variable must be a resolved commit SHA; run deploy/packer/build-golden-ami.sh instead of calling packer directly."
  }
}

variable "packer_template_sha" {
  type        = string
  description = <<-EOT
    host-golden.pkr.hcl 自身的 sha256。为什么单独记:模板变了而 provision-host.sh 没变时
    provision_sha256 察觉不到,而模板决定了步骤集合、顺序与注入的环境变量 —— 那同样改变
    产出。V-6 靠它发现「上一级验的模板和这一级用的不是同一份」。
  EOT
}

variable "hooks_sha" {
  type        = string
  description = <<-EOT
    客户 hook 集合的摘要(逐个 "sha256  basename" 再取一次摘要)。空目录是空串的 sha256,
    一个固定值,不是空字段 —— V-6 要比它相等,缺字段无法参与比较。

    构建侧(build-golden-ami.sh 用 bash find)与构建实例侧(assert-image.sh 重算镜像里
    实际执行过的那批)各算一次并比对。两侧独立计算才能发现「上传的和执行的不是同一批」。
  EOT
}

variable "env" {
  type        = string
  description = <<-EOT
    目标环境。写进 AMI 的 Env tag,切指针前会校验它与要切的环境相符 —— 这是防止把测试
    环境的 AMI 切进生产的那道断言(#537 V-5)。
  EOT
  validation {
    condition     = contains(["test", "staging", "prod"], var.env)
    error_message = "The env variable must be test, staging, or prod."
  }
}

variable "built_at" {
  type        = string
  description = <<-EOT
    构建发起时刻(UTC ISO8601)。由 build-golden-ami.sh 生成而不是用 packer 的
    {{isotime}}:V-3 要回读 tag 并与 manifest 比对成【相等】,而 AMI 转 available 要
    11 分钟,两边各取一次时间必然差出去,那条断言就做不成。一个来源,两处引用。
  EOT
}

variable "promotable" {
  type        = string
  default     = "true"
  description = <<-EOT
    "true" | "false"。工作树 dirty 时为 false —— 该 AMI 可以本地验证,但不可晋级:
    它对应的「变更集」在 git 里不存在,V-6 的同源比对无从进行。
    只进 manifest 不进 tag:tag 是给运行期查询的事实,而"能不能晋级"是流程判定,
    晋级门读 manifest。
  EOT
}

# ── 客户自定义扩展:固定 hook 目录(#537 D5)──────────────────────────────────
# 取代 #477 的单文件 custom_script。为什么换:单文件下客户要加第二个步骤只能改那一个
# 文件或改 HCL —— 前者把互不相关的步骤揉进一份脚本,后者正是 D5 要禁的「改主流程」。
# 目录 + 字典序让每个步骤是独立文件,加删互不影响,而主流程文件被改动时 V-1 门直接拒绝。
#
# 目录路径是固定的(deploy/packer/hooks/),不做成变量:可配置的扩展点位置意味着
# 「主流程文件摘要」这道门要跟着变量走,而那正是 D5 想钉死的东西。

variable "hook_env" {
  type        = map(string)
  default     = {}
  description = <<-EOT
    传给每个 hook 的环境变量。用于把参数从 pkrvars 传进去,而不是让客户把参数硬编码
    进脚本。

    不要经由此处传密钥:值会出现在构建日志里。运行期密钥应由 host 的 instance profile
    在启动时从 Secrets Manager / SSM Parameter Store(SecureString)获取。
  EOT
}

# ── 网络(客户必填)────────────────────────────────────────────────────────────
# 构建实例需要出网装包(apt / awscli / ADOT / fluent-bit),并且要能被 packer 从本机
# SSH 进去。留空则用账号的默认 VPC —— 很多企业账号没有默认 VPC,或默认 VPC 无
# NAT/IGW,那时 packer 会卡在等 SSH 直到超时。所以文档要求客户显式填。

variable "vpc_id" {
  type        = string
  default     = ""
  description = "构建实例所在 VPC。留空 = 用账号默认 VPC(企业账号常常没有,或没出网路径)"
}

variable "subnet_id" {
  type        = string
  default     = ""
  description = <<-EOT
    构建实例所在子网。必须能出网(公有子网 + IGW,或私有子网 + NAT)——
    provision 要装 apt 包、awscli、ADOT collector、Fluent Bit。
    留空 = packer 在 vpc_id 里自选一个,自选结果可能是无出网的隔离子网。
  EOT
}

variable "security_group_id" {
  type        = string
  default     = ""
  description = <<-EOT
    构建实例安全组。留空 = packer 临时建一个只放行本机公网 IP 的 22 端口,
    跑完删除(temporary_security_group_source_cidrs 行为)。
    企业账号若禁止建 SG,就必须显式给一个:出站全放、入站 22 允许 packer 执行处。
  EOT
}

variable "associate_public_ip" {
  type        = bool
  default     = true
  description = <<-EOT
    true = 给构建实例公网 IP,packer 走公网 SSH(公有子网场景)。
    false = 走私有 IP,要求 packer 执行处与该子网网络可达(VPN/堡垒/同 VPC 内)。
    私有子网 + NAT 的客户填 false,并确认执行处能路由到该子网。
  EOT
}

variable "ssh_interface" {
  type        = string
  default     = "public_ip"
  description = <<-EOT
    packer 用哪个地址 SSH:public_ip | private_ip | session_manager。
    session_manager 走 SSM 会话(不需要入站 22、不需要公网 IP),但要求构建实例的
    instance profile 带 AmazonSSMManagedInstanceCore 且子网能到 SSM 端点。
    企业网络最严的场景建议用它。
  EOT
  validation {
    condition     = contains(["public_ip", "private_ip", "session_manager"], var.ssh_interface)
    error_message = "The ssh_interface variable must be public_ip, private_ip, or session_manager."
  }
}

# ── locals ───────────────────────────────────────────────────────────────────

# parent AMI 走 Canonical 官方 SSM 公共参数,与 host_image.py:_UBUNTU_SSM 逐字一致。
# 不用 source_ami_filter 的 name 通配:那会在 Canonical 发新版时静默换 base,
# 而 SSM 参数是 Canonical 自己维护的 "current stable" 指针,语义明确。
#
# ★ 这里必须用 packer 的 aws_parameterstore 数据源,不能写 Image Builder 的
# `{{ssm:<path>}}`(那是 CFN/Image Builder 的解析语法,packer 会当模板函数报
# "function ssm not defined")。数据源在 validate 阶段就会真去读 SSM,所以
# validate 本身也验证了调用者有 ssm:GetParameter 且路径存在。
data "amazon-parameterstore" "ubuntu_ami" {
  name   = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
  region = var.region
}

locals {

  # host AMI 只出 arm64(理由见文件上方 arch 那段注释)。写成 local 而不是变量:
  # 它不是给人调的旋钮,是这套部署的事实 —— 四个 host 机型全是 Graviton。
  arch = "arm64"

  # 构建机型:与 host_image.py:369 的 arm64 默认同源。构建是一次性动作,不需要 metal,
  # 也没有调优空间,所以不留变量。
  build_instance = "c7g.large"

  ami_name = "openclaw-host-${var.recipe_version}-${local.arch}${var.gsuffix}-{{timestamp}}"

  # 脚本在构建实例上的落点。与 Image Builder component 的 S3Download destination 一致
  # (/opt/openclaw/...),因为 validate 阶段的 AssertProvisionIsIdempotent 会按这个
  # 绝对路径再跑一次 provision —— 路径不一致那条断言就跑不了。
  provision_dst = "/opt/openclaw/provision-host.sh"
  fb_dst        = "/opt/openclaw/install-fluent-bit.sh"

  # hook 目录与 runner 落在 /opt/openclaw/custom/ 而不是 /tmp:留在镜像里可审计
  # (起一台 host 就能 ls 出这批镜像装了什么客户内容),/tmp 在部分环境会被开机清空。
  # hooks-manifest 是 run-hooks.sh 生成的执行记录,与 hooks 目录同级。
  hooks_dst        = "/opt/openclaw/custom/hooks"
  hooks_runner_dst = "/opt/openclaw/custom/run-hooks.sh"
  hooks_manifest   = "/opt/openclaw/custom/hooks-manifest"
  assert_dst       = "/opt/openclaw/assert-image.sh"

  # 本地 hook 集合。fileset 返回的是相对 hooks/ 的路径,只收 *.sh —— 与
  # build-golden-ami.sh 的 `find -maxdepth 1 -type f -name '*.sh'` 和 run-hooks.sh 的
  # 同款 find 三处对齐。三处用三种实现(HCL fileset / bash find / bash find)是刻意的:
  # 数量不一致就说明某一处的枚举语义漂了,V-3 会拿 hooks_count 对账并报红。
  #
  # 不用 **/*.sh:hook 是平铺一层。递归会把客户放在子目录里的辅助脚本也算成 hook,
  # 而 run-hooks.sh 用 -maxdepth 1 不会执行它们 —— 那种不一致正是对账要抓的。
  hook_files = fileset("${path.root}/hooks", "*.sh")
}

# ── source ───────────────────────────────────────────────────────────────────

source "amazon-ebs" "host" {
  region        = var.region
  instance_type = local.build_instance
  ssh_username  = "ubuntu"

  # Canonical 的 current-stable 指针,由上面的 amazon-parameterstore 数据源解析成
  # 具体 ami-id。直接给 source_ami 而不是 source_ami_filter:指针已经唯一确定了
  # 镜像,再套 filter 只会多一次 DescribeImages 且引入"filter 匹配到别的"的面。
  source_ami = data.amazon-parameterstore.ubuntu_ami.value

  ami_name = local.ami_name
  # ASCII only. EC2 的 ModifyImageAttribute 拒非 ASCII:"Character sets beyond ASCII
  # are not supported"(实测 2026-08-12,em-dash 触发 400)。这一步在 AMI 已经造好
  # 之后才调用,所以失败时的表现是"AMI 存在但 build 报错、manifest 与 SSM 分发均未执行"
  # —— 看起来像收尾卡住,其实是描述里一个字符。用连字符,不用破折号。
  #
  # #523 判据 5:原文写 "boot path installs nothing",与事实不符 —— init-host.sh 在
  # LOGGING_ENABLED=true 时【无条件】从 S3 拉 install-fluent-bit.sh(S3 miss 即 exit 1),
  # 还要拉 oc-guest-log-reader.py、rootfs manifest.json 与全部生命周期脚本。真正成立的
  # 那一半是「组件零安装」:fluent-bit installer 在已装的镜像上 no-op,provision 整段跳过。
  # 写成事实,否则下一个人会按「零下载」去排查启动失败,方向就错了 —— 客户那两次 canary
  # ABANDON 恰好发生在「AMI 已经有、启动仍跑 S3 那份」这条路上(#520 A21)。
  ami_description = "OpenClaw host golden AMI (recipe ${var.recipe_version}, ${local.arch}) - Firecracker + jailer + awscli + ADOT + Fluent Bit baked; boot path installs no components, but still pulls config, lifecycle scripts and rootfs from S3"

  iam_instance_profile = var.iam_instance_profile

  # ── 网络 ──────────────────────────────────────────────────────────────────
  # 空字符串在 amazon-ebs 里等价于"不设" —— packer 回落默认 VPC/自选子网/临时 SG。
  # 客户场景下这三个都该显式给(见文档 §2),留空只适合本仓开发者在有默认 VPC 的
  # 账号上快速试运行构建。
  vpc_id                      = var.vpc_id
  subnet_id                   = var.subnet_id
  security_group_id           = var.security_group_id
  associate_public_ip_address = var.associate_public_ip
  ssh_interface               = var.ssh_interface

  # 与 host_image.py:432-441 的 InstanceBlockDeviceMapping 逐项对应。
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  # IMDSv2 only + hop limit 1,与 host_image.py:540-541 一致。
  # hop_limit=1 意味着容器内拿不到 IMDS —— provision 不在容器里跑,不受影响。
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }
  # imds_support = "v2.0" 让【产出的 AMI】默认强制 IMDSv2,而非只是构建实例。
  # 租户 microVM 的 IMDS 是 DROP 的(ha_edge SG),这里管的是 host 自身。
  imds_support = "v2.0"

  # #537 V-3:八项坐标(GitCommit / PackerTemplateSha / ProvisionSha256 / RecipeVersion /
  # Arch / Env / BuiltAt / HooksSha)必须齐,缺任一项该 AMI 不可晋级、不可切指针。
  # build-golden-ami.sh 在构建后回读这些 tag 并逐项与注入值比对 —— 只查"在不在"不够,
  # tag 存在但写着上一次构建的 SHA 会让 V-6 的同源比对拿错值去比。
  #
  # 全部值必须是 ASCII。EC2 的 ModifyImageAttribute 拒非 ASCII(实测 2026-08-12,
  # em-dash 触发 400),而 tag 与 description 走同一条约束。
  tags = {
    Name            = "openclaw-host-golden${var.gsuffix}"
    Project         = "openclaw"
    Role            = "metal-host"
    RecipeVersion   = var.recipe_version
    Arch            = local.arch
    BuiltBy         = "packer"
    ProvisionSource = "deploy/userdata/provision-host.sh"

    GitCommit         = var.git_commit
    PackerTemplateSha = var.packer_template_sha
    ProvisionSha256   = sha256(file("${path.root}/../userdata/provision-host.sh"))
    HooksSha          = var.hooks_sha
    Env               = var.env
    BuiltAt           = var.built_at
  }
  # 构建实例自身也打标,便于在 EC2 控制台区分"这是一台一次性构建实例,不是 host"。
  run_tags = {
    Name    = "openclaw-packer-builder${var.gsuffix}"
    Project = "openclaw"
    Role    = "image-builder"
  }
}

# ── build ────────────────────────────────────────────────────────────────────

build {
  name    = "openclaw-host-golden"
  sources = ["source.amazon-ebs.host"]

  # 1) 送脚本上机。两个都送:provision-host.sh 会调 fluent-bit installer
  #    (OC_PROVISION_FLUENT_BIT_INSTALLER),不给它就会走"boot path 装一次"的回落
  #    分支 —— 那正是 golden AMI 要消掉的网络动作。
  provisioner "shell" {
    inline = ["sudo install -d -m 0755 /opt/openclaw"]
  }
  provisioner "file" {
    source      = "${path.root}/../userdata/provision-host.sh"
    destination = "/tmp/provision-host.sh"
  }
  provisioner "file" {
    source      = "${path.root}/../edge/fluent-bit/install-fluent-bit.sh"
    destination = "/tmp/install-fluent-bit.sh"
  }
  # 镜像断言脚本。抽成一个文件而不是两段内联:同一套检查要在 provision 之后与
  # provision 重跑之后各跑一次,两处各维护一份必然漂移。实测 2026-08-13 的真缺陷正是
  # 如此 —— 幂等阶段只重查了 2 个命令和 2 个泄漏路径,漏掉 Fluent Bit / vmlinux /
  # ADOT / SSH host key / cloud-init 态,而"重跑重装了这些"恰好是幂等断言该抓的东西。
  provisioner "file" {
    source      = "${path.root}/assert-image.sh"
    destination = "/tmp/assert-image.sh"
  }
  provisioner "shell" {
    inline = [
      "sudo install -o root -g root -m 0755 /tmp/provision-host.sh ${local.provision_dst}",
      "sudo install -o root -g root -m 0755 /tmp/install-fluent-bit.sh ${local.fb_dst}",
      "sudo install -o root -g root -m 0755 /tmp/assert-image.sh ${local.assert_dst}",
      "rm -f /tmp/provision-host.sh /tmp/install-fluent-bit.sh /tmp/assert-image.sh",
    ]
  }

  # 2) 跑 provision。OC_PROVISION_BAKE=1 打开 §7 的 scrub —— 删掉 host_vm_key /
  #    platform.env / ssh host keys / cloud-init 实例态。AMI 被全 fleet 共享,
  #    任何 host-identifying 内容留在里面就变成 fleet 级共享状态;host_vm_key 的
  #    公钥半边注进每个租户 microVM,一把被共享的私钥等于任意 host 能 SSH 进任意
  #    租户的 microVM。scrub 自带 fail-closed 自校验(删不掉就 die)。
  #    #451:marker 现在写在 scrub【之后】,所以 scrub 失败的镜像不会自称完成。
  provisioner "shell" {
    environment_vars = [
      "OC_PROVISION_BAKE=1",
      "OC_PROVISION_RECIPE_VERSION=${var.recipe_version}",
      "OC_PROVISION_FLUENT_BIT_INSTALLER=${local.fb_dst}",
      "OC_ASSETS_BUCKET=${var.assets_bucket}",
      # #435 —— bake 必须 fail-closed:S3 取不到 FC 就【中止构建】,不许回落 github。
      # 不加这条的话,iam_instance_profile 留空 → AccessDenied → 回落 → 镜像照样烤成、
      # validate 照样过,而那台镜像请求过 github.com,「零 github 请求」验收成了假绿。
      # 代价是 iam_instance_profile 从"建议填"变成"不填就构建失败" —— 这正是想要的。
      "OC_FC_REQUIRE_S3=1",
      "AWS_REGION=${var.region}",
    ]
    # -E 传环境变量给 root。expect_disconnect=false:provision 不重启机器。
    inline = ["sudo -E bash ${local.provision_dst}"]
    # provision 最慢的一段是 ADOT deb(~100MB)+ rootfs 无关的 apt。900s 覆盖慢 CDN,
    # 不是给重试留窗口 —— 脚本内部自己有重试。
    timeout = "20m"
  }

  # 3) 客户自定义阶段 —— 固定 hook 目录,按字典序执行(#537 D5,取代单文件 custom_script)。
  #    这里的位置不是任选的,三个边界把它夹死在这一格:
  #    - provision 之后:组件(firecracker/awscli/ADOT/Fluent Bit)已装好,hook 才有
  #      东西可依赖;
  #    - scrub 之后(scrub 在 provision-host.sh §7 内部):所以 hook 写入的任何
  #      per-host 状态【不会再被清理】,会原样进镜像并被整个机队共享 —— 这正是下一
  #      条断言存在的理由;
  #    - 断言之前:断言是 hook 的唯一防线。放到断言之后,hook 留下的主机密钥、
  #      platform.env、cloud-init 实例态就没人检出了,fail-closed 属性会失效。
  #    ADR §6 把「hook 挪到断言之后」记为已知失效模式(构建仍绿而防线失效);
  #    build-golden-ami.sh 的 V-1.5 与 assert-parity.sh 第 9 项各按行号复核一次。
  #
  #    空目录不需要特殊分支:run-hooks.sh 自己在没有 *.sh 时打印一行并 exit 0。
  #    这比 #477 的「留空则执行 customize.sh.default」更简单 —— 少一个只为绕开
  #    「file provisioner 拿到空路径会报错」而存在的占位文件。
  provisioner "file" {
    # 传目录要带尾斜杠。不带时 packer 把 hooks/ 整个目录【放进】destination 里
    # (变成 /tmp/oc-hooks/hooks/*.sh);带尾斜杠才是"把目录内容拷进去"。
    # 目录不存在会直接报错,所以 hooks/ 里有入库的 README.md 与 *.sh.example 兜底,
    # 客户 clone 后目录必然存在。
    source      = "${path.root}/hooks/"
    destination = "/tmp/oc-hooks"
  }
  provisioner "file" {
    source      = "${path.root}/run-hooks.sh"
    destination = "/tmp/run-hooks.sh"
  }
  provisioner "shell" {
    inline = [
      "sudo install -d -m 0755 /opt/openclaw/custom ${local.hooks_dst}",
      # 只搬 *.sh。README.md 与 *.sh.example 是仓库文档,搬进镜像只会让
      # `ls /opt/openclaw/custom/hooks/` 的输出里混进"看着像 hook 其实不执行"的文件,
      # 而那正是排查"我的 hook 为什么没跑"时最容易看错的地方。
      # 用 find -exec 而不是 `cp /tmp/oc-hooks/*.sh`:后者在无匹配时 glob 不展开,
      # cp 会报 "cannot stat '*.sh'" 并让整个 provisioner 红 —— 空 hook 目录是合法状态。
      "sudo find /tmp/oc-hooks -maxdepth 1 -type f -name '*.sh' -exec install -o root -g root -m 0755 {} ${local.hooks_dst}/ ';'",
      "sudo install -o root -g root -m 0755 /tmp/run-hooks.sh ${local.hooks_runner_dst}",
      "rm -rf /tmp/oc-hooks /tmp/run-hooks.sh",
    ]
  }
  provisioner "shell" {
    # 客户参数经 environment_vars 传入,避免客户把参数硬编码进脚本。密钥不走这里 ——
    # 值会出现在构建日志中。
    environment_vars = [for k, v in var.hook_env : "${k}=${v}"]
    # -E 传环境变量给 root。与 provision 同样的调用形式,便于客户对照。
    inline = ["sudo -E bash ${local.hooks_runner_dst} ${local.hooks_dst}"]
    # 比 provision 的 20m 更宽:客户可能装大体积企业软件包,而这里是【全部 hook 合计】
    # 的上限。构建实例是一次性的,超时的代价只是这次构建失败,不像 lifecycle hook
    # 那样会触发换机。
    timeout = "30m"
  }

  # 4) validate:复刻 Image Builder 的 AssertZeroDownloadBootPath + 跨租户红线。
  #    Image Builder 的 validate 阶段是原生提供的;Packer 没有对应概念,所以显式跑一次。
  #    必须在 scrub 之后 —— 既验组件齐全,也验身份已擦净。检查项都在 assert-image.sh 里,
  #    与下一步的重跑断言共用同一份:加一项检查,两个时机自动都有。
  provisioner "shell" {
    # hook 对账的期望值走环境变量传进断言脚本。为什么在【构建实例上】比而不是在本地:
    # 本地算两次不算独立验证 —— file provisioner 少传一个文件、find 的 glob 语义在两处
    # 漂了、客户在 packer 起来之后又往 hooks/ 丢了一个文件,这些都只有拿"镜像里实际
    # 存在并执行过的那批"去比才能发现。构建侧算(bash find)与镜像侧重算(assert-image.sh)
    # 是两个独立实现,这才是 V-3 那条对账的意义。
    environment_vars = [
      "OC_EXPECT_HOOKS_SHA=${var.hooks_sha}",
      "OC_EXPECT_HOOKS_COUNT=${length(local.hook_files)}",
      "OC_HOOKS_DIR=${local.hooks_dst}",
      "OC_HOOKS_MANIFEST=${local.hooks_manifest}",
    ]
    inline = ["sudo -E bash ${local.assert_dst} post-provision"]
  }


  # 5) validate:复刻 AssertProvisionIsIdempotent。
  #    golden host 只跑 configure,但 plain-AMI 路径会在可能已 provision 过的机器上跑
  #    provision,重烤也会跑两次。在真镜像上重跑一次来证明幂等性,而不是在注释里声称。
  #    刻意【不】带 OC_PROVISION_BAKE:scrub 已经跑过且通过,再跑只是重删空气。
  #
  #    ★ 重跑之后必须重跑【完整】断言,不能只抽查几项。实测 2026-08-13 的真缺陷:此前
  #    这里只查 `command -v firecracker && command -v aws` 加两个泄漏路径,于是"重跑把
  #    Fluent Bit / ADOT / vmlinux 重装了一遍"或"重跑重新生成了 SSH host key"都不会被
  #    发现 —— 而那正是幂等断言存在的理由。现在调 assert-image.sh,与第 4 步同一份检查。
  provisioner "shell" {
    environment_vars = [
      "OC_PROVISION_RECIPE_VERSION=${var.recipe_version}",
      "OC_PROVISION_FLUENT_BIT_INSTALLER=${local.fb_dst}",
      "OC_ASSETS_BUCKET=${var.assets_bucket}",
      "AWS_REGION=${var.region}",
      # post-rerun 阶段也要能重算 hook 摘要 —— 断言脚本两个阶段共用同一份代码,
      # 少传这几个会让 post-rerun 走进"期望值为空"的分支。那个分支必须 fail 而不是
      # 跳过(见 assert-image.sh 里的处理),否则重跑阶段的 hook 断言会静默消失。
      "OC_EXPECT_HOOKS_SHA=${var.hooks_sha}",
      "OC_EXPECT_HOOKS_COUNT=${length(local.hook_files)}",
      "OC_HOOKS_DIR=${local.hooks_dst}",
      "OC_HOOKS_MANIFEST=${local.hooks_manifest}",
    ]
    # dash 不认 pipefail,不显式给 bash 这段就跑不到。
    inline_shebang = "/usr/bin/env bash"
    inline = [<<-IDEMPOTENT
      set -euo pipefail
      before="$(sudo sha256sum /etc/openclaw/.ami-provisioned | cut -d' ' -f1)"
      sudo -E bash ${local.provision_dst}
      after="$(sudo sha256sum /etc/openclaw/.ami-provisioned | cut -d' ' -f1)"
      # 重跑后跑完整断言:组件集合、SSM agent、跨租户红线、SSH host key、cloud-init 态
      # 全部重验。第 4 步验的是"provision 装对了",这一步验的是"重跑没把它弄坏"。
      # -E 是必须的:OC_EXPECT_HOOKS_* 走环境变量传进来,不带 -E 时 sudo 会清掉它们,
      # 断言脚本就看不到期望值(#537 起 hook 对账依赖它们)。
      sudo -E bash ${local.assert_dst} post-rerun
      # provisioned_at 是时间戳,marker 合法地会变;组件集合不能变,上一行已断言。
      echo "provision is idempotent (marker before=$before after=$after; timestamp differs by design)"
    IDEMPOTENT
    ]
    timeout = "20m"
  }


  # 6) 落 manifest:AMI id / region / 构建时间,供 CI 与 assert_parity 对账。
  #
  #    manifest 与 AMI tag 承载同一批坐标但服务不同链路:tag 跟着镜像走,切指针门
  #    (V-5)读它;manifest 能归档、能跨环境传,晋级门(V-6)读它。少一边就有一条链路
  #    读不到坐标,所以 build-golden-ami.sh 的 V-3 两边都查。
  post-processor "manifest" {
    output     = "${path.root}/manifest.json"
    strip_path = true
    custom_data = {
      recipe_version = var.recipe_version
      arch           = local.arch
      built_by       = "packer"

      # #537 V-3 的可追溯性坐标。git_commit 带 -dirty 后缀时 promotable=false ——
      # 晋级门读这两个字段就能拒绝一个"本地试出来的"镜像,不必再去问 git。
      git_commit          = var.git_commit
      packer_template_sha = var.packer_template_sha
      hooks_sha           = var.hooks_sha
      env                 = var.env
      built_at            = var.built_at
      promotable          = var.promotable
      # HCL 的 fileset 枚举出的 hook 数。build-golden-ami.sh 用 bash find 独立枚举一次
      # 并与这个值对账 —— 两种实现的 glob 语义漂了就会不等。字符串化是因为 custom_data
      # 只接受 map(string)。
      hooks_count = "${length(local.hook_files)}"
      # provision-host.sh 的内容摘要 —— 两套工具执行同一份脚本时,这个值必须相同。
      # 是 assert_parity 的锚点。
      provision_sha256 = sha256(file("${path.root}/../userdata/provision-host.sh"))
      fluentbit_sha256 = sha256(file("${path.root}/../edge/fluent-bit/install-fluent-bit.sh"))
    }
  }

  # 7) SSM 参数分发 —— Image Builder 的 distribution 原生提供该步骤,Packer 必须自己做。
  #    ha_edge.py:661 的 resolve_ssm_parameter_at_launch 读这个参数,所以不写就等于
  #    产出了一个没人用的 AMI。--overwrite:参数是"当前 golden AMI"的指针,按定义要覆盖。
  post-processor "shell-local" {
    only = ["amazon-ebs.host"]
    # 跑在执行 packer 的机器上,不是构建实例。仍要显式 bash:macOS 的 /bin/sh 是
    # POSIX 模式的 bash(认 pipefail),但 Debian 系执行机的 /bin/sh 是 dash(不认)。
    # 不写死就变成"在我的 Mac 上能跑,在 CI runner 上炸"。
    inline_shebang = "/usr/bin/env bash"
    inline = [
      "set -euo pipefail",
      "if [ -n '${var.ssm_parameter}' ]; then",
      # 用 grep+sed 而不是 python3/jq 解析 manifest:客户环境只被要求装 packer 与
      # awscli(见 CUSTOMER-GUIDE §1.1)。多一个解释器或 jq 就多一条前置依赖,而这里
      # 要取的只是 "region:ami-xxxx" 里的后半段 —— 用不着 JSON 解析器。
      # tail -1 取最后一条 build:同一个 manifest 可能累积多次构建记录。
      # 提取失败时(格式变化/文件缺失)显式退出,不能把空字符串写进 SSM ——
      # 那会让 ha_edge 的 resolve:ssm 解析到空值,ASG 起不来且原因难查。
      "  AMI_ID=$(grep -o '\"artifact_id\": *\"[^\"]*\"' '${path.root}/manifest.json' | tail -1 | sed 's/.*:\\(ami-[0-9a-f]*\\)\".*/\\1/')",
      "  case \"$AMI_ID\" in ami-*) ;; *) echo \"failed to extract an AMI id from manifest.json (got '$AMI_ID'); refusing to publish\" >&2; exit 1 ;; esac",
      "  echo \"publishing $AMI_ID -> ${var.ssm_parameter}\"",
      # --data-type aws:ec2:image 让 SSM 校验值是一个【真实存在的】AMI id,与
      # Image Builder 的 distribution 一致(host_image.py 的
      # ssm_parameter_configurations data_type="aws:ec2:image")。不给这个类型,
      # 参数就只是字符串:写进一个已注销或打错的 id 不会在此报错,而是等到 ASG 用
      # resolve:ssm 拉它时每台 host 启动失败 —— 症状离原因很远。
      # 校验走 ec2:DescribeImages,执行者身份需带该权限(CUSTOMER-GUIDE §1.8 已列)。
      "  aws ssm put-parameter --name '${var.ssm_parameter}' --type String --data-type aws:ec2:image --value \"$AMI_ID\" --overwrite --region '${var.region}'",
      # ★ 校验是【异步】的,put-parameter 无论值好坏都返回成功(实测 2026-08-13):
      #   已有好值 + 写坏值 → 坏版本静默丢弃,读回来还是旧的好值;
      #   全新参数 + 写坏值 → 参数根本不存在,GetParameter 报 ParameterNotFound。
      # 两种情况下退出码都是 0,所以【不能靠 put 的退出码判断发布成功】。必须回读。
      #
      # 但也【不能只读一次】:校验既然是异步的,一次立即回读可能落在校验完成之前,
      # 于是一个合法的发布被误判成失败(假红,反过来的错)。限时轮询到新值出现为止:
      # 30 次 × 2s = 60s 上限,足够覆盖 DescribeImages 的校验延迟,又不会在真失败时
      # 挂住构建。轮询结束仍不匹配才 fail-loud —— 否则构建会宣称"已发布",而 ASG
      # 拉到的是上一版镜像(或拉不到),那是最难查的一类不一致。
      "  _got=\"\"",
      "  for _i in $(seq 1 30); do",
      "    _got=$(aws ssm get-parameter --name '${var.ssm_parameter}' --region '${var.region}' --query 'Parameter.Value' --output text 2>/dev/null || true)",
      "    if [ \"$_got\" = \"$AMI_ID\" ]; then break; fi",
      "    sleep 2",
      "  done",
      "  if [ \"$_got\" != \"$AMI_ID\" ]; then echo \"SSM publish did not take effect within 60s: parameter holds '$_got', expected '$AMI_ID'. aws:ec2:image validation is asynchronous and discards a rejected version silently; a non-existent or deregistered AMI id looks exactly like this.\" >&2; exit 1; fi",
      "  echo \"verified after polling: ${var.ssm_parameter} = $_got\"",
      "else",
      "  echo 'ssm_parameter 未设 —— 只产出 AMI,不发布指针(ha_edge 的 resolve:ssm 读不到新镜像)'",
      "fi",
    ]
  }
}
