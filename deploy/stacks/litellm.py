# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import platform as _platform
import re
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
    aws_elasticloadbalancingv2_actions as elbv2_actions,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
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
    aws_bedrock as bedrock,
    aws_bedrock_agentcore_alpha as agentcore,
    aws_bedrockagentcore as agentcore_l1,
    aws_codebuild as codebuild,
    aws_s3_assets as s3_assets,
    aws_ssm as ssm,
    aws_secretsmanager as secretsmanager,
    aws_elasticache as elasticache,
    aws_rds as rds,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    custom_resources as cr,
    BundlingOptions,
    BundlingFileAccess,
    Duration,
    Fn,
    RemovalPolicy,
)
from constructs import Construct
from pathlib import Path


def build_litellm(self, ctx):
    """Build litellm resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    _guardrail_ssm_param_name = getattr(ctx, '_guardrail_ssm_param_name', None)
    api_fn = getattr(ctx, 'api_fn', None)
    assets_bucket = getattr(ctx, 'assets_bucket', None)
    clawpool_cmk = getattr(ctx, 'clawpool_cmk', None)
    metrics_cfg = getattr(ctx, 'metrics_cfg', None)
    sec_cfg = getattr(ctx, 'sec_cfg', None)
    vpc = getattr(ctx, 'vpc', None)

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
    # #187 P6: ai_gateway.ha_enabled=true 走 HA 路径(ASG min=2 + internal ALB
    # + RDS PostgreSQL Multi-AZ 共享 PG)。默认 false 保存量单机不变(HA-AUDIT
    # 记录 LiteLLM 单点是最后一个必修 CRITICAL)。HA 模式硬约束: 必须用外部
    # 共享 PG, 因为 compose 内嵌 postgres 只在容器本地存 vkey/spend, ASG 两台
    # 各自一份表, 一台 mint 的 vkey 另一台读不到, ALB round-robin 会随机误判
    # 计费; 长期方案就是把 PG 提出去。
    _ha_enabled = bool(_aigw_cfg.get("ha_enabled", False))
    if _aigw_url:
        ssm.StringParameter(
            self,
            "LiteLlmHostParam",
            parameter_name="/openclaw/litellm-host",
            string_value=_aigw_url,
        )
    elif _ha_enabled:
        # ---- HA 模式(#187 P6): ASG min=2 + internal ALB + RDS Multi-AZ ----
        # 共享 role(与单机一致, 少写一份)
        litellm_role = iam.Role(
            self,
            "LiteLlmHaRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                )
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
            "LiteLlmHaSecret",
            secret_name=f"openclaw-litellm-ha{self._gsuffix}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"user": "litellm"}),
                generate_string_key="master_key",
                exclude_punctuation=True,
                password_length=40,
            ),
        )
        litellm_secret.grant_read(litellm_role)
        # LiteLLM 实例 SG: internal ALB 到 4000
        litellm_sg = ec2.SecurityGroup(
            self,
            "LiteLlmHaSG",
            vpc=vpc,
            description="LiteLLM HA gateway: 4000 from ALB only, no 0.0.0.0",
            allow_all_outbound=True,
        )
        # internal ALB SG: VPC CIDR 到 4000(guest microVM 经 metal host 访问)
        litellm_alb_sg = ec2.SecurityGroup(
            self,
            "LiteLlmAlbSG",
            vpc=vpc,
            description="LiteLLM HA internal ALB: 4000 from VPC only",
            allow_all_outbound=True,
        )
        litellm_alb_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(4000),
            "VPC to LiteLLM ALB 4000",
        )
        litellm_sg.add_ingress_rule(
            ec2.Peer.security_group_id(litellm_alb_sg.security_group_id),
            ec2.Port.tcp(4000),
            "ALB to LiteLLM instance 4000",
        )
        # RDS PostgreSQL Multi-AZ (共享 PG, 两台实例连同一份 vkey/spend 表)
        _pg_secret = rds.DatabaseSecret(
            self,
            "LiteLlmPgSecret",
            username="litellm",
            secret_name=f"openclaw-litellm-pg{self._gsuffix}",
        )
        _pg_sg = ec2.SecurityGroup(
            self,
            "LiteLlmPgSG",
            vpc=vpc,
            description="LiteLLM RDS PG: 5432 from LiteLLM instances only",
            allow_all_outbound=False,
        )
        _pg_sg.add_ingress_rule(
            ec2.Peer.security_group_id(litellm_sg.security_group_id),
            ec2.Port.tcp(5432),
            "LiteLLM to PG 5432",
        )
        _pg_instance = rds.DatabaseInstance(
            self,
            "LiteLlmPg",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16_3
            ),
            # t4g.small: LiteLLM vkey/spend 表读写量低, 最小可行; 若打满换 r 系列
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON,
                ec2.InstanceSize.SMALL,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=(
                    ec2.SubnetType.PRIVATE_WITH_EGRESS
                    if vpc.private_subnets
                    else ec2.SubnetType.PUBLIC
                )
            ),
            credentials=rds.Credentials.from_secret(_pg_secret),
            database_name="litellm",
            security_groups=[_pg_sg],
            multi_az=True,  # HA 门, standby 在另一 AZ
            storage_encrypted=True,
            allocated_storage=20,  # gp3 默认, 后续按 spend 表增长扩
            deletion_protection=False,  # dev/test region 允许 stack teardown
            removal_policy=self._stateful_removal,
            backup_retention=Duration.days(7),
        )
        _pg_secret.grant_read(litellm_role)
        # SSM read: guardrail id(#80 同款)
        litellm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:"
                    f"parameter{_guardrail_ssm_param_name}"
                ],
            )
        )
        # userdata: 与单机一致的 docker+compose 拉起, 但 DATABASE_URL 指向 RDS,
        # 且 compose 不激活 embedded-db profile。凭据(master_key + pg secret)
        # 从 Secrets Manager 拉, #169 set +x 段照旧套。
        _pg_endpoint = _pg_instance.db_instance_endpoint_address
        _ha_ud = ec2.UserData.for_linux()
        _ha_ud.add_commands(
            "set -x",
            "dnf install -y docker jq || yum install -y docker jq",
            "mkdir -p /usr/libexec/docker/cli-plugins",
            'ARCH=$(uname -m); [ "$ARCH" = "aarch64" ] && CARCH=aarch64 || CARCH=x86_64',
            'curl -sL "https://github.com/docker/compose/releases/latest/download/'
            'docker-compose-linux-$CARCH" '
            "-o /usr/libexec/docker/cli-plugins/docker-compose && "
            "chmod +x /usr/libexec/docker/cli-plugins/docker-compose",
            "systemctl enable --now docker",
            "mkdir -p /opt/litellm && cd /opt/litellm",
            f"for i in $(seq 1 60); do "
            f"aws s3 cp s3://{assets_bucket.bucket_name}/deployment/litellm/ "
            f". --recursive --region {self.region} 2>/dev/null; "
            f"[ -f docker-compose.litellm.yml ] && "
            f"[ -f litellm-config.yaml ] && break; "
            f'echo "waiting for litellm assets ($i)"; sleep 10; done',
            # #169 纪律: 凭据段临时关 xtrace, 防 master_key + pg pw 明文进
            # EC2 console log。
            "set +x",
            f"MK=$(aws secretsmanager get-secret-value "
            f"--secret-id openclaw-litellm-ha{self._gsuffix} "
            f"--region {self.region} --query SecretString --output text "
            f"| jq -r .master_key)",
            f"PGPW=$(aws secretsmanager get-secret-value "
            f"--secret-id openclaw-litellm-pg{self._gsuffix} "
            f"--region {self.region} --query SecretString --output text "
            f"| jq -r .password)",
            'echo "LITELLM_MASTER_KEY=$MK" > .env',
            'echo "POSTGRES_USER=litellm" >> .env',
            'echo "POSTGRES_PASSWORD=$PGPW" >> .env',
            'echo "POSTGRES_DB=litellm" >> .env',
            # DATABASE_URL 指向 RDS Multi-AZ endpoint (DNS 会在 failover 时切
            # 到 standby, 无需应用改 endpoint)
            f'echo "DATABASE_URL=postgresql://litellm:$PGPW@'
            f'{_pg_endpoint}:5432/litellm" >> .env',
            "chmod 600 .env",
            "set -x",
            f"GR_ID=$(aws ssm get-parameter "
            f"--name {_guardrail_ssm_param_name} "
            f'--region {self.region} --query "Parameter.Value" '
            f'--output text 2>/dev/null || echo "")',
            # SSM 有值 → 启用 guardrail;无值(默认) → 删 guardrails 段无 guardrail 跑。
            # 绝不 fallback 到账号特定硬编码(od6s8sm533fs 是 ap-southeast-1 的 id,
            # 跨账号 400,memory #167)。与单机路径对称。
            'if [ -n "$GR_ID" ]; then '
            'echo "[litellm-ha-userdata] guardrail enabled id: $GR_ID"; '
            'sed "s|__GUARDRAIL_ID__|${GR_ID}|g" litellm-config.yaml > config.runtime.yaml; '
            "else "
            'echo "[litellm-ha-userdata] no guardrail id in SSM — running WITHOUT bedrock guardrail"; '
            'sed "/^guardrails:/,\\$d" litellm-config.yaml > config.runtime.yaml; '
            "fi",
            'if grep -q "__GUARDRAIL_ID__" config.runtime.yaml; then '
            'echo "[litellm-ha-userdata][ERR] guardrail placeholder '
            'not replaced" >&2; exit 1; fi',
            "sed -i 's|^\\(\\s*master_key:\\).*|\\1 "
            "os.environ/LITELLM_MASTER_KEY|' config.runtime.yaml || true",
            # HA 模式不激活 embedded-db profile, litellm-db 服务不启动;
            # DATABASE_URL 已指向 RDS。
            "docker compose -f docker-compose.litellm.yml up -d 2>&1 | tail -5",
            # HA 模式无需自写 SSM /openclaw/litellm-host: CDK synth 期就知道
            # internal ALB DNS, 直接 CDK 写入 StringParameter (下方); userdata
            # 因此不再需要 ssm:PutParameter 权限 (纯收权)。
        )
        # LaunchTemplate + ASG min=2 max=2 跨 AZ
        _ha_lt = ec2.LaunchTemplate(
            self,
            "LiteLlmHaLaunchTemplate",
            launch_template_name=f"openclaw-litellm-ha-lt{self._gsuffix}",
            instance_type=ec2.InstanceType(
                _aigw_cfg.get("instance_type", "c7i.large")
            ),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            role=litellm_role,
            security_group=litellm_sg,
            user_data=_ha_ud,
        )
        _ha_subnets = (
            vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
            if vpc.private_subnets
            else vpc.select_subnets(subnet_type=ec2.SubnetType.PUBLIC)
        )
        _ha_asg = autoscaling.AutoScalingGroup(
            self,
            "LiteLlmHaASG",
            auto_scaling_group_name=f"openclaw-litellm-ha-asg{self._gsuffix}",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=_ha_subnets.subnets),
            launch_template=_ha_lt,
            min_capacity=2,
            max_capacity=2,
            # ELB(不是 EC2)health check: ALB 判定 4000 unhealthy 后 ASG 拉新
            health_check=autoscaling.HealthCheck.elb(grace=Duration.seconds(300)),
        )
        # internal ALB (SG 只放 VPC CIDR:4000)
        _ha_alb = elbv2.ApplicationLoadBalancer(
            self,
            "LiteLlmHaAlb",
            load_balancer_name=f"openclaw-litellm-ha{self._gsuffix}"[:32],
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=_ha_subnets.subnets),
            internet_facing=False,
            security_group=litellm_alb_sg,
        )
        _ha_tg = elbv2.ApplicationTargetGroup(
            self,
            "LiteLlmHaTG",
            vpc=vpc,
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.INSTANCE,
            # /health/liveliness 是 LiteLLM 自带存活探针 (compose healthcheck
            # 也用它, deploy/litellm/docker-compose.litellm.yml:67)。
            health_check=elbv2.HealthCheck(
                path="/health/liveliness",
                protocol=elbv2.Protocol.HTTP,
                interval=Duration.seconds(15),
                timeout=Duration.seconds(6),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )
        _ha_alb.add_listener(
            "HaListener",
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            # internal ALB, SG 已锁 VPC CIDR, open=False 保持不加 0.0.0.0/0
            open=False,
            default_action=elbv2.ListenerAction.forward([_ha_tg]),
        )
        _ha_asg.attach_to_application_target_group(_ha_tg)
        # SSM /openclaw/litellm-host: synth 期直接写 ALB DNS, 不再靠 EC2 boot
        # 自写(去掉了 ssm:PutParameter 权限 + IMDSv2 token 段)。
        ssm.StringParameter(
            self,
            "LiteLlmHostParam",
            parameter_name="/openclaw/litellm-host",
            string_value=f"http://{_ha_alb.load_balancer_dns_name}:4000/v1",
        )
        # 反向 wiring 到 API Lambda / lifecycle_consumer (单机路径同款)
        _litellm_ssm_stmt_ha = iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:aws:ssm:{self.region}:{self.account}:"
                f"parameter/openclaw/litellm-host"
            ],
        )
        for _fn in filter(None, [api_fn, getattr(self, "_lifecycle_consumer", None)]):
            _fn.add_environment(
                "LITELLM_MASTER_KEY_SECRET", litellm_secret.secret_name
            )
            litellm_secret.grant_read(_fn)
            _fn.add_to_role_policy(_litellm_ssm_stmt_ha)
        cdk.CfnOutput(
            self,
            "LiteLlmHaAlbDns",
            value=_ha_alb.load_balancer_dns_name,
            description="LiteLLM HA internal ALB DNS (VPC-only:4000)",
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
        # #80 — LiteLLM userdata 从 SSM 读 guardrail id(去硬编码 od6s8sm533fs)。
        # 栈内建 Guardrail 时(security.guardrail_managed_by_stack=true)param 由本栈写;
        # 未开开关时 param 可能不存在,userdata 会走硬编码兜底(保存量兼容)+ 日志留痕。
        litellm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter{_guardrail_ssm_param_name}"
                ],
            )
        )
        # #17 — mint-shared-vkey.sh 在 LiteLLM 实例上(PROFILE=-,instance role)铸 shared
        # vkey 后写 SSM SecureString /openclaw/litellm-shared-vkey。写 SecureString 需两件事:
        #   (1) ssm:PutParameter 该参数;
        #   (2) 对加密用 CMK 的 kms:Encrypt/GenerateDataKey —— 必须用 host role 能 Decrypt 的
        #       CMK(alias/clawpool-general),否则 launch-vm 侧 --with-decryption 报 AccessDenied
        #       (mint-shared-vkey.sh 已显式 --key-id alias/clawpool-general)。
        # clawpool_cmk 仅在 security.clawpool_cmk_enabled=true 时存在;未开时 shared-vkey 走
        # 默认 aws/ssm key(host 侧同样能读,因 host role 有 aws/ssm 隐式解密)——故此权限
        # 仅在 CMK 启用时才需要,与 CMK 生命周期绑定。
        litellm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/litellm-shared-vkey"
                ],
            )
        )
        if clawpool_cmk is not None:
            clawpool_cmk.grant_encrypt(litellm_role)
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
            # #169 — disable xtrace around secret handling. The top-level `set -x`
            # would otherwise echo the resolved master key (also reused as the PG
            # password) into the EC2 console/system log, readable by anyone with
            # ec2:GetConsoleOutput. Re-enabled after the .env is written.
            "set +x",
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
            # #169 — secret is now in the 0600 .env; safe to resume tracing.
            "set -x",
            # config.runtime.yaml 必须先于 compose up 生成,否则 compose 把不存在的挂载源
            # 当目录建 → 容器内 /etc/litellm/config.yaml 成空目录 → IsADirectoryError 崩溃重启(已踩坑)。
            # litellm-config.yaml 在 S3 deployment/litellm/(setup.sh 已补传),就地生成 config.runtime.yaml。
            # guardrail id 从 SSM /openclaw/bedrock-guardrail-id 读:
            #   • SSM 有值(账号建了 Bedrock Guardrail 并写了 param) → sed 注入 id,启用 guardrail;
            #   • SSM 无值(默认;Bedrock guardrail 可能不在部署账号 / 客户不配) → 删掉整个
            #     guardrails 段,LiteLLM 无 guardrail 正常跑。绝不 fallback 到账号特定硬编码
            #     (旧 od6s8sm533fs 是 ap-southeast-1 的 id,美东一不存在 → ApplyGuardrail 400
            #     每条对话被拒,memory #167 踩过)。想启用只需写 SSM param,不动部署代码。
            f'GR_ID=$(aws ssm get-parameter --name {_guardrail_ssm_param_name} --region {self.region} --query "Parameter.Value" --output text 2>/dev/null || echo "")',
            'if [ -n "$GR_ID" ]; then '
            'echo "[litellm-userdata] guardrail enabled id: $GR_ID"; '
            'sed "s|__GUARDRAIL_ID__|${GR_ID}|g" litellm-config.yaml > config.runtime.yaml; '
            "else "
            'echo "[litellm-userdata] no guardrail id in SSM — running WITHOUT bedrock guardrail"; '
            # guardrails 是 config 末段,从 "guardrails:" 行删到文件尾(顶格 key,缩进块随之删)。
            'sed "/^guardrails:/,\\$d" litellm-config.yaml > config.runtime.yaml; '
            "fi",
            # fail-loud:启用路径若占位符没替换掉就 crash(guardrailIdentifier 会是字面
            # "__GUARDRAIL_ID__" → 每条对话 ApplyGuardrail 报错)。无 guardrail 路径已删段,
            # 不含占位符,天然跳过。
            'if grep -q "__GUARDRAIL_ID__" config.runtime.yaml; then echo "[litellm-userdata][ERR] guardrail placeholder not replaced" >&2; exit 1; fi',
            "sed -i 's|^\\(\\s*master_key:\\).*|\\1 os.environ/LITELLM_MASTER_KEY|' config.runtime.yaml || true",
            # 单机模式(默认 ha_enabled=false)DATABASE_URL 指向 compose 内网
            # litellm-db:5432,而 litellm-db 服务挂 profiles:["embedded-db"]
            # (docker-compose.litellm.yml:82)——不激活 profile 就不起 postgres,
            # litellm 连不上 DB 崩(P1001 Can't reach litellm-db:5432)。必须带
            # --profile embedded-db(与 litellm-up.sh:71 一致)。HA 模式(:2744)
            # 反而不带,因为 DATABASE_URL 指向外部 RDS,不需要 embedded-db。
            "docker compose --profile embedded-db -f docker-compose.litellm.yml up -d 2>&1 | tail -5",
            # IMDSv2: AL2023 强制 token,旧 IMDSv1 curl 取 IP 返回空→SSM 写成 http://:4000/v1(已踩坑)。先 PUT 拿 token。
            # #169 旁枝 — IMDSv2 token 也是凭据(300s 内可换实例角色临时凭据),xtrace
            # 会把展开后的明文 token 回显进 EC2 console log(与 master key 同类,持
            # ec2:GetConsoleOutput 可读回)。#169 secret 段修复只包了 master key,token
            # 段仍裸露。取/用 token 段临时关 xtrace;IP 是私网 IP(非凭据)顺带包进,put 后恢复。
            "set +x",
            'TOK=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 300")',
            'IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOK" http://169.254.169.254/latest/meta-data/local-ipv4)',
            f'aws ssm put-parameter --name /openclaw/litellm-host --type String --overwrite --value "http://$IP:4000/v1" --region {self.region}',
            # #169 旁枝 — IMDS token 已用完,恢复 xtrace(与 secret 段同款配对)。
            "set -x",
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
        for _fn in filter(None, [api_fn, getattr(self, "_lifecycle_consumer", None)]):
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
        # #187 P6: EC2 auto-recovery — 底层硬件挂了自动迁到健康 host, 保留
        # instance id / 私网 IP / EBS 卷; 与 docker compose restart=always 覆盖
        # "进程挂"和"系统挂"两层。集群化(2 manager + 共享 EFS + OpenSearch 集群)
        # 工作量大, 且 security.wazuh_enabled 默认关, 走 HA-AUDIT §13 认可的简版。
        _wazuh_recover_alarm = cloudwatch.Alarm(
            self,
            "WazuhMonitorSystemRecovery",
            metric=cloudwatch.Metric(
                namespace="AWS/EC2",
                metric_name="StatusCheckFailed_System",
                dimensions_map={"InstanceId": wazuh_instance.instance_id},
                period=Duration.minutes(1),
                statistic="Maximum",
            ),
            threshold=0,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="System status check failed, auto-recover EC2",
        )
        _wazuh_recover_alarm.add_alarm_action(
            cw_actions.Ec2Action(cw_actions.Ec2InstanceAction.RECOVER)
        )

    # ========== Self-hosted Prometheus + Grafana EC2 (#187 P6) ==========
    # 走自建路径(metrics.enabled=true 且 use_managed=false, 默认档): CDK 直接
    # 起一台监控 EC2, docker-compose 拉 Prometheus + Grafana, 复用
    # deploy/monitoring/{docker-compose.prom-grafana.yml, prometheus.yml,
    # grafana/} 一整套资产(setup.sh 上传到 assets bucket 的
    # deployment/monitoring/ 前缀, 参考 setup-monitoring-ec2.sh 的 userdata)。
    # AMG 强制 IAM Identity Center 走 SSO(HA-AUDIT §14 记录), 本环境没配 SSO,
    # 所以默认走自建, 让 cdk deploy 直接把观测栈拉起来, 不再靠手工跑 setup 脚本。
    # 自恢复: docker compose 内 restart=always + CloudWatch 系统健康告警触发
    # EC2 auto-recovery(StatusCheckFailed_System→recover), 挂了自动拉起。
    _prom_enabled = metrics_cfg.get("enabled", False) and not metrics_cfg.get(
        "use_managed", False
    )
    if _prom_enabled:
        _prom_type = metrics_cfg.get("self_hosted_instance_type", "c7i.large")
        prom_sg = ec2.SecurityGroup(
            self,
            "PromGrafanaSg",
            vpc=vpc,
            description="Prometheus + Grafana monitoring: VPC-only ingress",
            allow_all_outbound=True,
        )
        # 硬红线: 9090/3000 入站只放 VPC CIDR(setup-monitoring-ec2.sh:37 同款),
        # 绝不 0.0.0.0/0(#187 P7 已踩过 SG description 非 ASCII 400 拒的坑,
        # 描述文本一律 ASCII)。
        for _port, _desc in [(9090, "Prometheus"), (3000, "Grafana")]:
            prom_sg.add_ingress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.tcp(_port),
                _desc,
            )
        prom_role = iam.Role(
            self,
            "PromGrafanaRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                )
            ],
        )
        # ec2:DescribeInstances 只读: Prometheus ec2_sd 发现 metal host tag
        # Role=metal-host(prometheus.yml 里配 relabel), 拿私网 IP 后抓 :8899/metrics。
        # DescribeAvailabilityZones 是 ec2_sd 元数据补齐用(setup 脚本同款权限)。
        prom_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:DescribeAvailabilityZones",
                ],
                resources=["*"],
            )
        )
        assets_bucket.grant_read(prom_role)  # 拉 deployment/monitoring/ 资产
        _prom_ud = ec2.UserData.for_linux()
        _prom_prefix = "deployment/monitoring"
        _prom_ud.add_commands(
            "set -euxo pipefail",
            "dnf install -y docker || yum install -y docker",
            "systemctl enable --now docker",
            # docker compose v2 CLI plugin(AL2023 无 docker-compose-v2 包,
            # 手装,与 LiteLLM/Wazuh 段一致)
            "mkdir -p /usr/libexec/docker/cli-plugins",
            'ARCH=$(uname -m); [ "$ARCH" = "aarch64" ] && CARCH=aarch64 || CARCH=x86_64',
            'curl -sL "https://github.com/docker/compose/releases/latest/download/'
            'docker-compose-linux-$CARCH" '
            "-o /usr/libexec/docker/cli-plugins/docker-compose && "
            "chmod +x /usr/libexec/docker/cli-plugins/docker-compose",
            "mkdir -p /opt/monitoring",
            # S3 sync 全量资产(compose + prometheus.yml + grafana/ + targets/):
            # setup.sh 已将 deploy/monitoring/ 整目录 sync 到 assets bucket。
            # 竞态兜底: cdk deploy 顺序保证资产先到, 30 次重试 * 10s 足够。
            # 关键: 重试的成功判据是 compose 文件真到位, 不是 s3 sync exit 0。
            # (setup.sh 曾只上传 wazuh 资产, sync 返回 0 但 prom-grafana 缺失,
            #  导致 compose up 报 no such file — 判据看文件而非 sync 退出码。)
            f"for i in $(seq 1 30); do aws s3 sync "
            f"s3://{assets_bucket.bucket_name}/{_prom_prefix}/ /opt/monitoring/ "
            f"--region {self.region}; "
            "[ -f /opt/monitoring/docker-compose.prom-grafana.yml ] && break || sleep 10; done",
            # ec2_sd 发现按部署 region 抓 host tag; 资产里 prometheus.yml 的
            # region 默认写死 ap-southeast-1, 部署到别的 region 发现不到 host,
            # 用 sed 就地改成本栈 region(随重建继承, 不靠手改运行态)。
            f"sed -i 's/region: ap-southeast-1/region: {self.region}/' "
            "/opt/monitoring/prometheus.yml || true",
            # #169 同款纪律: Grafana admin 密码是凭据, 生成期临时关 xtrace 防
            # 明文进 EC2 console log (ec2:GetConsoleOutput 可读回)。写入 0600
            # .env 后恢复。
            "set +x",
            'echo "GRAFANA_ADMIN_PASSWORD=$(openssl rand -base64 24)" '
            "> /opt/monitoring/.env",
            "chmod 600 /opt/monitoring/.env",
            "set -x",
            "cd /opt/monitoring && docker compose --env-file .env "
            "-f docker-compose.prom-grafana.yml up -d",
        )
        prom_instance = ec2.Instance(
            self,
            "PromGrafanaMonitor",
            vpc=vpc,
            # 私有子网 + NAT 出网(拉 docker image / compose CLI); 若 VPC 是
            # default_vpc 全公有, fall back to public 但 SG 已锁 VPC CIDR。
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=(
                    ec2.SubnetType.PRIVATE_WITH_EGRESS
                    if vpc.private_subnets
                    else ec2.SubnetType.PUBLIC
                )
            ),
            instance_type=ec2.InstanceType(_prom_type),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            role=prom_role,
            security_group=prom_sg,
            user_data=_prom_ud,
            # user_data 改动自动重建(与 LiteLLM 段一致, 避免 CFN 只更 metadata
            # 不重跑首次 boot 的踩坑)
            user_data_causes_replacement=True,
            # EBS: 15d retention 的 TSDB + Grafana state, 100GB 足够(与
            # Wazuh 段同规格, encrypted at rest 硬红线)
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(100, encrypted=True),
                )
            ],
        )
        # EC2 auto-recovery: CloudWatch StatusCheckFailed_System alarm 触发
        # ec2:RecoverInstances(AWS 内建 action ARN), 底层硬件挂了自动迁移到
        # 健康 host, 保留 instance id / 私网 IP / EBS 卷。与 docker compose
        # restart=always 配合覆盖"进程挂"和"系统挂"两层。
        _prom_recover_alarm = cloudwatch.Alarm(
            self,
            "PromGrafanaSystemRecovery",
            metric=cloudwatch.Metric(
                namespace="AWS/EC2",
                metric_name="StatusCheckFailed_System",
                dimensions_map={"InstanceId": prom_instance.instance_id},
                period=Duration.minutes(1),
                statistic="Maximum",
            ),
            threshold=0,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="System status check failed, auto-recover EC2",
        )
        _prom_recover_alarm.add_alarm_action(
            cw_actions.Ec2Action(cw_actions.Ec2InstanceAction.RECOVER)
        )
        cdk.CfnOutput(self, "PromGrafanaMonitorId", value=prom_instance.instance_id)
        cdk.CfnOutput(
            self,
            "PromGrafanaHint",
            value=(
                "Grafana: http://<PromGrafanaMonitor private IP>:3000 "
                "(VPC-only SG; admin pw in /opt/monitoring/.env on host)"
            ),
        )

        # ── Grafana 对外 ALB (config-gated: metrics.grafana_alb, 默认建) ──
        # 私网 :3000 只 VPC 内可达; 要给 SA/运维在办公网看 dashboard 得有对外入口。
        # 安全红线同 DashboardALB: internet-facing ALB 入站【只】放 CloudFront
        # origin-facing prefix list, 绝不 0.0.0.0/0。对外访问走 CloudFront→ALB→
        # Grafana:3000; Grafana 自带 admin 登录(GF_AUTH_ANONYMOUS_ENABLED=false)
        # 作第二道认证。add_listener open=False 关掉 CDK 默认的 0.0.0.0/0 自动放行。
        if metrics_cfg.get("grafana_alb", True):
            # ALB 需 >=2 AZ 子网; 取前 2 个 public(无 public 则 private)。
            # 不复用后面才定义的 _az_count, 保持本段自洽。
            _g_alb_subnets = vpc.public_subnets[:2] or vpc.private_subnets[:2]
            grafana_alb = elbv2.ApplicationLoadBalancer(
                self,
                "GrafanaALB",
                load_balancer_name="openclaw-grafana-alb",
                vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnets=_g_alb_subnets),
                internet_facing=True,
            )
            _g_listener = grafana_alb.add_listener(
                "HTTP",
                port=80,
                open=False,  # 不自动开 0.0.0.0/0
                default_action=elbv2.ListenerAction.fixed_response(
                    404, content_type="text/plain", message_body="not found"
                ),
            )
            # 复用上面 DashboardALB 段的 region→prefix-list 映射逻辑(同一套红线)。
            _g_cf_pl_by_region = {
                "ap-southeast-1": "pl-31a34658",
                "us-east-1": "pl-3b927c52",
                "us-west-2": "pl-82a045eb",
            }
            _g_cf_pl = self.node.try_get_context(
                "cf_origin_facing_prefix_list"
            ) or _g_cf_pl_by_region.get(self.region)
            if _g_cf_pl:
                _g_listener.connections.allow_default_port_from(
                    ec2.Peer.prefix_list(_g_cf_pl),
                    "CloudFront origin-facing only (no 0.0.0.0/0)",
                )
            else:
                # 未知 region 且没传 context 则 fail-safe: 只放 VPC 内。
                _g_listener.connections.allow_default_port_from(
                    ec2.Peer.ipv4(vpc.vpc_cidr_block),
                    "fallback VPC-only: pass cf_origin_facing_prefix_list",
                )
            _g_tg = elbv2.ApplicationTargetGroup(
                self,
                "GrafanaTargetGroup",
                vpc=vpc,
                port=3000,
                protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.INSTANCE,
                targets=[
                    elbv2_targets.InstanceIdTarget(prom_instance.instance_id, 3000)
                ],
                health_check=elbv2.HealthCheck(
                    path="/api/health",
                    healthy_http_codes="200",
                    interval=Duration.seconds(15),
                ),
            )
            _g_listener.add_action(
                "GrafanaForward",
                action=elbv2.ListenerAction.forward([_g_tg]),
            )
            # 监控实例 SG 放行 ALB SG → :3000(SG 引用, 最小权限)。
            prom_sg.add_ingress_rule(
                ec2.Peer.security_group_id(
                    grafana_alb.connections.security_groups[0].security_group_id
                ),
                ec2.Port.tcp(3000),
                "Grafana ALB to :3000",
            )
            cdk.CfnOutput(
                self,
                "GrafanaAlbDns",
                value=grafana_alb.load_balancer_dns_name,
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


