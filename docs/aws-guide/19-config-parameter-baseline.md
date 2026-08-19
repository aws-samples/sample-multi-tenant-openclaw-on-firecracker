# 19 配置参数基线对照表(控制面 / 数据面)

> 每个参数四个事实 + 一个推荐值:**意义 · 当前默认 · 曾经默认 · 生产实际 · 10 万租户推荐值**。
>
> 本章不重复 [14 十万级规模化](14-scale-100k.md) 的阶段化红线,也不重复
> [08 参考](08-reference.md) 的键清单。这里只回答一个问题:**每个参数该填什么值,依据是什么。**

## 怎么读这张表

**规模锚点(定版,`SPEC/11-ENGINE-TRANSFORM/01-REQUIREMENTS.md § 2`)**:

| 项 | 值 |
| --- | --- |
| microVM 总量 | 10 万 |
| 每宿主机 microVM | 300–400 |
| 宿主机 EC2 | **约 300 台**(r8g.metal) |
| 每 VM WebSocket | 2–3 |
| 全系统并发连接 | 约 30 万 |
| 边缘网关 | 3 台 c6in.xlarge(ASG 跨 3 AZ) |

**推荐值来源标注**(`engineering/02-system-constraints/FACT-BASELINE.md` 是硬门:没有一手来源的数字
不许出现,只能标「待实测」):

| 标注 | 含义 |
| --- | --- |
| `SPEC` | 上表定版规模约束 |
| `实测` | 真机/压测一手证据 |
| `AWS` | 官方文档或 service-quotas 实查 |
| `ADR` | 决策文档;**括注 Proposed / Accepted** |
| `推算` | 算术推导,写出算式 |
| `待实测` | **无一手依据。不填看似合理的数** |

**"曾经默认值"** 一律带 commit sha,可 `git show <sha> -- config.yml.example` 复核。

---

## 19.1 先看这三处联动(填错任何一个,10 万规模都到不了)

### ① `vm.default_mem_mb` 决定 host 台数翻倍

380/台**不是**默认配置能达到的值。仓库默认 `default_mem_mb: 4096`,而一手实测是
**187 个全健康节点/台且受磁盘瓶颈约束**(见 14 章 § 14.1)。380/台是 **2048 MB/VM** 的目标容量档。

| `vm.default_mem_mb` | 每台可承载 | 10 万所需台数 | 依据 |
| --- | --- | --- | --- |
| 4096(**当前默认**) | 187(实测,磁盘瓶颈) | **535 台** | `推算` 100000 ÷ 187 |
| 2048(目标档) | 380(容量目标,非硬上限) | **264 台** | `推算` 100000 ÷ 380 |
| — | 300–400 | **约 300 台** | `SPEC` 定版 |

→ **10 万规模必须显式设 `2048`**,否则台数从 ~300 变 535,成本与 ASG 上限全部错算。

### ② `asg.max_capacity` 默认值和生产样例都装不下 10 万

| 来源 | 值 | 10 万够不够 |
| --- | --- | --- |
| `config.yml.example` 当前默认 | `8` | ❌ 差 37 倍 |
| `samples/config-sg-prod.yaml` 生产样例 | `100` | ❌ 差 3 倍 |
| `SPEC` 定版所需 | 约 300 台 | — |

→ 推荐 `320`(`推算`:300 + 约 7% 滚动/故障冗余)。`asg.min_capacity` 首次部署必须 `0`
(镜像未就绪时 host 会 ABANDON churn,见 14 章 § 14.2),镜像就绪后再抬到稳态台数。

### ③ 内存超卖不能靠 balloon 兜底

`balloon.enabled` 默认 `true`,但 `ADR-heterogeneous-memory-aware-scheduling` § 2.1 的裁决写明:
**实测 balloon 未接线(`amount_mib: 0`),回收没生效**。因此"配 balloon 时 mem 可超卖"这个前提
当前不成立 —— 这正是 `#430` 把 `mem_overcommit_ratio` 降到 `1.0` 的原因。

---

## 19.2 数据面 · host 容量与超卖

| 参数 | 意义 | 当前默认 | 曾经默认(commit) | 生产实际 | 10 万推荐值 | 推荐依据 |
| --- | --- | --- | --- | --- | --- | --- |
| `host.instance_type` | host 机型;列表首项同时是 LT 主类型与 scaler 容量估算基准 | `r8g.metal-24xl` | 同 | `r8g.metal-24xl` | `r8g.metal-24xl` | `SPEC` r8g.metal;`AWS` 768 GiB/96 vCPU |
| `host.instance_types` | 混池优先序;须与 `scheduling.family_order` 同序 | `[r8g.metal-24xl, r7g.metal, m8g.metal-24xl, m7g.metal]` | 无此键(单机型) | 未设(用单一机型) | 同默认 | `ADR-heterogeneous`(**Proposed**) |
| `host.cpu_overcommit_ratio` | vCPU 超卖比 | **`4.0`** | `2.0` → `6.0` → `8.0`(`38a4f262` #215 · 07-12)→ `6.0`(`ae758f86` #198R15 · 07-14 **误翻**)→ `4.0`(`70eff85f` #430 · 08-12) | ⚠️ **`6.0`**(drift) | `4.0` | `#430`:R 系 8.0÷2.0 GB/vCPU = 4.0 恰好匹配,95×4 = 380 槽 |
| `host.mem_overcommit_ratio` | 内存超卖比(硬限) | **`1.0`** | `1.5` → `2.0`(#215)→ `1.5`(`ae758f86` 误翻)→ `1.0`(`70eff85f` #430) | ⚠️ **`2.0`**(危险 drift) | `1.0` | `实测` #352:1:4 CPU 超卖下 m8g.metal-24xl 塞 374 VM,`free -g` 用到 376/376G **剩 0G** |
| `host.overcommit_by_family` | per-family 超卖比覆盖 | `{}` | 曾配 mem 补偿系数 1.022/1.023/1.025/1.028 | 未设 | `{}` | `#430`:改按标称规格注册容量后系数全部消失,不再需要魔数 |
| `host.reserved_vcpu` | host 自留 vCPU | `1` | 同 | `2` | `2` | 生产样例(host-agent + 系统留 2 核) |
| `host.reserved_mem_mb` | host 自留内存 | `2048` | 同 | `2048` | `2048` | 一致,无争议 |
| `host.data_volume_gb` | `/data` 承载全部 microVM 稀疏盘 | `900` | `200` → `600`(`8c22c9ea` · 06-30)→ `900`(`d63512df` #37 · 07-04) | `900` | `900` | `实测` #37:重载单 VM 实占可达 1.3G,380×1.3G ≈ 494G;900G 给重载余量 |
| `host.data_volume_iops` | data 卷 gp3 IOPS | `8000` | 隐式基线 `3000`(未下发)→ `8000`(`5aadfb7b` #424 · 08-18) | `8000` | `8000` | `实测` #424:apse1 写 IOPS 峰值 276(14 租户)外推 380 VM ≈ 7500 |
| `host.data_volume_throughput` | data 卷 gp3 吞吐 MiB/s | `500` | 隐式基线 `125` → `500`(`5aadfb7b` #424) | `500` | `500` | `实测` #424:成本边际拐点,过 500 后每 $1 仅省 0.6 min |
| `host.root_volume_gb` | host OS 盘 | `20` | 同 | `20` | `20` | 一致 |
| `host.root_volume_iops/throughput` | root 卷 gp3 性能 | 不下发(走基线 3000/125) | 同 | 不下发 | 不下发 | `#424`:OS 盘基线足够,零额外成本 |
| `host.keep_data_volume` | 实例删除后是否保留 EBS 卷 | `true` | 同 | `true` | `true` | 防误删丢租户数据;留下的孤儿卷不会被复用,须另按成本口径清理 |

## 19.3 数据面 · microVM 与调度

| 参数 | 意义 | 当前默认 | 曾经默认(commit) | 生产实际 | 10 万推荐值 | 推荐依据 |
| --- | --- | --- | --- | --- | --- | --- |
| `vm.default_mem_mb` | 每 VM 声明内存 | `4096` | 同 | `4096` | **`2048`** | `SPEC` 300–400/台只在 2 GB/VM 成立;`实测` 4096 下每台仅 187(见 § 19.1 ①) |
| `vm.default_vcpu` | 每 VM vCPU | `2` | 同 | `2` | `2` | 与 `cpu_overcommit_ratio=4.0` 联动得 380 槽 |
| `vm.rootfs_overlay_mb` | 每 VM 可写层上限(sparse) | `8192` | 同 | 未设 | `8192` | sparse 不预占,声明上限不等于实占 |
| `vm.data_disk_mb` | 每 VM 数据盘上限(sparse) | `8192` | 同 | 未设 | `8192` | 同上;380×8G 远超 900G 靠 sparse 不全占 |
| `vm.host_launch_slots` | 单 host 同时冷启 microVM 数上限(flock 信号量) | `30` | 无此键(`3493f152` #331 · 07-20 新增) | 未设 | `30` | `实测` #331 真机验证值;防批量 recover 二次洪峰压垮 host |
| `vm.gateway_port_base` | legacy gateway 端口基址 | `18789` | 同 | 未设 | `18789` | 不在任何 SG 入站规则内 |
| `balloon.enabled` | balloon 内存回收 | `true` | 同 | `true` | `true`(但**不可依赖**) | `ADR-heterogeneous` § 2.1 裁决:实测未接线(`amount_mib: 0`),回收未生效 |
| `balloon.max_inflate_ratio` | 最多回收 VM 声明内存比例 | `0.4` | 同 | 未设 | `0.4` | 未接线前该值无实际效果 |
| `scheduling.affinity_enabled` | 四级机型亲和排序 | `true` | 无此键(`70eff85f` #430 新增) | 未设 | `true` | `ADR-heterogeneous`(**Proposed**);false 回落 free_vcpu 排序 |
| `scheduling.family_order` | 机型优先序 | `["r8g","r7g","m8g","m7g"]` | 无此键(#430 新增) | 未设 | 同默认 | R 系先填满、M 系留安全余量;须与 `host.instance_types` 同序 |
| `scheduling.mem_safety_floor_ratio` | 物理内存安全水位;实测 MemAvailable 低于此比例不接新租户 | `0.10` | 无此键(`70eff85f` #430 新增) | 未设 | `0.10` | `ADR-heterogeneous` 验收口径:满载压测每台剩余内存 ≥ 物理 10%、OOM 恒为 0 |
| `scheduling.mem_check_ttl_sec` | 物理内存信号新鲜度 TTL | `300` | 无此键(#430 新增) | 未设 | `300` | 超期 fail-open,不用过期读数封锁 host |
| `quotas.enabled` | per-tenant 资源配额 | `false` | 同 | `false` | `true` | 10 万多租户需防单租户吃满;**上限值待业务定义**,不是技术推导 |
| `quotas.max_vcpu_per_tenant` 等 | 单租户上限(0=不限) | `0` | 同 | `0` | `待业务定义` | 无技术依据可推 |

## 19.4 数据面 · 边缘与网络

| 参数 | 意义 | 当前默认 | 曾经默认(commit) | 生产实际 | 10 万推荐值 | 推荐依据 |
| --- | --- | --- | --- | --- | --- | --- |
| `edge.enabled` | OpenResty 两级路由数据面 | `false` | 同 | `true` | `true` | 数据面唯一入口;须与 `redis.enabled` 成对开 |
| `edge.min_capacity` | edge ASG 最小台数 | `3` | 无此键(`0253dd55` #187P2b · 07-07 新增) | `3` | `3` | `SPEC` 3 台跨 3 AZ;14 章 R2:N-1 容灾 |
| `edge.max_capacity` | edge ASG 最大台数 | `6` | 无此键(#187P2b 新增) | `6` | `6` | 14 章 R2 弹性规则:`RequestCountPerTarget` p95 > 2000 rps 持续 3 min → desired +1 |
| `edge.instance_type` | edge 机型 | `c6in.xlarge` | 同 | `c6in.4xlarge`(升配) | `c6in.xlarge` 起步,按 30 万并发实测校准 | `SPEC` 定版 c6in.xlarge;**升到 4xlarge 的依据未在仓库留痕** → 按实测定 |
| `edge.health_check_grace_period_seconds` | 冷启宽限 | `300` | 无此键(#187P2b) | 未设 | `300` | 14 章 R2:apt openresty + nginx + Lua warmup + Redis 探活 30s 重试 |
| `edge.dnat_port_high` | host DNAT 端口段上界(下界固定 10000) | `15000` | `10400`(`6c3a3f6f` #187P7 · 07-08)→ `15000`(`9d649928` #205 · 07-11) | `15000` | `15000` | `推算` 5001 槽 > 单 host 内存维度理论 576 VM(768G÷2G×超卖)+ stopped 保留端口 |
| `redis.enabled` | 路由表 Redis/Valkey | `true` | 同 | `true` | `true` | 数据面查 `route:{tenant_id}` 的准静态路由 |
| `redis.engine` | 引擎 | `valkey` | `redis` → `valkey`(`558e3488` #271 · 07-15) | `valkey` | `valkey` | 协议线级兼容,`route.lua`/redis-py 无需改 |
| `redis.node_type` | 节点机型(**代码只读此键**) | `cache.r7g.large` | 无此键(`0253dd55` #187P2b 新增) | `cache.r7g.large` | `cache.r7g.large` 起步 | `推算` 10 万条 route × ~200 B ≈ 20 MB,内存维度充裕;**连接数/QPS 待实测**(约 300 host 双写 + 3 edge 高频查) |
| `redis.num_replicas` | replica 数(总节点 = 1 + N) | `2` | 无此键(#187P2b) | `2` | `2` | Multi-AZ automatic_failover 前提;14 章 R3:failover 15–30s |
| `multi_az.enabled` | 跨 AZ HA | `true` | 同 | `true` | `true` | 1.3.0 起生产推荐 |
| `multi_az.az_count` | 最多使用几个 AZ | ⚠️ `2` | `2` → `3`(`38a4f262` #215 · 07-12)→ `2`(`ae758f86` **误翻** · 07-14) | `3` | **`3`** | `SPEC` 3 AZ;`imported` 需 6 个子网齐、`edge.min_capacity=3` 也是"一 AZ 一台" → 默认 `2` 与这些契约不一致 |
| `network.mode` | VPC 形态 | `""`(交互选) | 同 | `imported` | `imported`(客户已有 VPC)或 `self_managed` | 生产禁 `default_vpc`(host 裸公网) |
| `alb.internal` | ALB 公网/内网(**必须显式声明**) | `false` | 无此键(早于 #423 的 config 会 synth raise) | 按形态 | 按形态显式声明 | `#499 D`:缺失即 fail-loud |
| — | `nf_conntrack_max` | 非 config 键(host/edge userdata) | — | `1048576` | `1048576` | 14 章 R1;`NFR-3` 单机 400 VM 无 table full 丢包 |
| — | NAT GW 数 | 非 config 键(`_helpers.py`) | — | `3` | `3`(每 AZ 一个) | 14 章 R6;10 万级把 Bedrock/S3/DDB/KMS 走 VPCE 绕 NAT 处理费 |

## 19.5 控制面 · API 与限流

| 参数 | 意义 | 当前默认 | 曾经默认(commit) | 生产实际 | 10 万推荐值 | 推荐依据 |
| --- | --- | --- | --- | --- | --- | --- |
| `api.throttle_rate_limit` | api-key 维度稳态 req/s | `500` | 注释记载旧默认 `10`(commit 未定位) | 未设(用默认) | `500` | `实测` 旧 10/20 下 300 并发 POST /tenants 有 173/300 撞 429;`ADR-sqs-dispatch` 目标 100 create/s < 500 |
| `api.throttle_burst_limit` | 瞬时 burst(令牌桶) | `1000` | 注释记载旧默认 `20` | 未设 | `1000` | 同上;自助用户走 Cognito 不过此 plan |
| `api.mode` | API Gateway 暴露形态 | 派生自 `private_api_enabled` | 无此键(#212 R1 新增) | `private` | `private` | 机器流量走 VPCE 内网,EDGE 挂 deny-public |
| `waf.enabled` | WAF | `false` | 同 | `true` | `true` | stack.py 强制追加 SQLi + IpReputation 底线 |
| `waf.rate_limit_per_ip` | 每 IP 5 分钟请求上限 | `1000` | 同 | 未设 | `1000` | 与 api 限流正交(per-IP vs per-key) |

## 19.6 控制面 · 生命周期与批量下发

| 参数 | 意义 | 当前默认 | 曾经默认(commit) | 生产实际 | 10 万推荐值 | 推荐依据 |
| --- | --- | --- | --- | --- | --- | --- |
| `asg.min_capacity` | host ASG 最小台数 | `0` | `2` → `1`(`d2b17df1` #210 · 07-12)→ `2`(`ae758f86`)→ `0`(`c063e9b2` #488 · 08-13) | `0`(注:镜像就绪后改 2+) | 首部署 `0`,稳态 `300` | 14 章 § 14.2:min≥1 会让 host 抢在镜像就绪前起 → ABANDON churn;稳态值 = `SPEC` 约 300 台 |
| `asg.max_capacity` | host ASG 最大台数 | ⚠️ `8` | 同 | ⚠️ `100` | **`320`** | `推算` `SPEC` 300 + 约 7% 滚动/故障冗余;默认 8 与生产 100 都装不下 10 万(见 § 19.1 ②) |
| `asg.lifecycle_hook_timeout` | init-host 必须在此超时内完成 | `3600` | `600` → `1200` → `3600`(`c063e9b2` #488 · 08-13) | `3600` | `3600` | `实测` imported VPC + metal 场景 1200s 也不够;`preflight-region.sh` 门是 ≥2700 |
| `asg.use_spot` | Spot 实例 | `false` | 同 | 未设 | `false` | metal + 有状态租户,Spot 中断代价高 |
| `scaler.lifecycle_queue_enabled` | lifecycle FIFO 队列削峰(stop/delete 有序) | `false` | 同 | `true` | `true` | `实测` 同步路径下仅 40 并发 POST /tenants 就有 11 个永久卡 creating |
| `scaler.create_via_queue` | 建租户走 FIFO | `false` | 同 | `false` | `false` | 与 `dispatch.enabled=true` **互斥**(同 true 则 synth raise) |
| `scaler.lifecycle_consumer_concurrency` | consumer 并发上限(限流阀) | ⚠️ `50` | `10`(#215)→ `50`(`ae758f86` · 07-14) | `50` | **`10`** | `实测` `ADR-sqs-dispatch` § 2.1:consumer 10 并发直发 SSM 即撞 ThrottlingException;40 并发 11 TimedOut。根因 `AWS` SSM Agent 单实例 `DefaultCommandWorkersLimit=5` + buffer 5(源码 `constants.go:31-35`)。**默认 50 与自身注释("应设 5-10,不是 50")矛盾** |
| `scaler.interval_minutes` | scaler 巡检间隔 | `3` | 同 | 未设 | `3` | 无争议 |
| `scaler.idle_timeout_minutes` | 空闲判定 | `10` | 同 | 未设 | `10` | 自动缩容当前在代码里硬关(`IDLE_RECLAIM_ENABLED=False`),该值当前无回收效果 |
| `dispatch.enabled` | SQS 标准队列 + 装箱消费 | `false` | 无此键(`f77ecf4e` · 07-05 新增) | `true` | `true` | 10 万规模建租户必走装箱;`ADR-sqs-dispatch` 目标每分钟 6000 租户(100/s) |
| `dispatch.mode` | manifest 载体 | `push` | 无此键 → `push`;`ddb` 载体 `f7250c95` · 07-06 加入 | `ddb` | **`ddb`** | `AWS` PutParameter 默认 3 TPS / higher throughput 10 TPS 是硬瓶颈;`ddb` 让 PutParameter 退出热路径 |
| `dispatch.esm_max_concurrency` | SQS Lambda ESM MaximumConcurrency | `10` | 无此键(`f77ecf4e` 新增即 10) | 未设 | `20` | `ADR-sqs-dispatch` § Q5 明确"设 10-20"(**Proposed**);约 300 host 规模取上界。`AWS` 硬 range 2–1000 |
| `dispatch.batching_window_seconds` | 攒批窗口 | `2` | 无此键(`f77ecf4e`) | 未设 | `2` | `AWS` 标准队列支持 window ≤300s(FIFO 不支持);2s 窗口在 100/s 下攒 200 条 |
| `dispatch.max_batch_size` | 单次 invoke 装箱租户数 | `500` | 无此键(`f77ecf4e`) | 未设 | `500` | `AWS` 标准队列 batch ≤10,000;`ddb` 载体不受 ParamStore 4 KB 限(`push` 载体须切到 100–200/host) |
| `dispatch.dlq_max_receive_count` | DLQ maxReceiveCount | `3` | 无此键 | 未设 | `3` | 一进 DLQ 即告警 `openclaw-dispatch-dlq-not-empty` |
| — | SSM managed nodes 配额 | 非 config 键 | — | — | 约 300 台 < `2400`/region | `AWS` 默认 managed nodes 2400/region,约 300 台有 8 倍余量 |

## 19.7 控制面 · 查询、健康与数据保护

| 参数 | 意义 | 当前默认 | 曾经默认(commit) | 生产实际 | 10 万推荐值 | 推荐依据 |
| --- | --- | --- | --- | --- | --- | --- |
| `scaler.add_gsi_tenant_user` 等 4 个 GSI 门 | 规模化查询索引 | 全 `false` | 同 | `false` | **全 `true`(逐个部署)** | 10 万租户下无 GSI = 全表扫;`AWS` DynamoDB **一次 update 只能加 1 个 GSI**,必须一次开一个、等 ACTIVE 再开下一个 |
| `tenant_query.enabled` | 规模化查询总开关 | `false` | 无此键(`adbb68c9` #388 · 07-27 新增) | `false` | `true`(**四个 GSI 全 ACTIVE 后**) | 顺序门:提前开会查不到索引 |
| `tenant_query.rootfs_backfill_complete` | rootfs 回填完成标记 | `false` | 无此键(#388) | `false` | `true`(回填脚本 apply 完成后) | 须先跑 `tenant_rootfs_query_backfill.py --dry-run` |
| `tenant_stats.enabled` | 统计写入器 + 分钟调度 | `false` | 无此键(#388) | `false` | `true` | 10 万规模需要容量可观测;显式 scan-cost 门,开启前算成本 |
| `health_check.interval_minutes` | 健康检查间隔 | `5` | 同 | 未设 | `5` | 约 300 台/5 min 无压力 |
| `health_check.max_failures` | 连续失败判死 | `3` | 同 | 未设 | `3` | 无争议 |
| `health_check.az_failover.enabled` | AZ 级故障转移 | `true` | 同 | `true` | `true` | 需 `multi_az.enabled=true` |
| `health_check.az_failover.unhealthy_threshold_minutes` | AZ 全停多久触发 | `10` | 同 | 未设 | `10` | 14 章 § 14.2:滚动换机抖动不得误判成 AZ 挂 |
| `health_check.az_failover.cooldown_minutes` | 同 AZ 再次触发冷却 | `30` | 同 | 未设 | `30` | 防抖 |
| `s3.backup_cron` | 备份扫描节拍(非统一时间) | `rate(30 minutes)` | 同 | 未设 | **见 § 19.8 缺口** | 当前节拍在 10 万规模下覆盖率 < 1% |
| `s3.backup_interval_hours` | 每租户至少多久备一次 | `24` | 同 | 未设 | `24` | 业务 SLA |
| `s3.backup_batch_limit` | 单次触发最多备份数 | `20` | 同 | 未设 | **见 § 19.8 缺口** | 同上 |
| `s3.backup_retention_days` | 备份保留 | `7` | 同 | 未设 | `7` | 业务 SLA |
| `dynamodb.point_in_time_recovery` | 控制面表 PITR | `true` | 同 | `true` | `true` | 安全默认;误删/坏写可回滚 |
| `dynamodb.recovery_period_in_days` | 连续备份保留 | `35` | 同 | 未设 | `35` | `AWS` DynamoDB 上限即 35 |
| `audit.retention_days` | 审计表 DDB TTL | `90` | 同 | 未设 | `90` | 热窗 90 天 + WORM 归档冷数据 |
| `flow_logs.enabled` | VPC Flow Logs | `true` | 同 | `true` | `true` | CIS 3.8;跨租户隔离取证证据 |
| `flow_logs.retention_days` | Flow Logs 保留 | `90` | 同 | 未设 | `90` | 10 万规模注意 CloudWatch Logs 成本 |
| `logging.enabled` | AOS 日志链 | `false` | 同 | `true` | `true` | 两步部署:先建资源再换实例(存量实例不追溯生效) |
| `logging.aos.data_nodes` | AOS 数据节点 | `2` | 无此键(`9594b24b` #219 · 07-13 新增即 2) | 未设 | **`≥3`** | 注释明写"≥3 才达 HA 底线(Multi-AZ)";**具体数待实测**(sizing 须按采样 edge 日志流量校准) |
| `logging.aos.master_nodes` | AOS 专用主节点 | `0`(demo) | 无此键(#219) | 未设 | **`3`** | 注释明写"3=HA 硬门,其它值 CDK 拒" |
| `logging.aos.ebs_volume_size_gib` | AOS 数据盘 | `100` | 无此键(#219) | 未设 | `待实测` | 注释:起步 80% 水位,CloudWatch 排队再调;10 万规模日志量无一手采样 |
| `logging.firehose.buffering_interval_seconds` | Firehose 攒批 | `60` | 无此键(#219) | 未设 | `60` | 越小越实时但请求数与成本升 |

---

## 19.8 已确认的六处矛盾与缺口

按严重度排序。**这些都是仓库现状,不是假设。**

### ① 生产样例的超卖值是已被实测证否的危险值 · 高

`samples/config-sg-prod.yaml` 仍是 `cpu_overcommit_ratio: 6.0` / `mem_overcommit_ratio: 2.0`,
而 `#430` 已把模板默认降到 `4.0` / `1.0`。`ADR-heterogeneous-memory-aware-scheduling` § 2.1 记载
`#352` 满载实测:m8g.metal-24xl 被塞 374 VM,物理内存 `free -g` 用到 **376/376G 剩 0G**。
照生产样例部署到含 M 系的混池会重演该 OOM。

### ② 备份节拍在 10 万规模下覆盖率不足 1% · 高

`推算`:`rate(30 minutes)` = 48 批/天 × `backup_batch_limit: 20` = **960 个/天**。
10 万租户要满足 `backup_interval_hours: 24` 需要 **每天 10 万次**,当前是它的 **0.96%**。

要闭合有两个方向(都需要先实测单批并发上限,故推荐值标 `待实测`):

- 加密节拍:`rate(5 minutes)` = 288 批/天 → 需 `backup_batch_limit ≈ 348`
- 加大批量:保持 30 分钟节拍 → 需 `backup_batch_limit ≈ 2084`

单批并发 348 或 2084 个备份对 host IO 与 S3 的影响**无一手数据**,不能直接填。

### ③ `lifecycle_consumer_concurrency` 默认值与自身注释矛盾 · 中

默认 `50`;同段注释写「实测 40 并发就触发 SSM TimedOut,故单 metal 场景应设到约 5-10,**不是 50**」;
`ADR-sqs-dispatch` § 2.1 记「consumer 10 并发直发 SSM 撞 ThrottlingException」。
根因是 `AWS` SSM Agent 单实例 `DefaultCommandWorkersLimit=5` + buffer 5(源码 `constants.go:31-35`)。

### ④ 默认值曾被无关 MR 静默翻回旧值 · 中

`#259` 记载:`ae758f86`(标题是 LiteLLM baseURL 修复)把生产默认值整片翻回旧值。同一 commit 里
`cpu_overcommit_ratio` 8.0→6.0、`mem_overcommit_ratio` 2.0→1.5、`az_count` 3→2、
`lifecycle_consumer_concurrency` 10→50、`asg.min_capacity` 1→2。
**其中 `az_count` 至今仍是被翻回的 `2`。** CI 当前没有"默认值被静默翻回"的回归门。

### ⑤ `multi_az.az_count` 默认 2 与多处 3 AZ 契约不一致 · 中

`SPEC` 是 3 AZ;`imported` 模式要求 6 个子网(3 公 + 3 私)齐、缺一 synth fail-loud;
`edge.min_capacity: 3` 的注释是"一 AZ 一台起步";生产样例是 `3`。只有模板默认还是 `2`。

### ⑥ balloon 声称启用但实测未接线 · 中

`balloon.enabled: true` 且 `max_inflate_ratio: 0.4`,但 `ADR-heterogeneous` § 2.1 裁决:
实测 `amount_mib: 0`,回收未生效。任何"配了 balloon 所以内存可以超卖"的推理当前都不成立。

> 附:`SPEC` § FR-5 写端口位图 `10000-10400`(401 槽),而 `#205` 已扩到 `15000`(5001 槽)。
> 这是 SPEC 文档过时,以 `config.yml` 与 `#205` 为准,不影响运行。

---

## 19.9 10 万租户配置骨架

只列**必须偏离模板默认**的键。其余保持默认。

```yaml
host:
  instance_type: r8g.metal-24xl
  cpu_overcommit_ratio: 4.0        # 不要用 sg-prod 样例的 6.0
  mem_overcommit_ratio: 1.0        # 不要用 2.0(#352 实测剩 0G)
  reserved_vcpu: 2
  data_volume_gb: 900
  data_volume_iops: 8000
  data_volume_throughput: 500

vm:
  default_mem_mb: 2048             # ← 决定 300 台还是 535 台
  default_vcpu: 2
  host_launch_slots: 30

scheduling:
  affinity_enabled: true
  mem_safety_floor_ratio: 0.10

multi_az:
  enabled: true
  az_count: 3                      # 模板默认 2 是 #259 事故残留

asg:
  min_capacity: 0                  # 首次部署必须 0;镜像就绪后再抬
  max_capacity: 320                # SPEC 约 300 + 冗余;默认 8 / 样例 100 都不够
  lifecycle_hook_timeout: 3600

edge:
  enabled: true
  min_capacity: 3
  max_capacity: 6
  dnat_port_high: 15000

redis:
  enabled: true
  engine: valkey
  num_replicas: 2

api:
  mode: private
  throttle_rate_limit: 500
  throttle_burst_limit: 1000

dispatch:
  enabled: true
  mode: ddb                        # PutParameter 3 TPS 是硬瓶颈
  esm_max_concurrency: 20
  batching_window_seconds: 2
  max_batch_size: 500

scaler:
  lifecycle_queue_enabled: true
  create_via_queue: false          # 与 dispatch.enabled 互斥
  lifecycle_consumer_concurrency: 10   # 不是 50
  # 四个 GSI 一次只能开一个,等 ACTIVE 再开下一个
  add_gsi_tenant_user: true
  add_gsi_tenant_host: true
  add_gsi_tenant_status: true
  add_gsi_tenant_rootfs: true
tenant_query:
  enabled: true                    # 四个 GSI 全 ACTIVE 后才开
tenant_stats:
  enabled: true

quotas:
  enabled: true                    # 上限值需业务定义,无技术推导

logging:
  enabled: true
  aos:
    data_nodes: 3                  # ≥3 才达 HA 底线;精确 sizing 待日志流量采样
    master_nodes: 3                # 3 是 HA 硬门,其它值 CDK 拒
```

**仍需实测才能定的三项**(不要凭感觉填):
`s3.backup_cron` + `backup_batch_limit`(见 § 19.8 ②)、`logging.aos.ebs_volume_size_gib`、
`edge.instance_type` 是否需要从 `c6in.xlarge` 升配。

## 19.10 与其他章节的关系

| 想解决 | 去哪 |
| --- | --- |
| 压测怎么打 / 上线怎么灰度 / 生产六条红线 | [14 十万级规模化](14-scale-100k.md) |
| 键的完整清单与死键 | `config.yml.example` 行内注释(权威源)、[08 参考](08-reference.md) |
| 参数改了要不要重建 host | [16 生效矩阵](16-hot-swap-vs-baked-and-host-rebuild.md) |
| 已有环境的 EBS 性能收敛 | `engineering/runbooks/HOST-EBS-GP3-PERF-CONVERGE.md` |
| 部署后必配项与责任划分 | [15 交付边界](15-delivery-boundary-and-responsibility.md) |
