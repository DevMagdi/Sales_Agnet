"""WhatsApp sending with the consent + 24h-window policy enforced before the adapter is touched."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import Blocked
from app.db.models import CampaignProspect, Company, Contact
from app.services import audit, consent, suppression
from app.services.contacts import contact_phone

APPROVED_TEMPLATES = {"demo_followup", "info_request"}


@dataclass
class WhatsAppOutcome:
    sent: bool
    blocked_reason: str = ""
    provider_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"sent": self.sent, "blocked_reason": self.blocked_reason, "provider_id": self.provider_id}


async def send_template(
    session: AsyncSession,
    *,
    registry: AdapterRegistry,
    phone: str,
    template_name: str,
    language: str,
    params: list[str],
    clock: Clock,
) -> WhatsAppOutcome:
    if template_name not in APPROVED_TEMPLATES:
        return WhatsAppOutcome(sent=False, blocked_reason="template_not_approved")
    if await suppression.is_suppressed(session, phone=phone):
        return WhatsAppOutcome(sent=False, blocked_reason="phone_suppressed")
    if not await consent.has_opt_in(session, phone):
        return WhatsAppOutcome(sent=False, blocked_reason="whatsapp_requires_opt_in")
    result = await registry.whatsapp.send_template(phone, template_name, language, params)
    await audit.record(
        session,
        action="whatsapp_template_sent" if result.ok else "whatsapp_template_failed",
        entity_type="whatsapp",
        detail={"template": template_name, "ok": result.ok},
    )
    return WhatsAppOutcome(
        sent=result.ok, blocked_reason="" if result.ok else (result.error or "send_failed"), provider_id=result.provider_message_id
    )


async def send_in_window_text(
    session: AsyncSession, *, registry: AdapterRegistry, phone: str, text: str, clock: Clock
) -> WhatsAppOutcome:
    if await suppression.is_suppressed(session, phone=phone):
        return WhatsAppOutcome(sent=False, blocked_reason="phone_suppressed")
    if not await consent.window_open(session, phone, clock):
        return WhatsAppOutcome(sent=False, blocked_reason="outside_customer_service_window")
    result = await registry.whatsapp.send_text(phone, text)
    return WhatsAppOutcome(sent=result.ok, blocked_reason="" if result.ok else (result.error or ""), provider_id=result.provider_message_id)


async def send_to_prospect(
    session: AsyncSession,
    *,
    registry: AdapterRegistry,
    prospect: CampaignProspect,
    company: Company,
    contact: Contact,
    text: str,
    clock: Clock,
    settings: Settings,
) -> WhatsAppOutcome:
    del company, settings
    phone = contact_phone(contact)
    if not phone:
        return WhatsAppOutcome(sent=False, blocked_reason="no_phone")
    if prospect.replied_at is None and not await consent.window_open(session, phone, clock):
        # cold outreach over WhatsApp is never allowed
        raise Blocked("whatsapp_requires_opt_in")
    return await send_in_window_text(session, registry=registry, phone=phone, text=text, clock=clock)
