"""WhatsApp consent store and the 24-hour customer-service window.

Outbound WhatsApp is possible only when:
  (a) the contact wrote first and we are inside the 24h window, or
  (b) a recorded opt-in exists and the message is an approved template.
Evidence (source + timestamp + the message that granted it) is stored with every record.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.crypto import blind_index
from app.core.text import normalise_phone
from app.db.models import ConsentRecord, InboundMessage
from app.db.models.enums import ConsentStatus
from app.services import audit

WINDOW_HOURS = 24


def phone_hash(phone: str) -> str:
    return blind_index(normalise_phone(phone), purpose="phone")


async def record_opt_in(
    session: AsyncSession, *, phone: str, source: str, evidence: dict[str, Any], clock: Clock
) -> ConsentRecord:
    record = ConsentRecord(
        phone_hash=phone_hash(phone),
        channel="whatsapp",
        status=ConsentStatus.OPTED_IN,
        source=source,
        evidence=evidence,
        occurred_at=clock.now(),
    )
    session.add(record)
    await session.flush()
    await audit.record(
        session,
        action="consent_opted_in",
        entity_type="consent",
        entity_id=record.id,
        actor_label="system",
        detail={"source": source},
    )
    return record


async def record_opt_out(session: AsyncSession, *, phone: str, reason: str, clock: Clock) -> ConsentRecord:
    record = ConsentRecord(
        phone_hash=phone_hash(phone),
        channel="whatsapp",
        status=ConsentStatus.OPTED_OUT,
        source="reply",
        evidence={"reason": reason},
        occurred_at=clock.now(),
    )
    session.add(record)
    await session.flush()
    return record


async def latest_status(session: AsyncSession, phone: str) -> str | None:
    result = await session.execute(
        select(ConsentRecord)
        .where(ConsentRecord.phone_hash == phone_hash(phone), ConsentRecord.channel == "whatsapp")
        .order_by(ConsentRecord.occurred_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row.status if row else None


async def has_opt_in(session: AsyncSession, phone: str) -> bool:
    return await latest_status(session, phone) == ConsentStatus.OPTED_IN


async def window_open(session: AsyncSession, phone: str, clock: Clock, hours: int = WINDOW_HOURS) -> bool:
    result = await session.execute(
        select(InboundMessage)
        .where(
            InboundMessage.channel == "whatsapp",
            InboundMessage.from_phone_hash == phone_hash(phone),
        )
        .order_by(InboundMessage.received_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    received = row.received_at
    now = clock.now()
    if received.tzinfo is None:
        from datetime import UTC

        received = received.replace(tzinfo=UTC)
    return (now - received) <= timedelta(hours=hours)
