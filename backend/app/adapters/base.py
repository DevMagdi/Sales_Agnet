"""Adapter interfaces. Every vendor sits behind one of these; tests use the fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


class Adapter(Protocol):
    name: str
    is_fake: bool

    def healthcheck(self) -> tuple[bool, str]: ...


# --------------------------------------------------------------------------- LLM


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMAdapter(Adapter, Protocol):
    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        model_tier: Literal["fast", "strong"] = "fast",
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ) -> LLMResult: ...


# --------------------------------------------------------------------------- email


@dataclass
class OutboundEmail:
    to_email: str
    from_email: str
    from_name: str
    subject: str
    body_text: str
    body_html: str | None = None
    reply_to: str | None = None
    message_id: str = ""
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    unsubscribe_url: str = ""


@dataclass
class SendResult:
    ok: bool
    provider: str
    provider_message_id: str | None = None
    rfc_message_id: str | None = None
    error: str | None = None
    permanent: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmailEvent:
    type: Literal["delivered", "bounce_hard", "bounce_soft", "complaint", "open", "click"]
    message_id: str | None = None
    recipient: str | None = None
    reason: str | None = None
    occurred_at: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EmailAdapter(Adapter, Protocol):
    async def send(self, message: OutboundEmail) -> SendResult: ...

    def parse_event(self, payload: dict[str, Any]) -> list[EmailEvent]: ...

    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool: ...


# --------------------------------------------------------------------------- whatsapp


@dataclass
class WhatsAppSendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    permanent: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class WhatsAppEvent:
    type: Literal["message", "status"]
    from_phone: str | None = None
    text: str | None = None
    message_id: str | None = None
    status: str | None = None
    timestamp: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class WhatsAppAdapter(Adapter, Protocol):
    async def send_text(self, to_phone: str, text: str) -> WhatsAppSendResult: ...

    async def send_template(
        self, to_phone: str, template_name: str, language: str, params: list[str]
    ) -> WhatsAppSendResult: ...

    def verify_signature(self, raw_body: bytes, signature_header: str) -> bool: ...

    def parse_events(self, payload: dict[str, Any]) -> list[WhatsAppEvent]: ...


# --------------------------------------------------------------------------- discovery


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass
class PlaceResult:
    name: str
    address: str = ""
    website: str = ""
    phone: str = ""
    category: str = ""
    city: str = ""
    place_id: str = ""
    lat: float | None = None
    lng: float | None = None


@runtime_checkable
class SearchAdapter(Adapter, Protocol):
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]: ...


@runtime_checkable
class PlacesAdapter(Adapter, Protocol):
    async def text_search(self, query: str, max_results: int = 20) -> list[PlaceResult]: ...


# --------------------------------------------------------------------------- verification


@dataclass
class VerifyResult:
    status: Literal["valid", "risky", "invalid", "disposable", "unknown"]
    reason: str = ""
    provider: str = ""


@runtime_checkable
class VerifyAdapter(Adapter, Protocol):
    async def verify(self, email: str) -> VerifyResult: ...


# --------------------------------------------------------------------------- calendar / notify


@runtime_checkable
class CalendarAdapter(Adapter, Protocol):
    def booking_link(self) -> str: ...

    def propose_slots(self, count: int = 3) -> list[str]: ...


@runtime_checkable
class NotifyAdapter(Adapter, Protocol):
    async def send_alert(
        self, *, title: str, body: str, payload: dict[str, Any] | None = None
    ) -> tuple[bool, str]: ...
