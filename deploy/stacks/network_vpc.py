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

from stacks._helpers import _build_vpc


def build_network_vpc(self, ctx):
    """Build network_vpc resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    _api_cfg = getattr(ctx, '_api_cfg', None)
    _platform_authorizer = getattr(ctx, '_platform_authorizer', None)
    api_fn = getattr(ctx, 'api_fn', None)
    sec_cfg = getattr(ctx, 'sec_cfg', None)

    # ========== VPC (P2b · #187 FR-10, INTERFACE-CONTRACT §6) ==========
    # 3 档:default_vpc(存量兼容,host 裸公网,不推荐)/ self_managed(自建 /20
    # + 3 公 + 3 私 + 3 NAT)/ imported(客户传 vpc_id + 6 subnet id)。
    # 生产走 self_managed(host 全落私有子网,守 AWS 暴露红线);切档=重建栈。
    vpc = _build_vpc(self, CFG.get("network", {}) or {})

    # ========== #122 Private API Gateway (生产加固,config-gated 默认关) ==========
    # 在现有 EDGE API(公网,浏览器/调试)之外,再建一个 PRIVATE REST API 指向
    # 同一个 api_fn,给机器/生产流量走私有通道。默认关 → synth byte-identical。
    # 官方最佳实践(AWS Documentation MCP 查证 2026-07-07,见 memory
    # private-apigw-sigv4-research):
    #  · PRIVATE / EDGE 互斥,一个 RestApi 只能一种 endpoint 类型 → 双 API 各自
    #    指同一 Lambda(受支持的模式),不是给一个 API 配两种类型。
    #  · 私有 API 无 resource policy 无法 deploy(fail-closed 默认全拒);用
    #    grant_invoke_from_vpc_endpoints_only([vpce]) 一步生成"只允许该 VPCE"策略。
    #  · execute-api Interface VPCE:InterfaceVpcEndpointAwsService.APIGATEWAY
    #    (常量名 APIGATEWAY 即 execute-api),private_dns_enabled=True,SG 只放
    #    443 from VPC CIDR(open=False 严格由 SG 控)。
    #  · method 走 AWS_IAM 授权:调用方须 SigV4 签名 + execute-api:Invoke 权限
    #    (漏设 authorization_type 会让 method 全网公开——高频坑,这里显式设)。
    #  · {proxy+} ANY 代理到 api_fn,避免逐条重声明 20+ 路由(与 EDGE 同后端)。
    # VPCE 挂当前(默认)VPC;#119 自建私有 VPC 落地后迁私有子网。
    if bool(_api_cfg.get("private_api_enabled", False)):
        _priv_vpce_sg = ec2.SecurityGroup(
            self,
            "ExecuteApiVpceSg",
            vpc=vpc,
            description="execute-api VPCE - HTTPS 443 from within VPC only (issue 122)",
            allow_all_outbound=False,
        )
        _priv_vpce_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "HTTPS from VPC CIDR to execute-api VPCE",
        )
        _execute_api_vpce = ec2.InterfaceVpcEndpoint(
            self,
            "ExecuteApiVpce",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.APIGATEWAY,  # = execute-api
            private_dns_enabled=True,
            security_groups=[_priv_vpce_sg],
            open=False,  # 不自动按 CIDR 放行,完全由上面 SG 控
        )
        # resource policy:只放行经本 VPCE 的流量,且**不带**无条件 Allow AnyPrincipal。
        # 安全评审 MEDIUM 修复:CDK 的 grant_invoke_from_vpc_endpoints_only 会额外
        # 生成一条无条件 `Allow AnyPrincipal execute-api:Invoke` —— 同账号语义下
        # (AWS authorization-flow Table A)identity policy 对 execute-api 沉默的主体
        # 也会被这条 Allow 放行,使 method 的 AWS_IAM 门形同虚设(与"须 execute-api:
        # Invoke"的意图矛盾)。改成**只留 Deny 非本 VPCE** 的 policy:非空 policy 满足
        # 私有 API 部署硬要求;VPCE 网络锁仍在;同账号调用方回落 Table A"两侧都沉默
        # → 隐式拒",于是 identity policy 真需要 execute-api:Invoke(与注释一致)。
        _priv_resource_policy = iam.PolicyDocument(
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.DENY,
                    principals=[iam.AnyPrincipal()],
                    actions=["execute-api:Invoke"],
                    resources=["execute-api:/*"],
                    conditions={
                        "StringNotEquals": {
                            "aws:SourceVpce": _execute_api_vpce.vpc_endpoint_id
                        }
                    },
                )
            ]
        )
        private_api = apigw.RestApi(
            self,
            "PrivateApi",
            rest_api_name="openclaw-orchestrator-private",
            deploy_options=apigw.StageOptions(stage_name="v1"),
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.PRIVATE],
                vpc_endpoints=[_execute_api_vpce],
            ),
            policy=_priv_resource_policy,
            # 所有 method 默认 AWS_IAM 授权(SigV4 + execute-api:Invoke),统一设
            # 防逐 method 漏配(漏设 = 该 method 全网公开)。
            default_method_options=apigw.MethodOptions(
                authorization_type=apigw.AuthorizationType.IAM
            ),
        )
        # {proxy+} ANY → 同一个 api_fn(与 EDGE 同后端;proxy 集成一条覆盖全路由)。
        #
        # 安全评审 HIGH 修复:私有 API 复用 api_fn,而 handler 的
        # _get_caller_identity(core/auth.py)对"无 Bearer"请求返回 is_admin=True
        # 的受信自动化 god-admin(EDGE 侧靠 api_key_required 的 admin-key 密钥门兜住)。
        # 若私有 proxy 只挂 AWS_IAM、不带 api-key/platform authorizer,则任何能到达
        # VPCE + 有 execute-api:Invoke 的 SigV4 主体都被解析成"无域全 fleet 管理员"
        # (跨 owner 读/删、fleet-power 停全部 microVM、#108 域隔离失效、审计丢归因)。
        # 修:私有 method 与 EDGE 同款门控——api_key_required(保留 admin-key 密钥门,
        # 即"SigV4 网络身份 + api-key 应用层密钥"双因子),并在 #108 配置了 platform
        # authorizer 时挂上(SigV4 机器调用方按平台域收敛,而非 blanket admin)。
        # authorization_type 仍 IAM(default_method_options 已设),这里叠 api-key。
        _priv_proxy = private_api.root.add_proxy(
            default_integration=apigw.LambdaIntegration(api_fn),
            any_method=False,
        )
        _priv_opts = {"api_key_required": True}
        if _platform_authorizer is not None:
            _priv_opts["authorizer"] = _platform_authorizer
            _priv_opts["authorization_type"] = apigw.AuthorizationType.CUSTOM
        _priv_proxy.add_method("ANY", apigw.LambdaIntegration(api_fn), **_priv_opts)
        # 私有 API 自己的 usage plan + key(与 EDGE 的 admin key 分开,便于独立轮换/收窄)。
        _priv_key = private_api.add_api_key(
            "PrivateApiKey", api_key_name="openclaw-private-key"
        )
        private_api.add_usage_plan(
            "PrivateUsagePlan",
            name="openclaw-private-plan",
            api_stages=[
                apigw.UsagePlanPerApiStage(
                    api=private_api, stage=private_api.deployment_stage
                )
            ],
        ).add_api_key(_priv_key)
        # 输出私有 API URL + VPCE id,供 SigV4 demo / 运维用(仅 feature on 时)。
        cdk.CfnOutput(self, "PrivateApiUrl", value=private_api.url)
        cdk.CfnOutput(
            self, "ExecuteApiVpceId", value=_execute_api_vpce.vpc_endpoint_id
        )

    # ========== Bedrock Guardrail (#80 部署时序 — 栈内资源,SSM 输出) ==========
    # 长期做法:把带外 apply-hardening.sh 建的 Guardrail 挪进 CDK 栈内,拿到 id 后
    # 写 SSM /openclaw/bedrock-guardrail-id,LiteLLM userdata 从 SSM 读(不硬编码
    # od6s8sm533fs 那种账号特定 id)。策略定义单一真相源仍是
    # deploy/runtime-config-export/bedrock-guardrail.json —— apply-hardening.sh
    # 和这里的 CfnGuardrail 都从这个 JSON 转换,保证两条路径策略一致。
    #
    # 迁移路径(默认 false 保存量兼容):
    #  ① 存量账号已经带外建过 Guardrail(id 已在 SSM 或硬编码) → 留 false,现状不变。
    #  ② 新账号 / 想统一走 IaC → config.yml 里设 security.guardrail_managed_by_stack: true,
    #    栈内建 Guardrail、写 SSM,LiteLLM userdata 从 SSM 读。同账号已有同名 Guardrail
    #    时 CFN 会冲突(create-guardrail 名字不唯一是异常),运营需先把带外那个改名或删了
    #    再切开关。切开关的运维笔记落 RUNBOOK.md,别静默切。
    _guardrail_ssm_param_name = "/openclaw/bedrock-guardrail-id"
    _guardrail_managed = sec_cfg.get("guardrail_managed_by_stack", False)
    if _guardrail_managed:
        from lib.guardrail_props import build_guardrail_kwargs, summary

        _gr_json = str(
            Path(__file__).resolve().parent.parent
            / "runtime-config-export"
            / "bedrock-guardrail.json"
        )
        _gr_kwargs = build_guardrail_kwargs(_gr_json)
        _gr_stats = summary(_gr_kwargs)
        print(
            f"[#80 guardrail] CfnGuardrail from {_gr_json}: "
            f"topics={_gr_stats['topics']} content_filters={_gr_stats['content_filters']} "
            f"words={_gr_stats['words']} pii={_gr_stats['pii_entities']} "
            f"regexes={_gr_stats['regexes']} grounding={_gr_stats['grounding_filters']}"
        )
        _guardrail = bedrock.CfnGuardrail(self, "OpenClawGuardrail", **_gr_kwargs)
        # id → SSM。LiteLLM userdata / apply-hardening 都可以从这里读,不再硬编码。
        ssm.StringParameter(
            self,
            "BedrockGuardrailIdParam",
            parameter_name=_guardrail_ssm_param_name,
            string_value=_guardrail.attr_guardrail_id,
            description="Bedrock Guardrail id created by stack (#80). "
            "LiteLLM userdata reads this at boot instead of hardcoded id.",
        )
        cdk.CfnOutput(
            self,
            "BedrockGuardrailId",
            value=_guardrail.attr_guardrail_id,
            description="Bedrock Guardrail id (#80 CfnGuardrail managed by stack).",
        )


    # --- Pack onto ctx ---
    ctx._guardrail_ssm_param_name = locals().get('_guardrail_ssm_param_name')
    ctx.vpc = locals().get('vpc')
