"""Notify adapter: fans out to every configured staff channel."""

from __future__ import annotations

from typing import Any

from app.adapters.base import NotifyAdapter
from app.adapters.notify.fake import FakeNotify
from app.adapters.notify.webhook import WebhookNotify
from app.core.config import Settings


class FanoutNotify:
    name = "fanout"
    is_fake = False

    def __init__(self, adapters: list[NotifyAdapter]) -> None:
        self._adapters = adapters

    def healthcheck(self) -> tuple[bool, str]:
        if not self._adapters:
            return False, "no staff notification channel configured (NOTIFY_WEBHOOK_URL / NOTIFY_EMAIL)"
        oks = [a.healthcheck() for a in self._adapters]
        return any(ok for ok, _ in oks), "; ".join(msg for _, msg in oks)

    async def send_alert(
        self, *, title: str, body: str, payload: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        results = [await a.send_alert(title=title, body=body, payload=payload) for a in self._adapters]
        if not results:
            return False, "no channel"
        return any(ok for ok, _ in results), "; ".join(msg for _, msg in results)


def build_notify(settings: Settings, email_adapter: Any | None = None) -> NotifyAdapter:
    adapters: list[NotifyAdapter] = []
    if settings.notify_webhook_url:
        adapters.append(WebhookNotify(settings.notify_webhook_url))
    if settings.notify_email and email_adapter is not None:
        from app.adapters.notify.email import EmailNotify

        adapters.append(EmailNotify(email_adapter, settings.notify_email, settings.sender_email))
    if not adapters:
        return FakeNotify()
    return FanoutNotify(adapters)
