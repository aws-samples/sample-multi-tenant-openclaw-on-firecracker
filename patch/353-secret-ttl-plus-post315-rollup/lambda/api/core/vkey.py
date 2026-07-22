# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/vkey — per-tenant LiteLLM 计费虚拟密钥(handler-split #132 Phase1 T1.2)。

从 handler.py 机械搬迁,函数体逐字不变。domain 专属常量/缓存(LITELLM_*/
TENANT_DEFAULT_*/_secrets_client/_litellm_*_cache)随迁,global 语义保持在本模块内。
共享 boto3 ssm client 从 core.clients import。按 design.md 层间契约:core 域只依赖
core.clients / core.utils,不反向 import services/routes。
facade:handler.py re-export 全部符号,旧 handler.<sym> patch/调用路径全程有效。
"""

import json
import os
import urllib.request

import boto3

from core.clients import ssm

LITELLM_MASTER_KEY_SECRET = os.environ.get("LITELLM_MASTER_KEY_SECRET", "")

# base_url: env 优先;空则运行时从 SSM /openclaw/litellm-host 读(CDK 自起 LiteLLM
# 时把 EC2 私网 IP:4000 写在这里,synth 时拿不到 IP,故不能靠 env 硬编码)。
LITELLM_HOST_SSM = os.environ.get("LITELLM_HOST_SSM", "/openclaw/litellm-host")

TENANT_DEFAULT_BUDGET = float(os.environ.get("TENANT_DEFAULT_BUDGET", "0") or 0)

TENANT_DEFAULT_RPM = int(os.environ.get("TENANT_DEFAULT_RPM", "0") or 0)

_secrets_client = None

_litellm_master_key_cache = None

_litellm_base_url_cache = None

def _get_litellm_base_url():
    """Resolve the LiteLLM base URL: explicit env wins; otherwise read the
    CDK-self-hosted gateway address from SSM (/openclaw/litellm-host, written at
    EC2 boot with the runtime private IP). Cached. Returns "" if neither set."""
    global _litellm_base_url_cache
    if _litellm_base_url_cache is not None:
        return _litellm_base_url_cache
    env_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    if env_url:
        _litellm_base_url_cache = env_url
        return env_url
    try:
        val = ssm.get_parameter(Name=LITELLM_HOST_SSM)["Parameter"]["Value"].strip()
        _litellm_base_url_cache = val
    except Exception as e:
        print(f"litellm: cannot read base url from SSM {LITELLM_HOST_SSM}: {e}")
        _litellm_base_url_cache = ""
    return _litellm_base_url_cache

def _get_litellm_master_key():
    """Read the LiteLLM master key from Secrets Manager (cached). Returns None
    if billing isn't configured — callers then skip vkey minting (backward
    compatible: tenants fall back to the shared key baked in the image).
    The secret may be a bare string or JSON {"master_key": "..."} (CDK's
    self-hosted LiteLLM stores JSON) — both are handled."""
    global _secrets_client, _litellm_master_key_cache
    if not (LITELLM_MASTER_KEY_SECRET and _get_litellm_base_url()):
        return None
    if _litellm_master_key_cache is not None:
        return _litellm_master_key_cache
    try:
        if _secrets_client is None:
            _secrets_client = boto3.client("secretsmanager")
        raw = _secrets_client.get_secret_value(SecretId=LITELLM_MASTER_KEY_SECRET).get(
            "SecretString", ""
        )
        mk = raw
        if raw and raw.lstrip().startswith("{"):
            try:
                mk = json.loads(raw).get("master_key", "") or ""
            except Exception:
                mk = ""
        _litellm_master_key_cache = mk or None
    except Exception as e:
        print(f"litellm: cannot read master key secret: {e}")
        _litellm_master_key_cache = None
    return _litellm_master_key_cache

def _mint_tenant_vkey(tenant_id, owner_sub):
    """Mint a per-tenant LiteLLM vkey with tenant/sub metadata + budget/rpm.
    Returns the vkey string, or None if billing unconfigured / call fails
    (non-fatal — tenant still launches on the shared key)."""
    mk = _get_litellm_master_key()
    if not mk:
        return None
    payload = {
        "key_alias": f"tenant-{tenant_id}",
        "metadata": {"tenant_id": tenant_id, "cognito_sub": owner_sub or ""},
    }
    if TENANT_DEFAULT_BUDGET > 0:
        payload["max_budget"] = TENANT_DEFAULT_BUDGET
    if TENANT_DEFAULT_RPM > 0:
        payload["rpm_limit"] = TENANT_DEFAULT_RPM
    try:
        req = urllib.request.Request(
            _get_litellm_base_url().rstrip("/") + "/key/generate",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + mk,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("key")
    except Exception as e:
        print(f"litellm: vkey mint failed for {tenant_id}: {e}")
        return None

def _revoke_tenant_vkey(vkey):
    """Go-live C: reclaim a tenant's LiteLLM vkey on delete (POST /key/delete).
    Without this the per-tenant key lingers in LiteLLM after the tenant is gone —
    a credential + budget leak that accumulates over churn. Non-fatal (delete
    proceeds even if revoke fails; we log) and best-effort. Returns True on
    confirmed revoke, False otherwise."""
    if not vkey:
        return False
    mk = _get_litellm_master_key()
    if not mk:
        return False
    try:
        req = urllib.request.Request(
            _get_litellm_base_url().rstrip("/") + "/key/delete",
            data=json.dumps({"keys": [vkey]}).encode(),
            headers={
                "Authorization": "Bearer " + mk,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
            return True
    except Exception as e:
        # don't leak the key value into logs (mask)
        print(f"litellm: vkey revoke failed for [REDACTED vkey]: {e}")
        return False
