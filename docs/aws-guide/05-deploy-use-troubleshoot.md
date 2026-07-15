# 部署解决方案

在部署此解决方案之前，请查看本指南中的架构和规划部署注意事项。该解决方案在 AWS 上为每个租户提供一台独立内核的 Firecracker microVM，运行带身份、技能与护栏的 OpenClaw AI agent；控制面（AWS Lambda、Amazon DynamoDB 与 Amazon API Gateway）负责注册、生命周期、备份与注销，运行后不向租户 microVM 注入业务数据。

> ⚠️ **注意：部署前必须满足的环境要求**
>
> 任一条不满足都会让部署或 host 自举直接失败，请逐条对照（对应的报错现象与修复见本章末尾"问题排查"）：
>
> | 要求                                      | 说明                                                                            | 不满足的后果                                                   |
> | ----------------------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------- |
> | **宿主机型带 KVM**                        | 生产 `r8g.metal-24xl`（arm64 裸金属，原生 KVM）；开发可用带 KVM 的机型          | 无 `/dev/kvm` → host 自举 `chmod /dev/kvm` 失败 → ASG 反复替换 |
> | **部署机装 Docker 且 daemon 在跑**        | AWS CDK 用容器给 Lambda 交叉打包 arm64 扩展                                     | `cdk deploy` 卡在 asset bundling，报无法连接 Docker daemon     |
> | **`config.yml` 已从 example 复制**        | 仓库只提供 `config.yml.example`（`config.yml` 在 `.gitignore`）                 | `setup.sh` 读不到配置直接报错退出                              |
> | **CDK 已 bootstrap**                      | 全新账号/区域首次需 `cdk bootstrap`（或用本章的最小 IAM policy 借道 bootstrap） | 报 `/cdk-bootstrap/hnb659fds/version not found`                |
> | **Python 3.12+ / AWS CLI / CDK CLI / uv** | `pyproject.toml` 要求 `requires-python >=3.12`，`aws-cdk-lib>=2.251.0`          | synth/deploy 依赖缺失                                          |
> | **挂自定义域名时 ACM 证书在 us-east-1**   | CloudFront 只认 us-east-1 的 ACM 证书                                           | CloudFront distribution 创建失败、整栈回滚                     |

## 部署流程概述

部署代码由四部分组成：基于 AWS Cloud Development Kit (AWS CDK) 的基础设施定义（`deploy/app.py` 与 `deploy/stack.py`，`stack.py` 入口已拆分为 `deploy/stacks/` 下的域模块）、host 与 microVM 生命周期脚本（`deploy/userdata/*.sh`）、黄金镜像构建脚本（`build-rootfs.sh`），以及 opt-in 数据面的边缘组件（`deploy/edge/`，OpenResty 边缘的 nginx/Lua 与自举脚本）。

AWS CDK 入口 `deploy/app.py` 读取 context 中的 `region`（默认 `us-east-1`），实例化 `OpenClawOrchestratorStack`，账号取自环境变量 `CDK_DEFAULT_ACCOUNT`。中心配置文件 `config.yml`（不入库，从 `config.yml.example` 复制）集中定义 host 规格、microVM 默认值、Auto Scaling Group (ASG)、Balloon 内存回收、健康检查、网络模式、Multi-AZ 与可选的数据面/认证开关。部署命令封装在 `setup.sh` 中。

推荐的部署顺序为：

1. `cp config.yml.example config.yml` 后核对，重点确认网络模式（`network.mode`）、区域（`region`）、实例类型（`instance_type`）与 ASG 容量。
2. 运行 `setup.sh`，完成 AWS CDK 部署并将 host/生命周期脚本、LiteLLM 网关资产、监控资产与 console/chat 静态资产上传到 Amazon Simple Storage Service (Amazon S3) assets 桶。
3. ASG 拉起第一台 host，host 执行 `init-host.sh` 完成自举并向宿主表注册。
4. 通过控制面 API 注册第一个租户，验证端到端链路连通。

## 前置准备：部署身份的最小 IAM 权限

本节给出在一台全新 Amazon EC2 实例（或任意工作机）上，用一组 AWS 访问密钥（Access Key ID / Secret Access Key）端到端跑通本解决方案部署所需的最小 IAM 权限。原则是最小够用：只授予实际用到的操作，且资源范围收敛到本解决方案自己创建的对象，避免使用 `AdministratorAccess`。

### 权限模型：一份 policy 覆盖首次 bootstrap 与日常部署

本解决方案通过 AWS CDK 部署。生产环境通常不会给部署身份 `AdministratorAccess`，因此本节把「首次 bootstrap」与「日常部署」两类操作合并进**同一份最小 policy**：部署身份挂上它，既能在全新账号/区域执行一次 `cdk bootstrap`，也能反复 `cdk deploy`，全程无需管理员介入，也无需事后更换权限。

理解这份 policy，先分清两类资源：

- **CDK 基础设施资源**（`CDKToolkit` 栈及其产出）：`cdk bootstrap` 首次运行时创建，包括 4 个执行角色 + 1 个 `cfn-exec-role`、1 个 Amazon S3 staging 桶、1 个 Amazon ECR 仓库、1 个版本参数。这些资源全部使用固定的 `cdk-hnb659fds-*` 命名前缀，policy 对它们的创建权限严格约束在该前缀内。（关于 ECR：本方案的 AWS Lambda 代码以 Amazon S3 zip 资产方式上传，运行时并不使用 ECR 容器镜像仓库；但默认 bootstrap 模板固定会创建该仓库，若缺少 ECR 权限，首次 `cdk bootstrap` 会因 `CDKToolkit` 栈创建失败而中止，故保留其创建权限。）
- **业务资源**（Amazon EC2、AWS Lambda、Amazon DynamoDB、Amazon CloudFront、Amazon VPC、Amazon ElastiCache for Redis、边缘 OpenResty Auto Scaling 组等）：由 bootstrap 产出的 `cfn-exec-role` 在 `cdk deploy` 时创建。部署身份本身**不需要**任何逐服务的业务资源创建权限——它只是 `AssumeRole` 那几个 CDK 角色，把创建业务资源的高权限交给 `cfn-exec-role`。

这种分层的好处：随 `stack.py` 增删业务资源（如数据面改造新增 VPC/Redis/Edge）无需改动这份 policy，新增服务的创建权限由 `cfn-exec-role` 承担；部署身份的权限面始终收敛在「管理 CDK 自己的基础设施」+「操作本解决方案两个 CloudFormation 栈」+「`setup.sh` 的少量 S3/SSM/API Gateway 直调」这三块。

### 首次部署流程（无管理员，仅凭本 policy）

给部署身份挂上下方 policy 后：

```bash
# 1. 首次在全新账号/区域初始化 CDK（本 policy 已覆盖，无需管理员）
cdk bootstrap aws://<account>/<region>

# 2. 确认 CDKToolkit 栈就绪
aws cloudformation describe-stacks --stack-name CDKToolkit --query 'Stacks[0].StackStatus'

# 3. 正常部署本解决方案
./setup.sh
```

若目标账号/区域此前已由管理员统一 bootstrap 过，第 1 步会识别为无变化直接跳过；此时可从 policy 中删去五条 `Bootstrap*` 与 `CdkBootstrapAndVersionParameters` 语句，只保留日常部署部分。

### 最小权限 policy

将下面的 policy JSON（也保存在 `docs/aws-guide/deploy-iam-policy.json`）附加到部署所用的 IAM 用户或角色上。其中 `ACCOUNT_ID`、`REGION` 是占位符——出于最小权限考虑保留精确值（把身份限定在单一账号与区域），部署前用一条命令替换：

```bash
sed -e 's/ACCOUNT_ID/123456789012/g' -e 's/REGION/us-east-1/g' \
  docs/aws-guide/deploy-iam-policy.json > /tmp/deploy-policy.json
```

（若希望同一身份能在任意区域部署，可把 ARN 中的 `REGION` 段改为 `*`；但 S3 桶名里的 `-REGION` 后缀是 AWS CDK 的固定命名，属于桶名的一部分，不可通配。）

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormationManageStacks",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack",
        "cloudformation:UpdateStack",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResources",
        "cloudformation:GetTemplate",
        "cloudformation:GetTemplateSummary",
        "cloudformation:CreateChangeSet",
        "cloudformation:DescribeChangeSet",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:DeleteChangeSet",
        "cloudformation:ListStacks"
      ],
      "Resource": [
        "arn:aws:cloudformation:REGION:ACCOUNT_ID:stack/OpenClawOrchestrator/*",
        "arn:aws:cloudformation:REGION:ACCOUNT_ID:stack/CDKToolkit/*"
      ]
    },
    {
      "Sid": "BootstrapIamRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PassRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:UpdateAssumeRolePolicy",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies"
      ],
      "Resource": "arn:aws:iam::ACCOUNT_ID:role/cdk-hnb659fds-*"
    },
    {
      "Sid": "BootstrapIamPolicies",
      "Effect": "Allow",
      "Action": [
        "iam:CreatePolicy",
        "iam:DeletePolicy",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
        "iam:CreatePolicyVersion",
        "iam:DeletePolicyVersion",
        "iam:ListPolicyVersions"
      ],
      "Resource": "arn:aws:iam::ACCOUNT_ID:policy/cdk-hnb659fds-*"
    },
    {
      "Sid": "BootstrapStagingBucket",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:PutBucketPolicy",
        "s3:DeleteBucketPolicy",
        "s3:GetBucketPolicy",
        "s3:PutBucketVersioning",
        "s3:GetBucketVersioning",
        "s3:PutBucketPublicAccessBlock",
        "s3:GetBucketPublicAccessBlock",
        "s3:PutEncryptionConfiguration",
        "s3:GetEncryptionConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:GetLifecycleConfiguration",
        "s3:GetBucketLocation"
      ],
      "Resource": "arn:aws:s3:::cdk-hnb659fds-assets-ACCOUNT_ID-REGION"
    },
    {
      "Sid": "BootstrapContainerAssetsRepo",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:DeleteRepository",
        "ecr:DescribeRepositories",
        "ecr:SetRepositoryPolicy",
        "ecr:GetRepositoryPolicy",
        "ecr:DeleteRepositoryPolicy",
        "ecr:PutLifecyclePolicy",
        "ecr:GetLifecyclePolicy",
        "ecr:TagResource",
        "ecr:ListTagsForResource"
      ],
      "Resource": "arn:aws:ecr:REGION:ACCOUNT_ID:repository/cdk-hnb659fds-container-assets-ACCOUNT_ID-REGION"
    },
    {
      "Sid": "AssumeCdkDeployRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [
        "arn:aws:iam::ACCOUNT_ID:role/cdk-hnb659fds-deploy-role-ACCOUNT_ID-REGION",
        "arn:aws:iam::ACCOUNT_ID:role/cdk-hnb659fds-file-publishing-role-ACCOUNT_ID-REGION",
        "arn:aws:iam::ACCOUNT_ID:role/cdk-hnb659fds-image-publishing-role-ACCOUNT_ID-REGION",
        "arn:aws:iam::ACCOUNT_ID:role/cdk-hnb659fds-lookup-role-ACCOUNT_ID-REGION"
      ]
    },
    {
      "Sid": "CdkBootstrapAndVersionParameters",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter"],
      "Resource": "arn:aws:ssm:REGION:ACCOUNT_ID:parameter/cdk-bootstrap/hnb659fds/version"
    },
    {
      "Sid": "SetupUploadHostScriptsAndConsoleAssets",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::openclaw-assets-ACCOUNT_ID*",
        "arn:aws:s3:::openclaw-assets-ACCOUNT_ID*/*"
      ]
    },
    {
      "Sid": "SetupWriteRuntimeCoordinatesToSsm",
      "Effect": "Allow",
      "Action": [
        "ssm:PutParameter",
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:AddTagsToResource",
        "ssm:DeleteParameter"
      ],
      "Resource": "arn:aws:ssm:REGION:ACCOUNT_ID:parameter/openclaw/*"
    },
    {
      "Sid": "SetupReadSharedVkeyApiKey",
      "Effect": "Allow",
      "Action": "apigateway:GET",
      "Resource": "arn:aws:apigateway:REGION::/apikeys/*"
    }
  ]
}
```

各语句用途分两组。**首次 bootstrap 相关**（仅在初始化 `CDKToolkit` 栈时用到，之后闲置）：`BootstrapIamRoles` / `BootstrapIamPolicies` 创建并管理 `cdk-hnb659fds-*` 前缀的 IAM 角色与策略；`BootstrapStagingBucket` 创建 CDK staging 桶；`BootstrapContainerAssetsRepo` 创建容器镜像仓库；`CdkBootstrapAndVersionParameters` 写入并读取 bootstrap 版本参数。**日常部署相关**：`CloudFormationManageStacks` 只允许操作本解决方案的 `OpenClawOrchestrator` 与 `CDKToolkit` 两个堆栈（含读回 assets 桶、backup 桶、备份密钥等 Outputs）；`AssumeCdkDeployRoles` 让 AWS CDK 扮演四个执行角色完成资产发布与堆栈变更；`SetupUploadHostScriptsAndConsoleAssets` 覆盖 `setup.sh` 向 assets 桶（`openclaw-assets-<account>`，带可选区域后缀）上传 host/hub 脚本与 console/chat 静态资产；`SetupWriteRuntimeCoordinatesToSsm` 覆盖 `setup.sh` 向 `/openclaw/*` 写入 CloudFront origin、Cognito 客户端 ID、共享 vkey 等运行时坐标；`SetupReadSharedVkeyApiKey` 覆盖 `setup.sh` 读取 API Gateway API key 明文以铸造共享 vkey。

> **安全边界**：这份 policy 不含任何创建业务资源（EC2/Lambda/DynamoDB/VPC/Redis 等）的权限——那些由 bootstrap 产出的 `cfn-exec-role` 承担。它对 IAM、S3、ECR 的创建权限严格锁定在 `cdk-hnb659fds-*` 命名前缀内，对 CloudFormation 的操作仅限 `OpenClawOrchestrator` 与 `CDKToolkit` 两个栈；即使部署身份凭据泄露，也无法用它创建任意 IAM 角色或触碰其他资源。

> **验证**：本 policy 的**日常部署部分**（`AssumeCdkDeployRoles`、`CloudFormationManageStacks`、`CdkBootstrapAndVersionParameters` 的读取、以及三条 `Setup*`）已经受限身份真部署实测（us-east-1）：建一个仅挂该部分权限的 IAM 用户，用其访问密钥运行 `cdk deploy`，全程无一条 `AccessDenied`，依次走过 synth、扮演四个 `cdk-hnb659fds-*` 角色、发布模板与代码资产、创建并执行 CloudFormation changeset，最终堆栈到达 `UPDATE_COMPLETE`（栈事件全程无 `FAILED`），AWS CDK 打印出完整 Outputs（`ApiUrl`、`CognitoUserPoolId`、`AssetsBucket`、各队列 URL 等）。该实测在目标账号**已由管理员 bootstrap** 的前提下进行。新并入的 **bootstrap 部分**（五条 `Bootstrap*` 语句）依据 795 生产账号 `CDKToolkit` 栈的真实资源清单（`aws cloudformation list-stack-resources --stack-name CDKToolkit`：5 个 IAM 角色/策略、1 个 S3 staging 桶、1 个 ECR 仓库、1 个版本参数）逐条对齐命名前缀设计，**尚未在一个全新未 bootstrap 账号上重新真跑 `cdk bootstrap` 验证**，待办；在已 bootstrap 的账号上，删去这五条只用日常部署部分即为上述已实测配置。可执行 `aws iam simulate-custom-policy --policy-input-list file://docs/aws-guide/deploy-iam-policy.json --action-names <action> --resource-arns <arn>` 对任意语句做正/反向模拟。

> **Note（不依赖 AWS IAM Identity Center）**：本解决方案的既定监控架构是自建 EC2 Prometheus + Grafana（`deploy/monitoring/`），默认不创建任何 AWS 托管监控工作区，因此部署不需要账号启用 AWS IAM Identity Center。该行为由 `config.yml` 的 `metrics.use_managed` 控制，默认 `false`。仅当显式设为 `true` 时，CDK 才会创建 Amazon Managed Prometheus 与 Amazon Managed Grafana 工作区；而 Amazon Managed Grafana 强制要求 AWS IAM Identity Center，届时账号须先启用它，否则栈内 `AWS::Grafana::Workspace` 会返回 `SSO is not enabled in any region` 并导致部署回滚。保持默认 `false` 即可规避该依赖。

> **Note**
>
> 该 policy 覆盖运行 `cdk bootstrap`（首次）与 `setup.sh` / `cdk deploy`（日常）的部署身份。**不**包含的是另外两条独立权限边界：黄金镜像构建（`build-rootfs.sh` 或远程 EC2 构建器）所需的权限，以及运行态 host 所用的 EC2 实例角色——后者由 `stack.py` 单独定义并遵循各自的最小权限。

## 步骤 1：部署基础设施

整套基础设施定义在 `deploy/stack.py` 的 `OpenClawOrchestratorStack` 类中。部署命令的核心是一行带区域参数的 `cdk deploy`：

```bash
cdk deploy -c region="<region>" --profile "<profile>" --require-approval never
```

部署完成后，从 AWS CloudFormation 堆栈的 Outputs 取回 assets 桶、backup 桶与备份密钥等运行时坐标。host 在运行时通过 `aws cloudformation describe-stacks` 查询这些 Outputs（最多重试 20 次、每次间隔 15 秒），而非通过 user-data 注入，以避免触及 EC2 user-data 的 16 KB 上限。

> **验证**：在 AWS CloudFormation 控制台确认堆栈状态为 `CREATE_COMPLETE`，并在 Outputs 中查到 `AssetsBucket`、`BackupBucket` 与 `BackupCmkKeyId`。assets 桶应已启用全部四个公网封锁开关并强制 HTTPS。

## 步骤 2：构建黄金镜像

`build-rootfs.sh` 仅能在 Linux 上运行，因为它依赖 `debootstrap` 与 `chroot`。在 macOS 上运行时，脚本会明确将操作者指向远程 Amazon Elastic Compute Cloud (Amazon EC2) 构建器脚本 `build-rootfs-on-ec2.sh`。运行前的预检要求依赖项齐备（debootstrap、aws、mkfs.ext4、curl、pigz、e2fsck、resize2fs），`/tmp` 至少 10 GB 可用空间，可用内存至少 2 GB（建议 4 GB 以上）。

一次构建产出三个独立的 ext4 镜像：

- **rootfs（只读黄金镜像）**：通过 debootstrap 拉取 Ubuntu Noble（arm64 走 ports.ubuntu.com，amd64 走 ec2.archive.ubuntu.com），在 chroot 内安装 Node.js 22.x、OpenClaw CLI、uv、auditd 与 GitHub CLI。身份文件与技能在构建时烤进 rootfs。
- **data-template**：数据盘模板，作为 microVM 首次启动时可写数据盘的基线。
- **immutable（只读权威盘）**：包含 7 个身份文件与 `IMMUTABLE_SKILLS` 列出的 11 个运维/安全技能，全部计算 SHA-256 生成 `golden-image.sha256` 基线（guest 内 `openclaw-fim.timer` 每 5 分钟对照基线查身份/skill 篡改）。

三个镜像的只读语义不依赖文件系统类型，而由 Firecracker 的 virtio 写屏障（`is_read_only:true`）、guest 内 `mount -o ro` 与 ro-bind 三层叠加保证。

灰度发布通过环境变量 `SKIP_MANIFEST` 控制。烤新版本时设 `SKIP_MANIFEST=1`，脚本会发布版本化镜像但不更新 `manifest.json`，旧镜像继续作为 live 版本提供给新启动的 microVM。后续流程为：在少量测试节点上运行新版本，验证通过后再更新 `manifest.json`，随后滚动重建。

> **验证**：确认 S3 中产出 rootfs、data-template 与 immutable 三个 ext4 镜像。设置 `SKIP_MANIFEST=1` 时，确认 `manifest.json` 未被更新，新 microVM 仍使用旧镜像。

> **Note**
>
> 三个 ext4 镜像与 `manifest.json` 上传到 assets 桶的 `deployment/rootfs/` 前缀；host 侧 `init-host.sh` 按 `manifest.json` 下载三块盘（等 manifest 最多 20 次 × 30 秒，超时 `exit 1`）。

## 步骤 3：注册首个租户验证

向控制面 API 发送 `POST /tenants` 请求注册一个租户。控制面 API 由 Amazon API Gateway（名为 `openclaw-orchestrator`，stage `v1`）承载，转发到运行在 ARM_64、2048 MB、120 秒超时的 AWS Lambda 函数。注册成功后，控制面在 DynamoDB 写入租户记录，调度到某台 host，由该 host 的 `launch-vm.sh` 在 Firecracker `InstanceStart` 之前完成全部冷注入并启动 microVM。

> **验证**：确认 `POST /tenants` 返回（实测约 1.7 秒），租户状态在约 4.0 秒内由 `creating` 经 `running` 变为 `vm_health` up（实测）。随后走实时聊天链路发一条消息确认 agent 回复（端到端首回复实测约 27 秒）：调用方 `GET /tenants/{id}` 在 `status=running` 时取回 gateway token 与 device 三件套的 KMS 密文并本地解密 → 平台后端持 device 私钥，用两级路由（wss `/gw/ws` → CloudFront → ALB → OpenResty 边缘 → microVM gateway:18789）与 microVM 完成 Ed25519 device 握手 → 发聊天消息、收流式回复。聊天不经任何旧的 hub 端点，详见『开发人员指南 — 实时聊天接入』与『数据面两级路由』。

## CDK 部署的核心资源

AWS CDK 部署在 DynamoDB 中创建三张主表，均为 `PAY_PER_REQUEST` 计费、`RETAIN` 删除保护：

- **`openclaw-tenants`**：租户表，主键 `id`。包含两个全局二级索引（GSI），均为 `ProjectionType=ALL`。`gsi_owner`（分区键 `owner_id`）按所有人反查节点，已 ACTIVE；`gsi_tenant_user`（分区键 `tenant_user_id`）按外部业务用户反查其节点舰队，支撑 `GET/POST /users/{tenant_user_id}/*` 三个端点。`gsi_tenant_user` 受 `scaler.add_gsi_tenant_user` 控制（默认 false）、默认不建；由于 DynamoDB 一次 update 只能新增一个 GSI，须在 `gsi_owner` 已 ACTIVE 后单独再部署一次才能建出。未建时上述三个端点降级，但不阻塞核心租户 CRUD。
- **`openclaw-hosts`**：宿主表，主键 `instance_id`。
- **`openclaw-groups`**：技能分组表，主键 `name`，用于 per-tenant 与 per-group 技能分发。

除主表外，另创建两张辅助表：

- **审计表 `openclaw-audit-log-<8 位 hex>`**：名带 per-deploy 后缀（STACK_ID UUID 首段）以避免 destroy 后重建与 RETAIN 残留表撞名；TTL 字段 `expires_ttl`，默认保留 90 天。
- **异步批量 job 表 `openclaw-batch-jobs`**：主键 `job_id`，TTL 字段 `expires_ttl`（完成的 job 行 7 天自动清理），`PAY_PER_REQUEST` 配合 `DESTROY`。大批量（超过 100 节点）或 `async:true` 的 `POST /batch/tenants` 记入此表，由 API Lambda 自调用的 worker 分批执行并增量刷新进度，客户端通过 `GET /batch/jobs/{job_id}` 轮询。

### 时间点恢复

tenants、hosts、groups 与审计四张 `RETAIN` 表均开启时间点恢复（Point-in-Time Recovery，PITR），DynamoDB 维持 35 天连续备份，可恢复到恢复期内任意一秒。该能力由 config 的 `dynamodb.point_in_time_recovery`（默认 true）与 `recovery_period_in_days`（默认 35，上限）控制。瞬态的 `openclaw-batch-jobs`（`DESTROY` 配 TTL）不开启 PITR。

> **Note**
>
> PITR 保护的是控制面元数据表本身。租户业务数据另有备份 Lambda 投递到 Amazon S3 WORM 桶兜底。

### 内置安全加固

以下安全加固写死在部署代码中，部署即生效，不受 `config.yml` 开关裁剪：

- **Amazon S3 assets 桶全封公网并强制 HTTPS**：assets 桶（承载租户数据盘、技能、备份与镜像）设 `block_public_access=BLOCK_ALL`（四个公网封锁开关全开）与 `enforce_ssl=True`（AWS CDK 自动追加 `aws:SecureTransport=false` 的 Deny 到桶策略）。Amazon CloudFront 通过源访问控制（Origin Access Control，OAC）签名访问，不破坏链路。
- **VPC Flow Logs**：默认开启（`flow_logs.enabled` 缺省 true），投递到 Amazon CloudWatch 日志组 `/openclaw/vpc/flow-logs`，`traffic_type=ALL`，用于检测跨租户东西向异常、验证 iptables 隔离是否生效。保留期默认 90 天。
- **AWS WAF baseline 规则（仅在 WAF 启用时生效）**：AWS WAF 整体是 config-gated 的（`waf.enabled` 默认 `false`，只在受限测试账号省成本时才考虑关；启用后关联到 API Gateway stage）。一旦启用，无论 `waf.managed_rules` 如何配置，代码侧总会将 `AWSManagedRulesSQLiRuleSet` 与 `AWSManagedRulesAmazonIpReputationList` 并入规则集（`dict.fromkeys` 去重保序）；当前示例 config 另配 `AWSManagedRulesCommonRuleSet` 与 `AWSManagedRulesKnownBadInputsRuleSet`，叠加后共 4 条。WAF 关闭时不创建 WebACL。

> **Note**
>
> 审计表的客户托管密钥（AWS Key Management Service (AWS KMS) CMK）为待办项，当前仍使用 AWS-owned key。CIS 2.2.2 建议客户托管 CMK，但现存审计表为 `RETAIN`，在线切换加密会强制 replace 并丢失审计数据，因此仅在全新账号首次部署时才加。运维换新环境做容灾规划时，应按「审计表目前不是 CMK」对待。

### 控制面 API Lambda 规格

控制面 API Lambda 运行在 ARM_64 架构、2048 MB 内存、120 秒超时，所有配置经环境变量注入。Amazon API Gateway 名为 `openclaw-orchestrator`，stage 为 `v1`，路由在 `add_resource` 与 `add_method` 块中定义（约 47 个 `add_method`，含同一资源的 GET/POST）。

---

# 使用解决方案

日常操作包括 host 与租户 microVM 的生命周期管理、镜像升级与灰度、监控告警与容灾，以及扩缩容与机型容量。

## host 与租户 microVM 生命周期

### host 自举

新 host 由 ASG 拉起后，`init-host.sh` 按以下顺序自举：配置 KVM 权限、host 加固、安装工具与 Firecracker、挂载数据卷、下载镜像（rootfs 与 vmlinux）、从 S3 同步 shared skills、部署生命周期脚本，最后向 `openclaw-hosts` 表注册。

EC2 user-data 有 16 KB 硬上限，而 `init-host.sh` 注入后约 23 KB。AWS CDK 将 `base64(gzip(init-host.sh))` 嵌入一个纯 ASCII 小 bootstrap，该 bootstrap 把脚本解码到 `/tmp/init-host.sh` 并执行。排查自举失败时，真正在运行的是 `/tmp/init-host.sh` 而非 user-data 原文；`/var/log/cloud-init-output.log` 记录 bootstrap 解码与 init-host 执行日志。

host 自举的几个要点：

- **Firecracker 版本钉死 v1.15.1**（可经环境变量 `FC_VERSION` 覆盖），因为 latest 可能缺少 CI 验证过的 guest 内核。
- **shared skills 每 5 分钟 cron 同步**：`*/5 * * * * root aws s3 sync s3://<bucket>/skills/ /data/shared-skills/`。
- **per-host SSH 密钥**：每台 host 生成一次 ed25519 密钥对，私钥留在 `/etc/openclaw/host_vm_key`，公钥注入每个 microVM 数据盘的 `.ssh/authorized_keys`。每个 microVM 只信任自己宿主的这把密钥。
- **生命周期 hook 保护**：`init-host.sh` 绑定 EXIT trap，成功返回 CONTINUE、失败返回 ABANDON，防止破损 host 挂住 ASG；DDB 注册重试 10 次仍失败则 ABANDON 退出。

### 启动租户 microVM

`launch-vm.sh` 接收 13 个位置参数：`tenant_id`、`vm_num`、`vcpu[2]`、`mem_mb[4096]`、`config_template`、`restore_backup_key`、`scoped_skills`、`litellm_vkey`、`channel_secret`（遗留死参数，见下）、`chat_endpoint_enabled`、第 11 位废弃占位、gateway_token 的 KMS 密文、device 配对文件 paired.json 的 base64（含公钥 + roles + scopes，私钥由平台后端代持不落 VM）。其中 `litellm_vkey`（per-tenant 计费密钥）与后两个 KMS 密文由 API Lambda 在注册时铸出并传入。

> **Note**
>
> 第 9 个参数 `channel_secret` 是旧数据面（claw-hub HMAC）的遗留，仍在函数签名里被接收，但其消费逻辑（写 `openclaw.json` 的 channel HMAC）已随 claw-hub 下线一并删除，是死参数，无"空值自生成"回退。清理尾巴留后续 issue。

> **Note**
>
> `launch-vm.sh` 的脚本默认 vCPU 2 / 内存 4096 MB 与 `config.yml` 的 microVM 默认值（`default_vcpu: 2` / `default_mem_mb: 4096`）一致；实际以调用方（API Lambda）传入的参数为准。容量推算以每 microVM 2 GB 内存基线计（r8g.metal-24xl 约 760 GB ÷ 2 GB ≈ 380/台，为推算口径；单台实测健康密度见容量章节 187 节点）。

所有注入都在 Firecracker `InstanceStart` 之前完成，这是「零运行时操作」的实现位置。启动流程为：

1. 挂载 data.ext4，注入 shared skills，注入 SSH 公钥；带注入凭据的租户以 `--consistent-read` 读回冻结的注入计划并把解密后的 dotenv 建成只读凭据盘。
2. 一次性段（仅首次启动）：下载 S3 config 模板，写入 gateway token（首次用 `openssl rand` 铸，或用控制面预铸的 KMS 密文按 `tenant_id` 上下文解密后覆盖，解密失败即 fail-closed），按 openclaw 2026.2.26 协议 v3 组装 device 配对文件 `paired.json`。
3. 幂等收敛段（每次启动都跑 `oc_harden_config`）：无条件删除 `dangerouslyDisableDeviceAuth`、将 `allowedOrigins` 收窄到当前 CloudFront origin、`baseUrl` 收敛到 LiteLLM host、`apiKey` 仅在显式非空时改写；`chatCompletions` 端点按 `chat_endpoint_enabled` 参数决定是否保留（默认删除，安全默认关）。
4. 挂载 `/dev/vdd` immutable 只读盘（及条件性的 `/dev/vde` 凭据盘），启动 Firecracker。

`InstanceStart` 之后，脚本关闭严格模式，仅执行 nginx conf 写入等收尾，不再向运行中的 microVM 推送数据。

每个租户 microVM 默认使用四块虚拟磁盘：`vda` rootfs（只读）、`vdb` overlay（读写 copy-on-write）、`vdc` data（读写持久）、`vdd` immutable（只读权威盘）；带注入凭据的租户另挂第五块 `vde` creds（只读）。核心三层栈为 rootfs 只读底盘 + overlay 每 microVM 稀疏可写层 + data 可写持久盘，rootfs、immutable、creds 均 `is_read_only:true`。

每个租户 microVM 叠加三层防火墙隔离，规则均以 `-I` 插入 FORWARD/INPUT 链顶部、先于 ACCEPT，按 tap 接口逐 microVM 各插一份：

- DROP guest 到 IMDS（169.254.169.254 与 IMDSv6 169.254.169.253），防止窃取宿主凭据。
- DROP guest 到整个租户超网（SUBNET_PREFIX/16），防止东西向 microVM 互联。
- INPUT DROP guest 到 host 的 8899/9090/22 端口，防止访问管理面。

加固后目标态为跨租户 100% 丢包（加固代码已落地、静态规则核对一致；漏洞态实测为 0% 丢包、跨租户 RTT 0.187 毫秒。带时间戳的新鲜 裸金属复测待核）。

### 停止、备份、迁移、扩容与克隆

生命周期脚本各自职责如下：

- **`stop-vm.sh`** 分四步停止：发送 Ctrl-Alt-Del 优雅关机、等待 2 秒、先 SIGTERM 再 sleep 后 SIGKILL、清理网络与 nginx。脚本注释强调不要 `pkill firecracker`，因为 `InstanceStart` 成功后 microVM 正常运行，后续 nginx race 不应反向清除它，崩溃恢复交由 host-agent 自动恢复处理。
- **`backup-data.sh`** 暂停 microVM、用 pigz 并行压缩 data.ext4、恢复 microVM、上传 S3，key 格式为 `backups/{tenant}/{timestamp}.gz`，`cleanup()` trap 保证失败时也恢复 microVM。
- **`migrate-vm.sh`** 提供 snapshot 与 restore 两种模式：snapshot 模式暂停后创建快照、恢复并上传 snapshot.vm、snapshot.mem、vm.json、data.ext4 与 overlay.ext4 到 S3；restore 模式下载全部磁盘、启动 Firecracker、POST `/snapshot/load` 恢复并自动唤醒。Firecracker 快照只记录磁盘路径，必须同时传输实体文件，否则跨 host restore 会报 `os error 2`。
- **`resize-disk.sh`** 在线扩容：暂停、truncate 增大稀疏 ext4、e2fsck、resize2fs、恢复，不需重启或调整分区。
- **`clone-data.sh`** 同主机克隆：暂停源、用 `cp --sparse=always` 复制 data 与 overlay、恢复源、`e2fsck -fy` 验证。该脚本接收 src_tenant、src_vm_num、dst_tenant、dst_vm_num 四个参数，克隆完成后调用方需再运行 `launch-vm.sh` 启动目标。

> **Important**
>
> 删除租户 microVM 前先确认其无真实数据或已完成备份。`DELETE /tenants/{id}?keep_data=false` 在删除数据盘前会同步备份到 S3，备份失败则返回 502 并中止删除（fail-closed）；仅 `?skip_backup=true` 才跳过备份。删除时还会回收该租户的 LiteLLM vkey 以防孤儿密钥。删除主机前先核清其上挂载了哪些租户，避免误删用于演示的节点。

> **Note**
>
> 多个生命周期脚本在边角场景的健壮性待加固：`resize-disk.sh` 假设无坏块，备份恢复后若前置 e2fsck 报码 4 未再复查可能导致扩容后文件系统损坏；`clone-data.sh` 的 e2fsck 后无返回码检查；`migrate-vm.sh` restore 模式容忍磁盘缺失，跨 host 时若快照引用路径不匹配会在 `/snapshot/load` 阶段失败；`stop-vm.sh` 后临时文件的空间回收机制未在脚本中显式说明。

## 镜像升级与灰度

升级 OpenClaw、修改配置或更换身份的正确做法是修改部署代码后重建，而非热改运行中的 microVM。修改租户身份需要重新构建镜像，而非调用运行时 API。具体路径为：修改 `build-rootfs.sh` 或 `launch-vm.sh`，烤制新镜像或调整 launch template，灰度后滚动重建，出问题时回滚 `manifest.json`。手改运行中的 microVM 仅可用于验证假设，验证完毕后必须落回部署代码。

这条纪律是整套隔离设计的基础：身份、技能、凭据与配置走启动前冷注入，运行后不开启 host 到 microVM 的批量热注入通道，少一条活通道就少一个横向移动面。

灰度发布通过 `SKIP_MANIFEST` 实现（参见『部署解决方案 — 步骤 2：构建黄金镜像』）。流程为：以 `SKIP_MANIFEST=1` 烤制新镜像、在少量测试节点验证、验证通过后更新 `manifest.json`、滚动重建。

> **Important**
>
> 滚动重建会逐台用新镜像重建 host 上的 microVM。重建会重新生成每个 microVM 的 gateway token 与 channel secret，并以新镜像启动。执行前确认新镜像已在测试节点验证通过；出问题时将 `manifest.json` 指回旧版本即可回滚。

## 监控告警与容灾

### 控制面定时 Lambda

容灾与运维由三个定时 Lambda 函数驱动：

| Lambda       | 频率                        | 职责                                                        |
| ------------ | --------------------------- | ----------------------------------------------------------- |
| health_check | 每 5 分钟                   | 判定 stale、重启 agent、AZ failover、迁移监控               |
| scaler       | 每 3 分钟                   | 空闲 host 回收、TTL 过期租户处理                            |
| backup       | 扫描节拍 `rate(30 minutes)` | 经 AWS Systems Manager RunShellScript 运行 `backup-data.sh` |

backup 的 `backup_cron` 是扫描节拍而非统一备份时间：每次触发只备份到期（距上次超过 `backup_interval_hours`，默认 24 小时）的一批、最多 `backup_batch_limit`（默认 20）个，错峰并限制并发，全量在 `backup_interval_hours` 内滚动覆盖。

### 健康判定与重启

租户超过 120 秒没有健康更新即判定为 stale，宿主 agent 可能已宕机。host-agent 重启设有 600 秒（10 分钟）冷却以防频繁重启。120 秒阈值与 host-agent 每 15 秒刷新一次时间戳配套：连续约 8 个周期未刷新才算 stale。

### AZ failover

`config.yml.example` 默认 `multi_az.enabled=true`（`az_count: 2`）、`health_check.az_failover.enabled=true`，即默认开启多 AZ 高可用。逻辑为：AZ 内所有 host 连续不健康超过 `unhealthy_threshold_minutes`（默认 10 分钟）触发 failover，冷却 `cooldown_minutes`（默认 30 分钟）防止重复触发。关闭多 AZ 可减少跨 AZ 数据传输费用，但会失去 AZ 级容灾。恢复设有前置门——必须存在备份，否则拒绝迁移并标记 `failover_blocked`。failover 后的 microVM 验证采用三层检测：Firecracker 进程存在、nginx 配置存在、本地 HTTP 探针返回小于 500；成功后记审计日志 `AZ_FAILOVER_TENANT_RECOVERED`。迁移过程监控超时 15 分钟，超时自动回滚到 running；failover 与迁移后还通过公网路径（经 ALB）核验 dashboard 真正可达，不可达则回滚。

### 运行时监控

每个租户 microVM 内部运行一层运行时监控，盯防两类动作：一是 microVM 内向外建立反向连接（典型的反弹 shell），二是关键系统与身份文件被修改。命中即产生告警。监控本身以系统最高权限运行，普通 agent 用户看不到、关不掉、读不到其日志，因而「先关监控再作案」的路径被堵死。这层监控为常驻、开销受控：文件与行为审计约占 11.7 MB、文件完整性监控约占 42 MB 内存（实测）。

### 自建监控平台

监控既定走自建 Amazon EC2 上的 Prometheus、Grafana 与 Wazuh，不依赖 Amazon Managed Service for Prometheus 与 Amazon Managed Grafana。两套监控资产均为可选、按需部署，不随主栈自动启动。AWS CDK 中的托管监控（Amazon Managed Service for Prometheus、Amazon Managed Grafana、Amazon GuardDuty、Amazon Simple Notification Service (Amazon SNS) 通知）均 config-gated 且默认关闭。两套自建监控都部署在专用 EC2（隔离爆炸半径，不跑在 metal host），安全组入站只对 VPC CIDR 或堡垒机安全组开放，绝不对 0.0.0.0/0 开放：

- **Prometheus 与 Grafana**：抓取各 metal host 的 host-agent `:8899/metrics`（microVM 内存、Balloon、磁盘、CPU、health 等 gauge），通过 ec2_sd 自动发现并附带 dashboard。采集链路依赖两个配套条件：host 实例须带 `Project=openclaw` 与 `Role=metal-host` 标签，host 安全组须放行 8899 入站给 VPC CIDR。
- **Wazuh（config-gated）**：`wazuh_enabled` 开启时，CDK 起一台专用监控 EC2（manager all-in-one：manager、indexer、dashboard），无公网 IP，dashboard 经堡垒机 SSH 隧道访问；microVM 内的 auditd 与文件完整性监控告警汇聚到它统一查看。

为避免告警只落在 manager 本机，部署脚本给 manager 配置最小权限实例角色，将告警实时镜像到独立的 CloudWatch 日志组与 Amazon SNS 通知主题。可选再开启独立的 Amazon OpenSearch Service 域（默认关闭，持续计费），将告警再落一份到独立信任域。microVM 内部的运行时告警可汇聚到这台 manager 统一查看。

## 扩缩容与机型容量

### ASG 弹性伸缩与机型

宿主机扩缩容交由 Amazon EC2 Auto Scaling 托管，Auto Scaling Group 与启动模板管理整队宿主机的拉起与滚动重建。host 由 ASG 拉起、自举注册，空闲超时由 scaler 经两轮确认后受控回收，整池容量按 `config.yml` 调整，默认值 `min_capacity: 1` / `max_capacity: 8` 台。

生产机型使用 metal 系列（Graviton4 ARM64，原生 KVM 运行 Firecracker，而非 x86 嵌套虚拟化）。`config.yml` 以 `arch: arm64` 与 `instance_type` 配置机型，容量推导由部署代码按 size token 查表并结合内存比计算。metal 机型走原生 KVM、不开启嵌套虚拟化；生产底座固定为 metal 原生 KVM。

若 config 的 `host.instance_types` 给出多个等容量机型（不少于 2 个），ASG 走 `MixedInstancesPolicy` 跨机型起 host，提升可用性与 Spot 韧性。硬约束是池内所有机型必须等容量（同 vCPU 与内存），否则 synth 直接报错。

### 容量配置

容量按 `config.yml` 调整：

- 每个租户 microVM 默认 2 vCPU / 4096 MB，由 `vm.default_vcpu` 与 `vm.default_mem_mb` 控制；容量推算仍以每 microVM 2 GB 内存基线取值。
- 超卖比由 `cpu_overcommit_ratio` 与 `mem_overcommit_ratio` 控制。可分配容量按 `allocatable_vcpu = total_vcpu × CPU_OVERCOMMIT_RATIO` 计，API 侧据 host 剩余容量调度。
- 每台 host 配 gp3 加密 EBS 数据盘（`/dev/sdf` 挂 `/data`，`config.yml` 的 `host.data_volume_gb` 默认 900 GB），承载所有 microVM 的稀疏盘与 rootfs overlay。单 microVM 稀疏盘实占约 187 MB 至 1.3 GB（轻载约 84–187 MB、重载可达 1.3 GB）。
- microVM 寻址上限到 vm_num 480（每 microVM 一个 /30 点对点链路）。
- 分配 vm_num 使用 DynamoDB ConditionExpression 乐观锁，CAS 重试 8 次。

### 并发瓶颈与削峰队列

大规模建租户的真正瓶颈是 AWS Systems Manager 并发，而非容量。同步路径下每个 create 或 start 都发一条独立 `ssm.send_command` 到目标 metal，一次性建满一台会瞬间超过 Systems Manager 单实例并发配额，部分请求永久卡在 `creating`。

解法是开启 `scaler.lifecycle_queue_enabled=true`，将 create/start/stop/delete 先写入 Amazon Simple Queue Service (Amazon SQS) 队列削峰，消费端按受控并发逐批执行（单台 metal 设约 5 至 10，对应 Systems Manager 单实例可承受速率，不用默认 50）。大规模扩容前须先开启此削峰队列，不要用客户端 N 并发直接 POST。

### 实测性能与容量数字

| 指标                   | 数值                                                       | 来源       |
| ---------------------- | ---------------------------------------------------------- | ---------- |
| microVM 纯启动         | 1.74 秒（p50）                                             | metal 实测 |
| launch 到 gateway 可用 | 6.48 秒（Firecracker 1.7 秒 + gateway 冷启 4.7 秒）        | metal 实测 |
| 空白 microVM RSS       | 约 609 MB                                                  | metal 实测 |
| 整机爬坡均值           | 669 MB                                                     | metal 实测 |
| 单台全 healthy 承载    | 187 节点（磁盘瓶颈，非内存上限）                           | metal 实测 |
| 每台 host 稳态承载     | 380 租户（r8g.metal-24xl，760 GB ÷ 2 GB）（推算）          | 容量推算   |
| 月度成本               | 约 8.36 USD/租户/月（80% Savings Plan + 20% Spot）（推算） | 成本推算   |

> **Note**
>
> 容量与扩缩容仍有若干不确定项，做容量规划时按尚未定论对待：187 节点的磁盘瓶颈是 IOPS 还是容量不明；ASG 自动扩容触发阈值待确认，当前疑似只有手动 SetDesiredCapacity；ALB 规则数硬墙与容量的关系待确认；成本约 8.36 USD/租户/月的详细分摊模型未给出。成本估算不含第三方服务与数据传输费用。

---

# 问题排查

以下涵盖常见问题、日志查看入口与支持渠道。

## 已知问题解决方案

条目按「错误现象（Symptom）— 根本原因（Root Cause）— 修复步骤（Resolution）」组织，均基于部署代码中真实的失败分支、自恢复逻辑与实测踩坑。所有 host 操作走 SSH；生产运营场景才用 AWS Systems Manager。前三条是部署新手最常撞的环境类问题。

### 问题 1：host 起来即被 Auto Scaling 反复替换（非 metal / 无 KVM 机型）

- **错误现象**：ASG 反复 terminate + launch 换机，CloudFormation/ASG 事件里 lifecycle hook 结果为 `ABANDON`；SSH 进去（若来得及）看 `/var/log/cloud-init-output.log`，`init-host.sh` 停在 KVM 设置步，报 `chmod: cannot access '/dev/kvm'`。
- **根本原因**：`init-host.sh` 在 `set -e` 下 `chmod 666 /dev/kvm`。非裸金属的 Graviton 机型没有 `/dev/kvm`（Graviton 只有 metal 才暴露 KVM，x86 嵌套虚拟化仅部分机型支持），`chmod` 失败触发 EXIT trap 返回 `ABANDON`。

```bash
# 确认配置的机型是带 KVM 的 metal 机型
grep -E 'instance_type|arch' config.yml
# 登到 host（若能上）验证 KVM 设备存在
ls -l /dev/kvm            # 缺失即根因
```

- **修复步骤**：把 `host.instance_type` 设为带 KVM 的机型（生产 `r8g.metal-24xl`），`host.arch` 与之匹配（`arm64`），重部署后 host 自举即通过。

### 问题 2：`cdk deploy` 卡在 Lambda 打包（Docker 未装/未启动）

- **错误现象**：`cdk synth`/`cdk deploy` 报 `Cannot connect to the Docker daemon` 或 `docker: command not found`，卡在 Lambda asset bundling 阶段。
- **根本原因**：API Lambda 走 Docker bundling（`deploy/stacks/lambdas.py`），在容器内交叉下载 arm64（`manylinux2014_aarch64`）wheel，部署机没有可用 Docker daemon 就无法打包。

```bash
# 启动 Docker 后确认 daemon 可用
docker ps                 # 能列出即可（无需有容器在跑）
```

- **修复步骤**：在部署机启动 Docker Desktop / dockerd，`docker ps` 通过后重跑 `setup.sh`。

### 问题 3：`setup.sh` 一上来就报错（`config.yml` 缺失或 CDK 未 bootstrap）

- **错误现象**：全新克隆后 `./setup.sh` 报 `VPC mode not configured` 或读 `config.yml` 抛 `FileNotFoundError`；或 `cdk deploy` 报 `SSM parameter /cdk-bootstrap/hnb659fds/version not found. Has the environment been bootstrapped?`。
- **根本原因**：`config.yml` 在 `.gitignore`，仓库只有 `config.yml.example`；全新账号/区域没跑过 `cdk bootstrap`，`CDKToolkit` 栈不存在。

```bash
# 1. 补齐本地配置
cp config.yml.example config.yml    # 再按需改 network.mode / arch / instance_type

# 2. 首次在全新账号/区域 bootstrap（或用本章最小 IAM policy 借道 bootstrap）
cdk bootstrap aws://<account>/<region>
```

- **修复步骤**：补 `config.yml` 并按需改网络模式/机型；全新环境先 `cdk bootstrap`，再跑 `setup.sh`。

### 问题 4：批量建租户部分永久卡 `creating`（AWS Systems Manager 单实例并发限流）

- **错误现象**：并发 `POST /tenants` 后一批租户永久停在 `creating`，`running` 数停涨；consumer 的 CloudWatch 日志出现 `SSM send error: ThrottlingException ... max retries`；host 账本 `used_vcpu`/`vm_count` 虚高（slot 泄漏）。
- **根本原因**：同步路径下每个 create/start 都对同一台 metal 发一条独立 `ssm.send_command`，一次性建满一台会超过 AWS Systems Manager 单实例并发配额（实测约 40 并发就开始 TimedOut），命令没跑起来，租户卡住。

```bash
# 开启 SQS 削峰（大规模建租户/扩容/压测必开）
# config.yml：
#   scaler.lifecycle_queue_enabled: true
#   scaler.create_via_queue: true
#   scaler.lifecycle_consumer_concurrency: 10   # 单台 metal 设 5-10，不是默认 50
```

- **修复步骤**：批量场景开启上面的削峰队列后重部署；已卡住的从 DLQ redrive（`aws sqs start-message-move-task`），并按账本差值回收泄漏 slot。

### 问题 5：聊天全部失败，LiteLLM 每条请求被拒（大模型 API 限流 / Guardrail 失效）

- **错误现象**：chat 全部无回复；LiteLLM 日志每条请求报 `ApplyGuardrail 400`，或上游 Amazon Bedrock 返回 `ThrottlingException` / `TPS exceeded`。
- **根本原因**：两类——① SSM `/openclaw/bedrock-guardrail-id` 缺失或指向失效/跨账号的 Guardrail id，导致每条对话被无效 Guardrail 拒；② 上游模型 TPS 配额不足，高并发被 Bedrock 限流。

```bash
# 取当前区域真实 Guardrail id，写回 SSM，再重建 LiteLLM 实例
aws bedrock list-guardrails --region <region>
aws ssm put-parameter --name /openclaw/bedrock-guardrail-id \
  --value <guardrail-id> --type String --overwrite --region <region>
```

- **修复步骤**：核对并修正 Guardrail id（无 id 就让 LiteLLM 不挂 Guardrail），改完回归越狱用例确认防护仍在；TPS 不足向 Bedrock 申请提额或降并发。

### 问题 6：某租户健康翻为 stale、聊天连接断开

- **错误现象**：控制面把租户标 `vm_health=stale`，用户聊天断开。判定阈值：超过 `STALE_SECONDS=120` 秒无健康更新。
- **根本原因**：宿主 agent 探活失败（Firecracker 进程消失、guest 网络连续不可达），或整台 host 的 host-agent 本身宕机。

```bash
# SSH 到该 host，看 host-agent 与该 microVM 的 Firecracker 日志
journalctl -u host-agent -n 200
cat /data/firecracker-vms/<tenant>/fc.log
```

- **修复步骤**：host-agent 提供两级自恢复，多数无需人工介入——`vm.json` 在但进程消失时 `_recover_vm`（健康检查的恢复动作）重启，Firecracker 活但 guest 连续不可达时 `_force_relaunch_vm`（健康检查的强制重启动作）走 stop+launch 重建。整台 host 全 stale 多半是 host-agent 宕机，health_check Lambda 经 SSM 下发 `systemctl restart host-agent`，设 600 秒冷却（`RESTART_COOLDOWN_SECONDS=600`），冷却期内不要手动反复重启。

### 问题 7：聊天连不上但健康正常（两级路由数据面排查）

- **错误现象**：控制面 VM Health 绿，但用户 wss 聊天连不上或 `/ws/{tenant_id}` 返回 404/503。
- **根本原因**：数据面是两级路由（平台后端网关 → OpenResty 边缘 → 宿主 DNAT → microVM gateway:18789），任一跳断都会连不上；404 = 边缘查不到该租户的 Redis 路由条目，503 = Redis 不可达。注意 gateway 无无认证 2xx 端点，`/healthz` 可能返回 404，别据此判 down（探 TCP 端口通即可）。

```bash
# 分层核（在 edge 实例或有 Redis 访问的机器上）
redis-cli -h <redis-primary-endpoint> GET route:<tenant_id>   # 空=路由未上报
# 在 host 上看 host-agent 是否把路由/DNAT 建好
journalctl -u host-agent -n 200 | grep -i route
```

- **修复步骤**：`redis-cli GET route:<tid>` 有值→查边缘实例的 `edge_redis_host` 环境与 nginx.conf；无值→查 host-agent 路由上报（VM 探活是否通过 promote 门，即数据面判活升级门控）。404 指向路由缺失、503 指向 Redis 链路断，两个状态码定位不同层。

### 问题 8：模板带新版本 key 毒死 gateway（`app_health` down、崩溃循环）

- **错误现象**：guest 内 openclaw gateway 崩溃重启数千次，租户 `app_health`（gateway 健康上报字段）长期 down、数据面全死，但控制面 DDB 仍报 `running`；console 上 VM Health 绿 / Gateway 红。
- **根本原因**：镜像钉死 openclaw `2026.2.26`，其 config schema 严格校验（`.strict()`）。模板（`templates/openclaw.json` 等）若带 2026.6.x 才有的 key（如 `heartbeat.isolatedSession`、`compaction.*`），gateway 启动校验直接拒起、循环崩溃。

```bash
# SSH 进 guest（agent 用户），看 gateway 重启计数与报错
journalctl --user -u openclaw-gateway -n 100
```

- **修复步骤**：对模板逐 key 核对钉住版本的 schema，`jq 'del(.heartbeat.isolatedSession, ...)'` 删非当前版本的 key 后**重烤镜像 / 重传模板**。热改活 VM 只作验证（重建会复发），必须改模板源。

### 问题 9：restore 后是空白盘（数据丢失级，静默）

- **错误现象**：`restore_from` 恢复后租户状态 `running`、无任何报错，但 guest 内 `~/.openclaw` 是模板原样，备份数据没回来。
- **根本原因**：dispatch pull 路径不透传 `restore_backup_key`/`config_template`，`launch-vm` 拿到空参走了空白模板盘（见 issue #199/#125）。
- **修复步骤**：restore 后必须做内容对账——比对 guest 内 `openclaw.json`/会话文件与备份时刻内容，不能只看 `status=running`。确认走的是修复后的 dispatch 链路（透传 restore 字段）。

### 问题 10：启动 microVM 报错、microVM 起不来

- **错误现象**：`launch-vm.sh` 非零退出，日志（前缀 `[oc:launch]`，ERR trap 打印 `FAIL line=<行号>`）停在某一步。
- **根本原因 / 修复**：几个直接 `exit 1` 的硬失败分支——备份恢复时 `e2fsck` 返回码 4 或 16（文件系统损坏未修）判 `FATAL: backup filesystem check failed` 拒绝启动（返回码 0/1/2/8 才接受，可换更早的备份 key 重试）；`tuntap add` 报 `EBUSY` 时强制清理 tap 后重试（不致命）；Firecracker `InstanceStart` 返回非空错误时 `exit 1` 并打印 `ERROR: <RESULT>`。带 `config_template` 时若模板未先传到 S3，`aws s3 cp` 报 404、脚本在 `set -euo pipefail` 下中途退出——建租户前先 `aws s3 ls s3://<assets-bucket>/templates/openclaw/<name>/openclaw.json` 确认存在。

### 问题 11：跨 host 迁移或 failover 后 microVM 起不来，fc.log 出现 `os error 2`

- **错误现象**：`migrate-vm.sh` restore 模式起 VM 失败，`fc.log` 报 `os error 2`（文件不存在）。
- **根本原因**：Firecracker 快照只记录磁盘路径，跨 host restore 必须把 `snapshot.vm`、`snapshot.mem`、`vm.json`、`data.ext4`、`overlay.ext4` 实体文件一起传过去；restore 模式对缺失磁盘以 `2>/dev/null || true` 容忍，缺文件不在下载阶段报错，而在 `/snapshot/load` 阶段失败。
- **修复步骤**：确认 S3 中这几个实体文件齐全后再 restore。

### 问题 12：failover 触发却被拒绝，租户标记 `failover_blocked`

- **错误现象**：AZ failover 触发但租户被标 `failover_blocked`，未迁移。
- **根本原因**：failover 恢复设有前置门——必须存在备份，否则拒绝迁移（数据安全优先于可用性，绝不静默丢数据）。
- **修复步骤**：先给该租户运行一次备份（参见「使用解决方案 — host 与租户 microVM 生命周期」中的备份脚本），再让 failover 重试。

### 问题 13：空闲 host 没被回收，或回收过于激进

- **错误现象**：空闲 host 迟迟不回收，或刚空闲就被终止。
- **根本原因 / 说明**：scaler 两轮确认，空闲超过 `idle_timeout_minutes`（默认 10 分钟）先标记 idle，下一轮仍空闲且 ASG `DesiredCapacity > MinSize` 才 terminate，受 ASG MinSize 保护。到达 MinSize 不回收是预期行为，不是缺陷。注意存在与 pending 租户的竞态：缩到 0 台后新建租户会 `no-host`，E2E/生产建议 ASG min 设 ≥1。

## 日志查看入口

| 看什么                                         | 在哪                                                          | 怎么取                                            |
| ---------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------- |
| host-agent 探针与自恢复                        | host 上 systemd 服务 `host-agent`                             | SSH 后 `journalctl -u host-agent -n 200`          |
| 单个 microVM 的 Firecracker 日志               | `${VM_DIR}/fc.log`（`/data/firecracker-vms/<tenant>/fc.log`） | SSH 后读取该文件                                  |
| nginx 反代                                     | host 上 `nginx` 服务                                          | `journalctl -u nginx` 与 `/var/log/nginx/*`       |
| 控制面 Lambda（注册、调度、健康、备份）        | Amazon CloudWatch Logs，按各 Lambda 函数名分组                | 控制台或 `aws logs tail`                          |
| guest 内运行时告警（反弹 shell、敏感文件改动） | microVM 内分析器，root:root 0700，agent 不可见                | 经 host-agent 或监控管道取，不在 guest 用户态可读 |

## 联系支持

如遇本节未覆盖的问题，请通过部署方的技术支持渠道联系支持团队。提交支持请求时，建议附上以下诊断信息以加快定位：

- 受影响租户的 `tenant_id` 与所在 host 的 `instance_id`。
- 相关时间窗内的 host-agent 日志（`journalctl -u host-agent`）与对应 microVM 的 `fc.log`。
- 控制面相关 Lambda 函数在 CloudWatch Logs 中的日志片段。
- 故障的复现步骤、预期行为与实际行为，以及任何已尝试的处置动作。

> **Note**
>
> 提交诊断信息前请脱敏，移除凭据（gateway token、API key、JWT 等）、真实账号 ID 与真实域名。
