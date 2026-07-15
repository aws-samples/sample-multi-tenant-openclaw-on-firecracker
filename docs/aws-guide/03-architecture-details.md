# 架构详情

> 数据面详见第 13 章（自 2026-07-08 起为唯一有效模型）。

本节介绍构成此解决方案的组件和 AWS 服务，以及这些组件如何协同工作的架构详情。该解决方案由三大组件构成：负责注册、生命周期、备份与注销的控制面（生命周期管理平面）；按租户把实时聊天直连到虚拟机 gateway 的数据面（两级路由）；以及为每个租户运行带身份、技能与护栏的 OpenClaw AI agent 的租户 microVM（基于 Firecracker 的独立内核运行时）。三者职责正交：控制面只做生命周期管理、运行后不向 microVM 注入业务数据；数据面只做按租户的消息路由；租户 microVM 是启动即成品的隔离运行时。安全模型在这三个组件之上以五层纵深防御叠加，详见本节的「纵深防御：五层安全模型」。

## 控制面（生命周期管理平面）

本节介绍控制面的组件构成与职责边界。该组件由 AWS Lambda、Amazon DynamoDB 与 Amazon API Gateway 构成，通过 AWS Cloud Development Kit (AWS CDK) 部署，负责租户的注册、启停、备份、迁移与注销，自身不参与实时聊天链路。

控制面对外暴露一组 REST API，全部经 Amazon API Gateway 接入并要求 `x-api-key`，后端由一个 AWS Lambda 函数承载租户注册、生命周期操作、备份、host/skill/group 管理与审计日志等端点。控制面状态持久化在 Amazon DynamoDB，其中三张核心状态表按职责划分如下（另有 audit 审计表及 batch-jobs、tenant-idp-map 等运维/联邦辅助表）：

- **tenants 表**：以 `id` 为主键的 schemaless 表，按需写入 `owner_id`、`tenant_user_id`、`authorized_users`、`litellm_vkey` 等字段，承载租户元数据与授权记录；gateway token 与 device 私钥的 KMS 密文另存 `openclaw-tenant-secrets` 表。该表不存在表级完整 schema 可对照。
- **hosts 表**：记录 host 节点状态、规格与容量计数（考虑 overcommit 比率，可用容量 =（total × ratio）− used）。
- **groups 表**：记录每个 group 的 skill 集合，用于按 group 解析租户的有效技能。

控制面遵循「零运行时操作」纪律：microVM 启动后，控制面对运行中的 VM 不做任何行为或数据注入，host 只做生命周期、资源与网络管理。修改租户身份、技能或配置需要重新构建镜像并滚动重建，而非调用运行时 API。身份、技能、凭据与配置在 VM 启动前冷注入到只读黄金镜像盘与数据盘，运行后不开启 host→VM 的批量热注入通道。

控制面注册请求定义每台 VM 的启动参数：`tenant_id`、`vm_num`，以及 `vcpu`（脚本默认 2）、`mem_mb`（脚本默认 4096）。

> **Note**
>
> `vcpu` 默认 2、`mem_mb` 默认 4096 是启动脚本的默认值，与按 2GB/VM 推算的稳态承载之间存在口径差异（默认值偏大）。本指南的容量说明统一以 2GB/VM 口径为准。

控制面的生命周期操作具备时间锚点的实测数据：注册 API 返回约 1.7s、creating→running 可用约 4.0s、休眠（stop→stopped）约 6.0s、唤醒（start→running）约 3.7s、备份约 6.6s（产物约 9.5MB）、注销（DELETE→deleted）约 12.2s、备份恢复约 16s（9MB 备份，均为生命周期实测）。

控制面表的数据保护与租户业务数据的保护是两道独立机制：tenants、hosts、groups、audit 四张 `RETAIN` 表均开启时间点恢复（Point-in-Time Recovery，PITR，默认 35 天连续备份，可恢复到恢复期内任意时点），防控制面元数据被误删、误改或坏写后无从回滚；而租户业务数据由备份 Lambda 投递到启用了对象锁定（Object Lock COMPLIANCE）的 Amazon S3 桶。PITR config-gated（`dynamodb.point_in_time_recovery` 默认 true），代码默认即开、随重建继承。

> **Note**
>
> Amazon DynamoDB 审计表的客户主密钥（CMK）加密目前属于规划（未实现）。现存表为 `RETAIN`，在线切换加密会强制 replace 并丢失审计数据，因此仅规划在全新账户首次部署时启用。

## 数据面（两级路由）

本节介绍数据面的连接模型与身份平面。数据面把浏览器或前端的实时聊天按 `tenant_id` 直连到对应 microVM 内的 OpenClaw gateway，microVM 只对宿主内部 TAP 网卡暴露 gateway 端口、不开放任何公网入站。完整链路、超时链与十万级规模化基线见 [第 13 章 · 数据面两级路由](13-data-plane-redesign.md)，本节只给连接模型与身份平面的概览。

数据面为 opt-in 能力，由 `config.yml` 的 `edge.enabled` 与 `redis.enabled` 控制，默认关闭；启用后链路为：

```
浏览器 ── wss /gw/ws ─▶ 平台后端 WebSocket 网关
  平台后端网关 ── ws ─▶ CloudFront ─▶ ALB ─▶ OpenResty 边缘 ASG
    OpenResty 边缘 ── 查 ElastiCache Redis route:{tenant_id} ─▶ 宿主 iptables DNAT ─▶ microVM gateway:18789
```

身份分两个正交平面，均不再依赖 Amazon Cognito：

- **第一跳（浏览器 ↔ 平台后端网关）**：浏览器带平台会话令牌经 `?token=` 连 `/gw/ws`，平台后端以自签 HS256 JWT（对称密钥，非 Cognito RS256）验签，失败即断连（fail-closed）。前端不接触任何密钥。
- **第二跳（平台后端网关 ↔ microVM gateway）**：平台后端作为 WebSocket 客户端连到边缘暴露的 `/ws/{tenant_id}`，用 OpenClaw 原生 Ed25519 device 非对称握手认证——收到 `connect.challenge` 后用 device 私钥签名回 `connect` 帧，同时携带 per-tenant gateway token（`Authorization: Bearer`）。device 私钥由平台后端服务端代持（从控制面取 KMS 密文后本地 `kms:Decrypt`），公钥以配对文件（`paired.json`）冷注入 microVM；`deviceId = SHA256(公钥 raw 32B)`。

gateway token 与 device 私钥都以 AWS KMS 信封加密存储（`EncryptionContext` 分别绑 `tenant_id` 与 `owner_id`），密文经 `GET /tenants/{id}` 交付给调用方，由调用方本地 `kms:Decrypt` 取明文；明文不落宿主磁盘、不进命令行、不入 AWS CloudTrail。

OpenResty 边缘本身不做业务鉴权，只做租户路由：按 `tenant_id` 查 Redis 路由表拿到 `{host, port, guest_ip}`，strip 掉 `/ws/{tenant_id}` 前缀后转发到 microVM gateway（gateway 不认带前缀的 URI），真正的身份校验在 microVM gateway 端完成。路由表由宿主 agent 在 VM 探活通过后写入 Redis（`route:{tenant_id}`，无 TTL、delete/migrate 时显式 `DEL`），边缘侧以三层缓存（worker 本地 lrucache → 共享字典 → Redis）读取，并在 ElastiCache failover 窗口内 fail-static 兜住旧值。

> 数据面详见第 13 章（自 2026-07-08 起为唯一有效模型）。媒体链路（出图/看图、会话历史回看）在两级路由下尚未重新落地，随仓库的 `finance-agent` 演示样本不含图片场景。

## 租户 microVM（OpenClaw agent 运行时）

本节介绍租户 microVM 的磁盘架构、身份注入方式与网络寻址模型。每个租户运行在一台独立内核的 Firecracker microVM 中，把「越权止于单租户」从容器级提升到 VM 级；microVM 内运行 OpenClaw agent，身份、技能与护栏在启动前冷注入。Firecracker 是 AWS 开源的轻量级 microVM 虚拟化技术，运行在 KVM 之上，启动开销低、隔离强。microVM 纯启动约 1.74s（metal 实测，p50），launch→gateway 可用约 6.48s（metal 实测）。

### 磁盘架构

Firecracker microVM 挂载四到五块盘，按 Firecracker PUT 顺序分配 `/dev/vd<N>`，root 固定为 `vda`，文件系统均为 ext4：

- **rootfs（vda）**：`is_read_only:true` + `is_root_device:true`，只读根。
- **overlay（vdb）**：`is_read_only:false`，per-VM 写时复制层。
- **data（vdc）**：`is_read_only:false`，承载租户配置与 skill。
- **immutable（vdd）**：`is_read_only:true`，身份与 skill 权威盘，承载身份文件与运维 skill；缺失则告警跳过（降级态）。immutable 盘必须在 data 之后 PUT，以确保 guest 看到的是 `/dev/vdd`（Firecracker 的盘符顺序约束）。
- **creds（vde，条件挂载）**：`is_read_only:true`，仅当租户带注入凭据（`injected_credentials`）时挂载，存放解密后的 dotenv（`.env`），ro-bind 进 `~/.openclaw/.env`。无注入凭据的租户仍是四块盘。

只读语义不依赖特殊的只读文件系统，而由三层叠加保证：① Firecracker 在 virtio-block 层以 `is_read_only:true` 设置写屏障，guest 内即便以 root 写入也会被底层挡住；② guest 启动后以 `mount -o ro /dev/vdd` 挂载；③ `openclaw-ro-harden` 把身份文件与护栏代码 bind 覆盖为只读。三层叠加后，以 root 写入身份文件或运维 skill 会被拒绝（Read-only file system）。

### 身份冷注入

身份、技能与配置遵循「启动前冷注入」：身份文件与运维 skill 烤进只读的 immutable 盘；租户专属配置（gateway token、device 配对文件 `paired.json`、per-tenant 计费 vkey）在启动前注入到 data 盘的 `openclaw.json` 与配对文件。VM 启动即为成品，控制面对运行中的 VM 不做行为或数据注入。修改租户身份需要重新构建镜像并重建，而非调用运行时 API。

数据面身份走 OpenClaw 原生 device 非对称认证：注册租户时控制面用 AWS KMS 铸出一对 Ed25519 device 密钥与一个 gateway token，私钥与 token 都以 KMS 信封加密后存 `openclaw-tenant-secrets` 表；启动 VM 时由部署脚本把 device 公钥组装成 `paired.json`（按 openclaw 2026.2.26 协议 v3，`roles` 含 operator、公钥匹配即过配对门）冷注入到盘，gateway token 密文经 KMS 解密后写入 `openclaw.json` 的 `.gateway.auth.token`。私钥与 token 明文从不烤进只读黄金镜像、从不下发给浏览器。

### 网络寻址模型

网络寻址采用每 VM 独占一个 /30 点对点链路（4 个地址：网络、host 端、guest 端、广播）。地址由 launch-vm 启动脚本按 `vm_num` 算出：`block=(vm_num-1)*4`，host 端 IP 为 `SUBNET_PREFIX.<o3>.<base+1>`、guest 端为 `SUBNET_PREFIX.<o3>.<base+2>`。所有链路都落在 `SUBNET_PREFIX/16`（生产用真实 `172.16`，文档占位 `10.0`）这张租户超网内，因此对 /16 生效的东西向 DROP 规则可覆盖每一台 VM。MAC 把 `vm_num` 编进后两字节，支持每台 host 上数百台 VM 而不冲突。每台 host 由 Amazon EC2 Auto Scaling Group（ASG）管理弹性伸缩与空闲回收，机型采用 metal 系列；单台 metal（r8g.metal-24xl）按 760GB÷2GB 推算稳态承载约 380 租户/台（推算），单台 metal 全 healthy 实测 187 节点（磁盘瓶颈，非内存上限，实测）。空白 VM RSS 约 609MB、整机均值约 669MB（实测）。

## 纵深防御：五层安全模型

本节介绍该解决方案的纵深防御安全模型。该模型由五层叠加构成，每一层都落到代码或 AWS 设备上，不依赖模型自律：L1 内容层（Amazon Bedrock Guardrails）、L2 工具执行层（sentinel-guard / acl-guard）、L3 身份层（OpenClaw 原生 device 认证 + 控制面 RBAC + owner 门控）、L4 网络层（iptables 三类 DROP + 出网默认拒绝白名单）、L5 凭据·只读·监控层。物理与网络隔离管「租户之间」，工具执行层与内容层管「一个 agent 被诱导或被注入后能干什么」。

### L1 内容层（Amazon Bedrock Guardrails）

本层管进出大语言模型的语言内容本身。在用户输入到达模型之前、模型输出返回用户之前，由 Amazon Bedrock Guardrails 这一 AWS 托管的内容护栏过一遍：识别并拒绝越狱与高危意图、按预定义类别过滤有害内容、对各类凭据与敏感串脱敏、专门识别中文场景的注入与社工话术。该层跑在 AWS 平台侧、与 agent 进程解耦，是纵深防御的最外层。越狱实测以 OWASP LLM Top 10 语料逐条实打：14 条攻击全拦 14/14、4 条正常对照零误伤 4/4（实测）。Guardrail 的资源标识（ID/ARN）为部署态值，本指南以占位符表示。

### L2 工具执行层（sentinel-guard / acl-guard）

本层钉在 agent 真正执行动作之前，不看 agent「想」做什么、只看 agent「要执行」的具体命令、要读的文件、要访问的地址，凡落入危险模式者在执行前直接拦下，采用 fail-closed（拿不准就拦）。该层由两个故意独立的后端构成，两者都烤在只读镜像内、跑在 agent 进程内：

- **sentinel-guard**：覆盖偷凭据、数据外泄、破坏性命令、反弹 shell、提权、资金操作强制确认、伪造内网请求（SSRF）、身份篡改、敏感信息脱敏、prompt 注入与恶意 skill 筛查、异常行为监控等多类威胁。其中资金操作强制确认是执行前的硬否决：任何提币、转账、真实下单类操作必须显式带「演练/预览/确认令牌」标记才放行，否则一律拒，不押在提示词约束上。
- **acl-guard**：先于 sentinel-guard 运行的凭据兜底层，按路径或目标匹配而非按命令名匹配，锁死对 SSH 私钥、AWS 凭据文件、进程环境变量密钥、云元数据凭据接口的引用，补上命令层可能漏掉的旁路（例如用 awk、dd、base64 等不常见工具读同一私钥文件）。

工具执行层护栏自带回归测试。对外的权威数字：面向恶意 skill 的针对性用例 41/41 全部拦截、凭据兜底护栏的针对性用例 8/8 全部命中（实测）。两个后端均 fail-closed，即便日志写不进去也维持拦截、绝不因记日志失败而放行。

### L3 身份层（device 认证 + 控制面 RBAC + owner 门控）

本层分两个平面。**数据面**以 OpenClaw 原生 Ed25519 device 非对称认证为身份根：平台后端用 device 私钥对 gateway 下发的 challenge 签名，microVM gateway 端用冷注入的公钥验签，配对门只放行 `paired.json` 里登记的公钥与角色；私钥服务端代持、不出前端，token 与私钥密文的 KMS `EncryptionContext` 绑 `tenant_id`/`owner_id`，解密上下文不匹配即拒（fail-closed）。数据面结构上不跨租户：一个 device 绑一个租户、边缘按 `tenant_id` 单键路由，路由不到就 404 而非串到别的租户。

**控制面**以 `x-api-key` 为第一道门，启用 RBAC 时叠加 Amazon Cognito 令牌验签：用 RS256 + JWKS 公钥验签 Cognito 签发的 token，`cognito:groups` 声明映射 viewer/operator/admin 三级角色，forged、expired、`alg:none`、错误 issuer 的 token 一律降级到只读；不带 Bearer 的纯 `x-api-key` 请求按 `DEFAULT_NO_JWT_ROLE`（默认 `viewer`）取角色。RBAC 门控自身是独立开关 `RBAC_ENABLED`，默认 `true`；即使关闭 RBAC，资源属主（`owner_id`）检查仍独立生效，不因关 RBAC 变成跨租户越权（IDOR）。

> **Note**
>
> Amazon Cognito 在当前部署代码中只用于控制面 console 登录（`console_auth.enabled` 默认 `false`，整段休眠），不在聊天数据面链路上。

### L4 网络层（iptables 三类 DROP + 出网默认拒绝白名单）

本层在 host 上以 iptables 强制跨租户与跨边界的网络隔离（Firecracker 自身不过滤流量，出网过滤全靠 host iptables）。所有安全 DROP 都插到链顶，确保排在追加的 ACCEPT/MASQUERADE 之前。三类 DROP 为：① IMDS 隔离，在 FORWARD 链 DROP guest 到 `169.254.169.254` 与 `169.254.169.253` 的流量，防租户经 MASQUERADE 到达 host 的实例元数据服务盗取 host EC2 instance-profile 凭据；② 东西向 L2 隔离，FORWARD 链 DROP guest 到整个租户超网 `SUBNET_PREFIX/16` 的流量，防包被路由进另一个租户的 /30；③ 管理面隔离，INPUT 链 DROP guest 到 host 的 8899/9090/22 端口。

出网方向支持默认拒绝白名单（config-gated `security.egress_allowlist_enabled`，默认 false，开启前 guest 出网无限制、行为不变）。开启后把 FORWARD 链末尾的无条件 ACCEPT 换成：放行 VPC CIDR 与运营自定义 CIDR（覆盖 LiteLLM、边缘、VPC Endpoint 等私网目标）、放行 host dnsmasq 按域名白名单解析出的真实 IP（内置 `s3.{region}.amazonaws.com` 等，运营可加第三方行情等域名），末尾对该租户到公网口补 DROP 兜底。域名白名单靠 host dnsmasq 实现：`nat/PREROUTING` 把 guest 的 :53 透明 DNAT 到 host dnsmasq，dnsmasq 边解析边把命中域名的 IP 灌进 ipset，FORWARD 用 `-m set --match-set` 放行——因此不改 guest 镜像（guest DNS 保持不变）即可收口。放行 `cognito-idp` 走域名白名单而非 IP 段，因为 AWS `ip-ranges.json` 未发布 Cognito 独立的地址段（只落在 AMAZON 大聚合段）。

需要说明：Amazon Route 53 Resolver DNS Firewall（config-gated `security.dns_firewall_enabled`，默认 false）作用于经 VPC Route 53 Resolver 的 DNS 查询；而 microVM 的 guest DNS 不经 VPC Resolver，因此 DNS Firewall 定位为带外/VPC 层的 C2 与数据外泄域名黑名单，与上述 tap 级出网白名单正交，不互相替代。收 microVM 出网以出网白名单为准。

跨租户隔离的核心数字：加固后东西向 100% 丢包（加固后目标态，加固代码已落地并经静态规则确认，新鲜 裸金属复测待核）；漏洞态（无 FORWARD DROP）下跨租户完全互通，0% 丢包、RTT 0.187ms（实测）。IMDS 在 IMDSv2 应用层无 token 时返回 401。

> **Note**
>
> IMDS DROP 当前只覆盖 IPv4（`169.254.169.254`/`.253`），IMDSv6（`fd00:ec2::254`）的 ip6tables 防护尚未落地。

### L5 凭据·只读·监控层

本层覆盖凭据最小化、镜像不可变与运行时监控。guest 内不放任何长期 AWS 凭据（zero-credential 黄金镜像）；gateway token 与 device 私钥以 AWS KMS 信封加密存储、按需下发密文由调用方本地解密（详见本节「数据面」）；per-tenant 计费 vkey 的 master key 只放 AWS Secrets Manager，Lambda 冷启动按需读一次缓存，从不进 env 明文、从不下到 host 或 guest，且 Lambda 角色的 `secretsmanager:GetSecretValue` 权限 scoped 到单一 secret 前缀。host 侧强制 IMDSv2（`HttpTokens=required` + `HttpPutResponseHopLimit=1`）。镜像不可变由只读盘的写屏障语义保证（详见本节「租户 microVM」）。host 启动时显式关闭三个跨租户侧信道与数据残留面：KSM（内核同页合并）、SMT（超线程）、swap，加固随重建继承、可审计、幂等。运行时监控含 in-guest 的 auditd（开销约 11.7MB，实测）与 Wazuh 文件完整性监控（FIM，约 42MB，实测）——guest 内运行时威胁监控由 Wazuh/auditd 承载，不依赖 Amazon GuardDuty Runtime agent（后者未在本栈启用）。AWS 平台侧另叠加 Amazon GuardDuty 账号级威胁检测（VPC/DNS/EC2/S3 等云层 findings，config-gated，`RUNTIME_MONITORING` 默认关闭）、Amazon S3 全桶级公网封锁 + 强制 HTTPS、VPC Flow Logs（config-gated，默认 true，投递到 Amazon CloudWatch 供网络取证）等基线控制。

审计层按「谁记录、可不可篡改、保多久」分开落地。租户业务数据的备份桶在数据保留区（`ap-southeast-1`）启用 Amazon S3 Object Lock（COMPLIANCE 模式）+ Versioning 实现 WORM，保留期内 root 账户也不能覆盖/删除对象版本、不能缩短保留期，实现见 `deploy/stacks/storage.py`（gate 在数据保留区判定）。控制面的 API 变更审计写入 Amazon DynamoDB 审计表（`GET /audit-log`），best-effort 追加、TTL 90 天到期即失，属可变审计流水而非 WORM，不适合作为不可篡改的合规证据。真正对齐 SEC 17a-4 / CFTC / FINRA 的不可篡改审计 trail（含 Object Lock 化的 AWS CloudTrail + 跨多区完整性校验）目前属于规划（未实现，见 issue #32）；当前 AWS CloudTrail 复用账号级 trail，仅启用日志文件完整性校验（log-file validation），不带 Object Lock，不应作为已交付能力对外表达。

## 此解决方案中的 AWS 服务

本节列出该解决方案部署或依赖的 AWS 服务及其职责。说明开头标「核心。」表示该服务承载关键路径，标「支持。」表示该服务提供辅助能力。

| AWS 服务                                  | 说明                                                                                                                                                                   |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Amazon API Gateway                        | 核心。对外暴露控制面 REST API，要求 `x-api-key`，并将请求转发到后端的控制面 Lambda 函数。                                                                              |
| AWS Lambda                                | 核心。承载控制面的租户注册、生命周期、备份、host/skill/group 管理与审计端点，以及异步备份函数。                                                                        |
| Amazon DynamoDB                           | 核心。以 tenants、hosts、groups 三张主表加 audit 表持久化租户元数据、节点状态、调度记录与授权字段，四张 `RETAIN` 表开启时间点恢复。                                    |
| Amazon Simple Storage Service (Amazon S3) | 核心。存放租户数据盘、skill、备份与镜像分片；备份桶启用对象锁定（COMPLIANCE）实现 WORM，全桶级公网封锁 + 强制 HTTPS。                                                  |
| Amazon CloudFront                         | 核心。作为公网唯一入口，反代 console/chat 静态资产（S3 origin）与数据面（回源 ALB → OpenResty 边缘），`/ws/*` 行为转发 WebSocket 升级头并禁用缓存。                    |
| Application Load Balancer                 | 核心。位于 CloudFront 之后，将数据面 WebSocket 流量分发到 OpenResty 边缘目标组，`idle_timeout` 3600s 承载长连接。                                                      |
| Amazon ElastiCache for Redis              | 核心（数据面 opt-in）。存放 OpenResty 边缘查询的 `tenant_id → {host,port,guest_ip}` 准静态路由表，Multi-AZ replication group，宿主 agent 双写。                        |
| Amazon Elastic Compute Cloud (Amazon EC2) | 核心。以 metal 系列实例运行 Firecracker microVM host（由 Auto Scaling Group 管理弹性伸缩，强制 IMDSv2 + hop-limit=1）；数据面 opt-in 时另有独立的 OpenResty 边缘 ASG。 |
| Amazon Cognito                            | 支持。仅用于控制面 console 登录（OAuth 2.0 授权码 + PKCE，`console_auth.enabled` 默认关）；不在聊天数据面链路上。                                                      |
| Amazon Bedrock                            | 核心。通过 Amazon Bedrock Guardrails 在内容层（L1）对进出大语言模型的内容做越狱拦截、有害内容过滤与敏感信息脱敏。                                                      |
| Amazon CloudWatch                         | 支持。接收 VPC Flow Logs 与日志组，供网络取证与隔离 DROP 规则的生效验证。                                                                                              |
| AWS Secrets Manager                       | 支持。托管 LiteLLM master key 与外部授权 HMAC 共享密钥，密钥明文不进部署模板，经动态引用注入。                                                                         |
| AWS Systems Manager                       | 支持。在无直接 SSH 通道时供生产运营对 host 做管理操作。                                                                                                                |
| Amazon Virtual Private Cloud (Amazon VPC) | 支持。承载 host 网络，配合 host iptables 与 Flow Logs 实现网络隔离与取证。                                                                                             |
| Amazon Route 53 Resolver DNS Firewall     | 支持。作为 L4 出网补充，在 DNS 解析层 BLOCK 已知 C2 与数据外泄域名（config-gated，默认关）。                                                                           |
| AWS WAF                                   | 支持。config-gated（默认关），启用时强制追加 SQL 注入与 IP 信誉两条托管基线规则并关联 API Gateway stage。                                                              |
| Amazon EventBridge                        | 支持。驱动后台 `process_pending` 等异步处理，触发待处理租户的容量调度。                                                                                                |
| Amazon Simple Queue Service (Amazon SQS)  | 支持。config-gated，为生命周期动作削峰，启用时 start/stop/restart 等操作入队返回 202 queued。                                                                          |
| Amazon GuardDuty                          | 支持。在 AWS 平台侧提供威胁检测，纳入 L5 监控层基线（config-gated）。                                                                                                  |
| AWS Key Management Service (AWS KMS)      | 核心。信封加密 gateway token 与 device 私钥（`EncryptionContext` 绑 `tenant_id`/`owner_id`），并为出站凭据提供 RSA-4096 非对称密钥；EBS 静态加密同用 CMK。             |

> **Note**
>
> 上表中标注 config-gated 的服务（Amazon Route 53 Resolver DNS Firewall、AWS WAF、Amazon SQS、Amazon GuardDuty 等）默认关闭，需在目标环境显式启用。接入或部署前请确认目标环境的实际开关取值，相关默认值与启用方式参见「规划部署」。
