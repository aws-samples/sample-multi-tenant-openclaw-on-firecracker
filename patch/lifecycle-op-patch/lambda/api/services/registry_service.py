# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""registry_service — Parameter_Registry 数据层(tenant-credential-contract)。

DDB 表 `openclaw-param-registry`(env PARAM_REGISTRY_TABLE):
  pk = config_template
  sk = "snapshot#<version>"(不可变快照行,含 entries)| "current"(指针行,current_version)

快照只追加从不改;current 指针用事务原子推进(put snapshot + 条件写 current),
回滚 = 只移指针。读路径单次 Query 全分区,内存里按指针选中快照,不做长缓存。
"""

import hashlib
import json
import os
import re

from boto3.dynamodb.conditions import Key

from core import clients
from core.utils import _now

TABLE_NAME = os.environ.get("PARAM_REGISTRY_TABLE", "openclaw-param-registry")

# 空 config_template 映射到的 shipped 模板分区名(与 tenant_service `config_template
# or "default"` / dispatch 同一约定)。只有它随部署自动补种;其它模板名走 admin
# POST /registry/{tpl} 发布,无指针仍 fail-loud(挡错名/typo)。
DEFAULT_TEMPLATE = "default"

# shipped default 模板的预置 Registry_Entries(publish_snapshot 的种子输入)
#
# R14.2 llm_key 注入契约(文档化,防再踩新加坡 kpweqnkwm9 2026-07-11 的 400):
#   llm_key 与业务凭据走同一条 injected_parameters 通道、同一套 envelope 校验
#   (envelope._validate_injected_parameters_v2)。**接受两种形态,拒第三种**:
#     ① `enc:v1:` 信封 → owner_id 绑定加密路径(与业务凭据同等 owner 隔离,首选)
#     ② 裸 base64(≤8192)→ 原值透传、**无 owner 绑定**;仅可承载自隔离的 opaque
#        值(如 per-tenant mint 的 litellm vkey),SHALL NOT 放需 owner 隔离的敏感值
#     ③ 原始 `sk-...` key(既非 enc:v1: 也非合法 base64)→ 400 must be base64(真机
#        撞的就是这个:上游直接塞原始 key。上游要么 base64 编码,要么走 enc:v1:)
#   空值走下方 empty_fallback=LITELLM_SHARED_VKEY(平台 shared vkey,仅上游未传时兜底,
#   优先级低于上游传入;见 launch-vm.sh:474 _APIKEY per-tenant > shared)。
SHIPPED_DEFAULT_ENTRIES = {
    "llm_key": {
        "param_class": "config",
        "injection_target": "models.providers.litellm.apiKey",
        "sensitive": True,
        "required": False,
        "empty_fallback": "LITELLM_SHARED_VKEY",
    },
    # 客户自带 AI 网关解耦:让调用方 per-tenant 传自己的 litellm/OpenAI-兼容 baseUrl
    #   (尤其自建 http 模式网关)。此前 baseUrl 只走全平台共享的 LITELLM_HOST(SSM),
    # 走 llm_key 同一条 injected_parameters/config-class 通道,注入到 openclaw.json 的
    #   models.providers.litellm.baseUrl(host oc_inject_config_from_plan 幂等写)。
    # sensitive=false:baseUrl 是端点地址不是密钥,明文注入(校验层对非敏感 config 值
    #   放行明文,不强求 base64/enc:v1: — 否则 http://host:4000/v1 会被 base64 门拒)。
    # 空值 → empty_fallback=LITELLM_HOST_DEFAULT:host 回退平台全局 LITELLM_HOST
    "llm_base_url": {
        "param_class": "config",
        "injection_target": "models.providers.litellm.baseUrl",
        "sensitive": False,
        "required": False,
        "empty_fallback": "LITELLM_HOST_DEFAULT",
    },
    "api_key": {
        "param_class": "env",
        "injection_target": "EXCHANGE_API_KEY",
        "sensitive": True,
        "required": False,
    },
    "api_secret_key": {
        "param_class": "env",
        "injection_target": "EXCHANGE_API_SECRET_KEY",
        "sensitive": True,
        "required": False,
    },
    "subaccount_api_key": {
        "param_class": "env",
        "injection_target": "EXCHANGE_SUBACCOUNT_API_KEY",
        "sensitive": True,
        "required": False,
    },
    "subaccount_api_secret_key": {
        "param_class": "env",
        "injection_target": "EXCHANGE_SUBACCOUNT_API_SECRET_KEY",
        "sensitive": True,
        "required": False,
    },
}

# allow-list: passing it never authorizes a config.  The target OpenClaw binary
# remains the authoritative validator in launch-vm.sh's pre-rebuild probe.
FORBIDDEN_BY_PIN = {
    "2026.2.26": {
        "agents.defaults.heartbeat.isolatedSession",
        "agents.defaults.heartbeat.lightContext",
        "agents.defaults.compaction.midTurnPrecheck",
        "agents.defaults.compaction.maxActiveTranscriptBytes",
        "plugins.entries.sentinel-guard.hooks.allowConversationAccess",
    },
    "2026.6.11": set(),
    "2026.7.1-2": set(),
}


def _table():
    return clients.ddb.Table(TABLE_NAME)


# 注意:事务走 clients.ddb.meta.client(resource 派生的 client 自带类型注入器),
# TransactItems 直接传 Python 原生类型,不要手动 TypeSerializer(会双重序列化炸掉)。


def _query_all(config_template):
    """单分区全行(快照数量级小,单次 Query 足够;ponytail: 不分页,超 1MB 再说)。"""
    resp = _table().query(
        KeyConditionExpression=Key("config_template").eq(config_template),
        ConsistentRead=True,
    )
    return resp.get("Items", [])


def ensure_default_seeded():
    """幂等补种 shipped `default` 模板的 v1 快照 + current 指针。

    根因(N6,新加坡 2026-07-11 真机):SHIPPED_DEFAULT_ENTRIES 定义了但从没被发布,
    致空 config_template(→"default")的注入参数租户首次 POST 就撞 no current pointer
    400,每个新 region/重部署都要人肉先 POST /registry/default 才能起租户。这里在读到
    default 无指针时补种一次。只补 default,不代替 admin 对具名模板的显式发布。
    幂等:已有 current 指针就 no-op(不重复 publish 出 snapshot#2——publish_snapshot
    只会把 version 越推越高)。并发补种撞车(两方都读到无指针)→ 一方赢、另一方
    publish_snapshot 抛 TransactionCanceledException,吞掉(赢家已写好指针,调用方
    重查即见),其它错误照常 fail-loud。
    """
    items = _query_all(DEFAULT_TEMPLATE)
    if any(i["sk"] == "current" for i in items):
        return  # 已补种过,幂等 no-op
    txn_cancelled = getattr(
        clients.ddb.meta.client.exceptions,
        "TransactionCanceledException",
        None,
    )
    try:
        publish_snapshot(DEFAULT_TEMPLATE, SHIPPED_DEFAULT_ENTRIES)
    except Exception as e:  # noqa: BLE001 — 仅吞并发补种撞车,余皆 re-raise
        if txn_cancelled is not None and isinstance(e, txn_cancelled):
            return
        raise


def _named_template_exists_in_s3(config_template):
    """#223 客户联调闭环:console 存自定义模板走 templates Lambda 只写 S3
    (templates/openclaw/<name>/openclaw.json),不建 registry 指针。判断该模板
    在 S3 是否真存在,存在才 lazy-seed 指针(见 ensure_named_seeded)。
    fail-safe:桶名缺失或 S3 报错 → False(退回 fail-loud,不误 seed)。
    """
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        return False
    key = f"templates/openclaw/{config_template}/openclaw.json"
    try:
        clients.s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 — 404/403/网络皆视作"不可用",退回 fail-loud
        return False


def ensure_named_seeded(config_template):
    """#223:具名模板在 S3 存在但 registry 无指针时,用标准默认注入契约补种一次。

    根因:console Agent Config 存模板(templates Lambda put_template)只写 S3,
    不联动 registry publish;建租户校验 registry current 指针 → 客户存完模板建租户
    仍撞 no current pointer 400,console 全套操作闭不了环。这里在"S3 有该模板 + 无
    指针"时补种,让"console 存模板"="可用模板"。entries 复用 SHIPPED_DEFAULT_ENTRIES
    (凭据注入路径通用);要非标 entries 仍可 admin POST /registry/{tpl} 覆盖。
    幂等 + 并发撞车吞 TransactionCanceledException,与 ensure_default_seeded 同。
    """
    items = _query_all(config_template)
    if any(i["sk"] == "current" for i in items):
        return
    if not _named_template_exists_in_s3(config_template):
        return  # S3 无此模板 → 不 seed,交回 fail-loud 挡 typo
    txn_cancelled = getattr(
        clients.ddb.meta.client.exceptions,
        "TransactionCanceledException",
        None,
    )
    try:
        publish_snapshot(config_template, SHIPPED_DEFAULT_ENTRIES)
    except Exception as e:  # noqa: BLE001 — 仅吞并发补种撞车,余皆 re-raise
        if txn_cancelled is not None and isinstance(e, txn_cancelled):
            return
        raise


def load_current_snapshot(config_template):
    """单次 Query 拿全部行,按 current 指针选中快照。返回 (version, entries)。

    无指针时:DEFAULT_TEMPLATE 补种 shipped 默认(随部署自愈);具名模板若 S3 存在
    则 lazy-seed(#223,console 存模板即可用),S3 也没有才 fail-loud(挡错名/typo)。
    """
    items = _query_all(config_template)
    current = next((i for i in items if i["sk"] == "current"), None)
    if current is None:
        if config_template == DEFAULT_TEMPLATE:
            ensure_default_seeded()
        else:
            ensure_named_seeded(config_template)
        items = _query_all(config_template)
        current = next((i for i in items if i["sk"] == "current"), None)
    if current is None:
        raise LookupError(f"registry: no current pointer for {config_template}")
    version = int(current["current_version"])
    snap = next((i for i in items if i["sk"] == f"snapshot#{version}"), None)
    if snap is None:
        raise LookupError(
            f"registry: current points to missing snapshot#{version} "
            f"for {config_template}"
        )
    return version, snap["entries"]


def load_snapshot(config_template, version=None):
    """#429 读取指定不可变快照;version=None 时解析 current。

    返回 ``(version, entries, metadata)``。metadata 只含快照发布时可取得的 body
    证据;reapply 仍会在受理期 HEAD S3 绑定【当前】VersionId,因为模板上传与 registry
    publish 是两条独立链路。default body 烤在镜像里,没有 S3 metadata。
    """
    items = _query_all(config_template)
    if version is None:
        current = next((i for i in items if i["sk"] == "current"), None)
        if current is None:
            if config_template == DEFAULT_TEMPLATE:
                ensure_default_seeded()
            else:
                ensure_named_seeded(config_template)
            items = _query_all(config_template)
            current = next((i for i in items if i["sk"] == "current"), None)
        if current is None:
            raise LookupError(f"registry: no current pointer for {config_template}")
        version = int(current["current_version"])
    else:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("config_template_version must be a positive integer")
    snap = next((i for i in items if i["sk"] == f"snapshot#{version}"), None)
    if snap is None:
        raise LookupError(
            f"registry: missing snapshot#{version} for {config_template}"
        )
    metadata = {
        key: snap.get(key, "")
        for key in ("body_version_id", "body_etag", "body_sha256")
        if snap.get(key)
    }
    return int(version), snap["entries"], metadata


def _template_body_key(config_template):
    return f"templates/openclaw/{config_template}/openclaw.json"


def _head_named_template(config_template):
    bucket = os.environ.get("ASSETS_BUCKET", "")
    if not bucket:
        raise LookupError("registry: ASSETS_BUCKET is not configured")
    response = clients.s3.head_object(
        Bucket=bucket,
        Key=_template_body_key(config_template),
    )
    version_id = str(response.get("VersionId") or "")
    if not version_id or version_id == "null":
        raise LookupError(
            f"registry: template body for {config_template} has no S3 VersionId"
        )
    return bucket, version_id, str(response.get("ETag") or "").strip('"')


def load_template_body(config_template, expected_version_id=None):
    """#429 绑定并读取模板 body。

    named: HEAD 取得当前 VersionId,随后用 VersionId GET,保证预筛/探针/提交指向同一
    字节。若 worker 带 expected_version_id,仍 HEAD 检查当前对象未漂移后再按该版本读。
    default:body 来自目标镜像 data-template,控制面只返回 host-baked sentinel。
    """
    if config_template == DEFAULT_TEMPLATE:
        return None, {
            "host_baked": True,
            "body_version_id": "",
            "body_sha256": "",
            "body_etag": "",
        }
    bucket, current_version_id, etag = _head_named_template(config_template)
    if expected_version_id and current_version_id != expected_version_id:
        raise LookupError(
            f"registry: template body VersionId changed for {config_template}"
        )
    version_id = expected_version_id or current_version_id
    response = clients.s3.get_object(
        Bucket=bucket,
        Key=_template_body_key(config_template),
        VersionId=version_id,
    )
    raw = response["Body"].read()
    if not isinstance(raw, bytes):
        raw = bytes(raw)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"registry: template body for {config_template} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ValueError(
            f"registry: template body for {config_template} must be a JSON object"
        )
    return body, {
        "host_baked": False,
        "body_version_id": version_id,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "body_etag": etag,
    }


def forbidden_paths_for_version(openclaw_version):
    """Return the fast-reject denylist, or None when the version is unclassified."""
    version = str(openclaw_version or "").strip()
    exact = FORBIDDEN_BY_PIN.get(version)
    if exact is not None:
        return set(exact)
    match = re.fullmatch(r"2026\.(\d+)\.(\d+)(?:-\d+)?", version)
    if not match:
        return None
    month, patch = (int(part) for part in match.groups())
    if (month, patch) >= (6, 11):
        return set()
    return None


def find_forbidden_paths(body, forbidden):
    """Return full dotted paths present in body that match the denylist."""
    hits = []

    def walk(node, prefix=""):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if dotted in forbidden:
                hits.append(dotted)
            walk(value, dotted)

    walk(body)
    return hits


def publish_snapshot(config_template, entries):
    """追加 snapshot#<version+1> 并原子推进 current 指针,返回新 version。

    从不原地改已有快照。事务两腿:put 新快照(条件不存在,防重复 version 覆盖)
    + current 指针条件推进(current_version == old;首发时条件为指针行不存在)。
    并发发布撞车 → TransactionCanceledException,fail-loud 交上游重试。
    """
    items = _query_all(config_template)
    current = next((i for i in items if i["sk"] == "current"), None)
    old_version = int(current["current_version"]) if current else 0
    new_version = old_version + 1

    snapshot_item = {
        "config_template": config_template,
        "sk": f"snapshot#{new_version}",
        "version": new_version,
        "created_at": _now(),
        "entries": entries,
    }
    # reapply binding (template upload and registry publish remain decoupled).
    if config_template != DEFAULT_TEMPLATE and os.environ.get("ASSETS_BUCKET"):
        try:
            _body, body_binding = load_template_body(config_template)
            snapshot_item.update(
                {
                    key: body_binding[key]
                    for key in ("body_version_id", "body_etag", "body_sha256")
                    if body_binding.get(key)
                }
            )
        except Exception:  # noqa: BLE001 — preserve existing publish availability
            pass
    if current:
        pointer_leg = {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": {"config_template": config_template, "sk": "current"},
                "UpdateExpression": "SET current_version = :new",
                "ConditionExpression": "current_version = :old",
                "ExpressionAttributeValues": {":new": new_version, ":old": old_version},
            }
        }
    else:
        pointer_leg = {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    "config_template": config_template,
                    "sk": "current",
                    "current_version": new_version,
                },
                "ConditionExpression": "attribute_not_exists(config_template)",
            }
        }
    clients.ddb.meta.client.transact_write_items(
        TransactItems=[
            {
                "Put": {
                    "TableName": TABLE_NAME,
                    "Item": snapshot_item,
                    "ConditionExpression": "attribute_not_exists(config_template)",
                }
            },
            pointer_leg,
        ]
    )
    return new_version


def rollback(config_template, version):
    """仅移动 current 指针回指 version;事务里 ConditionCheck 确认该快照存在。"""
    version = int(version)
    clients.ddb.meta.client.transact_write_items(
        TransactItems=[
            {
                "ConditionCheck": {
                    "TableName": TABLE_NAME,
                    "Key": {
                        "config_template": config_template,
                        "sk": f"snapshot#{version}",
                    },
                    "ConditionExpression": "attribute_exists(config_template)",
                }
            },
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {"config_template": config_template, "sk": "current"},
                    "UpdateExpression": "SET current_version = :v",
                    "ConditionExpression": "attribute_exists(config_template)",
                    "ExpressionAttributeValues": {":v": version},
                }
            },
        ]
    )
    return version
