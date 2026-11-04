"""Staff e-mail notifications via the configured email adapter."""

from __future__ import annotations

from typing import Any

from app.adapters.base import EmailAdapter, OutboundEmail


class EmailNotify:
    name = "email"
    is_fake = False

    def __init__(self, email_adapter: EmailAdapter, to_email: str, from_email: str) -> None:
        self._email = email_adapter
        self._to = to_email
        self._from = from_email

    def healthcheck(self) -> tuple[bool, str]:
        ok, detail = self._email.healthcheck()
        return (ok and bool(self._to), detail if self._to else "NOTIFY_EMAIL missing")

    async def send_alert(
        self, *, title: str, body: str, payload: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        if not self._to:
            return False, "NOTIFY_EMAIL missing"
        result = await self._email.send(
            OutboundEmail(
                to_email=self._to,
                from_email=self._from or self._to,
                from_name="Mantiq Outreach Agent",
                subject=f"[Outreach] {title}",
                body_text=body,
            )
        )
        return result.ok, result.error or "sent"
