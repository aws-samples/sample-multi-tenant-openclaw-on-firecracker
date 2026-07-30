"""Authenticated pagination cursors for tenant-scale APIs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


CURSOR_VERSION = 1
CURSOR_MAX_AGE_SECONDS = int(os.environ.get("CURSOR_MAX_AGE_SECONDS", "86400"))


def _key() -> bytes:
    raw = os.environ.get("PAGINATION_AES_KEY", "")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("PAGINATION_AES_KEY is invalid") from exc
    if len(key) != 32:
        raise RuntimeError("PAGINATION_AES_KEY must decode to exactly 32 bytes")
    return key


def condition_hash(condition: dict[str, Any]) -> str:
    canonical = json.dumps(
        condition, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def encode_cursor(
    cursor: dict[str, Any] | None,
    condition: dict[str, Any],
    *,
    now: int | None = None,
) -> str | None:
    if not cursor:
        return None
    payload = {
        "v": CURSOR_VERSION,
        "iat": int(time.time() if now is None else now),
        "cursor": cursor,
        "cond_hash": condition_hash(condition),
    }
    plaintext = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, plaintext, b"openclaw-pagination-v1")
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")


def decode_cursor(
    token: str | None,
    condition: dict[str, Any],
    *,
    now: int | None = None,
) -> dict[str, Any] | None:
    if token is None:
        return None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("next_token is invalid or expired")
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(raw) < 29:
            raise ValueError
        plaintext = AESGCM(_key()).decrypt(
            raw[:12], raw[12:], b"openclaw-pagination-v1"
        )
        payload = json.loads(plaintext)
        issued_at = int(payload["iat"])
        current = int(time.time() if now is None else now)
        if payload.get("v") != CURSOR_VERSION:
            raise ValueError
        if issued_at > current + 60 or current - issued_at > CURSOR_MAX_AGE_SECONDS:
            raise ValueError
        if payload.get("cond_hash") != condition_hash(condition):
            raise ValueError
        cursor = payload.get("cursor")
        if not isinstance(cursor, dict) or not cursor:
            raise ValueError
        return cursor
    except RuntimeError:
        raise
    except Exception as exc:
        raise ValueError("next_token is invalid or expired") from exc
