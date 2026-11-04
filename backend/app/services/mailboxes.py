"""Mailbox pools, warm-up ramp and per-mailbox capacity.

Warm-up ramp (days since warmup_started_at):
  1–3: 10/day · 4–7: 20 · week 2: 35 · week 3: 50 · then the mailbox cap (max 80/day).
A stage only advances when the previous stage's bounce rate < 2% and complaints == 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.crypto import decrypt_text, encrypt_text
from app.core.text import mask_email, mask_secret
from app.db.models import Mailbox, MessageEvent, OutreachMessage
from app.db.models.enums import MailboxStatus, MessageEventType, MessageStatus
from app.services import audit

HARD_MAILBOX_CAP = 80
WARMUP_RAMP: list[tuple[int, int]] = [(3, 10), (7, 20), (14, 35), (21, 50)]
MIN_SENDS_FOR_ADVANCE = 10


@dataclass
class MailboxCapacity:
    mailbox_id: str
    from_email: str
    daily_cap: int
    warmup_cap: int
    sent_today: int
    remaining: int
    warmup_stage: int

    @property
    def can_send(self) -> bool:
        return self.remaining > 0


def warmup_cap_for_days(days: int) -> int:
    for max_day, cap in WARMUP_RAMP:
        if days <= max_day:
            return cap
    return HARD_MAILBOX_CAP


def stage_for_days(days: int) -> int:
    stage = 0
    for index, (max_day, _cap) in enumerate(WARMUP_RAMP):
        if days > max_day:
            stage = index + 1
    return stage


def warmup_cap(mailbox: Mailbox, now: datetime) -> int:
    if mailbox.warmup_started_at is None:
        return min(mailbox.daily_cap, warmup_cap_for_days(0))
    started = mailbox.warmup_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    days = max(0, (now - started).days)
    return min(mailbox.daily_cap, HARD_MAILBOX_CAP, warmup_cap_for_days(days))


def _day_start_local(now: datetime, tz: str) -> datetime:
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(tz))
    start_local = datetime(local.year, local.month, local.day, tzinfo=local.tzinfo)
    return start_local.astimezone(UTC)


async def sent_today(session: AsyncSession, mailbox_id: str, clock: Clock, tz: str = "Africa/Cairo") -> int:
    start = _day_start_local(clock.now(), tz)
    result = await session.execute(
        select(func.count())
        .select_from(OutreachMessage)
        .where(
            OutreachMessage.mailbox_id == mailbox_id,
            OutreachMessage.direction == "outbound",
            OutreachMessage.sent_at.is_not(None),
            OutreachMessage.sent_at >= start,
            OutreachMessage.status.in_(
                [
                    MessageStatus.SENT,
                    MessageStatus.DELIVERED,
                    MessageStatus.REPLIED,
                    MessageStatus.BOUNCED_SOFT,
                    MessageStatus.BOUNCED_HARD,
                ]
            ),
        )
    )
    return int(result.scalar_one())


async def capacity(
    session: AsyncSession, mailbox: Mailbox, clock: Clock, tz: str = "Africa/Cairo"
) -> MailboxCapacity:
    cap = warmup_cap(mailbox, clock.now())
    sent = await sent_today(session, mailbox.id, clock, tz)
    return MailboxCapacity(
        mailbox_id=mailbox.id,
        from_email=mask_email(mailbox.from_email),
        daily_cap=mailbox.daily_cap,
        warmup_cap=cap,
        sent_today=sent,
        remaining=max(0, cap - sent),
        warmup_stage=mailbox.warmup_stage,
    )


async def pick_mailbox(
    session: AsyncSession, *, pool_id: str | None, clock: Clock, tz: str = "Africa/Cairo", exclude: list[str] | None = None
) -> Mailbox | None:
    stmt = select(Mailbox).where(Mailbox.status == MailboxStatus.ACTIVE)
    if pool_id:
        stmt = stmt.where(Mailbox.pool_id == pool_id)
    if exclude:
        stmt = stmt.where(Mailbox.id.not_in(exclude))
    stmt = stmt.order_by(Mailbox.last_sent_at.asc().nulls_first())
    result = await session.execute(stmt)
    for mailbox in result.scalars().all():
        cap = await capacity(session, mailbox, clock, tz)
        if cap.can_send:
            return mailbox
    return None


async def create(
    session: AsyncSession,
    *,
    from_email: str,
    from_name: str,
    pool_id: str | None = None,
    daily_cap: int = 20,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_username: str | None = None,
    smtp_password: str | None = None,
    imap_host: str | None = None,
    imap_port: int | None = None,
    imap_username: str | None = None,
    imap_password: str | None = None,
    signature_text: str = "",
    physical_address: str = "",
    phone: str = "",
    reply_to: str | None = None,
    provider: str = "smtp",
    created_by: str | None = None,
) -> Mailbox:
    mailbox = Mailbox(
        from_email=from_email.strip().lower(),
        from_name=from_name,
        pool_id=pool_id,
        daily_cap=min(daily_cap, HARD_MAILBOX_CAP),
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password_enc=encrypt_text(smtp_password) if smtp_password else None,
        imap_host=imap_host,
        imap_port=imap_port,
        imap_username=imap_username,
        imap_password_enc=encrypt_text(imap_password) if imap_password else None,
        signature_text=signature_text,
        physical_address=physical_address,
        phone=phone,
        reply_to=reply_to or from_email,
        provider=provider,
        status=MailboxStatus.ACTIVE,
    )
    session.add(mailbox)
    await session.flush()
    await audit.record(
        session,
        action="mailbox_created",
        entity_type="mailbox",
        entity_id=mailbox.id,
        actor_id=created_by,
        detail={"from_email": mask_email(from_email)},
    )
    return mailbox


def smtp_credentials(mailbox: Mailbox) -> dict[str, Any]:
    return {
        "host": mailbox.smtp_host,
        "port": mailbox.smtp_port,
        "username": mailbox.smtp_username,
        "password": decrypt_text(mailbox.smtp_password_enc),
    }


def public_view(mailbox: Mailbox) -> dict[str, Any]:
    """Secrets are write-only: APIs return a masked hint, never the value."""
    return {
        "id": mailbox.id,
        "from_name": mailbox.from_name,
        "from_email": mailbox.from_email,
        "reply_to": mailbox.reply_to,
        "pool_id": mailbox.pool_id,
        "provider": mailbox.provider,
        "status": mailbox.status,
        "daily_cap": mailbox.daily_cap,
        "warmup_stage": mailbox.warmup_stage,
        "warmup_started_at": mailbox.warmup_started_at,
        "signature_text": mailbox.signature_text,
        "physical_address": mailbox.physical_address,
        "phone": mailbox.phone,
        "last_error": mailbox.last_error,
        "smtp_host": mailbox.smtp_host,
        "smtp_port": mailbox.smtp_port,
        "smtp_password_masked": mask_secret(decrypt_text(mailbox.smtp_password_enc)),
        "imap_host": mailbox.imap_host,
        "imap_password_masked": mask_secret(decrypt_text(mailbox.imap_password_enc)),
        "api_key_masked": mask_secret(decrypt_text(mailbox.api_key_enc)),
    }


async def readiness_bounce_rate(session: AsyncSession, mailbox_id: str, clock: Clock, days: int = 7) -> tuple[float, int, int]:
    """Return (bounce_rate, hard_bounces, complaints) over the trailing window."""
    since = clock.now() - timedelta(days=days)
    sent = await session.execute(
        select(func.count())
        .select_from(OutreachMessage)
        .where(
            OutreachMessage.mailbox_id == mailbox_id,
            OutreachMessage.sent_at.is_not(None),
            OutreachMessage.sent_at >= since,
        )
    )
    total = int(sent.scalar_one())
    if total == 0:
        return 0.0, 0, 0
    events = await session.execute(
        select(MessageEvent.type, func.count())
        .join(OutreachMessage, OutreachMessage.id == MessageEvent.message_id)
        .where(
            OutreachMessage.mailbox_id == mailbox_id,
            MessageEvent.occurred_at >= since,
            MessageEvent.type.in_([MessageEventType.BOUNCE_HARD, MessageEventType.COMPLAINT]),
        )
        .group_by(MessageEvent.type)
    )
    counts = {str(kind): int(total_) for kind, total_ in events.all()}
    hard = counts.get(MessageEventType.BOUNCE_HARD, 0)
    complaints = counts.get(MessageEventType.COMPLAINT, 0)
    return (hard / total if total else 0.0), hard, complaints


async def advance_warmup(session: AsyncSession, clock: Clock) -> list[dict[str, Any]]:
    """Advance the warm-up stage when the trailing window looks healthy."""
    result = await session.execute(
        select(Mailbox).where(Mailbox.status == MailboxStatus.ACTIVE, Mailbox.warmup_started_at.is_not(None))
    )
    advanced: list[dict[str, Any]] = []
    for mailbox in result.scalars().all():
        started = mailbox.warmup_started_at
        if started is None:  # pragma: no cover - filtered above
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        days = max(0, (clock.now() - started).days)
        target_stage = stage_for_days(days)
        if target_stage <= mailbox.warmup_stage:
            continue
        bounce_rate, hard, complaints = await readiness_bounce_rate(session, mailbox.id, clock)
        sends = await sent_today(session, mailbox.id, clock)
        if hard + complaints == 0 and sends >= MIN_SENDS_FOR_ADVANCE:
            previous = mailbox.warmup_stage
            mailbox.warmup_stage = target_stage
            advanced.append(
                {"mailbox_id": mailbox.id, "from": previous, "to": target_stage, "bounce_rate": bounce_rate}
            )
            await audit.record(
                session,
                action="warmup_advanced",
                entity_type="mailbox",
                entity_id=mailbox.id,
                detail={"from": previous, "to": target_stage},
            )
        elif bounce_rate >= 0.02 or complaints > 0:
            advanced.append(
                {"mailbox_id": mailbox.id, "held": True, "bounce_rate": bounce_rate, "complaints": complaints}
            )
    await session.flush()
    return advanced
