# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import platform as _platform
from pathlib import Path

import aws_cdk as cdk
import yaml
from aws_cdk import (
    BundlingOptions,
    Duration,
    Fn,
    RemovalPolicy,
)
from aws_cdk import (
    aws_apigateway as apigw,
)
from aws_cdk import (
    aws_aps as aps,
)
from aws_cdk import (
    aws_autoscaling as autoscaling,
)
from aws_cdk import (
    aws_bedrock_agentcore_alpha as agentcore,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore_l1,
)
from aws_cdk import (
    aws_certificatemanager as acm,
)
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cloudwatch_actions as cw_actions,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_grafana as grafana,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_lambda_event_sources as lambda_event_sources,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from aws_cdk import (
    aws_wafv2 as wafv2,
)
from aws_cdk import (
    custom_resources as cr,
)
from constructs import Construct

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yml").read_text())


def _normalize_config(cfg):
    """Clamp config combinations that would silently misbehave at runtime."""
    # mem_overcommit_ratio drives the scheduler's per-host allocatable memory
    # independently of balloon. With balloon off there's no reclaim path
    # (host-agent._adjust_balloons early-returns), so overcommit > 1.0 would
    # oversubscribe host memory with nothing to claw it back → OOM. Force it to
    # 1.0 so memory overcommit is a no-op unless balloon is actually enabled.
    mem_ratio = float(cfg.get("host", {}).get("mem_overcommit_ratio", 1.0))
    balloon_on = bool(cfg.get("balloon", {}).get("enabled", False))
    if mem_ratio > 1.0 and not balloon_on:
        print(f"⚠️  config.yml: host.mem_overcommit_ratio={mem_ratio} ignored — "
              "memory overcommit requires balloon.enabled=true (its only reclaim path). "
              "Deploying with effective ratio 1.0; set balloon.enabled=true to use overcommit.")
        cfg.setdefault("host", {})["mem_overcommit_ratio"] = 1.0

    # Issue #77 — guardrail on the overcommit ratios. A too-high ratio (the
    # prod incident ran CPU=8.0) lets ~100 microVMs pile onto a 15-vCPU host,
    # pegging its CPU and starving the control plane. Clamp to a sane ceiling
    # so a stray config / stale env can't silently oversubscribe a host.
    _CPU_RATIO_CEILING = 4.0
    _MEM_RATIO_CEILING = 4.0
    cpu_ratio = float(cfg.get("host", {}).get("cpu_overcommit_ratio", 1.0))
    if cpu_ratio > _CPU_RATIO_CEILING:
        print(f"⚠️  config.yml: host.cpu_overcommit_ratio={cpu_ratio} exceeds the "
              f"{_CPU_RATIO_CEILING}× safety ceiling — clamping. Very high CPU "
              "overcommit saturates the host and starves SSM/health polling.")
        cfg.setdefault("host", {})["cpu_overcommit_ratio"] = _CPU_RATIO_CEILING
    mem_ratio = float(cfg.get("host", {}).get("mem_overcommit_ratio", 1.0))
    if mem_ratio > _MEM_RATIO_CEILING:
        print(f"⚠️  config.yml: host.mem_overcommit_ratio={mem_ratio} exceeds the "
              f"{_MEM_RATIO_CEILING}× safety ceiling — clamping.")
        cfg.setdefault("host", {})["mem_overcommit_ratio"] = _MEM_RATIO_CEILING

    # Issue #72 — validate balloon.migrate_mode (how a balloon tenant migrates).
    _MIGRATE_MODES = ("cold", "live", "reject")
    mode = str(cfg.get("balloon", {}).get("migrate_mode", "cold")).lower()
    if mode not in _MIGRATE_MODES:
        print(f"⚠️  config.yml: balloon.migrate_mode={mode!r} invalid — "
              f"must be one of {_MIGRATE_MODES}. Defaulting to 'cold'.")
        cfg.setdefault("balloon", {})["migrate_mode"] = "cold"


_normalize_config(CFG)


def _sam_build_image_for_host():
    """SAM build image tag for the deploy host's arch (avoids QEMU). pip still
    cross-downloads the aarch64 wheel to match the ARM_64 Lambda."""
    machine = _platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "public.ecr.aws/sam/build-python3.12:latest-arm64"
    return "public.ecr.aws/sam/build-python3.12:latest-x86_64"


_LAMBDA_SRC = Path(__file__).parent / "lambda"
_LAMBDA_STAGE = Path(__file__).parent.parent / ".build" / "lambda"


def _stage_lambda_asset(name):
    """Stage a Lambda's source + the shared `common/` package into a build dir
    and return its path for Code.from_asset (T3-3).

    The shared helpers live in deploy/lambda/common/, a sibling of each handler
    dir, so a plain Code.from_asset("deploy/lambda/<name>") would NOT include
    them. We copytree the handler dir, then overlay common/ inside it, so at
    runtime `from common import ...` resolves next to the handler.

    The staging dir is suffixed with a hash of the source contents, so:
      * identical source → identical path (reused, never rebuilt);
      * the path is NEVER rmtree'd while a concurrent/deferred CDK asset bundle
        is still executing pip from it (the api Lambda bundles from this dir).
    Rebuilding a fixed path in place (the old approach) let one synth's rmtree
    pull the directory out from under another synth's in-flight pip bundle →
    "The folder you are executing pip from can no longer be found." Content-
    addressed dirs make staging idempotent and collision-free instead.
    """
    import hashlib
    import shutil

    src_handler = _LAMBDA_SRC / name
    src_common = _LAMBDA_SRC / "common"

    # Fingerprint every .py under the handler dir + common/ (path + bytes) so
    # any edit yields a fresh dir and a stale file can never linger in a reused
    # bundle.
    h = hashlib.sha256()
    for root in (src_handler, src_common):
        for f in sorted(root.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            h.update(str(f.relative_to(_LAMBDA_SRC)).encode())
            h.update(f.read_bytes())
    dest = _LAMBDA_STAGE / f"{name}-{h.hexdigest()[:12]}"

    if dest.exists():
        return str(dest)  # already staged for this exact source; reuse
    _LAMBDA_STAGE.mkdir(parents=True, exist_ok=True)

    # Stage into a temp sibling then atomically rename, so a half-copied dir is
    # never observed under `dest` by a parallel synth.
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    tmp = _LAMBDA_STAGE / f".{name}-{h.hexdigest()[:12]}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src_handler, tmp, ignore=ignore)
    shutil.copytree(src_common, tmp / "common", ignore=ignore)
    try:
        tmp.rename(dest)
    except OSError:
        # Another synth won the race and created dest first — drop our temp.
        shutil.rmtree(tmp, ignore_errors=True)
    return str(dest)


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

        self._build_tables()
        self._build_assets_bucket()
        self._build_lambda_shared_policy()
        self._build_sns_notifications()
        self._build_api_lambda()
        self._build_api_gateway()
        self._build_waf()
        self._build_api_routes()
        self._build_health_check()
        self._build_failover_queue()
        self._build_skills_lambda()
        self._build_templates_lambda()
        self._build_scaler_lambda()
        self._build_backup_lambda()
        self._build_alarms()
        self._build_host_role()
        self._build_prometheus_grafana()
        self._build_asg()
        self._build_multi_az()
        self._build_agentcore()
        self._build_alb()
        self._build_cloudfront()
        self._build_console_auth()
        self._build_outputs()

    def _build_tables(self):
        # ========== DynamoDB ==========
        # All control-plane tables enable point-in-time recovery (35-day
        # continuous backup → restore to any second) and deletion protection.
        # These tables hold the authoritative tenant→host→port→ALB-rule mapping;
        # a bad bulk edit (cleanup scripts exist), buggy deploy, or accidental
        # delete would otherwise be unrecoverable while tenant VMs keep running.
        tenants_table = dynamodb.Table(self, "Tenants",
            table_name="openclaw-tenants",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        hosts_table = dynamodb.Table(self, "Hosts",
            table_name="openclaw-hosts",
            partition_key=dynamodb.Attribute(name="instance_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # 1.4.0 (#62): per-tenant / per-group skill distribution.
        # When a tenant doesn't carry a `skills` list and isn't assigned a
        # `group`, the launch path falls back to the legacy "broadcast all
        # shared skills" behavior. Otherwise the effective set is computed
        # as tenant.skills ∪ group.skills (with unknown groups silently
        # dropped from the union — see api/handler.py::_resolve_effective_skills).
        groups_table = dynamodb.Table(self, "Groups",
            table_name="openclaw-groups",
            partition_key=dynamodb.Attribute(name="name", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Issue #17 — Audit log table. Single-partition by design (`pk="audit"`)
        # for time-range queries; ts as sort key. DDB TTL auto-expires entries
        # after `audit.retention_days` (default 90).
        # Table name carries a per-deploy suffix derived from Aws.STACK_ID so
        # that `cdk destroy` + redeploy never collides with the RETAIN-ed
        # orphan table from the previous stack incarnation.
        audit_cfg = CFG.get("audit", {}) or {}
        audit_retention_days = int(audit_cfg.get("retention_days", 90))
        # Aws.STACK_ID format: arn:aws:cloudformation:<region>:<acct>:stack/<name>/<uuid>
        # Take the first 5 hex chars of the UUID's leading segment as the suffix.
        stack_uuid = Fn.select(2, Fn.split("/", cdk.Aws.STACK_ID))
        audit_suffix = Fn.select(0, Fn.split("-", stack_uuid))
        audit_table = dynamodb.Table(self, "AuditLog",
            # Final name e.g. openclaw-audit-log-a1b2c3d4 (UUID first segment, 8 hex)
            table_name=Fn.join("-", ["openclaw-audit-log", audit_suffix]),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_ttl",
            # PITR yes; NOT deletion_protection — the per-deploy-suffixed name is
            # deliberately designed for cdk destroy + redeploy, and audit rows
            # are TTL-churned reconstructable data, not authoritative state.
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.tenants_table = tenants_table
        self.hosts_table = hosts_table
        self.groups_table = groups_table
        self.audit_table = audit_table
        self.audit_retention_days = audit_retention_days

    def _build_assets_bucket(self):
        # ========== S3 Assets Bucket ==========
        assets_bucket = s3.Bucket(self, "Assets",
            bucket_name=f"openclaw-assets-{self.account}",
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Lifecycle rule managed via CustomResource (RETAIN bucket won't update inline rules)
        cr.AwsCustomResource(self, "BackupLifecycle",
            install_latest_aws_sdk=False,
            on_create=cr.AwsSdkCall(
                service="S3",
                action="putBucketLifecycleConfiguration",
                parameters={
                    "Bucket": assets_bucket.bucket_name,
                    "LifecycleConfiguration": {"Rules": [{
                        "ID": "backup-expiration",
                        "Filter": {"Prefix": f"{CFG['s3']['backup_prefix']}/"},
                        "Status": "Enabled",
                        "Expiration": {"Days": CFG["s3"]["backup_retention_days"]},
                    }]},
                },
                physical_resource_id=cr.PhysicalResourceId.of("backup-lifecycle"),
            ),
            on_update=cr.AwsSdkCall(
                service="S3",
                action="putBucketLifecycleConfiguration",
                parameters={
                    "Bucket": assets_bucket.bucket_name,
                    "LifecycleConfiguration": {"Rules": [{
                        "ID": "backup-expiration",
                        "Filter": {"Prefix": f"{CFG['s3']['backup_prefix']}/"},
                        "Status": "Enabled",
                        "Expiration": {"Days": CFG["s3"]["backup_retention_days"]},
                    }]},
                },
                physical_resource_id=cr.PhysicalResourceId.of("backup-lifecycle"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(actions=["s3:PutLifecycleConfiguration"], resources=[assets_bucket.bucket_arn]),
            ]),
        )

        self.assets_bucket = assets_bucket

    def _build_lambda_shared_policy(self):
        # ========== Lambda Shared Policy ==========
        # T2-5: scope the wildcards. These statements are declared before the
        # ASG (created later) so we build literal ARNs from the well-known
        # names rather than referencing constructs. A tag condition applies to
        # ALL resources in a statement, so the untagged RunShellScript document
        # ARN must live in its own conditionless statement — else SendCommand
        # to the document breaks.
        _asg_arn = Fn.join("", [
            "arn:", cdk.Aws.PARTITION, ":autoscaling:", cdk.Aws.REGION, ":",
            cdk.Aws.ACCOUNT_ID, ":autoScalingGroup:*:autoScalingGroupName/openclaw-hosts-asg"])
        _run_shell_doc_arn = Fn.join("", [
            "arn:", cdk.Aws.PARTITION, ":ssm:", cdk.Aws.REGION, "::document/AWS-RunShellScript"])
        # ssm:SendCommand needs BOTH the document (untagged) and the target
        # instances (scoped by ASG tag); GetCommandInvocation has no resource
        # granularity, so it stays *.
        ssm_policies = [
            iam.PolicyStatement(actions=["ssm:SendCommand"],
                                resources=[_run_shell_doc_arn]),
            iam.PolicyStatement(
                actions=["ssm:SendCommand"],
                resources=[Fn.join("", ["arn:", cdk.Aws.PARTITION, ":ec2:",
                    cdk.Aws.REGION, ":", cdk.Aws.ACCOUNT_ID, ":instance/*"])],
                conditions={"StringEquals": {
                    "aws:ResourceTag/aws:autoscaling:groupName": "openclaw-hosts-asg"}}),
            iam.PolicyStatement(actions=["ssm:GetCommandInvocation"], resources=["*"]),
        ]
        # ec2 Describe* has no resource-level support (stays *); TerminateInstances
        # is scoped to ASG-tagged instances.
        ec2_policies = [
            iam.PolicyStatement(
                actions=["ec2:DescribeInstances", "ec2:DescribeInstanceTypes"],
                resources=["*"]),
            iam.PolicyStatement(
                actions=["ec2:TerminateInstances"],
                resources=[Fn.join("", ["arn:", cdk.Aws.PARTITION, ":ec2:",
                    cdk.Aws.REGION, ":", cdk.Aws.ACCOUNT_ID, ":instance/*"])],
                conditions={"StringEquals": {
                    "aws:ResourceTag/aws:autoscaling:groupName": "openclaw-hosts-asg"}}),
        ]

        self._asg_arn = _asg_arn
        self.ssm_policies = ssm_policies
        self.ec2_policies = ec2_policies

    def _build_sns_notifications(self):
        # ========== SNS Lifecycle Notifications (issue #13, optional) ==========
        notif_cfg = CFG.get("notifications", {}) or {}
        notifications_topic = None
        notifications_topic_arn = ""
        if notif_cfg.get("enabled", False):
            notifications_topic = sns.Topic(self, "TenantEvents",
                topic_name="openclaw-tenant-events",
                display_name="OpenClaw Tenant Lifecycle Events",
            )
            notifications_topic_arn = notifications_topic.topic_arn

        # T2-3: shared DLQ for EventBridge → Lambda targets. Captures events
        # EventBridge could not deliver after retries (throttle / 5xx) so a
        # dropped host-terminate or health tick is recoverable, not silently lost.
        events_dlq = sqs.Queue(self, "EventsDLQ",
            queue_name="openclaw-events-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )

        self.notifications_topic = notifications_topic
        self.notifications_topic_arn = notifications_topic_arn
        self.events_dlq = events_dlq

    def _build_api_lambda(self):
        tenants_table = self.tenants_table
        hosts_table = self.hosts_table
        groups_table = self.groups_table
        audit_table = self.audit_table
        audit_retention_days = self.audit_retention_days
        assets_bucket = self.assets_bucket
        notifications_topic = self.notifications_topic
        notifications_topic_arn = self.notifications_topic_arn
        ssm_policies = self.ssm_policies
        ec2_policies = self.ec2_policies
        _asg_arn = self._asg_arn

        # ========== API Lambda ==========
        api_fn = _lambda.Function(self, "ApiHandler",
            function_name="openclaw-api",
            runtime=_lambda.Runtime.PYTHON_3_12,
            # 1.5.0: ARM_64 (Graviton) — cheaper/faster. Bundles PyJWT + cryptography
            # for Cognito JWT RS256 verification (cryptography has a native ext).
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(
                _stage_lambda_asset("api"),
                bundling=BundlingOptions(
                    # Image arch = build host (not Lambda) to avoid arm64-on-x86
                    # exec format error; pip cross-downloads the aarch64 wheel.
                    image=cdk.DockerImage.from_registry(_sam_build_image_for_host()),
                    command=[
                        "bash", "-c",
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
            environment={
                "TENANTS_TABLE": tenants_table.table_name,
                "HOSTS_TABLE": hosts_table.table_name,
                "GROUPS_TABLE": groups_table.table_name,
                "AUDIT_TABLE": audit_table.table_name,
                "AUDIT_TTL_DAYS": str(audit_retention_days),
                "ASSETS_BUCKET": assets_bucket.bucket_name,
                "NOTIFICATIONS_TOPIC_ARN": notifications_topic_arn,
                "ROOTFS_PREFIX": CFG["s3"]["rootfs_prefix"],
                "HOST_RESERVED_VCPU": str(CFG["host"]["reserved_vcpu"]),
                "HOST_RESERVED_MEM": str(CFG["host"]["reserved_mem_mb"]),
                "CPU_OVERCOMMIT_RATIO": str(CFG["host"].get("cpu_overcommit_ratio", 1.0)),
                "MEM_OVERCOMMIT_RATIO": str(CFG["host"].get("mem_overcommit_ratio", 1.0)),
                # Issue #77 — absolute per-host VM ceiling (0 = ratio-only, no cap).
                "MAX_VMS_PER_HOST": str(CFG["host"].get("max_vms_per_host", 0)),
                "VM_DEFAULT_VCPU": str(CFG["vm"]["default_vcpu"]),
                "VM_DEFAULT_MEM": str(CFG["vm"]["default_mem_mb"]),
                "VM_DATA_DISK_MB": str(CFG["vm"]["data_disk_mb"]),
                "VM_PORT_BASE": str(CFG["vm"]["gateway_port_base"]),
                "VM_SUBNET_PREFIX": CFG["vm"]["subnet_prefix"],
                "ASG_NAME": "openclaw-hosts-asg",
                "BACKUP_PREFIX": CFG["s3"]["backup_prefix"],
                # Issue #9 — per-tenant quotas (0 = unlimited)
                "QUOTAS_ENABLED": str(CFG.get("quotas", {}).get("enabled", False)).lower(),
                "QUOTAS_MAX_VCPU": str(CFG.get("quotas", {}).get("max_vcpu_per_tenant", 0)),
                "QUOTAS_MAX_MEM_MB": str(CFG.get("quotas", {}).get("max_mem_mb_per_tenant", 0)),
                "QUOTAS_MAX_DATA_DISK_MB": str(CFG.get("quotas", {}).get("max_data_disk_mb", 0)),
                # Surface feature flags to the API so /system/info can render
                # accurate state to the console without the console having to
                # re-parse config.yml. Empty / "false" when feature is off.
                "MULTI_AZ_ENABLED": str(CFG.get("multi_az", {}).get("enabled", False)).lower(),
                "MULTI_AZ_COUNT": str(CFG.get("multi_az", {}).get("az_count", 1)),
                "WAF_ENABLED": str(CFG.get("waf", {}).get("enabled", False)).lower(),
                # Issue #72: balloon state + how to migrate a balloon tenant.
                # cold (default) = stop+ship+relaunch (always safe, brief
                # restart); live = snapshot/restore with a host-agent quiesce
                # sentinel; reject = refuse (409). Off-balloon tenants always
                # use the plain live snapshot path.
                "BALLOON_ENABLED": str(CFG.get("balloon", {}).get("enabled", False)).lower(),
                "BALLOON_MIGRATE_MODE": str(CFG.get("balloon", {}).get("migrate_mode", "cold")).lower(),
                # COGNITO_USER_POOL_ID / COGNITO_CLIENT_ID are injected AFTER the
                # Cognito section computes the real, stack-owned pool/client ids
                # (see add_environment below). The config.yml value is unreliable
                # (often empty), so we never read it here — fail-safe RBAC needs
                # the genuine pool id to fetch JWKS for signature verification.
                "CONSOLE_AUTH_ENABLED": str((CFG.get("console_auth", {}) or {}).get("enabled", False)).lower(),
                # Fail-safe default role when a request carries no Bearer token
                # (API-key-only path). "viewer" = least privilege. Override per
                # environment only for trusted automation that cannot present a
                # Cognito id_token.
                "DEFAULT_NO_JWT_ROLE": str(CFG.get("console_auth", {}).get("default_no_jwt_role", "viewer")),
                # RBAC role-gating — independent of console_auth, default off.
                "RBAC_ENABLED": str((CFG.get("console_auth", {}) or {}).get("rbac_enabled", False)).lower(),
                "PROJECT_VERSION": _read_pyproject_version(),
            },
        )
        tenants_table.grant_read_write_data(api_fn)
        hosts_table.grant_read_write_data(api_fn)
        groups_table.grant_read_write_data(api_fn)
        # Issue #17 — api Lambda writes audits and reads them back via GET /audit-log
        audit_table.grant_read_write_data(api_fn)
        assets_bucket.grant_read(api_fn)
        # 1.4.1 (#63) — Console skills CRUD: api Lambda writes SKILL.md
        # via PUT /skills/{name} and removes the skills/{name}/ prefix
        # via DELETE /skills/{name}.
        assets_bucket.grant_put(api_fn)
        assets_bucket.grant_delete(api_fn)
        # Issue #13 — allow publishing tenant lifecycle events
        if notifications_topic is not None:
            notifications_topic.grant_publish(api_fn)
        for _s in ssm_policies + ec2_policies:
            api_fn.add_to_role_policy(_s)
        # T2-5: Describe* has no resource-level support (stays *); the three
        # mutating actions scope to the openclaw ASG ARN.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["autoscaling:DescribeAutoScalingGroups"], resources=["*"]))
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["autoscaling:SetDesiredCapacity",
                     "autoscaling:CompleteLifecycleAction",
                     "autoscaling:TerminateInstanceInAutoScalingGroup"],
            resources=[_asg_arn],
        ))

        self.api_fn = api_fn

    def _build_api_gateway(self):
        # ========== API Gateway ==========
        api = apigw.RestApi(self, "Api",
            rest_api_name="openclaw-orchestrator",
            deploy_options=apigw.StageOptions(stage_name="v1"),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "x-api-key", "Authorization"],
            ),
        )

        # API Key + Usage Plan
        api_key = api.add_api_key("ApiKey",
            api_key_name="openclaw-admin-key",
        )
        plan = api.add_usage_plan("UsagePlan",
            name="openclaw-plan",
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
            api_stages=[apigw.UsagePlanPerApiStage(api=api, stage=api.deployment_stage)],
        )
        plan.add_api_key(api_key)

        self.api = api
        self.api_key = api_key

    def _build_waf(self):
        api = self.api

        # ========== WAF (issue #7, optional) ==========
        waf_cfg = CFG.get("waf", {}) or {}
        if waf_cfg.get("enabled", False):
            rate_limit = int(waf_cfg.get("rate_limit_per_ip", 1000))
            managed_rule_names = list(waf_cfg.get("managed_rules", []) or [])

            rules = []
            priority = 0
            # Rule #1: rate-based per source IP. Always added when WAF is enabled.
            rules.append(wafv2.CfnWebACL.RuleProperty(
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
            ))
            priority += 1

            # AWS managed rule groups (CommonRuleSet, KnownBadInputs, etc.)
            for rule_name in managed_rule_names:
                rules.append(wafv2.CfnWebACL.RuleProperty(
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
                ))
                priority += 1

            web_acl = wafv2.CfnWebACL(self, "ApiWebACL",
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
            stage_arn = Fn.join("", [
                "arn:", cdk.Aws.PARTITION,
                ":apigateway:", cdk.Aws.REGION,
                "::/restapis/", api.rest_api_id,
                "/stages/", api.deployment_stage.stage_name,
            ])
            wafv2.CfnWebACLAssociation(self, "ApiWebACLAssociation",
                resource_arn=stage_arn,
                web_acl_arn=web_acl.attr_arn,
            )

    def _build_api_routes(self):
        api = self.api
        api_fn = self.api_fn

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
        api_fn.add_permission("ApiGwInvoke",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=Fn.join("", [
                "arn:", cdk.Aws.PARTITION, ":execute-api:", cdk.Aws.REGION,
                ":", cdk.Aws.ACCOUNT_ID, ":", api.rest_api_id, "/*/*",
            ]),
        )
        _api_fn_view = _lambda.Function.from_function_arn(
            self, "ApiHandlerView", api_fn.function_arn,
        )

        def _li():
            """A LambdaIntegration that does NOT add a per-method permission
            (built against the imported view). The single wildcard permission
            above authorises every method."""
            return apigw.LambdaIntegration(_api_fn_view)

        tenants_resource = api.root.add_resource("tenants")
        tenants_resource.add_method("GET", _li(), **key_required)
        tenants_resource.add_method("POST", _li(), **key_required)

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

        refresh_rootfs_resource = hosts_resource.add_resource("refresh-rootfs")
        refresh_rootfs_resource.add_method("POST", _li(), **key_required)

        rootfs_version_resource = hosts_resource.add_resource("rootfs-version")
        rootfs_version_resource.add_method("GET", _li(), **key_required)

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

        self.key_required = key_required
        self._li = _li

    def _build_health_check(self):
        tenants_table = self.tenants_table
        hosts_table = self.hosts_table
        audit_table = self.audit_table
        assets_bucket = self.assets_bucket
        notifications_topic = self.notifications_topic
        notifications_topic_arn = self.notifications_topic_arn
        ssm_policies = self.ssm_policies
        events_dlq = self.events_dlq
        api_fn = self.api_fn

        # ========== Health Check Lambda ==========
        hc_cfg = CFG.get("health_check", {}) or {}
        az_failover_cfg = hc_cfg.get("az_failover", {}) or {}
        health_fn = _lambda.Function(self, "HealthCheck",
            function_name="openclaw-health-check",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(_stage_lambda_asset("health_check")),
            timeout=Duration.seconds(180),  # 1.3.1: room for synchronous SSM wait during failover
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
                # T3-3: failover placement now uses the SAME capacity math as
                # the API scheduler (common.capacity), so the health_check
                # Lambda needs the overcommit ratios + per-host VM ceiling it
                # never had before. Without these it silently fell back to
                # 1.0/1.0/0 and could place a VM where the API would reject it.
                "CPU_OVERCOMMIT_RATIO": str(CFG["host"].get("cpu_overcommit_ratio", 1.0)),
                "MEM_OVERCOMMIT_RATIO": str(CFG["host"].get("mem_overcommit_ratio", 1.0)),
                "MAX_VMS_PER_HOST": str(CFG["host"].get("max_vms_per_host", 0)),
                # ALB_LISTENER_ARN injected after listener creation (see below)
                "AZ_FAILOVER_ENABLED": str(bool(az_failover_cfg.get("enabled", True))).lower(),
                "AZ_UNHEALTHY_THRESHOLD_MINUTES": str(int(az_failover_cfg.get("unhealthy_threshold_minutes", 10))),
                "AZ_COOLDOWN_MINUTES": str(int(az_failover_cfg.get("cooldown_minutes", 30))),
            },
        )
        tenants_table.grant_read_write_data(health_fn)
        hosts_table.grant_read_write_data(health_fn)
        audit_table.grant_write_data(health_fn)
        assets_bucket.grant_read(health_fn)  # 1.3.1: list backups for failover
        if notifications_topic is not None:
            notifications_topic.grant_publish(health_fn)
        # 1.3.1: ALB rule re-pointing during cross-host failover.
        # T2-5: ELB Describe* has no resource-level support (stays *); the
        # mutating actions scope to this account+region's ELB ARN space (rule
        # and target-group ARNs are created dynamically, so an account-scoped
        # prefix is the tightest safe bound without the not-yet-created ARNs).
        _elb_arns = [
            Fn.join("", ["arn:", cdk.Aws.PARTITION, ":elasticloadbalancing:",
                cdk.Aws.REGION, ":", cdk.Aws.ACCOUNT_ID, ":listener-rule/app/*"]),
            Fn.join("", ["arn:", cdk.Aws.PARTITION, ":elasticloadbalancing:",
                cdk.Aws.REGION, ":", cdk.Aws.ACCOUNT_ID, ":listener/app/*"]),
            Fn.join("", ["arn:", cdk.Aws.PARTITION, ":elasticloadbalancing:",
                cdk.Aws.REGION, ":", cdk.Aws.ACCOUNT_ID, ":targetgroup/*"]),
        ]
        health_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["elasticloadbalancing:DescribeRules",
                     "elasticloadbalancing:DescribeTargetGroups"],
            resources=["*"]))
        health_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticloadbalancing:CreateRule",
                "elasticloadbalancing:ModifyRule",
                "elasticloadbalancing:CreateTargetGroup",
                "elasticloadbalancing:RegisterTargets",
            ],
            resources=_elb_arns,
        ))
        for _s in ssm_policies:
            health_fn.add_to_role_policy(_s)

        events.Rule(self, "HealthCheckSchedule",
            schedule=events.Schedule.rate(Duration.minutes(CFG["health_check"]["interval_minutes"])),
            targets=[targets.LambdaFunction(health_fn,
                dead_letter_queue=events_dlq, retry_attempts=2)],
        )

        # T2-8: the api Lambda's POST /failover/{az} invokes the health-check
        # Lambda (which owns the AZ-failover routine) for manual evacuation.
        api_fn.add_environment("HEALTH_CHECK_FUNCTION", health_fn.function_name)
        health_fn.grant_invoke(api_fn)

        self.hc_cfg = hc_cfg
        self.az_failover_cfg = az_failover_cfg
        self.health_fn = health_fn
        self._elb_arns = _elb_arns

    def _build_failover_queue(self):
        tenants_table = self.tenants_table
        hosts_table = self.hosts_table
        audit_table = self.audit_table
        assets_bucket = self.assets_bucket
        notifications_topic = self.notifications_topic
        notifications_topic_arn = self.notifications_topic_arn
        ssm_policies = self.ssm_policies
        az_failover_cfg = self.az_failover_cfg
        health_fn = self.health_fn
        _elb_arns = self._elb_arns

        # ========== AZ-failover worker queue (T3-2) ==========
        # The detector (health_fn) enqueues one job per affected tenant; the
        # worker executes them in PARALLEL. This turns full-AZ recovery from
        # O(5min × tenant count) — the old synchronous loop died mid-loop past
        # ~1 tenant — into O(one tenant). Gated by FAILOVER_QUEUE_URL: unset ⇒
        # health_fn falls back to the legacy synchronous path (TF-path safe).
        failover_dlq = sqs.Queue(self, "FailoverDLQ",
            queue_name="openclaw-failover-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        # INVARIANT (asserted in tests): visibility_timeout (900s) MUST exceed
        # the worker's timeout (600s). A redelivery then proves the prior
        # attempt is dead, making the worker's re-claim-on-recovering safe.
        failover_queue = sqs.Queue(self, "FailoverQueue",
            queue_name="openclaw-failover",
            visibility_timeout=Duration.seconds(900),
            retention_period=Duration.days(4),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=2, queue=failover_dlq),
        )
        failover_worker_fn = _lambda.Function(self, "FailoverWorker",
            function_name="openclaw-failover-worker",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="worker.lambda_handler",
            # Same asset dir as health_fn so worker.py can `import handler`.
            code=_lambda.Code.from_asset(_stage_lambda_asset("health_check")),
            timeout=Duration.seconds(600),
            memory_size=256,
            environment={
                "TENANTS_TABLE": tenants_table.table_name,
                "HOSTS_TABLE": hosts_table.table_name,
                "AUDIT_TABLE": audit_table.table_name,
                "SNS_TOPIC_ARN": notifications_topic_arn,
                "ASSETS_BUCKET": assets_bucket.bucket_name,
                # Worker imports handler.py, which reads these at module load;
                # keep them in sync with the detector (T3-3).
                "CPU_OVERCOMMIT_RATIO": str(CFG["host"].get("cpu_overcommit_ratio", 1.0)),
                "MEM_OVERCOMMIT_RATIO": str(CFG["host"].get("mem_overcommit_ratio", 1.0)),
                "MAX_VMS_PER_HOST": str(CFG["host"].get("max_vms_per_host", 0)),
                "AZ_FAILOVER_ENABLED": str(bool(az_failover_cfg.get("enabled", True))).lower(),
                "AZ_UNHEALTHY_THRESHOLD_MINUTES": str(int(az_failover_cfg.get("unhealthy_threshold_minutes", 10))),
                "AZ_COOLDOWN_MINUTES": str(int(az_failover_cfg.get("cooldown_minutes", 30))),
                # ALB_LISTENER_ARN / PUBLIC_BASE_URL injected after listener creation.
            },
        )
        tenants_table.grant_read_write_data(failover_worker_fn)
        hosts_table.grant_read_write_data(failover_worker_fn)
        audit_table.grant_write_data(failover_worker_fn)
        assets_bucket.grant_read(failover_worker_fn)
        if notifications_topic is not None:
            notifications_topic.grant_publish(failover_worker_fn)
        # Worker re-points ALB rules + runs SSM launch-vm.sh, same as the
        # detector — mirror the ELB + SSM grants.
        failover_worker_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["elasticloadbalancing:DescribeRules",
                     "elasticloadbalancing:DescribeTargetGroups"],
            resources=["*"]))
        failover_worker_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticloadbalancing:CreateRule",
                "elasticloadbalancing:ModifyRule",
                "elasticloadbalancing:CreateTargetGroup",
                "elasticloadbalancing:RegisterTargets",
            ],
            resources=_elb_arns,
        ))
        for _s in ssm_policies:
            failover_worker_fn.add_to_role_policy(_s)
        # SQS event source: batch_size=1 (one tenant per invocation), capped
        # concurrency so parallel launch-vm.sh runs don't overwhelm a target.
        failover_worker_fn.add_event_source(lambda_event_sources.SqsEventSource(
            failover_queue, batch_size=1, max_concurrency=10))
        # Detector sends jobs; wire the URL so the queue path activates.
        health_fn.add_environment("FAILOVER_QUEUE_URL", failover_queue.queue_url)
        failover_queue.grant_send_messages(health_fn)

        self.failover_dlq = failover_dlq
        self.failover_queue = failover_queue
        self.failover_worker_fn = failover_worker_fn

    def _build_skills_lambda(self):
        tenants_table = self.tenants_table
        groups_table = self.groups_table
        assets_bucket = self.assets_bucket
        api = self.api
        key_required = self.key_required
        _li = self._li

        # ========== Skills Lambda ==========
        skills_fn = _lambda.Function(self, "Skills",
            function_name="openclaw-skills",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(_stage_lambda_asset("skills")),
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
        skills_resource.add_method("GET", apigw.LambdaIntegration(skills_fn), **key_required)
        # 1.4.1 (#63) — per-skill CRUD goes through api Lambda (reuses RBAC + audit log)
        skill_resource = skills_resource.add_resource("{name}")
        skill_resource.add_method("GET", _li(), **key_required)
        skill_resource.add_method("PUT", _li(), **key_required)
        skill_resource.add_method("DELETE", _li(), **key_required)

    def _build_templates_lambda(self):
        assets_bucket = self.assets_bucket
        api = self.api
        key_required = self.key_required

        # ========== Templates Lambda ==========
        templates_fn = _lambda.Function(self, "Templates",
            function_name="openclaw-templates",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(_stage_lambda_asset("templates")),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={"ASSETS_BUCKET": assets_bucket.bucket_name},
        )
        assets_bucket.grant_read_write(templates_fn)
        templates_resource = api.root.add_resource("templates")
        templates_resource.add_method("GET", apigw.LambdaIntegration(templates_fn), **key_required)
        template_item = templates_resource.add_resource("{name}")
        template_item.add_method("GET", apigw.LambdaIntegration(templates_fn), **key_required)
        template_item.add_method("PUT", apigw.LambdaIntegration(templates_fn), **key_required)
        template_item.add_method("DELETE", apigw.LambdaIntegration(templates_fn), **key_required)

    def _build_scaler_lambda(self):
        tenants_table = self.tenants_table
        hosts_table = self.hosts_table
        audit_table = self.audit_table
        audit_retention_days = self.audit_retention_days
        ssm_policies = self.ssm_policies
        events_dlq = self.events_dlq
        _asg_arn = self._asg_arn

        # ========== Scaler Lambda (idle host reclaim) ==========
        scaler_fn = _lambda.Function(self, "Scaler",
            function_name="openclaw-scaler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(_stage_lambda_asset("scaler")),
            timeout=Duration.seconds(60),
            memory_size=128,
            environment={
                "HOSTS_TABLE": hosts_table.table_name,
                "TENANTS_TABLE": tenants_table.table_name,
                "ASG_NAME": "openclaw-hosts-asg",
                "IDLE_TIMEOUT_MINUTES": str(CFG["scaler"]["idle_timeout_minutes"]),
                # Issue #71 — scaler audits its automated actions (TTL expiry,
                # scheduled office-hours stop/start, idle-host reclaim).
                "AUDIT_TABLE": audit_table.table_name,
                "AUDIT_TTL_DAYS": str(audit_retention_days),
            },
        )
        hosts_table.grant_read_write_data(scaler_fn)
        # Issue #15 — TTL processing reads tenants and updates status (stop/delete)
        tenants_table.grant_read_write_data(scaler_fn)
        # Issue #71 — scaler writes audit rows for its automated actions.
        audit_table.grant_write_data(scaler_fn)
        for _s in ssm_policies:  # SSM stop-vm.sh on TTL expiry
            scaler_fn.add_to_role_policy(_s)
        scaler_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["autoscaling:DescribeAutoScalingGroups"], resources=["*"]))
        scaler_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["autoscaling:TerminateInstanceInAutoScalingGroup"],
            resources=[_asg_arn],
        ))
        events.Rule(self, "ScalerSchedule",
            schedule=events.Schedule.rate(Duration.minutes(CFG["scaler"]["interval_minutes"])),
            targets=[targets.LambdaFunction(scaler_fn,
                dead_letter_queue=events_dlq, retry_attempts=2)],
        )

        self.scaler_fn = scaler_fn

    def _build_backup_lambda(self):
        tenants_table = self.tenants_table
        assets_bucket = self.assets_bucket
        ssm_policies = self.ssm_policies
        events_dlq = self.events_dlq
        api_fn = self.api_fn

        # ========== Backup Lambda (daily data backup) ==========
        backup_fn = _lambda.Function(self, "Backup",
            function_name="openclaw-backup",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(_stage_lambda_asset("backup")),
            timeout=Duration.seconds(900),
            memory_size=256,
            environment={
                "TENANTS_TABLE": tenants_table.table_name,
                "ASSETS_BUCKET": assets_bucket.bucket_name,
                "BACKUP_PREFIX": CFG["s3"]["backup_prefix"],
            },
        )
        tenants_table.grant_read_write_data(backup_fn)
        assets_bucket.grant_read_write(backup_fn)
        for _s in ssm_policies:
            backup_fn.add_to_role_policy(_s)
        backup_fn.grant_invoke(api_fn)  # API Lambda async invokes Backup Lambda

        events.Rule(self, "BackupSchedule",
            schedule=events.Schedule.expression(CFG["s3"]["backup_cron"]),
            targets=[targets.LambdaFunction(backup_fn,
                dead_letter_queue=events_dlq, retry_attempts=2)],
        )

        self.backup_fn = backup_fn

    def _build_alarms(self):
        notifications_topic = self.notifications_topic
        events_dlq = self.events_dlq
        api_fn = self.api_fn
        health_fn = self.health_fn
        scaler_fn = self.scaler_fn
        backup_fn = self.backup_fn
        failover_worker_fn = self.failover_worker_fn
        failover_dlq = self.failover_dlq

        # ===== CloudWatch alarms (T2-3) =====
        # There were ZERO alarms — every failure mode was log-only. Alarm on
        # errors>0 and throttles>0 for each control-plane Lambda, routed to the
        # lifecycle SNS topic when the operator enabled it, else a dedicated
        # alarms topic so an alarm always has somewhere to fire.
        alarms_topic = notifications_topic or sns.Topic(self, "AlarmsTopic",
            topic_name="openclaw-alarms",
            display_name="OpenClaw Lambda Alarms",
        )
        _sns_action = cw_actions.SnsAction(alarms_topic)
        for _fn, _label in ((api_fn, "Api"), (health_fn, "Health"),
                            (scaler_fn, "Scaler"), (backup_fn, "Backup"),
                            (failover_worker_fn, "FailoverWorker")):
            err = cloudwatch.Alarm(self, f"{_label}ErrorsAlarm",
                alarm_name=f"openclaw-{_label.lower()}-errors",
                metric=_fn.metric_errors(period=Duration.minutes(5)),
                threshold=1, evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            err.add_alarm_action(_sns_action)
            thr = cloudwatch.Alarm(self, f"{_label}ThrottlesAlarm",
                alarm_name=f"openclaw-{_label.lower()}-throttles",
                metric=_fn.metric_throttles(period=Duration.minutes(5)),
                threshold=1, evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            thr.add_alarm_action(_sns_action)
        # Alert when EventBridge parks an undeliverable event in the DLQ.
        dlq_alarm = cloudwatch.Alarm(self, "EventsDlqAlarm",
            alarm_name="openclaw-events-dlq-not-empty",
            metric=events_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)),
            threshold=1, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        dlq_alarm.add_alarm_action(_sns_action)
        # T3-2: alert when a failover job exhausts its retries and lands in the
        # failover DLQ — a tenant that could not be recovered needs a human.
        failover_dlq_alarm = cloudwatch.Alarm(self, "FailoverDlqAlarm",
            alarm_name="openclaw-failover-dlq-not-empty",
            metric=failover_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)),
            threshold=1, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        failover_dlq_alarm.add_alarm_action(_sns_action)

    def _build_host_role(self):
        tenants_table = self.tenants_table
        hosts_table = self.hosts_table
        assets_bucket = self.assets_bucket
        _asg_arn = self._asg_arn

        # ========== Host EC2 Role (SSM + S3 backup + self-register) ==========
        host_role = iam.Role(self, "HostRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        assets_bucket.grant_read_write(host_role)
        hosts_table.grant_read_write_data(host_role)
        tenants_table.grant_read_write_data(host_role)  # host-agent writes health status
        # T2-5: CompleteLifecycleAction is scopable to the ASG ARN.
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["autoscaling:CompleteLifecycleAction"],
            resources=[_asg_arn],
        ))
        # DescribeVolumes has no resource-level support (stays *); CreateTags is
        # scoped to the account's volumes + instances (host tags its own EBS).
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:DescribeVolumes"], resources=["*"]))
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:CreateTags"],
            resources=[
                Fn.join("", ["arn:", cdk.Aws.PARTITION, ":ec2:", cdk.Aws.REGION,
                    ":", cdk.Aws.ACCOUNT_ID, ":volume/*"]),
                Fn.join("", ["arn:", cdk.Aws.PARTITION, ":ec2:", cdk.Aws.REGION,
                    ":", cdk.Aws.ACCOUNT_ID, ":instance/*"]),
            ],
        ))
        # host-agent only reads its own stack outputs → scope to this stack ARN.
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks"],
            resources=[cdk.Aws.STACK_ID],
        ))

        self.host_role = host_role

    def _build_prometheus_grafana(self):
        host_role = self.host_role
        api_fn = self.api_fn

        # ========== Amazon Managed Prometheus + Grafana (issue #4) ==========
        # Host-agent exposes /metrics on :8899 (same listener as /health);
        # an ADOT collector on each host remote-writes to AMP using SigV4.
        # AMG reads from AMP for dashboards. (NOTE: 1.2.5 fixed a wiring
        # bug where ADOT was scraping :9090 — host-agent never bound 9090.)
        # Cost knob: metrics.enabled: false in config.yml skips both workspaces
        # (AMP is billed per sample/GB, AMG per active user).
        metrics_cfg = CFG.get("metrics", {})
        amp_remote_write_url = "none"
        if metrics_cfg.get("enabled", False):
            amp_workspace = aps.CfnWorkspace(self, "AmpWorkspace",
                alias=metrics_cfg.get("workspace_alias", "openclaw"),
            )
            # Host EC2 role can remote-write to this workspace.
            host_role.add_to_policy(iam.PolicyStatement(
                actions=["aps:RemoteWrite", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"],
                resources=[amp_workspace.attr_arn],
            ))
            # AMG service role with read access to AMP.
            grafana_role = iam.Role(self, "GrafanaServiceRole",
                assumed_by=iam.ServicePrincipal("grafana.amazonaws.com"),
            )
            grafana_role.add_to_policy(iam.PolicyStatement(
                actions=["aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"],
                resources=[amp_workspace.attr_arn],
            ))
            # AMG workspace itself. AWS_SSO is required when not using SAML.
            amg_workspace = grafana.CfnWorkspace(self, "GrafanaWorkspace",
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

        instance_profile = iam.CfnInstanceProfile(self, "HostInstanceProfile",
            roles=[host_role.role_name],
            instance_profile_name="openclaw-host-profile",
        )

        self.amp_remote_write_url = amp_remote_write_url
        self.instance_profile = instance_profile

    def _build_asg(self):
        host_role = self.host_role

        # ========== ASG (P1-4) ==========
        ac_cfg = CFG.get("agentcore", {})
        ac_enabled = ac_cfg.get("enabled", False)
        gateway_url = ""
        ac_gateway = None

        # Create AgentCore Gateway early (needed for userdata placeholder)
        if ac_enabled and ac_cfg.get("gateway", {}).get("enabled", True):
            ac_gateway = agentcore.Gateway(self, "AgentCoreGateway",
                gateway_name="openclaw-gateway",
                description="OpenClaw Agent tool gateway",
            )
            gateway_url = ac_gateway.gateway_url
            ac_gateway.grant_invoke(host_role)

        # import an existing (enterprise-managed) VPC when network.vpc_id is set;
        # otherwise fall back to the account default VPC (zero-config path).
        _net_cfg = CFG.get("network", {}) or {}
        _vpc_id = (_net_cfg.get("vpc_id") or "").strip()
        if _vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=_vpc_id)
        else:
            vpc = ec2.Vpc.from_lookup(self, "Vpc", is_default=True)

        # restrict to explicit subnets via SubnetFilter.by_ids (lazy, resolves
        # after context lookup); else fall back to public/private.
        _subnet_ids = list(_net_cfg.get("subnet_ids") or [])

        def _subnet_selection():
            if _subnet_ids:
                return ec2.SubnetSelection(subnet_filters=[ec2.SubnetFilter.by_ids(_subnet_ids)])
            if vpc.public_subnets:
                return ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
            return ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)

        self.ac_cfg = ac_cfg
        self.ac_enabled = ac_enabled
        self.gateway_url = gateway_url
        self.ac_gateway = ac_gateway
        self.vpc = vpc
        self._subnet_selection = _subnet_selection

    def _build_multi_az(self):
        assets_bucket = self.assets_bucket
        events_dlq = self.events_dlq
        api_fn = self.api_fn
        host_role = self.host_role
        amp_remote_write_url = self.amp_remote_write_url
        gateway_url = self.gateway_url
        vpc = self.vpc
        _subnet_selection = self._subnet_selection

        # ========== Multi-AZ HA (issue #8) ==========
        # `_az_count` controls how many AZs the ASG and ALB span. Default is
        # single-AZ to minimize cross-AZ data-transfer charges; opt in via
        # config.yml `multi_az.enabled: true`.
        _multi_az = CFG.get("multi_az", {}) or {}
        _az_count = int(_multi_az.get("az_count", 2)) if _multi_az.get("enabled", False) else 1

        sg = ec2.SecurityGroup(self, "HostSG",
            vpc=vpc, security_group_name="openclaw-host-sg",
            allow_all_outbound=True,
        )

        # Compute allocatable resources from instance type. Fallback to the
        # arch-aware default if config.yml omits instance_type (issue #19).
        _arch_default = "m8g.xlarge" if (CFG.get("host", {}) or {}).get("arch") == "arm64" else "m8i.xlarge"
        _itype = (CFG.get("host", {}) or {}).get("instance_type") or _arch_default
        _sizes = {"medium":1,"large":2,"xlarge":4,"2xlarge":8,"4xlarge":16,"8xlarge":32,"12xlarge":48,"16xlarge":64,"24xlarge":96}
        _mem_ratio = {"c":2048,"m":4096,"r":8192}
        _vcpu_total = _sizes[_itype.split(".")[1]]
        _mem_total = _vcpu_total * _mem_ratio[_itype.split(".")[0][0]]
        _avail_vcpu = _vcpu_total - CFG["host"]["reserved_vcpu"]
        _avail_mem = _mem_total - CFG["host"]["reserved_mem_mb"]

        # Load scripts from userdata/ and inject config
        ud_dir = Path(__file__).parent / "userdata"

        init_sh = (ud_dir / "init-host.sh").read_text()
        init_sh = init_sh.replace("{{ROOTFS_PREFIX}}", CFG["s3"]["rootfs_prefix"])
        init_sh = init_sh.replace("{{AVAIL_VCPU}}", str(_avail_vcpu))
        init_sh = init_sh.replace("{{AVAIL_MEM}}", str(_avail_mem))
        init_sh = init_sh.replace("{{SUBNET_PREFIX}}", CFG["vm"]["subnet_prefix"])
        init_sh = init_sh.replace("{{ROOTFS_OVERLAY_MB}}", str(CFG["vm"].get("rootfs_overlay_mb", 8192)))
        init_sh = init_sh.replace("{{AGENTCORE_GATEWAY_URL}}", gateway_url if gateway_url else "none")
        init_sh = init_sh.replace("{{AMP_REMOTE_WRITE_URL}}", amp_remote_write_url)
        # Balloon config
        balloon_cfg = CFG.get("balloon", {})
        init_sh = init_sh.replace("{{BALLOON_ENABLED}}", str(balloon_cfg.get("enabled", False)).lower())
        init_sh = init_sh.replace("{{BALLOON_DEFLATE_ON_OOM}}", str(balloon_cfg.get("deflate_on_oom", True)).lower())
        init_sh = init_sh.replace("{{BALLOON_STATS_INTERVAL}}", str(balloon_cfg.get("stats_polling_interval_s", 5)))
        init_sh = init_sh.replace("{{BALLOON_FREE_PAGE_REPORTING}}", str(balloon_cfg.get("free_page_reporting", True)).lower())
        init_sh = init_sh.replace("{{BALLOON_MAX_INFLATE_RATIO}}", str(balloon_cfg.get("max_inflate_ratio", 0.4)))
        init_sh = init_sh.replace("{{BALLOON_MIN_GUEST_AVAILABLE_MB}}", str(balloon_cfg.get("min_guest_available_mb", 512)))
        # Large scripts downloaded from S3 (userdata 16KB limit)
        init_sh = init_sh.replace("{{BACKUP_DATA_SCRIPT}}",
            "aws s3 cp s3://{{ASSETS_BUCKET}}/deployment/scripts/backup-data.sh /home/ubuntu/backup-data.sh --region ${REGION}\n"
            "chmod +x /home/ubuntu/backup-data.sh && chown ubuntu:ubuntu /home/ubuntu/backup-data.sh")

        host_agent_svc = (ud_dir / "host-agent.service").read_text()
        init_sh = init_sh.replace("{{HOST_AGENT_SCRIPT}}",
            f"cat > /etc/systemd/system/host-agent.service << 'SVCEOF'\n{host_agent_svc}SVCEOF")

        # MUST be after all script injections (they may contain {{ASSETS_BUCKET}})
        init_sh = init_sh.replace("{{ASSETS_BUCKET}}", "PLACEHOLDER_BUCKET")

        # Split script around PLACEHOLDER_BUCKET, inject actual bucket name via Fn::Join
        parts = init_sh.split("PLACEHOLDER_BUCKET")
        user_data = ec2.UserData.for_linux()
        join_parts = [parts[0]]
        for i in range(1, len(parts)):
            join_parts.append(assets_bucket.bucket_name)
            join_parts.append(parts[i])
        user_data.add_commands(cdk.Fn.join("", join_parts))

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
        _instance_type_str = (CFG.get("host", {}) or {}).get("instance_type") \
            or ("m8g.xlarge" if _arch == "arm64" else "m8i.xlarge")

        launch_template = ec2.LaunchTemplate(self, "HostLT",
            launch_template_name="openclaw-host-lt",
            instance_type=ec2.InstanceType(_instance_type_str),
            machine_image=ami,
            security_group=sg,
            role=host_role,
            user_data=user_data,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(CFG["host"]["root_volume_gb"],
                        volume_type=ec2.EbsDeviceVolumeType.GP3),
                ),
                ec2.BlockDevice(
                    device_name="/dev/sdf",
                    # Encrypt at rest with AWS-managed KMS key.
                    # Tenant data (rootfs overlays, data volumes, backups in transit) live here.
                    volume=ec2.BlockDeviceVolume.ebs(CFG["host"]["data_volume_gb"],
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                        delete_on_termination=not CFG["host"].get("keep_data_volume", False)),
                ),
            ],
        )

        cfn_lt = launch_template.node.default_child

        # ── SECURITY (defense-in-depth for IMDS): require IMDSv2 + hop-limit 1 ──
        # The primary guest→IMDS egress block is the iptables DROP in
        # launch-vm.sh; this hardens the host side. Requiring session tokens
        # (HttpTokens=required) kills IMDSv1 credential theft via simple SSRF,
        # and HttpPutResponseHopLimit=1 stops a process one network hop away
        # from obtaining a token. host-agent.py / the AWS SDK use the IMDSv2
        # flow, so this is transparent to legitimate callers.
        cfn_lt.add_property_override("LaunchTemplateData.MetadataOptions", {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
            "HttpEndpoint": "enabled",
        })

        if CFG["asg"].get("use_spot"):
            cfn_lt.add_property_override("LaunchTemplateData.InstanceMarketOptions", {
                "MarketType": "spot",
                "SpotOptions": {"SpotInstanceType": "one-time"},
            })

        # Enable nested virtualization via CustomResource (CFN doesn't support CpuOptions.NestedVirtualization)
        create_ver_call = cr.AwsSdkCall(
            service="EC2",
            action="createLaunchTemplateVersion",
            parameters={
                "LaunchTemplateId": launch_template.launch_template_id,
                "SourceVersion": "$Latest",
                "LaunchTemplateData": {
                    "CpuOptions": {"NestedVirtualization": "enabled"},
                    # Carry the IMDSv2/hop-limit hardening into the nested-virt
                    # version too. CreateLaunchTemplateVersion merges onto
                    # SourceVersion=$Latest so this would normally be inherited,
                    # but we restate it so the security posture is explicit and
                    # cannot silently regress if the base override is removed.
                    "MetadataOptions": {
                        "HttpTokens": "required",
                        "HttpPutResponseHopLimit": 1,
                        "HttpEndpoint": "enabled",
                    },
                },
            },
            physical_resource_id=cr.PhysicalResourceId.of(
                Fn.join("-", ["nested-virt", cfn_lt.ref, Fn.get_att(cfn_lt.logical_id, "LatestVersionNumber").to_string()])
            ),
            output_paths=["LaunchTemplateVersion.VersionNumber"],
        )
        nested_virt = cr.AwsCustomResource(self, "NestedVirt",
            on_create=create_ver_call,
            on_update=create_ver_call,
            install_latest_aws_sdk=True,
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=["ec2:CreateLaunchTemplateVersion", "ec2:DescribeLaunchTemplateVersions"],
                    resources=["*"],
                ),
            ]),
        )
        nested_virt.node.add_dependency(launch_template)

        set_default = cr.AwsCustomResource(self, "SetDefaultLTVersion",
            on_create=cr.AwsSdkCall(
                service="EC2", action="modifyLaunchTemplate",
                parameters={
                    "LaunchTemplateId": launch_template.launch_template_id,
                    "DefaultVersion": nested_virt.get_response_field("LaunchTemplateVersion.VersionNumber"),
                },
                physical_resource_id=cr.PhysicalResourceId.of("set-default-lt"),
            ),
            on_update=cr.AwsSdkCall(
                service="EC2", action="modifyLaunchTemplate",
                parameters={
                    "LaunchTemplateId": launch_template.launch_template_id,
                    "DefaultVersion": nested_virt.get_response_field("LaunchTemplateVersion.VersionNumber"),
                },
                physical_resource_id=cr.PhysicalResourceId.of("set-default-lt"),
            ),
            install_latest_aws_sdk=False,
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(actions=["ec2:ModifyLaunchTemplate"], resources=["*"]),
            ]),
        )
        set_default.node.add_dependency(nested_virt)

        asg = autoscaling.AutoScalingGroup(self, "HostASG",
            auto_scaling_group_name="openclaw-hosts-asg",
            vpc=vpc,
            vpc_subnets=_subnet_selection(),
            launch_template=launch_template,
            min_capacity=CFG["asg"]["min_capacity"],
            max_capacity=CFG["asg"]["max_capacity"],
        )
        asg.node.add_dependency(set_default)
        cfn_asg = asg.node.default_child
        cfn_asg.add_property_override("LaunchTemplate.Version",
            nested_virt.get_response_field("LaunchTemplateVersion.VersionNumber"))
        # Lifecycle hooks (standalone resources, not inline LifecycleHookSpecificationList)
        autoscaling.CfnLifecycleHook(self, "InitHook",
            auto_scaling_group_name=asg.auto_scaling_group_name,
            lifecycle_hook_name="openclaw-host-init",
            lifecycle_transition="autoscaling:EC2_INSTANCE_LAUNCHING",
            heartbeat_timeout=CFG["asg"]["lifecycle_hook_timeout"],
            default_result="ABANDON",
        )
        autoscaling.CfnLifecycleHook(self, "TerminateHook",
            auto_scaling_group_name=asg.auto_scaling_group_name,
            lifecycle_hook_name="openclaw-host-terminate",
            lifecycle_transition="autoscaling:EC2_INSTANCE_TERMINATING",
            heartbeat_timeout=120,
            default_result="CONTINUE",
        )

        # When a new host completes init → process pending tenants
        events.Rule(self, "HostReadyRule",
            event_pattern=events.EventPattern(
                source=["aws.autoscaling"],
                detail_type=["EC2 Instance Launch Successful"],
            ),
            targets=[targets.LambdaFunction(api_fn,
                dead_letter_queue=events_dlq, retry_attempts=2)],
        )

        # When a host is terminating → cleanup DynamoDB records
        events.Rule(self, "HostTerminateRule",
            event_pattern=events.EventPattern(
                source=["aws.autoscaling"],
                detail_type=["EC2 Instance-terminate Lifecycle Action"],
            ),
            targets=[targets.LambdaFunction(api_fn,
                dead_letter_queue=events_dlq, retry_attempts=2)],
        )

        self.sg = sg

    def _build_agentcore(self):
        api_fn = self.api_fn
        ac_cfg = self.ac_cfg
        ac_enabled = self.ac_enabled
        gateway_url = self.gateway_url
        ac_gateway = self.ac_gateway

        # ========== AgentCore (optional, continued) ==========
        if ac_enabled:
            # Gateway already created above (before userdata processing)

            # Register Lambda tools on Gateway
            if ac_gateway and ac_cfg.get("gateway", {}).get("enabled", True):
                tools_fn = _lambda.Function(self, "AgentCoreTools",
                    function_name="openclaw-agentcore-tools",
                    runtime=_lambda.Runtime.PYTHON_3_12,
                    handler="handler.lambda_handler",
                    code=_lambda.Code.from_asset(_stage_lambda_asset("agentcore_tools")),
                    timeout=Duration.seconds(30),
                    memory_size=128,
                )
                ac_gateway.add_lambda_target("tools",
                    lambda_function=tools_fn,
                    tool_schema=agentcore.ToolSchema.from_inline([
                        agentcore.ToolDefinition(
                            name="hello",
                            description="Say hello — test tool for verifying AgentCore Gateway connectivity",
                            input_schema=agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.OBJECT,
                                properties={"name": agentcore.SchemaDefinition(
                                    type=agentcore.SchemaDefinitionType.STRING,
                                    description="Name to greet",
                                )},
                            ),
                        ),
                        agentcore.ToolDefinition(
                            name="system_info",
                            description="Get Lambda runtime system information",
                            input_schema=agentcore.SchemaDefinition(type=agentcore.SchemaDefinitionType.OBJECT),
                        ),
                        agentcore.ToolDefinition(
                            name="timestamp",
                            description="Get current UTC timestamp",
                            input_schema=agentcore.SchemaDefinition(
                                type=agentcore.SchemaDefinitionType.OBJECT,
                                properties={"format": agentcore.SchemaDefinition(
                                    type=agentcore.SchemaDefinitionType.STRING,
                                    description="iso or unix",
                                )},
                            ),
                        ),
                    ]),
                    gateway_target_name="openclaw-tools",
                )

            # Memory — persistent cross-session memory
            if ac_cfg.get("memory", {}).get("enabled", True):
                strategies = []
                for s in ac_cfg.get("memory", {}).get("strategies", ["semantic"]):
                    if s == "semantic":
                        strategies.append(agentcore.MemoryStrategy.using_semantic(
                            name="openclaw_semantic",
                            namespaces=["/openclaw/tenant/{actorId}/semantic"],
                        ))
                    elif s == "user_preference":
                        strategies.append(agentcore.MemoryStrategy.using_user_preference(
                            name="openclaw_preferences",
                            namespaces=["/openclaw/tenant/{actorId}/preferences"],
                        ))
                agentcore.Memory(self, "AgentCoreMemory",
                    memory_name="openclaw_memory",
                    description="OpenClaw per-tenant memory",
                    expiration_duration=Duration.days(ac_cfg.get("memory", {}).get("expiration_days", 90)),
                    memory_strategies=strategies,
                )

            # Code Interpreter — secure sandboxed Python execution
            if ac_cfg.get("code_interpreter", {}).get("enabled", True):
                agentcore.CodeInterpreterCustom(self, "AgentCoreCodeInterpreter",
                    code_interpreter_custom_name="openclaw_code_interpreter",
                )

            # Browser — cloud-based web automation
            if ac_cfg.get("browser", {}).get("enabled", True):
                agentcore.BrowserCustom(self, "AgentCoreBrowser",
                    browser_custom_name="openclaw_browser",
                )

            # Identity — workload identity for agent AWS access
            agentcore_l1.CfnWorkloadIdentity(self, "AgentCoreIdentity",
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


    def _build_alb(self):
        api_fn = self.api_fn
        health_fn = self.health_fn
        failover_worker_fn = self.failover_worker_fn
        _elb_arns = self._elb_arns
        vpc = self.vpc
        _subnet_selection = self._subnet_selection
        sg = self.sg

        # ========== ALB (Dashboard Proxy) ==========
        # ALB requires ≥2 subnets in different AZs (AWS API constraint).
        # The multi_az.az_count knob controls ASG fan-out; ALB independently
        # always claims max(2, az_count) subnets so single-AZ ASG mode still
        # produces a valid load balancer.
        # ALB needs subnets in ≥2 AZs (AWS API constraint). When network.subnet_ids
        # is given the operator must ensure they span ≥2 AZs — AWS rejects the ALB
        # at deploy otherwise. With no explicit list, the VPC's public/private
        # subnets already span the account's AZs.
        alb = elbv2.ApplicationLoadBalancer(self, "DashboardALB",
            load_balancer_name="openclaw-dashboard",
            vpc=vpc,
            vpc_subnets=_subnet_selection(),
            internet_facing=True,
        )
        listener = alb.add_listener("HTTP", port=80,
            default_action=elbv2.ListenerAction.fixed_response(404, content_type="text/plain", message_body="not found"),
        )
        alb.connections.allow_to(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(80), "ALB to host Nginx")
        sg.add_ingress_rule(ec2.Peer.security_group_id(alb.connections.security_groups[0].security_group_id),
            ec2.Port.tcp(80), "ALB to Nginx")
        sg.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(80), "VPC to Nginx (ALB IP target health check)")

        # Pass ALB info to API Lambda for path-based routing
        api_fn.add_environment("ALB_LISTENER_ARN", listener.listener_arn)
        api_fn.add_environment("VPC_ID", vpc.vpc_id)
        # 1.3.1: health_check Lambda needs ALB listener for AZ failover
        # to repoint /vm/<tenant_id>* rules across hosts.
        health_fn.add_environment("ALB_LISTENER_ARN", listener.listener_arn)
        # T3-3: give the failover placer the VPC_ID env so it creates host
        # target groups in the right VPC instead of cloning VpcId from an
        # arbitrary existing TG. The worker runs the same repoint path.
        health_fn.add_environment("VPC_ID", vpc.vpc_id)
        failover_worker_fn.add_environment("VPC_ID", vpc.vpc_id)
        # 1.4.2 (#fake-failover fix): the failover gate verifies the
        # tenant's dashboard URL is genuinely reachable through the public
        # path (ALB → nginx → VM) before flipping DDB to running. We use
        # the ALB DNS directly because (a) it's already public, (b) it
        # bypasses the CloudFront cache, (c) no extra DNS hop. The probe
        # hits http://<alb_dns>/vm/<tenant_id>/ — same path CloudFront
        # would forward to anyway.
        health_fn.add_environment("PUBLIC_BASE_URL", f"http://{alb.load_balancer_dns_name}")
        # T3-2: the failover worker runs the same repoint + verify gates, so it
        # needs the same ALB env as the detector.
        failover_worker_fn.add_environment("ALB_LISTENER_ARN", listener.listener_arn)
        failover_worker_fn.add_environment("PUBLIC_BASE_URL", f"http://{alb.load_balancer_dns_name}")
        # T2-5: Describe* stays * (no resource-level support); the create/delete/
        # modify/register actions scope to this account+region's ELB ARN space.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["elasticloadbalancing:DescribeRules",
                     "elasticloadbalancing:DescribeTargetGroups",
                     "elasticloadbalancing:DescribeListeners"],
            resources=["*"]))
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticloadbalancing:CreateTargetGroup", "elasticloadbalancing:DeleteTargetGroup",
                "elasticloadbalancing:RegisterTargets", "elasticloadbalancing:DeregisterTargets",
                "elasticloadbalancing:CreateRule", "elasticloadbalancing:DeleteRule",
                "elasticloadbalancing:ModifyRule",
            ],
            resources=_elb_arns,
        ))

        self.alb = alb

    def _build_cloudfront(self):
        assets_bucket = self.assets_bucket
        alb = self.alb

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
        cf_cfg = CFG.get("cloudfront", {}) or {}
        # ----- DUAL mode candidates -----
        console_domain = (cf_cfg.get("console_domain") or "").strip()
        console_cert_arn = (cf_cfg.get("console_cert_arn") or "").strip()
        app_domain = (cf_cfg.get("app_domain") or "").strip()
        app_cert_arn = (cf_cfg.get("app_cert_arn") or "").strip()
        dual_mode = bool(console_domain and console_cert_arn and app_domain and app_cert_arn)
        # ----- LEGACY single-domain fallback -----
        custom_domain = (cf_cfg.get("custom_domain") or "").strip()
        acm_cert_arn = (cf_cfg.get("acm_cert_arn") or "").strip()
        # #79: bring-your-own CDN — skip CloudFront entirely.
        cf_enabled = cf_cfg.get("enabled", True)

        # S3 origin + url-rewrite Function only exist to back a distribution.
        if cf_enabled:
            s3_origin = origins.S3BucketOrigin.with_origin_access_control(assets_bucket)
            url_rewrite_fn = cloudfront.Function(self, "UrlRewrite",
                function_name="openclaw-url-rewrite",
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

        if not cf_enabled:
            # No CloudFront: expose ALB DNS (dashboards) + S3 REST endpoint
            # (console) for a customer-owned CDN. Bucket stays private — the CDN
            # origin-accesses it; we don't open it publicly.
            # Domain/cert are CloudFront-only; warn + ignore if set here.
            if any([console_domain, console_cert_arn, app_domain, app_cert_arn,
                    custom_domain, acm_cert_arn]):
                print("⚠️  config.yml: cloudfront.enabled=false — the console_domain / "
                      "app_domain / custom_domain / *_cert_arn values are ignored "
                      "(they only apply to a CloudFront distribution). Point your own "
                      "CDN at the ALB DNS + S3 endpoint outputs instead.")
            dual_mode = False
            console_host = assets_bucket.bucket_regional_domain_name
            dashboard_host = alb.load_balancer_dns_name
            console_cf_id = ""
            app_cf_id = ""
            cf_distribution = None
        elif dual_mode:
            # ===== DUAL mode: two distributions, two certs, two aliases =====
            # Distribution A: console — S3 origin only, /console/* + redirect / → /console/
            console_cf = cloudfront.Distribution(self, "ConsoleCF",
                comment="OpenClaw Operator Console",
                domain_names=[console_domain],
                certificate=acm.Certificate.from_certificate_arn(
                    self, "ConsoleCert", console_cert_arn),
                default_behavior=cloudfront.BehaviorOptions(
                    origin=s3_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    # T2-2: the console is static S3 assets — cache them at the
                    # edge (was CACHING_DISABLED = 100% origin fetches). The
                    # per-tenant dashboards (AppCF, /vm/*) stay uncached below.
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                    function_associations=[cloudfront.FunctionAssociation(
                        function=url_rewrite_fn,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )],
                ),
                default_root_object="",
            )
            # Distribution B: per-tenant dashboards — ALB origin only, /vm/*
            app_cf = cloudfront.Distribution(self, "AppCF",
                comment="OpenClaw Per-Tenant Dashboards",
                domain_names=[app_domain],
                certificate=acm.Certificate.from_certificate_arn(
                    self, "AppCert", app_cert_arn),
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(alb.load_balancer_dns_name,
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
                if custom_domain and acm_cert_arn else None
            )

            cf_distribution = cloudfront.Distribution(self, "DashboardCF",
                comment="OpenClaw Dashboard (single-domain mode)",
                domain_names=domain_names,
                certificate=certificate,
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(alb.load_balancer_dns_name,
                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                        http_port=80,
                        read_timeout=Duration.seconds(60),
                        keepalive_timeout=Duration.seconds(60),
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                    function_associations=[cloudfront.FunctionAssociation(
                        function=url_rewrite_fn,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )],
                ),
                additional_behaviors={
                    "/console/*": cloudfront.BehaviorOptions(
                        origin=s3_origin,
                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                        # T2-2: static console assets cached at the edge. The
                        # default behavior (ALB origin, incl. /vm/*) stays
                        # CACHING_DISABLED so tenant dashboards are never cached.
                        cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                        function_associations=[cloudfront.FunctionAssociation(
                            function=url_rewrite_fn,
                            event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                        )],
                    ),
                },
                default_root_object="",
            )

            # Single mode: console and dashboard share the same host
            console_host = custom_domain or cf_distribution.distribution_domain_name
            dashboard_host = console_host
            console_cf_id = cf_distribution.distribution_id
            app_cf_id = cf_distribution.distribution_id

        self.dual_mode = dual_mode
        self.custom_domain = custom_domain
        self.console_host = console_host
        self.dashboard_host = dashboard_host
        self.console_cf_id = console_cf_id
        self.app_cf_id = app_cf_id
        self.cf_distribution = cf_distribution

    def _build_console_auth(self):
        api_fn = self.api_fn
        dual_mode = self.dual_mode
        custom_domain = self.custom_domain
        console_host = self.console_host
        cf_distribution = self.cf_distribution

        # ========== Console Auth (Cognito) ==========
        auth_cfg = CFG.get("console_auth", {})
        cognito_outputs = {}
        if auth_cfg.get("enabled", False):
            existing_pool_id = auth_cfg.get("user_pool_id", "")

            # 1.3.4: callback URLs only target the console host (where the
            # operator actually logs in). In dual-mode, app_domain is NOT
            # listed here — the Cognito session cookie is therefore physically
            # scoped to console_domain and cannot be sent to per-tenant
            # dashboards on app_domain.
            callback_urls = [f"https://{console_host}/console/index.html"]
            # In legacy single-mode, also add the *.cloudfront.net default
            # so direct CF URL access still works during DNS migration.
            # Legacy single-mode with a custom domain: also allow the raw
            # *.cloudfront.net default so direct CF access works during DNS
            # migration. Skipped when there's no distribution (#79 no-CloudFront).
            if not dual_mode and custom_domain and cf_distribution is not None:
                callback_urls.append(
                    f"https://{cf_distribution.distribution_domain_name}/console/index.html")

            if existing_pool_id:
                # Import the existing pool but recreate the domain + client as stack-owned resources.
                user_pool = cognito.UserPool.from_user_pool_id(self, "ConsoleUserPool", existing_pool_id)
                cognito_outputs["CognitoUserPoolId"] = existing_pool_id

                # Legacy prefix (no account suffix) matches what 1.1.x created,
                # so existing users' bookmarked Cognito URLs keep working.
                domain_prefix = "openclaw-console"
                cognito.CfnUserPoolDomain(self, "ConsoleDomain",
                    user_pool_id=existing_pool_id,
                    domain=domain_prefix,
                )
                cfn_client = cognito.CfnUserPoolClient(self, "ConsoleClient",
                    user_pool_id=existing_pool_id,
                    generate_secret=False,
                    callback_ur_ls=callback_urls,
                    logout_ur_ls=callback_urls,
                    supported_identity_providers=["COGNITO"],
                    allowed_o_auth_flows=["implicit"],
                    allowed_o_auth_scopes=["openid", "email"],
                    allowed_o_auth_flows_user_pool_client=True,
                    explicit_auth_flows=[
                        "ALLOW_USER_PASSWORD_AUTH",
                        "ALLOW_USER_SRP_AUTH",
                        "ALLOW_REFRESH_TOKEN_AUTH",
                    ],
                )
                cognito_outputs["CognitoClientId"] = cfn_client.ref
                cognito_outputs["CognitoDomain"] = f"{domain_prefix}.auth.{self.region}.amazoncognito.com"
            else:
                user_pool = cognito.UserPool(self, "ConsoleUserPool",
                    user_pool_name="openclaw-console",
                    self_sign_up_enabled=auth_cfg.get("self_sign_up", False),
                    sign_in_aliases=cognito.SignInAliases(email=True),
                    password_policy=cognito.PasswordPolicy(
                        min_length=8, require_digits=True, require_lowercase=True,
                    ),
                    removal_policy=RemovalPolicy.RETAIN,
                )
                user_pool.add_domain("ConsoleDomain",
                    cognito_domain=cognito.CognitoDomainOptions(
                        # account_id suffix keeps the domain prefix globally
                        # unique across stacks/accounts and survives RETAIN
                        # cleanup races (the prefix is global, not regional).
                        domain_prefix=f"openclaw-console-{self.account}",
                    ),
                )
                client = user_pool.add_client("ConsoleClient",
                    o_auth=cognito.OAuthSettings(
                        flows=cognito.OAuthFlows(implicit_code_grant=True),
                        scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                        callback_urls=callback_urls,
                        logout_urls=callback_urls,
                    ),
                )
                cognito_outputs["CognitoUserPoolId"] = user_pool.user_pool_id
                cognito_outputs["CognitoClientId"] = client.user_pool_client_id
                cognito_outputs["CognitoDomain"] = f"openclaw-console-{self.account}.auth.{cdk.Stack.of(self).region}.amazoncognito.com"

            # RBAC groups (issue #14): admin / operator / viewer.
            # Created on both new and existing pools so an imported pool also
            # gets the role groups. The handler maps `cognito:groups` claim →
            # role hierarchy (admin > operator > viewer).
            for group_name, description, precedence in (
                ("admin",    "Full access — RBAC + CRUD + actions", 1),
                ("operator", "CRUD + lifecycle actions (no RBAC mgmt)", 2),
                ("viewer",   "Read-only access",                       3),
            ):
                cognito.CfnUserPoolGroup(self, f"Role{group_name.capitalize()}",
                    user_pool_id=user_pool.user_pool_id,
                    group_name=group_name,
                    description=description,
                    precedence=precedence,
                )

            # Fail-safe RBAC (1.5.0): inject the REAL, stack-owned Cognito ids so
            # the api Lambda can fetch JWKS and verify id_token signatures
            # (RS256). These override the construction-time placeholders — the
            # Cognito pool id is only known here, after the pool is created or
            # imported above. Without a genuine pool id the handler cannot
            # verify signatures and every request fails safe to `viewer`.
            api_fn.add_environment("COGNITO_USER_POOL_ID",
                                   cognito_outputs.get("CognitoUserPoolId", ""))
            api_fn.add_environment("COGNITO_CLIENT_ID",
                                   cognito_outputs.get("CognitoClientId", ""))

        self.cognito_outputs = cognito_outputs

    def _build_outputs(self):
        api = self.api
        api_key = self.api_key
        tenants_table = self.tenants_table
        hosts_table = self.hosts_table
        assets_bucket = self.assets_bucket
        instance_profile = self.instance_profile
        notifications_topic_arn = self.notifications_topic_arn
        dual_mode = self.dual_mode
        console_host = self.console_host
        dashboard_host = self.dashboard_host
        console_cf_id = self.console_cf_id
        app_cf_id = self.app_cf_id
        cognito_outputs = self.cognito_outputs

        # ========== Outputs ==========
        for key, val in {
            "ApiUrl": api.url,
            "ApiKeyId": api_key.key_id,
            "TenantsTable": tenants_table.table_name,
            "HostsTable": hosts_table.table_name,
            "AssetsBucket": assets_bucket.bucket_name,
            "HostInstanceProfileArn": instance_profile.attr_arn,
            # 1.3.4: dual-mode outputs.
            #   ConsoleUrl    — operator console (Cognito-protected, S3-served)
            #   DashboardUrl  — per-tenant dashboards (ALB-served, app_domain in dual mode)
            # In legacy single-mode the two URLs are equal and point to the
            # combined CloudFront distribution, preserving backward compat.
            "ConsoleUrl": f"https://{console_host}",
            "DashboardUrl": f"https://{dashboard_host}",
            "DualDomainMode": "true" if dual_mode else "false",
            # #79: omit CF distribution outputs when no CloudFront exists —
            # CfnOutput rejects empty values, and there's nothing to invalidate.
            **({"CloudfrontDistributionId": console_cf_id} if console_cf_id else {}),
            **({"AppCloudfrontDistributionId": app_cf_id} if app_cf_id else {}),
            **({"NotificationsTopicArn": notifications_topic_arn} if notifications_topic_arn else {}),
            **cognito_outputs,
        }.items():
            cdk.CfnOutput(self, key, value=val)
