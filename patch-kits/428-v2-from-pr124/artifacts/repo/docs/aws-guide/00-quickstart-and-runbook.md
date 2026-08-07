# 快速上手与部署运行手册（倒金字塔版）

> 本文是一份自包含的"从零到跑通"的可用文档，按倒金字塔组织：先给结论与系统价值，再给架构，最后是可勾选的部署步骤与常见报错排查。要逐章深读，见 `docs/aws-guide/` 的分章实施指南（本文的每一节都指向对应章）。
>
> 数字口径以 `engineering/02-system-constraints/FACT-BASELINE.md` 为准；架构口径以 `engineering/00-knowledge-base/map.md` 为准。示例中的账号 ID、域名、凭据均为占位符。

---

## 一、结论先行

**这套方案是什么**：在 AWS 上为成千上万租户各自交付一台独立内核的 Firecracker microVM，在其中跑带身份、技能与安全护栏的 OpenClaw AI agent。它用 microVM 把"越权止于单租户"从容器级下沉到虚拟机级，叠加 L1–L5 五层纵深防护，每层都落到部署代码或设备，不靠模型自律。

**它替你解决的核心问题**：容器多租户一旦被逃逸，攻击面是整个宿主上的所有租户；本方案给每租户一台独立 Linux 内核的 microVM，把单租户逃逸物理封在它自己的 VM 里，跨租户东西向流量在宿主 iptables 上被丢弃（加固后目标态 100% 丢包，实测漏洞态为 0% 丢包）。

**代价与边界**：每租户约 2 GB 内存的固定开销（`r8g.metal-24xl` 单台稳态承载约 380 租户，容量推算），以及"改配置必须重建镜像、不热改运行中 VM"的运维纪律。追求极致密度、能接受共享内核风险的场景，本方案不是最省的选择。

### CTO 摘要

以 SaaS 形态在 AWS 上托管成千上万个互相隔离的 AI agent，每租户独立内核虚拟机；控制面无服务器按用量付费，单台裸金属稳态承载约 380 租户、成本约每租户每月 8.36 美元（推算）。业务样本可替换，隔离与安全层不变。基础架构工程师视角与逐层参考架构详见第 1 章。

---

## 二、架构一页纸

```
数据面（实时聊天，opt-in edge/redis）
  浏览器 ──wss /gw/ws (平台会话 JWT)──► 平台后端 WebSocket 网关
                                          │ (ws 客户端 + Ed25519 device 握手)
                                          ▼
              CloudFront ──► ALB ──► OpenResty edge ASG
                                          │  查 ElastiCache Redis route:{tenant_id}
                                          ▼
              宿主 iptables DNAT ──► microVM gateway :18789
              （microVM 只对宿主内部 TAP 暴露，无公网入站）

控制面（CRUD / 生命周期，无服务器）
  管理员/后端 ──API Gateway (x-api-key + 可选 Cognito RBAC)──► Lambda ──► DynamoDB
                                                                 └─ SSM / SQS 派发 ──► EC2 metal host ──► microVM ×N
```

- **身份**：数据面 = OpenClaw 原生 Ed25519 device 非对称认证（平台后端代持私钥、公钥冷注入 microVM）+ per-tenant gateway token（KMS 信封加密）；控制面 = `x-api-key` + 可选 Cognito RBAC。**已弃**早期的 claw-hub 中枢 + claw-channel 出站 + Cognito 三处身份模型。
- **冷注入**：身份/skill/config/token 在 Firecracker `InstanceStart` 之前注入到只读/数据盘，VM 启动即成品，运行后控制面对它零数据注入。改身份 = 改部署代码 → 重烤镜像 → 灰度滚动重建。

逐跳 file:line 细节见「架构详情」（第 3 章）与「数据面两级路由」（第 13 章）。

---

## 三、部署前环境要求（逐条对照，缺一即失败）

> ⚠️ **注意**：下表每一条不满足都会让部署或 host 自举直接失败。对应报错现象与修复见"六、常见问题与排错"。

| #   | 要求                                                 | 说明                                                                   | 不满足的后果                                    |
| --- | ---------------------------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------- |
| 1   | **宿主机型带 KVM**                                   | 生产 `r8g.metal-24xl`（arm64 裸金属，原生 KVM）；开发用带 KVM 的机型   | 无 `/dev/kvm` → host 自举失败 → ASG 反复替换    |
| 2   | **部署机装 Docker 且 daemon 在跑**                   | AWS CDK 用容器给 Lambda 交叉打包 arm64 扩展                            | `cdk deploy` 卡在 asset bundling                |
| 3   | **`config.yml` 已从 example 复制**                   | 仓库只提供 `config.yml.example`（`config.yml` 在 `.gitignore`）        | `setup.sh` 读不到配置直接报错                   |
| 4   | **CDK 已 bootstrap**                                 | 全新账号/区域先 `cdk bootstrap`（或用最小 IAM policy 借道）            | 报 `/cdk-bootstrap/hnb659fds/version not found` |
| 5   | **Python 3.12+ / AWS CLI / CDK CLI / uv**            | `pyproject.toml` 要求 `requires-python >=3.12`，`aws-cdk-lib>=2.251.0` | synth/deploy 依赖缺失                           |
| 6   | **挂自定义域名时 ACM 证书在 us-east-1**              | CloudFront 只认 us-east-1 的 ACM 证书                                  | CloudFront 创建失败、整栈回滚                   |
| 7   | **目标区域支持 Graviton metal + Bedrock Guardrails** | 生产底座依赖二者                                                       | 无法拉起宿主 / L1 内容层不可用                  |

---

## 四、部署 Checklist

### 阶段 A — 准备（本地一次性）

- [ ] 1. 克隆仓库并进目录。
  ```bash
  git clone <repo-url> && cd sample-multi-tenant-openclaw-on-firecracker
  ```
- [ ] 2. 复制配置模板（`config.yml` 不入库，必须手动复制）。
  ```bash
  cp config.yml.example config.yml          # 再按需改 network.mode / arch / instance_type
  cp templates/openclaw.json.example templates/openclaw.json   # 填 LLM API key / provider
  ```
- [ ] 3. 确认部署机 Docker daemon 可用（CDK 交叉打包 arm64 Lambda 扩展要用）。
  ```bash
  docker ps                                 # 能列出即可，无需已有容器在跑
  ```
- [ ] 4. 全新账号/区域先 bootstrap（已 bootstrap 过则自动跳过）。
  ```bash
  cdk bootstrap aws://<account>/<region>    # 或用 docs/aws-guide/deploy-iam-policy.json 最小权限身份借道
  ```

### 阶段 B — 部署基础设施

- [ ] 5. 一键部署（默认 `image.build_in_stack: true`，栈内 CodeBuild 烤黄金镜像后 ASG 才起 host）。
  ```bash
  ./setup.sh <region> <your-aws-profile>    # 挂域名加 --console-domain/--console-cert/--app-domain/--app-cert
  ```
- [ ] 6. 确认 CloudFormation 栈 `OpenClawOrchestrator` 到达 `CREATE_COMPLETE`，并在 Outputs 查到 `ApiUrl`、`AssetsBucket`、`BackupBucket`。
  ```bash
  aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
    --query 'Stacks[0].StackStatus' --profile <profile> --region <region>
  ```
- [ ] 7. 等首台 host 自举完成并注册到宿主表（ASG 拉起 → `init-host.sh` → 注册）。

### 阶段 C — 验证端到端

- [ ] 8. 导出部署环境变量，注册第一个租户。
  ```bash
  source .env.deploy
  # 注册一个租户（仅 name 必填；vcpu/mem_mb 默认 2 / 4096）
  curl -s -X POST "${API_URL}tenants" -H "x-api-key: ${API_KEY}" \
    -d '{"name":"my-first-agent","vcpu":2,"mem_mb":4096}' | jq .
  ```
- [ ] 9. 轮询到 `status=running`（注册 API 返回约 1.7s、`creating→running` 约 4.0s，实测）。
  ```bash
  curl -s "${API_URL}tenants/my-first-agent-xxxx" -H "x-api-key: ${API_KEY}" | jq '.status'
  ```
- [ ] 10. （启用数据面时）取 gateway token 密文并本地解密，走两级路由发一条聊天消息确认 agent 回复（端到端首回复约 27s，实测）。取 token 只用 `GET /tenants/{id}`，无独立 `/token` 端点。

### 阶段 D — 大规模建租户前（必做）

- [ ] 11. 一次性建满一台或压测前，开启 SQS 削峰，避免 AWS Systems Manager 单实例并发墙（实测约 40 并发就开始 TimedOut）。
  ```yaml
  # config.yml
  scaler:
    lifecycle_queue_enabled: true
    create_via_queue: true
    lifecycle_consumer_concurrency: 10 # 单台 metal 设 5-10，不是默认 50
  ```

---

## 五、升级与回滚 Checklist

遵循"改部署代码 → 重建，绝不热改运行中 VM"（安全基石，与 AWS Lambda team 同款实践）。

- [ ] 1. 拉最新代码，合并新增的配置 key。
  ```bash
  git pull
  git diff HEAD@{1} HEAD -- config.yml.example    # 把新增 key 合进你的 config.yml
  ```
- [ ] 2. 重跑 `setup.sh`——不扰动正在运行的 host / tenant（更新 Launch Template 不替换在跑实例）。
  ```bash
  ./setup.sh <region> <profile>
  ```
- [ ] 3. 需要把 rootfs 修复 roll 进现有 host：就地推新 rootfs（现有租户下次 `reset` 时切换）。
  ```bash
  source .env.deploy
  curl -s -X POST "${API_URL}hosts/refresh-rootfs" -H "x-api-key: ${API_KEY}" | jq .
  ```
- [ ] 4. 灰度烤新镜像用 `SKIP_MANIFEST=1`（发布版本化镜像但不动活指针 `manifest.json`），在测试节点验证通过后再更新 `manifest.json` 滚动重建；出问题把 `manifest.json` 指回旧版本即回滚。

---

## 六、常见问题与排错（Troubleshooting）

下列条目按「错误现象 → 根本原因 → 修复步骤」组织，均来自部署代码的真实失败分支与实测踩坑。完整 13 条见「部署解决方案 — 问题排查」（第 5 章），这里给部署新手最常撞的高频项。

### 问题 1：host 起来即被反复替换（非 metal / 无 KVM 机型）

- **错误现象（Symptom）**：ASG 反复 terminate + launch，lifecycle hook 结果 `ABANDON`；`/var/log/cloud-init-output.log` 停在 `chmod: cannot access '/dev/kvm'`。
- **根本原因（Root Cause）**：`init-host.sh` 在 `set -e` 下 `chmod 666 /dev/kvm`；非裸金属 Graviton 机型没有 `/dev/kvm`，`chmod` 失败触发 EXIT trap 返回 `ABANDON`。
- **修复（Resolution）**：
  ```bash
  grep -E 'instance_type|arch' config.yml    # 确认是带 KVM 的 metal 机型
  ls -l /dev/kvm                             # 登上 host 验证设备存在（缺失即根因）
  # 把 host.instance_type 设为 r8g.metal-24xl、host.arch=arm64，重部署
  ```

### 问题 2：网络子网 / CIDR 配置导致 synth 失败或 host 落错网段

- **错误现象（Symptom）**：`imported` 模式下 `cdk synth` 抛 `ValueError`（缺 subnet id）；或 host 起在公网、跨租户超网与 VPC CIDR 冲突。
- **根本原因（Root Cause）**：`network.mode` 三档语义不同——`default_vpc`（host 裸公网，仅早期 dev）、`self_managed`（自建 /20，3 公 + 3 私 + 3 NAT）、`imported`（必须给恰好 3 公 + 3 私 共 6 个 subnet id，缺一 fail-loud）。microVM 的 `/30` 全部落在 `SUBNET_PREFIX/16` 超网（生产 `172.16`），与 VPC CIDR 不能重叠。
- **修复（Resolution）**：
  ```bash
  # 生产走 self_managed 或 imported；imported 必须补齐 6 个 subnet id
  # config.yml:
  #   network.mode: self_managed
  #   network.self_managed.cidr: "10.20.0.0/20"
  # 确认 vm.subnet_prefix（172.16）不与 VPC CIDR 重叠
  ```

### 问题 3：大模型 API 限流 / Guardrail 失效，聊天全部被拒

- **错误现象（Symptom）**：chat 全部无回复；LiteLLM 日志每条 `ApplyGuardrail 400`，或上游 Bedrock 返回 `ThrottlingException` / `TPS exceeded`。
- **根本原因（Root Cause）**：SSM `/openclaw/bedrock-guardrail-id` 缺失或指向失效/跨账号的 Guardrail id → 每条对话被无效 Guardrail 拒；或上游模型 TPS 配额不足被限流。
- **修复（Resolution）**：
  ```bash
  aws bedrock list-guardrails --region <region>
  aws ssm put-parameter --name /openclaw/bedrock-guardrail-id \
    --value <guardrail-id> --type String --overwrite --region <region>
  # 改完重建 LiteLLM 实例；回归越狱用例确认防护仍在；TPS 不足向 Bedrock 申请提额或降并发
  ```

### 问题 4：批量建租户部分永久卡 `creating`（SSM 单实例并发限流）

- **错误现象（Symptom）**：并发 `POST /tenants` 后一批租户永久停 `creating`，consumer 日志 `SSM send error: ThrottlingException`，host 账本 `used_vcpu`/`vm_count` 虚高（slot 泄漏）。
- **根本原因（Root Cause）**：同步路径下每个 create/start 都对同一台 metal 发独立 `ssm.send_command`，超过 AWS Systems Manager 单实例并发配额（实测约 40 并发起）。
- **修复（Resolution）**：开启阶段 D 的 SQS 削峰队列后重部署；已卡住的从 DLQ redrive（`aws sqs start-message-move-task`），按账本差值回收泄漏 slot。

### 问题 5：`cdk deploy` 卡在 Lambda 打包（Docker 未装/未启动）

- **错误现象（Symptom）**：`Cannot connect to the Docker daemon` / `docker: command not found`，卡在 asset bundling。
- **根本原因（Root Cause）**：API Lambda 走 Docker bundling 在容器内交叉下载 arm64 wheel，部署机没有可用 Docker daemon。
- **修复（Resolution）**：`docker ps` 通过后重跑 `setup.sh`。

### 问题 6：`config.yml` 缺失 / CDK 未 bootstrap，`setup.sh` 一上来就报错

- **错误现象（Symptom）**：`VPC mode not configured` / `FileNotFoundError`；或 `SSM parameter /cdk-bootstrap/hnb659fds/version not found`。
- **根本原因（Root Cause）**：`config.yml` 在 `.gitignore` 只有 example；全新账号/区域没 `cdk bootstrap`。
- **修复（Resolution）**：
  ```bash
  cp config.yml.example config.yml
  cdk bootstrap aws://<account>/<region>
  ./setup.sh <region> <profile>
  ```

### 问题 7：聊天连不上但健康正常（两级路由数据面）

- **错误现象（Symptom）**：控制面 VM Health 绿，但 `/ws/{tenant_id}` 返回 404/503。
- **根本原因（Root Cause）**：数据面任一跳断都连不上；404 = 边缘查不到该租户 Redis 路由条目，503 = Redis 不可达。gateway 无无认证 2xx 端点、`/healthz` 可能返回 404，别据此判 down。
- **修复（Resolution）**：
  ```bash
  redis-cli -h <redis-primary-endpoint> GET route:<tenant_id>   # 空=路由未上报
  journalctl -u host-agent -n 200 | grep -i route               # 查 host-agent 路由上报
  # 有值→查 edge 实例 edge_redis_host + nginx.conf；无值→查 VM 是否过 promote 门
  ```

---

## 七、往哪读更深

| 想深入                         | 去哪                                                 |
| ------------------------------ | ---------------------------------------------------- |
| 架构与五层安全模型             | 第 2、3 章（架构概述 / 架构详情）                    |
| 数据面两级路由逐跳与超时链     | 第 13 章（数据面两级路由）                           |
| 控制面 REST API 逐接口         | 第 6、9 章 + `openapi.yaml` / `swagger.html`         |
| 外部平台集成                   | 第 10 章（注意其顶部的身份方案调整说明）             |
| 镜像构建与样本定制             | 第 15 章（镜像构建入门）                             |
| 完整部署/运维/排查             | 第 5 章（部署解决方案 — 使用 — 问题排查）            |
| 生产私有化 API + 凭据 KMS 加密 | 第 12 章                                             |
| 十万级规模化与生产红线         | 第 14 章                                             |
| 权威数字来源                   | `engineering/02-system-constraints/FACT-BASELINE.md` |
