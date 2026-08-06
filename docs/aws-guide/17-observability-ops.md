# 可观测性运维手册（部署后维护）

> 本章给部署完成后的运维/SRE 团队用：可观测性三层（metrics / logs+tracing / alarms）各组件的日常维护、健康巡检、容量管理与故障排查。
> 设计与实施细节的权威源：`engineering/00-knowledge-base/SPEC/kiro/platform-observability/`（requirements/design/tasks 三件套）。本章只写"跑起来之后怎么养"，不重复设计论证。
> 与第 11 章（组件运维手册）的分工：11 章按业务组件（ALB/Redis/host ASG）分节，本章按可观测性链路分节；同一告警只在一处写阈值，本章为准。

---

## 17.1 三层架构速览（维护视角）

| 层             | 组件                                                                                                 | 配置开关                                                                                     | 默认                                                       |
| -------------- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| Metrics        | host-agent :8899 → 自建 Prometheus+Grafana（EC2）                                                    | `metrics.enabled` + `use_managed: false`                                                     | 开（自建栈；不用 AMP/AMG——强制 AWS SSO，运维复杂度不匹配） |
| Logs + Tracing | 控制面：X-Ray + Powertools 结构化日志（CloudWatch Logs）；数据面：Fluent Bit → Firehose → OpenSearch | `tracing.enabled`（默认开）/ `logging.enabled`（默认关，AOS 按小时计费，开启前先做容量采样） | tracing 开 / logging 关                                    |
| Alarms         | CloudWatch Business_Alarm_Set → SNS                                                                  | `alarms.enabled`                                                                             | 开                                                         |

全链路 correlation key：`X-Amzn-Trace-Id` 的 Root 24-hex（trace_root）。JDWS 发起、ALB 保留、edge 落日志、控制面进 X-Ray；查一个请求的全链路 = 拿 trace_root 在 OpenSearch 一次 term query + BFF trace viewer 看 X-Ray 瀑布。

## 17.2 日常巡检（建议每日一次，10 分钟）

1. **Grafana 首页**（自建栈，访问方式见 11.10）：host 容量水位（fleet 总 vCPU/内存分配 vs 物理）、per-host VM 数、balloon 回收量。混部场景重点看单 host 内存 RSS 水位——超 75% 持续 1h 应触发容量评估（超卖比 8.0/2.0 下的经验水位，见规格→密度表）。
2. **CloudWatch Alarms 面板**：全部 alarm 应为 OK。任何 In alarm 按 17.5 排查路径走。
3. **Console Queues 面板**（/system/queues，#211）：lifecycle 队列深度应接近 0，DLQ 必须为 0。DLQ 非零=有租户操作被放弃，立即按 17.5.2 处理。
4. **Fluent Bit 健康**（logging.enabled=true 时）：Grafana 看 `fluentbit_output_retries_failed_total`——非零增长=日志在丢，查 Firehose 限流或 AOS 集群水位。

## 17.3 容量与保留期管理

- **CloudWatch Logs**：所有 Lambda log group 设 retention（部署时统一，默认 90 天）。新增 Lambda 后核对 retention 不是"永不过期"（历史坑：全部 None）。
- **OpenSearch 域**（logging.enabled=true 时）：index 按天滚动 + ILM 策略热 7 天/温 30 天/删 90 天。**容量红线：磁盘水位 > 75% 或 JVM memory pressure > 80% 持续 15 分钟必须扩容**——AOS 写满会拒写，Firehose 退避重试 + S3 failed backup 兜底最多 24h，之后丢数据。
- **AOS sizing 纪律**：任何扩容决策基于实测摄入量（Firehose IncomingBytes 7 天均值），不拍脑袋；spec 里所有"GB/天"数字前面都挂着"推算"，以实测覆盖。
- **X-Ray**：采样固定 1 req/s + 5%（Lambda 侧不可配），控制面流量下在 10 万 traces/月免费额度内，无需容量维护；launch 类操作的 100% 记录靠日志链（trace_root + launch_id），不依赖 X-Ray 采样。
- **ALB access log**（R7 门后才开）：确认平台 JWT 已挪出 URL（Token_In_URL_Exposure 门）才开；桶按月分区，Athena partition projection 免手工加分区。

## 17.4 告警集与阈值（Business_Alarm_Set，CDK 建，阈值 config 可调）

| 告警                       | 阈值（默认）                   | 含义与首查动作                                                                            |
| -------------------------- | ------------------------------ | ----------------------------------------------------------------------------------------- |
| Lambda Errors              | >0 / 5min，per-function        | 控制面函数报错。CW Logs Insights 按 request_id 查结构化日志 → trace_root 进 X-Ray         |
| Lambda Throttles           | >0                             | 并发被限。查是否建租风暴/巡检脚本打满，考虑 reserved concurrency                          |
| API Gateway 5XX            | 率超阈值                       | 先分 5XX 来源（Lambda 错 vs 集成超时）；对照 API_SLO 基线（部署后 14 天实测填，禁拍脑袋） |
| lifecycle DLQ              | ApproximateNumberOfMessages >0 | 有租户 lifecycle 操作 5 次重试后被放弃。见 17.5.2                                         |
| SQS oldest-message age     | 超阈值                         | consumer 消费不动（并发上限/持续报错）。查 consumer Lambda Errors                         |
| DDB ThrottledRequests      | >0（tenants/hosts 表）         | 表容量被打穿。查突发扫描（巡检脚本全表 scan）或热分区                                     |
| dispatch DLQ               | >0（dispatch.enabled 时）      | 消费端认领后彻底放弃的死信，一进就人工介入                                                |
| Fluent Bit retry-exhausted | 非零增长                       | 日志管道在丢数据。查 Firehose 限流 → AOS 水位                                             |

全部告警发到现有运维 SNS topic；新增订阅走 SNS 订阅不改代码。

## 17.5 故障排查路径

### 17.5.1 标准路径：告警 → trace → 日志

1. 告警点名函数/队列 → CW Logs Insights 查该时段结构化日志（有 request_id / tenant_id / trace_root 字段）。
2. 拿 trace_root：BFF trace viewer（/capi/traces）看 X-Ray 瀑布定位慢/错的跳。
3. 数据面问题（logging.enabled=true）：同一 trace_root 在 OpenSearch term query，串起 JDWS→edge→gateway 三跳日志。

### 17.5.2 lifecycle DLQ 非零

1. 收消息看 body（tenant_id/action/launch_id），**别直接删**。
2. 按 launch_id 查日志链：`journalctl -t claw-launch | grep <launch_id>`（host 日志未接 AOS 时单机查）或 OpenSearch term query。
3. 常见根因：SSM 限流（并发超单实例上限）、host 容量不足（no-host）、IAM 缺权限（消息进 DLQ 且租户永久卡 creating——#141 同款）。
4. 修复根因后把消息重投主队列（同 MessageGroupId），验证租户状态收敛；不可修复的走租户删除+重建。

### 17.5.3 VM 启动失败（"创建没成功所以查不到"的盲区已封）

- 按 tenant_name 查（id 分配前就有）：创建请求从进 API 那一刻起就留痕，含 400 校验失败。
- launch 全程五事件日志（tenant_id/tenant_name/trace_root/launch_id/host_id/elapsed_ms），FATAL 带 abort 原因——host-agent 三条拉起路径都捕获（DEVNULL 盲区已修，R8.1）。

### 17.5.4 日志管道断流（AOS 里查不到新日志）

按数据流反向查：AOS index 最新 doc 时间戳 → Firehose DeliveryToElasticsearch.Success 指标 → S3 failed backup 桶有没有新对象（有=AOS 拒写，查 AOS 水位）→ edge 机器 `systemctl status fluent-bit` + 磁盘缓冲目录大小（缓冲堆积=出口不通）。nginx/路由不受影响（日志管道进程隔离，不上业务路径）——断流是可观测性事故不是业务事故，按此定级。

## 17.6 变更纪律

- 可观测性组件全部走 CDK/config，禁 console 手点（重建即丢）。开关翻转（如 logging.enabled false→true）= 改 config → cdk deploy → edge 滚动重建继承 Fluent Bit。
- **硬红线**（design decision 拍板，不可退）：日志 AOS 与 Wazuh 告警 AOS 绝不共域（连同域不同 index 都不行）；guest 内零凭据不可破——日志采集只在 host/edge 侧，绝不进 microVM 装 agent。
- Transaction Search 保持 OFF：开了 BatchGetTraces 失效，BFF trace viewer 直接废。
- 告警阈值调整走 config + MR，不 console 改（对齐"改部署代码→重建"铁律）。
