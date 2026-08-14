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
#   packer build    -var-file=deploy/packer/apse1.pkrvars.hcl deploy/packer
#
# 前置:
#   - 调用者身份需能 RunInstances / CreateImage / CreateTags / PutParameter
#   - assets_bucket 里要有 deployment/binaries/firecracker/<ver>/(#435 的镜像前缀);
#     没有时 provision-host.sh 会回落公网源,产出的镜像仍然可用但违反 #435 的验收

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

variable "arch" {
  type        = string
  default     = "arm64"
  description = "arm64 | amd64 —— 决定 parent AMI 与构建机型"
  # error_message 必须英文整句(大写开头、句号结尾)—— Packer 自己校验这条格式。
  validation {
    condition     = contains(["arm64", "amd64"], var.arch)
    error_message = "The arch variable must be either arm64 or amd64."
  }
}

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
    openclaw-assets-<account><gsuffix>。provision-host.sh 从
    deployment/binaries/firecracker/ 拉 FC 二进制(#435),构建实例的 instance profile
    需有该前缀的 s3:GetObject。
  EOT
  # 模板里 assets_bucket 是占位符 openclaw-assets-<ACCOUNT_ID>。不加这条校验的话
  # packer validate 会放过它(它只是个字符串),客户带着占位符直接 build,要等到
  # provision 第 3 步从 S3 拉 Firecracker 时才失败 —— 那时已经起了一台构建实例、
  # 跑了两分钟 apt。在 validate 阶段就拦住,反馈立刻可得。
  validation {
    condition     = !can(regex("<[A-Z_]+>", var.assets_bucket))
    error_message = "The assets_bucket variable still contains a placeholder. Replace <ACCOUNT_ID> with the 12-digit deployment account id."
  }
}

variable "instance_type" {
  type        = string
  default     = ""
  description = "留空 = 按 arch 选 c7g.large / c7i.large。构建实例是一次性的,不用 metal"
}

variable "root_volume_gb" {
  type        = number
  default     = 20
  description = "对应 config.yml host.root_volume_gb;与 Image Builder recipe 的 volume_size 同值"
}

variable "iam_instance_profile" {
  type        = string
  default     = ""
  description = <<-EOT
    构建实例的 instance profile 名。需要:S3 读 assets_bucket 的 firecracker 前缀 +
    SSM(可选,便于排障)。留空则 packer 用调用者凭据但机器上没有角色 ——
    provision-host.sh 从 S3 拉 FC 会失败并回落公网,违反 #435,故生产构建必须给。
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

# ── 客户自定义扩展 ───────────────────────────────────────────────────────────

variable "custom_script" {
  type        = string
  default     = ""
  description = <<-EOT
    客户自定义脚本的本地路径(相对本目录或绝对路径)。留空则跳过该阶段。

    执行时机:在 provision-host.sh 之后、两条 validate 断言之前,以 root 运行一次。
    这个位置是被约束死的,不是任选的:
      - 不能放在 provision 之前 —— 组件(firecracker/awscli/ADOT)还没装,脚本无从依赖;
      - 不能放在断言之后 —— 断言正是用来兜住自定义脚本引入的身份泄漏的,放后面就失去防线;
      - scrub 在 provision-host.sh 内部(§7)已经跑完,所以自定义脚本【写入的任何
        per-host 状态都不会再被清理】,会原样进镜像并被整个机队共享。

    因此自定义脚本必须只做与机器无关的事:装包、放配置模板、加监控 agent、调内核参数。
    不得生成主机密钥、写 /etc/platform.env、留凭据或留下 cloud-init 实例态 ——
    下一阶段的断言会检出这些并让构建失败(fail-closed,不会产出带泄漏的 AMI)。
  EOT
}

variable "custom_script_env" {
  type        = map(string)
  default     = {}
  description = <<-EOT
    传给自定义脚本的环境变量。用于把参数从 pkrvars 传进去,而不是让客户改脚本本体。

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
  name   = var.arch == "arm64" ? "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id" : "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
  region = var.region
}

locals {

  # 构建机型:与 host_image.py:369 的默认同源。构建是一次性动作,不需要 metal。
  build_instance = var.instance_type != "" ? var.instance_type : (
    var.arch == "arm64" ? "c7g.large" : "c7i.large"
  )

  ami_name = "openclaw-host-${var.recipe_version}-${var.arch}${var.gsuffix}-{{timestamp}}"

  # 脚本在构建实例上的落点。与 Image Builder component 的 S3Download destination 一致
  # (/opt/openclaw/...),因为 validate 阶段的 AssertProvisionIsIdempotent 会按这个
  # 绝对路径再跑一次 provision —— 路径不一致那条断言就跑不了。
  provision_dst = "/opt/openclaw/provision-host.sh"
  fb_dst        = "/opt/openclaw/install-fluent-bit.sh"

  # 自定义脚本落在 /opt/openclaw/custom/ 而不是 /tmp:留在镜像里可审计
  # (起一台 host 就能看到这批镜像装了什么客户内容),/tmp 在部分环境会被开机清空。
  custom_dst = "/opt/openclaw/custom/customize.sh"

  # 空字符串在 packer 的 provisioner 里不能作为 "跳过" 的开关(file provisioner 会
  # 直接报 source 不存在),所以用 count 惯用法:only/except 无法表达条件,
  # dynamic block 在 provisioner 上不可用 —— 用 shell 内部判断反而更简单可读。
  has_custom = var.custom_script != ""
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
  ami_description = "OpenClaw host golden AMI (recipe ${var.recipe_version}, ${var.arch}) - Firecracker + jailer + awscli + ADOT + Fluent Bit baked; boot path installs nothing"

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

  tags = {
    Name            = "openclaw-host-golden${var.gsuffix}"
    Project         = "openclaw"
    Role            = "metal-host"
    RecipeVersion   = var.recipe_version
    Arch            = var.arch
    BuiltBy         = "packer"
    ProvisionSource = "deploy/userdata/provision-host.sh"
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
  provisioner "shell" {
    inline = [
      "sudo install -o root -g root -m 0755 /tmp/provision-host.sh ${local.provision_dst}",
      "sudo install -o root -g root -m 0755 /tmp/install-fluent-bit.sh ${local.fb_dst}",
      "rm -f /tmp/provision-host.sh /tmp/install-fluent-bit.sh",
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
      "AWS_REGION=${var.region}",
    ]
    # -E 传环境变量给 root。expect_disconnect=false:provision 不重启机器。
    inline = ["sudo -E bash ${local.provision_dst}"]
    # provision 最慢的一段是 ADOT deb(~100MB)+ rootfs 无关的 apt。900s 覆盖慢 CDN,
    # 不是给重试留窗口 —— 脚本内部自己有重试。
    timeout = "20m"
  }

  # 3) 客户自定义阶段。这里的位置不是任选的,三个边界把它夹死在这一格:
  #    - provision 之后:组件(firecracker/awscli/ADOT/Fluent Bit)已装好,自定义脚本
  #      才有东西可依赖;
  #    - scrub 之后(scrub 在 provision-host.sh §7 内部):所以自定义脚本写入的任何
  #      per-host 状态【不会再被清理】,会原样进镜像并被整个机队共享 —— 这正是下一
  #      条断言存在的理由;
  #    - 断言之前:断言是自定义脚本的防线。放到断言之后,客户脚本留下的主机密钥、
  #      platform.env、cloud-init 实例态就没人检出了,fail-closed 属性会失效。
  #
  #    custom_script 留空时执行 customize.sh.default(无操作)。用无操作默认值而不是
  #    条件分支:packer 的 provisioner 不支持 dynamic block,only/except 只能选 source,
  #    而 file provisioner 拿到空路径会直接报错。同一条代码路径比两套分支可靠。
  provisioner "file" {
    source      = var.custom_script != "" ? "${path.root}/${var.custom_script}" : "${path.root}/customize.sh.default"
    destination = "/tmp/customize.sh"
  }
  provisioner "shell" {
    inline = [
      "sudo install -d -m 0755 /opt/openclaw/custom",
      "sudo install -o root -g root -m 0755 /tmp/customize.sh ${local.custom_dst}",
      "rm -f /tmp/customize.sh",
    ]
  }
  provisioner "shell" {
    # 客户参数经 environment_vars 传入,避免客户改脚本本体。密钥不走这里 ——
    # 值会出现在构建日志中。
    environment_vars = [for k, v in var.custom_script_env : "${k}=${v}"]
    # -E 传环境变量给 root。与 provision 同样的调用形式,便于客户对照。
    inline = ["sudo -E bash ${local.custom_dst}"]
    # 比 provision 的 20m 更宽:客户可能装大体积企业软件包。构建实例是一次性的,
    # 超时的代价只是这次构建失败,不像 lifecycle hook 那样会触发换机。
    timeout = "30m"
  }

  # 4) validate:复刻 Image Builder 的 AssertZeroDownloadBootPath。
  #    Image Builder 的 validate 阶段是原生提供的;Packer 没有对应概念,所以显式写成
  #    一个 provisioner。它必须在 scrub 之后跑 —— 既验组件齐全,也验身份已擦净。
  provisioner "shell" {
    # inline_shebang 必须显式给 bash。packer 的默认是 `/bin/sh -e`,而 Ubuntu 的
    # /bin/sh 是 dash —— dash 没有 pipefail,下面第一行 `set -euo pipefail` 会直接
    # 报 "Illegal option -o pipefail" 并让整个 provisioner 退出。实测 2026-08-12:
    # 断言主体完全未执行到,build 却已经跑完了 provision,失败点看起来像在别处。
    # `packer validate` 无法检出(它不执行脚本),只有实际执行构建才会暴露。
    inline_shebang = "/usr/bin/env bash"
    inline = [<<-ASSERT
      set -euo pipefail
      # 验收判据:golden host 必须能在【零下载】的前提下起到 running。
      # 下面每个缺失项 = boot path 要补做的一次下载,所以在这里一次性断言,
      # 而不是等 lifecycle hook 已经在计时的 host 上才发现。
      fail=0
      for b in aws firecracker jailer; do
        command -v "$b" >/dev/null 2>&1 || { echo "MISSING binary: $b"; fail=1; }
      done
      # Fluent Bit 官方包装到 /opt/fluent-bit/bin,不在 PATH 上(真机 2026-08-05)。
      # 按 PATH 探会在装好的镜像上误报缺失,而 installer 里同样的错探针会让每次
      # golden boot 都重新联网装一遍。断言打包位置。
      for f in /opt/fluent-bit/bin/fluent-bit \
               /opt/openclaw/baked/vmlinux \
               /etc/openclaw/.ami-provisioned; do
        [ -s "$f" ] || { echo "MISSING file: $f"; fail=1; }
      done
      dpkg -s aws-otel-collector >/dev/null 2>&1 || { echo "MISSING pkg: aws-otel-collector"; fail=1; }
      # SSM agent 必须留在镜像里。控制面靠 SSM 驱动 host(launch-vm/stop-vm 批量、
      # host-agent 命令),没有 agent 的 host 从控制面看就是不可达 —— 它会注册成
      # active 然后每条指令都超时。Image Builder 侧必须显式声明
      # uninstall_after_build=False(host_image.py 的 AdditionalInstanceConfiguration),
      # 因为它默认会卸载;Packer 走 SSH 不碰 agent,所以这里【断言】而不是配置,
      # 免得"Packer 不会卸载"这个前提在换 communicator 或加清理步骤后静默失效。
      systemctl list-unit-files 'snap.amazon-ssm-agent.*' 'amazon-ssm-agent.*' 2>/dev/null \
        | grep -q 'ssm-agent' || { echo "MISSING: amazon-ssm-agent unit not present in image"; fail=1; }
      # 跨租户红线。host_vm_key 是 per-host 的,公钥半边注进每个租户 microVM,
      # 一把被预置于镜像的私钥 = 任意 host 能 SSH 进任意 host 上任意租户的 microVM。
      # provision 的 scrub 已经查过;这里再查一遍,因为 validate 跑在所有 build 步骤
      # 之后 —— 能抓到"后来新增的步骤又造了一把 key"。
      for leak in /etc/openclaw/host_vm_key /etc/openclaw/host_vm_key.pub /etc/platform.env \
                  /data/agentcore.env /etc/openclaw/host_vm_key.instance; do
        [ ! -e "$leak" ] || { echo "LEAK: $leak present in image"; fail=1; }
      done
      # SSH host key。scrub 删了它们(`rm -f /etc/ssh/ssh_host_*`),但在自定义阶段
      # 引入之前没有断言兜住 —— 而客户脚本重装 openssh-server、或跑
      # `dpkg-reconfigure openssh-server`,都会重新生成一对。预置于镜像后整个机队共享
      # 同一把 host key:任何能起一台 host 的人都能冒充其余每一台,MITM 检测失效。
      # 正常路径下 cloud-init 在首次启动时按实例重新生成,所以镜像里应当一把都没有。
      # 用 find 而不是 `ls ssh_host_*`:glob 无匹配时 ls 返回非零,叠加 set -e 与
      # pipefail 会让整个断言脚本【在这一行就退出】—— 后面的检查全部不执行,而 packer
      # 只报 "Script exited with non-zero exit status: 2",看不出是断言自己死了。
      # 实测 2026-08-12:这行的第一版正是如此,把本该兜住自定义脚本的防线整条废掉。
      # find 在无匹配时返回 0 且输出为空,是这里唯一安全的写法。
      _sshkeys="$(find /etc/ssh -maxdepth 1 -name 'ssh_host_*' -print 2>/dev/null | tr '\n' ' ')"
      [ -z "$_sshkeys" ] || { echo "LEAK: SSH host keys present in image: $_sshkeys"; fail=1; }
      # cloud-init 实例态残留会让新实例复用旧 instance-id 的判定,首启逻辑被跳过。
      [ ! -d /var/lib/cloud/instances ] || [ -z "$(ls -A /var/lib/cloud/instances 2>/dev/null)" ] \
        || { echo "LEAK: cloud-init instance state present"; fail=1; }
      # 自定义阶段的产物本身要可审计:镜像里必须留着实际执行过的那份脚本,
      # 这样起一台 host 就能核对这批镜像装了什么客户内容。
      [ -s /opt/openclaw/custom/customize.sh ] \
        || { echo "MISSING file: /opt/openclaw/custom/customize.sh"; fail=1; }
      [ "$fail" = 0 ] || { echo "golden AMI validation failed"; exit 1; }
      echo "golden AMI validated: components present, no host identity"
    ASSERT
    ]
  }

  # 4) validate:复刻 AssertProvisionIsIdempotent。
  #    golden host 只跑 configure,但 plain-AMI 路径会在可能已 provision 过的机器上
  #    跑 provision,重新构建也会跑两次。在真镜像上重跑一次来证明幂等性,而不是在注释里
  #    声称。刻意【不】带 OC_PROVISION_BAKE —— scrub 已经跑过且通过,再跑只是重删空气。
  provisioner "shell" {
    environment_vars = [
      "OC_PROVISION_RECIPE_VERSION=${var.recipe_version}",
      "OC_PROVISION_FLUENT_BIT_INSTALLER=${local.fb_dst}",
      "OC_ASSETS_BUCKET=${var.assets_bucket}",
      "AWS_REGION=${var.region}",
    ]
    # 同上:dash 不认 pipefail,不显式给 bash 这段断言就跑不到。
    inline_shebang = "/usr/bin/env bash"
    inline = [<<-IDEMPOTENT
      set -euo pipefail
      before="$(sudo sha256sum /etc/openclaw/.ami-provisioned | cut -d' ' -f1)"
      sudo -E bash ${local.provision_dst}
      after="$(sudo sha256sum /etc/openclaw/.ami-provisioned | cut -d' ' -f1)"
      # provisioned_at 是时间戳,marker 合法地会变。不能变的是组件集合 ——
      # 上一步已断言过,而"重跑重装了什么"会破坏它。
      command -v firecracker >/dev/null && command -v aws >/dev/null
      # 重跑不得重新引入身份文件(非 bake 模式不跑 scrub,所以这条是真检查)。
      for leak in /etc/openclaw/host_vm_key /etc/platform.env; do
        [ ! -e "$leak" ] || { echo "LEAK after re-run: $leak"; exit 1; }
      done
      echo "provision is idempotent (marker before=$before after=$after; timestamp differs by design)"
    IDEMPOTENT
    ]
    timeout = "20m"
  }

  # 5) 落 manifest:AMI id / region / 构建时间,供 CI 与 assert_parity 对账。
  post-processor "manifest" {
    output     = "${path.root}/manifest.json"
    strip_path = true
    custom_data = {
      recipe_version = var.recipe_version
      arch           = var.arch
      built_by       = "packer"
      # provision-host.sh 的内容摘要 —— 两套工具执行同一份脚本时,这个值必须相同。
      # 是 assert_parity 的锚点。
      provision_sha256 = sha256(file("${path.root}/../userdata/provision-host.sh"))
      fluentbit_sha256 = sha256(file("${path.root}/../edge/fluent-bit/install-fluent-bit.sh"))
    }
  }

  # 6) SSM 参数分发 —— Image Builder 的 distribution 原生提供该步骤,Packer 必须自己做。
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
      # 两种情况下退出码都是 0,所以【不能靠 put 的退出码判断发布成功】。回读一次,
      # 确认参数里就是我们刚发布的那个 AMI id,否则 fail-loud —— 否则构建会宣称
      # "已发布",而 ASG 拉到的是上一版镜像(或拉不到),那是最难查的一类不一致。
      "  _got=$(aws ssm get-parameter --name '${var.ssm_parameter}' --region '${var.region}' --query 'Parameter.Value' --output text 2>/dev/null || true)",
      "  if [ \"$_got\" != \"$AMI_ID\" ]; then echo \"SSM publish did not take effect: parameter holds '$_got', expected '$AMI_ID' (data-type validation rejects values asynchronously)\" >&2; exit 1; fi",
      "  echo \"verified: ${var.ssm_parameter} = $_got\"",
      "else",
      "  echo 'ssm_parameter 未设 —— 只产出 AMI,不发布指针(ha_edge 的 resolve:ssm 读不到新镜像)'",
      "fi",
    ]
  }
}
