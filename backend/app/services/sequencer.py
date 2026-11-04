"""Sequencing: plan campaign prospects and compose messages when they fall due.

Locks are optimistic and short (`locked_until`) so two workers can run simultaneously without
composing the same prospect twice; the send path has its own exactly-once claim.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import Campaign, CampaignProspect, Company, OutreachMessage
from app.db.models.enums import CampaignStatus, CompanyStatus, EmailStatus, ProspectState
from app.services import audit, composer, state_machine, suppression
from app.services import contacts as contact_service
from app.services.contacts import contact_email

LOCK_MINUTES = 5


@dataclass
class PlanResult:
    created: int = 0
    skipped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class ComposeBatch:
    composed: int = 0
    queued: int = 0
    blocked: int = 0
    review: int = 0
    notes: list[str] = field(default_factory=list)


async def plan_sequences(
    session: AsyncSession,
    *,
    campaign: Campaign,
    clock: Clock,
    settings: Settings,
    limit: int = 500,
) -> PlanResult:
    del settings
    result = PlanResult()
    if campaign.status not in (CampaignStatus.ACTIVE, CampaignStatus.DRAFT):
        result.reasons["campaign_not_active"] = 1
        return result
    stmt = select(Company).where(
        Company.status.in_([CompanyStatus.QUALIFIED, CompanyStatus.RESEARCHED]),
        Company.excluded_reason.is_(None),
    ).order_by(Company.icp_score.desc()).limit(limit * 5)
    companies = list((await session.execute(stmt)).scalars().all())

    def _skip(reason: str) -> None:
        result.skipped += 1
        result.reasons[reason] = result.reasons.get(reason, 0) + 1

    for company in companies:
        if result.created >= limit:
            break
        if campaign.industry_filter and company.industry not in campaign.industry_filter:
            _skip("industry_filter")
            continue
        if campaign.country_filter and company.country not in campaign.country_filter:
            _skip("country_filter")
            continue
        if campaign.city_filter and (company.city or "") not in campaign.city_filter:
            _skip("city_filter")
            continue
        if company.icp_score < campaign.min_icp_score:
            _skip("below_icp_threshold")
            continue
        existing = await session.execute(
            select(CampaignProspect).where(
                CampaignProspect.campaign_id == campaign.id, CampaignProspect.company_id == company.id
            )
        )
        if existing.scalars().first() is not None:
            _skip("already_in_campaign")
            continue
        contact = await contact_service.pick_primary(session, company.id)
        if contact is None or contact.email_status in (EmailStatus.INVALID, EmailStatus.DISPOSABLE):
            _skip("no_usable_contact")
            continue
        # absolute suppression: a suppressed address or domain can never enter a sequence
        if await suppression.is_suppressed(
            session, email=contact_email(contact) or None, domain=company.domain or None
        ):
            _skip("suppressed")
            continue
        prospect = CampaignProspect(
            campaign_id=campaign.id,
            company_id=company.id,
            contact_id=contact.id,
            state=ProspectState.QUEUED,
            step_index=0,
            next_action_at=clock.now(),
            language=contact.preferred_language or "ar",
        )
        session.add(prospect)
        await session.flush()
        await state_machine.transition(
            session, prospect, ProspectState.QUEUED, clock=clock, reason="planned"
        )
        company.status = CompanyStatus.QUEUED
        result.created += 1
    await session.flush()
    if result.created:
        await audit.emit_event(
            session,
            kind="sequence_planned",
            entity_type="campaign",
            entity_id=campaign.id,
            payload={"created": result.created},
        )
    return result


async def claim_due_prospects(
    session: AsyncSession, *, clock: Clock, limit: int = 25, worker_id: str | None = None
) -> list[CampaignProspect]:
    worker = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    stmt = (
        select(CampaignProspect)
        .where(
            CampaignProspect.state.in_([ProspectState.QUEUED, ProspectState.COMPOSING]),
            CampaignProspect.next_action_at.is_not(None),
            CampaignProspect.next_action_at <= clock.now(),
            or_(
                CampaignProspect.locked_until.is_(None),
                CampaignProspect.locked_until < clock.now(),
            ),
        )
        .order_by(CampaignProspect.next_action_at.asc())
        .limit(limit)
    )
    prospects = list((await session.execute(stmt)).scalars().all())
    for prospect in prospects:
        prospect.locked_until = clock.now() + timedelta(minutes=LOCK_MINUTES)
        prospect.locked_by = worker
    await session.flush()
    return prospects


async def compose_due(
    session: AsyncSession,
    *,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
    limit: int = 25,
) -> ComposeBatch:
    batch = ComposeBatch()
    prospects = await claim_due_prospects(session, clock=clock, limit=limit)
    for prospect in prospects:
        campaign = await session.get(Campaign, prospect.campaign_id)
        if campaign is None or campaign.status not in (CampaignStatus.ACTIVE,):
            prospect.locked_until = None
            prospect.locked_by = None
            continue
        outcome = await composer.compose_for_prospect(
            session, prospect=prospect, registry=registry, clock=clock, settings=settings
        )
        prospect.locked_until = None
        prospect.locked_by = None
        batch.composed += 1
        if outcome.status in ("queued", "would_send"):
            batch.queued += 1
        elif outcome.status == "needs_review":
            batch.review += 1
        else:
            batch.blocked += 1
        batch.notes.extend(outcome.notes)
    await session.flush()
    return batch


async def mark_no_response(session: AsyncSession, *, campaign_id: str, clock: Clock, silence_days: int = 14) -> int:
    """Prospects whose last step was sent and have been silent past the gap become no_response."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return 0
    max_steps = 1 + campaign.max_followups
    cutoff = clock.now() - timedelta(days=silence_days)
    stmt = select(CampaignProspect).where(
        CampaignProspect.campaign_id == campaign_id,
        CampaignProspect.state.in_([ProspectState.STEP1_SENT, ProspectState.FOLLOWUP_1_SENT, ProspectState.FOLLOWUP_2_SENT]),
        CampaignProspect.step_index >= max_steps,
        CampaignProspect.replied_at.is_(None),
        CampaignProspect.last_step_sent_at.is_not(None),
        CampaignProspect.last_step_sent_at <= cutoff,
    )
    count = 0
    for prospect in (await session.execute(stmt)).scalars().all():
        if state_machine.is_legal(prospect.state, ProspectState.NO_RESPONSE):
            await state_machine.transition(
                session, prospect, ProspectState.NO_RESPONSE, clock=clock, reason="no_reply_after_last_step"
            )
            count += 1
    await session.flush()
    return count


async def campaign_stats(session: AsyncSession, campaign_id: str) -> dict[str, Any]:
    rows = await session.execute(
        select(CampaignProspect.state, func.count())
        .where(CampaignProspect.campaign_id == campaign_id)
        .group_by(CampaignProspect.state)
    )
    states = {str(state): int(total) for state, total in rows.all()}
    messages = await session.execute(
        select(OutreachMessage.status, func.count())
        .join(CampaignProspect, CampaignProspect.id == OutreachMessage.campaign_prospect_id)
        .where(CampaignProspect.campaign_id == campaign_id)
        .group_by(OutreachMessage.status)
    )
    return {
        "campaign_id": campaign_id,
        "prospect_states": states,
        "message_statuses": {str(k): int(v) for k, v in messages.all()},
        "total_prospects": sum(states.values()),
    }


async def due_counts(session: AsyncSession, clock: Clock) -> dict[str, int]:
    now = clock.now()
    prospects = await session.execute(
        select(func.count()).select_from(CampaignProspect).where(
            CampaignProspect.next_action_at.is_not(None), CampaignProspect.next_action_at <= now
        )
    )
    messages = await session.execute(
        select(func.count()).select_from(OutreachMessage).where(
            OutreachMessage.status == "queued", OutreachMessage.scheduled_for <= now
        )
    )
    return {"due_prospects": int(prospects.scalar_one()), "due_messages": int(messages.scalar_one())}
