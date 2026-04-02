# OpenClaw Pool on EC2 microVM

![Version](https://img.shields.io/badge/version-0.8.7-blue)

**[English](../README.md)** | **[中文](README-CN.md)** | **[Changelog](CHANGELOG.md)**

基于 AWS Firecracker microVM 的 OpenClaw 多租户隔离部署方案。每个租户运行在独立的 microVM 中，通过 API 统一管理，ASG 自动扩缩宿主机，空闲主机自动回收。

> 本项目使用 EC2 [嵌套虚拟化](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html) 功能，在 EC2 实例内运行 KVM + Firecracker microVM。目前支持 Intel 系列 (c8i/m8i/r8i 等) 实例家族。

## 功能概览

- **租户管理** — 通过 API 创建/删除/查询租户。每个租户是一个运行在独立 Firecracker microVM 中的 OpenClaw 实例，拥有独立的系统盘、数据盘和网络
- **安全隔离** — 基于 Firecracker microVM 实现租户间隔离，独立内核、独立网络，互不可见
- **自动调度** — 创建租户时自动选择有空闲资源的宿主机，资源不足时自动扩容
- **自动缩容** — 空闲宿主机超时后自动回收，节省成本（两轮确认防误杀）
- **健康检查** — 实时 VM 健康监控，状态自动更新
- **Web 管理控制台** — 在线管理控制台，Cognito 认证保护，实时 Host/Tenant 状态
- **Rootfs 预构建** — rootfs + data template 双镜像通过 S3 分发，宿主机启动时自动下载
- **Dashboard 直达** — 一键 HTTPS 访问每个租户的 OpenClaw Dashboard，无需自定义域名
- **自动备份** — EventBridge 定时备份所有租户数据盘到 S3，支持手动触发和备份查询
- **AgentCore 集成** — 可选开关，开启后所有 VM 自动连接 AgentCore Gateway（MCP 工具中心）、Memory（托管记忆）、Code Interpreter（安全沙箱）、Browser（云端浏览器）
- **共享 Skills** — 所有租户共享统一的 Skills（S3 集中管理，自动同步到所有 VM），记忆独立
- **默认工具链** — 每个 VM 预装 Python3/uv/git/gh/Node.js/htop/tmux/tree 等开发工具
- **统一配置管理** — 控制台展示每个租户的模型配置和共享 Skills 列表
- **自定义域名** — 一键绑定域名 + ACM 证书到 ALB，HTTPS 访问 Dashboard

## 部署架构

```
用户/管理员
    │
    ├── API Gateway (HTTPS, x-api-key) → Lambda → DynamoDB
    │                                     │         ├── tenants (租户状态)
    │                                     │         └── hosts (宿主机资源)
    │                                     │
    │                                     ├── SSM Run Command ──→ EC2 Host A
    │                                     │                       ├── Nginx (ALB 反向代理)
    │                                     │                       ├── microVM 01 (172.16.1.2)
    │                                     │                       ├── microVM 02 (172.16.2.2)
    │                                     │                       └── ...
    │                                     │
    │                                     └── SSM Run Command ──→ EC2 Host B ...
    │
    └── ALB (Dashboard) ──→ Host Nginx:80 ──→ VM Gateway:18789
                            (跨主机自动路由)

S3: rootfs 分发 + 数据卷备份
ASG: 宿主机自动扩缩 (配置参见: config.yml)
EventBridge: 健康检查 + 空闲回收 + 定时备份
```

<details>
<summary>系统架构图</summary>

![系统架构](./oc-system-arch.png)

</details>

<details>
<summary>部署架构图</summary>

![部署架构](./oc-deploy-arch.png)

</details>

## 项目结构

```
openclaw-firecracker/
├── config.yml                 # 全局配置 (唯一配置源)
├── build-rootfs.sh            # rootfs + data template 构建 + S3 上传
├── setup.sh                   # 一键部署 + 导出 .env.deploy
├── destroy.sh                 # 销毁环境 (--purge 彻底清理)
├── web-console.sh             # 启动 Web 管理控制台
├── oc-connect.sh              # 登录 OpenClaw microVM
├── oc-dashboard.sh            #  OpenClaw 控制面板
├── console/                   # 管理控制台前端
│   ├── index.html             # Alpine.js SPA
│   ├── style.css              # 控制台UI样式
│   └── config.js              # 动态生成
├── scripts/
│   └── bind-domain.sh         # 绑定自定义域名 + ACM 证书到 ALB
├── deploy/                    # CDK 项目
│   ├── stack.py               # 基础设施定义
│   ├── lambda/
│   │   ├── api/handler.py     # 租户 CRUD + 宿主机管理
│   │   ├── health_check/handler.py  # 定时健康检查
│   │   ├── backup/handler.py  # 定时/手动数据备份
│   │   ├── agentcore_tools/handler.py  # AgentCore Gateway Lambda 工具
│   │   └── scaler/handler.py  # 空闲宿主机回收
│   └── userdata/
│       ├── init-host.sh       # 宿主机初始化
│       ├── launch-vm.sh       # microVM 启动
│       ├── stop-vm.sh         # microVM 停止
│       └── backup-data.sh     # 数据盘备份 (宿主机上执行)
└── docs/
```

## 前置条件

- AWS 账号 + CLI 配置
- CDK CLI + Python 3.12+
- uv (Python 包管理)

## 快速开始

```bash
# 1. 部署基础设施
./setup.sh ap-northeast-1 lab
# 完成后环境变量保存在 .env.deploy

# 2. 配置 OpenClaw 应用参数 (首次)
cat > .env.openclaw << 'EOF'
# OpenClaw 默认配置 (烧入 data template)
OPENCLAW_API_KEY=your-bedrock-api-key
OPENCLAW_BASE_URL=https://bedrock-mantle.us-west-2.api.aws/v1
OPENCLAW_MODEL_ID=deepseek.v3.2
OPENCLAW_TOOLS_PROFILE=coding
OPENCLAW_DM_SCOPE=per-peer
EOF

# 3. 构建并上传 rootfs
./build-rootfs.sh v1.0

# 4. 创建租户(Openclaw 实例)
source .env.deploy
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" \
  -d '{"name":"my-agent","vcpu":2,"mem_mb":4096}' | jq .

# 4. 查看租户状态
curl -s "${API_URL}tenants" -H "x-api-key: ${API_KEY}" | jq .

# 5. SSH 登录 microVM
./oc-connect.sh <tenant-id>

# 6. 删除租户
curl -s -X DELETE "${API_URL}tenants/<tenant-id>" -H "x-api-key: ${API_KEY}" | jq .
```

## Management Console

Web 管理控制台，支持 Host/Tenant 可视化管理。

```bash
./web-console.sh    # 自动读取 .env.deploy，启动 http://localhost:8080
```

![Management Console](web_console.png)

功能：
- 查看宿主机资源使用情况 (vCPU / 内存 / VM 数量)
- 创建 / 删除 Tenant
- 按宿主机筛选 Tenant
- 健康状态实时展示 (vm_health / app_health)
- 快捷复制连接命令 (oc-connect.sh) 和 Dashboard 命令 (oc-dashboard.sh)
- API 地址和 Key 自动注入，支持手动修改

## Dashboard 直达

每只租户的 OpenClaw Dashboard 通过 ALB + Nginx 反向代理直接访问，支持 WebSocket，多宿主机自动路由：

```
ALB → 任意宿主机 Nginx:80 → 租户所在宿主机 → VM Gateway:18789
```

访问路径：
```
http://{ALB-DNS}/vm/{tenant-id}/     → 租户 Dashboard
https://{your-domain}/vm/{tenant-id}/ → 绑定自定义域名后 (HTTPS)
```

**多宿主机路由原理**：创建租户时，API 自动在所有宿主机上生成 nginx 配置。本机租户直接代理到 guest IP，远程租户通过宿主机间 DNAT 端口转发。ALB 无论路由到哪台宿主机，nginx 都能正确转发。

## 自定义域名

一键绑定自定义域名 + HTTPS 到 Dashboard ALB：

```bash
# 前置条件：
# 1. 在 ACM 中申请证书并完成 DNS 验证
# 2. 将域名 CNAME 指向 ALB DNS（见 .env.deploy 中的 DASHBOARD_URL）

# 绑定
./scripts/bind-domain.sh oc.example.com arn:aws:acm:ap-northeast-1:123456:certificate/xxx

# 完成后访问
https://oc.example.com/vm/{tenant-id}/
```

脚本会自动：创建 ALB HTTPS listener → 关联 ACM 证书 → 更新 `.env.deploy` 中的 `DASHBOARD_URL`

## 自动备份

EventBridge 定时备份所有 running 租户的数据盘到 S3，也支持手动触发。

**备份流程**：pause VM → pigz 压缩 data.ext4 → resume VM → 上传 S3。即使备份失败，VM 也会自动恢复运行（trap cleanup）。

```bash
source .env.deploy

# 手动触发单个租户备份
curl -s -X POST "${API_URL}tenants/{tenant-id}/backup" -H "x-api-key: ${API_KEY}" | jq .
# 返回 {"status": "started"} — 异步执行，不阻塞

# 查询租户的备份列表
curl -s "${API_URL}tenants/{tenant-id}/backups" -H "x-api-key: ${API_KEY}" | jq .
# 返回 {"backups": [{"key": "backups/alice-xxx/2026-03-22T03:00:00Z.gz", "size_mb": 1.2}, ...]}

# 定时备份配置（config.yml）
# backup_cron: "cron(0 19 * * ? *)"  # UTC 19:00 = 北京时间 03:00
# backup_retention_days: 7            # S3 lifecycle 自动清理 7 天前的备份
```

备份文件存储在 `s3://{bucket}/backups/{tenant-id}/{timestamp}.gz`。

## 共享 Skills

所有租户共享统一的 Skills（SKILL.md 文件），记忆各自独立。

```bash
# 上传 Skills 到 S3（所有 VM 自动同步）
aws s3 sync ./my-skills/ s3://${ASSETS_BUCKET}/skills/ --profile $PROFILE

# Skills 同步链路：
# S3 → 宿主机 /data/shared-skills/ (cron 5min) → 所有运行中的 VM
# 新建 VM 时自动注入到数据盘
```

Skills 目录结构：
```
s3://{bucket}/skills/
├── code-review/SKILL.md
├── summarizer/SKILL.md
└── web-search/SKILL.md
```

## 默认工具链

每个 VM 预装以下工具（rootfs v1.1+）：

| 工具 | 用途 |
|------|------|
| Python 3.12 + venv | Python 开发 |
| uv | Python 包管理 |
| Node.js 22 + npm | JavaScript 运行时 |
| OpenClaw CLI | AI Agent 框架 |
| git + gh | 版本控制 + GitHub CLI |
| curl / wget / jq | HTTP 请求 + JSON 处理 |
| htop / tmux / tree | 系统监控 + 终端复用 + 目录浏览 |
| vim / build-essential | 编辑器 + 编译工具链 |

## 自动扩缩容

**扩容** — 创建租户时无可用宿主机 → 租户进入 pending → ASG 自动启动新实例 → 初始化完成后自动分配 pending 租户

**缩容** — Scaler Lambda 每 5 分钟检测空闲宿主机：
1. 宿主机 `vm_count=0` 超过 `idle_timeout_minutes` → 标记 `idle`
2. 下一轮确认仍空闲且 ASG 实例数 > min → 终止实例
3. 期间如有新租户分配到该宿主机 → 自动恢复 `active`，取消回收

## 配置说明

### 配置文件

| 文件 | 用途 |
|------|------|
| `config.yml` | 基础设施配置 — 从 `config.yml.example` 复制后按需修改 |
| `.env.deploy` | 部署环境 (region、API URL/Key、bucket) — setup.sh 自动生成 |
| `.env.openclaw` | OpenClaw 应用配置 (模型、API key、tools profile) |

### config.yml

| 分类 | 配置项 | 默认值 | 说明 |
|------|--------|--------|------|
| host | instance_type | m8i.xlarge | 需支持 NestedVirtualization (c8i/m8i/r8i) |
| host | data_volume_gb | 200 | 宿主机数据卷 (rootfs 模板 + VM 数据盘) |
| host | cpu_overcommit_ratio | 2.0 | CPU 超配比例 (1.0=不超配, 2.0=可分配 2 倍 vCPU) |
| vm | default_vcpu | 2 | 默认 vCPU |
| vm | default_mem_mb | 4096 | 默认内存 (MB) |
| vm | data_disk_mb | 8192 | 数据盘大小 (MB) |
| asg | min_capacity | 1 | 最小实例数 |
| asg | max_capacity | 5 | 最大实例数 |
| asg | use_spot | false | Spot 实例 (省 ~60-70%，可能被回收) |
| scaler | idle_timeout_minutes | 10 | 空闲超时后回收宿主机 |
| health_check | interval_minutes | 5 | Lambda watchdog 间隔 |
| agentcore | enabled | false | AgentCore Gateway/Memory/CodeInterpreter/Browser |
| console_auth | enabled | false | Console Cognito 认证 |
| console_auth | self_sign_up | false | 允许用户自注册 |

完整配置参见 `config.yml.example`。修改后重新部署：`./setup.sh <region> <profile>`

### 销毁环境

```bash
./destroy.sh           # 销毁 stack，保留 S3 bucket 和 DynamoDB 表
./destroy.sh --purge   # 彻底清理，包括 S3 数据和 DynamoDB 表
```

## API 参考

所有请求需携带 `x-api-key` header。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /tenants | 列出所有租户 |
| POST | /tenants | 创建租户 `{"name":"xx","vcpu":2,"mem_mb":4096}` |
| GET | /tenants/{id} | 查询单个租户 |
| DELETE | /tenants/{id} | 删除租户 (`?keep_data=true` 保留数据盘) |
| POST | /tenants/{id}/restart | 重启租户 VM（复用磁盘，快速） |
| POST | /tenants/{id}/stop | 停止租户 VM（磁盘保留） |
| POST | /tenants/{id}/start | 启动已停止的租户 VM |
| POST | /tenants/{id}/pause | 冻结 vCPU（Firecracker 原生，即时） |
| POST | /tenants/{id}/resume | 恢复已暂停的租户 VM |
| POST | /tenants/{id}/reset | 重装系统盘（data 卷保留） |
| POST | /tenants/{id}/backup | 手动触发数据盘备份（异步） |
| GET | /tenants/{id}/backups | 查询租户的备份列表 |
| GET | /hosts | 列出所有宿主机 |
| POST | /hosts | 注册宿主机 (UserData 自动调用) |
| POST | /hosts/refresh-rootfs | 推送最新 rootfs + data template 到所有宿主机 |
| GET | /hosts/rootfs-version | 查询 S3 上当前 rootfs 版本 (manifest.json) |
| DELETE | /hosts/{id} | 注销宿主机 |

## 网络模型

每个 VM 使用独立 /24 子网，通过 TAP 设备与宿主机通信：

```
VM1: tap-vm1  host=172.16.1.1/24  guest=172.16.1.2/24
VM2: tap-vm2  host=172.16.2.1/24  guest=172.16.2.2/24
VMn: tap-vmN  host=172.16.N.1/24  guest=172.16.N.2/24
```

- 出站：iptables MASQUERADE → 外网
- 入站：DNAT 端口转发 (host_port → guest:18789)
- VM 间：完全隔离，不同子网无路由

## Rootfs 管理

构建脚本生成两个镜像：rootfs (系统+软件) 和 data template (/home/agent 预配置内容)。

镜像版本通过 S3 `manifest.json` 管理，hosts/tenants 表记录各自使用的 `rootfs_version`。

```bash
# 构建并上传 (更新 manifest.json + refresh 宿主机)
./build-rootfs.sh v1.8

# 手动刷新宿主机镜像
source .env.deploy
curl -s -X POST "${API_URL}hosts/refresh-rootfs" -H "x-api-key: ${API_KEY}" | jq .

# 查询当前版本
curl -s "${API_URL}hosts/rootfs-version" -H "x-api-key: ${API_KEY}" | jq .

# 新建的 VM 自动使用新版本，已有 VM 需 reset 才会更新
```

## 宿主机管理

宿主机由 ASG 全自动管理，通常无需手动操作。

```bash
# 查看 ASG 状态
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names openclaw-hosts-asg \
  --query 'AutoScalingGroups[0].{Desired:DesiredCapacity,Min:MinSize,Max:MaxSize}' \
  --profile lab --region ap-northeast-1

# 手动扩容
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name openclaw-hosts-asg \
  --desired-capacity 3 --profile lab --region ap-northeast-1

# 查看初始化日志
./oc-connect.sh 后在宿主机上: cat /var/log/openclaw-init.log
```

## API Key 管理

```bash
source .env.deploy

# 创建新 key
aws apigateway create-api-key --name "operator-alice" --enabled \
  --profile $PROFILE --region $REGION

# 关联到 usage plan
PLAN_ID=$(aws apigateway get-usage-plans \
  --query "items[?name=='openclaw-plan'].id" --output text \
  --profile $PROFILE --region $REGION)
aws apigateway create-usage-plan-key --usage-plan-id $PLAN_ID \
  --key-id <new-key-id> --key-type API_KEY \
  --profile $PROFILE --region $REGION

# 禁用 / 删除 key
aws apigateway update-api-key --api-key <key-id> \
  --patch-operations op=replace,path=/enabled,value=false \
  --profile $PROFILE --region $REGION
```
