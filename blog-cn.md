# OpenClaw Pool：在 AWS 上使用 Firecracker microVM 实现多租户 OpenClaw AI Agent 隔离部署

Neo Sun、Aleck Lin | AWS | 架构、计算、无服务器、SaaS

---

[OpenClaw](https://github.com/anthropics/openclaw) 是一个开源 AI Agent 框架，为自主编程 Agent 提供完整的运行时——包括 Gateway 服务器、交互式 Dashboard、工具执行和会话管理。当组织希望为多个团队或客户提供基于 OpenClaw 的 Agent 服务时，需要一种方式来运行多个独立实例并保持强隔离，同时无需为每个实例单独管理基础设施。

在本文中，我们介绍 **OpenClaw Pool**（[GitHub](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker)），一个使用 [Firecracker](https://firecracker-microvm.github.io/) microVM 在 AWS 上部署多租户 OpenClaw Agent 的开源参考架构。每个租户获得一个完全隔离的 OpenClaw 实例——独立的内核、文件系统、网络和预配置的开发环境——而无服务器控制面通过统一的 REST API 处理调度、扩缩容、健康监控和备份。

## 多租户 OpenClaw 的挑战

单个 OpenClaw 实例包含一个 Node.js Gateway 服务器、持久化工作空间、配置文件和可选的 MCP 工具集成。当你需要为多个团队、部门或客户提供隔离的 OpenClaw 环境时，通常会考虑以下三种方案：

| 方案 | 隔离级别 | 启动时间 | 部署密度 | 运维复杂度 |
|------|---------|---------|---------|-----------|
| 每租户一台 EC2 实例 | 硬件级（最强） | 分钟级 | 低——空闲实例浪费资源 | 单租户低，总成本高 |
| 容器（ECS/EKS） | Namespace 级（共享内核） | 秒级 | 高 | 高——需要编排专业知识 |
| Firecracker microVM | **内核级（独立内核）** | **<125ms** | **高——每 VM 开销极小** | **低——控制面自动化管理** |

容器共享宿主机内核，这意味着内核漏洞可能影响所有租户。独立 EC2 实例提供强隔离，但成本高且配置慢。Firecracker microVM 将虚拟机的安全特性——每个租户拥有独立的 Linux 内核——与容器级别的启动速度和资源效率相结合。这与驱动 [AWS Lambda](https://aws.amazon.com/lambda/) 和 [AWS Fargate](https://aws.amazon.com/fargate/) 的虚拟化技术完全相同。

## 方案概览

OpenClaw Pool 由三层组成：无服务器控制面、运行 Firecracker microVM 的 EC2 宿主机，以及提供 HTTPS Dashboard 访问的接入层。

*图 1. 系统架构，展示控制面、数据面和接入层。*

![系统架构](docs/oc-system-arch.png)

### 控制面（无服务器）

[Amazon API Gateway](https://aws.amazon.com/api-gateway/) 提供以 API Key 保护的 REST API。七个 [AWS Lambda](https://aws.amazon.com/lambda/) 函数分别处理租户生命周期操作、宿主机管理、备份编排、配置模板管理、共享 Skills 列表、健康监控和空闲宿主机回收。[Amazon DynamoDB](https://aws.amazon.com/dynamodb/) 存储租户状态（状态、健康、Gateway Token、rootfs 版本）和宿主机资源追踪（vCPU、内存、VM 数量、空闲时间戳）。[Amazon EventBridge](https://aws.amazon.com/eventbridge/) 调度三个定时任务：健康检查（每 5 分钟）、空闲宿主机回收（每 3 分钟）和每日数据备份。

### 数据面（EC2 宿主机）

[Auto Scaling 组](https://docs.aws.amazon.com/autoscaling/ec2/userguide/auto-scaling-groups.html)管理支持[嵌套虚拟化](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html)的 EC2 实例（Intel c8i/m8i/r8i 系列）。每台宿主机运行多个 Firecracker microVM。Host Agent 服务每 5 秒轮询所有本地 VM，执行三项功能：

1. **健康监控** — Ping 每个 VM 并探测 OpenClaw Gateway 端口（18789）。将健康状态直接写入 DynamoDB，当 Gateway 响应正常时将租户从 `creating` 提升为 `running`。
2. **自动恢复** — 检测 `vm.json` 存在但 Firecracker 进程未运行的 VM（例如意外崩溃后），自动重新启动。
3. **Balloon 内存管理** — 读取宿主机 `/proc/meminfo`，根据宿主机内存压力调整 Firecracker Balloon 设备以回收或归还内存。

### 接入层

[Amazon CloudFront](https://aws.amazon.com/cloudfront/) 提供 HTTPS 终止，无需自定义域名或 ACM 证书。[Application Load Balancer](https://aws.amazon.com/elasticloadbalancing/application-load-balancer/) 使用基于路径的规则（`/vm/{tenant-id}/`）将请求路由到正确的宿主机。每台宿主机上的 Nginx 反向代理到租户的 microVM Gateway，支持 WebSocket 以实现交互式 Dashboard 访问。Nginx 配置由 VM 启动和停止脚本自动管理。

## 部署架构

整个基础设施定义为单个 [AWS CDK](https://aws.amazon.com/cdk/) Stack，一条命令即可完成部署。

*图 2. 部署架构，展示 AWS 服务及其关系。*

![部署架构](docs/oc-deploy-arch.png)

部署创建以下资源：

- 两张 DynamoDB 表（`tenants`、`hosts`），按需计费
- 一个 S3 存储桶，用于 rootfs 镜像、数据模板、备份、共享 Skills、配置模板和 Web 控制台
- 七个 Lambda 函数（API、健康检查、缩容器、备份、Skills、模板、AgentCore 工具）
- API Gateway，含使用计划和 API Key 认证
- Auto Scaling 组，含生命周期钩子用于宿主机初始化和优雅终止清理
- ALB，含基于路径的路由用于租户 Dashboard 访问
- CloudFront 分配，含 S3 源用于 Web 控制台，以及 CloudFront Function 用于 URL 重写
- 可选的 [Amazon Cognito](https://aws.amazon.com/cognito/) 用户池用于控制台认证
- 可选的 [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) 集成（Gateway、Memory、Code Interpreter、Browser）

## 租户隔离机制

每个 microVM 在多个层面实现隔离：

**内核隔离。** 每个租户在 Firecracker microVM 内运行独立的 Linux 内核。与容器方案中所有租户共享宿主机内核不同，一个租户内核中的漏洞不会影响其他租户。

**文件系统隔离。** rootfs 使用 OverlayFS，包含共享的只读基础层和每 VM 独立的可写覆盖层（2 GB 稀疏文件）。自定义的 `/sbin/overlay-init` 脚本作为 VM 的 init 进程运行，在移交给 systemd 之前挂载 overlay。每个租户还拥有独立的数据卷（`data.ext4`），挂载在 `/home/agent`，在重启和 rootfs 重置后持久保留。

**网络隔离。** 每个 VM 获得专用的 TAP 设备和一个 `/24` 子网（例如，VM 1 使用 `172.16.1.0/24`，VM 2 使用 `172.16.2.0/24`）。出站流量通过宿主机默认接口的 iptables MASQUERADE 转发。VM 子网之间没有路由——租户在网络层面完全不可见。

**访问隔离。** 每个租户的 OpenClaw Gateway 在创建时生成唯一的认证 Token（48 字符十六进制）。Token 存储在 DynamoDB 中，并包含在控制台的"Open Dashboard"链接中，实现一键访问。

## 每个 OpenClaw 租户包含什么

每个租户是一个运行在 Firecracker microVM 内的完整、自包含的 OpenClaw 环境：

- **OpenClaw Gateway** — Agent 的 HTTP/WebSocket 服务器（端口 18789），作为 systemd 用户服务自动启动。提供交互式 Dashboard，用于与 Agent 对话、管理会话和审批工具使用。
- **OpenClaw CLI** — 全局预装。租户可以直接运行 `openclaw` 命令。
- **可配置的 LLM 后端** — 每个租户的 `openclaw.json` 指定模型提供商、API Key 和模型 ID。你可以使用配置模板标准化设置，也可以按租户自定义。
- **完整开发工具链** — Python 3.12、Node.js 22、uv、git、GitHub CLI、build-essential 和常用工具（curl、jq、htop、tmux、tree）。
- **共享 Skills，独立记忆** — 所有租户共享集中管理的 Skill 集（从 S3 同步），而每个租户的对话历史和工作空间完全隔离在各自的数据卷上。
- **可选 AgentCore 集成** — 启用后，每个租户自动连接 Amazon Bedrock AgentCore，获得 MCP 工具、持久化记忆、代码解释器和浏览器能力。

## Rootfs 管理与预装工具链

`build-rootfs.sh` 脚本基于 Ubuntu Noble (24.04) 使用 debootstrap 生成两个镜像：

- **rootfs**（只读，共享）— 基础操作系统，预装工具：Python 3.12、Node.js 22、uv（Python 包管理器）、git、GitHub CLI (gh)、OpenClaw CLI、curl、jq、htop、tmux、tree、vim 和 build-essential。
- **data template**（每租户复制）— `/home/agent` 目录，包含 OpenClaw 配置、Gateway 服务定义和工作空间结构。

两个镜像使用 pigz 压缩后上传到 S3，附带 `manifest.json` 版本指针。宿主机追踪其 rootfs 版本，`POST /hosts/refresh-rootfs` API 可将新镜像推送到所有活跃宿主机。新租户自动使用最新版本；已有租户可通过 `reset` 操作更新，该操作替换 overlay 层但保留数据卷。

## 租户生命周期操作

API 支持完整的生命周期操作，每个操作通过 [AWS Systems Manager](https://aws.amazon.com/systems-manager/) Run Command 发送到宿主机执行：

| 操作 | 行为 | 使用场景 |
|------|------|---------|
| `create` | 分配资源、启动 microVM、配置网络和 ALB 路由 | 新建租户 |
| `delete` | 停止 VM、移除 ALB 规则、更新宿主机计数。可选 `?keep_data=true` 保留数据卷 | 删除租户 |
| `stop` / `start` | 优雅关机 / 冷启动（复用现有磁盘） | 维护窗口 |
| `pause` / `resume` | 通过 Firecracker API 冻结/解冻 vCPU（即时，亚毫秒级） | 临时挂起 |
| `restart` | 先停止再启动（复用磁盘，快速） | 故障恢复 |
| `reset` | 删除 overlay、重新启动（数据卷保留，rootfs 刷新到最新版本） | 系统更新 |
| `backup` | 异步：暂停 VM、压缩数据卷、恢复 VM、上传到 S3 | 数据保护 |

创建租户时，你可以指定 `config_template` 参数，从 S3 管理的模板中应用预配置的 OpenClaw 配置（LLM 提供商、模型、API Key）。也可以使用 `restore_from` 从已有备份创建新租户。

## 共享 Skills

所有租户共享统一的 Skills（SKILL.md 文件），由 S3 集中管理，每个租户的记忆保持独立。同步链路如下：

1. 你将 Skills 上传到 `s3://{bucket}/skills/`（例如通过 `aws s3 sync`）
2. 每台宿主机上的 cron 任务每 5 分钟从 S3 同步到 `/data/shared-skills/`
3. 新 VM 启动时，`launch-vm.sh` 挂载数据卷并将 Skills 复制到 `.openclaw/skills/`
4. 运行中的 VM 在下一次宿主机同步周期接收更新的 Skills

## 自动扩缩容与空闲回收

**扩容。** 当你创建租户但没有宿主机拥有足够资源（考虑超配比例后）时，租户进入 `pending` 状态，控制面增加 Auto Scaling 组的期望容量。宿主机初始化过程约需 3–5 分钟（KVM 设置、Firecracker 安装、从 S3 下载 rootfs、DynamoDB 自注册、生命周期钩子完成）。`EC2 Instance Launch Successful` 事件的 EventBridge 规则触发 API Lambda，将所有 pending 租户分配到新可用的宿主机。

**缩容。** Scaler Lambda 每 3 分钟运行一次，实施两轮确认机制以防止误终止：

1. `vm_count=0` 超过配置的 `idle_timeout_minutes`（默认 10 分钟）的宿主机被标记为 `idle`
2. 下一轮检查时，如果宿主机仍然空闲且 Auto Scaling 组的期望容量超过最小值，则通过 ASG API 终止该宿主机（`ShouldDecrementDesiredCapacity=True`）
3. 如果在两轮之间有租户被分配到该宿主机，Scaler 自动将宿主机恢复为 `active` 状态

终止生命周期钩子触发 API Lambda 清理终止宿主机上所有租户的 DynamoDB 记录和 ALB 规则。

## 两级健康监控

健康监控采用两级架构以确保可靠性：

**一级：Host Agent（每 5 秒）。** Agent 作为 systemd 服务运行在每台宿主机上，通过 ping 和 HTTP 探测所有本地 VM。它将健康状态直接写入 DynamoDB，避免了逐租户 SSM 命令的延迟和成本。当 VM 从 `creating` 转为健康状态时，Agent 通过 SSH 读取 Gateway Token 并将租户提升为 `running`。

**二级：Lambda Watchdog（每 5 分钟）。** 健康检查 Lambda 扫描健康数据过期（2 分钟无更新）的运行中租户。如果某台宿主机上的所有租户都过期，Lambda 判定 Host Agent 本身已宕机，通过 SSM 重启它（`systemctl restart host-agent`）。10 分钟冷却期防止重启风暴。

## 备份与恢复

EventBridge 每日触发所有运行中租户数据卷到 [Amazon S3](https://aws.amazon.com/s3/) 的备份。宿主机上的备份脚本执行以下流程：

1. 暂停 VM（Firecracker API，即时）
2. 使用 pigz（并行 gzip）压缩 `data.ext4`
3. 恢复 VM
4. 上传压缩文件到 `s3://{bucket}/backups/{tenant-id}/{timestamp}.gz`

`trap` 处理器确保即使压缩或上传失败，VM 也会自动恢复运行。S3 生命周期规则（通过 CDK CustomResource 配置）自动删除超过保留期（默认 7 天）的备份。

*图 3. Backups 页签，展示跨租户备份管理和孤儿备份检测。*

![Web 控制台 — Backups 页签](docs/web_console_backup.png)

`GET /backups` API 返回所有租户的所有备份，与租户表左连接以标记每个备份为 `active`（源租户存在）或 `orphan`（源租户已删除）。恢复操作基于备份的数据卷创建新租户——源租户无需存在：

```bash
# 从指定租户的最新备份恢复（源租户可以已被删除）
curl -s -X POST "${API_URL}tenants" \
  -H "x-api-key: ${API_KEY}" \
  -d '{
    "name": "restored-agent",
    "vcpu": 2, "mem_mb": 4096,
    "restore_from": {"tenant_id": "original-agent-ab12"}
  }' | jq .

# 从指定时间戳的备份恢复
curl -s -X POST "${API_URL}tenants" \
  -H "x-api-key: ${API_KEY}" \
  -d '{
    "name": "restored-agent",
    "restore_from": {"tenant_id": "original-agent-ab12", "timestamp": "20260428-125402"}
  }' | jq .
```

## 成本优化：多层超配与资源效率

OpenClaw Pool 实现了从计算调度到存储效率的多层成本优化。下表总结了每一层及其效果：

| 优化层 | 机制 | 典型收益 | 配置方式 |
|--------|------|---------|---------|
| CPU 超配 | 调度超过物理核心数的 vCPU。AI Agent 通常是突发型负载——大部分时间空闲，推理时短暂活跃。 | 2 倍密度（默认） | `cpu_overcommit_ratio: 2.0` |
| 内存超配 + Balloon | 调度超过物理 RAM 的内存。Firecracker Balloon 设备动态回收 VM 空闲内存，需要时归还。 | 1.5 倍密度 | `mem_overcommit_ratio: 1.5` + `balloon.enabled: true` |
| 共享 rootfs（OverlayFS） | 同一宿主机上所有 VM 共享一个只读 rootfs 镜像（~1.5 GB）。每个 VM 仅在 2 GB 稀疏 overlay 文件中存储差异写入。实际磁盘占用通常为 50–200 MB。 | rootfs 存储节省 ~90% | 内置，无需配置 |
| 稀疏数据卷 | 数据卷使用 `cp --sparse=always` 进行模板复制，解压后使用 `fallocate --dig-holes`。名义上 8 GB 的数据卷实际可能仅占用 1–2 GB 磁盘块。 | 数据存储节省 ~75% | 内置 |
| Spot 实例 | 宿主机使用 EC2 Spot 定价。 | 计算成本降低 60–70% | `asg.use_spot: true` |
| 空闲宿主机自动回收 | 两轮确认：空宿主机先标记为 idle，下一轮仍空闲则终止。不为零租户的宿主机付费。 | 消除空闲浪费 | `scaler.idle_timeout_minutes: 10` |
| 无服务器控制面 | Lambda + DynamoDB 按需计费 + EventBridge。无 API 调用或定时事件时零成本。 | 基线成本接近零 | 内置 |
| Firecracker 极低开销 | 每个 microVM 的 VMM 进程本身仅消耗 <5 MB 宿主机内存，而完整 QEMU/KVM 虚拟机需要 ~500 MB+。 | 每 VM 开销降低 ~100 倍 | 内置 |

### CPU 超配详解

AI Agent 工作负载天然具有突发性：Agent 大部分时间在等待用户输入或 LLM API 响应，仅在工具执行期间短暂占用 CPU。`cpu_overcommit_ratio` 为 2.0 意味着 8 vCPU 的宿主机可以跨租户调度 16 个 vCPU。宿主机上的 Linux CFS 调度器透明地对物理核心进行时间片共享。只要不是所有租户同时突发——对于 AI Agent 工作负载这在统计上是不太可能的——这种方式就是安全的。

### 内存超配与 Balloon 详解

内存超配需要 Firecracker Balloon 设备才能真正生效。不启用时，超配仅是调度层面的优化——如果物理内存耗尽，宿主机内核可能 OOM-kill Firecracker 进程。

```yaml
balloon:
  enabled: true
  max_inflate_ratio: 0.4       # 最多回收 VM 声明内存的 40%
  min_guest_available_mb: 512  # Guest 可用内存不低于 512 MB
  deflate_on_oom: true         # Guest OOM 时自动归还内存
  free_page_reporting: true    # Guest 主动报告空闲页
```

Host Agent 每 5 秒读取 `/proc/meminfo` 并调整 Balloon 目标：

- 宿主机可用内存 < 20% → inflate（从 VM 回收内存到宿主机）
- 宿主机可用内存 > 40% → deflate（归还内存给 VM）
- 20%–40% 迟滞区间防止振荡
- 每个 VM 至少保留 `min_guest_available_mb`，防止过度回收

### 成本示例

以单台 `m8i.2xlarge` 宿主机（8 vCPU，32 GB RAM，us-east-1 按需约 $0.46/小时）为例：

| 场景 | 超配配置 | 每宿主机租户数 | 每租户每小时成本 |
|------|---------|-------------|---------------|
| 不超配 | 1.0x CPU, 1.0x Mem | 3（每个 2C/8G） | ~$0.15 |
| 仅 CPU 超配 | 2.0x CPU, 1.0x Mem | 6 | ~$0.08 |
| 全面超配 | 2.0x CPU, 1.5x Mem | 8–10 | ~$0.05 |
| 全面超配 + Spot | 2.0x CPU, 1.5x Mem, Spot | 8–10 | ~$0.015 |

Web 控制台在宿主机资源条上可视化超配：标记线指示物理容量边界，填充区域在分配超配资源时延伸到标记线之外。

## Web 管理控制台

项目包含一个基于 Alpine.js 的零依赖单页 Web 管理控制台，托管在 CloudFront 上。

*图 4. Tenants 页签，展示宿主机资源利用率和租户状态。*

![Web 控制台 — Tenants 页签](docs/web_console.png)

控制台提供四个页签：

- **Tenants** — 宿主机资源仪表盘（CPU/内存利用率条，含超配可视化和物理容量标记线）、租户列表（VM 和 Gateway 健康指示器）、一键操作（启动、停止、暂停、恢复、重启、重置、删除、打开 Dashboard）。支持按宿主机和状态过滤。
- **Application** — 配置模板 CRUD（创建、编辑、删除不同 LLM 提供商的 OpenClaw JSON 配置）、共享 Skills 列表（描述从 SKILL.md frontmatter 解析）。
- **Backups** — 跨租户备份浏览器（按租户分组，可折叠历史记录）、孤儿备份过滤、大小和时间戳显示、一键恢复到新租户。
- **Settings** — API 连接配置（URL 和 Key，含可见性切换）、AgentCore 状态、系统版本和 GitHub 项目链接。

可选的 Cognito 认证通过 OAuth2 隐式流保护控制台——启用后，用户必须登录才能访问任何页签。

## 可选：AgentCore 集成

在 `config.yml` 中启用 AgentCore 后，CDK Stack 会创建以下 [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) 资源：

- **Gateway** — MCP 兼容的工具中心，含 Lambda 支持的工具（通过 `agentcore_tools` Lambda 注册）。Gateway URL 自动注入到每个 VM 的 `openclaw.json` 中作为 MCP Server。
- **Memory** — 每租户持久化记忆，支持语义和用户偏好策略，按租户 ID 命名空间隔离。
- **Code Interpreter** — 安全沙箱 Python 执行环境。
- **Browser** — 云端 Web 自动化能力。
- **Workload Identity** — Agent AWS 访问的 IAM 身份。

每个 VM 在启动时自动连接这些服务——无需逐租户配置。

## 注意事项与限制

在评估本方案是否适合你的场景时，请考虑以下因素：

- **租户规模上限。** ALB Listener 默认支持最多 100 条规则（申请配额提升后为 200 条）。每个租户需要一条基于路径的规则，因此单集群最大租户数约为 200。更大规模的部署需要修改路由架构（例如基于 Host Header 的路由或自定义代理层）。
- **实例系列要求。** AWS 上的嵌套虚拟化目前仅支持 Intel 实例（c8i、m8i、r8i 系列）。不支持 AMD 和 Graviton 实例。
- **不支持 GPU 直通。** Firecracker 不支持 GPU 设备直通。本方案适用于基于 CPU 的 AI Agent 工作负载。
- **单可用区部署。** 默认部署使用默认 VPC 中的单个可用区。需要高可用的生产工作负载需要扩展架构以支持跨可用区。
- **示例项目定位。** 本项目作为参考架构发布在 [aws-samples](https://github.com/aws-samples) 下，旨在用于学习和实验，未经额外加固（例如静态加密、VPC 端点、WAF 集成）不建议直接用于生产环境。
- **DynamoDB Scan 操作。** 租户和宿主机列表操作使用 DynamoDB 全表 Scan，随着租户数量增长效率可能下降。当部署接近 200 租户上限时，建议添加全局二级索引。
- **SSM 命令并发。** 所有 VM 操作通过 SSM Run Command 执行。对大量租户的并发操作可能受到 SSM API 限流限制。
- **Spot 实例风险。** 可选的 Spot 实例模式（`use_spot: true`）可降低 60–70% 成本，但存在实例被回收的风险。被回收宿主机上的所有租户将被终止（终止生命周期钩子会清理 DynamoDB 记录）。

## 最佳实践

在部署和运维 OpenClaw Pool 时，我们建议：

- 从保守的超配比例开始（CPU 1.5 倍、内存 1.0 倍），根据观察到的工作负载模式逐步调整。监控 Host Agent 的 Balloon 统计数据以了解实际内存利用率。
- 使用内存超配时务必启用 Balloon 设备。不启用时，内存超配仅是调度层面的优化——如果物理内存耗尽，宿主机内核可能 OOM-kill Firecracker 进程。
- 使用配置模板标准化各租户的 LLM 提供商设置，减少配置漂移。`default` 模板受保护，不可删除。
- 需要更新租户基础系统时，使用 `reset` 操作（保留数据卷，将 rootfs overlay 刷新到最新版本），而非删除后重建。
- 为降低成本，非关键工作负载（开发、测试、Hackathon）可考虑 Spot 实例。`keep_data_volume: true` 设置在宿主机终止时保留 EBS 数据卷，允许数据恢复。
- 根据备份保留需求设置合适的 S3 生命周期规则。默认 7 天保留期在存储成本和恢复灵活性之间取得平衡。

## 清理资源

移除所有已部署的资源：

```bash
./scripts/destroy.sh           # 销毁 CDK Stack，保留 S3 存储桶和 DynamoDB 表
./scripts/destroy.sh --purge   # 彻底清理，包括 S3 数据、DynamoDB 表、孤立 IAM 角色和 EBS 卷
```

## 总结

OpenClaw Pool 展示了 Firecracker microVM 如何在保持无服务器控制面运维简洁性的同时，为 AI Agent 工作负载提供内核级租户隔离。通过将 Firecracker 的安全特性与 AWS 托管服务（用于调度、扩缩容和可观测性）相结合，你可以通过单次 CDK 部署和一套 REST API 部署多达 200 个隔离的 AI Agent 实例。

本方案适用于内部 AI 平台、培训环境、Hackathon 和概念验证部署——这些场景需要强租户隔离，但不希望承担 Kubernetes 的运维开销或每租户独立 EC2 实例的成本。

完整源代码、部署说明和 API 参考请访问 GitHub：

**[https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker)**

## 相关资源

- [Firecracker microVM](https://firecracker-microvm.github.io/) — 本方案使用的开源 VMM
- [AWS CDK](https://aws.amazon.com/cdk/) — 用于部署的基础设施即代码框架
- [Amazon EC2 嵌套虚拟化](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html) — 在 EC2 上运行 Firecracker 的前置条件
- [使用 AWS Lambda 租户隔离模式构建多租户 SaaS 应用](https://aws.amazon.com/blogs/compute/building-multi-tenant-saas-applications-with-aws-lambdas-new-tenant-isolation-mode/) — AWS 多租户隔离的相关方案
- [AWS Well-Architected Framework SaaS Lens](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/saas-lens.html) — AWS 上 SaaS 架构的最佳实践
- [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) — 可选集成，提供 Agent 工具网关、记忆和代码解释器

---

### 关于作者

**Neo Sun** 是 Amazon Web Services 的解决方案架构师，专注于金融服务行业的 AI/ML 和云原生架构。他与亚太区客户合作，在 AWS 上设计和实施可扩展的解决方案。

**Aleck Lin** 是 Amazon Web Services 的解决方案架构师，专注于无服务器和容器架构。他帮助客户在 AWS 上构建现代化应用，关注卓越运营和成本优化。