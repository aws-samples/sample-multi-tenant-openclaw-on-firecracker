# 客户部署与删除手册（Admin 权限 · 复用已有网络）

> 本手册面向**客户自助部署**场景，前提：
>
> - 你在自己的 AWS 账号里拥有 **Admin 权限**（或等效的部署权限，见 §1.2）。
> - 你**已经有一个 VPC，其中至少有公有子网 + NAT 网关**。
> - 除网络外，其余基础设施（Auto Scaling Group、Lambda、DynamoDB、S3、KMS、Cognito、CloudFront、ALB、ElastiCache 等）**全部由本仓库的 AWS CDK 自动构建**。
>
> 本手册补齐 [`05-deploy-use-troubleshoot.md`](./05-deploy-use-troubleshoot.md) 未覆盖的两块客户刚需：**复用已有网络**（§3）与**部署失败/退役后如何删干净**（§6–§7）。其余深度（IAM 细节、镜像、生命周期、监控、排障）见 `05` 文档，本手册引用不重复。

---

## 目录

1. [部署前检查清单](#1-部署前检查清单)
2. [第一步：准备工作机与代码](#2-第一步准备工作机与代码)
3. [第二步：配置网络（复用你已有的子网 + NAT）](#3-第二步配置网络复用你已有的子网--nat)
4. [第三步：核对 config.yml 关键项](#4-第三步核对-configyml-关键项)
5. [第四步：执行部署](#5-第四步执行部署)
6. [删除资源（部署失败或退役）](#6-删除资源部署失败或退役)
7. [手动清理 cdk destroy 删不掉的残留](#7-手动清理-cdk-destroy-删不掉的残留)
8. [附录：一次性排障速查](#8-附录一次性排障速查)

---

## 1. 部署前检查清单

### 1.1 工具与运行环境

在一台 Linux 或 macOS 工作机（或一台 EC2）上准备：

| 工具       | 版本要求 | 检查命令            |
| ---------- | -------- | ------------------- |
| AWS CLI v2 | 最新     | `aws --version`     |
| **Docker** | 运行中   | `docker info`       |
| AWS CDK    | v2       | `cdk --version`     |
| Python     | 3.12+    | `python3 --version` |
| jq         | 任意     | `jq --version`      |

> **⚠️ Docker 是控制面部署的硬前置**（`README.md:839-841`）：`setup.sh` 期间 CDK 要为控制面 API Lambda 打包 `cryptography` 原生扩展（ARM64 runtime，自 v1.5.0 起，无论是否开 RBAC），这一步需要本机 Docker 运行中。**部署前先 `docker info` 确认**，否则 `cdk deploy` 会在 asset bundling 阶段失败。
>
> **黄金镜像无需手动先烤**：本方案默认 `image.build_in_stack: true`，`cdk deploy` 期间由**栈内 CodeBuild 自动烤制**并上传 S3（云端构建，本机不需要 Linux，macOS/Windows 均可，见 `README.md:112`）。仅当你要自定义镜像或用现成镜像时才手动烤（`build-rootfs.sh` 需 Linux，macOS 用 `scripts/build-rootfs-on-ec2.sh`，详见 `05` 文档步骤 2）。

### 1.2 权限

- 最简单：用 **Admin** 身份部署（本手册假设你有）。
- 若要最小权限：用 `docs/aws-guide/deploy-iam-policy.json` 里的策略（覆盖 `cdk bootstrap` + `cdk deploy` + `setup.sh` 的 S3/SSM/API Gateway 直调）。详见 `05` 文档「前置准备：部署身份的最小 IAM 权限」。

### 1.3 账号一次性开通项

- **Amazon Bedrock 模型访问**：在部署区域的 Bedrock 控制台开通所用 Claude 模型（本方案默认经 LiteLLM 走 `claude-sonnet-4-6` 等，区域为 `ap-southeast-1`）。未开通会导致聊天链路最终调用 Bedrock 失败。
- **`cdk bootstrap`**：若目标账号/区域从未 bootstrap 过，需先跑一次（Admin 身份自动覆盖）。已 bootstrap 过则跳过。
  ```bash
  cdk bootstrap aws://<ACCOUNT_ID>/<REGION>
  ```

### 1.4 ⚠️ 区域会影响删除难度（务必先读）

部署代码用**部署区域**判定资源的删除保护级别（`deploy/stacks/` 各栈按 `_stateful_removal` 上下文,数据保留区 RETAIN/其它 DESTROY）：

- **`ap-southeast-1` 走数据保留档**：DynamoDB 表、S3 桶为 `RETAIN`（`cdk destroy` **删不掉**，会残留），**备份桶启用 S3 Object Lock COMPLIANCE（WORM）**——保留期内**连 root 账户都无法删除**。想让某个 region 走这一档,把它加进 `deploy/stack.py` 的保留区域判定即可（当前只列了 `ap-southeast-1`）。
- **其他区域走可重建档**：有状态资源为 `DESTROY` + `auto_delete_objects`，`cdk destroy` 能一键删干净。

**含义**：

- 如果你是**做验证/演示、之后要删干净**，建议用**非 `ap-southeast-1` 的区域**部署，删除无痛。
- 如果你**必须部署在 `ap-southeast-1`**（当前唯一走数据保留档的区域），请预先知悉删除时备份桶会因 WORM 无法立即删除，处理方式见 [§7.4](#74-备份桶-worm-object-lock-桶)。

---

## 2. 第一步：准备工作机与代码

```bash
# 1. 克隆代码（若还没有）
git clone <bb-on-firecracker-dev repo> && cd bb-on-firecracker-dev

# 2. 配置 AWS 凭据（Admin）——本手册后续用 profile 名 <profile>，区域 <region>
aws configure --profile <profile>
aws sts get-caller-identity --profile <profile>   # 确认账号正确

# 3. 从模板生成配置文件
cp config.yml.example config.yml                              # 主配置（§3/§4 要改）
cp templates/openclaw.json.example templates/openclaw.json    # provider/model 模板

# 4. 准备 CDK Python 虚拟环境（仓库根 .venv —— destroy.sh 也依赖它，见 §6.1 注意）
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

> **注意 region 不是 config.yml 字段**：部署区域由命令行 `-c region=` / `setup.sh <region>` 传入。`config.yml` 里配 `instance_type` / 容量 / `network` 等；`region` 走命令行。

---

## 3. 第二步：配置网络（复用你已有的子网 + NAT）

这是本手册最关键的一节。本方案的 VPC 有 **3 种模式**（`config.yml` 的 `network.mode`，由 `deploy/stacks/_helpers.py:34-69` 消费）：

| 模式           | 行为                                                                   | 适用                        |
| -------------- | ---------------------------------------------------------------------- | --------------------------- |
| `self_managed` | CDK **新建** /20 VPC：3 AZ × (公有 /24 + 私有 /22) + **3 个 NAT 网关** | 从零、不想操心网络          |
| `imported`     | **复用你已有的 VPC 和子网**（你提供 vpc_id + 子网 id）                 | **你已有网络 ← 本手册场景** |
| `default_vpc`  | 用区域默认 VPC（host 裸公网，**仅 dev/demo**）                         | 快速试跑                    |

### 3.1 `imported` 模式的硬性要求（务必满足，否则部署直接报错）

`deploy/stacks/_helpers.py` 对 `imported` 模式**强校验、缺一即 `ValueError` 中止**（不降级）：

- `vpc_id`：你的 VPC id
- `cidr`：你的 VPC CIDR（如 `10.0.0.0/16`，用于安全组规则引用）
- `public_subnet_ids`：**恰好 3 个**公有子网（承载 ALB / NAT 网关），每个 AZ 一个
- `private_subnet_ids`：**恰好 3 个**私有子网（承载 host / edge / Redis），每个 AZ 一个

> host / edge / ElastiCache 运行在**私有子网**（`PRIVATE_WITH_EGRESS`，靠 NAT 出网）；ALB 和 NAT 网关在**公有子网**。
>
> **⚠️ 数量硬编码为 3**：代码 `deploy/stacks/_helpers.py:69` 写死要求 `len(pubs)==3 and len(privs)==3`，即便你把 `multi_az.az_count` 设为 2，imported 模式**仍强制要 3 公有 + 3 私有共 6 个子网**（config 注释说"与 az_count 对齐"，但代码是写死的 3——以代码为准）。凑不齐 6 个就用 `self_managed`。

### 3.2 ⚠️ 「只有公有子网 + NAT」够不够？

**不完全够。** `imported` 模式要求 3 公有 **+ 3 私有** 共 6 个子网。你已有公有子网 + NAT，但还需要**每个 AZ 一个私有子网、且其路由表默认路由指向你的 NAT 网关**。三个选择：

**方案 A（推荐，复用你的网络）**：在你已有 VPC 里补建 3 个私有子网（每 AZ 一个），把它们的路由表默认路由 `0.0.0.0/0` 指向你已有的 NAT 网关。然后用 `imported` 模式。这样最大化复用你的现有网络。

```bash
# 示例：在你的 VPC 里补一个私有子网并挂到已有 NAT（每 AZ 重复一次，共 3 个）
aws ec2 create-subnet --vpc-id <你的vpc> --cidr-block 10.0.100.0/24 \
  --availability-zone <region>a --profile <profile> --region <region>
# 建私有路由表 → 默认路由指向你已有 NAT GW → 关联该子网
aws ec2 create-route-table --vpc-id <你的vpc> --profile <profile> --region <region>
aws ec2 create-route --route-table-id <rtb> --destination-cidr-block 0.0.0.0/0 \
  --nat-gateway-id <你已有的nat-gw> --profile <profile> --region <region>
aws ec2 associate-route-table --route-table-id <rtb> --subnet-id <私有子网> --profile <profile> --region <region>
```

**方案 B（最省事，但不复用你的网络）**：用 `self_managed`，让 CDK 全建一套新 VPC + 3 NAT。**代价**：你已有的公有子网/NAT 闲置不用，且 CDK 建的 3 个 NAT 网关有额外成本。

**方案 C（仅 dev）**：`default_vpc`，host 裸公网，不适合生产。

### 3.3 填写网络配置

`setup.sh` 首次运行会**交互式**弹出网络选择菜单（`setup.sh:192-282`）：

```
1) New VPC        — CDK 新建 /20 VPC(3 AZ, 3 公有 + 3 私有, 3 NAT)
2) Existing VPC   — 部署进你已有 VPC(你提供 VPC ID + 6 个子网 ID)
3) Default VPC    — 用区域默认 VPC(仅 dev)
```

选 `2` 后依次输入 VPC ID、VPC CIDR、3 个公有子网、3 个私有子网，脚本自动写入 `config.yml`。

也可**预先手动**在 `config.yml` 填好 `network` 段（脚本检测到已配置则跳过交互）：

```yaml
network:
  mode: imported
  imported:
    vpc_id: "vpc-xxxxxxxx" # 你的 VPC id
    cidr: "10.0.0.0/16" # 你的 VPC CIDR
    public_subnet_ids: # 恰好 3 个(ALB/NAT)
      - "subnet-pub-a"
      - "subnet-pub-b"
      - "subnet-pub-c"
    private_subnet_ids: # 恰好 3 个(host/edge/Redis)
      - "subnet-priv-a"
      - "subnet-priv-b"
      - "subnet-priv-c"
```

---

## 4. 第三步：核对 config.yml 关键项

`config.yml` 是中心配置。除 `network`（§3）外，客户通常要确认这几项：

| 字段                                 | 默认             | 说明                                                                                               |
| ------------------------------------ | ---------------- | -------------------------------------------------------------------------------------------------- |
| `host.instance_type`                 | `r8g.metal-24xl` | 生产用 metal（原生 KVM，约 380 租户/台）。**开发/验证可用 `m8g.xlarge`**（便宜，嵌套虚拟约 30 VM） |
| `host.min_capacity` / `max_capacity` | 见 config        | 首台起步与上限                                                                                     |
| `vm.default_vcpu` / `default_mem_mb` | 2 / 4096         | 每租户 microVM 规格                                                                                |
| `edge.min_capacity` / `max_capacity` | 3 / 6            | OpenResty 边缘层                                                                                   |
| `s3.backup_bucket_suffix`            | `""`             | **删除相关**：见 §7.4，重部撞 WORM 桶名时用                                                        |
| `multi_az.az_count`                  | 3                | 必须与你 imported 的子网数量一致                                                                   |
| `metrics.use_managed`                | `false`          | 保持 false，否则需账号启用 IAM Identity Center                                                     |

> 验证/演示建议：`host.instance_type` 改 `m8g.xlarge`、区域用非 `ap-southeast-1`（删除无痛，见 §1.4）。

> **⚠️ LiteLLM 的 Bedrock 区域当前硬编码为 `ap-southeast-1`**（`deploy/runtime-config-export/litellm-config.yaml` 的 `aws_region_name`，`litellm-up.sh` 只替换 guardrail id、不替换 region）。若你部署在其他区域并让本方案自动起 LiteLLM 网关，它仍会调用 `ap-southeast-1` 的 Bedrock。两个应对：① 在 `ap-southeast-1` 开通 Bedrock 模型访问（跨区调用）；② 或部署前把 `litellm-config.yaml` 的 region 改成你的区域并确保该区已开通模型。这是当前已知的区域耦合点。

---

## 5. 第四步：执行部署

一条命令封装全流程（`setup.sh`）：

```bash
./setup.sh <region> <profile>
```

> **⚠️ 本机 Docker 被企业策略封锁时的部署路径（实测经验）**
> 若你的工作机（如公司 macOS）Docker Desktop 被策略禁用，`setup.sh` 的 `cdk deploy` 会在 Lambda bundling 阶段失败（§1.1）。此时在一台**带 Docker 的 EC2 跳板机**上部署（下述步骤已实跑验证）：
>
> 1. 跳板机需装 Docker + cdk CLI + `uv`；本机把仓库打包（排除 `.git/.venv/node_modules/cdk.out`）上传 S3，跳板机拉下解包。
> 2. 跳板机上 `uv sync` 建 `.venv`（**本项目用 `uv`，不是 pip requirements**）。
> 3. 跳板机用**实例角色**部署时，`setup.sh` 仍要一个 named profile，但 **CDK CLI 不认 `credential_source = Ec2InstanceMetadata`**。解决：从实例角色取临时凭据写进该 profile 的 `~/.aws/credentials`（`aws_access_key_id`/`secret`/`session_token`），profile 的 `config` 只留 `region`。
> 4. 必须 `export CDK_DEFAULT_ACCOUNT=<account>`（`deploy/app.py:14` 从它取账号，缺则报 `Cannot retrieve value from context provider ami`）。
> 5. 然后 `./setup.sh <region> <profile>` 即可跑通（栈内 CodeBuild 烤镜像约 10-15 分钟，整栈约 300 资源 ~10-12 分钟到 `CREATE_COMPLETE`）。

`setup.sh` 依次完成（`setup.sh` 全文）：

1. （首次交互）网络模式选择 → 写入 `config.yml`（§3.3）。
2. 从已有栈导入 Cognito pool（若是升级部署）。
3. `cdk deploy` 构建全部基础设施（CloudFormation 栈名 **`OpenClawOrchestrator`**）。
4. 从栈 Outputs 读回 assets 桶、backup 桶、API URL、Cognito 等坐标。
5. 上传 host/hub 生命周期脚本、`console/`、`chat/` 静态资产到 assets 桶。
6. 铸造共享 LiteLLM vkey 写入 SSM。
7. 把全部运行时坐标写入 **`.env.deploy`**（删除时要用，勿删）。

带自定义域名（生产推荐双域名）：

```bash
./setup.sh <region> <profile> \
  --console-domain console.example.com --console-cert <us-east-1 ACM ARN> \
  --app-domain app.example.com --app-cert <us-east-1 ACM ARN>
```

**验证部署成功**：

```bash
# 1. 栈状态
aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].StackStatus' --profile <profile> --region <region>
# 期望 CREATE_COMPLETE 或 UPDATE_COMPLETE

# 2. 关键 Outputs
aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query 'Stacks[0].Outputs' --profile <profile> --region <region>
# 期望含 ApiUrl / CognitoUserPoolId / AssetsBucket / BackupBucket

# 3. 首台 host 起来后注册到 DDB
aws dynamodb scan --table-name openclaw-hosts --select COUNT \
  --profile <profile> --region <region>
```

> **黄金镜像**：默认 `image.build_in_stack: true`，`cdk deploy` 会触发栈内 CodeBuild 自动烤镜像并上传 S3，首台 host 自举时即可拉到。**新账号首次部署会因此多花十几分钟**（等 CodeBuild 完成，CDK 有 CustomResource 等它）。无需手动先烤。若你把 `build_in_stack` 设为 false，则需自己按 `05` 文档步骤 2 先烤并上传。

---

## 6. 删除资源（部署失败或退役）

> **⚠️ 破坏性操作。** 删除会终止所有 host、删除控制面。执行前确认无真实租户数据，或已备份。

### 6.1 官方删除脚本 `scripts/destroy.sh`

删除入口是 `scripts/destroy.sh`，它依赖部署时生成的 `.env.deploy`（读取 REGION/PROFILE/桶名/表名）。

**两种模式**：

```bash
# 模式一:只删 CDK 栈(RETAIN 的 S3/DDB 会残留,数据保全)
./scripts/destroy.sh

# 模式二:连 RETAIN 的 assets 桶 + tenants/hosts 表一起删(数据永久丢失)
./scripts/destroy.sh --purge
```

脚本会要你输入 `yes` 二次确认。`--purge` 额外做（`destroy.sh:29-60`）：

- 清空并删除 assets 桶
- 删除 `openclaw-tenants`、`openclaw-hosts` 表（**仅这 2 张**）
- 删除孤儿 IAM role（`OpenClawOrchestrator-*` 前缀）
- 删除孤儿 EBS 数据卷（tag `openclaw:role=host-data`、状态 available）

> **⚠️ destroy.sh 的一个已知路径问题**：脚本先 `cd deploy` 再用相对路径 `.venv/bin`（`destroy.sh:36-37`），但 venv 实际在**仓库根** `.venv`、不在 `deploy/.venv`。若你的 `cdk` 是全局安装（本手册 §1.1 前置），脚本照常工作；若你只在仓库根 `.venv` 里装了 cdk，`destroy.sh` 会找不到 —— 此时改为**从仓库根手动跑** `. .venv/bin/activate && cd deploy && cdk destroy -c region=<region> --profile <profile> --force`，再回到 §7 手动清残留。

### 6.2 destroy.sh 覆盖不到的（务必手动清，见 §7）

`destroy.sh --purge` **不处理**以下资源，退役或重部前需手动清理：

- **备份桶** `openclaw-backups-<account>*`（数据保留区是 WORM，特殊，见 §7.4）
- **审计表** `openclaw-audit-log-<8位hex>`、**groups 表** `openclaw-groups`、**batch-jobs 表** `openclaw-batch-jobs`
- **DynamoDB PITR 连续备份**（表删了 PITR 一起没，但要确认）
- **KMS CMK**（backup CMK，删栈后进 7-30 天待删除窗口）
- **CDKToolkit 栈**（CDK bootstrap 基础设施，跨方案共享，一般**不删**）
- 若在**数据保留区**：所有 RETAIN 的表和桶（destroy.sh 只删 tenants/hosts 两张）

---

## 7. 手动清理 cdk destroy 删不掉的残留

> 下列命令按顺序执行。把 `<account>` / `<region>` / `<profile>` 替换为实际值。**逐条确认输出**，删错难恢复。

### 7.1 确认还剩哪些资源

```bash
# 还在的栈
aws cloudformation list-stacks --profile <profile> --region <region> \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED \
  --query "StackSummaries[?contains(StackName,'OpenClaw')].{n:StackName,s:StackStatus}"

# 残留的 DynamoDB 表
aws dynamodb list-tables --profile <profile> --region <region> \
  --query "TableNames[?starts_with(@,'openclaw-')]"

# 残留的 S3 桶
aws s3api list-buckets --profile <profile> \
  --query "Buckets[?starts_with(Name,'openclaw-')].Name"
```

### 7.2 残留的 DynamoDB 表（数据保留区全部 RETAIN）

固定名表（真实环境实测存在这几张，可重建区 `cdk destroy` 已自动删，数据保留区需手动）：

```bash
for t in openclaw-tenants openclaw-hosts openclaw-groups openclaw-batch-jobs \
         openclaw-tenant-idp-map openclaw-tenant-secrets openclaw-assignments; do
  aws dynamodb delete-table --table-name "$t" --profile <profile> --region <region> \
    && echo "✓ $t 已删" || echo "⚠ $t 不存在或已删"
done
```

审计表名带 per-deploy 后缀（如实测的 `openclaw-audit-log-ea91c0a0`），需先列出再删：

```bash
aws dynamodb list-tables --profile <profile> --region <region> \
  --query "TableNames[?starts_with(@,'openclaw-audit-log-')]" --output text | tr '\t' '\n' | while read t; do
  [ -n "$t" ] && aws dynamodb delete-table --table-name "$t" --profile <profile> --region <region> && echo "✓ $t 已删"
done
```

> 一条命令列出所有 openclaw 表确认无遗漏：`aws dynamodb list-tables --profile <profile> --region <region> --query "TableNames[?starts_with(@,'openclaw')]"`

> 删表即删除其 PITR 连续备份。若只想停 PITR 不删表：`aws dynamodb update-continuous-backups --table-name <t> --point-in-time-recovery-specification PointInTimeRecoveryEnabled=false`。

### 7.3 普通 S3 桶（assets 等，非 WORM）

```bash
# 先清空(含所有版本),再删桶
BUCKET=openclaw-assets-<account>            # 若配了区域/后缀,补全实际桶名
aws s3 rm "s3://$BUCKET" --recursive --profile <profile> --region <region>
aws s3 rb "s3://$BUCKET" --profile <profile> --region <region>
```

若桶开了版本控制且有历史版本，`s3 rb` 会因非空失败，需先删所有版本：

```bash
aws s3api list-object-versions --bucket "$BUCKET" --profile <profile> --region <region> \
  --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json > /tmp/vers.json
aws s3api delete-objects --bucket "$BUCKET" --delete file:///tmp/vers.json --profile <profile> --region <region>
# 删除标记同理(把 Versions 换成 DeleteMarkers 再删一次),然后 s3 rb
```

### 7.4 备份桶（WORM / Object Lock 桶）⚠️

`openclaw-backups-<account>*` 在**数据保留区（`ap-southeast-1`）**启用了 **Object Lock COMPLIANCE 模式**（`deploy/stacks/storage.py:381` object_lock_enabled + COMPLIANCE 段）。这是硬约束：

> **COMPLIANCE 模式下，在对象的保留期（`s3.backup_retention_days`）内，连 root 账户都无法删除对象版本、无法缩短保留期、无法关闭 Object Lock。** 这是合规特性，不是 bug。

处理方式（三选一）：

1. **等保留期过**：保留期（默认见 `config.yml` `s3.backup_retention_days`）到期后，对象可删，再按 §7.3 清空 + 删桶。
2. **不删、留着**：桶本身不产生显著成本（只存备份），可保留到保留期自然到期再删。
3. **重部避开撞名**：如果你要**重新部署**但旧 WORM 桶还删不掉，在 `config.yml` 设 `s3.backup_bucket_suffix`（如 `-r2`），新部署会用 `openclaw-backups-<account>-r2` 避开撞名（`deploy/stacks/storage.py:368-370`）。旧桶留待保留期到期。

> **可重建区**：备份桶是 `DESTROY + auto_delete_objects`，`cdk destroy` 直接删干净，无此问题——这正是 §1.4 建议验证/演示用非 `ap-southeast-1` 的原因。
>
> **另一个 WORM 桶**：若开了 `audit.worm_archive_enabled`，还有一个审计归档桶 `openclaw-audit-archive-<account>*`，同样是 COMPLIANCE WORM，且**没有 suffix 规避机制**、保留期通常更长（默认按审计合规，可达数年）。数据保留区退役时它也只能等保留期到期。默认该开关关闭。

### 7.5 KMS CMK

删栈后 backup CMK 进入待删除窗口（7-30 天），到期自动删除，一般无需干预。若要立即安排删除：

```bash
KEY=$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
  --query "Stacks[0].Outputs[?OutputKey=='BackupCmkKeyId'].OutputValue" --output text --profile <profile> --region <region> 2>/dev/null)
[ -n "$KEY" ] && aws kms schedule-key-deletion --key-id "$KEY" --pending-window-in-days 7 --profile <profile> --region <region>
```

### 7.6 收尾核对

```bash
# 栈已删
aws cloudformation describe-stacks --stack-name OpenClawOrchestrator --profile <profile> --region <region> 2>&1 | grep -q "does not exist" && echo "✓ 栈已删除"
# 无残留 openclaw 资源
aws dynamodb list-tables --profile <profile> --region <region> --query "TableNames[?starts_with(@,'openclaw-')]"
aws s3api list-buckets --profile <profile> --query "Buckets[?starts_with(Name,'openclaw-')].Name"
# CDKToolkit 栈通常保留(下次部署复用);确要清理 bootstrap:
# aws cloudformation delete-stack --stack-name CDKToolkit --profile <profile> --region <region>
```

> **DELETE_FAILED 处理**：若 `cdk destroy` 报 `DELETE_FAILED`，多半是某资源有依赖（如 ENI 未释放、桶非空、安全组被引用）。到 CloudFormation 控制台看失败资源，手动清依赖后，用 `aws cloudformation delete-stack --stack-name OpenClawOrchestrator --retain-resources <逻辑ID>` 保留卡住的资源重试删栈，再单独手动删该资源。

---

## 8. 附录：一次性排障速查

| 现象                                                                    | 原因                                          | 处理                                                  |
| ----------------------------------------------------------------------- | --------------------------------------------- | ----------------------------------------------------- |
| `setup.sh` 报 `network.mode=imported requires ... 3 public + 3 private` | imported 子网没凑齐 6 个                      | 见 §3.1/§3.2，补私有子网或改 `self_managed`           |
| `cdk bootstrap` 缺权限                                                  | 账号未 bootstrap 且权限不足                   | 用 Admin 跑一次 bootstrap，见 §1.3                    |
| 首台 host 起不来                                                        | S3 无黄金镜像                                 | 先烤镜像（§5 备注）                                   |
| 聊天调用 Bedrock 失败                                                   | 模型未开通 / vkey 未注入                      | 开通 Bedrock 模型（§1.3），查 `mint-shared-vkey` 日志 |
| `SSO is not enabled in any region`                                      | `metrics.use_managed=true` 需 Identity Center | 保持 `metrics.use_managed=false`（§4）                |
| `cdk destroy` 后备份桶删不掉                                            | 数据保留区 WORM 保留期未过                    | 见 §7.4                                               |
| `DELETE_FAILED`                                                         | 资源有依赖                                    | 见 §7.6 收尾核对末尾                                  |

更多运行期排障（host stale、microVM 起不来、迁移 `os error 2`、failover blocked 等）见 [`05-deploy-use-troubleshoot.md`](./05-deploy-use-troubleshoot.md)「问题排查」章节。

---

_本手册基于最新 bb 分支代码核对（`deploy/stack.py`、`setup.sh`、`scripts/destroy.sh`、`config.yml`）。file:line 引用为该快照行号。部署与删除流程以真实脚本行为为准。_
