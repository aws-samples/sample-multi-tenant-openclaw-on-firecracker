# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import platform as _platform
import yaml
import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_sns as sns,
    aws_ec2 as ec2,
    aws_logs as logs,
    aws_autoscaling as autoscaling,
    aws_elasticloadbalancingv2 as elbv2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_wafv2 as wafv2,
    aws_aps as aps,
    aws_grafana as grafana,
    aws_guardduty as guardduty,
    aws_route53resolver as route53resolver,
    aws_sqs as sqs,
    aws_lambda_event_sources as lambda_event_sources,
    aws_bedrock_agentcore_alpha as agentcore,
    aws_bedrockagentcore as agentcore_l1,
    aws_codebuild as codebuild,
    aws_s3_assets as s3_assets,
    aws_ssm as ssm,
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
    BundlingOptions,
    Duration,
    Fn,
    RemovalPolicy,
)
from constructs import Construct
from pathlib import Path

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yml").read_text())


def _sam_build_image_for_host():
    """SAM build image tag for the deploy host's arch (avoids QEMU). pip still
    cross-downloads the aarch64 wheel to match the ARM_64 Lambda."""
    machine = _platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "public.ecr.aws/sam/build-python3.12:latest-arm64"
    return "public.ecr.aws/sam/build-python3.12:latest-x86_64"


def _read_pyproject_version():
    """Best-effort read of the project version so the API can advertise it
    via /system/info. Falls back to "dev" if pyproject.toml is unreadable
    (e.g. during a test that mocks the filesystem)."""
    try:
        import re

        text = (Path(__file__).parent.parent / "pyproject.toml").read_text()
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        return m.group(1) if m else "dev"
    except Exception:
        return "dev"


class OpenClawOrchestratorStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── Multi-region naming suffix ──
        # S3 bucket names, IAM instance-profile names, and Cognito domain prefixes
        # are GLOBALLY unique (not per-region). Deploying the same account into a
        # second region collides on these. Append the region to make them distinct,
        # EXCEPT for the original ap-southeast-1 deployment which keeps its bare
        # names so an existing prod stack there is never disturbed by a redeploy.
        _deploy_region = self.node.try_get_context("region") or "us-east-1"
        self._gsuffix = (
            "" if _deploy_region == "ap-southeast-1" else f"-{_deploy_region}"
        )

        # Removal policy by region: ap-southeast-1 is the production deployment so
        # RETAIN everything (data safety). Any other region is a rebuildable dev/test
        # environment → DESTROY + auto-delete, so a torn-down stack leaves NO retained
        # buckets/tables. That is what lets "delete the stack and redeploy" converge:
        # RETAIN residue otherwise collides on the next create / races S3 name release.
        _is_prod_region = _deploy_region == "ap-southeast-1"
        self._stateful_removal = (
            RemovalPolicy.RETAIN if _is_prod_region else RemovalPolicy.DESTROY
        )
        self._auto_delete = not _is_prod_region

        # ╔══════════════════════════════════════════════════════════════════╗
        # ║ 三包归属导航(配合 engineering/03-collaboration/OWNERS.md)         ║
        # ║ 本 __init__ 顺序构建,资源用局部变量交叉引用,暂不物理拆分(拆 con-  ║
        # ║ struct 会改 logical ID → 删库风险,见 work-items/WI-001)。改动时    ║
        # ║ 按下方 [包X] 标记认领自己的段,别动别的包的段;跨段枢纽变量(api_fn/ ║
        # ║ assets_bucket/vpc/host_role/alb 等)改动走 SHARED-FILES-PROTOCOL。  ║
        # ║   [包C 控制面+工程化] DDB/S3/Lambda/API GW/CodeBuild/Cognito/Outputs ║
        # ║   [包B 隔离安全]       HostRole/监控/ASG/userdata/DNS-FW/Wazuh/AgentCore║
        # ║   [包A 数据面]         ALB/CloudFront/CORS                          ║
        # ╚══════════════════════════════════════════════════════════════════╝

        # ╓─── [包C 控制面+工程化] owner=C ── 数据/Lambda/API 控制面 ───────────╖
        # ========== DynamoDB ==========
        # 控制面表 PITR(时间点恢复)。租户数据本身有 backup_fn→WORM 桶兜底,但
        # 控制面元数据(tenants/hosts/audit)误删/误改/坏写后无法回滚 —— 795 实跑
        # 5 张表 PITR 全 DISABLED(2026-06-30 巡检发现)。开 PITR 后 DynamoDB 维持
        # 35 天连续备份,可恢复到任意秒级时点。config 开关默认 true,短命的
        # batch-jobs(DESTROY+TTL)不开。开 PITR 对 PAY_PER_REQUEST 表只按备份存储
        # 量计费,不影响读写吞吐。
        # aws-cdk-lib 已 deprecated 布尔参数 point_in_time_recovery,改用
        # point_in_time_recovery_specification(可同时配 recovery_period_in_days,
        # 1-35,默认 35)。沿用新写法避免 synth deprecation warning。
        _ddb_cfg = CFG.get("dynamodb", {}) or {}
        _pitr_spec = dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=bool(
                _ddb_cfg.get("point_in_time_recovery", True)
            ),
            recovery_period_in_days=int(_ddb_cfg.get("recovery_period_in_days", 35)),
        )

        tenants_table = dynamodb.Table(
            self,
            "Tenants",
            table_name="openclaw-tenants",
            partition_key=dynamodb.Attribute(
                name="id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._stateful_removal,
            point_in_time_recovery_specification=_pitr_spec,
        )
        # PRD #50-58 — control-plane scale-out: reverse lookups "all nodes of a
        # user" without a full-table scan. PAY_PER_REQUEST means GSIs carry no
        # provisioned cost; a record only appears in a GSI when it has that
        # attribute, so legacy/api-key tenants without tenant_user_id simply don't
        # show up in gsi_tenant_user (we only index nodes that actually have an
        # owner). ProjectionType=ALL so list/batch read the full record without a
        # second round-trip back to the base table.
        tenants_table.add_global_secondary_index(
            index_name="gsi_owner",
            partition_key=dynamodb.Attribute(
                name="owner_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        # DynamoDB 硬限制:一次 update 只能加/删 1 个 GSI。现网表已有 gsi_owner
        # (ACTIVE),栈漂移导致 CFN 想一次补 gsi_owner+gsi_tenant_user 两个 → 报
        # "Cannot perform more than one GSI creation in a single update"(2026-06-29
        # deploy 实撞)。分两步:本次 deploy 只对齐 gsi_owner;gsi_tenant_user 由
        # config 开关 add_gsi_tenant_user=true 在**下一次单独 deploy** 加(per-user
        # 舰队反查 GET /users/{id}/* 用,缺它这些端点降级但不阻塞核心)。
        if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_user", False):
            tenants_table.add_global_secondary_index(
                index_name="gsi_tenant_user",
                partition_key=dynamodb.Attribute(
                    name="tenant_user_id", type=dynamodb.AttributeType.STRING
                ),
                projection_type=dynamodb.ProjectionType.ALL,
            )

        hosts_table = dynamodb.Table(
            self,
            "Hosts",
            table_name="openclaw-hosts",
            partition_key=dynamodb.Attribute(
                name="instance_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._stateful_removal,
            point_in_time_recovery_specification=_pitr_spec,
        )

        # 1.4.0 (#62): per-tenant / per-group skill distribution.
        # When a tenant doesn't carry a `skills` list and isn't assigned a
        # `group`, the launch path falls back to the legacy "broadcast all
        # shared skills" behavior. Otherwise the effective set is computed
        # as tenant.skills ∪ group.skills (with unknown groups silently
        # dropped from the union — see api/handler.py::_resolve_effective_skills).
        groups_table = dynamodb.Table(
            self,
            "Groups",
            table_name="openclaw-groups",
            partition_key=dynamodb.Attribute(
                name="name", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=self._stateful_removal,
            point_in_time_recovery_specification=_pitr_spec,
        )

        # Issue #17 — Audit log table. Single-partition by design (`pk="audit"`)
        # for time-range queries; ts as sort key. DDB TTL auto-expires entries
        # after `audit.retention_days` (default 90).
        # Table name carries a per-deploy suffix derived from Aws.STACK_ID so
        # that `cdk destroy` + redeploy never collides with the RETAIN-ed
        # orphan table from the previous stack incarnation.
        audit_cfg = CFG.get("audit", {}) or {}
        audit_retention_days = int(audit_cfg.get("retention_days", 90))
        # Phase 3 (task #25 / CIS 2.2.2): optional customer-managed CMK for the
        # audit table. config-gated `audit.cmk_encryption` (default false). MUST
        # only be turned on for a FRESH account/first deploy — flipping it on an
        # existing RETAIN-ed table forces a replace (loses audit history), so the
        # switch documents that constraint. When on, mint a rotation-enabled CMK
        # and set CUSTOMER_MANAGED; when off, leave the table on the AWS-owned key
        # (byte-identical to the prior behavior → no replace on existing stacks).
        audit_cmk_enabled = bool(audit_cfg.get("cmk_encryption", False))
        audit_cmk = None
        if audit_cmk_enabled:
            audit_cmk = kms.Key(
                self,
                "AuditCMK",
                alias="alias/openclaw-audit-log",
                description="Encrypts the control-plane audit log table (CIS 2.2.2); customer-managed, rotation enabled",
                enable_key_rotation=True,
                removal_policy=self._stateful_removal,
            )
        # Aws.STACK_ID format: arn:aws:cloudformation:<region>:<acct>:stack/<name>/<uuid>
        # Take the first 5 hex chars of the UUID's leading segment as the suffix.
        stack_uuid = Fn.select(2, Fn.split("/", cdk.Aws.STACK_ID))
        audit_suffix = Fn.select(0, Fn.split("-", stack_uuid))
        audit_table = dynamodb.Table(
            self,
            "AuditLog",
            # Final name e.g. openclaw-audit-log-a1b2c3d4 (UUID first segment, 8 hex)
            table_name=Fn.join("-", ["openclaw-audit-log", audit_suffix]),
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_ttl",
            removal_policy=self._stateful_removal,
            point_in_time_recovery_specification=_pitr_spec,
            # CUSTOMER_MANAGED only when config opts in (fresh deploy); otherwise
            # the SDK default (AWS-owned key) — unchanged for existing stacks.
            encryption=(
                dynamodb.TableEncryption.CUSTOMER_MANAGED
                if audit_cmk is not None
                else None
            ),
            encryption_key=audit_cmk,
        )

        # PRD #54 — async batch jobs. A large bulk lifecycle op (>100 nodes, or
        # ?async) is recorded here, then a self-invoked worker processes it in
        # chunks and updates progress, so the client gets a 202 + job_id instead
        # of a synchronous call that would blow the 30s API-GW timeout. Idempotent
        # by job_id; TTL auto-expires finished job rows.
        batch_jobs_table = dynamodb.Table(
            self,
            "BatchJobs",
            table_name="openclaw-batch-jobs",
            partition_key=dynamodb.Attribute(
                name="job_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )
        # #97 档A — external-platform → Cognito upstream-IdP routing map (SPEC/02 §2.7).
        # partition_key platform_id; rows {idp_provider_name, issuer_url, created_at}.
        # GET /tenantmatch reads it pre-login to route to the right federated IdP.
        # Rebuildable routing config (no tenant data) → DESTROY removal is fine.
        tenant_idp_table = dynamodb.Table(
            self,
            "TenantIdpMap",
            table_name="openclaw-tenant-idp-map",
            partition_key=dynamodb.Attribute(
                name="platform_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ========== S3 Assets Bucket ==========
        # 安全加固(task #25):
        # - BLOCK_ALL: 打开全部 4 个桶级公网封锁开关。租户数据盘/skill/备份/镜像
        #   都在这个桶,绝不能意外公开(CIS S3 2.1.x)。CloudFront 走 OAC 签名访问,
        #   非匿名,全封不破链路。
        # - enforce_ssl: CDK 自动追加 aws:SecureTransport=false 的 Deny 到桶策略,
        #   强制 HTTPS(CIS 2.1.1 Requiring SSL),与 OAC 策略并存不冲突。
        assets_bucket = s3.Bucket(
            self,
            "Assets",
            bucket_name=f"openclaw-assets-{self.account}{self._gsuffix}",
            removal_policy=self._stateful_removal,
            auto_delete_objects=self._auto_delete,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        # ========== Tenant-Backup CMK + WORM backup bucket (insider-threat hardening) ==========
        # 威胁模型:租户 data.ext4 备份是真实资产数据。光放 assets 桶(默认 SSE-S3,
        # AWS 托管密钥)防不住「拥有该桶权限的 S3 管理员」——他能 GetObject 读明文、
        # PutObject 覆盖、DeleteObject 删除。生产级隔离要堵的就是这个内部威胁面。两层独立机制:
        #
        # 1) 防篡改/防删 — S3 Object Lock COMPLIANCE 模式 + Versioning。AWS 官方:
        #    COMPLIANCE 模式下在保留期内**连 root 账户都不能删除/覆盖对象版本、不能缩短
        #    保留期**(WORM,经 Cohasset 评估满足 SEC 17a-4/CFTC/FINRA)。S3 管理员改不动备份。
        # 2) 防看明文 — 专用 CMK(SSE-KMS)+ key policy 权限分离。备份用本 CMK 加密;
        #    key policy 只授「备份/恢复执行者」(host_role / backup_fn)用密钥,**不授予
        #    泛 S3 桶管理员 kms:Decrypt**。纯 S3 管理员即便能 GetObject 也只拿到密文,解不开。
        #    (backup-data.sh 另叠客户端 envelope 加密做第三层纵深。)
        backup_cmk = kms.Key(
            self,
            "BackupCMK",
            alias="alias/openclaw-tenant-backup",
            description="Encrypts tenant data.ext4 backups; decrypt scoped to backup/restore role only",
            enable_key_rotation=True,
            removal_policy=self._stateful_removal,
        )

        # WORM (Object Lock COMPLIANCE) is a production compliance control: in prod
        # (ap-southeast-1) keep it + RETAIN. A COMPLIANCE-locked bucket cannot be
        # emptied/deleted even by root, so a rebuildable dev region would never be
        # able to tear down — there we drop Object Lock and use DESTROY+auto-delete.
        # backup_bucket_suffix (default "") lets a redeploy dodge a name collision
        # with a prior WORM/Object-Lock bucket that can't be torn down until its
        # COMPLIANCE retention lapses. Empty = unchanged for every existing deploy.
        _backup_suffix = CFG["s3"].get("backup_bucket_suffix", "")
        _backup_kwargs = dict(
            bucket_name=f"openclaw-backups-{self.account}{self._gsuffix}{_backup_suffix}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=backup_cmk,
            bucket_key_enabled=True,  # 降低 KMS 调用成本
        )
        if _is_prod_region:
            _backup_kwargs.update(
                removal_policy=RemovalPolicy.RETAIN,
                versioned=True,  # Object Lock 要求开版本控制
                object_lock_enabled=True,
                object_lock_default_retention=s3.ObjectLockRetention.compliance(
                    Duration.days(CFG["s3"]["backup_retention_days"])
                ),
            )
        else:
            _backup_kwargs.update(
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
            )
        backup_bucket = s3.Bucket(self, "TenantBackups", **_backup_kwargs)

        # Lifecycle rule managed via CustomResource (RETAIN bucket won't update inline rules)
        cr.AwsCustomResource(
            self,
            "BackupLifecycle",
            install_latest_aws_sdk=False,
            on_create=cr.AwsSdkCall(
                service="S3",
                action="putBucketLifecycleConfiguration",
                parameters={
                    "Bucket": assets_bucket.bucket_name,
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "ID": "backup-expiration",
                                "Filter": {"Prefix": f"{CFG['s3']['backup_prefix']}/"},
                                "Status": "Enabled",
                                "Expiration": {
                                    "Days": CFG["s3"]["backup_retention_days"]
                                },
                            }
                        ]
                    },
                },
                physical_resource_id=cr.PhysicalResourceId.of("backup-lifecycle"),
            ),
            on_update=cr.AwsSdkCall(
                service="S3",
                action="putBucketLifecycleConfiguration",
                parameters={
                    "Bucket": assets_bucket.bucket_name,
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "ID": "backup-expiration",
                                "Filter": {"Prefix": f"{CFG['s3']['backup_prefix']}/"},
                                "Status": "Enabled",
                                "Expiration": {
                                    "Days": CFG["s3"]["backup_retention_days"]
                                },
                            }
                        ]
                    },
                },
                physical_resource_id=cr.PhysicalResourceId.of("backup-lifecycle"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["s3:PutLifecycleConfiguration"],
                        resources=[assets_bucket.bucket_arn],
                    ),
                ]
            ),
        )

        # NOTE: media-upload CORS for this bucket is configured later, after the
        # CloudFront distribution is created (search "AssetsCors"), because the
        # allowed-origin must be scoped to the real CloudFront domain.

        # ========== Lambda Shared Policy ==========
        ssm_policy = iam.PolicyStatement(
            actions=["ssm:SendCommand", "ssm:GetCommandInvocation"],
            resources=["*"],
        )
        ec2_policy = iam.PolicyStatement(
            actions=[
                "ec2:DescribeInstances",
                "ec2:DescribeInstanceTypes",
                "ec2:TerminateInstances",
            ],
            resources=["*"],
        )

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
            "ASSETS_BUCKET": assets_bucket.bucket_name,
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
            "TENANT_DEFAULT_BUDGET": str(
                CFG.get("billing", {}).get("default_budget", 0)
            ),
            "TENANT_DEFAULT_RPM": str(CFG.get("billing", {}).get("default_rpm", 0)),
            "QUOTAS_ENABLED": str(CFG.get("quotas", {}).get("enabled", False)).lower(),
            "QUOTAS_MAX_VCPU": str(CFG.get("quotas", {}).get("max_vcpu_per_tenant", 0)),
            "QUOTAS_MAX_MEM_MB": str(
                CFG.get("quotas", {}).get("max_mem_mb_per_tenant", 0)
            ),
            "QUOTAS_MAX_DATA_DISK_MB": str(
                CFG.get("quotas", {}).get("max_data_disk_mb", 0)
            ),
            "MULTI_AZ_ENABLED": str(
                CFG.get("multi_az", {}).get("enabled", False)
            ).lower(),
            "MULTI_AZ_COUNT": str(CFG.get("multi_az", {}).get("az_count", 1)),
            "WAF_ENABLED": str(CFG.get("waf", {}).get("enabled", False)).lower(),
            "BALLOON_ENABLED": str(
                CFG.get("balloon", {}).get("enabled", False)
            ).lower(),
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
            memory_size=256,
            environment=dict(_api_env),
        )
        tenants_table.grant_read_write_data(api_fn)
        hosts_table.grant_read_write_data(api_fn)
        groups_table.grant_read_write_data(api_fn)
        # Issue #17 — api Lambda writes audits and reads them back via GET /audit-log
        audit_table.grant_read_write_data(api_fn)
        # PRD #54 — async batch jobs: read/write the job ledger, and self-invoke
        # asynchronously to run the worker (same function, routed by a marker in
        # the event payload — no separate Lambda to keep the blast radius small).
        batch_jobs_table.grant_read_write_data(api_fn)
        # #97 档A — /tenantmatch only reads the IdP map (least privilege: read-only).
        tenant_idp_table.grant_read_data(api_fn)
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

        # 控制面重构阶段1 — 把队列 URL 注入 api Lambda(产端:create/start/stop/delete
        # 入队)+ 建 consumer Lambda(同 handler 代码,SQS 事件触发,reserved
        # concurrency 当限流阀削峰)。consumer 复用 api_fn 的全部 env(同代码同权限)。
        if _lifecycle_q_enabled and lifecycle_queue is not None:
            api_fn.add_environment("LIFECYCLE_QUEUE_URL", lifecycle_queue.queue_url)
            # Phase 2 — route POST /tenants through the FIFO queue too (config-gated,
            # default off). Only meaningful when the queue exists, so it's set here.
            _create_via_queue = bool(
                CFG.get("scaler", {}).get("create_via_queue", False)
            )
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
                        actions=["sqs:SendMessage"],
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
                        image=cdk.DockerImage.from_registry(
                            _sam_build_image_for_host()
                        ),
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
                memory_size=256,
                # 限流阀:consumer 并发上限 = SSM/host 可承受速率(削峰核心)
                reserved_concurrent_executions=_consumer_reserved,
                # 同 api 配置(共享 _api_env);consumer 不入队故不给 LIFECYCLE_QUEUE_URL。
                # Cognito pool/client id 在下方 Cognito 段对两个 Lambda 都 add_environment。
                environment=dict(_api_env),
            )
            # consumer 同 api 权限(同代码路径,要读写表/调 SSM/发事件)
            tenants_table.grant_read_write_data(lifecycle_consumer)
            hosts_table.grant_read_write_data(lifecycle_consumer)
            groups_table.grant_read_write_data(lifecycle_consumer)
            audit_table.grant_read_write_data(lifecycle_consumer)
            batch_jobs_table.grant_read_write_data(lifecycle_consumer)
            assets_bucket.grant_read(lifecycle_consumer)
            assets_bucket.grant_put(lifecycle_consumer)
            lifecycle_queue.grant_consume_messages(lifecycle_consumer)
            # consumer emits the create-latency SLA metric on the create path.
            lifecycle_consumer.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["cloudwatch:PutMetricData"],
                    resources=["*"],
                    conditions={
                        "StringEquals": {
                            "cloudwatch:namespace": "OpenClaw/ControlPlane"
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
            lifecycle_consumer.add_to_role_policy(ssm_policy)
            lifecycle_consumer.add_to_role_policy(ec2_policy)
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
        api_fn.add_to_role_policy(ssm_policy)
        # Phase 2 — emit the TenantCreateLatencySeconds SLA metric. PutMetricData
        # can't be resource-scoped (no ARNs), so it's namespace-conditioned to
        # OpenClaw/ControlPlane to keep it least-privilege.
        cw_metrics_policy = iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={
                "StringEquals": {"cloudwatch:namespace": "OpenClaw/ControlPlane"}
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
        api_fn.add_to_role_policy(ec2_policy)
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
        api = apigw.RestApi(
            self,
            "Api",
            rest_api_name="openclaw-orchestrator",
            deploy_options=apigw.StageOptions(stage_name="v1"),
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
            api_stages=[
                apigw.UsagePlanPerApiStage(api=api, stage=api.deployment_stage)
            ],
        )
        plan.add_api_key(api_key)

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
                dict.fromkeys(
                    list(waf_cfg.get("managed_rules", []) or []) + _waf_baseline
                )
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
        api_fn.add_permission(
            "ApiGwInvoke",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=Fn.join(
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
            ),
        )
        _api_fn_view = _lambda.Function.from_function_arn(
            self,
            "ApiHandlerView",
            api_fn.function_arn,
        )

        def _li():
            """A LambdaIntegration that does NOT add a per-method permission
            (built against the imported view). The single wildcard permission
            above authorises every method."""
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

        tenant_action = tenant_resource.add_resource("{action}")
        tenant_action.add_method("POST", _li(), **key_required)
        tenant_action.add_method("GET", _li(), **key_required)

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
            memory_size=256,
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
        health_fn.add_to_role_policy(ssm_policy)

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
            memory_size=128,
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
            memory_size=128,
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
            memory_size=128,
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
        scaler_fn.add_to_role_policy(ssm_policy)  # SSM stop-vm.sh on TTL expiry
        # task #21 — read rootfs manifest (current golden version) for refresh
        assets_bucket.grant_read(scaler_fn)
        scaler_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "autoscaling:DescribeAutoScalingGroups",
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
            memory_size=256,
            environment={
                "TENANTS_TABLE": tenants_table.table_name,
                "ASSETS_BUCKET": assets_bucket.bucket_name,
                "BACKUP_BUCKET": backup_bucket.bucket_name,  # WORM + CMK 备份专用桶
                "BACKUP_CMK_KEY_ID": backup_cmk.key_id,
                "BACKUP_PREFIX": CFG["s3"]["backup_prefix"],
                # PRD 2.6 错峰+限并发:每租户距上次备份超 INTERVAL_HOURS 才备(错峰),
                # 单次触发最多 BATCH_LIMIT 个(削并发)。配合高频 schedule 滚动覆盖全量。
                "BACKUP_INTERVAL_HOURS": str(
                    CFG["s3"].get("backup_interval_hours", 24)
                ),
                "BACKUP_BATCH_LIMIT": str(CFG["s3"].get("backup_batch_limit", 20)),
            },
        )
        tenants_table.grant_read_write_data(backup_fn)
        assets_bucket.grant_read_write(backup_fn)
        backup_bucket.grant_read_write(backup_fn)  # 备份写入 + 恢复读取
        backup_cmk.grant_encrypt_decrypt(backup_fn)  # CMK 解密权限只授备份执行者
        backup_fn.add_to_role_policy(ssm_policy)
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

        # ╓─── [包B 隔离安全] owner=B ── host角色/监控(host_role,被ASG/AMP/AgentCore引用)─╖
        # ========== Host EC2 Role (SSM + S3 backup + self-register) ==========
        host_role = iam.Role(
            self,
            "HostRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )
        assets_bucket.grant_read_write(host_role)
        backup_bucket.grant_read_write(host_role)  # backup-data.sh 写备份/恢复读
        backup_cmk.grant_encrypt_decrypt(host_role)  # CMK 解密只授备份执行者
        hosts_table.grant_read_write_data(host_role)
        tenants_table.grant_read_write_data(
            host_role
        )  # host-agent writes health status
        host_role.add_to_policy(
            iam.PolicyStatement(
                actions=["autoscaling:CompleteLifecycleAction"],
                resources=["*"],
            )
        )
        # init-host.sh 运行时从 SSM 拉 CLOUDFRONT_ORIGIN(CloudFront 域晚于 LT 创建,
        # 走 SSM Parameter 解循环依赖)。只读 /openclaw/* 路径。
        host_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/*"
                ],
            )
        )
        host_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ec2:DescribeVolumes", "ec2:CreateTags"],
                resources=["*"],
            )
        )
        host_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=["*"],
            )
        )

        # ========== Amazon Managed Prometheus + Grafana (issue #4) ==========
        # Host-agent exposes /metrics on :8899 (same listener as /health);
        # an ADOT collector on each host remote-writes to AMP using SigV4.
        # AMG reads from AMP for dashboards. (NOTE: 1.2.5 fixed a wiring
        # bug where ADOT was scraping :9090 — host-agent never bound 9090.)
        # Cost knob: metrics.enabled: false in config.yml skips both workspaces
        # (AMP is billed per sample/GB, AMG per active user).
        #
        # 后端选择(2026-06-30):本项目既定监控架构是**自建 EC2 Prometheus+Grafana**
        # (deploy/monitoring/,见运维手册 §5.6),刻意不用 AMP/Amazon Managed Grafana
        # ——AMG 强制 AWS_SSO,正是要规避的。所以这段 AMP+AMG 托管资源只在显式
        # metrics.use_managed=true 时才建;默认(含只设 enabled=true)走自建,不建任何
        # 托管 workspace。这样 cdk deploy 默认行为与架构决策一致,不会意外建出强制 SSO
        # 的 AMG(795 实测 AMP workspace 为空,正是走自建)。想用 AWS 托管再显式开。
        metrics_cfg = CFG.get("metrics", {})
        amp_remote_write_url = "none"
        if metrics_cfg.get("enabled", False) and metrics_cfg.get("use_managed", False):
            amp_workspace = aps.CfnWorkspace(
                self,
                "AmpWorkspace",
                alias=metrics_cfg.get("workspace_alias", "openclaw"),
            )
            # Host EC2 role can remote-write to this workspace.
            host_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "aps:RemoteWrite",
                        "aps:GetSeries",
                        "aps:GetLabels",
                        "aps:GetMetricMetadata",
                    ],
                    resources=[amp_workspace.attr_arn],
                )
            )
            # AMG service role with read access to AMP.
            grafana_role = iam.Role(
                self,
                "GrafanaServiceRole",
                assumed_by=iam.ServicePrincipal("grafana.amazonaws.com"),
            )
            grafana_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "aps:QueryMetrics",
                        "aps:GetSeries",
                        "aps:GetLabels",
                        "aps:GetMetricMetadata",
                    ],
                    resources=[amp_workspace.attr_arn],
                )
            )
            # AMG workspace itself. AWS_SSO is required when not using SAML.
            amg_workspace = grafana.CfnWorkspace(
                self,
                "GrafanaWorkspace",
                account_access_type="CURRENT_ACCOUNT",
                authentication_providers=["AWS_SSO"],
                permission_type="SERVICE_MANAGED",
                role_arn=grafana_role.role_arn,
                data_sources=["PROMETHEUS"],
                name=metrics_cfg.get("grafana_name", "openclaw-metrics"),
            )
            # Build remote_write URL for the host-agent template substitution.
            # Format: https://aps-workspaces.<region>.amazonaws.com/workspaces/<id>/api/v1/remote_write
            amp_remote_write_url = (
                f"https://aps-workspaces.{self.region}.amazonaws.com/"
                f"workspaces/{amp_workspace.attr_workspace_id}/api/v1/remote_write"
            )
            cdk.CfnOutput(self, "AmpWorkspaceArn", value=amp_workspace.attr_arn)
            cdk.CfnOutput(self, "AmpRemoteWriteUrl", value=amp_remote_write_url)
            grafana_url = f"https://{amg_workspace.attr_endpoint}"
            cdk.CfnOutput(self, "GrafanaWorkspaceUrl", value=grafana_url)
            # Surface to API Lambda so /system/info can advertise it to the
            # console (Settings → Monitoring shows a clickable Grafana link).
            api_fn.add_environment("AMP_REMOTE_WRITE_URL", amp_remote_write_url)
            api_fn.add_environment("GRAFANA_WORKSPACE_URL", grafana_url)

        # ========== Security monitoring: GuardDuty + SNS feed (10h-goal #20) ==========
        # The Wazuh-style monitoring data platform aggregates three sources:
        #   1. in-guest auditd + FIM (openclaw-fim.sh, baked in build-rootfs;
        #      reverse-shell + sensitive-file-modify rules already fire) — these
        #      ship to a Wazuh manager (deploy via deploy/monitoring/, see runbook);
        #   2. AWS GuardDuty agent/account findings (this block) — VPC/DNS/EC2
        #      threat detection at the cloud layer, complementing in-guest HIDS;
        #   3. openclaw runtime metrics (host-agent /metrics → AMP, above).
        # GuardDuty is config-gated (security.guardduty_enabled) and idempotent:
        # CfnDetector errors if the account already has one, so default OFF and
        # let an account that already runs GuardDuty just point Wazuh at it.
        sec_cfg = CFG.get("security", {}) or {}
        if sec_cfg.get("guardduty_enabled", False):
            gd_detector = guardduty.CfnDetector(
                self,
                "GuardDutyDetector",
                enable=True,
                finding_publishing_frequency="FIFTEEN_MINUTES",
                # malware-protection + S3 logs are the highest-signal for this
                # workload (microVM data on EBS, tenant assets on S3).
                data_sources=guardduty.CfnDetector.CFNDataSourceConfigurationsProperty(
                    s3_logs=guardduty.CfnDetector.CFNS3LogsConfigurationProperty(
                        enable=True
                    ),
                ),
            )
            # Route GuardDuty findings to the notifications SNS topic so the
            # Wazuh platform (or any subscriber) ingests them alongside HIDS
            # alerts. EventBridge rule: GuardDuty Finding → SNS.
            if notifications_topic is not None:
                gd_rule = events.Rule(
                    self,
                    "GuardDutyToSns",
                    event_pattern=events.EventPattern(
                        source=["aws.guardduty"],
                        detail_type=["GuardDuty Finding"],
                    ),
                )
                gd_rule.add_target(targets.SnsTopic(notifications_topic))
            cdk.CfnOutput(self, "GuardDutyDetectorId", value=gd_detector.ref)

        instance_profile = iam.CfnInstanceProfile(
            self,
            "HostInstanceProfile",
            roles=[host_role.role_name],
            instance_profile_name=f"openclaw-host-profile{self._gsuffix}",
        )

        # ╓─── [包C 控制面+工程化] owner=C ── 栈内烤镜像(image_ready 被 ASG 依赖)──╖
        # ========== In-stack golden-image bake (CodeBuild) ==========
        # Solves the chicken-and-egg deadlock: with min_capacity>=1 the ASG would
        # launch a host before any golden image exists in S3, init-host can't pull
        # the rootfs, the lifecycle hook times out, and the ASG churns metal forever.
        # Here a CodeBuild project bakes the image DURING `cdk deploy` (reusing the
        # battle-tested deploy/codebuild/buildspec-golden-image.yml: docker-in-docker
        # debootstrap, same build-rootfs.sh, same hardening), and the ASG is made to
        # depend on a custom resource that BLOCKS until the build succeeds. So by the
        # time the first host boots, the image is already in the bucket.
        # Toggle with image.build_in_stack: false to skip (reuse existing image or
        # bake out-of-band via scripts/build-rootfs-on-ec2.sh).
        _img_cfg = CFG.get("image", {}) or {}
        image_version = str(_img_cfg.get("version", "v1.0"))
        image_ready = None  # node the ASG depends on; set when build_in_stack
        if _img_cfg.get("build_in_stack", True):
            # Repo source as an S3 asset (CDK zips + uploads). Exclude heavy/local-only
            # dirs so the upload stays small and never carries secrets.
            repo_asset = s3_assets.Asset(
                self,
                "GoldenImageSource",
                path=str(Path(__file__).parent.parent),
                exclude=[
                    ".git",
                    ".venv",
                    "cdk.out",
                    "node_modules",
                    "**/node_modules",
                    "*.bak",
                    "*.bak-*",
                    ".localbin",
                    ".remote-drift",
                    "engineering",
                    "docs/**",
                    "presentations/**",
                    "*.pyc",
                    "**/__pycache__",
                    ".ruff_cache",
                ],
            )

            # CodeBuild service role: read the source asset, read/write the assets
            # bucket (push baked image), and the EC2/describe bits build-rootfs needs.
            cb_role = iam.Role(
                self,
                "GoldenImageBuildRole",
                assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
            )
            assets_bucket.grant_read_write(cb_role)
            repo_asset.grant_read(cb_role)
            cb_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    resources=["*"],
                )
            )

            golden_project = codebuild.Project(
                self,
                "GoldenImageBuilder",
                project_name=f"openclaw-golden-image-builder{self._gsuffix}",
                role=cb_role,
                source=codebuild.Source.s3(
                    bucket=repo_asset.bucket,
                    path=repo_asset.s3_object_key,
                ),
                environment=codebuild.BuildEnvironment(
                    build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                    compute_type=codebuild.ComputeType.LARGE,
                    privileged=True,  # docker-in-docker for debootstrap
                ),
                environment_variables={
                    "ASSETS_BUCKET": codebuild.BuildEnvironmentVariable(
                        value=assets_bucket.bucket_name
                    ),
                    "IMAGE_VERSION": codebuild.BuildEnvironmentVariable(
                        value=image_version
                    ),
                    "AWS_REGION": codebuild.BuildEnvironmentVariable(value=self.region),
                },
                build_spec=codebuild.BuildSpec.from_source_filename(
                    "deploy/codebuild/buildspec-golden-image.yml"
                ),
                timeout=Duration.minutes(40),
            )

            # Custom-resource provider that starts the build and BLOCKS (isComplete
            # waiter) until it succeeds. A single onEvent Lambda can't wait ~10min for
            # the build (Lambda/CR timeout), so we use the async isComplete pattern:
            # onEvent fires start-build, isComplete polls batch-get-builds until the
            # build leaves IN_PROGRESS, failing the deploy if it didn't SUCCEED.
            cb_start_fn = _lambda.Function(
                self,
                "GoldenBuildStart",
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler="index.on_event",
                timeout=Duration.minutes(2),
                code=_lambda.Code.from_inline(
                    "import boto3, json\n"
                    "cb = boto3.client('codebuild')\n"
                    "def on_event(event, ctx):\n"
                    "    rt = event['RequestType']\n"
                    "    if rt == 'Delete':\n"
                    "        return {'PhysicalResourceId': event.get('PhysicalResourceId','golden-build')}\n"
                    "    proj = event['ResourceProperties']['ProjectName']\n"
                    "    # rerun whenever IMAGE_VERSION changes (Update) or on Create\n"
                    "    b = cb.start_build(projectName=proj)['build']\n"
                    "    return {'PhysicalResourceId': b['id'], 'Data': {'BuildId': b['id']}}\n"
                ),
            )
            cb_done_fn = _lambda.Function(
                self,
                "GoldenBuildPoll",
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler="index.is_complete",
                timeout=Duration.minutes(2),
                code=_lambda.Code.from_inline(
                    "import boto3\n"
                    "cb = boto3.client('codebuild')\n"
                    "def is_complete(event, ctx):\n"
                    "    rt = event['RequestType']\n"
                    "    if rt == 'Delete':\n"
                    "        return {'IsComplete': True}\n"
                    "    bid = event['PhysicalResourceId']\n"
                    "    b = cb.batch_get_builds(ids=[bid])['builds'][0]\n"
                    "    status = b['buildStatus']\n"
                    "    if status == 'IN_PROGRESS':\n"
                    "        return {'IsComplete': False}\n"
                    "    if status == 'SUCCEEDED':\n"
                    "        return {'IsComplete': True}\n"
                    "    raise Exception(f'golden image build {bid} ended {status}')\n"
                ),
            )
            cb_start_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["codebuild:StartBuild"],
                    resources=[golden_project.project_arn],
                )
            )
            cb_done_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["codebuild:BatchGetBuilds"],
                    resources=[golden_project.project_arn],
                )
            )
            golden_provider = cr.Provider(
                self,
                "GoldenBuildProvider",
                on_event_handler=cb_start_fn,
                is_complete_handler=cb_done_fn,
                query_interval=Duration.seconds(30),
                total_timeout=Duration.minutes(40),
            )
            image_ready = cdk.CustomResource(
                self,
                "GoldenImageReady",
                service_token=golden_provider.service_token,
                properties={
                    "ProjectName": golden_project.project_name,
                    # change forces re-bake when the image version changes
                    "ImageVersion": image_version,
                },
            )
            image_ready.node.add_dependency(golden_project)

        # ╓─── [包B 隔离安全] owner=B ── ASG/userdata/LiteLLM/DNS-FW/Wazuh/FlowLogs/AgentCore ─╖
        # ========== ASG (P1-4) ==========
        ac_cfg = CFG.get("agentcore", {})
        ac_enabled = ac_cfg.get("enabled", False)
        gateway_url = ""
        ac_gateway = None

        # Create AgentCore Gateway early (needed for userdata placeholder)
        if ac_enabled and ac_cfg.get("gateway", {}).get("enabled", True):
            ac_gateway = agentcore.Gateway(
                self,
                "AgentCoreGateway",
                gateway_name="openclaw-gateway",
                description="OpenClaw Agent tool gateway",
            )
            gateway_url = ac_gateway.gateway_url
            ac_gateway.grant_invoke(host_role)

        vpc = ec2.Vpc.from_lookup(self, "Vpc", is_default=True)

        # ========== AI Gateway (LiteLLM) toggle ==========
        # guest microVMs hold ZERO credentials; LLM calls go through an OpenAI-
        # compatible gateway (LiteLLM) → Bedrock. Two modes:
        #   ai_gateway.url filled  → write it straight to SSM /openclaw/litellm-host;
        #                            host's launch-vm.sh injects it into each VM.
        #   ai_gateway.url empty   → CDK stands up a LiteLLM EC2 (least-priv Bedrock
        #                            instance role, no static keys; master_key from
        #                            Secrets Manager) and writes its private IP:4000
        #                            to SSM. This is what makes a fresh region
        #                            one-click — no manual gateway step.
        _aigw_cfg = CFG.get("ai_gateway", {}) or {}
        _aigw_url = (_aigw_cfg.get("url") or "").strip()
        if _aigw_url:
            ssm.StringParameter(
                self,
                "LiteLlmHostParam",
                parameter_name="/openclaw/litellm-host",
                string_value=_aigw_url,
            )
        else:
            litellm_role = iam.Role(
                self,
                "LiteLlmRole",
                assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "AmazonSSMManagedInstanceCore"
                    ),
                ],
            )
            litellm_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                        "bedrock:Converse",
                        "bedrock:ConverseStream",
                        "bedrock:ApplyGuardrail",
                    ],
                    resources=["*"],
                )
            )
            assets_bucket.grant_read(litellm_role)
            litellm_secret = secretsmanager.Secret(
                self,
                "LiteLlmSecret",
                secret_name=f"openclaw-litellm{self._gsuffix}",
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template=json.dumps({"user": "litellm"}),
                    generate_string_key="master_key",
                    exclude_punctuation=True,
                    password_length=40,
                ),
            )
            litellm_secret.grant_read(litellm_role)
            litellm_sg = ec2.SecurityGroup(
                self,
                "LiteLlmSG",
                vpc=vpc,
                description="LiteLLM gateway: 4000 from VPC only, no 0.0.0.0",
                allow_all_outbound=True,
            )
            litellm_sg.add_ingress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.tcp(4000),
                "VPC to LiteLLM 4000",
            )
            litellm_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["ssm:PutParameter"],
                    resources=[
                        f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/litellm-host"
                    ],
                )
            )
            _lite_ud = ec2.UserData.for_linux()
            _lite_ud.add_commands(
                "set -x",
                # AMI 是 Amazon Linux 2023(machine_image=latest_amazon_linux2023),用 dnf 不是 apt。
                # 已踩坑:旧 user-data 用 apt-get install docker.io→AL2023 无 apt→docker 没装→LiteLLM 起不来。
                "dnf install -y docker jq || yum install -y docker jq",
                # docker compose v2 插件(AL2023 dnf 无 docker-compose-v2 包,手装 cli 插件)
                "mkdir -p /usr/libexec/docker/cli-plugins",
                'ARCH=$(uname -m); [ "$ARCH" = "aarch64" ] && CARCH=aarch64 || CARCH=x86_64',
                'curl -sL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$CARCH" -o /usr/libexec/docker/cli-plugins/docker-compose && chmod +x /usr/libexec/docker/cli-plugins/docker-compose',
                "systemctl enable --now docker",
                "mkdir -p /opt/litellm && cd /opt/litellm",
                # 时序竞态(重建实撞):CDK 建栈时本 EC2 立即 boot 拉 S3,但 setup.sh 是
                # cdk deploy 之后才上传 deployment/litellm/ → EC2 拉到空目录 → 容器起不来。
                # 轮询等关键文件出现(最多 ~10min),让 EC2 先起、资产后到也能自愈。
                f"for i in $(seq 1 60); do "
                f"aws s3 cp s3://{assets_bucket.bucket_name}/deployment/litellm/ . --recursive --region {self.region} 2>/dev/null; "
                f"[ -f docker-compose.litellm.yml ] && [ -f litellm-config.yaml ] && break; "
                f'echo "waiting for litellm assets in S3 ($i)"; sleep 10; done',
                f"MK=$(aws secretsmanager get-secret-value --secret-id openclaw-litellm{self._gsuffix} --region {self.region} --query SecretString --output text | jq -r .master_key)",
                # .env 必须补全 POSTGRES_* —— docker-compose 的 db 服务和 litellm 的
                # DATABASE_URL 都引用 ${POSTGRES_PASSWORD}(:? 断言),只写 MASTER_KEY+
                # DATABASE_URL 会让 compose 报 "POSTGRES_PASSWORD is missing" 起不来
                # (重建实撞)。db host 用 compose 服务名 litellm-db(见 docker-compose)。
                'echo "LITELLM_MASTER_KEY=$MK" > .env',
                'echo "POSTGRES_USER=litellm" >> .env',
                'echo "POSTGRES_PASSWORD=$MK" >> .env',
                'echo "POSTGRES_DB=litellm" >> .env',
                'echo "DATABASE_URL=postgresql://litellm:$MK@litellm-db:5432/litellm" >> .env',
                "chmod 600 .env",
                # config.runtime.yaml 必须先于 compose up 生成,否则 compose 把不存在的挂载源
                # 当目录建 → 容器内 /etc/litellm/config.yaml 成空目录 → IsADirectoryError 崩溃重启(已踩坑)。
                # litellm-config.yaml 在 S3 deployment/litellm/(setup.sh 已补传),就地 sed 注入 guardrail+master_key 引用。
                f': "${{GUARDRAIL_ID:=__GUARDRAIL_ID__}}"',
                'sed "s|__GUARDRAIL_ID__|${GUARDRAIL_ID}|g" litellm-config.yaml > config.runtime.yaml 2>/dev/null || cp litellm-config.yaml config.runtime.yaml',
                "sed -i 's|^\\(\\s*master_key:\\).*|\\1 os.environ/LITELLM_MASTER_KEY|' config.runtime.yaml || true",
                "docker compose -f docker-compose.litellm.yml up -d 2>&1 | tail -5",
                # IMDSv2: AL2023 强制 token,旧 IMDSv1 curl 取 IP 返回空→SSM 写成 http://:4000/v1(已踩坑)。先 PUT 拿 token。
                'TOK=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 300")',
                'IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOK" http://169.254.169.254/latest/meta-data/local-ipv4)',
                f'aws ssm put-parameter --name /openclaw/litellm-host --type String --overwrite --value "http://$IP:4000/v1" --region {self.region}',
            )
            ec2.Instance(
                self,
                # V2: logical id 翻新,强制 CFN 建全新实例(旧实例 user-data 是装不上
                # docker 的旧版本,且手动 terminate 后 CFN 状态漂移不重建)。
                "LiteLlmGatewayV2",
                vpc=vpc,
                instance_type=ec2.InstanceType(
                    _aigw_cfg.get("instance_type", "c7i.large")
                ),
                machine_image=ec2.MachineImage.latest_amazon_linux2023(),
                role=litellm_role,
                security_group=litellm_sg,
                user_data=_lite_ud,
                # 改 user-data 自动重建实例(否则 CFN 只更元数据不重跑首次 boot 脚本,
                # 导致 user-data 改了却不生效——本轮踩坑根因)。
                user_data_causes_replacement=True,
                # default VPC only has Public subnets; the SG still restricts 4000
                # to the VPC CIDR (no 0.0.0.0/0), so the gateway isn't internet-reachable.
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            )
            # WI-F/F1 fix: wire the self-hosted LiteLLM back to the control plane so
            # per-tenant vkeys actually get minted. Without this the API Lambda's
            # LITELLM_MASTER_KEY_SECRET was empty (it read the absent billing.* config)
            # → _get_litellm_master_key() returned None → vkey minting skipped → every
            # agent hit LiteLLM with no key → "Something went wrong". The base URL is
            # read at runtime from SSM /openclaw/litellm-host (EC2 writes its private
            # IP there at boot; unknown at synth), so only the secret name is injected.
            # create_tenant (which mints the vkey) runs on api_fn OR — when
            # CREATE_VIA_QUEUE is on — on lifecycle_consumer. Wire BOTH so vkey
            # minting works on whichever executes the create path.
            _litellm_ssm_stmt = iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/litellm-host"
                ],
            )
            for _fn in filter(None, [api_fn, locals().get("lifecycle_consumer")]):
                _fn.add_environment(
                    "LITELLM_MASTER_KEY_SECRET", litellm_secret.secret_name
                )
                litellm_secret.grant_read(_fn)
                _fn.add_to_role_policy(_litellm_ssm_stmt)

        # ========== Route53 Resolver DNS Firewall(出网 C2 域名拦截)==========
        # config-gated(security.dns_firewall_enabled,默认 false)。L4 出网防线:
        # 在 VPC DNS 解析层 BLOCK 已知 C2/数据外泄域名,guest 解析 C2 域直接 NXDOMAIN。
        # 此前由命令式脚本 deploy/runtime-config-export/apply-hardening.sh 旁路创建,
        # 不随 cdk deploy 对账(违反「改部署代码→重建」)。现纳入 CDK,随栈对账/回滚。
        # 命名沿用 openclaw-egress-fw / openclaw-egress-blocklist(跨账号巡检一致)。
        # 真实 C2 域名清单是安全敏感数据,不入仓库:domain list 只放 demo 占位,运营
        # 另用 route53resolver import-firewall-domains 从受控威胁情报源灌(幂等 ADD)。
        if sec_cfg.get("dns_firewall_enabled", False):
            _fw_domain_list = route53resolver.CfnFirewallDomainList(
                self,
                "EgressBlocklist",
                name="openclaw-egress-blocklist",
                # demo 占位域名,证明 DNS egress 拦截可演示;真实 C2 清单运营另灌,
                # 仓库不存真实 C2 明文(对齐 apply-hardening.sh 注释)。
                domains=["evil-c2-demo.com", "exfil-test.net"],
            )
            _fw_rule_group = route53resolver.CfnFirewallRuleGroup(
                self,
                "EgressFirewall",
                name="openclaw-egress-fw",
                firewall_rules=[
                    route53resolver.CfnFirewallRuleGroup.FirewallRuleProperty(
                        # BLOCK 必须带 block_response(NXDOMAIN/NODATA/OVERRIDE),否则
                        # ValidationException RSLVR-02016。NXDOMAIN=对 C2 域回「不存在」,
                        # 最干净的阻断,guest 解析直接失败(对齐 apply-hardening.sh:127)。
                        priority=100,
                        action="BLOCK",
                        block_response="NXDOMAIN",
                        firewall_domain_list_id=_fw_domain_list.attr_id,
                    )
                ],
            )
            route53resolver.CfnFirewallRuleGroupAssociation(
                self,
                "EgressFirewallAssoc",
                firewall_rule_group_id=_fw_rule_group.attr_id,
                vpc_id=vpc.vpc_id,
                priority=101,
                name="openclaw-assoc",
            )

        # ========== Wazuh 监控平台 EC2(10h-goal #20,CDK 一键部署)==========
        # config-gated(security.wazuh_enabled)。起一台专用监控 EC2,userdata 自动
        # 装 docker + compose,从 S3 assets 拉 docker-compose.wazuh.yml + 自定义
        # 规则,生成强随机凭据后 compose up,起 Wazuh manager+indexer+dashboard。
        # 与 metal host 隔离(独立 SG,只接受 agent 1514/1515 + 管理端口,dashboard
        # 不对 0.0.0.0 裸开 — 入站只放 VPC CIDR,生产再前置 ALB+ACM)。聚合 in-guest
        # auditd/FIM + GuardDuty(经 SNS)+ openclaw metrics。完整说明见
        # deploy/monitoring/WAZUH-RUNBOOK.md。
        if sec_cfg.get("wazuh_enabled", False):
            wazuh_sg = ec2.SecurityGroup(
                self,
                "WazuhSg",
                vpc=vpc,
                description="Wazuh monitoring platform: agent + mgmt, no 0.0.0.0",
                allow_all_outbound=True,
            )
            _vpc_cidr = vpc.vpc_cidr_block
            for _port, _desc in [
                (1514, "agent events"),
                (1515, "agent enrollment"),
                (55000, "manager API"),
                (443, "dashboard (front with ALB in prod)"),
            ]:
                wazuh_sg.add_ingress_rule(
                    ec2.Peer.ipv4(_vpc_cidr), ec2.Port.tcp(_port), _desc
                )
            wazuh_role = iam.Role(
                self,
                "WazuhRole",
                assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "AmazonSSMManagedInstanceCore"
                    )
                ],
            )
            assets_bucket.grant_read(wazuh_role)  # pull compose + rules from S3
            _wazuh_type = sec_cfg.get("wazuh_instance_type", "m7i.xlarge")
            _wz_ud = ec2.UserData.for_linux()
            _wz_prefix = "deployment/monitoring"
            _wz_pw_cmd = "openssl rand -base64 24"
            _wz_ud.add_commands(
                "set -euxo pipefail",
                "dnf install -y docker || yum install -y docker",
                "systemctl enable --now docker",
                'curl -sSL "https://github.com/docker/compose/releases/latest/download/'
                'docker-compose-linux-$(uname -m)" -o /usr/local/bin/docker-compose',
                "chmod +x /usr/local/bin/docker-compose",
                "sysctl -w vm.max_map_count=262144",  # wazuh-indexer requirement
                "mkdir -p /opt/wazuh/wazuh-rules",
                # retry pull — guards the race where the instance boots before
                # setup.sh finished uploading the monitoring assets to S3.
                f"for i in $(seq 1 30); do aws s3 cp s3://{assets_bucket.bucket_name}/{_wz_prefix}/docker-compose.wazuh.yml /opt/wazuh/docker-compose.yml && break || sleep 10; done",
                f"aws s3 cp s3://{assets_bucket.bucket_name}/{_wz_prefix}/wazuh-rules/openclaw_local_rules.xml /opt/wazuh/wazuh-rules/ || true",
                # strong random creds, never hardcoded; written to a 600 env file
                f'echo "WAZUH_INDEXER_PASSWORD=$({_wz_pw_cmd})" > /opt/wazuh/.env',
                f'echo "WAZUH_DASHBOARD_PASSWORD=$({_wz_pw_cmd})" >> /opt/wazuh/.env',
                "chmod 600 /opt/wazuh/.env",
                "cd /opt/wazuh && /usr/local/bin/docker-compose --env-file .env -f docker-compose.yml up -d",
            )
            wazuh_instance = ec2.Instance(
                self,
                "WazuhMonitor",
                vpc=vpc,
                instance_type=ec2.InstanceType(_wazuh_type),
                machine_image=ec2.MachineImage.latest_amazon_linux2023(),
                security_group=wazuh_sg,
                role=wazuh_role,
                user_data=_wz_ud,
                block_devices=[
                    ec2.BlockDevice(
                        device_name="/dev/xvda",
                        volume=ec2.BlockDeviceVolume.ebs(
                            100, encrypted=True
                        ),  # indexer needs disk; encrypted at rest
                    )
                ],
            )
            cdk.CfnOutput(self, "WazuhMonitorId", value=wazuh_instance.instance_id)
            cdk.CfnOutput(
                self,
                "WazuhDashboardHint",
                value="https://<WazuhMonitor private IP>:443 (front with ALB+ACM; SG = VPC CIDR only)",
            )

        # ========== VPC Flow Logs(安全加固 task #25)==========
        # 记录 VPC 内所有网络流量,用于检测跨租户东西向异常连接、验证 iptables
        # 隔离是否真生效、网络取证(CIS 3.8 Ensure VPC flow logging enabled)。
        # config-gated(flow_logs.enabled 默认 true);投递到受限保留期的
        # CloudWatch LogGroup;add_flow_log 自动建投递 IAM role。
        _flow_log_cfg = CFG.get("flow_logs", {}) or {}
        if _flow_log_cfg.get("enabled", True):
            _flow_log_group = logs.LogGroup(
                self,
                "VpcFlowLogGroup",
                log_group_name="/openclaw/vpc/flow-logs",
                retention=logs.RetentionDays.THREE_MONTHS
                if int(_flow_log_cfg.get("retention_days", 90)) >= 90
                else logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )
            vpc.add_flow_log(
                "VpcFlowLog",
                destination=ec2.FlowLogDestination.to_cloud_watch_logs(_flow_log_group),
                traffic_type=ec2.FlowLogTrafficType.ALL,
            )

        # ========== Multi-AZ HA (issue #8) ==========
        # `_az_count` controls how many AZs the ASG and ALB span. Default is
        # single-AZ to minimize cross-AZ data-transfer charges; opt in via
        # config.yml `multi_az.enabled: true`.
        _multi_az = CFG.get("multi_az", {}) or {}
        _az_count = (
            int(_multi_az.get("az_count", 2)) if _multi_az.get("enabled", False) else 1
        )

        sg = ec2.SecurityGroup(
            self,
            "HostSG",
            vpc=vpc,
            security_group_name="openclaw-host-sg",
            allow_all_outbound=True,
        )
        # 开发期 SSH:config host.ssh_ingress_sg 配了堡垒机 SG id,就放它入站 22
        # (绝不对 0.0.0.0/0 开 22,守红线;只允许指定堡垒机 SG)。生产留空=不开 22。
        _ssh_sg = (CFG.get("host", {}) or {}).get("ssh_ingress_sg") or None
        if _ssh_sg:
            sg.add_ingress_rule(
                ec2.Peer.security_group_id(_ssh_sg),
                ec2.Port.tcp(22),
                "SSH from bastion SG only (dev; never 0.0.0.0/0)",
            )

        # Compute allocatable resources from instance type. Fallback to the
        # arch-aware default if config.yml omits instance_type (issue #19).
        _arch_default = (
            "m8g.xlarge"
            if (CFG.get("host", {}) or {}).get("arch") == "arm64"
            else "m8i.xlarge"
        )
        _itype = (CFG.get("host", {}) or {}).get("instance_type") or _arch_default
        # vCPU count by size token. Handles virtual sizes ("2xlarge") and
        # bare-metal sizes. Graviton metal uses a "metal-24xl"/"metal-48xl"
        # suffix; the older 64-vCPU metals are a bare ".metal".
        _sizes = {
            "medium": 1,
            "large": 2,
            "xlarge": 4,
            "2xlarge": 8,
            "4xlarge": 16,
            "8xlarge": 32,
            "12xlarge": 48,
            "16xlarge": 64,
            "24xlarge": 96,
            # bare-metal size tokens (e.g. r8g.metal-24xl → "metal-24xl")
            "metal-24xl": 96,
            "metal-48xl": 192,
            "metal": 64,  # c7g/m7g/r7g.metal etc. (Graviton2/3 64-vCPU metal)
        }
        # Mem (MiB) per vCPU by family first letter. i8g/i8ge are memory-rich
        # like r-family (768GiB/96vcpu = 8GiB/vcpu), so map "i" alongside "r".
        _mem_ratio = {"c": 2048, "m": 4096, "r": 8192, "i": 8192}

        def _host_capacity(itype):
            """(vcpu, mem_mb) for a virtual or bare-metal instance type.
            All instances in a MixedInstancesPolicy pool must be the same
            capacity (host total_vcpu/total_mem is injected statically into
            the hosts table at init), so this is computed from the primary
            type and shared across the pool."""
            family, size = itype.split(".")[0], itype.split(".")[1]
            vcpu = _sizes[size]
            mem = vcpu * _mem_ratio[family[0]]
            return vcpu, mem

        _vcpu_total, _mem_total = _host_capacity(_itype)
        _avail_vcpu = _vcpu_total - CFG["host"]["reserved_vcpu"]
        _avail_mem = _mem_total - CFG["host"]["reserved_mem_mb"]

        # Load scripts from userdata/ and inject config
        ud_dir = Path(__file__).parent / "userdata"

        init_sh = (ud_dir / "init-host.sh").read_text()
        init_sh = init_sh.replace("{{ROOTFS_PREFIX}}", CFG["s3"]["rootfs_prefix"])
        # Mixed-instance-type: inject only the RESERVED headroom; init-host.sh
        # computes each host's real total from nproc + /proc/meminfo at boot and
        # subtracts this, so hosts of different sizes in one ASG each register
        # their TRUE capacity (the old {{AVAIL_VCPU}}/{{AVAIL_MEM}} baked one
        # size's total into every host → mis-sized hosts oversold/undersold).
        init_sh = init_sh.replace(
            "{{HOST_RESERVED_VCPU}}", str(CFG["host"]["reserved_vcpu"])
        )
        init_sh = init_sh.replace(
            "{{HOST_RESERVED_MEM}}", str(CFG["host"]["reserved_mem_mb"])
        )
        init_sh = init_sh.replace("{{SUBNET_PREFIX}}", CFG["vm"]["subnet_prefix"])
        init_sh = init_sh.replace(
            "{{ROOTFS_OVERLAY_MB}}", str(CFG["vm"].get("rootfs_overlay_mb", 8192))
        )
        init_sh = init_sh.replace(
            "{{AGENTCORE_GATEWAY_URL}}", gateway_url if gateway_url else "none"
        )
        init_sh = init_sh.replace("{{AMP_REMOTE_WRITE_URL}}", amp_remote_write_url)
        # Balloon config
        balloon_cfg = CFG.get("balloon", {})
        init_sh = init_sh.replace(
            "{{BALLOON_ENABLED}}", str(balloon_cfg.get("enabled", False)).lower()
        )
        init_sh = init_sh.replace(
            "{{BALLOON_DEFLATE_ON_OOM}}",
            str(balloon_cfg.get("deflate_on_oom", True)).lower(),
        )
        init_sh = init_sh.replace(
            "{{BALLOON_STATS_INTERVAL}}",
            str(balloon_cfg.get("stats_polling_interval_s", 5)),
        )
        init_sh = init_sh.replace(
            "{{BALLOON_FREE_PAGE_REPORTING}}",
            str(balloon_cfg.get("free_page_reporting", True)).lower(),
        )
        init_sh = init_sh.replace(
            "{{BALLOON_MAX_INFLATE_RATIO}}",
            str(balloon_cfg.get("max_inflate_ratio", 0.4)),
        )
        init_sh = init_sh.replace(
            "{{BALLOON_MIN_GUEST_AVAILABLE_MB}}",
            str(balloon_cfg.get("min_guest_available_mb", 512)),
        )
        # claw-hub dial-out endpoint(②EKS 灰度切生产流量的长期做法)。
        # 配 hub.alb_dns → 注入 EKS hub ALB 端点到 platform.env,launch-vm.sh
        # 据此覆盖每个 microVM 的 channel hubUrl/wsUrl;留空 → 注空串,launch-vm
        # 的 ${CLAW_HUB_WS:-...metal默认} 落回本地 hub(向后兼容)。改这里→重建
        # 节点即继承,绝不热改运行中 VM。
        hub_cfg = CFG.get("hub", {}) or {}
        hub_alb_dns = (hub_cfg.get("alb_dns") or "").strip()
        if hub_alb_dns:
            hub_scheme = (hub_cfg.get("alb_scheme") or "ws").strip()
            hub_port = str(hub_cfg.get("alb_port", 80)).strip()
            http_scheme = "https" if hub_scheme == "wss" else "http"
            hub_url_val = f"{http_scheme}://{hub_alb_dns}:{hub_port}"
            hub_ws_val = f"{hub_scheme}://{hub_alb_dns}:{hub_port}"
        else:
            hub_url_val = ""
            hub_ws_val = ""
        init_sh = init_sh.replace("{{CLAW_HUB_URL}}", hub_url_val)
        init_sh = init_sh.replace("{{CLAW_HUB_WS}}", hub_ws_val)
        # backup-data.sh is pulled from S3 at runtime (uses the $ASSETS_BUCKET shell
        # var init-host.sh resolves from the AssetsBucket stack output).
        init_sh = init_sh.replace(
            "{{BACKUP_DATA_SCRIPT}}",
            "aws s3 cp s3://${ASSETS_BUCKET}/deployment/scripts/backup-data.sh /home/ubuntu/backup-data.sh --region ${REGION}\n"
            "chmod +x /home/ubuntu/backup-data.sh && chown ubuntu:ubuntu /home/ubuntu/backup-data.sh",
        )

        host_agent_svc = (ud_dir / "host-agent.service").read_text()
        init_sh = init_sh.replace(
            "{{HOST_AGENT_SCRIPT}}",
            f"cat > /etc/systemd/system/host-agent.service << 'SVCEOF'\n{host_agent_svc}SVCEOF",
        )

        # NOTE: assets/backup bucket names + backup CMK key id are no longer injected
        # here. init-host.sh resolves them at runtime from stack outputs (AssetsBucket
        # / BackupBucket / BackupCmkKeyId), with an IMDS-account deterministic fallback.
        # This removes the ~19 Fn::Join bucket tokens that, together with the 21KB
        # script, blew the hard 16KB EC2 user-data limit.

        # user-data is now a plain string (no CFN tokens). The post-injection script
        # is ~23KB, over the hard 16KB EC2 limit. Raw gzip bytes can't go in a CFN
        # template (binary → "Template contains invalid characters"), so we embed a
        # base64(gzip(...)) blob inside a tiny ASCII bootstrap that decodes, gunzips
        # and execs it. base64 is ASCII-safe for the template; gzip keeps it small
        # (~9KB gzipped → ~12.5KB base64, comfortably under 16KB).
        import gzip as _gzip, base64 as _b64

        _blob = _b64.b64encode(_gzip.compress(init_sh.encode("utf-8"), 9)).decode(
            "ascii"
        )
        _bootstrap = (
            "#!/bin/bash\n"
            "set -e\n"
            f"echo {_blob} | base64 -d | gunzip > /tmp/init-host.sh\n"
            "exec bash /tmp/init-host.sh\n"
        )
        if len(_bootstrap.encode()) > 16384:
            raise ValueError(
                f"host user-data {len(_bootstrap.encode())}B exceeds 16KB even gzipped; "
                "move init-host.sh body to S3 bootstrap"
            )
        user_data = ec2.UserData.custom(_bootstrap)

        # AMI lookup — selects Ubuntu Noble for the configured CPU arch.
        # Graviton hosts (arch=arm64) need a *-arm64-server AMI; mismatched
        # AMI + instance type fails to boot, so we couple the two.
        _arch = (CFG.get("host", {}) or {}).get("arch", "x86_64")
        _ami_arch = "arm64" if _arch == "arm64" else "amd64"
        ami = ec2.MachineImage.lookup(
            name=f"ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-{_ami_arch}-server-*",
            owners=["099720109477"],
        )

        # InstanceType: honor an explicit override, otherwise pick a sensible
        # default per arch (m8g for Graviton, m8i for Intel).
        _instance_type_str = (CFG.get("host", {}) or {}).get("instance_type") or (
            "m8g.xlarge" if _arch == "arm64" else "m8i.xlarge"
        )

        # ── Mixed instance pool (task #22) ───────────────────────────────
        # config host.instance_types: optional list of types the ASG may pick
        # from (availability + Spot resilience). When given, the ASG runs a
        # MixedInstancesPolicy across them; the launch template declares the
        # primary type (pool's first entry). Phase 7: members may now be
        # DIFFERENT SIZES — init-host.sh self-reports each host's real capacity
        # (nproc + /proc/meminfo) at boot, so a smaller member registers its own
        # smaller total and is never oversold. The previous equal-capacity assert
        # is gone. Members must still share the SAME ARCH as the AMI (x86 vs
        # arm64 image is arch-specific); we assert that instead.
        _instance_pool = (CFG.get("host", {}) or {}).get("instance_types") or []
        if _instance_pool:
            # primary type comes from the pool's first entry
            _instance_type_str = _instance_pool[0]

            def _itype_is_arm(t):
                # Graviton families carry a 'g' in the family token
                # (m8g/c7g/r8g/i8g…); x86 (m8i/c7i/r7i…) do not.
                fam = t.split(".")[0]
                return "g" in fam

            _arch_is_arm = _arch == "arm64"
            _mismatched = [
                t for t in _instance_pool if _itype_is_arm(t) != _arch_is_arm
            ]
            if _mismatched:
                raise ValueError(
                    f"host.instance_types must all match host.arch={_arch} "
                    f"(AMI is arch-specific); mismatched: {_mismatched}"
                )
            # Capacity for scaler math is taken from the PRIMARY type; each host's
            # real capacity is what init-host.sh registers, so a mixed pool is
            # fine — this is only a planning estimate for scale-out headroom.
            _vcpu_total, _mem_total = _host_capacity(_instance_type_str)
            _avail_vcpu = _vcpu_total - CFG["host"]["reserved_vcpu"]
            _avail_mem = _mem_total - CFG["host"]["reserved_mem_mb"]

        # Bare-metal hosts run Firecracker on the Nitro hardware's native KVM,
        # NOT nested virtualization. Per AWS docs, CpuOptions.NestedVirtualization
        # applies only to *virtual* (non-metal) instance types and would fail /
        # be ignored on metal. We gate the nested-virt CustomResource on this.
        _is_metal = ".metal" in _instance_type_str

        # 开发期 SSH(CLAUDE.md 铁律:开发用 SSH 不用 SSM)。config host.ssh_key_name
        # 配了就给 metal 绑 keypair,让堡垒机能 SSH 进去调试/起节点。生产留空=无 key。
        _host_key_name = (CFG.get("host", {}) or {}).get("ssh_key_name") or None
        launch_template = ec2.LaunchTemplate(
            self,
            "HostLT",
            launch_template_name="openclaw-host-lt",
            instance_type=ec2.InstanceType(_instance_type_str),
            machine_image=ami,
            security_group=sg,
            role=host_role,
            user_data=user_data,
            key_name=_host_key_name,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    # Encrypt the root volume at rest too (AWS-managed KMS key),
                    # so the host has no plaintext EBS volume. Defense-in-depth:
                    # protects against EBS volume/snapshot exfiltration; does NOT
                    # protect microVM disk files against an on-host root dd
                    # (EBS encryption is transparent to host root).
                    volume=ec2.BlockDeviceVolume.ebs(
                        CFG["host"]["root_volume_gb"],
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                    ),
                ),
                ec2.BlockDevice(
                    device_name="/dev/sdf",
                    # Encrypt at rest with AWS-managed KMS key.
                    # Tenant data (rootfs overlays, data volumes, backups in transit) live here.
                    volume=ec2.BlockDeviceVolume.ebs(
                        CFG["host"]["data_volume_gb"],
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                        delete_on_termination=not CFG["host"].get(
                            "keep_data_volume", False
                        ),
                    ),
                ),
            ],
        )

        cfn_lt = launch_template.node.default_child

        # ── 实例 tag:让 host 被 Prometheus ec2_sd 自动发现 ──
        # deploy/monitoring/prometheus.yml 的 ec2_sd_configs 按 tag:Project=openclaw +
        # tag:Role=metal-host 过滤发现 metal host 抓 :8899/metrics。CDK LaunchTemplate
        # 默认只给实例打 Name,不打这俩 tag → Prometheus 发现不到 host、采集断链
        # (795 实测 metal 实例只有 Name tag)。这里在 LaunchTemplateData 层加
        # TagSpecifications,让 ASG 起的每台 host(及其卷)都带这俩 tag,随重建继承。
        # key 大小写须与 prometheus.yml filters 完全一致(Project/Role 首字母大写)。
        _host_tags = [
            {"Key": "Project", "Value": "openclaw"},
            {"Key": "Role", "Value": "metal-host"},
        ]
        cfn_lt.add_property_override(
            "LaunchTemplateData.TagSpecifications",
            [
                {"ResourceType": "instance", "Tags": _host_tags},
                {"ResourceType": "volume", "Tags": _host_tags},
            ],
        )

        # ── SECURITY (defense-in-depth for IMDS): require IMDSv2 + hop-limit 1 ──
        # The primary guest→IMDS egress block is the iptables DROP in
        # launch-vm.sh; this hardens the host side. Requiring session tokens
        # (HttpTokens=required) kills IMDSv1 credential theft via simple SSRF,
        # and HttpPutResponseHopLimit=1 stops a process one network hop away
        # from obtaining a token. host-agent.py / the AWS SDK use the IMDSv2
        # flow, so this is transparent to legitimate callers.
        cfn_lt.add_property_override(
            "LaunchTemplateData.MetadataOptions",
            {
                "HttpTokens": "required",
                "HttpPutResponseHopLimit": 1,
                "HttpEndpoint": "enabled",
            },
        )

        if CFG["asg"].get("use_spot"):
            cfn_lt.add_property_override(
                "LaunchTemplateData.InstanceMarketOptions",
                {
                    "MarketType": "spot",
                    "SpotOptions": {"SpotInstanceType": "one-time"},
                },
            )

        # Pin a launch-template version via CustomResource. On VIRTUAL hosts
        # this also flips CpuOptions.NestedVirtualization=enabled (CFN can't set
        # it directly). On BARE-METAL hosts (Firecracker on native KVM) nested
        # virtualization is neither needed nor supported, so we OMIT that field
        # and the version only restates the IMDS hardening. The CustomResource
        # still runs on metal so the ASG pins a known version either way.
        _lt_data_override = {
            # Carry the IMDSv2/hop-limit hardening into the pinned version too.
            # CreateLaunchTemplateVersion merges onto SourceVersion=$Latest so
            # this would normally be inherited, but we restate it so the
            # security posture is explicit and cannot silently regress.
            "MetadataOptions": {
                "HttpTokens": "required",
                "HttpPutResponseHopLimit": 1,
                "HttpEndpoint": "enabled",
            },
        }
        if not _is_metal:
            _lt_data_override["CpuOptions"] = {"NestedVirtualization": "enabled"}
        create_ver_call = cr.AwsSdkCall(
            service="EC2",
            action="createLaunchTemplateVersion",
            parameters={
                "LaunchTemplateId": launch_template.launch_template_id,
                "SourceVersion": "$Latest",
                "LaunchTemplateData": _lt_data_override,
            },
            physical_resource_id=cr.PhysicalResourceId.of(
                Fn.join(
                    "-",
                    [
                        "lt-version",
                        cfn_lt.ref,
                        Fn.get_att(
                            cfn_lt.logical_id, "LatestVersionNumber"
                        ).to_string(),
                    ],
                )
            ),
            output_paths=["LaunchTemplateVersion.VersionNumber"],
        )
        nested_virt = cr.AwsCustomResource(
            self,
            "NestedVirt",
            on_create=create_ver_call,
            on_update=create_ver_call,
            install_latest_aws_sdk=True,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "ec2:CreateLaunchTemplateVersion",
                            "ec2:DescribeLaunchTemplateVersions",
                        ],
                        resources=["*"],
                    ),
                ]
            ),
        )
        nested_virt.node.add_dependency(launch_template)

        set_default = cr.AwsCustomResource(
            self,
            "SetDefaultLTVersion",
            on_create=cr.AwsSdkCall(
                service="EC2",
                action="modifyLaunchTemplate",
                parameters={
                    "LaunchTemplateId": launch_template.launch_template_id,
                    "DefaultVersion": nested_virt.get_response_field(
                        "LaunchTemplateVersion.VersionNumber"
                    ),
                },
                physical_resource_id=cr.PhysicalResourceId.of("set-default-lt"),
            ),
            on_update=cr.AwsSdkCall(
                service="EC2",
                action="modifyLaunchTemplate",
                parameters={
                    "LaunchTemplateId": launch_template.launch_template_id,
                    "DefaultVersion": nested_virt.get_response_field(
                        "LaunchTemplateVersion.VersionNumber"
                    ),
                },
                physical_resource_id=cr.PhysicalResourceId.of("set-default-lt"),
            ),
            install_latest_aws_sdk=False,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["ec2:ModifyLaunchTemplate"], resources=["*"]
                    ),
                ]
            ),
        )
        set_default.node.add_dependency(nested_virt)

        asg = autoscaling.AutoScalingGroup(
            self,
            "HostASG",
            auto_scaling_group_name="openclaw-hosts-asg",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnets=vpc.public_subnets[:_az_count]
                or vpc.private_subnets[:_az_count]
            ),
            launch_template=launch_template,
            min_capacity=CFG["asg"]["min_capacity"],
            max_capacity=CFG["asg"]["max_capacity"],
        )
        asg.node.add_dependency(set_default)
        # Block ASG (and the first host) until the golden image is baked + in S3,
        # so init-host can pull the rootfs on first boot instead of timing out the
        # lifecycle hook. This is what lets min_capacity stay >=1 on a fresh region
        # without the manual two-phase (min0 → bake → scale1) dance.
        if image_ready is not None:
            asg.node.add_dependency(image_ready)
        cfn_asg = asg.node.default_child
        _pinned_ver = nested_virt.get_response_field(
            "LaunchTemplateVersion.VersionNumber"
        )
        if len(_instance_pool) >= 2:
            # MixedInstancesPolicy across the equal-capacity pool. This property
            # is mutually exclusive with the plain LaunchTemplate property, so
            # we null that out and supply the LT ref under the mixed policy.
            cfn_asg.add_property_override("LaunchTemplate", None)
            cfn_asg.add_property_override(
                "MixedInstancesPolicy",
                {
                    "LaunchTemplate": {
                        "LaunchTemplateSpecification": {
                            "LaunchTemplateId": launch_template.launch_template_id,
                            "Version": _pinned_ver,
                        },
                        "Overrides": [{"InstanceType": t} for t in _instance_pool],
                    },
                    # Capacity-optimized lowers Spot interruption by picking from
                    # the deepest-capacity pools; on-demand portion honors
                    # use_spot. Default: all on-demand unless use_spot sets a
                    # spot percentage in config.
                    "InstancesDistribution": {
                        "OnDemandBaseCapacity": CFG["asg"].get("on_demand_base", 0),
                        "OnDemandPercentageAboveBaseCapacity": (
                            0 if CFG["asg"].get("use_spot") else 100
                        ),
                        "SpotAllocationStrategy": "capacity-optimized",
                    },
                },
            )
        else:
            cfn_asg.add_property_override("LaunchTemplate.Version", _pinned_ver)
        # Lifecycle hooks (standalone resources, not inline LifecycleHookSpecificationList)
        autoscaling.CfnLifecycleHook(
            self,
            "InitHook",
            auto_scaling_group_name=asg.auto_scaling_group_name,
            lifecycle_hook_name="openclaw-host-init",
            lifecycle_transition="autoscaling:EC2_INSTANCE_LAUNCHING",
            heartbeat_timeout=CFG["asg"]["lifecycle_hook_timeout"],
            default_result="ABANDON",
        )
        autoscaling.CfnLifecycleHook(
            self,
            "TerminateHook",
            auto_scaling_group_name=asg.auto_scaling_group_name,
            lifecycle_hook_name="openclaw-host-terminate",
            lifecycle_transition="autoscaling:EC2_INSTANCE_TERMINATING",
            heartbeat_timeout=120,
            default_result="CONTINUE",
        )

        # When a new host completes init → process pending tenants
        events.Rule(
            self,
            "HostReadyRule",
            event_pattern=events.EventPattern(
                source=["aws.autoscaling"],
                detail_type=["EC2 Instance Launch Successful"],
            ),
            targets=[targets.LambdaFunction(api_fn)],
        )

        # When a host is terminating → cleanup DynamoDB records
        events.Rule(
            self,
            "HostTerminateRule",
            event_pattern=events.EventPattern(
                source=["aws.autoscaling"],
                detail_type=["EC2 Instance-terminate Lifecycle Action"],
            ),
            targets=[targets.LambdaFunction(api_fn)],
        )

        # ========== AgentCore (optional, continued) ==========
        if ac_enabled:
            # Gateway already created above (before userdata processing)

            # Register Lambda tools on Gateway
            if ac_gateway and ac_cfg.get("gateway", {}).get("enabled", True):
                tools_fn = _lambda.Function(
                    self,
                    "AgentCoreTools",
                    function_name="openclaw-agentcore-tools",
                    runtime=_lambda.Runtime.PYTHON_3_12,
                    handler="handler.lambda_handler",
                    code=_lambda.Code.from_asset("deploy/lambda/agentcore_tools"),
                    timeout=Duration.seconds(30),
                    memory_size=128,
                )
                ac_gateway.add_lambda_target(
                    "tools",
                    lambda_function=tools_fn,
                    tool_schema=agentcore.ToolSchema.from_inline(
                        [
                            agentcore.ToolDefinition(
                                name="hello",
                                description="Say hello — test tool for verifying AgentCore Gateway connectivity",
                                input_schema=agentcore.SchemaDefinition(
                                    type=agentcore.SchemaDefinitionType.OBJECT,
                                    properties={
                                        "name": agentcore.SchemaDefinition(
                                            type=agentcore.SchemaDefinitionType.STRING,
                                            description="Name to greet",
                                        )
                                    },
                                ),
                            ),
                            agentcore.ToolDefinition(
                                name="system_info",
                                description="Get Lambda runtime system information",
                                input_schema=agentcore.SchemaDefinition(
                                    type=agentcore.SchemaDefinitionType.OBJECT
                                ),
                            ),
                            agentcore.ToolDefinition(
                                name="timestamp",
                                description="Get current UTC timestamp",
                                input_schema=agentcore.SchemaDefinition(
                                    type=agentcore.SchemaDefinitionType.OBJECT,
                                    properties={
                                        "format": agentcore.SchemaDefinition(
                                            type=agentcore.SchemaDefinitionType.STRING,
                                            description="iso or unix",
                                        )
                                    },
                                ),
                            ),
                        ]
                    ),
                    gateway_target_name="openclaw-tools",
                )

            # Memory — persistent cross-session memory
            if ac_cfg.get("memory", {}).get("enabled", True):
                strategies = []
                for s in ac_cfg.get("memory", {}).get("strategies", ["semantic"]):
                    if s == "semantic":
                        strategies.append(
                            agentcore.MemoryStrategy.using_semantic(
                                name="openclaw_semantic",
                                namespaces=["/openclaw/tenant/{actorId}/semantic"],
                            )
                        )
                    elif s == "user_preference":
                        strategies.append(
                            agentcore.MemoryStrategy.using_user_preference(
                                name="openclaw_preferences",
                                namespaces=["/openclaw/tenant/{actorId}/preferences"],
                            )
                        )
                agentcore.Memory(
                    self,
                    "AgentCoreMemory",
                    memory_name="openclaw_memory",
                    description="OpenClaw per-tenant memory",
                    expiration_duration=Duration.days(
                        ac_cfg.get("memory", {}).get("expiration_days", 90)
                    ),
                    memory_strategies=strategies,
                )

            # Code Interpreter — secure sandboxed Python execution
            if ac_cfg.get("code_interpreter", {}).get("enabled", True):
                agentcore.CodeInterpreterCustom(
                    self,
                    "AgentCoreCodeInterpreter",
                    code_interpreter_custom_name="openclaw_code_interpreter",
                )

            # Browser — cloud-based web automation
            if ac_cfg.get("browser", {}).get("enabled", True):
                agentcore.BrowserCustom(
                    self,
                    "AgentCoreBrowser",
                    browser_custom_name="openclaw_browser",
                )

            # Identity — workload identity for agent AWS access
            agentcore_l1.CfnWorkloadIdentity(
                self,
                "AgentCoreIdentity",
                name="openclaw_identity",
            )

            # Policy — Cedar-based access control (configure via AgentCore console)
            # CfnPolicy requires PolicyEngine setup; deferred to console for initial deployment

            # Observability — enabled automatically via CloudWatch when Gateway/Memory are created

        # Pass AgentCore config to API Lambda
        if ac_enabled:
            api_fn.add_environment("AGENTCORE_ENABLED", "true")
            if gateway_url:
                api_fn.add_environment("AGENTCORE_GATEWAY_URL", gateway_url)

        # ╓─── [包A 数据面] owner=A ── ALB/CloudFront/CORS(入口,引用 api_fn/health_fn/assets_bucket)─╖
        # ========== ALB (Dashboard Proxy) ==========
        # ALB requires ≥2 subnets in different AZs (AWS API constraint).
        # The multi_az.az_count knob controls ASG fan-out; ALB independently
        # always claims max(2, az_count) subnets so single-AZ ASG mode still
        # produces a valid load balancer.
        _alb_az_count = max(2, _az_count)
        _alb_subnets = (
            vpc.public_subnets[:_alb_az_count] or vpc.private_subnets[:_alb_az_count]
        )
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "DashboardALB",
            load_balancer_name="openclaw-dashboard",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=_alb_subnets),
            internet_facing=True,
        )
        # 安全红线(Abel CLAUDE.md):公网走 CloudFront→ALB,ALB 入站【只】允许
        # CloudFront origin-facing managed prefix list,绝不对 0.0.0.0/0 开放。
        # add_listener 默认 open=True 会给 ALB SG 加 0.0.0.0/0:80 —— 必须 open=False,
        # 再显式只放行 CloudFront prefix list。prefix list id 各区不同,从 context 读
        # (cdk deploy 时 -c cf_origin_facing_prefix_list=pl-xxxx;ap-southeast-1=pl-31a34658)。
        listener = alb.add_listener(
            "HTTP",
            port=80,
            open=False,  # 不自动开 0.0.0.0/0
            default_action=elbv2.ListenerAction.fixed_response(
                404, content_type="text/plain", message_body="not found"
            ),
        )
        # CloudFront origin-facing managed prefix list,按 region 映射(context 可覆盖)。
        # 之前只从 context 读,不传就降级 VPC-only → CloudFront 回源被 SG 拒 → /hub 504
        # (重建实撞:必须手动补 pl 才通)。给常用 region 内置默认值让一键部署即可用。
        # ap-southeast-1=pl-31a34658 已真机实测放行后 CloudFront→ALB 通;其余 region 值
        # 若未列,部署时传 -c cf_origin_facing_prefix_list=<pl-id>(否则降级 VPC-only)。
        _CF_PL_BY_REGION = {
            "ap-southeast-1": "pl-31a34658",
        }
        _cf_pl = self.node.try_get_context(
            "cf_origin_facing_prefix_list"
        ) or _CF_PL_BY_REGION.get(self.region)
        if _cf_pl:
            listener.connections.allow_default_port_from(
                ec2.Peer.prefix_list(_cf_pl),
                "CloudFront origin-facing only (no 0.0.0.0/0)",
            )
        else:
            # 未知 region 且没传 context 则 fail-safe:只放 VPC 内(绝不退回 0.0.0.0/0)。
            listener.connections.allow_default_port_from(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                "fallback VPC-only: pass cf_origin_facing_prefix_list to lock to CloudFront",
            )
        # hub-WS 数据面接线(重建实撞:CloudFront /hub/* → ALB,但 ALB 只有 404 default、
        # 没有 hub target group / rule,host 也没注册 → 全部 504)。声明式建 TG(host:80,
        # 健康检查 nginx /hub/healthz)+ /hub/* forward rule + ASG 关联(host 随扩缩容自动
        # 进出 TG,不靠 init-host 命令式 register-targets)。metal 单进程 hub 走
        # CloudFront→ALB→host nginx:80 /hub/*→127.0.0.1:8790。
        hub_tg = elbv2.ApplicationTargetGroup(
            self,
            "HubTargetGroup",
            target_group_name="openclaw-hub-tg",
            vpc=vpc,
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.INSTANCE,
            health_check=elbv2.HealthCheck(
                path="/hub/healthz",
                healthy_http_codes="200",
                interval=Duration.seconds(15),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )
        listener.add_action(
            "HubRoute",
            priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/hub/*"])],
            action=elbv2.ListenerAction.forward([hub_tg]),
        )
        asg.attach_to_application_target_group(hub_tg)
        alb.connections.allow_to(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(80), "ALB to host Nginx"
        )
        sg.add_ingress_rule(
            ec2.Peer.security_group_id(
                alb.connections.security_groups[0].security_group_id
            ),
            ec2.Port.tcp(80),
            "ALB to Nginx",
        )
        sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(80),
            "VPC to Nginx (ALB IP target health check)",
        )
        # host-agent 在 :8899 暴露 /health + Prometheus /metrics(host-agent.py:1091
        # 绑 0.0.0.0:8899)。deploy/monitoring 的 Prometheus EC2(同 VPC,独立 SG)按
        # tag 发现 host 后抓 :8899/metrics —— 但 host SG 此前只放行 22/80,8899 入站没开,
        # 抓取会被挡(prometheus.yml 注释「host 入站 SG 反过来放行本机」这条要求此前没
        # 在 CDK 落地)。放行 8899 给 VPC CIDR(host-agent 只私网可达,同 80 健康检查的
        # VPC 内放行模式),让监控采集链通,随重建继承。绝不对 0.0.0.0/0 开。
        sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(8899),
            "VPC to host-agent :8899 (Prometheus /metrics scrape + /health)",
        )

        # Pass ALB info to API Lambda for path-based routing
        api_fn.add_environment("ALB_LISTENER_ARN", listener.listener_arn)
        api_fn.add_environment("VPC_ID", vpc.vpc_id)
        # 1.3.1: health_check Lambda needs ALB listener for AZ failover
        # to repoint /vm/<tenant_id>* rules across hosts.
        health_fn.add_environment("ALB_LISTENER_ARN", listener.listener_arn)
        # 1.4.2 (#fake-failover fix): the failover gate verifies the
        # tenant's dashboard URL is genuinely reachable through the public
        # path (ALB → nginx → VM) before flipping DDB to running. We use
        # the ALB DNS directly because (a) it's already public, (b) it
        # bypasses the CloudFront cache, (c) no extra DNS hop. The probe
        # hits http://<alb_dns>/vm/<tenant_id>/ — same path CloudFront
        # would forward to anyway.
        health_fn.add_environment(
            "PUBLIC_BASE_URL", f"http://{alb.load_balancer_dns_name}"
        )
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "elasticloadbalancing:CreateTargetGroup",
                    "elasticloadbalancing:DeleteTargetGroup",
                    "elasticloadbalancing:RegisterTargets",
                    "elasticloadbalancing:DeregisterTargets",
                    "elasticloadbalancing:CreateRule",
                    "elasticloadbalancing:DeleteRule",
                    "elasticloadbalancing:ModifyRule",
                    "elasticloadbalancing:DescribeRules",
                    "elasticloadbalancing:DescribeTargetGroups",
                    "elasticloadbalancing:DescribeListeners",
                ],
                resources=["*"],
            )
        )

        # ========== CloudFront ==========
        # 1.3.4 (#61): support two-distribution mode for security boundary between
        # operator console and per-tenant dashboards. Configured via:
        #
        #   cloudfront:
        #     console_domain: "console.example.com"     # operator console (S3)
        #     console_cert_arn: "arn:aws:acm:us-east-1:..."
        #     app_domain:     "app.example.com"         # per-tenant dashboards (ALB)
        #     app_cert_arn:   "arn:aws:acm:us-east-1:..."
        #
        # If both pairs are set → DUAL mode: two distinct CloudFront distributions
        # with independent ACM certs. Cognito session cookie scoped to console_domain
        # only — tenant dashboards on app_domain physically cannot read it.
        # If unset (or only legacy custom_domain set) → LEGACY single-distribution
        # mode, kept for backward-compat with v1.3.3 and earlier deployments.
        # Explicit OAC with a region-suffixed name. The default auto-named OAC is
        # derived from the stack name, so a same-named stack in another region
        # (ap-southeast-1) collides on this account-global CloudFront resource.
        _assets_oac = cloudfront.S3OriginAccessControl(
            self,
            "AssetsOAC",
            origin_access_control_name=f"openclaw-assets-oac{self._gsuffix}",
        )
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(
            assets_bucket, origin_access_control=_assets_oac
        )
        # CloudFront Function: rewrite /console/ → /console/index.html, / → /console/index.html
        url_rewrite_fn = cloudfront.Function(
            self,
            "UrlRewrite",
            function_name=f"openclaw-url-rewrite{self._gsuffix}",
            code=cloudfront.FunctionCode.from_inline("""
function handler(event) {
  var uri = event.request.uri;
  if (uri === '/') {
    return { statusCode: 302, statusDescription: 'Found',
      headers: { location: { value: '/console/' } } };
  }
  if (uri === '/console' || uri === '/console/') {
    event.request.uri = '/console/index.html';
  }
  return event.request;
}"""),
        )

        # Security response headers for edge-served content (#88 follow-up).
        # Static assets (S3 origin) shipped without HSTS / anti-clickjacking /
        # nosniff headers; add them at the edge so every cached response carries
        # them without touching each HTML file. Applied to the console/static
        # behaviors below.
        sec_headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "SecHeadersPolicy",
            response_headers_policy_name=f"openclaw-sec-headers{self._gsuffix}",
            comment="OpenClaw edge security headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                    override=True
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
            ),
        )

        cf_cfg = CFG.get("cloudfront", {}) or {}
        # ----- DUAL mode candidates -----
        console_domain = (cf_cfg.get("console_domain") or "").strip()
        console_cert_arn = (cf_cfg.get("console_cert_arn") or "").strip()
        app_domain = (cf_cfg.get("app_domain") or "").strip()
        app_cert_arn = (cf_cfg.get("app_cert_arn") or "").strip()
        dual_mode = bool(
            console_domain and console_cert_arn and app_domain and app_cert_arn
        )
        # ----- LEGACY single-domain fallback -----
        custom_domain = (cf_cfg.get("custom_domain") or "").strip()
        acm_cert_arn = (cf_cfg.get("acm_cert_arn") or "").strip()

        if dual_mode:
            # ===== DUAL mode: two distributions, two certs, two aliases =====
            # Distribution A: console — S3 origin only, /console/* + redirect / → /console/
            console_cf = cloudfront.Distribution(
                self,
                "ConsoleCF",
                comment="OpenClaw Operator Console",
                domain_names=[console_domain],
                certificate=acm.Certificate.from_certificate_arn(
                    self, "ConsoleCert", console_cert_arn
                ),
                default_behavior=cloudfront.BehaviorOptions(
                    origin=s3_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    response_headers_policy=sec_headers_policy,
                    function_associations=[
                        cloudfront.FunctionAssociation(
                            function=url_rewrite_fn,
                            event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                        )
                    ],
                ),
                default_root_object="",
            )
            # Distribution B: per-tenant dashboards — ALB origin only, /vm/*
            app_cf = cloudfront.Distribution(
                self,
                "AppCF",
                comment="OpenClaw Per-Tenant Dashboards",
                domain_names=[app_domain],
                certificate=acm.Certificate.from_certificate_arn(
                    self, "AppCert", app_cert_arn
                ),
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        alb.load_balancer_dns_name,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                        http_port=80,
                        read_timeout=Duration.seconds(60),
                        keepalive_timeout=Duration.seconds(60),
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                ),
                default_root_object="",
            )
            console_host = console_domain
            dashboard_host = app_domain
            console_cf_id = console_cf.distribution_id
            app_cf_id = app_cf.distribution_id
            cf_distribution = console_cf  # for downstream Cognito wiring
        else:
            # ===== LEGACY single-distribution mode =====
            domain_names = [custom_domain] if custom_domain else None
            certificate = (
                acm.Certificate.from_certificate_arn(self, "CustomCert", acm_cert_arn)
                if custom_domain and acm_cert_arn
                else None
            )

            cf_distribution = cloudfront.Distribution(
                self,
                "DashboardCF",
                comment="OpenClaw Dashboard (single-domain mode)",
                domain_names=domain_names,
                certificate=certificate,
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        alb.load_balancer_dns_name,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                        http_port=80,
                        read_timeout=Duration.seconds(60),
                        keepalive_timeout=Duration.seconds(60),
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                    function_associations=[
                        cloudfront.FunctionAssociation(
                            function=url_rewrite_fn,
                            event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                        )
                    ],
                ),
                additional_behaviors={
                    "/console/*": cloudfront.BehaviorOptions(
                        origin=s3_origin,
                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                        response_headers_policy=sec_headers_policy,
                        function_associations=[
                            cloudfront.FunctionAssociation(
                                function=url_rewrite_fn,
                                event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                            )
                        ],
                    ),
                    # /chat/* — chat 小程序前端,与 console 同 S3 origin(桶根 chat/,
                    # 见 setup.sh 把 console/chat/index.html 传到 s3://<assets>/chat/)。
                    # 缺这条 behavior 时 /chat/* 走 default→ALB(metal)→404。不挂
                    # url_rewrite_fn(那是 /console 路径改写专用);chat 是单 index.html。
                    "/chat/*": cloudfront.BehaviorOptions(
                        origin=s3_origin,
                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                        response_headers_policy=sec_headers_policy,
                    ),
                    # /hub/* — claw-hub WS multiplex (chat mini-app: Cognito
                    # /hub/token + the wss /hub/ws long connection). MUST forward
                    # Upgrade / Sec-WebSocket-* headers to the origin or the
                    # WebSocket handshake never completes (the default behavior's
                    # CachingDisabled policy alone forwards no headers → wss times
                    # out while plain POST /hub/token still works). ALL_VIEWER
                    # origin-request policy forwards every viewer header incl. the
                    # WS upgrade set. No url_rewrite fn here (it is for /console
                    # path rewriting only). Codifies the live fix applied during
                    # the 2026-06-28 P1 image bring-up.
                    "/hub/*": cloudfront.BehaviorOptions(
                        origin=origins.HttpOrigin(
                            alb.load_balancer_dns_name,
                            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                            http_port=80,
                            read_timeout=Duration.seconds(60),
                            keepalive_timeout=Duration.seconds(60),
                        ),
                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                        allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                        origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                    ),
                },
                default_root_object="",
            )

            # Single mode: console and dashboard share the same host
            console_host = custom_domain or cf_distribution.distribution_domain_name
            dashboard_host = console_host
            console_cf_id = cf_distribution.distribution_id
            app_cf_id = cf_distribution.distribution_id

        # ========== Assets bucket CORS (chat mini-app 图片功能) ==========
        # The chat mini-app uploads/downloads images via S3 presigned URLs
        # directly from the browser; without CORS the browser blocks the PUT
        # ("Failed to fetch"). Scope AllowedOrigins to the real CloudFront host
        # (console_host, set by both single- and dual-domain branches) — NOT "*"
        # — to keep minimal exposure. Managed via CustomResource because the
        # bucket is RETAIN (inline cors on an existing bucket won't reliably
        # update). This codifies the CORS that was first applied by hand during
        # the 2026-06-27 image-feature bring-up.
        _media_cors_params = {
            "Bucket": assets_bucket.bucket_name,
            "CORSConfiguration": {
                "CORSRules": [
                    {
                        "AllowedOrigins": [f"https://{console_host}"],
                        "AllowedMethods": ["GET", "PUT"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3000,
                    }
                ]
            },
        }
        cr.AwsCustomResource(
            self,
            "AssetsCors",
            install_latest_aws_sdk=False,
            on_create=cr.AwsSdkCall(
                service="S3",
                action="putBucketCors",
                parameters=_media_cors_params,
                physical_resource_id=cr.PhysicalResourceId.of("assets-cors"),
            ),
            on_update=cr.AwsSdkCall(
                service="S3",
                action="putBucketCors",
                parameters=_media_cors_params,
                physical_resource_id=cr.PhysicalResourceId.of("assets-cors"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["s3:PutBucketCORS"],
                        resources=[assets_bucket.bucket_arn],
                    ),
                ]
            ),
        )

        # ╓─── [包C 控制面+工程化] owner=C ── Cognito 鉴权 + Outputs(引用 cf_distribution/api_fn)─╖
        # ========== Console Auth (Cognito) ==========
        auth_cfg = CFG.get("console_auth", {})
        cognito_outputs = {}

        # ── Exchange IdP federation (task #13/#14) ───────────────────────────
        # Config-gated OIDC identity provider that lets external users sign in to
        # the same Cognito User Pool via their existing exchange account. The
        # exchange's real OIDC endpoints are NOT hardcoded — they come from
        # config.yml (`exchange_idp`). Empty / disabled config = no provider
        # added, the pool stays COGNITO-only, fully backward compatible.
        #
        # Federation is transparent to the hub: hub still only verifies the
        # *Cognito*-issued id_token (zero-credential guest constraint unchanged).
        # The external user's stable id (an OIDC claim, default `sub`) is mapped
        # to a Cognito custom attribute `custom:tenant_user_id`; Cognito's own
        # auto-generated `sub` becomes the tenant `owner_id`.
        idp_cfg = CFG.get("exchange_idp", {}) or {}
        idp_enabled = bool(idp_cfg.get("enabled", False)) and bool(
            idp_cfg.get("issuer_url")
        )
        idp_provider_name = (idp_cfg.get("provider_name") or "ExchangeIdP").strip()
        idp_stable_claim = (idp_cfg.get("stable_id_claim") or "sub").strip()
        idp_custom_attr = "tenant_user_id"

        def _idp_client_secret_ref():
            """CloudFormation dynamic reference for the OIDC client secret so the
            plaintext NEVER lands in the synthesized template. Accepts a Secrets
            Manager secret name/ARN (`client_secret_secret`) + optional JSON key
            (`client_secret_json_key`). Returns a `{{resolve:secretsmanager:...}}`
            token resolved at deploy time, or "" when no secret is configured
            (Cognito allows an empty secret for public OIDC clients)."""
            secret_name = (idp_cfg.get("client_secret_secret") or "").strip()
            if not secret_name:
                return ""
            json_key = (idp_cfg.get("client_secret_json_key") or "").strip()
            if json_key:
                return f"{{{{resolve:secretsmanager:{secret_name}:SecretString:{json_key}}}}}"
            return f"{{{{resolve:secretsmanager:{secret_name}:SecretString}}}}"

        def _idp_attribute_mapping():
            """Map the exchange stable-id claim into Cognito custom:tenant_user_id
            (the identity-chain join key), optionally email. AttributeMapping is
            an immutable jsii struct — every field must go to the constructor."""
            mapping_kwargs = {
                "custom": {
                    idp_custom_attr: cognito.ProviderAttribute.other(idp_stable_claim)
                },
            }
            if idp_cfg.get("map_email", True):
                mapping_kwargs["email"] = cognito.ProviderAttribute.other("email")
            return cognito.AttributeMapping(**mapping_kwargs)

        def _idp_request_method():
            m = (idp_cfg.get("attribute_request_method") or "GET").strip().upper()
            return (
                cognito.OidcAttributeRequestMethod.POST
                if m == "POST"
                else cognito.OidcAttributeRequestMethod.GET
            )

        if auth_cfg.get("enabled", False):
            existing_pool_id = auth_cfg.get("user_pool_id", "")

            # 1.3.4: callback URLs only target the console host (where the
            # operator actually logs in). In dual-mode, app_domain is NOT
            # listed here — the Cognito session cookie is therefore physically
            # scoped to console_domain and cannot be sent to per-tenant
            # dashboards on app_domain.
            #
            # chat 小程序(终端用户自助登录)与 console 同 CloudFront 同域(同
            # console_host,只是路径 /chat/index.html vs /console/index.html,见
            # CloudFront /chat/* behavior),故把 chat 回调一并列入 —— 它与 console
            # 共享同一 console_host 的 session cookie 域,不触动上面 app_domain 的
            # 跨域隔离设计。缺这条则 chat 自助登录回调被 Cognito redirect_mismatch
            # 拒(此前靠运行态手改 client 补,未随代码部署,现纳入 CDK 随重建继承)。
            callback_urls = [
                f"https://{console_host}/console/index.html",
                f"https://{console_host}/chat/index.html",
            ]
            # In legacy single-mode, also add the *.cloudfront.net default
            # so direct CF URL access still works during DNS migration.
            if not dual_mode and not custom_domain:
                pass  # console_host is already cf default domain
            elif not dual_mode and custom_domain:
                callback_urls.append(
                    f"https://{cf_distribution.distribution_domain_name}/console/index.html"
                )
                callback_urls.append(
                    f"https://{cf_distribution.distribution_domain_name}/chat/index.html"
                )

            if existing_pool_id:
                # Import the existing pool but recreate the domain + client as stack-owned resources.
                user_pool = cognito.UserPool.from_user_pool_id(
                    self, "ConsoleUserPool", existing_pool_id
                )
                cognito_outputs["CognitoUserPoolId"] = existing_pool_id

                # Legacy prefix (no account suffix) matches what 1.1.x created,
                # so existing users' bookmarked Cognito URLs keep working.
                domain_prefix = "openclaw-console"
                cognito.CfnUserPoolDomain(
                    self,
                    "ConsoleDomain",
                    user_pool_id=existing_pool_id,
                    domain=domain_prefix,
                )
                # Exchange IdP federation on the imported pool (task #13/#14).
                # The imported pool's custom attribute `custom:tenant_user_id`
                # must already exist on it (an imported pool's schema is
                # immutable from CDK); a stack-owned pool gets it added below.
                _exchange_idp = None
                _supported_idps = ["COGNITO"]
                if idp_enabled:
                    _exchange_idp = cognito.UserPoolIdentityProviderOidc(
                        self,
                        "ExchangeIdP",
                        user_pool=user_pool,
                        name=idp_provider_name,
                        client_id=idp_cfg.get("client_id", ""),
                        client_secret=_idp_client_secret_ref(),
                        issuer_url=idp_cfg["issuer_url"],
                        scopes=idp_cfg.get("scopes") or ["openid"],
                        attribute_request_method=_idp_request_method(),
                        attribute_mapping=_idp_attribute_mapping(),
                    )
                    _supported_idps.append(idp_provider_name)
                cfn_client = cognito.CfnUserPoolClient(
                    self,
                    "ConsoleClient",
                    user_pool_id=existing_pool_id,
                    generate_secret=False,
                    callback_ur_ls=callback_urls,
                    logout_ur_ls=callback_urls,
                    supported_identity_providers=_supported_idps,
                    # authorization-code flow (+ PKCE, client-side) instead of
                    # implicit: implicit returns only a 1h id_token and NO
                    # refresh_token, so the SPA was kicked back to login every
                    # hour. Code flow yields a refresh_token for silent renewal.
                    allowed_o_auth_flows=["code"],
                    allowed_o_auth_scopes=["openid", "email"],
                    allowed_o_auth_flows_user_pool_client=True,
                    explicit_auth_flows=[
                        "ALLOW_USER_PASSWORD_AUTH",
                        "ALLOW_USER_SRP_AUTH",
                        "ALLOW_REFRESH_TOKEN_AUTH",
                    ],
                    # 7-day refresh window (user's requirement) so a returning
                    # user within a week refreshes silently and never re-logs in.
                    # id/access tokens stay short-lived (Cognito hard-caps id at
                    # 24h); the refresh_token is what carries the 7 days.
                    refresh_token_validity=7,  # days (default unit)
                    id_token_validity=60,  # minutes
                    access_token_validity=60,  # minutes
                    token_validity_units=cognito.CfnUserPoolClient.TokenValidityUnitsProperty(
                        refresh_token="days",
                        id_token="minutes",
                        access_token="minutes",
                    ),
                )
                # The client may only list a provider that already exists.
                if _exchange_idp is not None:
                    cfn_client.node.add_dependency(_exchange_idp)
                cognito_outputs["CognitoClientId"] = cfn_client.ref
                cognito_outputs["CognitoDomain"] = (
                    f"{domain_prefix}.auth.{self.region}.amazoncognito.com"
                )
            else:
                # On a stack-owned pool we can add the `custom:tenant_user_id`
                # attribute the exchange IdP mapping writes into. Added only when
                # federation is enabled to keep the default pool minimal.
                _custom_attrs = None
                _ptg_fn = None
                if idp_enabled:
                    _custom_attrs = {
                        idp_custom_attr: cognito.StringAttribute(
                            min_len=1, max_len=256, mutable=True
                        )
                    }
                    # #97 档A — Pre-Token-Generation Lambda: on federated login,
                    # inject custom:tenant_user_id / custom:platform_id into the id_token
                    # so the broker (POST /tenants) and hub (POST /hub/token) get a stable
                    # tenant identity. Pure stdlib (no deps), fail-open (never blocks login).
                    # Only wired when federation is enabled to keep the default pool minimal.
                    _ptg_fn = _lambda.Function(
                        self,
                        "PreTokenGen",
                        function_name="openclaw-pretokengen",
                        runtime=_lambda.Runtime.PYTHON_3_12,
                        architecture=_lambda.Architecture.ARM_64,
                        handler="handler.handler",
                        code=_lambda.Code.from_asset("deploy/lambda/pretokengen"),
                        timeout=Duration.seconds(5),
                        memory_size=128,
                    )
                user_pool = cognito.UserPool(
                    self,
                    "ConsoleUserPool",
                    user_pool_name="openclaw-console",
                    self_sign_up_enabled=auth_cfg.get("self_sign_up", False),
                    sign_in_aliases=cognito.SignInAliases(email=True),
                    password_policy=cognito.PasswordPolicy(
                        min_length=8,
                        require_digits=True,
                        require_lowercase=True,
                    ),
                    custom_attributes=_custom_attrs,
                    # #97 档A — wire Pre-Token-Gen trigger (only set when federation on)
                    lambda_triggers=cognito.UserPoolTriggers(
                        pre_token_generation=_ptg_fn
                    )
                    if _ptg_fn is not None
                    else None,
                    removal_policy=self._stateful_removal,
                )
                user_pool.add_domain(
                    "ConsoleDomain",
                    cognito_domain=cognito.CognitoDomainOptions(
                        # account_id suffix keeps the domain prefix globally
                        # unique across stacks/accounts and survives RETAIN
                        # cleanup races (the prefix is global, not regional).
                        domain_prefix=f"openclaw-console-{self.account}{self._gsuffix}",
                    ),
                )
                # Exchange IdP federation on the new pool (task #13/#14).
                _exchange_idp = None
                _supported_l2_idps = [cognito.UserPoolClientIdentityProvider.COGNITO]
                if idp_enabled:
                    _exchange_idp = cognito.UserPoolIdentityProviderOidc(
                        self,
                        "ExchangeIdP",
                        user_pool=user_pool,
                        name=idp_provider_name,
                        client_id=idp_cfg.get("client_id", ""),
                        client_secret=_idp_client_secret_ref(),
                        issuer_url=idp_cfg["issuer_url"],
                        scopes=idp_cfg.get("scopes") or ["openid"],
                        attribute_request_method=_idp_request_method(),
                        attribute_mapping=_idp_attribute_mapping(),
                    )
                    _supported_l2_idps.append(
                        cognito.UserPoolClientIdentityProvider.custom(idp_provider_name)
                    )
                client = user_pool.add_client(
                    "ConsoleClient",
                    o_auth=cognito.OAuthSettings(
                        # authorization-code (+ PKCE) instead of implicit so the
                        # SPA receives a refresh_token for 7-day silent renewal.
                        flows=cognito.OAuthFlows(authorization_code_grant=True),
                        scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                        callback_urls=callback_urls,
                        logout_urls=callback_urls,
                    ),
                    supported_identity_providers=_supported_l2_idps,
                    auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
                    # 7-day refresh window; id/access short-lived (Cognito caps
                    # id_token at 24h, so the refresh_token carries the 7 days).
                    refresh_token_validity=Duration.days(7),
                    id_token_validity=Duration.minutes(60),
                    access_token_validity=Duration.minutes(60),
                )
                # The client must be created after the provider it references.
                if _exchange_idp is not None:
                    client.node.add_dependency(_exchange_idp)
                cognito_outputs["CognitoUserPoolId"] = user_pool.user_pool_id
                cognito_outputs["CognitoClientId"] = client.user_pool_client_id
                cognito_outputs["CognitoDomain"] = (
                    f"openclaw-console-{self.account}{self._gsuffix}.auth.{cdk.Stack.of(self).region}.amazoncognito.com"
                )

            # RBAC groups (issue #14): admin / operator / viewer.
            # Created on both new and existing pools so an imported pool also
            # gets the role groups. The handler maps `cognito:groups` claim →
            # role hierarchy (admin > operator > viewer).
            for group_name, description, precedence in (
                ("admin", "Full access — RBAC + CRUD + actions", 1),
                ("operator", "CRUD + lifecycle actions (no RBAC mgmt)", 2),
                ("viewer", "Read-only access", 3),
            ):
                cognito.CfnUserPoolGroup(
                    self,
                    f"Role{group_name.capitalize()}",
                    user_pool_id=user_pool.user_pool_id,
                    group_name=group_name,
                    description=description,
                    precedence=precedence,
                )

            # WI-002 — channel-plane machine-user app client (end-to-end Cognito).
            # config-gated (console_auth.channel_cognito_auth, default OFF) so
            # existing deployments are byte-unchanged until they opt in. A SINGLE
            # public client (no secret) shared by all per-tenant machine-users via
            # USER_PASSWORD_AUTH — one client, not one-per-tenant, so we never hit
            # the 1,000 app-clients-per-pool quota at 100k-tenant scale (the user
            # cap is 40M). Works for both imported and stack-owned pools (uses the
            # resolved user_pool.user_pool_id). The client id is injected into the
            # api/consumer Lambdas (COGNITO_CHANNEL_CLIENT_ID) which gate the whole
            # provision-on-create path on it being non-empty.
            if auth_cfg.get("channel_cognito_auth", False):
                channel_client = cognito.CfnUserPoolClient(
                    self,
                    "ChannelMachineUserClient",
                    user_pool_id=user_pool.user_pool_id,
                    generate_secret=False,  # public client → no in-guest SECRET_HASH
                    # USER_PASSWORD_AUTH only — machine users sign in with
                    # username(=tenant_id)+password; no OAuth/SRP/refresh flows.
                    explicit_auth_flows=["ALLOW_USER_PASSWORD_AUTH"],
                    access_token_validity=60,  # minutes
                    id_token_validity=60,  # minutes
                    refresh_token_validity=1,  # day (unused; min allowed)
                    token_validity_units=cognito.CfnUserPoolClient.TokenValidityUnitsProperty(
                        access_token="minutes",
                        id_token="minutes",
                        refresh_token="days",
                    ),
                    prevent_user_existence_errors="ENABLED",
                )
                cognito_outputs["CognitoChannelClientId"] = channel_client.ref

            # Fail-safe RBAC (1.5.0): inject the REAL, stack-owned Cognito ids so
            # the api Lambda can fetch JWKS and verify id_token signatures
            # (RS256). These override the construction-time placeholders — the
            # Cognito pool id is only known here, after the pool is created or
            # imported above. Without a genuine pool id the handler cannot
            # verify signatures and every request fails safe to `viewer`.
            api_fn.add_environment(
                "COGNITO_USER_POOL_ID", cognito_outputs.get("CognitoUserPoolId", "")
            )
            api_fn.add_environment(
                "COGNITO_CLIENT_ID", cognito_outputs.get("CognitoClientId", "")
            )
            # WI-002 — channel machine-user client id (empty unless gated on above).
            # The handler treats empty as "channel Cognito plane disabled" and
            # falls back to the legacy HMAC channel_secret (graceful rollout).
            _channel_client_id = cognito_outputs.get("CognitoChannelClientId", "")
            api_fn.add_environment("COGNITO_CHANNEL_CLIENT_ID", _channel_client_id)
            # cognito-idp admin perms to provision/delete the per-tenant
            # machine-user — granted only when the plane is enabled (least
            # privilege; no-op deployments get no extra IAM). Scoped to THIS pool.
            if _channel_client_id:
                _pool_arn = (
                    f"arn:aws:cognito-idp:{self.region}:{self.account}:"
                    f"userpool/{cognito_outputs.get('CognitoUserPoolId', '')}"
                )
                _cognito_admin_policy = iam.PolicyStatement(
                    actions=[
                        "cognito-idp:AdminCreateUser",
                        "cognito-idp:AdminSetUserPassword",
                        "cognito-idp:AdminDeleteUser",
                    ],
                    resources=[_pool_arn],
                )
                api_fn.add_to_role_policy(_cognito_admin_policy)
            # lifecycle consumer 跑同一 handler、做同样的 owner 验证(create_tenant
            # /tenant_action 经 _get_caller_identity),需同样的 Cognito pool/client id。
            if getattr(self, "_lifecycle_consumer", None) is not None:
                self._lifecycle_consumer.add_environment(
                    "COGNITO_USER_POOL_ID",
                    cognito_outputs.get("CognitoUserPoolId", ""),
                )
                self._lifecycle_consumer.add_environment(
                    "COGNITO_CLIENT_ID", cognito_outputs.get("CognitoClientId", "")
                )
                self._lifecycle_consumer.add_environment(
                    "COGNITO_CHANNEL_CLIENT_ID", _channel_client_id
                )
                # consumer also runs create_tenant (CREATE_VIA_QUEUE) → needs the
                # same provision/delete perms when the plane is enabled.
                if _channel_client_id:
                    self._lifecycle_consumer.add_to_role_policy(_cognito_admin_policy)

        # consumer 也需 ALB_LISTENER_ARN/VPC_ID(若 _add_alb_rule 被显式开)+ AgentCore
        # 等后置 env 与 api 对齐。这些 add_environment 默认对 consumer 无害(用到才读)。
        if getattr(self, "_lifecycle_consumer", None) is not None:
            self._lifecycle_consumer.add_environment(
                "ALB_LISTENER_ARN", listener.listener_arn
            )
            self._lifecycle_consumer.add_environment("VPC_ID", vpc.vpc_id)

        # ========== Outputs ==========
        for key, val in {
            "ApiUrl": api.url,
            "ApiKeyId": api_key.key_id,
            "TenantsTable": tenants_table.table_name,
            "HostsTable": hosts_table.table_name,
            "AssetsBucket": assets_bucket.bucket_name,
            # init-host.sh resolves these at runtime (kept out of user-data to
            # stay under the 16KB EC2 limit; see launch-template user-data below).
            "BackupBucket": backup_bucket.bucket_name,
            "BackupCmkKeyId": backup_cmk.key_id,
            "HostInstanceProfileArn": instance_profile.attr_arn,
            # 1.3.4: dual-mode outputs.
            #   ConsoleUrl    — operator console (Cognito-protected, S3-served)
            #   DashboardUrl  — per-tenant dashboards (ALB-served, app_domain in dual mode)
            # In legacy single-mode the two URLs are equal and point to the
            # combined CloudFront distribution, preserving backward compat.
            "ConsoleUrl": f"https://{console_host}",
            "DashboardUrl": f"https://{dashboard_host}",
            "DualDomainMode": "true" if dual_mode else "false",
            "CloudfrontDistributionId": console_cf_id,
            "AppCloudfrontDistributionId": app_cf_id,
            **(
                {"NotificationsTopicArn": notifications_topic_arn}
                if notifications_topic_arn
                else {}
            ),
            **cognito_outputs,
        }.items():
            cdk.CfnOutput(self, key, value=val)
