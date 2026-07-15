# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""marketplace-demo 独立 CDK 栈——二手电商联邦参考实现,可单独 cdk deploy 到日本 region。

一键起齐档A「外部 IdP 联邦」的客户侧全套:
  1. entry Cognito User Pool（模拟二手电商自有 IdP）+ classic Hosted UI 域 + PKCE app client
  2. broker Lambda（电商后端代开租户：校验 id_token → 持 x-api-key 调本平台 POST /tenants）
  3. S3 + CloudFront 托管 marketplace.html（二手电商 SPA）

与本平台（ClawPool，控制面/hub 在别的 region）解耦：demo 部署到 ap-northeast-1，
通过 context 传入 ClawPool 的控制面 API base + entry pool 作为 upstream IdP 联邦进 ClawPool Cognito
的注册在本平台侧做（见 README「联邦对接」）。本栈只起「客户平台」这一侧。

部署:
  cd console/marketplace-demo/cdk
  cdk deploy --context region=ap-northeast-1 \
    --context ctrl_api_base=<ClawPool 控制面 API base> \
    --context ctrl_api_key_secret=<Secrets Manager ARN,存 x-api-key>
凭据/真实 base 走 context 或 Secrets Manager,不硬编码(对齐 the ops guide 无硬编码密钥)。
"""

import os

from aws_cdk import (
    App,
    Stack,
    CfnOutput,
    Duration,
    RemovalPolicy,
    aws_cognito as cognito,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_cloudfront as cf,
    aws_cloudfront_origins as origins,
    aws_iam as iam,
)
from constructs import Construct


class MarketplaceDemoStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw):
        super().__init__(scope, cid, **kw)
        ctx = self.node.try_get_context
        platform_id = ctx("platform_id") or "demo-marketplace"
        ctrl_api_base = ctx("ctrl_api_base") or ""  # ClawPool 控制面 API base
        ctrl_api_key_secret = ctx("ctrl_api_key_secret") or ""  # Secrets Manager ARN

        # 1) entry pool —— 模拟二手电商自有 IdP。终端用户在这里注册/登录。
        entry_pool = cognito.UserPool(
            self,
            "EntryPool",
            user_pool_name="demo-marketplace-entry",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(min_length=8),
            removal_policy=RemovalPolicy.DESTROY,  # demo,可随时销
        )
        # classic Hosted UI 域(联邦流实测:managed-login v1 = classic 才不撞 redirect_mismatch)
        entry_pool.add_domain(
            "EntryDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"demo-mkt-entry-{self.account}"
            ),
        )
        # SPA app client:authorization_code + PKCE(public,无 secret)
        spa_client = entry_pool.add_client(
            "SpaClient",
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[ctx("spa_callback") or "https://example.com/cb"],
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            generate_secret=False,
        )
        # 联邦对接用的 confidential client(本平台 ClawPool 注册 entry pool 为 upstream IdP 时用)
        fed_client = entry_pool.add_client(
            "FederationClient",
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[
                    ctx("clawpool_idpresponse") or "https://example.com/idpresponse"
                ],
            ),
            generate_secret=True,
        )

        # 2) broker Lambda —— 电商后端代开租户(校验 id_token → 调本平台 POST /tenants)
        broker = _lambda.Function(
            self,
            "Broker",
            function_name="demo-marketplace-broker",
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("../broker"),
            timeout=Duration.seconds(70),
            memory_size=256,
            environment={
                "PLATFORM_ID": platform_id,
                "ENTRY_ISSUER": f"https://cognito-idp.{self.region}.amazonaws.com/{entry_pool.user_pool_id}",
                "ENTRY_JWKS_URL": f"https://cognito-idp.{self.region}.amazonaws.com/{entry_pool.user_pool_id}/.well-known/jwks.json",
                "CTRL_API_BASE": ctrl_api_base,
                # CTRL_API_KEY 从 Secrets Manager 运行时读,不进 env 明文
                "CTRL_API_KEY_SECRET": ctrl_api_key_secret,
            },
        )
        if ctrl_api_key_secret:
            broker.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[ctrl_api_key_secret],
                )
            )
        broker_api = apigw.LambdaRestApi(
            self,
            "BrokerApi",
            handler=broker,
            proxy=False,
            rest_api_name="demo-marketplace-broker",
        )
        broker_api.root.add_resource("open-ai-pro").add_method("POST")

        # 3) S3 + CloudFront 托管二手电商 SPA
        site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cf.Distribution(
            self,
            "SiteCdn",
            default_root_object="marketplace.html",
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
        )
        s3deploy.BucketDeployment(
            self,
            "SiteDeploy",
            sources=[
                s3deploy.Source.asset(
                    "..", exclude=["cdk/**", "broker/**", "*.md", "*.sh"]
                )
            ],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "EntryPoolId", value=entry_pool.user_pool_id)
        CfnOutput(
            self,
            "EntryIssuer",
            value=f"https://cognito-idp.{self.region}.amazonaws.com/{entry_pool.user_pool_id}",
        )
        CfnOutput(self, "SpaClientId", value=spa_client.user_pool_client_id)
        CfnOutput(self, "FederationClientId", value=fed_client.user_pool_client_id)
        CfnOutput(
            self,
            "SiteUrl",
            value=f"https://{distribution.distribution_domain_name}/marketplace.html",
        )
        CfnOutput(self, "BrokerUrl", value=broker_api.url)


app = App()
region = app.node.try_get_context("region") or "ap-northeast-1"  # 默认日本
MarketplaceDemoStack(
    app,
    "MarketplaceDemo",
    env={"account": os.environ.get("CDK_DEFAULT_ACCOUNT"), "region": region},
)
app.synth()
