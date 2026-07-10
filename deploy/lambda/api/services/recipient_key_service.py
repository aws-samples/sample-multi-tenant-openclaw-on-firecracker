"""services 层 · recipient_key_service:Recipient_Key_Store(平台接收方公钥)访问。

表 openclaw-recipient-keys(env RECIPIENT_KEYS_TABLE):
  pk=scope("platform") + sk="version#<n>"(版本行) / "current"(指针行)。
只存公钥元数据,私钥永不入 DDB;bootstrap 私钥用后走
purge_bootstrap_private_key 从 Secrets Manager 强删。
依赖方向:services → core(clients/utils),不反向 import handler。
"""

import os
import secrets

import boto3

from core.clients import ddb
from core.utils import _now

SCOPE = "platform"
ALGORITHM = "RSA_4096_OAEP_SHA256"

recipient_keys_table = ddb.Table(
    os.environ.get("RECIPIENT_KEYS_TABLE", "openclaw-recipient-keys")
)

# ponytail: core.clients 没有 secretsmanager client,按 core/vkey.py 同款惰性单例
_secrets_client = None


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _validate_rsa4096(public_key_pem):
    """解析 PEM 公钥,非 RSA-4096 抛 ValueError。返回规范化 PEM(str)。"""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    if not isinstance(public_key_pem, str) or "BEGIN" not in public_key_pem:
        raise ValueError("public_key_pem must be a PEM-encoded public key string")
    try:
        key = load_pem_public_key(public_key_pem.encode())
    except Exception as e:
        raise ValueError(f"cannot parse public_key_pem: {e}")
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("public key must be RSA (got %s)" % type(key).__name__)
    if key.key_size != 4096:
        raise ValueError(f"public key must be RSA-4096 (got {key.key_size} bits)")
    return public_key_pem


def _new_key_id():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return f"rk_{now.year}{now.month:02d}_{secrets.token_hex(2)}"


def get_current_key():
    """读 current 指针 + 对应版本行。返回公钥元数据 dict(绝不含私钥);
    无指针/无版本行时返回 None。disabled 的 key 原样返回(enabled=False),
    让调用方决定 fail-closed。"""
    ptr = recipient_keys_table.get_item(Key={"scope": SCOPE, "sk": "current"}).get(
        "Item"
    )
    if not ptr:
        return None
    version = int(ptr.get("current_version", 0))
    if version <= 0:
        return None
    item = recipient_keys_table.get_item(
        Key={"scope": SCOPE, "sk": f"version#{version}"}
    ).get("Item")
    return item or None


def register_key(public_key_pem, source="caller"):
    """校验 RSA-4096 → 追加新版本行 → 推进 current 指针。返回新版本元数据。"""
    _validate_rsa4096(public_key_pem)
    current = get_current_key()
    version = int(current["version"]) + 1 if current else 1
    item = {
        "scope": SCOPE,
        "sk": f"version#{version}",
        "key_id": _new_key_id(),
        "algorithm": ALGORITHM,
        "public_key_pem": public_key_pem,
        "version": version,
        "created_at": _now(),
        "enabled": True,
        "source": source,
    }
    # 版本行条件写防并发同号覆盖;撞了让调用方重试(极低频操作,不做自动重试)
    recipient_keys_table.put_item(
        Item=item, ConditionExpression="attribute_not_exists(sk)"
    )
    recipient_keys_table.put_item(
        Item={"scope": SCOPE, "sk": "current", "current_version": version}
    )
    return item


def disable_current():
    """把当前版本行标 enabled=False。返回更新后的版本行;无 current 返回 None。"""
    current = get_current_key()
    if not current:
        return None
    resp = recipient_keys_table.update_item(
        Key={"scope": SCOPE, "sk": current["sk"]},
        UpdateExpression="SET enabled = :f",
        ExpressionAttributeValues={":f": False},
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes")


def purge_bootstrap_private_key(secret_name):
    """强删 Secrets Manager 里的 bootstrap 私钥(无恢复期)。幂等:已删返回 False。"""
    try:
        _get_secrets_client().delete_secret(
            SecretId=secret_name, ForceDeleteWithoutRecovery=True
        )
        return True
    except _get_secrets_client().exceptions.ResourceNotFoundException:
        return False
