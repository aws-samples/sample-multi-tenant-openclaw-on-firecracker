# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import (
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_aps as aps,
    aws_grafana as grafana,
    aws_guardduty as guardduty,
    aws_bedrock_agentcore_alpha as agentcore,
    custom_resources as cr,
    Duration,
)


def build_compute(self, ctx):
    """Build compute resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    api_fn = getattr(ctx, "api_fn", None)
    assets_bucket = getattr(ctx, "assets_bucket", None)
    backup_bucket = getattr(ctx, "backup_bucket", None)
    backup_cmk = getattr(ctx, "backup_cmk", None)
    clawpool_cmk = getattr(ctx, "clawpool_cmk", None)
    clawpool_rsa_cmk = getattr(ctx, "clawpool_rsa_cmk", None)
    hosts_table = getattr(ctx, "hosts_table", None)
    notifications_topic = getattr(ctx, "notifications_topic", None)
    tenants_table = getattr(ctx, "tenants_table", None)

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
    # #152/#118 — host decrypts platform-injected credential ciphertext at VM
    # launch. Decrypt only (host never encrypts; upstream does). The
    # EncryptionContext (owner_id) is enforced by the caller in launch-vm.sh;
    # a stricter key-policy condition can be layered later if needed.
    if clawpool_cmk is not None:
        clawpool_cmk.grant_decrypt(host_role)
    # #149 asymmetric-v1 — host decrypts RSA-OAEP env-cred ciphertext at VM launch
    # (cred-inject.sh, no EncryptionContext: KMS asymmetric doesn't support it).
    if clawpool_rsa_cmk is not None:
        clawpool_rsa_cmk.grant_decrypt(host_role)
    hosts_table.grant_read_write_data(host_role)
    tenants_table.grant_read_write_data(host_role)  # host-agent writes health status
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

    # ========== SQS Dispatch (标准队列 + 装箱消费 + 聚合 SSM/pull 二期) ==========
    # config-gated by dispatch.enabled(default false → 零新资源)。所有一期/二期
    # 基础设施(队列/DLQ/ESM/assignments 表/andon param/Poller Rule/DLQ alarm)
    # 集中在 DispatchInfra Construct 里,stack.py 只保留最小侵入的实例化 + env 注入。
    # 双开关守卫在 API Lambda 段前已经走过 validate_no_double_enqueue。
    _dispatch_cfg = CFG.get("dispatch", {}) or {}
    if _dispatch_cfg.get("enabled", False):
        from lib.dispatch_infra import DispatchInfra

        dispatch_infra = DispatchInfra(
            self,
            "Dispatch",
            cfg=_dispatch_cfg,
            api_fn=api_fn,
            host_role=host_role,
        )
        # 契约 env(interfaces.md L6-17)注入 api_fn。lifecycle_consumer 复用同一
        # handler 代码,create_via_queue 迁移期两者共存,一并给到 consumer,避免
        # "同 handler 两个 Lambda 走出不一致行为"。
        for _k, _v in dispatch_infra.env_vars().items():
            api_fn.add_environment(_k, _v)
            if getattr(self, "_lifecycle_consumer", None) is not None:
                self._lifecycle_consumer.add_environment(_k, _v)

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
    # 的 AMG( 实测 AMP workspace 为空,正是走自建)。想用 AWS 托管再显式开。
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

    # #234 — /system/info metrics.enabled must reflect the real switch
    # (config metrics.enabled), not just the AMP path. The self-hosted
    # Prometheus/Grafana backend (use_managed=false, built in litellm.py) never
    # set AMP_REMOTE_WRITE_URL, so the console showed "monitoring off" while the
    # PromGrafanaMonitor EC2 was actually running. Inject the config-driven flag
    # here unconditionally; litellm.py backfills the self-hosted Grafana ALB URL.
    if api_fn is not None:
        api_fn.add_environment(
            "METRICS_ENABLED",
            "true" if metrics_cfg.get("enabled", False) else "false",
        )
        api_fn.add_environment(
            "METRICS_BACKEND",
            "managed" if metrics_cfg.get("use_managed", False) else "self-hosted",
        )

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
        # Optional: RUNTIME_MONITORING feature. Off by default because the
        # naive path (features=[{RUNTIME_MONITORING, ENABLED}]) also flips
        # EC2_AGENT_MANAGEMENT to ENABLED at the account level, and that
        # associates SSM to auto-install the GuardDuty agent on EVERY EC2
        # in the account (evidence: internal-docs/00-knowledge-base/evidence/
        # metal-experiments/8layer-evidence.md:163-181 — AWS "how runtime
        # monitoring works ec2" doc). That is not safe for a shared account
        # hosting other teams' hosts.
        # Safe stance when the flag is true:
        #   RUNTIME_MONITORING=ENABLED but EC2_AGENT_MANAGEMENT=DISABLED,
        # so the runtime feature is provisioned in the detector but agents
        # are only installed on EC2s explicitly tagged
        # GuardDutyManaged=true (inclusion-tag path). Anything else the
        # account already runs (Wazuh/auditd in-guest, existing agents)
        # keeps working.
        gd_features = None
        if sec_cfg.get("guardduty_runtime_monitoring", False):
            gd_features = [
                guardduty.CfnDetector.CFNFeatureConfigurationProperty(
                    name="RUNTIME_MONITORING",
                    status="ENABLED",
                    additional_configuration=[
                        guardduty.CfnDetector.CFNFeatureAdditionalConfigurationProperty(
                            name="EC2_AGENT_MANAGEMENT",
                            status="DISABLED",
                        ),
                    ],
                ),
            ]
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
            features=gd_features,
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

    # ========== Inspector2 host/ECR vulnerability scanning (issue #27) ==========
    # Amazon Inspector v2 is an account-level toggle (no L2 CDK construct
    # today — aws_inspectorv2 only exposes L1s for filters/CIS scans, not
    # for enabling the service itself). Path: config-gated AwsCustomResource
    # calling inspector2:Enable on-create and inspector2:Disable on-delete,
    # so the flag flip both enables the service AND tears it down cleanly
    # on stack destroy.
    # Default false: Enable is an account-level side effect (bills per
    # ec2/ecr resource scanned) — safer to leave the account operator in
    # control and let them either flip this flag or run the enable command
    # out of band.
    if sec_cfg.get("inspector_enabled", False):
        insp_resource_types = sec_cfg.get("inspector_resource_types", ["EC2", "ECR"])
        insp_enable = cr.AwsCustomResource(
            self,
            "Inspector2Enable",
            on_create=cr.AwsSdkCall(
                service="Inspector2",
                action="enable",
                parameters={
                    "resourceTypes": insp_resource_types,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"inspector2-{self.region}"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="Inspector2",
                action="enable",
                parameters={
                    "resourceTypes": insp_resource_types,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"inspector2-{self.region}"
                ),
            ),
            on_delete=cr.AwsSdkCall(
                service="Inspector2",
                action="disable",
                parameters={
                    "resourceTypes": insp_resource_types,
                },
            ),
            install_latest_aws_sdk=True,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "inspector2:Enable",
                            "inspector2:Disable",
                            "inspector2:BatchGetAccountStatus",
                        ],
                        resources=["*"],
                    ),
                    # inspector2:Enable creates the service-linked role on
                    # first call; grant iam:CreateServiceLinkedRole scoped
                    # to inspector2 SLR so the custom resource can bootstrap
                    # the account without needing pre-existing SLR.
                    iam.PolicyStatement(
                        actions=["iam:CreateServiceLinkedRole"],
                        resources=[
                            f"arn:aws:iam::{self.account}:role/aws-service-role/inspector2.amazonaws.com/AWSServiceRoleForAmazonInspector2"
                        ],
                        conditions={
                            "StringLike": {
                                "iam:AWSServiceName": "inspector2.amazonaws.com"
                            }
                        },
                    ),
                ]
            ),
        )
        # Route Inspector2 findings to the same SNS topic as GuardDuty so
        # the aggregated monitoring platform (Wazuh) picks them up.
        if notifications_topic is not None:
            insp_rule = events.Rule(
                self,
                "Inspector2ToSns",
                event_pattern=events.EventPattern(
                    source=["aws.inspector2"],
                    detail_type=["Inspector2 Finding"],
                ),
            )
            insp_rule.add_target(targets.SnsTopic(notifications_topic))
            # EventBridge rule depends on inspector2 being enabled first,
            # otherwise the source has no events to match.
            insp_rule.node.add_dependency(insp_enable)
        cdk.CfnOutput(
            self,
            "Inspector2Enabled",
            value=",".join(insp_resource_types),
        )

    instance_profile = iam.CfnInstanceProfile(
        self,
        "HostInstanceProfile",
        roles=[host_role.role_name],
        instance_profile_name=f"openclaw-host-profile{self._gsuffix}",
    )

    # Golden-image bake moved to its own stack (deploy/stacks/image.py →
    # OpenClawImageStack). It ran a CodeBuild project behind a BLOCKING custom
    # resource here, so a build failure rolled back the whole orchestrator
    # stack. Split out + made non-blocking: a bad build now only touches the
    # image stack. The ASG below no longer depends on image readiness (ctx has
    # no image_ready), so a fresh region may churn a host a few minutes until
    # the bake lands the rootfs — not a deploy failure.

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

    # --- Pack onto ctx ---
    ctx.ac_cfg = locals().get("ac_cfg")
    ctx.ac_enabled = locals().get("ac_enabled")
    ctx.ac_gateway = locals().get("ac_gateway")
    ctx.amp_remote_write_url = locals().get("amp_remote_write_url")
    ctx.gateway_url = locals().get("gateway_url")
    ctx.host_role = locals().get("host_role")
    ctx.instance_profile = locals().get("instance_profile")
    ctx.metrics_cfg = locals().get("metrics_cfg")
    ctx.sec_cfg = locals().get("sec_cfg")
