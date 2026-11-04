"""Runtime settings stored in the DB: operating mode, kill switch, budgets, feature switches.

Env is the source of truth for secrets and identity; the DB holds operator-controlled switches
so they can be flipped from the dashboard without a redeploy. Every change is audited.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import Setting
from app.services import audit

KILL_SWITCH_KEY = "kill_switch"
GLOBAL_STOP_KEY = "global_stop"
MODE_KEY = "outreach_mode"
BUDGET_KEY = "budgets"

VALID_MODES = ("dry_run", "sandbox", "live")


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        return default
    return row.value.get("value", default)


async def set_setting(
    session: AsyncSession,
    key: str,
    value: Any,
    *,
    actor: str = "system",
    actor_id: str | None = None,
    ip: str | None = None,
    reason: str = "",
) -> Setting:
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    before = row.value if row is not None else None
    if row is None:
        row = Setting(key=key, value={"value": value}, updated_by=actor_id)
        session.add(row)
    else:
        row.value = {"value": value}
        row.updated_by = actor_id
    await session.flush()
    await audit.record(
        session,
        action="setting_changed",
        entity_type="setting",
        entity_id=row.id,
        actor_id=actor_id,
        actor_label=actor,
        detail={"key": key, "reason": reason},
        before=before,
        after={"value": value},
        ip=ip,
    )
    await audit.emit_event(
        session, kind="setting_changed", entity_type="setting", entity_id=row.id, payload={"key": key}
    )
    return row


async def kill_switch_active(session: AsyncSession) -> bool:
    return bool(await get_setting(session, KILL_SWITCH_KEY, False))


async def set_kill_switch(
    session: AsyncSession,
    active: bool,
    *,
    actor: str,
    actor_id: str | None = None,
    reason: str = "",
    ip: str | None = None,
) -> None:
    await set_setting(
        session,
        KILL_SWITCH_KEY,
        active,
        actor=actor,
        actor_id=actor_id,
        ip=ip,
        reason=reason or ("kill switch engaged" if active else "kill switch released"),
    )


async def global_stop(session: AsyncSession) -> dict[str, Any] | None:
    value = await get_setting(session, GLOBAL_STOP_KEY, None)
    if isinstance(value, dict) and value.get("active"):
        return value
    return None


async def set_global_stop(
    session: AsyncSession,
    active: bool,
    *,
    reason: str,
    actor: str = "circuit_breaker",
    actor_id: str | None = None,
) -> None:
    """Circuit breakers stop everything and never auto-resume: a human clears this."""
    await set_setting(
        session,
        GLOBAL_STOP_KEY,
        {"active": active, "reason": reason, "stopped_by": actor},
        actor=actor,
        actor_id=actor_id,
        reason=reason,
    )


async def effective_mode(session: AsyncSession, settings: Settings) -> str:
    override = await get_setting(session, MODE_KEY, None)
    mode = str(override) if override in VALID_MODES else settings.outreach_mode
    return mode


async def set_mode(session: AsyncSession, mode: str, *, actor: str, actor_id: str | None = None) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode}")
    await set_setting(session, MODE_KEY, mode, actor=actor, actor_id=actor_id)


async def daily_budgets(session: AsyncSession, settings: Settings) -> dict[str, float]:
    stored = await get_setting(session, BUDGET_KEY, None)
    if isinstance(stored, dict):
        return {
            "llm": float(stored.get("llm", settings.llm_daily_budget_usd)),
            "api": float(stored.get("api", settings.api_daily_budget_usd)),
        }
    return {"llm": settings.llm_daily_budget_usd, "api": settings.api_daily_budget_usd}


async def set_daily_budgets(
    session: AsyncSession, llm: float, api: float, *, actor: str, actor_id: str | None = None
) -> None:
    await set_setting(session, BUDGET_KEY, {"llm": llm, "api": api}, actor=actor, actor_id=actor_id)


async def all_settings(session: AsyncSession) -> dict[str, Any]:
    result = await session.execute(select(Setting).order_by(Setting.key))
    return {row.key: row.value.get("value") for row in result.scalars().all()}


async def sending_allowed(session: AsyncSession, clock: Clock) -> tuple[bool, str]:
    """Kill switch + global stop + mode, in that order. Used by workers before any batch."""
    if await kill_switch_active(session):
        return False, "kill_switch"
    stop = await global_stop(session)
    if stop:
        return False, f"global_stop: {stop.get('reason', '')}"
    return True, "ok"
