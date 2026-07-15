# 十万级规模化(测试 / 上线 / 生产)

> 本章按 **测试 → 上线 → 生产** 三个阶段,把跑到 10 万 microVM 规模需要守的硬约束一次说清。**不重复 13 章**([数据面两级路由](13-data-plane-redesign.md))的架构原理,**也不重复 11 章**([组件运维手册](11-ops-maintenance.md))的日常告警指标。这里只讲三个问题:压测怎么打才算真、灰度上线怎么切、生产稳态守哪些红线。
>
> 规模基线来源:`internal design docs § 2`(10 万 microVM · ~300 host · 30 万并发 WS)。

---

## 14.1 测试阶段:满负载 380/台 + 反向用例

**满负载硬要求**:真机测试必须打**满负载 380/台**,且用例集合**必含反向场景**,不允许只跑 happy path。

### 满负载压测

- **单机上限**:`r8g.metal-24xl` 每台 380 microVM(2 GB/VM × 380 = 760 GB,匹配 768 GB 内存)。测试计划见 `internal design docs`。
- **不允许绕开的四类测试**(缺一挂 backlog,不许 close issue):
  1. **稳态**:380 microVM 全 running,SSE 保持 30 min 无掉线。
  2. **突发建租户**:一批 300 create/s 打进 SQS dispatch,验证削峰到 host 单实例 SSM 并发 ≤ 阈值(795 实测 40 并发就撞 TimedOut,`memory: loadtest-380-ssm-concurrency-bottleneck`)。
  3. **单 AZ 挂**:kill 一个 AZ 的 edge + host 混合负载,余两 AZ 承接;验证 `az_failover` 迁租户能力(`config.yml:health_check.az_failover`)。
  4. **conntrack 表满**:边缘 + host 同时打到 `nf_conntrack_max=1048576` 附近,验证不丢包。edge 侧 `install-edge.sh:131`,host 侧 `init-host.sh:85-99`。

### 反向用例(与 happy path 同等硬)

铁律 #11 明确:安全/隔离/删除类改动测试强度按爆炸半径上调。至少覆盖:

- **跨租户越权**:A 拿 B 的 tenant_id + 自己的 gateway_token → gateway 401(EncryptionContext 不匹配,`kms:Decrypt` 拒)。
- **过期 token**:密文表 TTL 900s 过期后 `GET /tenants/{id}/token` 返 410,再拉不到明文。
- **Redis brownout**:kill primary,15-30s failover 期 edge fail-static(L2 stale 60s)兜住 5xx。真机演练命令:`aws elasticache test-failover --replication-group-id openclaw-routes --node-group-id 0001`。
- **端口位图并发**:两个 host-agent worker 同时 alloc,期望不撞端口(`route_ops.alloc_and_dnat_atomic` 三步原子)。
- **descriptor 三方对账**:构造 iptables DNAT 有、DDB descriptor 无的漂移场景,`_probe_all` 应告警并自愈。

### 证据留痕

所有测试结果必须落 `internal test evidence`——没留痕 = 没测过(per the project test discipline)。

---

## 14.2 上线阶段:灰度滚动 + 分步部署

**核心纪律**(memory `goal-restructure-and-deploy-uswest2-2026-06-30`):不要 `min_capacity=1` 让 host 在镜像就绪前起。**正解顺序:先 min=0 → 烤镜像 → 再 scale 到目标容量**。

### 冷启部署顺序(fresh region)

1. **VPC + 网络先起**:`./setup.sh <region> <profile>` 走 `deploy/stack.py:_build_vpc(mode=self_managed)`(自建 /20 + 3 AZ + 3 NAT GW)。这一步栈成功但 host_asg **min=0**、edge_asg **min=0**。
2. **烤镜像**:`build-rootfs.sh --arch arm64` 或 stack 内 CodeBuild 自动烤(`image.build_in_stack=true`)。等 S3 里有对应 rootfs.
3. **拉 host 到最小容量**:改 `config.yml:asg.min_capacity=2` 再 `setup.sh` 一次。host 拉起时 rootfs 已在 S3,不会 Heartbeat Timeout 反复替换(踩过:`memory: uswest2-deploy-deadlock-and-fixes`)。
4. **拉 edge 到 min=3**:`config.yml:edge.enabled=true` + `edge.min_capacity=3`,`setup.sh`。edge userdata 会轮询 `/healthz` 到 200 才 CONTINUE(`install-edge.sh:170-183` warmup gate),ASG lifecycle 只在真能路由时才放行。

### 滚动升级(改镜像 / 改 stack.py)

**改镜像**(改身份、skill、config、guardrail 模型)= 重烤 + 滚动重建(铁律 #3)。步骤:

1. `build-rootfs.sh` 烤新镜像 · `image.version` 版本号加一(如 `v5.0 → v5.1`)。
2. `setup.sh` 部署 CDK 更新 → 触发 `aws autoscaling start-instance-refresh --auto-scaling-group-name openclaw-hosts-asg --preferences MinHealthyPercentage=66`(3 台以上 ASG 至少留 2/3 承接流量)。
3. 观察 `az_failover` 是否触发误判(单批换机的抖动 vs 真 AZ 挂,threshold 见 `config.yml:health_check.az_failover.unhealthy_threshold_minutes=10`)。

**改 stack.py**(改 IaC 结构、DDB schema、IAM):

- 直接改 `stack.py` → `setup.sh` 走 CFN update。
- **不可逆改动**(删 DDB 表 / 改 RemovalPolicy · 动安全红线 SG/IAM/凭据 · 动 Guardrail)必走 SHARED-FILES-PROTOCOL 串行 + 人工评审门。
- DDB 表 `RETAIN` 是硬默认(尤其 tenants / audit / tenant-secrets);删表前必快照(铁律 #4)。

### 数据面切换状态(旧 hub-WS → 新两级路由,已完成)

灰度切换已完成:旧 hub 路径(ALB `/hub/*` rule、HubTargetGroup、CloudFront `/hub/*` behavior)已随 #187 P5 全部从栈中删除;数据面唯一路径是 ALB `/vm/*` + `/ws/*` → EdgeTG(OpenResty edge),前端 SDK 走 `wss /gw/ws`(见 `docs/aws-guide/13-data-plane-redesign.md § 13.1`)。

---

## 14.3 生产阶段:守六条红线

生产 = 10 万 microVM 稳态。以下六条红线**破一条即事故**:

### R1 · conntrack 表 · 单机 400 microVM 硬门

- **值**:`nf_conntrack_max = 1048576`(edge `install-edge.sh:131` + host `init-host.sh:85-99` 都设)。
- **背景**:Ubuntu 22.04 aarch64 内核默认 262144,单机 380 microVM × 每 VM 5-10 stateful conn + 跨 host DNAT + LLM 出站 = 稳态几万到峰值 10 万级,靠默认会撞。
- **切换点(未来密度)**:单机 DNAT 规则 >1000 时(未来 microVM 密度提升),`iptables PREROUTING` 顺序匹配变热路径瓶颈,**迁 `nftables sets`** 常数时间查找。当前 400 条内 O(n) 走完 μs 级,不动。**记 backlog,不做**。
- **监控**:`cat /proc/sys/net/netfilter/nf_conntrack_max`(限)+ `wc -l /proc/net/nf_conntrack`(实占)· 实占 > 80% 限即 warning。

### R2 · edge ASG 弹性 · N-1 保底 + 冷启 300s

- **值**:`config.yml:edge.min_capacity=3`(3 AZ 各一台,N-1 容灾)· `edge.health_check_grace_period_seconds=300`(cold start = apt install openresty + nginx start + Lua warmup + Redis 探活 30s 重试)。
- **弹性**:`RequestCountPerTarget` p95 > 2000 rps 持续 3min 或 CPU > 70% → desired += 1;CPU < 30% 持续 30min → desired -= 1(不小于 min)。
- **失败模式**:warmup gate 三次 refresh 反复失败 → ElastiCache 未就绪或 SG 配错(见 `docs/aws-guide/11-ops-maintenance.md § 11.3`)。

### R3 · Redis primary endpoint DNS · TTL 30s 强制刷

- **值**:客户端(edge nginx + host-agent redis-py)DNS TTL 都按 30s 刷。edge 侧 `deploy/edge/nginx.conf:47-50` `resolver 169.254.169.253 valid=30s ipv6=off`。
- **不允许**:硬编码 Redis 节点 IP。ElastiCache Multi-AZ automatic_failover 会在 15-30s 内切主并更新 primary endpoint CNAME,DNS 不刷就连旧主。
- **应用侧兜底**:edge fail-static L2 TTL 60s(`deploy/edge/lib/backend.lua:60` `L2_TTL_SEC=60`),覆盖 failover 窗口。

### R4 · CloudFront 180s 硬上限 · 客户端 30s 心跳

- **值**:CloudFront origin `readTimeout` 硬上限 180s(CDK 源码校验,不可绕),ALB idle 3600s 和 OpenResty proxy 3600s 补救不了。
- **要求**:客户端 SDK 强制 **≤30s 一次心跳**(WebSocket ping 或 SSE keepalive),否则空闲 WS 经 CloudFront 断,ALB 层看不到断连。
- **文档**:接入方 SDK 文档必写清心跳纪律,不能默认。

### R5 · KMS 权限最小化 · API 无 Decrypt

- **值**:`deploy/stacks/lambdas.py:406-425` API Lambda role 只授 `kms:GenerateRandom + Encrypt`,**不授 `Decrypt`**。调用方(平台后端)本地 Decrypt(EncryptionContext={tenant_id})。
- **背景**:API 若能 Decrypt,一旦 Lambda role 被误授给别的 IAM principal,gateway_token 明文可被拉库枚举。分离权限使得 CloudTrail 上 `Decrypt` 事件只可能来自调用方 IAM,越权即可查。
- **监控**:CloudTrail `Decrypt` 事件超预期 IAM principal 立即 critical 告警。

### R6 · NAT GW 与 VPC Endpoint 分流 · 10 万级成本 lever

- **值**:每 AZ 独立 NAT GW(`deploy/stacks/_helpers.py:52 nat_gateways=3`),不跨 AZ 出站。
- **10 万级成本刀口**:LLM(Bedrock)/ S3 / DDB / KMS 都走 **VPC Interface / Gateway Endpoint** 绕 NAT 数据处理费(0.045 USD/GB);S3 与 DDB Gateway Endpoint 免费,Bedrock/KMS Interface Endpoint 按 AZ 小时费 + 处理费。10 万租户日 100 GB Bedrock 流量:走 NAT ≈ $4500/月,走 VPCE ≈ 几百刀。
- **监控**:NAT GW `ErrorPortAllocation > 0` 是 critical(数据面开始丢包 · 建议加 secondary EIP 或加 VPCE)。

---

## 14.4 演练节奏(半年一轮)

生产上线后必须建立"演练"节奏,不等真事故来。规格:

| 场景                      | 频率     | 命令 / 步骤                                                                                                                   |
| ------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------- |
| ElastiCache 手动 failover | **季度** | `aws elasticache test-failover --replication-group-id openclaw-routes --node-group-id 0001` · 观察 fail-static 触发时长 < 60s |
| Edge 单 AZ kill           | **季度** | terminate 一个 AZ 的 edge 实例 · 观察 ASG 补新 + ELB 分流时长                                                                 |
| Host AZ failover          | **半年** | 触发 `az_failover`(手工 disable 一个 AZ 的 host status)· 验证租户迁移路径                                                     |
| CloudFront 长静默门       | **季度** | 打一个 200s 静默 WS · 观察是否被 CloudFront 180s 断 · 验证客户端心跳 SDK 落地情况                                             |
| Guardrail 拦截采样        | **月度** | OWASP top 10 case 抽样跑 · 拦 14/14 是 baseline · 有掉的立刻查                                                                |

演练结果落 `internal test evidence` 归档。

---

## 14.5 与其他章节的关系

- **架构原理**:见 [第 13 章 · 数据面两级路由](13-data-plane-redesign.md)。
- **组件运维手册**(告警阈值 / 扩缩容触发 / 故障排查):见 [第 11 章 · 组件运维手册](11-ops-maintenance.md)。
- **私有 API 加固**:见 [第 12 章 · Private API Gateway](12-private-api-hardening.md)。
- **HA 审计**(15 组件逐条 · 已修 vs 仍单点):见 [`internal design docs`](../../internal design docs)。
- **交接文档**(新人上手):内部交接文档。
