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
        host_launch_slots: int = 30,
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
        mode = str(merged.get("mode", "push")).lower()
        if mode not in ("push", "pull", "ddb"):
            raise ValueError(
                f"dispatch.mode={merged.get('mode')!r} invalid; must be 'push', 'pull' "
                "or 'ddb' (interfaces.md L11)."
            )
        self.mode = mode

        # (SSM 超时公式分母)。【单一来源】= 调用方(compute.py)从 vm.host_launch_slots 读入传进来,
        # 校验正整数,非法(0/负/非数)fail-safe 回落 30,防两侧漂移或 migrate 抢锁死循环。
        try:
            self._launch_slots = int(host_launch_slots)
        except (TypeError, ValueError):
            self._launch_slots = 30
        if self._launch_slots < 1:
            self._launch_slots = 30

        # dev 环境 assignments 表用 DESTROY,生产由调用方覆盖为 RETAIN。默认 DESTROY 跟
        # 覆盖前必须先备份 → 我们保留调用方权限,不在这里锁死。
        removal = (
            removal_policy if removal_policy is not None else RemovalPolicy.DESTROY
        )

        # ---------- DLQ + 标准队列 ----------
        # 标准队列(非 FIFO):dispatch 一期靠"预生成 tenant_id + tenants 条件写(attribute_
        # not_exists) + 消费端认领闸(dispatch_claim CAS)"做幂等,不依赖 FIFO 的
        # MessageDeduplicationId(interfaces.md L52-53)。dlq_max_receive_count 从 cfg,默认 3。
        #
        # visibility_timeout 必须 ≥ 消费 Lambda 的 timeout(AWS SQS→Lambda 硬约束,否则
        # CFN CreateQueue/ESM 报 "visibility less than function timeout" 400)。ESM 挂在
        # 金丝雀链)。取 960 = 900 + 60s buffer:AWS 硬约束是 ≥(900 恰好合法),但官方
        # 最佳实践建议 visibility 大于 function timeout 留边界余量,避免 function 跑满
        # 900s 时消息在同一刻可见性到期被重投的竞态。消费侧 env 未显式下发:
        # clients.DISPATCH_VISIBILITY_TIMEOUT_SEC 走默认 900,cap SSM executionTimeout
        # = 900-60=840s(dispatch_service._derive_exec_timeout)≤ 960,方向安全
        # (消费侧假设的 visibility 比真实值小,只会更保守不会撞重投)。旧值
        # 300 < 900 是 bug(基础设施与消费侧不同源,dispatch.enabled=true 首次部署即 CFN 400)。
        dlq_max_receive = int(merged.get("dlq_max_receive_count", 3))
        # #522 P1-2 —— 存到 self,供 dispatch_env 注入消费端(收敛 backstop 须与队列真实
        # maxReceiveCount 同源:分两处各写死会漂 → backstop 提前误终态 / 静默 DLQ)。
        self._dlq_max_receive = dlq_max_receive
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
            visibility_timeout=Duration.seconds(960),
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
        # R10.2 — /system/queues 只读 DLQ 深度:只需 GetQueueAttributes(不收/不发)。
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sqs:GetQueueAttributes"],
                resources=[self.dlq.queue_arn],
            )
        )
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

        # 消费删租户时打下的 `vkey_revoke_failed` 标记,重试撤销 LiteLLM vkey。
        # 在 bb 上那个标记【没有任何消费者】(CHANGELOG 自己记着这条),于是回收失败的
        # 那把 key 永久留在 LiteLLM:凭据 + 预算泄漏,随 churn 累积。
        #
        # 为什么挂在 api_fn 而不是 health_check:只有 openclaw-api 的 env 带
        # LITELLM_MASTER_KEY_SECRET,health_check 一个 LiteLLM 相关的都没有 —— 放那边
        # 是一个注定空转的 reconciler。
        #
        # 为什么 15 分钟而不是 1 分钟:孤儿回收不紧急(标记稀疏、量小),低频省 invoke 成本;
        # 而重试上限 10 次 × 15 分钟 ≈ 覆盖 2.5 小时的瞬时故障,够长了。
        self.credential_reconciler_rule = events.Rule(
            self,
            "CredentialReconcilerRule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            description=(
                "#438 reclaim orphaned LiteLLM vkeys. Fires api_fn with "
                "{source:'credential.reconciler'} to consume vkey_revoke_failed "
                "markers left by best-effort revoke on tenant delete."
            ),
            targets=[
                targets.LambdaFunction(
                    api_fn,
                    event=events.RuleTargetInput.from_object(
                        {"source": "credential.reconciler"}
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

        #
        # 为什么是「缺席告警」而不是数值告警:要发现的是「poller 根本没跑」,而那种情况下
        # 连数据点都不会有 —— 任何基于数值的比较都永远不会触发。所以判据必须是
        # `treat_missing_data=BREACHING`:**没有数据 = 告警**。
        #
        # 为什么这件事在 #562 之后是必须的:死线执行者挂在同一个 poller 上,是「180s 内必进
        # 终态」这个对外承诺的唯一兜底。poller 停 10 分钟,那 10 分钟里所有过死线的租户都留在
        # creating/pending —— 承诺静默失效,而客户看到的仍然只是「还在创建中」。
        #
        # 为什么不是「加第二个定时器」:EventBridge rate(1 minute) 是 AWS 托管的 HA 调度器,
        # 不是会崩的单实例。真实失效模式(规则被误禁用 / Lambda 每拍都报错 / 被限流 / 超时)
        # 里,五分之四靠加定时器都治不了 —— 第二条规则会被同一次变更一起禁掉、第二个触发调的
        # 是同一个坏函数。它们共同的前提是「没人知道它没跑」,所以先补可发现性。
        # 详见 deploy/lambda/api/services/poller_heartbeat.py 的模块 docstring。
        #
        # 窗口取 5 分钟 / 连续 1 个周期:节拍是 1 分钟,给 4 拍的容错(单拍抖动/冷启/限流重试
        # 都不该告警),但连续 5 分钟一次都没跑成必须响 —— 那已经是 3 个死线周期。
        self.poller_heartbeat_alarm = cloudwatch.Alarm(
            self,
            "DispatchPollerHeartbeatAlarm",
            alarm_name="openclaw-dispatch-poller-stale",
            metric=cloudwatch.Metric(
                namespace="OpenClaw/Dispatch",
                metric_name="PollerHeartbeat",
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            # ★这一行是本告警的全部意义:没有数据点 = poller 没跑 = 告警。
            # 若写成 NOT_BREACHING(像 DLQ 那条那样),poller 完全停摆时它会一直显示 OK。
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            alarm_description=(
                "openclaw dispatch poller has not completed a run in 5 minutes "
                "(PollerHeartbeat missing or zero). The poller carries the #562 "
                "create-deadline enforcement — while it is down, tenants past their "
                "180s deadline stay in creating/pending and the terminal-state "
                "promise silently breaks. Check: EventBridge rule "
                "DispatchPollerRule enabled? openclaw-api errors/throttles? "
                "Lambda timeout? See services/poller_heartbeat.py."
            ),
        )

        # 心跳照发但 errors 持续非零 = 「跑了但没干成事」。这个状态长得和一切正常完全一样
        # (返回值形状正常、心跳在发),只有指标能把它和正常区分开。
        # 阈值取 0 / 连续 3 个 5 分钟周期:偶发一两个租户写失败是 #562 刻意的 fail-safe
        # (不让单个失败中断整轮),不该告警;连续 15 分钟都在吞错才是真问题。
        self.poller_errors_alarm = cloudwatch.Alarm(
            self,
            "DispatchPollerErrorsAlarm",
            alarm_name="openclaw-dispatch-poller-swallowing-errors",
            metric=cloudwatch.Metric(
                namespace="OpenClaw/Dispatch",
                metric_name="PollerErrors",
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=0,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            # 这条【不】用 BREACHING:没数据点由上面那条陈旧告警负责,
            # 两条都对缺席告警会在 poller 停摆时同时响两遍,噪声无收益。
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "openclaw dispatch poller keeps swallowing errors (PollerErrors > 0 "
                "for 15 minutes). It IS running, so the staleness alarm stays OK — "
                "but per-tenant writes keep failing inside the #562 fail-safe that "
                "deliberately does not abort the round. Check openclaw-api logs for "
                "[#562] deadline-error and DDB throttling on openclaw-tenants."
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
            # R10.2 — DLQ URL 给 /system/queues 只读深度(console DLQ 告警)
            "DISPATCH_DLQ_URL": self.dlq.queue_url,
            "DISPATCH_MODE": self.mode,
            "ASSIGNMENTS_TABLE": self.assignments_table.table_name,
            "DISPATCH_PARAM_PREFIX": _PARAM_PREFIX,
            # 字段】从 SSM /openclaw/dispatch/config 热读(见 dispatch_service._check_andon);
            # 下面这些旋钮只能改 Lambda env(update-function-configuration)后生效,不是改 SSM config。
            # 之前注释误称"改 SSM config 不重 deploy"——已更正:改这些要动 Lambda env,不重 cdk deploy 即可。
            "DISPATCH_MAX_PARALLEL": "96",  # 装箱密度(per_host_cap),一批往一台塞几个 VM
            # 须与 host 侧 OC_HOST_LAUNCH_SLOTS 同值(默认 30)。要调改这里 + ha_edge 的
            # vm.host_launch_slots 两处一起改(两侧同一物理含义,分别注入 Lambda / host)。
            "DISPATCH_HOST_LAUNCH_CONCURRENCY": str(self._launch_slots),
            "DISPATCH_INFLIGHT_TTL_SEC": "180",
            "DISPATCH_RETRY_BUDGET": "3",
            # #522 P1-2 —— 收敛 backstop 阈值,与队列 max_receive_count 同源(上面同一 cfg)。
            # 消费端到最后一次投递仍 unplaced → loud 转 requires_intervention,不静默进 DLQ。
            "DISPATCH_MAX_RECEIVE_COUNT": str(self._dlq_max_receive),
            # 秒数视为陈旧、跳过该门(fail-open)。改这两个同样是动 Lambda env(不重 cdk deploy)。
            # 0 关门。默认留一个默认 data 盘余量给新租户初始写入。
            "DISPATCH_HOST_DISK_MIN_FREE_MB": "2048",
            "DISPATCH_DISK_REPORT_TTL_SEC": "90",
        }
