# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Business alarm set (#209 R9 / #220).

R9.1: per-function Lambda Errors + Throttles, API Gateway 5XX, SQS lifecycle
DLQ non-empty (already in lambdas.py:LifecycleDlqAlarm — kept, not touched),
SQS lifecycle queue ApproximateAgeOfOldestMessage, DDB ThrottledRequests on
tenants and hosts tables.

R9.2: alarms notify the ops SNS topic and thresholds are config-tunable via
      observability.alarms.

R9.3: default enabled=true; toggle in config.yml.example.

The dispatch DLQ alarm (dispatch_infra.py:DispatchDlqAlarm) and the lifecycle
DLQ alarm (lambdas.py:LifecycleDlqAlarm) predate this module. Per #220 spec
they stay as-is; this module only adds. Both are attached to the same
alarm-actions SNS topic here so all business alarms fan out to the same
subscribers (existing alarms already emit CloudWatch state changes; wiring
their action was left to the operator, so we do not mutate them).
"""

from aws_cdk import (
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    Duration,
)


# Default thresholds. Kept intentionally simple: Lambda Errors/Throttles/DLQ
# alarm on the first hit (>0 in a 5-minute window). API 5XX and SQS oldest-age
# have real thresholds because a spurious burst is normal.
_DEFAULTS = {
    "lambda_error_threshold": 0,  # >0 in 5-min window → alarm
    "lambda_throttle_threshold": 0,
    "lambda_evaluation_periods": 1,
    "lambda_period_minutes": 5,
    "api_5xx_threshold": 5,  # >5 5XX responses in 5-min window
    "api_5xx_evaluation_periods": 1,
    "api_5xx_period_minutes": 5,
    "sqs_oldest_age_seconds": 300,  # 5-minute lag = investigate
    "sqs_oldest_age_evaluation_periods": 2,
    "sqs_oldest_age_period_minutes": 1,
    "ddb_throttle_threshold": 0,
    "ddb_throttle_evaluation_periods": 1,
    "ddb_throttle_period_minutes": 5,
    "redis_replication_lag_threshold_seconds": 5,
    "redis_replication_lag_evaluation_periods": 5,
    "redis_replication_lag_period_minutes": 1,
    # #625 edge reader endpoint 回落。安装期一次性指标,>0 即"至少一台没分流到
    # reader",没有"正常抖动"可言,所以阈值 0 / 单周期即报;5 分钟窗口给同批起的
    # 实例留出把数据点都送到的时间。
    "edge_reader_fallback_threshold": 0,
    "edge_reader_fallback_evaluation_periods": 1,
    "edge_reader_fallback_period_minutes": 5,
}


def _cfg(alarms_cfg: dict, key: str):
    """Read a threshold from observability.alarms, defaulting from _DEFAULTS."""
    v = alarms_cfg.get(key)
    return _DEFAULTS[key] if v is None else v


def build_alarms(self, ctx):
    """Wire the R9 business alarm set. Runs after all other build_* modules so
    every Lambda/API/table/queue is on ctx already."""
    CFG = ctx.CFG
    _obs_cfg = CFG.get("observability", {}) or {}
    alarms_cfg = _obs_cfg.get("alarms", {}) or {}

    # R9.3: default true; a deployer who explicitly opts out gets zero alarms
    # (byte-identical to a stack before this module existed, minus the SNS
    # topic).
    if not alarms_cfg.get("enabled", True):
        return

    # ── SNS: one dedicated ops topic, reused by every alarm below ─────────
    # Kept separate from `openclaw-tenant-events` (lifecycle notifications):
    # ops alarms and tenant lifecycle events have different subscribers.
    topic = sns.Topic(
        self,
        "AlarmsTopic",
        topic_name="openclaw-alarms",
        display_name="OpenClaw Business Alarms",
    )
    action = cw_actions.SnsAction(topic)

    def _add_action(alarm: cloudwatch.Alarm) -> cloudwatch.Alarm:
        alarm.add_alarm_action(action)
        return alarm

    # ── Per-function Lambda Errors + Throttles ────────────────────────────
    # Pull every runtime Lambda that lands on ctx. Skipped intentionally:
    #   · cb_start_fn / cb_done_fn (compute.py) — CFN custom-resource
    #     one-shots for the golden-image CodeBuild waiter; deploy-time only.
    #   · _ptg_attach_fn (auth.py) — one-shot UpdateUserPool custom resource.
    #   · tools_fn (ha_edge.py) — AgentCore demo tool (hello/system_info),
    #     not a control-plane path.
    # Conditional Lambdas (audit_archive/authorizer/console_bff/pretokengen/
    # lifecycle_consumer) are alarmed only when the config enables them.
    lambda_fns = [
        ("Api", ctx.api_fn),
        ("Health", getattr(ctx, "health_fn", None)),
        ("Scaler", getattr(ctx, "scaler_fn", None)),
        ("Backup", getattr(ctx, "backup_fn", None)),
        ("Skills", getattr(ctx, "skills_fn", None)),
        ("Templates", getattr(ctx, "templates_fn", None)),
        ("AuditArchive", getattr(ctx, "audit_archive_fn", None)),
        ("Authorizer", getattr(ctx, "authorizer_fn", None)),
        ("LifecycleConsumer", getattr(ctx, "lifecycle_consumer", None)),
        ("ConsoleBff", getattr(ctx, "console_bff_fn", None)),
        ("PreTokenGen", getattr(ctx, "pretokengen_fn", None)),
    ]
    lambda_period = Duration.minutes(_cfg(alarms_cfg, "lambda_period_minutes"))
    lambda_eval = int(_cfg(alarms_cfg, "lambda_evaluation_periods"))
    for label, fn in lambda_fns:
        if fn is None:
            continue
        _add_action(
            cloudwatch.Alarm(
                self,
                f"LambdaErrors{label}Alarm",
                alarm_name=f"openclaw-lambda-errors-{fn.function_name}",
                metric=fn.metric_errors(period=lambda_period, statistic="Sum"),
                threshold=int(_cfg(alarms_cfg, "lambda_error_threshold")),
                evaluation_periods=lambda_eval,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    f"Lambda {fn.function_name} recorded errors — check its log "
                    f"group and X-Ray traces (R9.1 Business_Alarm_Set)."
                ),
            )
        )
        _add_action(
            cloudwatch.Alarm(
                self,
                f"LambdaThrottles{label}Alarm",
                alarm_name=f"openclaw-lambda-throttles-{fn.function_name}",
                metric=fn.metric_throttles(period=lambda_period, statistic="Sum"),
                threshold=int(_cfg(alarms_cfg, "lambda_throttle_threshold")),
                evaluation_periods=lambda_eval,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    f"Lambda {fn.function_name} hit concurrency throttle — burst "
                    f"exceeded reserved concurrency (R9.1 Business_Alarm_Set)."
                ),
            )
        )

    # ── API Gateway 5XX ───────────────────────────────────────────────────
    api = getattr(ctx, "api", None)
    if api is not None:
        _add_action(
            cloudwatch.Alarm(
                self,
                "ApiGateway5XXAlarm",
                alarm_name="openclaw-apigw-5xx",
                metric=cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="5XXError",
                    dimensions_map={
                        "ApiName": api.rest_api_name,
                        "Stage": api.deployment_stage.stage_name,
                    },
                    statistic="Sum",
                    period=Duration.minutes(_cfg(alarms_cfg, "api_5xx_period_minutes")),
                ),
                threshold=int(_cfg(alarms_cfg, "api_5xx_threshold")),
                evaluation_periods=int(_cfg(alarms_cfg, "api_5xx_evaluation_periods")),
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    "API Gateway openclaw-orchestrator returning 5XX above "
                    "threshold — control-plane request failures (R9.1)."
                ),
            )
        )

    # ── SQS lifecycle queue: ApproximateAgeOfOldestMessage ────────────────
    # DLQ non-empty is already alarmed in lambdas.py:LifecycleDlqAlarm; we
    # add the "queue is backing up" signal here. Only when the queue exists
    # (scaler.lifecycle_queue_enabled=true).
    lifecycle_queue = getattr(ctx, "lifecycle_queue", None)
    if lifecycle_queue is not None:
        _add_action(
            cloudwatch.Alarm(
                self,
                "LifecycleQueueOldestMessageAgeAlarm",
                alarm_name="openclaw-lifecycle-queue-oldest-message-age",
                metric=lifecycle_queue.metric_approximate_age_of_oldest_message(
                    period=Duration.minutes(
                        _cfg(alarms_cfg, "sqs_oldest_age_period_minutes")
                    ),
                    statistic="Maximum",
                ),
                threshold=int(_cfg(alarms_cfg, "sqs_oldest_age_seconds")),
                evaluation_periods=int(
                    _cfg(alarms_cfg, "sqs_oldest_age_evaluation_periods")
                ),
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    "openclaw-lifecycle.fifo has messages older than the "
                    "threshold — the consumer is falling behind or wedged (R9.1)."
                ),
            )
        )

    # ── DDB ThrottledRequests on tenants and hosts tables ─────────────────
    ddb_period = Duration.minutes(_cfg(alarms_cfg, "ddb_throttle_period_minutes"))
    ddb_eval = int(_cfg(alarms_cfg, "ddb_throttle_evaluation_periods"))
    ddb_threshold = int(_cfg(alarms_cfg, "ddb_throttle_threshold"))
    for label, table in (
        ("Tenants", getattr(ctx, "tenants_table", None)),
        ("Hosts", getattr(ctx, "hosts_table", None)),
    ):
        if table is None:
            continue
        # Aggregate ThrottledRequests across all operations. R9.1 asks for a
        # single "table is throttling" signal, not per-op breakdown. The CDK
        # helper table.metric_throttled_requests() is deprecated ("returns an
        # invalid metric"); use the raw CloudWatch metric with only TableName
        # so it aggregates across every operation.
        _add_action(
            cloudwatch.Alarm(
                self,
                f"Ddb{label}ThrottlesAlarm",
                alarm_name=f"openclaw-ddb-throttles-{table.table_name}",
                metric=cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ThrottledRequests",
                    dimensions_map={"TableName": table.table_name},
                    period=ddb_period,
                    statistic="Sum",
                ),
                threshold=ddb_threshold,
                evaluation_periods=ddb_eval,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    f"DynamoDB {table.table_name} throttled requests > "
                    f"{ddb_threshold} — provisioned capacity or partition hot "
                    f"key (R9.1 Business_Alarm_Set)."
                ),
            )
        )

    # ── Redis 全节点: edge 路由新鲜度上界 ────────────────────────────────
    # ReplicationLag 的有效维度仍是每个节点的 CacheClusterId，不能用
    # ReplicationGroupId 替代。全部节点都出数，ha_edge.py 由 group id 加序号构造。
    redis_lag_eval = int(
        _cfg(alarms_cfg, "redis_replication_lag_evaluation_periods")
    )
    for index, cache_cluster_id in enumerate(
        getattr(ctx, "redis_node_cluster_ids", [])
    ):
        _add_action(
            cloudwatch.Alarm(
                self,
                f"RedisReplicationLagNode{index + 1}Alarm",
                alarm_name=(
                    "openclaw-edge-node-route-freshness-upper-bound-"
                    f"{index + 1}"
                ),
                metric=cloudwatch.Metric(
                    namespace="AWS/ElastiCache",
                    metric_name="ReplicationLag",
                    dimensions_map={"CacheClusterId": cache_cluster_id},
                    period=Duration.minutes(
                        _cfg(alarms_cfg, "redis_replication_lag_period_minutes")
                    ),
                    statistic="Maximum",
                ),
                threshold=float(
                    _cfg(alarms_cfg, "redis_replication_lag_threshold_seconds")
                ),
                evaluation_periods=redis_lag_eval,
                datapoints_to_alarm=redis_lag_eval,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    "仅在 edge_read_from_replica 打开时创建，覆盖复制组内每个节点；"
                    "当前 primary 正常出数且通常接近 0，角色互换后不会失明。"
                    "ReplicationLag 超阈值意味着“复制延迟 + POS_TTL_SEC”可能吃掉 "
                    "PORT_QUARANTINE_SECONDS 的余量。"
                ),
            )
        )

    # ── edge reader endpoint 回落: 机队半收敛 ────────────────────────────
    # #625 —— install-edge.sh 读 /openclaw/engine/redis/reader-endpoint 失败(调用失败 /
    # 空值 / 缺 :port)时静默回落 primary,机队会停在"一部分箱子读 reader、一部分读
    # primary"的半收敛态,而 primary 的读负载看起来像"已经分流完"。每台 edge 在安装期
    # 发一次 RedisReaderEndpointFallback:采用 SSM 值发 0、三种回落各发 1,所以 Sum>0
    # 就是"至少一台没分流到 reader"。
    #
    # 判据是两条【同时】成立,比隔壁 ReplicationLag 那组多一条:
    #   * ctx.edge_role 存在 ⟺ edge.enabled=true,即真的有实例会发这个指标;
    #   * redis_node_cluster_ids 非空 ⟺ ha_edge.py 的 `edge_read_from_replica and
    #     num_replicas > 0`,也就是 reader SSM 参数真的与 primary 不同的那一档。
    # 开关关或零副本时 reader 参数与 primary 逐字相等,"回落"没有任何后果,不建告警。
    #
    # NOT_BREACHING 是有意的:指标完全缺失既可能是机队还没起,也可能是 PutMetricData
    # 被拒(定向 promotion 不推 IAM 时会这样),两者都不该在这条告警上表现成 ALARM ——
    # 它只回答"有没有箱子回落"。指标是安装期一次性发的,所以这条告警会在下一个窗口
    # 自己 OK 回去;它响过一次就要查,不要等它持续。
    if (
        getattr(ctx, "edge_role", None) is not None
        and getattr(ctx, "redis_node_cluster_ids", [])
    ):
        _reader_fallback_eval = int(
            _cfg(alarms_cfg, "edge_reader_fallback_evaluation_periods")
        )
        _add_action(
            cloudwatch.Alarm(
                self,
                "EdgeRedisReaderFallbackAlarm",
                alarm_name="openclaw-edge-redis-reader-endpoint-fallback",
                metric=cloudwatch.Metric(
                    namespace="OpenClaw/Edge",
                    metric_name="RedisReaderEndpointFallback",
                    period=Duration.minutes(
                        _cfg(alarms_cfg, "edge_reader_fallback_period_minutes")
                    ),
                    statistic="Sum",
                ),
                threshold=float(
                    _cfg(alarms_cfg, "edge_reader_fallback_threshold")
                ),
                evaluation_periods=_reader_fallback_eval,
                datapoints_to_alarm=_reader_fallback_eval,
                comparison_operator=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=(
                    "至少一台 edge 在安装期没能采用 SSM 的 Redis reader endpoint、"
                    "回落到了 primary,机队处于半收敛态:那台箱子的路由读没有分流,"
                    "primary 的读负载比容量模型预期的高。按 Reason 维度"
                    "(ssm_error/empty/malformed)区分是权限/网络问题还是 SSM 参数值"
                    "本身畸形,再看该实例的 install-edge 日志。指标是安装期一次性"
                    "发出的,本告警会自行恢复 —— 响过即需排查。"
                ),
            )
        )

    # health_check 的中间态巡检(_reap_stuck_lifecycle)每轮发两个自定义指标到
    # OpenClaw/Lifecycle。指标【每轮都发】(包括 0),所以这里可以用 NOT_BREACHING:
    # 缺数据点意味着巡检本身没跑,那由 health_check 自己的错误告警覆盖。
    #
    # 人工发现的延迟本身就是事故」。reaper 能把卡死变成可见,但"可见"得有人看见。
    _add_action(
        cloudwatch.Alarm(
            self,
            "LifecycleStuckMarkedAlarm",
            alarm_name="openclaw-lifecycle-stuck-marked",
            metric=cloudwatch.Metric(
                namespace="OpenClaw/Lifecycle",
                metric_name="LifecycleStuckMarked",
                period=Duration.minutes(5),
                statistic="Maximum",
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "A tenant is stuck in suspending/restoring past the timeout and has "
                "been marked by health_check. It holds a per-host concurrency slot "
                "that will never free itself (#469 P4). For a stuck SUSPENDING tenant, "
                "resolve it with DELETE /tenants/{id}?force=true (permitted only for "
                "marked tenants). A stuck RESTORING tenant is NOT force-deletable: its "
                "row still points at the old host/vm while a reservation may already "
                "exist on the new host, so force-deleting would corrupt the capacity "
                "ledger — escalate instead. Inspect lifecycle_stuck_reason on the row."
            ),
        )
    )
    # 这一条比上面那条【更严重】:它意味着有租户卡着,而我们连它在 host 上的真实状态都
    # 探不到(host 不可达 / SSM 失败 / 超本轮限额)。此时既不能回滚也不能判定,人必须介入。
    # 持续非零 = 巡检对这些租户毫无判断力,是最容易被静默掉的一类可观测性缺口。
    _add_action(
        cloudwatch.Alarm(
            self,
            "LifecycleStuckUnconfirmedAlarm",
            alarm_name="openclaw-lifecycle-stuck-unconfirmed",
            metric=cloudwatch.Metric(
                namespace="OpenClaw/Lifecycle",
                metric_name="LifecycleStuckUnconfirmed",
                period=Duration.minutes(5),
                statistic="Maximum",
            ),
            threshold=0,
            # 3 个周期(15 分钟)才报:单轮探不到可能只是一次 SSM 抖动或限额,
            # 连续三轮说明是真的够不着 host,不是瞬时噪声。
            evaluation_periods=3,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "health_check found tenants stuck in suspending/restoring but could "
                "NOT determine their host-side state for 3 consecutive sweeps (host "
                "unreachable / SSM failing / over per-sweep budget). The sweep "
                "deliberately does nothing when it cannot confirm — so these tenants "
                "stay stuck AND unclassified. Check host reachability and SSM agent."
            ),
        )
    )
    # 上面两条都是 NOT_BREACHING —— 它们只在【有数据且非零】时响,所以谁来管
    # 「一个数据点都没有」?本来的答案是"health_check 自己的 Lambda 错误告警",
    # 但那条路是断的:handler.py:224 的 `except Exception` 把巡检异常降级成一行日志,
    # Lambda 正常返回 → 错误告警永不触发。再叠上这里的 NOT_BREACHING,
    # reaper 永久坏掉 / IAM 被收权 / put_metric_data 持续失败,都【零告警】。
    # (codex 独立复审第四轮指出这个洞。)
    #
    # 所以要一条【方向相反】的告警:唯一订阅 handler 那个显式心跳,并且
    # treat_missing_data=BREACHING —— 缺数据本身就是故障信号。
    # 窗口取 15 分钟(巡检 interval_minutes=5,即一窗期望 3 次心跳)× 2 个周期:
    # 单轮被 Lambda 冷启/限流/调度抖动吞掉不该报警,连续 30 分钟没有任何一次成功巡检
    # 一定是真坏了。用 Sum 而不是 Maximum:Maximum 在窗内只要有一次就满足,
    # 而 Sum<=0 才等价于"这一整窗一次都没成功"。
    _add_action(
        cloudwatch.Alarm(
            self,
            "LifecycleReaperHeartbeatAlarm",
            alarm_name="openclaw-lifecycle-reaper-heartbeat-missing",
            metric=cloudwatch.Metric(
                namespace="OpenClaw/Lifecycle",
                metric_name="LifecycleReaperHeartbeat",
                period=Duration.minutes(15),
                statistic="Sum",
            ),
            threshold=0,
            evaluation_periods=2,
            comparison_operator=(
                cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            alarm_description=(
                "The lifecycle-stuck sweep has not reported a single successful run "
                "for 30 minutes. The sweep is wrapped in a non-fatal except (see "
                "health_check/handler.py), so a permanently broken reaper, an IAM "
                "regression on cloudwatch:PutMetricData, or a failing metric emit "
                "produces NO Lambda error and NO stuck-tenant alarm — tenants keep "
                "getting stuck in suspending/restoring while every other alarm stays "
                "green. Missing data is breaching here on purpose. Check the "
                "health_check Lambda logs for 'lifecycle-stuck error (non-fatal)'."
            ),
        )
    )

    # ── Attach the alarm SNS topic to the pre-existing DLQ alarms ────────
    # emit CloudWatch state changes but had no action wired. Fanning them
    # into openclaw-alarms puts the whole business alarm set on one topic
    # without touching their creation sites.
    for node_id in ("LifecycleDlqAlarm", "DispatchDlqAlarm"):
        node = self.node.try_find_child(node_id)
        if isinstance(node, cloudwatch.Alarm):
            node.add_alarm_action(action)

    # Pack for downstream/tests.
    ctx.alarms_topic = topic
