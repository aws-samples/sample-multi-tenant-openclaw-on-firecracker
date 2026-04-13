import yaml
import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_s3 as s3,
    aws_ec2 as ec2,
    aws_autoscaling as autoscaling,
    aws_elasticloadbalancingv2 as elbv2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_bedrock_agentcore_alpha as agentcore,
    aws_bedrockagentcore as agentcore_l1,
    custom_resources as cr,
    Duration, Fn, RemovalPolicy,
)
from constructs import Construct
from pathlib import Path

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yml").read_text())


class OpenClawOrchestratorStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ========== DynamoDB ==========
        tenants_table = dynamodb.Table(self, "Tenants",
            table_name="openclaw-tenants",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        hosts_table = dynamodb.Table(self, "Hosts",
            table_name="openclaw-hosts",
            partition_key=dynamodb.Attribute(name="instance_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

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

        # ========== Lambda Shared Policy ==========
        ssm_policy = iam.PolicyStatement(
            actions=["ssm:SendCommand", "ssm:GetCommandInvocation"],
            resources=["*"],
        )
        ec2_policy = iam.PolicyStatement(
            actions=["ec2:DescribeInstances", "ec2:TerminateInstances"],
            resources=["*"],
        )

        # ========== API Lambda ==========
        api_fn = _lambda.Function(self, "ApiHandler",
            function_name="openclaw-api",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/api"),
            timeout=Duration.seconds(120),
            memory_size=256,
            environment={
                "TENANTS_TABLE": tenants_table.table_name,
                "HOSTS_TABLE": hosts_table.table_name,
                "ASSETS_BUCKET": assets_bucket.bucket_name,
                "ROOTFS_PREFIX": CFG["s3"]["rootfs_prefix"],
                "HOST_RESERVED_VCPU": str(CFG["host"]["reserved_vcpu"]),
                "HOST_RESERVED_MEM": str(CFG["host"]["reserved_mem_mb"]),
                "CPU_OVERCOMMIT_RATIO": str(CFG["host"].get("cpu_overcommit_ratio", 1.0)),
                "VM_DEFAULT_VCPU": str(CFG["vm"]["default_vcpu"]),
                "VM_DEFAULT_MEM": str(CFG["vm"]["default_mem_mb"]),
                "VM_DATA_DISK_MB": str(CFG["vm"]["data_disk_mb"]),
                "VM_PORT_BASE": str(CFG["vm"]["gateway_port_base"]),
                "VM_SUBNET_PREFIX": CFG["vm"]["subnet_prefix"],
                "ASG_NAME": "openclaw-hosts-asg",
                "BACKUP_PREFIX": CFG["s3"]["backup_prefix"],
            },
        )
        tenants_table.grant_read_write_data(api_fn)
        hosts_table.grant_read_write_data(api_fn)
        assets_bucket.grant_read(api_fn)
        api_fn.add_to_role_policy(ssm_policy)
        api_fn.add_to_role_policy(ec2_policy)
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["autoscaling:DescribeAutoScalingGroups", "autoscaling:SetDesiredCapacity",
                     "autoscaling:CompleteLifecycleAction",
                     "autoscaling:TerminateInstanceInAutoScalingGroup"],
            resources=["*"],
        ))

        # ========== API Gateway ==========
        api = apigw.RestApi(self, "Api",
            rest_api_name="openclaw-orchestrator",
            deploy_options=apigw.StageOptions(stage_name="v1"),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "x-api-key"],
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

        key_required = {"api_key_required": True}

        tenants_resource = api.root.add_resource("tenants")
        tenants_resource.add_method("GET", apigw.LambdaIntegration(api_fn), **key_required)
        tenants_resource.add_method("POST", apigw.LambdaIntegration(api_fn), **key_required)

        tenant_resource = tenants_resource.add_resource("{id}")
        tenant_resource.add_method("GET", apigw.LambdaIntegration(api_fn), **key_required)
        tenant_resource.add_method("DELETE", apigw.LambdaIntegration(api_fn), **key_required)

        tenant_action = tenant_resource.add_resource("{action}")
        tenant_action.add_method("POST", apigw.LambdaIntegration(api_fn), **key_required)
        tenant_action.add_method("GET", apigw.LambdaIntegration(api_fn), **key_required)

        hosts_resource = api.root.add_resource("hosts")
        hosts_resource.add_method("GET", apigw.LambdaIntegration(api_fn), **key_required)
        hosts_resource.add_method("POST", apigw.LambdaIntegration(api_fn), **key_required)

        host_resource = hosts_resource.add_resource("{instance_id}")
        host_resource.add_method("DELETE", apigw.LambdaIntegration(api_fn), **key_required)

        refresh_rootfs_resource = hosts_resource.add_resource("refresh-rootfs")
        refresh_rootfs_resource.add_method("POST", apigw.LambdaIntegration(api_fn), **key_required)

        rootfs_version_resource = hosts_resource.add_resource("rootfs-version")
        rootfs_version_resource.add_method("GET", apigw.LambdaIntegration(api_fn), **key_required)

        agentcore_resource = api.root.add_resource("agentcore")
        agentcore_status_resource = agentcore_resource.add_resource("status")
        agentcore_status_resource.add_method("GET", apigw.LambdaIntegration(api_fn), **key_required)

        # ========== Health Check Lambda ==========
        health_fn = _lambda.Function(self, "HealthCheck",
            function_name="openclaw-health-check",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/health_check"),
            timeout=Duration.seconds(120),
            memory_size=256,
            environment={
                "TENANTS_TABLE": tenants_table.table_name,
                "HOSTS_TABLE": hosts_table.table_name,
            },
        )
        tenants_table.grant_read_write_data(health_fn)
        hosts_table.grant_read_data(health_fn)
        health_fn.add_to_role_policy(ssm_policy)

        events.Rule(self, "HealthCheckSchedule",
            schedule=events.Schedule.rate(Duration.minutes(CFG["health_check"]["interval_minutes"])),
            targets=[targets.LambdaFunction(health_fn)],
        )

        # ========== Skills Lambda ==========
        skills_fn = _lambda.Function(self, "Skills",
            function_name="openclaw-skills",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/skills"),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={"ASSETS_BUCKET": assets_bucket.bucket_name},
        )
        assets_bucket.grant_read(skills_fn)
        skills_resource = api.root.add_resource("skills")
        skills_resource.add_method("GET", apigw.LambdaIntegration(skills_fn), **key_required)

        # ========== Templates Lambda ==========
        templates_fn = _lambda.Function(self, "Templates",
            function_name="openclaw-templates",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/templates"),
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

        # ========== Scaler Lambda (idle host reclaim) ==========
        scaler_fn = _lambda.Function(self, "Scaler",
            function_name="openclaw-scaler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/scaler"),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "HOSTS_TABLE": hosts_table.table_name,
                "ASG_NAME": "openclaw-hosts-asg",
                "IDLE_TIMEOUT_MINUTES": str(CFG["scaler"]["idle_timeout_minutes"]),
            },
        )
        hosts_table.grant_read_write_data(scaler_fn)
        scaler_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["autoscaling:DescribeAutoScalingGroups",
                     "autoscaling:TerminateInstanceInAutoScalingGroup"],
            resources=["*"],
        ))
        events.Rule(self, "ScalerSchedule",
            schedule=events.Schedule.rate(Duration.minutes(CFG["scaler"]["interval_minutes"])),
            targets=[targets.LambdaFunction(scaler_fn)],
        )

        # ========== Backup Lambda (daily data backup) ==========
        backup_fn = _lambda.Function(self, "Backup",
            function_name="openclaw-backup",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/backup"),
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
        backup_fn.add_to_role_policy(ssm_policy)
        backup_fn.grant_invoke(api_fn)  # API Lambda async invokes Backup Lambda

        events.Rule(self, "BackupSchedule",
            schedule=events.Schedule.expression(CFG["s3"]["backup_cron"]),
            targets=[targets.LambdaFunction(backup_fn)],
        )

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
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["autoscaling:CompleteLifecycleAction"],
            resources=["*"],
        ))
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:DescribeVolumes", "ec2:CreateTags"],
            resources=["*"],
        ))
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks"],
            resources=["*"],
        ))

        instance_profile = iam.CfnInstanceProfile(self, "HostInstanceProfile",
            roles=[host_role.role_name],
            instance_profile_name="openclaw-host-profile",
        )

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

        vpc = ec2.Vpc.from_lookup(self, "Vpc", is_default=True)

        sg = ec2.SecurityGroup(self, "HostSG",
            vpc=vpc, security_group_name="openclaw-host-sg",
            allow_all_outbound=True,
        )

        # Compute allocatable resources from instance type
        _itype = CFG["host"]["instance_type"]
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
        init_sh = init_sh.replace("{{AGENTCORE_GATEWAY_URL}}", gateway_url if gateway_url else "none")
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

        # AMI lookup
        ami = ec2.MachineImage.lookup(
            name="ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*",
            owners=["099720109477"],
        )

        launch_template = ec2.LaunchTemplate(self, "HostLT",
            launch_template_name="openclaw-host-lt",
            instance_type=ec2.InstanceType(CFG["host"]["instance_type"]),
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
                    volume=ec2.BlockDeviceVolume.ebs(CFG["host"]["data_volume_gb"],
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        delete_on_termination=False),
                ),
            ],
        )

        cfn_lt = launch_template.node.default_child

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
            targets=[targets.LambdaFunction(api_fn)],
        )

        # When a host is terminating → cleanup DynamoDB records
        events.Rule(self, "HostTerminateRule",
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
                tools_fn = _lambda.Function(self, "AgentCoreTools",
                    function_name="openclaw-agentcore-tools",
                    runtime=_lambda.Runtime.PYTHON_3_12,
                    handler="handler.lambda_handler",
                    code=_lambda.Code.from_asset("lambda/agentcore_tools"),
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


        # ========== ALB (Dashboard Proxy) ==========
        alb = elbv2.ApplicationLoadBalancer(self, "DashboardALB",
            load_balancer_name="openclaw-dashboard",
            vpc=vpc,
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
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticloadbalancing:CreateTargetGroup", "elasticloadbalancing:DeleteTargetGroup",
                "elasticloadbalancing:RegisterTargets", "elasticloadbalancing:DeregisterTargets",
                "elasticloadbalancing:CreateRule", "elasticloadbalancing:DeleteRule",
                "elasticloadbalancing:DescribeRules", "elasticloadbalancing:DescribeTargetGroups",
                "elasticloadbalancing:DescribeListeners",
            ],
            resources=["*"],
        ))

        # ========== CloudFront (HTTPS without custom domain) ==========
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(assets_bucket)
        cf_distribution = cloudfront.Distribution(self, "DashboardCF",
            comment="OpenClaw Dashboard",
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
            additional_behaviors={
                "/console/*": cloudfront.BehaviorOptions(
                    origin=s3_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                ),
            },
            default_root_object="",
        )

        # ========== Console Auth (Cognito) ==========
        auth_cfg = CFG.get("console_auth", {})
        cognito_outputs = {}
        if auth_cfg.get("enabled", False):
            existing_pool_id = auth_cfg.get("user_pool_id", "")
            existing_client_id = auth_cfg.get("user_pool_client_id", "")

            if existing_pool_id and existing_client_id:
                user_pool = cognito.UserPool.from_user_pool_id(self, "ConsoleUserPool", existing_pool_id)
                cognito_outputs["CognitoUserPoolId"] = existing_pool_id
                cognito_outputs["CognitoClientId"] = existing_client_id
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
                cf_domain = cf_distribution.distribution_domain_name
                callback_url = f"https://{cf_domain}/console/index.html"
                user_pool.add_domain("ConsoleDomain",
                    cognito_domain=cognito.CognitoDomainOptions(
                        domain_prefix="openclaw-console",
                    ),
                )
                client = user_pool.add_client("ConsoleClient",
                    o_auth=cognito.OAuthSettings(
                        flows=cognito.OAuthFlows(implicit_code_grant=True),
                        scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                        callback_urls=[callback_url],
                        logout_urls=[callback_url],
                    ),
                )
                cognito_outputs["CognitoUserPoolId"] = user_pool.user_pool_id
                cognito_outputs["CognitoClientId"] = client.user_pool_client_id
                cognito_outputs["CognitoDomain"] = f"openclaw-console.auth.{cdk.Stack.of(self).region}.amazoncognito.com"

        # ========== Outputs ==========
        for key, val in {
            "ApiUrl": api.url,
            "ApiKeyId": api_key.key_id,
            "TenantsTable": tenants_table.table_name,
            "HostsTable": hosts_table.table_name,
            "AssetsBucket": assets_bucket.bucket_name,
            "HostInstanceProfileArn": instance_profile.attr_arn,
            "DashboardUrl": f"https://{cf_distribution.distribution_domain_name}",
            **cognito_outputs,
        }.items():
            cdk.CfnOutput(self, key, value=val)
