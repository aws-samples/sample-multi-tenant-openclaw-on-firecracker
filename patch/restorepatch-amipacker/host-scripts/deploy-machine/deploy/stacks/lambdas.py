# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json as _json

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_sns as sns,
    aws_wafv2 as wafv2,
    aws_sqs as sqs,
    aws_secretsmanager as secretsmanager,
    aws_lambda_event_sources as lambda_event_sources,
    BundlingOptions,
    BundlingFileAccess,
    Duration,
    Fn,
    RemovalPolicy,
)

from stacks._helpers import _build_vpc, _sam_build_image_for_host, _read_pyproject_version


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
            # 超时那一刻消息刚好重新可见、而远端 SSM 可能仍在跑 → 重投与在途操作叠加
            # suspend 同步 backup 900s 预算),visibility 同步提到 960s = 900s + 60s 余量
            # (沿用仓库 "timeout+60s" 惯例)。非重动作处理完即删,不受影响。
            visibility_timeout=Duration.seconds(960),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5, queue=lifecycle_dlq
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
    # (_resolve_backup / list_backups / list_all_backups)读 `BACKUP_BUCKET or ASSETS_BUCKET`;
    # 此前只有 backup Lambda 拿到 BACKUP_BUCKET(见 :1481),api Lambda 缺 → 永远回退 assets 桶
    # 指向它。backup_bucket 可能未建(getattr None),判空 fail-safe(不建桶的部署不注入,读侧
    # 仍回退 assets,与旧行为一致)。IAM 读权限在下方 grant(:523 附近)。
    if backup_bucket is not None:
        _api_env["BACKUP_BUCKET"] = backup_bucket.bucket_name

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
        # 装 live(SSM 等)→ 起金丝雀 → poll 到 running → 晋级/回滚,需数分钟。APIGW
        # 集成 29s 会早早回 504,但 Lambda 后台跑完整链(浏览器靠 console 轮询看
        # upgrading→金丝雀→active)。timeout 是上限,普通请求仍秒回,不影响别的路由;
        # pull 期间占一个实例数分钟,并发别的请求靠 Lambda 自动扩实例。
        timeout=Duration.seconds(900),
        memory_size=2048,
        environment=dict(_api_env),
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
    version_snapshots_table.grant(api_fn, "dynamodb:PutItem")
    # 物删,故加 UpdateItem。不给 DeleteItem(软删不物理删,记录留档可审计/可恢复)。
    version_snapshots_table.grant(api_fn, "dynamodb:UpdateItem")
    # 不给 DeleteItem:Job 记录由 TTL(expires_at)回收,控制面无删除路径。
    image_jobs_table.grant_read_write_data(api_fn)
    #  · Pull admission:snapshot ConditionCheck + Job Put(→ 每次 pull 都 JOB_RECORD_UNAVAILABLE);
    #  · codex NB2 canary 租户固定:snapshot ConditionCheck + tenant Put(→ canary 建租户 AccessDenied)。
    # 真机实测教训:除 TransactWriteItems 外还必须显式给 **ConditionCheckItem** —— 事务里的
    # ConditionCheck 项按【被检查表】单独鉴权,缺它报
    # "not authorized to perform: dynamodb:ConditionCheckItem on .../openclaw-version-snapshots"。
    # resources 覆盖三张表:version-snapshots(被 ConditionCheck)+ image-jobs + tenants(被 Put)。
    # / poller)都用 TransactWriteItems 原子写 hosts+tenants;grant_read_write_data 不含
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
    # feature is on so synth stays byte-identical when off (no key on the env).
    # The API uses it as the real gate: it rejects injected_credentials whose
    # kms_key_arn != this ARN (and rejects any injection when this is empty).
    if clawpool_cmk is not None:
        api_fn.add_environment("CLAWPOOL_CMK_ARN", clawpool_cmk.key_arn)
    # GET /clawpool-rsa-public-key so they can locally OAEP-encrypt env creds. It
    # only needs GetPublicKey (never Decrypt — private key stays in KMS, host decrypts).
    if clawpool_rsa_cmk is not None:
        api_fn.add_environment("CLAWPOOL_RSA_CMK_ARN", clawpool_rsa_cmk.key_arn)
        clawpool_rsa_cmk.grant(api_fn, "kms:GetPublicKey")
    audit_table.grant_read_write_data(api_fn)
    # PRD #54 — async batch jobs: read/write the job ledger, and self-invoke
    # asynchronously to run the worker (same function, routed by a marker in
    # the event payload — no separate Lambda to keep the blast radius small).
    batch_jobs_table.grant_read_write_data(api_fn)
    tenant_idp_table.grant_read_data(api_fn)
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
    # (never GET /credentials), so it keeps encrypt-only.
    tenant_secrets_table.grant_read_write_data(api_fn)
    param_registry_table.grant_read_write_data(api_fn)
    recipient_keys_table.grant_read_write_data(api_fn)
    # (edge_admin.py 注释自标 "P5 后追加 elbv2:DescribeTarget* IAM")。Describe 类不支持
    # 资源级 → Resource=*。
    api_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["elasticloadbalancing:DescribeTargetHealth"],
            resources=["*"],
        )
    )
    if clawpool_cmk is not None:
        clawpool_cmk.grant_encrypt_decrypt(api_fn)
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateRandom"],
                resources=["*"],
            )
        )
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
                    # 需要它;原来只有 SendMessage → 面板 depth 静默返 null(fail-soft 吞了 AccessDenied)。
                    actions=["sqs:SendMessage", "sqs:GetQueueAttributes"],
                    resources=[lifecycle_queue.queue_arn],
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
            # 360s:suspend 同步 RequestResponse invoke backup Lambda(其 timeout=900s,见
            # backup_fn :1496)+ stop-vm(30s)+ rm(30s);restore 同步 _ssm_run launch(300s)。
            # 360s 会把合法执行硬杀在中途、卡中间态(suspending/restoring)。提到 Lambda 上限
            # 900s 覆盖最坏 backup 预算(与 delete 的删前备份同款同步 invoke,既有已接受模式);
            # 队列 visibility(:232)同步提到 >900s 防重投叠加。rebuild(300s)等旧动作不受影响。
            timeout=Duration.seconds(900),
            memory_size=2048,
            # 限流阀:consumer 并发上限 = SSM/host 可承受速率(削峰核心)
            reserved_concurrent_executions=_consumer_reserved,
            # 同 api 配置(共享 _api_env);consumer 不入队故不给 LIFECYCLE_QUEUE_URL。
            # Cognito pool/client id 在下方 Cognito 段对两个 Lambda 都 add_environment。
            environment=dict(_api_env),
        )
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
        image_jobs_table.grant_read_write_data(lifecycle_consumer)
        # Same grants as api_fn (secrets table r/w + CMK encrypt + GenerateRandom).
        # **No kms:Decrypt** — API side never decrypts (INTERFACE-CONTRACT §5,
        # ciphertext is folded into GET responses verbatim; caller decrypts).
        tenant_secrets_table.grant_read_write_data(lifecycle_consumer)
        # env_injected_credentials 分支时(tenant_service.py:751/806)调
        # registry_service.load_current_snapshot → param-registry 表 Query,补种
        # 时还 PutItem/transact_write。api_fn 有此 grant(:382)但 consumer 漏,
        # 带模板/凭据注入的租户 replay 时 AccessDenied → 穿窄 except → 重试进
        # DLQ → 永久卡 creating/queued(默认可达:lifecycle_queue+create_via_queue 均默认开)。
        param_registry_table.grant_read_write_data(lifecycle_consumer)
        # _persist_tenant_record 用 TransactWriteItems(snapshot ConditionCheck + tenant Put)
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
        # consumer 复用 _api_env(含 BACKUP_BUCKET)但缺 IAM grant → _resolve_backup 读备份桶
        # AccessDenied → suspend 停 VM/释放 slot 后消息重试、409 被 ack → 租户永久卡 suspending。
        # 与 api_fn 的 backup_bucket.grant_read 对称补上(consumer 只读备份,写归 backup Lambda)。
        if backup_bucket is not None:
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
        # 不加就是"consumer 按活跃 MessageGroup 数任意并发"(30 个不同租户 delete →
        # 最高 30 并发砸向少数 host),撞 SSM 单 host CommandWorkersLimit=5、饿死
        # launch-vm/start/stop。aws-cdk-lib 2.x 的 SqsEventSource 不直接暴露
        # ScalingConfig kwarg(见 dispatch_infra.py:253),用 add_event_source_mapping
        # + add_property_override 落 CFN 属性(与 dispatch ESM 同款,验证过的做法)。
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
        _lc_esm = lifecycle_consumer.add_event_source_mapping(
            "LifecycleQueueEsm",
            event_source_arn=lifecycle_queue.queue_arn,
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
    # via PUT /skills/{name} and removes the skills/{name}/ prefix
    # via DELETE /skills/{name}.
    assets_bucket.grant_put(api_fn)
    assets_bucket.grant_delete(api_fn)
    if notifications_topic is not None:
        notifications_topic.grant_publish(api_fn)
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
    # VPCE 创建前移到主 API 定义之前;network_vpc.py 后续只从 ctx 读取同一 VPC。
    vpc = _build_vpc(self, CFG.get("network", {}) or {})
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
    cdk.CfnOutput(
        self, "ExecuteApiVpceId", value=_execute_api_vpce.vpc_endpoint_id
    )

    _api_cfg = CFG.get("api", {}) or {}
    _vpce_allowlist = [
        str(v).strip()
        for v in (_api_cfg.get("vpce_ids") or [])
        if str(v).strip()
    ]
    if not _vpce_allowlist:
        _vpce_allowlist = [_execute_api_vpce.vpc_endpoint_id]
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

    waf_cfg = CFG.get("waf", {}) or {}
    if waf_cfg.get("enabled", False):
        rate_limit = int(waf_cfg.get("rate_limit_per_ip", 1000))
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

    # OAEP-encrypt env creds before POST /tenants (env_injected_credentials).
    rsa_pubkey_resource = api.root.add_resource("clawpool-rsa-public-key")
    rsa_pubkey_resource.add_method("GET", _li(), **key_required)

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
    # 精确 VersionId 拉 deployment/rootfs/(镜像三盘+manifest),校验 etag 后 copy+unzip 装 live。
    # 只作用一台 host。Admin op (x-api-key)。
    pull_image_resource = host_resource.add_resource("pull-image")
    pull_image_resource.add_method("POST", _li(), **key_required)
    pull_image_progress_resource = host_resource.add_resource("pull-image-progress")
    pull_image_progress_resource.add_method("GET", _li(), **key_required)
    copy_file_resource = host_resource.add_resource("copy-file-from-s3")
    copy_file_resource.add_method("POST", _li(), **key_required)
    # 一个小文件,不搬盘,故走同步 200(不需要 progress 轮询)。
    promote_canary_resource = host_resource.add_resource("promote-canary")
    promote_canary_resource.add_method("POST", _li(), **key_required)
    reclaim_images_resource = host_resource.add_resource("reclaim-images")
    reclaim_images_resource.add_method("POST", _li(), **key_required)
    # versions/),DDB 镜像的权威对照。viewer 可读(handler 内不额外 admin 门,只读)。
    # pull 覆盖 / promote 清空,不再提供显式清指针接口。
    image_slots_resource = host_resource.add_resource("image-slots")
    image_slots_resource.add_method("GET", _li(), **key_required)

    backups_resource = api.root.add_resource("backups")
    backups_resource.add_method("GET", _li(), **key_required)

    # (per-tenant data snapshot reuses GET /tenants/{id}/{action} action=data)
    images_resource = api.root.add_resource("images")
    images_resource.add_method("GET", _li(), **key_required)
    # body {snapshot_time},与 create-image-snapshot 对称(不用 path 带冒号的 ISO 时间)。
    # 只标 status=deleted,不动 S3 镜像文件。operator+。
    delete_snapshot_resource = api.root.add_resource("delete-image-snapshot")
    delete_snapshot_resource.add_method("POST", _li(), **key_required)

    # console 选 snapshot_time 拉。改名避免与 /images(列镜像文件)混淆。
    snapshots_resource = api.root.add_resource("list_image_versions")
    snapshots_resource.add_method("GET", _li(), **key_required)

    # 扫 deployment/ 全量对象 → 写 openclaw-version-snapshots 表。operator+(不在 _VIEWER_OK)。
    # 路径用连字符(与 pull-image/copy-file-from-s3/refresh-rootfs 等一致)。
    create_snapshot_resource = api.root.add_resource("create-image-snapshot")
    create_snapshot_resource.add_method("POST", _li(), **key_required)

    groups_resource = api.root.add_resource("groups")
    groups_resource.add_method("GET", _li(), **key_required)
    groups_resource.add_method("POST", _li(), **key_required)
    group_resource = groups_resource.add_resource("{name}")
    group_skills_resource = group_resource.add_resource("skills")
    group_skills_resource.add_method("POST", _li(), **key_required)
    group_skill_resource = group_skills_resource.add_resource("{skill}")
    group_skill_resource.add_method("DELETE", _li(), **key_required)

    batch_resource = api.root.add_resource("batch")
    batch_tenants_resource = batch_resource.add_resource("tenants")
    batch_tenants_resource.add_method("POST", _li(), **key_required)
    # PRD #54 — async batch job progress: GET /batch/jobs/{job_id}
    batch_jobs_resource = batch_resource.add_resource("jobs")
    batch_job_resource = batch_jobs_resource.add_resource("{job_id}")
    batch_job_resource.add_method("GET", _li(), **key_required)

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
    # (creating→failed + 扣 hosts 账本 + 清令牌一个 TransactWriteItems)。
    # grant_read_write_data 不含 TransactWriteItems,漏加则 reaper 释放时 AccessDenied。
    health_fn.add_to_role_policy(iam.PolicyStatement(
        actions=["dynamodb:TransactWriteItems"],
        resources=[
            hosts_table.table_arn,
            tenants_table.table_arn,
        ],
    ))
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
        environment={
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            "TENANTS_TABLE": tenants_table.table_name,
            "GROUPS_TABLE": groups_table.table_name,
        },
    )
    assets_bucket.grant_read(skills_fn)
    tenants_table.grant_read_data(skills_fn)
    groups_table.grant_read_data(skills_fn)
    skills_resource = api.root.add_resource("skills")
    skills_resource.add_method(
        "GET", apigw.LambdaIntegration(skills_fn), **key_required
    )
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
    tenants_table.grant_read_write_data(scaler_fn)
    # instance ARN 带 Project=openclaw/Role=metal-host 条件。
    _attach_ssm_policies(scaler_fn)
    assets_bucket.grant_read(scaler_fn)
    scaler_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=[
                "autoscaling:DescribeAutoScalingGroups",
                "autoscaling:TerminateInstanceInAutoScalingGroup",
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
    )
    tenants_table.grant_read_write_data(backup_fn)
    assets_bucket.grant_read_write(backup_fn)
    backup_bucket.grant_read_write(backup_fn)  # 备份写入 + 恢复读取
    backup_cmk.grant_encrypt_decrypt(backup_fn)  # CMK 解密权限只授备份执行者
    _attach_ssm_policies(backup_fn)  # #62 IAM 收窄:拆 SSM 多 statement
    backup_fn.grant_invoke(api_fn)  # API Lambda async invokes Backup Lambda
    # 缺 lambda:InvokeFunction → invoke AccessDenied → delete fail-closed 返 5xx → 消息卡
    # FIFO 无限重试、租户永久删不掉。真机实证(ap-southeast-1,2026-07-15):开
    # lifecycle_queue_enabled 后单删返 202 但消息卡 NotVisible、租户始终 running。
    if getattr(self, "_lifecycle_consumer", None) is not None:
        backup_fn.grant_invoke(self._lifecycle_consumer)

    # PRD 2.6: backup_cron 现在是"扫描节拍"而非"统一备份时间"——每次触发只备到期
    # 的一批(错峰+限并发)。配高频(如 rate(30 minutes))让全量在 INTERVAL_HOURS
    # 内滚动覆盖,避免开源版"写死统一时间全量同刻备份"。
    events.Rule(
        self,
        "BackupSchedule",
        schedule=events.Schedule.expression(CFG["s3"]["backup_cron"]),
        targets=[targets.LambdaFunction(backup_fn)],
    )

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
