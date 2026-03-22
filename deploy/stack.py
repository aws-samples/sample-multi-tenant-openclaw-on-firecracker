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
    aws_bedrock_agentcore_alpha as agentcore,
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
        ec2_describe_policy = iam.PolicyStatement(
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
        api_fn.add_to_role_policy(ec2_describe_policy)
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
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["autoscaling:CompleteLifecycleAction"],
            resources=["*"],
        ))
        host_role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:DescribeVolumes", "ec2:CreateTags"],
            resources=["*"],
        ))

        instance_profile = iam.CfnInstanceProfile(self, "HostInstanceProfile",
            roles=[host_role.role_name],
            instance_profile_name="openclaw-host-profile",
        )

        # ========== ASG (P1-4) ==========
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
        launch_vm_sh = (ud_dir / "launch-vm.sh").read_text().replace(
            "{{SUBNET_PREFIX}}", CFG["vm"]["subnet_prefix"])
        stop_vm_sh = (ud_dir / "stop-vm.sh").read_text()
        backup_data_sh = (ud_dir / "backup-data.sh").read_text()

        init_sh = (ud_dir / "init-host.sh").read_text()
        init_sh = init_sh.replace("{{ASSETS_BUCKET}}", "PLACEHOLDER_BUCKET")
        init_sh = init_sh.replace("{{ROOTFS_PREFIX}}", CFG["s3"]["rootfs_prefix"])
        init_sh = init_sh.replace("{{HOSTS_TABLE}}", "PLACEHOLDER_TABLE")
        init_sh = init_sh.replace("{{AVAIL_VCPU}}", str(_avail_vcpu))
        init_sh = init_sh.replace("{{AVAIL_MEM}}", str(_avail_mem))
        init_sh = init_sh.replace("{{AGENTCORE_GATEWAY_URL}}", gateway_url if ac_enabled and gateway_url else "none")
        # Embed launch/stop scripts as heredocs
        init_sh = init_sh.replace("{{LAUNCH_VM_SCRIPT}}",
            f"cat > /home/ubuntu/launch-vm.sh << 'LAUNCHEOF'\n{launch_vm_sh}LAUNCHEOF\n"
            "chmod +x /home/ubuntu/launch-vm.sh && chown ubuntu:ubuntu /home/ubuntu/launch-vm.sh")
        init_sh = init_sh.replace("{{STOP_VM_SCRIPT}}",
            f"cat > /home/ubuntu/stop-vm.sh << 'STOPEOF'\n{stop_vm_sh}STOPEOF\n"
            "chmod +x /home/ubuntu/stop-vm.sh && chown ubuntu:ubuntu /home/ubuntu/stop-vm.sh")
        init_sh = init_sh.replace("{{BACKUP_DATA_SCRIPT}}",
            f"cat > /home/ubuntu/backup-data.sh << 'BACKUPEOF'\n{backup_data_sh}BACKUPEOF\n"
            "chmod +x /home/ubuntu/backup-data.sh && chown ubuntu:ubuntu /home/ubuntu/backup-data.sh")

        # Split script around CDK token placeholders, inject as Fn::Join
        # PLACEHOLDER_BUCKET appears 5 times (manifest + rootfs + data template + skills sync + skills cron)
        # PLACEHOLDER_TABLE appears once (dynamodb put-item)
        parts = init_sh.split("PLACEHOLDER_BUCKET")
        # parts = [before_bucket1, ..., after_bucket5_with_table]
        table_split = parts[-1].split("PLACEHOLDER_TABLE")
        user_data = ec2.UserData.for_linux()
        join_parts = [parts[0]]
        for i in range(1, len(parts) - 1):
            join_parts.append(assets_bucket.bucket_name)
            join_parts.append(parts[i])
        join_parts.append(assets_bucket.bucket_name)
        join_parts.append(table_split[0])
        join_parts.append(hosts_table.table_name)
        join_parts.append(table_split[1])
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

        if CFG["asg"].get("use_spot"):
            cfn_lt = launch_template.node.default_child
            cfn_lt.add_property_override("LaunchTemplateData.InstanceMarketOptions", {
                "MarketType": "spot",
                "SpotOptions": {"SpotInstanceType": "one-time"},
            })

        # CFN doesn't support CpuOptions.NestedVirtualization yet.
        # Workaround: create a new LT version with nested virt via EC2 API, set as default.
        # Use $Latest as source so each CDK deploy (which creates a new LT version) triggers
        # a new nested-virt version on top of it.
        cfn_lt = launch_template.node.default_child
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
            # Include CDK LT version hash so update triggers when LT changes
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

        # Set the new version as default
        set_default_call = cr.AwsSdkCall(
            service="EC2",
            action="modifyLaunchTemplate",
            parameters={
                "LaunchTemplateId": launch_template.launch_template_id,
                "DefaultVersion": nested_virt.get_response_field("LaunchTemplateVersion.VersionNumber"),
            },
            physical_resource_id=cr.PhysicalResourceId.of("set-default-lt"),
            output_paths=["LaunchTemplate.DefaultVersionNumber"],
        )
        set_default = cr.AwsCustomResource(self, "SetDefaultLTVersion",
            on_create=set_default_call,
            on_update=set_default_call,
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
        # Override LT version to the nested-virt version created by AwsCustomResource
        cfn_asg.add_property_override("LaunchTemplate.Version",
            nested_virt.get_response_field("LaunchTemplateVersion.VersionNumber"))
        asg.add_lifecycle_hook("InitHook",
            lifecycle_hook_name="openclaw-host-init",
            lifecycle_transition=autoscaling.LifecycleTransition.INSTANCE_LAUNCHING,
            heartbeat_timeout=Duration.seconds(CFG["asg"]["lifecycle_hook_timeout"]),
            default_result=autoscaling.DefaultResult.ABANDON,
        )
        asg.add_lifecycle_hook("TerminateHook",
            lifecycle_hook_name="openclaw-host-terminate",
            lifecycle_transition=autoscaling.LifecycleTransition.INSTANCE_TERMINATING,
            heartbeat_timeout=Duration.seconds(120),
            default_result=autoscaling.DefaultResult.CONTINUE,
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

        # ========== AgentCore (optional) ==========
        ac_cfg = CFG.get("agentcore", {})
        ac_enabled = ac_cfg.get("enabled", False)
        gateway_url = ""

        if ac_enabled:
            # Gateway — MCP tool hub for all VMs
            if ac_cfg.get("gateway", {}).get("enabled", True):
                ac_gateway = agentcore.Gateway(self, "AgentCoreGateway",
                    name="openclaw-gateway",
                    description="OpenClaw Agent tool gateway",
                )
                gateway_url = ac_gateway.gateway_url
                ac_gateway.grant_invoke(host_role)

            # Memory — persistent cross-session memory
            if ac_cfg.get("memory", {}).get("enabled", True):
                strategies = []
                for s in ac_cfg.get("memory", {}).get("strategies", ["semantic"]):
                    if s == "semantic":
                        strategies.append(agentcore.MemoryStrategy.using_semantic(
                            name="openclaw-semantic",
                            namespaces=["/openclaw/tenant/{actorId}/semantic"],
                        ))
                    elif s == "user_preference":
                        strategies.append(agentcore.MemoryStrategy.using_user_preference(
                            name="openclaw-preferences",
                            namespaces=["/openclaw/tenant/{actorId}/preferences"],
                        ))
                ac_memory = agentcore.Memory(self, "AgentCoreMemory",
                    memory_name="openclaw-memory",
                    description="OpenClaw per-tenant memory",
                    expiration_duration=Duration.days(ac_cfg.get("memory", {}).get("expiration_days", 90)),
                    memory_strategies=strategies,
                )

        # Pass AgentCore config to API Lambda
        if ac_enabled and gateway_url:
            api_fn.add_environment("AGENTCORE_ENABLED", "true")
            api_fn.add_environment("AGENTCORE_GATEWAY_URL", gateway_url)

        # ========== ALB (Dashboard Proxy) ==========
        alb = elbv2.ApplicationLoadBalancer(self, "DashboardALB",
            load_balancer_name="openclaw-dashboard",
            vpc=vpc,
            internet_facing=True,
        )
        listener = alb.add_listener("HTTP", port=80)
        listener.add_targets("Hosts",
            port=80,
            targets=[asg],
            health_check=elbv2.HealthCheck(path="/health", interval=Duration.seconds(30)),
        )
        alb.connections.allow_from_any_ipv4(ec2.Port.tcp(443), "HTTPS inbound")
        sg.add_ingress_rule(ec2.Peer.security_group_id(alb.connections.security_groups[0].security_group_id),
            ec2.Port.tcp(80), "ALB to Nginx")
        # Host-to-host DNAT ports for cross-host nginx proxy
        sg.add_ingress_rule(ec2.Peer.security_group_id(sg.security_group_id),
            ec2.Port.tcp_range(18789, 18900), "Host-to-host DNAT")

        # ========== Outputs ==========
        for key, val in {
            "ApiUrl": api.url,
            "ApiKeyId": api_key.key_id,
            "TenantsTable": tenants_table.table_name,
            "HostsTable": hosts_table.table_name,
            "AssetsBucket": assets_bucket.bucket_name,
            "HostInstanceProfileArn": instance_profile.attr_arn,
            "DashboardUrl": f"http://{alb.load_balancer_dns_name}",
        }.items():
            cdk.CfnOutput(self, key, value=val)
