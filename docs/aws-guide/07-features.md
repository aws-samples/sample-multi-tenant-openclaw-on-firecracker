# 功能详细说明

本节介绍该解决方案的 agent 能为终端用户提供的能力，以及控制面对外暴露的编排功能清单。

## 平台核心与业务样本的分层

该解决方案的平台核心与具体业务解耦。控制面（AWS Lambda 与 Amazon DynamoDB）、数据面两级边缘路由（OpenResty 边缘 ASG + 宿主 iptables DNAT，路由到各 microVM 内 OpenClaw 原生 gateway）、宿主生命周期脚本本身不包含任何业务内容，只负责预配并管理大规模隔离的 AI agent microVM。每台虚拟机运行什么 agent，由一个可替换的样本决定：构建脚本通过 `SAMPLE` 环境变量选择样本，默认 `finance-agent`，更换样本目录即更换整套 agent 能力，平台核心不变。

随仓库发布的 `finance-agent` 样本是一个**最小骨架**：它演示样本该怎么组织（身份人设 `persona/`、能力技能 `skills/`、标配护栏 `security/`、部署配置 `config/`），但只带一个 `weather` 演示技能,把「业务能力」这一层留给部署方按自己的场景填充。这样开源发布物里不预置任何特定行业的业务技能,平台核心与安全层则完整可用。

**标配安全层（每个样本都带,平台保证）**

每个样本镜像都自带两类护栏(在 `security/` 下),不随业务技能增减:`sentinel-guard`(工具执行层 ACL,default-deny 拦读凭据/IMDS/敏感路径)、`acl-guard`(命令白名单)。叠加只读黄金镜像(EROFS)、auditd 与文件完整性监控。这层是 L2 工具护栏与 L5 只读/监控的落地,详见架构安全章。

**演示技能与供应链治理**

`weather` 是随样本发布的演示技能,展示技能目录结构(`SKILL.md` + scaffold)与加载方式,不代表生产能力。新增技能走供应链治理:技能进入只读黄金镜像前强制离线审核,不在运行中的虚拟机上热装,随下次镜像重建 / `refresh-rootfs` 生效。

**部署方如何加自己的业务技能**

在 `samples/<your-brand>/skills/` 下按 `weather` 的结构放自己的技能目录,在样本的 `MANIFEST.md` 声明每个技能的用途、防御目标、是否 `always=true`(每会话强制加载)。烤镜像(`SAMPLE=<your-brand> ./build-rootfs.sh`)即把这套技能冷注入只读盘。

> **Note**
>
> 一条建议给部署方的安全原则:让绝大多数技能为只读;任何涉及资金 / 不可逆的动作不在 agent 内直接执行,而是路由到带 CONFIRM 门的专门技能,把硬否决落在工具执行层(`sentinel-guard`),使提示词注入也无法绕过。样本的具体能力以其技能定义文件中的描述字段与 `MANIFEST.md` 为准。

## 控制面对外功能清单

除虚拟机内 agent 的能力外，该解决方案的控制面通过 REST API 对外暴露一组租户编排功能。完整端点契约见开发人员指南，下表给出功能地图。所有端点经 Amazon API Gateway、需 `x-api-key`，且默认启用 RBAC。

| 功能域             | 能力                                                                     | 主要端点                                            |
| ------------------ | ------------------------------------------------------------------------ | --------------------------------------------------- |
| 租户注册           | 注册租户（operator 级）；终端用户自助开通自己的节点（viewer 级，有上限） | `POST /tenants`、`POST /tenants/self`               |
| 生命周期           | 启停、重启、暂停与恢复、重置、热加 vCPU、离线扩盘、跨宿主迁移            | `POST /tenants/{id}/{action}`                       |
| 删除与数据保护     | 删除租户（销毁数据前强制同步备份，失败中止；回收计费密钥）               | `DELETE /tenants/{id}`                              |
| 备份与恢复         | 异步备份、列出全租户备份清单、从备份恢复                                 | `POST /tenants/{id}/backup`、`GET /backups`         |
| 查询               | 列出租户（标签过滤、分页、密钥脱敏）、查询单租户、查询授权               | `GET /tenants`、`GET /tenants/{id}`                 |
| 批量运维           | 按 ID 列表或标签过滤批量启停、删除、备份；大批量转异步作业与进度轮询     | `POST /batch/tenants`、`GET /batch/jobs/{id}`       |
| 按业务用户管理节点 | 按一个业务用户管理其名下所有节点：列节点、汇总、批量动作                 | `GET/POST /users/{tenant_user_id}/*`                |
| 授权               | 显式授权 grant/revoke；外部授权写权威外置给业务后端（HMAC 签名）         | `POST /tenants/{id}/access`、`POST /external/authz` |
| 宿主管理           | 注册、列出、下线宿主；刷新黄金镜像；查询镜像版本                         | `POST /hosts`、`POST /hosts/refresh-rootfs`         |
| 技能分发           | 技能分组增删、技能定义读写删、按租户或分组的技能范围控制                 | `GET/POST /groups`、`GET/PUT/DELETE /skills/{name}` |
| 系统与审计         | 特性快照、AgentCore 状态与工具、审计日志（默认保留 90 天）               | `GET /system/info`、`GET /audit-log`                |
| 实时聊天           | 经两级边缘路由到 microVM 原生 gateway（Bearer `gateway_token` 鉴权）     | `POST /ws/{tenant_id}/v1/chat/completions`（SSE）   |

> **Note**
>
> 上表中的"按业务用户管理节点"依赖一个二级索引（`gsi_tenant_user`），该索引受配置开关控制且默认不建。"自助注册"与"外部授权"受部署开关门控。各功能的开关状态与默认值以开发人员指南与规划部署对应章节为准。
