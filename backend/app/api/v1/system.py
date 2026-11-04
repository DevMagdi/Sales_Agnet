"""System control: status, kill switch, resume, mode, budgets, doctor, adapters, audit log."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.deps import AdminUser, AnyUser, ClockDep, DbDep, RegistryDep, SettingsDep, WriterUser, client_ip
from app.api.schemas import BudgetRequest, KillSwitchRequest, ModeRequest
from app.core.config import live_mode_blockers, startup_problems
from app.core.errors import ValidationFailed
from app.db.models import AuditLog, Campaign
from app.services import audit, budgets, circuit_breakers, deliverability, sequencer, settings_service

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
async def status(
    session: DbDep, user: AnyUser, settings: SettingsDep, clock: ClockDep, registry: RegistryDep
) -> dict[str, Any]:
    del user
    mode = await settings_service.effective_mode(session, settings)
    return {
        "mode": mode,
        "env_mode": settings.outreach_mode,
        "app_env": settings.app_env,
        "kill_switch": await settings_service.kill_switch_active(session),
        "global_stop": await settings_service.global_stop(session),
        "spend_today": (await budgets.spent_today(session, clock)).as_dict(),
        "limits": await settings_service.daily_budgets(session, settings),
        "due": await sequencer.due_counts(session, clock),
        "adapters": registry.status_report(),
        "live_blockers": live_mode_blockers(settings) if mode == "live" else [],
        "startup_problems": startup_problems(settings),
        "now": clock.now().isoformat(),
    }


@router.get("/adapters")
async def adapters(user: AnyUser, registry: RegistryDep) -> dict[str, Any]:
    del user
    return registry.status_report()


@router.post("/kill")
async def kill(
    payload: KillSwitchRequest,
    request: Request,
    session: DbDep,
    user: WriterUser,
    registry: RegistryDep,
    clock: ClockDep,
) -> dict[str, Any]:
    await settings_service.set_kill_switch(
        session,
        payload.active,
        actor=user.email,
        actor_id=user.id,
        reason=payload.reason,
        ip=client_ip(request),
    )
    if payload.active:
        queued = await session.execute(select(Campaign).where(Campaign.status == "active"))
        for campaign in queued.scalars().all():
            campaign.status = "paused"
            campaign.stopped_reason = f"kill_switch by {user.email}"
        await session.flush()
        await circuit_breakers.handoff.notify_staff(
            registry,
            title="KILL SWITCH ENGAGED",
            body=f"All sending stopped by {user.email}. Reason: {payload.reason or 'not given'}",
            payload={"actor": user.email},
        )
    await audit.record(
        session, action="kill_switch_changed", entity_type="system", actor_id=user.id,
        actor_label=user.email, detail={"active": payload.active, "reason": payload.reason},
    )
    return {"kill_switch": payload.active, "at": clock.now().isoformat()}


@router.post("/resume")
async def resume(
    session: DbDep, user: AdminUser, registry: RegistryDep, clock: ClockDep
) -> dict[str, Any]:
    result = await circuit_breakers.resume_all(session, actor=user.email, registry=registry, clock=clock)
    await settings_service.set_kill_switch(session, False, actor=user.email, actor_id=user.id, reason="resume")
    return result


@router.post("/mode")
async def set_mode(payload: ModeRequest, session: DbDep, user: AdminUser, settings: SettingsDep) -> dict[str, Any]:
    if payload.mode == "live":
        if not payload.confirm_live:
            raise ValidationFailed("live mode requires confirm_live=true", details={"hint": "complete GO_LIVE_CHECKLIST.md first"})
        blockers = live_mode_blockers(settings)
        if blockers:
            raise ValidationFailed("live mode is not allowed yet", details={"blockers": blockers})
    await settings_service.set_mode(session, payload.mode, actor=user.email, actor_id=user.id)
    await audit.record(
        session, action="mode_changed", entity_type="system", actor_id=user.id, actor_label=user.email,
        detail={"mode": payload.mode},
    )
    return {"mode": payload.mode}


@router.post("/budgets")
async def set_budgets(payload: BudgetRequest, session: DbDep, user: AdminUser) -> dict[str, Any]:
    if payload.llm_daily_usd < 0 or payload.api_daily_usd < 0:
        raise ValidationFailed("budgets must be >= 0")
    await settings_service.set_daily_budgets(
        session, payload.llm_daily_usd, payload.api_daily_usd, actor=user.email, actor_id=user.id
    )
    return {"llm": payload.llm_daily_usd, "api": payload.api_daily_usd}


@router.get("/doctor")
async def doctor(
    session: DbDep, user: AnyUser, settings: SettingsDep, clock: ClockDep, registry: RegistryDep
) -> dict[str, Any]:
    del user
    report = await deliverability.run_doctor(
        session, settings=settings, registry=registry, clock=clock
    )
    return report.as_dict()


@router.post("/breakers/evaluate")
async def evaluate_breakers(
    session: DbDep, user: WriterUser, registry: RegistryDep, clock: ClockDep, settings: SettingsDep
) -> dict[str, Any]:
    verdict = await circuit_breakers.evaluate(
        session, registry=registry, clock=clock, settings=settings
    )
    await audit.record(
        session, action="breakers_evaluated", entity_type="system", actor_id=user.id,
        actor_label=user.email, detail=verdict.as_dict(),
    )
    return verdict.as_dict()


@router.get("/audit")
async def audit_log(
    session: DbDep, user: AdminUser, limit: int = Query(100, le=500), action: str | None = None
) -> dict[str, Any]:
    del user
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = await session.execute(stmt)
    return {
        "items": [
            {
                "id": entry.id,
                "action": entry.action,
                "entity_type": entry.entity_type,
                "entity_id": entry.entity_id,
                "actor_label": entry.actor_label,
                "detail": entry.detail,
                "ip": entry.ip,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
            }
            for entry in rows.scalars().all()
        ]
    }


@router.get("/settings")
async def get_settings_dump(session: DbDep, user: AdminUser) -> dict[str, Any]:
    del user
    return await settings_service.all_settings(session)


@router.get("/health")
async def health(session: DbDep, clock: ClockDep, settings: SettingsDep) -> dict[str, Any]:
    await session.execute(select(1))
    return {
        "status": "ok",
        "mode": await settings_service.effective_mode(session, settings),
        "kill_switch": await settings_service.kill_switch_active(session),
        "time": clock.now().isoformat(),
    }


