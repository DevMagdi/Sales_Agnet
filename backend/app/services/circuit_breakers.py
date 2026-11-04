"""Circuit breakers on a rolling window. Trip once, stop, and never auto-resume.

Thresholds (spec §7.12):
  * campaign paused when its hard-bounce rate > 3% (min 30 sends in the window)
  * ALL sending stopped when global hard-bounce > 5%, complaint rate > 0.1%, provider auth errors,
    or the daily LLM/API budget is exceeded
Tripping sets status stopped_by_circuit_breaker, alerts admins and requires a human to resume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import BudgetExceeded
from app.core.logging import get_logger
from app.db.models import Campaign, CampaignProspect, MessageEvent, OutreachMessage
from app.db.models.enums import CampaignStatus, MessageEventType
from app.services import audit, budgets, settings_service
from app.services import handoff as handoff  # re-exported for callers

logger = get_logger("breakers")

GLOBAL_BOUNCE_LIMIT = 0.05
CAMPAIGN_BOUNCE_LIMIT = 0.03
COMPLAINT_LIMIT = 0.001
MIN_SENDS = 30
WINDOW_HOURS = 24


@dataclass
class BreakerVerdict:
    tripped: bool = False
    scope: str = "global"
    reason: str = ""
    rates: dict[str, Any] = field(default_factory=dict)
    campaigns_paused: list[str] = field(default_factory=list)
    already_stopped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "tripped": self.tripped,
            "scope": self.scope,
            "reason": self.reason,
            "rates": self.rates,
            "campaigns_paused": self.campaigns_paused,
            "already_stopped": self.already_stopped,
        }


@dataclass
class Rates:
    sent: int = 0
    hard_bounces: int = 0
    complaints: int = 0

    @property
    def bounce_rate(self) -> float:
        return self.hard_bounces / self.sent if self.sent else 0.0

    @property
    def complaint_rate(self) -> float:
        return self.complaints / self.sent if self.sent else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "hard_bounces": self.hard_bounces,
            "complaints": self.complaints,
            "bounce_rate": round(self.bounce_rate, 4),
            "complaint_rate": round(self.complaint_rate, 4),
        }


async def global_rates(session: AsyncSession, clock: Clock, hours: int = WINDOW_HOURS) -> Rates:
    since = clock.now() - timedelta(hours=hours)
    sent = await session.execute(
        select(func.count())
        .select_from(OutreachMessage)
        .where(OutreachMessage.sent_at.is_not(None), OutreachMessage.sent_at >= since)
    )
    events = await session.execute(
        select(MessageEvent.type, func.count())
        .where(MessageEvent.occurred_at >= since)
        .group_by(MessageEvent.type)
    )
    counts = {str(kind): int(value) for kind, value in events.all()}
    return Rates(
        sent=int(sent.scalar_one()),
        hard_bounces=counts.get(MessageEventType.BOUNCE_HARD, 0),
        complaints=counts.get(MessageEventType.COMPLAINT, 0),
    )


async def campaign_rates(
    session: AsyncSession, campaign_id: str, clock: Clock, hours: int = WINDOW_HOURS
) -> Rates:
    since = clock.now() - timedelta(hours=hours)
    sent = await session.execute(
        select(func.count())
        .select_from(OutreachMessage)
        .join(CampaignProspect, CampaignProspect.id == OutreachMessage.campaign_prospect_id)
        .where(
            CampaignProspect.campaign_id == campaign_id,
            OutreachMessage.sent_at.is_not(None),
            OutreachMessage.sent_at >= since,
        )
    )
    events = await session.execute(
        select(MessageEvent.type, func.count())
        .join(OutreachMessage, OutreachMessage.id == MessageEvent.message_id)
        .join(CampaignProspect, CampaignProspect.id == OutreachMessage.campaign_prospect_id)
        .where(
            CampaignProspect.campaign_id == campaign_id,
            MessageEvent.occurred_at >= since,
            MessageEvent.type.in_([MessageEventType.BOUNCE_HARD, MessageEventType.COMPLAINT]),
        )
        .group_by(MessageEvent.type)
    )
    counts = {str(kind): int(value) for kind, value in events.all()}
    return Rates(
        sent=int(sent.scalar_one()),
        hard_bounces=counts.get(MessageEventType.BOUNCE_HARD, 0),
        complaints=counts.get(MessageEventType.COMPLAINT, 0),
    )


async def trip_global(
    session: AsyncSession,
    *,
    reason: str,
    registry: AdapterRegistry | None,
    clock: Clock,
    settings: Settings,
) -> None:
    await settings_service.set_global_stop(session, True, reason=reason)
    rows = await session.execute(
        select(Campaign).where(Campaign.status == CampaignStatus.ACTIVE)
    )
    for campaign in rows.scalars().all():
        campaign.status = CampaignStatus.STOPPED_BY_CIRCUIT_BREAKER
        campaign.stopped_reason = reason[:280]
    queued = await session.execute(
        select(OutreachMessage).where(OutreachMessage.status == "queued")
    )
    for message in queued.scalars().all():
        message.status = "blocked"
        message.blocked_reason = f"circuit_breaker:{reason}"[:190]
    await session.flush()
    await audit.record(
        session,
        action="circuit_breaker_tripped",
        entity_type="system",
        detail={"scope": "global", "reason": reason},
    )
    await audit.emit_event(
        session, kind="circuit_breaker", entity_type="system", payload={"scope": "global", "reason": reason}
    )
    logger.error("circuit_breaker_global", reason=reason)
    if registry is not None:
        await handoff.notify_staff(
            registry,
            title="SENDING STOPPED — circuit breaker tripped",
            body=(
                f"Reason: {reason}\nAll sending is stopped and will NOT resume automatically.\n"
                f"Review /deliverability, fix the cause, then resume with POST /api/v1/system/resume.\n"
                f"Mode: {settings.outreach_mode}"
            ),
            payload={"scope": "global", "reason": reason},
        )


async def evaluate(
    session: AsyncSession,
    *,
    registry: AdapterRegistry | None,
    clock: Clock,
    settings: Settings,
) -> BreakerVerdict:
    verdict = BreakerVerdict()
    stop = await settings_service.global_stop(session)
    if stop:
        verdict.already_stopped = True
        verdict.tripped = True
        verdict.reason = str(stop.get("reason", "already_stopped"))
        verdict.rates = {"global": (await global_rates(session, clock)).as_dict()}
        return verdict

    rates = await global_rates(session, clock)
    verdict.rates = {"global": rates.as_dict()}
    global_reason = ""
    if rates.sent >= MIN_SENDS and rates.bounce_rate > GLOBAL_BOUNCE_LIMIT:
        global_reason = f"hard_bounce_rate {rates.bounce_rate:.2%} > {GLOBAL_BOUNCE_LIMIT:.0%}"
    elif rates.sent >= MIN_SENDS and rates.complaint_rate > COMPLAINT_LIMIT:
        global_reason = f"complaint_rate {rates.complaint_rate:.2%} > {COMPLAINT_LIMIT:.1%}"
    try:
        await budgets.check(session, clock, settings)
    except BudgetExceeded as exc:
        global_reason = global_reason or f"budget_exceeded:{exc.message}"

    if global_reason:
        verdict.tripped = True
        verdict.reason = global_reason
        await trip_global(session, reason=global_reason, registry=registry, clock=clock, settings=settings)
        return verdict

    campaigns = await session.execute(select(Campaign).where(Campaign.status == CampaignStatus.ACTIVE))
    for campaign in campaigns.scalars().all():
        campaign_rate = await campaign_rates(session, campaign.id, clock)
        verdict.rates[campaign.id] = campaign_rate.as_dict()
        if campaign_rate.sent >= MIN_SENDS and campaign_rate.bounce_rate > CAMPAIGN_BOUNCE_LIMIT:
            campaign.status = CampaignStatus.STOPPED_BY_CIRCUIT_BREAKER
            campaign.stopped_reason = f"campaign hard_bounce_rate {campaign_rate.bounce_rate:.2%}"
            verdict.campaigns_paused.append(campaign.id)
            verdict.tripped = True
            verdict.scope = "campaign"
            verdict.reason = campaign.stopped_reason
            await audit.record(
                session,
                action="circuit_breaker_tripped",
                entity_type="campaign",
                entity_id=campaign.id,
                detail={"reason": campaign.stopped_reason, "rates": campaign_rate.as_dict()},
            )
            if registry is not None:
                await handoff.notify_staff(
                    registry,
                    title=f"Campaign paused by circuit breaker: {campaign.name}",
                    body=f"{campaign.stopped_reason}\nNo auto-resume. Review before reactivating.",
                    payload={"campaign_id": campaign.id},
                )
    await session.flush()
    return verdict


async def resume_all(
    session: AsyncSession, *, actor: str, registry: AdapterRegistry | None, clock: Clock
) -> dict[str, Any]:
    """Human-only resume: clears the global stop and reactivates campaigns a human selects."""
    await settings_service.set_global_stop(session, False, reason=f"resumed_by:{actor}", actor=actor)
    await audit.record(
        session, action="sending_resumed", entity_type="system", actor_label=actor, detail={}
    )
    if registry is not None:
        await handoff.notify_staff(
            registry, title="Sending resumed", body=f"Resumed by {actor}.", payload={"actor": actor}
        )
    return {"resumed": True, "by": actor, "at": clock.now().isoformat()}
