# 规划部署

本节介绍部署此解决方案前应考虑的成本、安全性、支持的 AWS 区域与服务配额。

## 成本

您需要承担运行此解决方案时所使用的 AWS 服务的费用。截至本指南撰写时，按 80% Savings Plans 加 20% Spot 实例测算，该解决方案的计算成本约为 $8.36/租户/月（成本推算，非实测）。该数字仅摊算裸金属实例小时，未计入 EBS 数据盘、内存超卖比（默认 1.5）等因素，含这些后单租户实际成本可能显著更高；实际成本取决于宿主机型、租户密度、大模型调用量与可选监控组件的启用情况，请以 AWS Pricing Calculator 为准。

> **Important**
>
> 此估算为成本推算，未包含大模型推理、第三方数据 API、数据传输等费用，也未包含可选监控平台（自建 Amazon EC2 上的 Prometheus、Grafana、Wazuh）与可选托管安全服务（Amazon GuardDuty、Amazon OpenSearch Service）的费用。请以 AWS Pricing Calculator 结合目标区域与实际用量为准。建议为该解决方案创建预算，并通过 AWS Cost Explorer 跟踪实际支出。

平台的主要成本构成如下。

| AWS 服务                 | 计费维度                         | 说明                                                           |
| ------------------------ | -------------------------------- | -------------------------------------------------------------- |
| Amazon EC2（裸金属宿主） | 实例小时（Savings Plans / Spot） | 主要成本项。承载所有租户 microVM，生产用 Graviton Arm64 裸金属 |
| Amazon EBS               | 预置容量（gp3，加密）            | 每台宿主挂载数据卷承载所有 microVM 的稀疏盘与镜像 overlay      |
| AWS Lambda               | 请求数 + 运行时长                | 控制面 API、健康检查、扩缩容、备份函数                         |
| Amazon DynamoDB          | 按需读写 + 存储 + 时间点恢复     | 租户、宿主、技能分组、审计元数据                               |
| Amazon S3                | 存储 + 请求                      | 黄金镜像、租户备份、日志归档                                   |
| Amazon API Gateway       | 请求数                           | 控制面 REST API                                                |
| Amazon CloudFront        | 数据传输 + 请求                  | 公网唯一入口，反代 console/chat 静态资产与数据面（回源 ALB）   |
| Elastic Load Balancing   | 负载均衡器小时 + LCU             | Application Load Balancer，接 CloudFront 回源                  |
| Amazon CloudWatch        | 日志摄取 + 存储 + 指标           | 控制面日志与指标，日志组默认保留期见架构详情                   |

> **Note**
>
> 平台的每租户计费虚拟密钥（LiteLLM virtual key）将大模型调用花费按租户（`tenant_id`）拆分，便于将推理成本归因到具体租户。该能力默认关闭，需在配置中启用计费段后生效。

## 安全性

在 AWS 上构建系统时，安全责任由您和 AWS 共同承担。此责任共担模型减轻了您的运营负担。有关更多信息，请参阅 AWS 责任共担模型。

本节介绍平台在身份、网络、加密与公网暴露方面的安全设计。完整的五层安全模型见架构详情。

### IAM 角色

AWS Identity and Access Management (IAM) 角色允许客户向 AWS 服务和服务上的资源分配精细的访问策略和权限。平台为控制面 Lambda、宿主实例、监控组件分别创建最小权限 IAM 角色：

- 控制面 Lambda 角色仅在配置了计费段时才被授予对大模型主密钥的 `secretsmanager:GetSecretValue` 权限，并按密钥名收窄资源范围。
- 宿主实例角色具备读取黄金镜像、查询 CloudFormation 输出、下发生命周期命令所需的最小权限；虚拟机内为零凭据，不持有任何长期 AWS 凭据。
- 监控组件实例角色仅具备写入自身日志组与通知主题的权限。

### 身份与访问控制

平台的身份分两个平面。**数据面**以 OpenClaw 原生 Ed25519 device 非对称认证为身份根：平台后端代持 device 私钥、公钥冷注入 microVM，microVM gateway 端验签；不依赖 Amazon Cognito。**控制面**以 `x-api-key` 为第一道门，启用 RBAC 时叠加 Amazon Cognito 令牌验签（RS256 + JWKS），验签失败一律降级到最小权限（只读 viewer）。平台默认启用基于角色的访问控制（RBAC，`console_auth.rbac_enabled` 默认 `true`），强制按路由的角色检查与资源属主检查；即使关闭 RBAC，资源属主（`owner_id`）检查仍独立生效。Amazon Cognito 在当前部署代码中仅用于控制面 console 登录（默认关闭）。

### 网络与公网暴露

平台以 Amazon CloudFront 作为公网唯一入口（CloudFront 回源到 Application Load Balancer），不直接对全网开放后端。安全组入站不对 `0.0.0.0/0` 开放：宿主探针、监控平台、数据库与管理端口的入站仅放行 VPC CIDR、CloudFront 托管前缀列表或堡垒主机安全组。

平台在宿主 iptables 上对每台 microVM 强制三类丢弃规则：丢弃跨租户东西向流量、丢弃虚拟机访问实例元数据服务（IMDS）的流量、丢弃虚拟机访问宿主管理端口的流量。Firecracker 自身不过滤流量，跨租户网络隔离完全由宿主 iptables 承担。

> **Important**
>
> 平台默认删除 OpenClaw 原生的 OpenAI 兼容 `chatCompletions` HTTP 端点。该端点是一条直通大模型、绕过设备认证的旁路，全局开启会给每个租户增加一个对外攻击面。正确做法是按需做成每租户开关——该开关已实现且默认关闭：注册租户时传 `chat_endpoint_enabled: true` 才会为该租户在启动时保留此端点，默认不注入。不要全局开启。

### 加密

平台的所有数据存储默认加密：Amazon S3 资产桶封锁全部公网访问（4 个公网封锁开关全开）并强制 HTTPS（拒绝非 TLS 请求），Amazon DynamoDB 表静态加密。

> **Note**
>
> 审计日志表当前使用 AWS 拥有的密钥加密；客户托管密钥（CMK）仅规划在全新账户首次部署时启用，因为对现存保留（RETAIN）表在线切换加密会强制替换并丢失审计数据。做容灾规划时请按"审计表当前非 CMK"对待。

### VPC

平台部署在 Amazon Virtual Private Cloud (Amazon VPC) 内。默认启用 VPC Flow Logs（`traffic_type=ALL`），投递到受限保留期的 Amazon CloudWatch 日志组，用于检测跨租户东西向异常、验证 iptables 隔离是否真正生效。可选启用 Amazon Route 53 Resolver DNS Firewall，在 DNS 解析层拦截虚拟机解析已知恶意域名（默认关闭）。

## 支持的 AWS 区域

平台使用的核心 AWS 服务（AWS Lambda、Amazon API Gateway、Amazon S3、Amazon DynamoDB、Amazon Cognito、Amazon CloudFront、Amazon EC2）在多数 AWS 区域均可用。平台的生产底座要求目标区域提供 AWS Graviton（Arm64）裸金属实例机型，并提供 Amazon Bedrock 及其 Guardrails 能力。

> **Note**
>
> 部署前请确认目标区域同时支持所需的 Graviton 裸金属机型与 Amazon Bedrock Guardrails。Amazon Bedrock 与部分机型的区域可用性会随时间变化，请以 AWS 区域服务列表为准。

## 配额

平台使用的某些 AWS 服务有配额限制。请在部署和扩容前确认以下配额满足目标规模。

| 服务 / 资源         | 关注的配额                          | 说明                                                              |
| ------------------- | ----------------------------------- | ----------------------------------------------------------------- |
| Amazon EC2          | 目标裸金属机型的实例配额、Spot 配额 | 决定可拉起的宿主数量与租户总容量                                  |
| AWS Systems Manager | 单实例并发命令数                    | 大规模建租户的真实瓶颈，需启用 Amazon SQS 削峰队列规避            |
| Amazon ElastiCache  | 节点内存与连接数（数据面 opt-in）   | 边缘路由表；租户级路由查 Redis `route:{tenant_id}`，不占 ALB 规则 |
| AWS Lambda          | 并发执行数                          | 控制面与生命周期函数的并发                                        |
| Amazon DynamoDB     | 按需吞吐                            | 按需计费随流量自动伸缩                                            |

> **Important**
>
> 大规模建租户的真实瓶颈是 AWS Systems Manager 的单实例并发，而非计算容量。一次性在单台宿主上建满会瞬间超出 Systems Manager 单实例并发配额，导致部分请求永久停留在 `creating` 状态。扩容前必须启用 Amazon SQS 削峰队列（`scaler.lifecycle_queue_enabled=true`），将生命周期操作摊平成受控并发速率（单台裸金属建议约 5–10 并发），不要用客户端高并发直接调用注册接口。

> **Note**
>
> 平台的单租户资源配额检查（`QUOTAS_ENABLED`）默认关闭。容量按配置文件调节，单台宿主全健康承载实测为 187 个节点（受磁盘瓶颈约束），更高密度的稳态承载需补充压测才能定论；380 租户/台为容量推算。
