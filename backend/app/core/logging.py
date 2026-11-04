"""Structured logging with PII masking and secrets redaction.

Nothing logs a full email/phone: a structlog processor masks known keys and any string that
looks like an address before it reaches a handler.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from app.core.text import mask_email, mask_phone

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\+\d{8,15}")
_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "secret",
    "authorization",
    "smtp_password",
    "imap_password",
    "whatsapp_token",
}
_MASK_KEYS = {"email", "to_email", "from_email", "contact_email", "phone", "to_phone", "contact_phone"}


def _mask_value(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if key in _SENSITIVE_KEYS:
        return "••••redacted"
    if key in _MASK_KEYS:
        return mask_phone(value) if "phone" in key else mask_email(value)
    return value


def _mask_strings(value: Any) -> Any:
    if isinstance(value, str):
        masked = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), value)
        return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), masked)
    if isinstance(value, dict):
        return {k: _mask_value(k, _mask_strings(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_strings(v) for v in value]
    return value


def pii_masking_processor(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    for key in ("email", "to_email", "from_email", "contact_email", "phone", "to_phone"):
        if key in event_dict:
            event_dict[key] = _mask_value(key, event_dict[key])
    for key in _SENSITIVE_KEYS:
        if key in event_dict:
            event_dict[key] = "••••redacted"
    if "event" in event_dict and isinstance(event_dict["event"], str):
        event_dict["event"] = _mask_strings(event_dict["event"])
    return event_dict


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        pii_masking_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "mantiq") -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
