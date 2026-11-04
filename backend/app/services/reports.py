"""Funnel, cost and deliverability reporting for the dashboard and CSV exports."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import (
    ApiCost,
    CampaignProspect,
    Company,
    DemoRequest,
    LLMCall,
    MessageEvent,
    OutreachMessage,
    Reply,
    SuppressionEntry,
)
from app.db.models.enums import DemoStatus, MessageEventType, MessageStatus, ProspectState
from app.services import budgets

FUNNEL_STAGES = [
    "discovered",
    "researched",
    "qualified",
    "contacted",
    "delivered",
    "replied",
    "positive_reply",
    "demo_booked",
]


async def funnel(session: AsyncSession) -> list[dict[str, Any]]:
    companies = await session.execute(select(Company.status, func.count()).group_by(Company.status))
    by_status = {str(k): int(v) for k, v in companies.all()}
    messages = await session.execute(
        select(OutreachMessage.status, func.count()).group_by(OutreachMessage.status)
    )
    by_message = {str(k): int(v) for k, v in messages.all()}
    replies = await session.execute(select(Reply.classification, func.count()).group_by(Reply.classification))
    by_reply = {str(k): int(v) for k, v in replies.all()}
    demos = await session.execute(select(DemoRequest.status, func.count()).group_by(DemoRequest.status))
    by_demo = {str(k): int(v) for k, v in demos.all()}
    positive = sum(
        by_reply.get(k, 0)
        for k in ("interested", "wants_info", "meeting_proposal", "referral", "question_technical", "question_pricing")
    )
    contactors = by_message.get(MessageStatus.SENT, 0) + by_message.get(MessageStatus.DELIVERED, 0) + by_message.get(
        MessageStatus.REPLIED, 0
    )
    return [
        {"stage": "discovered", "count": sum(by_status.values())},
        {"stage": "researched", "count": by_status.get("researched", 0) + by_status.get("qualified", 0) + by_status.get("queued", 0)},
        {"stage": "qualified", "count": by_status.get("qualified", 0) + by_status.get("queued", 0)},
        {"stage": "contacted", "count": contactors},
        {"stage": "delivered", "count": by_message.get(MessageStatus.DELIVERED, 0)},
        {"stage": "replied", "count": sum(by_reply.values())},
        {"stage": "positive_reply", "count": positive},
        {"stage": "demo_booked", "count": by_demo.get(DemoStatus.BOOKED, 0) + by_demo.get(DemoStatus.HELD, 0)},
    ]


async def kpis(session: AsyncSession, clock: Clock, settings: Settings) -> dict[str, Any]:
    messages = await session.execute(
        select(OutreachMessage.status, func.count()).group_by(OutreachMessage.status)
    )
    by_message = {str(k): int(v) for k, v in messages.all()}
    events = await session.execute(
        select(MessageEvent.type, func.count()).group_by(MessageEvent.type)
    )
    by_event = {str(k): int(v) for k, v in events.all()}
    sent = (
        by_message.get(MessageStatus.SENT, 0)
        + by_message.get(MessageStatus.DELIVERED, 0)
        + by_message.get(MessageStatus.REPLIED, 0)
        + by_message.get(MessageStatus.BOUNCED_HARD, 0)
        + by_message.get(MessageStatus.BOUNCED_SOFT, 0)
    )
    replies = await session.execute(select(func.count()).select_from(Reply))
    demos = await session.execute(
        select(func.count()).select_from(DemoRequest).where(DemoRequest.status != DemoStatus.LOST)
    )
    unsubs = await session.execute(
        select(func.count()).select_from(SuppressionEntry).where(SuppressionEntry.reason == "unsubscribe")
    )
    spend = await budgets.spent_today(session, clock)
    demo_count = int(demos.scalar_one())
    return {
        "sent": sent,
        "replies": int(replies.scalar_one()),
        "replies_positive": await _count_classes(
            session, ["interested", "wants_info", "meeting_proposal", "referral"]
        ),
        "bounce_rate": round(by_message.get(MessageStatus.BOUNCED_HARD, 0) / sent, 4) if sent else 0.0,
        "complaint_rate": round(by_event.get(MessageEventType.COMPLAINT, 0) / sent, 4) if sent else 0.0,
        "unsubscribe_rate": round(int(unsubs.scalar_one()) / sent, 4) if sent else 0.0,
        "demos": demo_count,
        "cost_today_usd": spend.total,
        "cost_per_demo_usd": round(spend.total / demo_count, 2) if demo_count else None,
        "mode": settings.outreach_mode,
    }


async def _count_classes(session: AsyncSession, classes: list[str]) -> int:
    result = await session.execute(select(func.count()).select_from(Reply).where(Reply.classification.in_(classes)))
    return int(result.scalar_one())


async def costs(session: AsyncSession, clock: Clock, days: int = 7) -> dict[str, Any]:
    since = clock.now() - timedelta(days=days)
    llm = await session.execute(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0), func.coalesce(func.sum(LLMCall.input_tokens), 0),
               func.coalesce(func.sum(LLMCall.output_tokens), 0), func.count())
        .where(LLMCall.created_at >= since)
    )
    llm_row = llm.one()
    api = await session.execute(
        select(ApiCost.provider, func.coalesce(func.sum(ApiCost.cost_usd), 0.0))
        .where(ApiCost.occurred_at >= since)
        .group_by(ApiCost.provider)
    )
    by_task = await session.execute(
        select(LLMCall.task, func.coalesce(func.sum(LLMCall.cost_usd), 0.0), func.count())
        .where(LLMCall.created_at >= since)
        .group_by(LLMCall.task)
    )
    return {
        "window_days": days,
        "llm": {
            "cost_usd": round(float(llm_row[0]), 6),
            "input_tokens": int(llm_row[1]),
            "output_tokens": int(llm_row[2]),
            "calls": int(llm_row[3]),
        },
        "api": {str(provider): round(float(total), 6) for provider, total in api.all()},
        "llm_by_task": [
            {"task": str(task), "cost_usd": round(float(cost), 6), "calls": int(count)}
            for task, cost, count in by_task.all()
        ],
        "remaining": await budgets.remaining(session, clock, _settings_stub()),
    }


def _settings_stub() -> Any:
    from app.core.config import get_settings

    return get_settings()


async def industry_performance(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Company.industry, func.count(), func.coalesce(func.avg(Company.icp_score), 0))
        .group_by(Company.industry)
    )
    return [
        {"industry": str(industry or "unknown"), "companies": int(count), "avg_icp": round(float(avg), 1)}
        for industry, count, avg in rows.all()
    ]


async def daily_report(session: AsyncSession, clock: Clock, settings: Settings) -> dict[str, Any]:
    funnel_rows = await funnel(session)
    kpi_rows = await kpis(session, clock, settings)
    return {
        "generated_at": clock.now().isoformat(),
        "mode": settings.outreach_mode,
        "funnel": funnel_rows,
        "kpis": kpi_rows,
        "industry": await industry_performance(session),
        "costs": await costs(session, clock, days=1),
    }


def to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()})
    return buffer.getvalue()


async def live_feed(session: AsyncSession, limit: int = 30) -> list[dict[str, Any]]:
    from app.db.models import OutboxEvent

    rows = await session.execute(select(OutboxEvent).order_by(OutboxEvent.seq.desc()).limit(limit))
    return [
        {
            "seq": event.seq,
            "kind": event.kind,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "payload": event.payload,
            "at": event.created_at.isoformat() if event.created_at else "",
        }
        for event in rows.scalars().all()
    ]


async def state_breakdown(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(CampaignProspect.state, func.count()).group_by(CampaignProspect.state)
    )
    return {str(state): int(count) for state, count in rows.all()}


def prospect_state_label(state: str) -> str:
    return state.replace("_", " ").title()


def stages_list() -> list[str]:
    return [str(s) for s in ProspectState]
