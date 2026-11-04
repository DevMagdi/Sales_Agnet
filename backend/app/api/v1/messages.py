"""Messages: list, detail (with the full gate report), review queue and actions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import AnyUser, ClockDep, DbDep, RegistryDep, SettingsDep, WriterUser
from app.api.schemas import MessageReview
from app.core.errors import NotFound, ValidationFailed
from app.db.models import CampaignProspect, Company, Contact, OutreachMessage
from app.db.models.enums import MessageStatus
from app.services import audit, send_gate, sender, sequencer
from app.services import contacts as contact_service

router = APIRouter(prefix="/messages", tags=["messages"])


def message_view(message: OutreachMessage, company_name: str = "", to_masked: str = "") -> dict[str, Any]:
    return {
        "id": message.id,
        "campaign_prospect_id": message.campaign_prospect_id,
        "company_id": message.company_id,
        "company_name": company_name,
        "contact_id": message.contact_id,
        "to_masked": to_masked,
        "channel": message.channel,
        "direction": message.direction,
        "step": message.step,
        "locale": message.locale,
        "subject": message.subject,
        "body_text": message.body_text,
        "status": message.status,
        "blocked_reason": message.blocked_reason,
        "word_count": message.word_count,
        "provider": message.provider,
        "rfc_message_id": message.rfc_message_id,
        "scheduled_for": message.scheduled_for.isoformat() if message.scheduled_for else None,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "check_report": message.check_report,
        "claim_report": message.claim_report,
        "evidence_ids": message.evidence_ids,
    }


@router.get("")
async def list_messages(
    session: DbDep,
    user: AnyUser,
    status: str | None = None,
    campaign_id: str | None = None,
    step: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    del user
    stmt = select(OutreachMessage)
    count_stmt = select(func.count()).select_from(OutreachMessage)
    filters = []
    if status:
        filters.append(OutreachMessage.status == status)
    if step:
        filters.append(OutreachMessage.step == step)
    if campaign_id:
        stmt = stmt.join(CampaignProspect, CampaignProspect.id == OutreachMessage.campaign_prospect_id)
        count_stmt = count_stmt.join(
            CampaignProspect, CampaignProspect.id == OutreachMessage.campaign_prospect_id
        )
        filters.append(CampaignProspect.campaign_id == campaign_id)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = await session.execute(
        stmt.order_by(OutreachMessage.created_at.desc()).limit(limit).offset(offset)
    )
    items = []
    for message in rows.scalars().all():
        company = await session.get(Company, message.company_id)
        contact = await session.get(Contact, message.contact_id) if message.contact_id else None
        items.append(
            message_view(
                message,
                company.name if company else "",
                str(contact_service.public_view(contact)["email_masked"]) if contact else "",
            )
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/review")
async def review_queue(session: DbDep, user: AnyUser, limit: int = 50) -> dict[str, Any]:
    del user
    rows = await session.execute(
        select(OutreachMessage)
        .where(OutreachMessage.status == MessageStatus.NEEDS_REVIEW)
        .order_by(OutreachMessage.created_at.asc())
        .limit(limit)
    )
    items = []
    for message in rows.scalars().all():
        company = await session.get(Company, message.company_id)
        contact = await session.get(Contact, message.contact_id) if message.contact_id else None
        items.append(
            message_view(
                message,
                company.name if company else "",
                str(contact_service.public_view(contact)["email_masked"]) if contact else "",
            )
        )
    return {"items": items, "total": len(items)}


@router.get("/{message_id}")
async def get_message(message_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    message = await session.get(OutreachMessage, message_id)
    if message is None:
        raise NotFound("message not found")
    company = await session.get(Company, message.company_id)
    contact = await session.get(Contact, message.contact_id) if message.contact_id else None
    return message_view(
        message,
        company.name if company else "",
        str(contact_service.public_view(contact)["email_masked"]) if contact else "",
    )


@router.post("/{message_id}/review")
async def review_message(
    message_id: str,
    payload: MessageReview,
    session: DbDep,
    user: WriterUser,
    clock: ClockDep,
    settings: SettingsDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    message = await session.get(OutreachMessage, message_id)
    if message is None:
        raise NotFound("message not found")
    if message.status != MessageStatus.NEEDS_REVIEW:
        raise ValidationFailed("message is not in the review queue")
    if payload.action == "reject":
        message.status = MessageStatus.BLOCKED
        message.blocked_reason = (payload.reason or "rejected_by_reviewer")[:190]
        await audit.record(
            session, action="message_rejected", entity_type="outreach_message", entity_id=message.id,
            actor_id=user.id, actor_label=user.email, detail={"reason": payload.reason},
        )
        return message_view(message, "")

    if payload.subject:
        message.subject = payload.subject[:300]
    if payload.body_text:
        message.body_text = payload.body_text
        message.word_count = len(payload.body_text.split())
    prospect = (
        await session.get(CampaignProspect, message.campaign_prospect_id)
        if message.campaign_prospect_id
        else None
    )
    company = await session.get(Company, message.company_id)
    if company is None:
        raise NotFound("company for this message no longer exists")
    contact = await session.get(Contact, message.contact_id) if message.contact_id else None
    campaign = None
    if prospect is not None:
        from app.db.models import Campaign

        campaign = await session.get(Campaign, prospect.campaign_id)
    mailbox = None
    if message.mailbox_id:
        from app.db.models import Mailbox

        mailbox = await session.get(Mailbox, message.mailbox_id)
    ctx = await send_gate.build_context(
        session,
        company=company,
        contact=contact,
        campaign=campaign,
        prospect=prospect,
        mailbox=mailbox,
        message=message,
        channel=message.channel,
        step=message.step,
        locale=message.locale,
        subject=message.subject,
        body_text=message.body_text,
        unsubscribe_url=f"{settings.public_base_url}/u/preview-token",
        settings=settings,
        clock=clock,
        claims_report=message.claim_report or None,
    )
    report = await send_gate.evaluate(session, ctx, clock, settings)
    message.check_report = report.as_dict()
    if not report.passed:
        message.status = MessageStatus.BLOCKED
        message.blocked_reason = (report.blocked_reason or "blocked")[:190]
        await session.flush()
        await audit.record(
            session, action="message_review_blocked", entity_type="outreach_message", entity_id=message.id,
            actor_id=user.id, actor_label=user.email, detail={"reason": message.blocked_reason},
        )
        return message_view(message, company.name if company else "")
    message.status = MessageStatus.QUEUED
    message.scheduled_for = clock.now()
    message.blocked_reason = None
    await session.flush()
    await audit.record(
        session, action="message_approved", entity_type="outreach_message", entity_id=message.id,
        actor_id=user.id, actor_label=user.email, detail={"edited": bool(payload.body_text or payload.subject)},
    )
    return message_view(message, company.name if company else "")


@router.post("/{message_id}/requeue")
async def requeue(message_id: str, session: DbDep, user: WriterUser, clock: ClockDep) -> dict[str, Any]:
    message = await session.get(OutreachMessage, message_id)
    if message is None:
        raise NotFound("message not found")
    if message.status not in (MessageStatus.BLOCKED, MessageStatus.FAILED):
        raise ValidationFailed("only blocked or failed messages can be requeued")
    message.status = MessageStatus.QUEUED
    message.scheduled_for = clock.now()
    message.blocked_reason = None
    await session.flush()
    await audit.record(
        session, action="message_requeued", entity_type="outreach_message", entity_id=message.id,
        actor_id=user.id, actor_label=user.email,
    )
    return message_view(message, "")


@router.post("/send-due")
async def send_due(
    session: DbDep,
    user: WriterUser,
    registry: RegistryDep,
    clock: ClockDep,
    settings: SettingsDep,
    limit: int = 10,
) -> dict[str, Any]:
    batch = await sender.send_due(
        session, registry=registry, clock=clock, settings=settings, limit=limit, worker_id=f"api:{user.id}"
    )
    await audit.record(
        session, action="send_batch_triggered", entity_type="system", actor_id=user.id,
        actor_label=user.email, detail=batch.as_dict(),
    )
    return batch.as_dict()


@router.post("/compose-due")
async def compose_due(
    session: DbDep,
    user: WriterUser,
    registry: RegistryDep,
    clock: ClockDep,
    settings: SettingsDep,
    limit: int = 10,
) -> dict[str, Any]:
    """Compose the due steps now (the worker does this on a schedule; this button is for humans)."""
    batch = await sequencer.compose_due(
        session, registry=registry, clock=clock, settings=settings, limit=limit
    )
    await audit.record(
        session, action="compose_batch_triggered", entity_type="system", actor_id=user.id,
        actor_label=user.email,
        detail={"composed": batch.composed, "queued": batch.queued, "review": batch.review, "blocked": batch.blocked},
    )
    return {
        "composed": batch.composed,
        "queued": batch.queued,
        "review": batch.review,
        "blocked": batch.blocked,
        "notes": batch.notes,
    }
