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
    # DynamoDB 硬限制:已有表一次 update 只能加/删 1 个 GSI;新表可在创建时一次建齐。
    # 默认建齐四个索引是生产基线;tenant_query.enabled 默认 true 依赖它们。已有表仍由
    # `scripts/checks/tenant-query-rollout.py` 兜住逐个加索引的限制。
    if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_user", True):
        tenants_table.add_global_secondary_index(
            index_name="gsi_tenant_user",
            partition_key=dynamodb.Attribute(
                name="tenant_user_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
    if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_host", True):
        tenants_table.add_global_secondary_index(
            index_name="gsi_host",
            partition_key=dynamodb.Attribute(
                name="host_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
    if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_status", True):
        tenants_table.add_global_secondary_index(
            index_name="gsi_status",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
    if (CFG.get("scaler", {}) or {}).get("add_gsi_tenant_rootfs", True):
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

    # for time-range queries; ts as sort key. DDB TTL auto-expires entries
    # after `audit.retention_days` (default 90).
    # Table name carries a per-deploy suffix derived from Aws.STACK_ID so
    # that `cdk destroy` + redeploy never collides with the RETAIN-ed
    # orphan table from the previous stack incarnation.
    audit_cfg = CFG.get("audit", {}) or {}
    audit_retention_days = int(audit_cfg.get("retention_days", 90))
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
        stream=(dynamodb.StreamViewType.NEW_IMAGE if audit_archive_enabled else None),
    )
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
    # Holds the KMS-envelope-encrypted gateway token + device identity (tenant_id
    # EncryptionContext). Rows are ROTATED (not deleted) on each tenant create.
    # whole life so rebuild/recover/restore months or years later can still read
    # back the original token/device identity. An expired read → openssl fallback →
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
        # deployment/scripts 下的文件都留不可变 VersionId,让 version-snapshots 表能按
        # 精确 VersionId 重建任意历史时刻的完整文件集(镜像+脚本一致)。开 versioning 后
        # 旧对象版本不随覆盖丢失——是 stateful 属性变更(RemovalPolicy 域),需人工评审。
        versioned=True,
    )

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

    # config-gated `security.clawpool_cmk_enabled` (R14.6 default TRUE → RSA 非对称
    # 凭据出入站开箱可用;显式 false 走存量兼容路径,byte-identical synth 保留)。
    # 踩过:default false 时 GET credentials 出站字段全空,demo 脚本要手翻 config
    # (memory outbound-cred-demo)。The general encrypt/decrypt base
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
    # 之上,而此前只有 prod 开 versioned=True → 非 prod 桶【根本没有版本】,于是
    # "多版本共存 + 生命周期分治" 在测试环境不成立,拿它做的恢复验证会假通过。
    # 真正让 prod 桶"销毁不了"的是 Object Lock(COMPLIANCE 保留期内连 root 都删不掉),
    # 不是 versioning —— 所以 Object Lock 留在 prod 分支,versioning 提到分支外。
    # 非 prod 仍是 DESTROY + auto_delete_objects,而 CDK 的 auto-delete provider 走
    # ListObjectVersions 逐版本删,开 versioning 后销毁能力不变。
    _backup_kwargs.update(versioned=True)
    if _is_prod_region:
        _backup_kwargs.update(
            removal_policy=RemovalPolicy.RETAIN,
            object_lock_enabled=True,  # Object Lock 要求桶已开版本控制(上面已开)
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
    #
    # resources 三处都是)。为什么一直报绿:对 assets 桶调
    # putBucketLifecycleConfiguration 是【合法调用】(桶在、IAM 匹配、语法合规)→
    # CustomResource 成功 → CloudFormation 绿灯。真机只读实证是**双重空转**:备份桶
    # GetBucketLifecycleConfiguration 返 NoSuchLifecycleConfiguration,而 assets 桶那条
    # `backups/` 规则下 0 对象 → `backup_retention_days` 从未在任何一侧生效。
    #
    # ⚠ 不改成 CDK 原生 `lifecycle_rules=`:上面那句注释的理由仍然成立 —— prod 桶是
    # RETAIN + Object Lock,inline 规则更新会在 prod 栈更新时失败。保持 AwsCustomResource。
    #
    # ⚠ assets 桶上那条历史 `backup-expiration` 规则不会被自动摘掉。原因不是下面缺
    # on_delete(已经补了),而是**当前已部署的那一版没有 on_delete**:开关缺省 false ⇒
    # 首次部署时这个资源被移出模板 ⇒ CFN 发 Delete,而已部署那版的属性里没有 Delete
    # 调用 ⇒ provider 什么都不做。对"BACKUP_BUCKET 未注入的旧单桶部署"
    # (backup-data.sh 会回落 assets 桶)留着它仍是想要的行为,故不在本 MR 里动。
    #
    # 规则分治(ADR §4.5.8):只有 Expiration 时 noncurrent 版本永不释放 —— 而桶现在
    # 全环境开 versioning,Expiration 在版本桶上只是插一个 delete marker,数据落到
    # noncurrent 版本里。三件事必须各自有归属:
    #   · Expiration.Days                     → current 版本到期
    #   · NoncurrentVersionExpiration         → 历史版本到期(缺它 = 只增不减)
    #   · Expiration.ExpiredObjectDeleteMarker→ 清掉版本全删完后剩下的孤儿 delete marker
    # ExpiredObjectDeleteMarker 与 Expiration.Days 不能同规则共存(S3 返 MalformedXML),
    # 故拆成两条规则。
    #
    # ⚠ NoncurrentDays 必须【严格大于】Object Lock 保留期,不能等于:prod 的
    # COMPLIANCE retention 用的是同一个 backup_retention_days,同天数 = 生命周期到期与
    # WORM 解锁撞在同一天的竞态,S3 会去删一个仍被 COMPLIANCE 锁住的版本并反复失败。
    # +1 天把两者错开。
    _backup_lifecycle = {
        "Rules": [
            {
                "ID": "backup-expiration",
                "Filter": {"Prefix": f"{CFG['s3']['backup_prefix']}/"},
                "Status": "Enabled",
                "Expiration": {"Days": CFG["s3"]["backup_retention_days"]},
                "NoncurrentVersionExpiration": {
                    "NoncurrentDays": CFG["s3"]["backup_retention_days"] + 1
                },
                # 备份是 pigz 流式 multipart 上传;中断的分片不属于任何版本,只靠这条清。
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
            },
            {
                "ID": "backup-expired-delete-markers",
                "Filter": {"Prefix": f"{CFG['s3']['backup_prefix']}/"},
                "Status": "Enabled",
                "Expiration": {"ExpiredObjectDeleteMarker": True},
            },
        ]
    }
    # ⛔ 默认【不部署】这条规则(`backup_lifecycle_enabled` 缺省 false)。
    #
    # 这不是保守,是防一次数据丢失 —— Codex 独立复审的唯一阻断项:规则此前是空转的,
    # **打对桶的那一刻它就从"从不生效"变成"立刻生效"**,而 `backup_retention_days`
    # 至今没定(ADR §8 待确认 #1,合规诉求)。用缺省 7 天在存量桶上首次生效的后果:
    #   · 桶已开 versioning ⇒ `Expiration.Days` 不是删数据,而是给 current 版本
    #     **插一个 delete marker**,对象随即变成 noncurrent;
    #   · `_resolve_backup`(`tenant_service.py:6917`)与 `list_all_backups`
    #     (`console_info.py:50`)都走 `list_objects_v2`,它只返 current 版本、跳过
    #     delete marker ⇒ **所有超过 7 天的恢复点当天就对应用不可见**,restore 直接 404;
    #   · **Object Lock 挡不住这一步**(创建 delete marker 不是删除被锁版本),所以
    #     生产也一样不可见,只是数据还在;非 prod 再过 NoncurrentDays 天就真删。
    #   · 最容易被打中的正是最需要恢复的租户:停机/挂起的租户不被 `_backup_loop` 备份
    #     (它只备 running),其最新备份必然一路变老、越过 7 天。
    #
    # 所以合并本 MR 对任何在役环境都是**零行为变化**(备份桶继续 `NoSuchLifecycleConfiguration`)。
    # 打开它是一次显式的人工决定,且必须与"定下合规保留天数"同时发生。
    if CFG["s3"].get("backup_lifecycle_enabled", False):
        cr.AwsCustomResource(
            self,
            "BackupLifecycle",
            install_latest_aws_sdk=False,
            on_create=cr.AwsSdkCall(
                service="S3",
                action="putBucketLifecycleConfiguration",
                parameters={
                    "Bucket": backup_bucket.bucket_name,
                    "LifecycleConfiguration": _backup_lifecycle,
                },
                physical_resource_id=cr.PhysicalResourceId.of("backup-lifecycle"),
            ),
            on_update=cr.AwsSdkCall(
                service="S3",
                action="putBucketLifecycleConfiguration",
                parameters={
                    "Bucket": backup_bucket.bucket_name,
                    "LifecycleConfiguration": _backup_lifecycle,
                },
                physical_resource_id=cr.PhysicalResourceId.of("backup-lifecycle"),
            ),
            # 开关必须是【双向】的(Codex 独立复审第 2 轮阻断项)。没有 on_delete 时,
            # `backup_lifecycle_enabled` 只能挡住"第一次激活":一旦有人开了、部署了,
            # 再把它翻回 false 只是把 CustomResource 从模板里摘掉 → CFN 发 Delete →
            # provider 无事可做 → **桶上那份会删对象的规则原封不动继续生效**。
            # 那样这个"安全开关"是个单向门,救不了误开。
            #
            # 补上 on_delete 后:关掉开关 = 真的把规则从桶上撤掉,回滚是一次 config
            # 改动而不是一次手工 S3 操作。`DeleteBucketLifecycle` 要的权限就是
            # `s3:PutLifecycleConfiguration`(AWS 文档原文),所以下面的 policy 不用改。
            #
            # ⚠ 它删的是备份桶【整份】lifecycle 配置(S3 没有"只删某条规则"的 API)。
            # 当前备份桶除本资源外没有任何其它 lifecycle 来源(真机核对:
            # NoSuchLifecycleConfiguration),这个前提成立;若将来有人从别处往备份桶加
            # 规则,关本开关会把那条一起摘掉。
            #
            # ⚠ 已知未闭合的边角(Codex 复审第 4 轮提出,**有意不在本 MR 里改**):
            # physical_resource_id 是常量 "backup-lifecycle",不含桶名。所以当
            # `backup_bucket_suffix` 变化(→ 桶被替换,prod 下旧桶 RETAIN 留存)时,
            # 本资源收到的是 Update 而不是 replace ⇒ on_update 把规则写到新桶,
            # **旧桶上那份规则不会被摘掉、也再没人管**;此后关开关也只清新桶。
            #
            # 两个候选修法要分开看,它们的机制不同:
            #
            # (a) **只把桶名编进 physical_resource_id** —— 这条我有明确反驳:那样做是让
            #     CFN 在【替换】时补发 Delete,而替换后 Delete 的 `ResourceProperties` 是
            #     **新**属性(旧的在 `OldResourceProperties` 里),而 AwsCustomResource 的
            #     provider 按 `ResourceProperties` 组装调用 ⇒ 很可能 `deleteBucketLifecycle`
            #     打到【新桶】、把刚建好的规则又抹掉,**比现状更糟**。
            #
            # (b) **连 construct id(逻辑资源 id)一起随桶名变** —— 机制完全不同,而且
            #     **很可能是对的**:那不是"替换",是"旧逻辑资源被移除 + 新逻辑资源新增",
            #     而移除路径的 Delete 用的是【该逻辑资源自己最后一次成功状态】的属性
            #     = 旧桶 ⇒ 正好清对。**我对 (a) 的反驳不适用于 (b),这点如实写在这里。**
            #
            # 那为什么 (b) 也不在本 MR 里做:① 同样需要真部署才能坐实(本机无 aws_cdk;
            # 部署被本轮明确禁止);② 它会让**所有既有环境**的这个逻辑资源经历一次
            # "删除 + 新建"转换,那本身是一条没验证过的迁移路径。在数据销毁路径上落一个
            # 没验证过的改动,比留一个写清楚的约束更危险 ⇒ 按"不确定就不猜"处理。
            # **后续单开的卡应当优先评估 (b),不要从 (a) 起手。**
            #
            # 缓解:该场景要求"开关曾被打开"+"之后改了 suffix"两件事同时发生,而开关缺省
            # 关闭、打开是人工闸 ⇒ 把"改 suffix 前先手工摘掉旧桶的 lifecycle"写进
            # config.yml.example 的打开前置清单(第 ④ 条),人在打开开关时必然读到。
            on_delete=cr.AwsSdkCall(
                service="S3",
                action="deleteBucketLifecycle",
                parameters={"Bucket": backup_bucket.bucket_name},
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        # Put 与 Delete 共用这一个 action(S3 没有独立的 Delete 权限)。
                        actions=["s3:PutLifecycleConfiguration"],
                        resources=[backup_bucket.bucket_arn],
                    ),
                ]
            ),
        )

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
