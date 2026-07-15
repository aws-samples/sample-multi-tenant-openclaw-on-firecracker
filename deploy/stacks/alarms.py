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

    # ── Attach the alarm SNS topic to the pre-existing DLQ alarms ────────
    # LifecycleDlqAlarm and DispatchDlqAlarm were created before #220 and
    # emit CloudWatch state changes but had no action wired. Fanning them
    # into openclaw-alarms puts the whole business alarm set on one topic
    # without touching their creation sites.
    for node_id in ("LifecycleDlqAlarm", "DispatchDlqAlarm"):
        node = self.node.try_find_child(node_id)
        if isinstance(node, cloudwatch.Alarm):
            node.add_alarm_action(action)

    # Pack for downstream/tests.
    ctx.alarms_topic = topic
