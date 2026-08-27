# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json as _json
import sys as _sys
from pathlib import Path as _Path

import aws_cdk as cdk
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_logs as logs,
    aws_sns as sns,
    aws_wafv2 as wafv2,
    aws_sqs as sqs,
    aws_ssm as ssm,
    aws_secretsmanager as secretsmanager,
    aws_lambda_event_sources as lambda_event_sources,
    BundlingOptions,
    BundlingFileAccess,
    Duration,
    Fn,
    RemovalPolicy,
)

from stacks._helpers import _build_vpc, _sam_build_image_for_host, _read_pyproject_version

# #564 G5 —— 死线口径模块(env 名与七档操作的单一真相)。import 真模块而不是在 CDK 里
# 复制一份字符串拼接:两边各拼一次就会出现「注入了 A、代码读 B」这种静默失效。
# 同款做法与理由见 `scripts/checks/create-deadline-config.py:65`。
# 它是纯函数 + 零 boto3,synth 期 import 无副作用(导入期那两条 assert 只校验算术)。
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "lambda" / "api"))
import core.create_deadline as _create_deadline  # noqa: E402

# #565 G5 —— host 侧 SSM agent 的单机命令 worker 上限。**这是 host 上的事实,不是本栈的
# 选择**:真值由 `deploy/userdata/init-host.sh` step1c 写进
# `/etc/amazon/ssm/amazon-ssm-agent.json` 的 `Mds.CommandWorkersLimit`。两个数分处 shell
# 与 Python、共享不了常量,所以 `tests/test_565_g5_agent_worker_parity.py` 双向钉着它们相等。
#
# **为什么要在 CDK 侧留一份副本**:Codex 独立复审 2026-08-25 指出,原先那条 parity 只比
# 「代码默认值 ≤ worker 上限」,而部署真正用的是 `config.yml` 里的值 —— 在那里写 30 既不会
# 让测试红也不会让 synth 红,于是 ESM 会送 30 个并发进 20 个 worker 的 host,请求堆在 agent
# 前面排队,**而排队时间算在客户死线里**(表现成「到点判失败」而不是「变慢」)。
# 下面那条 `_lc_max_conc > _HOST_SSM_COMMAND_WORKERS` 的 fail-loud 把这条路也堵上。
_HOST_SSM_COMMAND_WORKERS = 20


def build_lambdas(self, ctx):
    """Build lambdas resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    _pitr_spec = getattr(ctx, "_pitr_spec", None)
    assets_bucket = getattr(ctx, "assets_bucket", None)
    audit_archive_bucket = getattr(ctx, "audit_archive_bucket", None)
    audit_archive_cmk = getattr(ctx, "audit_archive_cmk", None)
    audit_archive_enabled = getattr(ctx, "audit_archive_enabled", None)
    audit_cfg = getattr(ctx, "audit_cfg", None)
    audit_retention_days = getattr(ctx, "audit_retention_days", None)
    audit_table = getattr(ctx, "audit_table", None)
    backup_bucket = getattr(ctx, "backup_bucket", None)
    backup_cmk = getattr(ctx, "backup_cmk", None)
    batch_jobs_table = getattr(ctx, "batch_jobs_table", None)
    clawpool_cmk = getattr(ctx, "clawpool_cmk", None)
    clawpool_rsa_cmk = getattr(ctx, "clawpool_rsa_cmk", None)
    groups_table = getattr(ctx, "groups_table", None)
    hosts_table = getattr(ctx, "hosts_table", None)
    param_registry_table = getattr(ctx, "param_registry_table", None)
    recipient_keys_table = getattr(ctx, "recipient_keys_table", None)
    tenant_idp_table = getattr(ctx, "tenant_idp_table", None)
    version_snapshots_table = getattr(ctx, "version_snapshots_table", None)  # #217 V2
    image_jobs_table = getattr(ctx, "image_jobs_table", None)  # #394 step1 pull Job
    tenant_secrets_table = getattr(ctx, "tenant_secrets_table", None)
    tenant_stats_table = getattr(ctx, "tenant_stats_table", None)
    tenants_table = getattr(ctx, "tenants_table", None)
    tenant_stats_enabled = bool(
        (CFG.get("tenant_stats", {}) or {}).get("enabled", False)
    )

    # ========== Lambda Shared Policy ==========
    #
    # Issue #62(档 B,人工评审):IAM 收窄。原来 SendCommand /
    # TerminateInstances / Describe* 全通配 resources=["*"],跟审计
    # baseline 冲突(WI-E/M-7)。收窄按爆炸半径切三块:
    #
    #   1. ssm:SendCommand(可写路径)拆两条 statement:
    #        · document ARN(AWS-RunShellScript)— 不能带 aws:ResourceTag
    #          条件,document 资源身上不打这两个 tag,条件求值失败会全链路
    #          AccessDenied 卡死 create/terminate;
    #        · instance ARN — 带 aws:ResourceTag/Project=openclaw +
    #          aws:ResourceTag/Role=metal-host 条件,只允许对 ASG 打出的
    #          host 发命令。tag key/value 与 LaunchTemplate 里 TagSpecifications
    #          (stack.py:_host_tags)字面一致,拼错 → AccessDenied。
    #
    #   2. ec2:TerminateInstances(不可逆)单独一条,resources=instance ARN,
    #      带同款 aws:ResourceTag 条件——只能杀自己起的 metal host,不能
    #      误伤同账号别的 EC2。
    #
    #   3. 只读 List/Describe(ssm:GetCommandInvocation /
    #      ec2:DescribeInstances / ec2:DescribeInstanceTypes)—— 这三个 API
    #      多不支持资源级 IAM(SDK 校验时会拒绝带 ARN 的 resources),保留
    #      resources=["*"] 单列一条只读 statement,爆炸半径低。
    #
    # 防错:test_stack.py 里 synth 断言 tag key/value 与
    # LaunchTemplate TagSpecifications 一致(见 TestIamNarrowing),防两处
    # 漂移(改一处忘改另一处 → AccessDenied)。
    _host_tag_conditions = {
        "StringEquals": {
            "aws:ResourceTag/Project": "openclaw",
            "aws:ResourceTag/Role": "metal-host",
        }
    }
    _ssm_document_arn = f"arn:aws:ssm:{self.region}::document/AWS-RunShellScript"
    _ec2_instance_arn_wildcard = f"arn:aws:ec2:{self.region}:{self.account}:instance/*"
    # SSM SendCommand(可写)— 两条:document(无 tag 条件) + instance(tag 条件)
    ssm_send_document_policy = iam.PolicyStatement(
        actions=["ssm:SendCommand"],
        resources=[_ssm_document_arn],
    )
    ssm_send_instance_policy = iam.PolicyStatement(
        actions=["ssm:SendCommand"],
        resources=[_ec2_instance_arn_wildcard],
        conditions=_host_tag_conditions,
    )
    # SSM 只读回读 — 两个 action 都不支持资源级 IAM,保留 *
    #
    # ListCommandInvocations 此前没授:于是 _collect()(egress_admin_service / fleet_power
    # 等 wait=true 路径共用的逐机回收器)每一轮 list 都 AccessDenied,而它那里是裸
    # `except: continue` → 安静地轮询到 deadline、返回空列表。调用方看到的是
    # "没有任何 invocation 结果",与"命令确实没跑"不可区分。#603 在新加坡真机上撞出
    # (GET /hosts/egress/chain 每次返 INCONCLUSIVE),定位靠的是手工重放 SSM + 逐条扫
    # role 的 inline/managed 策略(溢出到 managed 的 grant 只扫 inline 会漏)。
    # 授予后 wait=true 才真的能拿到逐机 apply_exit / rules_sha256。
    ssm_readonly_policy = iam.PolicyStatement(
        actions=["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
        resources=["*"],
    )
    # 组合出兼容旧接口的 ssm_policy(变成 3 条 statement 的元组用法不方便,
    # 直接改成"多 statement 数组",调用点循环 add_to_role_policy 一次挂全部)
    ssm_policy_statements = [
        ssm_send_document_policy,
        ssm_send_instance_policy,
        ssm_readonly_policy,
    ]
    # EC2 TerminateInstances(不可逆)— 单独一条,带 tag 条件
    ec2_terminate_policy = iam.PolicyStatement(
        actions=["ec2:TerminateInstances"],
        resources=[_ec2_instance_arn_wildcard],
        conditions=_host_tag_conditions,
    )
    # EC2 Describe*(只读)— 不支持资源级 IAM,保留 *
    ec2_describe_policy = iam.PolicyStatement(
        actions=[
            "ec2:DescribeInstances",
            "ec2:DescribeInstanceTypes",
        ],
        resources=["*"],
    )
    ec2_policy_statements = [
        ec2_terminate_policy,
        ec2_describe_policy,
    ]

    def _attach_ssm_policies(fn):
        """帮助函数:把 SSM 收窄后的多条 statement 挂到 Lambda role。

        SSM SendCommand 被拆成 document+instance 两条(带/不带 tag 条件),
        外加只读 GetCommandInvocation 一条;共 3 条 statement。健康检查/
        scaler/backup 只需要 SSM(不 terminate 实例),用这个 helper。
        """
        for _st in ssm_policy_statements:
            fn.add_to_role_policy(_st)

    def _attach_shared_policies(fn):
        """帮助函数:把 ssm/ec2 收窄后的多条 statement 一次挂到 Lambda role。

        替代旧的 fn.add_to_role_policy(ssm_policy)/fn.add_to_role_policy(ec2_policy)
        单条形式;api_fn 和 lifecycle_consumer 需要完整 SSM + EC2(含 Terminate)。
        """
        _attach_ssm_policies(fn)
        for _st in ec2_policy_statements:
            fn.add_to_role_policy(_st)

    # ========== SNS Lifecycle Notifications (issue #13, optional) ==========
    notif_cfg = CFG.get("notifications", {}) or {}
    notifications_topic = None
    notifications_topic_arn = ""
    if notif_cfg.get("enabled", False):
        notifications_topic = sns.Topic(
            self,
            "TenantEvents",
            topic_name="openclaw-tenant-events",
            display_name="OpenClaw Tenant Lifecycle Events",
        )
        notifications_topic_arn = notifications_topic.topic_arn

    # Go-live A1: external-authz HMAC secret. When external_authz.enabled and
    # a Secrets Manager secret name is configured, pass a CFN dynamic
    # reference so the plaintext never appears in the synthesized template;
    # else empty (handler treats empty secret as "not configured" → 503).
    _ext_authz_cfg = CFG.get("external_authz", {}) or {}
    _ext_authz_secret_name = _ext_authz_cfg.get("secret_name", "")
    if _ext_authz_cfg.get("enabled", False) and _ext_authz_secret_name:
        _external_authz_secret_ref = (
            f"{{{{resolve:secretsmanager:{_ext_authz_secret_name}:SecretString}}}}"
        )
    else:
        _external_authz_secret_ref = ""

    # ========== SQS Dispatch(标准队列+装箱)双开关守卫(fail-loud) ==========
    # SPEC/specs/sqs-dispatch/interfaces.md L30:dispatch.enabled=true 时
    # create/start 一律走 dispatch 标准队列;两者同 true → synth 直接 raise,
    # 防止同一 create 消息同时落 dispatch(std) 和 lifecycle(fifo) 队列被
    # 消费两次起两个 VM。守卫抽在 deploy/lib/dispatch_infra.py 里可独测。
    from lib.dispatch_infra import validate_no_double_enqueue

    validate_no_double_enqueue(CFG)

    # ========== API Lambda ==========
    # 控制面重构阶段1 — lifecycle SQS 队列 + DLQ(削峰)。config-gated:
    # scaler.lifecycle_queue_enabled=true 时建队列并把 URL 注入 api Lambda,
    # 启用异步入队路径(治同步直驱 SSM 的雪崩,见 DESIGN-控制面重构)。默认关
    # → 不建队列、API 走原同步路径(向后兼容)。
    _lifecycle_q_enabled = bool(
        CFG.get("scaler", {}).get("lifecycle_queue_enabled", False)
    )
    # #564 G6 —— maxReceiveCount 的**单一来源**。
    #
    # 消费侧要知道"这是不是最后一次投递"(最后一次失败后消息就进 DLQ,而进 DLQ 之前必须先
    # 把租户回写成终态,否则 DLQ 里那条消息就是唯一记录、租户永远卡在中间态)。原来这个 5
    # 在本文件里手写了**三处**且互不同源:队列的 `max_receive_count`、上面 :237 的注释、
    # 下面 :260 告警文案里的 "after 5 receives"。再让消费侧抄第四份,就是让「backstop 提前
    # 误终态 / 静默进 DLQ」这两个方向的漂移都变成必然 —— dispatch 侧
    # (`dispatch_infra.py:188-191`)的注释逐字记了同一件事,它的处置是把值存进一个变量再
    # 分发到队列与消费侧 env。这里照同一形态办。
    #
    # **为什么不额外加一个 `config.yml` 键**(dispatch 侧那条链是从 config 起的):这个数没有
    # 任何"每部署不同"的需求,加一个没人要求可调的键属于投机性灵活度;而 plan 的要求是
    # 「消费侧不许硬写 5」,CDK 已经拥有这个值(它建 RedrivePolicy 用的就是它),所以 CDK
    # 就是那个单一来源。将来真需要 per-deployment 可调,从 CFG 读一行即可。
    #
    # 也**不在运行时读队列的 RedrivePolicy**:那是在热路径上加一次 `get_queue_attributes`,
    # 等于给每批消息加一次可被节流的 AWS 调用 —— #573 刚为同类事故打过补丁。
    _LIFECYCLE_MAX_RECEIVE = 5

    lifecycle_dlq = None
    lifecycle_queue = None
    if _lifecycle_q_enabled:
        # FIFO so per-tenant lifecycle ops stay ORDERED and DEDUPED. Why FIFO:
        # ① create/stop/start of the SAME tenant must not race or reorder (a
        #    stop landing before its create, or two creates from a double-click
        #    spinning two VMs); ② exactly-once-ish via MessageDeduplicationId =
        #    tenant_id:action (enqueue_lifecycle already sets it for .fifo
        #    queues). Parallelism is preserved by MessageGroupId = tenant_id:
        #    DIFFERENT tenants are different groups and consume concurrently
        #    (up to the consumer's reserved concurrency), so a 380-create burst
        #    is NOT serialized — only same-tenant ops are. A FIFO queue's DLQ
        #    must also be FIFO.
        lifecycle_dlq = sqs.Queue(
            self,
            "LifecycleDLQ",
            queue_name="openclaw-lifecycle-dlq.fifo",
            fifo=True,
            content_based_deduplication=True,
            retention_period=Duration.days(14),
        )
        # #469 P6 —— lifecycle DLQ 的非空告警。
        #
        # 这个资源【此前不存在】,但 deploy/stacks/alarms.py 有三处注释声称它在本文件里
        # (`:7` / `:16-17` / `:182`),`:259` 还用 `self.node.try_find_child("LifecycleDlqAlarm")`
        # 去给它挂 SNS action —— 找不到就被 `isinstance` 检查静默跳过。于是 lifecycle
        # consumer 重投 5 次进 DLQ 后【没有任何人知道】,只能等客户报障。issue #469 的评论
        # (2026-08-12)实查确认了这个缺口。
        #
        # construct id 必须【逐字】是 "LifecycleDlqAlarm"(不是 LifecycleDLQAlarm)——
        # alarms.py:259 按这个字符串查找,拼写不一致等于这个告警继续没人挂 topic。
        # 形态与 dispatch 侧对称(dispatch_infra.py:386 DispatchDlqAlarm):
        # Maximum > 0 / 1 个周期 / 缺数据点不算触发。
        cloudwatch.Alarm(
            self,
            "LifecycleDlqAlarm",
            alarm_name="openclaw-lifecycle-dlq-not-empty",
            metric=lifecycle_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1),
                statistic="Maximum",
            ),
            threshold=0,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            # **CloudWatch 硬约束:`AlarmDescription` ≤ 1024 字符。**
            # 这条上限**不由 CDK synth 校验**,是 CloudWatch API 在部署时才拒 —— 所以
            # `cdk synth`、`mechanical-gate`、`pytest-highrisk` 会全绿而 `cdk deploy` 必炸
            # (真机实撞:2026-08-24 us-west-2,本描述 1117 字符 → UPDATE_FAILED →
            # 整个栈 UPDATE_ROLLBACK_COMPLETE,`gitlab/bb` 一度部署不动)。
            # 判据因此下移到源码断言(`tests/test_532_dlq_alarm_locatable.py` 的
            # `test_alarm_description_fits_cloudwatch_limit`):它按 AST 算出拼接后的真实
            # 长度,不依赖 aws-cdk-lib 装没装 —— synth 那条断言是 `importorskip`,在没装
            # CDK 的 CI 上会被 skip,拿它守这条上限等于没守。
            #
            # 完整排障步骤不属于这个字段:1024 字符装不下一份 runbook。这里只留
            # 「谁在自动收敛 / 谁需要人 / 从哪个查询开始」,细节走 GET /tenants/{id} 与
            # api Lambda 日志的 `delete-reconciler:` 前缀。
            alarm_description=(
                "openclaw-lifecycle-dlq has a message — the lifecycle consumer gave "
                f"up after {_LIFECYCLE_MAX_RECEIVE} receives; a tenant's "
                "suspend/restore/delete/rebuild never completed. "
                # #532 —— 原文写的是「…and **nothing else will retry it**」。delete 那一支
                # 从 #532 起不成立:services/delete_reconciler 定时扫「deleting +
                # delete_retryable + claim/租约都已过期」并重新入队,有界。告警若还说
                # 「没人会重试」,运维会按「必须人工介入」去处置一件系统正在自动收敛的事 ——
                # 那正是本仓反复踩的「文案声称了系统不做的事」。
                #
                # 同时补上 AC 要求的**可定位信息**:告警本身是队列深度指标,带不了
                # tenant/op/error,所以这里点名「去哪儿查」。那些字段都在租户行上,而
                # GET /tenants/{id} 返回整行(只剔 _TENANT_SECRET_FIELDS),故控制台/API 直接可见。
                "DELETE is auto-reconciled: services/delete_reconciler re-enqueues "
                "status=deleting AND delete_retryable=true rows once both the delete "
                "claim and the lifecycle lease expire. It is bounded and sets "
                "delete_redrive_exhausted when it gives up — that flag, not this "
                "alarm, is when a human is required. "
                "suspend/restore/rebuild have NO reconciler and DO need a human. "
                # Codex 独立复审 blocker-3:原文只给了 GET /tenants/{id} —— 那**预设你已经
                # 知道是哪个租户**,而告警是队列深度指标,恰恰不告诉你租户是谁。所以必须先给
                # 一个**能找出候选租户**的入口。`status` 是受支持的 query 字段且走 gsi_status
                # (tenant_query_service.py:17/21),所以下面这条是真能跑的,不是示意。
                "This alarm cannot name a tenant, so START with "
                "GET /tenants?status=deleting — the stuck ones have "
                "delete_retryable=true. Then GET /tenants/{id} for "
                "delete_fail_reason, delete_fail_at, delete_redrive_attempts, "
                "delete_redrive_exhausted, delete_intent. Cross-check the api Lambda "
                "logs for the 'delete-reconciler:' prefix (tenant, op_id, attempt, "
                "intent) and the consumer's logs; a stuck suspending/restoring also "
                "raises OpenClaw/Lifecycle LifecycleStuckMarked."
            ),
        )
        lifecycle_queue = sqs.Queue(
            self,
            "LifecycleQueue",
            queue_name="openclaw-lifecycle.fifo",
            fifo=True,
            # explicit MessageDeduplicationId (tenant_id:action) is set by the
            # producer; content_based_deduplication=True is a safety net for
            # any future producer that forgets to pass one.
            content_based_deduplication=True,
            # #411/6.4 — 可见性超时必须【严格大于】consumer Lambda timeout,否则 Lambda
            # 超时那一刻消息刚好重新可见、而远端 SSM 可能仍在跑 → 重投与在途操作叠加
            # (codex round5#231)。#422 codex round2 #6 — consumer timeout 提到 900s(覆盖
            # suspend 同步 backup 900s 预算),visibility 同步提到 960s = 900s + 60s 余量
            # (沿用仓库 "timeout+60s" 惯例)。非重动作处理完即删,不受影响。
            visibility_timeout=Duration.seconds(960),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=_LIFECYCLE_MAX_RECEIVE, queue=lifecycle_dlq
            ),
        )

    # 控制面重构阶段1 — api_fn 的环境变量抽成共享 dict,lifecycle consumer 复用
    # (同一份 handler 需同配置:表名/区域/SSM/overcommit 等)。Cognito pool id 等
    # 后置 add_environment 的 key 在下方对两个 Lambda 都 add。
    _api_env = {
        "TENANTS_TABLE": tenants_table.table_name,
        "HOSTS_TABLE": hosts_table.table_name,
        "GROUPS_TABLE": groups_table.table_name,
        "AUDIT_TABLE": audit_table.table_name,
        "AUDIT_TTL_DAYS": str(audit_retention_days),
        "BATCH_JOBS_TABLE": batch_jobs_table.table_name,
        "TENANT_IDP_TABLE": tenant_idp_table.table_name,  # #97 档A /tenantmatch
        "TENANT_SECRETS_TABLE": tenant_secrets_table.table_name,  # #187 P1 gateway token
        "TENANT_QUERY_ENABLED": str(
            (CFG.get("tenant_query", {}) or {}).get("enabled", False)
        ).lower(),
        "PARAM_REGISTRY_TABLE": param_registry_table.table_name,
        "RECIPIENT_KEYS_TABLE": recipient_keys_table.table_name,
        "VERSION_SNAPSHOTS_TABLE": version_snapshots_table.table_name,  # #217 V2
        "IMAGE_JOBS_TABLE": image_jobs_table.table_name,  # #394 step1 pull Job
        "ASSETS_BUCKET": assets_bucket.bucket_name,
        "NOTIFICATIONS_TOPIC_ARN": notifications_topic_arn,
        "ROOTFS_PREFIX": CFG["s3"]["rootfs_prefix"],
        "HOST_RESERVED_VCPU": str(CFG["host"]["reserved_vcpu"]),
        "HOST_RESERVED_MEM": str(CFG["host"]["reserved_mem_mb"]),
        "CPU_OVERCOMMIT_RATIO": str(CFG["host"].get("cpu_overcommit_ratio", 1.0)),
        "MEM_OVERCOMMIT_RATIO": str(CFG["host"].get("mem_overcommit_ratio", 1.0)),
        # #430 异构混池 — per-family 超卖比覆盖(JSON)、四级亲和排序、物理内存软门。
        # 全部空/关默认 → 逐字节回落既有行为(回退开关,不需回滚代码)。
        "OVERCOMMIT_BY_FAMILY": _json.dumps(
            CFG["host"].get("overcommit_by_family") or {}, separators=(",", ":")
        ),
        "AFFINITY_ENABLED": str(
            bool((CFG.get("scheduling", {}) or {}).get("affinity_enabled", False))
        ).lower(),
        "FAMILY_ORDER": ",".join(
            (CFG.get("scheduling", {}) or {}).get("family_order")
            or ["r8g", "r7g", "m8g", "m7g"]
        ),
        "MEM_SAFETY_FLOOR_RATIO": str(
            (CFG.get("scheduling", {}) or {}).get("mem_safety_floor_ratio", 0.0)
        ),
        "MEM_CHECK_TTL_SEC": str(
            (CFG.get("scheduling", {}) or {}).get("mem_check_ttl_sec", 300)
        ),
        "VM_DEFAULT_VCPU": str(CFG["vm"]["default_vcpu"]),
        "VM_DEFAULT_MEM": str(CFG["vm"]["default_mem_mb"]),
        "VM_DATA_DISK_MB": str(CFG["vm"]["data_disk_mb"]),
        "VM_PORT_BASE": str(CFG["vm"]["gateway_port_base"]),
        "VM_SUBNET_PREFIX": CFG["vm"]["subnet_prefix"],
        "ASG_NAME": "openclaw-hosts-asg",
        "BACKUP_PREFIX": CFG["s3"]["backup_prefix"],
        "LITELLM_BASE_URL": CFG.get("billing", {}).get("litellm_base_url", ""),
        "LITELLM_MASTER_KEY_SECRET": CFG.get("billing", {}).get(
            "master_key_secret", ""
        ),
        "TENANT_DEFAULT_BUDGET": str(CFG.get("billing", {}).get("default_budget", 0)),
        "TENANT_DEFAULT_RPM": str(CFG.get("billing", {}).get("default_rpm", 0)),
        "QUOTAS_ENABLED": str(CFG.get("quotas", {}).get("enabled", False)).lower(),
        "QUOTAS_MAX_VCPU": str(CFG.get("quotas", {}).get("max_vcpu_per_tenant", 0)),
        "QUOTAS_MAX_MEM_MB": str(CFG.get("quotas", {}).get("max_mem_mb_per_tenant", 0)),
        "QUOTAS_MAX_DATA_DISK_MB": str(
            CFG.get("quotas", {}).get("max_data_disk_mb", 0)
        ),
        "MULTI_AZ_ENABLED": str(CFG.get("multi_az", {}).get("enabled", False)).lower(),
        "MULTI_AZ_COUNT": str(CFG.get("multi_az", {}).get("az_count", 1)),
        "WAF_ENABLED": str(CFG.get("waf", {}).get("enabled", False)).lower(),
        "BALLOON_ENABLED": str(CFG.get("balloon", {}).get("enabled", False)).lower(),
        "CONSOLE_AUTH_ENABLED": str(
            (CFG.get("console_auth", {}) or {}).get("enabled", False)
        ).lower(),
        "DEFAULT_NO_JWT_ROLE": str(
            CFG.get("console_auth", {}).get("default_no_jwt_role", "viewer")
        ),
        "RBAC_ENABLED": str(
            (CFG.get("console_auth", {}) or {}).get("rbac_enabled", True)
        ).lower(),
        "EXTERNAL_AUTHZ": str(
            (CFG.get("external_authz", {}) or {}).get("enabled", False)
        ).lower(),
        "EXTERNAL_AUTHZ_SECRET": _external_authz_secret_ref,
        "PROJECT_VERSION": _read_pyproject_version(),
    }
    if tenant_stats_enabled:
        _api_env["TENANT_STATS_TABLE"] = tenant_stats_table.table_name
    # #368/#422 — api Lambda(及复用 _api_env 的 lifecycle consumer)恢复/备份列表读桶
    # (_resolve_backup / list_backups / list_all_backups)读 `BACKUP_BUCKET or ASSETS_BUCKET`;
    # 此前只有 backup Lambda 拿到 BACKUP_BUCKET(见 :1481),api Lambda 缺 → 永远回退 assets 桶
    # → 恢复必 404、备份清单永远空(#368 RPO 兜底断裂)。备份写在 WORM+CMK 的专用桶,读也必须
    # 指向它。backup_bucket 可能未建(getattr None),判空 fail-safe(不建桶的部署不注入,读侧
    # 仍回退 assets,与旧行为一致)。IAM 读权限在下方 grant(:523 附近)。
    if backup_bucket is not None:
        _api_env["BACKUP_BUCKET"] = backup_bucket.bucket_name

    # #564 G5 —— 七档生命周期死线注入 api / lifecycle-consumer(两者共用 `_api_env`)。
    #
    # 客户明文:「需要可以参数化,**改 Lambda env 即可修改每个 lifecycle 配置**」。此前
    # create 的 180 是 `create_deadline.py` 的 Python 常量,另外六个操作压根没有死线。
    #
    # **env 名从模块的 `env_name_for()` 取,不在这里另拼字符串** —— 两边各拼一次就会出现
    # 「CDK 注入了 A、代码读 B」这种静默失效(注入了没人读的 env,而读的那个永远走默认值)。
    # import 真模块的先例在 `scripts/checks/create-deadline-config.py:65`,那里的注释写得
    # 更直白:「import 真模块,它改了这里自动跟着改」。
    #
    # 值的来源是 `config.yml` 的 `lifecycle.deadline_sec`(同源,见该段注释);**缺段/缺项时
    # 补模块里那份客户表格值**,八档一个不缺。
    #
    # ⚠ 这里原来写的是「缺项就不注入那一档,运行时回落到模块里同样那份客户表格值」——
    # **那个假设在 Lambda 里不成立**,2026-08-26 apse1 全新部署实测推翻:
    #   · `create_deadline._require_env()` 以 `AWS_LAMBDA_FUNCTION_NAME` 为判据,在 Lambda 里
    #     恒为 True,于是 `deadline_sec_for()` 对**缺 env 的那一档直接 raise**,压根不回落;
    #   · `handler.py:14` → `services.tenant_query_service` → `services.tenant_service:43`
    #     是**模块导入期**就 import 本模块,`assert_deadline_config_sane()` 又在导入期遍历八档,
    #     所以不是"走到写路径才炸",而是冷启动即 `Runtime.ExitError`,**每一条路由都 502**
    #     (含 `GET /system/info`)。
    # 而仓内除 `config.yml.example` 外的三份 config(含 `clawpool-deploy.sh all-imported`
    # 复制的 `engineering/deploy/testbed-config/config.sg-testbed.yaml`)都没有这一段 ——
    # 于是"按 runbook 走的全新部署"必然拿到一个全 502 的控制面。补齐是这条链上唯一
    # 不削弱 G5 的修法:fail-closed 仍然只在**真的配错**时开火,而不是在"没写这段"时自锁。
    #
    # 插入位置必须在下面 `api_fn` 的 `environment=dict(_api_env)` 之【前】:那两处 `dict()`
    # 是快照,而 `CREATE_VIA_QUEUE` 就是在两次快照之间加的 —— 所以只有 consumer 拿到它。
    # 加在这里两个 Lambda 才都有。
    _dl_cfg = (CFG.get("lifecycle") or {}).get("deadline_sec") or {}

    def _deadline_sec_for_deploy(action: str) -> int:
        """该档要注入的秒数:config 写了就用 config,没写用模块的权威默认。

        默认值取 `create_deadline.default_deadline_sec_for()`,不在这里另抄一份表 ——
        与 env 名/参数名同一条理由:两边各抄一次就会出现「CDK 注入 180、代码认 600」。

        config 里的值**在 synth 期就判死**,不做 `int()` 宽容转换。宽容转换会把
        「配错」变成三种更难查的形态,而它们的终点与本次修的缺陷是同一个:
          · `-1` / `0` → env 注入成 `"-1"`,`assert_deadline_config_sane()` 在 Lambda
            **导入期** raise → 每条路由 502(而 `cdk deploy` 报成功);
          · `180.5` → `int()` 静默截断成 180,线上跑的是另一个数、没人知道;
          · `True` → `int(True) == 1`,一秒死线,每个 lifecycle 操作恒超时。
        在 synth 期抛比在冷启动期抛便宜三个数量级:前者 `cdk deploy` 当场停,后者要先
        部完、再全 502、再翻 awslambdaric 被 latin-1 遮蔽过的日志。
        """
        if action not in _dl_cfg:
            return int(_create_deadline.default_deadline_sec_for(action))
        raw = _dl_cfg[action]
        # bool 先排:它是 int 的子类,不先判就会被下面那条放过去。
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ValueError(
                f"config.yml lifecycle.deadline_sec.{action}={raw!r} is not a "
                f"positive integer; it would be injected as "
                f"{_create_deadline.env_name_for(action)} and make every route 502 "
                f"at cold start (or silently truncate). Fix config.yml or remove the "
                f"key to take the built-in default "
                f"({_create_deadline.default_deadline_sec_for(action)}s)."
            )
        return int(raw)

    for _dl_action in _create_deadline.DEADLINE_ACTIONS:
        _api_env[_create_deadline.env_name_for(_dl_action)] = str(
            _deadline_sec_for_deploy(_dl_action)
        )

    # #564 G5 —— 死线值的**运行时载体**:SSM Parameter Store。与上面的 env 同源于同一段
    # config(`lifecycle.deadline_sec`),而 config 缺项时两边都用 `create_deadline` 的
    # **模块权威默认值**补齐(#630),所以八档恒全覆盖、不存在只建一半的形态。
    #
    # **为什么 env 不够、必须再加一个载体**:客户要的是「改配置即生效」,而真机实测证明改
    # Lambda env 做不到 —— 流量走 `live` 别名 → 已发布版本,而**已发布版本的 env 是冻结的**
    # (实测:改 `$LATEST` 的 `DEFAULT_NO_JWT_ROLE`,等 75s,请求仍按旧版本的值判权)。
    # 那是一个**看不见的失败**:运维以为改了,线上跑的是另一个数。
    # 参数则立即生效,运维用 `aws ssm put-parameter --overwrite` 直接改、不等 stack update ——
    # 与 `dispatch_infra.py` 的 andon 急停参数逐字同款的理由。
    #
    # 建参数时给默认值(照 andon 那条:"防首启读空被憋死"),运行时读不到就回落 env/代码默认。
    # **参数名从 `param_name_for()` 取,不在这里另拼字符串** —— 与上面 env 名同一条理由:
    # 两边各拼一次就会出现「CDK 建了 A、运行时读 B」,参数建好了没人读、而读的那个永远
    # ParameterNotFound → 一路静默回落默认。
    #
    # 下次 `cdk deploy` 会把手改的值覆盖回 config —— 那是刻意的(config 才是长期真相),
    # 漂移由 `create-deadline-config.py --live` 的复检兜。
    # 两个载体必须**同时**覆盖八档:只建 config 里写了的那些,会让没写的那档 env 可改而
    # 参数不可改,`/system/info` 报的 `source` 也跟着分叉 —— 与上面同一个缺陷类。
    for _dl_action in _create_deadline.DEADLINE_ACTIONS:
        ssm.StringParameter(
            self,
            f"LifecycleDeadlineSec{_dl_action.capitalize()}",
            parameter_name=_create_deadline.param_name_for(_dl_action),
            string_value=str(_deadline_sec_for_deploy(_dl_action)),
            description=(
                f"openclaw lifecycle deadline for '{_dl_action}' in seconds. "
                "Edit with `aws ssm put-parameter --overwrite` for an immediate "
                "effect (no redeploy). Read by api/lifecycle-consumer via "
                "core/deadline_config.py with a 60s in-process cache; an illegal "
                "value fails the request loudly instead of silently falling back. "
                "cdk deploy resets it to config.yml lifecycle.deadline_sec."
            ),
        )

    api_fn = _lambda.Function(
        self,
        "ApiHandler",
        function_name="openclaw-api",
        runtime=_lambda.Runtime.PYTHON_3_12,
        # 1.5.0: ARM_64 (Graviton) — cheaper/faster. Bundles PyJWT + cryptography
        # for Cognito JWT RS256 verification (cryptography has a native ext).
        architecture=_lambda.Architecture.ARM_64,
        handler="handler.lambda_handler",
        code=_lambda.Code.from_asset(
            "deploy/lambda/api",
            bundling=BundlingOptions(
                # Image arch = build host (not Lambda) to avoid arm64-on-x86
                # exec format error; pip cross-downloads the aarch64 wheel.
                image=cdk.DockerImage.from_registry(_sam_build_image_for_host()),
                # macOS Docker Desktop VirtioFS 下 bind-mount 输出目录对容器
                # uid(-u 503:20)拒写,bundling 只能 root。VOLUME_COPY 用 docker
                # volume 中转产物再拷回,跨平台稳健(Linux CI 也正常)。
                bundling_file_access=BundlingFileAccess.VOLUME_COPY,
                command=[
                    "bash",
                    "-c",
                    "pip install --no-cache-dir "
                    "--platform manylinux2014_aarch64 "
                    "--implementation cp --python-version 3.12 "
                    "--only-binary=:all: --upgrade "
                    "-r requirements.txt -t /asset-output "
                    "&& cp -au . /asset-output",
                ],
            ),
        ),
        # #217 §10.3 — 900s(Lambda 硬上限 15min)让 pull-image 金丝雀同步链跑完:
        # 装 live(SSM 等)→ 起金丝雀 → poll 到 running → 晋级/回滚,需数分钟。APIGW
        # 集成 29s 会早早回 504,但 Lambda 后台跑完整链(浏览器靠 console 轮询看
        # upgrading→金丝雀→active)。timeout 是上限,普通请求仍秒回,不影响别的路由;
        # pull 期间占一个实例数分钟,并发别的请求靠 Lambda 自动扩实例。
        timeout=Duration.seconds(900),
        memory_size=2048,
        environment=dict(_api_env),
        # #564 G6 ② —— 异步调用的失败出口。**这个函数会自调用**(`InvocationType="Event"`,
        # 六处:rebuild worker、host/fleet/rolling-upgrade 的后台任务、手动备份派发),而在
        # 这之前它**没有任何** DLQ / on-failure destination —— 异步 worker 里抛出的未处理
        # 异常在 AWS 重试耗尽后**无声消失**,没有一处能观测到。#565 的现状小节逐字记了这个
        # 缺口(「通道 C/D 的失败无声消失」),而它同时是 #564 G6 的第二半。
        #
        # **只开 DLQ,不动 `retry_attempts` / `max_event_age`**(与 plan 的括号里那句不同,
        # 理由如下):
        #   · `retry_attempts` 的默认 2 次是**承重的**:`handler.py` 自己的注释写着
        #     「异步 Lambda 调用对函数抛错自动重试 2 次 → 重试 = 同 job 第二个 worker」,
        #     而 rebuild 的幂等恢复(op_id + 生命周期围栏 + host 账本)正是建立在"重试会
        #     resume 同一次 rebuild"上。改这个数会改掉那条设计的前提,不在本 issue 范围。
        #   · `max_event_age` 会一次作用到**全部六处**自调用,而它们各有各的时间假设
        #     (rolling upgrade 的后台任务与一次 rebuild 不是同一个量级)。挑一个数套所有,
        #     是我从 #564 的原文里推不出来的改动;而"陈旧事件不该执行"这件事 G3 已经用
        #     消费前的死线检查解决了(过期的 rebuild 不执行)。
        #
        # 顺带记一个**既有**的版本偏斜(不是本次引入,只报告):自调用走的是
        # `os.environ["AWS_LAMBDA_FUNCTION_NAME"]`——**不带 qualifier**,即 `$LATEST`;
        # 而 API GW 与 SQS 事件源只认 `live` 别名指向的已发布版本。部署期间"API 走版本 N、
        # 异步 worker 走 $LATEST"是可能的。DLQ 挂在函数上,对 `$LATEST` 的调用同样生效。
        dead_letter_queue_enabled=True,
    )
    pagination_secret = secretsmanager.Secret(
        self,
        "PaginationCursorSecret",
        secret_name="openclaw/pagination-cursor",
        generate_secret_string=secretsmanager.SecretStringGenerator(
            secret_string_template='{"purpose":"pagination-aes-gcm"}',
            generate_string_key="key",
            password_length=43,
            exclude_punctuation=True,
        ),
    )
    api_fn.add_environment(
        "PAGINATION_AES_KEY",
        pagination_secret.secret_value_from_json("key").unsafe_unwrap(),
    )
    # ── Lambda Version + Alias "live" (#149) ──────────────────────────────
    # 目标拓扑: API GW → alias "live" → Version N
    # 每次部署自动发新 Version,alias "live" 始终指向最新。日后回滚只需
    # update-alias 指回旧 Version,无需 CodeDeploy。API GW 和 SQS event source
    # 从此只认 alias ARN,function 本体不再直接被外部触发。
    api_fn_version = api_fn.current_version
    api_fn_alias = _lambda.Alias(
        self,
        "ApiHandlerLive",
        alias_name="live",
        version=api_fn_version,
    )
    tenants_table.grant_read_write_data(api_fn)
    if tenant_stats_enabled:
        tenant_stats_table.grant_read_data(api_fn)
    hosts_table.grant_read_write_data(api_fn)
    groups_table.grant_read_write_data(api_fn)
    version_snapshots_table.grant_read_data(api_fn)  # #217 V2 — pull-image 只读快照
    # #376 — create_image_snapshot 落新快照:只加 PutItem(最小权限,不给 Delete/Update)。
    version_snapshots_table.grant(api_fn, "dynamodb:PutItem")
    # #394 — delete_image_snapshot 是【软删】(status=deleted 标记),用 UpdateItem 打标而非
    # 物删,故加 UpdateItem。不给 DeleteItem(软删不物理删,记录留档可审计/可恢复)。
    version_snapshots_table.grant(api_fn, "dynamodb:UpdateItem")
    # #394 step1 — pull Job 记录:api_fn 建/读/推进 Job(含两个 GSI 的 Query)。
    # 不给 DeleteItem:Job 记录由 TTL(expires_at)回收,控制面无删除路径。
    image_jobs_table.grant_read_write_data(api_fn)
    # #394 — 两处 TransactWriteItems 都要显式授权(否则条件在测试里对、生产 AccessDenied):
    #  · Pull admission:snapshot ConditionCheck + Job Put(→ 每次 pull 都 JOB_RECORD_UNAVAILABLE);
    #  · codex NB2 canary 租户固定:snapshot ConditionCheck + tenant Put(→ canary 建租户 AccessDenied)。
    # 真机实测教训:除 TransactWriteItems 外还必须显式给 **ConditionCheckItem** —— 事务里的
    # ConditionCheck 项按【被检查表】单独鉴权,缺它报
    # "not authorized to perform: dynamodb:ConditionCheckItem on .../openclaw-version-snapshots"。
    # resources 覆盖三张表:version-snapshots(被 ConditionCheck)+ image-jobs + tenants(被 Put)。
    # #412 — 新增 hosts 表:dispatch reserve(_reserve_batch_txn)+ 令牌释放(_release_reservation
    # / poller)都用 TransactWriteItems 原子写 hosts+tenants;grant_read_write_data 不含
    # TransactWriteItems,漏加会运行时 AccessDenied(与上面 #394 同类坑,moto/mock 测不出)。
    api_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["dynamodb:TransactWriteItems", "dynamodb:ConditionCheckItem"],
        resources=[
            version_snapshots_table.table_arn,
            image_jobs_table.table_arn,
            tenants_table.table_arn,
            hosts_table.table_arn,
        ],
    ))

    if tenant_stats_enabled:
        tenant_stats_fn = _lambda.Function(
            self,
            "TenantStatsWriter",
            function_name="openclaw-tenant-stats-writer",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("deploy/lambda/tenant_stats"),
            timeout=Duration.seconds(50),
            memory_size=8192,
            reserved_concurrent_executions=1,
            environment={
                "TENANTS_TABLE": tenants_table.table_name,
                "TENANT_STATS_TABLE": tenant_stats_table.table_name,
                "ASSETS_BUCKET": assets_bucket.bucket_name,
                "ROOTFS_PREFIX": CFG["s3"]["rootfs_prefix"],
                "STATS_SCAN_SEGMENTS": "8",
            },
        )
        tenants_table.grant_read_data(tenant_stats_fn)
        tenant_stats_table.grant_read_write_data(tenant_stats_fn)
        assets_bucket.grant_read(tenant_stats_fn)
        events.Rule(
            self,
            "TenantStatsSchedule",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            targets=[targets.LambdaFunction(tenant_stats_fn)],
        )
    # #152/#118 — the ClawPool credential-injection CMK ARN. Added ONLY when the
    # feature is on so synth stays byte-identical when off (no key on the env).
    # The API uses it as the real gate: it rejects injected_credentials whose
    # kms_key_arn != this ARN (and rejects any injection when this is empty).
    if clawpool_cmk is not None:
        api_fn.add_environment("CLAWPOOL_CMK_ARN", clawpool_cmk.key_arn)
    # #149 asymmetric-v1 — api Lambda serves the RSA CMK PUBLIC key to callers via
    # GET /clawpool-rsa-public-key so they can locally OAEP-encrypt env creds. It
    # only needs GetPublicKey (never Decrypt — private key stays in KMS, host decrypts).
    if clawpool_rsa_cmk is not None:
        api_fn.add_environment("CLAWPOOL_RSA_CMK_ARN", clawpool_rsa_cmk.key_arn)
        clawpool_rsa_cmk.grant(api_fn, "kms:GetPublicKey")
    # Issue #17 — api Lambda writes audits and reads them back via GET /audit-log
    audit_table.grant_read_write_data(api_fn)
    # PRD #54 — async batch jobs: read/write the job ledger, and self-invoke
    # asynchronously to run the worker (same function, routed by a marker in
    # the event payload — no separate Lambda to keep the blast radius small).
    batch_jobs_table.grant_read_write_data(api_fn)
    # #97 档A — /tenantmatch only reads the IdP map (least privilege: read-only).
    tenant_idp_table.grant_read_data(api_fn)
    # #187 P1 / #149 出站 — control-plane mints the per-tenant gateway token +
    # device identity ciphertext (SPEC/11-ENGINE-TRANSFORM · INTERFACE-CONTRACT §5).
    # Lambda needs:
    #   • r/w on the secrets table (put on mint, get on reveal, delete on cleanup);
    #   • kms:GenerateRandom (32B CSPRNG for the token, API-level not per-key);
    #   • kms:Encrypt on the ClawPool CMK (envelope encrypt with tenant_id ctx);
    #   • kms:Decrypt on the ClawPool CMK — GET /tenants/{id}/credentials decrypts
    #     the stored ciphertext then re-OAEP-encrypts under the platform recipient
    #     RSA public key (bootstrap keypair: public in DDB, private in Secrets
    #     Manager, handed to the caller offline by ops). Plaintext exists only
    #     inside the handler for the re-wrap, never logged / never returned raw.
    #   • create/read the bootstrap recipient private-key secret (first-call
    #     keypair generation in ensure_bootstrap_key).
    # Host role separately has kms:Decrypt for the SSM position-12 injection path
    # (unchanged, added with #118). The consumer Lambda below runs create only
    # (never GET /credentials), so it keeps encrypt-only.
    tenant_secrets_table.grant_read_write_data(api_fn)
    param_registry_table.grant_read_write_data(api_fn)
    recipient_keys_table.grant_read_write_data(api_fn)
    # #264 — GET /admin/edge/instances(edge_admin.py:48 查 edge TG 健康)需要它;
    # 原来 role 有一堆 #187 前遗留的 elbv2 写权却独缺这个读权,target_health 静默返 null
    # (edge_admin.py 注释自标 "P5 后追加 elbv2:DescribeTarget* IAM")。Describe 类不支持
    # 资源级 → Resource=*。
    api_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["elasticloadbalancing:DescribeTargetHealth"],
            resources=["*"],
        )
    )
    # #564 G5 —— 读七档死线参数。**资源必须精确到这个前缀,绝不能宽到 `/openclaw/*`。**
    #
    # AWS 文档(GetParametersByPath)明文:「If a user has access to a path, then the user can
    # access all levels of that path… **Even if a user has explicitly been denied access in IAM
    # for parameter `/a/b`, they can still call the GetParametersByPath API operation
    # recursively for `/a` and view `/a/b`**」—— 路径权限**向下穿透,而且显式 Deny 拦不住**。
    # 授到 `/openclaw/*` 就等于让 api role 能递归读 `/openclaw/litellm-host` 与 dispatch 的
    # SecureString manifest 前缀。代码侧也用 `Recursive=False` 配合。
    #
    # api_fn 现有的参数权限在 `dispatch_infra.py:318` —— 那条收窄到 dispatch 前缀,
    # **不覆盖**本前缀,所以这里是必要的加法而不是重复授权。
    # lifecycle_consumer 共用 `_api_env`、读同一份参数,所以它也要(见下面它自己的授权处)。
    _dl_param_policy = iam.PolicyStatement(
        actions=["ssm:GetParametersByPath"],
        resources=[
            f"arn:aws:ssm:{self.region}:{self.account}:"
            f"parameter{_create_deadline.PARAM_PREFIX}*"
        ],
    )
    api_fn.add_to_role_policy(_dl_param_policy)
    if clawpool_cmk is not None:
        clawpool_cmk.grant_encrypt_decrypt(api_fn)
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateRandom"],
                resources=["*"],
            )
        )
    # #149 出站 bootstrap — ensure_bootstrap_key 首调生成 recipient keypair,私钥
    # 存 Secrets Manager 固定名字(运维 get-secret-value 线下交调用方)。删除权
    # (purge_bootstrap_private_key)故意不给:强删是运维手动动作,不留给 API 面。
    api_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=[
                "secretsmanager:CreateSecret",
                "secretsmanager:PutSecretValue",
                "secretsmanager:GetSecretValue",
            ],
            resources=[
                f"arn:aws:secretsmanager:{self.region}:{self.account}:"
                "secret:openclaw/recipient-bootstrap-private-key*"
            ],
        )
    )
    # self-invoke(batch worker)权限:**不用** api_fn.grant_invoke(api_fn)。
    # 查证 CDK issue #11020:grantInvoke 给自身会把 api_fn ARN 注入自己的
    # ServiceRole DefaultPolicy,而 DefaultPolicy↔Lambda 是 CDK 经典 circular
    # (CFN 需 lambda 先于 ServiceRole、ServiceRole 又先于 lambda)。这条边叠加
    # 大量 API GW method permission → 整个 API GW 子系统 changeset circular
    # (2026-06-29 deploy 实撞,环恒含 ApiHandler+DefaultPolicy+ApiGwInvoke)。
    # 官方 workaround:用独立 iam.Policy(attachInlinePolicy 模式)挂 self-invoke
    # 权限,不在 Lambda↔DefaultPolicy 间建环。resources 用 ARN 字符串 token。
    iam.Policy(
        self,
        "ApiSelfInvokePolicy",
        roles=[api_fn.role],
        statements=[
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[api_fn.function_arn],
            )
        ],
    )
    assets_bucket.grant_read(api_fn)
    # #368/#422 — api Lambda 读备份专用桶(恢复下载 restore_backup_key + 备份清单)。此前
    # 只 backup_bucket.grant_read_write(backup_fn)(:1492),api_fn 对 backups 桶零权限 →
    # 即使 BACKUP_BUCKET env 指对了,IAM 也拒读 → 恢复 404。grant_read 只读(恢复不写备份桶,
    # 写由 backup Lambda 负责);判空与 env 注入对称。
    if backup_bucket is not None:
        backup_bucket.grant_read(api_fn)

    # 控制面重构阶段1 — 把队列 URL 注入 api Lambda(产端:create/start/stop/delete
    # 入队)+ 建 consumer Lambda(同 handler 代码,SQS 事件触发,reserved
    # concurrency 当限流阀削峰)。consumer 复用 api_fn 的全部 env(同代码同权限)。
    if _lifecycle_q_enabled and lifecycle_queue is not None:
        api_fn.add_environment("LIFECYCLE_QUEUE_URL", lifecycle_queue.queue_url)
        # Phase 2 — route POST /tenants through the FIFO queue too (config-gated,
        # default off). Only meaningful when the queue exists, so it's set here.
        _create_via_queue = bool(CFG.get("scaler", {}).get("create_via_queue", False))
        api_fn.add_environment("CREATE_VIA_QUEUE", str(_create_via_queue).lower())
        _api_env["CREATE_VIA_QUEUE"] = str(_create_via_queue).lower()
        # #564 G6 —— 把队列真实的 maxReceiveCount 注给消费侧,让它能判断"这是不是最后一次
        # 投递"。**只给 consumer,不给 api_fn**:产端不需要这个数,给了就等于多一个会漂的副本。
        # 与队列的 RedrivePolicy 同源(见 `_LIFECYCLE_MAX_RECEIVE` 的说明)—— 两处各写死会让
        # backstop 要么提前误判终态、要么永远判不到而消息静默进 DLQ。
        _api_env["LIFECYCLE_MAX_RECEIVE_COUNT"] = str(_LIFECYCLE_MAX_RECEIVE)
        # 给 api_fn 发队列权限用**独立 iam.Policy 资源**(非 grant_send_messages、
        # 非 add_to_role_policy)。原因:那两者都往 api role 的 DefaultPolicy 注入
        # 对 queue 的依赖,而 API GW ApiDeployment 间接依赖 api role/Lambda,queue
        # 又被 consumer grant 一堆表 → circular dependency(2026-06-29 实撞)。
        # 独立 Policy 把 queue 依赖隔离在自己资源上,不污染 DefaultPolicy → 断环。
        iam.Policy(
            self,
            "ApiLifecycleEnqueuePolicy",
            roles=[api_fn.role],
            statements=[
                iam.PolicyStatement(
                    # #264 — GetQueueAttributes: GET /system/queues(handler.py:757 读队列深度)
                    # 需要它;原来只有 SendMessage → 面板 depth 静默返 null(fail-soft 吞了 AccessDenied)。
                    actions=["sqs:SendMessage", "sqs:GetQueueAttributes"],
                    resources=[lifecycle_queue.queue_arn],
                )
            ],
        )
        # ---------- #532 卡住的 delete 对账:rate(15 minutes) → api_fn ----------
        # 消费「host 侧删除失败后保留的 `delete_retryable=true` + claim 已过期」这组行,
        # 按落库的 `delete_intent` 重新入队。没有这一拍,消息进 DLQ(maxReceiveCount=5)
        # 之后就没人接手 —— 即使根因已修,租户永久停在 `deleting`,只能人工再点一次删除
        # (issue 真机实例:ap-southeast-1 两个租户,脚本补回 S3 后仍卡着)。
        #
        # **落在这个 if 里面是刻意的**:本对账的前提是"删除走过异步队列、可能进 DLQ"。
        # 队列没开时 delete 同步跑完,不存在这个形态,建一条每 15 分钟空转的规则是纯浪费。
        # 与 #438 的 CredentialReconcilerRule 形成对照 —— 那条建在 `DispatchInfra` 里,
        # 于是被 `dispatch.enabled`(出厂 false)门控,而它需要的其实只是 api Lambda 本身;
        # 这条只跟它真正依赖的开关绑定。
        #
        # 15 分钟不是随手取的:`_DELETE_CLAIM_TTL_SECONDS = 900`(tenant_service),所以一行
        # 最迟在上次尝试后 15 分钟变成"claim 已过期"= 可收敛;扫描节拍与它对齐即可,更密只会
        # 撞 CCF 空转。× `DELETE_REDRIVE_MAX_ATTEMPTS = 10` ≈ 覆盖 2.5 小时的运维往返。
        events.Rule(
            self,
            "DeleteReconcilerRule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            description=(
                "#532 redrive deletes stuck in `deleting`. Fires api_fn with "
                "{source:'delete.reconciler'} to re-enqueue rows left with "
                "delete_retryable=true and an expired claim after the lifecycle "
                "queue exhausted its retries into the DLQ."
            ),
            targets=[
                targets.LambdaFunction(
                    api_fn,
                    event=events.RuleTargetInput.from_object(
                        {"source": "delete.reconciler"}
                    ),
                )
            ],
        )
        _consumer_reserved = int(
            CFG.get("scaler", {}).get("lifecycle_consumer_concurrency", 50)
        )
        lifecycle_consumer = _lambda.Function(
            self,
            "LifecycleConsumer",
            function_name="openclaw-lifecycle-consumer",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.lambda_handler",
            # 同一份 handler 代码资产(consumer 复用 api 的 lambda_handler,
            # 由 event 里的 Records/eventSource=aws:sqs 路由到消费分支)
            code=_lambda.Code.from_asset(
                "deploy/lambda/api",
                bundling=BundlingOptions(
                    image=cdk.DockerImage.from_registry(_sam_build_image_for_host()),
                    # VirtioFS bind-mount 拒写(见 api_fn 处注释),用 VOLUME_COPY。
                    bundling_file_access=BundlingFileAccess.VOLUME_COPY,
                    command=[
                        "bash",
                        "-c",
                        "pip install --no-cache-dir "
                        "--platform manylinux2014_aarch64 "
                        "--implementation cp --python-version 3.12 "
                        "--only-binary=:all: --upgrade "
                        "-r requirements.txt -t /asset-output "
                        "&& cp -au . /asset-output",
                    ],
                ),
            ),
            # #411/6.4 — consumer 必须活到能看完最慢的同步动作。
            # #422 codex round2 #6 — suspend/restore 走 consumer 同步执行,最坏链路远超原
            # 360s:suspend 同步 RequestResponse invoke backup Lambda + stop-vm(30s)+
            # rm(30s);restore 同步 _ssm_run launch(300s)。360s 会把合法执行硬杀在中途、
            # 卡中间态(suspending/restoring)。队列 visibility(:232)同步提到 >900s 防重投叠加。
            # rebuild(300s)等旧动作不受影响。
            #
            # #565 G1-a 更正 —— 原注释写「提到 Lambda 上限 900s **覆盖最坏 backup 预算**」,
            # 那是个错前提:同步 invoke 的实际上界从来不是被调方的 timeout,而是**调用侧的
            # socket read_timeout**。在本 issue 之前那三处调用点用裸 client、吃 botocore
            # 默认 60s,于是 900 这个数在同步路径上永远到不了。
            # 两个 900s 的角色因此要分清:
            #   · 本 consumer 的 900s 与 backup_fn 的 900s 都是**外壳上界**(进程被杀的那条线),
            #     不是任何一段预算;
            #   · 同步备份这一段的预算由调用侧 read_timeout 决定 —— 现为 330s,取值口径见
            #     `core/clients.BACKUP_SYNC_INVOKE_CONFIG`。
            # 真实业务死线(客户口径 180s/600s)仍未落地,归 #564 与 #565 G1。
            timeout=Duration.seconds(900),
            memory_size=2048,
            # 限流阀:consumer 并发上限 = SSM/host 可承受速率(削峰核心)
            reserved_concurrent_executions=_consumer_reserved,
            # 同 api 配置(共享 _api_env)。
            # Cognito pool/client id 在下方 Cognito 段对两个 Lambda 都 add_environment。
            environment=dict(_api_env),
        )
        # #604 —— consumer 也需要 LIFECYCLE_QUEUE_URL。原注释写的是"consumer 不入队故不给",
        # 那个前提现在不成立:consumer 要对**自己刚消费的那条消息**调
        # `sqs:ChangeMessageVisibility`,把良性 flock-skip 的重投退避从队列默认的 960s 缩到
        # 秒级(否则该消息占住 per-tenant FIFO 组头 16 分钟,同租户后续生命周期操作全撞 409
        # LIFECYCLE_IN_FLIGHT —— 三次真机复现)。改可见性不是入队,仍然不给 SendMessage。
        #
        # **必须显式声明的另一个理由**:验收环境的 consumer 上这个 env 其实
        # 已经存在,是历史漂移;靠漂移工作意味着下一次 `cdk deploy` 会把它删掉,而代码里
        # `if not LIFECYCLE_QUEUE_URL: return` 是静默 no-op —— 修复会无声失效,且不红不告警。
        lifecycle_consumer.add_environment(
            "LIFECYCLE_QUEUE_URL", lifecycle_queue.queue_url
        )
        # #152/#118 — consumer runs the SAME create_tenant handler (queue replay),
        # so it must see CLAWPOOL_CMK_ARN too or the queued-create path would
        # reject valid injections. Gated identically (only when feature on).
        if clawpool_cmk is not None:
            lifecycle_consumer.add_environment("CLAWPOOL_CMK_ARN", clawpool_cmk.key_arn)
        # consumer 同 api 权限(同代码路径,要读写表/调 SSM/发事件)
        tenants_table.grant_read_write_data(lifecycle_consumer)
        hosts_table.grant_read_write_data(lifecycle_consumer)
        groups_table.grant_read_write_data(lifecycle_consumer)
        audit_table.grant_read_write_data(lifecycle_consumer)
        batch_jobs_table.grant_read_write_data(lifecycle_consumer)
        # #413 P1/P2 — rebuild attempts/results share the image-ops ledger.
        image_jobs_table.grant_read_write_data(lifecycle_consumer)
        # #187 P1 — consumer replays create_tenant which now mints gateway token.
        # Same grants as api_fn (secrets table r/w + CMK encrypt + GenerateRandom).
        # **No kms:Decrypt** — API side never decrypts (INTERFACE-CONTRACT §5,
        # ciphertext is folded into GET responses verbatim; caller decrypts).
        tenant_secrets_table.grant_read_write_data(lifecycle_consumer)
        # #264 — consumer replay 走 config_template / injected_parameters /
        # env_injected_credentials 分支时(tenant_service.py:751/806)调
        # registry_service.load_current_snapshot → param-registry 表 Query,补种
        # 时还 PutItem/transact_write。api_fn 有此 grant(:382)但 consumer 漏,
        # 带模板/凭据注入的租户 replay 时 AccessDenied → 穿窄 except → 重试进
        # DLQ → 永久卡 creating/queued(默认可达:lifecycle_queue+create_via_queue 均默认开)。
        param_registry_table.grant_read_write_data(lifecycle_consumer)
        # #394 codex NB2 —— consumer replay 走 create_tenant 的 canary 分支时,
        # _persist_tenant_record 用 TransactWriteItems(snapshot ConditionCheck + tenant Put)
        # 把"租户固定版本"与"删快照"线性化。与上面 #264 同一类漏授权:api_fn 给了(:359)但
        # consumer 漏 → 真机实测 AccessDeniedException(ConditionCheckItem on version-snapshots)
        # → 穿 except → 消息重试进 DLQ → canary 租户永远建不出来(202 queued 后凭空消失)。
        # 需要:snapshot 表读(resolve/校验)+ 事务两个 action 覆盖被检查表与被写表。
        if version_snapshots_table is not None:
            version_snapshots_table.grant_read_data(lifecycle_consumer)
            lifecycle_consumer.add_to_role_policy(iam.PolicyStatement(
                actions=["dynamodb:TransactWriteItems", "dynamodb:ConditionCheckItem"],
                resources=[
                    version_snapshots_table.table_arn,
                    tenants_table.table_arn,
                ],
            ))
        # #412 — 队列化 delete 在 lifecycle_consumer 里跑,dispatch 预留的租户走令牌化释放
        # (_release_capacity_reservation:TransactWriteItems 扣 hosts + 清 tenants 令牌)。
        # 与 snapshot 事务分开、无条件授权(hosts+tenants),漏加则 delete 消费令牌时 AccessDenied。
        lifecycle_consumer.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:TransactWriteItems"],
            resources=[
                hosts_table.table_arn,
                tenants_table.table_arn,
            ],
        ))
        if clawpool_cmk is not None:
            clawpool_cmk.grant_encrypt(lifecycle_consumer)
            lifecycle_consumer.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["kms:GenerateRandom"],
                    resources=["*"],
                )
            )
        assets_bucket.grant_read(lifecycle_consumer)
        assets_bucket.grant_put(lifecycle_consumer)
        # #422 codex-blocker — suspend/restore 走 _async_actions 由 consumer 异步执行,
        # consumer 复用 _api_env(含 BACKUP_BUCKET)但缺 IAM grant → _resolve_backup 读备份桶
        # AccessDenied → suspend 停 VM/释放 slot 后消息重试、409 被 ack → 租户永久卡 suspending。
        # 与 api_fn 的 backup_bucket.grant_read 对称补上(consumer 只读备份,写归 backup Lambda)。
        if backup_bucket is not None:
            backup_bucket.grant_read(lifecycle_consumer)
        # #564 G5 —— consumer 复用 `_api_env` 且读同一份死线参数,所以必须和 api_fn 一样授权。
        # 这条与上面 #422 那个 blocker 是同一种形态:**复用了 env 却漏了 IAM**,表现是运行时
        # AccessDenied 后静默回落默认值 —— 客户改了参数、api 侧生效了、consumer 侧没生效,
        # 而两边跑的是同一份代码,日志上极难看出来。资源同样精确到死线前缀(穿透风险见上)。
        lifecycle_consumer.add_to_role_policy(_dl_param_policy)
        lifecycle_queue.grant_consume_messages(lifecycle_consumer)
        # consumer emits the create-latency SLA metric on the create path.
        # #432 —— namespace 条件同 api_fn 一并加上 OpenClaw/Dispatch:consumer 走
        # create_via_queue 时会执行完整 create_tenant,那条路径上 dispatch_service 也可能
        # 发熔断指标(DispatchCircuitOpen),命名空间不在条件里就会被 IAM 拒。
        lifecycle_consumer.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": [
                            "OpenClaw/ControlPlane",
                            "OpenClaw/Dispatch",
                        ]
                    }
                },
            )
        )
        # BUGFIX (loop 2026-07-01, 真机抓出): consumer 走 create_via_queue 时执行
        # 完整 create_tenant/tenant_action,需要与 api_fn 同款的 SSM(发 launch-vm)、
        # EC2、ALB target-group/rule、ASG 权限——之前只 grant 了表/队列/S3/CW,漏了
        # 这些,导致 consumer 消费 create 消息时 AccessDenied(ssm:SendCommand /
        # elasticloadbalancing:CreateTargetGroup),租户永远卡 creating、消息进 DLQ。
        # 注释一直写"consumer 同 api 权限"但代码没落实,现补齐。
        # 注:#62 IAM 收窄后 ssm_policy/ec2_policy 拆成多条 statement,
        # 用 _attach_shared_policies 一次挂上,不再 add_to_role_policy 单条。
        _attach_shared_policies(lifecycle_consumer)
        lifecycle_consumer.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "elasticloadbalancing:DescribeRules",
                    "elasticloadbalancing:DescribeTargetGroups",
                    "elasticloadbalancing:DescribeListeners",
                    "elasticloadbalancing:CreateTargetGroup",
                    "elasticloadbalancing:DeleteTargetGroup",
                    "elasticloadbalancing:RegisterTargets",
                    "elasticloadbalancing:DeregisterTargets",
                    "elasticloadbalancing:CreateRule",
                    "elasticloadbalancing:ModifyRule",
                    "elasticloadbalancing:DeleteRule",
                ],
                resources=["*"],
            )
        )
        lifecycle_consumer.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "autoscaling:DescribeAutoScalingGroups",
                    "autoscaling:SetDesiredCapacity",
                    "autoscaling:CompleteLifecycleAction",
                    "autoscaling:TerminateInstanceInAutoScalingGroup",
                ],
                resources=["*"],
            )
        )
        assets_bucket.grant_delete(lifecycle_consumer)
        # #264 — consumer replay create/delete 时 audit._publish_event 发 SNS
        # (core/audit.py:42,tenant_service.py:1705/1997/2581)。api_fn(:599)有
        # grant_publish 但 consumer 漏 → notifications.enabled=true 时 queued 租户
        # 的生命周期通知被 audit.py:51 静默吞("SNS publish failed"),订阅方看到
        # "部分租户有通知部分没"。与 api_fn 同门控(仅 notifications_topic 建了才授)。
        if notifications_topic is not None:
            notifications_topic.grant_publish(lifecycle_consumer)
        # consumer 跟 api_fn 共享 _api_env;Cognito pool/client id 等后置
        # add_environment 的 key,在 Cognito 段对 api_fn 和本 consumer 都加
        # (见下方 _lifecycle_consumer 引用)。存引用供 Cognito 段使用。
        self._lifecycle_consumer = lifecycle_consumer
        # #263 — ESM ScalingConfig.MaximumConcurrency 限流阀:治批删削峰的核心。
        # 不加就是"consumer 按活跃 MessageGroup 数任意并发"(30 个不同租户 delete →
        # 最高 30 并发砸向少数 host),撞 SSM 单 host 的 CommandWorkersLimit、饿死
        # launch-vm/start/stop。aws-cdk-lib 2.x 的 SqsEventSource 不直接暴露
        # ScalingConfig kwarg(见 dispatch_infra.py:253),用 add_event_source_mapping
        # + add_property_override 落 CFN 属性(与 dispatch ESM 同款,验证过的做法)。
        #
        # ⚠ **#565 G5 更正:上面那句原写着「撞 CommandWorkersLimit=5」,容易被读成
        # 「所以这个并发不能提」。顾虑是真的,结论是错的 —— 正确表述是【两侧必须成对提】。**
        # 我自己先掉进过这个坑:把 #469 的「host 侧 20→50 无改善」当成"host 侧提了没用",
        # 而那次绑死的是**控制面 API**(QPS 20 → 约 30 次 SendCommand/s → 89 次
        # ThrottlingException、502 占 89.5%),此时 host 侧 worker 多少都不影响结果。
        # 用户 2026-08-25 在真机上直接探测(不压控制面速率、每档有 agent 重启时间戳为证):
        # 默认 5 下发 12 条严格分 3 批每批 5;**20/10 得 20 同时执行**;50/25 得 50 ——
        # **host 侧线性生效**。所以那个 5 不是天花板,是**没配过**的默认值。
        #
        # 现在 host 侧由 `deploy/userdata/init-host.sh` 的 step1c 写成 **20**(buffer 10),
        # 而这条耦合两头都有闸:`tests/test_565_g5_agent_worker_parity.py` 钉住
        # `_HOST_SSM_COMMAND_WORKERS`(见文件顶部)与那个 shell 里的值相等,下面的 fail-loud
        # 钉住**部署真正用的** `config.yml` 值不超过它。**只提一侧即红。**
        # 提之前先读那个文件里写的第三道墙:SendCommand 服务端限流实测约 6.6 rps,
        # 且**不能自助提额** —— 提过某个点之后失败只是从"agent 排队"换成"控制面被限流"。
        _lc_max_conc = int(CFG.get("scaler", {}).get("lifecycle_max_concurrency", 10))
        # AWS 硬下限 2,上限 1000;且 reserved ≥ max_concurrency(否则部署行为异常)。
        # fail-loud 比 synth 过、CFN 报错或线上限流失效更好定位。
        if not (2 <= _lc_max_conc <= 1000):
            raise ValueError(
                f"scaler.lifecycle_max_concurrency={_lc_max_conc} out of range 2..1000 "
                "(SQS Lambda ESM ScalingConfig hard limits)."
            )
        if _lc_max_conc > _consumer_reserved:
            raise ValueError(
                f"scaler.lifecycle_max_concurrency={_lc_max_conc} exceeds "
                f"lifecycle_consumer_concurrency={_consumer_reserved}; reserved must be "
                ">= max_concurrency (AWS hard constraint) or the ESM can't scale to it."
            )
        # #565 G5 —— 与 host 侧 SSM worker 上限成对。超了不会报错、只会**静默变慢再到点判死**
        # (多出来的并发堆在 agent 前面排队,而排队时间算在客户死线里),所以在 synth 期 fail-loud。
        # 一条动作里若将来并发下发多条 SSM 命令,这个 1:1 对齐就不够了 —— 那时要按条数折算。
        if _lc_max_conc > _HOST_SSM_COMMAND_WORKERS:
            raise ValueError(
                f"scaler.lifecycle_max_concurrency={_lc_max_conc} exceeds host-side SSM "
                f"agent Mds.CommandWorkersLimit={_HOST_SSM_COMMAND_WORKERS} "
                "(deploy/userdata/init-host.sh step1c). Raise BOTH or neither: the extra "
                "concurrency would just queue in front of the agent, and queueing time is "
                "charged against the customer deadline."
            )
        _lc_esm = lifecycle_consumer.add_event_source_mapping(
            "LifecycleQueueEsm",
            event_source_arn=lifecycle_queue.queue_arn,
            # #411/6.4 codex(round3) — batch_size=1(原 10)。原来一个 invocation 串行处理
            # 10 条:两个各 ~300s 的 rebuild 就超过 consumer 360s 硬超时,invocation 被杀 →
            # 已完成的前几条副作用在重投时重放;且 503 后继续处理同组后续消息 = FIFO 组内
            # 乱序(rebuild 失败被后到的 stop/start 越过)。每次只取 1 条:单条最长 = rebuild
            # 300s < 360s 有余量,失败重投的就是那一条、天然不越序。吞吐由 consumer 的
            # reserved concurrency(不同租户不同 MessageGroup 并发)保证,不受 batch_size 影响。
            batch_size=1,
            report_batch_item_failures=True,
            enabled=True,
        )
        _lc_cfn_esm = _lc_esm.node.default_child
        if _lc_cfn_esm is not None:
            _lc_cfn_esm.add_property_override(
                "ScalingConfig", {"MaximumConcurrency": _lc_max_conc}
            )
        cdk.CfnOutput(self, "LifecycleQueueUrl", value=lifecycle_queue.queue_url)
    # 1.4.1 (#63) — Console skills CRUD: api Lambda writes SKILL.md
    # via PUT /skills/{name} and removes the skills/{name}/ prefix
    # via DELETE /skills/{name}.
    assets_bucket.grant_put(api_fn)
    assets_bucket.grant_delete(api_fn)
    # Issue #13 — allow publishing tenant lifecycle events
    if notifications_topic is not None:
        notifications_topic.grant_publish(api_fn)
    # #62 IAM 收窄:ssm_policy + ec2_policy 各拆成多条 statement,
    # 用 _attach_shared_policies 一次挂上。原 ssm_policy/ec2_policy 两次
    # add_to_role_policy 合并到这里(下方 ec2_policy 那行已删)。
    _attach_shared_policies(api_fn)
    # Phase 2 — emit the TenantCreateLatencySeconds SLA metric. PutMetricData
    # can't be resource-scoped (no ARNs), so it's namespace-conditioned to keep it
    # least-privilege.
    #
    # #432 —— 加上 `OpenClaw/Dispatch`。**这是真机抓出来的**:条件只写了
    # `OpenClaw/ControlPlane`,所以任何发到 `OpenClaw/Dispatch` 的指标都被 IAM 拒:
    #     AccessDenied ... not authorized to perform: cloudwatch:PutMetricData
    # 影响不止 #432 的 poller 心跳 —— `dispatch_service._emit_circuit_open()` 发的
    # `DispatchCircuitOpen`(熔断信号)用的就是那个 namespace,也就是说**熔断指标一直发不出去**,
    # 而它的 except 是 fail-safe(打日志不抛),所以这件事从未响过。
    # 用 namespace 列表而不是放开 `*`:least-privilege 不因为多一个命名空间而放弃。
    cw_metrics_policy = iam.PolicyStatement(
        actions=["cloudwatch:PutMetricData"],
        resources=["*"],
        conditions={
            "StringEquals": {
                "cloudwatch:namespace": ["OpenClaw/ControlPlane", "OpenClaw/Dispatch"]
            }
        },
    )
    api_fn.add_to_role_policy(cw_metrics_policy)
    # task #15 — read the LiteLLM master key secret to mint per-tenant
    # vkeys. Scoped to the configured secret (or all secrets named
    # openclaw-litellm-* if config gives a name prefix). Only granted when
    # billing is configured.
    _billing_secret = CFG.get("billing", {}).get("master_key_secret", "")
    if _billing_secret:
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{_billing_secret}*"
                ],
            )
        )
    # Go-live A1: read the external-authz HMAC secret (CFN dynamic ref above
    # injects it at deploy; this grants the runtime GetSecretValue if needed
    # for rotation tooling). Scoped to the configured secret name.
    if _ext_authz_cfg.get("enabled", False) and _ext_authz_secret_name:
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{_ext_authz_secret_name}*"
                ],
            )
        )
    # #62 IAM 收窄:ec2_policy 已经由 _attach_shared_policies(api_fn) 挂上,
    # 这里删掉旧的单条 add(ssm_policy + ec2_policy 两次调用合并成一次
    # _attach_shared_policies)。
    api_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=[
                "autoscaling:DescribeAutoScalingGroups",
                "autoscaling:SetDesiredCapacity",
                "autoscaling:CompleteLifecycleAction",
                # #510 —— 终止钩子的 HeartbeatTimeout 是 120s,而 #509 的撤离对每个租户做一次
                # 同步备份(实测 6.2s、最坏到 backup Lambda 的 SSM 上限 300s)。约 19 个租户就
                # 把 120s 走完 → ASG 放行终止 → 剩下的租户连数据盘一起消失,而一台 host 容量是
                # 几百个租户。cleanup_terminated_host 因此每撤一个租户前续一次心跳把窗口撑开。
                # 少这一条权限,续心跳会 AccessDenied 被日志吞掉,修复静默失效(真机实测:该权限
                # 原本不在本策略里,只有 CompleteLifecycleAction)。
                "autoscaling:RecordLifecycleActionHeartbeat",
                "autoscaling:TerminateInstanceInAutoScalingGroup",
            ],
            resources=["*"],
        )
    )

    # ========== API Gateway ==========
    # #423 解法 A:主 API 必须在本 build 函数内拿到 execute-api VPCE,故把 VPC 与
    # VPCE 创建前移到主 API 定义之前;network_vpc.py 后续只从 ctx 读取同一 VPC。
    vpc = _build_vpc(self, CFG.get("network", {}) or {})
    _api_cfg = CFG.get("api", {}) or {}
    # #496 — 复用开关,形状照 logging.aos.create_secretsmanager_vpce(#309)。
    # AWS 硬规则:同一 VPC 同一服务只允许一个开 private DNS 的 Interface VPCE。导入客户已有
    # VPC 时那里常常已经有一个 execute-api 端点(别的系统在用),而这里原来是**无条件**自建 →
    # CreateVpcEndpoint 被拒(`private-dns-enabled cannot be set because there is already a
    # conflicting DNS domain`)→ 整栈回滚,且上游没有任何开关能让它复用。真机在 2026-08-13
    # 首次部署时实撞过一次,只能改代码才继续。
    # 默认 true = 保持存量行为(自建),所以既有部署不受影响。
    _create_execute_api_vpce = bool(_api_cfg.get("create_execute_api_vpce", True))
    _reuse_vpce_id = str(_api_cfg.get("execute_api_vpce_id", "") or "").strip()
    if _create_execute_api_vpce:
        _priv_vpce_sg = ec2.SecurityGroup(
            self,
            "ExecuteApiVpceSg",
            vpc=vpc,
            description="execute-api VPCE - HTTPS 443 from within VPC only (issue 122)",
            allow_all_outbound=False,
        )
        _priv_vpce_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "HTTPS from VPC CIDR to execute-api VPCE",
        )
        _execute_api_vpce = ec2.InterfaceVpcEndpoint(
            self,
            "ExecuteApiVpce",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.APIGATEWAY,  # = execute-api
            private_dns_enabled=True,
            security_groups=[_priv_vpce_sg],
            open=False,  # 不自动按 CIDR 放行,完全由上面 SG 控
        )
    else:
        # fail-loud:PRIVATE RestApi 必须绑定一个 execute-api VPCE。少了 id 而放它过去,
        # 得到的是一个「建成了但谁都调不到」的 API —— 那比 synth 报错难查得多。
        if not _reuse_vpce_id:
            raise ValueError(
                "api.create_execute_api_vpce=false 时必须同时给 api.execute_api_vpce_id "
                "(要复用的那个 private-DNS execute-api 端点的 vpce-xxxx)。查法:"
                "aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=<VPC> "
                "Name=service-name,Values=com.amazonaws.<REGION>.execute-api "
                "--query 'VpcEndpoints[?PrivateDnsEnabled==`true`].VpcEndpointId'"
            )
        _execute_api_vpce = ec2.InterfaceVpcEndpoint.from_interface_vpc_endpoint_attributes(
            self,
            "ImportedExecuteApiVpce",
            vpc_endpoint_id=_reuse_vpce_id,
            port=443,
        )
    cdk.CfnOutput(
        self, "ExecuteApiVpceId", value=_execute_api_vpce.vpc_endpoint_id
    )

    _vpce_allowlist = [
        str(v).strip()
        for v in (_api_cfg.get("vpce_ids") or [])
        if str(v).strip()
    ]
    if not _vpce_allowlist:
        _vpce_allowlist = [_execute_api_vpce.vpc_endpoint_id]
    elif not _create_execute_api_vpce and _reuse_vpce_id not in _vpce_allowlist:
        # #496 — 复用的端点必须在放行名单里。写了 vpce_ids 却漏掉被复用的那个,请求会从
        # 它进来并被 aws:SourceVpce 条件拒成 403:栈是 CREATE_COMPLETE,API 却谁都调不通。
        _vpce_allowlist = [*_vpce_allowlist, _reuse_vpce_id]
    # #423 — 主 API method 是 AuthorizationType=NONE(x-api-key 应用层门),匿名
    # 调用方没有 IAM identity policy 提供 Allow,故 PRIVATE endpoint 必须由 resource
    # policy 显式 Allow,否则两侧都沉默会隐式拒绝、全部 403。安全评审结论仍成立:
    # 绝不加无条件 Allow AnyPrincipal;这里的 Allow 绑死 aws:SourceVpce 白名单,
    # 与下面 Deny 形成白名单内放行 + 白名单外显式拒绝的双向锁。
    _api_resource_policy = iam.PolicyDocument(
        statements=[
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=["execute-api:Invoke"],
                resources=["execute-api:/*"],
                conditions={"StringEquals": {"aws:SourceVpce": _vpce_allowlist}},
            ),
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["execute-api:Invoke"],
                resources=["execute-api:/*"],
                conditions={"StringNotEquals": {"aws:SourceVpce": _vpce_allowlist}},
            )
        ]
    )
    api = apigw.RestApi(
        self,
        "Api",
        rest_api_name="openclaw-orchestrator",
        deploy_options=apigw.StageOptions(stage_name="v1"),
        endpoint_configuration=apigw.EndpointConfiguration(
            types=[apigw.EndpointType.PRIVATE],
            vpc_endpoints=[_execute_api_vpce],
        ),
        policy=_api_resource_policy,
        default_cors_preflight_options=apigw.CorsOptions(
            allow_origins=apigw.Cors.ALL_ORIGINS,
            allow_methods=apigw.Cors.ALL_METHODS,
            # #394 — If-Match(cleanup-canary 的 CAS)、Idempotency-Key(promote/
            # cleanup 的幂等键)是浏览器眼中的自定义请求头,不在 allow-headers 里就会被 CORS
            # 预检拦掉 → 请求根本到不了 Lambda(前端只看到 "discard failed",Lambda 无日志)。
            allow_headers=[
                "Content-Type", "x-api-key", "Authorization",
                "If-Match", "Idempotency-Key",
            ],
        ),
    )

    # API Key + Usage Plan
    api_key = api.add_api_key(
        "ApiKey",
        api_key_name="openclaw-admin-key",
    )
    # API-key usage-plan throttle. The old default (rate 10 / burst 20) was a
    # hard scale ceiling: 300 concurrent POST /tenants on 2026-06-29 saw 173/300
    # rejected with 429 (burst=20 hit) before the control plane was even
    # exercised. Operator batch ops (bulk launch/stop) go through this api-key
    # plan, so it must clear the target peak. Bumped default to 500/1000 and
    # made it config-driven; per-IP protection is a separate WAF rate rule below.
    plan = api.add_usage_plan(
        "UsagePlan",
        name="openclaw-plan",
        throttle=apigw.ThrottleSettings(
            rate_limit=int(_api_cfg.get("throttle_rate_limit", 500)),
            burst_limit=int(_api_cfg.get("throttle_burst_limit", 1000)),
        ),
        api_stages=[apigw.UsagePlanPerApiStage(api=api, stage=api.deployment_stage)],
    )
    plan.add_api_key(api_key)

    # ========== #108 per-platform scoped API keys (config-gated, default off) ==========
    # Closes the god-key IDOR: one openclaw-admin-key today grants full-fleet
    # access, so handing it to any third-party platform leaks every platform's
    # tenants. When `api.platform_keys` is configured, each listed platform
    # gets its OWN APIGW key + usage plan, and a REQUEST authorizer resolves
    # which platform the presented key belongs to (via PlatformKeyMap: sha256
    # of the key → platform_id) and injects requestContext.authorizer.platform_id.
    # The handler then scopes list/get/action/create/delete to that namespace
    # (stage 1). DEFAULT OFF: with no platform_keys config the block is skipped
    # entirely → byte-identical single-key deploy (backward compatible).
    #
    # The legacy openclaw-admin-key stays as the operator super-key (not in the
    # map → no platform_id injected → full-fleet, internal ops only). Removing
    # it is an irreversible credential change left to a human decision (#0-C).
    _platform_keys = _api_cfg.get("platform_keys") or []
    _platform_authorizer = None
    if _platform_keys:
        # PlatformKeyMap: PK=key_hash (sha256 hex of the API key value — NEVER
        # the plaintext key), field platform_id. The authorizer reads it; an
        # operator seeds it out-of-band (create key → put {sha256(value),
        # platform_id}). RETAIN so a stack replace never drops the mapping and
        # silently downgrades every scoped key to unscoped (a security regression).
        platform_key_table = dynamodb.Table(
            self,
            "PlatformKeyMap",
            partition_key=dynamodb.Attribute(
                name="key_hash", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=_pitr_spec,
        )
        authorizer_fn = _lambda.Function(
            self,
            "PlatformAuthorizer",
            function_name="openclaw-platform-authorizer",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("deploy/lambda/platform_authorizer"),
            timeout=Duration.seconds(10),
            memory_size=2048,
            environment={"PLATFORM_KEY_TABLE": platform_key_table.table_name},
        )
        platform_key_table.grant_read_data(authorizer_fn)
        # REQUEST authorizer keyed on the x-api-key header. identity_source makes
        # API GW cache per distinct key value (results_cache) — same key → one
        # authorizer invoke per TTL, not per request.
        _platform_authorizer = apigw.RequestAuthorizer(
            self,
            "PlatformKeyAuthorizer",
            handler=authorizer_fn,
            identity_sources=[apigw.IdentitySource.header("x-api-key")],
            results_cache_ttl=Duration.minutes(5),
        )
        # One key + one usage plan per configured platform. Each plan carries
        # its own throttle (per-platform rate limiting, DoD) so one platform
        # can't exhaust another's budget.
        for _pk in _platform_keys:
            _pid = str(_pk.get("id", "")).strip()
            if not _pid:
                continue
            _pkey = api.add_api_key(
                f"PlatformKey{_pid}",
                api_key_name=f"openclaw-platform-{_pid}",
            )
            _pplan = api.add_usage_plan(
                f"PlatformPlan{_pid}",
                name=f"openclaw-plan-{_pid}",
                throttle=apigw.ThrottleSettings(
                    rate_limit=int(_pk.get("throttle_rate_limit", 100)),
                    burst_limit=int(_pk.get("throttle_burst_limit", 200)),
                ),
                api_stages=[
                    apigw.UsagePlanPerApiStage(api=api, stage=api.deployment_stage)
                ],
            )
            _pplan.add_api_key(_pkey)

    # ========== WAF (issue #7, optional) ==========
    waf_cfg = CFG.get("waf", {}) or {}
    if waf_cfg.get("enabled", False):
        rate_limit = int(waf_cfg.get("rate_limit_per_ip", 1000))
        evaluation_window_sec = int(waf_cfg.get("evaluation_window_sec", 300))
        if evaluation_window_sec not in (60, 120, 300, 600):
            raise ValueError(
                "waf.evaluation_window_sec must be one of "
                f"{{60, 120, 300, 600}}, got {evaluation_window_sec!r}"
            )
        if not 10 <= rate_limit <= 2000000000:
            raise ValueError(
                "waf.rate_limit_per_ip must be in [10, 2000000000], "
                f"got {rate_limit!r}"
            )

        _waf_per_ip_rps = rate_limit / evaluation_window_sec
        _api_throttle_rate_limit = int(
            (CFG.get("api", {}) or {}).get("throttle_rate_limit", 100)
        )
        # 这里只警告、不 raise:仓内现有配置本来就是 500 vs 1000/300。若改成硬失败,
        # 每次照 runbook 部署都会被阻断,把可诊断性缺陷升级成部署阻塞。这条门只负责
        # 让两层限流的矛盾在 synth 输出可见,安全姿态仍由运维明确取舍。
        if _waf_per_ip_rps < _api_throttle_rate_limit:
            print(
                "[#632 waf.rate_limit] WARNING: "
                f"waf.rate_limit_per_ip={rate_limit} / "
                f"waf.evaluation_window_sec={evaluation_window_sec} = "
                f"{_waf_per_ip_rps:.2f} req/s, "
                f"api.throttle_rate_limit={_api_throttle_rate_limit} req/s; "
                "单来源 IP 的真实上限是这两者取小。"
            )
        # 安全加固(task #25):无论 config 怎么配,代码侧总加 SQLi + IP 信誉
        # 两条 baseline,作为不可被 config.yml 静默裁掉的安全底线(同 IMDS 加固
        # 的显式不可回退姿态)。SQLi→OWASP A03 注入;IpReputation→A06/A10 已知
        # 恶意 IP。dict.fromkeys 对 config∪baseline 去重保序(WebACL 重复规则名
        # 会 synth 失败)。规则名对照 AWS managed rule groups reference。
        _waf_baseline = [
            "AWSManagedRulesSQLiRuleSet",
            "AWSManagedRulesAmazonIpReputationList",
        ]
        managed_rule_names = list(
            dict.fromkeys(list(waf_cfg.get("managed_rules", []) or []) + _waf_baseline)
        )

        rules = []
        priority = 0
        # Rule #1: rate-based per source IP. Always added when WAF is enabled.
        rules.append(
            wafv2.CfnWebACL.RuleProperty(
                name="RateLimitPerIP",
                priority=priority,
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                        limit=rate_limit,
                        aggregate_key_type="IP",
                        # 显式 300 与 AWS 隐式默认同值,运行时行为不变;只是把 req/s
                        # 折算分母写进代码和模板。AWS 只允许 60/120/300/600。
                        evaluation_window_sec=evaluation_window_sec,
                    ),
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    sampled_requests_enabled=True,
                    metric_name="OpenClawRateLimit",
                ),
            )
        )
        priority += 1

        # AWS managed rule groups (CommonRuleSet, KnownBadInputs, etc.)
        for rule_name in managed_rule_names:
            rules.append(
                wafv2.CfnWebACL.RuleProperty(
                    name=rule_name,
                    priority=priority,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name=rule_name,
                        ),
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        sampled_requests_enabled=True,
                        metric_name=rule_name,
                    ),
                )
            )
            priority += 1

        web_acl = wafv2.CfnWebACL(
            self,
            "ApiWebACL",
            name="openclaw-api-acl",
            scope="REGIONAL",  # API Gateway is regional. CloudFront would need scope=CLOUDFRONT (us-east-1 only).
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                sampled_requests_enabled=True,
                metric_name="OpenClawApiACL",
            ),
            rules=rules,
        )

        # Build the API Gateway stage ARN: arn:aws:apigateway:{region}::/restapis/{id}/stages/{stage}
        stage_arn = Fn.join(
            "",
            [
                "arn:",
                cdk.Aws.PARTITION,
                ":apigateway:",
                cdk.Aws.REGION,
                "::/restapis/",
                api.rest_api_id,
                "/stages/",
                api.deployment_stage.stage_name,
            ],
        )
        web_acl_association = wafv2.CfnWebACLAssociation(
            self,
            "ApiWebACLAssociation",
            resource_arn=stage_arn,
            web_acl_arn=web_acl.attr_arn,
        )
        web_acl_association.add_dependency(web_acl)

        if waf_cfg.get("logging_enabled", True):
            _waf_log_group_name = str(
                waf_cfg.get("log_group_name", "aws-waf-logs-openclaw-api")
            ).strip()
            if not _waf_log_group_name.startswith("aws-waf-logs-"):
                raise ValueError(
                    "waf.log_group_name must start with 'aws-waf-logs-', "
                    f"got {_waf_log_group_name!r}"
                )

            waf_log_group = logs.LogGroup(
                self,
                "ApiWafLogGroup",
                log_group_name=_waf_log_group_name,
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            )
            waf_logging = wafv2.CfnLoggingConfiguration(
                self,
                "ApiWafLoggingConfiguration",
                resource_arn=web_acl.attr_arn,
                log_destination_configs=[waf_log_group.log_group_arn],
                # issue 只要求至少留下 BLOCK 证据;千级 lifecycle 下全量 KEEP
                # 只会放大 CloudWatch Logs 成本,默认丢弃非 BLOCK。
                #
                # **`LoggingFilter` 必须写成 PascalCase 的裸 dict**:CDK 的
                # `LoggingFilterProperty` 对 `AWS::WAFv2::LoggingConfiguration` 不做 key
                # 大小写转换,会渲染成 `defaultBehavior` / `filters[].behavior`,而 CFN
                # 只认 `DefaultBehavior` / `Filters[].Behavior`
                # (见 aws-resource-wafv2-loggingconfiguration.html 的官方示例)。
                # 后果不是 synth 报错而是**部署期静默不生效**,所以 tests/test_waf.py
                # 逐键断言 PascalCase。
                logging_filter={
                    "DefaultBehavior": "DROP",
                    "Filters": [
                        {
                            "Behavior": "KEEP",
                            "Requirement": "MEETS_ANY",
                            "Conditions": [
                                {"ActionCondition": {"Action": "BLOCK"}}
                            ],
                        }
                    ],
                },
                # x-api-key 是凭据,绝不能进入 WAF 日志;即使只保留 BLOCK 也必须脱敏。
                #
                # `authorization` 同样必须脱敏,而且漏了它比漏 x-api-key 更糟:WAF 日志
                # 的 `httpRequest.headers` 是**全表**(logging-fields.html:"headers —
                # The list of headers"),而本文件 CORS 放行了 `Authorization`(见上文
                # allow_headers),控制台 BFF 走 Cognito JWT、`CTRL_API_AUTH_MODE=iam`
                # 走 SigV4,两者都把凭据放在这个头里。只 redact x-api-key 的话,每条被
                # BLOCK 的请求都会把可重放的 bearer token 完整写进 CloudWatch Logs ——
                # 那正是这段代码新建出来的日志沉淀点,等于亲手造了一个凭据泄漏面。
                # `RedactedFields` 只接受 UriPath / QueryString / SingleHeader / Method
                # 四种,上限 100 项(API_LoggingConfiguration.html),两项都合法。
                #
                # 外层用 `FieldToMatchProperty`(它会把 `single_header` 正确渲染成
                # `SingleHeader`),内层的 `{"Name": ...}` 必须是裸 dict:
                # `SingleHeaderProperty(name=…)` 会渲染成小写 `name`,CFN 不认 → redact
                # 静默失效 → api key 进日志。反过来整个元素写成裸 dict 也不行:jsii 会
                # 按 `FieldToMatchProperty` 的字段名过滤,`SingleHeader` 不是合法 kwarg,
                # 整项被抹成 `{}`。两种错法都不报错,只有断言能抓住。
                #
                # 头名必须小写:WAF 匹配 header 名前先全部转小写,写 `Authorization`
                # 会匹配不上、redact 静默失效(与 x-api-key 同一条规则)。
                redacted_fields=[
                    wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                        single_header={"Name": "x-api-key"}
                    ),
                    wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                        single_header={"Name": "authorization"}
                    ),
                ],
            )
            waf_logging.add_dependency(web_acl)

    key_required = {"api_key_required": True}
    # #108 — when per-platform keys are configured, attach the REQUEST
    # authorizer to every keyed method so requestContext.authorizer.platform_id
    # reaches the handler. Off by default → key_required stays exactly as before
    # (backward-compatible single-key deploy). CORS preflight (OPTIONS) is added
    # by default_cors_preflight_options WITHOUT this dict, so it stays unauthorized.
    if _platform_authorizer is not None:
        key_required = {
            "api_key_required": True,
            "authorizer": _platform_authorizer,
            "authorization_type": apigw.AuthorizationType.CUSTOM,
        }

    # ── Lambda permission policy size fix (deploy-blocking) ──
    # Each `LambdaIntegration(api_fn)` makes CDK attach a *separate*
    # AWS::Lambda::Permission scoped to that one method's ARN. With ~29
    # routes the function's resource-based policy crossed Lambda's hard
    # 20480-byte limit, so EVERY `cdk deploy` failed with
    # "The final policy size (20485) is bigger than the limit (20480)".
    # Fix: grant API Gateway invoke ONCE via a wildcard source ARN, and
    # build integrations against an *imported* view of the function.
    # CDK does not auto-add per-method permissions for an imported
    # IFunction (it assumes it doesn't own it), so the policy stays at a
    # single statement regardless of how many routes we add.
    # #149 — API GW invoke permission 给 alias（不再直接指 function）
    _apigw_source_arn = Fn.join(
        "",
        [
            "arn:",
            cdk.Aws.PARTITION,
            ":execute-api:",
            cdk.Aws.REGION,
            ":",
            cdk.Aws.ACCOUNT_ID,
            ":",
            api.rest_api_id,
            "/*/*",
        ],
    )
    api_fn_alias.add_permission(
        "ApiGwInvokeAlias",
        principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
        action="lambda:InvokeFunction",
        source_arn=_apigw_source_arn,
    )
    # 用 alias ARN 作 imported view——API GW 从此只认 alias
    _api_fn_view = _lambda.Function.from_function_arn(
        self,
        "ApiHandlerView",
        api_fn_alias.function_arn,
    )

    def _li():
        """A LambdaIntegration pointing to the alias (not $LATEST).
        The single wildcard permission above authorises every method."""
        return apigw.LambdaIntegration(_api_fn_view)

    tenants_resource = api.root.add_resource("tenants")
    tenants_resource.add_method("GET", _li(), **key_required)
    tenants_resource.add_method("POST", _li(), **key_required)
    tenant_stats_resource = api.root.add_resource("tenants-stats")
    tenant_stats_resource.add_method("GET", _li(), **key_required)

    # self-service: POST /tenants/self — a logged-in user provisions their
    # own node. Literal `self` is matched before the `{id}` greedy param by
    # API Gateway, so it doesn't collide with /tenants/{id}.
    tenant_self_resource = tenants_resource.add_resource("self")
    tenant_self_resource.add_method("POST", _li(), **key_required)

    tenant_resource = tenants_resource.add_resource("{id}")
    tenant_resource.add_method("GET", _li(), **key_required)
    tenant_resource.add_method("DELETE", _li(), **key_required)

    # tenant-credential-contract: 出站凭据子资源(字面段,优先于 {action} 贪婪匹配)
    tenant_creds_resource = tenant_resource.add_resource("credentials")
    tenant_creds_resource.add_method("GET", _li(), **key_required)

    tenant_action = tenant_resource.add_resource("{action}")
    tenant_action.add_method("POST", _li(), **key_required)
    tenant_action.add_method("GET", _li(), **key_required)

    # tenant-credential-contract: Parameter_Registry 管理接口(admin-only,handler 内校验)
    registry_resource = api.root.add_resource("registry")
    registry_tpl_resource = registry_resource.add_resource("{config_template}")
    registry_tpl_resource.add_method("GET", _li(), **key_required)
    registry_tpl_resource.add_method("POST", _li(), **key_required)
    registry_rollback_resource = registry_tpl_resource.add_resource("rollback")
    registry_rollback_resource.add_method("POST", _li(), **key_required)

    # tenant-credential-contract: Recipient_Public_Key 管理接口(admin-only)
    recipient_key_resource = api.root.add_resource("recipient-key")
    recipient_key_resource.add_method("GET", _li(), **key_required)
    recipient_key_resource.add_method("POST", _li(), **key_required)
    recipient_key_disable_resource = recipient_key_resource.add_resource("disable")
    recipient_key_disable_resource.add_method("POST", _li(), **key_required)

    # #149 asymmetric-v1 — serve the RSA CMK PUBLIC key so callers can locally
    # OAEP-encrypt env creds before POST /tenants (env_injected_credentials).
    rsa_pubkey_resource = api.root.add_resource("clawpool-rsa-public-key")
    rsa_pubkey_resource.add_method("GET", _li(), **key_required)

    # #389 v2 块5 — bootstrap 版本切换(admin-only,handler 内 identity 门)。
    #   GET  /bootstrap/versions   列 host+edge 可切换版本 + 各 fleet 当前启动摘要
    #   POST /bootstrap/promote    切到某个已存在的 S3 bootstrap 版本(传 sha256,不传脚本内容)
    bootstrap_resource = api.root.add_resource("bootstrap")
    bootstrap_versions_resource = bootstrap_resource.add_resource("versions")
    bootstrap_versions_resource.add_method("GET", _li(), **key_required)
    bootstrap_promote_resource = bootstrap_resource.add_resource("promote")
    bootstrap_promote_resource.add_method("POST", _li(), **key_required)

    hosts_resource = api.root.add_resource("hosts")
    hosts_resource.add_method("GET", _li(), **key_required)
    hosts_resource.add_method("POST", _li(), **key_required)

    host_resource = hosts_resource.add_resource("{instance_id}")
    host_resource.add_method("DELETE", _li(), **key_required)
    taint_resource = host_resource.add_resource("taint")
    taint_resource.add_method("POST", _li(), **key_required)
    taint_resource.add_method("DELETE", _li(), **key_required)
    # #309 V1 — POST /hosts/{instance_id}/pull-image?snapshot_time=<ISO>: 照 DDB 快照按
    # 精确 VersionId 拉 deployment/rootfs/(镜像三盘+manifest),校验 etag 后 copy+unzip 装 live。
    # 只作用一台 host。Admin op (x-api-key)。
    pull_image_resource = host_resource.add_resource("pull-image")
    pull_image_resource.add_method("POST", _li(), **key_required)
    # #309 — GET /hosts/{instance_id}/pull-image-progress:tail host 上 /tmp/<job_id>.txt。
    pull_image_progress_resource = host_resource.add_resource("pull-image-progress")
    pull_image_progress_resource.add_method("GET", _li(), **key_required)
    # #309 — POST /hosts/{instance_id}/copy-file-from-s3:单文件 S3→EC2(目标限资产目录白名单)。
    copy_file_resource = host_resource.add_resource("copy-file-from-s3")
    copy_file_resource.add_method("POST", _li(), **key_required)
    # #394 step5 — 同步槽位操作(admin-only,handler 内 identity 门):只改 host 上 slots.json
    # 一个小文件,不搬盘,故走同步 200(不需要 progress 轮询)。
    promote_canary_resource = host_resource.add_resource("promote-canary")
    promote_canary_resource.add_method("POST", _li(), **key_required)
    # #394 —— 无 rollback-image 路由:回滚 = pull 老版到 live(pull-image,快路径秒级翻指针)。
    # #394 — POST /hosts/{instance_id}/reclaim-images:回收无人引用的版本目录(手动 prune)。
    reclaim_images_resource = host_resource.add_resource("reclaim-images")
    reclaim_images_resource.add_method("POST", _li(), **key_required)
    # #394 — GET /hosts/{instance_id}/image-slots:真机实读 host 磁盘镜像状态(slots.json +
    # versions/),DDB 镜像的权威对照。viewer 可读(handler 内不额外 admin 门,只读)。
    # #394 —— 无 DELETE image-slots/canary(cleanup-canary 已移除,精简 API):放弃 canary 靠下次
    # pull 覆盖 / promote 清空,不再提供显式清指针接口。
    image_slots_resource = host_resource.add_resource("image-slots")
    image_slots_resource.add_method("GET", _li(), **key_required)

    backups_resource = api.root.add_resource("backups")
    backups_resource.add_method("GET", _li(), **key_required)

    # 10h-goal #19 — GET /images: golden-image inventory + live manifest.
    # (per-tenant data snapshot reuses GET /tenants/{id}/{action} action=data)
    images_resource = api.root.add_resource("images")
    images_resource.add_method("GET", _li(), **key_required)
    # #394 — POST /delete-image-snapshot: 软删一条快照记录(引用保护 → 409 IMAGE_VERSION_IN_USE)。
    # body {snapshot_time},与 create-image-snapshot 对称(不用 path 带冒号的 ISO 时间)。
    # 只标 status=deleted,不动 S3 镜像文件。operator+。
    delete_snapshot_resource = api.root.add_resource("delete-image-snapshot")
    delete_snapshot_resource.add_method("POST", _li(), **key_required)

    # #337(原#217 /snapshots)— GET /list_image_versions: 列镜像版本快照(time+label+count),
    # console 选 snapshot_time 拉。改名避免与 /images(列镜像文件)混淆。
    snapshots_resource = api.root.add_resource("list_image_versions")
    snapshots_resource.add_method("GET", _li(), **key_required)

    # #376 — POST /create-image-snapshot: 打一个版本快照(等价 snapshot-version.sh):
    # 扫 deployment/ 全量对象 → 写 openclaw-version-snapshots 表。operator+(不在 _VIEWER_OK)。
    # 路径用连字符(与 pull-image/copy-file-from-s3/refresh-rootfs 等一致)。
    create_snapshot_resource = api.root.add_resource("create-image-snapshot")
    create_snapshot_resource.add_method("POST", _li(), **key_required)

    # 1.4.0 (#62) — Groups CRUD endpoints
    groups_resource = api.root.add_resource("groups")
    groups_resource.add_method("GET", _li(), **key_required)
    groups_resource.add_method("POST", _li(), **key_required)
    group_resource = groups_resource.add_resource("{name}")
    group_skills_resource = group_resource.add_resource("skills")
    group_skills_resource.add_method("POST", _li(), **key_required)
    group_skill_resource = group_skills_resource.add_resource("{skill}")
    group_skill_resource.add_method("DELETE", _li(), **key_required)

    # Issue #23 — batch operations: POST /batch/tenants
    batch_resource = api.root.add_resource("batch")
    batch_tenants_resource = batch_resource.add_resource("tenants")
    batch_tenants_resource.add_method("POST", _li(), **key_required)
    # PRD #54 — async batch job progress: GET /batch/jobs/{job_id}
    batch_jobs_resource = batch_resource.add_resource("jobs")
    batch_job_resource = batch_jobs_resource.add_resource("{job_id}")
    batch_job_resource.add_method("GET", _li(), **key_required)

    # PRD #50-58 — control-plane scale-out: per-tenant-user fleet management.
    #   GET  /users/{tenant_user_id}/tenants   indexed, paginated fleet list
    #   GET  /users/{tenant_user_id}/summary   node count + per-status buckets
    #   POST /users/{tenant_user_id}/action    bulk start/stop the user's fleet
    users_resource = api.root.add_resource("users")
    user_resource = users_resource.add_resource("{tenant_user_id}")
    user_tenants_resource = user_resource.add_resource("tenants")
    user_tenants_resource.add_method("GET", _li(), **key_required)
    user_summary_resource = user_resource.add_resource("summary")
    user_summary_resource.add_method("GET", _li(), **key_required)
    user_action_resource = user_resource.add_resource("action")
    user_action_resource.add_method("POST", _li(), **key_required)
    user_upgrade_resource = user_resource.add_resource("upgrade")
    user_upgrade_resource.add_method("POST", _li(), **key_required)

    # Go-live A1 — POST /external/authz: the external backend pushes the
    # authoritative user↔tenant mapping. Auth is an HMAC signature verified
    # inside the handler (not Cognito); keeps x-api-key for shared throttling.
    external_resource = api.root.add_resource("external")
    external_authz_resource = external_resource.add_resource("authz")
    external_authz_resource.add_method("POST", _li(), **key_required)

    # claw-channel — POST /chat/sign: verify Cognito JWT, HMAC-sign a
    # {sub,text} envelope for the per-VM signed webhook. Replaces the bare
    # /v1/chat/completions path the mini-app used to hit. Keeps x-api-key
    # (shared throttling key); identity comes from the verified Bearer JWT.
    chat_resource = api.root.add_resource("chat")
    chat_sign_resource = chat_resource.add_resource("sign")
    chat_sign_resource.add_method("POST", _li(), **key_required)

    refresh_rootfs_resource = hosts_resource.add_resource("refresh-rootfs")
    refresh_rootfs_resource.add_method("POST", _li(), **key_required)

    rootfs_version_resource = hosts_resource.add_resource("rootfs-version")
    rootfs_version_resource.add_method("GET", _li(), **key_required)

    # Phase 8 — fleet power (start/stop EVERY VM via host-local fan-out).
    fleet_power_resource = hosts_resource.add_resource("fleet-power")
    fleet_power_resource.add_method("POST", _li(), **key_required)

    # #566 拆分② — fleet guest 出网防火墙运维 API:POST /hosts/egress
    # (mode=deny|off,一次改全部或指定 host 的 OPENCLAW-EGRESS 链)。api-key admin 门。
    #
    # 这一组路由【必须在这里声明】。handler.py 的路由表不会自动变成 API GW 资源 ——
    # 漏声明的表现是真实 curl 拿到 "Missing Authentication Token"(= 路由不存在),
    # 而所有直调 handler 的单测照样全绿(见 tests/test_apigw_routes.py 的开篇说明)。
    egress_resource = hosts_resource.add_resource("egress")
    egress_resource.add_method("POST", _li(), **key_required)
    # #577 只读收敛报告(支持 limit / instance_ids 定点查询)。
    egress_resource.add_method("GET", _li(), **key_required)
    # #603 命名版本 + 逐台回滚 + 当前生效链从内核回读。
    egress_revisions_resource = egress_resource.add_resource("revisions")
    egress_revisions_resource.add_method("GET", _li(), **key_required)
    # 删版本记录 = 销毁可回滚历史,所以服务端另有一道 confirm=="DELETE" 的门;
    # 这里只负责让路由存在(漏声明的表现是 curl 拿 Missing Authentication Token)。
    egress_revisions_resource.add_method("DELETE", _li(), **key_required)
    egress_chain_resource = egress_resource.add_resource("chain")
    egress_chain_resource.add_method("GET", _li(), **key_required)
    egress_rollback_resource = egress_resource.add_resource("rollback")
    egress_rollback_resource.add_method("POST", _li(), **key_required)
    # #668 —— POST /hosts/egress/allow/validate(只读 dry-run,同 admin 门与 API key)
    egress_allow_resource = egress_resource.add_resource("allow")
    egress_allow_validate_resource = egress_allow_resource.add_resource("validate")
    egress_allow_validate_resource.add_method("POST", _li(), **key_required)

    # #517 stage 4 — submit a bounded rolling upgrade and poll its progress.
    rolling_upgrade_resource = hosts_resource.add_resource("rolling-upgrade")
    rolling_upgrade_resource.add_method("POST", _li(), **key_required)
    rolling_jobs_resource = hosts_resource.add_resource("rolling-jobs")
    rolling_job_resource = rolling_jobs_resource.add_resource("{job_id}")
    rolling_job_resource.add_method("GET", _li(), **key_required)

    # Phase 4 — rootfs drift (which tenants are NOT on the current version).
    rootfs_drift_resource = hosts_resource.add_resource("rootfs-drift")
    rootfs_drift_resource.add_method("GET", _li(), **key_required)

    agentcore_resource = api.root.add_resource("agentcore")
    agentcore_status_resource = agentcore_resource.add_resource("status")
    agentcore_status_resource.add_method("GET", _li(), **key_required)
    agentcore_tools_resource = agentcore_resource.add_resource("tools")
    agentcore_tools_resource.add_method("GET", _li(), **key_required)

    # /system/info — feature flags + config snapshot for the console
    system_resource = api.root.add_resource("system")
    system_info_resource = system_resource.add_resource("info")
    system_info_resource.add_method("GET", _li(), **key_required)
    system_queues_resource = system_resource.add_resource("queues")
    system_queues_resource.add_method("GET", _li(), **key_required)

    # /audit-log — already created earlier in the routes, but the
    # resource needs to exist on the REST API; declare it here once.
    audit_log_resource = api.root.add_resource("audit-log")
    audit_log_resource.add_method("GET", _li(), **key_required)

    # ========== Health Check Lambda ==========
    hc_cfg = CFG.get("health_check", {}) or {}
    az_failover_cfg = hc_cfg.get("az_failover", {}) or {}
    health_fn = _lambda.Function(
        self,
        "HealthCheck",
        function_name="openclaw-health-check",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="handler.lambda_handler",
        code=_lambda.Code.from_asset("deploy/lambda/health_check"),
        timeout=Duration.seconds(
            180
        ),  # 1.3.1: room for synchronous SSM wait during failover
        memory_size=2048,
        # 1.3.2: prevent concurrent invocations from racing on the same
        # tenant migration. EventBridge fires every 5 min, but failover
        # can take 60-90s of synchronous SSM waits — if a long-running
        # invocation hasn't finished when the next tick fires, we used
        # to get two Lambdas both trying to migrate the same stale
        # tenants. Reserved concurrency=1 makes Lambda queue the second
        # invocation behind the first, restoring serialization.
        reserved_concurrent_executions=1,
        environment={
            "TENANTS_TABLE": tenants_table.table_name,
            "HOSTS_TABLE": hosts_table.table_name,
            "AUDIT_TABLE": audit_table.table_name,
            "SNS_TOPIC_ARN": notifications_topic_arn,
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            # ALB_LISTENER_ARN injected after listener creation (see below)
            "AZ_FAILOVER_ENABLED": str(
                bool(az_failover_cfg.get("enabled", True))
            ).lower(),
            "AZ_UNHEALTHY_THRESHOLD_MINUTES": str(
                int(az_failover_cfg.get("unhealthy_threshold_minutes", 10))
            ),
            "AZ_COOLDOWN_MINUTES": str(
                int(az_failover_cfg.get("cooldown_minutes", 30))
            ),
            "BACKUP_PREFIX": CFG["s3"]["backup_prefix"],
        },
    )
    # #199 同类缺陷第三处:failover 的 path-A 要 list 备份,而备份写在 BACKUP_BUCKET
    # (backup-data.sh:16 `${BACKUP_BUCKET:-${ASSETS_BUCKET}}`)。不注入 → handler 侧回退
    # 到 assets 桶 → 永远 list 空 → 每个租户都被 no-backup 拒绝,AZ failover 实质不可用。
    # 判空 fail-safe 与 api_fn(:331)同款:不建备份桶的部署不注入,读侧自然回退 assets。
    if backup_bucket is not None:
        health_fn.add_environment("BACKUP_BUCKET", backup_bucket.bucket_name)
        # env 指对了 IAM 也得给 —— 否则 list_objects_v2 AccessDenied,现象和"没备份"
        # 一样(见 :558 同一个坑)。只读:写备份是 backup Lambda 的事。
        backup_bucket.grant_read(health_fn)
    tenants_table.grant_read_write_data(health_fn)
    hosts_table.grant_read_write_data(health_fn)
    # #412 — reaper 对带 capacity_reservation_id 的卡 creating 租户走令牌化释放
    # (creating→failed + 扣 hosts 账本 + 清令牌一个 TransactWriteItems)。
    # grant_read_write_data 不含 TransactWriteItems,漏加则 reaper 释放时 AccessDenied。
    health_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["dynamodb:TransactWriteItems"],
        resources=[
            hosts_table.table_arn,
            tenants_table.table_arn,
        ],
    ))
    # #469 P6 —— 中间态卡死指标(OpenClaw/Lifecycle 的 LifecycleStuckMarked /
    # LifecycleStuckUnconfirmed)。真机冒烟实测:不加这条会 AccessDenied,指标发不出去
    # → 那两个 CloudWatch 告警永远没有数据点 = 卡死仍然只能等客户报障,P6 白做。
    # 这类漏授权【单测看不见】(单测把 cloudwatch client mock 掉了),与上面 #412 那条
    # TransactWriteItems 漏加是同一类坑,注释一并留在这里。
    # cloudwatch:PutMetricData 不支持资源级限制(只能 "*"),故用 condition 把它锁死在
    # 本项目自己的 namespace 上,避免这个角色能往任意 namespace 写指标。
    health_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={
                "StringEquals": {"cloudwatch:namespace": "OpenClaw/Lifecycle"}
            },
        )
    )
    audit_table.grant_write_data(health_fn)
    assets_bucket.grant_read(health_fn)  # 1.3.1: list backups for failover
    if notifications_topic is not None:
        notifications_topic.grant_publish(health_fn)
    # 1.3.1: ALB rule re-pointing during cross-host failover.
    health_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=[
                "elasticloadbalancing:DescribeRules",
                "elasticloadbalancing:DescribeTargetGroups",
                "elasticloadbalancing:CreateRule",
                "elasticloadbalancing:ModifyRule",
                "elasticloadbalancing:CreateTargetGroup",
                "elasticloadbalancing:RegisterTargets",
            ],
            resources=["*"],
        )
    )
    _attach_ssm_policies(health_fn)  # #62 IAM 收窄:拆 SSM 多 statement
    # #52 —— 心跳失效降级的第二重判据:SSM 侧还看不看得见这台 host。
    # 只挂 health_fn 而【不】进 _attach_ssm_policies 的共享组:那组共享给 5 个 Lambda,
    # 塞进去等于顺手给 ApiHandler/Scaler/Backup 都开机队清单读权限,与 #62 收窄方向相反
    # (起初就是那么写的,cdk diff 暴露出四个 role 的 policy 全被改动才收窄到这里)。
    # DescribeInstanceInformation 不支持资源级 IAM,故 resources=["*"];纯只读。
    health_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:DescribeInstanceInformation"],
            resources=["*"],
        )
    )

    events.Rule(
        self,
        "HealthCheckSchedule",
        schedule=events.Schedule.rate(
            Duration.minutes(CFG["health_check"]["interval_minutes"])
        ),
        targets=[targets.LambdaFunction(health_fn)],
    )

    # ========== Skills Lambda ==========
    skills_fn = _lambda.Function(
        self,
        "Skills",
        function_name="openclaw-skills",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="handler.lambda_handler",
        code=_lambda.Code.from_asset("deploy/lambda/skills"),
        timeout=Duration.seconds(30),
        memory_size=2048,
        environment={
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            # 1.4.0 (#62) — needed for ?tenant=... per-tenant scope filtering
            "TENANTS_TABLE": tenants_table.table_name,
            "GROUPS_TABLE": groups_table.table_name,
        },
    )
    assets_bucket.grant_read(skills_fn)
    # 1.4.0 (#62) — read-only access to compute effective skill sets
    tenants_table.grant_read_data(skills_fn)
    groups_table.grant_read_data(skills_fn)
    skills_resource = api.root.add_resource("skills")
    skills_resource.add_method(
        "GET", apigw.LambdaIntegration(skills_fn), **key_required
    )
    # 1.4.1 (#63) — per-skill CRUD goes through api Lambda (reuses RBAC + audit log)
    skill_resource = skills_resource.add_resource("{name}")
    skill_resource.add_method("GET", _li(), **key_required)
    skill_resource.add_method("PUT", _li(), **key_required)
    skill_resource.add_method("DELETE", _li(), **key_required)

    # ========== Templates Lambda ==========
    templates_fn = _lambda.Function(
        self,
        "Templates",
        function_name="openclaw-templates",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="handler.lambda_handler",
        code=_lambda.Code.from_asset("deploy/lambda/templates"),
        timeout=Duration.seconds(30),
        memory_size=2048,
        environment={"ASSETS_BUCKET": assets_bucket.bucket_name},
    )
    assets_bucket.grant_read_write(templates_fn)
    templates_resource = api.root.add_resource("templates")
    templates_resource.add_method(
        "GET", apigw.LambdaIntegration(templates_fn), **key_required
    )
    template_item = templates_resource.add_resource("{name}")
    template_item.add_method(
        "GET", apigw.LambdaIntegration(templates_fn), **key_required
    )
    template_item.add_method(
        "PUT", apigw.LambdaIntegration(templates_fn), **key_required
    )
    template_item.add_method(
        "DELETE", apigw.LambdaIntegration(templates_fn), **key_required
    )

    # ========== Scaler Lambda (idle host reclaim) ==========
    scaler_fn = _lambda.Function(
        self,
        "Scaler",
        function_name="openclaw-scaler",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="handler.lambda_handler",
        code=_lambda.Code.from_asset("deploy/lambda/scaler"),
        timeout=Duration.seconds(60),
        memory_size=2048,
        environment={
            "HOSTS_TABLE": hosts_table.table_name,
            "TENANTS_TABLE": tenants_table.table_name,
            "ASG_NAME": "openclaw-hosts-asg",
            "IDLE_TIMEOUT_MINUTES": str(CFG["scaler"]["idle_timeout_minutes"]),
            # task #21 — seamless rolling image refresh (gated OFF until verified)
            "IMAGE_REFRESH_ENABLED": str(
                CFG.get("scaler", {}).get("image_refresh_enabled", False)
            ).lower(),
            "REFRESH_INTERVAL_HOURS": str(
                CFG.get("scaler", {}).get("refresh_interval_hours", 48)
            ),
            "REFRESH_MAX_PER_TICK": str(
                CFG.get("scaler", {}).get("refresh_max_per_tick", 1)
            ),
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            "ROOTFS_PREFIX": CFG["s3"]["rootfs_prefix"],
            "BACKUP_PREFIX": CFG["s3"]["backup_prefix"],
            # 10h-goal #17 — reserve-capacity warm pool (gated OFF until verified)
            "RESERVE_ENABLED": str(
                CFG.get("scaler", {}).get("reserve_enabled", False)
            ).lower(),
            "RESERVE_PCT": str(CFG.get("scaler", {}).get("reserve_pct", 20)),
            "RESERVE_CORES": str(CFG.get("scaler", {}).get("reserve_cores", 0)),
            "RESERVE_SCALE_STEP": str(
                CFG.get("scaler", {}).get("reserve_scale_step", 1)
            ),
            "CPU_OVERCOMMIT_RATIO": str(
                CFG.get("host", {}).get("cpu_overcommit_ratio", 1.0)
            ),
        },
    )
    hosts_table.grant_read_write_data(scaler_fn)
    # Issue #15 — TTL processing reads tenants and updates status (stop/delete)
    tenants_table.grant_read_write_data(scaler_fn)
    # #62 IAM 收窄:SSM 拆多 statement;stop-vm.sh 走 SSM SendCommand,
    # instance ARN 带 Project=openclaw/Role=metal-host 条件。
    _attach_ssm_policies(scaler_fn)
    # task #21 — read rootfs manifest (current golden version) for refresh
    assets_bucket.grant_read(scaler_fn)
    scaler_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=[
                "autoscaling:DescribeAutoScalingGroups",
                "autoscaling:TerminateInstanceInAutoScalingGroup",
                # #264 — 两个躲在默认关开关后的缺失,一开就 AccessDenied:
                # SetDesiredCapacity: _ensure_reserve_capacity(handler.py:192,RESERVE_ENABLED=true 预留扩容)
                # DescribeAutoScalingInstances: _lifecycle_terminating(handler.py:330,IDLE_RECLAIM_ENABLED=true 防双扣 desired)
                "autoscaling:SetDesiredCapacity",
                "autoscaling:DescribeAutoScalingInstances",
            ],
            resources=["*"],
        )
    )
    events.Rule(
        self,
        "ScalerSchedule",
        schedule=events.Schedule.rate(
            Duration.minutes(CFG["scaler"]["interval_minutes"])
        ),
        targets=[targets.LambdaFunction(scaler_fn)],
    )

    # ========== Backup Lambda (daily data backup) ==========
    backup_fn = _lambda.Function(
        self,
        "Backup",
        function_name="openclaw-backup",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="handler.lambda_handler",
        code=_lambda.Code.from_asset("deploy/lambda/backup"),
        timeout=Duration.seconds(900),
        memory_size=2048,
        environment={
            "TENANTS_TABLE": tenants_table.table_name,
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            "BACKUP_BUCKET": backup_bucket.bucket_name,  # WORM + CMK 备份专用桶
            "BACKUP_CMK_KEY_ID": backup_cmk.key_id,
            "BACKUP_PREFIX": CFG["s3"]["backup_prefix"],
            # PRD 2.6 错峰+限并发:每租户距上次备份超 INTERVAL_HOURS 才备(错峰),
            # 单次触发最多 BATCH_LIMIT 个(削并发)。配合高频 schedule 滚动覆盖全量。
            "BACKUP_INTERVAL_HOURS": str(CFG["s3"].get("backup_interval_hours", 24)),
            "BACKUP_BATCH_LIMIT": str(CFG["s3"].get("backup_batch_limit", 20)),
        },
        # #564 G6 ② —— 通道 D(网关手动备份)的异步失败出口。手动备份走
        # `InvocationType="Event"` 派发到本函数(`tenant_service` 的手动备份分支),此前
        # 这个函数**没有任何** DLQ:异步 worker 里的未处理异常在 AWS 重试耗尽后无声消失,
        # 而客户手里只有一个 202 —— 那正是 issue 零节那句「接口返回成功、实际操作没有发生,
        # 且调用方无从察觉」。理由与 api_fn 那处相同,`retry_attempts` / `max_event_age`
        # 同样不动(说明见 api_fn 处)。
        dead_letter_queue_enabled=True,
    )
    tenants_table.grant_read_write_data(backup_fn)
    assets_bucket.grant_read_write(backup_fn)
    backup_bucket.grant_read_write(backup_fn)  # 备份写入 + 恢复读取
    backup_cmk.grant_encrypt_decrypt(backup_fn)  # CMK 解密权限只授备份执行者
    _attach_ssm_policies(backup_fn)  # #62 IAM 收窄:拆 SSM 多 statement
    backup_fn.grant_invoke(api_fn)  # API Lambda async invokes Backup Lambda
    # #263 — 走 FIFO 队列的删除由 lifecycle_consumer 执行 delete_tenant,keep_data=false
    # 时它同步 invoke backup Lambda 做删前备份(铁律#4 不可逆操作前先保护)。consumer role
    # 缺 lambda:InvokeFunction → invoke AccessDenied → delete fail-closed 返 5xx → 消息卡
    # FIFO 无限重试、租户永久删不掉。真机实证(ap-southeast-1,2026-07-15):开
    # lifecycle_queue_enabled 后单删返 202 但消息卡 NotVisible、租户始终 running。
    if getattr(self, "_lifecycle_consumer", None) is not None:
        backup_fn.grant_invoke(self._lifecycle_consumer)

    # PRD 2.6: backup_cron 现在是"扫描节拍"而非"统一备份时间"——每次触发只备到期
    # 的一批(错峰+限并发)。配高频(如 rate(30 minutes))让全量在 INTERVAL_HOURS
    # 内滚动覆盖,避免开源版"写死统一时间全量同刻备份"。
    #
    # #469 R7 —— 定时全量已下沉到 host-agent 的 _backup_loop,本中心 schedule 默认【关】。
    # 为什么必须二者其一而不能并存:两侧都按 last_backup_at 判到期、都调同一个
    # backup-data.sh,同刻跑会对同一个数据盘并发起两次备份(该脚本会 Pause/Resume VM,
    # 两个实例交错 Resume 会让另一个备到"运行中的盘"→ 备份内容不一致)。
    #
    # 为什么留开关而不是直接删:①灰度 —— host-agent 是运行时从 S3 拉的,滚动升级期间
    # 老版本没有 _backup_loop,若此刻中心 schedule 已删,那些机器上的租户会完全不被备份;
    # ②回滚 —— host 侧出问题时把这个开关打回 true 即恢复中心调度,不必回滚 CDK 全栈。
    # 上线顺序:先铺 host-agent(全部机器都有 _backup_loop 且 /metrics 见 backup tick)
    # → 再把 backup_central_schedule_enabled 置 false → cdk deploy。
    # 缺键时默认 **True**(不是 False)。codex 独立复审抓出的真问题,而且是本改动里
    # 最危险的一条:已有部署的 config.yml 里没有这个新键,若缺省为 False,他们只要
    # `cdk deploy` 一次就会【静默停掉全部定时备份】—— 而那些机器上跑的 host-agent 是
    # 旧版、没有 _backup_loop,于是两侧都不备份,直到某天需要恢复时才发现。
    # 关闭中心调度必须是【显式动作】,且只在确认 host-agent 已铺完(所有机器 /metrics
    # 都见 loop="backup")之后才做。本仓的 config.yml/.example 已显式写 false,因为
    # 本仓的 host 会随本 MR 一起铺;外部部署保持旧行为直到他们自己决定切换。
    if CFG["s3"].get("backup_central_schedule_enabled", True):
        events.Rule(
            self,
            "BackupSchedule",
            schedule=events.Schedule.expression(CFG["s3"]["backup_cron"]),
            targets=[targets.LambdaFunction(backup_fn)],
        )

    # ========== #32 Audit archive Lambda (DDB Stream → WORM bucket) ==========
    # 触发: audit_table DDB Stream (NEW_IMAGE)。每条审计条目 put 后 Lambda 消费
    # 事件,把 NEW_IMAGE 反 marshal 成 JSON,PutObject 到 audit_archive_bucket
    # 分区路径 `<prefix>/<owner_id>/<yyyy>/<mm>/<dd>/<id>.json`。retention 靠
    # Object Lock,不设 lifecycle expiration(WORM 满周期后 lifecycle 可后加)。
    # 幂等:key 里带 audit_row_id(uuid4)→ PutObject 覆盖同 key 得到同版本内容,
    # 加上 bucket 版本化,重放不会导致 lost-update。
    if audit_archive_enabled:
        audit_archive_fn = _lambda.Function(
            self,
            "AuditArchiveFn",
            function_name="openclaw-audit-archive",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("deploy/lambda/audit_archive"),
            timeout=Duration.seconds(60),
            memory_size=2048,
            environment={
                "AUDIT_ARCHIVE_BUCKET": audit_archive_bucket.bucket_name,
                "AUDIT_ARCHIVE_PREFIX": audit_cfg.get(
                    "archive_prefix", "audit-archive"
                ),
                "AUDIT_ARCHIVE_CMK_KEY_ID": audit_archive_cmk.key_id,
            },
            # dead-letter: 消费失败进 DLQ 让工程可见,不静默吞
            dead_letter_queue_enabled=True,
        )
        audit_archive_bucket.grant_write(audit_archive_fn)
        audit_archive_cmk.grant_encrypt(audit_archive_fn)
        audit_archive_fn.add_event_source(
            lambda_event_sources.DynamoEventSource(
                audit_table,
                starting_position=_lambda.StartingPosition.TRIM_HORIZON,
                batch_size=100,
                bisect_batch_on_error=True,
                retry_attempts=3,
                # 只关心新增(NEW_IMAGE);删除/修改事件跳过——TTL 到期删是保留策略
                # 一部分,不需要归档;INSERT 是唯一有效通道。
                filters=[
                    _lambda.FilterCriteria.filter(
                        {"eventName": _lambda.FilterRule.is_equal("INSERT")}
                    )
                ],
            )
        )

    # ╓─── [包B 隔离安全] owner=B ── host角色/监控(host_role,被ASG/AMP/AgentCore引用)─╖

    # --- Pack onto ctx ---
    ctx._api_cfg = locals().get("_api_cfg")
    ctx._platform_authorizer = locals().get("_platform_authorizer")
    ctx.api = locals().get("api")
    ctx.api_fn = locals().get("api_fn")
    ctx.api_key = locals().get("api_key")
    ctx.execute_api_vpce = locals().get("_execute_api_vpce")
    ctx.health_fn = locals().get("health_fn")
    ctx.notifications_topic = locals().get("notifications_topic")
    ctx.notifications_topic_arn = locals().get("notifications_topic_arn")
    ctx.vpc = locals().get("vpc")
