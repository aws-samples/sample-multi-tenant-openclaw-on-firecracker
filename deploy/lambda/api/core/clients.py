# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/clients — 共享模块级状态唯一定义点(handler-split #132 Phase1)。

从 handler.py 机械搬迁,逐字不变:boto3 client / DDB 表句柄 / env 常量 /
条件建的 sqs client。其它 core 域 `from core.clients import ssm, tenants_table, ...`。
按 design.md 层间契约:本层是最底叶子,只 import stdlib + boto3,不 import 仓内任何东西。
facade:handler.py re-export 全部符号,旧 patch/调用路径全程有效。
"""

import os
import boto3

ssm = boto3.client("ssm")

s3 = boto3.client("s3")

asg_client = boto3.client("autoscaling")

sns = boto3.client("sns")

ddb = boto3.resource("dynamodb")

elbv2 = boto3.client("elbv2")

# #152/#118 — KMS client for the credential-injection envelope helper
# (core/kms_envelope). The API path only VALIDATES/relays upstream ciphertext
# (guest zero-credential baseline: the Lambda has no kms:Decrypt); the host
# decrypts at VM launch. The client is here to keep boto3 construction in the
# singleton (no business code calls boto3.client directly), and for moto-backed
# unit tests of the envelope round-trip.
kms = boto3.client("kms")

# ClawPool general credential-injection CMK ARN (#152). Injected by CDK only when
# security.clawpool_cmk_enabled=true; empty otherwise (feature off → the API
# rejects any injected_credentials since it can't be encrypted against a key).
CLAWPOOL_CMK_ARN = os.environ.get("CLAWPOOL_CMK_ARN", "")

# #149 asymmetric-v1 — RSA-4096 CMK ARN. The API serves its PUBLIC key (kms:GetPublicKey)
# so callers locally OAEP-encrypt env creds; the API never decrypts (host does). Empty
# when security.clawpool_cmk_enabled=false → GET /clawpool-rsa-public-key returns 404.
CLAWPOOL_RSA_CMK_ARN = os.environ.get("CLAWPOOL_RSA_CMK_ARN", "")

# #187 P1 — 短寿命密文表(pk=tenant_id,TTL=expires_at=now+900),存 gateway token
# 密文(tenant_id EncryptionContext)。控制面 mint、reveal 从这里读;host 侧不读该表
# (host 走 SSM 位置 12 参数拿密文,和 #118 host 直读 injected_credentials from
# tenants_table 是两条独立路径)。Absent → gate 该功能没启用(P1 未部署环境用),
# mint_gateway_token 会 fail-loud;这样避免"表没建、悄悄跳过"的假绿。
tenant_secrets_table = (
    ddb.Table(os.environ["TENANT_SECRETS_TABLE"])
    if os.environ.get("TENANT_SECRETS_TABLE")
    else None
)

tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])

hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

# #217 V2 — 文件版本快照表(pull-image?snapshot_time 按此逐文件按精确 VersionId 拉)。
# env-gated:未部署 V2 的环境无此 env → None,pull-image 的 snapshot 分支 fail-loud。
version_snapshots_table = (
    ddb.Table(os.environ["VERSION_SNAPSHOTS_TABLE"])
    if os.environ.get("VERSION_SNAPSHOTS_TABLE")
    else None
)

# PRD #50-58 — control-plane scale-out GSIs on the tenants table (defined in
# deploy/stack.py). gsi_owner partitions by owner_id (Cognito sub) for "my
# nodes"; gsi_tenant_user partitions by tenant_user_id for the external backend's
# per-user fleet management. Names must match the CDK index_name exactly.
GSI_OWNER = "gsi_owner"

GSI_TENANT_USER = "gsi_tenant_user"

# 1.4.0 (#62) — per-tenant / per-group skill scoping. Optional table:
# legacy deployments without GROUPS_TABLE simply skip the group-resolution
# branch in _resolve_effective_skills() and continue with broadcast behavior.
groups_table = (
    ddb.Table(os.environ["GROUPS_TABLE"]) if os.environ.get("GROUPS_TABLE") else None
)

# Issue #17 — optional audit log; absent in legacy deployments
audit_table = (
    ddb.Table(os.environ["AUDIT_TABLE"]) if os.environ.get("AUDIT_TABLE") else None
)

# PRD #54 — optional async batch-job ledger; absent → batch stays synchronous
batch_jobs_table = (
    ddb.Table(os.environ["BATCH_JOBS_TABLE"])
    if os.environ.get("BATCH_JOBS_TABLE")
    else None
)

# #97 档A — optional external-platform → Cognito-IdP map (the design doc §2.7). Absent →
# federation not configured; /tenantmatch returns 404 (front-end falls back to
# passing identity_provider explicitly). Partition key: platform_id (S).
tenant_idp_table = (
    ddb.Table(os.environ["TENANT_IDP_TABLE"])
    if os.environ.get("TENANT_IDP_TABLE")
    else None
)

AUDIT_TTL_DAYS = int(os.environ.get("AUDIT_TTL_DAYS", "90"))

# Issue #13 — optional SNS topic for tenant lifecycle events.
# Empty string disables publishing (no-op).
NOTIFICATIONS_TOPIC_ARN = os.environ.get("NOTIFICATIONS_TOPIC_ARN", "")

# Per-host limits (from config.yml via env)
HOST_RESERVED_VCPU = int(os.environ.get("HOST_RESERVED_VCPU", 1))

HOST_RESERVED_MEM = int(os.environ.get("HOST_RESERVED_MEM", 2048))

CPU_OVERCOMMIT_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", 1.0))

MEM_OVERCOMMIT_RATIO = float(os.environ.get("MEM_OVERCOMMIT_RATIO", 1.0))

VM_DEFAULT_VCPU = int(os.environ.get("VM_DEFAULT_VCPU", 2))

VM_DEFAULT_MEM = int(os.environ.get("VM_DEFAULT_MEM", 4096))

VM_DATA_DISK_MB = int(os.environ.get("VM_DATA_DISK_MB", 2048))

VM_PORT_BASE = int(os.environ.get("VM_PORT_BASE", 18789))

VM_SUBNET_PREFIX = os.environ.get("VM_SUBNET_PREFIX", "172.16")

ASG_NAME = os.environ.get("ASG_NAME", "openclaw-hosts-asg")

ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")

VPC_ID = os.environ.get("VPC_ID", "")

# #187 转型:ENABLE_PER_TENANT_ALB_RULE + legacy_alb 全模块已下线,数据面走两级路由
# (ALB LOR → OpenResty edge → Redis 查表 → host DNAT → microVM:18789);ALB listener
# rule 硬上限 100 的历史坑随之消解。

# 控制面重构阶段1 — SQS lifecycle 队列(削峰)。LIFECYCLE_QUEUE_URL 配了即启用
# 异步入队路径:create/start/stop/delete 写 DDB desired-state + 入队 + 立即返 202,
# 不再同步等 SSM(治 p99 飙升 + 持续负载雪崩)。空 = 保持同步路径(向后兼容)。
LIFECYCLE_QUEUE_URL = os.environ.get("LIFECYCLE_QUEUE_URL", "")

# [hackathon] SQS client is also needed when only DISPATCH_QUEUE_URL is set
# (create-via-dispatch path).  Construct if either queue URL is present.
_DISPATCH_QUEUE_URL_BOOT = os.environ.get("DISPATCH_QUEUE_URL", "")
sqs = boto3.client("sqs") if (LIFECYCLE_QUEUE_URL or _DISPATCH_QUEUE_URL_BOOT) else None

# Phase 2 — route POST /tenants through the FIFO queue too (not just start/stop).
# Default OFF so the create path is unchanged until a deployment opts in (and the
# queue is actually deployed). When ON + queue present, a create-burst is shed
# onto SQS and drained at the consumer's reserved-concurrency rate.
CREATE_VIA_QUEUE = os.environ.get("CREATE_VIA_QUEUE", "false").lower() == "true"

# Issue #16 / #9 — quota ceilings (0 = unlimited; ENABLED=false → no checks)
QUOTAS_ENABLED = os.environ.get("QUOTAS_ENABLED", "false").lower() == "true"

QUOTAS_MAX_VCPU = int(os.environ.get("QUOTAS_MAX_VCPU", "0") or "0")

QUOTAS_MAX_MEM_MB = int(os.environ.get("QUOTAS_MAX_MEM_MB", "0") or "0")

QUOTAS_MAX_DATA_DISK_MB = int(os.environ.get("QUOTAS_MAX_DATA_DISK_MB", "0") or "0")

# Self-service: max openclaw nodes a single Cognito user may self-provision via
# POST /tenants/self (anti-abuse). Default 1 (one node per user); 0 = unlimited.
SELF_MAX_NODES_PER_USER = int(os.environ.get("SELF_MAX_NODES_PER_USER", "1") or "0")

# Firecracker can't snapshot a VM with an active balloon device, so live
# migrate is unavailable while balloon is on (issue #72). Reject early.
BALLOON_ENABLED = os.environ.get("BALLOON_ENABLED", "false").lower() == "true"

# ── 1.5.0 security hardening: Cognito JWT signature verification ──
# COGNITO_USER_POOL_ID is injected by CDK from the genuine, stack-owned pool
# (deploy/stack.py add_environment). Empty when console_auth is disabled — in
# which case signature verification is impossible and every Bearer token fails
# safe to `viewer`. AWS_REGION is provided by the Lambda runtime.
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")

COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")

COGNITO_REGION = os.environ.get("AWS_REGION", "") or os.environ.get(
    "AWS_DEFAULT_REGION", ""
)

# ── : end-to-end Cognito for the channel plane ──
# The app client (public, USER_PASSWORD_AUTH) the per-tenant machine-user signs
# in with. Injected by CDK from the stack-owned pool. Empty = channel Cognito
# DISABLED → create_tenant keeps minting the legacy HMAC channel_secret only
# (graceful rollout: nothing changes until the stack provisions this client).
COGNITO_CHANNEL_CLIENT_ID = os.environ.get("COGNITO_CHANNEL_CLIENT_ID", "")

# Fall-back role for requests with NO Bearer token (API-key-only path).
# "viewer" = least privilege (fail-safe). Trusted automation that needs write
# access must present a Cognito id_token.
DEFAULT_NO_JWT_ROLE = os.environ.get("DEFAULT_NO_JWT_ROLE", "viewer")

# RBAC role-gating is its own switch, independent of console_auth (Cognito login).
# SECURITY (go-live A2): default is now ON — owner/role checks are enforced by
# default so a production deploy is least-privilege out of the box. A demo/dev
# deploy that genuinely wants the old open behavior must set RBAC_ENABLED=false
# EXPLICITLY (config console_auth.rbac_enabled=false). Absent/unset → enforced.
RBAC_ENABLED = os.environ.get("RBAC_ENABLED", "true").lower() == "true"

# ── Go-live A1: external authorization (tenant↔user mapping write-authority外置) ──
# When EXTERNAL_AUTHZ is on, the "who may use which tenant" mapping is NOT derived
# by us (we stop implicitly owning a tenant by whoever's Cognito sub created it);
# instead the external backend is the WRITE AUTHORITY and pushes grants/revokes via
# the HMAC-signed POST /external/authz endpoint. Our DynamoDB then only CACHES that
# authoritative mapping (authorized_users), and a user's access is exactly what the
# external backend authorized — never something we minted. Default OFF (current
# behavior: creator owns the tenant). EXTERNAL_AUTHZ_SECRET is the shared HMAC key
# the external backend signs with (Secrets Manager-backed env; never logged).
EXTERNAL_AUTHZ = os.environ.get("EXTERNAL_AUTHZ", "false").lower() == "true"

EXTERNAL_AUTHZ_SECRET = os.environ.get("EXTERNAL_AUTHZ_SECRET", "")

# clock-skew window for the signed request timestamp (seconds)
EXTERNAL_AUTHZ_TS_WINDOW = int(os.environ.get("EXTERNAL_AUTHZ_TS_WINDOW", "300"))

# API-key caller's owner identity (design doc stays in handler.py near
# _get_caller_identity until the auth domain moves in T1.9).
API_KEY_OWNER = "api-key"

# ── Hackathon: SQS dispatch (see SPEC/specs/sqs-dispatch/interfaces.md) ──
# Standard SQS queue for packed-batch dispatch. Empty = feature OFF (create
# still falls through to CREATE_VIA_QUEUE FIFO or synchronous path).
DISPATCH_QUEUE_URL = os.environ.get("DISPATCH_QUEUE_URL", "")

# push=聚合SSM+ParamStore分片(默认,回退用), pull=写 assignments 表让 host-agent 5s 轮询自取(二期),
# ddb=写 assignments 表 + 一条 --from-ddb SSM 叫醒 host 自查表(一期默认载体,#73:
#     PutParameter 退出热路径,消除 3 TPS ParamStore 限流墙 + 24KB 参数区上限)
DISPATCH_MODE = os.environ.get("DISPATCH_MODE", "push")

ASSIGNMENTS_TABLE = os.environ.get("ASSIGNMENTS_TABLE", "")

assignments_table = ddb.Table(ASSIGNMENTS_TABLE) if ASSIGNMENTS_TABLE else None

DISPATCH_PARAM_PREFIX = os.environ.get("DISPATCH_PARAM_PREFIX", "/openclaw/dispatch")

DISPATCH_MAX_PARALLEL = int(os.environ.get("DISPATCH_MAX_PARALLEL", "96") or "96")

DISPATCH_INFLIGHT_TTL_SEC = int(
    os.environ.get("DISPATCH_INFLIGHT_TTL_SEC", "180") or "180"
)

DISPATCH_RETRY_BUDGET = int(os.environ.get("DISPATCH_RETRY_BUDGET", "3") or "3")

# 认领标记的死锁回收阈值:claim 打上后消费中途炸批,消息重投时旧 claim 超过
# 该秒数即视为残留可接管(Powertools INPROGRESS 超时释放同款语义)。必须明显
# 大于单次消费 Lambda 的最长执行时间,防止把"还在干活的赢家"的 claim 抢走。
DISPATCH_CLAIM_STALE_SEC = int(
    os.environ.get("DISPATCH_CLAIM_STALE_SEC", "300") or "300"
)

# Circuit break: consecutive SSM SendCommand failures in one invocation ≥ this
# → whole batch to batchItemFailures + emit DispatchCircuitOpen metric.
DISPATCH_CIRCUIT_THRESHOLD = int(
    os.environ.get("DISPATCH_CIRCUIT_THRESHOLD", "3") or "3"
)

# Per-VM launch budget (seconds) used to derive the aggregate SSM command
# executionTimeout. host-parallelism defaults to DISPATCH_MAX_PARALLEL.
DISPATCH_PER_VM_BUDGET_SEC = int(
    os.environ.get("DISPATCH_PER_VM_BUDGET_SEC", "8") or "8"
)

# SQS visibility timeout — used to cap the SSM executionTimeout so a slow SSM
# invocation never "hangs" past the visibility window and gets double-processed.
DISPATCH_VISIBILITY_TIMEOUT_SEC = int(
    os.environ.get("DISPATCH_VISIBILITY_TIMEOUT_SEC", "900") or "900"
)

# CloudWatch — DispatchCircuitOpen metric. Lazy: only construct when metric
# actually needs to be emitted (keeps cold-start clean when the feature is off).
cloudwatch = boto3.client("cloudwatch") if DISPATCH_QUEUE_URL else None
