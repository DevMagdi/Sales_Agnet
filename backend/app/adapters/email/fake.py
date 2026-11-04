"""Fake email adapter: records messages, never touches the network."""

from __future__ import annotations

import hashlib
from typing import Any

from app.adapters.base import EmailEvent, OutboundEmail, SendResult
from app.adapters.email.mime import build_mime


class FakeEmailAdapter:
    name = "fake"
    is_fake = True

    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []
        self.sent_raw: list[bytes] = []
        self.fail_next: str | None = None
        self.fail_permanent = False

    def healthcheck(self) -> tuple[bool, str]:
        return True, "fake email adapter (dry_run/sandbox/test only)"

    async def send(self, message: OutboundEmail) -> SendResult:
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            return SendResult(ok=False, provider="fake", error=error, permanent=self.fail_permanent)
        raw, message_id = build_mime(message)
        self.sent.append(message)
        self.sent_raw.append(raw)
        return SendResult(
            ok=True,
            provider="fake",
            provider_message_id="fake-" + hashlib.sha256(raw).hexdigest()[:16],
            rfc_message_id=message_id,
        )

    def parse_event(self, payload: dict[str, Any]) -> list[EmailEvent]:
        events: list[EmailEvent] = []
        raw_type = str(payload.get("type", payload.get("event", ""))).lower()
        mapping = {
            "delivered": "delivered",
            "delivery": "delivered",
            "bounce": "bounce_hard",
            "bounce_hard": "bounce_hard",
            "permanent_fail": "bounce_hard",
            "bounce_soft": "bounce_soft",
            "transient_fail": "bounce_soft",
            "complaint": "complaint",
            "spam": "complaint",
            "open": "open",
            "click": "click",
        }
        if raw_type in mapping:
            events.append(
                EmailEvent(
                    type=mapping[raw_type],  # type: ignore[arg-type]
                    message_id=payload.get("message_id") or payload.get("MessageId"),
                    recipient=payload.get("recipient") or payload.get("email"),
                    reason=payload.get("reason"),
                    occurred_at=payload.get("timestamp"),
                    meta=payload,
                )
            )
        return events

    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        return True
