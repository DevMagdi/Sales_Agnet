"""Identity, configuration and audit tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, OrgScopedMixin, TimestampMixin, UUIDPkMixin


class User(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="sales", nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(64))
    locale: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[str | None] = mapped_column(String(36))


class Setting(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    """DB overrides for env settings (mode, kill switch, budgets) — audited on every change."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(36))


class AuditLog(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "audit_log"

    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_label: Mapped[str] = mapped_column(String(200), default="system", nullable=False)
    action: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_audit_log_created_at", "created_at"),)


class Counter(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    """Durable small counters (daily per-mailbox send counts, idempotent send keys)."""

    __tablename__ = "counters"

    key: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    bucket: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    value: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (UniqueConstraint("key", "bucket", name="uq_counters_key_bucket"),)
