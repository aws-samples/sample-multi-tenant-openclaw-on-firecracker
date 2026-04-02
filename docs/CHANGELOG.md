# Changelog

## v0.8.7 — Console 在线托管 + Cognito 认证

- **Console S3 托管** — Console 通过 CloudFront `/console/` 路径在线访问，无需本地运行
- **Cognito 认证** — User Pool + Hosted UI，OAuth2 implicit flow 保护 Console 访问
- **bind-domain.sh 更新** — 支持 CloudFront 绑定自定义域名（需 us-east-1 ACM 证书）
- **config.yml.example** — 新增示例配置文件

## v0.8.6 — CloudFront + Dashboard Token

- **CloudFront** — ALB 前加 CloudFront，`*.cloudfront.net` 自带 HTTPS，无需自定义域名和 ACM 证书
- **Gateway Token 自动注入** — host-agent 在 VM 就绪时自动读取 gateway token 写入 DynamoDB
- **Console 一键访问** — "Open Dashboard" 按钮直接带 token 打开，无需手动输入
- **自定义域名变为可选** — bind-domain.sh 保留，不再是必需步骤

## v0.8.5 — Host Agent

- **host-agent daemon** — 宿主机常驻服务，每 5s 探活所有本机 VM（ping + curl），直写 DynamoDB
- **creating → running 实时提升** — agent 检测到 VM up 后立即提升状态，无需等待 Lambda
- **Health check Lambda 降级为 watchdog** — 5 分钟一次，仅检测 stale agent 数据 + 预留告警

## v0.8.4 — 架构优化

- **去除 SSM 健康检查依赖** — 探活从 N 次 SSM/分钟降为 0，SSM 仅用于生命周期操作（launch/stop/backup）
- **init-host.sh 运行时获取表名** — 通过 CloudFormation output 查询，去掉 CDK placeholder split 复杂度
- **S3 下载脚本** — backup-data.sh、host-agent.py 从 S3 下载，解决 userdata 16KB 限制
- **S3 下载日志精简** — `aws s3 cp --no-progress`

## v0.8.3 — Bug Fixes

- **AgentCore gateway URL 注入** — 修复 `{{AGENTCORE_GATEWAY_URL}}` 始终为 "none" 的问题
- **`{{ASSETS_BUCKET}}` 替换顺序** — 移到所有 script 注入之后，避免后插入的 placeholder 未替换
- **`ac_gateway` 未绑定** — 初始化为 None，使用前检查
- **`cfn_lt` 重复赋值** — 提前到 spot 判断之前
- **IAM policy 命名** — `ec2_describe_policy` → `ec2_policy`（包含 TerminateInstances）

## v0.8.2 — AgentCore 深化 + 协作规范

**AgentCore 深化:**
- **Gateway Lambda 工具注册** — hello/system_info/timestamp 三个示例工具通过 ToolSchema 注册到 Gateway
- **WorkloadIdentity** — 代理 agent AWS 资源访问
- **CDK 循环依赖修复** — lifecycle hooks 内嵌 ASG，Gateway 创建提前到 ASG 之前
- **AgentCore 资源命名** — 统一使用下划线（`openclaw_semantic`）

**改进:**
- **Health check 加速** — Target group interval 30s→10s，healthy threshold 5→2，新 tenant ~20s 可访问
- **跨平台 sed** — `sed -i.bak` 替代 `sed -i`，兼容 macOS/Linux
- **CDK AgentCore L2** — 统一使用 `aws_bedrock_agentcore_alpha` L2 构造

**协作:**
- 新增 CONTRIBUTING.md 协同开发规范

## v0.8.1 — ALB Path-Based Routing

**架构改进:**
- **ALB path-based routing** — 替代 v0.8.0 的跨主机 nginx 方案。每个 tenant 一条 ALB listener rule，精确路由到对应 host 的 IP target group。无跨主机流量，无状态同步，host 替换透明
- **ALB SG 出站修复** — CDK 生成的 ALB SG 默认禁止出站（因 listener 无 target group），导致 health check 和请求转发失败。添加 VPC CIDR 出站规则
- **Health check grace period** — 从 10 分钟缩短为 3 分钟，VM 实际启动仅需 ~30s
- **setup.sh 保留 DASHBOARD_URL** — 部署时不覆盖 bind-domain.sh 设置的 HTTPS 自定义域名

**清理:**
- 移除跨主机 nginx 代理代码（`_sync_nginx_to_other_hosts` / `_remove_nginx_from_all_hosts`）
- 移除 host 间 SG 互通规则（不再需要跨主机流量）

## v0.8.0 — Bug Fixes + AgentCore 集成

**Bug Fixes:**
- **SSM 队列堵塞修复** — 健康检查对 creating 状态 VM 增加 10 分钟 grace period，不发 SSM 命令；过了 grace 只做轻量 ping 提升，不自动重启
- **ALB 多实例路由修复** — 创建/删除租户时自动同步 nginx 代理配置到所有宿主机，ALB 无论路由到哪台宿主机都能正确转发
- **launch-vm.sh rmdir 容错** — `rmdir` 加 `|| true`，防止 umount 异步未完成时脚本退出导致 VM 启动失败
- **磁盘拷贝完整性校验** — 拷贝前校验文件大小是否与模板一致，损坏文件自动删除重新拷贝

**新功能:**
- **AgentCore 集成** — 可选的 Bedrock AgentCore Gateway、Memory、Code Interpreter、Browser 支持

## v0.7.2 — Bug Fixes + 文档更新

**Bug Fixes:**
- **fstab UUID** — 数据卷挂载改用 UUID 替代设备名，避免 NVMe 重启后设备名变化
- **manifest.json 重试** — 宿主机初始化时等待 S3 上的 manifest.json，最多重试 10 分钟
- **备份简化** — pause → pigz 直接压缩 → resume（去掉 8G cp 步骤）

**文档 & 结构:**
- 英文 README.md + 中文 docs/README-CN.md
- 脚本移至 scripts/: destroy.sh, oc-connect.sh, oc-dashboard.sh, bind-domain.sh
- 架构图更新

## v0.7.0 — ALB Dashboard + 备份系统

**新功能:**
- **ALB Dashboard 代理** — ALB (internet-facing) → Host Nginx → VM Gateway，支持 WebSocket，自定义域名 + HTTPS
- **自动备份系统** — Backup Lambda + EventBridge 定时备份；手动触发 `POST /tenants/{id}/backup`；查询 `GET /tenants/{id}/backups`；pause → pigz → resume 保证一致性
- **bind-domain.sh** — 一键绑定自定义域名 + ACM 证书到 ALB
- **Gateway allowedOrigins** — 自动设置 `allowedOrigins=["*"]`，Dashboard 可从任意域名访问
- **Console SVG 图标** — emoji 替换为 inline SVG sprite（零运行时依赖）

## v0.6.1 — Dashboard 代理 + 共享 Skills + 默认工具链

- 宿主机 Dashboard 路由代理
- 共享 Skills：S3 集中管理，自动同步到所有 VM
- 默认工具链：Python3/uv/git/gh/Node.js/htop/tmux/tree
- CPU 超配支持
- Gateway Console 替代 Mission Control Dashboard

## v0.5.2 — 初始版本

- 租户 CRUD（API Gateway + Lambda）
- Firecracker microVM 独立 rootfs/数据盘/网络
- ASG 自动扩缩容 + 空闲主机回收
- 健康检查 + 自动重启
- Web 管理控制台
- Rootfs 预构建 + S3 分发
