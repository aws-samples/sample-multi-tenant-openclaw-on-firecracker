# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import (
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_actions as elbv2_actions,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    aws_cognito as cognito,
    custom_resources as cr,
    Duration,
)

from stacks._bff_cidr import collect_bff_ingress_cidrs


def build_auth(self, ctx):
    """Build auth resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    alb = getattr(ctx, "alb", None)
    api = getattr(ctx, "api", None)
    api_fn = getattr(ctx, "api_fn", None)
    cf_distribution = getattr(ctx, "cf_distribution", None)
    console_host = getattr(ctx, "console_host", None)
    custom_domain = getattr(ctx, "custom_domain", None)
    dual_mode = getattr(ctx, "dual_mode", None)
    listener = getattr(ctx, "listener", None)
    m = getattr(ctx, "m", None)
    vpc = getattr(ctx, "vpc", None)

    # ========== Console Auth (Cognito) ==========
    auth_cfg = CFG.get("console_auth", {})
    cognito_outputs = {}

    # ── Exchange IdP federation (task #13/#14) ───────────────────────────
    # Config-gated OIDC identity provider that lets external users sign in to
    # the same Cognito User Pool via their existing exchange account. The
    # exchange's real OIDC endpoints are NOT hardcoded — they come from
    # config.yml (`exchange_idp`). Empty / disabled config = no provider
    # added, the pool stays COGNITO-only, fully backward compatible.
    #
    # Federation is transparent to the hub: hub still only verifies the
    # *Cognito*-issued id_token (zero-credential guest constraint unchanged).
    # The external user's stable id (an OIDC claim, default `sub`) is mapped
    # to a Cognito custom attribute `custom:tenant_user_id`; Cognito's own
    # auto-generated `sub` becomes the tenant `owner_id`.
    idp_cfg = CFG.get("exchange_idp", {}) or {}
    idp_enabled = bool(idp_cfg.get("enabled", False)) and bool(
        idp_cfg.get("issuer_url")
    )
    idp_provider_name = (idp_cfg.get("provider_name") or "ExchangeIdP").strip()
    idp_stable_claim = (idp_cfg.get("stable_id_claim") or "sub").strip()
    idp_custom_attr = "tenant_user_id"

    def _idp_client_secret_ref():
        """CloudFormation dynamic reference for the OIDC client secret so the
        plaintext NEVER lands in the synthesized template. Accepts a Secrets
        Manager secret name/ARN (`client_secret_secret`) + optional JSON key
        (`client_secret_json_key`). Returns a `{{resolve:secretsmanager:...}}`
        token resolved at deploy time, or "" when no secret is configured
        (Cognito allows an empty secret for public OIDC clients)."""
        secret_name = (idp_cfg.get("client_secret_secret") or "").strip()
        if not secret_name:
            return ""
        json_key = (idp_cfg.get("client_secret_json_key") or "").strip()
        if json_key:
            return (
                f"{{{{resolve:secretsmanager:{secret_name}:SecretString:{json_key}}}}}"
            )
        return f"{{{{resolve:secretsmanager:{secret_name}:SecretString}}}}"

    def _idp_attribute_mapping():
        """Map the exchange stable-id claim into Cognito custom:tenant_user_id
        (the identity-chain join key), optionally email. AttributeMapping is
        an immutable jsii struct — every field must go to the constructor."""
        mapping_kwargs = {
            "custom": {
                idp_custom_attr: cognito.ProviderAttribute.other(idp_stable_claim)
            },
        }
        if idp_cfg.get("map_email", True):
            mapping_kwargs["email"] = cognito.ProviderAttribute.other("email")
        return cognito.AttributeMapping(**mapping_kwargs)

    def _idp_request_method():
        m = (idp_cfg.get("attribute_request_method") or "GET").strip().upper()
        return (
            cognito.OidcAttributeRequestMethod.POST
            if m == "POST"
            else cognito.OidcAttributeRequestMethod.GET
        )

    if auth_cfg.get("enabled", False):
        existing_pool_id = auth_cfg.get("user_pool_id", "")

        # 1.3.4: callback URLs only target the console host (where the
        # operator actually logs in). In dual-mode, app_domain is NOT
        # listed here — the Cognito session cookie is therefore physically
        # scoped to console_domain and cannot be sent to per-tenant
        # dashboards on app_domain.
        #
        # chat 小程序(终端用户自助登录)与 console 同 CloudFront 同域(同
        # console_host,只是路径 /chat/index.html vs /console/index.html,见
        # CloudFront /chat/* behavior),故把 chat 回调一并列入 —— 它与 console
        # 共享同一 console_host 的 session cookie 域,不触动上面 app_domain 的
        # 跨域隔离设计。缺这条则 chat 自助登录回调被 Cognito redirect_mismatch
        # 拒(此前靠运行态手改 client 补,未随代码部署,现纳入 CDK 随重建继承)。
        callback_urls = [
            f"https://{console_host}/console/index.html",
            f"https://{console_host}/chat/index.html",
        ]
        # In legacy single-mode, also add the *.cloudfront.net default
        # so direct CF URL access still works during DNS migration.
        if not dual_mode and not custom_domain:
            pass  # console_host is already cf default domain
        elif not dual_mode and custom_domain:
            callback_urls.append(
                f"https://{cf_distribution.distribution_domain_name}/console/index.html"
            )
            callback_urls.append(
                f"https://{cf_distribution.distribution_domain_name}/chat/index.html"
            )

        if existing_pool_id:
            # Import the existing pool but recreate the domain + client as stack-owned resources.
            user_pool = cognito.UserPool.from_user_pool_id(
                self, "ConsoleUserPool", existing_pool_id
            )
            cognito_outputs["CognitoUserPoolId"] = existing_pool_id

            # Legacy prefix (no account suffix) matches what 1.1.x created,
            # so existing users' bookmarked Cognito URLs keep working.
            domain_prefix = "openclaw-console"
            cognito.CfnUserPoolDomain(
                self,
                "ConsoleDomain",
                user_pool_id=existing_pool_id,
                domain=domain_prefix,
            )
            # Exchange IdP federation on the imported pool (task #13/#14).
            # The imported pool's custom attribute `custom:tenant_user_id`
            # must already exist on it (an imported pool's schema is
            # immutable from CDK); a stack-owned pool gets it added below.
            _exchange_idp = None
            _supported_idps = ["COGNITO"]
            if idp_enabled:
                _exchange_idp = cognito.UserPoolIdentityProviderOidc(
                    self,
                    "ExchangeIdP",
                    user_pool=user_pool,
                    name=idp_provider_name,
                    client_id=idp_cfg.get("client_id", ""),
                    client_secret=_idp_client_secret_ref(),
                    issuer_url=idp_cfg["issuer_url"],
                    scopes=idp_cfg.get("scopes") or ["openid"],
                    attribute_request_method=_idp_request_method(),
                    attribute_mapping=_idp_attribute_mapping(),
                )
                _supported_idps.append(idp_provider_name)
                # #144 — this branch never wired Pre-Token-Generation, so
                # federated users' tokens carried custom:platform_id=None
                # forever (platform reporting/filter dead on this deploy
                # shape; NOT an authz face — auth.py:289 never uses
                # platform_id for decisions). from_user_pool_id returns an
                # interface proxy with no add_trigger and CFN has no
                # standalone LambdaConfig resource, so a provider-backed
                # custom resource calls UpdateUserPool. That API resets
                # every omitted field to defaults (API ref), hence the
                # handler describes → merges → overlays the trigger, and
                # fails the deploy loud when custom:tenant_user_id is
                # missing from the imported pool's (immutable) schema.
                _ptg_fn = _lambda.Function(
                    self,
                    "PreTokenGen",
                    function_name="openclaw-pretokengen",
                    runtime=_lambda.Runtime.PYTHON_3_12,
                    architecture=_lambda.Architecture.ARM_64,
                    handler="handler.handler",
                    code=_lambda.Code.from_asset("deploy/lambda/pretokengen"),
                    timeout=Duration.seconds(5),
                    memory_size=2048,
                )
                _ptg_fn.add_permission(
                    "CognitoInvoke",
                    principal=iam.ServicePrincipal("cognito-idp.amazonaws.com"),
                    source_arn=user_pool.user_pool_arn,
                )
                _ptg_attach_fn = _lambda.Function(
                    self,
                    "PtgAttach",
                    function_name="openclaw-ptg-attach",
                    runtime=_lambda.Runtime.PYTHON_3_12,
                    architecture=_lambda.Architecture.ARM_64,
                    handler="handler.handler",
                    code=_lambda.Code.from_asset("deploy/lambda/ptg_attach"),
                    timeout=Duration.seconds(30),
                    memory_size=2048,
                )
                _ptg_attach_fn.add_to_role_policy(
                    iam.PolicyStatement(
                        actions=[
                            "cognito-idp:DescribeUserPool",
                            "cognito-idp:UpdateUserPool",
                        ],
                        resources=[user_pool.user_pool_arn],
                    )
                )
                _ptg_provider = cr.Provider(
                    self,
                    "PtgAttachProvider",
                    on_event_handler=_ptg_attach_fn,
                )
                _ptg_attach = cdk.CustomResource(
                    self,
                    "PtgAttachTrigger",
                    service_token=_ptg_provider.service_token,
                    properties={
                        "UserPoolId": existing_pool_id,
                        "LambdaArn": _ptg_fn.function_arn,
                        "RequiredCustomAttr": idp_custom_attr,
                    },
                )
                _ptg_attach.node.add_dependency(_ptg_fn)
            cfn_client = cognito.CfnUserPoolClient(
                self,
                "ConsoleClient",
                user_pool_id=existing_pool_id,
                generate_secret=False,
                callback_ur_ls=callback_urls,
                logout_ur_ls=callback_urls,
                supported_identity_providers=_supported_idps,
                # authorization-code flow (+ PKCE, client-side) instead of
                # implicit: implicit returns only a 1h id_token and NO
                # refresh_token, so the SPA was kicked back to login every
                # hour. Code flow yields a refresh_token for silent renewal.
                allowed_o_auth_flows=["code"],
                allowed_o_auth_scopes=["openid", "email"],
                allowed_o_auth_flows_user_pool_client=True,
                explicit_auth_flows=[
                    "ALLOW_USER_PASSWORD_AUTH",
                    "ALLOW_USER_SRP_AUTH",
                    "ALLOW_REFRESH_TOKEN_AUTH",
                ],
                # 7-day refresh window (user's requirement) so a returning
                # user within a week refreshes silently and never re-logs in.
                # id/access tokens stay short-lived (Cognito hard-caps id at
                # 24h); the refresh_token is what carries the 7 days.
                refresh_token_validity=7,  # days (default unit)
                id_token_validity=60,  # minutes
                access_token_validity=60,  # minutes
                token_validity_units=cognito.CfnUserPoolClient.TokenValidityUnitsProperty(
                    refresh_token="days",
                    id_token="minutes",
                    access_token="minutes",
                ),
            )
            # The client may only list a provider that already exists.
            if _exchange_idp is not None:
                cfn_client.node.add_dependency(_exchange_idp)
            cognito_outputs["CognitoClientId"] = cfn_client.ref
            cognito_outputs["CognitoDomain"] = (
                f"{domain_prefix}.auth.{self.region}.amazoncognito.com"
            )
        else:
            # On a stack-owned pool we can add the `custom:tenant_user_id`
            # attribute the exchange IdP mapping writes into. Added only when
            # federation is enabled to keep the default pool minimal.
            _custom_attrs = None
            _ptg_fn = None
            if idp_enabled:
                _custom_attrs = {
                    idp_custom_attr: cognito.StringAttribute(
                        min_len=1, max_len=256, mutable=True
                    )
                }
                # #97 档A — Pre-Token-Generation Lambda: on federated login,
                # inject custom:tenant_user_id / custom:platform_id into the id_token
                # so the broker (POST /tenants) and hub (POST /hub/token) get a stable
                # tenant identity. Pure stdlib (no deps), fail-open (never blocks login).
                # Only wired when federation is enabled to keep the default pool minimal.
                _ptg_fn = _lambda.Function(
                    self,
                    "PreTokenGen",
                    function_name="openclaw-pretokengen",
                    runtime=_lambda.Runtime.PYTHON_3_12,
                    architecture=_lambda.Architecture.ARM_64,
                    handler="handler.handler",
                    code=_lambda.Code.from_asset("deploy/lambda/pretokengen"),
                    timeout=Duration.seconds(5),
                    memory_size=2048,
                )
            user_pool = cognito.UserPool(
                self,
                "ConsoleUserPool",
                user_pool_name="openclaw-console",
                self_sign_up_enabled=auth_cfg.get("self_sign_up", False),
                sign_in_aliases=cognito.SignInAliases(email=True),
                password_policy=cognito.PasswordPolicy(
                    min_length=8,
                    require_digits=True,
                    require_lowercase=True,
                ),
                custom_attributes=_custom_attrs,
                # #97 档A — wire Pre-Token-Gen trigger (only set when federation on)
                lambda_triggers=cognito.UserPoolTriggers(pre_token_generation=_ptg_fn)
                if _ptg_fn is not None
                else None,
                removal_policy=self._stateful_removal,
            )
            user_pool.add_domain(
                "ConsoleDomain",
                cognito_domain=cognito.CognitoDomainOptions(
                    # account_id suffix keeps the domain prefix globally
                    # unique across stacks/accounts and survives RETAIN
                    # cleanup races (the prefix is global, not regional).
                    domain_prefix=f"openclaw-console-{self.account}{self._gsuffix}",
                ),
            )
            # Exchange IdP federation on the new pool (task #13/#14).
            _exchange_idp = None
            _supported_l2_idps = [cognito.UserPoolClientIdentityProvider.COGNITO]
            if idp_enabled:
                _exchange_idp = cognito.UserPoolIdentityProviderOidc(
                    self,
                    "ExchangeIdP",
                    user_pool=user_pool,
                    name=idp_provider_name,
                    client_id=idp_cfg.get("client_id", ""),
                    client_secret=_idp_client_secret_ref(),
                    issuer_url=idp_cfg["issuer_url"],
                    scopes=idp_cfg.get("scopes") or ["openid"],
                    attribute_request_method=_idp_request_method(),
                    attribute_mapping=_idp_attribute_mapping(),
                )
                _supported_l2_idps.append(
                    cognito.UserPoolClientIdentityProvider.custom(idp_provider_name)
                )
            client = user_pool.add_client(
                "ConsoleClient",
                o_auth=cognito.OAuthSettings(
                    # authorization-code (+ PKCE) instead of implicit so the
                    # SPA receives a refresh_token for 7-day silent renewal.
                    flows=cognito.OAuthFlows(authorization_code_grant=True),
                    scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                    callback_urls=callback_urls,
                    logout_urls=callback_urls,
                ),
                supported_identity_providers=_supported_l2_idps,
                auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
                # 7-day refresh window; id/access short-lived (Cognito caps
                # id_token at 24h, so the refresh_token carries the 7 days).
                refresh_token_validity=Duration.days(7),
                id_token_validity=Duration.minutes(60),
                access_token_validity=Duration.minutes(60),
            )
            # The client must be created after the provider it references.
            if _exchange_idp is not None:
                client.node.add_dependency(_exchange_idp)
            cognito_outputs["CognitoUserPoolId"] = user_pool.user_pool_id
            cognito_outputs["CognitoClientId"] = client.user_pool_client_id
            cognito_outputs["CognitoDomain"] = (
                f"openclaw-console-{self.account}{self._gsuffix}.auth.{cdk.Stack.of(self).region}.amazoncognito.com"
            )

        # RBAC groups (issue #14): admin / operator / viewer.
        # Created on both new and existing pools so an imported pool also
        # gets the role groups. The handler maps `cognito:groups` claim →
        # role hierarchy (admin > operator > viewer).
        for group_name, description, precedence in (
            ("admin", "Full access — RBAC + CRUD + actions", 1),
            ("operator", "CRUD + lifecycle actions (no RBAC mgmt)", 2),
            ("viewer", "Read-only access", 3),
        ):
            cognito.CfnUserPoolGroup(
                self,
                f"Role{group_name.capitalize()}",
                user_pool_id=user_pool.user_pool_id,
                group_name=group_name,
                description=description,
                precedence=precedence,
            )

        # #187 P5 — WI-002 channel-plane machine-user app client 已随
        # channel/hub 数据面下线一并移除(ChannelMachineUserClient + cognito-idp
        # admin IAM + COGNITO_CHANNEL_CLIENT_ID env)。留下的 Cognito 段只服务
        # console RBAC(JWT 验签 + owner_id/RBAC 门),不再有 machine-user 铸造。

        # Fail-safe RBAC (1.5.0): inject the REAL, stack-owned Cognito ids so
        # the api Lambda can fetch JWKS and verify id_token signatures
        # (RS256). These override the construction-time placeholders — the
        # Cognito pool id is only known here, after the pool is created or
        # imported above. Without a genuine pool id the handler cannot
        # verify signatures and every request fails safe to `viewer`.
        api_fn.add_environment(
            "COGNITO_USER_POOL_ID", cognito_outputs.get("CognitoUserPoolId", "")
        )
        api_fn.add_environment(
            "COGNITO_CLIENT_ID", cognito_outputs.get("CognitoClientId", "")
        )
        # lifecycle consumer 跑同一 handler、做同样的 owner 验证(create_tenant
        # /tenant_action 经 _get_caller_identity),需同样的 Cognito pool/client id。
        if getattr(self, "_lifecycle_consumer", None) is not None:
            self._lifecycle_consumer.add_environment(
                "COGNITO_USER_POOL_ID",
                cognito_outputs.get("CognitoUserPoolId", ""),
            )
            self._lifecycle_consumer.add_environment(
                "COGNITO_CLIENT_ID", cognito_outputs.get("CognitoClientId", "")
            )

        # ── Task 9.2 (#149): Console BFF — 前端零 key ──────────────────
        # PoC 已真机验证(engineering/security/clawconsole-bff-poc):BFF Lambda
        # 托管 console 静态文件 + /capi/* 后端代持 admin key(浏览器全程零真 key),
        # 登录门在 ALB authenticate-cognito(未登录 302 Cognito Hosted UI)。
        # #217 — CTRL_API_KEY 部署时自动注入,根治"全量 deploy 后成占位符 → /capi 全 403"。
        # APIGW 随机生成的 admin key 明文 value 在 CFN 里 GetAtt 取不到(只有 id),故用
        # AwsCustomResource 部署时调 getApiKey(includeValue) 捞真值,再喂进 BFF env。
        # 全程在 cdk deploy 内闭环,不靠 setup.sh、不靠人手,裸 cdk deploy 也覆盖、幂等
        # (#250 的 setup.sh 部署后注入是带外兜底,与此互补;此处让 CDK 自身也自洽)。
        # data_hidden("value"):真 key 不落 custom-resource 的 CloudWatch 日志。
        # IAM 限这一把 key 的 ARN(apigateway:GET,最小权限)。
        _admin_key = getattr(ctx, "api_key", None)
        _ctrl_key_value = "PLACEHOLDER_INJECT_AT_DEPLOY"
        if _admin_key is not None:
            _key_arn = (
                f"arn:{self.partition}:apigateway:{self.region}::"
                f"/apikeys/{_admin_key.key_id}"
            )
            _get_key = cr.AwsSdkCall(
                service="APIGateway",
                action="getApiKey",
                parameters={"apiKey": _admin_key.key_id, "includeValue": True},
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"ctrl-api-key-value-{_admin_key.key_id}"
                ),
                output_paths=["value"],
                # 真 key 不落 custom-resource 的 CloudWatch 日志(隐藏响应数据)。
                logging=cr.Logging.with_data_hidden(),
            )
            _ctrl_key_cr = cr.AwsCustomResource(
                self,
                "CtrlApiKeyValue",
                on_create=_get_key,
                on_update=_get_key,
                install_latest_aws_sdk=False,
                policy=cr.AwsCustomResourcePolicy.from_statements(
                    [
                        iam.PolicyStatement(
                            actions=["apigateway:GET"], resources=[_key_arn]
                        )
                    ]
                ),
            )
            _ctrl_key_cr.node.add_dependency(_admin_key)
            _ctrl_key_value = _ctrl_key_cr.get_response_field("value")
        # #266 — 当 logging.enabled 时 BFF 进 VPC,才够得着 VPC-only 的 AOS 域查
        # vm/host 日志(observability.py 回填 AOS_ENDPOINT/AOS_SECRET_ARN + SG 入站)。
        # VPC 内经现有 3 NAT 出公网,control-plane API / Cognito / SSM / CloudWatch
        # 调用不断。private_dns 的 execute-api VPCE 只在 api.mode=private/both 建
        # (network_vpc.py:83),edge 模式(默认)不劫持 DNS,/capi 公网代理照走。
        _bff_in_vpc = (
            bool((CFG.get("logging", {}) or {}).get("enabled", False))
            and vpc is not None
        )
        _bff_vpc_kwargs = {}
        _bff_sg = None
        if _bff_in_vpc:
            _bff_sg = ec2.SecurityGroup(
                self,
                "ConsoleBffSg",
                vpc=vpc,
                description="console BFF Lambda ENIs - egress only (NAT + AOS 443)",
                allow_all_outbound=True,
            )
            _bff_vpc_kwargs = {
                "vpc": vpc,
                "vpc_subnets": ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                "security_groups": [_bff_sg],
            }
        console_bff_fn = _lambda.Function(
            self,
            "ConsoleBFF",
            function_name="openclaw-console-bff",
            runtime=_lambda.Runtime.NODEJS_20_X,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_lambda.Code.from_asset("deploy/console-bff"),
            timeout=Duration.seconds(30),
            memory_size=2048,
            **_bff_vpc_kwargs,
            environment={
                "CTRL_API_BASE": f"https://{api.rest_api_id}.execute-api.{self.region}.amazonaws.com/v1",
                # #217 — 部署时由 CtrlApiKeyValue custom resource 捞真值注入(见上)。
                # api_key 不可用时(理论上不会)回落占位符,行为同旧。
                "CTRL_API_KEY": _ctrl_key_value,
                # #217 — BFF 用 token username 查 Cognito 组得真实角色(ALB x-amzn-oidc-data
                # 不含 cognito:groups)。注入 pool id 供 AdminListGroupsForUser。空 → roleForUser
                # 回落 viewer(canWrite 全 false → 写操作入口全隐藏)。rebase 曾丢过此行(#209
                # docs 提交把 auth.py 回退掉),导致 console 全员降级 viewer、Pull 按钮消失。
                "USER_POOL_ID": cognito_outputs.get("CognitoUserPoolId", ""),
            },
        )
        # #217 — BFF 查用户组以判角色(canWrite 门控):ALB OIDC token 不带 groups,
        # 用 username 调 AdminListGroupsForUser。只读该动作,资源限本 user pool。
        console_bff_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:AdminListGroupsForUser"],
                resources=[user_pool.user_pool_arn],
            )
        )
        # #264 — BFF 系统默认值面板(GET/POST /capi/system/defaults, handler.mjs:425/460)
        # 读写这 4 个 /openclaw/ SSM 参数;role 原来只有 cognito 一条 → 生产全 AccessDenied
        # (真机实测 /capi/system/defaults + /capi/traces 全挂,热补后 200)。资源限 /openclaw/
        # 前缀非整账户通配。
        console_bff_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter"],
                resources=[
                    f"arn:{self.partition}:ssm:{self.region}:{self.account}:parameter/openclaw/*"
                ],
            )
        )
        # SecureString(litellm-shared-vkey)用 aws/ssm 托管 key,WithDecryption 需 kms:Decrypt。
        # 用 ViaService condition 限定只经 SSM 服务解密(最小权限,不必知道托管 key 的 key id)。
        console_bff_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"ssm.{self.region}.amazonaws.com"
                    }
                },
            )
        )
        # trace viewer(GET /capi/traces{,/detail,/map}, traces.mjs)。X-Ray 读 API 不支持
        # 资源级,Resource=* 是最小权限例外。
        console_bff_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:GetTraceSummaries",
                    "xray:BatchGetTraces",
                    "xray:GetServiceGraph",
                ],
                resources=["*"],
            )
        )
        # #266 — per-tenant Lambda log viewer(GET /capi/logs?source=lambda, logs.mjs)
        # 走 CloudWatch Logs Insights,按 tenant_id 过滤 /aws/lambda/openclaw-* 日志组。
        # StartQuery/GetQueryResults 是账户级异步查询 API(不支持资源级授权);
        # DescribeLogGroups 按前缀解析日志组名。Resource=* 是这类 API 的最小权限例外。
        console_bff_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:StartQuery",
                    "logs:GetQueryResults",
                    "logs:StopQuery",
                    "logs:DescribeLogGroups",
                ],
                resources=["*"],
            )
        )
        # #266 — 把 BFF fn / SG / in-vpc 标记挂 ctx,build_observability 回填 AOS
        # (vm/host 日志)端点 + secret 读权限 + AOS SG 入站(auth 先于 observability
        # 运行,AOS 域此刻还没建,故 AOS 相关接线延到那边做)。
        ctx.console_bff_fn = console_bff_fn
        ctx.console_bff_sg = _bff_sg
        ctx.console_bff_in_vpc = _bff_in_vpc
        console_bff_tg = elbv2.ApplicationTargetGroup(
            self,
            "ConsoleBffTG",
            target_type=elbv2.TargetType.LAMBDA,
            targets=[elbv2_targets.LambdaTarget(console_bff_fn)],
        )
        # authenticate-cognito 只能挂 HTTPS listener,且要求带 secret 的 app
        # client(现有 ConsoleClient 是 generate_secret=False,不能复用)。
        # DashboardALB 目前只有 :80(TLS 在 CloudFront 终结),所以 HTTPS
        # listener + 认证规则由 config 提供的【区域内】ACM 证书门控。
        _bff_cert_arn = auth_cfg.get("bff_certificate_arn", "")
        if _bff_cert_arn:
            _bff_host = auth_cfg.get("bff_domain") or alb.load_balancer_dns_name
            _bff_client = user_pool.add_client(
                "ConsoleBffClient",
                generate_secret=True,  # ALB authenticate-cognito 硬要求
                o_auth=cognito.OAuthSettings(
                    flows=cognito.OAuthFlows(authorization_code_grant=True),
                    scopes=[cognito.OAuthScope.OPENID],
                    callback_urls=[f"https://{_bff_host}/oauth2/idpresponse"],
                ),
            )
            _bff_https = alb.add_listener(
                "BffHTTPS",
                port=443,
                open=False,  # 同 :80 红线:绝不自动开 0.0.0.0/0
                certificates=[elbv2.ListenerCertificate.from_arn(_bff_cert_arn)],
                default_action=elbv2.ListenerAction.fixed_response(
                    404, content_type="text/plain", message_body="not found"
                ),
            )
            _bff_https.add_action(
                "ConsoleBffAuth",
                priority=10,
                conditions=[
                    elbv2.ListenerCondition.path_patterns(["/console/*", "/capi/*"])
                ],
                action=elbv2_actions.AuthenticateCognitoAction(
                    user_pool=user_pool,
                    user_pool_client=_bff_client,
                    user_pool_domain=cognito.UserPoolDomain.from_domain_name(
                        self,
                        "ConsoleBffDomainRef",
                        cognito_outputs["CognitoDomain"].split(".")[0],
                    ),
                    next=elbv2.ListenerAction.forward([console_bff_tg]),
                ),
            )
            # #255 — 建了 443 门必须给 ALB SG 补 443 入站白名单,否则门建了墙上没开
            # 洞、443 全超时(真机实测:手工建 443 listener 后仍需手动加 SG 才通)。
            # 白名单从 config console_auth.bff_ingress_cidrs 读(逗号分隔 CIDR),默认空
            # = 不开 443 入站(fail-safe)。绝不放 0.0.0.0/0(AWS 暴露红线);运营员
            # 填自家办公/VPN CIDR。校验(含 0.0.0.0/0 fail-loud)抽到 _bff_cidr(纯 stdlib,
            # 可脱离 CDK synth 单测,守暴露红线不变量)。
            for _c in collect_bff_ingress_cidrs(auth_cfg.get("bff_ingress_cidrs")):
                _bff_https.connections.allow_default_port_from(
                    ec2.Peer.ipv4(_c),
                    "console BFF 443 ingress allowlist (no 0.0.0.0/0, #255)",
                )
        # TODO(#149): 未配 bff_certificate_arn 时无 HTTPS listener,BFF Lambda
        # + TG 已就位但不接流量;补一张区域内 ACM 证书(config console_auth.
        # bff_certificate_arn)即接通。别把认证规则挂 :80(明文回传 session
        # cookie + CFN 直接拒绝 authenticate-cognito on HTTP)。

    # consumer 也需 ALB_LISTENER_ARN/VPC_ID(若 _add_alb_rule 被显式开)+ AgentCore
    # 等后置 env 与 api 对齐。这些 add_environment 默认对 consumer 无害(用到才读)。
    if getattr(self, "_lifecycle_consumer", None) is not None:
        self._lifecycle_consumer.add_environment(
            "ALB_LISTENER_ARN", listener.listener_arn
        )
        self._lifecycle_consumer.add_environment("VPC_ID", vpc.vpc_id)

    # --- Pack onto ctx ---
    ctx.cognito_outputs = locals().get("cognito_outputs")
