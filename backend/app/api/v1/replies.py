"""Inbox: replies, threads, classification overrides and human-approved sends."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import AnyUser, ClockDep, DbDep, RegistryDep, SettingsDep, WriterUser
from app.api.schemas import ReplyOverride, SendReplyRequest
from app.core.errors import NotFound, ValidationFailed
from app.db.models import (
    CampaignProspect,
    Company,
    Contact,
    InboundMessage,
    Reply,
)
from app.db.models.enums import MessageStatus, ReplyStatus
from app.services import audit, handoff, reply_handler, sender, state_machine
from app.services import contacts as contact_service
from app.services import inbound as inbound_service

router = APIRouter(prefix="/replies", tags=["replies"])


def reply_view(reply: Reply, company_name: str = "", body: str = "") -> dict[str, Any]:
    return {
        "id": reply.id,
        "inbound_message_id": reply.inbound_message_id,
        "campaign_prospect_id": reply.campaign_prospect_id,
        "company_name": company_name,
        "classification": reply.classification,
        "confidence": reply.confidence,
        "extracted": reply.extracted,
        "status": reply.status,
        "opt_out_flagged": reply.opt_out_flagged,
        "body": body[:2000],
        "created_at": reply.created_at.isoformat() if reply.created_at else None,
    }


@router.get("")
async def list_replies(
    session: DbDep, user: AnyUser, status: str | None = None, classification: str | None = None, limit: int = 50
) -> dict[str, Any]:
    del user
    stmt = select(Reply).order_by(Reply.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Reply.status == status)
    if classification:
        stmt = stmt.where(Reply.classification == classification)
    rows = await session.execute(stmt)
    items = []
    for reply in rows.scalars().all():
        prospect = (
            await session.get(CampaignProspect, reply.campaign_prospect_id)
            if reply.campaign_prospect_id
            else None
        )
        company = await session.get(Company, prospect.company_id) if prospect else None
        inbound = await session.get(InboundMessage, reply.inbound_message_id)
        items.append(
            reply_view(
                reply,
                company.name if company else "",
                (inbound.cleaned_text or inbound.body_text) if inbound else "",
            )
        )
    return {"items": items, "total": len(items)}


@router.get("/{reply_id}")
async def get_reply(reply_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    reply = await session.get(Reply, reply_id)
    if reply is None:
        raise NotFound("reply not found")
    inbound = await session.get(InboundMessage, reply.inbound_message_id)
    prospect = (
        await session.get(CampaignProspect, reply.campaign_prospect_id)
        if reply.campaign_prospect_id
        else None
    )
    company = await session.get(Company, prospect.company_id) if prospect else None
    contact = await session.get(Contact, prospect.contact_id) if prospect and prospect.contact_id else None
    thread = await inbound_service.recent_thread(session, prospect.id) if prospect else []
    return {
        "reply": reply_view(
            reply,
            company.name if company else "",
            (inbound.cleaned_text or inbound.body_text) if inbound else "",
        ),
        "inbound": {
            "subject": inbound.subject if inbound else "",
            "from": inbound.from_label if inbound else "",
            "received_at": inbound.received_at.isoformat() if inbound else None,
            "headers": inbound.raw_headers if inbound else {},
        },
        "company": {"id": company.id, "name": company.name, "industry": company.industry} if company else None,
        "contact": contact_service.public_view(contact) if contact else None,
        "thread": thread,
    }


@router.post("/{reply_id}/override")
async def override(
    reply_id: str, payload: ReplyOverride, session: DbDep, user: WriterUser, clock: ClockDep,
    settings: SettingsDep, registry: RegistryDep,
) -> dict[str, Any]:
    reply = await session.get(Reply, reply_id)
    if reply is None:
        raise NotFound("reply not found")
    previous = reply.classification
    reply.classification = payload.classification
    reply.status = ReplyStatus.NEW
    reply.notes = f"{reply.notes}\noverride by {user.email}: {previous} → {payload.classification}: {payload.reason}"
    await session.flush()
    await audit.record(
        session, action="reply_reclassified", entity_type="reply", entity_id=reply.id,
        actor_id=user.id, actor_label=user.email, detail={"from": previous, "to": payload.classification},
    )
    inbound = await session.get(InboundMessage, reply.inbound_message_id)
    if inbound is None:
        raise NotFound("inbound message missing")
    outcome = await reply_handler.handle(
        session, reply, inbound, registry=registry, clock=clock, settings=settings
    )
    return outcome.as_dict()


@router.post("/{reply_id}/send")
async def send_reply(
    reply_id: str,
    payload: SendReplyRequest,
    session: DbDep,
    user: WriterUser,
    clock: ClockDep,
    settings: SettingsDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    reply = await session.get(Reply, reply_id)
    if reply is None:
        raise NotFound("reply not found")
    inbound = await session.get(InboundMessage, reply.inbound_message_id)
    prospect = (
        await session.get(CampaignProspect, reply.campaign_prospect_id)
        if reply.campaign_prospect_id
        else None
    )
    if inbound is None or prospect is None:
        raise ValidationFailed("reply is not linked to a prospect thread")
    company = await session.get(Company, prospect.company_id)
    contact = await session.get(Contact, prospect.contact_id) if prospect.contact_id else None
    if company is None or contact is None:
        raise ValidationFailed("missing company or contact")
    message = await sender.send_reply_message(
        session,
        prospect=prospect,
        company=company,
        contact=contact,
        step="reply_info",
        locale=inbound_service.reply_language(payload.body_text),
        subject=payload.subject,
        body_text=payload.body_text,
        in_reply_to=inbound.rfc_message_id,
        references=list(inbound.references or []) + [inbound.rfc_message_id],
        registry=registry,
        clock=clock,
        settings=settings,
    )
    if message.status == MessageStatus.QUEUED:
        await sender.send_one(
            session, message.id, registry=registry, clock=clock, settings=settings, worker_id=f"api:{user.id}"
        )
        await session.refresh(message)
    reply.status = ReplyStatus.HANDLED
    reply.handled_by = user.id
    await session.flush()
    await audit.record(
        session, action="reply_sent_by_human", entity_type="reply", entity_id=reply.id,
        actor_id=user.id, actor_label=user.email, detail={"message_id": message.id, "status": message.status},
    )
    return {
        "message_id": message.id,
        "status": message.status,
        "blocked_reason": message.blocked_reason,
        "gate_report": message.check_report,
    }


@router.post("/{reply_id}/handoff")
async def create_handoff(
    reply_id: str, session: DbDep, user: WriterUser, clock: ClockDep, settings: SettingsDep, registry: RegistryDep
) -> dict[str, Any]:
    reply = await session.get(Reply, reply_id)
    if reply is None:
        raise NotFound("reply not found")
    inbound = await session.get(InboundMessage, reply.inbound_message_id)
    prospect = (
        await session.get(CampaignProspect, reply.campaign_prospect_id)
        if reply.campaign_prospect_id
        else None
    )
    if inbound is None or prospect is None:
        raise ValidationFailed("reply is not linked to a prospect thread")
    company = await session.get(Company, prospect.company_id)
    contact = await session.get(Contact, prospect.contact_id) if prospect.contact_id else None
    if company is None:
        raise ValidationFailed("company missing")
    result = await handoff.handoff(
        session, company=company, contact=contact, prospect=prospect, reply=reply,
        inbound=inbound, registry=registry, clock=clock, settings=settings,
    )
    if prospect is not None and state_machine.is_legal(prospect.state, "handed_off"):
        await state_machine.transition(session, prospect, "handed_off", clock=clock, reason="manual_handoff")
    await audit.record(
        session, action="handoff_created_manual", entity_type="reply", entity_id=reply.id,
        actor_id=user.id, actor_label=user.email, detail=result,
    )
    return result


@router.get("/threads/{prospect_id}")
async def thread(prospect_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    return {"items": await inbound_service.recent_thread(session, prospect_id)}
