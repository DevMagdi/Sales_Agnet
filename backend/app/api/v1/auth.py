"""Authentication: Argon2id passwords, TOTP, rotating refresh tokens, lockout."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import (
    ClockDep,
    CurrentUser,
    DbDep,
    SettingsDep,
    client_ip,
    limiter,
)
from app.api.schemas import LoginRequest, MeResponse, RefreshRequest, TokenResponse
from app.core.clock import Clock
from app.core.errors import Unauthorized
from app.core.security import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
    verify_totp,
)
from app.db.models import RefreshToken, User
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


async def _issue_tokens(session: DbDep, user: User, settings: SettingsDep, clock: Clock) -> TokenResponse:
    access = create_access_token(user.id, user.role, user.org_id, settings, clock)
    raw_refresh, refresh_hash = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=clock.now() + timedelta(days=settings.refresh_token_days),
        )
    )
    await session.flush()
    return TokenResponse(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=settings.access_token_minutes * 60,
        user={
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "locale": user.locale,
        },
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> TokenResponse:
    ip = client_ip(request)
    limiter().check(f"login:{ip}", settings.login_max_attempts * 4, 300)
    result = await session.execute(select(User).where(User.email == payload.email.strip().lower()))
    user = result.scalars().first()
    if user is None or not user.is_active:
        await audit.record(
            session,
            action="login_failed",
            entity_type="user",
            actor_label=payload.email,
            detail={"reason": "unknown_or_inactive"},
            ip=ip,
        )
        raise Unauthorized("invalid credentials")
    if user.locked_until is not None:
        locked_until = user.locked_until
        if locked_until.tzinfo is None:
            from datetime import UTC

            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until > clock.now():
            raise Unauthorized("account temporarily locked", details={"reason": "locked"})
    if not verify_password(payload.password, user.password_hash):
        user.failed_attempts += 1
        if user.failed_attempts >= settings.login_max_attempts:
            user.locked_until = clock.now() + timedelta(seconds=settings.login_lockout_seconds)
            user.failed_attempts = 0
        await audit.record(
            session,
            action="login_failed",
            entity_type="user",
            entity_id=user.id,
            actor_label=user.email,
            detail={"reason": "bad_password"},
            ip=ip,
        )
        await session.flush()
        raise Unauthorized("invalid credentials")
    if user.mfa_secret and not verify_totp(user.mfa_secret, payload.totp_code):
        raise Unauthorized("invalid or missing TOTP code", details={"mfa_required": True})
    user.failed_attempts = 0
    user.locked_until = None
    tokens = await _issue_tokens(session, user, settings, clock)
    await audit.record(
        session,
        action="login_succeeded",
        entity_type="user",
        entity_id=user.id,
        actor_label=user.email,
        ip=ip,
    )
    return tokens


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest, session: DbDep, settings: SettingsDep, clock: ClockDep, request: Request
) -> TokenResponse:
    digest = hash_refresh_token(payload.refresh_token)
    result = await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    stored = result.scalars().first()
    if stored is None or stored.revoked_at is not None:
        raise Unauthorized("refresh token is not valid")
    expires = stored.expires_at
    if expires.tzinfo is None:
        from datetime import UTC

        expires = expires.replace(tzinfo=UTC)
    if expires < clock.now():
        raise Unauthorized("refresh token expired")
    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("user inactive")
    stored.revoked_at = clock.now()
    tokens = await _issue_tokens(session, user, settings, clock)
    stored.replaced_by = None
    await audit.record(
        session,
        action="token_refreshed",
        entity_type="user",
        entity_id=user.id,
        actor_label=user.email,
        ip=client_ip(request),
    )
    return tokens


@router.post("/logout")
async def logout(payload: RefreshRequest, session: DbDep, user: CurrentUser) -> dict[str, Any]:
    digest = hash_refresh_token(payload.refresh_token)
    result = await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    stored = result.scalars().first()
    if stored is not None:
        stored.revoked_at = stored.created_at
    await audit.record(session, action="logout", entity_type="user", entity_id=user.id, actor_label=user.email)
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser) -> MeResponse:
    return MeResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        locale=user.locale,
        mfa_enabled=bool(user.mfa_secret),
    )
