# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SQS dispatch (标准队列 + 装箱消费 + 聚合 SSM) 的 CDK Construct。

**为什么单独一个 Construct**:一期 push 模式 + 二期 pull 模式的所有基础设施(队列/DLQ/
ESM/assignments 表/ParamStore 默认参数/EventBridge Poller/告警)集中在一个 self-contained
类里,stack.py 里只做一次 `DispatchInfra(self, "Dispatch", cfg=..., api_fn=..., host_role=...)`
就完事。stack.py 已经 4000+ 行,避免再散一堆 config-gated 分支;guardrail_props 是纯字典变换
(不能建 Construct),这里需要建真资源、拿真 arn,所以走 Construct 类而非 helper 函数。

**契约来源**:SPEC/specs/sqs-dispatch/interfaces.md。所有 env 名 / 队列名 / 表名 / 参数
前缀都从那里抄,任何改动先改 spec 再改这里。

**分层**:consumers/routes → services → core → clients/utils(SPEC 里 import-layers)。
本文件是 deploy/lib/ 下的 CDK 基础设施 Construct,只输出资源引用给 stack.py 注入
env 到 Lambda。装箱/聚合的运行时逻辑在 deploy/lambda/api/{core,services,consumers}/
下,由别的 agent 落地——本文件只负责建资源和 wire up,不写业务代码。

**开关矩阵**(interfaces.md 第 30 行):
- dispatch.enabled=false → 什么都不建,stack 现状不变。
- dispatch.enabled=true → 建队列/DLQ/ESM(挂到 api_fn)/assignments 表/默认 andon
  参数/Poller Rule/DLQ 告警;api_fn 拿 dispatch 队列 send+consume、assignments 表 RW、
  /openclaw/dispatch/* 参数 Put/Get/Delete;host_role 拿 /openclaw/dispatch/manifests/*
  只读 + assignments 表 RW(二期 pull 模式 host-agent 用)。
- dispatch.enabled=true 且 scaler.create_via_queue=true → stack.py 侧 synth 时
  raise ValueError(fail-loud,防两条队列都收 create → 双入队致重复起 VM)。这道闸
  由 stack.py 调 `DispatchInfra.validate_no_double_enqueue(cfg)` 触发,不放在
  __init__ 里(为了让"两开关同 true"的 synth 断言可以只测纯函数,不用 cdk.App)。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_cloudwatch as cloudwatch,
    aws_sqs as sqs,
    aws_ssm as ssm,
    Duration,
    RemovalPolicy,
)
from constructs import Construct


# 默认值(SPEC/specs/sqs-dispatch/interfaces.md 第 22-28 行 config.yml 段)。
_DEFAULTS: Dict[str, Any] = {
    "mode": "push",
    "esm_max_concurrency": 10,
    "batching_window_seconds": 2,
    "max_batch_size": 500,
    "dlq_max_receive_count": 3,
}

# 契约里定死的资源名前缀。改了要先改 SPEC + 消费端代码 + host-agent 一起改。
_QUEUE_NAME = "openclaw-dispatch"
_DLQ_NAME = "openclaw-dispatch-dlq"
_ASSIGNMENTS_TABLE_NAME = "openclaw-assignments"
_PARAM_PREFIX = "/openclaw/dispatch"
_CONFIG_PARAM_NAME = f"{_PARAM_PREFIX}/config"


def validate_no_double_enqueue(cfg: Dict[str, Any]) -> None:
    """双开关守卫(可独测的纯函数)。

    Rule (interfaces.md L30): `dispatch.enabled=true` 时 create/start 一律走
    dispatch 队列,`create_via_queue` 被忽略;两者同 true 时 fail-loud,不允许静默。

    stack.py 应在实例化 DispatchInfra 之前调用一次,拿到 ValueError 就 raise 掉
    整个 synth——防止运行时两个队列都收 create 消息、同一租户被消费两次起两 VM。

    抽成纯函数(不 import CDK)是为了让单测断言这条闸不依赖 cdk.App/Stack 的 synth,
    只测校验逻辑本身;跟 deploy/lib/guardrail_props.py 里 build_guardrail_kwargs
    的可测性理念同款。
    """
    dispatch_enabled = bool((cfg.get("dispatch", {}) or {}).get("enabled", False))
    create_via_queue = bool(
        (cfg.get("scaler", {}) or {}).get("create_via_queue", False)
    )
    if dispatch_enabled and create_via_queue:
        raise ValueError(
            "dispatch.enabled=true and scaler.create_via_queue=true both set — "
            "double-enqueue guard: create messages would land on both "
            "openclaw-dispatch (standard) and openclaw-lifecycle.fifo, causing "
            "the same tenant to be consumed twice and two VMs to spin up. "
            "Migration path (interfaces.md L30): keep dispatch.enabled=false "
            "until FIFO in-flight creates drain, then flip dispatch.enabled=true "
            "AND set create_via_queue=false in the same deploy."
        )


class DispatchInfra(Construct):
    """SQS dispatch(标准队列)+ 装箱消费的 CDK 基础设施。

    使用方式(stack.py 里):

        _dispatch_cfg = (CFG.get("dispatch", {}) or {})
        if _dispatch_cfg.get("enabled", False):
            validate_no_double_enqueue(CFG)
            dispatch = DispatchInfra(
                self, "Dispatch",
                cfg=_dispatch_cfg,
                api_fn=api_fn,
                host_role=host_role,
            )
            # dispatch.env_vars() 里的 env 一并注入 api_fn / lifecycle_consumer

    产出资源(dispatch.enabled=true 时):
      · queue        — sqs.Queue    openclaw-dispatch(标准队列,visibility_timeout 300s)
      · dlq          — sqs.Queue    openclaw-dispatch-dlq(retention 14 天)
      · assignments_table — dynamodb.Table   openclaw-assignments(PK instance_id/SK tenant_id)
      · config_param — ssm.StringParameter   /openclaw/dispatch/config(默认 "andon=ok")
      · poller_rule  — events.Rule           rate(1 minute) → api_fn
      · dlq_alarm    — cloudwatch.Alarm      DLQ ApproximateNumberOfMessagesVisible>0
      · IAM 挂载给 api_fn / host_role(见 __init__ 里对应段)
      · ESM(SqsEventSource)挂到 api_fn(报 batch item failures,ScalingConfig maxConcurrency)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: Dict[str, Any],
        api_fn: _lambda.IFunction,
        host_role: iam.IRole,
        removal_policy: Optional[RemovalPolicy] = None,
    ) -> None:
        super().__init__(scope, construct_id)

        # 契约默认值(cfg 只覆盖显式给的),别在这里一层层 .get 免得读值和 SPEC 漂移。
        merged: Dict[str, Any] = {
            **_DEFAULTS,
            **{k: v for k, v in cfg.items() if v is not None},
        }

        # 校验:mode 只允许 push|pull|ddb(spec 契约)。fail-loud 优于 synth 出错误资源。
        # push=聚合 SSM+ParamStore 分片(回退);pull=host-agent 轮询自取(二期);
        # ddb=写 assignments 表 + --from-ddb SSM 叫醒(一期默认载体,#73)。
        mode = str(merged.get("mode", "push")).lower()
        if mode not in ("push", "pull", "ddb"):
            raise ValueError(
                f"dispatch.mode={merged.get('mode')!r} invalid; must be 'push', 'pull' "
                "or 'ddb' (interfaces.md L11)."
            )
        self.mode = mode

        # dev 环境 assignments 表用 DESTROY,生产由调用方覆盖为 RETAIN。默认 DESTROY 跟
        # SPEC L105(RemovalPolicy DESTROY(dev))对齐。删表算不可逆(铁律#4),生产
        # 覆盖前必须先备份 → 我们保留调用方权限,不在这里锁死。
        removal = (
            removal_policy if removal_policy is not None else RemovalPolicy.DESTROY
        )

        # ---------- DLQ + 标准队列 ----------
        # 标准队列(非 FIFO):dispatch 一期靠"预生成 tenant_id + tenants 条件写(attribute_
        # not_exists) + 消费端认领闸(dispatch_claim CAS)"做幂等,不依赖 FIFO 的
        # MessageDeduplicationId(interfaces.md L52-53)。visibility_timeout 300s 给聚合
        # SSM 命令留足推导 executionTimeout 空间(SPEC L118 要求 executionTimeout ≤
        # visibility - 60s);dlq_max_receive_count 从 cfg,默认 3。
        dlq_max_receive = int(merged.get("dlq_max_receive_count", 3))
        self.dlq = sqs.Queue(
            self,
            "DispatchDLQ",
            queue_name=_DLQ_NAME,
            retention_period=Duration.days(14),
        )
        self.queue = sqs.Queue(
            self,
            "DispatchQueue",
            queue_name=_QUEUE_NAME,
            visibility_timeout=Duration.seconds(300),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=dlq_max_receive, queue=self.dlq
            ),
        )

        # ---------- Assignments 表(pull 模式二期用;push 模式也建,便于开关滚动切换) ----------
        # PK instance_id / SK tenant_id(SPEC L105-106)。PAY_PER_REQUEST(dispatch 洪峰
        # 是突发,provisioned throughput 反而更贵)。TTL 属性名 "ttl"(item 里存
        # created+24h epoch),让 DDB 自动清完 assignments(host-agent 幂等复位靠 vm.json,
        # DDB 只是任务派单板,不需要长期保留)。
        self.assignments_table = dynamodb.Table(
            self,
            "AssignmentsTable",
            table_name=_ASSIGNMENTS_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="instance_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="tenant_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=removal,
            time_to_live_attribute="ttl",
        )

        # ---------- ParamStore 默认 andon 参数 ----------
        # SPEC L32:CDK StringParameter 托管 andon,默认值 "andon=ok",参数永不缺失
        # (防首启读空被憋死)。运维改 andon=stop 用 aws ssm put-parameter --overwrite
        # 直接改,不走 CDK 更新(想改 andon=stop 就是紧急急停,不该等 stack update)。
        self.config_param = ssm.StringParameter(
            self,
            "DispatchConfigParam",
            parameter_name=_CONFIG_PARAM_NAME,
            string_value="andon=ok",
            description=(
                "openclaw dispatch runtime config (kv-line format). "
                "andon=ok|stop — 'stop' halts SendCommand/BatchWriteItem before "
                "every op (spec L32). Managed by CDK with a default so the "
                "consumer never fails-closed on cold start; runtime edits are "
                "expected to be done out-of-band via aws ssm put-parameter."
            ),
        )

        # ---------- ESM: SQS → api_fn ----------
        # api_fn 的 handler.lambda_handler 里已按 eventSourceARN 分派;SPEC L128 要求
        # ARN 里含 "openclaw-dispatch" 就走 consumers.dispatch。
        #
        # 关键参数(interfaces.md 契约):
        # · batch_size = max_batch_size (默认 500)——SQS Lambda ESM 上限 10000,标准
        #   队列可以吃到 10000,但装箱一批太大会撞 executionTimeout 推导上限
        #   (SPEC L118),保守 500 作默认。
        # · max_batching_window = batching_window_seconds(默认 2s)——攒批降 Lambda
        #   冷启和 SSM 每命令开销。
        # · report_batch_item_failures = True——装箱部分失败时只回队 unplaced,
        #   已放下的租户 ack 掉(SPEC L93 的 batchItemFailures 契约必需)。
        # · ScalingConfig maxConcurrency = esm_max_concurrency(默认 10)——治
        #   "consumer 打爆 SSM/PutParameter" 的核心限流阀,同时也守护 andon 急停时
        #   不会因队列积压瞬间起 100 个 consumer 实例把 andon read 拖挂。
        max_conc = int(merged.get("esm_max_concurrency", 10))
        batch_window = int(merged.get("batching_window_seconds", 2))
        batch_size = int(merged.get("max_batch_size", 500))
        # 参数合规性(fail-loud 比 synth 后 CFN 报错更好定位)
        if not (2 <= max_conc <= 1000):
            raise ValueError(
                f"dispatch.esm_max_concurrency={max_conc} out of range 2..1000 "
                "(SQS Lambda ESM ScalingConfig hard limits)."
            )
        if not (0 <= batch_window <= 300):
            raise ValueError(
                f"dispatch.batching_window_seconds={batch_window} out of 0..300"
            )
        if not (1 <= batch_size <= 10000):
            raise ValueError(f"dispatch.max_batch_size={batch_size} out of 1..10000")

        # NOTE: aws-cdk-lib 2.x 的 SqsEventSource 不直接暴露 ScalingConfig kwarg
        # (会随版本而变),这里用 add_event_source_mapping 更稳。add_event_source
        # 会把资源挂在 api_fn 上,不额外造 Function/Role,不打破 stack.py 里已经修好
        # 的 API GW ↔ api_fn circular 修复。
        self.event_source_mapping = api_fn.add_event_source_mapping(
            "DispatchQueueEsm",
            event_source_arn=self.queue.queue_arn,
            batch_size=batch_size,
            max_batching_window=Duration.seconds(batch_window),
            report_batch_item_failures=True,
            enabled=True,
        )
        # ScalingConfig maxConcurrency 由 L1 override 落 CFN 属性(L2 未直传):这是
        # SQS ESM 特有 config,不加就是"Lambda 会按 batch 数任意并发消费",直接把
        # SSM SendCommand 和 PutParameter 打爆。CFN 属性名 ScalingConfig 是官方稳定
        # 契约(FunctionScalingConfig),用 add_property_override 精确落。
        _cfn_esm = self.event_source_mapping.node.default_child
        if _cfn_esm is not None:
            _cfn_esm.add_property_override(
                "ScalingConfig", {"MaximumConcurrency": max_conc}
            )

        # ---------- IAM: api_fn 需要 dispatch 队列 send/consume + assignments 表 RW
        # + /openclaw/dispatch/* 参数 Put/Get/Delete + ssm SendCommand/GetCommandInvocation ----------
        # 注意:api_fn 的 SSM SendCommand 权限已在 stack.py _attach_shared_policies 里
        # 挂上(带 host tag 条件的收窄版本),我们这里"复用不复挂"——只加 dispatch 特有的
        # 参数/表/队列权限,SSM SendCommand 依赖 stack.py 已经收窄过的那份 policy。
        # 消费端 send 是"重入队 unplaced/失败重试"用(SPEC L93/L119);grant_consume 是 ESM 必需。
        self.queue.grant_send_messages(api_fn)
        self.queue.grant_consume_messages(api_fn)
        self.assignments_table.grant_read_write_data(api_fn)
        # ParamStore 权限收窄到 dispatch 前缀。SecureString manifest 需要
        # PutParameter/GetParameter/DeleteParameter,config 也共享该前缀。
        # PutParameter overwrite=True 是同一 API,不用额外权限。
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:PutParameter",
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:GetParametersByPath",
                    "ssm:DeleteParameter",
                    "ssm:DeleteParameters",
                    "ssm:AddTagsToResource",
                ],
                resources=[
                    f"arn:aws:ssm:{cdk.Stack.of(self).region}:"
                    f"{cdk.Stack.of(self).account}:parameter{_PARAM_PREFIX}/*",
                    f"arn:aws:ssm:{cdk.Stack.of(self).region}:"
                    f"{cdk.Stack.of(self).account}:parameter{_PARAM_PREFIX}",
                ],
            )
        )
        # DescribeParameters 不支持 resource-level,与 stack.py 已有 SSM 读只读 policy
        # 同款处理:单列一条 * 的低权限只读 statement,爆炸半径低。
        api_fn.add_to_role_policy(
            iam.PolicyStatement(actions=["ssm:DescribeParameters"], resources=["*"])
        )

        # ---------- IAM: host_role 需要 /openclaw/dispatch/manifests/* 只读 + assignments 表 RW ----------
        # host-agent(deploy/userdata/host-agent.py 二期)拉自己的 assignments、
        # push 模式下 launch-all-vms.sh 拉 manifest。host_role 已有 hosts_table RW +
        # /openclaw/* SSM 只读,这里"补上 assignments RW"和"确保 manifest 前缀能 get-decrypt"
        # (SecureString 需 ssm:GetParameter,虽然 /openclaw/* 已覆盖,把权限写在 dispatch
        # 附近方便运维追溯;资源不重叠地做加法,不会有旧权限被替换的风险)。
        self.assignments_table.grant_read_write_data(host_role)
        host_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{cdk.Stack.of(self).region}:"
                    f"{cdk.Stack.of(self).account}:parameter{_PARAM_PREFIX}/manifests/*",
                ],
            )
        )

        # ---------- EventBridge Poller: rate(1 minute) → api_fn ----------
        # SPEC L119:Poller 扫 hosts 有 dispatch_inflight 的、GetCommandInvocation,
        # 走 api_fn 的 `_poll_dispatch_commands` 路由(payload {"source":"dispatch.poller"})。
        # Rule 里带 CFG payload,handler.py 里已有 event router(参考 batch 自调 pattern)。
        self.poller_rule = events.Rule(
            self,
            "DispatchPollerRule",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            description=(
                "Poll dispatch in-flight SSM commands (interfaces.md L119). "
                "Fires api_fn with {source:'dispatch.poller'} to scan hosts "
                "table for dispatch_inflight tokens and reconcile."
            ),
            targets=[
                targets.LambdaFunction(
                    api_fn,
                    event=events.RuleTargetInput.from_object(
                        {"source": "dispatch.poller"}
                    ),
                )
            ],
        )

        # ---------- CloudWatch Alarm: DLQ 出现任何消息就告警 ----------
        # SPEC 隐含要求(interfaces.md L114-115 熔断 + DLQ):消费端连续失败会走 DLQ,
        # 任何一条进 DLQ 都得告警(不像 lifecycle 队列的 DLQ 有正常清理路径,dispatch
        # DLQ 是"消费端认领后彻底放弃"的死信,一进就得人介入)。
        self.dlq_alarm = cloudwatch.Alarm(
            self,
            "DispatchDlqAlarm",
            alarm_name="openclaw-dispatch-dlq-not-empty",
            metric=self.dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1),
                statistic="Maximum",
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=(cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "openclaw-dispatch-dlq has a message — dispatch consumer "
                "gave up after retries. Investigate: hosts table state, "
                "SSM RunCommand history for the command_id, and Lambda logs."
            ),
        )

        # ---------- CloudFormation Outputs (便于运维/测试快查资源坐标) ----------
        cdk.CfnOutput(
            self,
            "DispatchQueueUrl",
            value=self.queue.queue_url,
            description="SQS standard queue for dispatch (create/start).",
        )
        cdk.CfnOutput(
            self,
            "DispatchDlqUrl",
            value=self.dlq.queue_url,
            description="Dead-letter queue for openclaw-dispatch.",
        )
        cdk.CfnOutput(
            self,
            "AssignmentsTableName",
            value=self.assignments_table.table_name,
            description=(
                "DynamoDB table for host-agent pull-mode assignments (二期). "
                "PK=instance_id, SK=tenant_id, TTL=ttl."
            ),
        )
        cdk.CfnOutput(
            self,
            "DispatchConfigParamName",
            value=self.config_param.parameter_name,
            description=(
                "SSM param name for dispatch runtime config (andon). "
                "put-parameter --overwrite --value 'andon=stop' for emergency halt."
            ),
        )

    def env_vars(self) -> Dict[str, str]:
        """契约 env 名(SPEC/specs/sqs-dispatch/interfaces.md 第 6-17 行)。

        调用方在 stack.py 里把这个 dict merge 进 api_fn 和 lifecycle_consumer 的
        environment。DISPATCH_QUEUE_URL 空=功能关(消费端 handler 分派靠 eventSourceARN,
        不需要读这个 env;这里给 URL 主要给产端 handler 入队用)。
        """
        return {
            "DISPATCH_QUEUE_URL": self.queue.queue_url,
            "DISPATCH_MODE": self.mode,
            "ASSIGNMENTS_TABLE": self.assignments_table.table_name,
            "DISPATCH_PARAM_PREFIX": _PARAM_PREFIX,
            # 下面三个是运行时可调的默认值,契约默认写死在这里,想改运行时行为
            # 应直接改 SSM /openclaw/dispatch/config,不重 deploy。
            "DISPATCH_MAX_PARALLEL": "96",
            "DISPATCH_INFLIGHT_TTL_SEC": "180",
            "DISPATCH_RETRY_BUDGET": "3",
        }
