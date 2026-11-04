"""Password hashing, JWT access tokens, refresh-token hashing and TOTP."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import Unauthorized

_hasher = PasswordHasher(time_cost=2, memory_cost=64 * 1024, parallelism=2)


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (ValueError, VerificationError):
        return False


def _jwt_key(settings: Settings) -> bytes:
    return hashlib.sha256(f"jwt:{settings.secret_key}".encode()).digest()


def create_access_token(user_id: str, role: str, org_id: str, settings: Settings, clock: Clock) -> str:
    now = clock.now()
    payload = {
        "sub": user_id,
        "role": role,
        "org": org_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_minutes)).timestamp()),
        "typ": "access",
    }
    return jwt.encode(payload, _jwt_key(settings), algorithm="HS256")


def decode_access_token(token: str, settings: Settings, clock: Clock) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _jwt_key(settings),
            algorithms=["HS256"],
            options={"require": ["exp", "sub", "typ"]},
            leeway=5,
        )
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid or expired access token") from exc
    if payload.get("typ") != "access":
        raise Unauthorized("wrong token type")
    return payload


def new_refresh_token() -> tuple[str, str]:
    """Return (raw token, sha256 hash to store). The raw value is shown once."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    return bool(pyotp.TOTP(secret).verify(code.strip(), valid_window=1))


def totp_provisioning_uri(secret: str, email: str, issuer: str = "Mantiq Outreach") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
