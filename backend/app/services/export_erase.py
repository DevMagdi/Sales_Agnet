"""Data export and erasure (GDPR-style), plus retention jobs.

Erasure deletes PII, keeps a hashed suppression entry (so the person is never re-imported and
contacted) and writes an audit row. Nothing else about the person is retained.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.crypto import blind_index
from app.core.errors import NotFound
from app.core.text import domain_of_email, email_match_key, mask_email
from app.db.models import (
    Company,
    Contact,
    InboundMessage,
    MessageEvent,
    OutreachMessage,
    PageCache,
    Reply,
    ResearchSnapshot,
    Task,
)
from app.db.models.enums import SuppressionReason
from app.services import audit, suppression
from app.services.contacts import contact_email, contact_phone


async def export_contact(session: AsyncSession, contact_id: str) -> dict[str, Any]:
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise NotFound("contact not found")
    company = await session.get(Company, contact.company_id)
    messages = await session.execute(
        select(OutreachMessage).where(OutreachMessage.contact_id == contact_id)
    )
    replies = await session.execute(select(Reply).where(Reply.campaign_prospect_id.is_not(None)).limit(0))
    del replies
    return {
        "contact": {
            "id": contact.id,
            "full_name": contact.full_name,
            "role_title": contact.role_title,
            "role_category": contact.role_category,
            "email": contact_email(contact),
            "email_status": contact.email_status,
            "email_source_url": contact.email_source_url,
            "phone": contact_phone(contact),
            "phone_source_url": contact.phone_source_url,
            "preferred_language": contact.preferred_language,
            "created_at": contact.created_at.isoformat(),
        },
        "company": {
            "id": company.id,
            "name": company.name,
            "domain": company.domain,
            "country": company.country,
            "industry": company.industry,
        }
        if company
        else None,
        "messages": [
            {
                "id": m.id,
                "direction": m.direction,
                "step": m.step,
                "subject": m.subject,
                "body_text": m.body_text,
                "status": m.status,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            }
            for m in messages.scalars().all()
        ],
        "suppressed": bool(
            await suppression.is_suppressed(
                session, email=contact_email(contact) or None, phone=contact_phone(contact) or None
            )
        ),
    }


async def export_company(session: AsyncSession, company_id: str) -> dict[str, Any]:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("company not found")
    contacts = await session.execute(select(Contact).where(Contact.company_id == company_id))
    snapshot = await session.execute(
        select(ResearchSnapshot)
        .where(ResearchSnapshot.company_id == company_id)
        .order_by(ResearchSnapshot.version.desc())
        .limit(1)
    )
    research = snapshot.scalars().first()
    return {
        "company": {
            "id": company.id,
            "name": company.name,
            "name_ar": company.name_ar,
            "domain": company.domain,
            "website": company.website,
            "country": company.country,
            "city": company.city,
            "industry": company.industry,
            "icp_score": company.icp_score,
            "icp_reasons": company.icp_reasons,
            "status": company.status,
            "excluded_reason": company.excluded_reason,
            "source": company.source,
        },
        "contacts": [
            {
                "id": c.id,
                "full_name": c.full_name,
                "role_category": c.role_category,
                # exports are for review and portability, not for building a mailing list:
                # every address is masked, exactly like the CSV exports.
                "email": mask_email(contact_email(c)),
                "email_status": c.email_status,
                "email_source_url": c.email_source_url,
            }
            for c in contacts.scalars().all()
        ],
        "research": {
            "extracted": research.extracted if research else {},
            "evidence": research.evidence if research else [],
            "model": research.model if research else "",
        },
    }


async def erase_contact(
    session: AsyncSession, contact_id: str, *, actor: str, clock: Clock, reason: str = "erasure_request"
) -> dict[str, Any]:
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise NotFound("contact not found")
    email = contact_email(contact)
    phone = contact_phone(contact)
    if email:
        await suppression.add(
            session,
            kind="email",
            value=email,
            reason=SuppressionReason.ERASURE,
            created_by=actor,
            note=reason,
        )
        domain = domain_of_email(email)
        if domain:
            await suppression.add(
                session,
                kind="domain",
                value=domain,
                reason=SuppressionReason.ERASURE,
                created_by=actor,
                note="domain erased with contact",
            )
    if phone:
        await suppression.add(
            session, kind="phone", value=phone, reason=SuppressionReason.ERASURE, created_by=actor, note=reason
        )
    hashes = {
        "email_hash": blind_index(email_match_key(email), purpose="email") if email else None,
        "phone_hash": blind_index(phone, purpose="phone") if phone else None,
    }
    messages = await session.execute(
        select(OutreachMessage).where(OutreachMessage.contact_id == contact_id)
    )
    for message in messages.scalars().all():
        message.body_text = "[erased]"
        message.subject = "[erased]"
        message.provider_message_id = None
    inbound = await session.execute(select(InboundMessage).where(InboundMessage.contact_id == contact_id))
    for row in inbound.scalars().all():
        row.body_text = "[erased]"
        row.cleaned_text = "[erased]"
        row.from_email_enc = None
        row.from_label = "[erased]"
    await session.execute(delete(Task).where(Task.contact_id == contact_id))
    contact.full_name = None
    contact.email_enc = None
    contact.email_source_url = None
    contact.phone_enc = None
    contact.phone_source_url = None
    contact.email_status = "invalid"
    await session.flush()
    await audit.record(
        session,
        action="contact_erased",
        entity_type="contact",
        entity_id=contact_id,
        actor_label=actor,
        detail={"reason": reason, "retained": "hashed suppression only", **hashes},
    )
    return {"erased": True, "contact_id": contact_id, "suppression_written": True}


async def purge_retention(session: AsyncSession, *, settings: Settings, clock: Clock) -> dict[str, int]:
    """Delete raw page text, closed-lost contacts and old messages per retention policy."""
    page_cutoff = clock.now() - timedelta(days=settings.retention_page_text_days)
    pages = await session.execute(delete(PageCache).where(PageCache.fetched_at < page_cutoff))
    message_cutoff = clock.now() - timedelta(days=settings.retention_message_days)
    old_events = await session.execute(
        delete(MessageEvent).where(MessageEvent.occurred_at < message_cutoff)
    )
    closed_cutoff = clock.now() - timedelta(days=settings.retention_closed_contact_days)
    old_messages = await session.execute(
        delete(OutreachMessage).where(
            OutreachMessage.created_at < message_cutoff, OutreachMessage.status == "blocked"
        )
    )
    replies = await session.execute(delete(Reply).where(Reply.created_at < closed_cutoff))
    await session.flush()
    result = {
        "page_cache_deleted": int(cast(CursorResult[Any], pages).rowcount or 0),
        "message_events_deleted": int(cast(CursorResult[Any], old_events).rowcount or 0),
        "blocked_messages_deleted": int(cast(CursorResult[Any], old_messages).rowcount or 0),
        "replies_deleted": int(cast(CursorResult[Any], replies).rowcount or 0),
    }
    await audit.record(
        session, action="retention_purge", entity_type="system", detail=result
    )
    return result
