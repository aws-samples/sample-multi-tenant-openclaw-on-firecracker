# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/clients — 共享模块级状态唯一定义点(handler-split #132 Phase1)。

从 handler.py 机械搬迁,逐字不变:boto3 client / DDB 表句柄 / env 常量 /
条件建的 sqs client。其它 core 域 `from core.clients import ssm, tenants_table, ...`。
按 design.md 层间契约:本层是最底叶子,只 import stdlib + boto3/botocore,不 import 仓内任何东西。
facade:handler.py re-export 全部符号,旧 patch/调用路径全程有效。
"""

import json
import os
import boto3
from botocore.config import Config as _BotoConfig

# ── #565 G1-a:同步 invoke backup Lambda 专用的 botocore 配置(单一定义点)────────
#
# **为什么必须显式给**:三处同步备份调用点原先用裸 `boto3.client("lambda")`,吃 botocore
# 默认 `read_timeout=60`。而 backup 侧的真实上界是 **~305s** —— `backup/handler.py` 的
# `_ssm_run` 墙钟 = `sleep(5)` + `(300 // 3)` 轮 × `sleep(3)`,且 `backup-data.sh` 自己
# 写着「控制面给本脚本的预算是 300s」。于是任何超过 60s 的备份都会在【backup 侧仍在预算
# 内】时被调用侧掐掉,而那次备份**会继续跑完并写 S3** → 调用侧按 fail-closed 判失败并回滚
# → 上层看到失败、底层其实备成功了。这正是 #565 G1-a 要判的那条。
#
# **read_timeout 取值口径**(#565 G1 重算过一次,原值 330 已不够):
#
#   read_timeout = backup 侧 SSM 墙钟预算 300s
#                + 最后一次 get_command_invocation 自身的最坏耗时 ~71s
#                + 余量 ~49s
#                = 420s        (仍 < 两侧 Lambda 自身的 900s 外壳,不冲突)
#
# 那个 71s 是**算出来的**:#573 给两个 ssm client 都加了
# `retries={"max_attempts": 8, "mode": "adaptive"}`(防 SendCommand 节流毒 DLQ,那件事本身
# 是对的),于是单次调用被节流时最坏要等 7 次重试的指数退避。botocore `ExponentialBackoff`
# 的 `_MAX_BACKOFF=20`、基数 2 → `1+2+4+8+16+20+20 = 71s`(本机 botocore 1.43.66 逐项
# 实测 `delay_amount` 确认)。adaptive 的客户端 token-bucket 限速还在这之上,所以 420 是
# 「够用」而不是「上界证明」—— 真正的硬上界是 900s 外壳。
#
# **原值 330 为什么不够**:它按「backup 侧上界 = 305s」算,而那个 305 来自
# `sleep(5) + (300//3) 轮 × sleep(3)` —— **轮数**上限。#573 之后一轮可能 74s,那个算式就
# 不再是上界。本轮把两处 `_ssm_run`(`backup/handler.py` 与 `core/ssm_dispatch.py`)改成
# 真实墙钟 deadline,`timeout` 才重新成为可算的预算,这个 420 才有意义。
#
# 注:这个数只保证「不比 backup 侧先放弃」,**不代表业务死线**。
#
# **#565 G1 之后这条注解要重读一遍(值没变,依据变了)。** 业务死线现在真的落地了:同步备份
# 在 backup 侧的墙钟预算由**调用方按自己的死线档给定** —— delete 90s / suspend 90s /
# rebuild 55s(见 `create_deadline._EXEC_STEPS`),而撤离路径(`host_service`)不带预算、
# 回落 backup 侧默认 300s(它是运维动作,没有客户死线)。
#
# 于是 `read_timeout` 的角色变得更清楚:**它必须永远不是先放弃的那一个。** backup 侧到预算
# 就返回一个真实裁决(`success=False` + `OC_SSM_NO_VERDICT` 哨兵),而 ReadTimeout 只会给出
# 「不确定」——G1-a 的整个教训就是「不确定」被 fail-closed 读成「确定失败」的代价。所以下界是
# **最大可能的 backup 侧墙钟**(那条不带预算的撤离路径,300s)+ 最后一次
# `get_command_invocation` 最坏 71s = 371s。420 > 371 ✓,继续成立,不必改。
# 反过来说:**将来谁把 backup 侧的默认预算调大到 > 349s,这个 420 就不够了** —— 那条绑定
# 由 `tests/test_565_backup_sync_timeout_adversarial.py` 的取值层守着(它从源码算 backup 侧
# 上界再比这个数,改坏即红,已实测)。
#
# **retries 刻意取 0(只试一次)**,理由是重试比白等更坏:第二次 invoke 会撞
# `backup-data.sh` 的 per-tenant flock(`flock -w 30` → `exit 1`),于是 SSM Failed →
# backup 返回一个【成功的 HTTP 响应】携带 `success=False` → botocore 见到成功响应即停止
# 重试 → 调用侧在约 100s 拿到一个**错误的权威失败**,比真相到达得更快。重试把「不确定」
# 变成了「错误的确定」,而 fail-closed 会据此回滚一次本可成功的备份。
# 取 0 而不是 1:本机 botocore 1.43.66 实测真实尝试次数 —— `0 → 1 次`、`1 → 2 次`、
# `mode=standard + 1 → 2 次`、裸 client → **5 次**(≈ 5×60s + 退避 ≈ 311s 白等)。
BACKUP_SYNC_INVOKE_CONFIG = _BotoConfig(
    connect_timeout=10,
    read_timeout=420,
    retries={"max_attempts": 0},
)

ssm = boto3.client(
    "ssm", config=_BotoConfig(retries={"max_attempts": 8, "mode": "adaptive"})
)

s3 = boto3.client("s3")

asg_client = boto3.client("autoscaling")

sns = boto3.client("sns")

ddb = boto3.resource("dynamodb")

elbv2 = boto3.client("elbv2")

# (core/kms_envelope). The API path only VALIDATES/relays upstream ciphertext
# (guest zero-credential baseline: the Lambda has no kms:Decrypt); the host
# decrypts at VM launch. The client is here to keep boto3 construction in the
# singleton (no business code calls boto3.client directly), and for moto-backed
# unit tests of the envelope round-trip.
kms = boto3.client("kms")

# security.clawpool_cmk_enabled=true; empty otherwise (feature off → the API
# rejects any injected_credentials since it can't be encrypted against a key).
CLAWPOOL_CMK_ARN = os.environ.get("CLAWPOOL_CMK_ARN", "")

# so callers locally OAEP-encrypt env creds; the API never decrypts (host does). Empty
# when security.clawpool_cmk_enabled=false → GET /clawpool-rsa-public-key returns 404.
CLAWPOOL_RSA_CMK_ARN = os.environ.get("CLAWPOOL_RSA_CMK_ARN", "")

# 密文(tenant_id EncryptionContext)。控制面 mint、reveal 从这里读;host 侧不读该表
# tenants_table 是两条独立路径)。Absent → gate 该功能没启用(P1 未部署环境用),
# mint_gateway_token 会 fail-loud;这样避免"表没建、悄悄跳过"的假绿。
tenant_secrets_table = (
    ddb.Table(os.environ["TENANT_SECRETS_TABLE"])
    if os.environ.get("TENANT_SECRETS_TABLE")
    else None
)

tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])

hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

tenant_stats_table = (
    ddb.Table(os.environ["TENANT_STATS_TABLE"])
    if os.environ.get("TENANT_STATS_TABLE")
    else None
)

# env-gated:未部署 V2 的环境无此 env → None,pull-image 的 snapshot 分支 fail-loud。
version_snapshots_table = (
    ddb.Table(os.environ["VERSION_SNAPSHOTS_TABLE"])
    if os.environ.get("VERSION_SNAPSHOTS_TABLE")
    else None
)

# env-gated 同上:未部署本步的环境无此 env → None,core/image_jobs.py 全部降级成 no-op,
# 现有 live pull 行为不变(ADR §12 step1"不改变现有 live 路径")。
image_jobs_table = (
    ddb.Table(os.environ["IMAGE_JOBS_TABLE"])
    if os.environ.get("IMAGE_JOBS_TABLE")
    else None
)

# deploy/stack.py). gsi_owner partitions by owner_id (Cognito sub) for "my
# nodes"; gsi_tenant_user partitions by tenant_user_id for the external backend's
# per-user fleet management. Names must match the CDK index_name exactly.
GSI_OWNER = "gsi_owner"

GSI_TENANT_USER = "gsi_tenant_user"

# legacy deployments without GROUPS_TABLE simply skip the group-resolution
# branch in _resolve_effective_skills() and continue with broadcast behavior.
groups_table = (
    ddb.Table(os.environ["GROUPS_TABLE"]) if os.environ.get("GROUPS_TABLE") else None
)

audit_table = (
    ddb.Table(os.environ["AUDIT_TABLE"]) if os.environ.get("AUDIT_TABLE") else None
)

# PRD #54 — optional async batch-job ledger; absent → batch stays synchronous
batch_jobs_table = (
    ddb.Table(os.environ["BATCH_JOBS_TABLE"])
    if os.environ.get("BATCH_JOBS_TABLE")
    else None
)

# federation not configured; /tenantmatch returns 404 (front-end falls back to
# passing identity_provider explicitly). Partition key: platform_id (S).
tenant_idp_table = (
    ddb.Table(os.environ["TENANT_IDP_TABLE"])
    if os.environ.get("TENANT_IDP_TABLE")
    else None
)

AUDIT_TTL_DAYS = int(os.environ.get("AUDIT_TTL_DAYS", "90"))

# Empty string disables publishing (no-op).
NOTIFICATIONS_TOPIC_ARN = os.environ.get("NOTIFICATIONS_TOPIC_ARN", "")

# Per-host limits (from config.yml via env)
HOST_RESERVED_VCPU = int(os.environ.get("HOST_RESERVED_VCPU", 1))

HOST_RESERVED_MEM = int(os.environ.get("HOST_RESERVED_MEM", 2048))

CPU_OVERCOMMIT_RATIO = float(os.environ.get("CPU_OVERCOMMIT_RATIO", 1.0))

MEM_OVERCOMMIT_RATIO = float(os.environ.get("MEM_OVERCOMMIT_RATIO", 1.0))


def _parse_overcommit_by_family(raw: str) -> dict:
    """#430 — per-family 超卖比覆盖。env 传 JSON:{"m8g":{"cpu":2.0,"mem":1.0},...}。

    空/非法 → {}(全部回落全局 CPU/MEM_OVERCOMMIT_RATIO,即当前统一 1:4 行为)。
    非 dict 的顶层 JSON(list/str/number)一律视作未配置 —— 调度参数 fail-safe 回落
    默认,不因一个畸形环境变量让整个控制面拒绝放置(host_profile.ratios 侧对单条
    畸形 entry 另有兜底)。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        print(f"[clients] OVERCOMMIT_BY_FAMILY not valid JSON, ignoring: {raw!r}")
        return {}
    if not isinstance(parsed, dict):
        print(f"[clients] OVERCOMMIT_BY_FAMILY must be a JSON object, ignoring: {raw!r}")
        return {}
    return parsed


OVERCOMMIT_BY_FAMILY = _parse_overcommit_by_family(
    os.environ.get("OVERCOMMIT_BY_FAMILY", "")
)

AFFINITY_ENABLED = os.environ.get("AFFINITY_ENABLED", "false") == "true"

# 留空回落 host_profile.DEFAULT_FAMILY_ORDER。
FAMILY_ORDER = tuple(
    f.strip()
    for f in os.environ.get("FAMILY_ORDER", "r8g,r7g,m8g,m7g").split(",")
    if f.strip()
)

MEM_SAFETY_FLOOR_RATIO = float(os.environ.get("MEM_SAFETY_FLOOR_RATIO", 0.0))

MEM_CHECK_TTL_SEC = int(os.environ.get("MEM_CHECK_TTL_SEC", 300))

# #549 — host 心跳(last_seen)新鲜度门 TTL(秒)。last_seen 超期的 host 不再被选中放新租户
# 只在此处读默认,不接 stack/config —— 同步(_find_host)与队列(_snapshot_hosts)两条路径
# 共享同一 core.clients,默认一致就不会对"哪台算陈旧"产生 config 漂移。
HOST_SEEN_STALE_SEC = int(os.environ.get("HOST_SEEN_STALE_SEC", 600))

VM_DEFAULT_VCPU = int(os.environ.get("VM_DEFAULT_VCPU", 2))

VM_DEFAULT_MEM = int(os.environ.get("VM_DEFAULT_MEM", 4096))

VM_DATA_DISK_MB = int(os.environ.get("VM_DATA_DISK_MB", 2048))

VM_PORT_BASE = int(os.environ.get("VM_PORT_BASE", 18789))

VM_SUBNET_PREFIX = os.environ.get("VM_SUBNET_PREFIX", "172.16")

ASG_NAME = os.environ.get("ASG_NAME", "openclaw-hosts-asg")

ALB_LISTENER_ARN = os.environ.get("ALB_LISTENER_ARN", "")

VPC_ID = os.environ.get("VPC_ID", "")

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

QUOTAS_ENABLED = os.environ.get("QUOTAS_ENABLED", "false").lower() == "true"

QUOTAS_MAX_VCPU = int(os.environ.get("QUOTAS_MAX_VCPU", "0") or "0")

QUOTAS_MAX_MEM_MB = int(os.environ.get("QUOTAS_MAX_MEM_MB", "0") or "0")

QUOTAS_MAX_DATA_DISK_MB = int(os.environ.get("QUOTAS_MAX_DATA_DISK_MB", "0") or "0")

# Self-service: max openclaw nodes a single Cognito user may self-provision via
# POST /tenants/self (anti-abuse). Default 1 (one node per user); 0 = unlimited.
SELF_MAX_NODES_PER_USER = int(os.environ.get("SELF_MAX_NODES_PER_USER", "1") or "0")

# Firecracker can't snapshot a VM with an active balloon device, so live
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

# ── WI-002: end-to-end Cognito for the channel plane ──
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
#     PutParameter 退出热路径,消除 3 TPS ParamStore 限流墙 + 24KB 参数区上限)
DISPATCH_MODE = os.environ.get("DISPATCH_MODE", "push")

ASSIGNMENTS_TABLE = os.environ.get("ASSIGNMENTS_TABLE", "")

assignments_table = ddb.Table(ASSIGNMENTS_TABLE) if ASSIGNMENTS_TABLE else None

DISPATCH_PARAM_PREFIX = os.environ.get("DISPATCH_PARAM_PREFIX", "/openclaw/dispatch")

DISPATCH_MAX_PARALLEL = int(os.environ.get("DISPATCH_MAX_PARALLEL", "96") or "96")

# #661 —— dispatch_service 直接读 clients.X；若靠 scheduling 的 import 副作用创建属性，
# 属性存在性就依赖 import 顺序，四个 dispatch 测试组合运行时会在创建路径 AttributeError。
HOST_SELECTION_WEIGHT_ALPHA = float(
    os.environ.get("HOST_SELECTION_WEIGHT_ALPHA", "2.0")
)
HOST_SELECTION_SCORE_FLOOR = float(
    os.environ.get("HOST_SELECTION_SCORE_FLOOR", "0.5")
)
SPREAD_MAX_HOSTS_PER_BATCH = int(
    os.environ.get("SPREAD_MAX_HOSTS_PER_BATCH", "6")
)

# 的镜像值,默认 30)。仅用于 SSM executionTimeout 公式的分母:VM 现在经 host 级槽闸【排队限速】
# 起,有效并发是槽数(~30)不是装箱密度 DISPATCH_MAX_PARALLEL(96)。用 96 算会低估耗时 → 一批
# (都由 config vm.host_launch_slots 派生),这里给默认兜底。
DISPATCH_HOST_LAUNCH_CONCURRENCY = int(
    os.environ.get("DISPATCH_HOST_LAUNCH_CONCURRENCY", "30") or "30"
)

DISPATCH_INFLIGHT_TTL_SEC = int(
    os.environ.get("DISPATCH_INFLIGHT_TTL_SEC", "180") or "180"
)

DISPATCH_RETRY_BUDGET = int(os.environ.get("DISPATCH_RETRY_BUDGET", "3") or "3")

# ChangeMessageVisibility 把可见性从队列默认 960s(VisibilityTimeout)缩到此值,让 SQS 快速
# 重投(而非等 48min),receiveCount 自然递增到 maxReceiveCount 进 DLQ,预算走现有 _release_claims。
# 不发新消息 → 不重置 receiveCount、无 send/write 原子性窗口(区别于曾陷泥潭的"发新消息"方案)。
DISPATCH_UNPLACED_DELAY_BASE_SEC = int(
    os.environ.get("DISPATCH_UNPLACED_DELAY_BASE_SEC", "15") or "15"
)

# #522 P1-2 —— host 升级(refresh-rootfs/pull-image)期间被排除出装箱候选(_snapshot_hosts
# 只扫 active/idle)。此时新建租户找不到位子=unplaced,旧逻辑无差别烧 dispatch_retries →
# 超预算转终态 requires_intervention,host 升级完回 active/idle 后不自愈。此宽限秒数内(以
# host.upgrading_at 距今计),若 fleet 存在【新鲜】升级中的 host,则本轮 unplaced 视作瞬态,
# 走 no-budget 重投(不计预算、不缩 visibility),等升级完成再落位。升级卡死超过此值 → 退回
# 计预算行为,最终 requires_intervention(fail-loud,卡死升级是运维问题)。<=0 关此宽限。
DISPATCH_UPGRADE_GRACE_SEC = int(
    os.environ.get("DISPATCH_UPGRADE_GRACE_SEC", "900") or "900"
)

# #522 P1-2 收敛 backstop —— 必须与 dispatch 队列的 dlq_max_receive_count(dispatch_infra.py,
# 默认 3)保持一致。升级宽限走 no-budget 重投(不计 dispatch_retries),会让 SQS receiveCount 与
# dispatch_retries 脱钩:到消息进 DLQ 时 retries 可能 < DISPATCH_RETRY_BUDGET → 认领闸/release 的
# `>= budget` 终态标记打不出 → 租户永久卡 creating(消息静默进 DLQ)。故按【SQS 投递耗尽】
# (ApproximateReceiveCount >= 此值)直接收敛 requires_intervention(loud),不让宽限掩盖卡死。
DISPATCH_MAX_RECEIVE_COUNT = int(
    os.environ.get("DISPATCH_MAX_RECEIVE_COUNT", "3") or "3"
)

# #564 G6 —— lifecycle 队列的 maxReceiveCount,由 CDK 从队列 RedrivePolicy 的**同一个值**
# 注入(`lambdas.py` 的 `_LIFECYCLE_MAX_RECEIVE`)。消费侧靠它判断"这是不是最后一次投递":
# 最后一次失败之后消息就进 DLQ,而**进 DLQ 之前必须先把租户回写成终态** —— 不然 DLQ 里那条
# 消息成了唯一记录,租户永久停在 suspending/restoring/deleting,而客户只看到一个非终态。
#
# 默认 5 与 CDK 当前值一致,但**默认值不是真相** —— 它只是 env 缺失时的兜底(本地测试、
# 或队列没开时压根走不到消费侧)。真相在 CDK,漂移由 `tests/test_564_g6g7_dlq_backup.py`
# 的一条断言机械比对(正则从 `lambdas.py` 抓 `_LIFECYCLE_MAX_RECEIVE` 的字面值)。
# 形态照上面 dispatch 那条(#522)—— 同一条"投递耗尽即收敛"的思路,只是换了队列。
LIFECYCLE_MAX_RECEIVE_COUNT = int(
    os.environ.get("LIFECYCLE_MAX_RECEIVE_COUNT", "5") or "5"
)

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

# 根因:CAS 只门控 vcpu/mem,看不见磁盘;高密度下 /data 被存量活 VM 真实占满(data.ext4
# 是稀疏盘,随使用逐渐写实),新租户被派过来 `mkdir ${VM_DIR}` 报 No space → requires_intervention。
# host-agent 独立线程(_disk_report_loop)用 statvfs('/data') 写 host 表 avail_disk_mb;
# 装箱侧据此排除盘将满的 host。这是【软门】(装箱侧过滤,同 inflight_ok),不是 CAS 硬账本
# 默认 2048MB:留出至少一个默认 data 盘(VM_DATA_DISK_MB)的物理余量给新租户初始写入。
DISPATCH_HOST_DISK_MIN_FREE_MB = int(
    os.environ.get("DISPATCH_HOST_DISK_MIN_FREE_MB", "2048") or "2048"
)

# 陈旧,磁盘门对该 host 【跳过】(fail-open,退回旧行为)。防 host-agent 挂了/旧 host 从没
# 上报时,用过期读数误杀整台 host。默认 90s(3× 默认 poll 15s,容一次漏报);0 = 不校验新鲜度。
DISPATCH_DISK_REPORT_TTL_SEC = int(
    os.environ.get("DISPATCH_DISK_REPORT_TTL_SEC", "90") or "90"
)

# SQS visibility timeout — used to cap the SSM executionTimeout so a slow SSM
# invocation never "hangs" past the visibility window and gets double-processed.
DISPATCH_VISIBILITY_TIMEOUT_SEC = int(
    os.environ.get("DISPATCH_VISIBILITY_TIMEOUT_SEC", "900") or "900"
)

# CloudWatch — DispatchCircuitOpen metric. Lazy: only construct when metric
# actually needs to be emitted (keeps cold-start clean when the feature is off).
cloudwatch = boto3.client("cloudwatch") if DISPATCH_QUEUE_URL else None
