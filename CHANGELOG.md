# Changelog

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
