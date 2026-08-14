# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk


def build_outputs(self, ctx):
    """Build outputs resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    alb = getattr(ctx, 'alb', None)
    api = getattr(ctx, 'api', None)
    api_key = getattr(ctx, 'api_key', None)
    app_cf_id = getattr(ctx, 'app_cf_id', None)
    asg = getattr(ctx, 'asg', None)
    assets_bucket = getattr(ctx, 'assets_bucket', None)
    audit_table = getattr(ctx, 'audit_table', None)
    backup_bucket = getattr(ctx, 'backup_bucket', None)
    backup_cmk = getattr(ctx, 'backup_cmk', None)
    batch_jobs_table = getattr(ctx, 'batch_jobs_table', None)
    clawpool_cmk = getattr(ctx, 'clawpool_cmk', None)
    cognito_outputs = getattr(ctx, 'cognito_outputs', None)
    console_cf_id = getattr(ctx, 'console_cf_id', None)
    console_bff_alb = getattr(ctx, 'console_bff_alb', None)
    console_bff_host = getattr(ctx, 'console_bff_host', None)
    console_host = getattr(ctx, 'console_host', None)
    dashboard_host = getattr(ctx, 'dashboard_host', None)
    dual_mode = getattr(ctx, 'dual_mode', None)
    groups_table = getattr(ctx, 'groups_table', None)
    hosts_table = getattr(ctx, 'hosts_table', None)
    instance_profile = getattr(ctx, 'instance_profile', None)
    launch_template = getattr(ctx, 'launch_template', None)
    notifications_topic_arn = getattr(ctx, 'notifications_topic_arn', None)
    sg = getattr(ctx, 'sg', None)
    tenant_idp_table = getattr(ctx, 'tenant_idp_table', None)
    tenant_secrets_table = getattr(ctx, 'tenant_secrets_table', None)
    tenants_table = getattr(ctx, 'tenants_table', None)

    _bff_host_for_output = (
        console_bff_host
        or (CFG.get("console_auth", {}) or {}).get("bff_domain")
        or (
            console_bff_alb.load_balancer_dns_name
            if console_bff_alb is not None
            else None
        )
    )

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
        # 提供。ConsoleUrl 输出裸 host,路径由 setup.sh 与 Puppeteer 消费方拼接。
        #   ConsoleUrl    — operator console 裸 host (Cognito-protected, BFF ALB-served)
        #   DashboardUrl  — per-tenant dashboards (ALB-served, app_domain in dual mode)
        # 未配置 BFF ALB 时 ConsoleUrl 才回落原 console_host 行为。
        "ConsoleUrl": (
            f"https://{_bff_host_for_output}"
            if console_bff_alb is not None
            else f"https://{console_host}"
        ),
        "DashboardUrl": f"https://{dashboard_host}",
        "DualDomainMode": "true" if dual_mode else "false",
        "CloudfrontDistributionId": console_cf_id,
        "AppCloudfrontDistributionId": app_cf_id,
        **(
            {"ConsoleBffAlbDns": console_bff_alb.load_balancer_dns_name}
            if console_bff_alb is not None
            else {}
        ),
        **(
            {"NotificationsTopicArn": notifications_topic_arn}
            if notifications_topic_arn
            else {}
        ),
        # upstream service (which encrypts credentials before the API Gateway)
        # knows which key to use. Only emitted when the key exists.
        **(
            {"ClawPoolCmkArn": clawpool_cmk.key_arn}
            if clawpool_cmk is not None
            else {}
        ),
        **cognito_outputs,
    }.items():
        cdk.CfnOutput(self, key, value=val)

    # 防 CDK refactor/重命名时 CFN 把有状态资源 replace（数据丢失/入口断）。
    # 值 = CDK synth 当前产出的 logical ID（等同已部署的 CFN 真值）。
    # 明确不钉：Lambda/IAM Role/EventBridge（无状态）、LiteLLM/PromGrafana EC2
    # （userdata hash 机制 intentional replace）。
    _pins = {
        # 第一类：换了 = 数据没了
        tenants_table: "Tenants540CDC43",
        hosts_table: "Hosts95443A44",
        groups_table: "Groups1E262F83",
        audit_table: "AuditLog4517CFBA",
        batch_jobs_table: "BatchJobs2A3B4C5D",
        tenant_idp_table: "TenantIdpMap6E7F8A9B",
        tenant_secrets_table: "TenantSecrets1C2D3E4F",  # gitleaks:allow
        assets_bucket: "Assets560B5C73",
        backup_bucket: "TenantBackups7A8B9C0D",
    }
    # Cognito UserPool（条件创建，可能不存在）
    if hasattr(self, "_user_pool") and self._user_pool is not None:
        _pins[self._user_pool] = "ConsoleUserPool1A2B3C4D"
    # 第二类：换了 = 入口坐标变
    _pins.update(
        {
            alb: "DashboardALB5E6F7A8B",
            api: "Api9C0D1E2F",
            asg: "HostASG3A4B5C6D",
            launch_template: "HostLT7E8F9A0B",
            sg: "HostSG1C2D3E4F",
        }
    )
    if hasattr(self, "_edge_asg") and self._edge_asg is not None:
        _pins[self._edge_asg] = "EdgeASG5A6B7C8D"
    if hasattr(self, "_edge_lt") and self._edge_lt is not None:
        _pins[self._edge_lt] = "EdgeLaunchTemplate9E0F1A2B"
    if hasattr(self, "_edge_sg") and self._edge_sg is not None:
        _pins[self._edge_sg] = "EdgeSG3C4D5E6F"
    for resource, logical_id in _pins.items():
        if resource is not None and hasattr(resource, "node"):
            cfn = resource.node.default_child
            if cfn is not None:
                cfn.override_logical_id(logical_id)
