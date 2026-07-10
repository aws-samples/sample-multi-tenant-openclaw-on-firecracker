# 11 · 组件运维手册

> 本章按组件列日常维护、监控指标、告警阈值、扩缩容与故障排查。**十万级规模化的运维底线在这一章**,给运营和 SRE 团队用。
> 冲突裁决:AWS 官方文档 > 本手册 > 记忆。改变化以 CHANGELOG 为准。

---

## 11.1 CloudFront distribution

**职责**:全球边缘接入,TLS 终结,静态资源 S3 origin,数据面 /ws/\* 路由到 ALB。

**日常维护**:

- 无需运维,AWS 全托管。改动仅通过 CDK 部署。
- 每季度检视 `security_headers_behavior`(CSP/HSTS 等)是否落后于安全基线;更新 origin timeout 时同步 SPEC。

**监控**:

- CloudWatch 指标 `4xxErrorRate` / `5xxErrorRate` / `OriginLatency` / `TotalErrorRate`。
- WAF 关联时看 `BlockedRequests` 突增(=对方在扫)。

**告警阈值(建议)**:

- 5xx 率 5min 均值 > 1% → warning;> 5% → critical(通常是 origin/ALB 侧问题,不是 CloudFront 自己)。
- OriginLatency p99 > 5s(SSE 场景通常低,除非 microVM gateway 慢)→ 排查 gateway。

**长静默 WS 场景的注意事项**:CloudFront origin `readTimeout` 硬上限 **180s**(AWS 侧硬约束,CDK 源码 `aws-cloudfront-origins/lib/http-origin.ts:76` 明确 `validateSecondsInRangeOrUndefined('readTimeout', 1, 180)`)。WS 连接静默超过 180s 会经 CloudFront 断线;客户端必须发心跳(30s ping 一次是安全值)。这条要写进接入方的 SDK/接入指南。

**故障排查**:

- 客户报 502 从 CloudFront:先看 CloudWatch OriginLatency 有没有 spike;再看 ALB access log 有没有对应请求;都没有=CloudFront 侧问题,查 origin health 与证书链。
- SSE 断:确认 origin `read_timeout` 是不是 180s;客户端有没有 30s 心跳。

---

## 11.2 ALB(Application Load Balancer)

**职责**:数据面 L7 入口,least_outstanding_requests 分发到 OpenResty edge ASG;/hub/\* 分发到 host TG(过渡期)。

**日常维护**:

- ALB 跨 3 AZ AWS 托管,免运维。CDK 部署时确认 `idle_timeout=3600s`(SSE/WS 长连接)+ SG 只放 CloudFront prefix list(暴露红线)。
- ACM 证书 auto renewal 生效(AWS 自动)。

**监控**:

- CloudWatch:`RequestCount` / `TargetResponseTime` / `HTTPCode_ELB_5XX_Count` / `TargetConnectionErrorCount`。
- Target Group 健康:`HealthyHostCount` / `UnHealthyHostCount`。
- 连接错误 `TargetConnectionErrorCount`:非 0 通常是后端 SG 或 TG 端口配错。

**告警阈值**:

- HealthyHostCount < N-1(N=ASG desired)持续 2min → warning;=0 → critical(数据面全断)。
- HTTPCode_ELB_5XX_Count 5min > 100 → warning;意味着 ALB 层错(SG 拒/后端全 unhealthy)。
- TargetResponseTime p95 > 3s → 排查后端慢。

**扩缩容**:

- ALB 自动扩缩容,LCU(Load Capacity Unit)按用量计费。10w 租户 30w 并发连接场景 LCU 消费需运营人员估算成本:一个 LCU = 25 new conn/s 或 3000 active conn/min 或 1GB 处理数据(取最高)。30w active = 100 LCU/min,约 $0.008/LCU-hour × 100 × 730 = **$584/月**参考值。真值以账单为准。

**故障排查**:

- 客户报"WebSocket 挂着不通":先看 ALB idle_timeout 是不是 3600s(不是就是没落到最新 IaC);再看 TG healthy(edge instance 数量);edge 存不存活。
- 5xx 突增:看 ALB access log(建议开 access log 到 S3,现代码里`connection_logs`/`access_logs`未开,**建议开**);grep 具体 code 定位。

---

## 11.3 OpenResty edge ASG(数据面路由层)

**职责**:每 tenant_id 查 Redis 拿 host:port,proxy_pass 到宿主 DNAT 或本机 microVM gateway。**这是数据面新入口,10 万租户必守**。

**部署形态**:独立 ASG,跨 3 AZ,min=3 desired=3(N-1 容灾),max 按压测,c6in.xlarge(x86)或 c7g.xlarge(arm64),userdata=`deploy/edge/install-edge.sh`。

**日常维护**:

- 每 30 天走一次 rolling replace(用 ASG instance refresh)让实例吃到最新 OpenResty 补丁 + kernel 更新。步骤:`aws autoscaling start-instance-refresh --auto-scaling-group-name openclaw-edge-asg --preferences MinHealthyPercentage=66`(3 台时至少留 2 台,保 N-1)。
- OpenResty 版本升级:改 `install-edge.sh` 明确 `OPENRESTY_VERSION`,滚动重建。
- 检视 `/etc/sysctl.d/99-openclaw-edge.conf` 是否被 AMI 更新覆盖(不该被覆盖,是 sysctl.d 目录,但 AMI 换基础镜像时要检查)。

**监控**:

- Prometheus 抓 `:8080/metrics`(现在是 stub,P6 完善后有 cache-hit rate / route source distribution / redis latency)。
- CloudWatch 抓 CloudWatch agent 采的 `/journald` OpenResty error log(WARN/ERR grep)。
- ELB TG HealthyHostCount = min_capacity。

**告警阈值**:

- HealthyHostCount < 3(min_capacity)持续 5min → critical。
- Redis 错误率(edge log grep `redis transport err`)5min > 10 条 → 数据面进 fail-static,重要 warning——Redis 侧要立刻查。
- warmup 失败(/healthz 长期 503)→ 实例被 ASG 换掉,若换 3 次都失败(instance refresh 反复)= ElastiCache 未就绪或 SG 配错,立刻查。

**扩缩容**:

- 触发指标:`RequestCountPerTarget` p95 > 2000 rps 持续 3min 或 CPU 均值 > 70% → desired += 1。
- 缩容:CPU < 30% 持续 30min → desired -= 1(不小于 min)。

**冷启到 healthy 的墙钟(SPEC §6 关键)**:

- **grace_period 建议 300s**。真机在 P7 阶段实测冷启:EC2 boot ~60s + install-edge apt 装 openresty ~90s + nginx 起 <5s + route.lua async Redis warmup 最多 30s(每 2s 一次共 15 次)+ 缓冲。
- **warmup gate 已落 install-edge.sh**:userdata 尾部轮询 `/healthz` 到 200 才让 lifecycle 成功,ASG lifecycle hook 命中此才 CONTINUE。若某台反复 warmup 失败,journalctl -u claw-edge 看具体 err。

**故障排查**:

- 客户报"部分租户 404 部分 200":routes 缓存不一致——三层缓存 L1/L2/L3 有一层写坏。先 SSH edge 跑 `curl 127.0.0.1:8080/healthz` 确认 warmup ok;再看 route.lua log grep tenant_id 查是不是 L3 miss;再直连 Redis `redis-cli -h <primary-endpoint> get route:<tid>` 看数据在不在。
- 一波 503:大概率 Redis brownout,route.lua 进 fail-static;检查 ElastiCache event log 是否有 failover。

---

## 11.4 Host ASG(metal Firecracker 池)

**职责**:每台 r8g.metal-24xl 启 380 microVM,是"堡垒"(见项目铁律 #3)。

**日常维护**:

- 改身份/skill/config 只能重烤黄金镜像 + 滚动重建。**绝不热改运行中的 host 或 VM**(项目铁律 #3)。
- host 扩容:改 `config.yml:asg.max_capacity` 提升上限,再触发 scaling event。
- 版本升级(image v4 → v5):走 P3 阶段的镜像重烤 + instance refresh。灰度顺序按 rolling replacement,一台一台换,失败回滚 launch template version。

**监控**:

- host-agent `:8899/metrics`(Prometheus 抓):`active_vm_count` / `disk_usage_gb` / `dnat_rule_count` / `port_bitmap_usage` / `descriptor_drift_count`。
- CloudWatch Agent 抓 host system metrics(cpu/mem/disk io)。
- `descriptor_drift_count` > 0 持续 5min → warning(host-agent 端口位图/DNAT/DDB 三方对账失败,可能资源泄漏)。

**告警阈值**:

- active_vm_count / host 上限比 > 95%(容量吃紧,该扩容)。
- disk_usage_gb > 800(900GB 盘,留 100GB 应急)→ warning。
- host 心跳 > 5min 未上报 → critical(整台 host 挂,上面租户全影响,AZ failover 应触发 config.yml:health_check.az_failover)。

**扩缩容**:

- scaler Lambda(deploy/lambda/scaler)按 idle_timeout_minutes 判断 host 是否空转;满足条件 terminate。
- 建租户走 SQS dispatch 削峰(config `dispatch.enabled=true`,10 万租户建租户高峰必开,别走同步 API 打爆 SSM)。见 config.yml:139-148 注释。

**故障排查**:

- 单 host 挂但同 AZ 其他 host 正常:确认 az_failover 未误触发;看 EC2 console 有无 status check 失败;`aws ec2 get-console-output` 拿 kernel oops。EBS 卷是 `keep_data_volume=true` 时租户数据留着,新 host 起来后 host-agent 会 attach 并 recover。
- 整个 AZ 挂:config.yml:health_check.az_failover 会自动把 running 租户迁到其他 AZ;冷却 30min 内不重复触发。
- iptables DNAT 漂移(descriptor_drift_count > 0):`_probe_all` 会告警。运维直接 SSH 上 host 跑 `iptables -t nat -L PREROUTING -n --line-numbers` 对比 DDB descriptor(host_port)是否一致;差集清理走 host-agent 的对账函数,不手工删表。

**十万级规模化注意事项**:

- 单 host 380 租户是硬容量上限(2GB/VM × 380 = 760GB 匹配 768GB metal 内存),超了会 OOM。别为了塞多而拉高 `mem_overcommit_ratio` 到 >1.5,会掉进 balloon 回收窗口窄的坑(SPEC §firecracker-hardening `free_page_reporting=true` 是主力回收)。
- 300 host 到位后跨 AZ 分布确认均匀(不要一个 AZ 集中,单 AZ 挂损失过大)。ASG 的 `AvailabilityZoneImpaired` 自愈能力靠这个。
- 冷启时间 lifecycle_hook_timeout 已设 1200s(config.yml:71 注释:842MB 黄金镜像下载 + 解压 + 挂盘),300 host 满量启动需要一次买 1200s 冷窗口,压测时用 wave 上限(每 3-5min 一批 20 台)避免 SSM 打爆。

---

## 11.5 ElastiCache Redis(路由缓存权威源)

**职责**:tenant_id → {host, port, guest_ip} 权威路由表;host-agent 单向写,edge 单向读。

**部署形态**:Multi-AZ replication group,3 节点(1 primary + 2 replicas 跨 AZ),`automatic_failover_enabled=true`,Redis 7.x,私有子网,SG 只放 host + edge。

**日常维护**:

- Redis 内存增长监控:每租户 route 值 ~200B,10w 租户 = 20MB,极小。选 cache.r7g.large 起步(13GB)富余。
- 自动 snapshot 每天一次,保留 7 天(元数据丢了从 host-agent DDB 侧重建,不真依赖 snapshot 恢复;开着当第二保险)。
- 引擎补丁维护窗口:选 off-peak(如凌晨 4 点 UTC),`maintenance_window` config。

**监控**:

- CloudWatch: `EngineCPUUtilization`、`DatabaseMemoryUsagePercentage`、`Evictions`(不该有,因为不设 EXPIRE)、`ReplicationLag`、`ReplicationBytes`。
- **关键**:`CacheHits` / `CacheMisses` 比例;GET route:{tid} 应 99%+ 命中(host-agent 双写生效)。
- 应用侧(edge log):`redis transport err` 次数—— failover 期间会 spike,平常应 0。

**告警阈值**:

- ReplicationLag > 5s 持续 5min → warning(可能 primary 压力过大或 replica AZ 有问题)。
- Evictions > 0 → critical(设计不允许淘汰路由 key,evict=数据丢=部分租户 404)。
- EngineCPUUtilization > 70% → warning,可能要 scale up 节点类型或分片。
- CloudWatch Event `AWS ElastiCache Failover` → info(记录 failover 发生),同时看 edge 侧 fail-static 触发时长——建议 < 30s(应用 side L2 TTL 60s 覆盖窗口)。

**failover 与运维**:

- Multi-AZ failover 通常 15-30s(AWS 文档说 "<60s,大多数场景 15-30s",数字**待真机验证**)。期间 client 连接需重连;host-agent(redis-py)配 `retry_on_error=[ConnectionError]` + `health_check_interval=30`,不复用 stale socket。edge 侧靠 nginx resolver TTL 30s + L2 stale 60s 兜住。
- 手动 failover(演练用):`aws elasticache test-failover --replication-group-id openclaw-routes --node-group-id 0001`。生产每季度演练一次。

**扩容**:

- 内存不够(> 70%)→ 节点类型升级,先加 replica 后 promote,再删旧 primary。**是 disruptive,提前通告运维窗口**。
- QPS 不够(单节点 100k+ ops/s 才撞)→ Redis Cluster mode 分片。10w 租户的 QPS(peak ~10k GET/s)远达不到,单节点足够。

**故障排查**:

- edge 大量 fail-static(503) + 5xx:先看 Redis 连接性——从 edge 实例 `redis-cli -h <primary-endpoint> ping`;再看 primary endpoint DNS 是不是解析到活节点(`dig +short <primary-endpoint>`;看 AZ 有没有偏移)。
- 一段时间内某租户 404:host-agent 没写 Redis。SSH 上对应 host 看 host-agent log grep tenant_id;直接 `redis-cli -h <endpoint> get route:<tid>` 看空不空。

---

## 11.6 API Lambda + DDB(控制面)

**职责**:租户 CRUD + host 生命周期编排。已有,数据面重构不动。

**日常维护**:

- Lambda:`update-function-code` 时带完整依赖 wheel(项目坑 `e2e-795-passed-and-pyjwt-lesson`);别只传 .py。
- DDB 表:PITR 已开(35 天),audit 表 WORM 归档另配(config.yml:audit.worm_archive_enabled)。**删表前必快照**(项目铁律 #4)。

**监控**:

- Lambda:`Errors` / `Throttles` / `Duration` / `ConcurrentExecutions`。
- DDB:`ThrottledRequests` / `UserErrors` / `ConsumedReadCapacityUnits` / `ConsumedWriteCapacityUnits`;on-demand 模式配额自适应,极端 burst(几秒 10x)才会撞。
- SQS dispatch queue:`ApproximateNumberOfMessagesVisible` / `NumberOfMessagesReceived` / DLQ 深度。

**告警阈值**:

- Lambda Errors 5min > 5 → warning。
- DDB ThrottledRequests > 0 持续 3min → critical(切 provisioned + auto scaling)。
- SQS DLQ 深度 > 0 → critical(哪条消息到不了,查 dead letter,fail-loud)。

**扩缩容**:

- 大规模建租户:`config.yml:scaler.lifecycle_queue_enabled=true` + `create_via_queue=true` + `dispatch.enabled=true`(config.yml:139),走 SQS 装箱削峰。**否则同步直驱 SSM 40 并发就撞 TimedOut**(项目坑 loadtest-380-ssm-concurrency)。

---

## 11.7 KMS CMK

**职责**:gateway token 信封加密(EncryptionContext=tenant_id)+ 注入凭据信封加密(EncryptionContext=owner_id)+ audit CMK(可选)。

**日常维护**:

- CMK 开自动轮换(每年一次),旧密钥可解密老密文,新密文用新密钥。CDK 建 CMK 时 `enable_key_rotation=True`。
- Key policy 每季度审计一次:host role 只能 Decrypt,不能 Encrypt/GenerateDataKey;API Lambda 反之。
- KMS Key **绝不能删**(30 天 pending window 内可 cancel,期外密文永远丢)。删 stack 时 CMK 设 RemovalPolicy.RETAIN。

**监控**:

- CloudWatch 指标 `KMS.Requests` / `KMS.Errors`。
- CloudTrail 记 Decrypt/Encrypt 事件,per-role 审计。异常访问(非预期 role 调 Decrypt)立即告警。

**告警**:

- 单分钟 Decrypt 错误 > 20 → warning(可能 EncryptionContext 不匹配,身份被拆穿)。
- CloudTrail `DisableKey` / `ScheduleKeyDeletion` 触发 → critical(有人在删密钥)。

---

## 11.8 NAT Gateway(每 AZ 一个)

**职责**:私有子网 host + edge + Lambda 出网到 Bedrock/LiteLLM/公网 API。

**日常维护**:

- 每 AZ 一个 NAT GW,不共享跨 AZ(减跨 AZ 带宽费 + 单点风险)。EIP 建议 pre-allocate 固定 IP(方便上游放行白名单)。
- NAT GW 带宽自动扩缩(单 GW 最高 100 Gbps + 55000 并发连接/EIP/destination),不需运维干预。

**监控**:

- CloudWatch: `BytesInFromDestination` / `BytesOutToDestination` / `ActiveConnectionCount` / `ConnectionAttemptCount`。
- `ErrorPortAllocation` > 0 → warning(EIP 端口耗尽,同一目的 IP 连接太多,需要加 EIP 或用 VPC Endpoint 绕开)。

**告警**:

- ErrorPortAllocation > 0 → critical(数据面开始丢包)。
- 单 NAT GW 出站带宽 > 50 Gbps → warning(接近上限)。

**扩容 / 优化**:

- 出站到 AWS 服务(Bedrock/S3/DDB/KMS)加 VPC Endpoint 绕开 NAT——**十万级规模关键成本项**。NAT 数据处理费 0.045 USD/GB;VPCE gateway type(S3/DDB)免费;interface type $0.01/GB + $0.01/hour/AZ。LLM 调用(Bedrock 走 InvokeModel)如果走 NAT,10 万租户日活跃出量按 100GB 保守估计 = $4500/月;走 VPCE 拉到几百刀。
- 出站集中打一个上游 EIP 时加多 EIP(NAT GW 支持 secondary IP,每个 EIP 各 55000 连接/destination)。

**故障排查**:

- 部分 microVM 打不通外网:先看是不是撞 egress_allowlist(config.yml:security.egress_allowlist_enabled=true 时 tap 上有 iptables DROP);再看 NAT GW ErrorPortAllocation;最后看目标服务(LiteLLM/Bedrock)侧限流。

---

## 11.9 Wazuh manager(安全监控)

**职责**:in-guest auditd/FIM + GuardDuty + host-agent metrics 聚合。

**部署形态(当前)**:单 EC2(m7i.xlarge/AL2023)docker-compose 起 manager/indexer/dashboard;`config.yml:security.wazuh_enabled=false` 默认关。

**HA gap**:单实例 = 单点。生产建议:

1. 简单:`systemd Restart=always`(docker-compose 自身有 restart:unless-stopped),挂了自恢复,但数据落 EBS 单卷,卷坏就丢。
2. 稳:两台 manager cluster + 共享 EFS(容器 volume 挂 EFS);indexer 走 OpenSearch cluster(多节点)。
3. 简单版够 demo/dev,生产走稳版。

**日常维护**:

- 每周检视 alerts 分类,规则误报调 rule 权重。
- Wazuh version 每季度 minor upgrade(改 docker image tag,滚动)。

**监控 / 告警**: Wazuh 自身的 dashboard 看 alerts;critical 级 alert 由 SNS 转 email/slack。

---

## 11.10 Prometheus + Grafana(自建栈,P6)

**职责**:host-agent :8899 / edge :8080/metrics / Lambda CloudWatch metrics 汇总。

**部署形态(计划)**:CDK 起 EC2(m/r 系列)跑 docker-compose(deploy/monitoring/docker-compose.prom-grafana.yml)。**config.yml:metrics.enabled=true, use_managed=false 是本项目既定架构**(规避 AMG 强制 AWS_SSO)。

**HA gap**:单实例。SPEC §6 明确"自建栈也应放 ASG 或有重启策略"。生产建议:

- min 版:systemd Restart=always + EBS PITR 快照 + Grafana provisioning 版本化在 git。
- 稳版:2 台 EC2 + ALB + EBS 共享(EFS)或独立(每台自查),Prometheus 用 remote_write 双写。

**日常维护**:

- Prometheus 数据保留天数按盘算:15 天 × 每分钟 1k 序列 ≈ 5GB;30 天设 20GB。
- Grafana provisioning:dashboards + alerting rules 存 git,禁 UI 直改(否则重建丢配置)。

**监控 / 告警**: Grafana alerting rules 匹配上面各组件阈值;alert 走 SNS。

---

## 11.11 十万级规模化(全局注意)

**成本削减重点**(SA 团队应先算清):

1. LLM 出站(Bedrock)走 **VPC Interface Endpoint**,绕开 NAT 数据处理费 —— 十万租户日活场景每月省 $2000-5000。
2. S3(rootfs/backup)走 **VPC Gateway Endpoint**(免费),host 镜像下载不走 NAT。
3. SSM / KMS / CloudWatch Logs / STS 都开 VPC Interface Endpoint,收暴露面 + 降 NAT 费。
4. host 用 Reserved Instances / Savings Plans(80% 长期负载):300 台 r8g.metal-24xl 按需 $6.82/hr,3 年 SP compute 立省 ~55%,单台省 ~$3.75/hr × 24 × 30 × 300 = **$810k/月**(仅 host 层)。
5. NAT GW 每 AZ 一个不共享;跨 AZ 走本 AZ 出口不走别的 AZ(默认路由指本 AZ)。

**扩容顺序**:先加 host(容量瓶颈),再加 edge(路由容量,3 台起步够到 ~30w rps),ElastiCache 后加(除非 QPS 上 5w+/s 才需要),ALB/CloudFront 全自动免手动。

**演练节奏**:

- ElastiCache 手动 failover 演练:季度一次。
- edge 单 AZ kill 演练:季度一次(kill 一个 AZ 的 edge 实例,看 ASG 自愈时长和 ELB 分流)。
- host AZ failover 演练:半年一次(触发 az_failover,验证租户迁移路径)。

---

## 11.12 交接清单

新运维接手先跑一遍:

1. `aws sts get-caller-identity` 确认账号与 profile。
2. 拉一份最新 stack.py + config.yml,对照本手册第 11.1-11.10 逐项对齐实际部署。
3. Grafana 打开看有没有 dashboard;没有跑一遍 provisioning。
4. 一次手动 failover 演练(ElastiCache + host AZ)。
5. 打开 CHANGELOG.md 看最近 30 天改动。
6. 读 `.claude/rules/amazon-production-safety-do-not-delete.md` 铁律(删资源前必快照;假设生产)。
