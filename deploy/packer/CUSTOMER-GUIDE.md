# 使用 Packer 构建 host golden AMI — 操作手册

> **⚠️ 尚未可交付。** §3（预置 Firecracker 二进制）与 §1.8 中的 S3 权限依赖 issue #435
> 落地，在其合并前这两处配置不起作用，本文档"启动过程不访问任何第三方源"的表述也不成立。
> 交付前请先确认 #435 已合并，并删除本提示。详见 `README.md` 的前置依赖一节。

适用对象：自行运维 ClawPool 的客户。操作流程为：配置网络参数 → 执行构建命令 →
更新 LaunchTemplate 的 AMI id。全部操作在客户自有账号内完成，无需联系我们。

构建实例为一次性资源，构建完成后自动终止。实测数据（ap-southeast-1、`c7g.large`、
arm64）：**总耗时约 14 分钟**，其中组件安装约 1 分钟，其余为 EBS 快照生成等待时间。
单次构建费用约 $0.02。

---

## 构建 golden AMI 的目的

未使用 golden AMI 时，每台 host 在启动过程中需联网安装 Firecracker、awscli、
ADOT collector、Fluent Bit，合计约 200MB。任一次网络抖动或上游返回 404 都会导致该
实例的 lifecycle hook 超时，ASG 判定 ABANDON 并替换实例，而替换后的实例访问同一
上游源，故障重现 —— 形成不收敛的循环。

使用 golden AMI 时，组件已预置于镜像内，host 启动过程**不访问任何第三方源**，仅执行
每实例唯一的操作：生成本机密钥、挂载数据卷、注册至控制面。

启动耗时实测对比（ap-southeast-1、`m7g.metal`、同子网、两台实例）：

| 路径 | init 总耗时 | 其中组件安装 |
|---|---|---|
| plain AMI（启动时安装组件） | 3 分 06 秒 | 约 3 分钟 |
| **golden AMI** | **2 分 46 秒** | **0 秒（跳过）** |

两者仅相差 20 秒，原因是**两条路径均需从 S3 获取 rootfs 镜像**，该步骤耗时 2 分 17 秒
且与 golden AMI 无关。

因此 golden AMI 的核心价值不在于缩短启动时间，而在于**消除启动路径上的第三方依赖**：
未使用 golden AMI 时，每台 host 启动均需访问 GitHub、apt 源与 ADOT 的 CDN，其中任一
上游抖动或限流都会导致该实例 ABANDON。使用 golden AMI 后，启动过程仅依赖客户自有的
S3 存储桶。

---

## §1 环境准备

构建可在任意能访问 AWS API 的机器上执行（本地工作机、跳板机、CI runner）。该机器不需要
位于目标 VPC 内 —— Packer 通过 API 创建构建实例，仅需与其建立一条 SSH 通道（见 §2.1）。

### §1.1 需要安装的工具

| 工具 | 版本要求 | 用途 |
|---|---|---|
| Packer | ≥ 1.9 | 执行镜像构建 |
| AWS CLI | v2 | 凭据配置、参数与产物核对 |
| packer-plugin-amazon | ≥ 1.3 | Packer 的 EC2 构建器 |

以上三项即为全部前置工具。构建过程不依赖 Python、jq 或其他解释器 —— 模板内部仅使用
`grep`、`sed` 等 POSIX 工具与 AWS CLI。

本手册的实测环境为 Packer 1.15.2 + AWS CLI 2.34.37 + packer-plugin-amazon 1.8.2。

若 §2.1 选用 `ssh_interface = "session_manager"` 连接构建实例，则额外需要
`session-manager-plugin`（安装方式见 §1.4）。选用 `public_ip` 或 `private_ip` 时不需要。

### §1.2 安装 Packer

**macOS（Homebrew）**

```bash
brew tap hashicorp/tap
brew install hashicorp/tap/packer
```

**Amazon Linux 2023 / RHEL / CentOS**

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo
sudo dnf install -y packer
```

RHEL 与 CentOS 将上述 URL 中的 `AmazonLinux` 替换为 `RHEL`。

**Ubuntu / Debian**

```bash
wget -O - https://apt.releases.hashicorp.com/gpg | sudo gpg --batch --yes --dearmor \
  -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt-get update && sudo apt-get install -y packer
```

`gpg` 需传 `--batch --yes`：不传时若 keyring 已存在会弹出覆盖确认并阻塞。

**二进制直装（无包管理器权限时）**

```bash
PACKER_VERSION=1.16.0
# 按执行机架构选择：arm64 或 amd64
ARCH=$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')
OS=$(uname -s | tr '[:upper:]' '[:lower:]')

curl -fsSLO "https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_${OS}_${ARCH}.zip"
curl -fsSLO "https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_SHA256SUMS"

# 校验必须通过，否则不得继续。
# macOS 默认只有 shasum，多数 Linux 发行版只有 sha256sum，两者的 -c 接受同一份
# SHA256SUMS 格式。用函数而不是把命令存进变量再展开：zsh（macOS 默认 shell）
# 不对未加引号的变量做分词，会把整串当成一个命令名并报 command not found。
sumcheck() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -c -; else shasum -a 256 -c -; fi
}
grep " packer_${PACKER_VERSION}_${OS}_${ARCH}.zip\$" "packer_${PACKER_VERSION}_SHA256SUMS" | sumcheck

unzip -o "packer_${PACKER_VERSION}_${OS}_${ARCH}.zip"
sudo install -o root -g root -m 0755 packer /usr/local/bin/packer
```

验证：

```bash
packer version        # 需 ≥ 1.9
```

### §1.3 安装 AWS CLI v2

**macOS**

```bash
curl -fsSL "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o /tmp/AWSCLIV2.pkg
sudo installer -pkg /tmp/AWSCLIV2.pkg -target /
```

**Linux**

```bash
ARCH=$(uname -m)      # x86_64 或 aarch64，URL 直接使用该值
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" -o /tmp/awscliv2.zip
unzip -q -o /tmp/awscliv2.zip -d /tmp
sudo /tmp/aws/install --update
```

验证：

```bash
aws --version         # 需为 aws-cli/2.x
```

### §1.4 安装 session-manager-plugin（仅 session_manager 方案）

不属于 §1.1 的三项前置工具。仅当 §2.1 选用 `ssh_interface = "session_manager"` 时需要
安装，未安装时 Packer 会在 `Waiting for SSH` 阶段失败。选用 `public_ip` 或
`private_ip` 时跳过本节。

**macOS**

```bash
brew install --cask session-manager-plugin
```

**Amazon Linux / RHEL**

```bash
case "$(uname -m)" in
  x86_64)  SSM_DIR=linux_64bit ;;
  aarch64) SSM_DIR=linux_arm64 ;;
  *) echo "unsupported architecture" >&2; exit 1 ;;
esac
sudo dnf install -y \
  "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/${SSM_DIR}/session-manager-plugin.rpm"
```

**Ubuntu / Debian**

```bash
# AWS 对两个架构使用了不同的命名：amd64 是 ubuntu_64bit，arm64 是 ubuntu_arm64。
# 直接套 dpkg --print-architecture 的输出会在 amd64 上得到不存在的路径（403）。
case "$(dpkg --print-architecture)" in
  amd64) SSM_DIR=ubuntu_64bit ;;
  arm64) SSM_DIR=ubuntu_arm64 ;;
  *) echo "unsupported architecture" >&2; exit 1 ;;
esac
curl -fsSL -o /tmp/session-manager-plugin.deb \
  "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/${SSM_DIR}/session-manager-plugin.deb"
sudo dpkg -i /tmp/session-manager-plugin.deb
```

验证：

```bash
session-manager-plugin --version
```

官方安装文档：
<https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html>

### §1.5 安装 Packer 的 amazon 插件

```bash
packer init deploy/packer
```

该命令读取 `host-golden.pkr.hcl` 的 `required_plugins` 声明并自动下载
`packer-plugin-amazon`（本手册实测版本 1.8.2），安装至 `~/.packer.d/plugins/`。

若执行机无公网访问权限，需在有网络的机器上执行 `packer plugins install
github.com/hashicorp/amazon`，再将 `~/.packer.d/plugins/` 目录复制到执行机。

验证：

```bash
packer plugins installed
```

### §1.6 配置 AWS 凭据

```bash
# 方式一：SSO（推荐，凭据短期有效）
aws configure sso

# 方式二：静态密钥
aws configure

# 方式三：EC2 / ECS 上执行时无需配置，直接使用实例角色
```

验证凭据生效并确认账号正确：

```bash
aws sts get-caller-identity
```

Packer 使用与 AWS CLI 相同的凭据链（环境变量 → 配置文件 → 实例角色），无需额外配置。
使用具名 profile 时通过环境变量传入：

```bash
export AWS_PROFILE=your-profile
export AWS_REGION=ap-southeast-1
```

### §1.7 获取本仓库

```bash
# REPO_URL 替换为我们提供的仓库地址
git clone "$REPO_URL" bb-on-firecracker-dev
cd bb-on-firecracker-dev
```

构建所需文件均在 `deploy/packer/` 与 `deploy/userdata/` 下。Packer 模板通过相对路径
引用 `deploy/userdata/provision-host.sh`，因此**须保留仓库目录结构**，不可仅复制
`deploy/packer/` 单个目录。

### §1.8 执行者所需的 AWS 权限

构建实例自身所需权限见 §2.2，两者是不同主体。

| 权限 | 用途 |
|---|---|
| `ec2:RunInstances` `ec2:TerminateInstances` | 创建与终止一次性构建实例 |
| `ec2:CreateImage` `ec2:CreateTags` `ec2:CreateSnapshot` | 生成 AMI |
| `ec2:DescribeImages` `ec2:DescribeInstances` `ec2:DescribeSubnets` | Packer 轮询资源状态 |
| `ec2:CreateSecurityGroup` `ec2:DeleteSecurityGroup` | 临时安全组（`security_group_id` 留空时需要）|
| `ec2:CreateKeyPair` `ec2:DeleteKeyPair` | 临时 SSH 密钥对，构建完成后删除 |
| `ec2:ModifyImageAttribute` | 写入 AMI 描述（在 AMI 生成后调用）|
| `iam:PassRole`（作用于构建实例的角色） | 将 instance profile 关联至构建实例 |
| `ssm:GetParameter`（作用于 `/aws/service/canonical/*`） | 解析 Ubuntu 24.04 基础镜像 |
| `ssm:StartSession` `ssm:TerminateSession`（可选） | 仅 `ssh_interface = "session_manager"` 时需要 |

最小权限策略示例（将 `<ACCOUNT>` 替换为实际账号 id）：

```bash
cat > /tmp/packer-executor-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BuildInstanceLifecycle",
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances", "ec2:TerminateInstances", "ec2:StopInstances",
        "ec2:CreateTags", "ec2:CreateKeyPair", "ec2:DeleteKeyPair",
        "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress",
        "ec2:DescribeInstances", "ec2:DescribeInstanceStatus",
        "ec2:DescribeSubnets", "ec2:DescribeVpcs", "ec2:DescribeSecurityGroups",
        "ec2:DescribeRegions", "ec2:DescribeImages", "ec2:DescribeKeyPairs",
        "ec2:DescribeSnapshots", "ec2:DescribeVolumes",
        "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ImageCreation",
      "Effect": "Allow",
      "Action": ["ec2:CreateImage", "ec2:CreateSnapshot", "ec2:ModifyImageAttribute"],
      "Resource": "*"
    },
    {
      "Sid": "PassBuildInstanceRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::<ACCOUNT>:role/openclaw-packer-builder"
    },
    {
      "Sid": "ResolveParentImage",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:*::parameter/aws/service/canonical/*"
    },
    {
      "Sid": "SessionManagerAccess",
      "Effect": "Allow",
      "Action": ["ssm:StartSession", "ssm:TerminateSession", "ssm:DescribeInstanceInformation"],
      "Resource": "*"
    }
  ]
}
EOF
```

`SessionManagerAccess` 段仅在 `ssh_interface = "session_manager"` 时需要，
选用其他连接方式时可删除。

### §1.9 环境检查清单

开始配置参数前逐条确认：

```bash
packer version                      # ≥ 1.9
aws --version                       # aws-cli/2.x
packer plugins installed            # 含 packer-plugin-amazon ≥ 1.3
aws sts get-caller-identity         # 返回预期账号 id

# 仅 session_manager 方案需要（见 §1.4）：
session-manager-plugin --version
```

---

## §2 参数配置

复制参数模板：

```bash
cp deploy/packer/apse1.pkrvars.hcl deploy/packer/my.pkrvars.hcl
```

### §2.1 网络参数（**必填项，最易出错**）

构建实例需满足两项网络条件：**具备出站网络访问能力**、**可被 Packer 通过 SSH 访问**。
三种典型部署场景：

| 场景 | `subnet_id` | `associate_public_ip` | `ssh_interface` |
|---|---|---|---|
| **公有子网**（配置 IGW） | 公有子网 id | `true` | `"public_ip"` |
| **私有子网 + NAT**，执行机位于同 VPC 或可通过 VPN 访问 | 私有子网 id | `false` | `"private_ip"` |
| **私有子网 + NAT**，网络策略严格（禁止入站 22、不分配公网 IP） | 私有子网 id | `false` | `"session_manager"` |

```hcl
region    = "ap-southeast-1"
arch      = "arm64"              # Graviton metal 选用 arm64；Intel 选用 amd64
vpc_id    = "vpc-xxxxxxxx"
subnet_id = "subnet-xxxxxxxx"

# 留空时 Packer 将创建临时安全组（仅放行执行机当前公网 IP 的 22 端口），
# 构建完成后自动删除。若账号策略禁止创建安全组，需显式指定一个：
# 出站全放行 + 入站 22 放行 Packer 执行机。
security_group_id = ""

associate_public_ip = true
ssh_interface       = "public_ip"
```

**子网必须具备出站网络访问能力。** 无论通过 IGW（公有子网）还是 NAT（私有子网），
provision 阶段需安装 apt 包并下载 awscli、ADOT、Fluent Bit。若指定隔离子网，
Packer 将在等待 SSH 阶段持续阻塞直至超时。

选用 `session_manager` 时，`iam_instance_profile` 必须附加
`AmazonSSMManagedInstanceCore` 策略，且子网需能访问 SSM 端点（通过 NAT 或
VPC Endpoint）。

### §2.2 构建实例的 IAM instance profile（**必填项**）

```hcl
iam_instance_profile = "openclaw-packer-builder"
```

构建实例需读取客户 assets 存储桶内的 Firecracker 二进制（见 §3）。创建最小权限角色：

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="openclaw-assets-${ACCOUNT}"

cat > /tmp/trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

cat > /tmp/policy.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:GetObject",
 "Resource":"arn:aws:s3:::${BUCKET}/deployment/binaries/firecracker/*"}]}
EOF

aws iam create-role --role-name openclaw-packer-builder \
  --assume-role-policy-document file:///tmp/trust.json
aws iam put-role-policy --role-name openclaw-packer-builder \
  --policy-name read-firecracker-mirror --policy-document file:///tmp/policy.json
aws iam create-instance-profile --instance-profile-name openclaw-packer-builder
aws iam add-role-to-instance-profile --instance-profile-name openclaw-packer-builder \
  --role-name openclaw-packer-builder

# 选用 ssh_interface = "session_manager" 时追加此策略：
# aws iam attach-role-policy --role-name openclaw-packer-builder \
#   --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```

**不建议复用 `openclaw-host-profile`。** 该角色面向生产 metal host，附带 DynamoDB
写入、SSM 命令执行等宽泛权限，一次性构建实例不应持有此类权限。

### §2.3 其余参数

```hcl
assets_bucket  = "openclaw-assets-<ACCOUNT_ID>"   # 必填，见下方说明与 §3
recipe_version = "1.0.0"    # 镜像配方版本；组件构成变更时递增，将写入镜像 marker
root_volume_gb = 20
gsuffix        = ""         # 多环境后缀，单环境部署留空

custom_script     = ""      # 自定义构建步骤，见 §4；留空则跳过该阶段
custom_script_env = {}      # 传给自定义脚本的环境变量
```

`assets_bucket` 在模板中是占位符，**必须替换为实际账号 id**：

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
sed -i.bak "s/<ACCOUNT_ID>/${ACCOUNT}/" deploy/packer/my.pkrvars.hcl
grep assets_bucket deploy/packer/my.pkrvars.hcl
```

未替换时 `packer validate` 会直接拒绝并提示该项，不会等到构建中途才失败。

---

## §3 预置 Firecracker 二进制（**首次构建前必须执行一次**）

`provision-host.sh` 仅从**客户自有 S3 存储桶**获取 Firecracker，不访问 GitHub。

设计原因：10 万规模的 scale-out 会导致所有 host 同时访问 GitHub releases CDN 并触发
其速率限制，tarball 返回 429，host 无法安装 Firecracker，lifecycle hook 超时后
ASG 判定 ABANDON 并替换实例，而替换实例访问同一受限源 —— 该循环不收敛。因此启动路径
上不保留任何第三方源，且**有意未实现 GitHub 回落机制**：若存在回落，"GitHub 不可达时
host 仍可正常启动"这项验收将永远通过、无法暴露真实依赖。

构建阶段执行的是同一个脚本，因此构建前该 S3 前缀必须已包含所需制品。两种方式：

### 方案 A：执行镜像同步脚本（推荐）

```bash
engineering/tooling/operations/deployment/mirror-firecracker.sh <region> [aws-profile]
```

该脚本从 `deploy/userdata/provision-host.sh` 解析版本号与摘要（不重复硬编码 —— 两处
各维护一份必然产生漂移），默认同步**脚本中已声明摘要的全部架构**，逐个校验摘要后上传。

```bash
# 预演模式：仅输出将执行的操作，不实际上传
OC_DRY_RUN=1 engineering/tooling/operations/deployment/mirror-firecracker.sh ap-southeast-1

# 显式收窄至单一架构（默认同步全部架构，通常无需指定）
OC_ARCHES="aarch64" engineering/tooling/operations/deployment/mirror-firecracker.sh ap-southeast-1
```

该脚本幂等：S3 中已存在且摘要一致的制品将被跳过，可随时重复执行。

执行后验证：

```bash
aws s3 ls "s3://openclaw-assets-${ACCOUNT}/deployment/binaries/firecracker/" --recursive
```

### 方案 B：手工上传（脚本无法执行的环境）

```bash
V=v1.15.1                        # 需与 provision-host.sh 的 FC_VER 一致
A=aarch64                        # arm64 对应 aarch64；amd64 对应 x86_64
SHA=00654ac1e702a22744121ea9f10a4f792ebd7c3a744cba587dfac9fcb79b41a5   # aarch64
# x86_64 对应 d4a32ab2322d887ca1bc4a4e7afa9cc35393e6362dfc2b3becb389d362e4275a

curl -fsSL -o /tmp/fc.tgz \
  "https://github.com/firecracker-microvm/firecracker/releases/download/$V/firecracker-$V-$A.tgz"

# 校验必须通过，否则不得继续。macOS 只有 shasum，Linux 通常只有 sha256sum。
sumcheck() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -c -; else shasum -a 256 -c -; fi
}
echo "$SHA  /tmp/fc.tgz" | sumcheck
aws s3 cp /tmp/fc.tgz \
  "s3://openclaw-assets-${ACCOUNT}/deployment/binaries/firecracker/$V/firecracker-$V-$A.tgz"
```

版本号与摘要必须与 `deploy/userdata/provision-host.sh` 顶部的 `FC_VER` 及
`FC_SHA256_*` 一致。摘要不匹配时构建将在第 3 步失败并拒绝安装 —— 这是预期行为。

**构建两个架构时需分别上传对应制品。** 仅上传 aarch64 而构建 `arch = "amd64"` 将在
第 3 步报告 S3 miss，不会静默回落至公网源（见上文说明）。

**注意**：存储桶名必须与 `assets_bucket` 参数一致。脚本内的对象前缀
`deployment/binaries/firecracker` 为硬编码值，仅存储桶名可配置。

---

## §4 添加自定义构建步骤（可选）

如需在镜像中预置企业软件包、安全 agent、配置模板或内核参数，通过自定义脚本实现，
无需修改本仓库任何文件。

### §4.1 配置方式

```bash
cp deploy/packer/customize.sh.example deploy/packer/customize.sh
# 按需编辑 customize.sh
```

在 `my.pkrvars.hcl` 中声明：

```hcl
custom_script     = "customize.sh"
custom_script_env = {
  COMPANY_APT_MIRROR = "https://apt.example.internal"
  AGENT_ENDPOINT     = "https://collector.example.internal:4318"
}
```

`custom_script_env` 的键值对将作为环境变量传入脚本，用于把参数外置于脚本本体。
未声明 `custom_script` 时该阶段自动跳过。

### §4.2 执行时机

自定义脚本以 root 执行一次，位置固定在下列顺序中：

```
1. 上传 provision-host.sh 与 install-fluent-bit.sh
2. 执行 provision-host.sh（安装组件 → scrub 清除实例身份 → 写入 marker）
3. 执行自定义脚本          ← 此处
4. 零下载检查（断言）
5. 幂等检查（断言）
6. 生成 AMI
```

该位置由三项约束共同确定，不可调整：

- **位于 provision 之后** —— 此时 firecracker、jailer、awscli、ADOT collector、
  Fluent Bit 均已安装完成，自定义脚本可依赖这些组件。
- **位于 scrub 之后** —— scrub 属于 `provision-host.sh` 内部步骤（§7），在自定义脚本
  执行前已完成。因此**自定义脚本写入的任何内容都不会再被清理**，将原样进入镜像并被
  整个机队共享。
- **位于断言之前** —— 两轮断言是自定义脚本的安全防线。若置于断言之后，脚本引入的
  身份信息泄漏将无法被检出。

### §4.3 允许的操作

自定义脚本应仅执行与具体实例无关的操作：

| 类别 | 示例 |
|---|---|
| 安装软件包 | 安全 agent、合规采集器、监控 exporter |
| 写入静态配置 | 配置模板、CA 证书、systemd unit、sysctl 参数 |
| 预置静态资产 | 容器镜像、二进制、字体、语言包 |
| 创建目录与系统用户 | 不含实例身份信息的用户与目录结构 |

### §4.4 禁止的操作

以下操作会使产物成为机队级共享状态，**构建将在断言阶段失败并不产出 AMI**：

| 禁止项 | 后果 |
|---|---|
| 生成主机密钥或 SSH host key | 整个机队共享同一密钥，任何能创建实例者可冒充其余实例 |
| 写入 `/etc/platform.env` 或其他 per-host 配置 | 所有实例读取到同一份实例专属配置 |
| 硬编码密码、API key、token | 镜像可被任何有权创建实例者读取 |
| 触发 cloud-init 重新初始化 | 新实例复用旧 instance-id 判定，首次启动逻辑被跳过 |
| 启动会在构建阶段注册至外部系统的服务 | 该注册记录被整个机队复用 |

运行期所需的密钥应由 host 的 instance profile 在启动时从 Secrets Manager 或
SSM Parameter Store（SecureString）获取，不应预置于镜像。

### §4.5 注意事项

- **禁止交互式提示。** 构建阶段无终端，任何等待输入的操作都会阻塞至 Packer 超时。
  apt 操作需设置 `DEBIAN_FRONTEND=noninteractive`；涉及覆盖确认的命令需显式传
  `--yes` 或 `--force`。
- **脚本首行应为 `set -euo pipefail`。** 自定义脚本以 `bash` 执行，任一命令失败即
  终止构建，避免产出组件不完整的镜像。
- **超时上限为 30 分钟。** 若需安装大体积软件包可能触及该上限，此时应考虑将安装内容
  改为从自有 S3 获取而非从公网下载。
- **执行的脚本会保留在镜像的 `/opt/openclaw/custom/customize.sh`**，便于后续在运行中的
  host 上核对该批镜像包含哪些自定义内容。

### §4.6 验证自定义步骤已生效

构建日志中应包含自定义脚本的输出。构建完成后，使用该 AMI 创建实例并检查：

```bash
# 镜像中保留的自定义脚本
sudo cat /opt/openclaw/custom/customize.sh

# 自定义步骤的产物（以安装软件包为例）
dpkg -l | grep your-package
```

---

## §5 执行构建

```bash
# 插件已在 §1.5 安装；重复执行是幂等的，可省略
packer init deploy/packer

# validate 阶段会实际读取 SSM 解析基础镜像，故同时验证了凭据与网络参数
packer validate -var-file=deploy/packer/my.pkrvars.hcl deploy/packer

packer build -var-file=deploy/packer/my.pkrvars.hcl deploy/packer
```

**先执行 `validate`。** 它会实际调用 SSM 解析基础镜像，因此可在创建任何实例之前
发现凭据缺失、region 错误、参数取值非法等问题 —— 这些若留到 `build` 阶段暴露，
需先等待实例创建与启动，反馈周期长得多。

实测总耗时约 14 分钟（ap-southeast-1、`c7g.large`、arm64）。构建成功时末尾输出
AMI id，并写入 `deploy/packer/manifest.json`。

耗时分解（实测数据，用于定位当前所处阶段）：

| 阶段 | 实测耗时 |
|---|---|
| 创建构建实例并建立 SSH 连接 | 约 40 秒 |
| 执行 provision 安装全部组件 | **约 1 分钟** |
| 两轮自检（见下文） | 约 5 秒 |
| 停止实例并发起 CreateImage | 约 1.5 分钟 |
| **EBS 快照生成及 AMI 转为 available** | **约 11 分钟**（占总耗时主要部分） |

输出 `Waiting for AMI to become ready...` 后长时间无新输出属于**正常现象** ——
快照在后台生成，Packer 处于轮询状态。可另开终端查询进度：

```bash
aws ec2 describe-snapshots --snapshot-ids <构建日志中的 snapshot id> \
  --query 'Snapshots[0].Progress' --output text
```

构建过程包含两轮自检，任一轮失败则不产出 AMI：

1. **零下载检查** —— 验证 `firecracker`、`jailer`、`aws` 位于 PATH，Fluent Bit 位于
   `/opt/fluent-bit/bin`，guest kernel 与 marker 均存在，ADOT 已安装；同时验证
   `host_vm_key`、`platform.env`、SSH host key、cloud-init 实例状态**均已清除**
   （镜像由整个机队共享，任何实例身份信息残留将成为机队级共享身份）
2. **幂等检查** —— 重复执行一次 provision，确认不会重复安装任何组件

### 常见故障

| 现象 | 原因 |
|---|---|
| 阻塞在 `Waiting for SSH` 直至超时 | 子网无出站路径，或安全组、网络配置不通。参见 §2.1 |
| `digest mismatch for s3://...` | §3 上传的 tarball 与脚本中声明的摘要不一致 |
| `could not fetch s3://... (S3 miss)` | §3 未执行，或存储桶名与 `assets_bucket` 不一致 |
| 读取 S3 时报 `AccessDenied` | `iam_instance_profile` 未指定或权限不足。参见 §2.2 |
| `MISSING file: /opt/fluent-bit/bin/fluent-bit` | Fluent Bit 安装失败，需查阅构建日志中的 apt 报错 |

---

## §6 使 host 使用新 AMI

更新 LaunchTemplate 的 `ImageId`：

```bash
AMI_ID=ami-xxxxxxxxxxxx          # Packer 输出的 AMI id
LT=openclaw-host-lt

# 基于当前默认版本创建新版本，仅变更 ImageId。--query 直接返回版本号，
# 避免人工从 JSON 中提取（手工转录版本号是此步骤最主要的出错来源）
NEW_VER=$(aws ec2 create-launch-template-version \
  --launch-template-name "$LT" \
  --source-version '$Default' \
  --version-description "golden AMI $AMI_ID" \
  --launch-template-data "{\"ImageId\":\"$AMI_ID\"}" \
  --query 'LaunchTemplateVersion.VersionNumber' --output text)
echo "new LT version: $NEW_VER"

# 将新版本设为默认版本
aws ec2 modify-launch-template --launch-template-name "$LT" --default-version "$NEW_VER"

# 确认默认版本的 ImageId 已更新
aws ec2 describe-launch-template-versions --launch-template-name "$LT" \
  --versions '$Default' --query 'LaunchTemplateVersions[0].LaunchTemplateData.ImageId' --output text
```

**仅新创建的 host 使用新 AMI。** 存量 host 不受影响，将持续运行至被替换为止。
如需替换存量实例，可执行一次 ASG instance refresh，或逐台终止由 ASG 补充。

⚠️ **存量 host 上承载租户。** 执行滚动替换前需确认相关租户可迁移或可接受停机。

⚠️ **若 LaunchTemplate 由 CDK 创建**，上述新建的版本会在下次 `cdk deploy` 时被覆盖，
需在部署后重新执行本节。构建完成后请记录 AMI id（同时写入
`deploy/packer/manifest.json`），以便重新应用。

---

## §7 验证新 AMI 已生效

创建一台 host 后，通过 SSM 登录检查：

```bash
# marker 存在，且 recipe_version 与本次构建一致
sudo cat /etc/openclaw/.ami-provisioned

# 组件均已预置（应来自镜像，而非启动时安装）
command -v firecracker && firecracker --version
ls -l /opt/fluent-bit/bin/fluent-bit

# init 日志应显示跳过组件安装
sudo grep 'step2' /var/log/openclaw-init.log
# 预期输出：step2: AMI pre-provisioned (...) — skipping component install
# 若输出 "no provision marker — running provision-host.sh inline"，则 AMI 未生效
```

最后一项为关键判据：**仅当出现 `skipping component install` 时，才可确认 golden AMI
已实际生效**。

---

## §8 需要重新构建的情形

| 变更内容 | 是否需要重新构建 |
|---|---|
| `provision-host.sh` 变更（组件版本或安装方式） | ✅ 需要，并递增 `recipe_version` |
| Firecracker 版本升级 | ✅ 需要（同时更新 §3 的 tarball 与摘要） |
| `install-fluent-bit.sh` 变更 | ✅ 需要 |
| Fluent Bit **配置文件**变更（`.conf` / `.lua`） | ❌ 不需要 —— 配置在每次启动时从 S3 获取 |
| `init-host.sh` 变更 | ❌ 不需要 —— 该脚本每次启动均执行，不预置于镜像 |
| `launch-vm.sh`、`host-agent.py` 等变更 | ❌ 不需要 —— 从 S3 获取 |
| Ubuntu 发布安全更新 | ⚠️ 建议重新构建 —— 基础镜像指针自动指向新版本 |

判定原则：**该内容变更后，是否需要所有实例立即获取新版本？** 需要则不应预置于镜像
（改为存放 S3、每次启动获取）；不需要则可预置于镜像以缩短启动时间。
