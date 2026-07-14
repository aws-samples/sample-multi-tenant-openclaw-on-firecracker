# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import (
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_actions as elbv2_actions,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    aws_cognito as cognito,
    aws_logs as logs,
    custom_resources as cr,
    Duration,
)


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
    # #212 R2 ALB 拆分:BFF authenticate-cognito rule 挂内网 ALB(拆分开)或
    # 公网 DashboardALB(拆分关,现状)。console_alb_listener 由 ha_edge 建 :443 HTTPS 时提供。
    console_alb = getattr(ctx, "console_alb", None)
    console_alb_listener = getattr(ctx, "console_alb_listener", None)
    _alb_split_enabled = bool(getattr(ctx, "_alb_split_enabled", False))
    # R15.2:BFF 读 SecureString shared-vkey 需 kms:Decrypt clawpool CMK
    # (仅 security.clawpool_cmk_enabled=true 时存在,同 lambdas.py/litellm.py pattern)。
    clawpool_cmk = getattr(ctx, "clawpool_cmk", None)

    # ── Observability: X-Ray tracing + log retention (same gate as lambdas.py) ──
    _obs_cfg = CFG.get("observability", {}) or {}
    _tracing_mode = (
        _lambda.Tracing.ACTIVE
        if _obs_cfg.get("tracing_enabled", True)
        else _lambda.Tracing.PASS_THROUGH
    )
    _log_retention_days = int(_obs_cfg.get("log_retention_days", 30))
    _log_retention = {
        7: logs.RetentionDays.ONE_WEEK,
        30: logs.RetentionDays.ONE_MONTH,
        90: logs.RetentionDays.THREE_MONTHS,
        365: logs.RetentionDays.ONE_YEAR,
    }.get(_log_retention_days, logs.RetentionDays.ONE_MONTH)

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
                    tracing=_tracing_mode,
                    log_group=logs.LogGroup(
                        self,
                        "PreTokenGenLogGroup",
                        log_group_name="/aws/lambda/openclaw-pretokengen",
                        retention=_log_retention,
                    ),
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
                    tracing=_tracing_mode,
                    log_group=logs.LogGroup(
                        self,
                        "PtgAttachLogGroup",
                        log_group_name="/aws/lambda/openclaw-ptg-attach",
                        retention=_log_retention,
                    ),
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
                    tracing=_tracing_mode,
                    log_group=logs.LogGroup(
                        self,
                        "PreTokenGenLogGroup",
                        log_group_name="/aws/lambda/openclaw-pretokengen",
                        retention=_log_retention,
                    ),
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
        # PoC 已真机验证(an internal PoC):BFF Lambda
        # 托管 console 静态文件 + /capi/* 后端代持 admin key(浏览器全程零真 key),
        # 登录门在 ALB authenticate-cognito(未登录 302 Cognito Hosted UI)。
        # #229: BFF 增加 /capi/obs-config 只读端点回显 ADOT + Fluent Bit 当前
        # S3 配置版本;IAM 只授 s3:GetObject on deployment/observability/*(无写)。
        _assets_bucket = getattr(ctx, "assets_bucket", None)
        # #212 R1.5:api.mode=private/both 时 BFF 需进 VPC 私有子网(fetch VPCE DNS)。
        # 默认 mode=edge → 不进 VPC(现状,零冷启惩罚)。BFF ENI 首建 Hyperplane 分钟级,
        # 复用后可控;控制台低频流量可接受(spec design.md 2)。
        from aws_cdk import aws_ec2 as _ec2

        _api_mode_for_bff = str(getattr(ctx, "_api_mode", "edge") or "edge").lower()
        _bff_in_vpc = _api_mode_for_bff in ("private", "both")
        _bff_kwargs = {}
        if _bff_in_vpc:
            _bff_kwargs["vpc"] = vpc
            _bff_kwargs["vpc_subnets"] = _ec2.SubnetSelection(
                subnet_type=_ec2.SubnetType.PRIVATE_WITH_EGRESS
            )
        _ctrl_api_base = (
            f"https://{api.rest_api_id}.execute-api.{self.region}.amazonaws.com/v1"
        )
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
            tracing=_tracing_mode,
            log_group=logs.LogGroup(
                self,
                "ConsoleBffLogGroup",
                log_group_name="/aws/lambda/openclaw-console-bff",
                retention=_log_retention,
            ),
            environment={
                # mode=private 下客户走 VPCE DNS(<api-id>-<vpce-id>.execute-api...);
                # 但 vpce_id 是 network_vpc token 无法字符串拼接进 env,由 setup.sh 部署后
                # 用 aws lambda update-function-configuration 注入实际 VPCE DNS 覆盖此值。
                # 默认 mode=edge 时此值即公网 execute-api,BFF 出 NAT 通过公网访问。
                "CTRL_API_BASE": _ctrl_api_base,
                # 真 key 不进 IaC/模板:部署后由 setup.sh 注入(同 config.js 旧路径)。
                "CTRL_API_KEY": "PLACEHOLDER_INJECT_AT_DEPLOY",
                # #229: 观测配置只读桶(handler.mjs handleObsConfig 拉这个)。
                "OBS_ASSETS_BUCKET": _assets_bucket.bucket_name
                if _assets_bucket
                else "",
                "API_MODE": _api_mode_for_bff,
            },
            **_bff_kwargs,
        )
        # #221 — trace viewer read-only X-Ray query APIs. Spec R6.3 / F7:
        # exactly four read actions; X-Ray read APIs are not resource-scopable,
        # so Resource = "*" is the tightest possible policy. No write action
        # is granted — the console viewer is read-only.
        console_bff_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:GetTraceSummaries",
                    "xray:BatchGetTraces",
                    "xray:GetServiceGraph",
                    "xray:GetTraceGraph",
                ],
                resources=["*"],
            )
        )
        # #229: 只读授权 —— s3:GetObject 限 deployment/observability/* 前缀。
        # 不给 s3:PutObject/DeleteObject(写下发端点未实现,安全红线留人工)。
        if _assets_bucket is not None:
            console_bff_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["s3:GetObject"],
                    resources=[
                        f"{_assets_bucket.bucket_arn}/deployment/observability/*"
                    ],
                )
            )
        # construct id 带 "V2":BFF 早期部署把这个 TG 挂在独立的 ConsoleBffALB 上,
        # 后来改挂共享的 DashboardALB。一个 target group 只能属于一个 ALB,CFN 在单次
        # 部署里没法把同一个物理 TG 从旧 ALB 迁到新 ALB(会报 ServiceLimitExceeded:
        # "target group cannot be associated with more than one load balancer")。
        # 换 construct id → CFN 建全新 TG 挂 DashboardALB、旧 TG 随旧 ALB 一起删,
        # 迁移一次过、且此后幂等。
        # R15.2:BFF 直读 SSM 默认值(litellm-host/shared-vkey/config-template/
        # rootfs-manifest-version)供 console 一屏可查+可下发。IAM 限四个具名参数,
        # 不给通配。SecureString(shared-vkey)读要 host role 能 Decrypt 的 CMK →
        # BFF 也要 kms:Decrypt 该 CMK(仅 clawpool_cmk_enabled 开时存在)。
        _ssm_defaults = [
            f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/litellm-host",
            f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/litellm-shared-vkey",
            f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/config-template",
            f"arn:aws:ssm:{self.region}:{self.account}:parameter/openclaw/rootfs-manifest-version",
        ]
        console_bff_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameters", "ssm:PutParameter"],
                resources=_ssm_defaults,
            )
        )
        if clawpool_cmk is not None:
            clawpool_cmk.grant_encrypt_decrypt(console_bff_fn.grant_principal)

        console_bff_tg = elbv2.ApplicationTargetGroup(
            self,
            "ConsoleBffTGV2",
            target_type=elbv2.TargetType.LAMBDA,
            targets=[elbv2_targets.LambdaTarget(console_bff_fn)],
        )
        # authenticate-cognito 只能挂 HTTPS listener,且要求带 secret 的 app
        # client(现有 ConsoleClient 是 generate_secret=False,不能复用)。
        # #212 R2:拆分开(alb_split.enabled=true) → 挂内网 ALB(ha_edge 已建 :443 listener);
        # 拆分关 → 挂公网 DashboardALB(现状,由 auth_cfg.bff_certificate_arn 起 :443)。
        _bff_cert_arn = auth_cfg.get("bff_certificate_arn", "")
        # 决定挂哪个 ALB / listener。拆分时优先内网 ALB(已由 ha_edge 建好 :443 with cert)。
        _use_split = (
            _alb_split_enabled
            and console_alb is not None
            and console_alb_listener is not None
        )
        _target_alb = console_alb if _use_split else alb
        if _use_split or _bff_cert_arn:
            _bff_host = auth_cfg.get("bff_domain") or _target_alb.load_balancer_dns_name
            _bff_client = user_pool.add_client(
                "ConsoleBffClient",
                generate_secret=True,  # ALB authenticate-cognito 硬要求
                o_auth=cognito.OAuthSettings(
                    flows=cognito.OAuthFlows(authorization_code_grant=True),
                    scopes=[cognito.OAuthScope.OPENID],
                    callback_urls=[f"https://{_bff_host}/oauth2/idpresponse"],
                ),
            )
            if _use_split:
                # 内网 ALB 的 :443 listener 由 ha_edge 建好(alb_split.console_alb_certificate_arn 提供的证书)。
                _bff_https = console_alb_listener
            else:
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
                    session_timeout=Duration.seconds(1800),
                    next=elbv2.ListenerAction.forward([console_bff_tg]),
                ),
            )
            # Logout + 审计需要 Cognito 坐标(#212 MR2)
            console_bff_fn.add_environment(
                "COGNITO_DOMAIN", cognito_outputs["CognitoDomain"]
            )
            console_bff_fn.add_environment(
                "COGNITO_CLIENT_ID", _bff_client.user_pool_client_id
            )
            console_bff_fn.add_environment(
                "BFF_LOGOUT_URI", f"https://{_bff_host}/console/"
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
    # #220 (R9): expose runtime Lambdas so build_alarms can wire per-function
    # Errors/Throttles alarms. _ptg_attach_fn is a one-shot custom resource on
    # deploy — skipped intentionally.
    ctx.console_bff_fn = locals().get("console_bff_fn")
    ctx.pretokengen_fn = locals().get("_ptg_fn")
