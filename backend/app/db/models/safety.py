"""Suppression, consent, LLM calls, API costs and outbound webhook deliveries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, OrgScopedMixin, TimestampMixin, UUIDPkMixin


class SuppressionEntry(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    """Append-only. Hashes only: the raw value is never stored.

    Database triggers created by the migration reject UPDATE/DELETE on this table.
    """

    __tablename__ = "suppression_list"

    kind: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    value_normalized: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(36))
    created_by: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (UniqueConstraint("kind", "value_hash", name="uq_suppression_kind_hash"),)


class ConsentRecord(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "consent_records"

    phone_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), default="whatsapp", nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LLMCall(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "llm_calls"

    task: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validated: Mapped[bool] = mapped_column(default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    payload_request: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    payload_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ApiCost(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "api_costs"

    provider: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    unit: Mapped[str] = mapped_column(String(40), default="call", nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WebhookDelivery(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    """Idempotency ledger for inbound webhooks (provider event ids)."""

    __tablename__ = "webhook_deliveries"

    provider: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    event_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    processed: Mapped[bool] = mapped_column(default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),)


class OutboxEvent(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    """Dashboard live-feed events (SSE). Append-only, trimmed by the retention job."""

    __tablename__ = "outbox_events"

    seq: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (Index("ix_outbox_events_created", "created_at"),)
