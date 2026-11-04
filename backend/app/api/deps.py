"""FastAPI dependencies: settings, clock, registry, DB session, auth and RBAC."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry, get_registry
from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.errors import Forbidden, RateLimited, Unauthorized
from app.core.ratelimit import FixedWindowLimiter
from app.core.security import decode_access_token
from app.db.models import User
from app.db.models.enums import UserRole
from app.db.session import get_db


def settings_dep() -> Settings:
    return get_settings()


def clock_dep() -> Clock:
    return SystemClock()


def registry_dep() -> AdapterRegistry:
    return get_registry()


SettingsDep = Annotated[Settings, Depends(settings_dep)]
ClockDep = Annotated[Clock, Depends(clock_dep)]
RegistryDep = Annotated[AdapterRegistry, Depends(registry_dep)]
DbDep = Annotated[AsyncSession, Depends(get_db)]

_limiter = FixedWindowLimiter()


def limiter() -> FixedWindowLimiter:
    return _limiter


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def get_current_user(
    request: Request,
    session: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        token = str(request.cookies.get("access_token", "") or "")
    if not token:
        raise Unauthorized("missing access token")
    payload = decode_access_token(token, settings, clock)
    user = await session.get(User, str(payload.get("sub")))
    if user is None or not user.is_active:
        raise Unauthorized("user not found or inactive")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: str) -> object:
    async def _checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise Forbidden(f"role '{user.role}' may not perform this action")
        return user

    return _checker


AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
WriterUser = Annotated[User, Depends(require_roles(UserRole.ADMIN, UserRole.SALES))]
AnyUser = Annotated[User, Depends(require_roles(UserRole.ADMIN, UserRole.SALES, UserRole.VIEWER))]


async def user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.strip().lower()))
    return result.scalars().first()


def rate_limit(key: str, limit: int, window_seconds: int) -> None:
    try:
        _limiter.check(key, limit, window_seconds)
    except RateLimited:
        raise


async def resolve_user_from_token(token: str, session: AsyncSession, settings: Settings, clock: Clock) -> User:
    payload = decode_access_token(token, settings, clock)
    user = await session.get(User, str(payload.get("sub")))
    if user is None:
        raise Unauthorized("invalid token")
    return user


async def stream_auth(
    session: DbDep, settings: SettingsDep, clock: ClockDep, token: str = ""
) -> AsyncIterator[User]:
    user = await resolve_user_from_token(token, session, settings, clock)
    yield user
