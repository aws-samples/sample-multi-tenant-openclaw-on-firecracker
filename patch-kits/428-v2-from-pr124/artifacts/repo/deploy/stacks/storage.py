# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_ssm as ssm,
    custom_resources as cr,
    Duration,
    Fn,
    RemovalPolicy,
)


def build_storage(self, ctx):
    """Build storage resources (mechanical transplant from stack.py, issue #87)."""
    # --- Unpack from ctx ---
    CFG = ctx.CFG
    _is_prod_region = getattr(ctx, "_is_prod_region", None)

    # ========== DynamoDB ==========
    # 控制面表 PITR(时间点恢复)。租户数据本身有 backup_fn→WORM 桶兜底,但
    # 控制面元数据(tenants/hosts/audit)误删/误改/坏写后无法回滚 —— 实跑
    # 5 张表 PITR 全 DISABLED(2026-06-30 巡检发现)。开 PITR 后 DynamoDB 维持
    # 35 天连续备份,可恢复到任意秒级时点。config 开关默认 true,短命的
    # batch-jobs(DESTROY+TTL)不开。开 PITR 对 PAY_PER_REQUEST 表只按备份存储
    # 量计费,不影响读写吞吐。
    # aws-cdk-lib 已 deprecated 布尔参数 point_in_time_recovery,改用
    # point_in_time_recovery_specification(可同时配 recovery_period_in_days,
    # 1-35,默认 35)。沿用新写法避免 synth deprecation warning。
    _ddb_cfg = CFG.get("dynamodb", {}) or {}
    _pitr_spec = dynamodb.PointInTimeRecoverySpecification(
        point_in_time_recovery_enabled=bool(
            _ddb_cfg.get("point_in_time_recovery", True)
        ),
        recovery_period_in_days=int(_ddb_cfg.get("recovery_period_in_days", 35)),
    )

    tenants_table = dynamodb.Table(
        self,
        "Tenants",
        table_name="openclaw-tenants",
        partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
        point_in_time_recovery_specification=_pitr_spec,
        time_to_live_attribute="inflight_ttl",
    )
    # PRD #50-58 — control-plane scale-out: reverse lookups "all nodes of a
    # user" without a full-table scan. PAY_PER_REQUEST means GSIs carry no
    # provisioned cost; a record only appears in a GSI when it has that
    # attribute, so legacy/api-key tenants without tenant_user_id simply don't
    # show up in gsi_tenant_user (we only index nodes that actually have an
    # owner). ProjectionType=ALL so list/batch read the full record without a
    # second round-trip back to the base table.
    tenants_table.add_global_secondary_index(
        index_name="gsi_owner",
        partition_key=dynamodb.Attribute(
            name="owner_id", type=dynamodb.AttributeType.STRING
        ),
        projection_type=dynamodb.ProjectionType.ALL,
    )
    # DynamoDB 硬限制:一次 update 只能加/删 1 个 GSI。现网表已有 gsi_owner
    # (ACTIVE),栈漂移导致 CFN 想一次补 gsi_owner+gsi_tenant_user 两个 → 报
    # "Cannot perform more than one GSI creation in a single update"(2026-06-29
    # deploy 实撞)。分两步:本次 deploy 只对齐 gsi_owner;gsi_tenant_user 由
    # config 开关 add_gsi_tenant_user=true 在**下一次单独 deploy** 加(per-user
    # 舰队反查 GET /users/{id}/* 用,缺它这些端点降级但不阻塞核心)。
    if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_user", False):
        tenants_table.add_global_secondary_index(
            index_name="gsi_tenant_user",
            partition_key=dynamodb.Attribute(
                name="tenant_user_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
    if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_host", False):
        tenants_table.add_global_secondary_index(
            index_name="gsi_host",
            partition_key=dynamodb.Attribute(
                name="host_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
    if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_status", False):
        tenants_table.add_global_secondary_index(
            index_name="gsi_status",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
    if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_rootfs", False):
        tenants_table.add_global_secondary_index(
            index_name="gsi_rootfs_version",
            partition_key=dynamodb.Attribute(
                name="q_rootfs_version", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

    hosts_table = dynamodb.Table(
        self,
        "Hosts",
        table_name="openclaw-hosts",
        partition_key=dynamodb.Attribute(
            name="instance_id", type=dynamodb.AttributeType.STRING
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
        point_in_time_recovery_specification=_pitr_spec,
    )

    # 1.4.0 (#62): per-tenant / per-group skill distribution.
    # When a tenant doesn't carry a `skills` list and isn't assigned a
    # `group`, the launch path falls back to the legacy "broadcast all
    # shared skills" behavior. Otherwise the effective set is computed
    # as tenant.skills ∪ group.skills (with unknown groups silently
    # dropped from the union — see api/handler.py::_resolve_effective_skills).
    groups_table = dynamodb.Table(
        self,
        "Groups",
        table_name="openclaw-groups",
        partition_key=dynamodb.Attribute(
            name="name", type=dynamodb.AttributeType.STRING
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
        point_in_time_recovery_specification=_pitr_spec,
    )

    # Issue #17 — Audit log table. Single-partition by design (`pk="audit"`)
    # for time-range queries; ts as sort key. DDB TTL auto-expires entries
    # after `audit.retention_days` (default 90).
    # Table name carries a per-deploy suffix derived from Aws.STACK_ID so
    # that `cdk destroy` + redeploy never collides with the RETAIN-ed
    # orphan table from the previous stack incarnation.
    audit_cfg = CFG.get("audit", {}) or {}
    audit_retention_days = int(audit_cfg.get("retention_days", 90))
    # Phase 3 (task #25 / CIS 2.2.2): optional customer-managed CMK for the
    # audit table. config-gated `audit.cmk_encryption` (default false). MUST
    # only be turned on for a FRESH account/first deploy — flipping it on an
    # existing RETAIN-ed table forces a replace (loses audit history), so the
    # switch documents that constraint. When on, mint a rotation-enabled CMK
    # and set CUSTOMER_MANAGED; when off, leave the table on the AWS-owned key
    # (byte-identical to the prior behavior → no replace on existing stacks).
    audit_cmk_enabled = bool(audit_cfg.get("cmk_encryption", False))
    audit_cmk = None
    if audit_cmk_enabled:
        audit_cmk = kms.Key(
            self,
            "AuditCMK",
            alias="alias/openclaw-audit-log",
            description="Encrypts the control-plane audit log table (CIS 2.2.2); customer-managed, rotation enabled",
            enable_key_rotation=True,
            removal_policy=self._stateful_removal,
        )
    # #32 — WORM archive path config. When `audit.worm_archive_enabled=true`:
    # ① 开 DDB Stream(NEW_IMAGE)让归档 Lambda 消费;② 建独立 audit-archive
    # WORM 桶(Object Lock COMPLIANCE + Versioned + RETAIN,连 root 都改不动/删
    # 不掉,retention = `worm_retention_years` 年,默认 7);③ 加
    # `gsi_audit_owner` 按 actor_owner_id 反查,让 GET /audit-log?owner=... 走
    # GSI 而不是全表扫描。dev/rebuildable region 走 DESTROY+auto-delete 降级
    # (照 backup 桶写法),生产区自动切 WORM。
    #
    # ⚠ 硬约束(与 audit_cmk 相同):首次开只能在 FRESH account——存量
    # RETAIN 审计表加 Stream / GSI 会强制 replace 丢历史。开关闭默认所以现有
    # 部署完全向后兼容(byte-identical synth)。
    audit_archive_enabled = bool(audit_cfg.get("worm_archive_enabled", False))
    audit_worm_retention_years = int(audit_cfg.get("worm_retention_years", 7))
    if audit_worm_retention_years <= 0:
        raise ValueError(
            f"audit.worm_retention_years must be positive, got {audit_worm_retention_years}"
        )
    # Aws.STACK_ID format: arn:aws:cloudformation:<region>:<acct>:stack/<name>/<uuid>
    # Take the first 5 hex chars of the UUID's leading segment as the suffix.
    stack_uuid = Fn.select(2, Fn.split("/", cdk.Aws.STACK_ID))
    audit_suffix = Fn.select(0, Fn.split("-", stack_uuid))
    audit_table = dynamodb.Table(
        self,
        "AuditLog",
        # Final name e.g. openclaw-audit-log-a1b2c3d4 (UUID first segment, 8 hex)
        table_name=Fn.join("-", ["openclaw-audit-log", audit_suffix]),
        partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
        sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.STRING),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute="expires_ttl",
        removal_policy=self._stateful_removal,
        point_in_time_recovery_specification=_pitr_spec,
        # CUSTOMER_MANAGED only when config opts in (fresh deploy); otherwise
        # the SDK default (AWS-owned key) — unchanged for existing stacks.
        encryption=(
            dynamodb.TableEncryption.CUSTOMER_MANAGED if audit_cmk is not None else None
        ),
        encryption_key=audit_cmk,
        # #32 — Stream enables the WORM archive path. Off → None (unchanged).
        stream=(dynamodb.StreamViewType.NEW_IMAGE if audit_archive_enabled else None),
    )
    # #32 — GSI on actor_owner_id lets /audit-log?owner=... query one tenant's
    # trail without a full-partition scan. Shares the column _audit_write already
    # stamps (owner_id from the caller identity), so no data-model migration.
    # DDB caveats:一次 update 只能加/删 1 个 GSI(见 tenants_table gate 上的
    # 792 撞过的坑),所以放在 worm_archive_enabled gate 下,配合 cmk_encryption
    # 同一 FRESH-deploy 约束——不 mix 已有 audit_table 上多 GSI 追加。
    if audit_archive_enabled:
        audit_table.add_global_secondary_index(
            index_name="gsi_audit_owner",
            partition_key=dynamodb.Attribute(
                name="actor_owner_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

    # PRD #54 — async batch jobs. A large bulk lifecycle op (>100 nodes, or
    # ?async) is recorded here, then a self-invoked worker processes it in
    # chunks and updates progress, so the client gets a 202 + job_id instead
    # of a synchronous call that would blow the 30s API-GW timeout. Idempotent
    # by job_id; TTL auto-expires finished job rows.
    batch_jobs_table = dynamodb.Table(
        self,
        "BatchJobs",
        table_name="openclaw-batch-jobs",
        partition_key=dynamodb.Attribute(
            name="job_id", type=dynamodb.AttributeType.STRING
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        time_to_live_attribute="expires_ttl",
        removal_policy=RemovalPolicy.DESTROY,
    )
    # #97 档A — external-platform → Cognito upstream-IdP routing map (SPEC/02 §2.7).
    # partition_key platform_id; rows {idp_provider_name, issuer_url, created_at}.
    # GET /tenantmatch reads it pre-login to route to the right federated IdP.
    # Rebuildable routing config (no tenant data) → DESTROY removal is fine.
    tenant_idp_table = dynamodb.Table(
        self,
        "TenantIdpMap",
        table_name="openclaw-tenant-idp-map",
        partition_key=dynamodb.Attribute(
            name="platform_id", type=dynamodb.AttributeType.STRING
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=RemovalPolicy.DESTROY,
    )
    # #187 P1 — per-tenant secret ciphertext store (SPEC/11-ENGINE-TRANSFORM F).
    # Holds the KMS-envelope-encrypted gateway token + device identity (tenant_id
    # EncryptionContext). Rows are ROTATED (not deleted) on each tenant create.
    # #353 — NO DDB TTL (design decision, direction A): the ciphertext persists for the tenant's
    # whole life so rebuild/recover/restore months or years later can still read
    # back the original token/device identity. An expired read → openssl fallback →
    # token mismatch → JDWS can't connect (the #290/#312 recover paths read this
    # table). delete_tenant no longer removes the row either. (Was a 15-min TTL then
    # 30 days; both dropped — the `time_to_live_attribute` and the code-side
    # `expires_at`/`device_expires_at` soft-expiry checks are all removed.)
    # Ciphertext-only — plaintext is never persisted anywhere (only travels through the
    # `aws kms encrypt` call from control-plane Lambda → SSM command line → host
    # `aws kms decrypt` fresh at each launch). RETAIN in prod (rebuildable ciphertext
    # but the row is what enables reveal, so accidental table wipe would 410-lock every
    # existing tenant until re-mint — not data loss but observable outage).
    tenant_secrets_table = dynamodb.Table(
        self,
        "TenantSecrets",
        table_name="openclaw-tenant-secrets",
        partition_key=dynamodb.Attribute(
            name="tenant_id", type=dynamodb.AttributeType.STRING
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
    )
    # #353 — 显式声明 TTL 【disabled】,而不是仅删除 timeToLiveAttribute。理由:从 CFN
    # 模板删掉 TimeToLiveSpecification 不会 disable 存量表上已启用的 TTL(存量表继续删
    # 带过期 expires_at 的行 → recover 读空 → openssl 回退 → token 不一致 → JDWS 连不上)。
    # 用 L1 escape hatch 声明 Enabled=false,让 CloudFormation 在存量表上真正调
    # UpdateTimeToLive 关掉 TTL(cdk deploy 直接生效,不依赖 setup.sh 脚本、不被绕过)。
    # 全新部署等价于本就无 TTL。CDK L2 Table 不支持显式 disable,故走 CfnTable override。
    # AttributeName 必带:CFN TimeToLiveSpecification 文档规定 AttributeName 在
    # "enabling TTL 或 TTL 已 enabled" 时为 conditional-required —— 存量表 TTL 正开着时
    # 只给 {Enabled:false} 会参数校验失败,disable 跑不成、TTL 继续生效。带上原属性名。
    _cfn_secrets = tenant_secrets_table.node.default_child
    _cfn_secrets.add_property_override(
        "TimeToLiveSpecification",
        {"AttributeName": "expires_at", "Enabled": False},
    )
    # ========== Parameter Registry + Recipient Keys (tenant-credential-contract) ==========
    param_registry_table = dynamodb.Table(
        self,
        "ParamRegistry",
        table_name="openclaw-param-registry",
        partition_key=dynamodb.Attribute(
            name="config_template", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
    )
    recipient_keys_table = dynamodb.Table(
        self,
        "RecipientKeys",
        table_name="openclaw-recipient-keys",
        partition_key=dynamodb.Attribute(
            name="scope", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
    )

    # ========== S3 Assets Bucket ==========
    # 安全加固(task #25):
    # - BLOCK_ALL: 打开全部 4 个桶级公网封锁开关。租户数据盘/skill/备份/镜像
    #   都在这个桶,绝不能意外公开(CIS S3 2.1.x)。CloudFront 走 OAC 签名访问,
    #   非匿名,全封不破链路。
    # - enforce_ssl: CDK 自动追加 aws:SecureTransport=false 的 Deny 到桶策略,
    #   强制 HTTPS(CIS 2.1.1 Requiring SSL),与 OAC 策略并存不冲突。
    assets_bucket = s3.Bucket(
        self,
        "Assets",
        bucket_name=f"openclaw-assets-{self.account}{self._gsuffix}",
        removal_policy=self._stateful_removal,
        auto_delete_objects=self._auto_delete,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        # #217 V2 版本模型(ADR 第9节):开 versioning,每次覆盖 deployment/rootfs、
        # deployment/scripts 下的文件都留不可变 VersionId,让 version-snapshots 表能按
        # 精确 VersionId 重建任意历史时刻的完整文件集(镜像+脚本一致)。开 versioning 后
        # 旧对象版本不随覆盖丢失——是 stateful 属性变更(RemovalPolicy 域),需人工评审。
        versioned=True,
    )

    # #217 V2 版本模型(ADR 第9节)· 文件版本快照表。一条 = 某个时刻整个 deployment/
    # 所有文件的完整清单;value(JSON 属性 files)= [{path, s3_version_id, etag}, ...]
    # 覆盖 rootfs 镜像 + scripts 脚本 + edge/litellm/monitoring。pull-image?snapshot_time
    # =<ISO> 照此按精确 VersionId 逐文件拉,达成「镜像与脚本整版一致 + 精确回滚」。
    # 主键 snapshot_time = ISO8601 时间字符串(owner 2026-07-14:human 可读 + 字典序=
    # 时间序;如 "2026-07-14T08:15:30Z")。从 Number 换 String 走了 orphan→手工删→重建
    # 三步(DDB 主键不可原地改 + RETAIN)。
    version_snapshots_table = dynamodb.Table(
        self,
        "VersionSnapshots",
        table_name="openclaw-version-snapshots",
        partition_key=dynamodb.Attribute(
            name="snapshot_time", type=dynamodb.AttributeType.STRING
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
        point_in_time_recovery_specification=_pitr_spec,
    )

    # #394 step1(ADR §4.4)· 持久化 pull Job 表。
    image_jobs_table = dynamodb.Table(
        self,
        "ImageJobs",
        table_name="openclaw-image-jobs",
        partition_key=dynamodb.Attribute(
            name="job_id", type=dynamodb.AttributeType.STRING
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
        point_in_time_recovery_specification=_pitr_spec,
        time_to_live_attribute="expires_at",
    )
    image_jobs_table.add_global_secondary_index(
        index_name="gsi_idempotency",
        partition_key=dynamodb.Attribute(
            name="instance_id", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(
            name="idempotency_key", type=dynamodb.AttributeType.STRING
        ),
    )
    image_jobs_table.add_global_secondary_index(
        index_name="gsi_host_created",
        partition_key=dynamodb.Attribute(
            name="instance_id", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(
            name="created_at", type=dynamodb.AttributeType.STRING
        ),
    )

    tenant_stats_table = dynamodb.Table(
        self,
        "TenantStats",
        table_name="openclaw-tenant-stats",
        partition_key=dynamodb.Attribute(
            name="id", type=dynamodb.AttributeType.STRING
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        removal_policy=self._stateful_removal,
        point_in_time_recovery_specification=_pitr_spec,
    )

    # ========== Tenant-Backup CMK + WORM backup bucket (insider-threat hardening) ==========
    # 威胁模型:租户 data.ext4 备份是真实资产数据。光放 assets 桶(默认 SSE-S3,
    # AWS 托管密钥)防不住「拥有该桶权限的 S3 管理员」——他能 GetObject 读明文、
    # PutObject 覆盖、DeleteObject 删除。企业级要堵的就是这个内部威胁面。两层独立机制:
    #
    # 1) 防篡改/防删 — S3 Object Lock COMPLIANCE 模式 + Versioning。AWS 官方:
    #    COMPLIANCE 模式下在保留期内**连 root 账户都不能删除/覆盖对象版本、不能缩短
    #    保留期**(WORM,经 Cohasset 评估满足 SEC 17a-4/CFTC/FINRA)。S3 管理员改不动备份。
    # 2) 防看明文 — 专用 CMK(SSE-KMS)+ key policy 权限分离。备份用本 CMK 加密;
    #    key policy 只授「备份/恢复执行者」(host_role / backup_fn)用密钥,**不授予
    #    泛 S3 桶管理员 kms:Decrypt**。纯 S3 管理员即便能 GetObject 也只拿到密文,解不开。
    #    (backup-data.sh 另叠客户端 envelope 加密做第三层纵深。)
    backup_cmk = kms.Key(
        self,
        "BackupCMK",
        alias="alias/openclaw-tenant-backup",
        description="Encrypts tenant data.ext4 backups; decrypt scoped to backup/restore role only",
        enable_key_rotation=True,
        removal_policy=self._stateful_removal,
    )

    # ========== ClawPool general-purpose CMK (#152) — credential injection ==========
    # config-gated `security.clawpool_cmk_enabled` (R14.6 default TRUE → RSA 非对称
    # 凭据出入站开箱可用;显式 false 走存量兼容路径,byte-identical synth 保留)。
    # 踩过:default false 时 GET credentials 出站字段全空,demo 脚本要手翻 config
    # (memory outbound-cred-demo)。The general encrypt/decrypt base
    # for platform secrets injected at tenant provision (#116/#118): an upstream
    # service (before the API Gateway) encrypts each credential with THIS key
    # (EncryptionContext bound to owner_id, see core/kms_envelope.py); the API only
    # relays the ciphertext; only the host decrypts it with its own IAM role at VM
    # launch. We do NOT fold audit/backup/audit-archive into this key — those have
    # independent lifecycles/compliance edges. This is the shared base for future
    # sensitive-data types, not another single-purpose key. Rotation + alias so the
    # ARN is stable across rotations; grant split below (host decrypt only).
    # R14.6 default-on:未显式配置时视为 true,让 RSA 非对称凭据出入站开箱可用。
    # 显式 false 才走存量兼容路径。
    clawpool_cmk_enabled = bool(
        (CFG.get("security", {}) or {}).get("clawpool_cmk_enabled", True)
    )
    clawpool_cmk = None
    clawpool_rsa_cmk = None
    if clawpool_cmk_enabled:
        clawpool_cmk = kms.Key(
            self,
            "ClawPoolCMK",
            alias="alias/clawpool-general",
            description="ClawPool general credential-injection CMK; decrypt scoped to host role, EncryptionContext bound to owner_id (#152)",
            enable_key_rotation=True,
            removal_policy=self._stateful_removal,
        )
        # tenant-credential-contract asymmetric-v1 (#149 task 1.3/8.2): RSA-4096
        # ENCRYPT_DECRYPT CMK. Upstream encrypts env creds with this key's PUBLIC
        # key (kms:GetPublicKey, offline OAEP-SHA256); only the host decrypts at VM
        # launch via kms:Decrypt (private key never leaves KMS — host holds no key).
        # NOTE: asymmetric CMKs CANNOT enable rotation (KMS rejects it), and
        # RSAES_OAEP_SHA_256 decrypt does NOT accept EncryptionContext (verified:
        # ValidationException) — so tenant binding is NOT KMS-level AAD; it is the
        # frozen injection plan (field↔target) + envelope key_id (scheme-B decision).
        clawpool_rsa_cmk = kms.Key(
            self,
            "ClawPoolRSACMK",
            alias="alias/clawpool-rsa",
            description="ClawPool asymmetric-v1 credential-injection CMK (RSA-4096 OAEP-SHA256); public key for upstream encrypt, host-only kms:Decrypt (#149)",
            key_spec=kms.KeySpec.RSA_4096,
            key_usage=kms.KeyUsage.ENCRYPT_DECRYPT,
            removal_policy=self._stateful_removal,
        )
        # Publish the RSA CMK ARN to SSM so the host reads it at init (into
        # /etc/platform.env) and passes it to cred-inject.sh as the --key-id for
        # `aws kms decrypt`. The envelope's logical key_id is NOT a KMS ARN; the
        # host uses this concrete ARN. (Mirrors /openclaw/litellm-host pattern.)
        ssm.StringParameter(
            self,
            "ClawPoolRsaCmkArnParam",
            parameter_name="/openclaw/clawpool-rsa-cmk-arn",
            string_value=clawpool_rsa_cmk.key_arn,
        )

    # WORM (Object Lock COMPLIANCE) is a production compliance control: in prod
    # (ap-southeast-1) keep it + RETAIN. A COMPLIANCE-locked bucket cannot be
    # emptied/deleted even by root, so a rebuildable dev region would never be
    # able to tear down — there we drop Object Lock and use DESTROY+auto-delete.
    # backup_bucket_suffix (default "") lets a redeploy dodge a name collision
    # with a prior WORM/Object-Lock bucket that can't be torn down until its
    # COMPLIANCE retention lapses. Empty = unchanged for every existing deploy.
    _backup_suffix = CFG["s3"].get("backup_bucket_suffix", "")
    _backup_kwargs = dict(
        bucket_name=f"openclaw-backups-{self.account}{self._gsuffix}{_backup_suffix}",
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        encryption=s3.BucketEncryption.KMS,
        encryption_key=backup_cmk,
        bucket_key_enabled=True,  # 降低 KMS 调用成本
    )
    if _is_prod_region:
        _backup_kwargs.update(
            removal_policy=RemovalPolicy.RETAIN,
            versioned=True,  # Object Lock 要求开版本控制
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.compliance(
                Duration.days(CFG["s3"]["backup_retention_days"])
            ),
        )
    else:
        _backup_kwargs.update(
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
    backup_bucket = s3.Bucket(self, "TenantBackups", **_backup_kwargs)

    # Lifecycle rule managed via CustomResource (RETAIN bucket won't update inline rules)
    cr.AwsCustomResource(
        self,
        "BackupLifecycle",
        install_latest_aws_sdk=False,
        on_create=cr.AwsSdkCall(
            service="S3",
            action="putBucketLifecycleConfiguration",
            parameters={
                "Bucket": assets_bucket.bucket_name,
                "LifecycleConfiguration": {
                    "Rules": [
                        {
                            "ID": "backup-expiration",
                            "Filter": {"Prefix": f"{CFG['s3']['backup_prefix']}/"},
                            "Status": "Enabled",
                            "Expiration": {"Days": CFG["s3"]["backup_retention_days"]},
                        }
                    ]
                },
            },
            physical_resource_id=cr.PhysicalResourceId.of("backup-lifecycle"),
        ),
        on_update=cr.AwsSdkCall(
            service="S3",
            action="putBucketLifecycleConfiguration",
            parameters={
                "Bucket": assets_bucket.bucket_name,
                "LifecycleConfiguration": {
                    "Rules": [
                        {
                            "ID": "backup-expiration",
                            "Filter": {"Prefix": f"{CFG['s3']['backup_prefix']}/"},
                            "Status": "Enabled",
                            "Expiration": {"Days": CFG["s3"]["backup_retention_days"]},
                        }
                    ]
                },
            },
            physical_resource_id=cr.PhysicalResourceId.of("backup-lifecycle"),
        ),
        policy=cr.AwsCustomResourcePolicy.from_statements(
            [
                iam.PolicyStatement(
                    actions=["s3:PutLifecycleConfiguration"],
                    resources=[assets_bucket.bucket_arn],
                ),
            ]
        ),
    )

    # ========== #32 Audit-archive WORM bucket (independent from backup bucket) ==========
    # 为什么单独一个桶(而不是复用 backup 桶):backup 桶装租户 data.ext4(体量大、
    # retention 走 CFG["s3"]["backup_retention_days"] 数十天量级 lifecycle),审计
    # 归档是控制面 CRUD 事件(体量小、retention 数年 SEC-17a-4 级)。权限面/生命
    # 周期/密钥分离 → 一个 S3 管理员事故不牵动另一个。
    #
    # 双层保护:
    # 1) 防篡改/防删 — S3 Object Lock COMPLIANCE + Versioning + RETAIN(prod)。
    #    COMPLIANCE 保留期内 root 都改不动/删不掉,连缩短 retention 都不行。
    # 2) 防看明文 — 独立 audit-archive CMK(SSE-KMS)。key policy 只授归档
    #    Lambda 的 role 用密钥;S3 管理员即便 GetObject 也拿到密文,解不开。
    audit_archive_bucket = None
    audit_archive_cmk = None
    if audit_archive_enabled:
        audit_archive_cmk = kms.Key(
            self,
            "AuditArchiveCMK",
            alias="alias/openclaw-audit-archive",
            description="Encrypts the WORM-archived audit trail; decrypt scoped to archive Lambda only",
            enable_key_rotation=True,
            removal_policy=self._stateful_removal,
        )
        _audit_archive_kwargs = dict(
            bucket_name=f"openclaw-audit-archive-{self.account}{self._gsuffix}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=audit_archive_cmk,
            bucket_key_enabled=True,
        )
        if _is_prod_region:
            _audit_archive_kwargs.update(
                removal_policy=RemovalPolicy.RETAIN,
                versioned=True,  # Object Lock 要求
                object_lock_enabled=True,
                object_lock_default_retention=s3.ObjectLockRetention.compliance(
                    Duration.days(365 * audit_worm_retention_years)
                ),
            )
        else:
            _audit_archive_kwargs.update(
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
            )
        audit_archive_bucket = s3.Bucket(self, "AuditArchive", **_audit_archive_kwargs)

    # NOTE: media-upload CORS for this bucket is configured later, after the
    # CloudFront distribution is created (search "AssetsCors"), because the
    # allowed-origin must be scoped to the real CloudFront domain.

    # --- Pack onto ctx ---
    ctx._pitr_spec = locals().get("_pitr_spec")
    ctx.assets_bucket = locals().get("assets_bucket")
    ctx.audit_archive_bucket = locals().get("audit_archive_bucket")
    ctx.audit_archive_cmk = locals().get("audit_archive_cmk")
    ctx.audit_archive_enabled = locals().get("audit_archive_enabled")
    ctx.audit_cfg = locals().get("audit_cfg")
    ctx.audit_retention_days = locals().get("audit_retention_days")
    ctx.audit_table = locals().get("audit_table")
    ctx.backup_bucket = locals().get("backup_bucket")
    ctx.backup_cmk = locals().get("backup_cmk")
    ctx.batch_jobs_table = locals().get("batch_jobs_table")
    ctx.clawpool_cmk = locals().get("clawpool_cmk")
    ctx.clawpool_rsa_cmk = locals().get("clawpool_rsa_cmk")
    ctx.groups_table = locals().get("groups_table")
    ctx.hosts_table = locals().get("hosts_table")
    ctx.param_registry_table = locals().get("param_registry_table")
    ctx.recipient_keys_table = locals().get("recipient_keys_table")
    ctx.tenant_idp_table = locals().get("tenant_idp_table")
    ctx.tenant_secrets_table = locals().get("tenant_secrets_table")
    ctx.tenants_table = locals().get("tenants_table")
    ctx.version_snapshots_table = locals().get("version_snapshots_table")  # #217 V2
    ctx.image_jobs_table = locals().get("image_jobs_table")  # #394 step1 pull Job
    ctx.tenant_stats_table = locals().get("tenant_stats_table")
