# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""demo-marketplace broker — 二手电商后端「代开租户」最小实现(开发测试床,不交付)。

定位:ADR-dataplane-external-saas-auth 档 A 时序图步骤 8-15 的「交易平台后端」。
用户在电商前端点「开通 AI Pro」→ 电商前端带用户 id_token 调本 broker →
broker 校验 id_token(电商 entry pool 签)→ 持 x-api-key 调**本平台控制面** POST /tenants
代开一个独立 openclaw microVM(带 tenant_user_id + platform_id)→ 轮询到 running → 回租户 id。

关键纪律(对齐 ADR):
  - **代开,非用户自助**:租户由 broker(后端,持 x-api-key)创建,用户全程不碰本平台控制面。
    这样电商能在开通前插入自己的购买/计费/配额门,而不是让用户直接 POST /tenants/self。
  - **owner 归属按 tenant_user_id**:用电商用户的稳定 id 作 tenant_user_id,后续「按用户管理 fleet」
    (GET /users/{tenant_user_id}/tenants)才能把节点关联回电商用户。
  - **无硬编码凭据**:x-api-key / 控制面 URL / entry pool 校验参数全从环境变量读。
  - **id_token 校验 fail-loud**:校验失败拒开(不信客户端自报的用户身份)。

这是最小骨架:真部署为电商自己的一个 Lambda/API。本地可直接函数调用测。
"""

import json
import os
import time
import urllib.request
import urllib.error

# —— 配置(环境变量,无硬编码)——
CTRL_API_BASE = os.environ.get("CTRL_API_BASE", "").rstrip(
    "/"
)  # 本平台控制面 API GW base
CTRL_API_KEY = os.environ.get("CTRL_API_KEY", "")  # x-api-key(电商后端持有,server-side)
ENTRY_JWKS_URL = os.environ.get("ENTRY_JWKS_URL", "")  # 电商 entry Cognito pool 的 JWKS
ENTRY_ISSUER = os.environ.get("ENTRY_ISSUER", "")  # 电商 entry pool issuer
PLATFORM_ID = os.environ.get("PLATFORM_ID", "demo-marketplace")  # 本电商的平台标识
POLL_MAX_SEC = int(os.environ.get("POLL_MAX_SEC", "60"))


def _http(method, url, headers=None, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _verify_user_token(id_token):
    """校验电商 entry pool 签的 id_token,返回 (claims, err).

    最小实现:解码 + 校验 iss + 未过期。生产应加 JWKS 验签(与本平台 hub
    verifyCognitoIdToken 同款,jose/PyJWT + JWKS）。这里 fail-loud:任何异常都拒。
    """
    if not id_token:
        return None, "missing id_token"
    try:
        import base64

        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None, "malformed id_token"
    if ENTRY_ISSUER and claims.get("iss") != ENTRY_ISSUER:
        return None, "issuer mismatch"
    if claims.get("exp", 0) < time.time():
        return None, "token expired"
    # 用户稳定 id:优先 sub;email 仅作展示
    if not claims.get("sub"):
        return None, "no sub in token"
    return claims, None


def open_ai_pro(id_token):
    """代开租户主流程。返回 {tenant_id, status} 或 {error}."""
    if not (CTRL_API_BASE and CTRL_API_KEY):
        return {"error": "broker not configured (CTRL_API_BASE/CTRL_API_KEY)"}
    claims, err = _verify_user_token(id_token)
    if err:
        return {"error": f"auth: {err}"}
    tenant_user_id = claims["sub"]  # 电商用户稳定 id → 关联本平台租户
    # 代开:持 x-api-key 调本平台控制面(非用户自助)。client_token 做幂等,
    # 同一电商用户重复点「开通」不会双开(命中 409 → 复用已有租户)。
    body = {
        "name": f"aipro-{tenant_user_id[:8]}",
        "tenant_user_id": tenant_user_id,
        "platform_id": PLATFORM_ID,
        "client_token": f"{PLATFORM_ID}:{tenant_user_id}",
    }
    status, resp = _http(
        "POST",
        f"{CTRL_API_BASE}/tenants",
        headers={"x-api-key": CTRL_API_KEY, "content-type": "application/json"},
        body=body,
    )
    if status == 409:
        # 幂等重放:该用户已有租户,从冲突响应/列表取回 id
        tid = resp.get("id") or resp.get("existing_id")
        if tid:
            return {"tenant_id": tid, "status": "existing"}
    if status not in (200, 202):
        return {"error": f"create failed {status}: {json.dumps(resp)[:160]}"}
    tid = resp.get("id")
    if not tid:
        return {"error": f"no tenant id in response: {json.dumps(resp)[:160]}"}
    # 轮询到 running(电商后端负责等待,用户前端只收最终结果)
    deadline = time.time() + POLL_MAX_SEC
    last = resp.get("status", "queued")
    while time.time() < deadline:
        st, node = _http(
            "GET",
            f"{CTRL_API_BASE}/tenants/{tid}",
            headers={"x-api-key": CTRL_API_KEY},
        )
        last = node.get("status", last)
        if last == "running":
            return {"tenant_id": tid, "status": "running"}
        time.sleep(3)
    return {
        "tenant_id": tid,
        "status": last,
        "note": "still provisioning past poll window",
    }


def lambda_handler(event, context=None):
    """API GW proxy 入口:POST /open-ai-pro,Authorization: Bearer <电商 id_token>."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    id_token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    result = open_ai_pro(id_token)
    code = 200 if result.get("tenant_id") else 400
    return {
        "statusCode": code,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
        },
        "body": json.dumps(result),
    }
