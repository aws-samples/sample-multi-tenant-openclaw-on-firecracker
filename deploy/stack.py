# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import platform as _platform
import re
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

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yml").read_text())

# --- Domain modules (issue #87 mechanical split) ---
from types import SimpleNamespace as _NS
from stacks._helpers import (
    _sam_build_image_for_host,
    _read_pyproject_version,
    _build_vpc,
)
from stacks.storage import build_storage
from stacks.lambdas import build_lambdas
from stacks.compute import build_compute
from stacks.network_vpc import build_network_vpc
from stacks.litellm import build_litellm
from stacks.ha_edge import build_ha_edge
from stacks.auth import build_auth
from stacks.outputs import build_outputs


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
        # ║ 三包归属导航(配合项目归属文档)                                    ║
        # ║ 本 __init__ 顺序构建,资源用局部变量交叉引用,暂不物理拆分(拆 con-  ║
        # ║ struct 会改 logical ID → 删库风险,见 work-items/WI-001)。改动时    ║
        # ║ 按下方 [包X] 标记认领自己的段,别动别的包的段;跨段枢纽变量(api_fn/ ║
        # ║ assets_bucket/vpc/host_role/alb 等)改动走 SHARED-FILES-PROTOCOL。  ║
        # ║   [包C 控制面+工程化] DDB/S3/Lambda/API GW/CodeBuild/Cognito/Outputs ║
        # ║   [包B 隔离安全]       HostRole/监控/ASG/userdata/DNS-FW/Wazuh/AgentCore║
        # ║   [包A 数据面]         ALB/CloudFront/CORS                          ║
        # ╚══════════════════════════════════════════════════════════════════╝

        # ╓─── [包C 控制面+工程化] owner=C ── 数据/Lambda/API 控制面 ───────────╖

        # --- Domain resource construction (issue #87 mechanical split) ---
        ctx = _NS()
        ctx.CFG = CFG
        ctx._deploy_region = _deploy_region
        ctx._is_prod_region = _is_prod_region

        build_storage(self, ctx)
        build_lambdas(self, ctx)
        build_compute(self, ctx)
        build_network_vpc(self, ctx)
        build_litellm(self, ctx)
        build_ha_edge(self, ctx)
        build_auth(self, ctx)
        build_outputs(self, ctx)
