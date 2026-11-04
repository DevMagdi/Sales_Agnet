"""SMTP adapter (aiosmtplib, STARTTLS). Used for live sending from a mailbox."""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Any

import aiosmtplib

from app.adapters.base import EmailEvent, OutboundEmail, SendResult
from app.adapters.email.dkim import sign as sign_dkim
from app.adapters.email.mime import build_mime


def _classify_smtp_error(exc: Exception) -> tuple[bool, str]:
    """Return (permanent, message). 5xx recipient errors are permanent."""
    code = getattr(exc, "code", None)
    text = str(exc)
    if isinstance(code, int) and 500 <= code < 600:
        permanent = code not in (550, 551, 552, 553, 554) or any(
            marker in text.lower()
            for marker in ("user unknown", "does not exist", "no such user", "recipient rejected")
        )
        return permanent, text
    return False, text


class SMTPEmailAdapter:
    name = "smtp"
    is_fake = False

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool = True,
        webhook_secret: str = "",
        timeout: float = 30.0,
        dkim_private_key: str = "",
        dkim_selector: str = "",
        sending_domain: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.use_tls = use_tls
        self._webhook_secret = webhook_secret
        self.timeout = timeout
        self._dkim_private_key = dkim_private_key
        self._dkim_selector = dkim_selector
        self._sending_domain = sending_domain

    def healthcheck(self) -> tuple[bool, str]:
        if not self.host or not self.username or not self._password:
            return False, "SMTP host/username/password missing"
        return True, f"smtp {self.host}:{self.port} (TLS={'starttls' if self.use_tls else 'none'})"

    async def send(self, message: OutboundEmail) -> SendResult:
        raw, message_id = build_mime(message)
        if self._dkim_private_key:
            raw = sign_dkim(
                raw,
                domain=self._sending_domain or (message.from_email.partition("@")[2] or ""),
                selector=self._dkim_selector,
                private_key=self._dkim_private_key,
            )
        try:
            kwargs: dict[str, Any] = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "password": self._password,
                "timeout": self.timeout,
            }
            if self.use_tls:
                kwargs["start_tls"] = True
            else:
                kwargs["use_tls"] = False
            result = await aiosmtplib.send(raw, sender=message.from_email, recipients=[message.to_email], **kwargs)
            provider_id = None
            if isinstance(result, tuple) and result:
                responses = result[-1]
                provider_id = responses.get(message.to_email) if isinstance(responses, dict) else None
            return SendResult(
                ok=True, provider="smtp", provider_message_id=provider_id, rfc_message_id=message_id
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            permanent, detail = _classify_smtp_error(exc)
            return SendResult(ok=False, provider="smtp", error=detail, permanent=permanent)

    def parse_event(self, payload: dict[str, Any]) -> list[EmailEvent]:
        return []

    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        if not self._webhook_secret:
            return False
        signature = headers.get("x-mantiq-signature", "")
        expected = hmac.new(self._webhook_secret.encode(), raw_body, sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
