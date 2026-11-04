"""Reporting endpoints and CSV exports."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from app.api.deps import AnyUser, ClockDep, DbDep, SettingsDep
from app.services import deliverability, reports

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/funnel")
async def funnel(session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    return {"funnel": await reports.funnel(session)}


@router.get("/kpis")
async def kpis(session: DbDep, user: AnyUser, clock: ClockDep, settings: SettingsDep) -> dict[str, Any]:
    del user
    return await reports.kpis(session, clock, settings)


@router.get("/costs")
async def costs(session: DbDep, user: AnyUser, clock: ClockDep, days: int = 7) -> dict[str, Any]:
    del user
    return await reports.costs(session, clock, days)


@router.get("/industries")
async def industries(session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    return {"items": await reports.industry_performance(session)}


@router.get("/daily")
async def daily(session: DbDep, user: AnyUser, clock: ClockDep, settings: SettingsDep) -> dict[str, Any]:
    del user
    return await reports.daily_report(session, clock, settings)


@router.get("/deliverability")
async def deliverability_report(session: DbDep, user: AnyUser, clock: ClockDep) -> dict[str, Any]:
    del user
    return await deliverability.rates_last_24h(session, clock)


@router.get("/funnel.csv")
async def funnel_csv(session: DbDep, user: AnyUser) -> Response:
    del user
    rows = await reports.funnel(session)
    return Response(
        content=reports.to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="funnel.csv"'},
    )


@router.get("/states")
async def states(session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    return await reports.state_breakdown(session)


@router.get("/feed")
async def feed(session: DbDep, user: AnyUser, limit: int = 30) -> dict[str, Any]:
    del user
    return {"items": await reports.live_feed(session, limit)}
