"""Campaign CRUD, lifecycle, preview (full gate in check-only mode), stats and capacity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import AnyUser, ClockDep, DbDep, RegistryDep, SettingsDep, WriterUser
from app.api.schemas import CampaignCreate, CampaignStatusUpdate, CampaignUpdate, PreviewRequest
from app.core.errors import NotFound, ValidationFailed
from app.db.models import Campaign, CampaignProspect, Company, Contact, Mailbox, OutreachMessage
from app.db.models.enums import CampaignStatus
from app.services import audit, composer, sequencer
from app.services import contacts as contact_service
from app.services import mailboxes as mailbox_service

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def campaign_view(campaign: Campaign) -> dict[str, Any]:
    return {
        "id": campaign.id,
        "name": campaign.name,
        "status": campaign.status,
        "industry_filter": campaign.industry_filter,
        "country_filter": campaign.country_filter,
        "city_filter": campaign.city_filter,
        "min_icp_score": campaign.min_icp_score,
        "language_policy": campaign.language_policy,
        "template_set": campaign.template_set,
        "mailbox_pool_id": campaign.mailbox_pool_id,
        "daily_cap": campaign.daily_cap,
        "ramp_schedule": campaign.ramp_schedule,
        "send_windows": campaign.send_windows,
        "timezone": campaign.timezone,
        "review_first_n": campaign.review_first_n,
        "auto_reply_classes": campaign.auto_reply_classes,
        "channels_enabled": campaign.channels_enabled,
        "max_followups": campaign.max_followups,
        "followup_gaps_days": campaign.followup_gaps_days,
        "allow_risky_emails": campaign.allow_risky_emails,
        "stopped_reason": campaign.stopped_reason,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
    }


@router.get("")
async def list_campaigns(session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    rows = await session.execute(select(Campaign).order_by(Campaign.created_at.desc()))
    return {"items": [campaign_view(c) for c in rows.scalars().all()]}


@router.post("")
async def create_campaign(payload: CampaignCreate, session: DbDep, user: WriterUser) -> dict[str, Any]:
    campaign = Campaign(
        name=payload.name,
        status=CampaignStatus.DRAFT,
        industry_filter=payload.industry_filter,
        country_filter=payload.country_filter,
        city_filter=payload.city_filter,
        min_icp_score=payload.min_icp_score,
        language_policy=payload.language_policy,
        template_set=payload.template_set,
        mailbox_pool_id=payload.mailbox_pool_id,
        daily_cap=payload.daily_cap,
        ramp_schedule=payload.ramp_schedule,
        send_windows=payload.send_windows,
        timezone=payload.timezone,
        review_first_n=payload.review_first_n,
        auto_reply_classes=payload.auto_reply_classes,
        channels_enabled=payload.channels_enabled,
        max_followups=payload.max_followups,
        followup_gaps_days=payload.followup_gaps_days,
        allow_risky_emails=payload.allow_risky_emails,
        created_by=user.id,
    )
    session.add(campaign)
    await session.flush()
    await audit.record(
        session, action="campaign_created", entity_type="campaign", entity_id=campaign.id,
        actor_id=user.id, actor_label=user.email, detail={"name": campaign.name},
    )
    return campaign_view(campaign)


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    return campaign_view(campaign)


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: str, payload: CampaignUpdate, session: DbDep, user: WriterUser
) -> dict[str, Any]:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    before = campaign_view(campaign)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if value is not None and hasattr(campaign, key):
            setattr(campaign, key, value)
    await session.flush()
    await audit.record(
        session, action="campaign_updated", entity_type="campaign", entity_id=campaign.id,
        actor_id=user.id, actor_label=user.email, before=before, after=campaign_view(campaign),
    )
    return campaign_view(campaign)


@router.post("/{campaign_id}/status")
async def set_status(
    campaign_id: str,
    payload: CampaignStatusUpdate,
    session: DbDep,
    user: WriterUser,
    clock: ClockDep,
) -> dict[str, Any]:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    previous = campaign.status
    if payload.action == "activate":
        if campaign.status == CampaignStatus.STOPPED_BY_CIRCUIT_BREAKER:
            raise ValidationFailed("a campaign stopped by a circuit breaker needs an explicit resume")
        campaign.status = CampaignStatus.ACTIVE
    elif payload.action == "pause":
        campaign.status = CampaignStatus.PAUSED
    elif payload.action == "stop":
        campaign.status = CampaignStatus.COMPLETED
    elif payload.action == "resume":
        campaign.status = CampaignStatus.ACTIVE
    else:
        raise ValidationFailed("unknown action")
    await session.flush()
    await audit.record(
        session,
        action="campaign_status_changed",
        entity_type="campaign",
        entity_id=campaign.id,
        actor_id=user.id,
        actor_label=user.email,
        detail={"from": previous, "to": campaign.status, "reason": payload.reason},
    )
    await audit.emit_event(
        session, kind="campaign_status", entity_type="campaign", entity_id=campaign.id,
        payload={"status": campaign.status},
    )
    return campaign_view(campaign)


@router.post("/{campaign_id}/plan")
async def plan(campaign_id: str, session: DbDep, user: WriterUser, clock: ClockDep, settings: SettingsDep) -> dict[str, Any]:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    result = await sequencer.plan_sequences(
        session, campaign=campaign, clock=clock, settings=settings
    )
    await audit.record(
        session, action="campaign_planned", entity_type="campaign", entity_id=campaign.id,
        actor_id=user.id, actor_label=user.email, detail={"created": result.created, "skipped": result.skipped},
    )
    return {"created": result.created, "skipped": result.skipped, "reasons": result.reasons}


@router.get("/{campaign_id}/stats")
async def stats(campaign_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    return await sequencer.campaign_stats(session, campaign_id)


@router.get("/{campaign_id}/capacity")
async def capacity(campaign_id: str, session: DbDep, user: AnyUser, clock: ClockDep) -> dict[str, Any]:
    del user
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    stmt = select(Mailbox).where(Mailbox.status == "active")
    if campaign.mailbox_pool_id:
        stmt = stmt.where(Mailbox.pool_id == campaign.mailbox_pool_id)
    boxes = list((await session.execute(stmt)).scalars().all())
    caps = [await mailbox_service.capacity(session, box, clock, campaign.timezone) for box in boxes]
    return {
        "campaign_daily_cap": sequencer_cap(campaign, clock.now()),
        "mailboxes": [
            {
                "mailbox_id": cap.mailbox_id,
                "from_email": cap.from_email,
                "daily_cap": cap.daily_cap,
                "warmup_cap": cap.warmup_cap,
                "sent_today": cap.sent_today,
                "remaining": cap.remaining,
            }
            for cap in caps
        ],
    }


def sequencer_cap(campaign: Campaign, now: datetime) -> int:
    from app.services.send_gate import campaign_daily_cap

    return campaign_daily_cap(campaign, now)


@router.post("/{campaign_id}/preview")
async def preview(
    campaign_id: str,
    payload: PreviewRequest,
    session: DbDep,
    user: AnyUser,
    registry: RegistryDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    del user
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("campaign not found")
    company = await session.get(Company, payload.company_id)
    if company is None:
        raise NotFound("company not found")
    contact = (
        await session.get(Contact, payload.contact_id)
        if payload.contact_id
        else await contact_service.pick_primary(session, company.id)
    )
    if contact is None:
        raise ValidationFailed("company has no usable contact")
    return await composer.preview_message(
        session,
        campaign=campaign,
        company=company,
        contact=contact,
        step=payload.step,
        registry=registry,
        clock=clock,
        settings=settings,
    )


@router.get("/{campaign_id}/prospects")
async def prospects(campaign_id: str, session: DbDep, user: AnyUser, limit: int = 100) -> dict[str, Any]:
    del user
    rows = await session.execute(
        select(CampaignProspect)
        .where(CampaignProspect.campaign_id == campaign_id)
        .order_by(CampaignProspect.next_action_at.asc().nulls_last())
        .limit(limit)
    )
    items = []
    for prospect in rows.scalars().all():
        company = await session.get(Company, prospect.company_id)
        contact = await session.get(Contact, prospect.contact_id) if prospect.contact_id else None
        items.append(
            {
                "id": prospect.id,
                "company_id": prospect.company_id,
                "company_name": company.name if company else "",
                "contact_masked": contact_service.public_view(contact)["email_masked"] if contact else "",
                "state": prospect.state,
                "step_index": prospect.step_index,
                "next_action_at": prospect.next_action_at.isoformat() if prospect.next_action_at else None,
                "last_step_sent_at": prospect.last_step_sent_at.isoformat() if prospect.last_step_sent_at else None,
                "replied_at": prospect.replied_at.isoformat() if prospect.replied_at else None,
            }
        )
    return {"items": items}


@router.get("/{campaign_id}/messages")
async def campaign_messages(
    campaign_id: str, session: DbDep, user: AnyUser, status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    del user
    stmt = (
        select(OutreachMessage)
        .join(CampaignProspect, CampaignProspect.id == OutreachMessage.campaign_prospect_id)
        .where(CampaignProspect.campaign_id == campaign_id)
        .order_by(OutreachMessage.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(OutreachMessage.status == status)
    rows = await session.execute(stmt)
    return {
        "items": [
            {
                "id": m.id,
                "step": m.step,
                "status": m.status,
                "subject": m.subject,
                "locale": m.locale,
                "blocked_reason": m.blocked_reason,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows.scalars().all()
        ]
    }


@router.get("/{campaign_id}/counts")
async def counts(campaign_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    rows = await session.execute(
        select(OutreachMessage.status, func.count())
        .join(CampaignProspect, CampaignProspect.id == OutreachMessage.campaign_prospect_id)
        .where(CampaignProspect.campaign_id == campaign_id)
        .group_by(OutreachMessage.status)
    )
    return {str(k): int(v) for k, v in rows.all()}
