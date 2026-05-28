<h1 align="center">🦞 龙虾池 (OpenClaw Pool)</h1>

<p align="center">
  <b>基于 Firecracker microVM 的 AWS 多租户 AI Agent 隔离池</b><br/>
  <i>强隔离 · 真容灾 · 全观测 · 一键部署</i>
</p>

<p align="center">
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/License-MIT--0-blue.svg" alt="License: MIT-0"/>
  </a>
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/releases/latest">
    <img src="https://img.shields.io/github/v/release/aws-samples/sample-multi-tenant-openclaw-on-firecracker?color=success&label=release" alt="Latest release"/>
  </a>
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/stargazers">
    <img src="https://img.shields.io/github/stars/aws-samples/sample-multi-tenant-openclaw-on-firecracker?style=flat&color=yellow" alt="GitHub Stars"/>
  </a>
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/network/members">
    <img src="https://img.shields.io/github/forks/aws-samples/sample-multi-tenant-openclaw-on-firecracker?style=flat&color=orange" alt="GitHub Forks"/>
  </a>
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues">
    <img src="https://img.shields.io/github/issues/aws-samples/sample-multi-tenant-openclaw-on-firecracker?color=brightgreen" alt="Open issues"/>
  </a>
  <img src="https://img.shields.io/badge/tests-426%20passing-brightgreen" alt="Tests passing"/>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/AWS-Sample-FF9900?logo=amazon-aws&logoColor=white" alt="AWS Sample"/>
  <img src="https://img.shields.io/badge/Firecracker-microVM-E47A22?logo=firefox&logoColor=white" alt="Firecracker"/>
  <img src="https://img.shields.io/badge/AWS%20CDK-2.x-orange?logo=amazon-aws" alt="AWS CDK"/>
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/Bedrock-AgentCore-7C4DFF?logo=amazon&logoColor=white" alt="Bedrock AgentCore"/>
  <img src="https://img.shields.io/badge/Multi--AZ-HA-brightgreen" alt="Multi-AZ HA"/>
  <img src="https://img.shields.io/badge/Prometheus-Ready-E6522C?logo=prometheus&logoColor=white" alt="Prometheus"/>
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs welcome"/>
</p>

<p align="center">
  <b>📖 阅读语言：</b>
  <a href="../README.md">English</a> ·
  <a href="README-CN.md">中文</a>
</p>

<p align="center">
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-功能特性">功能</a> ·
  <a href="#%EF%B8%8F-web-console">Console</a> ·
  <a href="#%EF%B8%8F-架构">架构</a> ·
  <a href="#-api-reference">API</a> ·
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/releases/latest">Releases</a> ·
  <a href="../CHANGELOG.md">Changelog</a>
</p>

---

<p align="center">
  <img src="../docs/web_console.png" alt="OpenClaw Pool Console - 多 AZ 多 host 视图" width="92%"/>
  <br/>
  <i>真实生产部署 — 租户跨 2 个 AZ 分布、CPU/Memory/Disk 实时指标、一键迁移和 Dashboard 直达。</i>
</p>

> ⚠️ **声明**: 本项目为示例代码，仅供演示用途，不建议直接用于生产环境。请自行评估部署风险。
>
> 💡 **说明**: 本项目使用 AWS EC2 [嵌套虚拟化](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html) 在 EC2 实例内运行 KVM + Firecracker。当前支持 Intel 实例族 (c8i / m8i / r8i) 和 Graviton (ARM64)。

---

## ✨ 为什么选择龙虾池？

<table>
<tr>
<td width="50%" valign="top">

**🔒 设计层面的强隔离**
每个租户跑在自己的 Firecracker microVM 里 — 和 AWS Lambda、Fargate 用同一种轻量虚拟化技术。独立内核、OverlayFS rootfs、/24 子网、KMS 加密 EBS。**不是** Linux namespace 共享一个内核。

**🛡 真实压测过的 AZ failover**
默认 2-host 多 AZ 部署 + 自动 AZ failover。真实 AWS 上端到端验证过 — multi-tenant 同时 failover 双 Dashboard 都在 90 秒内回到 HTTP 200。6 个深层 race condition 一一治掉，对应单元测试守门。

**🤖 Bedrock AgentCore 原生集成**
一个 toggle 让每个 microVM 自动接入 AgentCore Gateway / Memory / Code Interpreter / Browser / Workload Identity。AWS Sample 仓库里少有的完整跑通 5 件套的项目。

</td>
<td width="50%" valign="top">

**📊 全栈观测，零静态凭证**
开箱即用 Amazon Managed Prometheus + Grafana。每台 host 上 ADOT collector 自动 SigV4 签名远程写。6 个 per-VM gauge (`openclaw_vm_cpu_pct`, `memory_used_mb`, `disk_used_pct`...) + audit log + Console 内置 PromQL 示例。

**⚡ 高密度低成本**
CPU 2× / Mem 1.5× 超卖（Firecracker balloon）。Spot 实例支持省 60-70%。Per-tenant Quota 防 noisy neighbor。100 租户场景 ~$250/月（vs 每租户独立 EC2 的 $2000/月）。

**🚀 一行命令 CDK 部署**
`./setup.sh <region> <profile>` 5 分钟拉起完整栈 — VPC、ASG、ALB、Lambda、DynamoDB、AMP、Cognito、AgentCore，全部接通即用。云端 rootfs build 意味着**无需本地 Linux**（macOS / Windows 直接用）。

</td>
</tr>
</table>

---

## 📑 目录

- [🚀 快速开始](#-快速开始)
- [🎯 功能特性](#-功能特性)
- [🖥️ Web Console](#%EF%B8%8F-web-console)
- [🏗️ 架构](#%EF%B8%8F-架构)
- [⚙️ 配置](#%EF%B8%8F-配置)
- [📚 API Reference](#-api-reference)
- [🌐 进阶主题](#-进阶主题)
- [⬆️ 升级指南](#%EF%B8%8F-升级指南)
- [🤝 贡献](#-贡献)
- [📄 协议](#-协议)

---

## 🚀 快速开始

> **前置条件**：AWS 账号 + CLI 已配置 · CDK CLI · Python 3.12+ · [uv](https://docs.astral.sh/uv/) 包管理

```bash
# 1️⃣ 克隆 + 配置
git clone https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker
cd sample-multi-tenant-openclaw-on-firecracker
cp config.yml.example config.yml                                  # 按需调整
cp templates/openclaw.json.example templates/openclaw.json        # 填入 LLM API key

# 2️⃣ 一键 CDK 部署 (~5 分钟) — 自动创建 VPC、ASG、ALB、Lambda、DynamoDB、
#                                AMP、Grafana、Cognito、AgentCore、KMS、WAF
./setup.sh ap-northeast-1 your-aws-profile

# 3️⃣ 构建 rootfs (~10 分钟，一次性，云端 build — 无需本地 Linux)
./scripts/build-rootfs-on-ec2.sh v1.0

# 4️⃣ 创建第一个租户
source .env.deploy
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" \
  -d '{"name":"my-first-agent","vcpu":2,"mem_mb":4096}' | jq .
```

> 打开 `setup.sh` 输出的 Console URL 即可在浏览器中管理租户。每个租户都有一键 HTTPS Dashboard，无需配置自定义域名或证书。

---

## 🎯 功能特性

> 9 大类开箱即用的能力，每一类都可以在 `config.yml` 里独立 toggle。

<details open>
<summary><b>🔒 强隔离</b> — Firecracker microVM、独立内核、/24 子网、EBS 加密</summary>

| 能力 | 详情 |
|---|---|
| **Firecracker microVM** | 每租户独占的 KVM microVM（同 Lambda/Fargate）。启动 ~200ms。 |
| **独立内核** | 每租户独立 Linux kernel — 内核 panic 不跨租户。 |
| **OverlayFS rootfs** | 只读 base + 每租户可写层，sparse 不预分配。 |
| **EBS 静态加密** | rootfs + data 卷默认 KMS 加密。 |
| **独立 /24 子网** | 每 VM `172.16.N.0/24` + 独立 tap 设备。 |
| **iptables 网络隔离** | 跨租户路由默认 **DROP** — 必须显式打洞。 |
| **PID 命名空间** | host 看不到 guest 进程；guest 互相看不到。 |

</details>

<details open>
<summary><b>🛡 高可用 — 多 AZ + 自动 AZ failover</b> (v1.3.x 旗舰)</summary>

| 能力 | 详情 |
|---|---|
| **默认 2-host 多 AZ** | `min_capacity: 2` + `multi_az.enabled: true` 都是默认值 — HA 是 opt-out 而不是 opt-in。 |
| **自动 AZ failover** | Lambda 每 5 分钟扫描 AZ outage，把受影响 tenant 迁到健康 AZ。 |
| **30 分钟冷却** | 单 AZ 防抖动，避免 outage flapping 反复迁。 |
| **ALB rule 自动跟随** | 租户迁移时 ALB listener rule 自动更新 — Dashboard URL 不变。 |
| **备份必须策略** | Path A: 没备份 → 拒绝迁 + SNS 告警（数据安全 > 可用性）。 |
| **并发 Lambda 安全** | `reserved_concurrent_executions=1` + DDB ConditionalCheck → 零数据竞争。 |
| **SSM-vs-VM verify 探针** | 用 `pgrep firecracker` + nginx conf 交叉验证 — 区分"真失败"和"SSM exit 误报"。 |
| **Audit log** | 每个 failover 事件 — `AZ_OUTAGE_DETECTED`、`AZ_FAILOVER_RECOVERED_BY_VERIFY` 等。 |

> **真实环境验证 (v1.3.2)**：双 tenant 同时迁移，2/2 都在 90 秒内 `status=running` + Dashboard HTTP 200。`tenants_failed_over: 2, tenants_failed: 0, tenants_blocked: 0`。

</details>

<details>
<summary><b>🔧 完整租户生命周期</b> — 12 个开箱即用操作，API + Console 双入口</summary>

| 操作 | API | 作用 |
|---|---|---|
| **Create / Delete** | `POST /tenants` / `DELETE /tenants/{id}` | 起 / 删租户。`?keep_data=true` 保留 data 卷。 |
| **Restart / Reset** | `/restart` / `/reset` | 重启 VM 保留磁盘 / 重装 rootfs 保留 data。 |
| **Stop / Start** | `/stop` / `/start` | 离线但保留磁盘。 |
| **Pause / Resume** | `/pause` / `/resume` | Firecracker 原生冻结 vCPU（瞬时）。 |
| **Backup** | `/backup` | 手动触发 data 卷备份到 S3。 |
| **Hot-resize vCPU** | `/resize` | 不停机扩 vCPU。 |
| **Resize disk** | `/resize-disk` | 在线扩 data 卷，自动 resize2fs。 |
| **Live migrate** | `/migrate` | snapshot/restore 跨 host — Dashboard URL 不变。 |
| **Clone** | 创建时 `clone_from` | 同 host 内 cp 数据卷 — 比备份恢复快。 |
| **Restore** | 创建时 `restore_from` | 从任意备份恢复（含 orphan）。 |
| **Tags + TTL + 定时** | 创建时 body 字段 | Tag 检索、TTL 自动停、office-hours 定时。 |
| **批量操作** | `POST /batch/tenants` | 按 ID 列表或 tag filter 做 stop/start/delete/backup。 |

</details>

<details>
<summary><b>⚡ 资源弹性</b> — ASG、超卖、Spot、Quota、Graviton</summary>

| 能力 | 详情 |
|---|---|
| **ASG 自动扩缩** | 按需起 EC2；闲置 host 两轮确认后回收。 |
| **CPU 超卖** | `cpu_overcommit_ratio: 2.0` → 8 物理 vCPU = 16 可分配。 |
| **内存超卖** | `mem_overcommit_ratio: 1.5` + Firecracker balloon → 32 GiB 物理 = 48 GiB 可分配。 |
| **Spot 实例** | `asg.use_spot: true` 省 60-70%。ASG 自动替补被回收的实例。 |
| **Per-tenant Quota** | `QUOTAS_MAX_VCPU/MEM/DATA_DISK_MB` 在创建时硬性拦。 |
| **Graviton (ARM64)** | `instance_type: r8g.2xlarge` ✅ — rootfs 同时支持双架构。 |

</details>

<details>
<summary><b>📊 观测</b> — 双层健康 + Prometheus + Grafana，零静态凭证</summary>

| 能力 | 详情 |
|---|---|
| **host-agent (5s)** | 每 host 上的 systemd service，5 秒一轮扫所有 VM 写实时指标到 DDB。 |
| **Lambda watchdog (5min)** | 跨整 fleet 扫描，检测 AZ outage，编排 failover。 |
| **Amazon Managed Prometheus** | 全托管 AMP workspace，PromQL 兼容。 |
| **Amazon Managed Grafana** | IAM Identity Center 登录 + AMP 数据源 + 示例仪表盘。 |
| **ADOT collector** | 自动 SigV4 远程写 — 全程零静态凭证。 |
| **6 个 per-VM gauge** | `openclaw_vm_health`, `cpu_pct`, `memory_used_mb`, `memory_balloon_mib`, `disk_used_mb`, `disk_used_pct` — 都带 `tenant` 和 `instance` 标签。 |
| **Audit log** | 全部变更操作 → DynamoDB（90 天 TTL）；通过 `GET /audit-log` 查询。 |

</details>

<details>
<summary><b>🤖 Bedrock AgentCore 集成</b> — 可选一键开关，5 件套全部接通</summary>

| 组件 | 作用 |
|---|---|
| **Gateway** | MCP tool hub — Lambda 函数注册成 MCP 工具。3 个 demo: `hello`, `system_info`, `timestamp`。 |
| **Memory** | 多轮对话上下文持久化。`create_event` / `list_events` / `batch_create_memory_records`。每租户独立。 |
| **Code Interpreter** | Python 3.12 sandbox。`start_session` → `executeCode` → `stop_session`。 |
| **Browser** | 远程 Chromium + WebSocket stream。可自动化。 |
| **Workload Identity** | 每个 VM 启动时自动注入临时凭证 — 零静态 key、自动刷新。 |

> AWS Sample 仓库里少数几个把 **AgentCore 5 件套**完整端到端跑通 + 用 E2E 测试验证过的项目。

</details>

<details>
<summary><b>💾 备份 + 恢复</b> — 定时 / 手动 / 跨租户 / 孤儿可恢复</summary>

| 能力 | 详情 |
|---|---|
| **定时备份** | EventBridge cron — 每天 running 租户的 data 卷 → S3。 |
| **手动触发** | `POST /tenants/{id}/backup` — 异步返回 202。 |
| **孤儿可恢复** | 源 tenant 可删，备份仍可恢复进新 tenant。 |
| **S3 lifecycle** | `backup_retention_days` 自动清理（默认 7 天）。 |
| **Trap 安全** | 任何步失败也保证 VM 一定 resume — 不会卡 paused 状态。 |
| **Pause-compress-resume** | 原子流程：暂停 → pigz 压缩 → 上传 → resume — guest 中断 < 1 秒。 |

</details>

<details>
<summary><b>🔐 安全 + 合规</b> — 7 层独立防御</summary>

| 层 | 实现 |
|---|---|
| **静态加密** | EBS 卷（rootfs + data）默认 KMS 加密。 |
| **传输加密** | CloudFront → ALB → Nginx → VM Gateway 全链路 TLS。 |
| **API 鉴权** | API Gateway 带 `x-api-key` + 可选 AWS WAF（rate limit / geo block / OWASP）。 |
| **Console 鉴权** | Cognito OAuth2 implicit flow + 可选 MFA。 |
| **RBAC** | Cognito Groups: `admin` / `operator` / `viewer` 每路由强制。 |
| **Audit log** | 所有 `POST` / `PUT` / `DELETE` 操作记录，90 天 TTL。 |
| **网络隔离** | iptables `FORWARD DROP` 跨 tenant 子网 — 跨租户通信默认禁止。 |

</details>

<details>
<summary><b>🚀 简单部署</b> — 一键 CDK + 云端 rootfs build</summary>

| 能力 | 详情 |
|---|---|
| **一键 setup** | `./setup.sh <region> <profile>` — 完整 CDK 栈 ~5 分钟。 |
| **云端 rootfs build** | `./scripts/build-rootfs-on-ec2.sh` — 启一次性 EC2 + SSM，无需本地 Linux。 |
| **自定义域名** | `./setup.sh --domain claw.example.com --cert <acm-arn>` — ACM 在 `us-east-1`。 |
| **Cognito + RBAC** | 可选 console_auth；admin/operator/viewer 三档守门所有变更 API。 |
| **Manifest rootfs 版本** | `manifest.json` 跟踪 rootfs 版本；每 host 注册表。 |
| **Terraform 平价** | Terraform 模块与 CDK 栈对等，已有 Terraform 体系的团队可选。 |

</details>

---

## 🖥️ Web Console

CloudFront 上托管的 Web Console（`/console/`），Cognito 鉴权，5 个 tab 涵盖运维所需。

### Tenants tab — 多 host、多 AZ 实时操作

左侧 Hosts 按 AZ 分组（每个卡片显示 CPU / Memory / VM 数 + 超卖比例）。Tenants 表格每行实时 vCPU / Memory / Disk 进度条 + AZ 列 + gateway/health 信号灯 + 每租户 Migrate 按钮。AgentCore + Shared Skills 折叠在顶部：

![Tenants tab](../docs/web_console.png)

### Application tab — 模板、MCP 工具、共享 Skills

Config Templates 管理器 + MCP Tools 卡片（自动通过 AgentCore Gateway 拉取 Lambda-backed 工具列表 + input schema）+ Shared Skills 含每个 Skill 的 S3 直链：

![Application tab](../docs/web_console_application.png)

### Monitoring tab — AMP / Grafana / SNS 一目了然

观测概览：AMP / Grafana / SNS 状态、所有 per-VM Prometheus gauge 含类型 + 标签 + 描述、可复制粘贴的 PromQL 示例、AMP `remote_write` / Grafana endpoint：

![Monitoring tab](../docs/web_console_monitoring.png)

### Backups tab — 跨租户浏览 + 孤儿支持

跨租户备份浏览器，标记 active vs orphan（orphan = 源租户已删但备份仍可恢复进新租户）。默认 S3 lifecycle 7 天清理：

![Backups tab](../docs/web_console_backup.png)

### Settings tab — 基础设施状态 + Fleet by AZ

一页全览：API 连接 / AgentCore Gateway URL / Multi-AZ HA / Prometheus + Grafana / AWS WAF / Cognito + RBAC / SNS 生命周期事件 / per-tenant Quota / Host 超卖比例 / 实时 **Fleet by AZ** 表显示 host 和 VM 在各 AZ 的分布：

![Settings tab](../docs/web_console_settings.png)

---

## 🏗️ 架构

### 系统架构

![System Architecture](../docs/oc-system-arch.png)

### 部署架构

![Deployment Architecture](../docs/oc-deploy-arch.png)

<details>
<summary><b>ASCII 版（适合 AI / 文本访问）</b></summary>

```
管理员 / 用户
    │
    ├── API Gateway (HTTPS, x-api-key) ──→ Lambda ──→ DynamoDB
    │                                                  ├── tenants
    │                                                  └── hosts
    │
    └── ALB (HTTPS) ──→ Host Nginx:80 ──→ VM Gateway:18789
                        ├── /vm/{tenant-a}/ → 172.16.1.2
                        └── /vm/{tenant-b}/ → 172.16.2.2

Lambda ── SSM Run Command ──→ EC2 Host
                               ├── microVM 01 (172.16.1.2)
                               ├── microVM 02 (172.16.2.2)
                               └── ...

S3: rootfs 分发 + 数据备份 + 共享 Skills
ASG: 自动扩缩 host
EventBridge: 健康检查 + 闲置回收 + 定时备份
```

</details>

### 网络模型

每 VM 独立 /24 子网，通过 TAP 设备与 host 通信：

```
VM1: tap-vm1  host=172.16.1.1/24  guest=172.16.1.2/24
VM2: tap-vm2  host=172.16.2.1/24  guest=172.16.2.2/24
VMn: tap-vmN  host=172.16.N.1/24  guest=172.16.N.2/24
```

- **出网**：iptables MASQUERADE → 互联网
- **入网**：ALB → Nginx 反代 → VM:18789
- **跨 VM**：完全隔离，子网间无路由

### 项目结构

```
sample-multi-tenant-openclaw-on-firecracker/
├── deploy/                    # CDK 项目
│   ├── app.py                 # CDK app 入口
│   ├── stack.py               # 基础设施定义
│   ├── lambda/
│   │   ├── api/handler.py             # 租户 CRUD + host 管理
│   │   ├── templates/handler.py       # 配置模板 CRUD
│   │   ├── skills/handler.py          # 共享 Skills 列表
│   │   ├── health_check/handler.py    # 定时健康 + AZ failover
│   │   ├── agentcore_tools/handler.py # AgentCore Gateway Lambda 工具
│   │   └── scaler/handler.py          # 闲置 host 回收
│   └── userdata/
│       ├── init-host.sh       # host 初始化
│       ├── host-agent.py      # VM 健康轮询 + DDB 写入 + balloon
│       ├── launch-vm.sh       # microVM 启动
│       └── stop-vm.sh         # microVM 停止
├── console/                   # Web 管理控制台
├── tests/                     # 426+ 测试（unit / integration / e2e）
├── templates/                 # OpenClaw 配置模板
├── scripts/
│   ├── build-rootfs-on-ec2.sh # 云端构建（无需本地 Linux）
│   ├── destroy.sh             # 销毁栈
│   ├── oc-connect.sh          # SSH 风格直连租户 VM
│   └── oc-dashboard.sh        # 打开租户 Dashboard URL
├── pyproject.toml             # Python 项目配置
├── cdk.json                   # CDK app 配置 + feature flag
├── config.yml.example         # 基础设施配置模板
└── setup.sh                   # 一键部署 + .env.deploy 导出
```

---

## ⚙️ 配置

### 配置文件

| 文件 | 用途 |
|---|---|
| `config.yml` | 基础设施配置 — 从 `config.yml.example` 复制并自定义 |
| `templates/openclaw.json` | OpenClaw 应用配置（model、API key、provider）|
| `.env.deploy` | 部署环境（region、API URL/Key、bucket）— `setup.sh` 自动生成 |

### `config.yml` 关键 toggle

| 段 | Key | 默认 | 说明 |
|---|---|---|---|
| `host` | `instance_type` | `m8i.2xlarge` | 必须支持嵌套虚拟化（c8i / m8i / r8i / r8g）。 |
| `host` | `cpu_overcommit_ratio` | `2.0` | CPU 超卖比例。 |
| `host` | `mem_overcommit_ratio` | `1.0` | 内存超卖比例（需开 balloon）。 |
| `vm` | `default_vcpu` | `2` | 每租户默认 vCPU。 |
| `vm` | `default_mem_mb` | `4096` | 每租户默认内存（MB）。 |
| `balloon` | `enabled` | `false` | Firecracker balloon 设备（用于内存超卖）。 |
| `asg` | `min_capacity` | `2` | 最小 host 实例数（默认 Multi-AZ）。 |
| `asg` | `use_spot` | `false` | Spot 实例（省 60-70%，可能被回收）。 |
| `multi_az` | `enabled` | `true` | Multi-AZ HA — 启用 AZ failover。 |
| `health_check` | `interval_minutes` | `5` | Lambda watchdog 间隔。 |
| `metrics` | `enabled` | `false` | 创建 AMP + Grafana + ADOT。 |
| `agentcore` | `enabled` | `false` | 创建 Gateway + Memory + CodeInterpreter + Browser + Identity。 |
| `console_auth` | `enabled` | `false` | Console 启用 Cognito 鉴权。 |
| `backup_cron` | — | `cron(0 19 * * ? *)` | UTC 19:00 每天备份。 |

> 完整参考请见 [`config.yml.example`](../config.yml.example)。

### 销毁栈

```bash
./scripts/destroy.sh           # 销毁栈，保留 S3 + DynamoDB
./scripts/destroy.sh --purge   # 完全清理含数据
```

---

## 📚 API Reference

所有请求都需要 `x-api-key` header。

### Tenants

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/tenants` | 列出所有租户。`?tag=key:value` 过滤（可重复，对 pair 做 AND）。|
| `POST` | `/tenants` | 创建。Body: `{name, vcpu, mem_mb, data_disk_mb, config_template, tags, ttl_hours, on_expiry, schedule, restore_from, clone_from}` — 仅 `name` 必填。 |
| `GET` | `/tenants/{id}` | 获取租户详情。|
| `DELETE` | `/tenants/{id}` | 删除（`?keep_data=true` 保留 data 卷）。|
| `POST` | `/tenants/{id}/restart` | 重启 VM（保留磁盘）。|
| `POST` | `/tenants/{id}/stop` · `/start` | 停止 / 启动。|
| `POST` | `/tenants/{id}/pause` · `/resume` | Firecracker 原生 vCPU 冻结 / 解冻。|
| `POST` | `/tenants/{id}/reset` | 重装 rootfs（保留 data）。|
| `POST` | `/tenants/{id}/backup` | 手动数据备份。|
| `POST` | `/tenants/{id}/resize` | 热扩 vCPU。Body: `{"vcpu":4}`。|
| `POST` | `/tenants/{id}/resize-disk` | 离线扩 data 卷。Body: `{"new_size_mb":16384}`。|
| `POST` | `/tenants/{id}/migrate` | 实时迁移。Body: `{"target_host_id":"i-..."}`。|
| `GET` | `/tenants/{id}/backups` | 单租户的备份列表。|
| `POST` | `/batch/tenants` | 批量操作。Body: `{"action":"stop\|start\|delete\|backup", "ids":[...]\|"filter":{"tag":"k:v"}}`。|

### Backups, Hosts, AgentCore, Audit, Skills, Templates

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/backups` | 跨租户列出所有备份（标 active vs orphan）。|
| `GET` | `/hosts` | 列出所有 host。|
| `POST` | `/hosts/refresh-rootfs` | 推送最新 rootfs 到所有 host。|
| `GET` | `/hosts/rootfs-version` | 查询当前 rootfs 版本。|
| `GET` | `/agentcore/status` | AgentCore 启用状态 + Gateway URL。|
| `GET` | `/agentcore/tools` | 列出 Gateway 上注册的 MCP 工具。|
| `GET` | `/audit-log` | 查 audit log。`?since=<ISO8601>&limit=<n>` — 90 天 TTL。|
| `GET` | `/skills` | 列出共享 Skills（S3 管理）。|
| `GET` · `PUT` · `DELETE` | `/templates/{name}` | 配置模板 CRUD（`default` 只读）。|
| `GET` | `/system/info` | feature flag + config 快照（region、version、multi_az、metrics、…）。|

---

## 🌐 进阶主题

<details>
<summary><b>自动备份 + 恢复 — 流程 + 任意备份恢复（孤儿安全）</b></summary>

EventBridge 每天定时备份所有 running 租户的 data 卷到 S3。也支持手动触发。

**备份流程**：暂停 VM → `pigz` 压缩 `data.ext4` → 恢复 VM → 上传 S3。失败时 VM 自动 resume（trap cleanup）。

```bash
source .env.deploy

# 手动备份（异步返回 202）
curl -s -X POST "${API_URL}tenants/{id}/backup" -H "x-api-key: ${API_KEY}" | jq .

# 跨租户列出所有备份（标 active vs orphan）
curl -s "${API_URL}backups" -H "x-api-key: ${API_KEY}" | jq .
```

**从备份恢复**（孤儿安全 — 源 tenant 不存在也行）：

```bash
# 从某个（可能已删除的）租户的最新备份恢复
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

</details>

<details>
<summary><b>🔐 双域名安全分离（v1.3.4+，生产环境强烈推荐）</b></summary>

**为什么要分？** 当 `console/*` 和 `vm/*` 共用一个 CloudFront + 一个域名时：

- Cognito session cookie 设在父域 → 自动发给 `/vm/*` 请求
- 某个租户 dashboard 渲染未转义的输入（XSS）→ 同源策略下能访问 console DOM → 偷走 admin token
- 一个租户被攻破 = 全平台 admin 权限沦陷

**双域名模式**：创建两个独立的 CloudFront distribution，各自 ACM cert。Cognito session cookie 物理 scope 到 `console_domain`，浏览器原生不允许把它发到 `app_domain`。

```bash
# 前置条件：
#   1. 在 us-east-1 申请 2 张 ACM 证书（console_domain 一张，app_domain 一张）
#   2. 部署后把两个域名分别 CNAME 到对应的 CloudFront

./setup.sh ap-northeast-1 lab \
  --console-domain console.example.com --console-cert arn:aws:acm:us-east-1:xxx:certificate/console-xxx \
  --app-domain     app.example.com     --app-cert     arn:aws:acm:us-east-1:xxx:certificate/app-xxx
```

部署完成后 `setup.sh` 会打印两个独立的 URL：

```
→ Console URL:    https://console.example.com/console/index.html  (操作员登录)
→ Dashboard URL:  https://app.example.com/vm/<tenant-id>/         (每租户)
  ✓ Dual-domain mode active — Cognito session 物理隔离于 tenant dashboards
```

| 属性 | 单域名（legacy）| **双域名（推荐）** |
|---|---|---|
| CloudFront 分发数 | 1 | 2 |
| ACM 证书数 | 1 | 2 |
| Cookie scope | 共享同源 | **仅 console_domain** |
| XSS 影响范围 | 全 dashboard + console | 单租户 |
| `OC_CONSOLE_BASE` / `OC_DASHBOARD_BASE` | 相同 | 不同 |

老的 `--domain` / `--cert` 仍兼容（适合 dev / sample 部署）。

</details>

<details>
<summary><b>共享 Skills — S3 → Host → VM 同步链（带 per-tenant / per-group 分发）</b></summary>

所有租户共享统一的 Skill 集（`SKILL.md` 文件），但每租户 memory 独立。

```bash
# 上传 Skill 到 S3（自动同步到所有 host，新 VM 启动时注入）
aws s3 sync ./my-skills/ s3://${ASSETS_BUCKET}/skills/ --profile $PROFILE

# 同步链：
#   S3 → Host /data/shared-skills/ (cron 5min) → 新 VM 启动时
```

**1.4.0 (#62) — per-tenant / per-group skill 分发**：默认每个 VM 拿全部 skill（广播，v1.3.x 行为）。要限制某租户只拿子集：

```bash
# 1) 定义一个 skill 组（可选）
curl -s -X POST "${API_URL}groups" -H "x-api-key: ${API_KEY}" -d '{
  "name": "team-sre",
  "skills": ["k8s-debug", "incident-response"],
  "description": "SRE 团队标准工具"
}'

# 2a) 单租户 scoping（启动时只 cp 这些 skill 子目录）
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" -d '{
  "name": "research-agent",
  "vcpu": 2, "mem_mb": 4096,
  "skills": ["web-search", "code-review"]
}'

# 2b) 仅 group scoping
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" -d '{
  "name": "sre-bot",
  "group": "team-sre"
}'

# 2c) 两者都设 — effective set = tenant.skills ∪ group.skills
curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" -d '{
  "name": "sre-extras",
  "skills": ["pagerduty"],
  "group": "team-sre"
}'

# 检查某租户实际会拿到哪些 skill
curl -s "${API_URL}tenants/{id}" -H "x-api-key: ${API_KEY}" | jq .effective_skills
# → ["incident-response", "k8s-debug", "pagerduty"]   （或 "*" 表示广播）
```

老租户（无 `skills`、无 `group`）继续拿到所有 skill — **完全向后兼容**，从 v1.3.x 升级无需迁移。

</details>

<details>
<summary><b>自定义域名 — 绑定 ACM + CloudFront（legacy 单域名模式）</b></summary>

```bash
# 前置条件：
#   1. 在 us-east-1 申请 ACM 证书（CloudFront 必需）+ 完成 DNS 验证
#   2. 把您的域名 CNAME 到 CloudFront 域名（见 DashboardUrl 输出）

./setup.sh ap-northeast-1 lab \
  --domain claw.example.com \
  --cert   arn:aws:acm:us-east-1:xxx:certificate/xxx

# 或直接编辑 config.yml 的 cloudfront: 段。
# 解绑：--domain "" 重新跑 setup.sh。
```

</details>

<details>
<summary><b>自动扩缩 — 扩容 + 闲置回收</b></summary>

**扩容** — 创建租户时没有可用 host → 租户进 `pending` → ASG 起新实例 → 初始化完后 pending 租户自动分配。

**缩容** — Scaler Lambda 每 5 分钟扫一次：
1. `vm_count=0` 的 host 超过 `idle_timeout_minutes` → 标 `idle`。
2. 下一轮还是 idle 且 ASG 实例数 > min → 终止。
3. 期间分配进来一个租户 → 自动恢复 `active`。

</details>

<details>
<summary><b>观测（可选） — AMP + Grafana + 示例 PromQL</b></summary>

`config.yml` 里 `metrics.enabled: true` 时，stack 创建一个 AMP workspace 和一个 AMG workspace。每台 host 跑一个 ADOT collector 作为旁车 systemd service，每 30 秒抓 `host-agent` 的 `/metrics` endpoint，自动 SigV4 签名远程写到 AMP — 全程零静态凭证。

每个 VM 暴露的 gauge（带 `tenant=<tenant_id>` 和 `instance=<host_instance_id>` 标签）：

```
openclaw_vm_memory_used_mb        openclaw_vm_disk_used_mb
openclaw_vm_memory_balloon_mib    openclaw_vm_disk_total_mb
openclaw_vm_health (0|1)          openclaw_vm_disk_used_pct
```

示例 PromQL：

```promql
# 单租户所有 running VM 的内存合计
sum by (tenant) (openclaw_vm_memory_used_mb)

# 最近 1 分钟有过不健康 VM 的 host
min_over_time(openclaw_vm_health[1m]) == 0
```

Grafana 访问通过 **AWS IAM Identity Center**：

1. 部署后 `GrafanaWorkspaceUrl` CFN output 里有 workspace URL。
2. 在 IAM Identity Center 把您（或某个 group）assign 到那个 AMG workspace。
3. 首次登录：Grafana → *Connections → Data sources → Add → Prometheus*，从下拉里选 AMP workspace（AMG service role 已经有 `aps:QueryMetrics` 权限）。

之后想关观测，把 `metrics.enabled: false` 重新跑 `setup.sh` — AMP 和 AMG 自动删除，停止计费。

</details>

<details>
<summary><b>Rootfs 管理 — 版本化 + 刷新</b></summary>

构建脚本产出两个镜像：rootfs（OS + 软件）和 data template（`/home/agent` 预配内容）。版本通过 S3 `manifest.json` 管理，host 和 tenant 都跟踪自己的 `rootfs_version`。

```bash
# 构建 + 上传（更新 manifest.json + 刷新 host）
./build-rootfs.sh v1.8

# 手动刷新 host 镜像
source .env.deploy
curl -s -X POST "${API_URL}hosts/refresh-rootfs" -H "x-api-key: ${API_KEY}" | jq .

# 查询当前版本
curl -s "${API_URL}hosts/rootfs-version" -H "x-api-key: ${API_KEY}" | jq .

# 新建 tenant 立刻用新版本；已有 tenant 需要 reset API 才换。
```

</details>

---

## ⬆️ 升级指南

升级通常涉及**两层**：控制面（CDK 栈）和数据面（rootfs + host-agent）。

### 通用流程（任意版本）

```bash
git pull                                  # 拉最新 main
git diff HEAD@{1} HEAD -- config.yml.example  # 检查新配置 key
# 如有新 key，手动合并到本地 config.yml

./setup.sh <region> <profile>             # 重新部署 CDK 栈（幂等）

# 重建 rootfs（见"快速开始"第 3 步）
source .env.deploy && ./build-rootfs.sh <new_version>

# 新建 tenant 立刻用新 rootfs；已有 tenant 调 `reset` API 才会切换。
```

### 最新：v1.4.1 → **v1.4.2**（关键修复：修复"假 failover" bug — 生产强烈推荐升级）

v1.4.2 修复一个**沉默的正确性 bug**：之前的版本可能标记 `AZ_FAILOVER_TENANT_RECOVERED` 并把 DDB flip 到 `status=running`，但实际 dashboard URL 是 502 不通的。三个根因都在 `_failover_tenant_to_host`：

1. verify probe 只检查 firecracker 进程和 nginx conf 文件存在 —— 既不能证明 guest 启动完了，也不能证明 nginx reload 了新 conf
2. ALB repoint 失败被静默 swallow，failover 仍标记成 "recovered"
3. flip DDB 之前没有任何一步真去确认 dashboard URL 能打开

v1.4.2 让 failover **真正** end-to-end 验证之后才标 success：

- **强化 verify probe**：加 `curl http://127.0.0.1/vm/<tid>/` 检查，5xx / connection-refused 都视为失败
- **ALB repoint 失败现在 raise** — 不再 swallow
- **新增跨 ALB 可达性 gate**：直接打 `http://<ALB_DNS>/vm/<tid>/`，30s 内必须回非 5xx，否则不 flip DDB
- **新状态 `failover_failed_partial`**：VM 起来了但 ALB/跨-ALB 失败 → 运维必须人工修 ALB rule

```bash
git pull && ./setup.sh <region> <profile>
```

redeploy 后，health_check Lambda 多了 `PUBLIC_BASE_URL` 环境变量（由 CDK 自动注入 `http://<alb_dns>`），跨 ALB gate 启用。CloudWatch 日志现在会区分 "VM 没起来" 和 "VM 起来了但 ALB 没更新"，不再静默吐 fake success。

`tests/test_failover_genuine.py` 加 16 个新单测明确覆盖三个根因（6 假阳性 + 4 happy-path + 6 跨 ALB probe）。**534 passed / 0 failed**。

要找出可能被老 bug 影响的租户：
```bash
aws dynamodb scan --table-name openclaw-tenants \
  --filter-expression 'attribute_exists(failover_at) AND #s = :running' \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":running":{"S":"running"}}' --profile $PROFILE
# 然后人工 probe 每个 dashboard URL — 502 的就是 fake-failover 受害者
```

### v1.4.0 → v1.4.1（Console 的 skills + groups CRUD UI）

v1.4.1 修复 [#63](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/63)：v1.4.0 (#62) 落地了 per-tenant skill scoping 的数据模型 + API，但运维还得开终端 `aws s3 cp` 才能编辑/上传 skill，用 `curl` 才能管 group。v1.4.1 把 Application tab 升级成真正的管理界面 —— `GET/PUT/DELETE /skills/{name}` 撑起的 Edit/Upload/Delete 按钮，加上一张连到 v1.4.0 端点的 Skill Groups 卡。

```bash
git pull && ./setup.sh <region> <profile>
```

无数据迁移。`s3://${ASSETS_BUCKET}/skills/<name>/SKILL.md` 在新编辑器里立刻可见。Console 在下一次 CloudFront 刷新生效。RBAC：`viewer` 看到只读列表 + 预览；`operator+` 看到 Edit / Delete / + New / Group 管理按钮。

`tests/test_skill_crud.py` 加 16 个单元测试，覆盖 read/update/delete + content 校验（必须含 `# Title`、≤256 KiB、合法 name 正则）+ RBAC routing。**493 passed / 0 failed**（1.4.0 baseline 477 + 16 new）。

### v1.3.4 → v1.4.0（per-tenant skill 分发 — 可选的安全加固）

v1.4.0 修复 [#62](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/62)：v1.4.0 之前每个 VM 启动时都会被 `cp -r` 整个 `s3://${ASSETS_BUCKET}/skills/` —— 没法把 SRE 团队的应急响应 skill 隔离到只给 SRE 团队的 tenant。v1.4.0 加上 tenant 级 + group 级的 skill scoping；不设 scope 的 tenant 仍然拿到全部 skill，**完全向后兼容**。

```bash
git pull && ./setup.sh <region> <profile>
```

新增 `openclaw-groups` DDB 表 + 4 个 API 端点（`GET/POST /groups`、`POST /groups/{name}/skills`、`DELETE /groups/{name}/skills/{skill}`）。`POST /tenants` body 现在接受 `skills: [...]` 和/或 `group: "..."` 字段。`GET /tenants/{id}` 返回 `effective_skills`（resolved 后的 union，或 `"*"` 表示广播）。用法见进阶主题里的 [共享 Skills](#-进阶主题) 章节。

`tests/test_skill_scoping.py` 加 29 个新单元测试，覆盖 issue 中提到的 6 个语义场景：空 / 单 / 仅 group / 仅 tenant / 都设 / 未知 group。

### v1.3.3 → v1.3.4（安全加固 — 任何生产多租户部署强烈推荐）

v1.3.4 修复 [#61](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/61)：v1.3.3 及之前的版本里，操作员控制台（`/console/*`）和每个租户的 dashboard（`/vm/*`）共用一个 CloudFront + 一个域名，意味着 Cognito session cookie 会被发到 tenant DOM 上。v1.3.4 引入**双域名模式**：创建两个独立的 CloudFront distribution + 两张 ACM cert，Cognito session cookie 物理上 scope 到 `console_domain`，浏览器不会把它发到 `app_domain`。

**向前兼容**：v1.3.3 老部署不设新字段时继续用 legacy 单域名模式。**不强制迁移**，但生产多租户部署强烈建议切换。

```bash
# 在 us-east-1 准备两张 ACM 证书（console_domain 一张，app_domain 一张）
git pull && ./setup.sh <region> <profile> \
  --console-domain console.example.com --console-cert <acm-arn> \
  --app-domain     app.example.com     --app-cert     <acm-arn>
```

部署后：

- setup 打印**两个独立 URL** —— 操作员控制台（Cognito 鉴权）和每租户 dashboard
- `OC_CONSOLE_BASE` 和 `OC_DASHBOARD_BASE` 在 `console/config.js` 里独立注入
- CloudFormation outputs 多了 `DualDomainMode: true`
- 完整安全推理见进阶主题里的 [双域名安全分离](#-进阶主题)

### v1.3.2 → v1.3.3（host 容量计数器一致性）

v1.3.3 修复 [#59](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/59) 和 [#60](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues/60)：`/migrate` API 没更新 host 容量计数器（源 host `used_vcpu` 未减、目标 host 未加），也没做目标容量预检。v1.3.3 加上这两点，并把 AZ failover 也补上 `used_vcpu`/`used_mem_mb` 增量更新（之前只更新 `vm_count`）。

```bash
git pull && ./setup.sh <region> <profile>
```

无数据迁移需求 — 修复仅影响部署后的新迁移。`tests/test_migration.py` 加了 4 个回归测试。

### 最新：v1.3.1 → **v1.3.2**（强烈推荐任何多租户部署）

v1.3.2 修了所有真实环境压测才会暴露的问题：并发 Lambda 互踩、multi-tenant 同时迁移、内核短暂 race、host-agent ⇄ Lambda 互踩导致 SSM exit 不准。

```bash
git pull && ./setup.sh <region> <profile>     # 加上 reserved_concurrent_executions=1
```

部署完成后，重新分发 `launch-vm.sh` 到现有 host：

```bash
source .env.deploy
aws s3 cp deploy/userdata/launch-vm.sh s3://${ASSETS_BUCKET}/deployment/scripts/launch-vm.sh
aws ssm send-command --document-name AWS-RunShellScript \
    --targets Key=tag:aws:autoscaling:groupName,Values=openclaw-hosts-asg \
    --parameters 'commands=[
      "aws s3 cp s3://'${ASSETS_BUCKET}'/deployment/scripts/launch-vm.sh /home/ubuntu/launch-vm.sh",
      "chmod +x /home/ubuntu/launch-vm.sh"
    ]'
```

**新增的 audit log 条目要关注：**

- `AZ_FAILOVER_RECOVERED_BY_VERIFY` — 信息（verify 探针救回了一个看似失败的迁移）
- `AZ_FAILOVER_SKIPPED_CONCURRENT` — 信息（并发 Lambda 主动让步）
- `AZ_FAILOVER_TENANT_FAILED` — 需要处理（verify 探针确认真失败）
- `AZ_FAILOVER_NO_BACKUP` — 需要处理（tenant 无备份，拒绝迁移）

**summary 字段语义彻底分离**：`tenants_failed_over`、`tenants_failed`、`tenants_blocked` 三相独立。

<details>
<summary><b>更老的升级路径（v1.3.0 → v1.3.1, v1.2.9 → v1.3.0, v1.2.7 → v1.2.9）</b></summary>

更老版本升级请参考 **[CHANGELOG.md](../CHANGELOG.md)**，每版本都有 operator notes。

简要参考：

- **v1.3.0 → v1.3.1**：5 个 path-A 集成 bug 修复。重新分发 `launch-vm.sh`。`__az_failover_state__` 合成记录用于 cooldown 持久化。
- **v1.2.9 → v1.3.0**：ASG `min_capacity` 1 → 2（multi-AZ 默认）。`health_check` Lambda 新增 IAM 权限。
- **v1.2.7 → v1.2.9**：多个 console + 观测修复。已有 host 需要重新分发 `host-agent.py`（带真实 CPU% / 内存指标）。

</details>

---

## 🤝 贡献

欢迎贡献！请见 [CONTRIBUTING.md](../CONTRIBUTING.md) 了解代码规范、PR 指南和安全 issue 报告流程。

安全相关问题请见 [CONTRIBUTING.md#security-issue-notifications](../CONTRIBUTING.md#security-issue-notifications)。

---

## 📄 协议

本仓库采用 **MIT-0 协议**。请见 [LICENSE](../LICENSE) 文件。

> MIT-0 是 MIT 协议的"无需署名"变体。您可以使用、修改、再分发本代码（含商用），无任何强制义务。AWS Sample 项目用此协议是为了降低采用门槛。

---

## 🌟 Star History

<a href="https://www.star-history.com/#aws-samples/sample-multi-tenant-openclaw-on-firecracker&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=aws-samples/sample-multi-tenant-openclaw-on-firecracker&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=aws-samples/sample-multi-tenant-openclaw-on-firecracker&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=aws-samples/sample-multi-tenant-openclaw-on-firecracker&type=Date" />
 </picture>
</a>

---

<p align="center">
  Made with 🦞 by AWS Samples · MIT-0 · 2026<br/>
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues">🐛 报告 Bug</a> ·
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/issues">💡 功能建议</a> ·
  <a href="https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker/discussions">💬 讨论</a>
</p>
