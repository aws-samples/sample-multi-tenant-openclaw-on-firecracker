# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Observability data-plane log domain (issue #219, task 6 of #209 spec).

Wires the Fluent Bit → Kinesis Data Firehose → Amazon OpenSearch Service
pipeline described in `.kiro/specs/platform-observability/design.md` §5-§6.

Design constraints enforced here (also see spec R5.1-R5.8):

- Independent AOS domain. SHALL NOT share with the Wazuh alerts domain
  (`deploy/monitoring/wazuh-two-ec2/`) — L5 security-audit and operational
  logs must be fault- and permission-isolated (internal review).
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
    aws_opensearchservice as opensearch,
    aws_s3 as s3,
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
    # (master password auto-generated inside the domain); rolesmapping to
    # the Firehose delivery role is bootstrapped post-deploy by
    # scripts/observability/aos-bootstrap.sh (CDK L2 cannot express AOS
    # security-plugin role mappings).
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

    _subnets = (
        vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets
        or vpc.private_subnets
    )
    _capacity_kwargs = {
        "data_node_instance_type": str(
            _aos_cfg.get("data_node_instance_type", "r6g.large.search")
        ),
        "data_nodes": int(_aos_cfg.get("data_nodes", 2)),
    }
    _master_nodes = int(_aos_cfg.get("master_nodes", 0))
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

    log_domain = opensearch.Domain(
        self,
        "LogDomain",
        domain_name=f"claw-logs{_gsuffix}",
        version=opensearch.EngineVersion.OPENSEARCH_2_17,
        vpc=vpc,
        vpc_subnets=[ec2.SubnetSelection(subnets=_subnets[: len(_subnets)])],
        security_groups=[_domain_sg],
        zone_awareness=opensearch.ZoneAwarenessConfig(
            enabled=len(_subnets) >= 2,
            availability_zone_count=min(3, max(2, len(_subnets)))
            if len(_subnets) >= 2
            else None,
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
            resources=["*"],  # AWS API requires * for ENI describe/create
            conditions={
                "StringEquals": {
                    "ec2:Vpc": (
                        f"arn:{cdk.Aws.PARTITION}:ec2:{self.region}:{self.account}:vpc/{vpc.vpc_id}"
                    )
                }
            },
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
    _delivery_stream_name = f"claw-logs{_gsuffix}"
    delivery_stream = firehose.CfnDeliveryStream(
        self,
        "LogFirehoseStream",
        delivery_stream_name=_delivery_stream_name,
        delivery_stream_type="DirectPut",
        amazonopensearchservice_destination_configuration=(
            firehose.CfnDeliveryStream.AmazonopensearchserviceDestinationConfigurationProperty(
                index_name="claw-logs",
                role_arn=firehose_role.role_arn,
                domain_arn=log_domain.domain_arn,
                index_rotation_period="OneDay",
                s3_backup_mode="FailedDocumentsOnly",
                buffering_hints=firehose.CfnDeliveryStream.AmazonopensearchserviceBufferingHintsProperty(
                    interval_in_seconds=int(
                        _fh_cfg.get("buffering_interval_seconds", 60)
                    ),
                    size_in_m_bs=int(_fh_cfg.get("buffering_size_mib", 5)),
                ),
                retry_options=firehose.CfnDeliveryStream.AmazonopensearchserviceRetryOptionsProperty(
                    duration_in_seconds=int(_fh_cfg.get("retry_duration_seconds", 300)),
                ),
                vpc_configuration=firehose.CfnDeliveryStream.VpcConfigurationProperty(
                    role_arn=firehose_role.role_arn,
                    subnet_ids=[s.subnet_id for s in _subnets],
                    security_group_ids=[_fh_sg.security_group_id],
                ),
                s3_configuration=firehose.CfnDeliveryStream.S3DestinationConfigurationProperty(
                    bucket_arn=log_archive_bucket.bucket_arn,
                    role_arn=firehose_role.role_arn,
                    prefix="firehose-failed/claw-logs/",
                    error_output_prefix="firehose-errors/claw-logs/",
                    buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                        interval_in_seconds=60,
                        size_in_m_bs=5,
                    ),
                    compression_format="GZIP",
                ),
            )
        ),
    )
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

    # Ensure Firehose stream is created after its dependent role/domain/bucket.
    # CDK infers most of this, but the L1 uses raw ARN strings — pin explicitly.
    delivery_stream.node.add_dependency(firehose_role)
    delivery_stream.node.add_dependency(log_domain)
    delivery_stream.node.add_dependency(log_archive_bucket)

    ctx.log_domain = log_domain
    ctx.log_firehose_stream_name = _delivery_stream_name
    ctx.log_archive_bucket = log_archive_bucket
