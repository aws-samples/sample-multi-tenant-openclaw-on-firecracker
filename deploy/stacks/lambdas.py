# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_cloudwatch as cloudwatch,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_logs as logs,
    aws_sns as sns,
    aws_wafv2 as wafv2,
    aws_sqs as sqs,
    aws_lambda_event_sources as lambda_event_sources,
    BundlingOptions,
    BundlingFileAccess,
    Duration,
    Fn,
    RemovalPolicy,
)

from stacks._helpers import _sam_build_image_for_host, _read_pyproject_version


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
    tenant_secrets_table = getattr(ctx, "tenant_secrets_table", None)
    tenants_table = getattr(ctx, "tenants_table", None)

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
    # SSM GetCommandInvocation(只读)— 不支持资源级 IAM,保留 *
    ssm_readonly_policy = iam.PolicyStatement(
        actions=["ssm:GetCommandInvocation"],
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
        lifecycle_queue = sqs.Queue(
            self,
            "LifecycleQueue",
            queue_name="openclaw-lifecycle.fifo",
            fifo=True,
            # explicit MessageDeduplicationId (tenant_id:action) is set by the
            # producer; content_based_deduplication=True is a safety net for
            # any future producer that forgets to pass one.
            content_based_deduplication=True,
            # 可见性超时 ≥ consumer 处理时长(launch ~6s);留足够重试窗口
            visibility_timeout=Duration.seconds(180),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5, queue=lifecycle_dlq
            ),
        )
        # prod-config MR1(#212):lifecycle 队列的 DLQ 也要告警(此前只有 dispatch
        # DLQ 有,见 dispatch_infra.py:359)。lifecycle 消费端连续 5 次失败(SSM 打不动
        # host / launch-vm 崩)会把该 create/stop/start/delete 死信进 DLQ——一进就得人
        # 介入(租户永久卡态),不该只靠 dispatch DLQ 告警旁证。阈值 0 = 任何一条即告警。
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
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "openclaw-lifecycle-dlq has a message — a tenant lifecycle op "
                "(create/stop/start/delete) gave up after retries. Investigate: "
                "SSM RunCommand history, host-agent logs, tenant stuck state."
            ),
        )

    # ── Observability: X-Ray tracing + log retention (design.md §1) ──
    _obs_cfg = CFG.get("observability", {}) or {}
    _tracing_enabled = _obs_cfg.get("tracing_enabled", True)
    _tracing_mode = (
        _lambda.Tracing.ACTIVE if _tracing_enabled else _lambda.Tracing.PASS_THROUGH
    )
    _log_retention_days = int(_obs_cfg.get("log_retention_days", 30))
    _log_retention = {
        7: logs.RetentionDays.ONE_WEEK,
        30: logs.RetentionDays.ONE_MONTH,
        90: logs.RetentionDays.THREE_MONTHS,
        365: logs.RetentionDays.ONE_YEAR,
    }.get(_log_retention_days, logs.RetentionDays.ONE_MONTH)

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
        "PARAM_REGISTRY_TABLE": param_registry_table.table_name,
        "RECIPIENT_KEYS_TABLE": recipient_keys_table.table_name,
        "ASSETS_BUCKET": assets_bucket.bucket_name,
        # #199 fix — api_fn/consumer 的 _resolve_backup 需从 WORM+CMK 备份桶(非
        # assets 桶)list/resolve 备份;此前只注了 ASSETS_BUCKET → restore 永远
        # "backup not found"(备份数据在 backup 桶里但 resolve 拉错桶)。空串时
        # _resolve_backup 回退 ASSETS_BUCKET(兼容未建 backup_bucket 的旧部署)。
        "BACKUP_BUCKET": backup_bucket.bucket_name if backup_bucket else "",
        "NOTIFICATIONS_TOPIC_ARN": notifications_topic_arn,
        "ROOTFS_PREFIX": CFG["s3"]["rootfs_prefix"],
        "HOST_RESERVED_VCPU": str(CFG["host"]["reserved_vcpu"]),
        "HOST_RESERVED_MEM": str(CFG["host"]["reserved_mem_mb"]),
        "CPU_OVERCOMMIT_RATIO": str(CFG["host"].get("cpu_overcommit_ratio", 1.0)),
        "MEM_OVERCOMMIT_RATIO": str(CFG["host"].get("mem_overcommit_ratio", 1.0)),
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
        timeout=Duration.seconds(120),
        memory_size=2048,
        tracing=_tracing_mode,
        log_retention=_log_retention,
        environment=dict(_api_env),
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
    hosts_table.grant_read_write_data(api_fn)
    groups_table.grant_read_write_data(api_fn)
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
    # device identity ciphertext (the design spec · the data-plane contract §5).
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
    # #199 fix — _resolve_backup 从 backup 桶 list/resolve 备份(restore/迁移入口)。
    if backup_bucket:
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
        # 给 api_fn 发队列权限用**独立 iam.Policy 资源**(非 grant_send_messages、
        # 非 add_to_role_policy)。原因:那两者都往 api role 的 DefaultPolicy 注入
        # 对 queue 的依赖,而 API GW ApiDeployment 间接依赖 api role/Lambda,queue
        # 又被 consumer grant 一堆表 → circular dependency(2026-06-29 实撞)。
        # 独立 Policy 把 queue 依赖隔离在自己资源上,不污染 DefaultPolicy → 断环。
        # R10.2 — /system/queues 只读 lifecycle 队列+DLQ 深度用 GetQueueAttributes;
        # DLQ URL 注入 env(独立 Policy 隔离依赖,同断环理由)。
        if lifecycle_dlq is not None:
            api_fn.add_environment("LIFECYCLE_DLQ_URL", lifecycle_dlq.queue_url)
        _lifecycle_read_arns = [lifecycle_queue.queue_arn] + (
            [lifecycle_dlq.queue_arn] if lifecycle_dlq is not None else []
        )
        iam.Policy(
            self,
            "ApiLifecycleEnqueuePolicy",
            roles=[api_fn.role],
            statements=[
                iam.PolicyStatement(
                    actions=["sqs:SendMessage"],
                    resources=[lifecycle_queue.queue_arn],
                ),
                iam.PolicyStatement(
                    actions=["sqs:GetQueueAttributes"],
                    resources=_lifecycle_read_arns,
                ),
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
            timeout=Duration.seconds(180),
            memory_size=2048,
            tracing=_tracing_mode,
            log_retention=_log_retention,
            # 限流阀:consumer 并发上限 = SSM/host 可承受速率(削峰核心)
            reserved_concurrent_executions=_consumer_reserved,
            # 同 api 配置(共享 _api_env);consumer 不入队故不给 LIFECYCLE_QUEUE_URL。
            # Cognito pool/client id 在下方 Cognito 段对两个 Lambda 都 add_environment。
            environment=dict(_api_env),
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
        # #187 P1 — consumer replays create_tenant which now mints gateway token.
        # Same grants as api_fn (secrets table r/w + CMK encrypt + GenerateRandom).
        # **No kms:Decrypt** — API side never decrypts (the data-plane contract §5,
        # ciphertext is folded into GET responses verbatim; caller decrypts).
        tenant_secrets_table.grant_read_write_data(lifecycle_consumer)
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
        # #199 fix — consumer 也走 _resolve_backup(dispatch/queue 建租户带 restore_from)。
        if backup_bucket:
            backup_bucket.grant_read(lifecycle_consumer)
        lifecycle_queue.grant_consume_messages(lifecycle_consumer)
        # consumer emits the create-latency SLA metric on the create path.
        lifecycle_consumer.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {"cloudwatch:namespace": "OpenClaw/ControlPlane"}
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
        # consumer 跟 api_fn 共享 _api_env;Cognito pool/client id 等后置
        # add_environment 的 key,在 Cognito 段对 api_fn 和本 consumer 都加
        # (见下方 _lifecycle_consumer 引用)。存引用供 Cognito 段使用。
        self._lifecycle_consumer = lifecycle_consumer
        lifecycle_consumer.add_event_source(
            lambda_event_sources.SqsEventSource(
                lifecycle_queue,
                batch_size=10,
                report_batch_item_failures=True,
            )
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
    # can't be resource-scoped (no ARNs), so it's namespace-conditioned to
    # OpenClaw/ControlPlane to keep it least-privilege.
    cw_metrics_policy = iam.PolicyStatement(
        actions=["cloudwatch:PutMetricData"],
        resources=["*"],
        conditions={"StringEquals": {"cloudwatch:namespace": "OpenClaw/ControlPlane"}},
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
                "autoscaling:TerminateInstanceInAutoScalingGroup",
            ],
            resources=["*"],
        )
    )

    # ========== API Gateway ==========
    # #212 R1:EDGE resource policy — mode=private 或 edge_resource_policy_deny_public=true 时
    # 挂条 policy 让 EDGE 只对指定 VPCE 放行(其余 IP 拒),避免 mode=private 时 EDGE 还
    # 公网可达。默认 mode=edge → policy=None → 现状不变。
    _edge_api_cfg = CFG.get("api", {}) or {}
    _edge_mode_raw = str(_edge_api_cfg.get("mode", "")).strip().lower()
    _edge_legacy = _edge_api_cfg.get("private_api_enabled", None)
    if _edge_mode_raw:
        _edge_api_mode = _edge_mode_raw
    else:
        _edge_api_mode = "both" if bool(_edge_legacy) else "edge"
    _edge_deny_pub = _edge_api_mode == "private" or bool(
        _edge_api_cfg.get("edge_resource_policy_deny_public", False)
    )
    _edge_policy = None
    if _edge_deny_pub:
        # 通用 deny-any-non-vpce policy;private 模式下 EDGE 事实上应被 network_vpc 建的
        # execute-api VPCE 兜住(SourceVpce 条件用 aws:SourceVpce 通配未匹配即拒)。
        # 因 network_vpc 段在 lambdas 后跑,此处用 aws:SourceIp !aws:VpcSourceIp 二段式
        # 拒(拒所有源 IP 除非来自 VPC 内)。fail-safe:即便 EDGE 有公网 DNS,resource policy
        # 会挡下所有非 VPC 源。同时不影响后续 method AWS_IAM/api-key 门控。
        _edge_policy = iam.PolicyDocument(
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.DENY,
                    principals=[iam.AnyPrincipal()],
                    actions=["execute-api:Invoke"],
                    resources=["execute-api:/*"],
                    conditions={
                        "NotIpAddress": {
                            # 私有 IP 段(RFC1918 + link-local + carrier-grade NAT)
                            "aws:SourceIp": [
                                "10.0.0.0/8",
                                "172.16.0.0/12",
                                "192.168.0.0/16",
                            ]
                        }
                    },
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    principals=[iam.AnyPrincipal()],
                    actions=["execute-api:Invoke"],
                    resources=["execute-api:/*"],
                ),
            ]
        )
    api = apigw.RestApi(
        self,
        "Api",
        rest_api_name="openclaw-orchestrator",
        policy=_edge_policy,
        deploy_options=apigw.StageOptions(
            stage_name="v1",
            tracing_enabled=_tracing_enabled,
            access_log_destination=apigw.LogGroupLogDestination(
                logs.LogGroup(
                    self,
                    "ApiAccessLog",
                    log_group_name="/aws/apigateway/openclaw-orchestrator",
                    retention=logs.RetentionDays.ONE_MONTH,
                )
            ),
            access_log_format=apigw.AccessLogFormat.custom(
                '{"requestId":"$context.requestId",'
                '"traceId":"$context.xrayTraceId",'
                '"ip":"$context.identity.sourceIp",'
                '"method":"$context.httpMethod",'
                '"path":"$context.resourcePath",'
                '"status":"$context.status",'
                '"latency":"$context.responseLatency",'
                '"integrationLatency":"$context.integrationLatency"}'
            ),
        ),
        default_cors_preflight_options=apigw.CorsOptions(
            allow_origins=apigw.Cors.ALL_ORIGINS,
            allow_methods=apigw.Cors.ALL_METHODS,
            allow_headers=["Content-Type", "x-api-key", "Authorization"],
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
    _api_cfg = CFG.get("api", {}) or {}
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
            tracing=_tracing_mode,
            log_retention=_log_retention,
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
        wafv2.CfnWebACLAssociation(
            self,
            "ApiWebACLAssociation",
            resource_arn=stage_arn,
            web_acl_arn=web_acl.attr_arn,
        )

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

    hosts_resource = api.root.add_resource("hosts")
    hosts_resource.add_method("GET", _li(), **key_required)
    hosts_resource.add_method("POST", _li(), **key_required)

    host_resource = hosts_resource.add_resource("{instance_id}")
    host_resource.add_method("DELETE", _li(), **key_required)

    backups_resource = api.root.add_resource("backups")
    backups_resource.add_method("GET", _li(), **key_required)

    # 10h-goal #19 — GET /images: golden-image inventory + live manifest.
    # (per-tenant data snapshot reuses GET /tenants/{id}/{action} action=data)
    images_resource = api.root.add_resource("images")
    images_resource.add_method("GET", _li(), **key_required)

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
        tracing=_tracing_mode,
        log_retention=_log_retention,
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
        },
    )
    tenants_table.grant_read_write_data(health_fn)
    hosts_table.grant_read_write_data(health_fn)
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
        tracing=_tracing_mode,
        log_retention=_log_retention,
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
        tracing=_tracing_mode,
        log_retention=_log_retention,
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
        tracing=_tracing_mode,
        log_retention=_log_retention,
        environment={
            "HOSTS_TABLE": hosts_table.table_name,
            "TENANTS_TABLE": tenants_table.table_name,
            "ASG_NAME": "openclaw-hosts-asg",
            "IDLE_TIMEOUT_MINUTES": str(CFG["scaler"]["idle_timeout_minutes"]),
            # R8 — 空闲缩容总开关,默认 false(不自动 terminate host,防缩到 0 撞
            # pending 租户;OFF 时仍做 idle 标记)。客户按需在 config 开。
            "IDLE_RECLAIM_ENABLED": str(
                CFG.get("scaler", {}).get("idle_reclaim_enabled", False)
            ).lower(),
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
                # R8 — terminate 前查实例 lifecycle,防已终止中的实例被重复 terminate
                # 多扣一次 desired(references.md#R8-Ref-2)。只读,Describe* 不能带
                # resource ARN 条件(AWS 侧 Describe 动作不支持 resource-level),故 *。
                "autoscaling:DescribeAutoScalingInstances",
                "autoscaling:TerminateInstanceInAutoScalingGroup",
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
        tracing=_tracing_mode,
        log_retention=_log_retention,
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
    )
    tenants_table.grant_read_write_data(backup_fn)
    assets_bucket.grant_read_write(backup_fn)
    backup_bucket.grant_read_write(backup_fn)  # 备份写入 + 恢复读取
    backup_cmk.grant_encrypt_decrypt(backup_fn)  # CMK 解密权限只授备份执行者
    _attach_ssm_policies(backup_fn)  # #62 IAM 收窄:拆 SSM 多 statement
    backup_fn.grant_invoke(api_fn)  # API Lambda async invokes Backup Lambda

    # PRD 2.6: backup_cron 现在是"扫描节拍"而非"统一备份时间"——每次触发只备到期
    # 的一批(错峰+限并发)。配高频(如 rate(30 minutes))让全量在 INTERVAL_HOURS
    # 内滚动覆盖,避免开源版"写死统一时间全量同刻备份"。
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
            tracing=_tracing_mode,
            log_retention=_log_retention,
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
    ctx.health_fn = locals().get("health_fn")
    ctx.notifications_topic = locals().get("notifications_topic")
    ctx.notifications_topic_arn = locals().get("notifications_topic_arn")
    # #220 (R9 business alarms): expose the rest of the Lambdas + the lifecycle
    # queue so build_alarms can create per-function Errors/Throttles alarms and
    # SQS age-of-oldest-message alarm without touching this module.
    ctx.audit_archive_fn = locals().get("audit_archive_fn")
    ctx.authorizer_fn = locals().get("authorizer_fn")
    ctx.backup_fn = locals().get("backup_fn")
    ctx.lifecycle_consumer = locals().get("lifecycle_consumer")
    ctx.lifecycle_queue = locals().get("lifecycle_queue")
    ctx.scaler_fn = locals().get("scaler_fn")
    ctx.skills_fn = locals().get("skills_fn")
    ctx.templates_fn = locals().get("templates_fn")
