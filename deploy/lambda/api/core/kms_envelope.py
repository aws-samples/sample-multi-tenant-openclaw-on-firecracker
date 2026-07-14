# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""core/kms_envelope — ClawPool credential-injection KMS helper (#152/#118).

Single encrypt/decrypt entry for platform secrets injected at tenant provision.
The EncryptionContext is bound to `owner_id` on BOTH sides: KMS cryptographically
requires the same context to decrypt, so a ciphertext minted for user A can never
be decrypted under user B (cross-user containment). We bind owner_id (the platform
user identity), NOT tenant_id: the upstream user-registration center creates the
userkey and encrypts it BEFORE any tenant exists, so tenant_id isn't available at
encrypt time — but the platform's owner_id is (it's passed on create_tenant as
body.owner_id, the create-on-behalf path). Ciphertext travels as base64 text (safe
in DDB / SSM / CloudTrail — none of them can decrypt it).

Split of duties (guest zero-credential baseline):
  • upstream / control plane may `encrypt` (needs kms:Encrypt on the CMK);
  • only the EC2 host `decrypt`s at VM launch (host role has kms:Decrypt).
The API Lambda itself neither encrypts nor decrypts injected creds — it only
validates + relays ciphertext. This module is used by moto-backed unit tests and
is available to any component that IS granted the key.
"""

import base64

from core.clients import kms


def _ctx(owner_id):
    if not owner_id:
        raise ValueError("owner_id is required for EncryptionContext binding")
    return {"owner_id": str(owner_id)}


def _ctx_tenant(tenant_id):
    """#187 P1 — tenant-bound EncryptionContext for pre-minted gateway tokens.

    A gateway token is a per-tenant secret (it protects one tenant's control
    plane in the microVM), so the natural cross-tenant guard is tenant_id, NOT
    owner_id. Kept as a SEPARATE helper from _ctx so the two purposes cannot
    accidentally collide: a ciphertext minted under tenant_id={a} will not
    decrypt as owner_id={a} even when the strings match — KMS EncryptionContext
    is a strict key/value dict comparison, so the KEY NAME itself is part of the
    guard. #118 owner_id-bound ciphertexts are 100% unaffected (encrypt/decrypt
    still use _ctx, byte-identical)."""
    if not tenant_id:
        raise ValueError("tenant_id is required for EncryptionContext binding")
    return {"tenant_id": str(tenant_id)}


def encrypt(plaintext, owner_id, key_id):
    """Envelope-encrypt a credential value under `key_id`, binding owner_id in the
    EncryptionContext. Returns base64 ciphertext (str). Raises on failure —
    fail-loud, never return a half-encrypted value."""
    if key_id is None or key_id == "":
        raise ValueError("key_id is required")
    data = plaintext.encode() if isinstance(plaintext, str) else plaintext
    resp = kms.encrypt(
        KeyId=key_id,
        Plaintext=data,
        EncryptionContext=_ctx(owner_id),
    )
    return base64.b64encode(resp["CiphertextBlob"]).decode()


def decrypt(ciphertext_b64, owner_id):
    """Decrypt base64 ciphertext, requiring the same owner_id EncryptionContext.
    KMS raises (InvalidCiphertextException) when the context mismatches — that is
    the cross-user guard, so callers MUST let it propagate (fail-closed), never
    swallow it into an empty value. Returns the plaintext bytes."""
    blob = base64.b64decode(ciphertext_b64)
    resp = kms.decrypt(
        CiphertextBlob=blob,
        EncryptionContext=_ctx(owner_id),
    )
    return resp["Plaintext"]


def encrypt_with_tenant(plaintext, tenant_id, key_id):
    """#187 P1 — envelope-encrypt binding tenant_id in the EncryptionContext.

    Semantic twin of encrypt() for per-tenant secrets (gateway token). Returns
    base64 ciphertext; raises on failure (fail-loud). The dedicated helper (vs.
    a ctx-name parameter on encrypt()) keeps callers from silently mixing the
    two purposes: pick the wrong helper and the plaintext round-trip works but
    the cross-*whatever* guard is misapplied — the type system won't catch it,
    so the guard is the function name."""
    if key_id is None or key_id == "":
        raise ValueError("key_id is required")
    data = plaintext.encode() if isinstance(plaintext, str) else plaintext
    resp = kms.encrypt(
        KeyId=key_id,
        Plaintext=data,
        EncryptionContext=_ctx_tenant(tenant_id),
    )
    return base64.b64encode(resp["CiphertextBlob"]).decode()


def decrypt_with_tenant(ciphertext_b64, tenant_id):
    """#187 P1 — decrypt requiring the same tenant_id EncryptionContext.

    KMS raises when the context mismatches (cross-tenant containment guard);
    callers MUST let it propagate (fail-closed). Returns plaintext bytes.

    Cross-purpose defense: a ciphertext produced by encrypt() (owner_id ctx)
    passed here (tenant_id ctx) will ALSO fail even if the id strings match —
    KMS treats the whole EncryptionContext dict as the guard, so a ctx of
    {"owner_id": "x"} is not equal to {"tenant_id": "x"}."""
    blob = base64.b64decode(ciphertext_b64)
    resp = kms.decrypt(
        CiphertextBlob=blob,
        EncryptionContext=_ctx_tenant(tenant_id),
    )
    return resp["Plaintext"]
