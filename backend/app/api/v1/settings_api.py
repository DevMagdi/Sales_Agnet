"""Mailbox and user administration (secrets are write-only and returned masked)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import AdminUser, AnyUser, DbDep, RegistryDep
from app.api.schemas import MailboxCreate, MailboxStatusUpdate, UserCreate
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.security import generate_totp_secret, hash_password, totp_provisioning_uri
from app.db.models import Mailbox, MailboxPool, User
from app.db.models.enums import UserRole
from app.services import audit
from app.services import mailboxes as mailbox_service

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/mailboxes")
async def list_mailboxes(session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    rows = await session.execute(select(Mailbox).order_by(Mailbox.created_at.asc()))
    return {"items": [mailbox_service.public_view(m) for m in rows.scalars().all()]}


@router.post("/mailboxes")
async def create_mailbox(payload: MailboxCreate, session: DbDep, user: AdminUser) -> dict[str, Any]:
    duplicate = await session.execute(select(Mailbox).where(Mailbox.from_email == str(payload.from_email)))
    if duplicate.scalars().first() is not None:
        raise Conflict("a mailbox with that from-address already exists")
    pool = None
    if payload.pool_id:
        pool = await session.get(MailboxPool, payload.pool_id)
    else:
        pool = (await session.execute(select(MailboxPool).where(MailboxPool.name == "default"))).scalars().first()
        if pool is None:
            pool = MailboxPool(name="default", description="Default sending pool")
            session.add(pool)
            await session.flush()
    mailbox = await mailbox_service.create(
        session,
        from_email=str(payload.from_email),
        from_name=payload.from_name,
        pool_id=pool.id if pool else None,
        daily_cap=payload.daily_cap,
        smtp_host=payload.smtp_host,
        smtp_port=payload.smtp_port,
        smtp_username=payload.smtp_username,
        smtp_password=payload.smtp_password,
        imap_host=payload.imap_host,
        imap_port=payload.imap_port,
        imap_username=payload.imap_username,
        imap_password=payload.imap_password,
        signature_text=payload.signature_text,
        physical_address=payload.physical_address,
        phone=payload.phone,
        created_by=user.id,
    )
    return mailbox_service.public_view(mailbox)


@router.post("/mailboxes/{mailbox_id}/test")
async def test_mailbox(
    mailbox_id: str, session: DbDep, user: AnyUser, registry: RegistryDep
) -> dict[str, Any]:
    mailbox = await session.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise NotFound("mailbox not found")
    ok, detail = registry.email.healthcheck()
    return {
        "mailbox": mailbox.from_email,
        "adapter": registry.email.name,
        "adapter_ok": ok,
        "detail": detail,
        "config_complete": bool(mailbox.smtp_host and mailbox.smtp_username and mailbox.smtp_password_enc),
    }


@router.post("/mailboxes/{mailbox_id}/status")
async def set_mailbox_status(
    mailbox_id: str, payload: MailboxStatusUpdate, session: DbDep, user: AdminUser
) -> dict[str, Any]:
    status = payload.status
    if status not in ("active", "paused", "error"):
        raise ValidationFailed("invalid status")
    mailbox = await session.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise NotFound("mailbox not found")
    mailbox.status = status
    await session.flush()
    await audit.record(
        session, action="mailbox_status_changed", entity_type="mailbox", entity_id=mailbox.id,
        actor_id=user.id, actor_label=user.email, detail={"status": status},
    )
    return mailbox_service.public_view(mailbox)


@router.get("/users")
async def list_users(session: DbDep, user: AdminUser) -> dict[str, Any]:
    del user
    rows = await session.execute(select(User).order_by(User.created_at.asc()))
    users = list(rows.scalars().all())
    return {
        "total": len(users),
        "items": [
            {
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "role": u.role,
                "is_active": u.is_active,
                "mfa_enabled": bool(u.mfa_secret),
                "locale": u.locale,
            }
            for u in users
        ]
    }


@router.post("/users")
async def create_user(
    payload: UserCreate, session: DbDep, user: AdminUser
) -> dict[str, Any]:
    email, password, role, name = payload.email, payload.password, payload.role, payload.name
    if role not in tuple(UserRole):
        raise ValidationFailed("invalid role")
    existing = await session.execute(select(User).where(User.email == email.strip().lower()))
    if existing.scalars().first() is not None:
        raise ValidationFailed("user already exists")
    new_user = User(
        email=email.strip().lower(),
        name=name or email.split("@")[0],
        password_hash=hash_password(password),
        role=role,
        locale="en",
    )
    session.add(new_user)
    await session.flush()
    await audit.record(
        session, action="user_created", entity_type="user", entity_id=new_user.id,
        actor_id=user.id, actor_label=user.email, detail={"role": role},
    )
    return {"user": {"id": new_user.id, "email": new_user.email, "role": new_user.role}}


@router.post("/users/me/mfa")
async def enable_mfa(session: DbDep, user: AnyUser) -> dict[str, Any]:
    secret = generate_totp_secret()
    db_user = await session.get(User, user.id)
    if db_user is None:
        raise NotFound("user not found")
    db_user.mfa_secret = secret
    await session.flush()
    await audit.record(
        session, action="mfa_enabled", entity_type="user", entity_id=user.id, actor_id=user.id,
        actor_label=user.email,
    )
    return {"secret": secret, "provisioning_uri": totp_provisioning_uri(secret, user.email)}


@router.post("/users/{user_id}/deactivate")
async def deactivate_user(user_id: str, session: DbDep, user: AdminUser) -> dict[str, Any]:
    target = await session.get(User, user_id)
    if target is None:
        raise NotFound("user not found")
    if target.id == user.id:
        raise ValidationFailed("you cannot deactivate yourself")
    target.is_active = False
    await session.flush()
    await audit.record(
        session, action="user_deactivated", entity_type="user", entity_id=target.id,
        actor_id=user.id, actor_label=user.email,
    )
    return {"id": target.id, "is_active": False}
