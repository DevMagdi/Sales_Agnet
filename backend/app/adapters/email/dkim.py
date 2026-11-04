"""DKIM signing (RFC 6376) for outbound mail.

The private key is base64-wrapped PEM in `DKIM_PRIVATE_KEY` (newlines may be literal `\\n`). If a key
is configured we sign; if signing fails we raise instead of sending an unsigned business email,
because an unsigned first contact lands in spam and burns the domain.
"""

from __future__ import annotations

import base64
from typing import cast

from app.core.errors import ConfigError


def _decode_key(value: str) -> bytes:
    raw = value.strip()
    if "-----BEGIN" in raw:
        return raw.replace("\\n", "\n").encode()
    try:
        return base64.b64decode(raw, validate=False)
    except Exception as exc:  # pragma: no cover - defensive
        raise ConfigError("DKIM_PRIVATE_KEY is neither PEM nor base64 PEM") from exc


def sign(raw_message: bytes, *, domain: str, selector: str, private_key: str, headers: list[str] | None = None) -> bytes:
    if not domain or not selector or not private_key:
        raise ConfigError("DKIM signing requires SENDING_DOMAIN, DKIM_SELECTOR and DKIM_PRIVATE_KEY")
    try:
        import dkim
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ConfigError("dkimpy is required to sign outbound mail") from exc
    signature = cast(
        bytes,
        dkim.sign(
            raw_message,
            selector.encode(),
            domain.encode(),
            _decode_key(private_key),
            include_headers=tuple(
                headers or ["from", "to", "subject", "date", "message-id", "list-unsubscribe"]
            ),
        ),
    )
    return signature + raw_message
