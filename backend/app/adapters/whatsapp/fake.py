"""Fake WhatsApp adapter for tests and dry_run."""

from __future__ import annotations

from typing import Any

from app.adapters.base import WhatsAppEvent, WhatsAppSendResult


class FakeWhatsAppAdapter:
    name = "fake"
    is_fake = True

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_next: str | None = None

    def healthcheck(self) -> tuple[bool, str]:
        return True, "fake WhatsApp adapter (dev/test/dry_run only)"

    async def send_text(self, to_phone: str, text: str) -> WhatsAppSendResult:
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            return WhatsAppSendResult(ok=False, error=error)
        self.sent.append({"kind": "text", "to": to_phone, "text": text})
        return WhatsAppSendResult(ok=True, provider_message_id=f"wamid.fake.{len(self.sent)}")

    async def send_template(
        self, to_phone: str, template_name: str, language: str, params: list[str]
    ) -> WhatsAppSendResult:
        self.sent.append(
            {"kind": "template", "to": to_phone, "name": template_name, "lang": language, "params": params}
        )
        return WhatsAppSendResult(ok=True, provider_message_id=f"wamid.fake.{len(self.sent)}")

    def verify_signature(self, raw_body: bytes, signature_header: str) -> bool:
        return True

    def parse_events(self, payload: dict[str, Any]) -> list[WhatsAppEvent]:
        sample = payload.get("sample")
        if isinstance(sample, dict):
            return [
                WhatsAppEvent(
                    type="message",
                    from_phone=sample.get("from"),
                    text=sample.get("text", ""),
                    message_id=sample.get("id", "wamid.fake"),
                    raw=sample,
                )
            ]
        return []
