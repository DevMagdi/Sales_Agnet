"""Envelope encryption for contact PII, blind indexes for matching, signed public tokens.

Design:
  * master key (ENCRYPTION_MASTER_KEY) -> HKDF -> key-encryption-key (KEK)
  * every encrypted value gets a fresh 256-bit data key (DEK)
  * DEK is wrapped with AES-256-GCM under the KEK; the value is sealed with AES-256-GCM
    under the DEK. Stored form: ``enc:v1:<b64(wrap_nonce||wrapped_dek)>:<b64(nonce||ct)>``
  * blind indexes are HMAC-SHA256 of the normalised value with a purpose-scoped key, so the
    DB can match suppression/dedupe without ever holding plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings
from app.core.errors import ConfigError

_PREFIX = "enc:v1:"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _master_key() -> bytes:
    settings = get_settings()
    key = settings.encryption_master_key
    if not key:
        # dev/test convenience only; startup validation refuses this in staging/production
        key = "dev-insecure-encryption-key"
    return key.encode("utf-8")


def _derive(info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=b"mantiq-outreach-v1", info=info).derive(
        _master_key()
    )


def _kek() -> AESGCM:
    return AESGCM(_derive(b"kek"))


def encrypt_text(plaintext: str) -> str:
    if plaintext is None:  # pragma: no cover - defensive
        raise ValueError("cannot encrypt None")
    dek = AESGCM.generate_key(bit_length=256)
    wrap_nonce = os.urandom(12)
    wrapped = _kek().encrypt(wrap_nonce, dek, b"dek")
    payload_nonce = os.urandom(12)
    sealed = AESGCM(dek).encrypt(payload_nonce, plaintext.encode("utf-8"), b"pii")
    return f"{_PREFIX}{_b64e(wrap_nonce + wrapped)}:{_b64e(payload_nonce + sealed)}"


def decrypt_text(token: str | None) -> str:
    if not token:
        return ""
    if not token.startswith(_PREFIX):
        # tolerate plaintext for imported rows created before encryption existed
        return token
    try:
        wrapped_part, sealed_part = token[len(_PREFIX) :].split(":", 1)
        wrapped_raw = _b64d(wrapped_part)
        dek = _kek().decrypt(wrapped_raw[:12], wrapped_raw[12:], b"dek")
        sealed_raw = _b64d(sealed_part)
        return AESGCM(dek).decrypt(sealed_raw[:12], sealed_raw[12:], b"pii").decode("utf-8")
    except (InvalidTag, ValueError) as exc:
        raise ConfigError("PII decryption failed (wrong ENCRYPTION_MASTER_KEY or tampered data)") from exc


def blind_index(value: str, purpose: str = "email") -> str:
    key = _derive(f"blind-index:{purpose}".encode())
    return hmac.new(key, value.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()


def _token_key() -> bytes:
    return _derive(b"public-token")


def sign_token(payload: dict[str, Any], ttl_seconds: int) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_token_key(), raw, hashlib.sha256).digest()[:24]
    return f"{_b64e(raw)}.{_b64e(signature)}"


def verify_token(token: str) -> dict[str, Any]:
    """Return the payload or raise ValueError. No PII is ever inside these tokens."""
    try:
        body_b64, sig_b64 = token.split(".", 1)
        raw = _b64d(body_b64)
        expected = hmac.new(_token_key(), raw, hashlib.sha256).digest()[:24]
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            raise ValueError("bad signature")
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid token") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("expired token")
    if not isinstance(payload, dict):
        raise ValueError("invalid token payload")
    return payload
