# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Observability data-plane log domain (issue #219, task 6 of #209 spec).

Wires the Fluent Bit → Kinesis Data Firehose → Amazon OpenSearch Service
pipeline described in `engineering/00-knowledge-base/SPEC/kiro/platform-observability/design.md` §5-§6.

Design constraints enforced here (also see spec R5.1-R5.8):

- Independent AOS domain. SHALL NOT share with the Wazuh alerts domain
  (`deploy/monitoring/wazuh-two-ec2/`) — L5 security-audit and operational
  logs must be fault- and permission-isolated (design decision).
- VPC-only, FGAC (internal user database), encryption at rest,
  node-to-node encryption, enforce HTTPS, TLS 1.2 minimum. No SG ingress
  from the public internet ("0.0.0.0/0") — VPC CIDR + Firehose SG only.
- Guest microVMs stay zero-credential: this stack only builds host/edge-
  side aggregation. Nothing here provisions anything inside a microVM.
- Config-gated (`logging.enabled` — default false). When false, this
  module returns before instantiating any construct; synth is
  byte-identical to a build with the module unimported.
- Sizing (data node count, EBS size) is parameterized via `config.yml`.
  Defaults are the smallest supportable footprint and MUST be re-tuned
  after task 0.1 samples real edge log volume — see spec §6.1 F sizing
  table.

Firehose → AOS uses `firehose.CfnDeliveryStream` (L1). L2 `DeliveryStream`
has no OpenSearch destination binding (2026-07 CDK 2.x); the L1 shape is
what the AWS API accepts and what the standard reference solution
`opensource/centralized-logging-with-opensearch` builds up to via CDK
overrides.
"""

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kinesisfirehose as firehose,
    aws_lambda as _lambda,
    aws_opensearchservice as opensearch,
    aws_s3 as s3,
    custom_resources as cr,
)


def build_observability(self, ctx):
    """Build Firehose + AOS domain + Log_Archive_Bucket.

    config.yml key (see config.yml.example for full docstring):

        logging:
          enabled: false        # master gate — false disables the whole stack
          aos:
            data_node_instance_type: "r6g.large.search"
            data_nodes: 2
            master_node_instance_type: "m6g.large.search"   # 0 disables dedicated masters (demo only)
            master_nodes: 0
            ebs_volume_size_gib: 100
          firehose:
            buffering_interval_seconds: 60
            buffering_size_mib: 5
            retry_duration_seconds: 300
          log_archive_bucket:
            retention_days: 90
    """
    CFG = ctx.CFG
    _cfg = CFG.get("logging") or {}
    if not bool(_cfg.get("enabled", False)):
        # Config gate: absolutely nothing rendered. Property 8 (Design.md).
        return

    _deploy_region = getattr(ctx, "_deploy_region", None) or self.region
    _stateful_removal = getattr(ctx, "_stateful_removal", RemovalPolicy.DESTROY)
    _auto_delete = bool(getattr(ctx, "_auto_delete", True))
    vpc = getattr(ctx, "vpc", None)
    if vpc is None:
        raise ValueError(
            "observability.enabled=true requires ctx.vpc; ensure "
            "build_network_vpc runs before build_observability."
        )

    _aos_cfg = _cfg.get("aos") or {}
    _fh_cfg = _cfg.get("firehose") or {}
    _bkt_cfg = _cfg.get("log_archive_bucket") or {}

    # ────────────────────────────────────────────────────────────────
    # Log_Archive_Bucket — Firehose backup + future ALB access logs.
    # SSE-S3 (ELB access log delivery constraint), block public, lifecycle
    # expiration on config-set retention days.
    # ────────────────────────────────────────────────────────────────
    _retention_days = int(_bkt_cfg.get("retention_days", 90))
    _gsuffix = getattr(self, "_gsuffix", "")
    log_archive_bucket = s3.Bucket(
        self,
        "LogArchiveBucket",
        bucket_name=f"openclaw-log-archive-{self.account}{_gsuffix}",
        encryption=s3.BucketEncryption.S3_MANAGED,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        removal_policy=_stateful_removal,
        auto_delete_objects=_auto_delete,
        lifecycle_rules=[
            s3.LifecycleRule(
                id="expire-log-records",
                enabled=True,
                expiration=Duration.days(_retention_days),
                # multipart-upload cleanup so failed uploads don't accrue cost
                abort_incomplete_multipart_upload_after=Duration.days(1),
            )
        ],
    )

    # ────────────────────────────────────────────────────────────────
    # AOS domain — VPC-only, FGAC, encryption, TLS 1.2.
    # Node count/type come from config. FGAC uses an internal user database
    # (master password auto-generated inside the domain into a Secrets Manager
    # secret, child construct id "MasterUser"). rolesmapping of the Firehose
    # delivery role into the security plugin is bootstrapped at deploy time by
    # the AosRolesMapping custom resource below (#219 follow-up) — CDK L2 has
    # no property for AOS security-plugin role mappings, so a VPC Lambda calls
    # the _plugins/_security API directly. Replaces the old manual
    # scripts/observability/aos-bootstrap.sh step.
    # ────────────────────────────────────────────────────────────────
    _domain_sg = ec2.SecurityGroup(
        self,
        "LogDomainSg",
        vpc=vpc,
        description="AOS log domain: ingress only from Firehose delivery ENIs + BFF (VPC-only)",
        allow_all_outbound=False,
    )
    # VPC-internal only — Firehose ENIs live in the same VPC, BFF (future
    # trace viewer / log search) likewise. No 0.0.0.0/0. HTTPS API port only.
    _domain_sg.add_ingress_rule(
        peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
        connection=ec2.Port.tcp(443),
        description="HTTPS from VPC (Firehose delivery ENIs, BFF, bootstrap script)",
    )

    # #272 — logging.aos.subnet_ids 非空时显式选 OpenSearch 域子网(imported 私有
    # 子网场景);缺省回落 PRIVATE_WITH_EGRESS → private_subnets。
    # #280 — 用 from_subnet_attributes 显式带 AZ:from_subnet_id 不带 AZ,CDK 拿到
    # dummy AZ token,OpenSearch Domain 的 zone_awareness 多 AZ 分布错乱、LogDomain
    # 部署失败。契约:subnet_ids 书写顺序须与 stack AZ 顺序(self.availability_zones)
    # 一致 —— 按 index 配对,乱序填会给子网标错 AZ。
    _aos_subnet_ids = _aos_cfg.get("subnet_ids") or []
    if _aos_subnet_ids:
        _stack_azs = list(self.availability_zones)
        _subnets = [
            ec2.Subnet.from_subnet_attributes(
                self,
                f"AosSubnet{_i}",
                subnet_id=_sid,
                availability_zone=_stack_azs[_i]
                if _i < len(_stack_azs)
                else _stack_azs[0],
            )
            for _i, _sid in enumerate(_aos_subnet_ids)
        ]
    else:
        _subnets = (
            vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets
            or vpc.private_subnets
        )
    _data_nodes = int(_aos_cfg.get("data_nodes", 2))
    _capacity_kwargs = {
        "data_node_instance_type": str(
            _aos_cfg.get("data_node_instance_type", "r6g.large.search")
        ),
        "data_nodes": _data_nodes,
    }
    # OpenSearch requires data_nodes to be an exact multiple of the domain's
    # AZ count. Picking AZ count from subnet count alone (min(3, len(subnets)))
    # breaks whenever data_nodes isn't divisible by it — a /20 VPC yields 3
    # PRIVATE_WITH_EGRESS subnets, so data_nodes=2 asked for 3 AZs and the
    # domain create failed with "must be a multiple of the AZs configured".
    # Cap AZ count at the largest value in 2..min(3, subnets) that divides
    # data_nodes; if only 1 data node or 1 subnet, disable zone awareness.
    _n_subnets = len(_subnets)
    _az_count = max(
        (n for n in range(min(3, _n_subnets), 1, -1) if _data_nodes % n == 0),
        default=1,
    )
    _master_nodes = int(_aos_cfg.get("master_nodes", 0))
    # Multi-AZ with Standby REQUIRES 3 dedicated master nodes. The CDK feature
    # flag enableOpensearchMultiAzWithStandby defaults it ON, so a zone-aware
    # domain with master_nodes=0 fails "must turn on dedicated master nodes for
    # domains with standby". Only allow standby when 3 masters are configured;
    # otherwise disable it explicitly (zone awareness for redundancy still on).
    if _master_nodes > 0:
        # AWS Config `opensearch-primary-node-fault-tolerance` requires
        # exactly 3 for HA. Anything else is a demo/dev shape and the
        # config gate below fails loud so operators don't ship a 1-master
        # split-brain by accident.
        if _master_nodes != 3:
            raise ValueError(
                "logging.aos.master_nodes must be 0 (no dedicated masters, "
                "demo only) or 3 (HA quorum). Config Rule "
                "opensearch-primary-node-fault-tolerance flags anything else."
            )
        _capacity_kwargs["master_node_instance_type"] = str(
            _aos_cfg.get("master_node_instance_type", "m6g.large.search")
        )
        _capacity_kwargs["master_nodes"] = _master_nodes
    else:
        _capacity_kwargs["multi_az_with_standby_enabled"] = False

    log_domain = opensearch.Domain(
        self,
        "LogDomain",
        domain_name=f"claw-logs{_gsuffix}",
        version=opensearch.EngineVersion.OPENSEARCH_2_17,
        vpc=vpc,
        vpc_subnets=[ec2.SubnetSelection(subnets=_subnets[:_az_count])],
        security_groups=[_domain_sg],
        zone_awareness=opensearch.ZoneAwarenessConfig(
            enabled=_az_count >= 2,
            availability_zone_count=_az_count if _az_count >= 2 else None,
        ),
        capacity=opensearch.CapacityConfig(**_capacity_kwargs),
        ebs=opensearch.EbsOptions(
            enabled=True,
            volume_type=ec2.EbsDeviceVolumeType.GP3,
            volume_size=int(_aos_cfg.get("ebs_volume_size_gib", 100)),
        ),
        encryption_at_rest=opensearch.EncryptionAtRestOptions(enabled=True),
        node_to_node_encryption=True,
        enforce_https=True,
        tls_security_policy=opensearch.TLSSecurityPolicy.TLS_1_2,
        fine_grained_access_control=opensearch.AdvancedSecurityOptions(
            master_user_name="claw-logs-admin",
        ),
        # ── Resource access policy (open — FGAC is the real gate) ──────────
        # 真机修复 (us-east-1 2026-07-14): 一个 VPC + FGAC 域若 access policy 为空,
        # AOS 会在**域访问策略这道闸**上把每个请求当 `anonymous` 拒掉 (403
        # "no resource-based policy allows es:ESHttpGet"),**根本轮不到 FGAC 校验
        # basic-auth 口令**。证据:edge (in-VPC) 对 `/`、`/_cluster/health`、
        # `/_plugins/_security/...` 的匿名 GET 全 403 "resource-based";SigV4 则报
        # "identity-based" —— 即两条路径都被访问策略挡在 FGAC 之前。AosRolesMapFn 的
        # basic-auth GET/PUT 以及 Firehose 的 es:ESHttp* 交付因此全部 403。
        # AWS 对 "FGAC + 内部用户库" 的标准做法就是一条**开放**资源策略,把真正的
        # 鉴权完全交给 FGAC(内部用户库 / rolesmapping)。加它不降低安全性:域仍是
        # VPC-only + FGAC 门禁,这条策略只是把 HTTP 层授权决定权让渡给 FGAC。
        # 注:不能用 log_domain.domain_arn(构造期自引用会成环),按 domain_name 手拼
        # ARN;CDK 会用一个 post-create 的 OpenSearchAccessPolicy 自定义资源套用它。
        access_policies=[
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=["es:ESHttp*"],
                resources=[
                    f"arn:{self.partition}:es:{self.region}:{self.account}"
                    f":domain/claw-logs{_gsuffix}/*"
                ],
            )
        ],
        removal_policy=_stateful_removal,
    )

    # ────────────────────────────────────────────────────────────────
    # Firehose delivery role — minimum permissions to fan out into AOS
    # + S3 backup + VPC ENI management (VpcConfiguration requires this).
    # Resources scoped to this domain and this bucket only.
    # ────────────────────────────────────────────────────────────────
    firehose_role = iam.Role(
        self,
        "LogFirehoseRole",
        assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
        description="Firehose delivery role for claw-logs stream: writes to claw-logs AOS + S3 backup",
    )
    # AOS: describe domain + POST bulk to claw-logs-* indices only.
    firehose_role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "es:DescribeDomain",
                "es:DescribeDomains",
                "es:DescribeDomainConfig",
            ],
            resources=[log_domain.domain_arn],
        )
    )
    firehose_role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "es:ESHttpPost",
                "es:ESHttpPut",
                "es:ESHttpGet",
            ],
            resources=[
                log_domain.domain_arn,
                f"{log_domain.domain_arn}/*",
            ],
        )
    )
    # S3 backup (FailedDocumentsOnly). Scoped to this bucket only.
    log_archive_bucket.grant_write(firehose_role)
    # VpcConfiguration ENI management. Actions are documented in
    # https://docs.aws.amazon.com/firehose/latest/APIReference/API_VpcConfiguration.html
    # and cannot be resource-scoped for ENI create/delete (AWS API design).
    # 真机(us-east-1, 2026-07-14):带 `ec2:Vpc` StringEquals condition 时 Firehose 建
    # 交付流报 Access Denied —— `ec2:CreateNetworkInterface`/`ec2:Describe*` 这些 ENI
    # 动作**不支持** `ec2:Vpc` condition key(ENI 创建那刻 VPC 上下文尚不存在,AWS IAM
    # 设计限制),condition 永不满足 → 实际等价 deny。上面注释自己也写了"cannot be
    # resource-scoped",却又加了 condition,自相矛盾。修:去掉无效 condition(这些 ENI
    # 管理动作按 AWS 设计本就只能 resources=["*"] 无条件授予,Firehose 交付流才建得起来)。
    firehose_role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "ec2:DescribeVpcs",
                "ec2:DescribeVpcAttribute",
                "ec2:DescribeSubnets",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeNetworkInterfaces",
                "ec2:CreateNetworkInterface",
                "ec2:DeleteNetworkInterface",
                "ec2:CreateNetworkInterfacePermission",
            ],
            resources=[
                "*"
            ],  # ENI describe/create 按 AWS API 设计必须 *,且不支持 ec2:Vpc condition
        )
    )

    # ────────────────────────────────────────────────────────────────
    # Firehose DeliveryStream (L1). Direct PUT; AOS destination with VPC
    # config, daily index rotation, 60s/5MiB buffering, 300s retry,
    # FailedDocumentsOnly → S3 backup.
    # ────────────────────────────────────────────────────────────────
    _fh_sg = ec2.SecurityGroup(
        self,
        "FirehoseDeliverySg",
        vpc=vpc,
        description="Firehose delivery ENIs into AOS VPC - egress only, no ingress",
        allow_all_outbound=True,
    )

    # One delivery role + one rolesmapping back all three streams — the role's
    # es:ESHttp* is scoped to domain/* (covers every claw-logs* index) and the
    # index is set per-stream below, so no extra IAM principals are needed
    # (#245: edge/host/vm streams, one role). Helper builds a CfnDeliveryStream
    # for a given index reusing the shared role/sg/bucket/domain.
    _fh_buf_interval = int(_fh_cfg.get("buffering_interval_seconds", 60))
    _fh_buf_size = int(_fh_cfg.get("buffering_size_mib", 5))
    _fh_retry = int(_fh_cfg.get("retry_duration_seconds", 300))

    def _mk_stream(cid, stream_name, index_name):
        return firehose.CfnDeliveryStream(
            self,
            cid,
            delivery_stream_name=stream_name,
            delivery_stream_type="DirectPut",
            amazonopensearchservice_destination_configuration=(
                firehose.CfnDeliveryStream.AmazonopensearchserviceDestinationConfigurationProperty(
                    index_name=index_name,
                    role_arn=firehose_role.role_arn,
                    domain_arn=log_domain.domain_arn,
                    index_rotation_period="OneDay",
                    s3_backup_mode="FailedDocumentsOnly",
                    buffering_hints=firehose.CfnDeliveryStream.AmazonopensearchserviceBufferingHintsProperty(
                        interval_in_seconds=_fh_buf_interval,
                        size_in_m_bs=_fh_buf_size,
                    ),
                    retry_options=firehose.CfnDeliveryStream.AmazonopensearchserviceRetryOptionsProperty(
                        duration_in_seconds=_fh_retry,
                    ),
                    vpc_configuration=firehose.CfnDeliveryStream.VpcConfigurationProperty(
                        role_arn=firehose_role.role_arn,
                        subnet_ids=[s.subnet_id for s in _subnets],
                        security_group_ids=[_fh_sg.security_group_id],
                    ),
                    s3_configuration=firehose.CfnDeliveryStream.S3DestinationConfigurationProperty(
                        bucket_arn=log_archive_bucket.bucket_arn,
                        role_arn=firehose_role.role_arn,
                        prefix=f"firehose-failed/{index_name}/",
                        error_output_prefix=f"firehose-errors/{index_name}/",
                        buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                            interval_in_seconds=60,
                            size_in_m_bs=5,
                        ),
                        compression_format="GZIP",
                    ),
                )
            ),
        )

    _delivery_stream_name = f"claw-logs{_gsuffix}"
    _host_stream_name = f"claw-logs-host{_gsuffix}"
    _vm_stream_name = f"claw-logs-vm{_gsuffix}"
    delivery_stream = _mk_stream(
        "LogFirehoseStream", _delivery_stream_name, "claw-logs"
    )
    host_stream = _mk_stream(
        "LogFirehoseStreamHost", _host_stream_name, "claw-logs-host"
    )
    vm_stream = _mk_stream("LogFirehoseStreamVm", _vm_stream_name, "claw-logs-vm")
    _all_streams = [delivery_stream, host_stream, vm_stream]
    # Firehose ENIs need to reach the AOS SG. Allow the SG-to-SG rule
    # explicitly (Peer.security_group) so the VPC-CIDR ingress above is not
    # the only path (defence in depth: shrinking VPC CIDR wouldn't
    # accidentally cut the delivery path).
    _domain_sg.add_ingress_rule(
        peer=ec2.Peer.security_group_id(_fh_sg.security_group_id),
        connection=ec2.Port.tcp(443),
        description="HTTPS from Firehose delivery SG",
    )

    # ────────────────────────────────────────────────────────────────
    # AOS FGAC rolesmapping (#219 follow-up 真机修复):
    # FGAC 开启后,仅给 firehose_role 挂 IAM `es:ESHttp*` 权限**不够** —— AOS
    # security 插件还要求把该 role 的 ARN 映射进一个能写索引的插件角色,否则
    # Firehose→AOS 交付被 security 插件拒(真机现象:IncomingRecords>0 但
    # DeliveryToAmazonOpenSearchService.Success=0)。CDK L2 无此属性,故用一个
    # **跑在 VPC 内**的 Lambda(域是 VPC-only)在部署时直接调
    # `_plugins/_security/api/rolesmapping/all_access`(GET 现状→并集→PUT),把
    # firehose_role.role_arn 并入 backend_roles。用户已明确授权映射到 all_access
    # (full-admin),不另建窄角色。凭据:Lambda 运行时按 SECRET_ARN 现取
    # master 口令(绝不进 env/CFN),用户名 "claw-logs-admin"。
    # ────────────────────────────────────────────────────────────────
    # master 口令由 CDK 存入 Secrets Manager(Domain 的子构造,id="MasterUser",
    # 内容为 {"username","password"} JSON)。取该 Secret 构造以授读 + 拿 ARN。
    _master_secret = log_domain.node.find_child("MasterUser")

    # Lambda 专属 SG:仅出站(去域 443),无入站。
    _rmap_sg = ec2.SecurityGroup(
        self,
        "AosRolesMapSg",
        vpc=vpc,
        description="AOS rolesmapping bootstrap Lambda ENIs - egress only, no ingress",
        allow_all_outbound=True,
    )
    # 允许该 Lambda SG 到域 SG 的 443(镜像上面 Firehose SG-to-SG 的做法)。
    _domain_sg.add_ingress_rule(
        peer=ec2.Peer.security_group_id(_rmap_sg.security_group_id),
        connection=ec2.Port.tcp(443),
        description="HTTPS from AOS rolesmapping bootstrap Lambda SG",
    )

    # Secrets Manager Interface VPCE:AosRolesMapFn 在 VPC 内,运行时 boto3 调 secretsmanager
    # 取 AOS master 口令。imported 客户 VPC 无法保证 private 子网真有 NAT 出网(from_vpc_attributes
    # 只按名字当 egress,不验路由)——2026-07-17 真机实撞:主栈部署到 AOS rolesmapping 时 Lambda
    # `Connect timeout on secretsmanager.<region>.amazonaws.com` → 整栈 ROLLBACK(#295,codex 判根因)。
    # VPCE 走 VPC 内直达(private_dns 让标准域名自动导向,代码不改),不依赖 NAT,对 imported/self_managed
    # 都普适。SG 只放 rolesmapping Lambda SG 443。
    #
    # #309 — 幂等门 `create_secretsmanager_vpce`(默认 true,保存量行为):AWS 硬规则同一
    # 服务在同一 VPC 只允许一个 Interface VPCE 开 private_dns_enabled。若 VPC 里已存在一个
    # secretsmanager VPCE(上轮 RETAIN 残留 / 客户自建),再建开 private DNS 的必冲突报
    # `private-dns-enabled ... conflicts` → 整栈 ROLLBACK(2026-07-17 真机撞)。已有 VPCE 的
    # 环境把此开关设 false:栈不建自己的,复用现有那个(private DNS 让标准域名照样解到它,
    # Lambda 透明可用)。default_vpc/self_managed 全新 VPC 保持 true。
    _create_sm_vpce = bool(_aos_cfg.get("create_secretsmanager_vpce", True))
    _sm_vpce = None
    if _create_sm_vpce:
        _sm_vpce_sg = ec2.SecurityGroup(
            self,
            "SecretsManagerVpceSg",
            vpc=vpc,
            description="Secrets Manager VPCE - 443 from AOS rolesmapping Lambda SG only",
            allow_all_outbound=False,
        )
        _sm_vpce_sg.add_ingress_rule(
            peer=ec2.Peer.security_group_id(_rmap_sg.security_group_id),
            connection=ec2.Port.tcp(443),
            description="HTTPS from AOS rolesmapping Lambda",
        )
        _sm_vpce = ec2.InterfaceVpcEndpoint(
            self,
            "SecretsManagerVpce",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            private_dns_enabled=True,
            open=False,
            security_groups=[_sm_vpce_sg],
            subnets=ec2.SubnetSelection(subnets=_subnets),
        )

    # onEvent handler(纯 stdlib urllib + ssl,无三方依赖;boto3 为 Lambda 运行时自带)。
    # Create/Update:取口令 → basic-auth GET 现有 all_access 映射 → 并集 backend_roles
    #   → PUT。幂等(已含该 ARN 则不重复)。域刚就绪/ENI 冷启的瞬态做退避重试。
    # Delete:no-op(见 handler 内注释,避免 teardown 竞态)。
    _rmap_fn = _lambda.Function(
        self,
        "AosRolesMapFn",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="index.on_event",
        timeout=Duration.minutes(5),
        memory_size=256,
        vpc=vpc,
        vpc_subnets=ec2.SubnetSelection(subnets=_subnets),
        security_groups=[_rmap_sg],
        environment={
            # 只放 ARN 与用户名;口令运行时用 GetSecretValue 现取,绝不入 env/CFN。
            "SECRET_ARN": _master_secret.secret_arn,
            "MASTER_USERNAME": "claw-logs-admin",
        },
        code=_lambda.Code.from_inline(
            "import base64, json, os, ssl, time\n"
            "import urllib.request, urllib.error\n"
            "import boto3\n"
            "\n"
            "# all_access = 内置全权限角色(用户已明确授权映射到 full-admin,不另建窄角色)。\n"
            '_API = "/_plugins/_security/api/rolesmapping/all_access"\n'
            "\n"
            "\n"
            "def _password():\n"
            "    # 口令绝不进 env/CFN,运行时按 SECRET_ARN 现取(CDK 生成的 MasterUser secret)。\n"
            '    arn = os.environ["SECRET_ARN"]\n'
            '    v = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]\n'
            "    try:\n"
            '        return json.loads(v)["password"]  # secret 为 {"username","password"} JSON\n'
            "    except Exception:\n"
            "        return v  # 兜底:非 JSON 时按裸口令\n"
            "\n"
            "\n"
            "def _http(method, url, hdr, body=None):\n"
            "    r = urllib.request.Request(url, data=body, headers=hdr, method=method)\n"
            "    with urllib.request.urlopen(r, timeout=15, context=ssl.create_default_context()) as x:\n"
            "        return x.status, x.read().decode()\n"
            "\n"
            "\n"
            "def _map_role(ep, user, pw, role_arn):\n"
            "    hdr = {\n"
            '        "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),\n'
            '        "Content-Type": "application/json",\n'
            "    }\n"
            '    url = f"https://{ep}{_API}"\n'
            "    cur = {}\n"
            "    try:\n"
            '        _, b = _http("GET", url, hdr)  # 先读现状,可能 404(尚无 all_access 映射)\n'
            '        cur = (json.loads(b) or {}).get("all_access", {}) if b else {}\n'
            "    except urllib.error.HTTPError as e:\n"
            "        if e.code != 404:\n"
            "            raise\n"
            '    be = list(cur.get("backend_roles") or [])\n'
            "    if role_arn not in be:  # 幂等:已含则不重复(重部署安全)\n"
            "        be.append(role_arn)\n"
            "    payload = json.dumps({  # PUT 整体替换,保留原 users/hosts,仅并入 backend_roles\n"
            '        "backend_roles": be,\n'
            '        "users": list(cur.get("users") or []),\n'
            '        "hosts": list(cur.get("hosts") or []),\n'
            "    }).encode()\n"
            '    return _http("PUT", url, hdr, payload)\n'
            "\n"
            "\n"
            "def on_event(event, ctx):\n"
            '    rt = event.get("RequestType")\n'
            '    pid = event.get("PhysicalResourceId") or "aos-firehose-rolesmapping"\n'
            '    if rt == "Delete":\n'
            "        # 删除 no-op:teardown 时域常已在销毁,写映射无意义且会引竞态;并集逻辑\n"
            "        # 已保证重部署幂等,残留一条 backend_role 亦无害。\n"
            '        return {"PhysicalResourceId": pid}\n'
            '    p = event["ResourceProperties"]\n'
            '    user = os.environ["MASTER_USERNAME"]\n'
            "    pw = _password()\n"
            "    # #266: 映射多个 backend_role 进 all_access —— firehose(写)+ BFF(读查询)。\n"
            "    # RoleArns 优先;缺省回落旧单值 FirehoseRoleArn(向后兼容既有栈)。\n"
            '    arns = list(p.get("RoleArns") or [])\n'
            '    if not arns and p.get("FirehoseRoleArn"):\n'
            '        arns = [p["FirehoseRoleArn"]]\n'
            "    err = None\n"
            "    for i in range(6):  # 域刚就绪 + ENI/DNS 冷启的瞬态,退避重试\n"
            "        try:\n"
            "            last = 200\n"
            "            for arn in arns:  # 逐个并集写入(_map_role 幂等,重复 ARN 不重复加)\n"
            '                st, body = _map_role(p["DomainEndpoint"], user, pw, arn)\n'
            "                if st not in (200, 201):\n"
            '                    raise RuntimeError(f"rolesmapping PUT -> {st}: {body}")\n'
            "                last = st\n"
            '            return {"PhysicalResourceId": pid, "Data": {"Status": str(last)}}\n'
            "        except urllib.error.HTTPError:\n"
            "            raise  # 有 HTTP 响应=连通正常,属 API 层错误,不重试\n"
            "        except (urllib.error.URLError, ssl.SSLError, OSError) as e:\n"
            "            err = e\n"
            "            time.sleep(min(2 ** i, 15))\n"
            '    raise RuntimeError(f"AOS security API 不可达(重试后仍失败): {err}")\n'
        ),
    )
    # 运行时按 ARN 现取口令 —— 仅授这个 secret 的读权限(最小权限)。
    _master_secret.grant_read(_rmap_fn)

    # Provider 框架:onEvent Lambda 在 VPC 内跑;框架自身的 responder Lambda 在
    # VPC 外(才能回 PUT CFN response),两者解耦。用 Provider 而非 cfnresponse
    # 手搓,house style 与 auth.py/compute.py 一致。
    _rmap_provider = cr.Provider(
        self,
        "AosRolesMapProvider",
        on_event_handler=_rmap_fn,
    )
    # #266 — BFF(console 日志查询)也要能读 AOS。它的 role 与 firehose role 一并
    # 映射进 all_access(读查询 + 写交付共用同一后端角色,用户已授权 all_access,
    # 不另建窄角色,与既有 firehose 映射决策一致)。BFF 不在 VPC(logging 关)时
    # ctx.console_bff_fn 为 None 或未进 VPC → 只映射 firehose role。
    _bff_fn = getattr(ctx, "console_bff_fn", None)
    _bff_in_vpc = getattr(ctx, "console_bff_in_vpc", False)
    _rmap_arns = [firehose_role.role_arn]
    if _bff_fn is not None and _bff_in_vpc:
        _rmap_arns.append(_bff_fn.role.role_arn)
    _rmap_cr = cdk.CustomResource(
        self,
        "AosRolesMapping",
        service_token=_rmap_provider.service_token,
        properties={
            # 端点/ARN 走 ResourceProperties:值变化(如换域/换 role)即触发 Update。
            "DomainEndpoint": log_domain.domain_endpoint,
            "RoleArns": _rmap_arns,
        },
    )
    # 顺序:映射必须在域**就绪**且交付流建成后再跑(交付流本身依赖域/role/bucket,
    # 见下方 add_dependency;这里显式再钉一次域与全部三条交付流)。一条 rolesmapping
    # 覆盖所有 stream(它们共用 firehose_role)。
    _rmap_cr.node.add_dependency(log_domain)
    for _s in _all_streams:
        _rmap_cr.node.add_dependency(_s)
    # VPCE 必须先就绪:Lambda 运行时经它调 secretsmanager 取口令(否则 imported VPC 无 NAT 时超时)。
    # #309 — 仅当本栈建了 VPCE 才钉依赖;复用现有 VPCE(create_secretsmanager_vpce=false)时
    # 它已存在、无需 add_dependency。
    if _sm_vpce is not None:
        _rmap_cr.node.add_dependency(_sm_vpce)

    # #219 修复(真机 us-east-1 2026-07-14):edge 的 Fluent Bit 用 **edge instance
    # role** 调 firehose:PutRecordBatch 往 edge stream 送 nginx access log,但此前
    # 从没给 edge role 授过 Firehose 写权限 → 真机报 `PutRecordBatch API responded
    # with error='AccessDeniedException'`、日志送不进 AOS。build_ha_edge 先跑并把
    # _edge_role 挂到 ctx.edge_role,这里补授 PutRecord/PutRecordBatch(仅 edge stream)。
    # 缺 ctx.edge_role 时(理论上不会)跳过,不硬失败。
    _edge_role = getattr(ctx, "edge_role", None)
    if _edge_role is not None:
        _edge_role.add_to_policy(
            iam.PolicyStatement(
                actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                resources=[delivery_stream.attr_arn],
            )
        )

    # #245: host 的 Fluent Bit 用 **host instance role** 送 journald(host stream)
    # + 每租户 fc.log(vm stream)。build_compute 把 host_role 挂到 ctx.host_role;
    # 只授这两条 stream(最小权限,不含 edge stream)。
    _host_role = getattr(ctx, "host_role", None)
    if _host_role is not None:
        _host_role.add_to_policy(
            iam.PolicyStatement(
                actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                resources=[host_stream.attr_arn, vm_stream.attr_arn],
            )
        )

    # ────────────────────────────────────────────────────────────────
    # #266 — 回填 console BFF 的 AOS 查询接线(vm/host 日志):域端点 + master
    # secret 读权限 + AOS SG 入站放行 BFF SG。auth.py 已让 BFF 进 VPC(logging.
    # enabled 时)并挂 ctx.console_bff_fn/_sg/_in_vpc;此处只在真进 VPC 时接线。
    # aos-client.mjs 运行时按 AOS_SECRET_ARN 现取口令 basic-auth,口令绝不进 env。
    # ────────────────────────────────────────────────────────────────
    if _bff_fn is not None and _bff_in_vpc:
        _bff_sg = getattr(ctx, "console_bff_sg", None)
        _bff_fn.add_environment("AOS_ENDPOINT", log_domain.domain_endpoint)
        _bff_fn.add_environment("AOS_SECRET_ARN", _master_secret.secret_arn)
        _bff_fn.add_environment("AOS_MASTER_USERNAME", "claw-logs-admin")
        _master_secret.grant_read(_bff_fn)
        if _bff_sg is not None:
            _domain_sg.add_ingress_rule(
                peer=ec2.Peer.security_group_id(_bff_sg.security_group_id),
                connection=ec2.Port.tcp(443),
                description="HTTPS from console BFF SG (per-tenant log search, #266)",
            )

    # ────────────────────────────────────────────────────────────────
    # Outputs — install-edge.sh reads FIREHOSE_DELIVERY_STREAM at userdata
    # time; the bootstrap script needs the AOS endpoint. Both must survive
    # a fresh clone (no manual copy).
    # ────────────────────────────────────────────────────────────────
    cdk.CfnOutput(
        self,
        "LogFirehoseStreamName",
        value=_delivery_stream_name,
        description=(
            "Kinesis Data Firehose delivery stream name for edge Fluent Bit "
            "(FIREHOSE_DELIVERY_STREAM env in install-edge.sh)."
        ),
    )
    cdk.CfnOutput(
        self,
        "LogFirehoseStreamNameHost",
        value=_host_stream_name,
        description="Firehose stream for host journald logs (FB_STREAM_HOST in init-host.sh).",
    )
    cdk.CfnOutput(
        self,
        "LogFirehoseStreamNameVm",
        value=_vm_stream_name,
        description="Firehose stream for per-tenant fc.log (FB_STREAM_VM in init-host.sh).",
    )
    cdk.CfnOutput(
        self,
        "LogDomainEndpoint",
        value=log_domain.domain_endpoint,
        description="AOS domain endpoint (VPC-only). Used by aos-bootstrap.sh and BFF log search.",
    )
    cdk.CfnOutput(
        self,
        "LogArchiveBucketName",
        value=log_archive_bucket.bucket_name,
        description="S3 bucket receiving Firehose FailedDocumentsOnly backups + future ALB access logs.",
    )

    # Ensure Firehose streams are created after their dependent role/domain/bucket.
    # CDK infers most of this, but the L1 uses raw ARN strings — pin explicitly.
    for _s in _all_streams:
        _s.node.add_dependency(firehose_role)
        _s.node.add_dependency(log_domain)
        _s.node.add_dependency(log_archive_bucket)

    ctx.log_domain = log_domain
    ctx.log_firehose_stream_name = _delivery_stream_name
    ctx.log_firehose_stream_name_host = _host_stream_name
    ctx.log_firehose_stream_name_vm = _vm_stream_name
    ctx.log_archive_bucket = log_archive_bucket
