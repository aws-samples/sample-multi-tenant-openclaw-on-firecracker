# Multi-Tenant OpenClaw on Firecracker

**[English](../README.md)** | **[中文](README-CN.md)**

基于 AWS Firecracker microVM 的 OpenClaw 多租户隔离部署方案，俗称 **龙虾池**。每个租户运行在独立的 microVM 中，通过 API 统一管理，ASG 自动扩缩宿主机，空闲主机自动回收。

> 本项目使用 AWS EC2 [嵌套虚拟化](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html) 功能，在 EC2 实例内运行 KVM + Firecracker microVM。目前支持 Intel 系列 (c8i/m8i/r8i 等) 实例家族。

> ⚠️ 本项目仅用于演示目的，不适用于生产环境。使用风险自负。

## 功能概览

- **租户管理** — 通过 API 创建/删除/查询租户。每个租户是一个运行在独立 Firecracker microVM 中的 OpenClaw 实例，拥有独立的系统盘、数据盘和网络
- **安全隔离** — 基于 Firecracker microVM 实现租户间隔离，独立内核、独立网络，互不可见
- **自动调度** — 创建租户时自动选择有空闲资源的宿主机，资源不足时自动扩容
- **自动缩容** — 空闲宿主机超时后自动回收，节省成本（两轮确认防误杀）
- **健康检查** — 实时 VM 健康监控，状态自动更新
- **Web 管理控制台** — 在线管理控制台，Cognito 认证保护，实时 Host/Tenant 状态
- **Rootfs 预构建** — rootfs + data template 双镜像通过 S3 分发，宿主机启动时自动下载
- **Dashboard 直达** — 一键 HTTPS 访问每个租户的 OpenClaw Dashboard，无需自定义域名
- **自动备份与恢复** — EventBridge 定时备份所有租户数据盘到 S3，支持手动触发、跨租户备份查询，以及一键从备份恢复为新租户（支持孤儿备份 —— 源租户已删除也能恢复）
- **AgentCore 集成** — 可选开关，开启后所有 VM 自动连接 AgentCore Gateway（MCP 工具中心）、Memory（托管记忆）、Code Interpreter（安全沙箱）、Browser（云端浏览器）
- **共享 Skills** — 所有租户共享统一的 Skills（S3 集中管理，自动同步到所有 VM），记忆独立
- **配置模板** — 自定义 OpenClaw 配置模板（支持不同 LLM 提供商/模型），创建租户时可选模板
- **默认工具链** — 每个 VM 预装 Python3/uv/git/gh/Node.js/htop/tmux/tree 等开发工具

## 快速开始

**前置条件:**

- AWS 账号 + CLI 配置
- CDK CLI + Python 3.12+
- uv (Python 包管理)
- 运行 `build-rootfs.sh` 还需要：`sudo` 权限；`debootstrap` / `pigz` / `e2fsprogs` 工具；
  ≥2GB 可用内存, `/tmp` ≥10GB 可用空间

```bash
# 1. 配置
cp config.yml.example config.yml          # 编辑基础设施配置
cp templates/openclaw.json.example templates/openclaw.json  # 设置 API key、模型 provider 等

# 2. 部署基础设施
./setup.sh ap-northeast-1 lab
# 完成后环境变量保存在 .env.deploy

# 3. 构建 rootfs —— 创建任何租户前都必须先做这一步
#    (自动上传 S3 + 推送到 host)
source .env.deploy
./build-rootfs.sh v1.0

# 4. 创建租户（OpenClaw 实例）
source .env.deploy
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" \
  -d '{"name":"my-agent","vcpu":2,"mem_mb":4096}' | jq .

# 5. 打开 Console 管理租户、模板和设置
# Console URL 在部署完成后输出
```

## Management Console

Web 管理控制台，通过 CloudFront (`/console/`) 在线访问，Cognito 认证。

功能：
- **Tenants** — 宿主机资源概览，创建/删除租户，一键打开 Dashboard
- **Application** — 共享 Skills 列表，配置模板管理（创建/编辑/删除）
- **Backups** — 跨租户备份浏览器，按租户分组聚合、孤儿备份过滤、一键恢复到新租户
- **Settings** — API 连接、AgentCore 状态、系统信息

### 截图

![Tenants 页签](web_console.png)

![Backups 页签](web_console_backup.png)

## Dashboard 访问

每个租户的 OpenClaw Dashboard 通过 CloudFront + ALB + Nginx 反向代理访问：

```
https://{cloudfront-domain}/vm/{tenant-id}/    → 租户 Dashboard (WebSocket)
```

CloudFront 自动提供 HTTPS，无需自定义域名或 ACM 证书。Console 的 "Open Dashboard" 按钮自动带上 gateway token，一键访问。

流量路径：`Browser → CloudFront:443 → ALB:80 → Host Nginx:80 → VM Gateway:18789`

Nginx 配置由 launch-vm.sh / stop-vm.sh 自动管理。

## 自定义域名（可选）

绑定自定义域名到 CloudFront。配置位于 `config.yml` 的 `cloudfront:` section，可以直接编辑文件，也可以通过 `setup.sh` 参数传入：

```bash
# 前置条件：
# 1. 在 us-east-1 申请 ACM 证书（CloudFront 要求）并完成 DNS 验证
# 2. 将域名 CNAME 指向 CloudFront 域名（见 DashboardUrl output）

# 一条命令：写入 config.yml 并部署
./setup.sh ap-northeast-1 lab \
  --domain claw.example.com \
  --cert   arn:aws:acm:us-east-1:xxx:certificate/xxx

# 或手动编辑 config.yml 后不带 flag 跑 setup.sh。
# 取消绑定：--domain "" 然后重跑 setup.sh。
```

自定义域名/证书通过 CDK 管理，不会被下次 `setup.sh` 覆盖。

## 自动备份与恢复

EventBridge 定时备份所有 running 租户的数据盘到 S3，也支持手动触发。

**备份流程**：pause VM → pigz 压缩 data.ext4 → resume VM → 上传 S3。即使备份失败，VM 也会自动恢复运行（trap cleanup）。

```bash
source .env.deploy

# 手动触发单个租户备份
curl -s -X POST "${API_URL}tenants/{tenant-id}/backup" -H "x-api-key: ${API_KEY}" | jq .
# 返回 {"status": "started"} — 异步执行，不阻塞

# 查询单个租户的备份列表
curl -s "${API_URL}tenants/{tenant-id}/backups" -H "x-api-key: ${API_KEY}" | jq .

# 查询所有租户的备份（包含 orphan 标记）
curl -s "${API_URL}backups" -H "x-api-key: ${API_KEY}" | jq .

# 定时备份配置（config.yml）
# backup_cron: "cron(0 19 * * ? *)"  # UTC 19:00 = 北京时间 03:00
# backup_retention_days: 7            # S3 lifecycle 自动清理 7 天前的备份
```

备份文件存储在 `s3://{bucket}/backups/{tenant-id}/{timestamp}.gz`。

### 从备份恢复

Restore 会基于某个备份创建一个**新租户**。源租户无需存在 —— 已删除租户的孤儿备份也能完整恢复。

```bash
# 从指定租户的最新备份恢复（源租户可以已被删除）
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" -d '{
  "name": "restored-agent",
  "vcpu": 2, "mem_mb": 4096,
  "restore_from": {"tenant_id": "my-agent-ab12"}
}' | jq .

# 从指定时间戳的备份恢复
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" -d '{
  "name": "restored-agent",
  "restore_from": {"tenant_id": "my-agent-ab12", "timestamp": "20260428-125402"}
}' | jq .
```

- `restore_from` 与 `vcpu`/`mem_mb`/`config_template` 解耦 —— 这些字段跟随新租户的规格
- 数据盘大小 = 备份实际大小（不自动 resize）
- 新租户拿到新的 ID，不继承源租户身份

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
| `templates/openclaw.json` | OpenClaw 应用配置（模型、API key、provider）— 从 `.example` 复制 |
| `.env.deploy` | 部署环境 (region、API URL/Key、bucket) — setup.sh 自动生成 |

### config.yml

| 分类 | 配置项 | 默认值 | 说明 |
|------|--------|--------|------|
| host | instance_type | m8i.xlarge | 需支持 NestedVirtualization (c8i/m8i/r8i) |
| host | data_volume_gb | 200 | 宿主机数据卷 (rootfs 模板 + VM 数据盘) |
| host | cpu_overcommit_ratio | 2.0 | CPU 超配比例 (1.0=不超配, 2.0=可分配 2 倍 vCPU) |
| host | mem_overcommit_ratio | 1.0 | 内存超配比例 (需启用 balloon) |
| host | keep_data_volume | true | 实例终止时保留 EBS 数据卷 |
| vm | default_vcpu | 2 | 默认 vCPU |
| vm | default_mem_mb | 4096 | 默认内存 (MB) |
| vm | rootfs_overlay_mb | 8192 | 每 VM 可写层上限 (sparse, 不预占空间) |
| vm | data_disk_mb | 8192 | 每 VM 数据盘 `/home/agent` 上限 (sparse) |
| balloon | enabled | false | Firecracker balloon 设备实现内存超配 |
| balloon | max_inflate_ratio | 0.4 | 最多回收 VM 声明内存的比例 |
| balloon | min_guest_available_mb | 512 | guest 至少保留的可用内存 |
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

## 架构

### 系统架构

![系统架构](./oc-system-arch.png)

### 部署架构

![部署架构](./oc-deploy-arch.png)

<details>
<summary>ASCII 版本（方便 AI/文本访问）</summary>

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

</details>

## 项目结构

```
sample-multi-tenant-openclaw-on-firecracker/
├── deploy/                    # CDK 项目
│   ├── app.py                 # CDK 入口
│   ├── stack.py               # 基础设施定义
│   ├── lambda/
│   │   ├── api/handler.py     # 租户 CRUD + 宿主机管理
│   │   ├── templates/handler.py  # 配置模板 CRUD
│   │   ├── skills/handler.py  # 共享 Skills 列表
│   │   ├── health_check/handler.py  # 定时健康检查
│   │   ├── agentcore_tools/handler.py  # AgentCore Gateway Lambda 工具
│   │   └── scaler/handler.py  # 空闲宿主机回收
│   └── userdata/
│       ├── init-host.sh       # 宿主机初始化
│       ├── host-agent.py      # VM 健康探活 + DDB 写入 + balloon
│       ├── launch-vm.sh       # microVM 启动
│       └── stop-vm.sh         # microVM 停止
├── console/                   # Web 管理控制台
│   ├── index.html             # Alpine.js SPA (4 页签)
│   └── style.css
├── tests/                     # 测试套件 (unit + e2e)
├── templates/                 # OpenClaw 配置模板
│   └── openclaw.json.example  # 示例配置
├── pyproject.toml             # Python 项目配置 + 依赖
├── cdk.json                   # CDK 应用配置
├── config.yml                 # 基础设施配置 (唯一配置源)
├── setup.sh                   # 一键部署 + 导出 .env.deploy
├── build-rootfs.sh            # rootfs + data template 构建 + S3 上传
├── scripts/
│   ├── destroy.sh             # 销毁 stack
│   ├── oc-connect.sh          # 快速登录某个租户 VM
│   └── oc-dashboard.sh        # 打开某个租户的 Dashboard URL
└── docs/
```

## API 参考

所有请求需携带 `x-api-key` header。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /tenants | 列出所有租户 |
| POST | /tenants | 创建租户 `{"name":"xx","vcpu":2,"mem_mb":4096}` — 加上 `"restore_from":{"tenant_id":"..."}` 则从备份恢复 |
| GET | /tenants/{id} | 查询单个租户 |
| DELETE | /tenants/{id} | 删除租户 (`?keep_data=true` 保留数据盘) |
| POST | /tenants/{id}/restart | 重启租户 VM（复用磁盘，快速） |
| POST | /tenants/{id}/stop | 停止租户 VM（磁盘保留） |
| POST | /tenants/{id}/start | 启动已停止的租户 VM |
| POST | /tenants/{id}/pause | 冻结 vCPU（Firecracker 原生，即时） |
| POST | /tenants/{id}/resume | 恢复已暂停的租户 VM |
| POST | /tenants/{id}/reset | 重装系统盘（data 卷保留） |
| POST | /tenants/{id}/backup | 手动触发数据盘备份（异步） |
| GET | /tenants/{id}/backups | 查询单个租户的备份列表 |
| GET | /backups | 跨租户查询所有备份（含 orphan 标记） |
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

## 安全

安全问题报告请参见 [CONTRIBUTING](../CONTRIBUTING.md#security-issue-notifications)。

## License

本项目基于 MIT-0 License 开源，详见 [LICENSE](../LICENSE) 文件。

---

## 附录

### 默认工具链

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

### 宿主机管理

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

### API Key 管理

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
