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


#: bootstrap 私钥在 Secrets Manager 的固定名字。平台首启自动生成 keypair 后私钥存这里,
#: 运维 get-secret-value 取出线下交给调用方;交接完可走 purge_bootstrap_private_key 强删。
BOOTSTRAP_SECRET_NAME = os.environ.get(
    "RECIPIENT_BOOTSTRAP_SECRET", "openclaw/recipient-bootstrap-private-key"
)


def ensure_bootstrap_key():
    """无任何 recipient key 时平台自动生成一对 RSA-4096:私钥先落 Secrets Manager,
    公钥再登记 DDB(source=bootstrap)。已有 current key 则原样返回,幂等。

    顺序契约:私钥必须先存好再推进公钥指针——反过来会有"公钥已生效、私钥没人拿到"
    的窗口,那段时间封出的凭据谁都解不开。并发双起时 register_key 的版本行条件写让
    后到者失败;后到者重读 current(先到者已写好)即可,两边私钥/公钥各自成对不串。
    """
    current = get_current_key()
    if current:
        return current
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    sm = _get_secrets_client()
    try:
        sm.create_secret(Name=BOOTSTRAP_SECRET_NAME, SecretString=private_pem)
    except sm.exceptions.ResourceExistsException:
        # 上次半程失败的残留(私钥存了、公钥没登记成)——覆盖成本次的私钥,
        # 保证 Secrets Manager 里的私钥与即将登记的公钥永远是同一对。
        sm.put_secret_value(SecretId=BOOTSTRAP_SECRET_NAME, SecretString=private_pem)
    try:
        return register_key(public_pem, source="bootstrap")
    except Exception:
        # 并发撞版本行条件写:后到者读先到者的结果;仍读不到才是真失败,fail-loud。
        current = get_current_key()
        if current:
            return current
        raise


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


class RecipientKeyChanged(RuntimeError):
    """调用方指定的 key_id 已不是当前那把 —— 期间发生过轮换。

    #615 —— 让"校验 key_id"与"禁用"成为一次原子操作。调用方先 GET 拿 key_id、再 POST
    disable,这两步之间可能有人 register 了新 key(轮换 = 新建 version#N+1 并推进 current
    指针)。如果只在读到的 current 上比对一次再无条件写,那次写落在**新**那把上,调用方却
    收到 200 —— 客户以为禁掉的是旧 key,实际禁掉的是刚上线的新 key,整个平台的凭据获取
    随即中断。所以 expected_key_id 必须进条件写,由 DynamoDB 保证"比对与写"不可分割。
    """


def disable_current(expected_key_id=None):
    """把当前版本行标 enabled=False。返回更新后的版本行;无 current 返回 None。

    expected_key_id 非 None 时,只有该行的 key_id 仍等于它才落写;不等则抛
    RecipientKeyChanged(不写任何东西)。这条件是**在同一次 update_item 里**判的,
    所以它挡得住 get 与 update 之间的轮换 —— 见 RecipientKeyChanged 的说明。
    """
    current = get_current_key()
    if not current:
        return None
    kwargs = {
        "Key": {"scope": SCOPE, "sk": current["sk"]},
        "UpdateExpression": "SET enabled = :f",
        "ExpressionAttributeValues": {":f": False},
        "ReturnValues": "ALL_NEW",
    }
    if expected_key_id is not None:
        kwargs["ConditionExpression"] = "key_id = :kid"
        kwargs["ExpressionAttributeValues"][":kid"] = expected_key_id
    ccf = recipient_keys_table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        resp = recipient_keys_table.update_item(**kwargs)
    except ccf as exc:
        raise RecipientKeyChanged(expected_key_id) from exc
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
