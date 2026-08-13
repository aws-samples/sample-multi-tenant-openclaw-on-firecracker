# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/envelope — 统一 enc:v1: 自描述信封的解析/序列化/scheme 分派(tenant-credential-contract 模块7)。

一个 sensitive 值被序列化为 ASCII 字符串,`:` 分隔字段,末段为 base64 密文体:

    enc:v1:<alg_code>:<key_id>:<hybrid_flag>:<base64_body>

设计契约(design.md「enc:v1: 自描述信封格式规范」):
  • **前缀即分类**:值以 `enc:v1:` 开头 → encrypted;否则(含空串/非字符串)→ plaintext,
    `parse_envelope` 返回 None,调用方绝不尝试解密(R1.3, R1.4, R4.9)。
  • **一条解码路径**:入站解密与出站加密共用 `parse_envelope`/`serialize_envelope`,以及同一
    Fixed_Asymmetric_Algorithm(R4.3, R4.6, R8.3)。
  • **fail-closed**:以 `enc:v1:` 开头但结构非法的值绝不被降级成明文——`parse_envelope` 抛
    `EnvelopeError`(可由 handler 映射为 400 VALIDATION),而不是返回 None。

本模块是最底叶子(handler-split 层间契约):只依赖 stdlib,不 import 仓内任何东西。
后续任务(1.3)在同文件补 `decrypt_inbound`/`encrypt_outbound`,复用这里的 parse/serialize。
"""

import re
from dataclasses import dataclass

# ── 信封版本与前缀 ──────────────────────────────────────────────────────────
ENVELOPE_VERSION = "v1"
ENVELOPE_PREFIX = f"enc:{ENVELOPE_VERSION}:"  # 分类前缀,恒 "enc:v1:"


# ── Fixed_Asymmetric_Algorithm:alg_code=1 ↔ RSA_4096_OAEP_SHA_256 ───────────
@dataclass(frozen=True)
class _Algorithm:
    code: int
    name: str


#: 唯一固定的非对称算法。RSA-4096 明文上限约 446B,当前所有字段(四个 Exchange_Keys、
#: llm_key、gateway_token、Ed25519 PKCS8 私钥)均在上限内 → 直接 RSA-OAEP(hybrid=0)。
FIXED_ASYMMETRIC_ALGORITHM = _Algorithm(code=1, name="RSA_4096_OAEP_SHA_256")
_ALG_BY_CODE = {FIXED_ASYMMETRIC_ALGORITHM.code: FIXED_ASYMMETRIC_ALGORITHM}

# ── scheme 常量 ─────────────────────────────────────────────────────────────
#: 缺省/legacy 对称 CMK + owner_id EncryptionContext(core/kms_envelope)。
SCHEME_KMS_CMK = "kms-cmk"
#: 固定 RSA-4096 非对称信封 + AAD=owner_id‖\n‖field。
SCHEME_ASYMMETRIC = "asymmetric-v1"
#: 受支持 scheme 全集(缺省等价 kms-cmk)。resolve_scheme 之外的值一律拒绝。
SUPPORTED_SCHEMES = (SCHEME_KMS_CMK, SCHEME_ASYMMETRIC)

# ── 校验常量(与 core/utils 保持同源语义,此处自带以维持叶子自足)───────────────
#: base64 体尺寸上限:一个小密钥的 KMS/RSA 密文的 base64,宽松上限(design R13.3)。
_MAX_BODY_LEN = 8192
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
#: key_id 取值如 `rk_2026a`、`clawpool`;禁 `:`(会破坏分隔)与空值。
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_HYBRID_FLAGS = frozenset({0, 1})


class EnvelopeError(ValueError):
    """信封解析/序列化/scheme 解析的可预期错误。

    继承 ValueError,携带可选 `supported`(受支持 scheme 列表),便于 handler 统一映射为
    400 VALIDATION 并回带支持项。绝不用于把非法密文降级成明文。"""

    def __init__(self, message, supported=None):
        super().__init__(message)
        self.supported = list(supported) if supported is not None else None


@dataclass(frozen=True)
class EnvelopeHeader:
    """解析后的信封头 + 体。frozen → 可安全用于往返相等比较(Property 2)。"""

    version: str = ENVELOPE_VERSION
    alg_code: int = FIXED_ASYMMETRIC_ALGORITHM.code
    key_id: str = ""
    hybrid_flag: int = 0
    body_b64: str = ""

    @property
    def algorithm(self):
        """返回本头对应的 _Algorithm(name/code),便于下游按名字选加解密实现。"""
        return _ALG_BY_CODE[self.alg_code]


def looks_encrypted(value):
    """当且仅当 value 是以 `enc:v1:` 开头的字符串时返回 True(Property 1 的分类判定)。

    空串、非字符串、无前缀 → False(plaintext),这些值绝不进入解密路径。"""
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def _validate_alg_code(alg_code):
    if alg_code not in _ALG_BY_CODE:
        raise EnvelopeError(
            f"unsupported envelope alg_code {alg_code!r} "
            f"(only {FIXED_ASYMMETRIC_ALGORITHM.code}={FIXED_ASYMMETRIC_ALGORITHM.name})"
        )


def _validate_key_id(key_id):
    if not isinstance(key_id, str) or not _KEY_ID_RE.match(key_id):
        raise EnvelopeError("envelope key_id must match ^[A-Za-z0-9._-]{1,128}$")


def _validate_hybrid_flag(hybrid_flag):
    if hybrid_flag not in _HYBRID_FLAGS:
        raise EnvelopeError("envelope hybrid_flag must be 0 (direct) or 1 (hybrid)")


def _validate_body(body_b64):
    if not isinstance(body_b64, str) or not body_b64:
        raise EnvelopeError("envelope body must be a non-empty base64 string")
    if len(body_b64) > _MAX_BODY_LEN or not _B64_RE.match(body_b64):
        raise EnvelopeError(
            f"envelope body must be base64 within {_MAX_BODY_LEN} chars"
        )


def parse_envelope(value):
    """把一个值解析为 EnvelopeHeader,或对明文返回 None。

    - 非字符串 / 无 `enc:v1:` 前缀(含空串)→ 返回 None(plaintext,绝不尝试解密)。
    - 以 `enc:v1:` 开头但结构非法 → 抛 EnvelopeError(fail-closed,绝不降级成明文)。
    - 合法 → 返回 EnvelopeHeader(version, alg_code, key_id, hybrid_flag, body_b64)。
    """
    if not looks_encrypted(value):
        return None
    # 固定 6 段;base64 体与 key_id 均不含 ':',按 ':' 精确切分即可。
    parts = value.split(":")
    if len(parts) != 6:
        raise EnvelopeError(
            "malformed enc:v1: envelope (expected "
            "enc:v1:<alg_code>:<key_id>:<hybrid_flag>:<base64_body>)"
        )
    _enc, version, alg_s, key_id, hybrid_s, body_b64 = parts
    if version != ENVELOPE_VERSION:
        raise EnvelopeError(f"unsupported envelope version {version!r} (expected v1)")
    try:
        alg_code = int(alg_s)
        hybrid_flag = int(hybrid_s)
    except ValueError:
        raise EnvelopeError("envelope alg_code and hybrid_flag must be integers")
    _validate_alg_code(alg_code)
    _validate_key_id(key_id)
    _validate_hybrid_flag(hybrid_flag)
    _validate_body(body_b64)
    return EnvelopeHeader(
        version=version,
        alg_code=alg_code,
        key_id=key_id,
        hybrid_flag=hybrid_flag,
        body_b64=body_b64,
    )


def serialize_envelope(header, body_b64=None):
    """把 EnvelopeHeader(+ base64 体)反向序列化为 `enc:v1:` 字符串。

    body_b64 显式传入时优先使用,否则取 header.body_b64(便于「先建头、后填体」)。
    序列化前做与 parse 对称的字段校验(fail-loud),保证 parse↔serialize 往返一致
    (Property 2)。"""
    if not isinstance(header, EnvelopeHeader):
        raise EnvelopeError("serialize_envelope requires an EnvelopeHeader")
    body = header.body_b64 if body_b64 is None else body_b64
    if header.version != ENVELOPE_VERSION:
        raise EnvelopeError(
            f"unsupported envelope version {header.version!r} (expected v1)"
        )
    _validate_alg_code(header.alg_code)
    _validate_key_id(header.key_id)
    _validate_hybrid_flag(header.hybrid_flag)
    _validate_body(body)
    return (
        f"{ENVELOPE_PREFIX}{header.alg_code}:{header.key_id}:"
        f"{header.hybrid_flag}:{body}"
    )


def resolve_scheme(scheme):
    """把入站 `scheme` 归一化为受支持的规范值,决定加解密路径(design 模块3/7)。

    - 缺省(None/空串)或 `kms-cmk` → 返回 SCHEME_KMS_CMK(legacy 对称 CMK 路径标识)。
    - `asymmetric-v1` → 返回 SCHEME_ASYMMETRIC(固定 RSA-4096 非对称路径标识)。
    - 其它 → 抛 EnvelopeError(可映射为 400 VALIDATION),并回带 supported schemes 列表。
    """
    if scheme is None or scheme == "":
        return SCHEME_KMS_CMK
    if scheme == SCHEME_KMS_CMK:
        return SCHEME_KMS_CMK
    if scheme == SCHEME_ASYMMETRIC:
        return SCHEME_ASYMMETRIC
    raise EnvelopeError(
        f"unsupported scheme {scheme!r} (supported: {list(SUPPORTED_SCHEMES)})",
        supported=SUPPORTED_SCHEMES,
    )


# ── 加解密路径(Task 1.3) ──────────────────────────────────────────────────────


def _build_aad(owner_id, field):
    """AAD = owner_id ‖ "\\n" ‖ fieldname (utf-8 bytes)。"""
    if not owner_id or not field:
        raise EnvelopeError("AAD requires non-empty owner_id and field")
    return f"{owner_id}\n{field}".encode()


def decrypt_inbound(value, scheme, owner_id, field):
    """入站解密统一分派(design 模块 7)。

    - scheme=kms-cmk → 委派 core/kms_envelope.decrypt(value, owner_id)(对称 CMK)
    - scheme=asymmetric-v1 → parse_envelope → KMS 非对称 Decrypt + AAD
    - hybrid_flag=1 → 预留,当前抛 EnvelopeError

    返回明文 str。入站值无 enc:v1: 前缀时调用方不应调本函数(先用 looks_encrypted 判)。
    """
    from core import kms_envelope

    resolved = resolve_scheme(scheme)
    if resolved == SCHEME_KMS_CMK:
        plaintext_bytes = kms_envelope.decrypt(value, owner_id)
        return (
            plaintext_bytes.decode()
            if isinstance(plaintext_bytes, bytes)
            else plaintext_bytes
        )

    # asymmetric-v1
    header = parse_envelope(
        f"{ENVELOPE_PREFIX}{value}" if not looks_encrypted(value) else value
    )
    if header is None:
        raise EnvelopeError("asymmetric-v1 requires enc:v1: envelope value")
    if header.hybrid_flag == 1:
        raise EnvelopeError("hybrid envelope (flag=1) not yet implemented")

    import base64
    from core.clients import kms as kms_client

    ciphertext_blob = base64.b64decode(header.body_b64)
    aad = _build_aad(owner_id, field)
    try:
        resp = kms_client.decrypt(
            CiphertextBlob=ciphertext_blob,
            KeyId=header.key_id,
            EncryptionAlgorithm="RSAES_OAEP_SHA_256",
            EncryptionContext={"aad": aad.decode()},
        )
    except Exception as e:
        raise EnvelopeError(f"asymmetric decrypt failed: {e}") from e
    return resp["Plaintext"].decode()


def encrypt_outbound(plaintext, recipient_public_key_pem, field, key_id="platform"):
    """出站加密：用 recipient 公钥(RSA-4096 OAEP-SHA256)加密 sensitive 字段,
    返回 enc:v1: 字符串(design 模块 7,R8.2/R8.3)。

    使用 cryptography 库本地加密(recipient key 是调用方公钥,不在 KMS)。
    """
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    if isinstance(recipient_public_key_pem, str):
        recipient_public_key_pem = recipient_public_key_pem.encode()
    pub_key = load_pem_public_key(recipient_public_key_pem)
    plaintext_bytes = plaintext.encode() if isinstance(plaintext, str) else plaintext
    ciphertext = pub_key.encrypt(
        plaintext_bytes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    body_b64 = base64.b64encode(ciphertext).decode()
    header = EnvelopeHeader(
        version=ENVELOPE_VERSION,
        alg_code=FIXED_ASYMMETRIC_ALGORITHM.code,
        key_id=key_id,
        hybrid_flag=0,
        body_b64=body_b64,
    )
    return serialize_envelope(header)


def _validate_injected_parameters_v2(params, configured_cmk_arn, registry_entries):
    """统一校验归一化后的 injected_parameters(Task 3.3)。返回 (clean_items|None, err)。

    从 core/utils 移来:本函数依赖 envelope 的 scheme/信封解析,而 utils 是 core-leaf
    叶子禁 import 仓内模块;envelope 是 core 层,可向下 import utils 的校验常量
    (方向合规,见 scripts/checks/import-layers.sh)。

    params 是 utils._normalize_injected_parameters 的输出;registry_entries 是参数注册表
    {field: {param_class, injection_target, sensitive, required, empty_fallback?}}。
    校验:类型(R13.1/P23)、scheme(R5.5/P11)、条数上限、未知字段指名(R1.7/P6)、
    env-class 注入目标名合法且非危险名(P7/P8)、enc:v1: 值需 CMK 或非对称 scheme +
    信封结构 fail-closed(P22)、非信封值 base64+尺寸(P22)、required 且无
    empty_fallback 的字段缺失(P24)。config_template 的 DNS-label 校验留给调用方。
    通过后返回的 clean 仍是密文/原值——本层绝不解密(guest 零凭据基线)。"""
    from core.utils import (
        _DANGEROUS_ENV_NAMES,
        _DANGEROUS_ENV_PREFIXES,
        _ENV_NAME_RE,
        _MAX_CIPHERTEXT_LEN,
        _MAX_INJECTED_ITEMS,
    )

    if params is None:
        return None, None
    if not isinstance(params, dict):
        return None, "injected_parameters must be an object"
    items = params.get("items")
    if not isinstance(items, dict):
        return None, "injected_parameters.items must be an object of {field: value}"
    try:
        scheme = resolve_scheme(params.get("scheme"))
    except EnvelopeError as e:
        return None, str(e)
    if len(items) > _MAX_INJECTED_ITEMS:
        return None, f"injected_parameters.items exceeds {_MAX_INJECTED_ITEMS} entries"
    entries = registry_entries or {}
    clean = {}
    for field, value in items.items():
        entry = entries.get(field)
        if not isinstance(entry, dict):
            return None, f"unknown injected parameter field: {field}"
        target = entry.get("injection_target")
        if entry.get("param_class") == "env":
            if not isinstance(target, str) or not _ENV_NAME_RE.match(target):
                return (
                    None,
                    f"injection_target for {field} must match ^[A-Z_][A-Z0-9_]*$ "
                    "(POSIX env var name)",
                )
            if target in _DANGEROUS_ENV_NAMES or any(
                target.startswith(p) for p in _DANGEROUS_ENV_PREFIXES
            ):
                return (
                    None,
                    f"injection_target '{target}' for {field} is a disallowed env "
                    "var that could execute code before in-guest guards load "
                    "(NODE_OPTIONS/LD_*/BASH_ENV/...)",
                )
        if not isinstance(value, str) or not value:
            return None, f"value for {field} must be a non-empty string"
        if looks_encrypted(value):
            if not configured_cmk_arn and scheme != SCHEME_ASYMMETRIC:
                return (
                    None,
                    f"encrypted value for {field} not supported: no CMK configured "
                    "and scheme is not asymmetric-v1",
                )
            try:
                parse_envelope(value)
            except EnvelopeError as e:
                return None, f"invalid enc:v1: envelope for {field}: {e}"
        else:
            # sensitive 强求 base64。原因:base64 不是加密(DDB 明文存、一眼可解),
            # 而 host 侧 plaintext 分支(harden-config.sh / cred-inject.sh)直接用原值、
            # 不做 base64 -d——强制 base64 只是让客户多编码一步却拿不到任何保密,反把
            # 原始 sk- key 编码后注进 openclaw.json.apiKey 致 litellm 401。真正的保密
            # 路径是上面的 enc:v1: 信封(owner_id 绑定)。故这里对 sensitive/非 sensitive
            # 一视同仁:明文透传的值本就只能承载自隔离的 opaque 值(per-tenant vkey /
            # 客户自持 key),需 owner 隔离的敏感值仍应走 enc:v1:。
            # fail-loud 不放松:超长拒(防塞爆)、控制字符拒(防 \r\n 注进 dotenv/JSON)。
            # #479:显式 asymmetric-v1 不允许 sensitive 字段静默降级为明文。
            if scheme == SCHEME_ASYMMETRIC and entry.get("sensitive"):
                return None, (
                    f"{field} declared under scheme=asymmetric-v1 must be an enc:v1: "
                    "envelope (plaintext rejected for sensitive fields)"
                )
            if len(value) > _MAX_CIPHERTEXT_LEN:
                return (
                    None,
                    f"value for {field} exceeds {_MAX_CIPHERTEXT_LEN} chars",
                )
            if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
                return (
                    None,
                    f"value for {field} must not contain control characters",
                )
        clean[field] = value
    for field, entry in entries.items():
        if (
            isinstance(entry, dict)
            and entry.get("required")
            and not entry.get("empty_fallback")
            and field not in clean
        ):
            return None, f"required injected parameter field missing: {field}"
    return clean, None
