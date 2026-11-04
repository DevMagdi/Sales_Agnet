"""Inbound ingestion: one entry point for IMAP polling and provider/WhatsApp webhooks.

Dedupe by Message-ID, match to the thread via In-Reply-To/References then subject token then
sender address, strip quoted text and signatures before classification, and detect DSN bounces.
Inbound text is untrusted: injection attempts are flagged, never executed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import injection
from app.core.clock import Clock
from app.core.config import Settings
from app.core.crypto import blind_index
from app.core.intents import detect_bounce, looks_automated, looks_out_of_office
from app.core.logging import get_logger
from app.core.text import (
    detect_language,
    email_match_key,
    extract_emails,
    mask_email,
    normalise_phone,
    normalise_space,
)
from app.db.models import (
    CampaignProspect,
    Company,
    Contact,
    InboundMessage,
    OutreachMessage,
)
from app.db.models.enums import MessageEventType, MessageStatus, ProspectState
from app.services import audit, consent

logger = get_logger("inbound")

QUOTE_HEADER_PATTERNS = [
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^On .{5,120} wrote:$", re.IGNORECASE),
    re.compile(r"^From:\s.+$", re.IGNORECASE),
    re.compile(r"^(?:في|بتاريخ|كتب|كتبت).{0,80}(?:كتب|كتبت)?\s*:$"),
    re.compile(r"^_{10,}$"),
    re.compile(r"^Sent from my .+$", re.IGNORECASE),
    re.compile(r"^(?:--|__)\s*$"),
]
SIGNATURE_MARKERS = ("unsubscribe", "إلغاء الاشتراك", "confidential", "سرية", "sent from")


@dataclass
class RawInbound:
    message_id: str
    from_email: str | None = None
    from_phone: str | None = None
    from_label: str = ""
    subject: str = ""
    body_text: str = ""
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    received_at: datetime | None = None
    channel: str = "email"
    thread_hint: str | None = None


def strip_quoted(text: str) -> str:
    """Remove quoted history and signature blocks so the classifier sees only the new text."""
    lines = (text or "").splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if any(pattern.match(stripped) for pattern in QUOTE_HEADER_PATTERNS):
            break
        lowered = stripped.lower()
        if any(marker in lowered for marker in SIGNATURE_MARKERS) and len(stripped) < 80:
            continue
        kept.append(line)
    cleaned = normalise_space("\n".join(kept))
    return cleaned or normalise_space(text)


def subject_token(subject: str) -> str:
    token = re.sub(r"^\s*(?:re|fw|fwd|رد|إعادة)\s*:\s*", "", subject or "", flags=re.IGNORECASE)
    token = re.sub(r"\[[^\]]{1,40}\]", " ", token)
    return normalise_space(token).lower()[:120]


def _hash_email(email: str) -> str:
    return blind_index(email_match_key(email), purpose="email")


async def _find_thread(
    session: AsyncSession, raw: RawInbound, clock: Clock
) -> tuple[OutreachMessage | None, CampaignProspect | None]:
    if raw.in_reply_to:
        found = await session.execute(
            select(OutreachMessage).where(OutreachMessage.rfc_message_id == raw.in_reply_to)
        )
        message = found.scalars().first()
        if message is not None:
            return message, await _prospect_for(session, message)
    if raw.references:
        found = await session.execute(
            select(OutreachMessage).where(OutreachMessage.rfc_message_id.in_(raw.references))
        )
        message = found.scalars().first()
        if message is not None:
            return message, await _prospect_for(session, message)
    if raw.subject:
        token = subject_token(raw.subject)
        recent = await session.execute(
            select(OutreachMessage)
            .where(
                OutreachMessage.direction == "outbound",
                OutreachMessage.sent_at.is_not(None),
                OutreachMessage.sent_at >= clock.now() - timedelta(days=60),
            )
            .order_by(OutreachMessage.sent_at.desc())
            .limit(200)
        )
        for message in recent.scalars().all():
            if token and subject_token(message.subject) == token:
                return message, await _prospect_for(session, message)
    if raw.from_email:
        digest = _hash_email(raw.from_email)
        found = await session.execute(
            select(OutreachMessage)
            .where(
                OutreachMessage.to_email_hash == digest,
                OutreachMessage.direction == "outbound",
                OutreachMessage.sent_at.is_not(None),
            )
            .order_by(OutreachMessage.sent_at.desc())
            .limit(1)
        )
        message = found.scalars().first()
        if message is not None:
            return message, await _prospect_for(session, message)
    return None, None


async def _prospect_for(session: AsyncSession, message: OutreachMessage) -> CampaignProspect | None:
    if not message.campaign_prospect_id:
        return None
    return await session.get(CampaignProspect, message.campaign_prospect_id)


async def ingest(
    session: AsyncSession,
    raw: RawInbound,
    *,
    clock: Clock,
    settings: Settings,
) -> InboundMessage:
    del settings
    message_id = raw.message_id or f"<generated-{hashlib.sha256(raw.body_text.encode()).hexdigest()[:24]}@local>"
    existing = await session.execute(
        select(InboundMessage).where(InboundMessage.rfc_message_id == message_id)
    )
    if (found := existing.scalars().first()) is not None:
        return found

    matched, prospect = await _find_thread(session, raw, clock)
    company_id = None
    contact_id = None
    if matched is not None:
        company_id = matched.company_id
        contact_id = matched.contact_id
        matched.status = MessageStatus.REPLIED
        if prospect is not None and prospect.replied_at is None:
            prospect.replied_at = clock.now()
            prospect.next_action_at = None
            prospect.locked_until = None
    elif raw.from_email:
        digest = _hash_email(raw.from_email)
        contact_row = await session.execute(select(Contact).where(Contact.email_hash == digest))
        contact = contact_row.scalars().first()
        if contact is not None:
            contact_id = contact.id
            company_id = contact.company_id

    cleaned = strip_quoted(raw.body_text)
    flags = [f.kind for f in injection.detect(raw.body_text)]
    inbound = InboundMessage(
        channel=raw.channel,
        rfc_message_id=message_id,
        from_email_enc=None,
        from_email_hash=_hash_email(raw.from_email) if raw.from_email else None,
        from_phone_hash=blind_index(normalise_phone(raw.from_phone), purpose="phone") if raw.from_phone else None,
        from_label=raw.from_label or mask_email(raw.from_email or raw.from_phone or ""),
        subject=(raw.subject or "")[:500],
        body_text=(raw.body_text or "")[:100_000],
        cleaned_text=cleaned[:50_000],
        in_reply_to=raw.in_reply_to,
        references=raw.references or [],
        raw_headers={k.lower(): v for k, v in (raw.headers or {}).items() if isinstance(v, str)},
        company_id=company_id,
        contact_id=contact_id,
        campaign_prospect_id=prospect.id if prospect is not None else None,
        received_at=raw.received_at or clock.now(),
    )
    if raw.from_email:
        from app.core.crypto import encrypt_text

        inbound.from_email_enc = encrypt_text(raw.from_email)
    session.add(inbound)
    await session.flush()

    if flags:
        await audit.record(
            session,
            action="inbound_injection_flagged",
            entity_type="inbound_message",
            entity_id=inbound.id,
            detail={"flags": sorted(set(flags))[:10]},
        )
    if raw.channel == "whatsapp" and raw.from_phone:
        await consent.record_opt_in(
            session,
            phone=raw.from_phone,
            source="inbound_message",
            evidence={"message_id": message_id, "text": cleaned[:200]},
            clock=clock,
        )
    await audit.emit_event(
        session,
        kind="inbound_received",
        entity_type="inbound_message",
        entity_id=inbound.id,
        payload={"channel": raw.channel, "company_id": company_id},
    )
    logger.info("inbound_ingested", channel=raw.channel, matched=matched is not None)
    return inbound


def is_bounce(raw: RawInbound) -> tuple[bool, str]:
    text = f"{raw.subject}\n{raw.body_text}"
    return detect_bounce(text, raw.headers)


def is_auto_reply(raw: RawInbound) -> bool:
    return looks_automated(raw.body_text, {**raw.headers, "subject": raw.subject})


def is_out_of_office(raw: RawInbound) -> bool:
    return looks_out_of_office(raw.body_text, {**raw.headers, "subject": raw.subject})


def reply_language(text: str) -> str:
    lang = detect_language(text)
    return "en" if lang == "en" else "ar" if lang in ("ar", "mixed") else "ar"


def find_referred_email(text: str) -> str:
    emails = extract_emails(text)
    return emails[0] if emails else ""


async def company_for(session: AsyncSession, inbound: InboundMessage) -> Company | None:
    if inbound.company_id is None:
        return None
    return await session.get(Company, inbound.company_id)


async def recent_thread(session: AsyncSession, prospect_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Thread view for the inbox UI: outbound + inbound in order."""
    outbound = await session.execute(
        select(OutreachMessage)
        .where(OutreachMessage.campaign_prospect_id == prospect_id)
        .order_by(OutreachMessage.created_at.asc())
        .limit(limit)
    )
    inbound_rows = await session.execute(
        select(InboundMessage)
        .where(InboundMessage.campaign_prospect_id == prospect_id)
        .order_by(InboundMessage.received_at.asc())
        .limit(limit)
    )
    items: list[dict[str, Any]] = []
    for message in outbound.scalars().all():
        items.append(
            {
                "id": message.id,
                "direction": "outbound",
                "at": (message.sent_at or message.created_at).isoformat() if message.sent_at or message.created_at else "",
                "subject": message.subject,
                "text": message.body_text,
                "status": message.status,
                "step": message.step,
            }
        )
    for row in inbound_rows.scalars().all():
        items.append(
            {
                "id": row.id,
                "direction": "inbound",
                "at": row.received_at.isoformat(),
                "subject": row.subject,
                "text": row.cleaned_text or row.body_text,
                "status": "received",
                "step": "",
            }
        )
    items.sort(key=lambda item: str(item["at"]))
    return items


async def unanswered_count(session: AsyncSession, clock: Clock) -> int:
    since = clock.now() - timedelta(days=30)
    rows = await session.execute(
        select(InboundMessage).where(InboundMessage.received_at >= since).limit(500)
    )
    return len(list(rows.scalars().all()))


def utcnow() -> datetime:
    return datetime.now(UTC)


async def prospect_state_for_reply(state: str) -> str:
    return state if state in tuple(ProspectState) else ProspectState.PAUSED


async def bump_event(session: AsyncSession, message_id: str, clock: Clock) -> None:
    from app.db.models import MessageEvent

    session.add(
        MessageEvent(
            message_id=message_id,
            type=MessageEventType.REPLY,
            payload={},
            occurred_at=clock.now(),
        )
    )
    await session.flush()
