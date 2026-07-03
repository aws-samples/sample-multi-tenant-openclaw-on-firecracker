# 部署解决方案

在部署此解决方案之前，请查看本指南中的架构和规划部署注意事项。该解决方案在 AWS 上为每个租户提供一台独立内核的 Firecracker microVM，运行带身份、技能与护栏的 OpenClaw AI agent；控制面（AWS Lambda、Amazon DynamoDB 与 Amazon API Gateway）负责注册、生命周期、备份与注销，运行后不向租户 microVM 注入业务数据。本节介绍该解决方案的部署流程与所需步骤。

## 部署流程概述

本节介绍该解决方案的部署入口、配置文件与推荐执行顺序。

该解决方案的部署代码由四部分组成：基于 AWS Cloud Development Kit (AWS CDK) 的基础设施定义（`deploy/app.py` 与 `deploy/stack.py`）、host 与 microVM 生命周期脚本（`deploy/userdata/*.sh`）、黄金镜像构建脚本（`build-rootfs.sh`），以及数据面 WebSocket 中枢 claw-hub（`deploy/hub/`）。

AWS CDK 入口 `deploy/app.py` 读取 context 中的 `region`（默认 `us-east-1`），实例化 `OpenClawOrchestratorStack`，账号取自环境变量 `CDK_DEFAULT_ACCOUNT`。中心配置文件 `config.yml` 集中定义 host 规格、microVM 默认值、Auto Scaling Group (ASG)、Balloon 内存回收、健康检查、Multi-AZ 与 Amazon Cognito 认证。部署命令封装在 `setup.sh` 中。

推荐的部署顺序为：

1. 核对 `config.yml`，重点确认区域（`region`）、实例类型（`instance_type`）与 ASG 容量。
2. 运行 `setup.sh`，完成 AWS CDK 部署并将 host 脚本与 `deploy/hub/` 资产上传到 Amazon Simple Storage Service (Amazon S3) assets 桶。
3. ASG 拉起第一台 host，host 执行 `init-host.sh` 完成自举并向宿主表注册。
4. 通过控制面 API 注册第一个租户，验证端到端链路连通。

## 步骤 1：部署基础设施

本节介绍如何部署该解决方案的控制面与网络基础设施。

整套基础设施定义在 `deploy/stack.py` 的 `OpenClawOrchestratorStack` 类中。部署命令的核心是一行带区域参数的 `cdk deploy`：

```bash
cdk deploy -c region="<region>" --profile "<profile>" --require-approval never
```

部署完成后，从 AWS CloudFormation 堆栈的 Outputs 取回 assets 桶、backup 桶与备份密钥等运行时坐标。host 在运行时通过 `aws cloudformation describe-stacks` 查询这些 Outputs（最多重试 20 次、每次间隔 15 秒），而非通过 user-data 注入，以避免触及 EC2 user-data 的 16 KB 上限。

> **验证**：在 AWS CloudFormation 控制台确认堆栈状态为 `CREATE_COMPLETE`，并在 Outputs 中查到 `AssetsBucket`、`BackupBucket` 与 `BackupCmkKeyId`。assets 桶应已启用全部四个公网封锁开关并强制 HTTPS。

## 步骤 2：构建黄金镜像

本节介绍如何构建该解决方案使用的只读黄金镜像。

`build-rootfs.sh` 仅能在 Linux 上运行，因为它依赖 `debootstrap` 与 `chroot`。在 macOS 上运行时，脚本会明确将操作者指向远程 Amazon Elastic Compute Cloud (Amazon EC2) 构建器脚本 `build-rootfs-on-ec2.sh`。运行前的预检要求依赖项齐备（debootstrap、aws、mkfs.ext4、curl、pigz、e2fsck、resize2fs），`/tmp` 至少 10 GB 可用空间，可用内存至少 2 GB（建议 4 GB 以上）。

一次构建产出三个独立的 ext4 镜像：

- **rootfs（只读黄金镜像）**：通过 debootstrap 拉取 Ubuntu Noble（arm64 走 ports.ubuntu.com，amd64 走 ec2.archive.ubuntu.com），在 chroot 内安装 Node.js 22.x、OpenClaw CLI、uv、auditd 与 GitHub CLI。身份文件与技能在构建时烤进 rootfs。
- **data-template**：数据盘模板，作为 microVM 首次启动时可写数据盘的基线。
- **immutable（只读权威盘）**：包含 7 个身份文件与 `IMMUTABLE_SKILLS` 列出的 31 个技能，全部计算 SHA-256 生成 `golden-image.sha256` 基线。

三个镜像的只读语义不依赖文件系统类型，而由 Firecracker 的 virtio 写屏障（`is_read_only:true`）、guest 内 `mount -o ro` 与 ro-bind 三层叠加保证。

灰度发布通过环境变量 `SKIP_MANIFEST` 控制。烤新版本时设 `SKIP_MANIFEST=1`，脚本会发布版本化镜像但不更新 `manifest.json`，旧镜像继续作为 live 版本提供给新启动的 microVM。后续流程为：在少量测试节点上运行新版本，验证通过后再更新 `manifest.json`，随后滚动重建。

> **验证**：确认 S3 中产出 rootfs、data-template 与 immutable 三个 ext4 镜像。设置 `SKIP_MANIFEST=1` 时，确认 `manifest.json` 未被更新，新 microVM 仍使用旧镜像。

> **Note**
>
> 三个 ext4 镜像在 S3 的完整路径前缀待核，上传目标需在换环境时补充确认（参见源运维手册标注）。

## 步骤 3：注册首个租户验证

本节介绍如何通过控制面 API 注册首个租户以验证端到端链路。

向控制面 API 发送 `POST /tenants` 请求注册一个租户。控制面 API 由 Amazon API Gateway（名为 `openclaw-orchestrator`，stage `v1`）承载，转发到运行在 ARM_64、256 MB、120 秒超时的 AWS Lambda 函数。注册成功后，控制面在 DynamoDB 写入租户记录，调度到某台 host，由该 host 的 `launch-vm.sh` 在 Firecracker `InstanceStart` 之前完成全部冷注入并启动 microVM。

> **验证**：确认 `POST /tenants` 返回（实测约 1.7 秒），租户状态在约 4.0 秒内由 `creating` 经 `running` 变为 `vm_health` up（实测）。随后走实时聊天链路发一条消息确认 agent 回复（端到端首回复实测约 27 秒）：以 Amazon Cognito 登录取 id_token →`POST {CloudFront}/hub/token` 带 `Authorization: Bearer {id_token}` 与 `{"tenant_id":"<id>"}` 换前端 token →`wss {CloudFront}/hub/ws?token=<前端token>` 上发 `{"text":"..."}` →收 `{"type":"reply"}`。聊天消息走 claw-hub WebSocket 中枢与虚拟机内 claw-channel 出站连接，不经任何 gateway HTTP 端点，详见『开发人员指南 — 实时聊天接入』。

## CDK 部署的核心资源

本节介绍 AWS CDK 部署所创建的核心资源及其内置的安全加固。

该解决方案在 DynamoDB 中创建三张主表，均为 `PAY_PER_REQUEST` 计费、`RETAIN` 删除保护：

- **`openclaw-tenants`**：租户表，主键 `id`。包含两个全局二级索引（GSI），均为 `ProjectionType=ALL`。`gsi_owner`（分区键 `owner_id`）按所有人反查节点，已 ACTIVE；`gsi_tenant_user`（分区键 `tenant_user_id`）按外部业务用户反查其节点舰队，支撑 `GET/POST /users/{tenant_user_id}/*` 三个端点。`gsi_tenant_user` 受 `scaler.add_gsi_tenant_user` 控制（默认 false）、默认不建；由于 DynamoDB 一次 update 只能新增一个 GSI，须在 `gsi_owner` 已 ACTIVE 后单独再部署一次才能建出。未建时上述三个端点降级，但不阻塞核心租户 CRUD。
- **`openclaw-hosts`**：宿主表，主键 `instance_id`。
- **`openclaw-groups`**：技能分组表，主键 `name`，用于 per-tenant 与 per-group 技能分发。

除主表外，该解决方案另创建两张辅助表：

- **审计表 `openclaw-audit-log-<8 位 hex>`**：名带 per-deploy 后缀（STACK_ID UUID 首段）以避免 destroy 后重建与 RETAIN 残留表撞名；TTL 字段 `expires_ttl`，默认保留 90 天。
- **异步批量 job 表 `openclaw-batch-jobs`**：主键 `job_id`，TTL 字段 `expires_ttl`（完成的 job 行 7 天自动清理），`PAY_PER_REQUEST` 配合 `DESTROY`。大批量（超过 100 节点）或 `async:true` 的 `POST /batch/tenants` 记入此表，由 API Lambda 自调用的 worker 分批执行并增量刷新进度，客户端通过 `GET /batch/jobs/{job_id}` 轮询。

### 时间点恢复

tenants、hosts、groups 与审计四张 `RETAIN` 表均开启时间点恢复（Point-in-Time Recovery，PITR），DynamoDB 维持 35 天连续备份，可恢复到恢复期内任意一秒。该能力由 config 的 `dynamodb.point_in_time_recovery`（默认 true）与 `recovery_period_in_days`（默认 35，上限）控制。瞬态的 `openclaw-batch-jobs`（`DESTROY` 配 TTL）不开启 PITR。

> **Note**
>
> PITR 保护的是控制面元数据表本身。租户业务数据另有备份 Lambda 投递到 Amazon S3 WORM 桶兜底。

### 内置安全加固

以下安全加固写死在部署代码中，部署即生效，不受 `config.yml` 开关裁剪：

- **Amazon S3 assets 桶全封公网并强制 HTTPS**：assets 桶（承载租户数据盘、技能、备份与镜像）设 `block_public_access=BLOCK_ALL`（四个公网封锁开关全开）与 `enforce_ssl=True`（AWS CDK 自动追加 `aws:SecureTransport=false` 的 Deny 到桶策略）。Amazon CloudFront 通过源访问控制（Origin Access Control，OAC）签名访问，不破坏链路。
- **VPC Flow Logs**：默认开启（`flow_logs.enabled` 缺省 true），投递到 Amazon CloudWatch 日志组 `/openclaw/vpc/flow-logs`，`traffic_type=ALL`，用于检测跨租户东西向异常、验证 iptables 隔离是否生效。保留期默认 90 天。
- **AWS WAF baseline 两条规则不可被 config 裁剪**：无论 `config.yml` 的 `waf.managed_rules` 如何配置，代码侧总会将 `AWSManagedRulesSQLiRuleSet` 与 `AWSManagedRulesAmazonIpReputationList` 并入规则集（`dict.fromkeys` 去重保序）。当前 config 另配 `AWSManagedRulesCommonRuleSet` 与 `AWSManagedRulesKnownBadInputsRuleSet`，叠加后共 4 条。

> **Note**
>
> 审计表的客户托管密钥（AWS Key Management Service (AWS KMS) CMK）为待办项，当前仍使用 AWS-owned key。CIS 2.2.2 建议客户托管 CMK，但现存审计表为 `RETAIN`，在线切换加密会强制 replace 并丢失审计数据，因此仅在全新账号首次部署时才加。运维换新环境做容灾规划时，应按「审计表目前不是 CMK」对待。

### 控制面 API Lambda 规格

控制面 API Lambda 运行在 ARM_64 架构、256 MB 内存、120 秒超时，所有配置经环境变量注入。Amazon API Gateway 名为 `openclaw-orchestrator`，stage 为 `v1`，路由在 `add_resource` 与 `add_method` 块中定义（约 37 个 `add_method`，含同一资源的 GET/POST）。

---

# 使用解决方案

本节介绍该解决方案的日常操作，包括 host 与租户 microVM 的生命周期管理、镜像升级与灰度、监控告警与容灾，以及扩缩容与机型容量。

## host 与租户 microVM 生命周期

本节介绍 host 自举、租户 microVM 启动流程，以及停止、备份、迁移、扩容与克隆等生命周期脚本的职责。

### host 自举

新 host 由 ASG 拉起后，`init-host.sh` 按以下顺序自举：配置 KVM 权限、host 加固、安装工具与 Firecracker、挂载数据卷、下载镜像（rootfs 与 vmlinux）、从 S3 同步 shared skills、部署生命周期脚本，最后向 `openclaw-hosts` 表注册。

EC2 user-data 有 16 KB 硬上限，而 `init-host.sh` 注入后约 23 KB。AWS CDK 将 `base64(gzip(init-host.sh))` 嵌入一个纯 ASCII 小 bootstrap，该 bootstrap 把脚本解码到 `/tmp/init-host.sh` 并执行。排查自举失败时，真正在运行的是 `/tmp/init-host.sh` 而非 user-data 原文；`/var/log/cloud-init-output.log` 记录 bootstrap 解码与 init-host 执行日志。

host 自举的几个要点：

- **Firecracker 版本钉死 v1.15.1**（可经环境变量 `FC_VERSION` 覆盖），因为 latest 可能缺少 CI 验证过的 guest 内核。
- **shared skills 每 5 分钟 cron 同步**：`*/5 * * * * root aws s3 sync s3://<bucket>/skills/ /data/shared-skills/`。
- **per-host SSH 密钥**：每台 host 生成一次 ed25519 密钥对，私钥留在 `/etc/openclaw/host_vm_key`，公钥注入每个 microVM 数据盘的 `.ssh/authorized_keys`。每个 microVM 只信任自己宿主的这把密钥。
- **生命周期 hook 保护**：`init-host.sh` 绑定 EXIT trap，成功返回 CONTINUE、失败返回 ABANDON，防止破损 host 挂住 ASG；DDB 注册重试 10 次仍失败则 ABANDON 退出。

### 启动租户 microVM

`launch-vm.sh` 接收 9 个参数（方括号为默认值）：`tenant_id`、`vm_num`、`vcpu[2]`、`mem_mb[4096]`、`config_template`、`restore_backup_key`、`scoped_skills`、`litellm_vkey`、`channel_secret`。其中第 8 个 `litellm_vkey`（per-tenant 计费密钥）与第 9 个 `channel_secret`（per-tenant hub HMAC 密钥）由 API Lambda 在注册时铸出并传入，空值时回退（vkey 回退共享 key，channel_secret 自生成）。

> **Note**
>
> `launch-vm.sh` 的脚本默认 vCPU 2 / 内存 4096 MB 与 `config.yml` 的 microVM 默认值（1 vCPU / 2048 MB）不一致，实际以调用方（API Lambda）传入的参数为准；容量规划按每 microVM 2 GB 计。

该解决方案的所有注入都在 Firecracker `InstanceStart` 之前完成，这是「零运行时操作」的实现位置。启动流程为：

1. 挂载 data.ext4，注入 shared skills，生成 gateway token，注入 SSH 公钥。
2. 通过 jq 修改 `openclaw.json`：写入随机 `NEW_TOKEN` 作为 `gateway.auth.token`、写入 claw-channel HMAC secret、设 `claw-channel.enabled=true` 并将 hubUrl/wsUrl 指向宿主 IP 的 8790 端口；同一 jq 块删除 chatCompletions 与 dangerouslyDisableDeviceAuth、将 allowedOrigins 收窄到单个 CloudFront origin；若 API 铸出了 per-tenant vkey，再写入 `.models.providers.litellm.apiKey`。
3. 挂载 `/dev/vdd` immutable 只读盘，启动 Firecracker。

`InstanceStart` 之后，脚本关闭严格模式，仅执行 nginx reload 与 ssh-keygen 清理，不再向运行中的 microVM 推送数据。

每个租户 microVM 使用四块虚拟磁盘：`vda` rootfs（只读）、`vdb` overlay（读写 copy-on-write）、`vdc` data（读写持久）、`vdd` immutable（只读权威盘）。三层栈为 rootfs 只读底盘 + overlay 每 microVM 稀疏可写层 + data 可写持久盘，rootfs 与 immutable 均 `is_read_only:true`。

该解决方案为每个租户 microVM 叠加三层防火墙隔离，规则均以 `-I` 插入 FORWARD/INPUT 链顶部、先于 ACCEPT，按 tap 接口逐 microVM 各插一份：

- DROP guest 到 IMDS（169.254.169.254 与 IMDSv6 169.254.169.253），防止窃取宿主凭据。
- DROP guest 到整个租户超网（SUBNET_PREFIX/16），防止东西向 microVM 互联。
- INPUT DROP guest 到 host 的 8899/9090/22 端口，防止访问管理面。

加固后目标态为跨租户 100% 丢包（加固代码已落地、静态规则核对一致；漏洞态实测为 0% 丢包、跨租户 RTT 0.187 毫秒。带时间戳的新鲜 裸金属复测待核）。

### 停止、备份、迁移、扩容与克隆

该解决方案提供一组生命周期脚本，各自职责如下：

- **`stop-vm.sh`** 分四步停止：发送 Ctrl-Alt-Del 优雅关机、等待 2 秒、先 SIGTERM 再 sleep 后 SIGKILL、清理网络与 nginx。脚本注释强调不要 `pkill firecracker`，因为 `InstanceStart` 成功后 microVM 正常运行，后续 nginx race 不应反向清除它，崩溃恢复交由 host-agent 自动恢复处理。
- **`backup-data.sh`** 暂停 microVM、用 pigz 并行压缩 data.ext4、恢复 microVM、上传 S3，key 格式为 `backups/{tenant}/{timestamp}.gz`，`cleanup()` trap 保证失败时也恢复 microVM。
- **`migrate-vm.sh`** 提供 snapshot 与 restore 两种模式：snapshot 模式暂停后创建快照、恢复并上传 snapshot.vm、snapshot.mem、vm.json、data.ext4 与 overlay.ext4 到 S3；restore 模式下载全部磁盘、启动 Firecracker、POST `/snapshot/load` 恢复并自动唤醒。Firecracker 快照只记录磁盘路径，必须同时传输实体文件，否则跨 host restore 会报 `os error 2`。
- **`resize-disk.sh`** 在线扩容：暂停、truncate 增大稀疏 ext4、e2fsck、resize2fs、恢复，不需重启或调整分区。
- **`clone-data.sh`** 同主机克隆：暂停源、用 `cp --sparse=always` 复制 data 与 overlay、恢复源、`e2fsck -fy` 验证。该脚本接收 src_tenant、src_vm_num、dst_tenant、dst_vm_num 四个参数，克隆完成后调用方需再运行 `launch-vm.sh` 启动目标。

> **Important**
>
> 删除租户 microVM 前先确认其无真实数据或已完成备份。`DELETE /tenants/{id}?keep_data=false` 在删除数据盘前会同步备份到 S3，备份失败则返回 502 并中止删除（fail-closed）；仅 `?skip_backup=true` 才跳过备份。删除时还会回收该租户的 LiteLLM vkey 以防孤儿密钥。删除主机前先核清其上挂载了哪些租户，避免误删用于演示的节点。

> **Note**
>
> 多个生命周期脚本在边角场景的健壮性待加固：`resize-disk.sh` 假设无坏块，备份恢复后若前置 e2fsck 报码 4 未再复查可能导致扩容后文件系统损坏；`clone-data.sh` 的 e2fsck 后无返回码检查；`migrate-vm.sh` restore 模式容忍磁盘缺失，跨 host 时若快照引用路径不匹配会在 `/snapshot/load` 阶段失败；`stop-vm.sh` 后临时文件的空间回收机制未在脚本中显式说明。

## 镜像升级与灰度

本节介绍该解决方案的镜像升级纪律与灰度发布机制。

升级 OpenClaw、修改配置或更换身份的正确做法是修改部署代码后重建，而非热改运行中的 microVM。修改租户身份需要重新构建镜像，而非调用运行时 API。具体路径为：修改 `build-rootfs.sh` 或 `launch-vm.sh`，烤制新镜像或调整 launch template，灰度后滚动重建，出问题时回滚 `manifest.json`。手改运行中的 microVM 仅可用于验证假设，验证完毕后必须落回部署代码。

这条纪律是整套隔离设计的基础：身份、技能、凭据与配置走启动前冷注入，运行后不开启 host 到 microVM 的批量热注入通道，少一条活通道就少一个横向移动面。

灰度发布通过 `SKIP_MANIFEST` 实现（参见『部署解决方案 — 步骤 2：构建黄金镜像』）。流程为：以 `SKIP_MANIFEST=1` 烤制新镜像、在少量测试节点验证、验证通过后更新 `manifest.json`、滚动重建。

> **Important**
>
> 滚动重建会逐台用新镜像重建 host 上的 microVM。重建会重新生成每个 microVM 的 gateway token 与 channel secret，并以新镜像启动。执行前确认新镜像已在测试节点验证通过；出问题时将 `manifest.json` 指回旧版本即可回滚。

## 监控告警与容灾

本节介绍该解决方案的控制面定时任务、健康判定、AZ failover、运行时监控与自建监控平台。

### 控制面定时 Lambda

该解决方案的容灾与运维由三个定时 Lambda 函数驱动：

| Lambda       | 频率                        | 职责                                                        |
| ------------ | --------------------------- | ----------------------------------------------------------- |
| health_check | 每 5 分钟                   | 判定 stale、重启 agent、AZ failover、迁移监控               |
| scaler       | 每 3 分钟                   | 空闲 host 回收、TTL 过期租户处理                            |
| backup       | 扫描节拍 `rate(30 minutes)` | 经 AWS Systems Manager RunShellScript 运行 `backup-data.sh` |

backup 的 `backup_cron` 是扫描节拍而非统一备份时间：每次触发只备份到期（距上次超过 `backup_interval_hours`，默认 24 小时）的一批、最多 `backup_batch_limit`（默认 20）个，错峰并限制并发，全量在 `backup_interval_hours` 内滚动覆盖。

### 健康判定与重启

租户超过 120 秒没有健康更新即判定为 stale，宿主 agent 可能已宕机。host-agent 重启设有 600 秒（10 分钟）冷却以防频繁重启。120 秒阈值与 host-agent 每 15 秒刷新一次时间戳配套：连续约 8 个周期未刷新才算 stale。

### AZ failover

当前 `config.yml` 中 `multi_az.enabled=false`、`az_failover.enabled=false`，测试期单 AZ 运行以节省跨 AZ 流量费用。开启后的逻辑为：AZ 内所有 host 连续不健康超过 10 分钟触发 failover，冷却 30 分钟防止重复触发。恢复设有前置门——必须存在备份，否则拒绝迁移并标记 `failover_blocked`。failover 后的 microVM 验证采用三层检测：Firecracker 进程存在、nginx 配置存在、本地 HTTP 探针返回小于 500；成功后记审计日志 `AZ_FAILOVER_TENANT_RECOVERED`。迁移过程监控超时 15 分钟，超时自动回滚到 running；failover 与迁移后还通过公网路径（经 ALB）核验 dashboard 真正可达，不可达则回滚。

### 运行时监控

每个租户 microVM 内部运行一层运行时监控，盯防两类动作：一是 microVM 内向外建立反向连接（典型的反弹 shell），二是关键系统与身份文件被修改。命中即产生告警。监控本身以系统最高权限运行，普通 agent 用户看不到、关不掉、读不到其日志，因而「先关监控再作案」的路径被堵死。这层监控为常驻、开销受控：文件与行为审计约占 11.7 MB、文件完整性监控约占 42 MB 内存（实测）。

### 自建监控平台

监控既定走自建 Amazon EC2 上的 Prometheus、Grafana 与 Wazuh，不依赖 Amazon Managed Service for Prometheus 与 Amazon Managed Grafana。两套监控资产均为可选、按需部署，不随主栈自动启动。AWS CDK 中的托管监控（Amazon Managed Service for Prometheus、Amazon Managed Grafana、Amazon GuardDuty、Amazon Simple Notification Service (Amazon SNS) 通知）均 config-gated 且默认关闭。两套自建监控都部署在专用 EC2（隔离爆炸半径，不跑在 metal host），安全组入站只对 VPC CIDR 或堡垒机安全组开放，绝不对 0.0.0.0/0 开放：

- **Prometheus 与 Grafana**：抓取各 metal host 的 host-agent `:8899/metrics`（microVM 内存、Balloon、磁盘、CPU、health 等 gauge），通过 ec2_sd 自动发现并附带 dashboard。采集链路依赖两个配套条件：host 实例须带 `Project=openclaw` 与 `Role=metal-host` 标签，host 安全组须放行 8899 入站给 VPC CIDR。
- **Wazuh 双 EC2**：EC2-1 为 manager all-in-one（manager、indexer、dashboard），EC2-2 为 agent（wazuh-agent、auditd 与实时文件完整性监控）。两台 EC2 均无公网 IP，dashboard 经堡垒机 SSH 隧道访问。

为避免告警只落在 manager 本机，部署脚本给 manager 配置最小权限实例角色，将告警实时镜像到独立的 CloudWatch 日志组与 Amazon SNS 通知主题。可选再开启独立的 Amazon OpenSearch Service 域（默认关闭，持续计费），将告警再落一份到独立信任域。microVM 内部的运行时告警可汇聚到这台 manager 统一查看。

## 扩缩容与机型容量

本节介绍该解决方案的 ASG 弹性伸缩、机型选择、容量配置与大规模扩容的并发控制。

### ASG 弹性伸缩与机型

宿主机扩缩容交由 Amazon EC2 Auto Scaling 托管。该解决方案用 Auto Scaling Group 与启动模板管理整队宿主机的拉起与滚动重建。host 由 ASG 拉起、自举注册，空闲超时由 scaler 经两轮确认后受控回收，整池容量按 `config.yml` 调整，当前 min 1 / max 3 台。

生产机型使用 metal 系列（Graviton4 ARM64，原生 KVM 运行 Firecracker，而非 x86 嵌套虚拟化）。`config.yml` 以 `arch: arm64` 与 `instance_type` 配置机型，容量推导由部署代码按 size token 查表并结合内存比计算。metal 机型走原生 KVM、不开启嵌套虚拟化；生产底座固定为 metal 原生 KVM。

若 config 的 `host.instance_types` 给出多个等容量机型（不少于 2 个），ASG 走 `MixedInstancesPolicy` 跨机型起 host，提升可用性与 Spot 韧性。硬约束是池内所有机型必须等容量（同 vCPU 与内存），否则 synth 直接报错。

### 容量配置

容量按 `config.yml` 调整：

- 每个租户 microVM 默认 1 vCPU / 2048 MB（2 GB），由 `vm.default_vcpu` 与 `vm.default_mem_mb` 控制。
- 超卖比由 `cpu_overcommit_ratio` 与 `mem_overcommit_ratio` 控制。可分配容量按 `allocatable_vcpu = total_vcpu × CPU_OVERCOMMIT_RATIO` 计，API 侧据 host 剩余容量调度。
- 每台 host 配 600 GB gp3 加密 EBS 数据盘（`/dev/sdf` 挂 `/data`），承载所有 microVM 的稀疏盘与 rootfs overlay。单 microVM 稀疏盘实占约 187 MB 至 1.3 GB。
- microVM 寻址上限到 vm_num 480（每 microVM 一个 /30 点对点链路）。
- 分配 vm_num 使用 DynamoDB ConditionExpression 乐观锁，CAS 重试 8 次。

### 并发瓶颈与削峰队列

大规模建租户的真正瓶颈是 AWS Systems Manager 并发，而非容量。同步路径下每个 create 或 start 都发一条独立 `ssm.send_command` 到目标 metal，一次性建满一台会瞬间超过 Systems Manager 单实例并发配额，部分请求永久卡在 `creating`。

解法是开启 `scaler.lifecycle_queue_enabled=true`，将 create/start/stop/delete 先写入 Amazon Simple Queue Service (Amazon SQS) 队列削峰，消费端按受控并发逐批执行（单台 metal 设约 5 至 10，对应 Systems Manager 单实例可承受速率，不用默认 50）。大规模扩容前须先开启此削峰队列，不要用客户端 N 并发直接 POST。

### 实测性能与容量数字

| 指标                   | 数值                                                       | 来源       |
| ---------------------- | ---------------------------------------------------------- | ---------- |
| microVM 纯启动         | 1.74 秒（p50）                                             | metal 实测 |
| launch 到 gateway 可用 | 6.48 秒（Firecracker 1.7 秒 + gateway 冷启 4.7 秒）        | metal 实测 |
| 空白 microVM RSS       | 约 609 MB                                                  | metal 实测 |
| 整机爬坡均值           | 669 MB                                                     | metal 实测 |
| 单台全 healthy 承载    | 187 节点（磁盘瓶颈，非内存上限）                           | metal 实测 |
| 每台 host 稳态承载     | 380 租户（r8g.metal-24xl，760 GB ÷ 2 GB）（推算）          | 容量推算   |
| 月度成本               | 约 8.36 USD/租户/月（80% Savings Plan + 20% Spot）（推算） | 成本推算   |

> **Note**
>
> 容量与扩缩容仍有若干不确定项，做容量规划时按尚未定论对待：187 节点的磁盘瓶颈是 IOPS 还是容量不明；ASG 自动扩容触发阈值待确认，当前疑似只有手动 SetDesiredCapacity；ALB 规则数硬墙与容量的关系待确认；成本约 8.36 USD/租户/月的详细分摊模型未给出。成本估算不含第三方服务与数据传输费用。

---

# 问题排查

本节介绍该解决方案的常见问题及解决方案、日志查看入口与支持渠道。

## 已知问题解决方案

下列「症状、定位、处置」基于代码中真实的错误分支与自恢复逻辑。所有 host 操作走 SSH；生产运营场景才用 AWS Systems Manager。

### 症状 A：某租户健康状态翻为 stale，聊天连接断开

判定：租户超过 120 秒（`STALE_SECONDS=120`）没有健康更新即判定为 stale。

定位：SSH 到该 host，检查 Firecracker 进程是否存在，查看该 microVM 的 `fc.log`（`--log-path ${VM_DIR}/fc.log`，VM_DIR 为 `/data/firecracker-vms/<tenant>`）。

处置：host-agent 提供两级自恢复，多数情况无需人工介入。`vm.json` 存在但进程消失时，`_recover_vm` 重新启动；Firecracker 存活但 guest 网络连续不可达到阈值时，`_force_relaunch_vm` 走 stop 加 launch 重建。若整台 host 上所有租户同时 stale，多半是 host-agent 本身宕机，health_check Lambda 经 Systems Manager 下发 `systemctl restart host-agent` 拉起，设有 600 秒冷却（`RESTART_COOLDOWN_SECONDS=600`）。冷却期内不要手动反复重启，按 `agent_restart_at` 等待过冷却。

聊天专项排查（健康正常但用户聊天连不上）：聊天链路与虚拟机内的 gateway control UI 是两条正交路径，聊天走 claw-hub WebSocket 中枢加虚拟机内 claw-channel 出站连接，与 control UI 无关。按此顺序排查——① claw-hub 服务（`claw-hub.service`）是否健康、`/hub/healthz` 是否返回正常、宿主 nginx `/hub/` 反向代理是否生效；② 虚拟机内 claw-channel 的出站连接日志（`claw-channel.log`），看是否出现 `token-fail`、`ws-unexpected-response`、`ws-close` 等决策；③ Amazon DynamoDB 租户记录中 `channel_secret` 是否已镜像就绪（hub 据此验签虚拟机的通道注册）。

### 症状 B：启动 microVM 报错、microVM 起不来

定位：查看 `launch-vm.sh` 日志（`log()` 打到 stdout，前缀 `[oc:launch]`；ERR trap 打印 `FAIL line=<行号>`）。

处置：几个直接 `exit 1` 的硬失败分支——备份恢复时 `e2fsck` 返回码 4 或 16（文件系统损坏未修）判 `FATAL: backup filesystem check failed` 拒绝启动，返回码 0/1/2/8 才接受，可换更早的备份 key 重试；`tuntap add` 报 EBUSY 时强制清理 tap 后重试（不致命）；Firecracker `InstanceStart` 返回非空错误时 `exit 1` 并打印 `ERROR: <RESULT>`。

### 症状 C：跨 host 迁移或 failover 后 microVM 起不来，fc.log 出现 `os error 2`

原因：Firecracker 快照只记录磁盘路径，跨 host restore 必须把 snapshot.vm、snapshot.mem、vm.json、data.ext4 与 overlay.ext4 实体文件一起传过去。

处置：确认 S3 中这几个实体文件齐全后再 restore。`migrate-vm.sh` restore 模式对缺失磁盘以 `2>/dev/null || true` 容忍，缺文件不在下载阶段报错，而在 `/snapshot/load` 阶段失败。

### 症状 D：failover 触发却被拒绝，租户标记 failover_blocked

原因：failover 恢复设有前置门——必须存在备份，否则拒绝迁移。

处置：先给该租户运行一次备份（参见『使用解决方案 — host 与租户 microVM 生命周期』中的备份脚本），再让 failover 重试。

### 症状 E：空闲 host 没被回收，或回收过于激进

逻辑：scaler 两轮确认，空闲超过 10 分钟先标记 idle，下一轮仍空闲且 ASG 允许才 terminate，受 ASG MinSize 保护，到 min 就跳过。到达 MinSize 不回收是预期行为，不是缺陷。

## 日志查看入口

| 看什么                                         | 在哪                                                          | 怎么取                                            |
| ---------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------- |
| host-agent 探针与自恢复                        | host 上 systemd 服务 `host-agent`                             | SSH 后 `journalctl -u host-agent -n 200`          |
| 单个 microVM 的 Firecracker 日志               | `${VM_DIR}/fc.log`（`/data/firecracker-vms/<tenant>/fc.log`） | SSH 后读取该文件                                  |
| nginx 反代                                     | host 上 `nginx` 服务                                          | `journalctl -u nginx` 与 `/var/log/nginx/*`       |
| 控制面 Lambda（注册、调度、健康、备份）        | Amazon CloudWatch Logs，按各 Lambda 函数名分组                | 控制台或 `aws logs tail`                          |
| guest 内运行时告警（反弹 shell、敏感文件改动） | microVM 内分析器，root:root 0700，agent 不可见                | 经 host-agent 或监控管道取，不在 guest 用户态可读 |

## 联系支持

如遇本节未覆盖的问题，请通过部署方的技术支持渠道联系支持团队。提交支持请求时，建议附上以下诊断信息以加快定位：

- 受影响租户的 `tenant_id` 与所在 host 的 `instance_id`。
- 相关时间窗内的 host-agent 日志（`journalctl -u host-agent`）与对应 microVM 的 `fc.log`。
- 控制面相关 Lambda 函数在 CloudWatch Logs 中的日志片段。
- 故障的复现步骤、预期行为与实际行为，以及任何已尝试的处置动作。

> **Note**
>
> 提交诊断信息前请脱敏，移除凭据（gateway token、API key、JWT 等）、真实账号 ID 与真实域名。
