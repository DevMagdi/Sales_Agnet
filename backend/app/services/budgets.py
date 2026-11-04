"""Daily spend metering and enforcement (LLM + external APIs)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import BudgetExceeded
from app.db.models import ApiCost, LLMCall
from app.services import settings_service


def _day_start(clock: Clock) -> datetime:
    today = clock.today()
    return datetime(today.year, today.month, today.day, tzinfo=UTC)


@dataclass
class Spend:
    llm_usd: float
    api_usd: float

    @property
    def total(self) -> float:
        return round(self.llm_usd + self.api_usd, 6)

    def as_dict(self) -> dict[str, float]:
        return {"llm_usd": round(self.llm_usd, 6), "api_usd": round(self.api_usd, 6), "total_usd": self.total}


async def spent_today(session: AsyncSession, clock: Clock) -> Spend:
    start = _day_start(clock)
    llm = await session.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.created_at >= start))
    api = await session.execute(
        select(func.coalesce(func.sum(ApiCost.cost_usd), 0.0)).where(ApiCost.occurred_at >= start)
    )
    return Spend(float(llm.scalar_one()), float(api.scalar_one()))


async def check(session: AsyncSession, clock: Clock, settings: Settings) -> Spend:
    limits = await settings_service.daily_budgets(session, settings)
    spend = await spent_today(session, clock)
    if spend.llm_usd > limits["llm"]:
        raise BudgetExceeded(
            "daily LLM budget exceeded", details={"spent": spend.llm_usd, "limit": limits["llm"]}
        )
    if spend.api_usd > limits["api"]:
        raise BudgetExceeded(
            "daily API budget exceeded", details={"spent": spend.api_usd, "limit": limits["api"]}
        )
    return spend


async def remaining(session: AsyncSession, clock: Clock, settings: Settings) -> dict[str, Any]:
    limits = await settings_service.daily_budgets(session, settings)
    spend = await spent_today(session, clock)
    return {
        "llm_remaining_usd": round(max(0.0, limits["llm"] - spend.llm_usd), 6),
        "api_remaining_usd": round(max(0.0, limits["api"] - spend.api_usd), 6),
        "limits": limits,
    }


async def record_api_cost(
    session: AsyncSession,
    *,
    provider: str,
    cost_usd: float,
    clock: Clock,
    unit: str = "call",
    quantity: float = 1.0,
    meta: dict[str, Any] | None = None,
) -> ApiCost:
    row = ApiCost(
        provider=provider,
        unit=unit,
        quantity=quantity,
        cost_usd=cost_usd,
        occurred_at=clock.now(),
        meta=meta or {},
    )
    session.add(row)
    await session.flush()
    return row
